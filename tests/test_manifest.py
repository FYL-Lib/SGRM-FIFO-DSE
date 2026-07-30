import json
from pathlib import Path

import pytest

from sgrm.manifest import ManifestError, load_manifest, resolve_solution_dir
from sgrm.verify_cli import verify_result


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_builtin_manifest_is_portable():
    manifest_path, manifest = load_manifest(
        REPOSITORY_ROOT / "examples" / "traces" / "manifest.json"
    )
    entry = manifest["designs"][0]
    assert entry["design"] == "bicg"
    assert resolve_solution_dir(manifest_path, entry).is_dir()


def test_manifest_rejects_parent_traversal(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "designs": [
                    {
                        "design": "bicg",
                        "solution_dir": "../outside",
                        "trace_sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="stay inside"):
        load_manifest(manifest_path)


def test_manifest_rejects_symlink_escape(tmp_path):
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    outside.mkdir()
    (bundle / "escape").symlink_to(outside, target_is_directory=True)
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "designs": [
                    {
                        "design": "bicg",
                        "solution_dir": "escape",
                        "trace_sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _, manifest = load_manifest(manifest_path)
    with pytest.raises(ManifestError, match="escapes"):
        resolve_solution_dir(manifest_path, manifest["designs"][0])


def test_result_verifier_rejects_non_finite_latency():
    _, manifest = load_manifest(
        REPOSITORY_ROOT / "examples" / "traces" / "manifest.json"
    )
    entry = manifest["designs"][0]
    expected = entry["expected"]
    result = {
        "schema_version": 1,
        "design": "bicg",
        "trace_sha256": entry["trace_sha256"],
        "epsilon": 0.0,
        "fifo_count": expected["fifo_count"],
        "group_count": expected["group_count"],
        "evaluations": expected["evaluations"],
        "selected_evaluation": expected["selected_evaluation"],
        "cutil_reduction_pct": expected["cutil_reduction_pct"],
        "raw_resource_reduction_pct": expected["raw_resource_reduction_pct"],
        "baseline": {
            **expected["baseline"],
            "cutil": 0.00088043429943101,
        },
        "selected": {
            **expected["selected"],
            "cutil": 0.0007762491109530584,
        },
    }
    assert verify_result(result, entry) == []
    result["selected"]["latency"] = float("nan")
    errors = verify_result(result, entry)
    assert any("finite numeric" in error for error in errors)
