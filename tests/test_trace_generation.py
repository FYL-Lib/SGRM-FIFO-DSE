"""Tool-free tests for the optional local-source trace workflow."""

import hashlib
import json
from pathlib import Path
import pickle
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trace_generation import bind_sources, generate_trace, tooling


def test_completed_synthesis_does_not_need_optional_marker(tmp_path):
    solution = tmp_path / "project/solution1"
    for path in generate_trace.required_synthesis_outputs(solution, "forward"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"completed output")
    (solution / "syn/report/forward_csynth.xml").write_text(
        "<profile><ReportVersion><Version>2024.2</Version></ReportVersion></profile>")
    assert not (solution / ".autopilot/db/dut.hcp").exists()
    generate_trace.validate_completed_synthesis(solution, "forward")
    (solution / ".autopilot/db/a.o.3.bc").unlink()
    with pytest.raises(ValueError, match="Complete HLS C synthesis"):
        generate_trace.validate_completed_synthesis(solution, "forward")


def test_trace_export_never_replaces_existing_output(tmp_path):
    path = tmp_path / "trace.pkl"
    generate_trace.write_new_pickle({"first": True}, path)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        generate_trace.write_new_pickle({"second": True}, path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".sgrm-trace-*"))


def test_export_status_is_distinct_from_testbench_pass():
    assert generate_trace.export_status(0) == "exported"
    assert generate_trace.export_status(-6) == "exported_with_testbench_failure"
    assert generate_trace.export_status(1) == "exported_with_testbench_failure"


@pytest.mark.parametrize("returncode", [0, -6, 1])
def test_testbench_status_is_recorded_without_duplicate_warning_text(returncode, tmp_path):
    record = {"status": generate_trace.export_status(returncode),
              "testbench_passed": returncode == 0, "testbench_returncode": returncode}
    path = tmp_path / "trace.generation.json"
    generate_trace.write_new_json(record, path)
    saved = json.loads(path.read_text())
    assert saved == record
    if returncode != 0:
        assert saved["testbench_passed"] is False
        assert saved["status"] == "exported_with_testbench_failure"
    assert "testbench_warning" not in saved


def test_strict_testbench_option_is_preserved(monkeypatch):
    observed = {}

    async def fake_generate(args):
        observed.update(strict_testbench=args.strict_testbench)
        return 0

    monkeypatch.setattr(generate_trace, "generate", fake_generate)
    assert generate_trace.main(["--solution-dir", "project/solution1", "--strict-testbench"]) == 0
    assert observed == {"strict_testbench": True}


def test_audit_export_never_replaces_existing_evidence(tmp_path):
    path = tmp_path / "trace.generation.json"
    generate_trace.write_new_json({"testbench_passed": False}, path)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        generate_trace.write_new_json({"testbench_passed": True}, path)
    assert path.read_bytes() == original


def test_fifo_contract_rejects_duplicate_ids_and_boolean_depths():
    fifo = SimpleNamespace(id=1, name="channel", width=32)
    trace = SimpleNamespace(fifos=[fifo], params=SimpleNamespace(fifo_depths={1: 2}))
    assert generate_trace.fifo_metadata(trace) == [{"id": 1, "name": "channel", "width": 32, "depth": 2}]
    trace.fifos.append(fifo)
    with pytest.raises(RuntimeError, match="duplicate IDs"):
        generate_trace.fifo_metadata(trace)
    trace.fifos.pop()
    trace.params.fifo_depths[1] = True
    with pytest.raises(RuntimeError, match="positive baseline"):
        generate_trace.fifo_metadata(trace)


def test_rebinding_checks_integrity_before_unpickling(tmp_path, monkeypatch):
    path = tmp_path / "trace.pkl"
    path.write_bytes(b"not a trusted pickle")
    monkeypatch.setattr(bind_sources.pickle, "load", lambda _: pytest.fail("Unpickled before integrity check"))
    with pytest.raises(ValueError, match="Trace integrity check failed"):
        bind_sources.checked_trace(path, "incorrect")


def test_rebinding_can_read_a_checked_trusted_trace(tmp_path):
    path = tmp_path / "trace.pkl"
    path.write_bytes(pickle.dumps({"trusted": True}))
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    assert bind_sources.checked_trace(path, checksum) == {"trusted": True}


