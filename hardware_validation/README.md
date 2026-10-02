# Paired hardware validation

This workflow measures the synthesized FIFO resources of the original 30
Stream-HLS designs and their SGRM-selected configurations. It takes your own
trace-search JSON results as input; it does not substitute an author's selected
configuration or use analytical estimates as measured resource values.

The versioned source bundle contains the original C++ kernels, C++ testbenches,
binary input/reference vectors, and original HLS scripts. Its manifest links
every source-level FIFO to the exact trace used by the search. Native kernels
are copied unchanged. Selected kernels receive the requested `STREAM` depth and
an explicit `bind_storage type=fifo impl=...` directive, including for SRL.

## Prerequisites

- Python 3.10 or newer; the validation script uses the standard library only.
- AMD **classic Vitis HLS 2024.2** and **Vivado 2024.2**, including the VCK190 part
  `xcvc1902-vsva2197-2MP-e-S` and the required tool licenses.
- A configured AMD environment in which `vitis_hls` can invoke Vivado. Tool
  binaries, licenses, and vendor headers are not included in this repository.
- Per-design JSON results from the [30-design trace searches](../REPRODUCING.md).

AMD tools are needed only for `--stage run` or `--stage all` (the default).
`--stage prepare` and `--stage report` do not require them. Search replay does
not require them either. Conda supplies the Python environment, not AMD tools.

Follow [Hardware tool setup](TOOLS.md) using your own installation paths, then
check both tools before starting any hardware job:

```bash
python hardware_validation/validate_hardware.py --stage check-tools
```

This standalone command checks resolved executable paths and versions only;
it needs no search results or prepared plan and does not start synthesis.
Both releases must be 2024.2. A newer `vitis_hls` wrapper that forwards to
`vitis-run` is rejected with instructions for selecting the classic interface.

The source archive is approximately 28 MiB compressed (37.8 MB uncompressed).
Hardware-tool installations and generated projects need additional disk space;
the 1.5 GiB search-replay allowance does not cover synthesis. Storage and runtime
depend on the chosen parallelism and tool installation. Start with one paired
design to measure the requirements on your host.

For scale, the author's retained native/SGRM projects for these 60 jobs occupy
approximately 10.8 GiB, excluding the AMD installation and transient working
files. This is an observed retained size, not a peak-space guarantee; reserving
several tens of GiB for generated projects is prudent. A 4 TB SSD is not a
minimum requirement.

## One command for all 60 jobs

For a smaller end-to-end check, start with the
[representative three-design workflow](QUICKSTART.md). It builds six jobs and
provides per-design reference measurements for direct comparison.

From the repository root, after completing the searches:

```bash
python hardware_validation/validate_hardware.py \
  --results-dir results/stream-hls-30 \
  --output-dir results/hardware-validation \
  --jobs 2
```

The script verifies and extracts the supplied source archive automatically,
checks all requested result files, prepares 30 native and 30 selected projects,
and runs them with at most two simultaneous hardware jobs. Use `--jobs 1` on a
memory-constrained host. Use `--vitis-hls` and `--vivado` with the full paths to
your classic HLS and Vivado executables to select installations explicitly;
see [the setup guide](TOOLS.md) for complete commands. `VITIS_HLS` and `VIVADO`
can also provide the executable paths. Explicit flags take precedence, then
these environment variables, then the commands found on `PATH`.

Every `run` or `all` invocation repeats the tool precheck before launching
jobs. Explicit tool selections apply to that invocation, so repeat the flags
when resuming. For standard AMD installations, the child-process environment
selects the checked HLS/Vivado roots and their executable directories; the
calling shell is not modified. The HLS Tcl export step invokes Vivado, rather
than treating `run_vivado.tcl` as a standalone Vivado Tcl script.

The default target is VCK190 at 10 ns. Both variants use the same compiler
settings from the reference measurement protocol, including unsafe
math optimizations and
`config_rtl -add_register_in_block_condition=false`. The latter is applied to
both variants; it does not change the native kernel's FIFO pragmas.

The measurement pipeline is:

1. `csynth_design`: generate RTL from each C++ configuration.
2. `export_design -flow syn -format ip_catalog`: invoke Vivado RTL synthesis.
3. Parse Vivado hierarchical reports and aggregate the FIFO subsystem.

The paper's resource measurements are post-synthesis, so placement and routing
are not required for this comparison. The script does not run RTL co-simulation
or claim that an HLS synthesis latency estimate is a measured co-simulation
cycle count.

