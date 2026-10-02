"""Unit fixtures for the endpoint checker; these are not synthesis evidence."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hardware_reference", REPOSITORY / "hardware_validation/check_reference.py"
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def write_fixture(output):
    reference = json.loads(checker.REFERENCE.read_text())
    hardware, pairs = [], []
    for design in reference["designs"]:
        for variant in ("native", "sgrm"):
            row = {"design": design["design"], "variant": variant, "status": "OK"}
            row.update({"fifo_" + key: design[variant][key] for key in (*checker.RESOURCES, "cutil")})
            row["fifo_n_fifo"] = design["fifo_count"]
            hardware.append(row)
        pairs.append({
            "design": design["design"],
            "native_fifo_cutil": design["native"]["cutil"],
            "sgrm_fifo_cutil": design["sgrm"]["cutil"],
            "fifo_subsystem_reduction_pct": design["fifo_subsystem_reduction_pct"],
        })
    for name, records in (("hardware_resources.csv", hardware), ("paired_comparison.csv", pairs)):
        with (output / name).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=records[0])
            writer.writeheader()
            writer.writerows(records)
    (output / "summary.json").write_text(json.dumps({
        "status": "PASS", "requested_design_count": 3, "completed_pair_count": 3,
        "requested_hardware_job_count": 6, "successful_hardware_job_count": 6,
        "partial_aggregate": False, "capacities": reference["capacities"],
    }))


def test_complete_reference_fixture_matches(tmp_path):
    write_fixture(tmp_path)
    messages = checker.check(tmp_path)
    assert len(messages) == 3
    assert messages[0] == "PASS bicg: measured FIFO-subsystem reduction 21.4772%"


def test_reference_check_rejects_missing_outputs(tmp_path, capsys):
    assert checker.main(["--output-dir", str(tmp_path)]) == 1
    assert "FAIL reference check:" in capsys.readouterr().out


def test_reference_check_rejects_incomplete_summary(tmp_path):
    write_fixture(tmp_path)
    path = tmp_path / "summary.json"
    summary = json.loads(path.read_text())
    summary["status"] = "INCOMPLETE"
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="summary status"):
        checker.check(tmp_path)


def test_reference_check_rejects_resource_mismatch(tmp_path):
    write_fixture(tmp_path)
    path = tmp_path / "hardware_resources.csv"
    rows = checker.load_csv(path)
    rows[0]["fifo_lut"] = "2413"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="bicg/native lut"):
        checker.check(tmp_path)


def test_reference_check_rejects_duplicate_hardware_rows(tmp_path):
    write_fixture(tmp_path)
    path = tmp_path / "hardware_resources.csv"
    rows = checker.load_csv(path)
    with path.open("a", newline="") as stream:
        csv.DictWriter(stream, fieldnames=rows[0]).writerow(rows[0])
    with pytest.raises(ValueError, match="duplicate hardware row"):
        checker.check(tmp_path)
