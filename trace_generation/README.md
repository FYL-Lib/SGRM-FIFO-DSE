# Generate traces from HLS sources

This optional workflow builds and uses the complete local modified LightningSim
source included in [third_party/lightningsim](../third_party/lightningsim).
It is independent of the standard supplied-trace searches: those searches do
not require this setup or AMD tools.

The source includes the local Python/Rust backend, C++ trace runtime, FMADD
helpers, and the Versal schedule compatibility layer. The unchanged LLVM
instrumenter, SystemC runtime, and web assets come from the pinned upstream
LightningSim 0.2.6 Conda package. No private runtime binary is required.
See [Validation results](VALIDATION.md) for the tested environment and the
separate trace-export, search-reproduction, and testbench-status checks.

## 1. Build in a separate environment

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
and verifies the imported package/core locations and FMADD symbols. It does not
modify `.conda-sgrm`, old experiment environments, or shared Conda cache files.
Use `--jobs 1` through `--jobs 16` to control build parallelism.

Keep this checkout at the same location after an editable installation; rebuild
if you move it. The upstream GUI source and build recipe are retained, but the
headless export commands below do not need Node.js or a web server.

## 2. Complete HLS C synthesis

To perform C synthesis, install Vitis HLS 2024.2 with its classic Tcl interface,
VCK190 device support, and required licenses. For an already completed project,
the exporter can use the matching 2024.2 headers/runtime directly as described
in step 3. Vivado is not required for trace generation. No physical board is
needed. AMD executables, headers, and licenses are not distributed here.

For an existing completed HLS project, skip to step 3. To start with the original
`gemm` source included in this repository:

```bash
python datasets/check_bundles.py
tar -xJf datasets/sgrm-stream-hls-30-sources-v0.1.0.tar.xz
```

Load your own installation's settings file; replace the example path with the
actual path on your machine:

```bash
source /path/to/Vitis_HLS/2024.2/settings64.sh
```

Then run the original synthesis script:

```bash
(
  cd sgrm-stream-hls-30-sources-v0.1.0/gemm
  vitis_hls -l hls_synthesis.log hls.tcl gemm syn
)
```

The original settings are top `forward`, part `xcvc1902-vsva2197-2MP-e-S`,
10 ns, `open_solution -flow_target vivado`, and
`config_compile -unsafe_math_optimizations`. This step runs `csynth_design`;
it does not run RTL co-simulation or Vivado implementation. Do not alter these
settings when checking the reference workload.

## 3. Export a fresh trace

With the generation environment active, run:

```bash
python trace_generation/generate_trace.py \
  --solution-dir sgrm-stream-hls-30-sources-v0.1.0/gemm/hls_gemm/solution1 \
  --output results/regenerated-traces/gemm/hls_gemm/solution1/trace.pkl
```

The command automatically discovers classic Vitis HLS 2024.2 and loads its
settings privately. If discovery needs help, add `--tool-root /path/to/AMD` or
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

The exporter checks the completed synthesis reports and bitcode directly. It
does not wait for or fabricate `.autopilot/db/dut.hcp`. It sets `PRJ_PATH` to the
source project directory, uses the built local fork, applies the original
Versal compatibility layer, and raises only its own stack soft limit for large
testbenches. Use `--project-dir` when workload data resides elsewhere.

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

The original local runner resolves execution traces separately from the final
testbench exit code. The exporter preserves that policy and records both
`trace_exported` and `testbench_passed`. Console output reports trace-export
progress; the generation record retains the original testbench exit status.
Some original
floating-point reference-output assertions can fail after the kernel finishes;
the exporter does not edit assertions, inputs, reference outputs, or tolerance.

The exporter does not add duplicate warning messages to the console or logs.
The original return code and `testbench_passed` remain in
`trace.generation.json`, and captured subprocess output remains in
`trace.processes.json`. Saving a trace is not a claim that the numerical
testbench check passed.

For an export that requires a successful testbench exit as well, add:

```bash
python trace_generation/generate_trace.py \
  --solution-dir sgrm-stream-hls-30-sources-v0.1.0/gemm/hls_gemm/solution1 \
  --output results/strict-traces/gemm/hls_gemm/solution1/trace.pkl \
  --strict-testbench
```

Failed parsing, linking, incomplete synthesis, invalid FIFO metadata, or baseline
deadlock always rejects export. Inspect the generation/process
logs rather than treating a saved trace as a functional-verification result.

## 4. Search and verify the regenerated traces

In the same generation environment:

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

The same synthesis/export procedure applies to each design in
`datasets/stream_hls_30.txt`. Keep the output layout
`<design>/hls_<design>/solution1/trace.pkl` so the manifest builder can discover
the regenerated designs. Add `--design-list datasets/stream_hls_30.txt` to the
manifest command to require all 30 traces rather than a selected subset.

## 5. Prepare hardware sources for a regenerated search

The published source manifest is bound to the supplied traces. For a freshly
generated trace, first derive a separate mapping after checking exact FIFO IDs,
names, widths, baseline depths, and original source-file integrity:

```bash
python trace_generation/bind_sources.py \
  --sources-dir sgrm-stream-hls-30-sources-v0.1.0 \
  --manifest results/regenerated-traces/manifest.json \
  --output-dir results/regenerated-sources

python hardware_validation/validate_hardware.py \
  --sources-dir results/regenerated-sources \
  --results-dir results/regenerated-search \
  --output-dir results/regenerated-hardware \
  --design gemm \
  --stage prepare
```

Expected preparation output begins with `PREPARED 1 paired designs (2 hardware
jobs)`. The original sources/manifest remain unchanged, and preparation does
not start synthesis. Subsequent resource measurements follow
[Hardware validation](../hardware_validation/README.md).

Only load trusted trace files. Pickle can execute code; an integrity check does
not make an untrusted pickle safe. See [Security](../SECURITY.md).

## Source and licensing

[Local changes](../third_party/lightningsim/LOCAL_CHANGES.md) identifies the
included adaptations and packaging-only adjustments. LightningSim retains its
upstream [AGPL-3.0-only license](../third_party/lightningsim/LICENSE) and
authorship. SGRM's top-level license does not replace that third-party license.
