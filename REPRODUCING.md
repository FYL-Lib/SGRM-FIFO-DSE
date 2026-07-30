# Reproducing the trace searches

This guide runs SGRM against pre-generated LightningSim traces. Each result records the trace checksum, parameters, evaluated-point count, selected point, latency, modeled FIFO resources, and both normalized and raw resource reductions.

## Tested environment

| Component | Tested value |
|---|---|
| Operating system | Ubuntu 24.04.3, Linux 6.8, x86-64 |
| CPU | Intel Core Ultra 9 285K |
| Python | 3.12.13 |
| Conda | 26.1.1 |
| LightningSim | 0.2.6 |
| llvmlite | 0.43.0 |
| Peak resident memory | 380 MiB on `FeedForward_large` |

Allow approximately 1.5 GiB of free disk space for the environment, extracted 30-design bundle, and result files. CPU model and core count are not fixed requirements; searches run sequentially by default.

## Install

From the repository root:

```bash
conda env create --file environment.yml
conda activate sgrm
python -m pip install -e '.[dev]'
pytest
```

The environment file pins the versions used to serialize and replay the supplied trace objects. If an older local Conda package cache has been modified, create the environment with a clean package cache before diagnosing the repository itself.

## Included bicg trace

The repository versions one small real trace. Run it directly:

```bash
sgrm-trace-search \
  --solution-dir examples/traces/bicg/solution1 \
  --design bicg \
  --budget 1000 \
  --seed 1 \
  --epsilon 0 \
  --expected-trace-sha256 88c68755bfbf99d6c451765fdc89b69d2fc7604632790b5860cce273553aca63 \
  --output results/bicg.json
```

Or use the manifest-driven path:

```bash
sgrm-trace-batch \
  --manifest examples/traces/manifest.json \
  --output-dir results/bicg

sgrm-verify-results \
  --manifest examples/traces/manifest.json \
  --results-dir results/bicg
```

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
sha256sum --check datasets/SHA256SUMS
tar -xJf datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz
```

The checksum command must print:

```text
datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz: OK
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

Each design has a default 1,800-second timeout. Use `--timeout-s <seconds>` to
adjust it for the host system.

## Result validation

`sgrm-verify-results` checks:

- manifest and result schema versions;
- design identity and trace SHA-256;
- non-deadlocking baseline and selected points;
- `selected_latency <= baseline_latency * (1 + epsilon)`;
- non-negative integer BRAM, URAM, FF, and LUT values;
- raw-resource sums, VCK190-normalized Cutil, and both reductions;
- one-based selected-evaluation bounds; and
- the reference subset stored in the manifest.

Platform strings, output paths, timestamps, and wall times are recorded for provenance but are not required to match.

## Building a manifest for another trace bundle

The manifest builder expects this archival layout:

```text
<trace-root>/
  <design>/
    hls_<design>/
      hls.app
      solution1/
        trace.pkl
```

Then run:

```bash
sgrm-build-manifest --trace-root <trace-root>
```

The included `bicg` fixture uses a deliberately compact layout and ships with
its manifest already generated; it is not an input to this archival-layout
builder example.

An optional newline-delimited corpus file can fix membership and ordering:

```bash
sgrm-build-manifest \
  --trace-root <trace-root> \
  --design-list datasets/stream_hls_30.txt
```

The generated `manifest.json` stores per-trace size and SHA-256 metadata. Use `--expected-results-dir <directory>` to embed a validated reference subset from existing per-design JSON results.
