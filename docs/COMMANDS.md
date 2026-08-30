# BMCU-Klipper commands

Reference for BMCU-Klipper G-code commands. Use the panel for normal configuration; commands are useful for custom macros, slicer integration and service work.

## Conventions

| Field | Meaning |
| --- | --- |
| `DEVICE` | module name, e.g. `bmcu0`; with one device, many commands allow it to be omitted |
| `CHANNEL` | physical channel `0..3`; the panel shows the same channels as 1-4 |
| `TOOL` | logical `T` tool used by the slicer |
| `ENDPOINT` | name of the printer's physical path/adapter |

Generic uses `T0` as External by default and `T1+` for BMCU. Snapmaker U1 keeps native `T0-T3`, while BMCU uses `T4+`.

Commands that change a physical route require an unambiguous state and a free route. `UNCERTAIN` requires explicit confirmation before motion.

## Most commonly used

| Command | Purpose |
| --- | --- |
| `BMCU_STATUS` | device, routing and active-session state |
| `BMCU_CALIBRATE` | channel buffer calibration |
| `BMCU_SET_ENDPOINT` | printer endpoint configuration |
| `BMCU_SET_ROUTE` | assign a channel to an endpoint |
| `BMCU_SET_TOOL` | assign a logical `T` |
| `BMCU_LOAD` | full source load |
| `BMCU_UNLOAD` | full source unload |
| `BMCU_CHANNEL_RETRACT` | retract filament parked at the BMCU input |
| `BMCU_TOOL_CHANGE` | logical tool change during a print |
| `BMCU_REFILL_STATUS` | runout/refill status |

## Full command index

### Status and service

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_STATUS` | common | current BMCU-Klipper state |
| `BMCU_REFRESH` | common | force a device refresh |
| `BMCU_STOP` | common | stop motion on ready BMCU modules |
| `BMCU_CLEAR_ERROR` | common | clear host/controller error state |
| `BMCU_SAVE_STATE` | advanced | force persistent state save |
| `BMCU_ANALYZE_PRINTER` | service | analyze Klipper objects and detected platform |

### Calibration and channels

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_CALIBRATE` | common | automatic buffer calibration |
| `BMCU_CALIBRATE_CANCEL` | common | cancel calibration |
| `BMCU_CALIBRATE_POINT` | common | save a manual MIN/NEUTRAL/MAX point |
| `BMCU_CALIBRATE_COMMIT` | common | validate and save manual calibration |
| `BMCU_CALIBRATION_STATUS` | common | show calibration |
| `BMCU_TEST_ENCODER` | common | motion and encoder measurement test |
| `BMCU_CHANNEL_AUTOLOAD` | service | controlled channel input feed |
| `BMCU_CHANNEL_RETRACT` | common | fully retract filament parked in BMCU |
| `BMCU_SET_FILAMENT` | common | channel material, color, refill and metadata |
| `BMCU_SET_TOOL` | common | persistent logical `T` assignment |
| `BMCU_MAP_TOOL` | print plan | alias for source mapping in an open transaction |
| `BMCU_SET_OUTPUT` | common | routing-setting alias |
| `BMCU_SET_ROUTE` | common | assign a channel to an endpoint |
| `BMCU_SET_PREFERENCES` | common | global host preferences |

### Endpoints, presets and motion

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_SET_ENDPOINT` | common | create/edit an endpoint |
| `BMCU_APPLY_PRESET` | configuration | apply a detected printer preset |
| `BMCU_SETUP` | configuration | configuration/preset from the console |
| `BMCU_LOAD` | common | full source load |
| `BMCU_UNLOAD` | common | full source unload |
| `BMCU_PRESTAGE` | common | prepare a source before load |
| `BMCU_CLEAR_PRESTAGE` | common | cancel prestage |
| `BMCU_ROUTE_FEED` | advanced | controlled motion along a route |
| `BMCU_BUFFER_MODE` | advanced | change buffer operating mode |

### Recovery and physical state

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_ROUTE_CONFIRM` | common | explicitly confirm route state |
| `BMCU_ROUTE_RECOVER` | common | recover after an uncertain operation |
| `BMCU_HEAD_CONFIRM_EMPTY` | U1 / advanced | confirm an empty head |
| `BMCU_RECONCILE` | common | reconcile host and device state |

