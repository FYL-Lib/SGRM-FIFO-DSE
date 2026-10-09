#!/usr/bin/env python3
"""Derive a source mapping for newly generated traces, checking every FIFO."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import pickle
import shutil

if __package__:
    from .tooling import hardware_module
else:
    from tooling import hardware_module


def checked_trace(path: Path, expected: str):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f"Trace integrity check failed: {path}")
    # Pickle is intentionally loaded only after the trusted manifest's file
    # integrity check. This check does not make an untrusted pickle safe.
    with path.open("rb") as stream:
        return pickle.load(stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    from sgrm.manifest import load_manifest, resolve_solution_dir

    source_root = args.sources_dir.resolve()
    output = args.output_dir.resolve()
    if output.exists() or output.is_relative_to(source_root):
        raise ValueError("Use a new output directory outside the original source tree.")
    tools = hardware_module()
    original = tools.load_source_manifest(source_root)
    derived = copy.deepcopy(original)
    source_designs = {entry["design"]: entry for entry in derived["designs"]}
    manifest_path, manifest = load_manifest(args.manifest)
    rebound = []
    for entry in manifest["designs"]:
        name = entry["design"]
        if name not in source_designs:
            raise ValueError(f"No original source contract for {name}")
        source = source_designs[name]
        solution = resolve_solution_dir(manifest_path, entry)
        trace = checked_trace(solution / "trace.pkl", entry["trace_sha256"])
        actual = {int(fifo.id): (str(fifo.name), int(fifo.width), trace.params.fifo_depths[fifo.id])
                  for fifo in trace.fifos}
        expected = {fifo["id"]: (fifo["name"], fifo["width"], fifo["trace_baseline_depth"])
                    for fifo in source["fifos"]}
        if len(actual) != len(trace.fifos) or len(expected) != len(source["fifos"]):
            raise ValueError(f"{name}: duplicate FIFO IDs in the regenerated or original contract")
        if actual != expected:
            raise ValueError(f"{name}: regenerated FIFO ID/name/width/depth contract differs from the source bundle")
        source["trace_sha256"] = entry["trace_sha256"]
        rebound.append(name)
    # No original file or checksum is edited. Copy only the archived files,
    # not generated HLS caches, then write the separate derived mapping.
    output.mkdir(parents=True)
    for design in original["designs"]:
        src = source_root / design["directory"]
        dst = output / design["directory"]
        for relative in design["files"]:
            target = dst / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / relative, target)
    for name in ("PROVENANCE.txt", "STREAM_HLS_LICENSE"):
        if (source_root / name).is_file():
            shutil.copy2(source_root / name, output / name)
    shutil.copy2(source_root / "manifest.json", output / "manifest.original.json")
    derived["trace_regeneration"] = {"regenerated_designs": rebound,
                                     "fifo_contract_checked": True,
                                     "original_source_files_changed": False}
    (output / "manifest.json").write_text(json.dumps(derived, indent=2, sort_keys=True) + "\n")
    print(f"BOUND {len(rebound)} regenerated trace/source mappings: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"ERROR: {exc}")
