# BMCU-Klipper - Generic Klipper

Integration for Voron, VzBot and other Klipper-based printers.

BMCU handles the channel, buffer, PTFE transport and source selection. The printer only needs to know how to take over the filament at the toolhead and how to release it safely afterward. In Generic, this is handled by two Klipper macros.

Installation and updates are shared across all printers: [main README](../../README.md#installation-and-update). After updating the printer firmware or operating system, run the BMCU-Klipper installer again.

## Required macros

Generic requires two macros:

- `Toolhead preparation macro` - fully takes over the filament after BMCU delivers it;
- `Before pullback macro` - fully prepares the filament to be retracted by BMCU.

In these macros, set the hotend temperature, extruder movements and filament lengths appropriate for your printer.

### Toolhead preparation macro

This macro runs after BMCU delivers the filament through PTFE to the end of its part of the route.

When the macro finishes, the filament must be ready for printing. Depending on the printer, this usually means:

```text
heat the hotend
-> let the extruder take over the filament
-> feed it into the melt zone / nozzle
-> optionally prime or purge
```

If the printer already has a correct loading macro, you can simply use it:

```ini
[gcode_macro BMCU_TOOLHEAD_PREPARE]
gcode:
    LOAD_FILAMENT
```

`LOAD_FILAMENT` is only an example. Use the existing macro from your printer or place your own tested loading sequence here.

### Before pullback macro

This macro runs before BMCU performs the long filament retraction.

When it finishes, the filament must no longer be locked in the hotend or by the extruder gears. Depending on the machine, perform the following here:

```text
heat the hotend
-> tip forming or cutting
-> retract the filament from the hotend
-> release the filament from the extruder
```

If the printer already has a correct unload macro:

```ini
[gcode_macro BMCU_BEFORE_PULLBACK]
gcode:
    UNLOAD_FILAMENT
```

`UNLOAD_FILAMENT` is an example. The macro must finish only when BMCU can safely retract the filament through the entire PTFE route.

Both macros receive these parameters:

```text
ENDPOINT
MATERIAL
REASON
```

You do not have to use them. They are available if you want different behavior for PLA, TPU, normal load, tool change or refill.

Example using the material parameter:

```ini
[gcode_macro BMCU_TOOLHEAD_PREPARE]
gcode:
    {% set material = params.MATERIAL|default("") %}
    LOAD_FILAMENT MATERIAL={material}
```

The macro names configured in the panel must exist in Klipper.

## Panel configuration

After restarting Klipper, open `Settings`.

Under `Generic Klipper toolhead`, set:

1. `Extruder object` - the correct Klipper extruder, usually `extruder`.
2. `Toolhead preparation macro` - the macro that prepares filament for printing.
3. `Before pullback macro` - the macro that releases filament before retraction.
4. `Loading arrival sensor` - optional sensor at the toolhead, if the printer has one.

For a standard single-extruder printer, this is enough. `Select`, `Verify`, `Deselect` and additional settings under `Advanced` are only needed for a toolchanger or custom mechanics.

Then assign each BMCU channel to the endpoint its PTFE tube actually reaches.

If multiple channels lead to one toolhead, their physical routes must be joined with an appropriate PTFE splitter or combiner.

## Calibration and first filament

Before calibration, completely remove filament from the selected BMCU channels and leave the buffers free.

Start calibration from the panel. A separate encoder test is not required during normal setup - filament movement and the encoder are checked automatically during autoload or a real load.

After calibration, insert filament into a channel and perform the first `Load` from the panel. Verify that:

```text
BMCU feeds filament through PTFE
-> runs the Toolhead preparation macro
-> filament reaches the nozzle correctly
-> the channel changes to In use
```

Then perform `Unload` and verify:

```text
Before pullback macro
-> filament is released from the toolhead
-> BMCU retracts it through PTFE
-> the route ends empty
```

Configure automatic material changes in the slicer only after manual `Load` and `Unload` work correctly.

## Tools and routing

In Generic, `T` numbers describe filament sources, not the number of toolheads:

```text
T0  = manual External source for the main endpoint
T1+ = BMCU channels
```

Generic can have multiple endpoints/toolheads. The integration creates separate endpoints for detected Klipper extruders, and each BMCU channel can be assigned to any selected endpoint.

Example with multiple toolheads:

```text
T0 -> External -> extruder
T1 -> bmcu0 Channel 1 -> extruder
T2 -> bmcu0 Channel 2 -> extruder1
T3 -> bmcu0 Channel 3 -> extruder2
T4 -> bmcu0 Channel 4 -> extruder1
```

`T0` is the manual External source for the main endpoint. Other endpoints can still be used normally as BMCU route targets.

## OrcaSlicer

The panel shows the current `T` numbers and generates blocks matching the configured channels. Open `Setup and OrcaSlicer settings` after routing is complete.

In OrcaSlicer:

- set the number of extruders to match the highest used slot;
- enable `Single Extruder Multi Material`;
- leave `Manual Filament Change` disabled;
- copy the current `Machine start G-code` block from the panel;
- set `Change filament G-code` to call `BMCU_TOOL_CHANGE`;
- add `BMCU_PRINT_END` before the normal end of the print.

The BMCU plan must be placed at the beginning of `Machine start G-code`, before the printer's normal start sequence:

```gcode
BMCU_PRINT_BEGIN SCHEMA=1 RESET=1
BMCU_PRINT_MAP ...
BMCU_PRINT_COMMIT
```

Select the first source after the hotend is prepared for loading, but before the first purge or printing:

```gcode
BMCU_TOOL_CHANGE TOOL={initial_extruder}
```

A filament change calls:

```gcode
BMCU_TOOL_CHANGE TOOL=<next T>
```

End of print:

```gcode
BMCU_PRINT_END MODE=AUTO CLEAR=1
```

It is safest to copy these blocks directly from the panel because they reflect the current channel and slot assignments.

## Runout

If the endpoint has a filament sensor, BMCU can track the end of the old filament at the toolhead and use a compatible refill channel.

If no ready replacement is available, the printer remains paused. Insert new filament into the same BMCU channel and use normal `RESUME`. BMCU first performs a full `Load` and only then passes the actual `RESUME` to the printer.

Without a sensor at the toolhead, BMCU pauses when its own input detects the end of the filament. Manual replacement works the same way.

Full command list: [../../docs/COMMANDS.md](../../docs/COMMANDS.md).  
Back to the main documentation: [../../README.md](../../README.md).