### Print plan and tool changes

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_PRINT_BEGIN` | slicer | open a plan transaction |
| `BMCU_PRINT_REQUIRE` | slicer | mark a required tool |
| `BMCU_PRINT_MAP` | slicer | add a source to the plan |
| `BMCU_PRINT_COMMIT` | slicer | atomically commit the plan |
| `BMCU_PRINT_PREEXTRUDE` | U1 / slicer | prepare pre-extrusion |
| `BMCU_PRINT_END` | slicer | end the print session |
| `BMCU_TOOL_CHANGE` | slicer | change logical tool |
| `BMCU_AUTO_FEED` | U1 / slicer | U1 auto-feed integration |
| `BMCU_SNAP_CHECK` | U1 | verify a U1 endpoint |
| `BMCU_SNAP_FEEDER` | U1 / advanced | native U1 feeder ownership |

### Refill

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_REFILL_STATUS` | common | active/previous refill status |
| `BMCU_REFILL_NOW` | common | manually start refill |
| `BMCU_PRINT_REFILL` | slicer | declare a backup in the print plan |
| `BMCU_REFILL_RESUME` | U1 / recovery | resume cross-head refill from journal |

The printer's normal `RESUME` is additionally used as a fail-closed gate for a pending manual same-channel refill. If such a refill is pending, BMCU first performs a full `LOAD` and only then passes `RESUME` to the printer's original handler.

### Firmware parameters and lighting

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_HANDOFF` | common | loading-phase handoff threshold |
| `BMCU_PRESSURE` | common | buffer/pressure target |
| `BMCU_SPEED` | common | transport speeds |
| `BMCU_LED` | common | basic color/brightness |
| `BMCU_LIGHTING` | common | full lighting configuration |
| `BMCU_LIGHTING_PROFILE` | common | lighting profiles |
| `BMCU_LED_PREVIEW` | common | temporary LED preview |

### Snapmaker U1 - tip forming

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_TIP_PROFILE` | U1 | manage tip-forming profiles |
| `BMCU_U1_GCODE` | U1 / alias | alias for `BMCU_TIP_PROFILE` |
| `BMCU_U1` | U1 / alias | alias for `BMCU_TIP_PROFILE` |
| `BMCU_APPLY_TIP_TEMP` | U1 / internal | apply temperature in the tip-forming program |
| `BMCU_APPLY_TIP_FAN` | U1 / internal | apply fan setting in the tip-forming program |

### Package management

| Command | Scope | Short description |
| --- | --- | --- |
| `BMCU_PREPARE_UNINSTALL` | service | prepare runtime for uninstallation |
| `BMCU_FORGET_DEVICE` | service | controlled removal of persistent device data |
| `BMCU_UPDATE_ACCESS` | internal | updater interface for quiesce/update/recovery |

---

## 1. Status, refresh and stop

| Command | Parameters | Behavior |
| --- | --- | --- |
| `BMCU_STATUS` | - | Returns BMCU-Klipper state. |
| `BMCU_REFRESH` | optional `DEVICE` | Forces a device-status refresh. |
| `BMCU_STOP` | - | Sends stop to all ready BMCU modules. |
| `BMCU_CLEAR_ERROR` | `DEVICE` | Clears controller error state, refreshes the snapshot and clears host `last_error`. |
| `BMCU_SAVE_STATE` | - | Forces saving the current persistent state. Normally this is done automatically. |
| `BMCU_ANALYZE_PRINTER` | - | Analyzes Klipper objects and shows the detected topology/preset. Useful when creating a new integration. |

Examples:

```gcode
BMCU_REFRESH DEVICE=bmcu0
BMCU_CLEAR_ERROR DEVICE=bmcu0
BMCU_ANALYZE_PRINTER
```

---

## 2. Calibration and channels

### `BMCU_CALIBRATE`

Automatic buffer calibration.

```gcode
BMCU_CALIBRATE DEVICE=bmcu0 CHANNEL=ALL
BMCU_CALIBRATE DEVICE=bmcu0 CHANNEL=2
```

`CHANNEL` can be `ALL` or `0..3`. Calibration must be performed with an empty route and no filament detected in the channel.

