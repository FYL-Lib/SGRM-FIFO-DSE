# Changelog

All notable changes to the SGRM core package are documented here.

## 0.1.0

- Published the four-stage SGRM optimization core.
- Added grouped FIFO depth and concrete implementation decisions.
- Added capacity-normalized BRAM/URAM/FF/LUT objectives.
- Added cooperative pair/triple moves and reduced-core enumeration.
- Added final binary depth refinement with implementation selection.
- Added a passive feasible Pareto archive.
- Ensured Stage 1 produces a complete downstream state when its bounded budget cannot profile every group.
- Isolated evaluator integrations behind a typed protocol.
- Added pre-generated LightningSim trace replay and a runnable bicg trace.
- Added manifest-driven batch execution, result verification, and trace hashing.
- Added a pinned Conda environment and 30-design trace-bundle workflow.
- Licensed the release under Apache License 2.0 with repository attribution.
