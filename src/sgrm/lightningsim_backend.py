"""LightningSim adapter for replaying pre-generated HLS traces.

The adapter deliberately loads an existing ``trace.pkl`` and never invokes
Vitis HLS.  This keeps trace replay separate from trace generation: users only
need Vitis HLS when they want to build a new trace from C/C++ sources.
"""

from __future__ import annotations

import pickle
import time
from numbers import Integral
from pathlib import Path

from .interfaces import EvalResult, FIFODescriptor
from .resource_model import predict_design_resources


class LightningSimTraceBackend:
    """Evaluate FIFO configurations using a pre-generated LightningSim trace.

    Parameters
    ----------
    solution_dir:
        Vitis HLS solution directory containing ``trace.pkl``.  The trace must
        have been generated with a LightningSim version compatible with the
        installed runtime (the published traces use LightningSim 0.2.6).

        ``trace.pkl`` is a Python pickle. Only load a trace from a trusted
        source after checking its SHA-256 digest.
    """

    def __init__(self, solution_dir: str | Path):
        self.solution_dir = Path(solution_dir).expanduser().resolve()
        trace_path = self.solution_dir / "trace.pkl"
        if not trace_path.is_file():
            raise FileNotFoundError(
                f"pre-generated trace not found: {trace_path}\n"
                "Download the trace bundle or point --solution-dir to a "
                "directory containing trace.pkl."
            )

        try:
            with trace_path.open("rb") as stream:
                self.trace_base = pickle.load(stream)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("lightningsim"):
                raise RuntimeError(
                    "Loading trace.pkl requires LightningSim 0.2.6. "
                    "Create the reproduction environment before running the search."
                ) from exc
            raise
        except (
            pickle.UnpicklingError,
            ImportError,
            AttributeError,
            TypeError,
            EOFError,
        ) as exc:
            raise RuntimeError(
                "The trace could not be loaded with this Python/LightningSim "
                "runtime. Use the versions in environment.yml."
            ) from exc

        required = ("compiled", "params", "fifos")
        missing = [name for name in required if not hasattr(self.trace_base, name)]
        if missing:
            raise TypeError(
                f"{trace_path} is not a compatible resolved trace; missing {missing}"
            )

        resolved_fifos = list(self.trace_base.fifos)
        self.fifos = [
            FIFODescriptor(
                id=int(fifo.id),
                name=str(fifo.name),
                width=int(fifo.width),
                group_name=str(fifo.get_display_name()),
            )
            for fifo in resolved_fifos
        ]
        self.fifo_sizes_base = {
            int(fifo.id): self.trace_base.params.fifo_depths[fifo.id]
            for fifo in resolved_fifos
        }
        self.fifo_widths = {
            int(fifo.id): int(fifo.width) for fifo in resolved_fifos
        }

    def eval_solution_single(
        self,
        fifo_depths: dict[int, int],
        fifo_impl_types: dict[int, str] | None = None,
    ) -> EvalResult:
        """Replay one FIFO-depth map and return latency plus model resources."""

        expected_ids = set(self.fifo_widths)
        supplied_depth_ids = set(fifo_depths)
        if supplied_depth_ids != expected_ids:
            missing = sorted(expected_ids - supplied_depth_ids)
            extra = sorted(supplied_depth_ids - expected_ids)
            raise ValueError(
                f"FIFO depth map does not match the trace; missing={missing}, "
                f"extra={extra}"
            )
        invalid_depths = {
            fifo_id: depth
            for fifo_id, depth in fifo_depths.items()
            if not isinstance(depth, Integral) or isinstance(depth, bool) or depth < 1
        }
        if invalid_depths:
            raise ValueError(f"FIFO depths must be positive integers: {invalid_depths}")

        normalized_depths = {
            int(fifo_id): int(depth) for fifo_id, depth in fifo_depths.items()
        }
        normalized_impl_types: dict[int, str] | None = None
        if fifo_impl_types is not None:
            supplied_impl_ids = set(fifo_impl_types)
            if supplied_impl_ids != expected_ids:
                missing = sorted(expected_ids - supplied_impl_ids)
                extra = sorted(supplied_impl_ids - expected_ids)
                raise ValueError(
                    f"FIFO implementation map does not match the trace; "
                    f"missing={missing}, extra={extra}"
                )
            valid_impl_types = {"srl", "lutram", "bram", "uram", "auto"}
            invalid_impl_types = {
                fifo_id: impl
                for fifo_id, impl in fifo_impl_types.items()
                if impl not in valid_impl_types
            }
            if invalid_impl_types:
                raise ValueError(
                    "Unsupported FIFO implementation types: "
                    f"{invalid_impl_types}"
                )
            normalized_impl_types = {
                int(fifo_id): str(impl)
                for fifo_id, impl in fifo_impl_types.items()
            }

        dse_results = self.trace_base.compiled.dse(
            self.trace_base.params,
            [normalized_depths],
        )
        if len(dse_results) != 1:
            raise RuntimeError(
                f"LightningSim returned {len(dse_results)} points for one candidate"
            )

        point = dse_results[0]
        timestamp = time.perf_counter()
        if point.latency is None:
            return EvalResult(
                fifo_sizes=normalized_depths,
                deadlock=True,
                latency=None,
                bram_usage_total=None,
                uram_usage_total=None,
                ff_usage_total=None,
                lut_usage_total=None,
                fifo_impl_types=normalized_impl_types,
                timestamp=timestamp,
            )

        bram, uram, ff, lut, _ = predict_design_resources(
            self.fifo_widths,
            normalized_depths,
            normalized_impl_types,
        )
        return EvalResult(
            fifo_sizes=normalized_depths,
            deadlock=False,
            latency=float(point.latency),
            bram_usage_total=int(bram),
            uram_usage_total=int(uram),
            ff_usage_total=int(ff),
            lut_usage_total=int(lut),
            fifo_impl_types=normalized_impl_types,
            timestamp=timestamp,
        )

    def eval_solution_default(self) -> EvalResult:
        """Evaluate the trace's default depths with automatic resource mapping."""

        missing = [
            fifo_id
            for fifo_id, depth in self.fifo_sizes_base.items()
            if depth is None
        ]
        if missing:
            raise ValueError(f"baseline depth is undefined for FIFO IDs {missing}")
        baseline = {
            fifo_id: int(depth)
            for fifo_id, depth in self.fifo_sizes_base.items()
            if depth is not None
        }
        return self.eval_solution_single(baseline)
