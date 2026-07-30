# Repository scope

This repository contains the backend-neutral SGRM optimization core and one
optional integration for replaying pre-generated LightningSim traces.

## Included

- FIFO grouping and compact state representation
- Four-stage sensitivity-guided search
- Hard deadlock and latency feasibility policy
- BRAM/URAM/FF/LUT cost functions
- Concrete SRL/LUTRAM/BRAM/URAM implementation search
- Analytical FIFO resource model
- Evaluator protocols and result data structures
- Pre-generated LightningSim trace adapter
- Manifest-driven trace runner and result verifier
- Unit tests for core formulas and invariants

## Maintained as integrations

The following components are deliberately decoupled from the core package:

- candidate-evaluation backends other than the included trace adapter;
- benchmark sources and workload inputs;
- generated synthesis projects and reports;
- experiment orchestration and plotting pipelines; and
- baseline optimizer implementations.

This separation keeps SGRM reusable across evaluation systems and prevents generated artifacts from obscuring the optimization logic.
