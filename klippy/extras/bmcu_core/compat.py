# SPDX-License-Identifier: GPL-3.0-or-later

import re

_EXTRUDER_RE = re.compile(r'^extruder(?:\d+)?$')
_SENSOR_PREFIXES = ('filament_switch_sensor ', 'filament_motion_sensor ')
_HEATER_PREFIXES = ('heater_generic ',)

def _lookup_names(printer):
    names = []
    try:
        for name, _obj in printer.lookup_objects():
            names.append(str(name))
    except Exception:
        pass
    if names:
        return sorted(set(names))
    try:
        configfile = printer.lookup_object('configfile', None)
        settings = getattr(configfile, 'settings', {}) if configfile is not None else {}
        names.extend(str(name) for name in settings)
    except Exception:
        pass
    return sorted(set(names))

def _extruder_sort_key(name):
    if name == 'extruder':
        return 0
    try:
        return int(name[len('extruder'):]) + 1
    except Exception:
        return 999

def analyze_printer(printer, controller_mode='standalone'):
    names = _lookup_names(printer)
    lower = [name.lower() for name in names]
    extruders = sorted((name for name in names if _EXTRUDER_RE.match(name)),
                       key=_extruder_sort_key)
    sensors = sorted(name for name in names if name.startswith(_SENSOR_PREFIXES))
    heaters = sorted(set(extruders + [
        name for name in names if name.startswith(_HEATER_PREFIXES)]))
    macros = sorted(name for name in names if name.startswith('gcode_macro '))
    macro_names = set(name[len('gcode_macro '):].strip().upper() for name in macros)
    is_u1 = ('filament_feed left' in lower and
             'filament_feed right' in lower and
             'print_task_config' in lower)
    topology = 'snapmaker_u1' if is_u1 else 'generic'
    preset = 'snapmaker_u1' if is_u1 else 'generic_single_extruder'
    notes = []
    if sensors:
        notes.append('Filament sensors were detected and may be assigned by endpoint role.')
    if is_u1:
        notes.append('Physical BMCU Output routing to each Snapmaker head must be assigned explicitly.')
    return {
        'topology': topology,
        'recommended_preset': preset,
        'confidence': 'high',
        'supported': True,
        'controller_mode': str(controller_mode or 'standalone').strip().lower(),
        'physical_route_required': is_u1,
        'extruders': extruders,
        'heaters': heaters,
        'filament_sensors': sensors,
        'gcode_macros': macros,
        'gcode_macro_names': sorted(macro_names),
        'external_managers': [],
        'features': {'snapmaker_u1': is_u1},
        'notes': notes,
        'blockers': [],
    }
