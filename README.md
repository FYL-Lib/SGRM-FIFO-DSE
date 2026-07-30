# SGRM

**Sensitivity-Guided Resource Minimization for FIFO Design-Space Exploration in HLS Dataflow Designs**

SGRM jointly selects FIFO depth and storage implementation while enforcing a hard latency constraint:

```text
latency <= baseline_latency * (1 + epsilon)
```

The reference configuration uses `epsilon = 0`. Candidate latency and deadlock behavior are evaluated by replaying a pre-generated LightningSim trace; FIFO BRAM, URAM, FF, and LUT costs are computed by the included analytical model.

## Quick start

The tested search-replay environment is Linux x86-64, Python 3.12, LightningSim 0.2.6, and llvmlite 0.43. Conda installs the pinned runtime:

```bash
conda env create --file environment.yml
conda activate sgrm
python -m pip install -e '.[dev]'
pytest
```

Run and verify the included `bicg` trace:

```bash
sgrm-trace-batch \
  --manifest examples/traces/manifest.json \
  --output-dir results/bicg

sgrm-verify-results \
  --manifest examples/traces/manifest.json \
  --results-dir results/bicg
```

Expected final line:

```text
PASS bicg
```

The reference run evaluates 11 distinct points. Evaluation 1 is the baseline; the selected solution first appears at evaluation 3 and changes latency from 835 to 834 cycles while changing modeled FIFO resources from `(BRAM, URAM, FF, LUT) = (0, 0, 572, 2883)` to `(0, 0, 572, 2508)`.

## Reproduce all 30 trace searches

The repository is self-contained: the versioned Stream-HLS trace bundle is
tracked at
[`datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz`](datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz).
Download the repository and run the following commands from its root.

Verify the archive before extracting it:

```bash
sha256sum --check datasets/SHA256SUMS
```

The expected output is:

```text
datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz: OK
```

The recorded SHA-256 is `b25327f6a5085cec9305cc9fb3116072df31778efb0ce80fe1ba0ac238df491c`.

Extract the archive, run the reference search on all 30 designs, and verify the generated JSON results:

```bash
tar -xJf datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz

sgrm-trace-batch \
  --manifest sgrm-stream-hls-30-traces-v0.1.0/manifest.json \
  --output-dir results/stream-hls-30

sgrm-verify-results \
  --manifest sgrm-stream-hls-30-traces-v0.1.0/manifest.json \
  --results-dir results/stream-hls-30
```

A successful batch prints 30 `PASS` lines, and the verifier prints another 30 `PASS` lines with no `FAIL`. Per-design results are written to `results/stream-hls-30/<design>.json`; the batch summary is `results/stream-hls-30/index.json`.

See [Reproducing the trace searches](REPRODUCING.md) for the direct single-design command, exact environment details, result-field definitions, timeout controls, and manifest construction.

## Trace security

LightningSim traces are Python pickle files. Only load trace bundles from a
trusted source and verify the manifest SHA-256 values before replay. See
[Security](SECURITY.md) for the trust boundary.

## Highlights

- Joint FIFO depth and storage-implementation optimization
- Four concrete storage choices: SRL, LUTRAM, BRAM, and URAM
- Hard deadlock and latency feasibility checks
- Grouped decision variables for structurally equivalent channels
- Sensitivity-guided four-stage search with bounded evaluation budgets
- Capacity-normalized BRAM/URAM/FF/LUT objective
- Manifest-driven batch execution with trace checksums
- Machine-readable result verification
- Backend-neutral core with a pre-generated-trace integration

## Repository layout

```text
.
+-- src/sgrm/
|   +-- sgrm.py                  four-stage optimization algorithm
|   +-- resource_model.py        analytical FIFO resource model
|   +-- interfaces.py            evaluator and FIFO data contracts
|   +-- lightningsim_backend.py  pre-generated trace adapter
|   +-- cli.py                   single-trace command
|   +-- batch_cli.py             manifest-driven batch command
|   +-- verify_cli.py            result verifier
|   +-- build_manifest_cli.py    trace-manifest builder
+-- examples/traces/             versioned bicg trace and manifest
+-- datasets/                    fixed corpus lists, trace archive, and checksum
+-- tests/                       unit and trace-replay regression tests
+-- docs/                        algorithm and integration documentation
+-- environment.yml              pinned search-replay environment
+-- REPRODUCING.md               complete runnable instructions
```

## Core API

```python
from sgrm import SGRMOptimizer

optimizer = SGRMOptimizer(
    backend,
    epsilon=0.0,
    budget=1000,
    seed=1,
)
evaluated_points = optimizer.solve()
best = optimizer.get_best_feasible()
```

`backend` implements the `EvaluationBackend` protocol in `src/sgrm/interfaces.py`. The included `LightningSimTraceBackend` is one concrete integration; other evaluators can implement the same protocol.

## Search state and stages

Channels with the same display name share one decision variable:

```text
(depth_lattice_index, implementation_type)
```

The implementation search uses `srl`, `lutram`, `bram`, and `uram`. The reference four-stage budget split is:

| Stage | Budget | Purpose |
|---|---:|---|
| Profile & Seed | 15% | Measure sensitivity and generate feasible seeds |
| Guided Shrink | 50% | Apply guided one-step, halved, and minimum-depth moves |
| Coordinated Moves | 25% | Explore interacting groups and a reduced exact core |
| Final Halve & Flip | 10% | Refine individual depths and implementation choices |

Unused evaluations roll forward. `get_best_feasible()` returns the lowest-cost evaluated point satisfying the original latency and deadlock constraints.

## Resource objective

The VCK190-normalized objective used by the reference search is:

```text
Cutil = (BRAM/967 + URAM/463 + FF/1,799,680 + LUT/899,840) / 4
```

The JSON output reports both `cutil_reduction_pct` and `raw_resource_reduction_pct`. The latter is based on the unweighted sum `BRAM + URAM + FF + LUT`; it is included to make the two quantities explicit rather than interchangeable.

## Reference configuration

The command-line tools apply these controls:

```text
SGRM_COST_MODE=util
SGRM_IMPL_TYPE_MOVES=1
SGRM_DEPTH_BASED_IMPL=1
SGRM_START_FROM_SMALL=1
SGRM_DEPTH_HALVE_NEIGHBOR=1
SGRM_STAGE5_HALVE_REFINE=1
SGRM_STAGES=1,2,3,4
```

`SGRM_STAGE5_HALVE_REFINE` is the backward-compatible internal name for the refinement orchestrated as public Stage 4.

## Documentation

- [Reproducing the trace searches](REPRODUCING.md)
- [Algorithm](docs/algorithm.md)
- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Evaluator interface](docs/interfaces.md)
- [Resource model](docs/resource-model.md)
- [Repository scope](docs/repository-scope.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

SGRM is licensed under the [Apache License 2.0](LICENSE). Attribution and
external-runtime information are recorded in [NOTICE](NOTICE).
