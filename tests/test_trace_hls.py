"""Portable source-synthesis entry point tests; never run AMD synthesis."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trace_generation import generate_trace, run_hls, tooling


@pytest.fixture(autouse=True)
def isolated_tools(tmp_path, monkeypatch):
    """No test can accidentally select a real tool or machine configuration."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for name in ("VITIS_HLS", "VIVADO", "XILINX_HLS", "XILINX_VIVADO", "XILINX_VITIS"):
        monkeypatch.delenv(name, raising=False)
    tools = tooling.hardware_module()
    monkeypatch.setattr(tools, "DEFAULT_TOOL_CONFIG", tmp_path / ".sgrm-tools.json")
    monkeypatch.setattr(tools, "standard_tool_roots", lambda: [])
    monkeypatch.setattr(tooling, "hardware_module", lambda: tools)
    monkeypatch.setattr(run_hls, "hardware_module", lambda: tools)
    return tools


@pytest.fixture
def sources(tmp_path, monkeypatch):
    root = tmp_path / "download with spaces and 'quote'" / "original sources"
    names = (ROOT / "datasets/stream_hls_30.txt").read_text().splitlines()
    names = [name.split("#", 1)[0].strip() for name in names if name.split("#", 1)[0].strip()]
    designs = []
    for name in names:
        directory = root / name
        files = {}
        for relative in ("hls.tcl", f"src/{name}.cpp", f"src/{name}_tb.cpp"):
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = b"original source fixture\n"
            path.write_bytes(content)
            files[relative] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        designs.append({"design": name, "directory": name, "files": files,
                        "fifo_count": 0, "fifos": []})
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "corpus": "Stream-HLS30", "design_count": 30,
        "designs": designs, "capacities": isolated_capacities(),
        "part": "xcvc1902-vsva2197-2MP-e-S", "clock_period_ns": 10.0,
    }))
    monkeypatch.setattr(run_hls, "DEFAULT_SOURCES", root)
    return root


def isolated_capacities():
    return {"bram": 967, "uram": 463, "ff": 1799680, "lut": 899840}


def fake_hls(base, version="2024.2", layout="product-first"):
    root = base / "Vitis_HLS" / version if layout == "product-first" else base / version / "Vitis_HLS"
    executable = root / "bin/vitis_hls"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        '#!/bin/sh\n'
        'if [ "$1" != "-version" ]; then exit 99; fi\n'
        'if [ "${SGRM_TRACE_TEST_READY:-}" != "loaded" ]; then exit 98; fi\n'
        f'printf "%s\\n" "Vitis HLS v{version} (64-bit)"\n'
    )
    executable.chmod(0o755)
    (root / "settings64.sh").write_text('export SGRM_TRACE_TEST_READY="loaded"\n')
    (root / "include").mkdir()
    for name in ("Makefile.sysc.rules", "hls_stream.h"):
        (root / "include" / name).write_text("runtime fixture\n")
    return executable


def test_check_tools_needs_neither_sources_nor_vivado(tmp_path, capsys):
    base = tmp_path / "custom vendor prefix"
    executable = fake_hls(base)
    assert run_hls.main(["--stage", "check-tools", "--tool-root", str(base)]) == 0
    output = capsys.readouterr().out
    assert str(executable) in output and "TOOL CHECK PASS" in output
    assert "no synthesis started" in output
    assert not list(tmp_path.rglob("hls_synthesis.log"))
    assert not list(tmp_path.rglob(".sgrm-tools.json"))


