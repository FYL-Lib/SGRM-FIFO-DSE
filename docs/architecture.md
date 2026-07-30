# Architecture

SGRM separates optimization policy from candidate measurement.

```text
FIFO descriptors + legal depth spaces
                 |
                 v
        grouped SGRM state
                 |
                 v
 profile -> shrink -> coordinate -> refine
                 |
                 v
       per-FIFO depth and implementation maps
                 |
                 v
          EvaluationBackend
                 |
                 v
 latency + deadlock + BRAM/URAM/FF/LUT
```

## Components

### `interfaces.py`

Defines the stable integration boundary:

- `FIFODescriptor` identifies a channel, width, and optional group label.
- `EvalResult` records feasibility, performance, resources, and the evaluated maps.
- `DesignSpaceProvider` supplies legal depth lattices.
- `EvaluationBackend` measures one complete candidate.

### `resource_model.py`

Provides deterministic per-FIFO BRAM, URAM, FF, and LUT estimates for concrete storage implementations. It also aggregates predictions across a design and supports an optional external calibration overlay.

### `sgrm.py`

Contains grouping, state expansion, feasibility checks, cost functions, all four search stages, budget rollover, Pareto bookkeeping, and final solution selection.

### `lightningsim_backend.py`

Loads a compatible pre-generated trace, exposes its FIFO metadata and legal
depth spaces, replays candidate depth maps, and combines measured latency with
the analytical FIFO resource model.

## Control flow

1. The constructor groups structurally equivalent channels and constructs one depth lattice per group.
2. `solve()` evaluates the default state and derives the hard latency threshold.
3. The global budget is partitioned across four stages.
4. Each proposal is expanded to complete per-FIFO maps before evaluation.
5. Feasible results update the passive Pareto archive.
6. Unused stage budget rolls forward.
7. `get_best_feasible()` selects the minimum-cost point satisfying the original constraint.

## Extension boundary

The optimization core never creates projects, parses workload inputs, or chooses a particular evaluation engine. Those responsibilities belong to an `EvaluationBackend` integration. This keeps search behavior testable and portable without embedding platform-specific execution code in the optimizer.
