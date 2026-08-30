# BMCU-Klipper - Snapmaker U1

Dedicated BMCU integration for the four-head Snapmaker U1.

The integration uses the native U1 model of heads, feeders, sensors and states, while adding BMCU channels as additional logical material sources. The entire workflow remains consistent with Snapmaker mechanics: U1 handles its own heads and hotends, while BMCU handles material transport, routing and source preparation.

## Contents

- [Integration scope](#integration-scope)
- [Requirements](#requirements)
- [Installation and update](#installation-and-update)
- [First setup](#first-setup)
- [Source and tool model](#source-and-tool-model)
- [Load, unload and tool change](#load-unload-and-tool-change)
- [Tip forming and background work](#tip-forming-and-background-work)
- [Runout and refill](#runout-and-refill)
- [OrcaSlicer and Snapmaker Orca](#orcaslicer-and-snapmaker-orca)
- [Panel and BMCU firmware](#panel-and-bmcu-firmware)
- [Optional 24 V power](#optional-24-v-power)
- [Diagnostics](#diagnostics)
- [After a U1 firmware update](#after-a-u1-firmware-update)
- [Uninstallation](#uninstallation)

## Integration scope

| Area | U1 implementation |
| --- | --- |
| **Native heads** | `T0-T3` correspond to the four physical Snapmaker heads. |
| **BMCU sources** | BMCU channels are mapped as logical `T4+` sources. |
| **Feeder and sensors** | The integration uses native U1 feeder and sensor states. |
| **Load / unload** | BMCU handles PTFE transport, while the U1 head performs final loading and filament release. |
| **Tip forming** | Editable profiles with temperature, fan, E movement and the `BMCU_PARK_HEAD` marker. |
| **Prestaging** | The next source can be prepared in advance when the route and head state are confirmed. |
| **Planner** | Future tool changes are analyzed by a separate process. |
| **Transport** | BMCU UART is handled by a separate `bmcu_transportd.py` process. |
| **Refill** | The native head sensor determines the end of the old filament; a compatible backup triggers automatic refill, while manual replacement uses the same channel. |
| **Orca** | The panel generates the `Snapmaker U1 BMCU` profile, source plan and material/color synchronization. |
| **Recovery** | Route state is explicit: `EMPTY`, `LOADED`, `UNCERTAIN`. |

## Requirements

Before installation:

1. Enable **Root Access** on the U1 screen.
2. Enable **Auto Loading** for all four heads:

```text
Settings > Print Preferences > Auto Loading
```

3. Finish any active print and bring Klipper to the `ready` state.

Root Access is used by the installer for persistent host changes. Klipper runs as the `lava` user.

## Installation and update

Connect to the U1 over SSH:

```sh
ssh root@PRINTER_IP
```

The default U1 password is:

```text
snapmaker
```

Install BMCU-Klipper:

```sh
curl -fsSL https://raw.githubusercontent.com/jarczakpawel/BMCU-Klipper/main/install.sh | sh
```

Updating works the same way - run the same command again. After a U1 firmware update, also run the BMCU-Klipper installer again.

Manual installation over SFTP/SCP is described in the [main README](../../README.md#manual-installation).

## First setup

1. Open the BMCU panel.
2. Add a BMCU module and select its serial port.
3. Calibrate the channels you use.
4. Assign BMCU channels to the selected U1 heads.
5. Set materials and colors.
6. Download the `Snapmaker U1 BMCU` profile for the OrcaSlicer variant you use.
7. Import the profile into the slicer.
8. Perform a single `LOAD` and `UNLOAD` test.
9. Perform a short multicolor test.

## Source and tool model

| Tool | Source |
| --- | --- |
| `T0` | native U1 head 1 |
| `T1` | native U1 head 2 |
| `T2` | native U1 head 3 |
| `T3` | native U1 head 4 |
| `T4+` | BMCU channels |

Example:

```text
T0 -> Head 1
T1 -> Head 2
T2 -> Head 3
T3 -> Head 4
T4 -> bmcu0 Channel 1 -> Head 1
T5 -> bmcu0 Channel 2 -> Head 2
T6 -> bmcu0 Channel 3 -> Head 3
T7 -> bmcu0 Channel 4 -> Head 4
```

A BMCU channel ends its route at one of the four physical U1 heads. This lets the slicer treat each material as a separate logical source while the printer still uses its native head-selection mechanism.

### BMCU channel routing

Routing is independent for every channel. Any channel from any BMCU module can be assigned to any of the four U1 heads. There is no requirement for 1:1 mapping or for assigning an entire module to one head.

Example with three modules:

```text
bmcu0 Channel 1 -> Head 1
bmcu0 Channel 2 -> Head 4
bmcu1 Channel 1 -> Head 2
bmcu1 Channel 4 -> Head 1
bmcu2 Channel 3 -> Head 3
```

Each channel can have its own PTFE route and target head.

### Stock U1 feeders

The original Snapmaker feeders can continue to work as native `T0-T3` sources. Their drive, however, is not bidirectional transport for a single filament: the two motor directions operate two separate channels. A stock feeder can feed filament toward the head, but it cannot retract it back through a shared PTFE route.

If stock filament occupies the route, it must be retracted manually before BMCU takes over that route. The simplest approach is to use the stock feeder as an independent source or as the final source in a print, when later automatic filament retraction will not be needed.

BMCU works well as a replacement for the stock feeders because every channel can both feed and retract filament. PTFE routes can be configured freely, and the panel can decide whether the final filament remains loaded after a successful print. On the next print, the integration checks the state of every used head: if another BMCU source is loaded on a route, it is safely unloaded before the correct material is loaded.

I consider BMCU an ideal feeder for the Snapmaker U1 and I am genuinely proud of the results of this integration. You can connect multiple BMCU modules, assign all filaments in OrcaSlicer and not worry about which material is currently loaded.

## Host changes made by the installer

| Component | Role |
| --- | --- |
| `/home/lava/printer_data/config/bmcu/` | managed BMCU configuration |
| `printer.cfg` | managed `include` block |
| Klipper `extras` | symlinks to `bmcu.py`, `bmcu_core`, `bmcu_panel.py` |
| `/oem/bmcu-klipper/` | persistent U1 integration bootstrap |
| `/etc/init.d/S60klipper` | hook executed before the actual Klipper startup |

## Load, unload and tool change

### Loading a BMCU source

```text
BMCU channel
  -> SEND_OUT through PTFE
  -> U1 endpoint
  -> takeover by the native head
  -> ON_USE
  -> print
```

After the material is taken over, BMCU firmware applies a soft handoff to `ON_USE`. For up to 10 seconds, the high buffer level at the end of loading becomes the initial ceiling, and the printer naturally consumes the reserve toward the normal target. After the target is reached or 10 seconds have passed, the normal `ON_USE` controller takes over.

### Unload

```text
end of source use
  -> prepare the head
  -> tip forming / parking
  -> release filament from the head
  -> long BMCU pullback
  -> route = EMPTY
```

The long pullback begins after the hotend- and head-dependent part has finished.

### Tool change

`BMCU_TOOL_CHANGE` resolves the logical `T` to the correct source. Native `T0-T3` use the U1 workflow, while `T4+` start the BMCU routing and operations required by the assigned head.

## Tip forming and background work

Tip forming is stored as an editable U1 program. The profile can control:

- temperature,
- fan,
- extruder movement,
- the `BMCU_PARK_HEAD` marker.

`BMCU_PARK_HEAD` separates the part executed before and after parking the head. This allows later tool-change stages to be prepared at the correct moment in the process.

Background work starts only when all conditions are met:

- unambiguous source state,
- correct endpoint,
- available shared route,
- free BMCU motion channel,
- planner-confirmed context for the future change.

Otherwise, the operation follows the synchronous path.

## Runout and refill

For a BMCU source on U1, the native head sensor determines the physical end of the old filament.

| Situation | Behavior |
| --- | --- |
| **Ready compatible replacement source** | After the end of the old filament is confirmed, BMCU performs automatic refill and resumes the print. |
| **No ready replacement source** | After the end of the old filament is confirmed, U1 pauses and waits for new filament in the same BMCU channel. |
| **Native Snapmaker feeder runout** | The stock U1 mechanism handles the native source. |

Manual replacement in the same channel:

```text
PAUSE
  -> insert filament into the indicated BMCU channel
  -> normal Resume
  -> check filament presence and head sensor state
  -> full BMCU LOAD to the same head
  -> stock U1 RESUME
```

The pending replacement state remains active until `LOAD` completes successfully, so another `Resume` attempt can be made after adding filament or removing the cause of the error.

## OrcaSlicer and Snapmaker Orca

The panel generates the profile:

```text
Snapmaker U1 BMCU
```

Variants are available for standard OrcaSlicer and Snapmaker Orca.

The profile includes:

- the `T0-T3` model for native heads,
- logical BMCU `T4+` sources,
- the startup `BMCU_PRINT_BEGIN / MAP / COMMIT` plan,
- `BMCU_TOOL_CHANGE` in tool changes,
- material and color data for used BMCU sources,
- integration with the native U1 startup sequence.

### Material, color and LED

For BMCU sources, the profile passes OrcaSlicer data:

```gcode
BMCU_PRINT_MAP TOOL=5 MATERIAL="{filament_type[5]}" COLOR="{filament_colour[5]}"
```

After a successful `BMCU_PRINT_COMMIT`, material and color are stored for the channel, updated in the panel and synchronized with the physical BMCU module using one batched `SET_SLOTS` operation per module.

The data comes from the specific sliced G-code file, so BMCU state matches the materials used in that print.

## Panel and BMCU firmware

Panel:

```text
http://PRINTER_IP:8291/
```

It supports, among other things:

- BMCU devices,
- channels and routing,
- materials and colors,
- calibration,
- manual load/unload/retract,
- lighting,
- Orca profiles,
- firmware update,
- diagnostic export.

Firmware:

```sh
cd firmware
pio run -e klipper
```

Output:

```text
.pio/build/klipper/firmware.bin
```

The host checks firmware protocol compatibility before allowing motion operations.

## Optional 24 V power

The general wiring diagram is in the [BMCU physical connection guide](../../docs/CONNECTION.md).

It is best to power BMCU from a separate `24V` supply. Optionally, on Snapmaker U1 you can use `24V` and `GND` from the upper 6-pin fan connector. The included helper enables it:

```sh
./printers/Snapmaker_U1/enable-24v.sh
```

Run it as `root`. If you use this power source, run the helper again after a U1 firmware update.

## Diagnostics

In the panel:

```text
Diagnostics -> Export logs
```

The export may contain:

- BMCU log,
- Klipper log,
- Moonraker log,
- BMCU configuration,
- runtime status,
- printer status,
- last G-code.

From the console:

```sh
./collect-logs
./collect-logs --include-last-gcode
```

## After a U1 firmware update

After every Snapmaker firmware update:

1. verify `Root Access`,
2. verify `Auto Loading` for all four heads,
3. run the BMCU-Klipper installer again from the [main README](../../README.md#installation-and-update),
4. if you use 24 V power from the upper connector, run `enable-24v.sh` again,
5. open the BMCU panel and verify module status.

## Uninstallation

Connect to U1 over SSH, enter the extracted BMCU-Klipper directory matching the installed version and run:

```sh
./uninstall
```

The uninstaller removes managed BMCU components from `printer.cfg`, Klipper modules, runtime, serial-port access and the U1 hook.

For the 24 V configuration, helper backups are stored in:

```text
/oem/user-24v/S60klipper.before-24v
/oem/user-24v/printer.cfg.before-24v
```

When disabling 24 V, restore the sections belonging to the helper from the appropriate backup.

Full command list: [../../docs/COMMANDS.md](../../docs/COMMANDS.md).  
Back to the main README: [../../README.md](../../README.md).
