"""
Analytical resource prediction model for HLS dataflow FIFOs.

Pure formula - does NOT consult any HLS synthesis artifacts (no bindinfo.xml,
no _csynth.rpt, no per-design offsets). The predictions depend only on
(width, depth, impl_type) for each FIFO.

Storage types - five impls, with a clean separation between search-space
backends and the auto-pick proxy:

  Search-space (SGRM picks among these 4 concrete backends):
    - "srl"    : Shift-register chain (SRL16E/SRL32E primitives in LUT).
                 Cheap for very small FIFOs; for depth>32 Vitis cascades
                 SRL32 chains.
    - "lutram" : SLICEM distributed RAM (RAM32X1D primitive). Storage in
                 LUT, no BRAM/URAM consumed.
    - "bram"   : BRAM18K block RAM. Storage in BRAM, control in LUT/FF.
    - "uram"   : URAM288. Storage in URAM, control in LUT/FF.

  Out of search space, accepted only as a tag for HLS auto-selection:
    - "auto"   : Vitis HLS picks at synthesis time. The predict_*() formulas
                 for "auto" are EMPIRICALLY calibrated against the VCK190
                 2024.2 csynth corpus (30 SGRM finalrun designs, 4781
                 FIFOs): BRAM=0 for srl-class and narrow+very-deep FIFOs
                 (Vitis materialises as LUTRAM), BRAM-storage formula
                 otherwise. NOT a guess - pred-vs-actual r=1.0000 on BRAM,
                 r=0.9972 on Cutil. SGRM never selects "auto" (search
                 space excludes it); it survives only as a tag for the
                 SGRM baseline state and pre-cleanup legacy data.

FF/LUT formulas:
  - srl, bram, uram, auto: refit against VCK190 2024.2 csynth sweep
    (144 obs x 4781 FIFOs across 24 designs in best_points_real_hls_100mhz
    _strict_validation.csv). MAPE: BRAM 0%, FF 4%, LUT 20%.
  - lutram: refit against single-FIFO csynth sweep over (W, D) in {8..128} x
    {16..4096} on VCK190 2024.2. Storage formula `ceil(D/32)*W` from
    Xilinx UG574 Section "Distributed RAM in SLICEM" (RAM32X1D primitive).
    LUT MAPE 19.8%; FF MAPE ~30% (under-counts cascade pipeline at very
    deep widths but SGRM rarely picks lutram in that regime).
"""

from __future__ import annotations

# Optional fitted coefficients can be supplied as an external calibration
# file. Without one, the deterministic analytical formulas remain active.

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ImplType = Literal["srl", "lutram", "bram", "uram", "auto"]
# search-space impls: the four concrete backends SGRM picks among.
_SEARCH_IMPLS: tuple[str, ...] = ("srl", "lutram", "bram", "uram")
# all impls predict_* understands: search impls + the empirically-calibrated
# "auto" path that models Vitis HLS's auto-selection behaviour.
_VALID_IMPLS: tuple[str, ...] = (*_SEARCH_IMPLS, "auto")


def _check_impl(impl_type: str) -> None:
    if impl_type not in _VALID_IMPLS:
        raise ValueError(
            f"impl_type={impl_type!r} not accepted; predict_* takes "
            f"{_VALID_IMPLS}."
        )

# --- Optional calibration layer (SGRM V4) -----------------------------
# Loaded lazily on first use; activated by env var SGRM_USE_CALIBRATION=1.
# Calibration leaves all per-FIFO predictors (predict_bram/uram/ff/lut) and
# legacy callers untouched - only `predict_design_resources()` consults the
# overlay, and only when the env var is set.

_CAL_BUCKETS = ("srl", "bram_ram", "uram_ram")
_CAL_RESOURCES = ("bram", "uram", "ff", "lut")
_CAL_PATH = Path(__file__).resolve().parent / "resource_calibration.json"
_CAL_CACHE: dict | None = None
_CAL_LOADED = False