### `BMCU_CALIBRATE_CANCEL`

Cancels active automatic calibration.

```gcode
BMCU_CALIBRATE_CANCEL DEVICE=bmcu0
```

### `BMCU_CALIBRATE_POINT`

Manually save a calibration point:

```gcode
BMCU_CALIBRATE_POINT DEVICE=bmcu0 CHANNEL=0 POINT=MIN
BMCU_CALIBRATE_POINT DEVICE=bmcu0 CHANNEL=0 POINT=NEUTRAL
BMCU_CALIBRATE_POINT DEVICE=bmcu0 CHANNEL=0 POINT=MAX
```

### `BMCU_CALIBRATE_COMMIT`

Validates and saves manually collected points:

```gcode
BMCU_CALIBRATE_COMMIT DEVICE=bmcu0 CHANNEL=0
```

### `BMCU_CALIBRATION_STATUS`

Shows saved points and the raw reading:

```gcode
BMCU_CALIBRATION_STATUS DEVICE=bmcu0
BMCU_CALIBRATION_STATUS DEVICE=bmcu0 CHANNEL=0
```

### `BMCU_TEST_ENCODER`

Service motion and encoder-measurement test. It is not required during normal setup - motion correctness is also verified during actual autoload/load:

```gcode
BMCU_TEST_ENCODER DEVICE=bmcu0 CHANNEL=0 MM=50
```

`MM` has a range of 5-250 mm. The command works with an assigned endpoint and a free route.

### `BMCU_CHANNEL_AUTOLOAD`

Test/service filament feed at the channel input:

```gcode
BMCU_CHANNEL_AUTOLOAD DEVICE=bmcu0 CHANNEL=0 MM=120
```

The default length comes from the channel's `autoload_mm` setting.

### `BMCU_CHANNEL_RETRACT`

Retracts filament parked only inside BMCU:

```gcode
BMCU_CHANNEL_RETRACT DEVICE=bmcu0 CHANNEL=0
```

Safety conditions include, among others:

- valid channel calibration,
- detected input filament,
- route=`EMPTY`,
- no prestage,
- no active detached tail,
- no other BMCU motion.

Use `BMCU_UNLOAD` for filament routed through the printer path.

With firmware using two microswitches, autoload is re-armed after the filament is completely removed and inserted again.

---

## 3. Material and channel configuration

### `BMCU_SET_FILAMENT`

Changes metadata for one channel.

Basic syntax:

```gcode
BMCU_SET_FILAMENT DEVICE=bmcu0 CHANNEL=0 MATERIAL=PLA COLOR=#FF8000
```

Main parameters:

| Parameter | Meaning |
| --- | --- |
| `MATERIAL` | Material type. |
| `COLOR` | Main color as `RRGGBB` or `#RRGGBB`. |
| `COLORS` | Color list for multicolor material. |
| `NAME` | Channel/spool name. |
| `VENDOR` | Filament manufacturer. |
| `SUBTYPE` | Additional type/subtype. |
| `PROFILE_ID` | Profile identifier. |
| `SPOOL_ID` | Spool identifier. |
| `TOOL` | Logical `T` assigned to the channel. |
| `TEMP_MIN`, `TEMP_MAX` | Informational material temperature limits. |
| `UNLOAD_RETRACT_MM` | Retraction length used by channel policy during unload. |
| `AUTOLOAD_MM` | Service/autoload filament feed length for the channel. |
| `REFILL` | Enable/disable use of the channel as a refill source. |
| `REFILL_PRIORITY` | Refill priority. |
| `REFILL_GROUP` | Refill compatibility group. |
| `COLOR_MODE` | Color interpretation mode. |
| `CLEAR_NAME=1` | Clears the name. |
| `CLEAR_REFILL_GROUP=1` | Clears the refill group. |
| `SWAP=1` | Allows an atomic swap of a conflicting logical T assignment when safe. |
| `CONFIRM_ORCA=1` | Required when `TOOL` changes the persistent T assignment. |

After saving, the panel updates immediately. Material/color are also synchronized with the physical module and its LEDs when firmware is online.

### `BMCU_SET_TOOL`

