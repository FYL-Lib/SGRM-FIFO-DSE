import importlib.util
import json
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hardware_validation", REPOSITORY / "hardware_validation/validate_hardware.py"
)
hardware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hardware)


def contract_and_result():
    contract = {
        "design": "example",
        "trace_sha256": "a" * 64,
        "fifo_count": 2,
        "fifos": [
            {"id": 0, "source_variable": "channels"},
            {"id": 1, "source_variable": "channels"},
        ],
    }
    result = {
        "schema_version": 1,
        "design": "example",
        "trace_sha256": "a" * 64,
        "fifo_count": 2,
        "epsilon": 0,
        "baseline": {"deadlock": False, "latency": 100},
        "selected": {
            "deadlock": False,
            "latency": 99,
            "fifo_depths": {"0": 2, "1": 2},
            "fifo_impl_types": {"0": "srl", "1": "srl"},
        },
    }
    return contract, result


def test_read_search_result_requires_exact_uniform_choices(tmp_path):
    contract, result = contract_and_result()
    path = tmp_path / "example.json"
    path.write_text(json.dumps(result))
    _, choices = hardware.read_search_result(path, contract)
    assert choices == {"channels": (2, "srl")}
    result["selected"]["fifo_depths"]["1"] = 4
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="different choices"):
        hardware.read_search_result(path, contract)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda x: x.update(trace_sha256="b" * 64), "different trace"),
        (lambda x: x["selected"]["fifo_depths"].pop("1"), "cover the source contract"),
        (lambda x: x["selected"].update(deadlock=True), "not deadlock-free"),
        (lambda x: x["baseline"].update(deadlock=True), "not deadlock-free"),
        (lambda x: x.update(selected=None), "not deadlock-free"),
        (lambda x: x["selected"].update(latency=101), "latency limit"),
        (lambda x: x["selected"]["fifo_impl_types"].update({"0": "auto"}), "must use"),
        (lambda x: x["selected"]["fifo_impl_types"].update({"0": []}), "must use"),
        (lambda x: x["selected"]["fifo_depths"].update({"0": True}), "invalid depth"),
    ],
)
def test_read_search_result_rejects_invalid_results(tmp_path, change, message):
    contract, result = contract_and_result()
    change(result)
    path = tmp_path / "example.json"
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match=message):
        hardware.read_search_result(path, contract)


def test_rewrite_emits_explicit_srl_and_preserves_unrelated_source():
    source = (
        "void forward() {\n"
        "  hls::stream<float> channels[2];\n"
        "  #pragma HLS STREAM variable=channels depth=82\n"
        "  #pragma HLS bind_storage variable=channels type=fifo impl=bram\n"
        "  hls::stream<float> other;\n"
        "  #pragma HLS STREAM variable=other depth=8\n"
        "  compute(channels, other);\n"
        "}\n"
    )
    rewritten = hardware.rewrite_source(source, {"channels": (2, "srl")})
    assert rewritten.count("STREAM variable=channels depth=2") == 1
    assert rewritten.count("bind_storage variable=channels type=fifo impl=srl") == 1
    assert "depth=82" not in rewritten
    assert "impl=bram" not in rewritten
    assert "#pragma HLS STREAM variable=other depth=8\n  compute(channels, other);" in rewritten


def test_rewrite_rejects_missing_or_ambiguous_declarations():
    with pytest.raises(ValueError, match="missing"):
        hardware.rewrite_source("void forward() {}\n", {"x": (2, "lutram")})
    with pytest.raises(ValueError, match="repeated"):
        hardware.rewrite_source(
            "hls::stream<float> x;\nhls::stream<float> x;\n", {"x": (2, "lutram")}
        )