## Inspect replacements or run a small test

Prepare all 60 projects without invoking either hardware tool:

```bash
python hardware_validation/validate_hardware.py \
  --results-dir results/stream-hls-30 \
  --output-dir results/hardware-validation \
  --stage prepare
```

The complete plan is saved as `plan.json`. Each job has a `configuration.json`
containing source checksums, the input result/trace checksums, and the exact
depth/implementation choices. Original bundle files are never rewritten.
The two configurations have readable paths:

```text
work/
  bicg/
    native/    original configuration
    sgrm/      optimized configuration
```

Checksums are internal verification metadata, not configuration names. The
workflow checks them automatically; no checksum needs to be typed manually.

To build only the native and selected `bicg` configurations:

```bash
python hardware_validation/validate_hardware.py \
  --results-dir results/stream-hls-30 \
  --output-dir results/bicg-hardware \
  --design bicg \
  --jobs 2
```

Repeat `--design` to select a larger subset. The default, with no `--design`, is
all 30 designs. Use a dedicated output directory for each subset/protocol.

## Resume and inspect results

Run an already prepared plan:

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/hardware-validation \
  --stage run \
  --jobs 2
```

Successful jobs are reused only when their configuration fingerprints and
report checksums still match. A failed Vivado export can resume after a
successful HLS stage. Failures retain logs and are marked explicitly; no failed
design is silently removed from a complete-corpus aggregate. A selected
configuration is rejected if HLS reports automatically increasing its FIFO
depth, rather than silently measuring a different configuration.

Existing plans created by earlier releases remain supported: `run` and
`report` use the work directories recorded in their plan. When preparing
after a script update or changing FIFO choices, choose a fresh `--output-dir`
to preserve existing projects and measurements.

Rebuild the summaries without invoking tools:

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/hardware-validation \
  --stage report
```

Outputs:

- `hardware_resources.csv`: raw FIFO-subsystem BRAM, URAM, FF, LUT counts,
  normalized cost, timing estimates, status, and report locations for each job.
- `paired_comparison.csv`: native/selected costs and resource reductions for
  each completed pair.
- `summary.json`: completed coverage and geometric-mean resource reduction.
- `work/<design>/native/`: original C++ configuration, scripts, logs, and reports.
- `work/<design>/sgrm/`: optimized C++ configuration, scripts, logs, and reports.

`PASS` requires all requested native/selected pairs to succeed. Partial results
are labeled `INCOMPLETE` and the command returns a nonzero exit status.

## Resource metric and interpretation

The FIFO subsystem includes FIFO storage and its synthesized control logic.
The parser aggregates inclusive, outermost FIFO module rows and excludes
parenthesized self rows to avoid counting nested resources twice. BRAM is
reported in 36-Kbit tile equivalents: `RAMB36 + RAMB18/2`.

For the VCK190 target:

```text
Cutil = (BRAM/967 + URAM/463 + FF/1,799,680 + LUT/899,840) / 4
ratio[design] = Cutil_selected[design] / Cutil_native[design]
geometric-mean reduction (%) = 100 * (1 - geometric_mean(ratio))
```

These exact device capacities match the search model. An archived table using
rounded FF/LUT capacities can differ slightly before rounding. The CSV also
records HLS schedule estimates and post-synthesis clock estimates, clearly
separated from the trace latencies in the search results.

Search time is copied from the supplied JSON `wall_time_s`, including trace
initialization; it is not the paper's optimizer-only wall time. HLS and Vivado
durations are recorded separately. No synthesis time is counted as DSE time.

The 60-job workflow verifies **SGRM versus the original Stream-HLS baseline**.
It does not rerun FIFOAdvisor's five optimizers. Reconstructing a chart that
also includes those methods requires their separately recorded measurements
or independently synthesizing their selected configurations.

## Input and provenance checks

Results must match the source manifest's design name, trace checksum, and full
FIFO-ID set. Every selected FIFO must have an explicit SRL/LUTRAM/BRAM/URAM
implementation. The script rejects infeasible trace results, malformed values,
or differing choices for elements represented by a single source declaration;
it never collapses differing choices by silently taking the maximum depth.

The bundle preserves the original Stream-HLS MIT license as
`STREAM_HLS_LICENSE` and provides `PROVENANCE.txt` and per-file integrity records.
See [NOTICE](../NOTICE). The original HLS scripts are included for inspection;
generated run scripts apply the common validation protocol described above.