@pytest.mark.parametrize("layout", ["product-first", "version-first"])
def test_plan_skips_unified_path_wrapper_and_handles_spaces(tmp_path, monkeypatch, sources, capsys, layout):
    base = tmp_path / "nonstandard AMD with spaces and 'quote'"
    executable = fake_hls(base, layout=layout)
    wrapper = tmp_path / "wrong PATH/bin/vitis_hls"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "ERROR: [vitis-run] unknown option"\nexit 1\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(wrapper.parent) + ":/usr/bin:/bin")
    monkeypatch.setattr(run_hls, "invoke_hls", lambda *args: pytest.fail("Plan started synthesis"))
    before = dict(os.environ)
    files = sorted(str(path.relative_to(sources)) for path in sources.rglob("*"))
    unrelated = tmp_path / "unrelated working directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    assert run_hls.main(["--design", "gemm", "--tool-root", str(base)]) == 0
    assert dict(os.environ) == before
    assert sorted(str(path.relative_to(sources)) for path in sources.rglob("*")) == files
    output = capsys.readouterr().out
    assert str(executable) in output and "PLAN PASS" in output
    assert str(sources / "gemm") in output
    assert "hls.tcl gemm syn" in output
    assert "-f hls.tcl" not in output
    assert not list(unrelated.iterdir())


def test_explicit_unified_wrapper_is_not_silently_replaced(tmp_path, sources, monkeypatch):
    base = tmp_path / "vendor"
    fake_hls(base)
    wrapper = tmp_path / "wrapper/vitis_hls"
    wrapper.parent.mkdir()
    wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "vitis-run rejects -version"\nexit 1\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(run_hls, "invoke_hls", lambda *args: pytest.fail("Rejected tool started synthesis"))
    with pytest.raises(RuntimeError, match="forwards to the unified"):
        run_hls.main(["--design", "gemm", "--stage", "run", "--vitis-hls", str(wrapper),
                      "--tool-root", str(base)])
    assert not (sources / "gemm/hls_gemm").exists()


def test_explicit_nonstandard_executable_and_site_settings(tmp_path, sources, monkeypatch):
    executable = fake_hls(tmp_path / "custom install")
    settings = tmp_path / "site settings with spaces.sh"
    settings.write_text('export SGRM_TRACE_SITE="site selected"\n')
    observed = {}

    def invoke(command, project, environment):
        observed.update(command=command, project=project, environment=environment)
        return 7

    monkeypatch.setattr(run_hls, "invoke_hls", invoke)
    before = dict(os.environ)
    with pytest.raises(RuntimeError, match=r"failed \(exit 7\)"):
        run_hls.main(["--design", "gemm", "--stage", "run", "--vitis-hls", str(executable),
                      "--settings", str(settings)])
    assert observed["environment"]["SGRM_TRACE_SITE"] == "site selected"
    assert observed["project"] == sources / "gemm"
    assert dict(os.environ) == before


def test_relative_root_and_site_settings_are_resolved_before_private_loading(tmp_path, sources, monkeypatch):
    base = tmp_path / "relative vendor"
    fake_hls(base)
    settings = tmp_path / "site settings.sh"
    settings.write_text('export SGRM_TRACE_RELATIVE="loaded"\n')
    monkeypatch.chdir(tmp_path)
    assert run_hls.main(["--design", "gemm", "--tool-root", "relative vendor",
                         "--settings", "site settings.sh"]) == 0
    assert "SGRM_TRACE_RELATIVE" not in os.environ


def test_version_first_classic_launcher_uses_sibling_vitis_runtime(tmp_path):
    base = tmp_path / "vendor"
    executable = fake_hls(base, layout="version-first")
    includes = executable.parent.parent / "include"
    runtime = base / "2024.2/Vitis/include"
    runtime.mkdir(parents=True)
    for path in list(includes.iterdir()):
        path.rename(runtime / path.name)
    selected, environment = tooling.find_hls(None, [base], [], dict(os.environ))
    assert selected == str(executable)
    assert environment["XILINX_HLS"] == str(runtime.parent)


@pytest.mark.parametrize("version", ["2024.1", "2025.1"])
def test_wrong_tool_release_fails_before_run(tmp_path, sources, monkeypatch, version):
    executable = fake_hls(tmp_path / "vendor", version=version)
    monkeypatch.setattr(run_hls, "invoke_hls", lambda *args: pytest.fail("Wrong version started synthesis"))
    with pytest.raises(RuntimeError, match=f"found {version}"):
        run_hls.main(["--design", "gemm", "--stage", "run", "--vitis-hls", str(executable)])
    assert not (sources / "gemm/hls_gemm").exists()


