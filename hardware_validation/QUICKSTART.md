# Representative three-design hardware check

Use `bicg`, `gemm`, and `ResidualBlock` to check the complete source-rewriting
and post-synthesis measurement workflow before running all 30 designs. These
three cases cover LUT/SRL-dominated FIFOs, BRAM-backed FIFOs, and URAM-backed
FIFOs. They are selected for distinct storage/resource behavior, not simply
for the shortest synthesis time.

## 1. Prepare six source configurations

After the [trace-search workflow](../REPRODUCING.md) has produced the search
JSON files, run the following from the repository root. This preparation step
does not require AMD tools:

```bash
python hardware_validation/validate_hardware.py \
  --results-dir results/stream-hls-30 \
  --output-dir results/three-design-hardware \
  --design bicg \
  --design gemm \
  --design ResidualBlock \
  --stage prepare
```

Only these three JSON files are needed by this command. The script prepares
native and SGRM-selected configurations for each design, for six hardware jobs
in total. The source archive is verified and extracted automatically. Expect
`PREPARED 3 paired designs (6 hardware jobs)`.

Inspect the generated files with these shell commands:

```bash
ls -l results/three-design-hardware/

diff -u \
  results/three-design-hardware/work/bicg/native/src/bicg.cpp \
  results/three-design-hardware/work/bicg/sgrm-*/src/bicg.cpp
```

FIFO pragma changes are expected; `diff` returns status 1 when it finds them.
`plan.json` records the jobs, and each work directory contains its exact
configuration and generated Tcl scripts. File and directory names shown in
documentation are for inspection, not commands to paste into a shell.

## 2. Run HLS and Vivado RTL synthesis

Configure AMD Vitis HLS/Vivado 2024.2, the VCK190 part, and licenses as described
in [Hardware validation](README.md). Then run:

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/three-design-hardware \
  --stage run \
  --jobs 2
```

This builds the six prepared jobs. Repeating the command safely reuses completed
jobs whose fingerprints and report checksums match. The existing plan defines
the subset; omit `--design` and `--results-dir` from `run` and `report` commands.

## 3. Collect and check the measurements

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/three-design-hardware \
  --stage report

python hardware_validation/check_reference.py \
  --output-dir results/three-design-hardware
```

These commands do not invoke synthesis. The second checks the four resource
counts, FIFO counts, normalized costs, and reductions against the recorded
reference. A matching run prints three `PASS` lines followed by
`REFERENCE MATCH: 3 paired designs`.

## Check the expected resources

The following are recorded **Vivado post-synthesis FIFO-subsystem** resources,
not the analytical resource estimates in the search JSON. BRAM is in 36-Kbit
tile equivalents. Original and selected configurations use the same VCK190,
10 ns, and compiler settings documented in [Hardware validation](README.md).

| Design | Configuration | BRAM | URAM | FF | LUT |
|---|---|---:|---:|---:|---:|
| bicg | Native | 0 | 0 | 754 | 2412 |
| bicg | SGRM | 0 | 0 | 494 | 1943 |
| gemm | Native | 88 | 0 | 2156 | 2594 |
| gemm | SGRM | 0 | 0 | 3080 | 987 |
| ResidualBlock | Native | 0 | 32 | 1172 | 1283 |
| ResidualBlock | SGRM | 0 | 0 | 944 | 651 |

The corresponding normalized FIFO-subsystem cost reductions are 21.48%,
97.05%, and 98.25%, respectively. Full-precision reference values and the
normalization capacities are in
[three_design_reference.json](three_design_reference.json).

Compare these values with `hardware_resources.csv` and
`paired_comparison.csv` in the output directory. A complete hardware run
reports `PASS`, `completed_pair_count: 3`, and
`successful_hardware_job_count: 6` in `summary.json`. All underlying sources,
logs, and hierarchical resource reports remain available under `work/`.

The three-design aggregate verifies this subset; it is not the paper's
30-design geometric-mean result. To reproduce the entire corpus, use a new
output directory and omit the three `--design` options. That builds 60 jobs.

## Time and full results

Hardware synthesis is much more time-consuming than replaying the searches.
On the author's workstation, a fresh native/selected `bicg` pair ran in
approximately 40.5 minutes with two parallel jobs; `ResidualBlock` took
approximately 3.5 minutes under the same two-job setting. Runtime varies by
design and host; these are per-design measurements, not a prediction for the
complete six-job batch.

The full 30-design workflow is available in [Hardware validation](README.md).
