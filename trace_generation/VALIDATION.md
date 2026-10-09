# Local-source workflow validation

The included fork was built in a new, separate Conda environment on Ubuntu
24.04.3 LTS, Linux x86-64. The validation used completed Vitis HLS 2024.2
(build 5238294) synthesis outputs from the original 30-design source bundle,
VCK190, and a 10 ns target clock. Original benchmark files were checked
against the source archive before reusing these outputs. No HLS or Vivado
rerun is required for the source-build/export checks below.

## Tested environment

| Component | Version |
|---|---|
| Python | 3.12.15 |
| Local LightningSim source baseline | 0.2.6 |
| llvmlite | 0.43.0 |
| LLVM instrumenter | 15.0.5 |
| Rust | 1.85.1 |
| GCC/G++ | 16.2.0 |
| setuptools-rust | 1.13.0 |

These are the versions of this source-release validation, not a claim about
the compiler patch versions used when the archived traces were first created.
The local Python/Rust backend and C++ trace runtime were rebuilt from the
included source, not taken from an old experiment environment.

## Results

| Check | Result |
|---|---|
| Source build and local module/runtime checks | Pass |
| Unit and supplied-trace regression tests, in each new environment | 129 passed |
| Supplied-trace searches equal published reference summaries | 30/30 |
| Fresh trace export under the original local policy | 30/30 |
| Baseline cycles equal published references | 30/30 |
| FIFO ID/name/width/baseline-depth contracts equal original sources | 30/30 |
| Regenerated-trace searches equal published reference summaries | 30/30 |
| Selected FIFO depths and explicit implementations equal supplied-trace searches | 30/30 |
| Mean selected-solution evaluation index | 215.8, matching the paper |
| Trace-level latency regressions | 0/30 |
| Original/optimized source preparation | 30 pairs, 60 hardware jobs prepared |
| Strict `gemm` export with launcher discovery | Pass |
| Strict `gemm` export with explicit runtime directory, no HLS invocation | Pass |

The searches used budget 1000, seed 1, and epsilon 0. Reference-summary checks
cover FIFO/group counts, evaluation counts and selected index, baseline and
selected cycles, analytical resources, and resource-reduction metrics. They
do not substitute for a new post-synthesis resource measurement or assert
byte-identical pickle serialization. A separate per-design comparison also
checks every baseline/selected FIFO depth and explicit storage implementation,
not only the summary metrics. All 30 complete configurations match.

Both strict `gemm` exports produced 44 FIFOs and a baseline of 30,014 cycles.
No old trace was loaded in the fresh-export pipeline. Source preparation used
a separate derived manifest after checking the regenerated FIFO contracts;
the published archives and original source manifest were left unchanged.

## Testbench status is separate from trace export

Of the 30 instrumented testbenches, 19 exited successfully. The other 11
triggered reference-output assertions in the unchanged testbench sources:

- `k15mmseq_balanced`, `k15mmseq_balanced_relu`
- `k15mmseq_unbalanced`, `k15mmseq_unbalanced_relu`
- `k15mmtree_balanced`, `k15mmtree_balanced_relu`
- `k15mmtree_unbalanced`, `k15mmtree_unbalanced_relu`
- `k7mmseq_balanced_wide`
- `k7mmtree_balanced_relu`, `k7mmtree_unbalanced_relu`

Their traces resolved, and their cycles, FIFO contracts, and search reference
summaries matched. This is not a passing functional test for those 11 designs.
The exporter records their nonzero return codes and subprocess output, preserving
the original local export policy. `--strict-testbench` rejects these exports.
No assertion, input, reference output, or numerical tolerance was changed.
