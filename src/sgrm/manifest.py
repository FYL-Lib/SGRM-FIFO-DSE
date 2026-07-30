"""Portable manifest loading for pre-generated trace bundles."""

from __future__ import annotations

import json
import re
from pathlib import Path


_DESIGN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


class ManifestError(ValueError):
    """Raised when a trace-bundle manifest is malformed."""


def load_manifest(path: str | Path) -> tuple[Path, dict]:
    """Load and validate a version-1 trace manifest."""

    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not load manifest {manifest_path}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError("manifest schema_version must be 1")
    designs = payload.get("designs")
    if not isinstance(designs, list) or not designs:
        raise ManifestError("manifest designs must be a non-empty list")

    seen: set[str] = set()
    for index, entry in enumerate(designs):
        label = f"designs[{index}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{label} must be an object")
        design = entry.get("design")
        if not isinstance(design, str) or _DESIGN_NAME.fullmatch(design) is None:
            raise ManifestError(f"{label}.design is not a portable design name")
        if design in seen:
            raise ManifestError(f"duplicate design in manifest: {design}")
        seen.add(design)

        solution_dir = entry.get("solution_dir")
        if not isinstance(solution_dir, str) or not solution_dir:
            raise ManifestError(f"{label}.solution_dir must be a relative path")
        relative_path = Path(solution_dir)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ManifestError(f"{label}.solution_dir must stay inside the bundle")

        digest = entry.get("trace_sha256")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ManifestError(f"{label}.trace_sha256 must be 64 hexadecimal digits")
        expected = entry.get("expected")
        if expected is not None and not isinstance(expected, dict):
            raise ManifestError(f"{label}.expected must be an object")

    return manifest_path, payload


def resolve_solution_dir(manifest_path: Path, entry: dict) -> Path:
    """Resolve one validated relative solution directory."""

    bundle_root = manifest_path.parent.resolve()
    resolved = (bundle_root / entry["solution_dir"]).resolve()
    try:
        resolved.relative_to(bundle_root)
    except ValueError as exc:
        raise ManifestError(
            f"solution directory escapes the trace bundle: {entry['solution_dir']}"
        ) from exc
    return resolved