def test_hierarchy_counts_inclusive_fifo_rows_once(tmp_path):
    columns = [
        "Instance", "Module", "Total LUTs", "Logic LUTs", "LUTRAMs", "SRLs",
        "FFs", "RAMB36", "RAMB18", "URAM", "DSP Blocks",
    ]
    rows = [
        ["top", "forward", 99, 99, 0, 0, 99, 99, 99, 99, 99],
        ["  fifo0", "forward_fifo_w32_d82_A", 8, 5, 1, 2, 11, 3, 1, 4, 0],
        ["    (fifo0)", "forward_fifo_w32_d82_A", 8, 5, 1, 2, 11, 3, 1, 4, 0],
        ["    nested", "nested_fifo_w32_d82", 8, 5, 1, 2, 11, 3, 1, 4, 0],
        ["  compute", "operator", 50, 50, 0, 0, 50, 0, 0, 0, 1],
        ["  fifo1", "forward_fifo_w16_d4_1", 5, 5, 0, 0, 7, 0, 0, 0, 0],
    ]
    path = tmp_path / "hierarchy.rpt"
    path.write_text("\n".join("|" + "|".join(map(str, row)) + "|" for row in [columns, *rows]))
    resources = hardware.parse_hierarchy(path)
    assert {key: resources[key] for key in ["lut", "ff", "bram", "uram", "n_fifo"]} == {
        "lut": 13, "ff": 18, "bram": 3.5, "uram": 4, "n_fifo": 2,
    }
    assert resources["lutram"] == 1
    assert resources["srl"] == 2
    assert resources["cutil"] == pytest.approx(
        (3.5 / 967 + 4 / 463 + 18 / 1799680 + 13 / 899840) / 4
    )


def test_hierarchy_rejects_missing_fifo_evidence(tmp_path):
    path = tmp_path / "empty.rpt"
    path.write_text("no hierarchical resource table\n")
    with pytest.raises(ValueError, match="no FIFO subsystem"):
        hardware.parse_hierarchy(path)


@pytest.mark.parametrize("table_format", [False, True])
def test_export_and_hls_schedule_are_distinct(tmp_path, table_format):
    export = tmp_path / "export.rpt"
    export.write_text(
        "LUT: 76\nFF: 54\nBRAM: 3\nURAM: 2\nDSP: 1\n"
        + (
            "| Target | 10.000 |\n| Post-Synthesis | 8.099 |\n"
            if table_format else
            "CP achieved post-synthesis: 8.099\nCP required: 10.000\n"
        )
    )
    parsed = hardware.parse_export(export)
    assert parsed["resources"]["uram"] == 2
    assert parsed["clock_period_ns"] == pytest.approx(8.099)
    assert parsed["timing_met"] is True
    schedule = tmp_path / "csynth.xml"
    schedule.write_text(
        "<Report><SummaryOfOverallLatency><Worst-caseLatency>834</Worst-caseLatency>"
        "</SummaryOfOverallLatency></Report>"
    )
    assert hardware.parse_hls_latency(schedule) == 834


def test_geometric_mean_is_not_arithmetic_mean():
    assert hardware.geometric_mean([0.25, 1]) == pytest.approx(0.5)
    assert hardware.geometric_mean([0, 1]) == 0
    assert hardware.geometric_mean([]) is None
    with pytest.raises(ValueError):
        hardware.geometric_mean([float("nan")])


def test_report_paths_prefer_canonical_export_and_support_legacy(tmp_path):
    job = {"run_dir": str(tmp_path), "top": "forward"}
    canonical = tmp_path / "hls_proj/solution1/impl/report/verilog/export_syn.rpt"
    legacy = canonical.with_name("forward_export.rpt")
    assert hardware.report_paths(job)[1] == canonical
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    assert hardware.report_paths(job)[1] == legacy
    canonical.touch()
    assert hardware.report_paths(job)[1] == canonical


def test_incomplete_pairs_are_not_reported_as_complete(tmp_path):
    jobs = []
    for variant in ("native", "sgrm"):
        directory = tmp_path / variant
        directory.mkdir()
        jobs.append({"design": "example", "variant": variant, "run_dir": str(directory)})
    hardware.write_json(tmp_path / "sgrm/status.json", {"status": "FAILED", "error": "tool failure"})
    summary = hardware.collect_reports({"jobs": jobs, "designs": ["example"], "design_count": 1}, tmp_path)
    assert summary["status"] == "INCOMPLETE"
    assert summary["completed_pair_count"] == 0
    assert summary["geometric_mean_fifo_subsystem_reduction_pct"] is None
    assert summary["partial_aggregate"] is True


