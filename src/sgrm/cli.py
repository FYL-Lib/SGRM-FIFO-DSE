"""Command-line entry point for SGRM trace replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import sys
import time
from pathlib import Path

from .lightningsim_backend import LightningSimTraceBackend
from .reproduction import (
    CANONICAL_ENVIRONMENT,
    configure_canonical_search,
    vck190_cutil,
)
from .sgrm import SGRMOptimizer


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result_payload(result) -> dict:
    resource_values = (
        result.bram_usage_total,
        result.uram_usage_total,
        result.ff_usage_total,
        result.lut_usage_total,
    )
    raw_resource_sum = (
        sum(value for value in resource_values if value is not None)
        if all(value is not None for value in resource_values)
        else None
    )
    return {
        "deadlock": bool(result.deadlock),
        "latency": result.latency,
        "bram": result.bram_usage_total,
        "uram": result.uram_usage_total,
        "ff": result.ff_usage_total,
        "lut": result.lut_usage_total,
        "cutil": vck190_cutil(result),
        "raw_resource_sum": raw_resource_sum,
        "fifo_depths": {str(k): int(v) for k, v in result.fifo_sizes.items()},
        "fifo_impl_types": (
            {str(k): str(v) for k, v in result.fifo_impl_types.items()}
            if result.fifo_impl_types is not None
            else None
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run canonical SGRM search from a pre-generated LightningSim trace.",
    )
    parser.add_argument(
        "--solution-dir",
        type=Path,
        required=True,
        help="solution directory containing trace.pkl",
    )
    parser.add_argument("--design", help="design label stored in the JSON output")
    parser.add_argument("--budget", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--epsilon", type=float, default=0.0)
    parser.add_argument("--output", type=Path, help="write result JSON to this path")
    parser.add_argument(
        "--expected-trace-sha256",
        help="abort if trace.pkl does not match this SHA-256 digest",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.budget < 1:
        raise SystemExit("--budget must be positive")
    if args.epsilon < 0:
        raise SystemExit("--epsilon must be non-negative")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    configure_canonical_search(overwrite=True)

    solution_dir = args.solution_dir.expanduser().resolve()
    trace_path = solution_dir / "trace.pkl"
    if not trace_path.is_file():
        raise SystemExit(f"pre-generated trace not found: {trace_path}")
    try:
        trace_sha256 = _sha256(trace_path)
    except OSError as exc:
        raise SystemExit(f"could not read pre-generated trace: {exc}") from exc
    if (
        args.expected_trace_sha256
        and trace_sha256.lower() != args.expected_trace_sha256.lower()
    ):
        raise SystemExit(
            "trace SHA-256 mismatch: "
            f"expected {args.expected_trace_sha256}, got {trace_sha256}"
        )
    if not args.expected_trace_sha256:
        logging.warning(
            "trace.pkl uses Python pickle; load only a trusted trace and pass "
            "--expected-trace-sha256 when a reference digest is available"
        )

    started = time.perf_counter()
    backend = LightningSimTraceBackend(solution_dir)
    optimizer = SGRMOptimizer(
        backend,
        epsilon=args.epsilon,
        budget=args.budget,
        seed=args.seed,
    )
    points = optimizer.solve()
    elapsed = time.perf_counter() - started
    best = optimizer.get_best_feasible()
    if not points or best is None:
        raise SystemExit("search produced no feasible result")

    baseline = points[0]
    selected_eval = next(
        index for index, point in enumerate(points, start=1) if point is best
    )
    baseline_cutil = vck190_cutil(baseline)
    best_cutil = vck190_cutil(best)
    reduction = (
        100.0 * (baseline_cutil - best_cutil) / baseline_cutil
        if baseline_cutil > 0
        else 0.0
    )
    baseline_raw_sum = _result_payload(baseline)["raw_resource_sum"]
    best_raw_sum = _result_payload(best)["raw_resource_sum"]
    if baseline_raw_sum is None or best_raw_sum is None:
        raise SystemExit("feasible result is missing one or more resource measurements")
    raw_reduction = (
        100.0 * (baseline_raw_sum - best_raw_sum) / baseline_raw_sum
        if baseline_raw_sum > 0
        else 0.0
    )
    payload = {
        "schema_version": 1,
        "design": args.design or solution_dir.parent.name,
        "solution_dir": str(solution_dir),
        "trace_sha256": trace_sha256,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "seed": args.seed,
        "budget": args.budget,
        "epsilon": args.epsilon,
        "canonical_environment": dict(CANONICAL_ENVIRONMENT),
        "fifo_count": len(backend.fifos),
        "group_count": len({fifo.get_display_name() for fifo in backend.fifos}),
        "evaluations": len(points),
        "selected_evaluation": selected_eval,
        "wall_time_s": elapsed,
        "cutil_reduction_pct": reduction,
        "raw_resource_reduction_pct": raw_reduction,
        "baseline": _result_payload(baseline),
        "selected": _result_payload(best),
    }

    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        output = args.output.expanduser().resolve()
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"could not write result JSON: {exc}") from exc
        print(output)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