Persistent assignment of a logical tool to a channel:

```gcode
BMCU_SET_TOOL DEVICE=bmcu0 CHANNEL=0 TOOL=5 CONFIRM_ORCA=1
```

`CONFIRM_ORCA=1` is required when the T assignment actually changes. Update the OrcaSlicer profile after changing it. If the selected T is already used by another channel, also use `SWAP=1`.

### `BMCU_MAP_TOOL`

Alias for `BMCU_PRINT_MAP`, available only inside an open print plan. Outside a plan, the command returns an error.

```gcode
BMCU_PRINT_BEGIN SCHEMA=1 RESET=1
BMCU_MAP_TOOL TOOL=5 DEVICE=bmcu0 CHANNEL=0
BMCU_PRINT_COMMIT
```

Use `BMCU_SET_TOOL` for persistent assignment.

### `BMCU_SET_ROUTE` / `BMCU_SET_OUTPUT`

Both names perform the same operation: assigning a channel to a printer endpoint.

```gcode
BMCU_SET_ROUTE DEVICE=bmcu0 CHANNEL=0 ENDPOINT=main
BMCU_SET_ROUTE DEVICE=bmcu0 CHANNEL=0 ENDPOINT=NONE
```

Routing changes are blocked if the route is active or the endpoint is occupied.

### `BMCU_SET_PREFERENCES`

Global operating preferences:

```gcode
BMCU_SET_PREFERENCES LEAVE_FINAL_FILAMENT_LOADED=1
```

`1` leaves the last confirmed route loaded after a normal print end. `0` allows the print-end policy to unload it.

---

## 4. Printer endpoints

### `BMCU_SET_ENDPOINT`

Creates or updates an endpoint.

Minimal Generic example:

```gcode
BMCU_SET_ENDPOINT NAME=main DRIVER=generic_single_extruder EXTRUDER=extruder TOOLHEAD_PREPARE_MACRO=BMCU_TOOLHEAD_PREPARE BEFORE_PULLBACK_MACRO=BMCU_BEFORE_PULLBACK
```

Main common fields:

| Parameter | Meaning |
| --- | --- |
| `NAME` | Endpoint name. |
| `DRIVER` | Adapter type, e.g. `generic_single_extruder` or `snapmaker_u1`. |
| `EXTRUDER` | Klipper extruder object. |
| `SELECT_MACRO` | Toolhead/extruder selection macro. |
| `DESELECT_MACRO` | Release/park macro. |
| `VERIFY_MACRO` | Additional selection verification. |
| `ENTRY_SENSOR` | Entry sensor. |
| `POST_GEARS_SENSOR` | Sensor after the extruder gears. |
| `MOTION_SENSOR` | Motion sensor. |
| `SENSOR_POLICY` | Sensor handling policy. |
| `SHARED_PATH_GROUP` | Group of endpoints sharing a physical route. |
| `PRESTAGE` | Allows preparing a source for an inactive endpoint. |
| `PRESTAGE_MM` | Prestage length. |
| `MAX_ROUTE_MM` | Maximum long-transport distance. |
| `CONTACT_BUFFER_PCT` | Endpoint contact/pressure limit. |
| `CONTACT_TIMEOUT` | Arrival timeout. |
| `TAIL_MODE` | Compatibility parameter for older configurations. Generic currently selects the mode automatically: sensor or manual refill after pause. |
| `AUTO_REFILL` | Automatic refill for the endpoint. |
| `REFILL_MODE` | Refill mode. Generic supports `pause`. |
| `REFILL_MATCH` | `exact`, `material`, `group` or `any`. |

For Generic, the most important fields are:

```text
TOOLHEAD_PREPARE_MACRO
BEFORE_PULLBACK_MACRO
```

For Generic, `ENTRY_SENSOR`, `POST_GEARS_SENSOR` or `MOTION_SENSOR` provides physical confirmation that the old filament has ended. A configuration without a sensor pauses the print after the BMCU input is confirmed empty. A ready backup starts automatic refill. Manual same-channel refill uses normal `RESUME`: BMCU performs a full `LOAD`, then resumes the printer after it succeeds.

For U1, additional `SNAP_*` fields are managed by the preset and panel. The full U1 model is described in the [Snapmaker U1 README](../printers/Snapmaker_U1/README.md).

