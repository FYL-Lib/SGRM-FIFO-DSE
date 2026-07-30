# Configuration

SGRM accepts algorithm-wide parameters through the constructor and optional search controls through environment variables.

## Constructor parameters

| Parameter | Default | Meaning |
|---|---:|---|
| `epsilon` | `0.0` | Allowed latency ratio above the baseline |
| `budget` | `1000` | Maximum number of candidate evaluations |
| `seed` | `7` | Deterministic random seed |
| `sa_temperature` | `1.0` | Retained cooperative-search temperature parameter |
| `sa_cooling` | `0.995` | Retained cooling parameter |

## Search controls

| Variable | Default | Purpose |
|---|---|---|
| `SGRM_COST_MODE` | `area` | Select `area`, `sum`, `util`, or `util_lex` |
| `SGRM_IMPL_TYPE_MOVES` | `0` | Enable concrete implementation alternatives in the SRL regime |
| `SGRM_DEPTH_BASED_IMPL` | `0` | Apply depth-aware implementation ordering |
| `SGRM_START_FROM_SMALL` | `0` | Probe low-depth feasible seeds in Stage 1 |
| `SGRM_DEPTH_HALVE_NEIGHBOR` | `0` | Add halved and direct-to-minimum candidates in Stage 2 |
| `SGRM_STAGE5_HALVE_REFINE` | `0` | Enable the refinement orchestrated as public Stage 4 |
| `SGRM_STAGES` | `1,2,3,4` | Select a non-empty subset of stages |
| `SGRM_STAGE_DIAG` | `0` | Emit stage-level depth diagnostics |

## Paper configuration

```text
SGRM_COST_MODE=util
SGRM_IMPL_TYPE_MOVES=1
SGRM_DEPTH_BASED_IMPL=1
SGRM_START_FROM_SMALL=1
SGRM_DEPTH_HALVE_NEIGHBOR=1
SGRM_STAGE5_HALVE_REFINE=1
SGRM_STAGES=1,2,3,4
```

## Budget allocation

The four stages receive 15%, 50%, 25%, and 10% of the remaining global budget at the start of the search. Unused evaluations roll into the next stage. The global counter remains authoritative, so rollover never exceeds `budget`.
