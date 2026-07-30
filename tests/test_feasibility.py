from sgrm.interfaces import EvalResult
from sgrm.sgrm import _is_feasible


def result(*, deadlock: bool = False, latency: float | None = 100.0) -> EvalResult:
    return EvalResult({}, deadlock, latency, 0, 0, 0, 0)


def test_feasible_at_hard_latency_boundary():
    assert _is_feasible(result(latency=100.0), 100.0)


def test_latency_regression_is_infeasible():
    assert not _is_feasible(result(latency=100.01), 100.0)


def test_deadlock_and_missing_latency_are_infeasible():
    assert not _is_feasible(result(deadlock=True), 100.0)
    assert not _is_feasible(result(latency=None), 100.0)