def _calibration_enabled() -> bool:
    return os.environ.get("SGRM_USE_CALIBRATION", "0") not in ("0", "", "false", "False")


def _load_calibration() -> dict | None:
    global _CAL_CACHE, _CAL_LOADED
    if _CAL_LOADED:
        return _CAL_CACHE
    _CAL_LOADED = True
    if not _CAL_PATH.exists():
        _CAL_CACHE = None
        return None
    try:
        data = json.loads(_CAL_PATH.read_text())
        # Sanity: must have every resource x bucket coefficient.
        for r in _CAL_RESOURCES:
            entry = data["resources"][r]
            assert len(entry["alpha"]) == len(_CAL_BUCKETS)
            assert len(entry["beta"]) == len(_CAL_BUCKETS)
        _CAL_CACHE = data
    except Exception:
        _CAL_CACHE = None
    return _CAL_CACHE


def _bucket_of_impl(impl: str, width: int, depth: int) -> str:
    """Map an impl_type to the calibration bucket."""
    if impl == "srl":
        return "srl"
    if impl == "uram":
        return "uram_ram"
    if impl == "auto":
        # auto -> falls into srl bucket for srl-class FIFOs, else bram_ram.
        return "srl" if infer_fifo_type(width, depth) == "srl" else "bram_ram"
    # bram and lutram both share the bram_ram calibration bucket - LUTRAM has
    # no fitted bucket of its own; its storage-LUT contribution is added
    # directly in predict_lut, outside the calibration overlay.
    return "bram_ram"


def infer_fifo_type(width: int, depth: int) -> Literal["srl", "ram"]:
    """Replicate FifoType::from_width_and_depth from fifo.rs."""
    if depth <= 2 or width * depth <= 1024:
        return "srl"
    return "ram"


_FULL_IMPL_SPACE: list[str] = ["srl", "lutram", "bram", "uram"]


def get_impl_type_space(width: int, depth: int) -> list[str]:
    """Return the search-space impl types for every FIFO.

    Only the four concrete storage backends are returned. `"auto"` is
    intentionally absent: it is a meta-choice meaning "let Vitis HLS pick"
    and so its resource cost is, by definition, unknown at search time -
    SGRM cannot make a deterministic decision against an unknown cost.
    Including `"auto"` in the search would force `predict_*` to mock
    Vitis's heuristic, which (a) is brittle, (b) creates non-monotonic
    cost surfaces (e.g. the auto-elision discontinuity at depth=4096
    width<=36), and (c) muddles the paper narrative - SGRM's job is to
    *replace* HLS auto-selection, not to second-guess it.

    `"auto"` remains valid as the BASELINE impl (see
    `sgrm.SGRM._get_baseline_impl_type`) - there it represents the
    no-DSE reference point where HLS is allowed to pick freely.

    Order: SRL first as the tie-break default; LUTRAM/BRAM/URAM ordered
    by typical specificity (LUTRAM for narrow, BRAM common ram-class,
    URAM only for very wide/deep).
    """
    return list(_FULL_IMPL_SPACE)


def get_valid_impl_types(width: int, depth: int) -> list[ImplType]:
    """Backward-compatible alias for get_impl_type_space()."""
    return get_impl_type_space(width, depth)


def _ceil_log2(n: int) -> int:
    if n <= 1:
        return 0
    return math.ceil(math.log2(n))


def _next_power_of_two(n: int) -> int:
    if n <= 1:
        return 1
    return 1 << (n - 1).bit_length()


# --- Per-FIFO resource functions --------------------------------------
#
# Every predictor accepts impl_type in {"srl", "lutram", "bram", "uram"}
# (the search-space concrete backends) plus the legacy value "auto".
# "auto" is NOT a real storage primitive - it means "Vitis HLS picks at
# synthesis time". Predicting its resource cost requires mocking Vitis's
# internal heuristic, which is brittle and produces non-monotonic cost
# surfaces. We keep the auto branch only for backward compatibility with
# pre-cleanup records that may carry impl=auto. The current `get_impl_type_space()` excludes auto from the
# search space, so SGRM never picks it for new searches.


