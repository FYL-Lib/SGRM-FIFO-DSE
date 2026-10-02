# Repository scope

This repository contains SGRM and the files needed to reproduce its search
and post-synthesis resource measurements on 30 Stream-HLS designs.

## Included

- FIFO grouping and compact state representation
- Four-stage sensitivity-guided search
- Hard deadlock and latency feasibility policy
- BRAM/URAM/FF/LUT cost functions
- Concrete SRL/LUTRAM/BRAM/URAM implementation search
- Analytical FIFO resource model
- Evaluator protocols and result data structures
- Pre-generated execution traces and the SGRM replay adapter
- Manifest-driven trace runner and result verifier
- Unit tests for core formulas and invariants
- Versioned sources, testbenches, and workload inputs for 30 Stream-HLS designs
- Paired native/SGRM hardware-validation script and post-synthesis reporting

## Reproduction workflow

1. Install the pinned SGRM environment using `environment.yml`.
2. Run the searches and result checks in [Reproducing the SGRM searches](../REPRODUCING.md).
3. Prepare original and optimized C++ source copies from the search results.
4. Run synthesis and compare the measured FIFO-subsystem resources using
   [Hardware validation](../hardware_validation/README.md).

The supplied traces are ready for search replay; no separate simulator command
or trace-generation step is required. AMD tools are needed only for the
post-synthesis measurements and are not distributed with this repository.