def test_published_source_bundle_covers_all_thirty_designs(tmp_path):
    root = hardware.extract_source_archive(tmp_path)
    manifest = hardware.load_source_manifest(root)
    assert len(manifest["designs"]) == 30
    assert sum(item["fifo_count"] for item in manifest["designs"]) == 4819
    assert manifest["hardware_protocol"]["disable_block_condition_registers"] is True
    assert (root / "STREAM_HLS_LICENSE").is_file()
    for design in manifest["designs"]:
        directory = root / design["directory"]
        assert (directory / design["kernel"]).is_file()
        assert (directory / design["testbench"]).is_file()
        assert any(name.startswith("data/") for name in design["files"])


@pytest.fixture
def preparation_inputs(tmp_path):
    contract, result = contract_and_result()
    source_root = tmp_path / "source-bundle"
    original = source_root / "example"
    kernel = original / "src/example.cpp"
    testbench = original / "src/example_tb.cpp"
    kernel.parent.mkdir(parents=True)
    kernel.write_text(
        "void forward() {\n"
        "  hls::stream<float> channels[2];\n"
        "  #pragma HLS STREAM variable=channels depth=82\n"
        "  #pragma HLS bind_storage variable=channels type=fifo impl=bram\n"
        "}\n"
    )
    testbench.write_text("int main() { return 0; }\n")
    contract.update({
        "directory": "example",
        "kernel": "src/example.cpp",
        "testbench": "src/example_tb.cpp",
        "top": "forward",
        "files": {
            str(path.relative_to(original)): {
                "bytes": path.stat().st_size,
                "sha256": hardware.sha256(path),
            }
            for path in (kernel, testbench)
        },
    })
    manifest = {
        "designs": [contract],
        "part": "xcvc1902-vsva2197-2MP-e-S",
        "clock_period_ns": 10.0,
        "hardware_protocol": {
            "unsafe_math_optimizations": True,
            "disable_block_condition_registers": True,
        },
    }
    hardware.write_json(source_root / "manifest.json", manifest)
    results_dir = tmp_path / "search-results"
    hardware.write_json(results_dir / "example.json", result)
    return source_root, manifest, results_dir, tmp_path / "hardware"


def test_prepare_uses_readable_names_and_preserves_fifo_choices(preparation_inputs):
    source_root, manifest, results_dir, output = preparation_inputs
    plan = hardware.prepare_jobs(source_root, manifest, results_dir, output, ["example"])
    assert [job["variant"] for job in plan["jobs"]] == ["native", "sgrm"]
    assert [Path(job["run_dir"]) for job in plan["jobs"]] == [
        output / "work/example/native", output / "work/example/sgrm",
    ]
    assert (output / "work/example/native/src/example.cpp").read_bytes() == (
        source_root / "example/src/example.cpp"
    ).read_bytes()
    selected = plan["jobs"][1]
    assert selected["requested_choices_by_source_variable"] == {
        "channels": {"depth": 2, "implementation": "srl"},
    }
    text = (Path(selected["run_dir"]) / "src/example.cpp").read_text()
    assert "STREAM variable=channels depth=2" in text
    assert "bind_storage variable=channels type=fifo impl=srl" in text


def test_prepare_same_configuration_keeps_the_same_plan(preparation_inputs):
    source_root, manifest, results_dir, output = preparation_inputs
    first = hardware.prepare_jobs(source_root, manifest, results_dir, output, ["example"])
    second = hardware.prepare_jobs(source_root, manifest, results_dir, output, ["example"])
    assert second == first


def test_prepare_changed_choices_preserves_existing_selected_files(preparation_inputs):
    source_root, manifest, results_dir, output = preparation_inputs
    plan = hardware.prepare_jobs(source_root, manifest, results_dir, output, ["example"])
    selected_dir = Path(plan["jobs"][1]["run_dir"])
    before = {
        path.relative_to(selected_dir): path.read_bytes()
        for path in selected_dir.rglob("*") if path.is_file()
    }
    result_path = results_dir / "example.json"
    result = hardware.read_json(result_path)
    result["selected"]["fifo_depths"] = {"0": 4, "1": 4}
    hardware.write_json(result_path, result)
    with pytest.raises(ValueError, match="fresh --output-dir"):
        hardware.prepare_jobs(source_root, manifest, results_dir, output, ["example"])
    after = {
        path.relative_to(selected_dir): path.read_bytes()
        for path in selected_dir.rglob("*") if path.is_file()
    }
    assert after == before
    assert hardware.read_json(output / "plan.json") == plan


