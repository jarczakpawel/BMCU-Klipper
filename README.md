# BMCU-Klipper

BMCU is an open **Multi Color Unit**, originally designed for Bambu Lab printers. Firmware updates for Bambu Lab printers that restrict interoperability - which in my opinion are incompatible with European law ([more on this topic](https://github.com/jarczakpawel/BMCU-C-PJARCZAK/blob/main/bmcu-vs-firmware-locks.md)) - are what led to the creation of BMCU-Klipper.

BMCU-Klipper is an integration with an open system, so channel routing, toolhead assignments, load/unload, refill, prestaging, tip forming and source planning can be adapted precisely to a specific printer. You do not need to adapt the printer to a closed ecosystem - here you can change both BMCU behavior and the way it works with the printer, and every part of the integration can later be improved or modified for your own needs.

One of the most important goals of the project was to keep the integration as lightweight as possible so that BMCU also works on weaker Klipper hosts. Each module has its own process handling USB/UART transport, while G-code analysis and planning of future source changes run in a separate process. The module running in Klipper only receives small batches of prepared data - by default at most 2 packets, 256 B and 0.5 ms of work per reactor entry - and then returns control to Klipper. Subsequent state updates are merged, background tasks are deferred while the printer is moving, and BMCU reactor activity is completely suppressed during homing and probing. When nothing is happening, the manager automatically reduces its polling frequency. Motors, the buffer and current filament transport are controlled by BMCU firmware, so the printer's main MCU does not receive this workload.

In my tests, the impact of the integration is unnoticeable. It was meant to be feather-light, and in my opinion that goal has been achieved.

## Connecting BMCU

[How to physically connect BMCU - USB, TTL/CH340 and 24 V power](docs/CONNECTION.md).

## Supported integrations

| Integration | Status | Scope |
| --- | --- | --- |
| **Snapmaker U1** | **Fully completed** | Dedicated support for four U1 heads, feeders and sensors, load/unload, tip forming, prestaging, refill, G-code planning and OrcaSlicer profiles. |
| **Generic Klipper** | **In continuous development** | Layer for Voron, VzBot and other machines. Printer mechanics are defined through an endpoint and user macros. |

The project core is prepared for additional printers and hardware adapters.

## Installation and update

Installation is the same on every printer.

The simplest method is to run the installer directly on the Klipper host:

```sh
curl -fsSL https://raw.githubusercontent.com/jarczakpawel/BMCU-Klipper/main/install.sh | sh
```

If BMCU-Klipper is already installed, the same command performs an update. After updating the printer firmware or operating system, run the installer again.

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

From the extracted package matching the installed version:

```sh
./uninstall
```

The uninstaller removes components managed by BMCU-Klipper and restores modified host components.

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
