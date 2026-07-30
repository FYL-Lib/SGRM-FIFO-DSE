"""Build a portable manifest for a directory of pre-generated traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hash a trace bundle and write its manifest.json.",
    )
    parser.add_argument(
        "--trace-root",
        type=Path,
        required=True,
        help="directory containing <design>/hls_<design>/solution1/trace.pkl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output manifest; defaults to <trace-root>/manifest.json",
    )
    parser.add_argument(
        "--expected-results-dir",
        type=Path,
        help="optionally embed reference fields from <design>.json results",
    )
    parser.add_argument(
        "--design-list",
        type=Path,
        help="optional newline-delimited design allowlist",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_subset(result: dict) -> dict:
    point_keys = (
        "deadlock",
        "latency",
        "bram",
        "uram",
        "ff",
        "lut",
        "cutil",
        "raw_resource_sum",
    )
    return {
        "fifo_count": result["fifo_count"],
        "group_count": result["group_count"],
        "evaluations": result["evaluations"],
        "selected_evaluation": result["selected_evaluation"],
        "cutil_reduction_pct": result["cutil_reduction_pct"],
        "raw_resource_reduction_pct": result["raw_resource_reduction_pct"],
        "baseline": {key: result["baseline"][key] for key in point_keys},
        "selected": {key: result["selected"][key] for key in point_keys},
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trace_root = args.trace_root.expanduser().resolve()
    if not trace_root.is_dir():
        raise SystemExit(f"trace root is not a directory: {trace_root}")
    output = (
        args.output.expanduser().resolve()
        if args.output
        else trace_root / "manifest.json"
    )
    if output.parent != trace_root:
        raise SystemExit("--output must be directly inside --trace-root")

    expected_results_dir = (
        args.expected_results_dir.expanduser().resolve()
        if args.expected_results_dir
        else None
    )
    selected_designs: list[str] | None = None
    if args.design_list:
        try:
            selected_designs = [
                line.strip()
                for line in args.design_list.expanduser().read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except OSError as exc:
            raise SystemExit(f"could not read --design-list: {exc}") from exc
        if not selected_designs or len(selected_designs) != len(set(selected_designs)):
            raise SystemExit("--design-list must contain unique design names")

    entries_by_design: dict[str, dict] = {}
    for trace_path in sorted(trace_root.glob("*/hls_*/solution1/trace.pkl")):
        relative = trace_path.relative_to(trace_root)
        if len(relative.parts) != 4:
            continue
        design, hls_directory, solution, filename = relative.parts
        if hls_directory != f"hls_{design}" or solution != "solution1":
            continue
        if filename != "trace.pkl":
            continue

        entry = {
            "design": design,
            "solution_dir": (relative.parent).as_posix(),
            "trace_sha256": _sha256(trace_path),
            "metadata": {
                "trace_bytes": trace_path.stat().st_size,
            },
        }
        hls_app = trace_path.parents[1] / "hls.app"
        if hls_app.is_file():
            entry["metadata"]["hls_app_sha256"] = _sha256(hls_app)
        entries_by_design[design] = entry

    if selected_designs is not None:
        missing = [design for design in selected_designs if design not in entries_by_design]
        if missing:
            raise SystemExit(f"listed designs have no trace: {missing}")
        entries = [entries_by_design[design] for design in selected_designs]
    else:
        entries = [entries_by_design[design] for design in sorted(entries_by_design)]

    if expected_results_dir is not None:
        for entry in entries:
            design = entry["design"]
            result_path = expected_results_dir / f"{design}.json"
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                entry["expected"] = _expected_subset(result)
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise SystemExit(
                    f"could not read reference result for {design}: {exc}"
                ) from exc

    if not entries:
        raise SystemExit(
            "no traces matched <design>/hls_<design>/solution1/trace.pkl"
        )
    manifest = {
        "schema_version": 1,
        "runtime": {"python": "3.12", "lightningsim": "0.2.6"},
        "designs": entries,
    }
    try:
        output.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise SystemExit(f"could not write manifest: {exc}") from exc
    print(f"{output} ({len(entries)} designs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
