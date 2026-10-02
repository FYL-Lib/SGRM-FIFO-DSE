import hashlib

import pytest

from sgrm import cli


def test_integrity_error_is_readable_and_precedes_trace_loading(tmp_path, monkeypatch):
    solution_dir = tmp_path / "solution1"
    solution_dir.mkdir()
    trace_path = solution_dir / "trace.pkl"
    trace_path.write_bytes(b"not the expected trace")
    expected = hashlib.sha256(b"trusted reference trace").hexdigest()
    actual = hashlib.sha256(trace_path.read_bytes()).hexdigest()

    def unexpected_trace_load(*args, **kwargs):
        pytest.fail("an integrity failure must be rejected before loading the trace")

    monkeypatch.setattr(cli, "LightningSimTraceBackend", unexpected_trace_load)
    with pytest.raises(SystemExit, match="trace integrity check failed") as exc:
        cli.main([
            "--solution-dir", str(solution_dir),
            "--expected-trace-sha256", expected,
        ])
    message = str(exc.value)
    assert "sgrm-trace-batch" in message
    assert expected not in message
    assert actual not in message