### Generic macro contract

`TOOLHEAD_PREPARE_MACRO` is called as:

```text
MACRO ENDPOINT=<name> MATERIAL=<material> REASON=<load|toolchange|refill>
```

`BEFORE_PULLBACK_MACRO` receives:

```text
MACRO ENDPOINT=<name> MATERIAL=<material> REASON=<unload|toolchange|...>
```

The preparation macro must finish only after the printer has physically taken over the material. The before-pullback macro must finish only when the filament has been safely released from the hotend and extruder.

More examples: [Generic Klipper](../printers/Generic/README.md).

---

## 5. Presets and automatic configuration

### `BMCU_APPLY_PRESET`

Builds configuration from an existing preset.

```gcode
BMCU_APPLY_PRESET PRESET=generic_single_extruder COUNT=1
BMCU_APPLY_PRESET PRESET=snapmaker_u1 COUNT=4
```

Parameters that narrow the operation are also supported, including:

```text
DEVICE CHANNEL ENDPOINT HEAD REPLACE MISSING_ONLY
```

Each preset is tied to a supported topology.

### `BMCU_SETUP`

Convenience layer over presets:

```gcode
BMCU_SETUP AUTO=1
BMCU_SETUP PRESET=generic_single_extruder
```

`AUTO=1` first analyzes the printer and selects the recommended preset.

---

## 6. Manual load, unload and prestage

### `BMCU_LOAD`

Two forms:

```gcode
BMCU_LOAD TOOL=5
```

or:

```gcode
BMCU_LOAD DEVICE=bmcu0 CHANNEL=1
```

Use only one source-selection method in a single command.

`LOAD` performs the full operation through the assigned endpoint. In Generic, after long transport it calls the `Toolhead preparation macro`. On U1, it uses the dedicated head path.

### `BMCU_UNLOAD`

You can specify:

```gcode
BMCU_UNLOAD TOOL=5
```

or:

```gcode
BMCU_UNLOAD DEVICE=bmcu0 CHANNEL=1
```

If multiple routes are loaded, the command requires an unambiguous source selection.

### `BMCU_PRESTAGE`

Prepares a source toward the endpoint without fully loading it:

```gcode
BMCU_PRESTAGE TOOL=5
```

The endpoint must explicitly support prestaging.

### `BMCU_CLEAR_PRESTAGE`

Retracts a prepared source:

```gcode
BMCU_CLEAR_PRESTAGE TOOL=5
BMCU_CLEAR_PRESTAGE DEVICE=bmcu0
```

If one device has more than one prestage, it must be identified unambiguously.

### `BMCU_ROUTE_FEED`

Manually feeds filament along the assigned route without pretending that the operation completed a full load:

```gcode
BMCU_ROUTE_FEED DEVICE=bmcu0 CHANNEL=0 MM=300 BUFFER_LIMIT=90 TIMEOUT=30
```

After this motion, the route becomes `UNCERTAIN`; the next step is to confirm the actual physical state.

### `BMCU_BUFFER_MODE`

Manual control of buffer following:

```gcode
BMCU_BUFFER_MODE DEVICE=bmcu0 CHANNEL=0 MODE=FOLLOW
BMCU_BUFFER_MODE DEVICE=bmcu0 CHANNEL=0 MODE=STOP
BMCU_BUFFER_MODE DEVICE=bmcu0 MODE=STOP
```

`FOLLOW` requires confirmed `LOADED`. `STOP` stops one channel or the entire device.

---

## 7. Recovery and physical route confirmation

### `BMCU_ROUTE_CONFIRM`

Explicitly confirms state after checking the mechanics:

```gcode
BMCU_ROUTE_CONFIRM DEVICE=bmcu0 CHANNEL=0 STATE=EMPTY
BMCU_ROUTE_CONFIRM DEVICE=bmcu0 CHANNEL=0 STATE=PARKED
BMCU_ROUTE_CONFIRM DEVICE=bmcu0 CHANNEL=0 STATE=LOADED
```

Meaning:

- `EMPTY` - the shared route and BMCU input are empty,
- `PARKED` - the printer route is empty, but filament is still parked in BMCU,
- `LOADED` - the channel is physically loaded to its assigned endpoint.

