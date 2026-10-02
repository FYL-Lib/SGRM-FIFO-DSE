# Hardware tool setup

The Python searches use supplied traces and do not need AMD tools. Preparing
original/selected source copies and collecting existing reports do not need
AMD tools either. Install and configure tools only if you want to run the
post-synthesis resource measurements.

## Supported measurement environment

Use Linux with **classic Vitis HLS 2024.2** and **Vivado 2024.2**, the required
licenses, and device support for `xcvc1902-vsva2197-2MP-e-S` (VCK190).
No physical FPGA board is needed. The measurement protocol targets 10 ns.
The repository does not redistribute AMD tools, headers, or licenses, and
the Conda environment does not install them.

The generated scripts use the classic `vitis_hls -f` Tcl interface. They run
`csynth_design`, followed by `export_design -flow syn -format ip_catalog`;
the latter invokes Vivado RTL synthesis from HLS. They do not perform
placement/routing or RTL co-simulation.

Newer installations can expose a command named `vitis_hls` that forwards to
the unified `vitis-run` interface. That interface uses different command-line
options and is not a verified replacement for this workflow. A matching
command name alone is not sufficient: use the classic 2024.2 installation.

## 1. Load the environment on your machine

In the same Bash terminal used for the Python workflow, find the
`settings64.sh` files inside your own AMD installation. Their locations
depend on the installation root; if needed, ask your system administrator.
Do not copy paths from a different workstation.

If both tools are already configured, skip to the precheck below. Otherwise,
these commands ask for your paths instead of assuming an installation root:

```bash
read -r -p "Path to Vitis HLS 2024.2 settings64.sh: " SGRM_HLS_SETTINGS
source "$SGRM_HLS_SETTINGS"

read -r -p "Path to Vivado 2024.2 settings64.sh: " SGRM_VIVADO_SETTINGS
source "$SGRM_VIVADO_SETTINGS"

hash -r
```

Enter the full path to an existing `settings64.sh` file at each prompt,
without adding shell quotes to the entered text. Some installations load
both tools from one settings file; sourcing the matching Vivado settings
explicitly also avoids leaving a different release active on `PATH`.
Re-run these steps when opening a new terminal if your site does not load
the tool environment automatically.

Confirm that the search environment still imports this checkout:

```bash
python -c "import sgrm; print(sgrm.__file__)"
```

## 2. Check paths and versions without synthesis

Run from the repository root:

```bash
python hardware_validation/validate_hardware.py --stage check-tools
```

Expected output includes the actual executable paths, followed by:

```text
PASS Vitis HLS 2024.2 (classic Tcl interface)
PASS Vivado 2024.2
TOOL CHECK PASS: executable paths and versions verified; no synthesis started.
```

The command needs no search result or hardware plan. It performs version
queries in temporary directories, cleans any version-query logs, and does not
create synthesis projects or change your shell environment. A failed query,
an unrecognized version banner, or a release other than 2024.2 is an error.
License availability and installed VCK190 device support are verified by
the AMD tools during the actual hardware run, not by this version-only check.

## 3. Select explicit executables when needed

When several AMD releases are installed, avoid relying on their order on
`PATH`. After loading the matching settings, enter full paths to the actual
classic HLS and Vivado executables, not their directories or shell aliases:

```bash
read -r -p "Path to classic 2024.2 vitis_hls executable: " SGRM_HLS_BIN
read -r -p "Path to 2024.2 vivado executable: " SGRM_VIVADO_BIN

python hardware_validation/validate_hardware.py \
  --stage check-tools \
  --vitis-hls "$SGRM_HLS_BIN" \
  --vivado "$SGRM_VIVADO_BIN"
```

Quote these variables in commands, including when installation paths contain
spaces. For standard AMD installations, the validator selects the matching
installation roots and places both executable directories on the child
process's `PATH`. This ensures that the HLS export uses the selected Vivado
installation, not only that a separate Vivado version query passes. Site
wrapper scripts must also preserve the configured AMD environment.

A precheck does not save executable selections. Repeat the same flags when
running a prepared plan:

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/three-design-hardware \
  --stage run \
  --jobs 2 \
  --vitis-hls "$SGRM_HLS_BIN" \
  --vivado "$SGRM_VIVADO_BIN"
```

This last command **does start synthesis**; run it only after preparing the
plan and passing the precheck. If you are checking the software workflow only,
stop after `--stage prepare` and `--stage check-tools`.

For automation, `VITIS_HLS` and `VIVADO` may contain executable paths. The
selection order is explicit command-line flags, these environment variables,
then `vitis_hls`/`vivado` on `PATH`. Neither variable replaces installation,
license configuration, or required host libraries.

## Troubleshooting

| Message | Action |
|---|---|
| `Vivado executable not found` | Load your Vivado 2024.2 settings, or provide its full executable path with `--vivado`. Installing only HLS is not enough for hardware resources. |
| `Vitis HLS executable not found` | Load your classic HLS 2024.2 settings, or select its full executable path with `--vitis-hls`. |
| `vitis-run` / unrecognized `-version` | The selected HLS command is using the unified CLI. Select classic HLS 2024.2; changing only the version flag does not make the Tcl export workflow compatible. |
| `found 2025.1` (or another release) | Select matching 2024.2 tools. Do not mix releases or use newer tools to check the 2024.2 reference measurements. |
| Version check exits nonzero or times out | Inspect the reported path/output, host-library requirements, and AMD installation setup. A version string in failed output is not a successful precheck. |
| License/device error during synthesis | Check licenses and VCK190 device support in your local installation. A version precheck cannot validate those requirements. |

Existing plans and finished reports remain usable with `--stage run` and
`--stage report`. After changing the validator or FIFO configuration, prepare
a new output directory rather than overwriting earlier hardware results.

AMD documents [environment setup](https://docs.amd.com/r/2024.2-English/ug1742-vitis-release-notes/Setting-Up-the-Environment-to-Run-the-Vitis-Software-Platform)
and the [unified command-line interface](https://docs.amd.com/r/2024.2-English/ug1399-vitis-hls/vitis-v-and-vitis-run-Commands)
for installations that use it.
