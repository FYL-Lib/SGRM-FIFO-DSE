# SGRM

**Sensitivity-Guided Resource Minimization for FIFO Design-Space Exploration in HLS Dataflow Designs**

SGRM jointly optimizes FIFO depth and storage implementation while enforcing
deadlock freedom and a hard latency constraint on the evaluated execution trace:

```text
latency <= baseline_latency * (1 + epsilon)
```

The reference configuration uses `epsilon = 0`. The repository provides the
execution traces needed to rerun SGRM on all 30 Stream-HLS designs. The
four-stage optimizer uses these traces and the analytical FIFO resource model
to select FIFO configurations. The hardware-validation workflow applies those
configurations to the original C++ designs and measures their FIFO-subsystem
resources with Vitis HLS and Vivado.

## Install

Run commands from the repository root in a Linux x86-64 terminal with Conda
available. The local prefix avoids changing an existing named environment.

```bash
conda env create --prefix ./.conda-sgrm --file environment.yml
conda activate ./.conda-sgrm
python -m pip install -e '.[dev]'
python -m pytest
```

The environment file installs Python 3.12 and all pinned search dependencies
automatically. The supplied traces are ready to use: no separate simulator
setup, simulator command, or trace-generation step is required.
Confirm that Python imports this checkout:

```bash
python -c "import sgrm; print(sgrm.__file__)"
```

If you already installed the search environment, you can reuse it and start
with the archive checks below.

## Reproduce the 30 SGRM searches

Verify both supplied archives before extracting or replaying traces:

```bash
python datasets/check_bundles.py
```

Both the trace archive and the source archive must report `PASS`. Then run:

```bash
tar -xJf datasets/sgrm-stream-hls-30-traces-v0.1.0.tar.xz

sgrm-trace-batch \
  --manifest sgrm-stream-hls-30-traces-v0.1.0/manifest.json \
  --output-dir results/stream-hls-30 \
  --budget 1000 \
  --seed 1 \
  --epsilon 0

sgrm-verify-results \
  --manifest sgrm-stream-hls-30-traces-v0.1.0/manifest.json \
  --results-dir results/stream-hls-30
```

Each command should print 30 `PASS` lines with no `FAIL`. The sequential
search batch took approximately 127 seconds on the tested workstation; runtime
on other hosts can differ. These steps do not invoke AMD tools.

Each `results/stream-hls-30/<design>.json` records the selected FIFO depths,
explicit storage implementations, trace latency, modeled resources, and
provenance. `index.json` records the batch status. The resource estimates in
the search JSON are distinct from the post-synthesis measurements below.

See [Reproducing the SGRM searches](REPRODUCING.md) for the small included
`bicg` example, exact environment details, and result-field definitions.

## Reproduce post-synthesis resources

The source archive contains all 30 original Stream-HLS C++ designs,
testbenches, workload inputs, and source/trace mappings. Start with three
representative designs: `bicg`, `gemm`, and `ResidualBlock`. They cover
LUT/SRL-, BRAM-, and URAM-backed FIFO configurations.

### 1. Prepare the original and selected source copies

After the searches, run:

```bash
python hardware_validation/validate_hardware.py \
  --results-dir results/stream-hls-30 \
  --output-dir results/three-design-hardware \
  --design bicg \
  --design gemm \
  --design ResidualBlock \
  --stage prepare
```

Expected output begins with `PREPARED 3 paired designs (6 hardware jobs)`.
This step automatically verifies/extracts the source bundle and generates an
unchanged native copy and an SGRM-selected copy for each design. It does not
start synthesis or modify the archived originals.

The selected copies use your own search JSON, not a preselected configuration.
Every selected FIFO receives its requested depth and an explicit
`bind_storage type=fifo impl=...` directive, including SRL.

Inspect the generated files with actual shell commands:

```bash
ls -l results/three-design-hardware/

diff -u \
  results/three-design-hardware/work/bicg/native/src/bicg.cpp \
  results/three-design-hardware/work/bicg/sgrm/src/bicg.cpp
```

Changes to FIFO pragmas are expected. `diff` returns status 1 when it finds
differences. Each work directory also contains `configuration.json`,
`run_hls.tcl`, and `run_vivado.tcl`; `plan.json` records all six jobs.
Do not edit staged files after preparation: their checksums are verified.
Directory names use the design and configuration labels, such as
`work/bicg/native` and `work/bicg/sgrm`. Integrity metadata is handled
automatically; you do not need to read or enter checksum values.

### 2. Configure the hardware tools

Install and configure **Vitis HLS 2024.2** and **Vivado 2024.2**, including
the VCK190 part `xcvc1902-vsva2197-2MP-e-S` and the required licenses.
Load the installation's `settings64.sh` files as appropriate for your host,
then check:

```bash
vitis_hls -version
vivado -version
python -c "import sgrm; print(sgrm.__file__)"
```

Both AMD tools must report 2024.2, and Python should still import this
checkout. Tool binaries, licenses, and vendor headers are not included.
No physical board is required. Both configurations target VCK190 at 10 ns.

### 3. Run the six hardware jobs

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/three-design-hardware \
  --stage run \
  --jobs 2
```

This runs HLS RTL generation followed by Vivado RTL synthesis, with at most
two simultaneous hardware jobs. Placement/routing is not needed for these
post-synthesis resource measurements. The workflow does not run RTL
co-simulation.

The native/selected `bicg` pair took approximately 40.5 minutes with two
parallel jobs on the tested workstation; `ResidualBlock` took approximately
3.5 minutes. These are per-design observations, not a promised six-job total.

Logs, exact sources, scripts, and raw reports are retained in each work
directory. Repeat the run command to resume: successful jobs are reused only
when their input fingerprints and report checksums still match. The
`run` and `report` stages use the existing plan; do not repeat
`--design` or `--results-dir` for those stages.

### 4. Collect results and check the reference measurements

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/three-design-hardware \
  --stage report

python hardware_validation/check_reference.py \
  --output-dir results/three-design-hardware
```

Neither command starts synthesis. The outputs are:

- `hardware_resources.csv`: measured FIFO-subsystem resources for all six jobs.
- `paired_comparison.csv`: native/selected normalized costs and reductions.
- `summary.json`: completion status, coverage, and the subset geometric mean.

A complete run reports `PASS`, three completed pairs, and six successful
hardware jobs. The reference checker compares all four resource counts,
FIFO counts, costs, and reductions against
[three_design_reference.json](hardware_validation/three_design_reference.json).

Expected checker output (for comparison only, not shell commands):

```text
PASS bicg: measured FIFO-subsystem reduction 21.4772%
PASS gemm: measured FIFO-subsystem reduction 97.0465%
PASS ResidualBlock: measured FIFO-subsystem reduction 98.2470%
REFERENCE MATCH: 3 paired designs
```

These correspond to the paper's per-design resource reductions of 21.5%,
97.0%, and 98.2%. They cover the FIFO subsystem, including storage and
synthesized control logic, rather than whole-design resource totals. The
three-design aggregate is not the paper's 30-design geometric mean. The
HLS schedule estimate in the CSV is not an RTL co-simulation cycle count.

For the complete 30-design measurement, use a new output directory and omit
the three `--design` options when preparing the plan. That creates 60 jobs.
See [Hardware validation](hardware_validation/README.md) for the full protocol
and [Three-design walkthrough](hardware_validation/QUICKSTART.md) for the
raw resource reference table.

## Storage requirements

Allow approximately 1.5 GiB for the search environment, extracted traces, and
search results. The source archive is approximately 28 MiB compressed.
AMD installation and generated synthesis projects require additional space.
Retained projects for all 60 hardware jobs occupied approximately 10.8 GiB
on the tested workstation, excluding the tools and transient files; reserve
several tens of GiB for generated projects. A 4 TB SSD is not a minimum
requirement.

## Algorithm and resource objective

SGRM groups structurally equivalent channels and searches concrete `srl`,
`lutram`, `bram`, and `uram` implementations in four stages:

| Stage | Role |
|---|---|
| Profile & Seed | Measure sensitivity and construct feasible seeds |
| Guided Shrink | Apply prioritized depth and implementation moves |
| Coordinated Moves | Explore interacting groups and a reduced exact core |
| Final Halve & Flip | Refine depths and implementation choices |

The normalized VCK190 resource objective is:

```text
Cutil = (BRAM/967 + URAM/463 + FF/1,799,680 + LUT/899,840) / 4
reduction (%) = 100 * (1 - Cutil_selected / Cutil_native)
```

BRAM measurements use 36-Kbit tile equivalents: `RAMB36 + RAMB18/2`.
Corpus resource reduction is computed from the geometric mean of per-design
selected/native cost ratios.

## Documentation

- [SGRM searches](REPRODUCING.md)
- [Hardware validation](hardware_validation/README.md)
- [Three-design walkthrough](hardware_validation/QUICKSTART.md)
- [Algorithm](docs/algorithm.md)
- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Evaluator interface](docs/interfaces.md)
- [Resource model](docs/resource-model.md)
- [Repository scope](docs/repository-scope.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

Traces are serialized Python pickle files. Load only trusted bundles after
verifying their checksums; see [Security](SECURITY.md).

## License

SGRM is licensed under [Apache License 2.0](LICENSE). Upstream Stream-HLS
benchmark sources retain their MIT license, included in the source bundle.
External-runtime information and attribution are recorded in [NOTICE](NOTICE).
