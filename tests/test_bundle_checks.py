import hashlib
import importlib.util
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bundle_checks", REPOSITORY / "datasets/check_bundles.py"
)
bundles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundles)


@pytest.fixture
def bundle_repository(tmp_path):
    repository = tmp_path / "repository"
    directory = repository / "datasets"
    directory.mkdir(parents=True)
    records = ["# Bundle verification records"]
    for filename, contents in (("traces.tar.xz", b"example trace bundle"), ("sources.tar.xz", b"example source bundle")):
        (directory / filename).write_bytes(contents)
        checksum = hashlib.sha256(contents).hexdigest()
        records.append(f"{checksum}  datasets/{filename}")
    (repository / bundles.RECORDS_FILE).write_text("\n".join(records) + "\n")
    return repository


def test_bundle_check_prints_readable_names(bundle_repository, monkeypatch, capsys):
    monkeypatch.setattr(bundles, "REPOSITORY", bundle_repository)
    assert bundles.main([]) == 0
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        "PASS datasets/traces.tar.xz", "PASS datasets/sources.tar.xz",
    ]
    assert output.err == ""


def test_bundle_check_rejects_modified_data_without_printing_values(bundle_repository, capsys):
    records = bundles.read_verification_records(bundle_repository / bundles.RECORDS_FILE)
    archive = bundle_repository / "datasets/traces.tar.xz"
    archive.write_bytes(b"modified trace bundle")
    actual = bundles.file_checksum(archive)
    assert bundles.verify_bundles(bundle_repository) == 1
    output = capsys.readouterr()
    assert "FAIL datasets/traces.tar.xz: file integrity check failed" in output.err
    assert "PASS datasets/sources.tar.xz" in output.out
    assert actual not in output.err
    assert all(expected not in output.err for _, expected in records)


def test_bundle_check_reports_missing_data(bundle_repository, capsys):
    (bundle_repository / "datasets/traces.tar.xz").unlink()
    assert bundles.verify_bundles(bundle_repository) == 1
    assert "file is missing or cannot be read" in capsys.readouterr().err


@pytest.mark.parametrize("records", [
    "not a valid record\n",
    "a" * 64 + "  ../outside.tar.xz\n",
    "a" * 64 + "  /outside.tar.xz\n",
    ("a" * 64 + "  datasets/traces.tar.xz\n") * 2,
    "# No bundle records\n",
])
def test_bundle_check_rejects_invalid_records(bundle_repository, records, capsys):
    (bundle_repository / bundles.RECORDS_FILE).write_text(records)
    assert bundles.verify_bundles(bundle_repository) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "could not read bundle verification records" in output.err
    assert "a" * 64 not in output.err


def test_bundle_check_rejects_symlinks_outside_repository(bundle_repository, tmp_path, capsys):
    outside = tmp_path / "outside.tar.xz"
    outside.write_bytes(b"example trace bundle")
    archive = bundle_repository / "datasets/traces.tar.xz"
    archive.unlink()
    archive.symlink_to(outside)
    assert bundles.verify_bundles(bundle_repository) == 1
    assert "bundle path leaves the repository" in capsys.readouterr().err
