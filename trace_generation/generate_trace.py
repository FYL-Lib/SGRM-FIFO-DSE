#!/usr/bin/env python3
"""Export a fresh trace using the complete included local LightningSim fork."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import pickle
import resource
import sys
import tempfile
import time
from xml.etree import ElementTree

if __package__:
    from .build_local import FORK, build_environment
    from .tooling import explicit_runtime, find_hls
else:
    from build_local import FORK, build_environment
    from tooling import explicit_runtime, find_hls


def required_synthesis_outputs(solution_dir: Path, kernel: str) -> list[Path]:
    return [solution_dir.parent / "hls.app",
            solution_dir / "syn/report" / f"{kernel}_csynth.xml",
            solution_dir / ".autopilot/db/a.o.3.bc",
            solution_dir / ".autopilot/db" / f"mapper_{kernel}.cpp",
            solution_dir / ".autopilot/db" / f"apatb_{kernel}_ir.bc"]


def validate_completed_synthesis(solution_dir: Path, kernel: str) -> None:
    missing = [str(path) for path in required_synthesis_outputs(solution_dir, kernel)
               if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise ValueError("Complete HLS C synthesis before exporting a trace. Missing:\n" + "\n".join(missing))
    try:
        report = ElementTree.parse(solution_dir / "syn/report" / f"{kernel}_csynth.xml")
    except ElementTree.ParseError as exc:
        raise ValueError("The completed HLS synthesis report is not valid XML.") from exc
    if report.findtext("./ReportVersion/Version", "").strip() != "2024.2":
        raise ValueError("This workflow requires a completed Vitis HLS 2024.2 synthesis report.")


def export_status(returncode: int) -> str:
    return "exported" if returncode == 0 else "exported_with_testbench_failure"


def write_new_pickle(trace, path: Path) -> None:
    """Publish a fully written pickle without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".sgrm-trace-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            pickle.dump(trace, stream, protocol=pickle.HIGHEST_PROTOCOL)
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_new_json(value, path: Path) -> None:
    """Write a new audit log without replacing an earlier run's evidence."""
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def fifo_metadata(trace) -> list[dict]:
    fifos = []
    seen = set()
    for fifo in trace.fifos:
        if fifo.id in seen or fifo.id not in trace.params.fifo_depths:
            raise RuntimeError("The resolved trace has duplicate IDs or missing FIFO depths.")
        seen.add(fifo.id)
        depth = trace.params.fifo_depths[fifo.id]
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
            raise RuntimeError("SGRM requires finite, positive baseline FIFO depths.")
        if fifo.width <= 0:
            raise RuntimeError("The resolved trace has invalid FIFO widths.")
        fifos.append({"id": int(fifo.id), "name": str(fifo.name),
                      "width": int(fifo.width), "depth": depth})
    if not fifos:
        raise RuntimeError("The resolved trace has no FIFOs.")
    return fifos


