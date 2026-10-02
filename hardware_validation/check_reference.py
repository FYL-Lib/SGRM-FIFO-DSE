#!/usr/bin/env python3
"""Check a completed three-design hardware run against measured references.

Reads existing CSV/JSON outputs only. Does not invoke hardware tools or write
results. Reference values are comparison targets, never replacement evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


REFERENCE = Path(__file__).resolve().with_name("three_design_reference.json")
RESOURCES = ("bram", "uram", "ff", "lut")


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid numeric value for {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid numeric value for {field}: {value!r}") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"invalid numeric value for {field}: {value!r}")
    return parsed


def check(output: Path) -> list[str]:
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    designs = {item["design"]: item for item in reference["designs"]}
    count = len(designs)
    expected_summary = {
        "status": "PASS",
        "requested_design_count": count,
        "completed_pair_count": count,
        "requested_hardware_job_count": count * 2,
        "successful_hardware_job_count": count * 2,
        "partial_aggregate": False,
        "capacities": reference["capacities"],
    }
    for key, expected in expected_summary.items():
        if summary.get(key) != expected:
            raise ValueError(f"summary {key}: expected {expected!r}, got {summary.get(key)!r}")

    measured = {}
    for row in load_csv(output / "hardware_resources.csv"):
        identity = (row.get("design"), row.get("variant"))
        if identity in measured:
            raise ValueError(f"duplicate hardware row: {identity}")
        measured[identity] = row
    identities = {(name, variant) for name in designs for variant in ("native", "sgrm")}
    if set(measured) != identities:
        raise ValueError("hardware rows must cover exactly bicg/gemm/ResidualBlock, native and sgrm")

    pairs = {}
    for row in load_csv(output / "paired_comparison.csv"):
        name = row.get("design")
        if name in pairs:
            raise ValueError(f"duplicate comparison row: {name}")
        pairs[name] = row
    if set(pairs) != set(designs):
        raise ValueError("comparison rows must cover exactly the three reference designs")

    messages = []
    for name, expected in designs.items():
        costs = {}
        for variant in ("native", "sgrm"):
            row = measured[(name, variant)]
            if row.get("status") != "OK":
                raise ValueError(f"{name}/{variant}: hardware job is not OK")
            for resource in RESOURCES:
                field = "fifo_" + resource
                actual = number(row.get(field), f"{name}/{variant}/{field}")
                target = expected[variant][resource]
                if actual != target:
                    raise ValueError(f"{name}/{variant} {resource}: expected {target}, got {actual:g}")
            if number(row.get("fifo_n_fifo"), "FIFO count") != expected["fifo_count"]:
                raise ValueError(f"{name}/{variant}: FIFO count does not match the reference")
            cost = sum(
                number(row["fifo_" + resource], resource) / reference["capacities"][resource]
                for resource in RESOURCES
            ) / 4
            costs[variant] = cost
            actual_cost = number(row.get("fifo_cutil"), f"{name}/{variant}/fifo_cutil")
            if not math.isclose(actual_cost, cost, rel_tol=1e-10, abs_tol=1e-12):
                raise ValueError(f"{name}/{variant}: inconsistent fifo_cutil")
            pair_cost = number(pairs[name].get(variant + "_fifo_cutil"), "paired Cutil")
            if not math.isclose(pair_cost, cost, rel_tol=1e-10, abs_tol=1e-12):
                raise ValueError(f"{name}: paired {variant} Cutil is inconsistent")
        reduction = 100 * (1 - costs["sgrm"] / costs["native"])
        pair_reduction = number(pairs[name].get("fifo_subsystem_reduction_pct"), "reduction")
        for actual in (pair_reduction, expected["fifo_subsystem_reduction_pct"]):
            if not math.isclose(actual, reduction, rel_tol=1e-10, abs_tol=1e-8):
                raise ValueError(f"{name}: resource reduction does not match the reference")
        messages.append(f"PASS {name}: measured FIFO-subsystem reduction {reduction:.4f}%")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("results/three-design-hardware"))
    args = parser.parse_args(argv)
    try:
        messages = check(args.output_dir.expanduser().resolve())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL reference check: {exc}")
        return 1
    for message in messages:
        print(message)
    print(f"REFERENCE MATCH: {len(messages)} paired designs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