def test_missing_source_does_not_probe_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hls, "find_hls", lambda *args: pytest.fail("Probed tools with missing sources"))
    with pytest.raises(FileNotFoundError, match="--sources-dir"):
        run_hls.main(["--design", "gemm", "--sources-dir", str(tmp_path / "missing")])


def test_changed_original_source_fails_before_tool_selection(sources, monkeypatch):
    (sources / "gemm/hls.tcl").write_text("changed original Tcl\n")
    monkeypatch.setattr(run_hls, "find_hls", lambda *args: pytest.fail("Probed tools with changed sources"))
    with pytest.raises(ValueError, match="original benchmark checksum mismatch"):
        run_hls.main(["--design", "gemm", "--stage", "run"])


@pytest.mark.parametrize("design", ["unknown", "../gemm", "/tmp/gemm"])
def test_unknown_or_escaping_design_fails_before_tool_selection(sources, monkeypatch, design):
    monkeypatch.setattr(run_hls, "find_hls", lambda *args: pytest.fail("Probed tools for invalid design"))
    with pytest.raises(ValueError, match="Unknown Stream-HLS design"):
        run_hls.main(["--design", design])


@pytest.mark.parametrize("output", ["hls_gemm", "hls_synthesis.log"])
def test_run_never_overwrites_existing_synthesis(tmp_path, sources, monkeypatch, output):
    executable = fake_hls(tmp_path / "vendor")
    path = sources / "gemm" / output
    path.mkdir() if output == "hls_gemm" else path.write_text("old log\n")
    monkeypatch.setattr(run_hls, "invoke_hls", lambda *args: pytest.fail("Overwrote old synthesis"))
    with pytest.raises(FileExistsError, match="Refusing to reuse or overwrite"):
        run_hls.main(["--design", "gemm", "--stage", "run", "--vitis-hls", str(executable)])
    assert path.exists()


