"""
Sensitivity-Guided Resource Minimization (SGRM) solver.

Minimizes FIFO resource usage (BRAM, URAM, FF, LUT) subject to a latency
constraint: latency <= baseline_latency * (1 + epsilon).

Four-stage pipeline:
  Stage 1: Profile & seed - sensitivity probes plus optional all-small seeding.
  Stage 2: Guided shrink - greedy depth reductions over one-step, halve, and
           min-depth candidates.
  Stage 3: Coordinated moves - cooperative pair/triple moves, impl flips, and
           inline exact enumeration for tiny residual subspaces.
  Stage 4: Final halve & flip - per-group halving refinement with impl search.
"""

from __future__ import annotations

# Backend integration note: SGRM depends only on the abstract evaluator
# contract in ``interfaces.py``. Concrete execution backends are maintained
# independently from the core optimization package.

import logging
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations, product

from .interfaces import EvalResult, EvaluationBackend, FIFODescriptor, FIFOOptimizer
from .resource_model import get_valid_impl_types

logger = logging.getLogger(__name__)


# -- Helpers ---------------------------------------------------------------


# Versal VCK190 silicon-area weights, expressed in LUT-equivalent units.
#   1 LUT  = 1.0  (reference)
#   1 FF   = 0.5  (Versal CLB = 8 LUT + 16 FF, so one LUT's footprint ~ two FFs)
#   1 BRAM = 280  (BRAM18K tile's on-die area ~ 280 CLB LUTs)
#   1 URAM = 4480 (URAM tile ~ 16x BRAM capacity/area)
VCK190_AREA_WEIGHT_LUT = 1.0
VCK190_AREA_WEIGHT_FF = 0.5
VCK190_AREA_WEIGHT_BRAM = 280.0
VCK190_AREA_WEIGHT_URAM = 4480.0

# Versal VCK190 XCVC1902 device capacities (primary SLR-ish totals used as normalizers).
VCK190_CAPACITY_BRAM = 967
VCK190_CAPACITY_URAM = 463
VCK190_CAPACITY_FF = 1_799_680
VCK190_CAPACITY_LUT = 899_840


def _resource_cost(r: EvalResult) -> float:
    """Scalar resource cost: sum of all four resource types."""
    if r.deadlock or r.latency is None:
        return float("inf")
    return float(
        (r.bram_usage_total or 0)
        + (r.uram_usage_total or 0)
        + (r.ff_usage_total or 0)
        + (r.lut_usage_total or 0)
    )


def _area_cost(r: EvalResult) -> float:
    """Silicon-area cost in LUT-equivalents for Versal VCK190.

    Replaces the earlier baseline-normalized 1:1:1:1 weighting, which rewarded
    FF-for-LUT trades that are net-negative in real FPGA area.
    """
    if r.deadlock or r.latency is None:
        return float("inf")
    return float(
        VCK190_AREA_WEIGHT_BRAM * (r.bram_usage_total or 0)
        + VCK190_AREA_WEIGHT_URAM * (r.uram_usage_total or 0)
        + VCK190_AREA_WEIGHT_FF * (r.ff_usage_total or 0)
        + VCK190_AREA_WEIGHT_LUT * (r.lut_usage_total or 0)
    )


def _util_cost(r: EvalResult) -> float:
    """Mean fractional utilization across BRAM/URAM/FF/LUT vs VCK190 capacities."""
    if r.deadlock or r.latency is None:
        return float("inf")
    bram = (r.bram_usage_total or 0) / VCK190_CAPACITY_BRAM
    uram = (r.uram_usage_total or 0) / VCK190_CAPACITY_URAM
    ff = (r.ff_usage_total or 0) / VCK190_CAPACITY_FF
    lut = (r.lut_usage_total or 0) / VCK190_CAPACITY_LUT
    return float((bram + uram + ff + lut) / 4.0)


def _util_cost_tuple(r: EvalResult):
    """Lexicographic fractional utilization tuple (BRAM, URAM, FF, LUT)."""
    if r.deadlock or r.latency is None:
        return (float("inf"), float("inf"), float("inf"), float("inf"))
    bram = (r.bram_usage_total or 0) / VCK190_CAPACITY_BRAM
    uram = (r.uram_usage_total or 0) / VCK190_CAPACITY_URAM
    ff = (r.ff_usage_total or 0) / VCK190_CAPACITY_FF
    lut = (r.lut_usage_total or 0) / VCK190_CAPACITY_LUT
    return (float(bram), float(uram), float(ff), float(lut))


def _normalized_resource_cost(
    r: EvalResult,
    baseline: EvalResult,
    weights: tuple[float, float, float, float] = (0.25, 0.25, 0.25, 0.25),
) -> float:
    """Normalized weighted resource cost relative to baseline resource totals."""
    if r.deadlock or r.latency is None:
        return float("inf")

    w_bram, w_uram, w_ff, w_lut = weights
    bram = r.bram_usage_total or 0
    uram = r.uram_usage_total or 0
    ff = r.ff_usage_total or 0
    lut = r.lut_usage_total or 0

    baseline_bram = baseline.bram_usage_total or 0
    baseline_uram = baseline.uram_usage_total or 0
    baseline_ff = baseline.ff_usage_total or 0
    baseline_lut = baseline.lut_usage_total or 0

    return float(
        w_bram * bram / max(baseline_bram, 1)
        + w_uram * uram / max(baseline_uram, 1)
        + w_ff * ff / max(baseline_ff, 1)
        + w_lut * lut / max(baseline_lut, 1)
    )


def _is_feasible(r: EvalResult, lat_threshold: float) -> bool:
    """No deadlock and latency within threshold."""
    if r.deadlock or r.latency is None:
        return False
    return r.latency <= lat_threshold


@dataclass
class GroupProfile:
    """Sensitivity profile for one FIFO group."""

    name: str
    fifo_ids: list[int]
    design_space: list[int]
    default_depth_idx: int
    default_impl_type: str
    probed: dict[tuple[int, str], tuple[float, float]] = field(default_factory=dict)
    resource_savings: float = 0.0
    latency_sensitivity: float = 0.0
    efficiency: float = 0.0
    min_feasible_state: tuple[int, str] = (0, "srl")
    locked: bool = False


# -- Main Solver -----------------------------------------------------------


