import json
import subprocess

from sgrm.batch_cli import main


def test_batch_records_per_design_timeout(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    solution = bundle / "toy" / "solution1"
    solution.mkdir(parents=True)
    manifest = bundle / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "designs": [
                    {
                        "design": "toy",
                        "solution_dir": "toy/solution1",
                        "trace_sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="sgrm.cli", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    output_dir = tmp_path / "results"
    assert main(
        [
            "--manifest",
            str(manifest),
            "--output-dir",
            str(output_dir),
            "--timeout-s",
            "1",
        ]
    ) == 1
    index = json.loads((output_dir / "index.json").read_text(encoding="utf-8"))
    assert index["timeout_s"] == 1.0
    assert index["results"] == [
        {
            "design": "toy",
            "status": "failed",
            "error": "search exceeded 1 seconds",
        }
    ]
