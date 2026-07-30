# Algorithm

## Problem formulation

For FIFO groups `g = 1..G`, SGRM chooses a legal depth `d_g` and a concrete storage implementation `m_g`. The objective is to minimize a scalarized four-resource cost while satisfying:

```text
deadlock = false
T(d, m) <= T0 * (1 + epsilon)
```

`T0` is measured from the default design before optimization. With the canonical `epsilon = 0`, resource reductions cannot be purchased with latency regression.

## State representation

`SGRMOptimizer` groups FIFO descriptors by display name. For each group it records:

- the member FIFO identifiers;
- one common bit width;
- a sorted legal depth lattice;
- the current depth-lattice index; and
- the current concrete implementation type.

`_make_config_and_impl()` expands this compact group state into complete per-FIFO depth and implementation dictionaries. This reduction is important for designs containing many structurally repeated channels.

## Feasibility and cost

`_evaluate()` is the only search-side evaluation gateway. It expands the state, invokes the abstract backend once, consumes one unit of budget, records the result, and updates the passive feasible Pareto archive.

A result is feasible only when it is deadlock-free, has a defined latency, and does not exceed the baseline-derived latency threshold. Infeasible points receive infinite active cost and cannot be accepted as solutions.

## Stage 1: Profile & Seed

`_stage1_profile()` probes lower depth/implementation states for each group while holding the remaining groups at their reference states. It derives resource savings, latency sensitivity, and an efficiency score used to order later moves. `_stage1_profile_and_seed()` reserves a small bounded allowance for optional low-depth seeding.

The final working version includes a large-design completeness safeguard: if the Stage 1 budget ends before every group is profiled, measured profiles are preserved and unvisited groups are filled with baseline structural metadata. The ordered profile list therefore always covers every group required by downstream state keys.

## Stage 2: Guided Shrink

`_stage2_greedy_shrink()` starts from the feasible seed when available, otherwise from the default configuration. Candidate depth changes include the previous lattice point, a halved-index neighbor, and the minimum point when enabled. Concrete implementation alternatives are evaluated at candidate depths. A move is retained only if feasibility is preserved and resource cost does not regress.

Groups whose local reductions cannot be accepted are marked for coordinated treatment rather than discarded.

## Stage 3: Coordinated Moves

`_stage3_cooperative_unlock()` explores depth changes and implementation flips jointly across interacting groups. Pair and triple proposals allow the search to cross local barriers that cannot be traversed by an isolated single-group move. An affinity record concentrates subsequent proposals on combinations that previously produced useful behavior.

The remainder of the stage budget is passed to `_stage4_reduced_core_exact()`. Despite its legacy internal name, this routine is part of public Stage 3: it identifies a small residual set and performs bounded exact enumeration when the reduced Cartesian product is tractable.

## Stage 4: Final Halve & Flip

`_stage4_final_halve()` calls the retained refinement routine to binary-search smaller legal depth indices group by group. Each depth probe also selects a valid implementation. Only feasible, non-worse points are committed.

Some private routine names still contain historical stage numbers. They are retained to preserve provenance with earlier revisions; `solve()` is the authoritative four-stage orchestration.

## Budget discipline

The default split is 15% / 50% / 25% / 10%. A stage can consume no more than its allocation and the remaining global budget. Unused evaluations roll forward, allowing later stages to exploit cheap early convergence without changing the total cap.

## Result selection

`solve()` returns the recorded evaluation sequence. `get_best_feasible()` re-applies the hard latency/deadlock condition and selects the minimum point under the active cost mode. `get_pareto_archive()` exposes the non-dominated feasible measurements without replacing the hard-constrained search policy.