@pytest.fixture
def legacy_cached_job(tmp_path, monkeypatch):
    fingerprint = hardware.object_hash({"legacy_configuration": True})
    run_dir = tmp_path / "work/example" / ("sgrm-" + fingerprint[:16])
    source = run_dir / "src/example.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("void forward() {}\n")
    report = run_dir / "hierarchical.rpt"
    report.write_text("cached report\n")
    job = {
        "design": "example",
        "variant": "sgrm",
        "fingerprint": fingerprint,
        "run_dir": str(run_dir),
        "source_files_sha256": {"src/example.cpp": hardware.sha256(source)},
    }
    state = {
        "status": "OK",
        "fingerprint": fingerprint,
        "report_sha256": {str(report): hardware.sha256(report)},
    }
    hardware.write_json(run_dir / "status.json", state)

    def unexpected_tool_call(*args, **kwargs):
        pytest.fail("a valid cached job must not invoke hardware tools")

    monkeypatch.setattr(hardware, "command", unexpected_tool_call)
    return job, state, source, report


def test_legacy_named_completed_job_is_reused_without_synthesis(legacy_cached_job, capsys):
    job, state, _, _ = legacy_cached_job
    assert hardware.run_job(job, "unused-tool", "2024.2") == state
    assert capsys.readouterr().out.strip() == "CACHED example/sgrm"


@pytest.mark.parametrize("changed_file", ["source", "report"])
def test_legacy_cache_still_rejects_changed_files(legacy_cached_job, changed_file):
    job, _, source, report = legacy_cached_job
    path = source if changed_file == "source" else report
    path.write_text("changed after preparation\n")
    message = "staged input changed" if changed_file == "source" else "cached report changed"
    with pytest.raises(ValueError, match=message):
        hardware.run_job(job, "unused-tool", "2024.2")


def test_run_job_uses_checked_vivado_environment_for_hls_export(preparation_inputs, monkeypatch):
    source_root, manifest, results_dir, output = preparation_inputs
    job = hardware.prepare_jobs(source_root, manifest, results_dir, output, ["example"])["jobs"][1]
    run_dir = Path(job["run_dir"])
    hls_xml, export, hierarchy = hardware.report_paths(job)
    child = {"PATH": "/selected/Vivado/bin", "XILINX_VIVADO": "/selected/Vivado"}
    toolchain = {
        "environment": child,
        "executables": {"hls": "/selected/HLS/bin/vitis_hls", "vivado": "/selected/Vivado/bin/vivado"},
        "versions": {"hls": "Vitis HLS v2024.2", "vivado": "Vivado v2024.2"},
    }
    calls = []

    def mock_command(tool, script, directory, log, *, env):
        assert tool == toolchain["executables"]["hls"]
        assert directory == run_dir
        assert env is child
        calls.append(script.name)
        log.write_text("mock tool log\n")
        if script.name == "run_hls.tcl":
            hls_xml.parent.mkdir(parents=True)
            hls_xml.write_text(
                "<Report><SummaryOfOverallLatency><Worst-caseLatency>99</Worst-caseLatency>"
                "</SummaryOfOverallLatency></Report>"
            )
        else:
            assert "export_design -flow syn -format ip_catalog" in script.read_text()
            export.parent.mkdir(parents=True)
            export.write_text(
                "LUT: 8\nFF: 12\nBRAM: 0\nURAM: 0\n"
                "CP achieved post-synthesis: 8.0\nCP required: 10.0\n"
            )
            hierarchy.parent.mkdir(parents=True)
            hierarchy.write_text(
                "|Instance|Module|Total LUTs|FFs|RAMB36|RAMB18|URAM|\n"
                "|fifo0|example_fifo_w32_d2|8|12|0|0|0|\n"
            )
        return 0.0

    monkeypatch.setattr(hardware, "command", mock_command)
    state = hardware.run_job(
        job, toolchain["executables"]["hls"], toolchain["versions"]["hls"], toolchain=toolchain,
    )
    assert state["status"] == "OK"
    assert calls == ["run_hls.tcl", "run_vivado.tcl"]
    assert state["vivado_tool"] == toolchain["executables"]["vivado"]
    assert state["vivado_tool_version"] == toolchain["versions"]["vivado"]
    assert state["hls_interface"] == "classic"
