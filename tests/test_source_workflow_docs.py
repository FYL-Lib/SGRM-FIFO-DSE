"""Check the documented 30-design wiring without invoking AMD tools."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = Path("results/source-to-search-30")


def workflow_block():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Reproduce all 30 searches from original sources\n", 1)[1]
    section = section.split("\n## ", 1)[0]
    return re.search(r"```bash\n(.*?)\n```", section, re.DOTALL).group(1)


@pytest.fixture
def documented_workflow(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("The documented batch workflow requires Bash")
    checkout = tmp_path / "checkout with spaces"
    (checkout / "datasets").mkdir(parents=True)
    shutil.copyfile(ROOT / "datasets/stream_hls_30.txt", checkout / "datasets/stream_hls_30.txt")
    names = [line.strip() for line in (checkout / "datasets/stream_hls_30.txt").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    binary = tmp_path / "command fixtures"
    binary.mkdir()
    calls = tmp_path / "calls.jsonl"
    # All external processes are fixtures. Only the manifest builder is real;
    # it checks the exported layout and mandatory 30-design coverage.
    shim = binary / "workflow-command"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "name = Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['SGRM_DOC_CALLS']).open('a') as log:\n"
        "    log.write(json.dumps([name, *args]) + '\\n')\n"
        "phase = ''\n"
        "design = ''\n"
        "if name == 'python' and args[0] == 'trace_generation/run_hls.py' and '--design' in args:\n"
        "    phase = 'synthesis'\n"
        "    design = args[args.index('--design') + 1]\n"
        "if name == 'python' and args[0] == 'trace_generation/generate_trace.py':\n"
        "    phase = 'export'\n"
        "    output = Path(args[args.index('--output') + 1])\n"
        "    design = output.parts[-4]\n"
        "if os.environ.get('SGRM_DOC_FAIL') == phase + ':' + design:\n"
        "    sys.exit(7)\n"
        "if phase == 'export' and design != os.environ.get('SGRM_DOC_OMIT_TRACE'):\n"
        "    output.parent.mkdir(parents=True, exist_ok=True)\n"
        "    output.write_bytes(('trace layout fixture for ' + design).encode())\n"
        "if name == 'sgrm-build-manifest':\n"
        "    from sgrm.build_manifest_cli import main\n"
        "    sys.exit(main(args))\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    for name in ("python", "tar", "sgrm-build-manifest", "sgrm-trace-batch", "sgrm-verify-results"):
        (binary / name).symlink_to(shim.name)
    environment = dict(os.environ)
    environment.update(PATH=str(binary) + os.pathsep + os.defpath,
                       PYTHONPATH=str(ROOT / "src"), SGRM_DOC_CALLS=str(calls))
    for name in ("SGRM_DOC_FAIL", "SGRM_DOC_OMIT_TRACE"):
        environment.pop(name, None)

    def run(**updates):
        child = dict(environment, **updates)
        result = subprocess.run([bash, "-c", workflow_block()], cwd=checkout,
                                env=child, capture_output=True, text=True, timeout=30)
        records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, records

    return checkout, names, run


def test_readme_full_source_workflow_is_valid_bash():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash is unavailable")
    result = subprocess.run([bash, "-n"], input=workflow_block(),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_readme_generates_all_30_and_searches_their_new_manifest(documented_workflow):
    checkout, names, run = documented_workflow
    original = checkout / "sgrm-stream-hls-30-sources-v0.1.0/gemm/hls_gemm"
    original.mkdir(parents=True)
    marker = original / "keep.txt"
    marker.write_text("earlier single-design run", encoding="utf-8")

    result, records = run()
    assert result.returncode == 0, result.stdout + result.stderr
    operations = [args for args in records if (args[0] == "python" and "--design" in args)
                  or args[:2] == ["python", "trace_generation/generate_trace.py"]]
    assert len(names) == 30 and len(operations) == 60
    for index, name in enumerate(names):
        synthesis, export = operations[2 * index:2 * index + 2]
        assert synthesis == ["python", "trace_generation/run_hls.py", "--sources-dir",
                             str(WORKFLOW / "sources/sgrm-stream-hls-30-sources-v0.1.0"),
                             "--design", name, "--stage", "run"]
        assert export == ["python", "trace_generation/generate_trace.py", "--solution-dir",
                          str(WORKFLOW / "sources/sgrm-stream-hls-30-sources-v0.1.0" /
                              name / f"hls_{name}/solution1"), "--output",
                          str(WORKFLOW / "traces" / name / f"hls_{name}/solution1/trace.pkl")]

    manifest = WORKFLOW / "traces/manifest.json"
    entries = json.loads((checkout / manifest).read_text())["designs"]
    assert [entry["design"] for entry in entries] == names
    assert all((checkout / manifest.parent / entry["solution_dir"] / "trace.pkl").is_file()
               for entry in entries)
    assert records[-2] == ["sgrm-trace-batch", "--manifest", str(manifest), "--output-dir",
                           str(WORKFLOW / "search"), "--budget", "1000", "--seed", "1",
                           "--epsilon", "0", "--fail-fast"]
    assert records[-1] == ["sgrm-verify-results", "--manifest", str(manifest),
                           "--results-dir", str(WORKFLOW / "search")]
    assert marker.read_text() == "earlier single-design run"


@pytest.mark.parametrize("phase", ["synthesis", "export"])
def test_readme_stops_before_search_on_generation_failure(documented_workflow, phase):
    checkout, _, run = documented_workflow
    result, records = run(SGRM_DOC_FAIL=f"{phase}:bicg")
    assert result.returncode == 7
    assert records[-1][1] == ("trace_generation/run_hls.py" if phase == "synthesis"
                              else "trace_generation/generate_trace.py")
    assert not any(args[0].startswith("sgrm-") for args in records)
    assert not (checkout / WORKFLOW / "traces/manifest.json").exists()


def test_readme_rejects_29_traces_instead_of_searching_a_subset(documented_workflow):
    checkout, _, run = documented_workflow
    result, records = run(SGRM_DOC_OMIT_TRACE="bicg")
    assert result.returncode != 0
    assert "listed designs have no trace" in result.stderr
    assert records[-1][0] == "sgrm-build-manifest"
    assert not (checkout / WORKFLOW / "traces/manifest.json").exists()
    assert not any(args[0] in ("sgrm-trace-batch", "sgrm-verify-results") for args in records)


def test_readme_never_overwrites_an_existing_full_run(documented_workflow):
    checkout, _, run = documented_workflow
    previous = checkout / WORKFLOW
    previous.mkdir(parents=True)
    marker = previous / "keep.txt"
    marker.write_text("previous result", encoding="utf-8")
    result, records = run()
    assert result.returncode == 1 and records == []
    assert "existing runs are not overwritten" in result.stderr
    assert marker.read_text() == "previous result"