def _bram_storage_count(width: int, depth: int) -> int:
    """BRAM18K count for a BRAM-backed FIFO. Independent of impl_type."""
    if depth <= 2:
        return 0
    ram_depth = depth - 1
    if width <= 18:
        return math.ceil(ram_depth / 1024)
    if width <= 36:
        blocks = max(1, math.ceil(ram_depth / 512))
        return _next_power_of_two(blocks)
    cols_36k = math.ceil(width / 72)
    rows = math.ceil(ram_depth / 512)
    return 2 * cols_36k * rows


def _uram_storage_count(width: int, depth: int) -> int:
    """URAM288 count for a URAM-backed FIFO."""
    if depth <= 1:
        return 0
    ram_depth = depth - 1
    cols = (width + 71) // 72
    rows = (ram_depth + 4095) // 4096
    return cols * rows


def _lutram_storage_luts(width: int, depth: int) -> int:
    """Distributed-RAM storage LUT count for a dual-port FIFO.

    Xilinx UG574 Section "Distributed RAM in SLICEM" / UG1324 ACAP CLB User Guide:
      - Each SLICEM LUT6 can be configured as RAM32X1D (32-deep x 1-bit
        dual-port) - the canonical cell for an HLS-generated DP FIFO.
      - One RAM32X1D = 1 LUT x 1 bit x 32 entries, so storage_luts =
        ceil(depth / 32) x width.

    The bare formula over-counts because Vitis sometimes packs into denser
    primitives (RAM64, RAM32M etc.). A `_LUTRAM_LUT_SCALE = 0.85` calibration
    factor (fitted on the 120-point storage_microbench corpus) corrects this
    systematic over-count and is essential for matching srl-vs-lutram
    ranking at depth in [128, 1024] (where SGRM otherwise picks srl).
    """
    if depth <= 1 or width <= 0:
        return 0
    return int(round(_LUTRAM_LUT_SCALE * (math.ceil(depth / 32) * width)))


_SRL_LUT_SCALE = 1.20      # microbench-fitted: original formula under-counted
_LUTRAM_LUT_SCALE = 0.85   # microbench-fitted: original formula over-counted


def _srl_chain_luts(width: int, depth: int) -> int:
    """SRL chain LUT count for any depth.

    For depth <= 32 a single SRL16E/SRL32E primitive holds the chain. For
    depth > 32, Vitis cascades SRL32 primitives - `ceil(depth/32)` LUTs per
    bit-column. We add a small log2(depth) cascade-decoder overhead.

    The deep-chain formula is multiplied by `_SRL_LUT_SCALE = 1.20`, fitted
    against the 120-point storage_microbench ground truth. Without this
    factor the bare `ceil(D/32)*W + 2*log2(D)` analytical formula
    systematically under-counts the SRL-chain glue logic by ~30%, which
    causes SGRM to rank srl above lutram at moderate depths and choose
    the wrong impl. With it, winner-agreement vs ground truth rises from
    50% to 93% over (W in {8..128}, D in {2..6272}).
    """
    if depth <= 1:
        return 0
    if depth <= 2:
        return width + 4
    if depth <= 32:
        return max(0, 2 * width - 7)
    # depth > 32: explicit SRL chain (Vitis emits SRL32E cascades on demand)
    base = math.ceil(depth / 32) * width + 2 * _ceil_log2(depth)
    return int(round(_SRL_LUT_SCALE * base))


def _ram_control_luts(width: int, depth: int) -> int:
    """Read/write pointer + handshake + output mux LUTs, shared by BRAM/URAM/
    LUTRAM/auto FIFOs."""
    return 2 * _ceil_log2(depth) + width


