#!/usr/bin/env python3
"""Verify the supplied data bundles without extracting or loading them."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
RECORDS_FILE = "datasets/bundle_checksums.txt"


def read_verification_records(path: Path) -> list[tuple[str, str]]:
    records = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]) is None:
            raise ValueError(f"invalid verification record on line {line_number}")
        expected, filename = fields
        filename = filename.lstrip("*")
        relative = Path(filename)
        if not filename or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid bundle path on line {line_number}")
        if filename in seen:
            raise ValueError(f"duplicate bundle record on line {line_number}")
        seen.add(filename)
        records.append((filename, expected.lower()))
    if not records:
        raise ValueError("no bundle verification records found")
    return records


def file_checksum(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def verify_bundles(repository: Path) -> int:
    repository = repository.resolve()
    try:
        records = read_verification_records(repository / RECORDS_FILE)
    except (OSError, ValueError) as exc:
        print(f"FAIL: could not read bundle verification records: {exc}", file=sys.stderr)
        return 1

    failures = 0
    for filename, expected in records:
        path = (repository / filename).resolve()
        if not path.is_relative_to(repository):
            print(f"FAIL {filename}: bundle path leaves the repository", file=sys.stderr)
            failures += 1
            continue
        try:
            actual = file_checksum(path)
        except OSError:
            print(f"FAIL {filename}: file is missing or cannot be read", file=sys.stderr)
            failures += 1
            continue
        if actual != expected:
            print(f"FAIL {filename}: file integrity check failed", file=sys.stderr)
            failures += 1
        else:
            print(f"PASS {filename}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return verify_bundles(REPOSITORY)


if __name__ == "__main__":
    raise SystemExit(main())
