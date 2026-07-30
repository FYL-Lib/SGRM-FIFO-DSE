import hashlib
import json

from sgrm.build_manifest_cli import main
from sgrm.manifest import load_manifest


def test_build_manifest_hashes_portable_layout(tmp_path):
    trace_root = tmp_path / "traces"
    hls_root = trace_root / "toy" / "hls_toy"
    solution_dir = hls_root / "solution1"
    solution_dir.mkdir(parents=True)
    (solution_dir / "trace.pkl").write_bytes(b"trace fixture")
    (hls_root / "hls.app").write_bytes(b"metadata")

    assert main(["--trace-root", str(trace_root)]) == 0
    manifest_path, manifest = load_manifest(trace_root / "manifest.json")
    entry = manifest["designs"][0]
    assert manifest_path == (trace_root / "manifest.json").resolve()
    assert entry["design"] == "toy"
    assert entry["solution_dir"] == "toy/hls_toy/solution1"
    assert entry["trace_sha256"] == hashlib.sha256(b"trace fixture").hexdigest()
    assert entry["metadata"]["trace_bytes"] == len(b"trace fixture")
    assert entry["metadata"]["hls_app_sha256"] == hashlib.sha256(
        b"metadata"
    ).hexdigest()
    assert json.loads((trace_root / "manifest.json").read_text())["schema_version"] == 1


def test_build_manifest_honors_design_list(tmp_path):
    trace_root = tmp_path / "traces"
    for design in ("kept", "extra"):
        solution = trace_root / design / f"hls_{design}" / "solution1"
        solution.mkdir(parents=True)
        (solution / "trace.pkl").write_bytes(design.encode())
    design_list = tmp_path / "designs.txt"
    design_list.write_text("# fixed corpus\nkept\n", encoding="utf-8")

    assert main(
        [
            "--trace-root",
            str(trace_root),
            "--design-list",
            str(design_list),
        ]
    ) == 0
    _, manifest = load_manifest(trace_root / "manifest.json")
    assert [entry["design"] for entry in manifest["designs"]] == ["kept"]
