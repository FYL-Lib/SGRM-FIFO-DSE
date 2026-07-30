# Contributing

Contributions to the SGRM core should preserve the hard feasibility policy and the separation between search logic and evaluator integrations.

Unless explicitly stated otherwise, submitted contributions are licensed
under the repository's Apache License 2.0 terms.

## Development setup

```bash
python -m pip install -e '.[dev]'
pytest
```

## Guidelines

- Keep optional evaluator adapters separated from the optimizer core.
- Add or update unit tests for changes to state transitions, feasibility, resource formulas, or budget accounting.
- Preserve deterministic behavior under a fixed random seed.
- Document changes to public interfaces and environment controls.
- Do not commit generated projects, reports, large trace sets, or benchmark
  outputs. A small trace may be versioned when it is a documented regression
  fixture with a checksum and stable license provenance.

## Change checklist

1. Run the unit tests.
2. Confirm that all accepted points satisfy deadlock and latency constraints.
3. Confirm that evaluation counts remain within the global budget.
4. Update `CHANGELOG.md` when behavior or public APIs change.