The command performs checks before changing persistent state and works only when the print is stopped and no motion is active.

### `BMCU_ROUTE_RECOVER`

Generic-only recovery for loose filament at the input:

```gcode
BMCU_ROUTE_RECOVER DEVICE=bmcu0 CHANNEL=0 MODE=INPUT_RETRACT
```

The command handles `UNCERTAIN` when downstream is free of a persistent tail and the input can be safely retracted.

### `BMCU_HEAD_CONFIRM_EMPTY`

Dedicated Snapmaker U1 empty-head confirmation:

```gcode
BMCU_HEAD_CONFIRM_EMPTY HEAD=2
BMCU_HEAD_CONFIRM_EMPTY ENDPOINT=u1_head1
```

The command extends confirmation with U1 state and path-ownership checks.

### `BMCU_RECONCILE`

Attempts to reconcile saved BMCU state with the current device state:

```gcode
BMCU_RECONCILE
BMCU_RECONCILE DEVICE=bmcu0
```

This is a recovery tool. With incomplete data, the state remains blocked until the route is explicitly reconciled.

---

## 8. Print plan

### `BMCU_PRINT_BEGIN`

Opens a new atomic plan:

```gcode
BMCU_PRINT_BEGIN SCHEMA=1 RESET=1
```

Optional `JOB=` stores the print identifier.

The public workflow uses `RESET=1` and provides a complete fresh plan for every print.

### `BMCU_PRINT_REQUIRE`

Marks a logical tool as required:

```gcode
BMCU_PRINT_REQUIRE TOOL=5
```

Commit requires a valid source for every tool marked by `BMCU_PRINT_REQUIRE`.

### `BMCU_PRINT_MAP`

Adds a source to the plan.

The simplest form uses the persistent T assignment:

```gcode
BMCU_PRINT_MAP TOOL=5
```

A source can also be specified explicitly:

```text
UID=<stable_device_uid>
CHANNEL=0..3
```

and, in the appropriate context, `DEVICE` or `NATIVE_HEAD`.

Validation parameters:

```text
EXPECT_SPOOL
EXPECT_ENDPOINT
EXPECT_MATERIAL
```

Metadata from the slicer:

```text
MATERIAL
COLOR
```

OrcaSlicer example:

```gcode
BMCU_PRINT_MAP TOOL=5 MATERIAL="{filament_type[5]}" COLOR="{filament_colour[5]}"
```

`MATERIAL` and `COLOR` metadata are assigned to BMCU sources.

### `BMCU_PRINT_COMMIT`

Finishes validation and activates the plan:

```gcode
BMCU_PRINT_COMMIT
```

At this point routing and staged metadata become the active print state. Commit applies the plan atomically.

### `BMCU_PRINT_PREEXTRUDE`

Source preparation/pre-extrusion in the U1 workflow:

```gcode
BMCU_PRINT_PREEXTRUDE TOOL=5
```

It is part of the U1 integration pre-extrusion sequence.

### `BMCU_PRINT_END`

Ends the print session:

```gcode
BMCU_PRINT_END MODE=AUTO CLEAR=1
```

`MODE`:

```text
AUTO
UNLOAD
KEEP
```

The older `UNLOAD` field is also supported. `CLEAR` controls whether the session map is cleared after completion.

---

## 9. Tool changes

### `BMCU_TOOL_CHANGE`

Main command executed by the slicer:

```gcode
BMCU_TOOL_CHANGE TOOL=5
```

The command:

- resolves the source for the logical T,
- delegates native sources to the printer,
- switches BMCU sources,
- performs load/unload according to the endpoint,
- uses prestage/background preparation when the context is ready,
- pauses when the state requires recovery.

### `BMCU_AUTO_FEED`

Snapmaker U1. Replaces the point where stock U1 would normally auto-load the physical head:

```gcode
BMCU_AUTO_FEED EXTRUDER=1 INITIAL_TOOL={initial_extruder} REQUIRE_PLAN=1
```

If the source is a native feeder, the operation is delegated to the stock workflow. If the source is BMCU, the correct BMCU channel is loaded.

### `BMCU_SNAP_CHECK`

