# Reproduce SGRM from HLS sources

This workflow starts with the original C++ sources, generates execution traces,
and feeds those new traces directly into SGRM search and result verification.
It uses the complete local modified LightningSim source included in
[third_party/lightningsim](../third_party/lightningsim). The generator and SGRM
run in the same environment. The supplied traces are an alternative starting
point that skips synthesis and export, not a different search implementation.

The source includes the local Python/Rust backend, C++ trace runtime, FMADD
helpers, and the Versal schedule compatibility layer. The unchanged LLVM
instrumenter, SystemC runtime, and web assets come from the pinned upstream
LightningSim 0.2.6 Conda package. No private runtime binary is required.

## 1. Build the source-to-search environment

Run from the repository root on Linux x86-64 with Conda available:

```bash
conda env create --prefix ./.conda-sgrm-trace --file trace_generation/environment.yml
conda activate ./.conda-sgrm-trace
python trace_generation/build_local.py --jobs 4
python -m pip install -e '.[dev]'
python trace_generation/build_local.py --check-only
python -m pytest
```

Expected build/check output ends with `LOCAL LIGHTNINGSIM BUILD PASS`. The helper
compiles both the Rust extension and C++ trace runtime from the included source
and verifies the imported package/core locations and FMADD symbols. The editable
SGRM installation supplies the search, manifest, and verification commands.
Keep this environment activated for every step, including search; there is no
need to create or switch to `.conda-sgrm` for this workflow.
Use `--jobs 1` through `--jobs 16` to control build parallelism.

Keep this checkout at the same location after an editable installation; rebuild
if you move it. The commands below do not need Node.js or a web server.

## 2. Generate all 30 traces and run SGRM

