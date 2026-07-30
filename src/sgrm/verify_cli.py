"""Verify trace-search result JSON files against a bundle manifest."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from .manifest import ManifestError, load_manifest
from .reproduction import (
    VCK190_CAPACITY_BRAM,
    VCK190_CAPACITY_FF,
    VCK190_CAPACITY_LUT,
    VCK190_CAPACITY_URAM,
)


RESOURCE_KEYS = ("bram", "uram", "ff", "lut")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate SGRM result files and reference values.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    return parser


def _close(actual: float, expected: float) -> bool:
    return (
        math.isfinite(actual)
        and math.isfinite(expected)
        and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
    )


def _compare_subset(actual, expected, path: str, errors: list[str]) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            errors.append(f"{path}: expected an object")
            return
        for key, value in expected.items():
            if key not in actual:
                errors.append(f"{path}.{key}: missing")
            else:
                _compare_subset(actual[key], value, f"{path}.{key}", errors)
    elif isinstance(expected, float):
        if (
            not isinstance(actual, (int, float))
            or isinstance(actual, bool)
            or not _close(actual, expected)
        ):
            errors.append(f"{path}: expected {expected!r}, got {actual!r}")
    elif actual != expected:
        errors.append(f"{path}: expected {expected!r}, got {actual!r}")


def _cutil(point: dict) -> float:
    return (
        point["bram"] / VCK190_CAPACITY_BRAM
        + point["uram"] / VCK190_CAPACITY_URAM
        + point["ff"] / VCK190_CAPACITY_FF
        + point["lut"] / VCK190_CAPACITY_LUT
    ) / 4.0


def verify_result(result: dict, entry: dict) -> list[str]:
    """Return all structural, feasibility, formula, and reference errors."""

    errors: list[str] = []
    if not isinstance(result, dict):
        return ["result must be a JSON object"]
    if result.get("schema_version") != 1:
        errors.append("result.schema_version must be 1")
    if result.get("design") != entry["design"]:
        errors.append("result.design does not match the manifest")
    if result.get("trace_sha256", "").lower() != entry["trace_sha256"].lower():
        errors.append("result.trace_sha256 does not match the manifest")

    baseline = result.get("baseline")
    selected = result.get("selected")
    if not isinstance(baseline, dict) or not isinstance(selected, dict):
        errors.append("result must contain baseline and selected objects")
        return errors

    for label, point in (("baseline", baseline), ("selected", selected)):
        if point.get("deadlock") is not False:
            errors.append(f"{label}.deadlock must be false")
        for key in RESOURCE_KEYS:
            value = point.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{label}.{key} must be a non-negative integer")
        if all(
            isinstance(point.get(key), int) and not isinstance(point.get(key), bool)
            for key in RESOURCE_KEYS
        ):
            raw_sum = sum(point[key] for key in RESOURCE_KEYS)
            if point.get("raw_resource_sum") != raw_sum:
                errors.append(f"{label}.raw_resource_sum is inconsistent")
            if (
                not isinstance(point.get("cutil"), (int, float))
                or isinstance(point.get("cutil"), bool)
                or not _close(point["cutil"], _cutil(point))
            ):
                errors.append(f"{label}.cutil is inconsistent")

    baseline_latency = baseline.get("latency")
    selected_latency = selected.get("latency")
    epsilon = result.get("epsilon")
    latency_values = (baseline_latency, selected_latency, epsilon)
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in latency_values
    ):
        errors.append(
            "baseline latency, selected latency, and epsilon must be finite numeric values"
        )
    elif epsilon < 0:
        errors.append("epsilon must be non-negative")
    elif selected_latency > baseline_latency * (1.0 + epsilon) + 1e-12:
        errors.append("selected result violates the latency constraint")

    evaluations = result.get("evaluations")
    selected_evaluation = result.get("selected_evaluation")
    if (
        not isinstance(evaluations, int)
        or not isinstance(selected_evaluation, int)
        or isinstance(evaluations, bool)
        or isinstance(selected_evaluation, bool)
        or not 1 <= selected_evaluation <= evaluations
    ):
        errors.append("selected_evaluation must be a one-based evaluated-point index")

    baseline_raw_sum = baseline.get("raw_resource_sum")
    selected_raw_sum = selected.get("raw_resource_sum")
    if (
        isinstance(baseline_raw_sum, int)
        and baseline_raw_sum > 0
        and isinstance(selected_raw_sum, int)
    ):
        expected_raw_reduction = 100.0 * (
            baseline_raw_sum - selected_raw_sum
        ) / baseline_raw_sum
        if not isinstance(
            result.get("raw_resource_reduction_pct"), (int, float)
        ) or not _close(
            result["raw_resource_reduction_pct"], expected_raw_reduction
        ):
            errors.append("raw_resource_reduction_pct is inconsistent")
    baseline_cutil = baseline.get("cutil")
    selected_cutil = selected.get("cutil")
    if (
        isinstance(baseline_cutil, (int, float))
        and baseline_cutil > 0
        and isinstance(selected_cutil, (int, float))
    ):
        expected_cutil_reduction = 100.0 * (
            baseline_cutil - selected_cutil
        ) / baseline_cutil
        if not isinstance(
            result.get("cutil_reduction_pct"), (int, float)
        ) or not _close(
            result["cutil_reduction_pct"], expected_cutil_reduction
        ):
            errors.append("cutil_reduction_pct is inconsistent")

    expected = entry.get("expected")
    if expected is not None:
        _compare_subset(result, expected, "result", errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _, manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        raise SystemExit(str(exc)) from exc

    results_dir = args.results_dir.expanduser().resolve()
    failures = 0
    for entry in manifest["designs"]:
        design = entry["design"]
        result_path = results_dir / f"{design}.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"FAIL {design}: could not load {result_path}: {exc}")
            failures += 1
            continue
        errors = verify_result(result, entry)
        if errors:
            print(f"FAIL {design}")
            for error in errors:
                print(f"  - {error}")
            failures += 1
        else:
            print(f"PASS {design}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
