"""Portable tool selection tests; no AMD tool is invoked by these tests."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hardware_tools", REPOSITORY / "hardware_validation/validate_hardware.py"
)
hardware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hardware)


@pytest.fixture(autouse=True)
def isolated_tool_setup(tmp_path, monkeypatch):
    # Unit tests must never discover the host's real tools or save host config.
    monkeypatch.setattr(hardware, "DEFAULT_TOOL_CONFIG", tmp_path / ".sgrm-tools.json")
    monkeypatch.setattr(hardware, "standard_tool_roots", lambda: [])
    monkeypatch.setattr(hardware, "load_amd_environment", lambda scripts, base: base.copy())
    for variable in ("VITIS_HLS", "VIVADO", "XILINX_HLS", "XILINX_VIVADO", "XILINX_VITIS"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def tool_paths(tmp_path):
    paths = {}
    for key, directory, name in (
        ("hls", "Vitis HLS", "vitis_hls"), ("vivado", "Vivado", "vivado")
    ):
        root = tmp_path / "AMD tools with spaces" / directory / "2024.2"
        binary = root / "bin" / name
        binary.parent.mkdir(parents=True)
        binary.touch()
        binary.chmod(0o755)
        (root / "settings64.sh").touch()
        if key == "vivado":
            (root / "data").mkdir()
        paths[key] = str(binary)
    return paths


def version_process(argv, **kwargs):
    assert argv[1:] == ["-version"]
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 30
    assert Path(kwargs["cwd"]).is_dir()
    assert not Path(kwargs["cwd"]).is_relative_to(REPOSITORY)
    product = "Vitis HLS" if Path(argv[0]).name == "vitis_hls" else "Vivado"
    return subprocess.CompletedProcess(argv, 0, f"****** {product} v2024.2 (64-bit)\n")


def test_check_tools_handles_spaces_and_selects_child_environment(tool_paths, monkeypatch, capsys):
    monkeypatch.setenv("XILINX_HLS", "/other/HLS/2025.1")
    monkeypatch.setenv("XILINX_VIVADO", "/other/Vivado/2025.1")
    monkeypatch.setenv("XILINX_HLS_DEVICE_DATADIR", "/other/Vivado/2025.1/data")
    before = dict(os.environ)
    monkeypatch.setattr(hardware.subprocess, "run", version_process)
    checked = hardware.check_tools(tool_paths["hls"], tool_paths["vivado"])
    assert checked["executables"] == tool_paths
    child = checked["environment"]
    assert child["XILINX_HLS"] == str(Path(tool_paths["hls"]).parent.parent)
    assert child["XILINX_VIVADO"] == str(Path(tool_paths["vivado"]).parent.parent)
    assert child["XILINX_HLS_DEVICE_DATADIR"] == str(Path(child["XILINX_VIVADO"]) / "data")
    assert child["PATH"].split(os.pathsep)[:2] == [
        str(Path(tool_paths["hls"]).parent), str(Path(tool_paths["vivado"]).parent)
    ]
    assert dict(os.environ) == before
    assert "TOOL CHECK PASS" in capsys.readouterr().out


def test_resolve_tool_uses_path_not_author_installation(tool_paths, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(tool_paths["hls"]).parent))
    assert hardware.resolve_tool("vitis_hls", "Vitis HLS", "--vitis-hls") == tool_paths["hls"]


def test_resolve_tool_expands_user_path(tool_paths):
    # Exercise real pathlib behavior without changing the user's environment
    # or relying on version-specific internal function lookup.
    relative = os.path.relpath(tool_paths["hls"], Path.home())
    assert hardware.resolve_tool("~/" + relative, "Vitis HLS", "--vitis-hls") == tool_paths["hls"]


def test_wrapper_does_not_create_an_invented_install_root(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "vivado"
    monkeypatch.setenv("XILINX_VIVADO", "/configured/amd/root")
    child = hardware.hardware_environment({"vivado": str(binary)})
    assert child["XILINX_VIVADO"] == "/configured/amd/root"


def test_missing_tools_are_reported_together(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError) as error:
        hardware.check_tools("vitis_hls", "vivado")
    message = str(error.value)
    assert "Vitis HLS executable not found" in message
    assert "Vivado executable not found" in message
    assert "--vitis-hls" in message and "--vivado" in message


def test_missing_vivado_cannot_pass_an_hls_only_check(tool_paths, monkeypatch):
    monkeypatch.setattr(hardware.subprocess, "run", version_process)
    with pytest.raises(RuntimeError, match="Vivado executable not found"):
        hardware.check_tools(tool_paths["hls"], tool_paths["vivado"] + "-missing")


@pytest.mark.parametrize("key,product", [("hls", "Vitis HLS"), ("vivado", "Vivado")])
def test_wrong_version_cannot_pass_from_a_2024_2_mention(tool_paths, monkeypatch, key, product):
    def wrong_version(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, f"{product} v2025.1\nPlease install 2024.2 instead.\n")

    monkeypatch.setattr(hardware.subprocess, "run", wrong_version)
    with pytest.raises(RuntimeError, match="found 2025.1"):
        hardware.probe_tool(tool_paths[key], key, {})


@pytest.mark.parametrize("key,product", [("hls", "Vitis HLS"), ("vivado", "Vivado")])
def test_failed_version_query_cannot_pass_even_with_a_valid_banner(tool_paths, monkeypatch, key, product):
    monkeypatch.setattr(
        hardware.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, f"{product} v2024.2\nERROR: setup failed\n"),
    )
    with pytest.raises(RuntimeError, match="exited with 1"):
        hardware.probe_tool(tool_paths[key], key, {})


def test_unrecognized_version_output_is_rejected(tool_paths, monkeypatch):
    monkeypatch.setattr(
        hardware.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "Usage: install AMD 2024.2\n"),
    )
    with pytest.raises(RuntimeError, match="unrecognized version output"):
        hardware.probe_tool(tool_paths["hls"], "hls", {})


@pytest.mark.parametrize("banner", [
    "Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2024.2 (64-bit)",
    "****** Vitis HLS v2024.2 (64-bit)",
])
def test_classic_hls_version_banner_formats_are_supported(tool_paths, monkeypatch, banner):
    monkeypatch.setattr(
        hardware.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, banner + "\nSW Build 5238294\n"),
    )
    assert hardware.probe_tool(tool_paths["hls"], "hls", {}).startswith(banner)


def test_unified_wrapper_has_actionable_error(tool_paths, monkeypatch):
    monkeypatch.setattr(
        hardware.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, "ERROR: [vitis-run 60-1520] unrecognised option '-version'\n"
        ),
    )
    with pytest.raises(RuntimeError, match="forwards to the unified vitis-run CLI"):
        hardware.probe_tool(tool_paths["hls"], "hls", {})


@pytest.mark.parametrize("name", ["vitis-run", "vitis"])
def test_direct_unified_cli_is_not_run_as_a_classic_launcher(tmp_path, monkeypatch, name):
    def unexpected_call(*args, **kwargs):
        pytest.fail("the known unified CLI must not be launched with classic options")

    monkeypatch.setattr(hardware.subprocess, "run", unexpected_call)
    with pytest.raises(RuntimeError, match="is the unified Vitis CLI"):
        hardware.probe_tool(str(tmp_path / name), "hls", {})


def test_version_timeout_is_reported_without_leaking_temp_files(tool_paths, monkeypatch):
    temporary = []

    def timeout(argv, **kwargs):
        temporary.append(Path(kwargs["cwd"]))
        (temporary[-1] / "vitis_hls.log").touch()
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(hardware.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out after 30 s"):
        hardware.probe_tool(tool_paths["hls"], "hls", {})
    assert not temporary[0].exists()


def test_check_tools_cli_does_not_need_results_or_write_an_output_dir(tool_paths, monkeypatch, tmp_path):
    monkeypatch.setattr(hardware.subprocess, "run", version_process)
    output = tmp_path / "not-created"
    assert hardware.main([
        "--stage", "check-tools", "--output-dir", str(output),
        "--vitis-hls", tool_paths["hls"], "--vivado", tool_paths["vivado"],
    ]) == 0
    assert not output.exists()


def test_tool_environment_defaults_remain_supported(tool_paths, monkeypatch):
    monkeypatch.setenv("VITIS_HLS", tool_paths["hls"])
    monkeypatch.setenv("VIVADO", tool_paths["vivado"])
    monkeypatch.setattr(hardware.subprocess, "run", version_process)
    assert hardware.main(["--stage", "check-tools"]) == 0


def test_all_prechecks_tools_before_creating_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("VITIS_HLS", raising=False)
    monkeypatch.delenv("VIVADO", raising=False)
    output = tmp_path / "hardware-output"
    with pytest.raises(RuntimeError, match="precheck failed"):
        hardware.main([
            "--stage", "all", "--results-dir", str(tmp_path / "search-results"),
            "--output-dir", str(output),
        ])
    assert not output.exists()


@pytest.mark.parametrize("stage", ["prepare", "report"])
def test_tool_free_stages_do_not_check_or_launch_amd_tools(stage, tmp_path, monkeypatch):
    output = tmp_path / "hardware-output"
    output.mkdir()
    plan = {"jobs": [], "designs": [], "design_count": 0}
    hardware.write_json(output / "plan.json", plan)

    def unexpected_check(*args, **kwargs):
        pytest.fail("prepare/report must not require or run AMD tools")

    monkeypatch.setattr(hardware, "check_tools", unexpected_check)
    monkeypatch.setattr(hardware, "collect_reports", lambda *args: {"status": "PASS"})
    args = ["--stage", stage, "--output-dir", str(output)]
    if stage == "prepare":
        monkeypatch.setattr(hardware, "extract_source_archive", lambda *args: tmp_path)
        monkeypatch.setattr(hardware, "load_source_manifest", lambda *args: {"designs": []})
        monkeypatch.setattr(hardware, "prepare_jobs", lambda *args: plan)
        args.extend(["--results-dir", str(tmp_path / "search-results")])
    assert hardware.main(args) == 0


def test_command_passes_selected_environment_and_keeps_classic_tcl(tool_paths, monkeypatch, tmp_path):
    child = hardware.hardware_environment(tool_paths)

    def mock_run(argv, **kwargs):
        assert argv == [tool_paths["hls"], "-f", str(tmp_path / "run_vivado.tcl")]
        assert kwargs["env"] is child
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(hardware.subprocess, "run", mock_run)
    assert hardware.command(
        tool_paths["hls"], tmp_path / "run_vivado.tcl", tmp_path,
        tmp_path / "export.log", env=child,
    ) >= 0


def test_run_cli_forwards_checked_tools_to_every_job(tool_paths, monkeypatch, tmp_path):
    plan = {"jobs": [{"design": "example", "variant": variant} for variant in ("native", "sgrm")]}
    hardware.write_json(tmp_path / "plan.json", plan)
    checked = {
        "executables": tool_paths, "versions": {"hls": "Vitis HLS v2024.2", "vivado": "Vivado v2024.2"},
        "environment": hardware.hardware_environment(tool_paths),
    }
    monkeypatch.setattr(hardware, "check_tools", lambda *args, **kwargs: checked)
    monkeypatch.setattr(hardware, "collect_reports", lambda *args: {"status": "PASS"})
    calls = []

    def run_job(job, tool, version, *, toolchain):
        calls.append((job["variant"], tool, version, toolchain))

    monkeypatch.setattr(hardware, "run_job", run_job)
    assert hardware.main(["--stage", "run", "--output-dir", str(tmp_path)]) == 0
    assert len(calls) == 2
    for _, tool, version, toolchain in calls:
        assert tool == tool_paths["hls"]
        assert version == checked["versions"]["hls"]
        assert toolchain is checked