class SGRMOptimizer(FIFOOptimizer):
    """Sensitivity-Guided Resource Minimization optimizer.

    Args:
        sim_env: Backend implementing the EvaluationBackend contract.
        epsilon: Allowed latency degradation ratio (0.0 = no degradation).
        budget: Total evaluation budget across all stages.
        seed: Random seed.
        sa_temperature: Initial temperature for Stage 3 SA.
        sa_cooling: Cooling factor for Stage 3 SA.
    """

    def __init__(
        self,
        sim_env: EvaluationBackend,
        epsilon: float = 0.0,
        budget: int = 1000,
        seed: int = 7,
        sa_temperature: float = 1.0,
        sa_cooling: float = 0.995,
    ):
        super().__init__(sim_env)
        self.epsilon = epsilon
        self.budget = budget
        self.seed = seed
        self.sa_temperature = sa_temperature
        self.sa_cooling = sa_cooling
        self.rng = random.Random(seed)
        self._cost_mode = os.environ.get("SGRM_COST_MODE", "area").strip().lower()
        if self._cost_mode not in {"area", "sum", "util", "util_lex"}:
            self._cost_mode = "area"

        self._eval_count = 0
        self._all_results: list[EvalResult] = []
        self._baseline_result: EvalResult | None = None
        self._lat_threshold = float("inf")
        self._pareto_archive: list[EvalResult] = []

        # Build FIFO groups
        self.fifo_groups: dict[str, list[FIFODescriptor]] = defaultdict(list)
        for fifo in self.sim_env.fifos:
            self.fifo_groups[fifo.get_display_name()].append(fifo)

        self.group_names = list(self.fifo_groups.keys())

        self.fifo_ids_by_group: dict[str, list[int]] = {}
        for gname, fifos in self.fifo_groups.items():
            self.fifo_ids_by_group[gname] = [f.id for f in fifos]
        self.group_widths: dict[str, int] = {}
        for gname, fifos in self.fifo_groups.items():
            self.group_widths[gname] = fifos[0].width

        # Design space per group.
        #
        # The evaluator's get_fifo_design_space generates a sparse depth lattice
        # whose ram-class step is 1024 (one BRAM18K tile worth). That made
        # sense when the search space was {srl, bram, uram} only - there is no
        # cheaper impl in the 64-512 range than "wait for the next BRAM tile",
        # so intermediate depths were search-space waste.
        #
        # With LUTRAM now in the search space (see resource_model._FULL_IMPL_SPACE),
        # the 64-512 range is LUTRAM's sweet spot - microbench shows lutram@d=128
        # uses ~6x less LUT than lutram@d=1024 for the same FIFO. We augment the
        # lattice by inserting 128 and 512 whenever they fall inside an existing
        # gap, so SGRM can actually pick those depths.
        _LATTICE_INTERMEDIATES = (128, 512)

        def _augment_with_intermediates(ds: list[int]) -> list[int]:
            extras = [x for x in _LATTICE_INTERMEDIATES
                      if ds and ds[0] < x < ds[-1] and x not in ds]
            if not extras:
                return ds
            return sorted(set(ds) | set(extras))

        self.group_design_space: dict[str, list[int]] = {}
        for gname, fifos in self.fifo_groups.items():
            ds = self.sim_env.trace_base.compiled.get_fifo_design_space(
                [f.id for f in fifos], fifos[0].width
            )
            if ds == [2]:
                ds = [2, 64 * fifos[0].width]
            ds = _augment_with_intermediates(ds)
            self.group_design_space[gname] = ds

    # -- Evaluation helpers --------------------------------------------

    def _get_impl_type_space(self, gname: str, depth_idx: int) -> list[str]:
        """Return valid impl_type choices for a group at a depth index.

        The unified search space (resource_model.get_impl_type_space) returns
        the four concrete backends - srl, lutram, bram, uram - for every FIFO.
        SGRM relies on the cost model to discriminate, but two policy hooks
        re-order or trim the list:

          - SRL-only mode (`SGRM_IMPL_TYPE_MOVES=0`): for srl-class FIFOs
            (depth <= 2 or W*D <= 1024) restrict to ["srl"] so SGRM does not
            spend evals exploring obviously-worse impls.
          - Depth-based ordering (`SGRM_DEPTH_BASED_IMPL=1`): keep srl as the
            tie-break default for moderate depths (<= 32) where it still
            competes with LUTRAM/BRAM/URAM.
        """
        ds = self.group_design_space[gname]
        idx = min(max(depth_idx, 0), len(ds) - 1)
        depth = ds[idx]
        width = self.group_widths[gname]
        valid = get_valid_impl_types(width, depth)  # four concrete backends

        # SRL regime: impl_type is mostly fixed to srl (not a useful decision
        # variable) - but if SGRM_IMPL_TYPE_MOVES is set, expose the rest.
        if depth <= 2 or (width * depth) <= 1024:
            if getattr(self, "_impl_type_moves", False):
                return ["srl"] + [impl for impl in valid if impl != "srl"]
            return ["srl"]

        # Ram-class regime: full four-way space. Optionally promote SRL to the
        # front for moderate depths where SRL chains stay competitive.
        if getattr(self, "_depth_based_impl", False) and depth <= 32:
            return ["srl"] + [impl for impl in valid if impl != "srl"]
        return valid

    def _get_baseline_impl_type(self, gname: str, depth_idx: int) -> str:
        """Baseline impl mirrors HLS default: srl for srl-class, auto for ram-class.

        Unaffected by the new search space - baseline is the "no DSE, let
        HLS pick" reference point and intentionally emits `impl=auto`.
        """
        ds = self.group_design_space[gname]
        idx = min(max(depth_idx, 0), len(ds) - 1)
        depth = ds[idx]
        width = self.group_widths[gname]
        if depth <= 2 or (width * depth) <= 1024:
            return "srl"
        return "auto"

    def _make_config_and_impl(
        self,
        group_state: dict[str, tuple[int, str]],
    ) -> tuple[dict[int, int], dict[int, str]]:
        """Convert group state to full FIFO depth config and impl_type assignment."""
        config: dict[int, int] = {}
        impl: dict[int, str] = {}
        for gname, (idx, impl_type) in group_state.items():
            ds = self.group_design_space[gname]
            clamped_idx = min(max(idx, 0), len(ds) - 1)
            depth = ds[clamped_idx]
            valid_impl_types = self._get_impl_type_space(gname, clamped_idx)
            chosen_impl = impl_type if impl_type in valid_impl_types else valid_impl_types[0]
            for fid in self.fifo_ids_by_group[gname]:
                config[fid] = depth
                impl[fid] = chosen_impl
        return config, impl

    def _evaluate(self, group_state: dict[str, tuple[int, str]]) -> EvalResult:
        """Evaluate a configuration and track budget."""
        config, impl = self._make_config_and_impl(group_state)
        result = self.sim_env.eval_solution_single(config, impl)
        self._eval_count += 1
        self._all_results.append(result)
        self._update_pareto_archive(
            result, getattr(self, "_lat_threshold", float("inf"))
        )
        return result

    def _remaining_budget(self) -> int:
        return max(0, self.budget - self._eval_count)

    def _cost(self, r: EvalResult):
        if self._cost_mode == "util_lex":
            return _util_cost_tuple(r)
        return self._cost_scalar(r)

    def _cost_scalar(self, r: EvalResult) -> float:
        if self._cost_mode in {"util", "util_lex"}:
            return _util_cost(r)
        if self._cost_mode == "sum":
            return _resource_cost(r)
        return _area_cost(r)

    def _worst_cost(self):
        if self._cost_mode == "util_lex":
            return (float("inf"), float("inf"), float("inf"), float("inf"))
        return float("inf")

    def _is_finite_cost(self, c) -> bool:
        if isinstance(c, tuple):
            return all(math.isfinite(v) for v in c)
        return math.isfinite(c)

    def _strictly_worse(self, cand, best) -> bool:
        if isinstance(cand, tuple) or isinstance(best, tuple):
            return cand > best
        return cand > best + 1e-9

    def _fmt_cost(self, c) -> str:
        if isinstance(c, tuple):
            if not self._is_finite_cost(c):
                return "(inf, inf, inf, inf)"
            return "(" + ", ".join(f"{v:.4f}" for v in c) + ")"
        if not math.isfinite(c):
            return "inf"
        return f"{c:.4f}"

    def _scalar_of(self, c) -> float:
        if isinstance(c, tuple):
            if not self._is_finite_cost(c):
                return float("inf")
            return float(sum(c) / len(c))
        return float(c)

    def _update_pareto_archive(self, r: EvalResult, lat_threshold: float) -> None:
        """Passive update: add r to archive if feasible and non-dominated."""
        if r.deadlock or r.latency is None or r.latency > lat_threshold:
            return

        r_vec = (
            r.bram_usage_total or 0,
            r.uram_usage_total or 0,
            r.ff_usage_total or 0,
            r.lut_usage_total or 0,
        )

        def dominates(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
            return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))

        kept: list[EvalResult] = []
        for member in self._pareto_archive:
            member_vec = (
                member.bram_usage_total or 0,
                member.uram_usage_total or 0,
                member.ff_usage_total or 0,
                member.lut_usage_total or 0,
            )
            if dominates(member_vec, r_vec):
                return
            if not dominates(r_vec, member_vec):
                kept.append(member)

        kept.append(r)
        self._pareto_archive = kept

    def _estimate_remaining_resource_saving(
        self,
        profile: GroupProfile,
        current_state: dict[str, tuple[int, str]],
    ) -> float:
        """Approximate how much resource cost a group can still save."""
        cur_idx, _ = current_state[profile.name]
        if cur_idx <= 0 or profile.default_depth_idx <= 0:
            return 0.0

        ds = profile.design_space
        default_depth = ds[profile.default_depth_idx]
        current_depth = ds[cur_idx]
        min_depth = ds[0]
        if default_depth <= min_depth:
            return 0.0

        remaining_fraction = (current_depth - min_depth) / max(default_depth - min_depth, 1)
        remaining_fraction = max(0.0, min(1.0, remaining_fraction))
        return profile.resource_savings * remaining_fraction

    def _build_frontier(
        self,
        current_state: dict[str, tuple[int, str]],
        profiles_by_name: dict[str, GroupProfile],
        locked_groups: set[str],
        limit: int = 10,
    ) -> list[str]:
        """Build the Stage 3 frontier from locked groups plus top residual groups."""
        shrinkable = [g for g in self.group_names if current_state[g][0] > 0]
        if not shrinkable:
            return []

        def residual_score(gname: str) -> float:
            return self._estimate_remaining_resource_saving(
                profiles_by_name[gname], current_state
            )

        locked_rank = sorted(
            [g for g in shrinkable if g in locked_groups],
            key=residual_score,
            reverse=True,
        )
        residual_rank = sorted(
            [g for g in shrinkable if g not in locked_groups],
            key=residual_score,
            reverse=True,
        )

        frontier: list[str] = []
        seen: set[str] = set()
        for gname in locked_rank + residual_rank:
            if gname in seen:
                continue
            frontier.append(gname)
            seen.add(gname)
            if len(frontier) >= limit:
                break
        return frontier

    def _shrink_groups_by_one(
        self,
        current_state: dict[str, tuple[int, str]],
        groups: tuple[str, ...] | list[str],
    ) -> dict[str, tuple[int, str]] | None:
        """Return a candidate state with each target group shrunk by one depth index."""
        candidate = dict(current_state)
        for gname in groups:
            cur_idx, cur_impl = candidate[gname]
            if cur_idx <= 0:
                return None
            new_idx = cur_idx - 1
            valid_impl_types = self._get_impl_type_space(gname, new_idx)
            new_impl = cur_impl if cur_impl in valid_impl_types else valid_impl_types[0]
            candidate[gname] = (new_idx, new_impl)
        return candidate

    def _halve_depth_candidate(
        self,
        current_state: dict[str, tuple[int, str]],
        gname: str,
        target_idx: int | None = None,
    ) -> dict[str, tuple[int, str]] | None:
        """Return a candidate state with one group's depth index halved.

        P2-6 neighbor: try a big jump (idx -> idx // 2) rather than a single
        step. If ``target_idx`` is given, jump directly to that index instead.
        """
        cur_idx, cur_impl = current_state[gname]
        new_idx = cur_idx // 2 if target_idx is None else target_idx
        if new_idx >= cur_idx or new_idx < 0:
            return None
        valid_impl_types = self._get_impl_type_space(gname, new_idx)
        new_impl = cur_impl if cur_impl in valid_impl_types else valid_impl_types[0]
        candidate = dict(current_state)
        candidate[gname] = (new_idx, new_impl)
        return candidate

    def _flip_impl_type(
        self,
        current_state: dict[str, tuple[int, str]],
        gname: str,
    ) -> dict[str, tuple[int, str]] | None:
        """Return a candidate state with one group's impl flipped at fixed depth."""
        cur_idx, cur_impl = current_state[gname]
        valid_impl_types = self._get_impl_type_space(gname, cur_idx)
        flip_options = [impl for impl in valid_impl_types if impl != cur_impl]
        if not flip_options:
            return None

        candidate = dict(current_state)
        candidate[gname] = (cur_idx, self.rng.choice(flip_options))
        return candidate

    def _quick_single_group_sweep(
        self,
        current_state: dict[str, tuple[int, str]],
        current_cost: float,
        lat_threshold: float,
        profiles_by_name: dict[str, GroupProfile],
        preferred_groups: list[str],
    ) -> tuple[dict[str, tuple[int, str]], float, bool]:
        """Single pass of opportunistic one-step shrinks after a cooperative move."""
        order: list[str] = []
        seen: set[str] = set()

        for gname in preferred_groups:
            if gname in seen or current_state[gname][0] <= 0:
                continue
            order.append(gname)
            seen.add(gname)

        remaining = sorted(
            [g for g in self.group_names if current_state[g][0] > 0 and g not in seen],
            key=lambda g: self._estimate_remaining_resource_saving(
                profiles_by_name[g], current_state
            ),
            reverse=True,
        )
        order.extend(remaining)

        improved = False
        for gname in order:
            if self._remaining_budget() <= 0:
                break

            candidate = self._shrink_groups_by_one(current_state, [gname])
            if candidate is None:
                continue

            r = self._evaluate(candidate)
            if not _is_feasible(r, lat_threshold):
                continue

            cand_cost = self._cost(r)
            if cand_cost < current_cost:
                current_state = candidate
                current_cost = cand_cost
                improved = True
                logger.info(
                    "  Stage 3 sweep accepted %s -> idx %d",
                    gname,
                    current_state[gname][0],
                )

        return current_state, current_cost, improved

    def _group_choice_space(
        self,
        gname: str,
        current_state: dict[str, tuple[int, str]],
    ) -> list[tuple[int, str]]:
        """Enumerate all remaining assignments for a group up to its Stage 3 state."""
        cur_idx, cur_impl = current_state[gname]
        choices: list[tuple[int, str]] = []
        seen: set[tuple[int, str]] = set()

        preferred = (cur_idx, cur_impl)
        choices.append(preferred)
        seen.add(preferred)

        for idx in range(cur_idx + 1):
            for impl_type in self._get_impl_type_space(gname, idx):
                choice = (idx, impl_type)
                if choice in seen:
                    continue
                choices.append(choice)
                seen.add(choice)

        return choices

    def _state_key(self, group_state: dict[str, tuple[int, str]]) -> tuple[tuple[str, int, str], ...]:
        return tuple((gname, *group_state[gname]) for gname in self.group_names)

    def _stage2_candidate_indices(self, cur_idx: int) -> list[int]:
        candidates: list[int] = []
        raw = [cur_idx - 1, 0]
        if getattr(self, "_depth_halve_neighbor", False):
            raw.insert(1, cur_idx // 2)
        for idx in raw:
            idx = max(0, idx)
            if idx >= cur_idx or idx in candidates:
                continue
            candidates.append(idx)
        return candidates

    # -- Stage 1: Sensitivity Profiling --------------------------------

    def _find_default_depth_idx(self, gname: str) -> int:
        """Find the index of default depth in group design space."""
        ds = self.group_design_space[gname]
        default_depth = None
        for fid in self.fifo_ids_by_group[gname]:
            d = self.sim_env.fifo_sizes_base[fid]
            if d is not None:
                default_depth = d
                break
        if default_depth is None:
            return len(ds) - 1
        best_idx = 0
        best_diff = abs(ds[0] - default_depth)
        for i, d in enumerate(ds):
            diff = abs(d - default_depth)
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        return best_idx

    def _stage1_profile(
        self, baseline_result: EvalResult, budget: int
    ) -> list[GroupProfile]:
        """Profile each group's sensitivity to depth changes."""
        logger.info("=== Stage 1: Sensitivity Profiling ===")

        baseline_latency = baseline_result.latency
        baseline_cost = self._cost_scalar(baseline_result)
        lat_threshold = baseline_latency * (1.0 + self.epsilon)
        evals_at_start = self._eval_count

        default_state: dict[str, tuple[int, str]] = {}
        for gname in self.group_names:
            default_depth_idx = self._find_default_depth_idx(gname)
            default_impl_type = self._get_baseline_impl_type(gname, default_depth_idx)
            default_state[gname] = (default_depth_idx, default_impl_type)

        profiles: list[GroupProfile] = []

        for gname in self.group_names:
            if (self._eval_count - evals_at_start) >= budget:
                break

            ds = self.group_design_space[gname]
            def_idx, def_impl = default_state[gname]

            profile = GroupProfile(
                name=gname,
                fifo_ids=self.fifo_ids_by_group[gname],
                design_space=ds,
                default_depth_idx=def_idx,
                default_impl_type=def_impl,
            )
            profile.probed[(def_idx, def_impl)] = (baseline_latency, baseline_cost)

            # Probe depths: min, 25%, 50%, 75%
            probe_indices = set()
            probe_indices.add(0)
            if def_idx > 0:
                for frac in [0.25, 0.5, 0.75]:
                    probe_indices.add(max(0, int(def_idx * frac)))

            for pidx in sorted(probe_indices):
                if pidx == def_idx:
                    continue
                if self._remaining_budget() <= 0:
                    break
                valid_impl_types = self._get_impl_type_space(gname, pidx)
                pimpl = def_impl if def_impl in valid_impl_types else valid_impl_types[0]
                test_state = dict(default_state)
                test_state[gname] = (pidx, pimpl)
                r = self._evaluate(test_state)
                lat = r.latency if (not r.deadlock and r.latency is not None) else float("inf")
                cost = self._cost_scalar(r)
                profile.probed[(pidx, pimpl)] = (lat, cost)

            # Compute sensitivity metrics
            min_feasible_state = (def_idx, def_impl)
            max_lat_increase = 0.0
            max_resource_savings = 0.0
            for (pidx, pimpl), (lat, cost) in sorted(
                profile.probed.items(), key=lambda x: x[0][0]
            ):
                if lat <= lat_threshold:
                    if pidx < min_feasible_state[0]:
                        min_feasible_state = (pidx, pimpl)
                    savings = baseline_cost - cost
                    if savings > max_resource_savings:
                        max_resource_savings = savings
                lat_increase = max(0, lat - baseline_latency)
                if lat_increase > max_lat_increase:
                    max_lat_increase = lat_increase

            profile.min_feasible_state = min_feasible_state
            profile.resource_savings = max_resource_savings
            profile.latency_sensitivity = max_lat_increase
            profile.efficiency = max_resource_savings / max(max_lat_increase, 1e-6)

            profiles.append(profile)

        logger.info(
            f"Stage 1 complete: {self._eval_count} evals used, "
            f"{self._remaining_budget()} remaining"
        )
        return profiles

    # -- Stage 2: Guided Greedy Shrinking ------------------------------

    def _stage2_greedy_shrink(
        self,
        profiles: list[GroupProfile],
        baseline_result: EvalResult,
        budget: int,
        min_remaining_after: int | None = None,
        seed_state: dict[str, tuple[int, str]] | None = None,
    ) -> dict[str, tuple[int, str]]:
        """Iteratively shrink groups by efficiency order."""
        logger.info("=== Stage 2: Guided Greedy Shrinking ===")

        baseline_latency = baseline_result.latency
        lat_threshold = baseline_latency * (1.0 + self.epsilon)

        if seed_state is not None:
            current_state = dict(seed_state)
            current_result = self._evaluate(current_state)
            if not _is_feasible(current_result, lat_threshold):
                current_state = {
                    p.name: (p.default_depth_idx, p.default_impl_type) for p in profiles
                }
                current_result = baseline_result
        else:
            current_state = {
                p.name: (p.default_depth_idx, p.default_impl_type) for p in profiles
            }
            current_result = baseline_result
        best_cost = self._cost(current_result)

        stage2_budget = budget
        evals_at_start = self._eval_count
        tried_states = {self._state_key(current_state)}

        def stage2_should_stop() -> bool:
            remaining = self._remaining_budget()
            if remaining <= 0:
                return True
            if min_remaining_after is not None and remaining <= min_remaining_after:
                return True
            return False

        iteration = 0
        while (self._eval_count - evals_at_start) < stage2_budget:
            if stage2_should_stop():
                break

            unlocked = [p for p in profiles if not p.locked]
            if not unlocked:
                break
            unlocked.sort(key=lambda p: (-p.efficiency, p.name))

            best_candidate: tuple[
                dict[str, tuple[int, str]],
                EvalResult,
                str,
                int,
                str,
                str,
                float,
                float,
            ] | None = None
            best_rank: tuple[float, float, float, int, int, int] | None = None
            for profile in unlocked:
                gname = profile.name
                cur_idx, cur_impl = current_state[gname]
                found_candidate = False

                for new_idx in self._stage2_candidate_indices(cur_idx):
                    if stage2_should_stop():
                        break
                    valid_impl_types = self._get_impl_type_space(gname, new_idx)
                    new_impl = cur_impl if cur_impl in valid_impl_types else valid_impl_types[0]
                    test_state = dict(current_state)
                    test_state[gname] = (new_idx, new_impl)
                    state_key = self._state_key(test_state)
                    if state_key in tried_states:
                        continue
                    tried_states.add(state_key)

                    r = self._evaluate(test_state)
                    if not _is_feasible(r, lat_threshold):
                        continue

                    new_cost = self._cost(r)
                    cost_gain = self._scalar_of(best_cost) - self._scalar_of(new_cost)
                    if cost_gain <= 0.0:
                        continue

                    found_candidate = True
                    lat_gain = max(0.0, r.latency - current_result.latency)
                    ratio = cost_gain / max(lat_gain, 1e-6)
                    rank = (
                        ratio,
                        cost_gain,
                        -lat_gain,
                        cur_idx - new_idx,
                        -self.group_names.index(gname),
                        1,
                    )
                    if best_rank is None or rank > best_rank:
                        best_rank = rank
                        best_candidate = (
                            test_state,
                            r,
                            gname,
                            new_idx,
                            new_impl,
                            "shrink",
                            ratio,
                            cost_gain,
                        )

                if getattr(self, "_impl_type_moves", False) and not stage2_should_stop():
                    candidate = self._flip_impl_type(current_state, gname)
                    if candidate is not None:
                        state_key = self._state_key(candidate)
                        if state_key not in tried_states:
                            tried_states.add(state_key)
                            r = self._evaluate(candidate)
                            if _is_feasible(r, lat_threshold):
                                new_cost = self._cost(r)
                                cost_gain = self._scalar_of(best_cost) - self._scalar_of(new_cost)
                                if cost_gain > 0.0:
                                    found_candidate = True
                                    lat_gain = max(0.0, r.latency - current_result.latency)
                                    ratio = cost_gain / max(lat_gain, 1e-6)
                                    rank = (
                                        ratio,
                                        cost_gain,
                                        -lat_gain,
                                        0,
                                        -self.group_names.index(gname),
                                        0,
                                    )
                                    if best_rank is None or rank > best_rank:
                                        best_rank = rank
                                        best_candidate = (
                                            candidate,
                                            r,
                                            gname,
                                            candidate[gname][0],
                                            candidate[gname][1],
                                            "flip",
                                            ratio,
                                            cost_gain,
                                        )

                profile.locked = not found_candidate

            iteration += 1
            if best_candidate is None:
                break

            current_state, current_result, gname, new_idx, new_impl, move_kind, ratio, cost_gain = best_candidate
            best_cost = self._cost(current_result)
            logger.info(
                "  Stage 2 accept %s %s -> idx %d impl %s cost=%s gain=%.4f ratio=%.4f",
                move_kind,
                gname,
                new_idx,
                new_impl,
                self._fmt_cost(best_cost),
                cost_gain,
                ratio,
            )

            # Re-profile every 5 iterations to capture interactions
            if iteration % 5 == 0:
                for profile in profiles:
                    if profile.locked:
                        continue
                    gname = profile.name
                    cur_idx, cur_impl = current_state[gname]
                    if cur_idx <= 0:
                        profile.locked = True
                        continue
                    if stage2_should_stop():
                        break
                    new_idx = self._stage2_candidate_indices(cur_idx)[0]
                    valid_impl_types = self._get_impl_type_space(gname, new_idx)
                    new_impl = cur_impl if cur_impl in valid_impl_types else valid_impl_types[0]
                    test_state = dict(current_state)
                    test_state[gname] = (new_idx, new_impl)
                    r = self._evaluate(test_state)
                    lat = r.latency if (not r.deadlock and r.latency is not None) else float("inf")
                    cost = self._cost_scalar(r)
                    lat_delta = max(0, lat - current_result.latency)
                    cost_delta = max(0, self._cost_scalar(current_result) - cost)
                    profile.efficiency = cost_delta / max(lat_delta, 1e-6)

        logger.info(
            f"Stage 2 complete: {self._eval_count} evals used, "
            f"cost={self._fmt_cost(best_cost)}, "
            f"{self._remaining_budget()} remaining"
        )
        return current_state

    # -- Stage 3: Cooperative Unlock -----------------------------------

    def _stage3_cooperative_unlock(
        self,
        current_state: dict[str, tuple[int, str]],
        baseline_result: EvalResult,
        profiles: list[GroupProfile],
        locked_groups: list[str],
        budget: int,
    ) -> tuple[dict[str, tuple[int, str]], dict[tuple[str, str], int]]:
        """Search for cooperative multi-group shrink moves missed by Stage 2."""
        logger.info("=== Stage 3: Cooperative Unlock ===")

        if budget <= 0 or self._remaining_budget() <= 0:
            logger.info("Stage 3 skipped: no budget available")
            return dict(current_state), {}

        baseline_latency = baseline_result.latency
        lat_threshold = baseline_latency * (1.0 + self.epsilon)
        profiles_by_name = {p.name: p for p in profiles}

        best_state = dict(current_state)
        best_result = self._evaluate(best_state)
        best_cost = self._cost(best_result)
        if not _is_feasible(best_result, lat_threshold):
            logger.warning("Stage 3 entry state is infeasible; skipping cooperative search")
            return best_state, {}

        search_budget = max(0, budget - 1)
        pair_budget = int(search_budget * 0.70)
        triple_budget = int(search_budget * 0.25)
        diversify_budget = search_budget - pair_budget - triple_budget
        logger.info(
            "Stage 3 budgets: pair=%d triple=%d diversify=%d",
            pair_budget,
            triple_budget,
            diversify_budget,
        )

        affinity: defaultdict[tuple[str, str], int] = defaultdict(int)
        locked_priority = set(locked_groups)
        pair_evals = 0
        triple_evals = 0
        diversify_evals = 0
        rounds_completed = 0
        stage_improved = False
        impl_type_moves = getattr(self, "_impl_type_moves", False)
        min_frontier_size = 1 if impl_type_moves else 2

        while self._remaining_budget() > 0 and (pair_evals < pair_budget or triple_evals < triple_budget):
            frontier = self._build_frontier(
                best_state,
                profiles_by_name,
                locked_priority,
                limit=10,
            )
            if len(frontier) < min_frontier_size:
                logger.info("Stage 3 frontier exhausted: %d shrinkable groups", len(frontier))
                break

            rounds_completed += 1
            logger.info("Stage 3 round %d frontier=%s", rounds_completed, frontier)
            round_improved = False
            pair_seed_scores: dict[tuple[str, str], float] = {}

            if pair_evals < pair_budget:
                for pair in combinations(frontier, 2):
                    if pair_evals >= pair_budget or self._remaining_budget() <= 0:
                        break

                    candidate = self._shrink_groups_by_one(best_state, pair)
                    if candidate is None:
                        continue

                    r = self._evaluate(candidate)
                    pair_evals += 1
                    if not _is_feasible(r, lat_threshold):
                        continue

                    cand_cost = self._cost(r)
                    if cand_cost < best_cost:
                        improvement = self._scalar_of(best_cost) - self._scalar_of(cand_cost)
                        pair_key = tuple(sorted(pair))
                        pair_seed_scores[pair_key] = max(
                            pair_seed_scores.get(pair_key, 0.0), improvement
                        )
                        affinity[pair_key] += 1
                        best_state = candidate
                        best_cost = cand_cost
                        round_improved = True
                        stage_improved = True
                        locked_priority.clear()
                        logger.info(
                            "  Pair accept %s cost=%s",
                            pair_key,
                            self._fmt_cost(best_cost),
                        )
                        best_state, best_cost, _ = self._quick_single_group_sweep(
                            best_state,
                            best_cost,
                            lat_threshold,
                            profiles_by_name,
                            list(pair) + frontier,
                        )

                # P2-6 depth-halve neighbor: try idx -> idx//2 for each frontier group
                if (
                    getattr(self, "_depth_halve_neighbor", False)
                    and pair_evals < pair_budget
                    and self._remaining_budget() > 0
                ):
                    for gname in frontier:
                        if pair_evals >= pair_budget or self._remaining_budget() <= 0:
                            break
                        cur_idx = best_state[gname][0]
                        if cur_idx < 2:
                            continue

                        candidate = self._halve_depth_candidate(best_state, gname)
                        if candidate is None:
                            continue

                        r = self._evaluate(candidate)
                        pair_evals += 1
                        if not _is_feasible(r, lat_threshold):
                            continue

                        cand_cost = self._cost(r)
                        if cand_cost < best_cost:
                            best_state = candidate
                            best_cost = cand_cost
                            round_improved = True
                            stage_improved = True
                            locked_priority.clear()
                            logger.info(
                                "  Halve accept %s idx -> %d cost=%s",
                                gname,
                                best_state[gname][0],
                                self._fmt_cost(best_cost),
                            )
                            best_state, best_cost, _ = self._quick_single_group_sweep(
                                best_state,
                                best_cost,
                                lat_threshold,
                                profiles_by_name,
                                [gname] + frontier,
                            )

                if impl_type_moves and pair_evals < pair_budget and self._remaining_budget() > 0:
                    for gname in frontier:
                        if pair_evals >= pair_budget or self._remaining_budget() <= 0:
                            break

                        candidate = self._flip_impl_type(best_state, gname)
                        if candidate is None:
                            continue

                        r = self._evaluate(candidate)
                        pair_evals += 1
                        if not _is_feasible(r, lat_threshold):
                            continue

                        cand_cost = self._cost(r)
                        if cand_cost < best_cost:
                            best_state = candidate
                            best_cost = cand_cost
                            round_improved = True
                            stage_improved = True
                            locked_priority.clear()
                            logger.info(
                                "  Flip accept %s cost=%s",
                                gname,
                                self._fmt_cost(best_cost),
                            )
                            best_state, best_cost, _ = self._quick_single_group_sweep(
                                best_state,
                                best_cost,
                                lat_threshold,
                                profiles_by_name,
                                [gname] + frontier,
                            )

                    if pair_evals < pair_budget and self._remaining_budget() > 0:
                        mixed_pairs = [
                            (shrink_g, flip_g)
                            for shrink_g in frontier
                            for flip_g in frontier
                            if shrink_g != flip_g
                        ]
                        self.rng.shuffle(mixed_pairs)
                        for shrink_g, flip_g in mixed_pairs[: len(frontier)]:
                            if pair_evals >= pair_budget or self._remaining_budget() <= 0:
                                break

                            candidate = self._shrink_groups_by_one(best_state, [shrink_g])
                            if candidate is None:
                                continue
                            candidate = self._flip_impl_type(candidate, flip_g)
                            if candidate is None:
                                continue

                            r = self._evaluate(candidate)
                            pair_evals += 1
                            if not _is_feasible(r, lat_threshold):
                                continue

                            cand_cost = self._cost(r)
                            if cand_cost < best_cost:
                                pair_key = tuple(sorted((shrink_g, flip_g)))
                                affinity[pair_key] += 1
                                best_state = candidate
                                best_cost = cand_cost
                                round_improved = True
                                stage_improved = True
                                locked_priority.clear()
                                logger.info(
                                    "  Mixed accept %s cost=%s",
                                    pair_key,
                                    self._fmt_cost(best_cost),
                                )
                                best_state, best_cost, _ = self._quick_single_group_sweep(
                                    best_state,
                                    best_cost,
                                    lat_threshold,
                                    profiles_by_name,
                                    [shrink_g, flip_g] + frontier,
                                )

            if triple_evals < triple_budget:
                best_pair_seeds = [
                    seed
                    for seed, _ in sorted(
                        pair_seed_scores.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                ]
                for pair_seed in best_pair_seeds:
                    if triple_evals >= triple_budget or self._remaining_budget() <= 0:
                        break

                    for third in frontier:
                        if triple_evals >= triple_budget or self._remaining_budget() <= 0:
                            break
                        if third in pair_seed:
                            continue

                        triple = tuple(sorted((*pair_seed, third)))
                        candidate = self._shrink_groups_by_one(best_state, triple)
                        if candidate is None:
                            continue

                        r = self._evaluate(candidate)
                        triple_evals += 1
                        if not _is_feasible(r, lat_threshold):
                            continue

                        cand_cost = self._cost(r)
                        if cand_cost < best_cost:
                            best_state = candidate
                            best_cost = cand_cost
                            round_improved = True
                            stage_improved = True
                            locked_priority.clear()
                            for pair_key in combinations(triple, 2):
                                affinity[tuple(sorted(pair_key))] += 1
                            logger.info(
                                "  Triple accept %s cost=%s",
                                triple,
                                self._fmt_cost(best_cost),
                            )
                            best_state, best_cost, _ = self._quick_single_group_sweep(
                                best_state,
                                best_cost,
                                lat_threshold,
                                profiles_by_name,
                                list(triple) + frontier,
                            )

            if not round_improved:
                logger.info(
                    "Stage 3 early termination after round %d: no pair/triple improvement",
                    rounds_completed,
                )
                break

        if stage_improved and diversify_budget > 0 and self._remaining_budget() > 0:
            frontier = self._build_frontier(
                best_state,
                profiles_by_name,
                locked_priority,
                limit=10,
            )
            frontier = [g for g in frontier if best_state[g][0] > 0]
            logger.info("Stage 3 diversification frontier=%s", frontier)
            min_diversify_frontier_size = 1 if impl_type_moves else 2
            diversify_flip_done = False

            while (
                diversify_evals < diversify_budget
                and self._remaining_budget() > 0
                and len(frontier) >= min_diversify_frontier_size
            ):
                if len(frontier) >= 2:
                    combo_size = 2 if len(frontier) == 2 else self.rng.choice([2, 3])
                    combo_size = min(combo_size, len(frontier))
                    combo = tuple(sorted(self.rng.sample(frontier, combo_size)))
                    candidate = self._shrink_groups_by_one(best_state, combo)
                    if candidate is not None:
                        r = self._evaluate(candidate)
                        diversify_evals += 1
                        if _is_feasible(r, lat_threshold):
                            cand_cost = self._cost(r)
                            if cand_cost < best_cost:
                                best_state = candidate
                                best_cost = cand_cost
                                locked_priority.clear()
                                for pair_key in combinations(combo, 2):
                                    affinity[tuple(sorted(pair_key))] += 1
                                logger.info(
                                    "  Diversification accept %s cost=%s",
                                    combo,
                                    self._fmt_cost(best_cost),
                                )
                                best_state, best_cost, _ = self._quick_single_group_sweep(
                                    best_state,
                                    best_cost,
                                    lat_threshold,
                                    profiles_by_name,
                                    list(combo) + frontier,
                                )
                                frontier = self._build_frontier(
                                    best_state,
                                    profiles_by_name,
                                    locked_priority,
                                    limit=10,
                                )
                                frontier = [g for g in frontier if best_state[g][0] > 0]
                                diversify_flip_done = False
                                continue

                if impl_type_moves and not diversify_flip_done:
                    flip_pass_improved = False

                    for gname in frontier:
                        if diversify_evals >= diversify_budget or self._remaining_budget() <= 0:
                            break

                        candidate = self._flip_impl_type(best_state, gname)
                        if candidate is None:
                            continue

                        r = self._evaluate(candidate)
                        diversify_evals += 1
                        if not _is_feasible(r, lat_threshold):
                            continue

                        cand_cost = self._cost(r)
                        if cand_cost < best_cost:
                            best_state = candidate
                            best_cost = cand_cost
                            flip_pass_improved = True
                            locked_priority.clear()
                            logger.info(
                                "  Diversification flip accept %s cost=%s",
                                gname,
                                self._fmt_cost(best_cost),
                            )
                            best_state, best_cost, _ = self._quick_single_group_sweep(
                                best_state,
                                best_cost,
                                lat_threshold,
                                profiles_by_name,
                                [gname] + frontier,
                            )

                    if diversify_evals < diversify_budget and self._remaining_budget() > 0:
                        mixed_pairs = [
                            (shrink_g, flip_g)
                            for shrink_g in frontier
                            for flip_g in frontier
                            if shrink_g != flip_g
                        ]
                        self.rng.shuffle(mixed_pairs)
                        for shrink_g, flip_g in mixed_pairs[: len(frontier)]:
                            if diversify_evals >= diversify_budget or self._remaining_budget() <= 0:
                                break

                            candidate = self._shrink_groups_by_one(best_state, [shrink_g])
                            if candidate is None:
                                continue
                            candidate = self._flip_impl_type(candidate, flip_g)
                            if candidate is None:
                                continue

                            r = self._evaluate(candidate)
                            diversify_evals += 1
                            if not _is_feasible(r, lat_threshold):
                                continue

                            cand_cost = self._cost(r)
                            if cand_cost < best_cost:
                                pair_key = tuple(sorted((shrink_g, flip_g)))
                                affinity[pair_key] += 1
                                best_state = candidate
                                best_cost = cand_cost
                                flip_pass_improved = True
                                locked_priority.clear()
                                logger.info(
                                    "  Diversification mixed accept %s cost=%s",
                                    pair_key,
                                    self._fmt_cost(best_cost),
                                )
                                best_state, best_cost, _ = self._quick_single_group_sweep(
                                    best_state,
                                    best_cost,
                                    lat_threshold,
                                    profiles_by_name,
                                    [shrink_g, flip_g] + frontier,
                                )

                    if flip_pass_improved:
                        frontier = self._build_frontier(
                            best_state,
                            profiles_by_name,
                            locked_priority,
                            limit=10,
                        )
                        frontier = [g for g in frontier if best_state[g][0] > 0]
                        diversify_flip_done = False
                        continue

                    diversify_flip_done = True

                if len(frontier) < 2:
                    break

        logger.info(
            "Stage 3 complete: cost=%s pair_evals=%d triple_evals=%d diversify_evals=%d affinity_pairs=%d remaining=%d",
            self._fmt_cost(best_cost),
            pair_evals,
            triple_evals,
            diversify_evals,
            len(affinity),
            self._remaining_budget(),
        )
        return best_state, dict(affinity)

    # -- Stage 4: Reduced-Core Exact -----------------------------------

    def _stage4_reduced_core_exact(
        self,
        current_state: dict[str, tuple[int, str]],
        baseline_result: EvalResult,
        profiles: list[GroupProfile],
        affinity: dict[tuple[str, str], int],
        budget: int,
    ) -> dict[str, tuple[int, str]]:
        """Exactly enumerate the remaining coupled subspace on a reduced core."""
        logger.info("=== Stage 4: Reduced-Core Exact ===")

        if budget <= 0 or self._remaining_budget() <= 0:
            logger.info("Stage 4 skipped: no budget available")
            return dict(current_state)

        baseline_latency = baseline_result.latency
        lat_threshold = baseline_latency * (1.0 + self.epsilon)
        profiles_by_name = {p.name: p for p in profiles}

        shrinkable = [g for g in self.group_names if current_state[g][0] > 0]
        if not shrinkable:
            logger.info("Stage 4 skipped: all groups already at minimum depth")
            return dict(current_state)

        interaction_counts: defaultdict[str, int] = defaultdict(int)
        for (ga, gb), score in affinity.items():
            interaction_counts[ga] += score
            interaction_counts[gb] += score

        scored_groups: list[tuple[str, float, int]] = []
        for gname in shrinkable:
            profile = profiles_by_name[gname]
            remaining_saving = self._estimate_remaining_resource_saving(
                profile, current_state
            )
            interaction_count = interaction_counts[gname]
            sensitivity = profile.latency_sensitivity
            core_score = remaining_saving * (1 + interaction_count) / (1 + sensitivity)
            choice_count = len(self._group_choice_space(gname, current_state))
            scored_groups.append((gname, core_score, choice_count))

        scored_groups.sort(key=lambda item: item[1], reverse=True)
        available_budget = min(budget, self._remaining_budget())
        active_core: list[str] = []
        combination_count = 1

        for gname, core_score, choice_count in scored_groups:
            next_combination_count = combination_count * choice_count
            if next_combination_count > available_budget:
                break
            active_core.append(gname)
            combination_count = next_combination_count
            logger.info(
                "  Core add %s score=%.6f choices=%d combinations=%d",
                gname,
                core_score,
                choice_count,
                combination_count,
            )

        if not active_core:
            logger.info(
                "Stage 4 skipped: no exact core fits within budget=%d",
                available_budget,
            )
            return dict(current_state)

        logger.info(
            "Stage 4 enumerating core=%s combinations=%d frozen=%d",
            active_core,
            combination_count,
            len(self.group_names) - len(active_core),
        )

        choice_spaces = {
            gname: self._group_choice_space(gname, current_state)
            for gname in active_core
        }
        best_state = dict(current_state)
        best_cost = self._worst_cost()

        for assignment in product(*(choice_spaces[gname] for gname in active_core)):
            if self._remaining_budget() <= 0:
                break

            candidate = dict(current_state)
            for gname, state_value in zip(active_core, assignment):
                candidate[gname] = state_value

            r = self._evaluate(candidate)
            if not _is_feasible(r, lat_threshold):
                continue

            cand_cost = self._cost(r)
            if cand_cost < best_cost:
                best_cost = cand_cost
                best_state = candidate
                logger.info(
                    "  Exact core update cost=%s",
                    self._fmt_cost(best_cost),
                )

        logger.info(
            "Stage 4 complete: best_cost=%s remaining=%d",
            self._fmt_cost(best_cost),
            self._remaining_budget(),
        )
        return best_state

    # -- Stage 1.5: Start-From-Small initialization (P2-3) -------------

    def _stage15_start_from_small(
        self,
        profiles: list[GroupProfile],
        baseline_result: EvalResult,
        budget: int,
    ) -> dict[str, tuple[int, str]] | None:
        """Try an initial state with every group at idx=0; fall back to Stage 1's
        per-group min_feasible_state on infeasibility.

        Returns a seed state to feed into Stage 2, or None if all attempts fail.
        """
        logger.info("=== Stage 1.5: Start-From-Small ===")
        if budget <= 0 or self._remaining_budget() <= 0:
            logger.info("Stage 1.5 skipped: no budget available")
            return None

        lat_threshold = baseline_result.latency * (1.0 + self.epsilon)

        # Attempt 1: all groups at idx=0 with impl chosen from the space.
        all_small: dict[str, tuple[int, str]] = {}
        for p in profiles:
            valid_impl_types = self._get_impl_type_space(p.name, 0)
            all_small[p.name] = (0, valid_impl_types[0])
        r = self._evaluate(all_small)
        if _is_feasible(r, lat_threshold):
            logger.info(
                "  Stage 1.5 all-small feasible, cost=%s lat=%s",
                self._fmt_cost(self._cost(r)),
                r.latency,
            )
            return all_small

        # Attempt 2: fall back to per-group Stage-1 min_feasible_state.
        mixed_small: dict[str, tuple[int, str]] = {}
        for p in profiles:
            idx, impl = p.min_feasible_state
            valid_impl_types = self._get_impl_type_space(p.name, idx)
            chosen = impl if impl in valid_impl_types else valid_impl_types[0]
            mixed_small[p.name] = (idx, chosen)
        if self._remaining_budget() <= 0:
            return None
        r = self._evaluate(mixed_small)
        if _is_feasible(r, lat_threshold):
            logger.info(
                "  Stage 1.5 mixed-small feasible, cost=%s lat=%s",
                self._fmt_cost(self._cost(r)),
                r.latency,
            )
            return mixed_small

        logger.info("  Stage 1.5 both starts infeasible; Stage 2 will run from default")
        return None

    # -- Stage 5: Per-FIFO depth halving refinement (P2-2) -------------

    def _stage5_halve_refine(
        self,
        current_state: dict[str, tuple[int, str]],
        baseline_result: EvalResult,
        budget: int,
    ) -> dict[str, tuple[int, str]]:
        """Post-Stage-4 refinement: for each group, binary-search the smallest
        feasible depth index by halving. Each accepted halve reduces one group's
        depth by up to 2x while preserving util-cost monotonicity.
        """
        logger.info("=== Stage 5: Halve Refinement ===")
        if budget <= 0 or self._remaining_budget() <= 0:
            logger.info("Stage 5 skipped: no budget available")
            return dict(current_state)

        lat_threshold = baseline_result.latency * (1.0 + self.epsilon)
        best_state = dict(current_state)
        best_result = self._evaluate(best_state)
        if not _is_feasible(best_result, lat_threshold):
            logger.warning("Stage 5 entry state infeasible; skipping halve refine")
            return best_state
        best_cost = self._cost(best_result)
        evals_at_start = self._eval_count

        groups_by_headroom = sorted(
            self.group_names,
            key=lambda g: best_state[g][0],
            reverse=True,
        )

        for gname in groups_by_headroom:
            while (self._eval_count - evals_at_start) < budget and self._remaining_budget() > 0:
                cur_idx = best_state[gname][0]
                if cur_idx <= 0:
                    break

                # Binary search: low = smallest feasible (start 0), high = cur_idx-1.
                low, high = 0, cur_idx - 1
                best_new_idx: int | None = None
                while low <= high and (self._eval_count - evals_at_start) < budget and self._remaining_budget() > 0:
                    mid = (low + high) // 2
                    candidate = self._halve_depth_candidate(best_state, gname, target_idx=mid)
                    if candidate is None:
                        break
                    r = self._evaluate(candidate)
                    if _is_feasible(r, lat_threshold):
                        cand_cost = self._cost(r)
                        if cand_cost <= best_cost:
                            best_new_idx = mid
                            high = mid - 1
                        else:
                            # Feasible but cost regressed (impl flip side-effect);
                            # try higher indices which may be cheaper at same feasibility.
                            low = mid + 1
                    else:
                        low = mid + 1

                if best_new_idx is None or best_new_idx >= cur_idx:
                    break

                new_candidate = self._halve_depth_candidate(
                    best_state, gname, target_idx=best_new_idx
                )
                if new_candidate is None:
                    break
                r = self._evaluate(new_candidate)
                if not _is_feasible(r, lat_threshold):
                    break
                cand_cost = self._cost(r)
                if self._strictly_worse(cand_cost, best_cost):
                    break
                best_state = new_candidate
                best_cost = cand_cost
                logger.info(
                    "  Stage 5 halve %s idx %d -> %d cost=%s",
                    gname,
                    cur_idx,
                    best_new_idx,
                    self._fmt_cost(best_cost),
                )
                break  # one halve per group per pass

        logger.info(
            "Stage 5 complete: cost=%s evals=%d remaining=%d",
            self._fmt_cost(best_cost),
            self._eval_count - evals_at_start,
            self._remaining_budget(),
        )
        return best_state

    # -- Stage orchestration wrappers (4-stage pipeline) ---------------

    def _default_profiles(self) -> list[GroupProfile]:
        """Build a minimal GroupProfile per group with zero evals.

        Used by the SGRM_STAGES ablation when Stage 1 is skipped: downstream
        stages still need profiles for structural metadata (name, design_space,
        default_depth_idx, default_impl_type), but the sensitivity-derived
        fields (efficiency, latency_sensitivity, resource_savings,
        min_feasible_state, locked) just take their dataclass defaults.
        """
        profiles: list[GroupProfile] = []
        for gname in self.group_names:
            def_idx = self._find_default_depth_idx(gname)
            def_impl = self._get_baseline_impl_type(gname, def_idx)
            profiles.append(
                GroupProfile(
                    name=gname,
                    fifo_ids=self.fifo_ids_by_group[gname],
                    design_space=self.group_design_space[gname],
                    default_depth_idx=def_idx,
                    default_impl_type=def_impl,
                )
            )
        return profiles

    def _stage1_profile_and_seed(
        self,
        baseline_result: EvalResult,
        budget: int,
    ) -> tuple[list[GroupProfile], dict[str, tuple[int, str]] | None]:
        """Stage 1 (4-stage pipeline): run sensitivity profiling and optional
        all-small seed probe in a single stage. Reserves 2 evals for the seed
        probe when enabled; the rest goes to profiling.
        """
        seed_budget = 2 if self._start_from_small else 0
        profile_budget = max(0, budget - seed_budget)
        profiles = self._stage1_profile(baseline_result, budget=profile_budget)

        # Stage 1 is budget bounded, so very large designs may exhaust its
        # allocation before every group is profiled.  Downstream stages require
        # a complete group state; preserve measured profiles and fill any
        # unprofiled groups with their baseline metadata rather than passing a
        # partial state that later raises KeyError in _state_key().
        profiles_by_name = {profile.name: profile for profile in profiles}
        missing_names = [
            gname for gname in self.group_names if gname not in profiles_by_name
        ]
        if missing_names:
            default_profiles = {
                profile.name: profile for profile in self._default_profiles()
            }
            for gname in missing_names:
                profiles_by_name[gname] = default_profiles[gname]
            logger.info(
                "Stage 1 budget exhausted before profiling %d group(s); "
                "using baseline profiles for: %s",
                len(missing_names),
                missing_names,
            )
        profiles = [profiles_by_name[gname] for gname in self.group_names]

        profiles_sorted = sorted(profiles, key=lambda p: p.efficiency, reverse=True)
        for p in profiles_sorted:
            logger.info(
                f"  Group '{p.name}': efficiency={p.efficiency:.1f}, "
                f"savings={p.resource_savings:.1f}, "
                f"sensitivity={p.latency_sensitivity:.1f}, "
                f"space_size={len(p.design_space)}, "
                f"default_idx={p.default_depth_idx}"
            )
        seed_state: dict[str, tuple[int, str]] | None = None
        if self._start_from_small:
            seed_state = self._stage15_start_from_small(
                profiles, baseline_result, budget=seed_budget
            )
        return profiles, seed_state

    def _stage2_guided_shrink(
        self,
        profiles: list[GroupProfile],
        baseline_result: EvalResult,
        budget: int,
        seed_state: dict[str, tuple[int, str]] | None,
    ) -> dict[str, tuple[int, str]]:
        """Stage 2 (4-stage pipeline): greedy shrink. Candidate set per group
        is {idx-1, idx//2 when SGRM_DEPTH_HALVE_NEIGHBOR=1, 0} - the halve and
        direct-to-min moves are already inside `_stage2_greedy_shrink` when
        that flag is set, absorbing what old Stage 5 did.
        """
        return self._stage2_greedy_shrink(
            profiles,
            baseline_result,
            budget=budget,
            min_remaining_after=None,
            seed_state=seed_state,
        )

    def _stage3_coordinated(
        self,
        shrunk_state: dict[str, tuple[int, str]],
        baseline_result: EvalResult,
        profiles: list[GroupProfile],
        locked_groups: list[str],
        budget: int,
    ) -> dict[str, tuple[int, str]]:
        """Stage 3 (4-stage pipeline): coordinated moves. Runs cooperative
        pair/triple + impl-type flips, then a reduced-core exact enumeration
        on any residual small subspace. Absorbs old Stage 3 + Stage 4.
        """
        coop_budget = int(budget * 0.6)
        exact_budget = budget - coop_budget
        evals_start = self._eval_count
        refined_state, affinity = self._stage3_cooperative_unlock(
            shrunk_state,
            baseline_result,
            profiles,
            locked_groups,
            budget=coop_budget,
        )
        coop_used = self._eval_count - evals_start
        exact_budget += max(0, coop_budget - coop_used)
        logger.info(
            "Stage 3 coop rollover into exact core: +%d evals",
            max(0, coop_budget - coop_used),
        )
        return self._stage4_reduced_core_exact(
            refined_state,
            baseline_result,
            profiles,
            affinity,
            budget=exact_budget,
        )

    def _stage4_final_halve(
        self,
        refined_state: dict[str, tuple[int, str]],
        baseline_result: EvalResult,
        budget: int,
    ) -> dict[str, tuple[int, str]]:
        """Stage 4 (4-stage pipeline): per-group binary halve search for the
        smallest feasible depth idx, with impl-type selection at each probed
        idx. Gated by SGRM_STAGE5_HALVE_REFINE=1 (env var kept for backward
        compat). Absorbs old Stage 5 and gets a larger share of the budget.
        """
        if not self._stage5_halve_refine_enabled:
            return refined_state
        return self._stage5_halve_refine(
            refined_state,
            baseline_result,
            budget=budget,
        )

    # -- Main solve ----------------------------------------------------

    def solve(self) -> list[EvalResult]:
        """Run the four-stage SGRM pipeline.

        Stage 1 - Profile & Seed   (15%)
        Stage 2 - Guided Shrink    (50%)
        Stage 3 - Coordinated Moves (25%)
        Stage 4 - Final Halve & Flip (10%)
        """
        self._impl_type_moves = os.environ.get("SGRM_IMPL_TYPE_MOVES", "0") == "1"
        self._depth_based_impl = os.environ.get("SGRM_DEPTH_BASED_IMPL", "0") == "1"
        self._start_from_small = os.environ.get("SGRM_START_FROM_SMALL", "0") == "1"
        self._depth_halve_neighbor = os.environ.get("SGRM_DEPTH_HALVE_NEIGHBOR", "0") == "1"
        self._stage5_halve_refine_enabled = (
            os.environ.get("SGRM_STAGE5_HALVE_REFINE", "0") == "1"
        )
        stage3_quota_frac = max(0.0, float(os.environ.get("SGRM_STAGE3_QUOTA_FRAC", "0.0")))
        if stage3_quota_frac > 0.0:
            logger.info(
                "SGRM_STAGE3_QUOTA_FRAC=%.3f is deprecated under the 4-stage pipeline "
                "and has no effect.",
                stage3_quota_frac,
            )

        # Ablation: SGRM_STAGES selects which of {1,2,3,4} run; default = all.
        stages_raw = os.environ.get("SGRM_STAGES", "1,2,3,4")
        try:
            active_stages = {int(s.strip()) for s in stages_raw.split(",") if s.strip()}
        except ValueError as exc:
            raise ValueError(f"SGRM_STAGES must be comma-separated ints in {{1,2,3,4}}, got {stages_raw!r}") from exc
        if not active_stages or not active_stages.issubset({1, 2, 3, 4}):
            raise ValueError(f"SGRM_STAGES must be a non-empty subset of {{1,2,3,4}}, got {sorted(active_stages)}")
        logger.info("SGRM_STAGES active: %s", sorted(active_stages))

        self._lat_threshold = float("inf")
        start_time = time.perf_counter()

        # Evaluate baseline (default FIFO depths)
        default_state: dict[str, tuple[int, str]] = {}
        for gname in self.group_names:
            default_depth_idx = self._find_default_depth_idx(gname)
            initial_impl_type = self._get_baseline_impl_type(gname, default_depth_idx)
            default_state[gname] = (default_depth_idx, initial_impl_type)
        baseline_result = self._evaluate(default_state)
        self._baseline_result = baseline_result

        if baseline_result.deadlock or baseline_result.latency is None:
            logger.error("Baseline configuration deadlocks! Cannot proceed.")
            return self._all_results

        self._lat_threshold = baseline_result.latency * (1.0 + self.epsilon)
        baseline_cost = self._cost(baseline_result)
        logger.info(
            f"Baseline: latency={baseline_result.latency}, "
            f"resource_cost={self._fmt_cost(baseline_cost)}, "
            f"BRAM={baseline_result.bram_usage_total}, "
            f"URAM={baseline_result.uram_usage_total}, "
            f"FF={baseline_result.ff_usage_total}, "
            f"LUT={baseline_result.lut_usage_total}"
        )

        # 4-stage fixed budget split: 15 / 50 / 25 / 10
        total = self._remaining_budget()
        s1_budget = int(total * 0.15)
        s2_budget = int(total * 0.50)
        s3_budget = int(total * 0.25)
        s4_budget = total - s1_budget - s2_budget - s3_budget
        logger.info(
            "Stage budgets (4-stage): s1=%d, s2=%d, s3=%d, s4=%d",
            s1_budget, s2_budget, s3_budget, s4_budget,
        )

        def _state_sum_depth(state: dict[str, tuple[int, str]]) -> int:
            total = 0
            for gname, (idx, _impl) in state.items():
                ds = self.group_design_space[gname]
                clamped = min(max(idx, 0), len(ds) - 1)
                total += ds[clamped] * len(self.fifo_ids_by_group[gname])
            return total

        baseline_sum_depth = _state_sum_depth(default_state)
        diag_enabled = os.environ.get("SGRM_STAGE_DIAG", "0") == "1"
        if diag_enabled:
            print(f"[DIAG] baseline sum_depth={baseline_sum_depth}", flush=True)

        # Stage 1: Profile & Seed
        if 1 in active_stages:
            s1_start = self._eval_count
            profiles, seed_state = self._stage1_profile_and_seed(
                baseline_result, budget=s1_budget
            )
            s2_budget += max(0, s1_budget - (self._eval_count - s1_start))
            if diag_enabled:
                seed_sd = _state_sum_depth(seed_state) if seed_state else baseline_sum_depth
                print(f"[DIAG] after_stage1 seed_sum_depth={seed_sd}", flush=True)
        else:
            logger.info("Stage 1 SKIPPED (SGRM_STAGES ablation)")
            profiles = self._default_profiles()
            seed_state = None
            s2_budget += s1_budget  # roll skipped budget into next active stage
            if diag_enabled:
                print(f"[DIAG] stage1 skipped seed_sum_depth={baseline_sum_depth}", flush=True)

        # Stage 2: Guided Shrink
        if 2 in active_stages:
            s2_start = self._eval_count
            shrunk_state = self._stage2_guided_shrink(
                profiles, baseline_result, budget=s2_budget, seed_state=seed_state
            )
            s3_budget += max(0, s2_budget - (self._eval_count - s2_start))
            locked_groups = [p.name for p in profiles if p.locked]
            logger.info("Stage 2 locked groups carried into Stage 3: %s", locked_groups)
            if diag_enabled:
                print(f"[DIAG] after_stage2 sum_depth={_state_sum_depth(shrunk_state)}", flush=True)
        else:
            logger.info("Stage 2 SKIPPED (SGRM_STAGES ablation)")
            shrunk_state = seed_state if seed_state is not None else default_state
            locked_groups = []  # no S2 -> nothing got locked
            s3_budget += s2_budget
            if diag_enabled:
                print(f"[DIAG] stage2 skipped sum_depth={_state_sum_depth(shrunk_state)}", flush=True)

        # Stage 3: Coordinated Moves (cooperative + exact)
        if 3 in active_stages:
            s3_start = self._eval_count
            refined_state = self._stage3_coordinated(
                shrunk_state, baseline_result, profiles, locked_groups, budget=s3_budget
            )
            s4_budget += max(0, s3_budget - (self._eval_count - s3_start))
            if diag_enabled:
                print(f"[DIAG] after_stage3 sum_depth={_state_sum_depth(refined_state)}", flush=True)
        else:
            logger.info("Stage 3 SKIPPED (SGRM_STAGES ablation)")
            refined_state = shrunk_state
            s4_budget += s3_budget
            if diag_enabled:
                print(f"[DIAG] stage3 skipped sum_depth={_state_sum_depth(refined_state)}", flush=True)

        # Stage 4: Final Halve & Flip
        if 4 in active_stages:
            final_state = self._stage4_final_halve(
                refined_state, baseline_result, budget=s4_budget
            )
            if diag_enabled:
                print(f"[DIAG] after_stage4 sum_depth={_state_sum_depth(final_state)}", flush=True)
        else:
            logger.info("Stage 4 SKIPPED (SGRM_STAGES ablation)")
            final_state = refined_state
            if diag_enabled:
                print(f"[DIAG] stage4 skipped sum_depth={_state_sum_depth(final_state)}", flush=True)

        # Evaluate the terminal state when budget permits, then report the best
        # feasible point seen anywhere in the search.  The terminal state is
        # not necessarily the lowest-cost feasible state.
        if self._remaining_budget() > 0:
            self._evaluate(final_state)
        final_result = self.get_best_feasible() or baseline_result
        final_cost = self._cost(final_result)
        baseline_cost_scalar = self._scalar_of(baseline_cost)
        final_cost_scalar = self._scalar_of(final_cost)
        savings_pct = (
            (1 - final_cost_scalar / baseline_cost_scalar) * 100
            if baseline_cost_scalar > 0
            else 0.0
        )

        elapsed = time.perf_counter() - start_time
        logger.info(
            f"\n{'=' * 60}\n"
            f"SGRM complete in {elapsed:.1f}s, {self._eval_count} evaluations\n"
            f"  Baseline: cost={self._fmt_cost(baseline_cost)}\n"
            f"  Final:    cost={self._fmt_cost(final_cost)}\n"
            f"  Savings:  {savings_pct:.1f}%\n"
            f"  Latency:  {baseline_result.latency} -> {final_result.latency}\n"
            f"  BRAM:     {baseline_result.bram_usage_total} -> {final_result.bram_usage_total}\n"
            f"  URAM:     {baseline_result.uram_usage_total} -> {final_result.uram_usage_total}\n"
            f"  FF:       {baseline_result.ff_usage_total} -> {final_result.ff_usage_total}\n"
            f"  LUT:      {baseline_result.lut_usage_total} -> {final_result.lut_usage_total}\n"
            f"{'=' * 60}"
        )

        return self._all_results

    def get_pareto_archive(self) -> list[EvalResult]:
        """Return a copy of the passive feasible Pareto archive."""
        return list(self._pareto_archive)

    def get_best_feasible(self) -> EvalResult | None:
        """Return the best feasible result from all evaluations, ranked by
        the active cost mode (so util-mode search returns its util-best point)."""
        if not self._all_results:
            return None
        baseline = self._all_results[0]
        if baseline.deadlock or baseline.latency is None:
            return None
        lat_threshold = baseline.latency * (1.0 + self.epsilon)
        feasible = [r for r in self._all_results if _is_feasible(r, lat_threshold)]
        if not feasible:
            return None
        return min(feasible, key=self._cost)


SGRMSolver = SGRMOptimizer