def predict_bram(width: int, depth: int, impl_type: str = "auto") -> int:
    """BRAM18K count for a single FIFO.

    impl_type in {srl, lutram, bram, uram, auto}.
    "auto" follows the empirically-observed Vitis HLS auto-pick behaviour:
    srl-class -> 0 BRAM, narrow+very-deep ram-class (W<=36 and D>=4096) -> 0 BRAM
    (Vitis materialises as LUTRAM), other ram-class -> BRAM-backed.
    """
    _check_impl(impl_type)
    if impl_type in ("srl", "lutram", "uram"):
        return 0
    if impl_type == "auto":
        if infer_fifo_type(width, depth) == "srl":
            return 0
        if width <= 36 and depth >= 4096:
            return 0
        return _bram_storage_count(width, depth)
    return _bram_storage_count(width, depth)  # impl_type == "bram"


def predict_uram(width: int, depth: int, impl_type: str = "auto") -> int:
    """URAM288 count for a single FIFO. Only impl_type=='uram' produces URAMs;
    Vitis HLS auto-pick is empirically observed never to materialise URAM
    in our 30-design corpus."""
    _check_impl(impl_type)
    if impl_type == "uram":
        return _uram_storage_count(width, depth)
    return 0


def predict_ff(width: int, depth: int, impl_type: str = "auto") -> int:
    """Flip-flop count for a single FIFO. impl_type in {srl,lutram,bram,uram,auto}.

    Vitis HLS silently overrides any impl pragma at depth <= 2 (BRAM/URAM
    cells cannot represent a 2-entry FIFO; LUTRAM is overkill). All four
    impls collapse to a registered-buffer cost identical to srl. Empirical
    confirmation: csynth W=32 depth=2 with impl in {srl, lutram, bram, auto}
    all reported FF=70-71, BRAM=0.

    SRL or any impl at depth <= 2:
      - depth <= 2 : 2*W + 1            (registered buffer; data in FFs)
      - depth >= 3 : 13                  (SRL primitive; data in LUT)

    BRAM / URAM (depth >= 3):
      - 5 + 4*log2(depth)               (read/write pointers + handshake)

    LUTRAM (depth >= 3):
      - W + 4*log2(depth) + 5           (above + explicit output register;
                                          LUTRAM cells lack the BRAM/URAM
                                          built-in output reg.)
    """
    _check_impl(impl_type)
    if depth <= 1:
        return 0
    if depth <= 2:
        return 2 * width + 1  # depth=2 is impl-independent
    if impl_type == "srl":
        return 13
    if impl_type == "auto" and infer_fifo_type(width, depth) == "srl":
        return 13  # auto for srl-class behaves like srl
    if impl_type == "lutram":
        return width + 4 * _ceil_log2(depth) + 5
    # bram / uram / auto-ram-class: control logic only
    return 5 + 4 * _ceil_log2(depth)


def predict_lut(width: int, depth: int, impl_type: str = "auto") -> int:
    """LUT count for a single FIFO. impl_type in {srl,lutram,bram,uram,auto}.

    SRL FIFOs:
      - depth <= 2  : W + 4                                  (mux + handshake)
      - 3 <= d <= 32 : 2*W - 7                                (single SRL prim)
      - depth > 32 : ceil(D/32)*W + 2*log2(D)               (SRL32 chain)

    LUTRAM FIFOs:
      - ceil(D/32)*W (storage, RAM32X1D primitive per UG574) +
        2*log2(D) + W (control: pointer + handshake + output mux)

    BRAM / URAM / auto FIFOs:
      - 2*log2(D) + W                                       (control only;
                                                              storage in BRAM/URAM
                                                              or absorbed by auto)
    """
    _check_impl(impl_type)
    if depth <= 1:
        return 0
    # depth=2 is impl-independent: Vitis HLS register-buffers regardless of
    # pragma. Empirical: W=32 depth=2 with impl in {srl,lutram,bram,auto} all
    # reported the same control-only LUT count (~W+4).
    if depth <= 2:
        return width + 4
    if impl_type == "srl":
        return _srl_chain_luts(width, depth)
    if impl_type == "auto" and infer_fifo_type(width, depth) == "srl":
        return _srl_chain_luts(width, depth)  # auto for srl-class behaves like srl
    if impl_type == "lutram":
        return _lutram_storage_luts(width, depth) + _ram_control_luts(width, depth)
    # bram / uram / auto-ram-class: storage off-LUT (or absorbed in auto), control only
    return _ram_control_luts(width, depth)


