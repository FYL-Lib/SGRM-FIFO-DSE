"""Reuse SGRM's portable AMD discovery without requiring Vivado."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def hardware_module():
    spec = importlib.util.spec_from_file_location(
        "sgrm_trace_hardware_tools", ROOT / "hardware_validation/validate_hardware.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_environment(root: Path, environment: dict[str, str]) -> dict[str, str]:
    root = root.expanduser().resolve()
    required = [root / "include/Makefile.sysc.rules", root / "include/hls_stream.h"]
    if any(not path.is_file() for path in required):
        raise RuntimeError("The selected HLS runtime lacks include/Makefile.sysc.rules or include/hls_stream.h.")
    environment = environment.copy()
    environment["XILINX_HLS"] = str(root)
    fp_candidates = [root / "lnx64/tools/fpo_v7_1",
                     root.parent.parent / "Vitis/2024.2/lnx64/tools/fpo_v7_1"]
    vitis = environment.get("XILINX_VITIS")
    if vitis and Path(vitis).name == "2024.2":
        fp_candidates.append(Path(vitis) / "lnx64/tools/fpo_v7_1")
    fp_dirs = list(dict.fromkeys(str(path) for path in fp_candidates if path.is_dir()))
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        [*fp_dirs, *([environment["LD_LIBRARY_PATH"]] if environment.get("LD_LIBRARY_PATH") else [])]
    )
    return environment


def explicit_runtime(root: Path, settings: list[Path], base: dict[str, str]) -> dict[str, str]:
    """Use AMD headers/libraries for completed synthesis without invoking HLS."""
    root = root.expanduser().resolve()
    automatic = root / "settings64.sh"
    scripts = list(dict.fromkeys([*([automatic] if automatic.is_file() else []), *settings]))
    environment = hardware_module().load_amd_environment(scripts, base)
    return runtime_environment(root, environment)


def find_hls(value: str | None, roots: list[Path] | None,
             settings: list[Path], base: dict[str, str]) -> tuple[str, dict[str, str]]:
    tools = hardware_module()
    saved = tools.read_tool_config(tools.DEFAULT_TOOL_CONFIG)
    candidates, explicit = tools.discover_tool("hls", value, saved, roots)
    errors = []
    for executable, origin in candidates:
        try:
            scripts = tools.installation_settings({"hls": executable})
            if saved.get("vitis_hls") == executable:
                scripts.extend(Path(path) for path in saved.get("settings_files", []))
            scripts = list(dict.fromkeys([*scripts, *settings]))
            environment = tools.load_amd_environment(scripts, base)
            environment = tools.hardware_environment({"hls": executable}, environment)
            tools.probe_tool(executable, "hls", environment)
            root = Path(environment.get("XILINX_HLS", ""))
            # Some AMD installations keep the classic launcher in Vitis_HLS
            # but its 2024.2 headers/runtime in the sibling Vitis directory.
            runtime_roots = [root, root.parent.parent / "Vitis" / tools.TOOL_VERSION]
            vitis = environment.get("XILINX_VITIS")
            if vitis and Path(vitis).name == tools.TOOL_VERSION:
                runtime_roots.append(Path(vitis))
            root = next((path for path in runtime_roots
                         if (path / "include/Makefile.sysc.rules").is_file()), None)
            if root is None:
                raise RuntimeError("The selected HLS environment lacks include/Makefile.sysc.rules.")
            return executable, runtime_environment(root, environment)
        except (OSError, RuntimeError) as exc:
            errors.append(f"{origin}: {exc}")
            if explicit:
                break
    detail = "\n- ".join(errors[:5]) or "No executable was discovered."
    raise RuntimeError("No compatible Vitis HLS 2024.2 launcher/runtime was discovered.\n- " + detail
                       + "\nSupply --vitis-hls with its classic executable path or --tool-root with the installation root."
                       + "\nFor completed synthesis, --hls-root can instead select the 2024.2 directory containing include/Makefile.sysc.rules.")
