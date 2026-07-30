"""Canonical configuration and result helpers for SGRM trace searches."""

from __future__ import annotations

import os

from .interfaces import EvalResult


VCK190_CAPACITY_BRAM = 967
VCK190_CAPACITY_URAM = 463
VCK190_CAPACITY_FF = 1_799_680
VCK190_CAPACITY_LUT = 899_840

CANONICAL_ENVIRONMENT = {
    "SGRM_COST_MODE": "util",
    "SGRM_IMPL_TYPE_MOVES": "1",
    "SGRM_DEPTH_BASED_IMPL": "1",
    "SGRM_START_FROM_SMALL": "1",
    "SGRM_DEPTH_HALVE_NEIGHBOR": "1",
    "SGRM_STAGE5_HALVE_REFINE": "1",
    "SGRM_STAGES": "1,2,3,4",
}


def configure_canonical_search(*, overwrite: bool = True) -> None:
    """Apply the reference configuration for the four-stage search."""

    for name, value in CANONICAL_ENVIRONMENT.items():
        if overwrite or name not in os.environ:
            os.environ[name] = value


def vck190_cutil(result: EvalResult) -> float:
    """Return the four-resource VCK190-normalized utilization objective."""

    if result.deadlock or result.latency is None:
        return float("inf")
    resources = (
        result.bram_usage_total,
        result.uram_usage_total,
        result.ff_usage_total,
        result.lut_usage_total,
    )
    if any(value is None for value in resources):
        raise ValueError("Cutil requires BRAM, URAM, FF, and LUT measurements")
    return (
        result.bram_usage_total / VCK190_CAPACITY_BRAM
        + result.uram_usage_total / VCK190_CAPACITY_URAM
        + result.ff_usage_total / VCK190_CAPACITY_FF
        + result.lut_usage_total / VCK190_CAPACITY_LUT
    ) / 4.0
