"""Abstract data contracts used by the SGRM core package.

These definitions keep the optimizer independent from any particular
simulation, synthesis, workload, or generated-project implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class FIFODescriptor:
    """Structural FIFO metadata required by SGRM."""

    id: int
    name: str
    width: int
    group_name: str | None = None

    def get_display_name(self) -> str:
        """Return the stable label used to merge equivalent channels."""
        return self.group_name or self.name


@dataclass
class EvalResult:
    """One measured FIFO configuration returned by an evaluator."""

    fifo_sizes: dict[int, int]
    deadlock: bool
    latency: float | None
    bram_usage_total: int | None
    uram_usage_total: int | None = None
    ff_usage_total: int | None = None
    lut_usage_total: int | None = None
    fifo_impl_types: dict[int, str] | None = None
    timestamp: float | None = None


class DesignSpaceProvider(Protocol):
    """Supplies legal FIFO-depth lattices for a compiled design."""

    def get_fifo_design_space(
        self,
        fifo_ids: list[int],
        width: int,
    ) -> list[int]:
        """Return a non-empty, ordered legal depth list for one group."""
        ...


class DesignView(Protocol):
    """Minimal compiled-design view consulted during optimizer setup."""

    compiled: DesignSpaceProvider


class EvaluationBackend(Protocol):
    """Abstract measurement boundary required by ``SGRMOptimizer``.

    Integrations implement this protocol to connect SGRM to an evaluator.
    """

    fifos: Sequence[FIFODescriptor]
    fifo_sizes_base: Mapping[int, int | None]
    trace_base: DesignView

    def eval_solution_single(
        self,
        fifo_depths: dict[int, int],
        fifo_impl_types: dict[int, str] | None = None,
    ) -> EvalResult:
        """Measure latency, deadlock status, and four FIFO resources."""
        ...


class FIFOOptimizer(ABC):
    """Small optimizer base retained to expose the original class contract."""

    def __init__(self, sim_env: EvaluationBackend):
        self.sim_env: EvaluationBackend = sim_env

    @abstractmethod
    def solve(self) -> list[EvalResult]:
        """Evaluate the bounded search and return its recorded points."""
        raise NotImplementedError
