"""First-run tool setup using fake installations, never AMD synthesis."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "tool_discovery", REPOSITORY / "hardware_validation/validate_hardware.py"
)
hardware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hardware)


@pytest.fixture(autouse=True)
def isolate_host_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for name in ("VITIS_HLS", "VIVADO", "XILINX_HLS", "XILINX_VIVADO", "XILINX_VITIS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hardware, "standard_tool_roots", lambda: [])
    monkeypatch.setattr(hardware, "DEFAULT_TOOL_CONFIG", tmp_path / ".sgrm-tools.json")


def fake_installation(base, version="2024.2", layout="product-first"):
    paths = {}
    for key, product, name, banner in (
        ("hls", "Vitis_HLS", "vitis_hls", "Vitis HLS"),
        ("vivado", "Vivado", "vivado", "Vivado"),
    ):
        root = base / product / version if layout == "product-first" else base / version / product
        binary = root / "bin" / name
        binary.parent.mkdir(parents=True)
        binary.write_text(
            '#!/bin/sh\n'
            'if [ "$1" != "-version" ]; then exit 9; fi\n'
            'if [ "${SGRM_TEST_READY:-}" != "loaded" ]; then exit 8; fi\n'
            f'printf "%s\\n" "{banner} v{version} (64-bit)"\n'
        )
        binary.chmod(0o755)
        (root / "settings64.sh").write_text(
            'export SGRM_TEST_READY="loaded"\n'
            'export SGRM_TEST_PAYLOAD="value with spaces"\n'
        )
        if key == "vivado":
            (root / "data").mkdir()
        paths[key] = str(binary)
    return paths


@pytest.mark.parametrize("layout", ["product-first", "version-first"])
def test_unloaded_tools_are_discovered_and_configured_privately(tmp_path, monkeypatch, layout):
    base = tmp_path / "AMD tools with spaces and 'quote'"
    paths = fake_installation(base, layout=layout)
    parent = dict(os.environ)
    config = tmp_path / ".sgrm-tools.json"
    checked = hardware.check_tools(config_path=config, search_roots=[base], save_config=True)
    assert checked["executables"] == paths
    assert checked["environment"]["SGRM_TEST_READY"] == "loaded"
    assert checked["environment"]["SGRM_TEST_PAYLOAD"] == "value with spaces"
    assert dict(os.environ) == parent
    saved = json.loads(config.read_text())
    assert saved["vitis_hls"] == paths["hls"] and saved["vivado"] == paths["vivado"]
    assert len(saved["settings_files"]) == 2
    assert "environment" not in saved


def test_vendor_environment_roots_work_without_tool_binaries_on_path(tmp_path, monkeypatch):
    paths = fake_installation(tmp_path / "custom")
    monkeypatch.setenv("XILINX_HLS", str(Path(paths["hls"]).parent.parent))
    monkeypatch.setenv("XILINX_VIVADO", str(Path(paths["vivado"]).parent.parent))
    assert hardware.check_tools(search_roots=[])["executables"] == paths


def test_vitis_environment_root_can_expose_a_classic_launcher(tmp_path, monkeypatch):
    paths = fake_installation(tmp_path / "custom")
    monkeypatch.setenv("XILINX_VITIS", str(Path(paths["hls"]).parent.parent))
    monkeypatch.setenv("XILINX_VIVADO", str(Path(paths["vivado"]).parent.parent))
    assert hardware.check_tools(search_roots=[])["executables"] == paths


def test_standard_discovery_finds_an_install_without_vendor_variables(tmp_path, monkeypatch):
    base = tmp_path / "vendor"
    paths = fake_installation(base)
    monkeypatch.setattr(hardware, "standard_tool_roots", lambda: [base])
    assert hardware.check_tools()["executables"] == paths


def test_saved_configuration_is_reused_but_versions_are_rechecked(tmp_path, monkeypatch):
    base = tmp_path / "vendor"
    paths = fake_installation(base)
    config = tmp_path / ".sgrm-tools.json"
    hardware.check_tools(config_path=config, search_roots=[base], save_config=True)

    def unnecessary_search():
        pytest.fail("a valid saved pair should not scan installation roots again")

    monkeypatch.setattr(hardware, "standard_tool_roots", unnecessary_search)
    checked = hardware.check_tools(config_path=config)
    assert checked["executables"] == paths
    Path(paths["hls"]).write_text('#!/bin/sh\nexit 7\n')
    with pytest.raises(RuntimeError, match="none passed"):
        hardware.check_tools(paths["hls"], paths["vivado"], config_path=config)


def test_stale_paths_are_rediscovered_and_saved_again(tmp_path):
    base = tmp_path / "vendor"
    paths = fake_installation(base)
    config = tmp_path / ".sgrm-tools.json"
    hardware.write_json(config, {
        "schema_version": 1, "tool_version": "2024.2",
        "vitis_hls": str(tmp_path / "old/vitis_hls"), "vivado": str(tmp_path / "old/vivado"),
        "settings_files": [str(tmp_path / "old/settings64.sh")],
    })
    assert hardware.check_tools(config_path=config, search_roots=[base], save_config=True)["executables"] == paths
    assert json.loads(config.read_text())["vitis_hls"] == paths["hls"]


def test_one_stale_cached_tool_does_not_block_the_other_compatible_tool(tmp_path):
    old_base, new_base = tmp_path / "old vendor", tmp_path / "new vendor"
    old = fake_installation(old_base)
    newer = fake_installation(new_base)
    config = tmp_path / ".sgrm-tools.json"
    hardware.check_tools(config_path=config, search_roots=[old_base], save_config=True)
    Path(old["hls"]).unlink()
    old_settings = Path(old["hls"]).parent.parent / "settings64.sh"
    old_settings.unlink()
    checked = hardware.check_tools(config_path=config, search_roots=[new_base], save_config=True)
    assert checked["executables"] == {"hls": newer["hls"], "vivado": old["vivado"]}
    assert str(old_settings) not in json.loads(config.read_text())["settings_files"]


def test_explicit_new_installation_does_not_load_stale_cached_settings(tmp_path):
    old_base, new_base = tmp_path / "old vendor", tmp_path / "new vendor"
    fake_installation(old_base)
    newer = fake_installation(new_base)
    config = tmp_path / ".sgrm-tools.json"
    hardware.check_tools(config_path=config, search_roots=[old_base], save_config=True)
    saved = json.loads(config.read_text())
    saved["settings_files"].append(str(tmp_path / "missing old site settings.sh"))
    hardware.write_json(config, saved)
    assert hardware.check_tools(newer["hls"], newer["vivado"], config_path=config)["executables"] == newer


def test_wrong_release_on_path_does_not_hide_compatible_installation(tmp_path, monkeypatch, capsys):
    base = tmp_path / "vendor"
    newer = fake_installation(base, version="2025.1")
    compatible = fake_installation(base)
    monkeypatch.setenv("PATH", os.pathsep.join([
        str(Path(newer["hls"]).parent), str(Path(newer["vivado"]).parent), "/usr/bin", "/bin",
    ]))
    checked = hardware.check_tools(search_roots=[base])
    assert checked["executables"] == compatible
    assert "SKIP" in capsys.readouterr().out


def test_explicit_wrong_release_is_not_silently_replaced(tmp_path):
    base = tmp_path / "vendor"
    newer = fake_installation(base, version="2025.1")
    fake_installation(base)
    with pytest.raises(RuntimeError, match="found 2025.1"):
        hardware.check_tools(newer["hls"], newer["vivado"], search_roots=[base])


def test_newer_cached_tools_are_rediscovered(tmp_path):
    base = tmp_path / "vendor"
    newer = fake_installation(base, version="2025.1")
    compatible = fake_installation(base)
    config = tmp_path / ".sgrm-tools.json"
    hardware.write_json(config, {
        "schema_version": 1, "vitis_hls": newer["hls"], "vivado": newer["vivado"],
    })
    assert hardware.check_tools(config_path=config, search_roots=[base])["executables"] == compatible


def test_no_tools_found_is_not_reported_as_proof_of_no_installation(tmp_path):
    with pytest.raises(RuntimeError) as error:
        hardware.check_tools(search_roots=[tmp_path / "empty"])
    message = str(error.value)
    assert "Vitis HLS not discovered" in message and "Vivado not discovered" in message
    assert "installed elsewhere" in message and "--tool-root" in message
    assert "search/prepare/report do not need AMD tools" in message


def test_only_newer_installations_are_reported_as_incompatible(tmp_path):
    base = tmp_path / "vendor"
    fake_installation(base, version="2025.1")
    with pytest.raises(RuntimeError) as error:
        hardware.check_tools(search_roots=[base])
    assert "candidates were found" in str(error.value)
    assert "found 2025.1" in str(error.value)
    assert "not discovered" not in str(error.value)


def test_unified_launcher_is_skipped_in_automatic_discovery(tmp_path, monkeypatch):
    base = tmp_path / "vendor"
    paths = fake_installation(base)
    wrapper = tmp_path / "wrapper" / "vitis_hls"
    wrapper.parent.mkdir()
    wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "ERROR: [vitis-run] unknown option"\nexit 1\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(wrapper.parent), "/usr/bin", "/bin"]))
    assert hardware.check_tools(search_roots=[base])["executables"] == paths


def test_configuration_does_not_store_license_or_credential_environment(tmp_path, monkeypatch):
    base = tmp_path / "vendor"
    fake_installation(base)
    monkeypatch.setenv("XILINXD_LICENSE_FILE", "synthetic-license-value-for-test")
    monkeypatch.setenv("SGRM_TEST_CREDENTIAL", "synthetic-credential-value-for-test")
    config = tmp_path / ".sgrm-tools.json"
    checked = hardware.check_tools(config_path=config, search_roots=[base], save_config=True)
    assert checked["environment"]["XILINXD_LICENSE_FILE"] == "synthetic-license-value-for-test"
    text = config.read_text()
    assert "synthetic-license" not in text and "synthetic-credential" not in text
    assert "XILINXD_LICENSE_FILE" not in text and "SGRM_TEST_CREDENTIAL" not in text


def test_site_settings_are_loaded_and_remembered(tmp_path, monkeypatch):
    base = tmp_path / "vendor"
    paths = fake_installation(base)
    settings = tmp_path / "site settings.sh"
    settings.write_text('export SGRM_SITE_MARKER="site configured"\n')
    config = tmp_path / ".sgrm-tools.json"
    checked = hardware.check_tools(config_path=config, search_roots=[base], settings_files=[settings], save_config=True)
    assert checked["environment"]["SGRM_SITE_MARKER"] == "site configured"
    assert str(settings) in json.loads(config.read_text())["settings_files"]
    assert "SGRM_SITE_MARKER" not in os.environ
    assert hardware.check_tools(config_path=config)["executables"] == paths
    assert hardware.check_tools(config_path=config)["environment"]["SGRM_SITE_MARKER"] == "site configured"


def test_failed_settings_are_distinct_from_missing_tools(tmp_path):
    base = tmp_path / "vendor"
    paths = fake_installation(base)
    (Path(paths["hls"]).parent.parent / "settings64.sh").write_text("return 5\n")
    with pytest.raises(RuntimeError, match="environment setup failed"):
        hardware.check_tools(search_roots=[base])


def test_explicit_missing_settings_are_reported(tmp_path):
    with pytest.raises(FileNotFoundError, match="settings file not found"):
        hardware.check_tools(settings_files=[tmp_path / "missing.sh"])


def test_settings_timeout_is_bounded_and_environment_is_not_dumped(tmp_path, monkeypatch):
    settings = tmp_path / "settings64.sh"
    settings.touch()

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(hardware.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="settings loading timed out"):
        hardware.load_amd_environment([settings], os.environ.copy())


def test_cli_remembers_a_nonstandard_root_without_repeating_flags(tmp_path):
    base = tmp_path / "nonstandard vendor prefix"
    paths = fake_installation(base)
    config = tmp_path / ".sgrm-tools.json"
    assert hardware.main([
        "--stage", "check-tools", "--tool-root", str(base), "--tool-config", str(config),
    ]) == 0
    assert hardware.main(["--stage", "check-tools", "--tool-config", str(config)]) == 0
    assert json.loads(config.read_text())["vivado"] == paths["vivado"]


def test_no_save_flag_verifies_without_creating_a_configuration(tmp_path):
    base = tmp_path / "vendor"
    fake_installation(base)
    config = tmp_path / ".sgrm-tools.json"
    assert hardware.main([
        "--stage", "check-tools", "--tool-root", str(base), "--tool-config", str(config), "--no-save-tools",
    ]) == 0
    assert not config.exists()


def test_malformed_configuration_is_not_overwritten(tmp_path):
    config = tmp_path / ".sgrm-tools.json"
    config.write_text('{"unrelated_user_data": true}\n')
    original = config.read_bytes()
    with pytest.raises(ValueError, match="unsupported tool configuration"):
        hardware.check_tools(config_path=config, search_roots=[], save_config=True)
    assert config.read_bytes() == original


def test_configuration_save_failure_gives_a_writable_path_option(tmp_path, monkeypatch):
    base = tmp_path / "vendor"
    fake_installation(base)

    def unwritable(*args, **kwargs):
        raise PermissionError("synthetic test permission error")

    monkeypatch.setattr(hardware, "write_json", unwritable)
    with pytest.raises(RuntimeError, match="writable --tool-config or use --no-save-tools"):
        hardware.check_tools(config_path=tmp_path / ".sgrm-tools.json", search_roots=[base], save_config=True)


def test_settings_environment_preserves_values_with_newlines(tmp_path):
    settings = tmp_path / "settings64.sh"
    settings.write_text("export SGRM_MULTILINE=$'first line\\nsecond line'\n")
    loaded = hardware.load_amd_environment([settings], os.environ.copy())
    assert loaded["SGRM_MULTILINE"] == "first line\nsecond line"


@pytest.mark.parametrize("stage", ["prepare", "report"])
def test_software_stages_ignore_malformed_tool_configuration(tmp_path, monkeypatch, stage):
    config = tmp_path / ".sgrm-tools.json"
    config.write_text("not valid JSON\n")
    output = tmp_path / "hardware-output"
    output.mkdir()
    hardware.write_json(output / "plan.json", {"jobs": [], "designs": [], "design_count": 0})
    monkeypatch.setattr(hardware, "collect_reports", lambda *args: {"status": "PASS"})
    args = ["--stage", stage, "--output-dir", str(output)]
    if stage == "prepare":
        monkeypatch.setattr(hardware, "extract_source_archive", lambda *args: tmp_path)
        monkeypatch.setattr(hardware, "load_source_manifest", lambda *args: {"designs": []})
        monkeypatch.setattr(hardware, "prepare_jobs", lambda *args: {})
        args.extend(["--results-dir", str(tmp_path / "search-results")])
    assert hardware.main(args) == 0
    assert config.read_text() == "not valid JSON\n"
