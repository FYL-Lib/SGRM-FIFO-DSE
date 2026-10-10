#!/usr/bin/env python3
"""Check or run the original Stream-HLS synthesis with a verified AMD launcher."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

if __package__:
    from .generate_trace import validate_completed_synthesis
    from .tooling import ROOT, find_hls, hardware_module
else:
    from generate_trace import validate_completed_synthesis
    from tooling import ROOT, find_hls, hardware_module


DEFAULT_SOURCES = ROOT / "sgrm-stream-hls-30-sources-v0.1.0"


def source_project(sources_dir: Path, design: str) -> Path:
    """Check the published source contracts before using the original Tcl."""
    sources_dir = sources_dir.expanduser().resolve()
    if not (sources_dir / "manifest.json").is_file():
        raise FileNotFoundError(
            f"Source bundle not found at {sources_dir}. From the repository root, run:\n"
            "python datasets/check_bundles.py\n"
            "tar -xJf datasets/sgrm-stream-hls-30-sources-v0.1.0.tar.xz\n"
            "Use --sources-dir if you extracted it elsewhere."
        )
    tools = hardware_module()
    manifest = tools.load_source_manifest(sources_dir)
    entry = next((item for item in manifest["designs"] if item["design"] == design), None)
    if entry is None:
        raise ValueError(f"Unknown Stream-HLS design: {design}. See datasets/stream_hls_30.txt.")
    project = tools.checked_path(sources_dir, entry["directory"])
    required = ("hls.tcl", f"src/{design}.cpp", f"src/{design}_tb.cpp")
    for relative in required:
        if relative not in entry["files"] or not (project / relative).is_file():
            raise ValueError(f"The source manifest does not cover the required original file: {design}/{relative}")
    return project


def synthesis_command(executable: str, design: str) -> list[str]:
    # The classic 2024.2 positional-script interface leaves Tcl argv as
    # [design, syn]. Adding -f here shifts those arguments in the AMD launcher.
    return [executable, "-l", "hls_synthesis.log", "hls.tcl", design, "syn"]


def invoke_hls(command: list[str], project: Path, environment: dict[str, str]) -> int:
    """Stream AMD output while leaving the parent shell/environment unchanged."""
    child = dict(environment, PWD=str(project))
    process = subprocess.run(command, cwd=project, env=child,
                             stdin=subprocess.DEVNULL, check=False)
    return process.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("check-tools", "plan", "run"), default="plan",
                        help="Default: plan. Only run starts HLS synthesis.")
    parser.add_argument("--design", help="One original design from datasets/stream_hls_30.txt")
    parser.add_argument("--sources-dir", type=Path,
                        help="Extracted original bundle; defaults to this checkout's source directory")
    parser.add_argument("--vitis-hls", help="Explicit classic Vitis HLS 2024.2 executable, not a directory")
    parser.add_argument("--tool-root", action="append", type=Path,
                        help="AMD installation base to search; may be repeated")
    parser.add_argument("--settings", action="append", type=Path, default=[],
                        help="Additional trusted AMD/site settings file; may be repeated")
    args = parser.parse_args(argv)
    if args.stage == "check-tools" and (args.design or args.sources_dir):
        parser.error("check-tools does not use source files; omit --design/--sources-dir")
    if args.stage != "check-tools" and not args.design:
        parser.error("--design is required for plan or run")

    project = None
    if args.stage != "check-tools":
        project = source_project(args.sources_dir or DEFAULT_SOURCES, args.design)

    executable, environment = find_hls(args.vitis_hls, args.tool_root, args.settings, dict(os.environ))
    print(f"FOUND classic Vitis HLS 2024.2: {executable}", flush=True)
    print(f"HLS runtime: {environment['XILINX_HLS']}", flush=True)
    if args.stage == "check-tools":
        print("TOOL CHECK PASS: no synthesis started. Vivado is not required.\n"
              "Licenses and VCK190 device support are checked during synthesis.", flush=True)
        return 0

    command = synthesis_command(executable, args.design)
    solution = project / f"hls_{args.design}" / "solution1"
    print(f"DESIGN: {args.design}\nWORK DIRECTORY: {project}\n"
          f"COMMAND: {shlex.join(command)}\nSOLUTION: {solution}", flush=True)
    if args.stage == "plan":
        print("PLAN PASS: sources and tool interface checked; no synthesis started.\n"
              "Use --stage run with the same options to start HLS C synthesis.", flush=True)
        return 0

    # Never silently reuse or overwrite earlier hardware evidence.
    for path in (solution.parent, project / "hls_synthesis.log"):
        if path.exists():
            raise FileExistsError(
                f"Refusing to reuse or overwrite existing synthesis output: {path}. "
                "For a completed project, use trace_generation/generate_trace.py directly. "
                "For a separate synthesis run, extract a fresh source copy and use --sources-dir."
            )
    print("RUNNING HLS C synthesis only; no RTL co-simulation or Vivado implementation.", flush=True)
    returncode = invoke_hls(command, project, environment)
    if returncode:
        raise RuntimeError(
            f"HLS synthesis failed (exit {returncode}). Inspect {project / 'hls_synthesis.log'}. "
            "No trace was exported."
        )
    validate_completed_synthesis(solution, "forward")
    print(f"SYNTHESIZED {args.design}: {solution}\n"
          "Next: export a fresh trace using trace_generation/generate_trace.py.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