Snapmaker U1 endpoint check:

```gcode
BMCU_SNAP_CHECK ENDPOINT=u1_head1 STRICT=1
```

Intended for diagnostics/verification of the U1 integration.

### `BMCU_SNAP_FEEDER`

Controls ownership of a native U1 feeder:

```gcode
BMCU_SNAP_FEEDER ENDPOINT=u1_head1 TAKEOVER=1
```

`TAKEOVER=1` is only a runtime lease and cannot be saved as a persistent setting. Normal U1 configuration should use the panel and preset.

---

## 10. Refill

### `BMCU_REFILL_STATUS`

Shows the current refill state. For Generic without an endpoint sensor, the status may contain a pending manual replacement; in that case, insert filament into the indicated channel and use the printer's normal `RESUME`.

```gcode
BMCU_REFILL_STATUS
```

### `BMCU_REFILL_NOW`

Starts refill for the selected source:

```gcode
BMCU_REFILL_NOW TOOL=5
```

A print must be active, filament-end monitoring must be enabled, and no other refill may already be running.

### `BMCU_PRINT_REFILL`

Declares a backup in the print plan:

```gcode
BMCU_PRINT_REFILL TOOL=5 BACKUP=6 PRIORITY=100
```

`TOOL` and `BACKUP` must point to two different physical channels.

### `BMCU_REFILL_RESUME`

Snapmaker U1 - resumes a saved cross-head refill transaction after the required physical-route verification:

```gcode
BMCU_REFILL_RESUME
```

The command works only when the appropriate persistent recovery journal exists.

### Normal `RESUME` during manual refill

Manual refill of the same channel has no separate command. Insert new filament into the indicated channel and use normal `Resume` in the printer interface. BMCU checks filament, performs a full `LOAD`, and only after success passes control to the original `RESUME`. When no manual refill is pending, `RESUME` works normally.

---

## 11. Firmware motion parameters

### `BMCU_HANDOFF`

Sets the saved threshold for handing off from the loading phase:

```gcode
BMCU_HANDOFF DEVICE=bmcu0 TARGET=82
```

`TARGET` range: 60-98%.

### `BMCU_PRESSURE`

Sets the target loading pressure/buffer:

```gcode
BMCU_PRESSURE DEVICE=bmcu0 TARGET=82 SAVE=1
```

Range: 75-95%.

`SAVE=1` stores the policy in host state and synchronizes runtime. `SAVE=0` changes controller RAM only for the current connection and requires firmware that supports runtime config.

### `BMCU_SPEED`

Transport speeds:

```gcode
BMCU_SPEED DEVICE=bmcu0 LOAD=80 PULL=80 PULL_END=12 SAVE=1
```

Ranges:

| Field | Range |
| --- | --- |
| `LOAD` | 10-120 mm/s |
| `PULL` | 10-120 mm/s |
| `PULL_END` | 4-40 mm/s |

`SAVE=0` works only as temporary RAM configuration for a ready device.

---

## 12. LEDs and lighting

### `BMCU_LED`

Simple change of the main system light:

```gcode
BMCU_LED DEVICE=bmcu0 COLOR=#FFFFFF BRIGHTNESS=128 SAVE=1
```

`BRIGHTNESS`: 0-255.

### `BMCU_LIGHTING`

Full lighting configuration. Main parameters:

```text
DEFAULTS=1
SYSTEM_BRIGHTNESS=0..255
FILAMENT_BRIGHTNESS=0..255
SYSTEM_COLOR=#RRGGBB
BUFFER_MIN_COLOR=#RRGGBB
BUFFER_NEUTRAL_COLOR=#RRGGBB
BUFFER_MAX_COLOR=#RRGGBB
STATUS_IDLE_COLOR=#RRGGBB
STATUS_BEFORE_LOAD_COLOR=#RRGGBB
STATUS_LOADING_COLOR=#RRGGBB
STATUS_ACTIVE_COLOR=#RRGGBB
STATUS_BEFORE_UNLOAD_COLOR=#RRGGBB
STATUS_UNLOADING_COLOR=#RRGGBB
STATUS_ERROR_COLOR=#RRGGBB
STATUS_EMPTY_COLOR=#RRGGBB
SAVE=0|1
APPLY_SAVED=0|1
```