def test_successful_run_uses_original_syn_branch_and_checks_export_outputs(tmp_path, sources, monkeypatch, capsys):
    executable = fake_hls(tmp_path / "vendor")
    observed = {}

    def invoke(command, project, environment):
        observed.update(command=command, project=project, environment=environment)
        solution = project / "hls_gemm/solution1"
        for path in generate_trace.required_synthesis_outputs(solution, "forward"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthesis output fixture")
        (solution / "syn/report/forward_csynth.xml").write_text(
            "<profile><ReportVersion><Version>2024.2</Version></ReportVersion></profile>")
        return 0

    monkeypatch.setattr(run_hls, "invoke_hls", invoke)
    before = dict(os.environ)
    assert run_hls.main(["--design", "gemm", "--stage", "run", "--vitis-hls", str(executable)]) == 0
    assert observed["command"] == [str(executable), "-l", "hls_synthesis.log", "hls.tcl", "gemm", "syn"]
    assert observed["project"] == sources / "gemm"
    assert observed["environment"]["SGRM_TRACE_TEST_READY"] == "loaded"
    assert dict(os.environ) == before
    assert "SYNTHESIZED gemm" in capsys.readouterr().out
    assert not list(sources.rglob("trace.pkl"))
    assert not list(sources.rglob("dut.hcp"))


def test_success_exit_without_required_outputs_is_not_reported_as_complete(tmp_path, sources, monkeypatch):
    executable = fake_hls(tmp_path / "vendor")
    monkeypatch.setattr(run_hls, "invoke_hls", lambda *args: 0)
    with pytest.raises(ValueError, match="Complete HLS C synthesis"):
        run_hls.main(["--design", "gemm", "--stage", "run", "--vitis-hls", str(executable)])


def test_invoke_hls_passes_argv_cwd_and_private_environment_without_a_shell(tmp_path, monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(run_hls.subprocess, "run", fake_run)
    command = ["/vendor path/bin/vitis_hls", "-l", "hls_synthesis.log", "hls.tcl", "gemm", "syn"]
    environment = {"PATH": "/vendor/bin", "PWD": "/old location"}
    assert run_hls.invoke_hls(command, tmp_path, environment) == 3
    assert observed["command"] == command and observed["cwd"] == tmp_path
    assert observed["env"] == {"PATH": "/vendor/bin", "PWD": str(tmp_path)}
    assert observed["stdin"] == subprocess.DEVNULL
    assert "shell" not in observed
    assert environment["PWD"] == "/old location"


@pytest.mark.parametrize("arguments", [[], ["--stage", "run"],
                                       ["--stage", "check-tools", "--design", "gemm"]])
def test_incomplete_or_ambiguous_cli_stops_before_tools(monkeypatch, arguments):
    monkeypatch.setattr(run_hls, "find_hls", lambda *args: pytest.fail("Invalid CLI probed tools"))
    with pytest.raises(SystemExit) as error:
        run_hls.main(arguments)
    assert error.value.code == 2


@pytest.fixture(scope="module")
def original_tcl_contract(tmp_path_factory):
    """Execute the archived Tcl with stub HLS commands, not a mocked argv list.

    This checks all original scripts' project/source/action selection without
    AMD tools. Real AMD argument forwarding is also checked separately by the
    source-to-trace integration validation documented in VALIDATION.md.
    """
    tclsh = shutil.which("tclsh")
    if not tclsh:
        pytest.skip("Tcl interpreter is unavailable; no AMD tool is invoked")
    root = tmp_path_factory.mktemp("original Tcl contracts with spaces")
    driver = root / "contract.tcl"
    driver.write_text(
        'set script [lindex $argv 0]\n'
        'set argv [lrange $argv 1 end]\n'
        'proc open_project {name} {puts "PROJECT $name"}\n'
        'proc add_files {args} {puts "SOURCE $args"}\n'
        'foreach name {set_top open_solution set_part create_clock config_compile config_dataflow} {\n'
        '    proc $name {args} {}\n'
        '}\n'
        'proc csynth_design {} {puts "ACTION SYNTHESIS"}\n'
        'foreach name {csim_design cosim_design export_design get_clock_period} {\n'
        '    proc $name {args} {error "Unexpected hardware action"}\n'
        '}\n'
        'source $script\n'
    )
    launcher = root / "classic-launcher-contract"
    launcher.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-l" ]; then shift 2; fi\n'
        f'exec {shlex.quote(tclsh)} {shlex.quote(str(driver))} "$@"\n'
    )
    launcher.chmod(0o755)
    projects = {}
    with tarfile.open(ROOT / "datasets/sgrm-stream-hls-30-sources-v0.1.0.tar.xz") as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if member.isfile() and path.name == "hls.tcl":
                design = path.parent.name
                project = root / design
                project.mkdir()
                (project / "hls.tcl").write_bytes(archive.extractfile(member).read())
                projects[design] = project
    assert len(projects) == 30
    return launcher, projects


@pytest.mark.parametrize("design", [
    name for name in (ROOT / "datasets/stream_hls_30.txt").read_text().splitlines()
    if name.strip() and not name.lstrip().startswith("#")
])
def test_original_tcl_selects_correct_design_and_synthesis_branch(original_tcl_contract, design):
    launcher, projects = original_tcl_contract
    completed = subprocess.run(run_hls.synthesis_command(str(launcher), design),
                               cwd=projects[design], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = completed.stdout.splitlines()
    assert lines == [f"PROJECT hls_{design}", f"SOURCE src/{design}.cpp",
                     f"SOURCE -tb src/{design}_tb.cpp", "ACTION SYNTHESIS"]


def test_original_tcl_contract_rejects_shifted_launcher_options(original_tcl_contract):
    launcher, projects = original_tcl_contract
    completed = subprocess.run([str(launcher), "-l", "hls_synthesis.log",
                                "-f", "hls.tcl", "gemm", "syn"],
                               cwd=projects["gemm"], capture_output=True, text=True, check=False)
    assert completed.returncode != 0
    assert "ACTION SYNTHESIS" not in completed.stdout
