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

## 1. Discover and check tools without synthesis

Keep the SGRM Python environment activated and run from the repository root.
You do not need to source AMD settings manually first:

```bash
python hardware_validation/validate_hardware.py --stage check-tools
```

For each tool, the validator checks these sources in order:

1. Explicit `--vitis-hls` / `--vivado` arguments, or `VITIS_HLS` / `VIVADO`
   executable-path overrides. An invalid override fails rather than silently
   selecting another installation.
2. Previously validated paths in the machine-local `.sgrm-tools.json`.
3. AMD installation variables: `XILINX_HLS`, `XILINX_VITIS`, and `XILINX_VIVADO`.
4. Executables on `PATH`.
5. Bounded installation-directory searches under `Xilinx*` and `AMD*` roots
   in `/opt`, `/tools`, `/usr/local`, and the user's home directory.

Both product-first and version-first layouts are supported. Automatic
discovery tries candidates in this order, skips failed or incompatible ones,
and selects the first verified classic 2024.2 installation. Directory names
alone never establish a tool's version. Use explicit paths when a particular
installation is required. Discovery does not scan the entire filesystem.

Expected output includes the actual executable paths and:

```text
PASS Vitis HLS 2024.2 (classic Tcl interface)
PASS Vivado 2024.2
TOOL CONFIG: <your repository>/.sgrm-tools.json
TOOL CHECK PASS: executable paths and versions verified; no synthesis started.
```

The validator loads the selected installations' `settings64.sh` files in a
private Bash child process. It then selects the checked tool roots and places
the HLS and Vivado executable directories first on that child's `PATH`, so HLS
export uses the checked Vivado. Your interactive terminal and Conda environment
are not changed.

The command needs no search result or hardware plan. It performs version
queries in temporary directories, cleans any version-query logs, and does not
create synthesis projects. Every invocation checks versions again, including
when paths come from saved configuration. A failed query, an unrecognized
version banner, or a release other than 2024.2 is not accepted.
License availability and installed VCK190 device support are verified by
the AMD tools during the actual hardware run, not by this version-only check.

## 2. Point to a nonstandard installation

If your tools are installed outside the discovered locations, provide the
installation directory once:

```bash
read -r -p "AMD installation directory: " SGRM_AMD_ROOT

python hardware_validation/validate_hardware.py \
  --stage check-tools \
  --tool-root "$SGRM_AMD_ROOT"
```

Enter a full path without adding shell quotes to the prompted text. The root
can be a shared installation directory, a product directory, or a release
directory. Repeat `--tool-root` for separate HLS and Vivado locations. Supplied
roots replace the common-directory fallback; higher-priority configuration,
environment variables, and `PATH` are still considered first.

Once the check succeeds, future commands reuse the saved executable paths and
settings, even in a new terminal. You do not need to repeat `--tool-root`.

## 3. Select executables or site settings explicitly

When several AMD releases are installed, avoid relying on their order on
`PATH`. Enter full paths to the actual
classic HLS and Vivado executables, not their directories or shell aliases:

```bash
read -r -p "Path to classic 2024.2 vitis_hls executable: " SGRM_HLS_BIN
read -r -p "Path to 2024.2 vivado executable: " SGRM_VIVADO_BIN

python hardware_validation/validate_hardware.py \
  --stage check-tools \
  --vitis-hls "$SGRM_HLS_BIN" \
  --vivado "$SGRM_VIVADO_BIN"
```

Quote variables in commands, including when installation paths contain spaces.
You do not need to load the installation settings manually. To add a trusted
site script, for example for host libraries or license setup, use:

```bash
read -r -p "Path to trusted site settings script: " SGRM_SITE_SETTINGS

python hardware_validation/validate_hardware.py \
  --stage check-tools \
  --vitis-hls "$SGRM_HLS_BIN" \
  --vivado "$SGRM_VIVADO_BIN" \
  --settings "$SGRM_SITE_SETTINGS"
```

Repeat `--settings` for multiple scripts. Loading order is the detected HLS
settings, detected Vivado settings, and then your site scripts. Tool root and
executable-directory selection is applied after settings loading. Site wrapper
scripts must preserve this selected environment. Settings scripts are executable
code; load only trusted files. See [Security](../SECURITY.md).

## 4. Reuse the validated configuration

Successful checks save `.sgrm-tools.json` with the executable paths, settings
paths, schema, and required version. The file contains no environment dump,
license values, or credentials, and is ignored by Git. The default location
is the repository root regardless of the current working directory.

On another machine, run the precheck there rather than copying this file.
Stale or incompatible cached executables trigger rediscovery. Site settings
are reused when the saved tool pair is selected; missing saved settings produce
an actionable error. Malformed configuration is not silently overwritten.

Use `--tool-config /a/writable/path/tools.json` for a different configuration
file. Repeat that option in subsequent commands to reuse it. Use
`--no-save-tools` to suppress configuration writes; an existing configuration
is still read and checked.

After preparing a plan and passing the precheck, run without repeating tool
paths or settings:

```bash
python hardware_validation/validate_hardware.py \
  --output-dir results/three-design-hardware \
  --stage run \
  --jobs 2
```

This last command **does start synthesis**; run it only after preparing the
plan and passing the precheck. If you are checking the software workflow only,
stop after `--stage prepare` and `--stage check-tools`.

Every `run` or `all` invocation rechecks the tools and reloads their private
environment. Saved configuration does not replace installation, licensing,
required host libraries, or device support.

## Troubleshooting

| Message | Action |
|---|---|
| Tool `not discovered` | It may be installed outside the searched locations. Supply `--tool-root` or its full executable path. This message does not assert that the tool is uninstalled. |
| `installation candidates were found, but none passed` | Inspect the candidate paths and stated version/environment failures. Select a compatible installation or correct its setup. |
| `vitis-run` / unrecognized `-version` | The candidate uses the unified CLI. Automatic discovery tries other candidates; an explicit override requires correction. Select classic HLS 2024.2, not just a different version flag. |
| `found 2025.1` (or another release) | Select matching 2024.2 tools. Do not mix releases for the 2024.2 reference measurements. |
| `AMD environment setup failed` | Check the named trusted settings scripts and their host requirements; use `--settings` for required site setup. The full environment is not printed. |
| Settings script is missing | Update the cached settings paths, select the intended installation, or supply a replacement trusted script. |
| Cannot read or unsupported tool configuration | Repair the indicated file or use a new `--tool-config` path; unrelated JSON is not overwritten. |
| Cannot save validated tool configuration | Choose a writable `--tool-config` path, or use `--no-save-tools`. |
| Version check exits nonzero or times out | Inspect the reported path/output, host-library requirements, and installation setup. A version string in failed output is not a successful check. |
| License/device error during synthesis | Check licenses and VCK190 device support in your local installation. A version precheck cannot validate those requirements. |

Existing plans and finished reports remain usable with `--stage run` and
`--stage report`. After changing the validator or FIFO configuration, prepare
a new output directory rather than overwriting earlier hardware results.

AMD documents [environment setup](https://docs.amd.com/r/2024.2-English/ug1742-vitis-release-notes/Setting-Up-the-Environment-to-Run-the-Vitis-Software-Platform)
and the [unified command-line interface](https://docs.amd.com/r/2024.2-English/ug1399-vitis-hls/vitis-v-and-vitis-run-Commands)
for installations that use it.