def test_trace_discovery_requires_hls_only(tmp_path, monkeypatch):
    (tmp_path / "include").mkdir()
    (tmp_path / "include/Makefile.sysc.rules").write_text("test fixture")
    (tmp_path / "include/hls_stream.h").write_text("test fixture")
    probed = []
    fake = SimpleNamespace(
        DEFAULT_TOOL_CONFIG=tmp_path / "config.json", TOOL_VERSION="2024.2",
        read_tool_config=lambda _: {},
        discover_tool=lambda key, value, saved, roots: ([("/tools/classic/vitis_hls", "fixture")], False),
        installation_settings=lambda _: [],
        load_amd_environment=lambda scripts, base: dict(base),
        hardware_environment=lambda executables, env: dict(env, XILINX_HLS=str(tmp_path)),
        probe_tool=lambda executable, key, env: probed.append(key),
    )
    monkeypatch.setattr(tooling, "hardware_module", lambda: fake)
    executable, env = tooling.find_hls(None, None, [], {})
    assert executable == "/tools/classic/vitis_hls"
    assert env["XILINX_HLS"] == str(tmp_path)
    assert probed == ["hls"]


def test_local_fork_has_runtime_helpers_license_and_locked_rust_source():
    fork = ROOT / "third_party/lightningsim"
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (fork / "LICENSE").read_text()
    assert (fork / "backend/LICENSE").read_bytes() == (fork / "LICENSE").read_bytes()
    assert "fmadd_stub.o" in (fork / "Makefile").read_text()
    assert "_IONBF" in (fork / "src/trace.cpp").read_text()
    assert (fork / "backend/Cargo.lock").is_file()
    assert "include Cargo.lock" in (fork / "backend/MANIFEST.in").read_text()


def test_classic_launcher_can_use_matching_vitis_runtime(tmp_path, monkeypatch):
    launcher = tmp_path / "Vitis_HLS/2024.2/bin/vitis_hls"
    runtime = tmp_path / "Vitis/2024.2"
    (runtime / "include").mkdir(parents=True)
    (runtime / "include/Makefile.sysc.rules").write_text("test fixture")
    (runtime / "include/hls_stream.h").write_text("test fixture")
    fake = SimpleNamespace(
        DEFAULT_TOOL_CONFIG=tmp_path / "config.json", TOOL_VERSION="2024.2",
        read_tool_config=lambda _: {},
        discover_tool=lambda *args: ([(str(launcher), "fixture")], False),
        installation_settings=lambda _: [],
        load_amd_environment=lambda scripts, base: dict(base),
        hardware_environment=lambda executables, env: dict(env, XILINX_HLS=str(launcher.parent.parent)),
        probe_tool=lambda *args: None,
    )
    monkeypatch.setattr(tooling, "hardware_module", lambda: fake)
    executable, env = tooling.find_hls(None, None, [], {})
    assert executable == str(launcher)
    assert env["XILINX_HLS"] == str(runtime)


def test_explicit_runtime_never_requires_a_launcher(tmp_path, monkeypatch):
    (tmp_path / "include").mkdir()
    (tmp_path / "include/Makefile.sysc.rules").write_text("test fixture")
    (tmp_path / "include/hls_stream.h").write_text("test fixture")
    fake = SimpleNamespace(load_amd_environment=lambda scripts, base: dict(base))
    monkeypatch.setattr(tooling, "hardware_module", lambda: fake)
    environment = tooling.explicit_runtime(tmp_path, [], {"PATH": "/conda/bin"})
    assert environment["XILINX_HLS"] == str(tmp_path)
    assert environment["PATH"] == "/conda/bin"


def test_published_trace_source_and_documentation_are_english():
    suffixes = {".py", ".rs", ".cpp", ".hpp", ".jinja", ".md", ".toml", ".yml", ".yaml", ".sh"}
    for directory in (ROOT / "trace_generation", ROOT / "third_party/lightningsim"):
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix in suffixes:
                text = path.read_text()
                assert not re.search(r"[\u3400-\u9fff]", text), str(path)
                assert not re.search(r"/(?:home|Users)/[A-Za-z0-9_.-]+/", text), str(path)
                assert not re.search(r"/opt/(?:Xilinx|AMD)[0-9]", text), str(path)
