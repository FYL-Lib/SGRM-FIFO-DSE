#!/usr/bin/env python3
"""Apply SGRM search results and measure paired Stream-HLS hardware resources.

Uses Python's standard library. Vitis HLS/Vivado 2024.2 are needed only for
--stage run or all. --stage prepare performs all input and rewriting checks
without invoking either hardware tool.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ARCHIVE = "sgrm-stream-hls-30-sources-v0.1.0.tar.xz"
SCHEMA_VERSION = 1
IMPLEMENTATIONS = {"srl", "lutram", "bram", "uram"}
DECLARATION = re.compile(r"^(?P<indent>\s*)hls::stream<[^>]+>\s+(?P<name>[A-Za-z_]\w*)\b.*;\s*$")
STREAM_PRAGMA = re.compile(
    r"^\s*#pragma\s+HLS\s+STREAM\s+variable=([A-Za-z_]\w*)\s+depth=.+?\s*$", re.IGNORECASE
)
STORAGE_PRAGMA = re.compile(
    r"^\s*#pragma\s+HLS\s+bind_storage\s+variable=([A-Za-z_]\w*)\s+type=fifo\s+impl=\w+\s*$",
    re.IGNORECASE,
)
FIFO_MODULE = re.compile(r"_fifo_w\d+_d\d+(?:_[A-Z])?(?:_\d+)?$")
AUTO_DEPTH = re.compile(r"depth is automatically increased", re.IGNORECASE)
RESOURCE_CAPACITIES = {"bram": 967, "uram": 463, "ff": 1799680, "lut": 899840}


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checked_path(directory: Path, relative: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError(f"expected a relative path: {relative!r}")
    target = (directory / relative).resolve()
    if not target.is_relative_to(directory.resolve()):
        raise ValueError(f"path escapes the source directory: {relative}")
    return target


def extract_source_archive(destination: Path) -> Path:
    archive_path = REPOSITORY / "datasets" / SOURCE_ARCHIVE
    if not archive_path.is_file():
        raise FileNotFoundError(f"source archive not found: {archive_path}")
    entries = {}
    for line in (REPOSITORY / "datasets/SHA256SUMS").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            digest, name = line.split(maxsplit=1)
            entries[name.lstrip("*")] = digest
    expected = entries.get("datasets/" + SOURCE_ARCHIVE)
    if expected is None or sha256(archive_path) != expected:
        raise ValueError("source archive checksum does not match datasets/SHA256SUMS")
    root = destination / SOURCE_ARCHIVE.removesuffix(".tar.xz")
    if root.is_dir():
        return root
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:xz") as archive:
        for member in archive.getmembers():
            target = checked_path(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            else:
                raise ValueError(f"archive links or special files are unsupported: {member.name}")
    return root


def load_source_manifest(source_root: Path) -> dict:
    manifest = read_json(source_root / "manifest.json")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("corpus") != "Stream-HLS30":
        raise ValueError("unsupported source manifest")
    designs = manifest.get("designs", [])
    names = [item["design"] for item in designs]
    if len(names) != 30 or len(set(names)) != 30 or manifest.get("design_count") != 30:
        raise ValueError("the source manifest must contain exactly 30 unique designs")
    published = [
        line.split("#", 1)[0].strip()
        for line in (REPOSITORY / "datasets/stream_hls_30.txt").read_text().splitlines()
        if line.split("#", 1)[0].strip()
    ]
    if len(published) != 30 or set(names) != set(published):
        raise ValueError("source manifest differs from datasets/stream_hls_30.txt")
    if manifest.get("capacities") != RESOURCE_CAPACITIES:
        raise ValueError("unexpected resource-normalization capacities")
    if manifest.get("part") != "xcvc1902-vsva2197-2MP-e-S" or manifest.get("clock_period_ns") != 10.0:
        raise ValueError("this validation protocol targets VCK190 at 10 ns")
    for design in designs:
        directory = checked_path(source_root, design["directory"])
        for relative, record in design["files"].items():
            path = checked_path(directory, relative)
            if not path.is_file() or path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
                raise ValueError(f"original benchmark checksum mismatch: {design['design']}/{relative}")
        fifo_ids = [item["id"] for item in design["fifos"]]
        if len(set(fifo_ids)) != len(fifo_ids) or len(fifo_ids) != design["fifo_count"]:
            raise ValueError(f"{design['design']}: invalid FIFO-ID contract")
    return manifest


def read_search_result(path: Path, design: dict) -> tuple[dict, dict[str, tuple[int, str]]]:
    result = read_json(path)
    name = design["design"]
    if result.get("schema_version") != 1 or result.get("design") != name:
        raise ValueError(f"{path}: unexpected schema or design identity")
    if result.get("trace_sha256") != design["trace_sha256"]:
        raise ValueError(f"{name}: search result uses a different trace")
    if result.get("fifo_count") != design["fifo_count"]:
        raise ValueError(f"{name}: search result FIFO count differs from the source contract")
    selected, baseline = result.get("selected", {}), result.get("baseline", {})
    for label, point in (("selected", selected), ("baseline", baseline)):
        if not isinstance(point, dict) or point.get("deadlock") is not False:
            raise ValueError(f"{name}: {label} search result is not deadlock-free")
        latency = point.get("latency")
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency <= 0:
            raise ValueError(f"{name}: {label} has no valid trace latency")
    epsilon = result.get("epsilon", 0)
    if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)) or not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError(f"{name}: invalid epsilon")
    if selected["latency"] > baseline["latency"] * (1 + epsilon):
        raise ValueError(f"{name}: selected search result violates its trace latency limit")
    fifo_ids = {str(item["id"]) for item in design["fifos"]}
    depths = selected.get("fifo_depths")
    implementations = selected.get("fifo_impl_types")
    if not isinstance(depths, dict) or set(depths) != fifo_ids:
        raise ValueError(f"{name}: selected FIFO depths do not cover the source contract exactly")
    if not isinstance(implementations, dict) or set(implementations) != fifo_ids:
        raise ValueError(f"{name}: explicit implementations are required for every selected FIFO")
    configs = {}
    for fifo in design["fifos"]:
        fifo_id = str(fifo["id"])
        depth, impl = depths[fifo_id], implementations[fifo_id]
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
            raise ValueError(f"{name}: FIFO {fifo_id} has an invalid depth")
        if not isinstance(impl, str) or impl not in IMPLEMENTATIONS:
            raise ValueError(f"{name}: FIFO {fifo_id} must use srl/lutram/bram/uram")
        key = fifo["source_variable"]
        if key in configs and configs[key] != (depth, impl):
            raise ValueError(
                f"{name}: the elements of stream declaration {key} have different choices; "
                "a single declaration cannot express this result exactly"
            )
        configs[key] = (depth, impl)
    return result, configs


def rewrite_source(text: str, configs: dict[str, tuple[int, str]]) -> str:
    """Apply one uniform depth/type per source declaration; reject missing ones."""
    lines, output, seen = text.splitlines(), [], set()
    index = 0
    while index < len(lines):
        match = DECLARATION.match(lines[index])
        if not match or match["name"] not in configs:
            output.append(lines[index])
            index += 1
            continue
        name, indent = match["name"], match["indent"]
        if name in seen:
            raise ValueError(f"ambiguous repeated stream declaration: {name}")
        seen.add(name)
        depth, impl = configs[name]
        output.append(lines[index])
        following = index + 1
        while following < len(lines):
            stream = STREAM_PRAGMA.match(lines[following])
            storage = STORAGE_PRAGMA.match(lines[following])
            if (stream and stream[1] == name) or (storage and storage[1] == name):
                following += 1
                continue
            break
        output.extend([
            f"{indent}#pragma HLS STREAM variable={name} depth={depth}",
            f"{indent}#pragma HLS bind_storage variable={name} type=fifo impl={impl}",
        ])
        index = following
    missing = set(configs) - seen
    if missing:
        raise ValueError(f"FIFO declarations missing from source: {sorted(missing)}")
    return "\n".join(output) + "\n"


def tcl_path(path: Path) -> str:
    text = str(path.resolve())
    if any(char in text for char in "{}\n\r"):
        raise ValueError(f"unsupported characters in Tcl path: {path}")
    return "{" + text + "}"


def build_hls_tcl(run_dir: Path, design: dict, manifest: dict) -> str:
    source = run_dir / design["kernel"]
    testbench = run_dir / design["testbench"]
    protocol = manifest["hardware_protocol"]
    lines = [
        "open_project -reset " + tcl_path(run_dir / "hls_proj"),
        "set_top " + design["top"],
        "add_files " + tcl_path(source),
        "add_files -tb " + tcl_path(testbench),
        'open_solution "solution1" -flow_target vivado',
        "set_part {" + manifest["part"] + "}",
        "create_clock -name ap_clk -period 10.0",
    ]
    if protocol.get("unsafe_math_optimizations"):
        lines.append("config_compile -unsafe_math_optimizations")
    if protocol.get("disable_block_condition_registers"):
        lines.append("config_rtl -add_register_in_block_condition=false")
    lines.extend(["csynth_design", "exit"])
    return "\n".join(lines) + "\n"


def prepare_jobs(source_root: Path, manifest: dict, results_dir: Path, output: Path, names: list[str]) -> dict:
    designs = {item["design"]: item for item in manifest["designs"]}
    results, choices = {}, {}
    # Validate every requested result before creating or altering any workdir.
    for name in names:
        results[name], choices[name] = read_search_result(results_dir / f"{name}.json", designs[name])
    jobs = []
    writer_hash = sha256(Path(__file__))
    for name in names:
        design = designs[name]
        original = checked_path(source_root, design["directory"])
        for variant in ("native", "sgrm"):
            config = choices[name] if variant == "sgrm" else {}
            fingerprint = object_hash({
                "schema_version": SCHEMA_VERSION,
                "writer_sha256": writer_hash,
                "design": design,
                "hardware_protocol": manifest["hardware_protocol"],
                "variant": variant,
                "choices": config,
            })
            # Keep user-facing paths readable; the fingerprint guards reuse only.
            run_dir = output / "work" / name / variant
            record_path = run_dir / "configuration.json"
            if record_path.is_file() and read_json(record_path).get("fingerprint") != fingerprint:
                raise ValueError(f"incompatible existing workdir; choose a fresh --output-dir: {run_dir}")
            run_dir.mkdir(parents=True, exist_ok=True)
            for relative in design["files"]:
                source = checked_path(original, relative)
                target = checked_path(run_dir, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            if variant == "sgrm":
                kernel = run_dir / design["kernel"]
                kernel.write_text(rewrite_source(kernel.read_text(), config), encoding="utf-8")
            tcl = run_dir / "run_hls.tcl"
            tcl.write_text(build_hls_tcl(run_dir, design, manifest), encoding="utf-8")
            vivado_tcl = run_dir / "run_vivado.tcl"
            vivado_tcl.write_text(
                "open_project " + tcl_path(run_dir / "hls_proj")
                + "\nopen_solution solution1\nexport_design -flow syn -format ip_catalog\nexit\n",
                encoding="utf-8",
            )
            staged_files = {
                relative: sha256(checked_path(run_dir, relative)) for relative in design["files"]
            }
            staged_files.update({path.name: sha256(path) for path in (tcl, vivado_tcl)})
            job = {
                "design": name,
                "variant": variant,
                "fingerprint": fingerprint,
                "run_dir": str(run_dir),
                "top": design["top"],
                "logical_fifo_count": design["fifo_count"],
                "source_files_sha256": staged_files,
                "requested_choices_by_source_variable": {
                    key: {"depth": depth, "implementation": impl}
                    for key, (depth, impl) in sorted(config.items())
                },
                "search_result_path": str((results_dir / f"{name}.json").resolve()),
                "search_result_sha256": sha256(results_dir / f"{name}.json"),
                "search_trace_sha256": results[name]["trace_sha256"],
                "search_cli_wall_s": results[name].get("wall_time_s"),
                "trace_baseline_latency_cycles": results[name]["baseline"]["latency"],
                "trace_selected_latency_cycles": results[name]["selected"]["latency"],
            }
            write_json(record_path, job)
            jobs.append(job)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "design_count": len(names),
        "job_count": len(jobs),
        "designs": names,
        "part": manifest["part"],
        "clock_period_ns": manifest["clock_period_ns"],
        "capacities": RESOURCE_CAPACITIES,
        "hardware_protocol": manifest["hardware_protocol"],
        "source_manifest_sha256": sha256(source_root / "manifest.json"),
        "jobs": jobs,
    }
    write_json(output / "plan.json", plan)
    print(f"PREPARED {len(names)} paired designs ({len(jobs)} hardware jobs): {output / 'plan.json'}", flush=True)
    return plan


def parse_hierarchy(path: Path) -> dict:
    """Sum inclusive outermost FIFO cells and skip parenthesized self rows."""
    resources = {
        "bram": 0.0, "uram": 0, "ff": 0, "lut": 0, "lutram": 0, "srl": 0, "n_fifo": 0
    }
    stack = []
    columns = None
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        raw_cells = line.split("|")[1:-1]
        cells = [cell.strip() for cell in raw_cells]
        if len(cells) < 2:
            continue
        if "Instance" in cells and "Module" in cells and "Total LUTs" in cells:
            columns = {label: index for index, label in enumerate(cells)}
            required = {"Instance", "Module", "Total LUTs", "FFs", "RAMB36", "RAMB18", "URAM"}
            if not required.issubset(columns):
                raise ValueError(f"unsupported hierarchical report columns: {path}")
            continue
        if columns is None or len(cells) <= max(columns.values()):
            continue
        instance = cells[columns["Instance"]]
        if instance.startswith("(") and instance.endswith(")"):
            continue
        indentation = len(raw_cells[columns["Instance"]]) - len(raw_cells[columns["Instance"]].lstrip())
        while stack and stack[-1][0] >= indentation:
            stack.pop()
        is_fifo = bool(FIFO_MODULE.search(cells[columns["Module"]]))
        covered_by_parent_fifo = any(item[1] for item in stack)
        stack.append((indentation, is_fifo))
        if not is_fifo or covered_by_parent_fifo:
            continue
        def number(label: str) -> int:
            if label not in columns:
                return 0
            value = cells[columns[label]].replace(",", "")
            if not re.fullmatch(r"\d+", value):
                raise ValueError(f"invalid FIFO resource value for {label}: {path}")
            return int(value)
        resources["lut"] += number("Total LUTs")
        resources["ff"] += number("FFs")
        resources["uram"] += number("URAM")
        resources["bram"] += number("RAMB36") + number("RAMB18") / 2.0
        resources["lutram"] += number("LUTRAMs")
        resources["srl"] += number("SRLs")
        resources["n_fifo"] += 1
    if columns is None or resources["n_fifo"] == 0:
        raise ValueError(f"no FIFO subsystem rows found in hierarchical report: {path}")
    resources["cutil"] = sum(
        resources[resource] / RESOURCE_CAPACITIES[resource] for resource in RESOURCE_CAPACITIES
    ) / 4.0
    return resources


def parse_export(path: Path) -> dict:
    text = path.read_text(errors="replace")
    resources = {key.lower(): int(value) for key, value in re.findall(r"^([A-Z]+):\s+(\d+)\s*$", text, re.MULTILINE)}
    if not {"lut", "ff", "bram", "uram"}.issubset(resources):
        raise ValueError(f"incomplete post-synthesis resource report: {path}")
    clock = re.search(r"CP achieved post-synthesis:\s*([0-9.]+)", text)
    target = re.search(r"CP required:\s*([0-9.]+)", text)
    if not clock or not target:
        clock = re.search(r"^\|\s*Post-Synthesis\s*\|\s*([0-9.]+)\s*\|", text, re.MULTILINE)
        target = re.search(r"^\|\s*Target\s*\|\s*([0-9.]+)\s*\|", text, re.MULTILINE)
    if not clock or not target:
        raise ValueError(f"missing post-synthesis timing fields: {path}")
    return {
        "resources": resources,
        "clock_period_ns": float(clock[1]),
        "required_clock_period_ns": float(target[1]),
        "timing_met": float(clock[1]) <= float(target[1]),
    }


def report_paths(job: dict) -> tuple[Path, Path, Path]:
    solution = Path(job["run_dir"]) / "hls_proj/solution1"
    top = job["top"]
    export = solution / "impl/report/verilog/export_syn.rpt"
    legacy_export = solution / "impl/report/verilog" / f"{top}_export.rpt"
    if not export.is_file() and legacy_export.is_file():
        export = legacy_export
    return (
        solution / "syn/report" / f"{top}_csynth.xml",
        export,
        solution / "impl/verilog/report" / f"{top}_utilization_hierarchical_synth.rpt",
    )


def parse_hls_latency(path: Path) -> int | None:
    root = ET.parse(path).getroot()
    value = root.findtext(".//SummaryOfOverallLatency/Worst-caseLatency")
    return int(value) if value is not None and value.isdecimal() else None


def command(tool: str, tcl: Path, run_dir: Path, log: Path) -> float:
    started = time.perf_counter()
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.run(
            [tool, "-f", str(tcl)],
            cwd=run_dir,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
    elapsed = time.perf_counter() - started
    if process.returncode:
        raise RuntimeError(f"tool exited with {process.returncode}; see {log}")
    return elapsed


def run_job(job: dict, tool: str, tool_version: str) -> dict:
    run_dir = Path(job["run_dir"])
    for relative, digest in job["source_files_sha256"].items():
        if sha256(checked_path(run_dir, relative)) != digest:
            raise ValueError(f"staged input changed after preparation: {run_dir / relative}")
    state_path = run_dir / "status.json"
    state = read_json(state_path) if state_path.is_file() else {}
    if state and state.get("fingerprint") != job["fingerprint"]:
        raise ValueError(f"incompatible cached tool run: {run_dir}")
    if state.get("status") == "OK":
        for path, digest in state["report_sha256"].items():
            if not Path(path).is_file() or sha256(Path(path)) != digest:
                raise ValueError(f"cached report changed: {path}")
        print(f"CACHED {job['design']}/{job['variant']}", flush=True)
        return state
    state.update({
        "schema_version": SCHEMA_VERSION,
        "fingerprint": job["fingerprint"],
        "design": job["design"],
        "variant": job["variant"],
        "status": "RUNNING",
        "tool": tool,
        "tool_version": tool_version,
    })
    write_json(state_path, state)
    try:
        hls_xml, export, hierarchy = report_paths(job)
        if not state.get("hls_complete"):
            print(f"HLS {job['design']}/{job['variant']}", flush=True)
            state["hls_wall_s"] = command(tool, run_dir / "run_hls.tcl", run_dir, run_dir / "hls.log")
            if not hls_xml.is_file():
                raise RuntimeError(f"HLS did not generate {hls_xml}")
            depth_messages = [
                line for line in (run_dir / "hls.log").read_text(errors="replace").splitlines()
                if AUTO_DEPTH.search(line)
            ]
            state["auto_depth_messages"] = depth_messages
            if job["variant"] == "sgrm" and depth_messages:
                raise RuntimeError("HLS increased a selected FIFO depth; inspect hls.log")
            state["hls_complete"] = True
            write_json(state_path, state)
        print(f"VIVADO {job['design']}/{job['variant']}", flush=True)
        state["vivado_wall_s"] = command(tool, run_dir / "run_vivado.tcl", run_dir, run_dir / "vivado_export.log")
        hls_xml, export, hierarchy = report_paths(job)
        state["fifo_subsystem"] = parse_hierarchy(hierarchy)
        state["whole_design"] = parse_export(export)
        state["hls_latency_max_cycles"] = parse_hls_latency(hls_xml)
        state["report_sha256"] = {str(path): sha256(path) for path in (hls_xml, export, hierarchy)}
        state["status"] = "OK"
        state.pop("error", None)
        write_json(state_path, state)
        print(f"PASS {job['design']}/{job['variant']}", flush=True)
    except Exception as exc:
        state.update({"status": "FAILED", "error": str(exc)})
        write_json(state_path, state)
        print(f"FAIL {job['design']}/{job['variant']}: {exc}", flush=True)
    return state


def geometric_mean(values: list[float]) -> float | None:
    if not values:
        return None
    if any(value < 0 or not math.isfinite(value) for value in values):
        raise ValueError("geometric mean requires finite nonnegative values")
    if any(value == 0 for value in values):
        return 0.0
    return math.exp(math.fsum(math.log(value) for value in values) / len(values))


def collect_reports(plan: dict, output: Path) -> dict:
    rows, paired = [], {}
    for job in plan["jobs"]:
        state_path = Path(job["run_dir"]) / "status.json"
        state = read_json(state_path) if state_path.is_file() else {"status": "NOT_RUN"}
        row = {"design": job["design"], "variant": job["variant"], "status": state["status"]}
        if state.get("status") == "OK":
            if state.get("fingerprint") != job["fingerprint"]:
                raise ValueError(f"report is associated with a different input: {state_path}")
            for path, digest in state["report_sha256"].items():
                if not Path(path).is_file() or sha256(Path(path)) != digest:
                    raise ValueError(f"report checksum mismatch: {path}")
            for key, value in state["fifo_subsystem"].items():
                row["fifo_" + key] = value
            row.update({
                "hls_latency_max_cycles": state.get("hls_latency_max_cycles"),
                "post_synthesis_clock_ns": state["whole_design"]["clock_period_ns"],
                "timing_met": state["whole_design"]["timing_met"],
                "hls_wall_s": state.get("hls_wall_s"),
                "vivado_wall_s": state.get("vivado_wall_s"),
                "search_cli_wall_s": job.get("search_cli_wall_s"),
                "trace_baseline_latency_cycles": job["trace_baseline_latency_cycles"],
                "trace_selected_latency_cycles": job["trace_selected_latency_cycles"],
                "source_configuration": str(Path(job["run_dir"]) / "configuration.json"),
                "hierarchical_report": str(report_paths(job)[2]),
            })
            paired.setdefault(job["design"], {})[job["variant"]] = row
        row["error"] = state.get("error", "")
        rows.append(row)
    comparisons = []
    for name in plan["designs"]:
        pair = paired.get(name, {})
        if set(pair) != {"native", "sgrm"}:
            continue
        native, selected = pair["native"], pair["sgrm"]
        if native["fifo_cutil"] <= 0:
            raise ValueError(f"{name}: a positive native FIFO Cutil is required")
        ratio = selected["fifo_cutil"] / native["fifo_cutil"]
        comparisons.append({
            "design": name,
            "native_fifo_cutil": native["fifo_cutil"],
            "sgrm_fifo_cutil": selected["fifo_cutil"],
            "fifo_cutil_ratio": ratio,
            "fifo_subsystem_reduction_pct": 100 * (1 - ratio),
            "native_hls_latency_max_cycles": native.get("hls_latency_max_cycles"),
            "sgrm_hls_latency_max_cycles": selected.get("hls_latency_max_cycles"),
            "native_post_synthesis_clock_ns": native["post_synthesis_clock_ns"],
            "sgrm_post_synthesis_clock_ns": selected["post_synthesis_clock_ns"],
            "search_cli_wall_s": selected.get("search_cli_wall_s"),
        })
    for filename, records in (("hardware_resources.csv", rows), ("paired_comparison.csv", comparisons)):
        fields = list(dict.fromkeys(key for row in records for key in row))
        if not fields:
            fields = ["design", "status"]
        with (output / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
    complete = len(comparisons) == plan["design_count"]
    ratio = geometric_mean([row["fifo_cutil_ratio"] for row in comparisons])
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if complete else "INCOMPLETE",
        "requested_design_count": plan["design_count"],
        "completed_pair_count": len(comparisons),
        "requested_hardware_job_count": len(plan["jobs"]),
        "successful_hardware_job_count": sum(row["status"] == "OK" for row in rows),
        "geometric_mean_fifo_cutil_ratio": ratio,
        "geometric_mean_fifo_subsystem_reduction_pct": 100 * (1 - ratio) if ratio is not None else None,
        "geometric_mean_search_cli_wall_s": geometric_mean([
            row["search_cli_wall_s"] for row in comparisons
            if isinstance(row.get("search_cli_wall_s"), (int, float))
            and not isinstance(row.get("search_cli_wall_s"), bool)
            and row["search_cli_wall_s"] > 0
        ]),
        "resource_evidence": "Vivado RTL-synthesis hierarchical reports with recorded checksums",
        "capacities": RESOURCE_CAPACITIES,
        "bram_unit": "36-Kbit tiles (RAMB36 + RAMB18/2)",
        "latency_evidence": "HLS synthesis schedule estimates; not RTL co-simulation",
        "search_time_scope": "wall_time_s in supplied search JSON, including trace-backend initialization",
        "partial_aggregate": not complete,
    }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, help="directory containing the 30 search <design>.json files")
    parser.add_argument("--sources-dir", type=Path, help="extracted source-bundle root; by default verify and extract the supplied archive")
    parser.add_argument("--output-dir", type=Path, default=Path("results/hardware_validation"))
    parser.add_argument("--design", action="append", help="validate a named design; repeat for a subset")
    parser.add_argument("--stage", choices=("prepare", "run", "report", "all"), default="all")
    parser.add_argument("--jobs", type=int, default=2, help="maximum simultaneous hardware jobs")
    parser.add_argument("--vitis-hls", default=os.environ.get("VITIS_HLS", "vitis_hls"))
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    output = args.output_dir.expanduser().resolve()
    if output == REPOSITORY or not output.name:
        parser.error("choose a dedicated output directory")
    if args.stage in {"prepare", "all"}:
        if args.results_dir is None:
            parser.error("--results-dir is required for prepare/all")
        source_root = (
            args.sources_dir.expanduser().resolve()
            if args.sources_dir else extract_source_archive(output / "_sources")
        )
        manifest = load_source_manifest(source_root)
        corpus = [item["design"] for item in manifest["designs"]]
        names = args.design or corpus
        if len(set(names)) != len(names) or set(names) - set(corpus):
            parser.error("--design must contain distinct names from the published corpus")
        plan = prepare_jobs(source_root, manifest, args.results_dir.expanduser().resolve(), output, names)
        if args.stage == "prepare":
            return 0
    else:
        if args.design or args.results_dir or args.sources_dir:
            parser.error("run/report use the existing plan; choose the subset with --stage prepare")
        plan = read_json(output / "plan.json")
    if args.stage in {"run", "all"}:
        tool = shutil.which(args.vitis_hls)
        if tool is None:
            raise FileNotFoundError("vitis_hls not found; source the AMD 2024.2 settings or use --vitis-hls")
        version = subprocess.run(
            [tool, "-version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, check=False,
        ).stdout
        if not re.search(r"\bv?2024\.2\b", version):
            raise RuntimeError("this protocol requires Vitis HLS 2024.2")
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(run_job, job, tool, version.strip()) for job in plan["jobs"]]
            for future in as_completed(futures):
                future.result()
    summary = collect_reports(plan, output)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        raise SystemExit(f"ERROR: {exc}")
