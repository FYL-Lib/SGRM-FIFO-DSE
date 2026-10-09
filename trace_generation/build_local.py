#!/usr/bin/env python3
"""Build the included local LightningSim backend and trace runtime.

The pinned upstream Conda dependency supplies the unchanged LLVM instrumenter,
SystemC, and web assets. Python/Rust backend and FIFO trace-runtime code come
from this repository, not an installed private binary or another checkout.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FORK = ROOT / "third_party/lightningsim"


def build_environment() -> tuple[Path, dict[str, str]]:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise ValueError("The pinned trace-generation toolchain supports Linux x86-64.")
    prefix = Path(sys.prefix).resolve()
    if sys.version_info[:2] != (3, 12) or not (prefix / "conda-meta").is_dir():
        raise ValueError("Use Python 3.12 in the dedicated Conda trace-generation environment.")
    if prefix in {ROOT / ".conda-sgrm", ROOT / ".conda-sgrm-clean"}:
        raise ValueError("Keep the search environment unchanged; create .conda-sgrm-trace instead.")
    if os.environ.get("CONDA_PREFIX") and Path(os.environ["CONDA_PREFIX"]).resolve() != prefix:
        raise ValueError("The active Conda environment does not match this Python executable.")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("LD_LIBRARY_PATH", None)
    environment["CONDA_PREFIX"] = str(prefix)
    environment["PATH"] = os.pathsep.join([str(prefix / "bin"), environment.get("PATH", "")])
    for variable, name in (("CC", "gcc"), ("CXX", "c++"), ("AR", "ar")):
        compiler = prefix / "bin" / f"x86_64-conda-linux-gnu-{name}"
        if not compiler.is_file():
            raise FileNotFoundError(f"Compiler tool missing: {compiler}; recreate the generation environment.")
        environment[variable] = str(compiler)
    return prefix, environment


def run(command: list[str], environment: dict[str, str], *, cwd: Path | None = None) -> None:
    print("RUN " + " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def check_installation(prefix: Path, environment: dict[str, str]) -> dict:
    backend = FORK / "backend"
    code = (
        "import json, lightningsim, lightningsim._core; "
        "from pathlib import Path; "
        f"root=Path({str(backend)!r}).resolve(); "
        "module=Path(lightningsim.__file__).resolve(); "
        "core=Path(lightningsim._core.__file__).resolve(); "
        "assert module.is_relative_to(root), 'Not importing the included local LightningSim'; "
        "assert core.is_relative_to(root), 'Not using the locally built Rust core'; "
        "print('SGRM_BUILD_CHECK ' + json.dumps({'python_package':str(module),'rust_extension':str(core)}))"
    )
    checked = subprocess.run([sys.executable, "-c", code], env=environment, check=True,
                             text=True, stdout=subprocess.PIPE)
    lines = [line.removeprefix("SGRM_BUILD_CHECK ") for line in checked.stdout.splitlines()
             if line.startswith("SGRM_BUILD_CHECK ")]
    if len(lines) != 1:
        raise RuntimeError("The local LightningSim import check did not return its build record.")
    record = json.loads(lines[0])
    library = prefix / "share/lightningsim/templates/liblightningsimrt.a"
    nm = shutil.which("x86_64-conda-linux-gnu-nm", path=environment["PATH"])
    if nm is None:
        raise FileNotFoundError("The Conda binutils nm executable is missing.")
    symbols = subprocess.run([nm, "--defined-only", str(library)], env=environment,
                             check=True, text=True, stdout=subprocess.PIPE).stdout
    for symbol in ("_ssdm_op_FMADD", "_ssdm_op_FMADD_d"):
        if not any(line.split()[-1:] == [symbol] for line in symbols.splitlines()):
            raise RuntimeError(f"The built trace runtime is missing {symbol}.")
    record.update(runtime_library=str(library), upstream_version="0.2.6",
                  local_source_directory=str(FORK), python=sys.version.split()[0])
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 16:
        parser.error("--jobs must be between 1 and 16")
    prefix, environment = build_environment()
    runtime = prefix / "share/lightningsim"
    required = [runtime / "llvm/bin/opt", runtime / "llvm/lib/LLVMHLSLiteSim.so",
                runtime / "systemc/lib-linux64/libsystemc.a"]
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f"Upstream runtime component missing: {path}; recreate the environment.")
    if not args.check_only:
        environment["CARGO_BUILD_JOBS"] = str(args.jobs)
        environment["CARGO_TARGET_DIR"] = str(prefix / "var/cache/sgrm-lightningsim")
        # Build in a private temporary directory, never in an old experiment
        # tree or an extracted shared Conda package cache.
        with tempfile.TemporaryDirectory(prefix="sgrm-lightningsim-build-") as temporary:
            directory = Path(temporary)
            shutil.copy2(FORK / "Makefile", directory / "Makefile")
            shutil.copytree(FORK / "src", directory / "src",
                            ignore=shutil.ignore_patterns("*.o", "*.a", "__pycache__"))
            (directory / "templates").mkdir()
            run(["make", f"-j{args.jobs}", f"CXX={environment['CXX']}",
                 f"AR={environment['AR']}"], environment, cwd=directory)
            destination = runtime / "templates"
            destination.mkdir(exist_ok=True)
            for source in (directory / "templates").iterdir():
                # Atomic replacement breaks Conda hard links instead of editing
                # a shared package-cache file in place.
                temporary_target = destination / (source.name + ".sgrm-new")
                shutil.copy2(source, temporary_target)
                temporary_target.replace(destination / source.name)
        run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation",
             "--editable", str(FORK / "backend")], environment)
    record = check_installation(prefix, environment)
    (runtime / "sgrm-build.json").write_text(json.dumps(record, indent=2) + "\n")
    print("LOCAL LIGHTNINGSIM BUILD PASS", flush=True)
    print(f"Python/Rust source: {FORK / 'backend'}", flush=True)
    print(f"Trace runtime: {record['runtime_library']}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
