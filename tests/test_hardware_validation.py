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