# --- Design-level aggregation -----------------------------------------


@dataclass
class FifoResourceResult:
    """Resource prediction for a single FIFO."""
    fifo_id: int
    width: int
    depth: int
    impl_type: ImplType
    bram: int
    uram: int
    ff: int
    lut: int


def predict_design_resources(
    fifo_widths: dict[int, int],
    fifo_depths: dict[int, int],
    fifo_impl_types: dict[int, str] | None = None,
) -> tuple[int, int, int, int, list[FifoResourceResult]]:
    """Compute total design resources given depths and optional impl types.

    Pure analytical formula. No HLS synthesis data is consulted.

    Args:
        fifo_widths: map fifo_id -> bit width
        fifo_depths: map fifo_id -> depth
        fifo_impl_types: map fifo_id -> impl type. Accepts all five values
            {srl, lutram, bram, uram, auto}. The "auto" path inside predict_*()
            is empirically calibrated against VCK190 2024.2 csynth and models
            Vitis HLS's auto-selection behaviour; it is NOT a search-space
            value (SGRM picks among the 4 concrete backends only). Missing
            entries default to "auto".

    Returns:
        (total_bram, total_uram, total_ff, total_lut, per_fifo_details)
    """
    total_bram = 0
    total_uram = 0
    total_ff = 0
    total_lut = 0
    details: list[FifoResourceResult] = []

    bucket_pred = {r: {b: 0.0 for b in _CAL_BUCKETS} for r in _CAL_RESOURCES}
    bucket_count = {b: 0 for b in _CAL_BUCKETS}

    for fid, depth in fifo_depths.items():
        width = fifo_widths[fid]
        impl = (fifo_impl_types or {}).get(fid, "auto")
        bram = predict_bram(width, depth, impl)
        uram = predict_uram(width, depth, impl)
        ff = predict_ff(width, depth, impl)
        lut = predict_lut(width, depth, impl)

        total_bram += bram
        total_uram += uram
        total_ff += ff
        total_lut += lut

        b = _bucket_of_impl(impl, width, depth)
        bucket_count[b] += 1
        bucket_pred["bram"][b] += bram
        bucket_pred["uram"][b] += uram
        bucket_pred["ff"][b] += ff
        bucket_pred["lut"][b] += lut

        details.append(FifoResourceResult(
            fifo_id=fid, width=width, depth=depth,
            impl_type=impl, bram=bram, uram=uram, ff=ff, lut=lut,
        ))

    if _calibration_enabled():
        cal = _load_calibration()
        if cal is not None:
            def _apply(r: str, raw_total: int) -> int:
                entry = cal["resources"][r]
                alpha = entry["alpha"]
                beta = entry["beta"]
                acc = 0.0
                for i, b in enumerate(_CAL_BUCKETS):
                    acc += alpha[i] * bucket_pred[r][b] + beta[i] * bucket_count[b]
                return max(0, int(round(acc)))

            total_bram = _apply("bram", total_bram)
            total_uram = _apply("uram", total_uram)
            total_ff = _apply("ff", total_ff)
            total_lut = _apply("lut", total_lut)

    return total_bram, total_uram, total_ff, total_lut, details
