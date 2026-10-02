# Reproducing the SGRM searches

This guide reruns SGRM using the execution traces supplied with the repository.
Each result records the selected FIFO depths and implementations, latency,
modeled resources, search parameters, and evaluated-point count. The supplied
traces require no generation step or separate simulator command.

## Tested environment

| Component | Tested value |
|---|---|
| Operating system | Ubuntu 24.04.3, Linux 6.8, x86-64 |
| CPU | Intel Core Ultra 9 285K |
| Python | 3.12.13 |
| Conda | 26.1.1 |
| Peak resident memory | 380 MiB on `FeedForward_large` |

Allow approximately 1.5 GiB of free disk space for the environment, extracted 30-design bundle, and result files. CPU model and core count are not fixed requirements; searches run sequentially by default.

## Install

From the repository root:

```bash
conda env create --prefix ./.conda-sgrm --file environment.yml
conda activate ./.conda-sgrm
python -m pip install -e '.[dev]'
pytest
```

The local prefix keeps this installation separate from existing named
environments. `environment.yml` installs all pinned search-runtime dependencies
automatically; no additional simulator installation or configuration is needed.

## Included bicg trace

The repository versions one small real trace. Use its supplied manifest to
run the search and integrity checks automatically:

```bash
sgrm-trace-batch \
  --manifest examples/traces/manifest.json \
  --output-dir results/bicg

sgrm-verify-results \
  --manifest examples/traces/manifest.json \
  --results-dir results/bicg
```

The manifest supplies the reference checksums internally. You do not need to
read or manually enter them; results use the design name, such as `bicg.json`.

The verifier should print `PASS bicg`. Its reference checks include:

| Field | Expected value |
|---|---:|
| FIFO count | 44 |
| Group count | 2 |
| Evaluated points | 11 |
| Selected evaluation | 3 |
| Baseline latency | 835 |
| Selected latency | 834 |
| Baseline BRAM/URAM/FF/LUT | 0 / 0 / 572 / 2883 |
| Selected BRAM/URAM/FF/LUT | 0 / 0 / 572 / 2508 |
| Cutil reduction | 11.8333859262% |
| Raw resource-sum reduction | 10.8538350217% |

`selected_evaluation` is one-based: the baseline is evaluation 1, so a value of 3 means the selected point first appeared as the third distinct evaluated candidate. It is not a convergence threshold or the evaluation budget.

## Stream-HLS 30-design bundle

The versioned trace archive is tracked in this repository. Check and extract it
from the repository root:

```bash
python datasets/check_bundles.py
tar -xJf datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz
```

The bundle check must print:

```text
PASS datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz
PASS datasets/sgrm-stream-hls-30-sources-v0.1.0.tar.xz
```

Run and verify all 30 designs:

```bash
sgrm-trace-batch \
  --manifest sgrm-stream-hls-30-traces-v0.1.0/manifest.json \
  --output-dir results/stream-hls-30

sgrm-verify-results \
  --manifest sgrm-stream-hls-30-traces-v0.1.0/manifest.json \
  --results-dir results/stream-hls-30
```

Success is 30 `PASS` lines from each command. On the tested workstation, the sequential batch completed in approximately 127 seconds; timing is system-dependent and is not a reference-value check.

The batch runner continues after a per-design error and returns a nonzero exit status if any design fails. Add `--fail-fast` to stop at the first failure. Every result is written as `<design>.json`; `index.json` records the manifest checksum, global parameters, statuses, and total wall time.

Each design has a default 1,800-second timeout. Use `--timeout-s 3600`, for example, to
adjust it for the host system.

## Result validation

`sgrm-verify-results` checks:

- manifest and result schema versions;
- design identity and trace integrity metadata;
- non-deadlocking baseline and selected points;
- `selected_latency <= baseline_latency * (1 + epsilon)`;
- non-negative integer BRAM, URAM, FF, and LUT values;
- raw-resource sums, VCK190-normalized Cutil, and both reductions;
- one-based selected-evaluation bounds; and
- the reference subset stored in the manifest.

Platform strings, output paths, timestamps, and wall times are recorded for provenance but are not required to match.

## Optional post-synthesis resource validation

The search JSON records analytical resource estimates and concrete FIFO choices.
Actual synthesized resources are obtained by applying those choices to the
original C++ designs and synthesizing the resulting RTL. The separate
[hardware-validation workflow](hardware_validation/README.md) supplies all 30
native sources and builds 30 native/SGRM pairs with AMD 2024.2 tools. It reports
FIFO-subsystem measurements separately from the search-model estimates.

The approximately 1.5 GiB disk allowance above covers trace searches only.
Hardware-tool installation and generated synthesis projects require additional
storage and are not prerequisites for replaying or verifying the searches.