The source archive contains all 30 original designs, not just `gemm`. After
step 1, run the [complete 30-design command block in the main README](../README.md#reproduce-all-30-searches-from-original-sources).
That block performs the following sequence without changing environments:

```text
original sources -> HLS C synthesis -> 30 new traces
                 -> new 30-design manifest -> SGRM search -> result verification
```

All artifacts stay under `results/source-to-search-30/`. The search reads
`traces/manifest.json` in that directory and writes `search/<design>.json`;
it never substitutes the supplied trace archive. The design-list check prevents
an incomplete export from becoming a successful smaller batch. A dedicated
source copy also avoids reusing projects from a previous single-design test.

The block requires classic Vitis HLS 2024.2, VCK190 support, and licenses. It
does not require Vivado or a board, and does not run RTL co-simulation. Use
[tool selection](#tool-selection) if the AMD installation is not discovered.
For comparison against the verified supplied-trace results, continue with
[the reference check](../README.md#check-against-the-supplied-trace-search-results)
in the main README. For measured resources, continue to step 5 below.

## 3. Single-design walkthrough: gemm (optional)

Use this smaller walkthrough instead of step 2 to try one design first. Its
source and output directories are separate from the complete 30-design run.

To perform C synthesis, install Vitis HLS 2024.2 with its classic Tcl interface,
VCK190 device support, and required licenses. For an already completed project,
the exporter can use the matching 2024.2 headers/runtime directly as described
under [Export a fresh trace](#export-a-fresh-trace). Vivado is not required for
trace generation. No physical board is needed. AMD executables, headers, and
licenses are not distributed here.

For an existing completed HLS project, skip to
[Export a fresh trace](#export-a-fresh-trace). To start with the original `gemm`
source included in this repository:

```bash
python datasets/check_bundles.py
tar -xJf datasets/sgrm-stream-hls-30-sources-v0.1.0.tar.xz
```

Check the HLS tool using the repository's portable entry point. Do not call a
bare `vitis_hls` command: on some installations that name forwards to the
incompatible unified `vitis-run` interface.

```bash
python trace_generation/run_hls.py --stage check-tools
```

Expected output ends with `TOOL CHECK PASS`. This checks only HLS, not Vivado,
and does not start synthesis. The script discovers installed tools, rejects
incompatible launchers and releases, and privately loads the selected
installation's settings. It does not change your active Conda environment or
require installation paths to match another machine.

Preview the original `gemm` synthesis, then run it:

```bash
python trace_generation/run_hls.py --design gemm --stage plan
python trace_generation/run_hls.py --design gemm --stage run
```

All commands in this guide run from the repository root; do not `cd` into
`gemm`. The entry point checks the original source files and selects the
correct working directory automatically. Only `--stage run` starts synthesis.
It uses the original `hls.tcl` with `gemm syn`, retains `hls_synthesis.log`,
and checks the completed outputs needed by the exporter. The default stage is
`plan`, so omitting `--stage` cannot accidentally start a hardware job.

### Tool selection

For an installation outside common locations, add its actual installation
base to **each** tool-dependent command (replace the example path):

```bash
python trace_generation/run_hls.py --stage check-tools --tool-root /path/to/AMD
python trace_generation/run_hls.py --design gemm --stage plan --tool-root /path/to/AMD
python trace_generation/run_hls.py --design gemm --stage run --tool-root /path/to/AMD
```

Alternatively use `--vitis-hls /path/to/Vitis_HLS/2024.2/bin/vitis_hls` to select
a classic executable explicitly, or `--settings /path/to/site-settings.sh`
for trusted site-specific setup. Use the same selection options for the
export command. In the full 30-design loop, add them to both Python commands
inside the loop as well as its tool precheck. `--sources-dir` accepts a source
bundle extracted elsewhere. Use paths from your own installation.
Shell aliases do not select a tool for Python; use an executable path instead.

If only the unified CLI is installed, install the matching classic Tcl
interface for synthesis, or use an already completed 2024.2 project with the
runtime-only export described below.

Existing HLS projects and logs are not overwritten. Export a completed project
directly, or extract the archive in a new directory and pass `--sources-dir`
for an independent synthesis run.

The original settings are top `forward`, part `xcvc1902-vsva2197-2MP-e-S`,
10 ns, `open_solution -flow_target vivado`, and
`config_compile -unsafe_math_optimizations`. This step runs `csynth_design`;
it does not run RTL co-simulation or Vivado implementation. Do not alter these
settings when checking the reference workload.

### Export a fresh trace

With the generation environment active, run:

```bash
python trace_generation/generate_trace.py \
  --solution-dir sgrm-stream-hls-30-sources-v0.1.0/gemm/hls_gemm/solution1 \
  --output results/regenerated-traces/gemm/hls_gemm/solution1/trace.pkl
```

The command uses the same tool discovery as `run_hls.py` and loads settings
privately. If discovery needs help, add `--tool-root /path/to/AMD` or
`--vitis-hls /path/to/Vitis_HLS/2024.2/bin/vitis_hls`. An explicitly selected
incompatible executable is rejected, not silently replaced. `--settings` can
provide an additional trusted settings file. Neither your parent shell nor the
machine-local hardware-tool configuration is changed by this command.
Installations with a separate classic launcher and matching Vitis 2024.2
header/runtime directory are also supported.

For a completed synthesis project, the exporter only needs AMD's 2024.2
headers and runtime libraries, not an HLS launcher. On a container or a unified
Vitis installation, you can skip launcher discovery by specifying that directory
directly (replace the example path):

```bash
python trace_generation/generate_trace.py \
  --solution-dir sgrm-stream-hls-30-sources-v0.1.0/gemm/hls_gemm/solution1 \
  --hls-root /path/to/Vitis/2024.2 \
  --output results/runtime-only-traces/gemm/hls_gemm/solution1/trace.pkl
```

The directory must contain `include/Makefile.sysc.rules` and `include/hls_stream.h`
and must come from the 2024.2 installation used for synthesis. The synthesis
report's version is also checked. `--hls-root` is an alternative to
`--vitis-hls`/`--tool-root`; no HLS executable is invoked in this mode.

The exporter uses the completed synthesis reports and bitcode. Use
`--project-dir` when workload data resides outside the source project directory.

Expected `gemm` output contains:

```text
EXPORTED .../trace.pkl: 44 FIFOs, baseline 30014 cycles
```

The exporter never loads an old trace or overwrites an existing output. Along
with `trace.pkl`, it writes `trace.generation.json` and `trace.processes.json`,
including package/tool locations, FIFO metadata, baseline cycles, subprocess
exit codes, and captured output. `--debug` retains intermediate compilation
objects; it is not needed for normal use.

### Testbench exit status

Trace export and numerical testbench checks have separate statuses.
`trace.generation.json` records `trace_exported`, `testbench_passed`, and the
testbench exit code; `trace.processes.json` contains subprocess output.
An exported trace does not by itself mean the numerical assertions passed.

For an export that requires a successful testbench exit as well, add:

```bash
python trace_generation/generate_trace.py \
  --solution-dir sgrm-stream-hls-30-sources-v0.1.0/gemm/hls_gemm/solution1 \
  --output results/strict-traces/gemm/hls_gemm/solution1/trace.pkl \
  --strict-testbench
```

Incomplete synthesis, parsing/linking failures, invalid FIFO metadata, or
baseline deadlock always reject export.

## 4. Search and verify the single-design trace

Continue the `gemm` walkthrough in the same environment. If you followed the
full 30-design block in step 2, its search and verification are already included;
do not replace those paths with the single-design paths below.

```bash
sgrm-build-manifest \
  --trace-root results/regenerated-traces

sgrm-trace-batch \
  --manifest results/regenerated-traces/manifest.json \
  --output-dir results/regenerated-search \
  --budget 1000 --seed 1 --epsilon 0

sgrm-verify-results \
  --manifest results/regenerated-traces/manifest.json \
  --results-dir results/regenerated-search
```

Do not substitute a new trace into the published trace archive or reuse its old
manifest. Serialization can change file identity even when cycles and search
results agree. The new manifest automatically records the newly generated files.

To compare against the supplied-trace searches, first complete the standard
30-design search and verifier in the [main README](../README.md). Then add
`--expected-results-dir results/stream-hls-30` to `sgrm-build-manifest` above.
The regenerated-trace verifier will also compare the deterministic reference
fields, including baseline/selected cycles, resource estimates, evaluation
counts, and reductions. Runtime and serialized file identity are not expected
to be identical across machines or freshly generated traces.

For all 30 designs, use step 2 rather than manually repeating this example.
The full workflow supplies `--design-list datasets/stream_hls_30.txt` when
building the new manifest, making all 30 exports mandatory.

## 5. Prepare hardware sources for the regenerated search

The published source manifest is bound to the supplied traces. For a freshly
generated trace, first derive a separate mapping after checking exact FIFO IDs,
names, widths, baseline depths, and original source-file integrity:

```bash
python trace_generation/bind_sources.py \
  --sources-dir results/source-to-search-30/sources/sgrm-stream-hls-30-sources-v0.1.0 \
  --manifest results/source-to-search-30/traces/manifest.json \
  --output-dir results/source-to-search-30/hardware-sources

python hardware_validation/validate_hardware.py \
  --sources-dir results/source-to-search-30/hardware-sources \
  --results-dir results/source-to-search-30/search \
  --output-dir results/source-to-search-30/hardware \
  --stage prepare
```

These commands continue the full 30-design source-to-search run. Expected
preparation output begins with `PREPARED 30 paired designs (60 hardware jobs)`.
For a subset, add options such as `--design gemm` to the preparation command.
To continue the single-design walkthrough instead, substitute its source,
trace-manifest, and search-result paths and select `--design gemm`.
The original sources/manifest remain unchanged, and preparation does not start
synthesis. Subsequent resource measurements follow
[Hardware validation](../hardware_validation/README.md); the run/report stages
must use the same `results/source-to-search-30/hardware` output directory.

Only load trusted trace files. Pickle can execute code; an integrity check does
not make an untrusted pickle safe. See [Security](../SECURITY.md).

## Source and licensing

[Local changes](../third_party/lightningsim/LOCAL_CHANGES.md) identifies the
included adaptations and packaging-only adjustments. LightningSim retains its
upstream [AGPL-3.0-only license](../third_party/lightningsim/LICENSE) and
authorship. SGRM's top-level license does not replace that third-party license.