### `BMCU_LIGHTING_PROFILE`

Manages named profiles:

```gcode
BMCU_LIGHTING_PROFILE DEVICE=bmcu0 ACTION=GET
BMCU_LIGHTING_PROFILE DEVICE=bmcu0 PROFILE=Night ACTION=SET SYSTEM_BRIGHTNESS=40
BMCU_LIGHTING_PROFILE DEVICE=bmcu0 PROFILE=Night ACTION=APPLY
BMCU_LIGHTING_PROFILE DEVICE=bmcu0 PROFILE=Night ACTION=DELETE
```

`DEFAULT` is the built-in base profile; custom settings are stored as material profiles.

### `BMCU_LED_PREVIEW`

Preview without persistent saving:

```gcode
BMCU_LED_PREVIEW DEVICE=bmcu0 TARGET=STATUS COLOR=#FF0000 SCALE=255
BMCU_LED_PREVIEW DEVICE=bmcu0 TARGET=FILAMENT SCALE=96
```

`TARGET`:

```text
STATUS
SECOND
BOARD
FILAMENT
```

Preview is blocked during a print and while BMCU motion is active.

---

## 13. Snapmaker U1 tip forming

### `BMCU_TIP_PROFILE`

Manages U1 tip-forming profiles:

```gcode
BMCU_TIP_PROFILE PROFILE=DEFAULT ACTION=GET
BMCU_TIP_PROFILE PROFILE=PLA ACTION=SET DATA=<encoded-data>
BMCU_TIP_PROFILE PROFILE=PLA ACTION=DELETE
BMCU_TIP_PROFILE PROFILE=DEFAULT ACTION=RESET
```

`ACTION`:

```text
GET SET RESET DELETE
```

The panel is the recommended editing method because it validates and previews the sequence.

### `BMCU_U1_GCODE` and `BMCU_U1`

Aliases for the same handler as `BMCU_TIP_PROFILE`. They are kept for compatibility with existing configurations. New macros should use `BMCU_TIP_PROFILE`.

### `BMCU_APPLY_TIP_TEMP`

Internal temperature event used by the prepared tip-forming program:

```gcode
BMCU_APPLY_TIP_TEMP HEAD=1 TARGET=205
```

The command is used for thermal sequences managed by the BMCU U1 integration.

### `BMCU_APPLY_TIP_FAN`

Internal hotend fan event:

```gcode
BMCU_APPLY_TIP_FAN HEAD=1 SPEED=1.0
BMCU_APPLY_TIP_FAN HEAD=1 RESET=1
```

It is allowed only in the correct U1 program context.

#### Directives inside the editable U1 program

The panel also understands:

```text
BMCU_PARK_HEAD
BMCU_TEMP S205
BMCU_TEMP OFFSET=-15
BMCU_TEMP_RESET
BMCU_HOTEND_FAN SPEED=1.0
BMCU_HOTEND_FAN_RESET
```

`BMCU_PARK_HEAD` is a tip-forming sequence marker. It separates moves performed before and after parking the source head.

---

## 14. Package and device management

### `BMCU_PREPARE_UNINSTALL`

Prepares runtime for safe uninstallation. It is used by the `uninstall` script.

```gcode
BMCU_PREPARE_UNINSTALL
```

Normally it is called by the `uninstall` script and is not needed during regular operation.

### `BMCU_FORGET_DEVICE`

Controlled removal of persistent device data. The operation has two phases (`PREPARE` / `CANCEL`) and is used by the panel when removing configuration.

```gcode
BMCU_FORGET_DEVICE DEVICE=bmcu0 ACTION=PREPARE
BMCU_FORGET_DEVICE DEVICE=bmcu0 ACTION=CANCEL
```

Before using it, bring physical routes to a safe, unambiguous state.

### `BMCU_UPDATE_ACCESS`

Internal updater interface for controlled resource shutdown/access during firmware or package updates.

Supported actions:

```text
EXPORT
PREPARE
CANCEL
RESUME
QUIESCE
UNQUIESCE
STATUS
```

It can use `TOKEN` and `RECOVERY` mode. In normal workflow it is called by the updater or panel.

---
