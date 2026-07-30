import hashlib
import json
from pathlib import Path

import pytest


pytest.importorskip(
    "lightningsim",
    reason="the pre-generated trace test requires LightningSim 0.2.6",
)

from sgrm.cli import main
from sgrm.lightningsim_backend import LightningSimTraceBackend
from sgrm.manifest import load_manifest
from sgrm.verify_cli import verify_result


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOLUTION_DIR = REPOSITORY_ROOT / "examples" / "traces" / "bicg" / "solution1"
TRACE_SHA256 = "88c68755bfbf99d6c451765fdc89b69d2fc7604632790b5860cce273553aca63"
HLS_APP_SHA256 = "5d12636798ab5c7aba12ad7220029343f270bce21220f26e4533c0f1603ca109"


def test_bicg_trace_metadata_and_baseline():
    trace_path = SOLUTION_DIR / "trace.pkl"
    assert hashlib.sha256(trace_path.read_bytes()).hexdigest() == TRACE_SHA256
    hls_app = SOLUTION_DIR.parent / "hls.app"
    assert hashlib.sha256(hls_app.read_bytes()).hexdigest() == HLS_APP_SHA256

    backend = LightningSimTraceBackend(SOLUTION_DIR)
    assert len(backend.fifos) == 44
    assert len({fifo.get_display_name() for fifo in backend.fifos}) == 2
    assert {backend.fifo_sizes_base[index] for index in range(5)} == {82}
    assert {backend.fifo_sizes_base[index] for index in range(5, 44)} == {10}

    baseline = backend.eval_solution_default()
    assert baseline.latency == 835.0


def test_bicg_cli_matches_reference_search(tmp_path):
    output = tmp_path / "bicg.json"
    assert main(
        [
            "--solution-dir",
            str(SOLUTION_DIR),
            "--design",
            "bicg",
            "--budget",
            "1000",
            "--seed",
            "1",
            "--epsilon",
            "0",
            "--expected-trace-sha256",
            TRACE_SHA256,
            "--output",
            str(output),
        ]
    ) == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["fifo_count"] == 44
    assert result["group_count"] == 2
    assert result["evaluations"] == 11
    assert result["selected_evaluation"] == 3
    assert result["baseline"]["latency"] == 835.0
    assert result["selected"]["latency"] == 834.0
    assert (
        result["selected"]["bram"],
        result["selected"]["uram"],
        result["selected"]["ff"],
        result["selected"]["lut"],
    ) == (0, 0, 572, 2508)
    assert result["selected"]["raw_resource_sum"] == 3080
    assert result["cutil_reduction_pct"] == pytest.approx(11.833385926159666)
    assert result["raw_resource_reduction_pct"] == pytest.approx(
        10.853835021707669
    )
    _, manifest = load_manifest(REPOSITORY_ROOT / "examples" / "traces" / "manifest.json")
    assert verify_result(result, manifest["designs"][0]) == []
