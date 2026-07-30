# FIFO resource model

The analytical model estimates FIFO storage and control resources from three values:

```text
(width, depth, implementation_type)
```

No per-design synthesis report is required by the deterministic formulas.

## Storage implementations

| Type | Storage resource | Typical operating region |
|---|---|---|
| `srl` | LUT shift-register chains | Shallow or low-bit-capacity FIFOs |
| `lutram` | Distributed RAM in LUTs | Moderate narrow FIFOs |
| `bram` | Block RAM | General deep FIFOs |
| `uram` | UltraRAM | Very deep or wide FIFOs |

`auto` is accepted only as a model tag for the unoptimized HLS reference. `get_impl_type_space()` returns the four concrete choices above.

## BRAM

BRAM storage accounts for depth rows and width columns. Narrow FIFOs use 18-bit or 36-bit organizations; wider FIFOs are tiled across 72-bit columns. The model uses `depth - 1` for RAM-backed storage because one FIFO position is represented by control state.

## URAM

URAM count is tiled in 72-bit columns and 4096-entry rows:

```text
columns = ceil(width / 72)
rows    = ceil((depth - 1) / 4096)
URAM    = columns * rows
```

## SRL and LUTRAM

SRL storage uses one LUT chain per bit for shallow depths and cascades 32-entry chains for deeper configurations. LUTRAM storage starts from the dual-port distributed-memory relation:

```text
storage_LUTs = ceil(depth / 32) * width
```

The implementation applies fitted scale factors to the storage terms and adds pointer, handshake, cascade, and output-multiplexing control logic.

## FF and control logic

Depth-two FIFOs are modeled as registered buffers independently of the requested implementation. Deeper SRL FIFOs keep data in LUT primitives, while RAM-backed implementations account primarily for pointers, handshakes, and output registers in FF/LUT totals.

## Design aggregation

`predict_design_resources()` evaluates every FIFO and returns total BRAM, URAM, FF, and LUT counts plus per-FIFO details. If an external calibration file is enabled and available, a bucketed affine overlay adjusts the design totals without changing the per-FIFO formulas or implementation search space.
