"""Batch runner for a manifest of pre-generated LightningSim traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from .manifest import ManifestError, load_manifest, resolve_solution_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run SGRM for every pre-generated trace in a manifest.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--epsilon", type=float, default=0.0)
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=1800.0,
        help="maximum wall time for one design (default: 1800 seconds)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first failed design instead of recording all failures",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.budget < 1:
        raise SystemExit("--budget must be positive")
    if args.epsilon < 0:
        raise SystemExit("--epsilon must be non-negative")
    if args.timeout_s <= 0:
        raise SystemExit("--timeout-s must be positive")

    try:
        manifest_path, manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        raise SystemExit(str(exc)) from exc

    output_dir = args.output_dir.expanduser().resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"could not create output directory: {exc}") from exc
    started = time.perf_counter()
    records: list[dict] = []

    for entry in manifest["designs"]:
        design = entry["design"]
        result_path = output_dir / f"{design}.json"
        try:
            solution_dir = resolve_solution_dir(manifest_path, entry)
        except ManifestError as exc:
            record = {
                "design": design,
                "status": "failed",
                "error": str(exc),
            }
            records.append(record)
            print(f"FAIL {design}: {exc}", file=sys.stderr)
            if args.fail_fast:
                break
            continue
        command = [
            sys.executable,
            "-m",
            "sgrm.cli",
            "--solution-dir",
            str(solution_dir),
            "--design",
            design,
            "--budget",
            str(args.budget),
            "--seed",
            str(args.seed),
            "--epsilon",
            str(args.epsilon),
            "--expected-trace-sha256",
            entry["trace_sha256"],
            "--output",
            str(result_path),
        ]
        if args.verbose:
            command.append("--verbose")

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=args.timeout_s,
            )
        except subprocess.TimeoutExpired:
            record = {
                "design": design,
                "status": "failed",
                "error": f"search exceeded {args.timeout_s:g} seconds",
            }
            print(f"FAIL {design}: {record['error']}", file=sys.stderr)
        else:
            error = (completed.stderr or completed.stdout).strip()
            if completed.returncode == 0:
                record = {
                    "design": design,
                    "status": "passed",
                    "result": result_path.name,
                }
                print(f"PASS {design}")
            else:
                record = {
                    "design": design,
                    "status": "failed",
                    "returncode": completed.returncode,
                    "error": error[-4000:],
                }
                print(f"FAIL {design}: {error}", file=sys.stderr)
        records.append(record)
        if record["status"] == "failed" and args.fail_fast:
            break

    index = {
        "schema_version": 1,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "budget": args.budget,
        "seed": args.seed,
        "epsilon": args.epsilon,
        "timeout_s": args.timeout_s,
        "wall_time_s": time.perf_counter() - started,
        "results": records,
    }
    try:
        (output_dir / "index.json").write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise SystemExit(f"could not write batch index: {exc}") from exc
    return 1 if any(record["status"] == "failed" for record in records) else 0


if __name__ == "__main__":
    sys.exit(main())
