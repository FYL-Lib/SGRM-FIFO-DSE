# SGRM evaluator interface

SGRM is deliberately separated from the mechanism that measures a candidate. The core package defines the optimizer-facing contract, while platform integrations provide concrete implementations.

## FIFO descriptors

Each `FIFODescriptor` supplies:

- `id`: stable integer identifier;
- `name`: channel name;
- `width`: payload width in bits; and
- `group_name`: optional grouping label.

`get_display_name()` returns the grouping label when present and otherwise the channel name.

## Depth-space provider

The backend exposes a compiled-design view with:

```python
get_fifo_design_space(fifo_ids: list[int], width: int) -> list[int]
```

The returned list must be non-empty, ordered, and legal for every FIFO in the group. SGRM may insert selected intermediate depths strictly within its endpoints.

## Candidate evaluation

The backend method is:

```python
eval_solution_single(
    fifo_depths: dict[int, int],
    fifo_impl_types: dict[int, str] | None,
) -> EvalResult
```

It must return the evaluated depth map, deadlock status, latency, and total FIFO BRAM, URAM, FF, and LUT usage. The implementation map is optional result provenance and is not read by SGRM's acceptance logic. A deadlocked point has `latency=None`; resource fields may also be unavailable.

The optimizer treats the backend as authoritative for feasibility. The analytical formulas in `resource_model.py` support resource accounting inside an implementation. `LightningSimTraceBackend` provides one concrete adapter; additional workload-specific execution state remains outside the optimizer core.

## Backend integration

`EvaluationBackend` is a protocol. A platform adapter supplies design metadata and implements `eval_solution_single()`. Keeping adapters separate from `sgrm.py` allows the same optimizer to connect to different evaluation systems without changing the search algorithm.
