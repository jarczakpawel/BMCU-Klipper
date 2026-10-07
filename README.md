# BMCU-Klipper

> [!WARNING]
> **Before updating BMCU-Klipper, completely unload all filament from every BMCU and toolhead.**
> A BMCU-Klipper update may also require a matching BMCU firmware update. If the required firmware version changes, update the BMCU firmware before using BMCU again.

BMCU is an open **Multi Color Unit**, originally designed for Bambu Lab printers. Firmware updates for Bambu Lab printers that restrict interoperability - which in my opinion are incompatible with European law ([more on this topic](https://github.com/jarczakpawel/BMCU-C-PJARCZAK/blob/main/bmcu-vs-firmware-locks.md)) - are what led to the creation of BMCU-Klipper.

BMCU-Klipper integrates BMCU with Klipper and provides routing, load/unload, refill, prestaging, tip forming and source planning.

### Snapmaker U1 - video guide

[Watch the complete BMCU-Klipper installation, configuration and real-world demonstration on Snapmaker U1](https://www.youtube.com/watch?v=mTMcayuyzYw).

BMCU-Klipper is also compatible with PAXX12 Extended Firmware on Snapmaker U1. Installation is the same as on the stock firmware.

The integration keeps USB/UART transport and print planning outside Klipper's main reactor where possible, while BMCU firmware handles motor and buffer control.

## Connecting BMCU

[How to physically connect BMCU - USB, TTL/CH340 and 24 V power](docs/CONNECTION.md).

## Supported integrations

| Integration | Status | Scope |
| --- | --- | --- |
| **Snapmaker U1** | **Fully completed** | Dedicated support for four U1 heads, feeders and sensors, load/unload, tip forming, prestaging, refill, G-code planning and OrcaSlicer profiles. |
| **Generic Klipper** | **In continuous development** | Layer for Voron, VzBot and other machines. Printer mechanics are defined through an endpoint and user macros. |

The project core is prepared for additional printers and hardware adapters.

## Installation and update

The installer supports Snapmaker U1 and Generic Klipper hosts. If several Klipper instances are detected, select the intended instance or pass `--config-dir`.

The simplest method is to run the installer directly on the Klipper host:

```sh
curl -fsSL https://raw.githubusercontent.com/jarczakpawel/BMCU-Klipper/main/install.sh | sh
```

Installation, update and reinstallation use the same command. Do not run it during an active print.

After updating the printer firmware or operating system, run the installer again.

Panel:

```text
http://PRINTER_IP:8291/
```

### Manual installation

Download `BMCU-Klipper.zip` from [GitHub Releases](https://github.com/jarczakpawel/BMCU-Klipper/releases) and extract it into a `BMCU-Klipper` directory. Then upload that directory to the printer using SFTP or SCP.

Example from a terminal or PowerShell on Windows, macOS or Linux:

```sh
scp -r BMCU-Klipper USER@PRINTER_IP:/tmp/
ssh USER@PRINTER_IP
cd /tmp/BMCU-Klipper
sh ./install
```

## Uninstallation

From an extracted BMCU-Klipper package:

```sh
sh ./uninstall
```

Without a package on the printer:

```sh
curl -fsSL https://raw.githubusercontent.com/jarczakpawel/BMCU-Klipper/main/install.sh | sh -s -- --uninstall
```

BMCU-Klipper is removed automatically and Klipper is restarted. On Snapmaker U1, the native feeder configuration is restored.

A backup of BMCU settings is kept in `printer_data/bmcu-backups/`.

## First setup

| Printer | Documentation |
| --- | --- |
| Snapmaker U1 | [Snapmaker U1](printers/Snapmaker_U1/README.md) |
| Voron, VzBot, custom machine | [Generic Klipper](printers/Generic/README.md) |

Full command reference: [docs/COMMANDS.md](docs/COMMANDS.md).

## Print plan

The slicer provides the source plan at the beginning of the G-code:

```gcode
BMCU_PRINT_BEGIN SCHEMA=1 RESET=1
BMCU_PRINT_MAP TOOL=5 MATERIAL="{filament_type[5]}" COLOR="{filament_colour[5]}"
BMCU_PRINT_MAP TOOL=7 MATERIAL="{filament_type[7]}" COLOR="{filament_colour[7]}"
BMCU_PRINT_COMMIT
```

`BMCU_PRINT_MAP` builds the plan, while `BMCU_PRINT_COMMIT` commits routing and metadata as one transaction.

During a print, sources are selected using logical `T` numbers:

- Snapmaker U1: `T0-T3` are the native sources for the four heads, while `T4+` are BMCU channels;
- Generic: `T0` is the manual External source for the main endpoint, while `T1+` are BMCU channels. There may be multiple endpoints/toolheads, and every BMCU channel can be assigned independently to any of them.

## Runout and refill

| Situation | Behavior |
| --- | --- |
| Endpoint with sensor + ready backup | The sensor confirms the end of the old filament, BMCU loads the backup and resumes the print. |
| Endpoint with sensor + no ready backup | The sensor confirms the end of the old filament, the print pauses and waits for manual replacement in the same channel. |
| Generic without an endpoint sensor | `EMPTY` at the BMCU input triggers a pause and manual replacement in the same channel. |

For manual replacement, normal `RESUME` starts a full BMCU `LOAD`, and the printer is actually resumed only after loading completes successfully.

## BMCU firmware

Firmware sources are in `firmware/`:

```sh
cd firmware
pio run -e klipper
```

Output:

```text
.pio/build/klipper/firmware.bin
```

The host checks firmware protocol compatibility before allowing BMCU motion operations.

## Documentation

| File | Contents |
| --- | --- |
| [docs/CONNECTION.md](docs/CONNECTION.md) | Physical BMCU connection, USB/TTL and 24 V power. |
| [printers/Snapmaker_U1/README.md](printers/Snapmaker_U1/README.md) | Snapmaker U1 setup and integration details. |
| [printers/Generic/README.md](printers/Generic/README.md) | Generic setup using endpoints and macros. |
| [docs/COMMANDS.md](docs/COMMANDS.md) | Public G-code commands and parameters. |

## Repository structure

```text
firmware/              BMCU controller firmware
klippy/extras/         Klipper modules
scripts/               transport, planner, installer, updater and diagnostics
web/                   web panel and OrcaSlicer profile generators
printers/Generic/      Generic integration
printers/Snapmaker_U1/ Snapmaker U1 integration
docs/                  technical documentation and command reference
```

## Diagnostics

In the panel:

```text
Diagnostics -> Export logs
```

From the console:

```sh
./collect-logs
./collect-logs --include-last-gcode
```

## License

GPL-3.0. Details: [LICENSE](LICENSE).

BMCU-Klipper is an independent open-source project.
