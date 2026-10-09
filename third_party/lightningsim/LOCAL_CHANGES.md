# Local LightningSim source used by SGRM

This directory contains the local LightningSim source used by the SGRM
Stream-HLS workflow. The baseline package version is 0.2.6. Upstream authorship
and the AGPL-3.0-only license are retained in [LICENSE](LICENSE).

## Included local adaptations

- `src/fmadd_stub.cpp` and `Makefile`: host definitions for the float/double
  `_ssdm_op_FMADD` helpers emitted by Vitis HLS 2024.2.
- `src/trace.cpp`: unbuffered trace output, preventing truncated final records
  when a testbench terminates without flushing buffered output.
- `backend/lightningsim/runner.py`: prefer the bundled LLVM libraries for LLVM
  subprocesses and preserve the AMD runtime library path for testbench execution.
- `../../trace_generation/lightningsim_compat.py`: the original Versal
  `muxlogic` schedule/LLVM alignment, FIFO-event, and FIFO-width adaptations.
- Complete Python and Rust backend sources, including the local native core,
  rather than a replacement Python wrapper around a stock installed backend.

## Source-release packaging adjustments

The simulation and schedule-resolution algorithms are unchanged from the local
source snapshot. The release makes only these packaging adjustments:

- Use the active Python/Conda prefix rather than a build-machine placeholder
  for runtime and GUI asset locations.
- Include `Cargo.lock` and a copy of the upstream license in the backend source
  distribution.
- Build the backend and trace runtime from these sources in a separate
  environment; do not modify the supplied-trace search environment.

The unchanged upstream LLVM instrumenter, SystemC, and prebuilt web assets are
obtained from the pinned upstream Conda package. They are not copied from a
private machine. The included upstream recipe remains available for reference;
the supported SGRM build procedure is [Trace generation](../../trace_generation/README.md).

## Export behavior

The local runner resolves traces independently of the testbench's final exit
code. The SGRM exporter preserves this original behavior and records the exit
code and captured subprocess output. Exporting a resolved trace is not a
claim that reference-output assertions passed. `--strict-testbench` makes a
nonzero exit reject the export. No testbench assertion, input, reference output,
or floating-point tolerance is rewritten.

The completed-synthesis exporter checks real synthesis outputs directly and
does not wait for the optional `dut.hcp` marker. The original GUI's live
synthesis-watching behavior is left unchanged.