async def generate(args) -> int:
    output = args.output.resolve() if args.output else args.solution_dir.resolve() / "trace.pkl"
    audit_path = output.with_suffix(".generation.json")
    process_path = output.with_suffix(".processes.json")
    for path in (output, audit_path, process_path):
        if path.exists():
            raise FileExistsError(f"Refusing to reuse or overwrite an existing trace or audit log: {path}")
    prefix, environment = build_environment()
    build_record = prefix / "share/lightningsim/sgrm-build.json"
    if not build_record.is_file():
        raise RuntimeError("Build the included fork first: python trace_generation/build_local.py")
    build = json.loads(build_record.read_text())
    if Path(build["local_source_directory"]).resolve() != FORK.resolve():
        raise RuntimeError("This environment belongs to a different LightningSim checkout; rebuild this one.")
    if args.hls_root:
        environment = explicit_runtime(args.hls_root, args.settings, environment)
        hls = None  # Completed-project export does not execute an HLS binary.
    else:
        hls, environment = find_hls(args.vitis_hls, args.tool_root, args.settings, environment)
    solution_dir = args.solution_dir.resolve()
    environment["PRJ_PATH"] = str(args.project_dir.resolve() if args.project_dir else solution_dir.parent.parent)
    environment.setdefault("RAYON_NUM_THREADS", "1")
    # Replace this child process's environment, rather than leaving stale
    # library/Python paths that build_environment deliberately removed.
    os.environ.clear()
    os.environ.update(environment)
    soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
    desired = 256 * 1024 * 1024 if hard == resource.RLIM_INFINITY else min(256 * 1024 * 1024, hard)
    if soft != resource.RLIM_INFINITY and soft < desired:
        resource.setrlimit(resource.RLIMIT_STACK, (desired, hard))

    sys.path.insert(0, str(FORK / "backend"))
    import lightningsim
    import lightningsim._core
    from lightningsim.model import Solution
    from lightningsim.runner import Runner
    if __package__:
        from .lightningsim_compat import apply_patches
    else:
        from lightningsim_compat import apply_patches

    backend = FORK / "backend"
    for module in (lightningsim, lightningsim._core):
        if not Path(module.__file__).resolve().is_relative_to(backend.resolve()):
            raise RuntimeError("Not importing the included, locally built LightningSim fork.")
    apply_patches()

    class CompletedSolution(Solution):
        async def get_bitcode_path(self):
            # This command is for already completed synthesis, not the GUI's
            # live-synthesis watcher. It never waits for or fabricates dut.hcp.
            path = self.path / ".autopilot/db/a.o.3.bc"
            if not path.is_file() or not path.stat().st_size:
                raise FileNotFoundError(path)
            return path

    solution = CompletedSolution(solution_dir)
    validate_completed_synthesis(solution_dir, solution.kernel_name)
    sys.setrecursionlimit(10000)
    runner = Runner(solution, debug=args.debug)
    for step, progress in runner.steps.items():
        progress.on_start(lambda _, name=step.name: print(f"STEP {name}", flush=True))
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    record = {"status": "failed", "trace_exported": False,
              "generator": "SGRM local LightningSim", "solution_dir": str(solution_dir),
              "hls_executable": hls, "python": sys.version.split()[0],
              "hls_runtime_directory": environment["XILINX_HLS"],
              "synthesis_version": "2024.2",
              "lightningsim_version": importlib.metadata.version("lightningsim"),
              "lightningsim_package": lightningsim.__file__,
              "strict_testbench": args.strict_testbench,
              "existing_trace_loaded": False}
    try:
        trace = await asyncio.wait_for(runner.run(), timeout=args.timeout_s)
        if runner.testbench is None:
            raise RuntimeError("The instrumented testbench did not execute.")
        returncode = runner.testbench.returncode
        record.update(testbench_returncode=returncode, testbench_passed=returncode == 0)
        fifos = fifo_metadata(trace)
        baseline = trace.compiled.dse(trace.params, [{fifo["id"]: fifo["depth"] for fifo in fifos}])[0]
        if baseline.latency is None:
            raise RuntimeError("The original configuration deadlocks in the resolved trace.")
        record.update(baseline_latency=baseline.latency, fifo_count=len(fifos), fifos=fifos)
        if args.strict_testbench:
            runner.testbench.check_returncode()
        write_new_pickle(trace, output)
        record.update(status=export_status(returncode), trace_exported=True, trace_bytes=output.stat().st_size)
        print(f"EXPORTED {output}: {len(fifos)} FIFOs, baseline {baseline.latency} cycles", flush=True)
        return 0
    except BaseException as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record["generation_wall_time_s"] = time.perf_counter() - started
        try:
            write_new_json(record, audit_path)
            write_new_json([
                {"command": process.command, "returncode": process.returncode, "output": process.output}
                for process in runner.processes], process_path)
        except OSError as exc:
            if "error" not in record:
                raise
            print(f"WARNING: unable to write an audit log: {exc}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solution-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--project-dir", type=Path, help="Directory containing workload data; defaults to the HLS project parent")
    parser.add_argument("--vitis-hls")
    parser.add_argument("--hls-root", type=Path,
                        help="2024.2 AMD directory containing include/Makefile.sysc.rules; skips launcher discovery")
    parser.add_argument("--tool-root", action="append", type=Path)
    parser.add_argument("--settings", action="append", type=Path, default=[])
    parser.add_argument("--timeout-s", type=float, default=1800)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--strict-testbench", action="store_true",
                        help="Reject a nonzero testbench exit instead of preserving the original export policy")
    args = parser.parse_args(argv)
    if args.timeout_s <= 0:
        parser.error("--timeout-s must be positive")
    if args.hls_root and (args.vitis_hls or args.tool_root):
        parser.error("Use --hls-root for runtime-only export, or --vitis-hls/--tool-root for launcher discovery.")
    return asyncio.run(generate(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
