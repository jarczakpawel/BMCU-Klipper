# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import logging
import math
import re
import string

import chelper

U1_TIP_GCODE_MAX_BYTES = 8192
U1_TIP_GCODE_MAX_LINES = 96

U1_TIP_MOVEMENT_MAX_BYTES = U1_TIP_GCODE_MAX_BYTES
U1_TIP_MOVEMENT_MAX_LINES = U1_TIP_GCODE_MAX_LINES
U1_TIP_TOTAL_MOTION_MAX_MM = 500.0
U1_TIP_TOTAL_DWELL_MAX_S = 10.0
_U1_TIP_TEMPERATURE_MODES = ('default', 'project', 'custom')
_U1_TIP_PROFILE_MODES = ('movements',)
_U1_TIP_PARK_MARKER = 'BMCU_PARK_HEAD'
_U1_TIP_TEMP_COMMAND = 'BMCU_TEMP'
_U1_TIP_TEMP_RESET = 'BMCU_TEMP_RESET'
_U1_TIP_TEMP_EVENT_COMMAND = 'BMCU_APPLY_TIP_TEMP'
_U1_TIP_HOTEND_FAN_COMMAND = 'BMCU_HOTEND_FAN'
_U1_TIP_HOTEND_FAN_RESET = 'BMCU_HOTEND_FAN_RESET'
_U1_TIP_FAN_EVENT_COMMAND = 'BMCU_APPLY_TIP_FAN'
_U1_TIP_MOVEMENT_KEYS = (
    'rigid_standard', 'soft_standard', 'rigid_fine', 'soft_fine')
_U1_TIP_PROFILE_FIELDS = (
    'mode', 'gcode', 'movements', 'temperature_mode', 'temperature')
_U1_TIP_GCODE_FIELDS = {
    'tip_temp', 'unload_temp', 'soft',
}
_U1_TIP_GCODE_COMMANDS = {
    'MOVE_TO_DISCARD_FILAMENT_POSITION',
    'M104', 'M109', 'M83', 'M400', 'G0', 'G1', 'G4',
    _U1_TIP_TEMP_EVENT_COMMAND, _U1_TIP_FAN_EVENT_COMMAND,
    'CONTROL_RETRACT_ACTION',
}

def u1_tip_gcode_default():
    return (
        '; Tip forming runs while the source head is still attached\n'
        'MOVE_TO_DISCARD_FILAMENT_POSITION\n'
        'M109 S{tip_temp}\n'
        'CONTROL_RETRACT_ACTION SOFT={soft}\n'
        'M400\n'
        '; After M400 U1 may park the head; BMCU then performs the long pullback'
    )

def u1_tip_snapmaker_movement_defaults():

    return {
        'rigid_standard': (
            'G1 E57 F400\n'
            'G1 E3 F1500\n'
            'G1 E-27 F2700\n'
            'G1 E-5.5 F40\n'
            'G1 E-37.5 F1500'),
        'soft_standard': (
            'G1 E5 F600\n'
            'G1 E-27 F2700\n'
            'G1 E-5.5 F40\n'
            'G1 E-37.5 F1500'),
        'rigid_fine': (
            'G1 E57 F210\n'
            'G1 E3 F300\n'
            'G1 E-27 F2700\n'
            'G1 E-5.5 F40\n'
            'G1 E-37.5 F1500'),
        'soft_fine': (
            'G1 E5 F60\n'
            'G1 E-27 F2700\n'
            'G1 E-5.5 F40\n'
            'G1 E-37.5 F1500'),
    }

def u1_tip_movement_defaults():
    defaults = u1_tip_snapmaker_movement_defaults()
    defaults['rigid_standard'] = (
        'BMCU_TEMP OFFSET=-10\n'
        'G1 E30 F600\n'
        'BMCU_PARK_HEAD\n'
        'G1 E-15 F1800\n'
        'G1 E-22 F2400\n'
        'G1 E-6.2 F1200\n'
        'G1 E-3.2 F720\n'
        'G1 E20 F600\n'
        'G1 E-20 F470\n'
        'G1 E46.4 F1740\n'
        'G1 E-46.4 F2400\n'
        'G1 E20 F340\n'
        'BMCU_TEMP S0\n'
        'G1 E-20 F210\n'
        'G1 E-21.1 F2700')
    defaults['rigid_fine'] = defaults['rigid_standard'].replace(
        'G1 E30 F600', 'G1 E30 F350', 1)
    defaults['soft_standard'] = defaults['soft_standard'].replace(
        'G1 E5 F600\n', 'G1 E5 F600\nBMCU_PARK_HEAD\n', 1)
    defaults['soft_fine'] = defaults['soft_fine'].replace(
        'G1 E5 F60\n', 'G1 E5 F60\nBMCU_PARK_HEAD\n', 1)
    return defaults

def u1_load_gcode_default():

    return (
        'MOVE_TO_DISCARD_FILAMENT_POSITION\n'
        'M204 S10000\n'
        'M109 S{load_temp}\n'
        'M83\n'
        'INNER_APPLY_FLOW_K EXTRUDER={extruder} APPLY=1\n'
        'G1 E{length} F{speed}\n'
        'INNER_APPLY_FLOW_K EXTRUDER={extruder} APPLY=0\n'
        'G1 E-0.500\n'
        'M400\n'
        'M106 S255\n'
        'G4 P500\n'
        'M107\n'
        'M109 S{load_temp}\n'
        'M400\n'
        'INNER_CUTOFF_BASE_DISCARD\n'
        'INNER_DISCARD_FILAMENT_BASE_DISCARD\n'
        'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=4'
    )

def u1_tip_profile_defaults():
    return {
        'mode': 'movements',
        'gcode': u1_tip_gcode_default(),
        'movements': u1_tip_movement_defaults(),
        'temperature_mode': 'project',
        'temperature': None,
    }

def normalize_u1_material_name(material):
    material = str(material or '').strip().upper()
    if (not material or len(material) > 40 or
            re.fullmatch(r'[A-Z0-9][A-Z0-9 ._+/-]*', material) is None):
        raise EndpointError(
            'material name must use 1..40 letters, numbers, space, dot, +, /, _ or -')
    return material

def _u1_tip_lines(script):
    script = str(script or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not script:
        raise EndpointError('U1 tip-forming G-code cannot be empty')
    if len(script.encode('utf-8')) > U1_TIP_GCODE_MAX_BYTES:
        raise EndpointError('U1 tip-forming G-code exceeds %d bytes' %
                            U1_TIP_GCODE_MAX_BYTES)
    lines = script.split('\n')
    if len(lines) > U1_TIP_GCODE_MAX_LINES:
        raise EndpointError('U1 tip-forming G-code exceeds %d lines' %
                            U1_TIP_GCODE_MAX_LINES)
    return script, lines

def validate_u1_tip_gcode(script):
    script, lines = _u1_tip_lines(script)
    formatter = string.Formatter()
    executable_template = '\n'.join(
        raw for raw in lines
        if raw.strip() and not raw.strip().startswith((';', '#')))
    try:
        for _literal, field, format_spec, conversion in formatter.parse(
                executable_template):
            if field is None:
                continue
            if (field not in _U1_TIP_GCODE_FIELDS or conversion or
                    format_spec):
                raise EndpointError(
                    'unsupported U1 tip-forming placeholder {%s}' % field)
    except ValueError as exc:
        raise EndpointError(
            'invalid U1 tip-forming placeholder syntax: %s' % exc)

    commands = []
    at_discard = False
    temperature_ready = False
    relative_e = False
    negative_e = False
    release_action = False
    total_motion = 0.0
    total_dwell = 0.0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(';') or line.startswith('#'):
            continue
        if ';' in line or '\x00' in line or any(ord(ch) < 32 for ch in line):
            raise EndpointError(
                'inline comments and control characters are not allowed')
        command = line.split(None, 1)[0].upper()
        if command not in _U1_TIP_GCODE_COMMANDS:
            raise EndpointError(
                'U1 tip-forming command %s is not allowed' % command)
        commands.append(command)
        upper = line.upper()

        if command == 'MOVE_TO_DISCARD_FILAMENT_POSITION':
            if len(line.split()) != 1:
                raise EndpointError(
                    'MOVE_TO_DISCARD_FILAMENT_POSITION accepts no parameters')
            at_discard = True
            continue
        if command == 'M83':
            if len(line.split()) != 1:
                raise EndpointError('M83 accepts no parameters')
            relative_e = True
            continue
        if command == 'M400':
            if len(line.split()) != 1:
                raise EndpointError('M400 accepts no parameters')
            continue
        if command == _U1_TIP_TEMP_EVENT_COMMAND:

            parts = line.split()
            if len(parts) != 3:
                raise EndpointError(
                    'BMCU_APPLY_TIP_TEMP requires HEAD=1..4 TARGET=<temperature>')
            parsed = {}
            for token in parts[1:]:
                match = re.fullmatch(
                    r'(HEAD|TARGET)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))',
                    token, re.IGNORECASE)
                if match is None or match.group(1).upper() in parsed:
                    raise EndpointError(
                        'BMCU_APPLY_TIP_TEMP requires HEAD=1..4 TARGET=<temperature>')
                parsed[match.group(1).upper()] = float(match.group(2))
            head = parsed.get('HEAD')
            target = parsed.get('TARGET')
            if (head is None or int(head) != head or not 1 <= int(head) <= 4 or
                    target is None or not math.isfinite(target)):
                raise EndpointError(
                    'BMCU_APPLY_TIP_TEMP requires HEAD=1..4 TARGET=<temperature>')
            if not at_discard or not temperature_ready:
                raise EndpointError(
                    'BMCU_APPLY_TIP_TEMP is valid only inside the prepared U1 tip program')
            continue
        if command == _U1_TIP_FAN_EVENT_COMMAND:

            parts = line.split()
            if len(parts) != 3:
                raise EndpointError(
                    'BMCU_APPLY_TIP_FAN requires HEAD=1..4 and SPEED=<0..1> or RESET=1')
            parsed = {}
            for token in parts[1:]:
                match = re.fullmatch(
                    r'(HEAD|SPEED|RESET)=([+]?(?:\d+(?:\.\d*)?|\.\d+))',
                    token, re.IGNORECASE)
                if match is None or match.group(1).upper() in parsed:
                    raise EndpointError(
                        'BMCU_APPLY_TIP_FAN requires HEAD=1..4 and SPEED=<0..1> or RESET=1')
                parsed[match.group(1).upper()] = float(match.group(2))
            head = parsed.get('HEAD')
            speed = parsed.get('SPEED')
            reset = parsed.get('RESET')
            valid_mode = ((speed is not None and reset is None and
                           math.isfinite(speed) and 0.0 <= speed <= 1.0) or
                          (speed is None and reset == 1.0))
            if (head is None or int(head) != head or not 1 <= int(head) <= 4 or
                    not valid_mode):
                raise EndpointError(
                    'BMCU_APPLY_TIP_FAN requires HEAD=1..4 and SPEED=<0..1> or RESET=1')
            if not at_discard or not temperature_ready:
                raise EndpointError(
                    'BMCU_APPLY_TIP_FAN is valid only inside the prepared U1 tip program')
            continue
        if command in ('M104', 'M109'):
            parts = line.split()
            if len(parts) != 2:
                raise EndpointError(
                    '%s requires one S temperature' % command)
            value = parts[1]
            if value.lower() not in ('s{tip_temp}', 's{unload_temp}'):
                match = re.fullmatch(
                    r'S([+]?(?:\d+(?:\.\d*)?|\.\d+))',
                    value, re.IGNORECASE)
                if match is None or not 170.0 <= float(match.group(1)) <= 300.0:
                    raise EndpointError(
                        '%s temperature must use {tip_temp}/{unload_temp} or 170..300 C' %
                        command)
            if command == 'M109':
                if not at_discard:
                    raise EndpointError(
                        'M109 must run at the discard position, not above the print')
                temperature_ready = True
            continue
        if command == 'CONTROL_RETRACT_ACTION':
            parts = line.split()
            if (len(parts) != 2 or
                    not re.fullmatch(r'SOFT=(?:\{soft\}|[01])',
                                     parts[1], re.IGNORECASE)):
                raise EndpointError(
                    'CONTROL_RETRACT_ACTION accepts only SOFT={soft}, SOFT=0 or SOFT=1')
            if not at_discard:
                raise EndpointError(
                    'CONTROL_RETRACT_ACTION must run after the discard-position move')
            if not temperature_ready:
                raise EndpointError(
                    'CONTROL_RETRACT_ACTION must run after M109')
            release_action = True
            continue
        if command in ('G0', 'G1'):
            if re.search(r'(?:^|\s)[XYZABC][^\s]*', upper):
                raise EndpointError(
                    'editable G0/G1 may use only E and F axes')
            tokens = upper.split()[1:]
            values = {}
            for token in tokens:
                match = re.fullmatch(
                    r'([EF])([+-]?(?:\d+(?:\.\d*)?|\.\d+))', token)
                if match is None or match.group(1) in values:
                    raise EndpointError(
                        'editable G0/G1 accepts one E and optional F value')
                values[match.group(1)] = float(match.group(2))
            if 'E' not in values:
                raise EndpointError('editable G0/G1 requires an E distance')
            if not relative_e:
                raise EndpointError(
                    'editable extrusion must use relative mode (M83) first')
            if not at_discard:
                raise EndpointError(
                    'all tip-forming extrusion must run at the discard position')
            if not temperature_ready:
                raise EndpointError(
                    'all tip-forming extrusion must run after M109')
            if 'F' in values and not 0.0 < values['F'] <= 12000.0:
                raise EndpointError(
                    'editable extrusion feed must be 0..12000 mm/min')
            total_motion += abs(values['E'])
            if total_motion > U1_TIP_TOTAL_MOTION_MAX_MM:
                raise EndpointError(
                    'tip-forming G-code exceeds %.1f mm total E motion' %
                    U1_TIP_TOTAL_MOTION_MAX_MM)
            negative_e = negative_e or values['E'] < 0.0
            continue
        if command == 'G4':
            if not at_discard:
                raise EndpointError(
                    'G4 dwell must run at the discard position, not above the print')
            if not temperature_ready:
                raise EndpointError(
                    'G4 dwell must run after M109')
            parts = upper.split()
            if len(parts) != 2:
                raise EndpointError('G4 requires one P or S value')
            match = re.fullmatch(
                r'([PS])([+]?(?:\d+(?:\.\d*)?|\.\d+))', parts[1])
            if match is None:
                raise EndpointError('G4 requires one P or S value')
            dwell = float(match.group(2))
            if match.group(1) == 'P':
                dwell /= 1000.0
            total_dwell += dwell
            if total_dwell > U1_TIP_TOTAL_DWELL_MAX_S:
                raise EndpointError(
                    'tip-forming G-code exceeds %.1f seconds total dwell' %
                    U1_TIP_TOTAL_DWELL_MAX_S)

    if not commands:
        raise EndpointError('U1 tip-forming G-code has no executable commands')
    if 'MOVE_TO_DISCARD_FILAMENT_POSITION' not in commands:
        raise EndpointError(
            'tip-forming G-code must move the active head to the discard position')
    if 'M109' not in commands:
        raise EndpointError(
            'tip-forming G-code must wait for the selected temperature with M109')
    if not release_action and not negative_e:
        raise EndpointError(
            'tip-forming G-code must release the filament tip')
    if commands[-1] != 'M400':
        raise EndpointError(
            'tip-forming G-code must finish with M400 before U1 parks the head')
    return script

def _u1_tip_temperature_directive(line):

    text = str(line or '').strip()
    upper = text.upper()
    if upper == _U1_TIP_TEMP_RESET:
        return {'kind': 'temp_reset'}
    if not upper.startswith(_U1_TIP_TEMP_COMMAND):
        return None
    parts = text.split()
    if len(parts) != 2 or parts[0].upper() != _U1_TIP_TEMP_COMMAND:
        raise EndpointError(
            'BMCU_TEMP requires exactly S<temperature> or OFFSET=<delta>')
    token = parts[1]
    match = re.fullmatch(
        r'S([+-]?(?:\d+(?:\.\d*)?|\.\d+))', token, re.IGNORECASE)
    if match is not None:
        value = float(match.group(1))
        if not math.isfinite(value):
            raise EndpointError('BMCU_TEMP absolute temperature is invalid')
        return {'kind': 'temp_set', 'mode': 'absolute', 'value': value}
    match = re.fullmatch(
        r'OFFSET=([+-]?(?:\d+(?:\.\d*)?|\.\d+))',
        token, re.IGNORECASE)
    if match is not None:
        value = float(match.group(1))
        if not math.isfinite(value):
            raise EndpointError('BMCU_TEMP offset is invalid')
        return {'kind': 'temp_set', 'mode': 'offset', 'value': value}
    raise EndpointError(
        'BMCU_TEMP requires S<temperature> or OFFSET=<delta>')

def _u1_tip_temperature_target(directive, base_tip_temp):
    base = float(base_tip_temp)
    if not math.isfinite(base):
        raise EndpointError('U1 base tip temperature is invalid')
    if directive.get('kind') == 'temp_reset':
        return base
    mode = str(directive.get('mode', '') or '')
    value = float(directive.get('value'))
    target = value if mode == 'absolute' else base + value
    if not math.isfinite(target):
        raise EndpointError('BMCU tip temperature target is invalid')
    return target

def _u1_tip_hotend_fan_directive(line):

    text = str(line or '').strip()
    upper = text.upper()
    if upper == _U1_TIP_HOTEND_FAN_RESET:
        return {'kind': 'fan_reset'}
    if not upper.startswith(_U1_TIP_HOTEND_FAN_COMMAND):
        return None
    parts = text.split()
    if len(parts) != 2 or parts[0].upper() != _U1_TIP_HOTEND_FAN_COMMAND:
        raise EndpointError(
            'BMCU_HOTEND_FAN requires exactly SPEED=<0.0..1.0>')
    match = re.fullmatch(
        r'SPEED=([+]?(?:\d+(?:\.\d*)?|\.\d+))',
        parts[1], re.IGNORECASE)
    if match is None:
        raise EndpointError(
            'BMCU_HOTEND_FAN requires exactly SPEED=<0.0..1.0>')
    speed = float(match.group(1))
    if not math.isfinite(speed) or speed < 0.0 or speed > 1.0:
        raise EndpointError('BMCU_HOTEND_FAN SPEED must be between 0.0 and 1.0')
    return {'kind': 'fan_set', 'speed': speed}

def _render_u1_tip_movement_lines(lines, values):

    base = float(values['tip_temp'])
    head = int(values['head'])
    if head < 1 or head > 4:
        raise EndpointError('U1 tip temperature event Head must be 1..4')
    rendered = []
    for raw in lines:
        directive = _u1_tip_temperature_directive(raw)
        if directive is not None:
            target = _u1_tip_temperature_target(directive, base)
            rendered.append(
                '%s HEAD=%d TARGET=%.1f' %
                (_U1_TIP_TEMP_EVENT_COMMAND, head, target))
            continue
        fan_directive = _u1_tip_hotend_fan_directive(raw)
        if fan_directive is not None:
            if fan_directive.get('kind') == 'fan_reset':
                rendered.append(
                    '%s HEAD=%d RESET=1' %
                    (_U1_TIP_FAN_EVENT_COMMAND, head))
            else:
                rendered.append(
                    '%s HEAD=%d SPEED=%.3f' %
                    (_U1_TIP_FAN_EVENT_COMMAND, head,
                     float(fan_directive['speed'])))
            continue
        rendered.append(raw)
    return rendered

def _u1_tip_movement_value(line):
    upper = str(line or '').strip().upper()
    command = upper.split(None, 1)[0] if upper else ''
    if command not in ('G0', 'G1'):
        return None
    values = {}
    for token in upper.split()[1:]:
        match = re.fullmatch(
            r'([EF])([+-]?(?:\d+(?:\.\d*)?|\.\d+))', token)
        if match is None or match.group(1) in values:
            raise EndpointError(
                'movement sets accept exactly one E and one F value per G0/G1')
        values[match.group(1)] = float(match.group(2))
    if 'E' not in values:
        raise EndpointError('movement sets require an E distance')
    if 'F' not in values:
        raise EndpointError('every E movement requires an explicit F feed')
    if values['F'] <= 0.0:
        raise EndpointError('movement feed must be greater than zero')
    return values

def validate_u1_tip_movements(script):

    script, lines = _u1_tip_lines(script)
    if len(script.encode('utf-8')) > U1_TIP_MOVEMENT_MAX_BYTES:
        raise EndpointError(
            'tip-forming movement set exceeds %d bytes' %
            U1_TIP_MOVEMENT_MAX_BYTES)
    executable_line_count = sum(
        1 for raw in lines
        if raw.strip() and not raw.strip().startswith((';', '#')))
    if executable_line_count > U1_TIP_MOVEMENT_MAX_LINES:
        raise EndpointError(
            'tip-forming movement set exceeds %d executable lines' %
            U1_TIP_MOVEMENT_MAX_LINES)

    marker_count = sum(
        1 for raw in lines
        if raw.strip().upper() == _U1_TIP_PARK_MARKER)
    if marker_count > 1:
        raise EndpointError(
            'movement set may contain at most one BMCU_PARK_HEAD marker')

    motion_seen = False
    commands = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(';') or line.startswith('#'):
            continue
        if ';' in line or '\x00' in line or any(ord(ch) < 32 for ch in line):
            raise EndpointError(
                'inline comments and control characters are not allowed')
        upper = line.upper()
        if upper == _U1_TIP_PARK_MARKER:
            commands += 1
            continue
        directive = _u1_tip_temperature_directive(line)
        if directive is not None:
            commands += 1
            continue
        fan_directive = _u1_tip_hotend_fan_directive(line)
        if fan_directive is not None:
            commands += 1
            continue

        command = upper.split(None, 1)[0]
        if command in ('G0', 'G1'):
            values = _u1_tip_movement_value(line)
            if float(values['E']) != 0.0:
                motion_seen = True
        elif command == 'G4':
            parts = upper.split()
            if len(parts) != 2:
                raise EndpointError('G4 requires one P or S value')
            match = re.fullmatch(
                r'([PS])([+]?(?:\d+(?:\.\d*)?|\.\d+))', parts[1])
            if match is None:
                raise EndpointError('G4 requires one P or S value')
        else:
            raise EndpointError(
                'movement sets accept E-only G0/G1, optional G4, '
                'BMCU_TEMP, BMCU_TEMP_RESET, BMCU_HOTEND_FAN, '
                'BMCU_HOTEND_FAN_RESET and an optional BMCU_PARK_HEAD marker')
        commands += 1

    if not commands:
        raise EndpointError('tip-forming movement set cannot be empty')
    if not motion_seen:
        raise EndpointError('tip-forming movement set requires at least one E move')
    return script

def validate_u1_tip_profile(profile):
    if not isinstance(profile, dict):
        raise EndpointError('U1 tip profile must be an object')

    temperature_mode = str(profile.get(
        'temperature_mode', 'project') or 'project').strip().lower()
    temperature = profile.get('temperature')
    movements = profile.get('movements')
    if not isinstance(movements, dict):
        movements = u1_tip_movement_defaults()
    normalized_movements = {}
    defaults = u1_tip_movement_defaults()
    for key in _U1_TIP_MOVEMENT_KEYS:
        candidate = movements.get(key) if isinstance(movements, dict) else None
        normalized_movements[key] = validate_u1_tip_movements(
            candidate or defaults[key])
    if temperature_mode not in _U1_TIP_TEMPERATURE_MODES:
        raise EndpointError(
            'temperature_mode must be default, project or custom')
    if temperature_mode == 'custom':
        temperature = _coerce_finite_float(temperature)
        if temperature is None or temperature < 170.0 or temperature > 300.0:
            raise EndpointError('temperature must be between 170 and 300 C')
    else:
        temperature = None
    return {
        'mode': 'movements',
        'gcode': u1_tip_gcode_default(),
        'movements': normalized_movements,
        'temperature_mode': temperature_mode,
        'temperature': temperature,
    }

def split_u1_tip_movements(profile, values):
    profile = validate_u1_tip_profile(profile)
    key = ('soft_' if bool(values.get('soft')) else 'rigid_') + (
        'fine' if float(values.get('nozzle_diameter', 0.4)) < 0.3
        else 'standard')
    movement_lines = profile['movements'][key].splitlines()
    marker_indexes = [
        index for index, line in enumerate(movement_lines)
        if line.strip().upper() == _U1_TIP_PARK_MARKER]

    if marker_indexes:
        marker_index = marker_indexes[0]
        before_lines = _render_u1_tip_movement_lines(
            movement_lines[:marker_index], values)
        after_lines = _render_u1_tip_movement_lines(
            movement_lines[marker_index + 1:], values)
        before = '\n'.join(before_lines).strip()
        after = '\n'.join(after_lines).strip()
        post_script = 'M83\n%s\nM400' % after
    else:

        before = '\n'.join(_render_u1_tip_movement_lines(
            movement_lines, values)).strip()
        post_script = ''

    pre_script = (
        'MOVE_TO_DISCARD_FILAMENT_POSITION\n'
        'M109 S%d\n'
        'M83\n%s\nM400' % (int(values['tip_temp']), before))
    return pre_script, post_script, key

def parse_u1_parked_tip_tail(post_script):

    operations = []
    for raw in str(post_script or '').splitlines():
        line = raw.strip()
        if not line or line.startswith(';') or line.startswith('#'):
            continue
        upper = line.upper()
        command = upper.split(None, 1)[0]
        if command in ('M83', 'M400'):
            continue
        if command == 'M104':
            parts = upper.split()
            if len(parts) != 2:
                raise EndpointError('post-marker M104 requires one S target')
            match = re.fullmatch(
                r'S([+-]?(?:\d+(?:\.\d*)?|\.\d+))', parts[1])
            if match is None:
                raise EndpointError('post-marker M104 requires one S target')
            operations.append({
                'kind': 'temp_set',
                'mode': 'absolute',
                'value': float(match.group(1)),
            })
            continue
        if command == _U1_TIP_TEMP_EVENT_COMMAND:
            parts = upper.split()
            target = None
            if len(parts) == 3:
                for token in parts[1:]:
                    match = re.fullmatch(
                        r'TARGET=([+-]?(?:\d+(?:\.\d*)?|\.\d+))',
                        token)
                    if match is not None:
                        target = float(match.group(1))
            if target is None or not math.isfinite(target):
                raise EndpointError(
                    'post-marker BMCU_APPLY_TIP_TEMP requires a finite TARGET')
            operations.append({
                'kind': 'temp_set',
                'mode': 'absolute',
                'value': target,
            })
            continue
        if command == _U1_TIP_FAN_EVENT_COMMAND:
            parts = upper.split()
            speed = None
            reset = False
            if len(parts) == 3:
                for token in parts[1:]:
                    speed_match = re.fullmatch(
                        r'SPEED=([+]?(?:\d+(?:\.\d*)?|\.\d+))', token)
                    reset_match = re.fullmatch(r'RESET=1(?:\.0*)?', token)
                    if speed_match is not None:
                        speed = float(speed_match.group(1))
                    elif reset_match is not None:
                        reset = True
            if reset and speed is None:
                operations.append({'kind': 'fan_reset'})
                continue
            if (speed is None or not math.isfinite(speed) or
                    speed < 0.0 or speed > 1.0 or reset):
                raise EndpointError(
                    'post-marker BMCU_APPLY_TIP_FAN requires SPEED=0..1 or RESET=1')
            operations.append({'kind': 'fan_set', 'speed': speed})
            continue
        if command in ('G0', 'G1'):
            values = _u1_tip_movement_value(line)
            distance = float(values['E'])
            if distance != 0.0:
                operations.append({
                    'kind': 'move',
                    'distance': distance,
                    'speed': float(values['F']) / 60.0,
                })
            continue
        if command == 'G4':
            parts = upper.split()
            if len(parts) != 2:
                raise EndpointError('post-marker G4 requires one P or S value')
            match = re.fullmatch(
                r'([PS])([+]?(?:\d+(?:\.\d*)?|\.\d+))', parts[1])
            if match is None:
                raise EndpointError('post-marker G4 requires one P or S value')
            seconds = float(match.group(2))
            if match.group(1) == 'P':
                seconds /= 1000.0
            if seconds > 0.0:
                operations.append({'kind': 'dwell', 'seconds': seconds})
            continue
        raise EndpointError(
            'post-marker tip program accepts only M83, E-only G0/G1, G4, '
            'M104, BMCU_APPLY_TIP_TEMP, BMCU_APPLY_TIP_FAN and M400')
    if not operations:
        raise EndpointError('post-marker tip program cannot be empty')
    return operations

def parse_u1_background_tip_program(profile, values):

    profile = validate_u1_tip_profile(profile)
    key = ('soft_' if bool(values.get('soft')) else 'rigid_') + (
        'fine' if float(values.get('nozzle_diameter', 0.4)) < 0.3
        else 'standard')
    lines = profile['movements'][key].splitlines()
    operations = []
    marker_index = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(';') or line.startswith('#'):
            continue
        upper = line.upper()
        if upper == _U1_TIP_PARK_MARKER:
            if marker_index is not None:
                raise EndpointError('background tip program has multiple park markers')
            marker_index = len(operations)
            continue
        directive = _u1_tip_temperature_directive(line)
        if directive is not None:
            operations.append(copy.deepcopy(directive))
            continue
        fan_directive = _u1_tip_hotend_fan_directive(line)
        if fan_directive is not None:
            operations.append(copy.deepcopy(fan_directive))
            continue
        command = upper.split(None, 1)[0]
        if command in ('G0', 'G1'):
            parsed = _u1_tip_movement_value(line)
            distance = float(parsed['E'])
            if distance == 0.0:
                continue
            operations.append({
                'kind': 'move',
                'distance': distance,
                'speed': float(parsed['F']) / 60.0,
            })
            continue
        if command == 'G4':
            parts = upper.split()
            if len(parts) != 2:
                raise EndpointError('background tip G4 requires one P or S value')
            match = re.fullmatch(
                r'([PS])([+]?(?:\d+(?:\.\d*)?|\.\d+))', parts[1])
            if match is None:
                raise EndpointError('background tip G4 requires one P or S value')
            seconds = float(match.group(2))
            if match.group(1) == 'P':
                seconds /= 1000.0
            if seconds > 0.0:
                operations.append({'kind': 'dwell', 'seconds': seconds})
            continue
        raise EndpointError(
            'background tip program accepts only E-only G0/G1, G4, '
            'BMCU_TEMP, BMCU_TEMP_RESET, BMCU_HOTEND_FAN, '
            'BMCU_HOTEND_FAN_RESET and BMCU_PARK_HEAD')
    if marker_index is None:
        raise EndpointError('background tip program requires BMCU_PARK_HEAD')
    if marker_index <= 0 or marker_index >= len(operations):
        raise EndpointError('background tip park marker must be inside the program')
    if not any(op.get('kind') == 'move' and float(op.get('distance', 0.0)) != 0.0
               for op in operations[:marker_index]):
        raise EndpointError('background tip marker has no E movement before it')
    if not any(op.get('kind') == 'move' and float(op.get('distance', 0.0)) != 0.0
               for op in operations[marker_index:]):
        raise EndpointError('background tip marker has no E movement after it')
    return operations, marker_index, key

def _u1_stock_e_move_profile(move, acceleration):

    distance = abs(float(move['distance']))
    speed = float(move['speed'])
    if distance <= 0.0 or speed <= 0.0 or acceleration <= 0.0:
        raise EndpointError('background tip contains an invalid E move')
    peak = min(speed, math.sqrt(acceleration * distance))
    accel_t = peak / acceleration
    accel_d = peak * peak / (2.0 * acceleration)
    cruise_d = max(0.0, distance - 2.0 * accel_d)
    cruise_t = cruise_d / peak if peak > 0.0 else 0.0
    return {
        'distance': float(move['distance']),
        'start_v': 0.0,
        'cruise_v': peak,
        'accel_t': accel_t,
        'cruise_t': cruise_t,
        'decel_t': accel_t,
    }

def render_u1_tip_movements(profile, values):
    pre_script, post_script, key = split_u1_tip_movements(profile, values)
    if not post_script.strip():
        return validate_u1_tip_gcode(pre_script), key

    combined = pre_script.rsplit('\nM400', 1)[0] + '\n' + post_script
    return validate_u1_tip_gcode(combined), key

def render_u1_tip_gcode(script, values):
    script = validate_u1_tip_gcode(script)
    try:
        rendered = script.format(**values)
    except (KeyError, ValueError, IndexError) as exc:
        raise EndpointError(
            'could not render U1 tip-forming G-code: %s' % exc)
    return validate_u1_tip_gcode(rendered)

def render_u1_load_gcode(values):

    try:
        return u1_load_gcode_default().format(**values)
    except (KeyError, ValueError, IndexError) as exc:
        raise EndpointError('could not render U1 loading G-code: %s' % exc)

def _coerce_finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None

def _normalize_sensor_name(object_name):
    object_name = str(object_name or '').strip()
    if ':' in object_name and ' ' not in object_name:
        object_name = object_name.replace(':', ' ', 1)
    return object_name

def _sensor_argument(object_name):
    object_name = _normalize_sensor_name(object_name)
    parts = object_name.split(None, 1)
    return parts[1] if len(parts) == 2 else object_name

class EndpointError(RuntimeError):
    pass

class Endpoint(object):
    def __init__(self, manager, name, config):
        self.manager = manager
        self.printer = manager.printer
        self.gcode = manager.gcode
        self.name = name
        self.config = dict(config)
        self.driver = self.config.get('driver', 'generic_single_extruder')
        self.locked = False
        self.sensor_restore = {}
        self.runtime_sensor_restore = {}

    def get(self, key, default=None):
        return self.config.get(key, default)

    def update(self, values):
        self.config.update(values)
        self.driver = self.config.get('driver', self.driver)

    def material_operation_kwargs(self, method_name, temperature_profile=None,
                                  **context):

        kwargs = {}
        if method_name == 'refill_prime' and 'exact_match' in context:
            kwargs['exact_match'] = bool(context.get('exact_match'))
        if method_name == 'assist_unload' and 'maximum_mm' in context:
            kwargs['maximum_mm'] = float(context.get('maximum_mm') or 0.0)
        return kwargs

    def shared_path_group(self):
        return str(self.get('shared_path_group', '') or self.name).strip()

    def configured_tail_sensor_role(self):
        for role in ('post_gears_sensor', 'motion_sensor', 'entry_sensor'):
            if str(self.get(role, '') or '').strip():
                return role
        return ''

    def tail_tracking_mode(self):
        mode = str(self.get('tail_tracking_mode', 'auto') or 'auto').lower()
        if self.driver == 'generic_single_extruder':
            return 'sensor' if self.configured_tail_sensor_role() else 'manual'
        if mode not in ('auto', 'sensor', 'distance'):
            mode = 'auto'
        if mode == 'auto':
            return 'sensor' if self.configured_tail_sensor_role() else 'distance'
        return mode

    def tail_sensor_role(self):
        return (self.configured_tail_sensor_role()
                if self.tail_tracking_mode() == 'sensor' else '')

    def uses_distance_tail_tracking(self):
        return self.tail_tracking_mode() == 'distance'

    def verify_prestage_safe(self):

        for role in ('entry_sensor', 'post_gears_sensor'):
            if self.get(role, '') and self.sensor_detected(role) is True:
                raise EndpointError('%s is not empty: %s is active' %
                                    (self.name, role))
        return True

    def wait_prestage_safe(self, timeout=0.0):
        return self.verify_prestage_safe()

    def delegates_long_unload_to_feeder(self):
        return False

    def prepare_prestage(self):
        self.verify_prestage_safe()
        if self.driver == 'generic_single_extruder':

            return None
        self.run_macro('prepare_prestage_macro', endpoint=self.name,
                       head=self.get('head_index', -1))

    @staticmethod
    def _script_requires_printer_priority(script):
        for raw_line in str(script or '').splitlines():
            line = raw_line.split(';', 1)[0].strip().upper()
            if not line:
                continue
            command = line.split(None, 1)[0]
            if ((command.startswith('T') and command[1:].isdigit()) or
                    command in ('G0', 'G1', 'G2', 'G3', 'G28', 'G29', 'M400',
                                'ACTIVATE_EXTRUDER') or
                    command.startswith(('PARK_EXTRUDER', 'PICK_EXTRUDER',
                                        'MOVE_TO_', 'INNER_', 'ROUGHLY_CLEAN_',
                                        'CLEAN_')) or
                    command == 'SM_PRINT_AUTO_FEED' or
                    (command == 'FEED_AUTO' and
                     (' LOAD=1' in (' ' + line) or
                      ' UNLOAD=1' in (' ' + line)))):
                return True
        return False

    def _run(self, script, wait_moves=True):
        if not script:
            return

        def execute():
            self.gcode.run_script_from_command(script)
            if not wait_moves:
                return
            toolhead = self.printer.lookup_object('toolhead', None)
            if toolhead is not None:
                toolhead.wait_moves()

        critical = self._script_requires_printer_priority(script)
        guard = getattr(self.manager, 'printer_critical_section', None)
        if critical and callable(guard):
            with guard('endpoint:%s' % self.name):
                execute()
            return
        execute()

    def run_macro(self, key, **values):
        macro = str(self.get(key, '') or '').strip()
        if not macro:
            return
        try:
            script = macro.format(**values)
        except Exception:
            script = macro
        self._run(script)

    def _select_command_name(self):
        macro = str(self.get('select_macro', '') or '').strip()
        return macro.split(None, 1)[0].upper() if macro else ''

    def _command_exists(self, name):
        if not name:
            return False
        for attr in ('ready_gcode_handlers', 'base_gcode_handlers', 'commands'):
            handlers = getattr(self.gcode, attr, None)
            if isinstance(handlers, dict) and name in handlers:
                return True
        analysis = getattr(self.manager, 'printer_analysis', {}) or {}
        return name in set(analysis.get('gcode_macro_names', ()))

    def select(self):
        command = self._select_command_name()
        if command.startswith('T') and command[1:].isdigit():
            tool = int(command[1:])
            if tool in getattr(self.manager, '_registered_tools', set()):
                raise EndpointError(
                    "%s select_macro %s resolves to BMCU's own tool command" %
                    (self.name, command))
        if bool(self.get('require_select_macro', False)) and not self._command_exists(command):
            raise EndpointError('%s requires existing printer command %s' %
                                (self.name, command or '<empty>'))
        self.run_macro('select_macro', endpoint=self.name,
                       head=self.get('head_index', -1))

    def verify_selected(self):
        verify_macro = self.get('verify_selected_macro', '')
        if verify_macro:
            self.run_macro('verify_selected_macro', endpoint=self.name,
                           head=self.get('head_index', -1))
        if bool(self.get('verify_active_extruder', False)):
            expected = str(self.get('expected_active_extruder', '') or '').strip()
            toolhead = self.printer.lookup_object('toolhead', None)
            if toolhead is None:
                raise EndpointError('%s cannot verify active extruder: toolhead is unavailable' % self.name)
            status = toolhead.get_status(self.manager.reactor.monotonic())
            active = str((status or {}).get('extruder', '') or '').strip()
            if expected and active != expected:
                raise EndpointError('%s expected %s but toolhead reports extruder %s' %
                                    (self.name, expected, active or '<unknown>'))
        return True

    def release_endpoint(self):
        self.run_macro('deselect_macro', endpoint=self.name,
                       head=self.get('head_index', -1))

    def _heater_status(self):
        heater_name = str(
            self.get('heater', self.get('extruder', 'extruder')) or '').strip()
        heater = self.printer.lookup_object(heater_name, None)
        if heater is None and heater_name:
            heaters = self.printer.lookup_object('heaters', None)
            lookup = getattr(heaters, 'lookup_heater', None)
            if callable(lookup):
                try:
                    heater = lookup(heater_name.split()[-1])
                except Exception:
                    heater = None
        if heater is None:
            return None
        try:
            status = heater.get_status(self.manager.reactor.monotonic())
            return dict(status or {})
        except Exception:
            return None

    def _heater_temperature(self):
        status = self._heater_status()
        return (_coerce_finite_float(status.get('temperature'))
                if status is not None else None)

    def capture_heater_target(self):
        status = self._heater_status()
        return (_coerce_finite_float(status.get('target'))
                if status is not None else None)

    @staticmethod
    def _gcode_param(value):
        value = str(value or '')
        if value and not re.search(r'\s|[\"\\]', value):
            return value
        return '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"')

    def restore_heater_target(self, target):
        if self.driver == 'generic_single_extruder':
            raise EndpointError(
                '%s generic Klipper core may not change heater targets; '
                'use Toolhead preparation / Before pullback macros' % self.name)
        target = _coerce_finite_float(target)
        if target is None or target < 0.0 or target > 500.0:
            raise EndpointError(
                '%s cannot restore an invalid heater target' % self.name)
        heater_name = str(
            self.get('heater', self.get('extruder', 'extruder')) or '').strip()
        if not heater_name:
            raise EndpointError('%s heater is unavailable' % self.name)
        heater_command_name = heater_name.split()[-1]
        self._run('SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f' %
                  (self._gcode_param(heater_command_name), target), wait_moves=False)
        return target

    def _ensure_minimum_temperature(self, minimum, wait=True):
        minimum = float(minimum or 0.0)
        if minimum <= 0.0:
            return
        if self.driver == 'generic_single_extruder':
            raise EndpointError(
                '%s generic Klipper core may not control toolhead temperature; '
                'use Toolhead preparation / Before pullback macros' % self.name)
        current = self._heater_temperature()
        if current is not None and current >= minimum - 1.0:
            return
        self._run(('M109' if wait else 'M104') + ' S%.1f' % minimum,
                  wait_moves=bool(wait))

    @staticmethod
    def _generic_macro_name_valid(name):

        return bool(re.match(r'^[A-Za-z_]+$', str(name or '').strip()))

    def _generic_macro_command(self, key, label, required=False):
        macro = str(self.get(key, '') or '').strip()
        if not macro:
            if required:
                raise EndpointError('%s requires %s' % (self.name, label))
            return ''
        if not self._generic_macro_name_valid(macro):
            raise EndpointError(
                '%s %s must use letters and underscores only, got %s' %
                (self.name, label, macro))
        command = macro.upper()
        if not self._command_exists(command):
            raise EndpointError(
                '%s requires existing Klipper macro %s' %
                (self.name, command))
        return command

    def _required_generic_macro(self, key, label):
        return self._generic_macro_command(key, label, required=True)

    def validate_generic_operation_contract(self):

        if self.driver != 'generic_single_extruder':
            return True
        self._required_generic_macro(
            'toolhead_prepare_macro', 'Toolhead preparation macro')
        self._required_generic_macro(
            'before_pullback_macro', 'Before pullback macro')
        for key, label in (
                ('select_macro', 'Select macro'),
                ('deselect_macro', 'Deselect macro'),
                ('verify_selected_macro', 'Verify selected macro'),
                ('refill_pause_macro', 'Refill pause macro'),
                ('refill_resume_macro', 'Refill resume macro')):
            self._generic_macro_command(key, label, required=False)
        return True

    def _run_generic_contract_macro(self, key, label, material='', reason='load'):
        command = self._required_generic_macro(key, label)
        script = '%s ENDPOINT=%s MATERIAL=%s REASON=%s' % (
            command, self._gcode_param(self.name),
            self._gcode_param(material), self._gcode_param(reason))
        self._run(script)

    def prepare_load(self, material=''):
        if self.driver == 'generic_single_extruder':

            return None

        minimum = float(self.get('min_bite_temp', 0.0) or 0.0)
        cold_preload = bool(self.get('cold_preload_allowed', True))
        self._ensure_minimum_temperature(minimum, wait=not cold_preload)
        self.run_macro('prepare_load_macro', endpoint=self.name, material=material)

    def ensure_bite_ready(self, material=''):
        if self.driver == 'generic_single_extruder':
            return None
        minimum = float(self.get('min_bite_temp', 0.0) or 0.0)
        self._ensure_minimum_temperature(minimum, wait=True)
        self.run_macro('before_bite_macro', endpoint=self.name, material=material)

    def prepare_toolhead_for_use(self, material='', reason='load'):

        if self.driver != 'generic_single_extruder':
            raise EndpointError(
                '%s prepare_toolhead_for_use is a generic Klipper contract' %
                self.name)
        return self._run_generic_contract_macro(
            'toolhead_prepare_macro', 'Toolhead preparation macro',
            material=material, reason=reason)

    def prepare_toolhead_for_pullback(self, material='', reason='unload'):

        if self.driver != 'generic_single_extruder':
            raise EndpointError(
                '%s prepare_toolhead_for_pullback is a generic Klipper contract' %
                self.name)
        return self._run_generic_contract_macro(
            'before_pullback_macro', 'Before pullback macro',
            material=material, reason=reason)

    def toolhead_load_mode(self):
        mode = str(self.get('toolhead_load_mode', 'distance') or 'distance').lower()
        return mode if mode in ('distance', 'macro') else 'distance'

    def load_toolhead_macro(self, material=''):
        if self.driver == 'generic_single_extruder':
            raise EndpointError(
                '%s generic Klipper uses prepare_toolhead_for_use' % self.name)
        macro = str(self.get('toolhead_load_macro', '') or '').strip()
        if not macro:
            raise EndpointError('%s toolhead_load_mode=macro but toolhead_load_macro is empty' % self.name)
        self.run_macro('toolhead_load_macro', endpoint=self.name, material=material)

    def load_ready(self, material=''):
        if self.driver == 'generic_single_extruder':
            return None
        self.run_macro('load_ready_macro', endpoint=self.name, material=material)

    def prime(self, material=''):
        if self.driver == 'generic_single_extruder':
            return None
        self.run_macro('purge_macro', endpoint=self.name, material=material)
        self.run_macro('wipe_macro', endpoint=self.name, material=material)
        self.run_macro('prime_macro', endpoint=self.name, material=material)

    def refill_prime(self, material='', exact_match=True):

        if self.driver == 'generic_single_extruder':
            return None
        if exact_match:
            self.run_macro('refill_prime_macro', endpoint=self.name, material=material)
        else:
            self.prime(material)

    def capture_signal(self):

        return None

    def capture_signal_delta(self, start, material=''):
        current = self.capture_signal()
        if start is None or current is None:
            return None
        current_value = _coerce_finite_float(current)
        start_value = _coerce_finite_float(start)
        if current_value is None or start_value is None:
            return None
        return abs(current_value - start_value)

    def capture_signal_ok(self, delta, material=''):
        return False

    def prepare_unload(self, material=''):
        if self.driver == 'generic_single_extruder':

            return None
        self._ensure_minimum_temperature(
            self.get('min_unload_temp', self.get('min_bite_temp', 0.0)),
            wait=True)
        self.run_macro('prepare_unload_macro', endpoint=self.name, material=material)

    def cut_or_form_tip(self, material=''):
        if self.driver == 'generic_single_extruder':
            raise EndpointError(
                '%s generic Klipper uses prepare_toolhead_for_pullback' %
                self.name)
        mode = str(self.get('cutter_mode', 'none')).lower()
        if mode in ('toolhead', 'native', 'upstream'):
            macro = self.get('cut_macro', '')
            if not macro:
                raise EndpointError('%s cutter_mode=%s but cut_macro is empty' % (self.name, mode))
            self.run_macro('cut_macro', endpoint=self.name, material=material)
            self.run_macro('post_cut_macro', endpoint=self.name, material=material)
        else:
            self.run_macro('tip_form_macro', endpoint=self.name, material=material)

    def release_filament(self, material=''):
        if self.driver == 'generic_single_extruder':
            return None
        self.run_macro('release_macro', endpoint=self.name, material=material)

    def verify_release(self, material=''):
        if self.driver == 'generic_single_extruder':
            return True
        self.run_macro('verify_release_macro', endpoint=self.name, material=material)
        return True

    def assist_unload(self, material='', maximum_mm=0.0):

        return {
            'limit_mm': max(0.0, float(maximum_mm or 0.0)),
            'moved_mm': 0.0,
            'sensor_before': self.sensor_detected('entry_sensor'),
            'sensor_after': self.sensor_detected('entry_sensor'),
            'sensor_cleared': self.sensor_detected('entry_sensor') is False,
        }

    def extrude(self, millimeters, feed):
        if self.driver == 'generic_single_extruder':
            raise EndpointError(
                '%s generic Klipper core may not move the printer extruder; '
                'use Toolhead preparation / Before pullback macros' % self.name)
        distance = _coerce_finite_float(millimeters)
        if distance is None:
            raise EndpointError('extrusion distance must be a finite number')
        limit = 50.0
        chunk = max(1.0, limit * 0.95)
        remaining = abs(distance)
        sign = -1.0 if distance < 0.0 else 1.0
        moves = []
        while remaining > 0.00005:
            step = min(remaining, chunk)
            moves.append('G1 E%.4f F%d' % (sign * step, int(feed)))
            remaining -= step
        state_name = 'BMCU_ENDPOINT_MOVE'
        script = '\n'.join([
            'SAVE_GCODE_STATE NAME=%s' % state_name,
            'M83',
        ] + moves + [

            'M400',
            'RESTORE_GCODE_STATE NAME=%s MOVE=0' % state_name,
        ])
        self._run(script, wait_moves=False)

    def retract(self, millimeters, feed):
        distance = _coerce_finite_float(millimeters)
        if distance is None:
            raise EndpointError('retraction distance must be a finite number')
        return self.extrude(-abs(distance), feed)

    def sensor_status(self, role):
        object_name = _normalize_sensor_name(self.get(role, ''))
        if not object_name:
            return None
        sensor = self.printer.lookup_object(object_name, None)
        if sensor is None:
            return None
        try:
            return sensor.get_status(self.manager.reactor.monotonic())
        except Exception:
            logging.exception('BMCU endpoint sensor read failed: %s', object_name)
            return None

    def sensor_detected(self, role):
        status = self.sensor_status(role)
        if not status:
            return None
        value = status.get('filament_detected')
        return None if value is None else bool(value)

    def entry_debug_snapshot(self):

        return {
            'driver': self.driver,
            'detailed': False,
            'detected': self.sensor_detected('entry_sensor'),
        }

    def _sensor_objects(self):
        result = []
        for role in ('entry_sensor', 'post_gears_sensor', 'motion_sensor'):
            object_name = _normalize_sensor_name(self.get(role, ''))
            if object_name and object_name not in result:
                result.append(object_name)
        return result

    def _sensor_status_by_name(self, object_name):
        sensor = self.printer.lookup_object(_normalize_sensor_name(object_name), None)
        if sensor is None:
            return None
        try:
            return sensor.get_status(self.manager.reactor.monotonic())
        except Exception:
            logging.exception('BMCU endpoint sensor read failed: %s', object_name)
            return None

    def _set_sensor_enabled(self, object_name, enabled):
        command = 'SET_FILAMENT_SENSOR SENSOR=%s ENABLE=%d' % (
            _sensor_argument(object_name), 1 if enabled else 0)

        if self.driver == 'snapmaker_u1':
            command += ' SAVE=0'
        self._run(command, wait_moves=False)

    def suspend_managed_sensors(self):
        if str(self.get('sensor_policy', 'managed')).lower() != 'managed':
            return
        for object_name in self._sensor_objects():
            if object_name in self.sensor_restore or object_name in self.runtime_sensor_restore:
                continue
            status = self._sensor_status_by_name(object_name)
            if not status or 'enabled' not in status:
                continue
            enabled = bool(status.get('enabled'))
            self.sensor_restore[object_name] = enabled
            if enabled:
                self._set_sensor_enabled(object_name, False)

    def restore_managed_sensors(self):
        for object_name, enabled in list(self.sensor_restore.items()):
            try:
                self._set_sensor_enabled(object_name, enabled)
            except Exception:
                logging.exception('BMCU failed to restore sensor %s', object_name)
        self.sensor_restore.clear()

    def wants_runtime_sensor_takeover(self, force=False):

        if str(self.get('sensor_policy', 'managed')).lower() != 'managed':
            return False
        if force:
            return True
        if self.driver == 'snapmaker_u1':
            return (bool(self.get('u1_native_feeder_takeover', False)) and
                    bool(self.get('u1_sensor_takeover', True)))
        return (bool(self.get('auto_refill_enabled', False)) or
                bool(self.get('tail_runout_enabled', True)))

    def activate_runtime_sensor_takeover(self, force=False):

        if not self.wants_runtime_sensor_takeover(force=force):
            self.release_runtime_sensor_takeover()
            return
        for object_name in self._sensor_objects():
            if object_name in self.runtime_sensor_restore:
                continue
            if object_name in self.sensor_restore:
                enabled = self.sensor_restore.pop(object_name)
            else:
                status = self._sensor_status_by_name(object_name)
                if not status or 'enabled' not in status:
                    continue
                enabled = bool(status.get('enabled'))
                if enabled:
                    self._set_sensor_enabled(object_name, False)
            self.runtime_sensor_restore[object_name] = enabled

    def ensure_runtime_sensor_takeover(self):
        if not self.runtime_sensor_restore:
            self.activate_runtime_sensor_takeover()
            return
        for object_name in self.runtime_sensor_restore:
            status = self._sensor_status_by_name(object_name)
            if status and bool(status.get('enabled', False)):
                self._set_sensor_enabled(object_name, False)

    def release_runtime_sensor_takeover(self):
        self.restore_managed_sensors()
        for object_name, enabled in list(self.runtime_sensor_restore.items()):
            try:
                self._set_sensor_enabled(object_name, enabled)
            except Exception:
                logging.exception('BMCU failed to restore runtime sensor %s', object_name)
        self.runtime_sensor_restore.clear()

    def supports_background_prestage(self):
        return bool(self.get('prestage_while_unselected', False))

    def capabilities(self):
        return {
            'selectable': bool(self.get('select_macro', '')) or self.driver == 'snapmaker_u1',
            'entry_sensor': bool(self.get('entry_sensor', '')),
            'post_gears_sensor': bool(self.get('post_gears_sensor', '')),
            'motion_sensor': bool(self.get('motion_sensor', '')),
            'cutter_mode': (None if self.driver == 'generic_single_extruder'
                            else str(self.get('cutter_mode', 'none')).lower()),
            'native_filament_manager': bool(self.get('native_filament_manager', False)),
            'cold_preload_allowed': (False if self.driver == 'generic_single_extruder'
                                     else bool(self.get('cold_preload_allowed', True))),
            'toolhead_load_mode': ('macro' if self.driver == 'generic_single_extruder'
                                   else self.toolhead_load_mode()),
            'toolhead_prepare_macro': str(self.get('toolhead_prepare_macro', '') or ''),
            'before_pullback_macro': str(self.get('before_pullback_macro', '') or ''),
            'background_prestage': self.supports_background_prestage(),
            'shared_path_group': self.shared_path_group(),
            'auto_refill': bool(self.get('auto_refill_enabled', False)),
            'tail_runout': bool(self.get('tail_runout_enabled', True)),
            'tail_tracking_mode': self.tail_tracking_mode(),
            'tail_sensor_role': self.tail_sensor_role(),
            'refill_mode': str(self.get('refill_mode', 'pause')).lower(),
            'refill_match': str(self.get('refill_match', 'exact')).lower(),
            'cross_endpoint_refill': bool(
                self.get('cross_endpoint_refill', False) and
                str(self.get('cross_refill_resume_macro', '') or '').strip()),
            'runtime_sensor_takeover': bool(self.runtime_sensor_restore),
        }

    def validate(self):
        errors = []
        warnings = []
        extruder_name = str(self.get('extruder', '') or '').strip()
        if not extruder_name:
            errors.append('extruder object is not configured')
        elif self.printer.lookup_object(extruder_name, None) is None:
            if self.driver not in ('generic_single_extruder', 'snapmaker_u1'):
                errors.append(
                    'printer-owned endpoint requires existing extruder object %s' %
                    extruder_name)
            else:
                warnings.append(
                    'extruder object %s is not currently available' % extruder_name)
        policy = str(self.get('sensor_policy', 'managed')).lower()
        if policy not in ('managed', 'observe', 'external'):
            errors.append('invalid sensor policy %s' % policy)
        configured_tail_mode = str(
            self.get('tail_tracking_mode', 'auto') or 'auto').lower()
        if self.driver != 'generic_single_extruder':
            if configured_tail_mode not in ('auto', 'sensor', 'distance'):
                errors.append('invalid tail tracking mode %s' % configured_tail_mode)
            if (configured_tail_mode == 'sensor' and
                    not self.configured_tail_sensor_role()):
                errors.append(
                    'tail tracking mode sensor requires an endpoint sensor')
        if self.driver == 'generic_single_extruder':
            generic_macros = (
                ('toolhead_prepare_macro', 'Toolhead preparation macro', True),
                ('before_pullback_macro', 'Before pullback macro', True),
                ('select_macro', 'Select macro', False),
                ('deselect_macro', 'Deselect macro', False),
                ('verify_selected_macro', 'Verify selected macro', False),
                ('refill_pause_macro', 'Refill pause macro', False),
                ('refill_resume_macro', 'Refill resume macro', False),
            )
            for key, label, required in generic_macros:
                macro = str(self.get(key, '') or '').strip()
                if not macro:
                    if required:
                        warnings.append(
                            '%s is not configured; automatic BMCU motion is disabled' %
                            label)
                    continue
                if not self._generic_macro_name_valid(macro):
                    errors.append(
                        '%s must use letters and underscores only' % label)
                    continue
                if not self._command_exists(macro.upper()):
                    warnings.append(
                        'Klipper macro %s is not currently available; automatic '
                        'BMCU motion is disabled' % macro.upper())
        else:
            configured_load_mode = str(
                self.get('toolhead_load_mode', 'distance') or 'distance').lower()
            if configured_load_mode not in ('distance', 'macro'):
                errors.append('invalid toolhead load mode %s' % configured_load_mode)
            elif configured_load_mode == 'macro' and not str(
                    self.get('toolhead_load_macro', '') or '').strip():
                errors.append('toolhead load mode macro requires toolhead_load_macro')
        refill_mode = str(self.get('refill_mode', 'pause')).lower()
        if refill_mode not in ('pause', 'continuous'):
            errors.append('invalid refill mode %s' % refill_mode)
        elif (self.driver == 'generic_single_extruder' and
              refill_mode == 'continuous'):
            errors.append(
                'generic Klipper v1 supports pause refill only; toolhead '
                'handoff belongs to Toolhead preparation macro REASON=refill')
        refill_match = str(self.get('refill_match', 'exact')).lower()
        if refill_match not in ('exact', 'material', 'group', 'any'):
            errors.append('invalid refill match %s' % refill_match)
        if self.driver != 'generic_single_extruder':
            mode = str(self.get('cutter_mode', 'none')).lower()
            if mode not in ('none', 'toolhead', 'native', 'upstream'):
                errors.append('invalid cutter mode %s' % mode)
            if mode in ('toolhead', 'upstream') and not self.get('cut_macro', ''):
                errors.append('cutter mode %s requires cut_macro' % mode)
            if mode == 'native' and not (self.get('cut_macro', '') or self.get('native_filament_manager', False)):
                warnings.append('native cutter has no native manager or cut macro')
        if bool(self.get('require_select_macro', False)):
            command = self._select_command_name()
            if not command:
                errors.append('printer-owned endpoint requires select_macro')
            elif not self._command_exists(command):
                errors.append('required printer command %s is not available' % command)
        if bool(self.get('verify_active_extruder', False)) and not str(
                self.get('expected_active_extruder', '') or '').strip():
            errors.append('verify_active_extruder requires expected_active_extruder')
        numeric_ranges = (
            ('prestage_distance_mm', 0.0, 3000.0),
            ('max_route_mm', 10.0, 5000.0),
            ('final_search_mm', 5.0, 1000.0),
            ('contact_buffer_pct', 60.0, 98.0),
            ('contact_timeout', 1.0, 180.0),
            ('prestage_buffer_limit_pct', 55.0, 98.0),
            ('prestage_timeout', 1.0, 180.0),
            ('tail_to_output_mm', 0.0, 5000.0),
            ('sensor_tail_remaining_mm', 0.0, 1000.0),
            ('tail_reserve_mm', 0.0, 500.0),
            ('refill_contact_buffer_pct', 60.0, 98.0),
            ('refill_timeout', 2.0, 300.0),
            ('refill_runout_debounce', 0.0, 5.0),
            ('refill_handoff_max_mm', 5.0, 500.0),
            ('refill_handoff_chunk_mm', 1.0, 30.0),
            ('refill_handoff_feed', 10.0, 3000.0),
        )
        numeric_values = {}
        for key, minimum, maximum in numeric_ranges:
            if key not in self.config:
                continue
            try:
                value = float(self.config[key])
            except (TypeError, ValueError, OverflowError):
                errors.append('%s must be numeric' % key)
                continue
            if not math.isfinite(value):
                errors.append('%s must be a finite number' % key)
                continue
            numeric_values[key] = value
            if value < minimum or value > maximum:
                errors.append('%s must be between %.1f and %.1f' %
                              (key, minimum, maximum))
        if (self.supports_background_prestage() and
                numeric_values.get('prestage_distance_mm', 0.0) <= 0.0):
            errors.append('background prestage requires prestage_distance_mm')
        for role in ('entry_sensor', 'post_gears_sensor', 'motion_sensor'):
            object_name = _normalize_sensor_name(self.get(role, ''))
            if object_name and self.printer.lookup_object(object_name, None) is None:
                warnings.append('%s object %s is not currently available' % (role, object_name))
        has_endpoint_sensor = bool(self.configured_tail_sensor_role())
        distance_tracking = self.tail_tracking_mode() == 'distance'
        if (self.driver == 'generic_single_extruder' and
                bool(self.get('tail_runout_enabled', True)) and
                not has_endpoint_sensor):
            warnings.append(
                'no endpoint filament sensor is configured; runout pauses '
                'immediately and Resume performs a manual same-Channel refill')
            if not self._command_exists('PAUSE') or not self._command_exists('RESUME'):
                warnings.append(
                    'manual Generic runout recovery requires Klipper PAUSE and RESUME commands')
        if bool(self.get('cross_endpoint_refill', False)) and not str(
                self.get('cross_refill_resume_macro', '') or '').strip():
            errors.append(
                'cross_endpoint_refill requires cross_refill_resume_macro')
        if bool(self.get('auto_refill_enabled', False)):
            if (self.driver == 'generic_single_extruder' and
                    not has_endpoint_sensor):
                warnings.append(
                    'Automatic refill is disabled without an endpoint sensor; '
                    'runout uses manual same-Channel refill after PAUSE')
            elif refill_mode == 'continuous':
                if distance_tracking:
                    warnings.append(
                        'continuous distance refill requires explicit route data')
                elif numeric_values.get('tail_to_output_mm', 0.0) <= 0.0:
                    errors.append(
                        'continuous sensor refill requires tail_to_output_mm')
        return {'valid': not errors, 'errors': errors, 'warnings': warnings}

class SnapmakerU1Endpoint(Endpoint):
    supports_print_temperature_profile = True
    def __init__(self, manager, name, config):
        super(SnapmakerU1Endpoint, self).__init__(manager, name, dict(config))
        self._last_runtime_metadata = None
        self._last_runtime_source = None
        self._native_cache_dirty = True
        self._load_temperature_context = None
        self._load_previous_target = None
        self._load_restore_target = None
        self._parked_tip_tail_ticket = None
        self._parked_tip_tail_generation = 0

        self._tip_hotend_fan_override_state = None

        self._entry_sensor_backend_key = None
        self._entry_sensor_backend = None
        self._entry_sensor_backend_pin = ''

        self._entry_sensor_observation_signature = None
        self._entry_sensor_observation_revision = 0
        self._entry_sensor_last_advanced_host = None
        self._entry_sensor_state_signature = None
        self._entry_sensor_state_revision = 0
        self._entry_motion_latch_generation = 0
        self._native_empty_projection = False

    _FEEDER_MAP = {
        0: ('left', 1),
        1: ('left', 0),
        2: ('right', 0),
        3: ('right', 1),
    }

    def material_operation_kwargs(self, method_name, temperature_profile=None,
                                  **context):

        kwargs = super(SnapmakerU1Endpoint, self).material_operation_kwargs(
            method_name, temperature_profile, **context)
        if method_name in (
                'prepare_load', 'ensure_bite_ready', 'load_ready', 'prime',
                'prepare_unload', 'cut_or_form_tip', 'refill_prime',
                'prepare_runout_tail_handoff'):
            kwargs['temperature_profile'] = temperature_profile
        if method_name == 'prepare_load':
            kwargs['discard_position_prepared'] = bool(
                context.get('discard_position_prepared', False))
        if method_name in ('prepare_unload', 'cut_or_form_tip'):
            kwargs['tip_profile'] = context.get('tip_profile')
        if method_name == 'cut_or_form_tip':
            kwargs['park_at_marker'] = bool(
                context.get('park_at_marker', False))
        if method_name == 'prepare_runout_tail_handoff':
            if 'cancel_check' in context:
                kwargs['cancel_check'] = context.get('cancel_check')
            if 'restore_heater' in context:
                kwargs['restore_heater'] = bool(
                    context.get('restore_heater'))
        return kwargs

    def _head(self):
        head = int(self.get('head_index', -1))
        if head < 0 or head > 3:
            raise EndpointError('Snapmaker U1 endpoint requires head_index 0..3')
        return head

    def suspend_managed_sensors(self):

        return

    def restore_managed_sensors(self):
        self.sensor_restore.clear()
        self.runtime_sensor_restore.clear()

    def wants_runtime_sensor_takeover(self, force=False):
        return False

    def activate_runtime_sensor_takeover(self, force=False):
        self.sensor_restore.clear()
        self.runtime_sensor_restore.clear()

    def ensure_runtime_sensor_takeover(self):
        self.sensor_restore.clear()
        self.runtime_sensor_restore.clear()

    def release_runtime_sensor_takeover(self):
        self.sensor_restore.clear()
        self.runtime_sensor_restore.clear()

    def _entry_sensor_object(self):
        object_name = _normalize_sensor_name(self.get('entry_sensor', ''))
        if not object_name:
            return '', None
        return object_name, self.printer.lookup_object(object_name, None)

    def _resolve_entry_sensor_backend(self, object_name, sensor_obj):

        key = (str(object_name or ''), id(sensor_obj))
        buttons = self.printer.lookup_object('buttons', None)
        adc_buttons = getattr(buttons, 'adc_buttons', None)
        if not isinstance(adc_buttons, dict) or sensor_obj is None:
            self._entry_sensor_backend_key = key
            self._entry_sensor_backend = None
            self._entry_sensor_backend_pin = ''
            return None

        def callback_owner(callback):
            owner = getattr(callback, '__self__', None)
            if owner is not None:
                return owner

            return getattr(callback, '_bmcu_motion_sensor_owner', None)

        def matches(backend):
            if backend is None or backend not in adc_buttons.values():
                return False
            for item in list(getattr(backend, 'buttons', ()) or ()):
                callback = (item[2] if isinstance(item, (tuple, list)) and
                            len(item) >= 3 else None)
                if callback_owner(callback) is sensor_obj:
                    return True
            return False

        if (self._entry_sensor_backend_key == key and
                matches(self._entry_sensor_backend)):
            return self._entry_sensor_backend

        self._entry_sensor_backend_key = key
        self._entry_sensor_backend = None
        self._entry_sensor_backend_pin = ''
        for pin, backend in adc_buttons.items():
            if matches(backend):
                self._entry_sensor_backend = backend
                self._entry_sensor_backend_pin = str(pin or '')
                return backend
        return None

    def _record_entry_sensor_observation(
            self, now, backend, adc_timestamp, adc_value,
            adc_detected, adc_candidate, physical):

        try:
            rounded_value = round(float(adc_value), 7)
        except (TypeError, ValueError, OverflowError):
            rounded_value = None
        signature = (
            id(backend) if backend is not None else 0,
            adc_timestamp, rounded_value, adc_detected, adc_candidate)
        if signature != self._entry_sensor_observation_signature:
            self._entry_sensor_observation_signature = signature
            self._entry_sensor_observation_revision += 1
            self._entry_sensor_last_advanced_host = float(now)
        state_signature = (
            id(backend) if backend is not None else 0, physical)
        if physical is not None and state_signature != self._entry_sensor_state_signature:
            self._entry_sensor_state_signature = state_signature
            self._entry_sensor_state_revision += 1
        age = None
        if self._entry_sensor_last_advanced_host is not None:
            age = max(0.0, float(now) -
                      float(self._entry_sensor_last_advanced_host))
        return self._entry_sensor_observation_revision, self._entry_sensor_state_revision, age

    def entry_sensor_snapshot(self):

        now = self.manager.reactor.monotonic()
        object_name, sensor_obj = self._entry_sensor_object()
        if sensor_obj is None:
            return {
                'sensor': object_name, 'available': False,
                'physical_detected': None, 'source': 'missing',
                'fresh': False, 'coherent': False, 'sample_age': None,
            }
        try:
            status = dict(sensor_obj.get_status(now) or {})
        except TypeError:
            status = dict(sensor_obj.get_status() or {})
        except Exception as exc:
            return {
                'sensor': object_name, 'available': False,
                'physical_detected': None, 'source': 'status_error',
                'fresh': False, 'coherent': False, 'sample_age': None,
                'error': str(exc),
            }

        public = status.get('filament_detected')
        public = None if public is None else bool(public)
        enabled = status.get('enabled')
        enabled = None if enabled is None else bool(enabled)
        callback_available = hasattr(sensor_obj, 'runout_buttun_state')
        callback_state = (bool(getattr(sensor_obj, 'runout_buttun_state'))
                          if callback_available else None)
        backend = self._resolve_entry_sensor_backend(object_name, sensor_obj)

        adc_value = None
        adc_timestamp = None
        adc_age = None
        adc_initialized = False
        adc_stable = False
        adc_detected = None
        adc_candidate = None
        debounce_age = None
        if backend is not None:
            mcu_adc = getattr(backend, 'mcu_adc', None)
            try:
                adc_value, adc_timestamp = mcu_adc.get_last_value()
                adc_value = float(adc_value)
                adc_timestamp = float(adc_timestamp)
                adc_initialized = bool(adc_timestamp > 0.0)

                adc_age = max(0.0, now - adc_timestamp)
            except Exception:
                adc_value = adc_timestamp = adc_age = None
                adc_initialized = False

            pressed = getattr(backend, 'last_pressed', None)
            candidate = getattr(backend, 'last_button', None)
            adc_detected = bool(pressed is not None)
            adc_candidate = bool(candidate is not None)
            try:
                debounce_age = max(
                    0.0, now - float(getattr(backend, 'last_debouncetime')))
            except (TypeError, ValueError, OverflowError, AttributeError):
                debounce_age = None

            adc_stable = bool(
                adc_initialized and adc_candidate == adc_detected and
                (debounce_age is None or debounce_age >= 0.025))

        if backend is not None:
            physical = adc_detected if adc_stable else None
            if not adc_initialized:
                source = 'adc_uninitialized'
            elif not adc_stable:
                source = 'adc_debouncing'
            else:
                source = 'adc_debounced'
            coherent = bool(adc_stable)
        elif callback_available:
            physical = callback_state
            source = 'callback_state'
            coherent = True
        else:
            physical = public
            source = 'public_cache'
            coherent = public is not None

        observation_revision, state_revision, observation_age = (
            self._record_entry_sensor_observation(
                now, backend, adc_timestamp, adc_value,
                adc_detected, adc_candidate, physical))

        return {
            'sensor': object_name,
            'available': bool(physical is not None),
            'physical_detected': physical,
            'source': source,

            'fresh': bool(coherent),
            'coherent': bool(coherent),
            'enabled': enabled,
            'public_detected': public,
            'callback_available': bool(callback_available),
            'callback_detected': callback_state,
            'adc_available': bool(backend is not None),
            'adc_initialized': bool(adc_initialized),
            'adc_pin': self._entry_sensor_backend_pin,
            'adc_backend_id': id(backend) if backend is not None else 0,
            'adc_detected': adc_detected,
            'adc_candidate_detected': adc_candidate,
            'adc_value': adc_value,
            'adc_timestamp': adc_timestamp,
            'sample_age': adc_age,
            'sample_age_diagnostic_only': True,
            'debounce_age': debounce_age,
            'observation_revision': observation_revision,
            'state_revision': state_revision,
            'observation_age': observation_age,

            'read_elapsed_ms': max(
                0.0, (self.manager.reactor.monotonic() - now) * 1000.0),
        }

    def entry_debug_snapshot(self):
        snapshot = dict(self.entry_sensor_snapshot() or {})
        snapshot['driver'] = self.driver
        snapshot['detailed'] = True
        return snapshot

    def probe_entry_sensor_stream(self, timeout=0.35):

        timeout = max(0.0, min(1.0, float(timeout)))
        first = self.entry_sensor_snapshot()
        if not first.get('adc_available'):
            result = dict(first)
            result['stream_progressed'] = False
            result['stream_probe_supported'] = False
            return result
        first_timestamp = first.get('adc_timestamp')
        first_revision = int(first.get('observation_revision', 0) or 0)
        deadline = self.manager.reactor.monotonic() + timeout
        last = first
        while self.manager.reactor.monotonic() < deadline:
            now = self.manager.reactor.monotonic()
            self.manager.reactor.pause(min(deadline, now + 0.05))
            last = self.entry_sensor_snapshot()
            if (last.get('adc_timestamp') != first_timestamp or
                    int(last.get('observation_revision', 0) or 0) >
                    first_revision):
                result = dict(last)
                result['stream_progressed'] = True
                result['stream_probe_supported'] = True
                return result
        result = dict(last)
        result['stream_progressed'] = False
        result['stream_probe_supported'] = True
        return result

    def require_entry_sensor_snapshot(self, timeout=1.25):

        deadline = self.manager.reactor.monotonic() + max(0.0, float(timeout))
        last = self.entry_sensor_snapshot()
        while (last.get('adc_available') and not last.get('available') and
               self.manager.reactor.monotonic() < deadline):
            now = self.manager.reactor.monotonic()
            self.manager.reactor.pause(min(deadline, now + 0.05))
            last = self.entry_sensor_snapshot()
        if not last.get('available'):
            if last.get('adc_available'):
                raise EndpointError(
                    'U1_ENTRY_SENSOR_STATE_UNAVAILABLE: %s source=%s '
                    'timestamp=%s debounce_age=%s' %
                    (last.get('sensor') or self.name,
                     last.get('source'),
                     'unknown' if last.get('adc_timestamp') is None else
                     '%.6f' % float(last.get('adc_timestamp')),
                     'unknown' if last.get('debounce_age') is None else
                     '%.3fs' % float(last.get('debounce_age'))))
            raise EndpointError(
                'U1_ENTRY_SENSOR_UNAVAILABLE: %s has no physical sensor state' %
                (last.get('sensor') or self.name))
        return last

    def arm_entry_motion_latch(self, reason=''):

        snapshot = self.require_entry_sensor_snapshot(timeout=1.25)
        if not snapshot.get('adc_available'):
            raise EndpointError(
                'U1 motion sensor has no readable stock ADC backend')
        self._entry_motion_latch_generation += 1
        token = {
            'generation': self._entry_motion_latch_generation,
            'sensor': snapshot.get('sensor'),
            'backend_id': int(snapshot.get('adc_backend_id', 0) or 0),
            'state_revision': int(snapshot.get('state_revision', 0) or 0),
            'accepted_phase': snapshot.get('physical_detected'),
            'callback_phase': snapshot.get('callback_detected'),
            'adc_timestamp': snapshot.get('adc_timestamp'),
            'armed_at': self.manager.reactor.monotonic(),
            'reason': str(reason or '')[:120],
        }
        logging.info(
            'BMCU armed read-only U1 SEND_OUT phase latch %s generation=%d '
            'phase=%s (%s)', snapshot.get('sensor') or self.name,
            token['generation'], token.get('accepted_phase'),
            token.get('reason') or 'arrival')
        return token

    def entry_motion_latch_status(self, token):

        if not isinstance(token, dict):
            raise EndpointError('invalid U1 motion-latch token')
        snapshot = self.entry_sensor_snapshot()
        if not snapshot.get('available') or not snapshot.get('coherent'):
            result = dict(snapshot)
            result.update({'triggered': False, 'backend_changed': False,
                           'event_advanced': False})
            return result
        expected_backend = int(token.get('backend_id', 0) or 0)
        current_backend = int(snapshot.get('adc_backend_id', 0) or 0)
        backend_changed = bool(
            expected_backend and current_backend != expected_backend)
        baseline_revision = int(token.get('state_revision', 0) or 0)
        state_advanced = bool(
            int(snapshot.get('state_revision', 0) or 0) > baseline_revision)
        phase_changed = bool(
            snapshot.get('physical_detected') != token.get('accepted_phase'))
        result = dict(snapshot)
        result.update({
            'triggered': bool(
                not backend_changed and state_advanced and phase_changed),
            'backend_changed': backend_changed,
            'event_advanced': state_advanced,
            'accepted_phase_changed': phase_changed,
            'latch_generation': token.get('generation'),
            'latch_reason': token.get('reason', ''),
            'read_only_observer': True,
        })
        return result

    def synchronize_entry_sensor_cache(self, expected, reason=''):

        snapshot = self.entry_sensor_snapshot()
        snapshot['route_expected'] = bool(expected)
        snapshot['public_projection_skipped'] = True
        snapshot['projection_reason'] = str(reason or '')[:160]
        return snapshot

    def sensor_detected(self, role):

        if role in ('entry_sensor', 'motion_sensor'):
            return self.entry_sensor_snapshot().get('physical_detected')
        return super(SnapmakerU1Endpoint, self).sensor_detected(role)

    def native_feeder_target(self):
        head = self._head()
        default_module, default_channel = self._FEEDER_MAP[head]
        module = str(self.get('u1_feeder_module', default_module) or default_module).strip()
        channel = int(self.get('u1_feeder_channel', default_channel))
        if module not in ('left', 'right') or channel not in (0, 1):
            raise EndpointError('invalid U1 feeder mapping module=%s channel=%s' %
                                (module, channel))
        return module, channel

    def native_feeder_takeover_enabled(self):
        return bool(self.get('u1_native_feeder_takeover', False))

    def native_feeder_confirmation_required(self):
        return bool(self.get('u1_require_feeder_confirmation', True))

    def _native_feeder_object(self, strict=False):
        module, _channel = self.native_feeder_target()
        obj = self.printer.lookup_object('filament_feed %s' % module, None)
        if obj is None and strict:
            raise EndpointError(
                'U1 native feeder object filament_feed %s is unavailable' % module)
        return obj

    def _native_feeder_status(self, strict=False):
        obj = self._native_feeder_object(strict=strict)
        if obj is None:
            return None
        try:
            status = obj.get_status(self.manager.reactor.monotonic())
        except TypeError:
            status = obj.get_status()
        head = self._head()
        state = status.get('extruder%d' % head)
        if state is None and strict:
            raise EndpointError(
                'U1 native feeder status for head %d is unavailable' % head)
        return state

    def restore_heater_target(self, target):
        target = _coerce_finite_float(target)
        if target is None or target < 0.0 or target > 500.0:
            raise EndpointError(
                'U1 head %d cannot restore an invalid heater target' %
                self._head())

        self._run('M104 T%d S%.1f A0' % (self._head(), target),
                  wait_moves=False)
        return target

    def move_to_safe_wait(self):

        self._run('MOVE_TO_XY_IDLE_POSITION_EXTRUDER\nM400',
                  wait_moves=False)

    def native_source_preflight(self, expected_enabled=None,
                                require_filament=True):
        state = self._native_feeder_status(strict=True)
        if not bool(state.get('module_exist', True)):
            raise EndpointError(
                'U1 native feeder module for head %d is unavailable' %
                self._head())
        if expected_enabled is None:
            expected_enabled = not bool(state.get('disable_auto', False))
        if not bool(expected_enabled):
            raise EndpointError(
                'U1 native feeder for head %d was disabled before BMCU takeover' %
                self._head())
        if require_filament and not bool(state.get('filament_detected', False)):
            raise EndpointError(
                'U1 native feeder for head %d has no filament at its input' %
                self._head())
        return dict(state)

    def native_feeder_load(self, printing=True):
        module, channel = self.native_feeder_target()
        self._run('FEED_AUTO MODULE=%s CHANNEL=%d LOAD=1 PRINTING=%d' %
                  (module, channel, 1 if printing else 0))
        state = self._native_feeder_status(strict=True)
        if str(state.get('channel_state', '') or '').strip().lower() != 'load_finish':
            raise EndpointError(
                'U1 native feeder did not confirm LOADED for head %d' %
                self._head())
        self._native_empty_projection = False
        return state

    def reconcile_entry_sensor_empty_after_pullback(
            self, pullback=None, expected_retract_mm=0.0,
            require_park=True, allow_verified_cache_reconcile=False):

        if self.driver != 'snapmaker_u1':
            raise EndpointError(
                'entry-sensor verification is only valid on Snapmaker')

        pullback = pullback if isinstance(pullback, dict) else {}
        measured_mm = _coerce_finite_float(pullback.get('encoder_mm'))

        if require_park:
            park_timeout = _coerce_finite_float(self.get(
                'u1_prestage_park_timeout', 20.0))
            park_timeout = 20.0 if park_timeout is None else park_timeout
            self.wait_parked_for_prestage(
                min(max(park_timeout, 0.0), 60.0))
        else:
            self.verify_selected()

        verified = self.synchronize_entry_sensor_cache(
            False, reason='BMCU firmware completed signed pullback')
        return {
            'sensor': verified.get('sensor'),
            'detected': False,
            'motion_sensor_source': verified.get('source'),
            'final_motion_phase': verified.get('motion_phase'),
            'adc_value': verified.get('adc_value'),
            'sample_age': verified.get('sample_age'),
            'measured_mm': measured_mm,
            'required_park': bool(require_park),
            'reconciled': True,
            'reconcile_basis': 'firmware_completed_signed_pullback',
            'hardware_verified': False,
            'motion_sensor_phase_ignored': True,
        }

    def commit_native_path_empty(self, sensor_snapshot=None,
                                 manual_confirmation=False,
                                 route_empty_verified=False):

        if not manual_confirmation and not route_empty_verified:
            raise EndpointError(
                'U1 head %d EMPTY projection requires completed route evidence '
                'or explicit operator confirmation' % self._head())

        self.synchronize_entry_sensor_cache(
            False, reason=('operator-confirmed route EMPTY'
                           if manual_confirmation else
                           'verified complete route EMPTY'))

        module, channel = self.native_feeder_target()
        obj = self._native_feeder_object(strict=True)
        config = getattr(obj, 'config', None)
        config_path = str(getattr(obj, 'config_path', '') or '')
        if not isinstance(config, dict) or not config_path:
            raise EndpointError(
                'U1 native feeder %s has no persistent configuration' % module)
        load_finish = config.get('load_finish')
        if not isinstance(load_finish, list) or len(load_finish) != 2:
            raise EndpointError(
                'U1 native feeder %s load_finish schema is invalid' % module)

        previous_flag = bool(load_finish[channel])
        previous_state = None
        channel_state = getattr(obj, 'channel_state', None)
        if isinstance(channel_state, list) and len(channel_state) == 2:
            previous_state = channel_state[channel]

        previous_projection = self._native_empty_projection
        load_finish[channel] = False
        if previous_state is not None:
            channel_state[channel] = 'unload_finish'
        try:
            self.manager._persist_u1_json(
                config_path, config,
                'Snapmaker U1 native feeder EMPTY state for head %d' %
                self._head())
        except Exception:
            load_finish[channel] = previous_flag
            if previous_state is not None:
                channel_state[channel] = previous_state
            self._native_empty_projection = previous_projection
            raise

        self._native_empty_projection = True
        state = self._native_feeder_status(strict=True)
        if str(state.get('channel_state', '') or '').strip().lower() == 'load_finish':
            raise EndpointError(
                'U1 native feeder EMPTY state did not update for head %d' %
                self._head())
        return previous_flag

    def set_native_feeder_enabled(self, enabled, save=False):
        module, channel = self.native_feeder_target()
        strict = self.native_feeder_confirmation_required()

        if strict:
            self._native_feeder_status(strict=True)
        self._run('FEED_AUTO MODULE=%s CHANNEL=%d AUTO=%d SAVE=%d' %
                  (module, channel, 1 if enabled else 0, 1 if save else 0),
                  wait_moves=False)
        state = self._native_feeder_status(strict=strict)
        if state is not None:
            disabled = bool(state.get('disable_auto', False))
            expected_disabled = not enabled
            if disabled != expected_disabled:
                raise EndpointError(
                    'U1 native feeder state did not change for head %d' % self._head())
        return state

    def native_path_status(self, sensor_snapshot=None,
                           route_empty_verified=False):

        state = self._native_feeder_status()
        if state is None:
            return {
                'known': False, 'busy': True, 'channel_state': 'unknown',
                'entry_detected': None, 'feeder_input_detected': None,
                'verified_empty_projection': False,
                'input_released': False,
            }
        channel_state = str(state.get('channel_state', '') or '').strip().lower()
        feeder_input = state.get('filament_detected')
        feeder_input = None if feeder_input is None else bool(feeder_input)
        if channel_state != 'unload_finish':
            self._native_empty_projection = False
        verified_projection = bool(
            self._native_empty_projection and channel_state == 'unload_finish')
        inherently_safe = channel_state in ('', 'none', 'inited', 'wait_insert')
        input_released = bool(
            feeder_input is False and channel_state in (
                'load_finish', 'unload_finish', 'preload_finish'))
        busy = not (inherently_safe or verified_projection or input_released)
        stale_load_finish = bool(
            channel_state == 'load_finish' and route_empty_verified)
        return {
            'known': True, 'busy': bool(busy),
            'channel_state': channel_state or 'none',
            'entry_detected': None,
            'feeder_input_detected': feeder_input,
            'auto_disabled': bool(state.get('disable_auto', False)),
            'stale_load_finish': stale_load_finish,
            'verified_empty_projection': verified_projection,
            'input_released': input_released,
            'manual_retract_required': bool(
                busy and channel_state in (
                    'load_finish', 'unload_finish', 'preload_finish')),
        }

    def ensure_native_feeder_takeover(self, save=False, allow_occupied=False):
        if not self.native_feeder_takeover_enabled():
            return
        state = self._native_feeder_status()
        if state is not None and bool(state.get('disable_auto', False)):
            return
        path = self.native_path_status()
        if path.get('busy') and not allow_occupied:
            raise EndpointError(
                'U1 native path for head %d is occupied (%s); manually retract the stock filament before BMCU takeover' %
                (self._head(), path.get('channel_state', 'unknown')))
        self.set_native_feeder_enabled(False, save=save)

    def restore_native_feeder(self, save=False, enabled=None):

        if enabled is None:
            raise EndpointError(
                'U1 feeder restore requires an explicit captured baseline')
        state = self.set_native_feeder_enabled(bool(enabled), save=save)
        if enabled:
            self._native_empty_projection = False

        if enabled and self.printer.lookup_object(
                'filament_detect', None) is not None:
            try:
                command = ('FILAMENT_DT_UPDATE' if state and
                           bool(state.get('filament_detected', False)) else
                           'FILAMENT_DT_CLEAR')
                self._run('%s CHANNEL=%d' % (command, self._head()),
                          wait_moves=False)
            except Exception:
                logging.exception('BMCU could not refresh native U1 filament metadata')
        return state

    @staticmethod
    def _safe_token(value, fallback):
        value = re.sub(r'[^A-Za-z0-9_.+-]+', '_', str(value or '').strip())
        return value[:48] or fallback

    @staticmethod
    def _ensure_runtime_list(config, key, length, default):
        values = config.get(key)
        if not isinstance(values, list):
            values = []
        while len(values) < length:
            values.append(default() if callable(default) else default)
        config[key] = values
        return values

    def _task_config(self, strict=True):
        task = self.printer.lookup_object('print_task_config', None)
        config = getattr(task, 'print_task_config', None) if task is not None else None
        required = {
            'filament_vendor': 4, 'filament_type': 4,
            'filament_sub_type': 4, 'filament_soft': 4,
            'filament_color': 4, 'filament_color_rgba': 4,
            'filament_color_multi': 4, 'filament_official': 4,
            'filament_sku': 4, 'filament_exist': 4,
            'filament_edit': 4, 'extruder_map_table': 32,
            'extruders_used': 4, 'extruders_replenished': 4,
        }
        error = None
        if not isinstance(config, dict):
            error = 'U1 print_task_config is unavailable'
        elif not callable(getattr(task, 'backup_filament_info', None)):
            error = 'U1 print_task_config backup API is unavailable'
        else:
            for key, minimum in required.items():
                values = config.get(key)
                if not isinstance(values, list) or len(values) < minimum:
                    error = 'unsupported U1 print_task_config schema: %s length must be >= %d' % (key, minimum)
                    break
            if error is None:
                flag_fields = ('filament_soft', 'filament_official',
                               'filament_exist', 'filament_edit')
                for head in range(4):
                    for key in ('filament_vendor', 'filament_type',
                                'filament_sub_type'):
                        if not isinstance(config[key][head], str):
                            error = 'unsupported U1 print_task_config schema: %s[%d] must be a string' % (key, head)
                            break
                    if error is not None:
                        break
                    for key in flag_fields:
                        value = config[key][head]
                        if not isinstance(value, (bool, int)) or int(value) not in (0, 1):
                            error = 'unsupported U1 print_task_config schema: %s[%d] must be boolean' % (key, head)
                            break
                    if error is not None:
                        break
                    color = config['filament_color'][head]
                    rgba = config['filament_color_rgba'][head]
                    sku = config['filament_sku'][head]
                    replacement = config['extruders_replenished'][head]
                    if not isinstance(color, int) or isinstance(color, bool) or not 0 <= color <= 0xFFFFFFFF:
                        error = 'unsupported U1 print_task_config schema: filament_color[%d] is invalid' % head
                        break
                    if not isinstance(rgba, str) or re.fullmatch(r'[0-9A-Fa-f]{8}', rgba) is None:
                        error = 'unsupported U1 print_task_config schema: filament_color_rgba[%d] is invalid' % head
                        break
                    if not isinstance(sku, int) or isinstance(sku, bool) or sku < 0:
                        error = 'unsupported U1 print_task_config schema: filament_sku[%d] is invalid' % head
                        break
                    if not isinstance(replacement, int) or isinstance(replacement, bool) or not 0 <= replacement < 4:
                        error = 'unsupported U1 print_task_config schema: extruders_replenished[%d] is invalid' % head
                        break
                    value = config['filament_color_multi'][head]
                    if not isinstance(value, dict):
                        error = 'unsupported U1 print_task_config schema: filament_color_multi[%d] must be an object' % head
                        break
                    nums = value.get('nums')
                    alpha = value.get('alpha')
                    mode = value.get('mode')
                    colors = value.get('colors')
                    if (not isinstance(nums, int) or isinstance(nums, bool) or
                            not 1 <= nums <= 5 or
                            not isinstance(alpha, int) or isinstance(alpha, bool) or
                            not 0 <= alpha <= 255 or
                            not isinstance(mode, int) or isinstance(mode, bool) or
                            not 0 <= mode <= 255 or
                            not isinstance(colors, list) or len(colors) < nums or
                            any(not isinstance(item, str) or
                                re.fullmatch(r'[0-9A-Fa-f]{6}', item) is None
                                for item in colors[:nums])):
                        error = 'unsupported U1 print_task_config schema: filament_color_multi[%d] is invalid' % head
                        break
                if error is None:
                    for logical, physical in enumerate(config['extruder_map_table'][:32]):
                        if (not isinstance(physical, int) or isinstance(physical, bool) or
                                not 0 <= physical < 4):
                            error = 'unsupported U1 print_task_config schema: extruder_map_table[%d] is invalid' % logical
                            break
                if error is None:
                    for head, used in enumerate(config['extruders_used'][:4]):
                        if not isinstance(used, (bool, int)) or int(used) not in (0, 1):
                            error = 'unsupported U1 print_task_config schema: extruders_used[%d] must be boolean' % head
                            break
        if error is not None:
            if strict:
                raise EndpointError(error)
            return None, None
        return task, config

    @staticmethod
    def _backup_head(task, head):
        try:
            task.backup_filament_info(head)
        except TypeError:
            task.backup_filament_info()

    @staticmethod
    def _projection_tuple(config, head):
        value = config['filament_color_multi'][head]
        colors = tuple(str(item or '').lstrip('#').upper()
                       for item in value.get('colors', [])[:5])
        return (
            str(config['filament_vendor'][head]),
            str(config['filament_type'][head]),
            str(config['filament_sub_type'][head]),
            bool(config['filament_soft'][head]),
            int(config['filament_color'][head]),
            str(config['filament_color_rgba'][head]).upper(),
            int(value.get('nums', 0) or 0),
            int(value.get('alpha', 0) or 0),
            colors,
            int(value.get('mode', 0) or 0) & 0xFF,
            bool(config['filament_official'][head]),
            int(config['filament_sku'][head]),
            bool(config['filament_exist'][head]),
            bool(config['filament_edit'][head]),
        )

    @staticmethod
    def _projection_color_coherent(config, head):
        rgba = str(config['filament_color_rgba'][head]).upper()
        multi = config['filament_color_multi'][head]
        colors = multi.get('colors', [])
        if not colors:
            return False
        primary = str(colors[0]).upper()
        alpha = int(multi.get('alpha', -1))
        if rgba[:6] != primary or rgba[6:8] != '%02X' % alpha:
            return False
        expected = (alpha << 24) | int(primary, 16)
        return int(config['filament_color'][head]) == expected

    @staticmethod
    def _projection_fields():
        return (
            'filament_vendor', 'filament_type', 'filament_sub_type',
            'filament_soft', 'filament_color', 'filament_color_rgba',
            'filament_color_multi', 'filament_official', 'filament_sku',
            'filament_exist', 'filament_edit',
        )

    def _wait_native_filament_quiescent(self, required=None):
        if required is None:
            required = self.native_feeder_takeover_enabled()
        detector = self.printer.lookup_object('filament_detect', None)
        if detector is None:
            if required:
                raise EndpointError('U1 filament_detect is unavailable')
            return False
        get_status = getattr(detector, 'get_status', None)
        if not callable(get_status):
            if required:
                raise EndpointError('U1 filament_detect status API is unavailable')
            return False
        try:
            timeout = float(self.get('u1_rfid_quiesce_timeout', 3.0) or 3.0)
        except (TypeError, ValueError):
            raise EndpointError('invalid u1_rfid_quiesce_timeout')
        if not 0.1 <= timeout <= 10.0:
            raise EndpointError('u1_rfid_quiesce_timeout must be 0.1..10.0 seconds')
        reactor = self.manager.reactor
        deadline = reactor.monotonic() + timeout
        idle_reads = 0
        while True:
            eventtime = reactor.monotonic()
            try:
                status = get_status(eventtime)
            except TypeError:
                status = get_status()
            states = status.get('state') if isinstance(status, dict) else None
            if not isinstance(states, (list, tuple)) or len(states) < 4:
                raise EndpointError('unsupported U1 filament_detect state schema')
            try:
                state = int(states[self._head()])
            except (TypeError, ValueError):
                raise EndpointError('invalid U1 filament_detect state')
            if state == 0:
                idle_reads += 1
                if idle_reads >= 2:
                    return True
            else:
                idle_reads = 0
            if eventtime >= deadline:
                raise EndpointError('U1 RFID detector did not become idle for head %d' % self._head())
            reactor.pause(min(deadline, eventtime + 0.05))

    def _clear_native_filament_cache(self):
        if not self._native_cache_dirty:
            return False
        detector = self.printer.lookup_object('filament_detect', None)
        if detector is None:
            if self.native_feeder_takeover_enabled():
                raise EndpointError('U1 filament_detect is unavailable')
            self._native_cache_dirty = False
            return False
        self._run('FILAMENT_DT_CLEAR CHANNEL=%d' % self._head(),
                  wait_moves=False)
        try:
            self._wait_native_filament_quiescent(required=True)
        except Exception:
            self._native_cache_dirty = True
            raise
        self._native_cache_dirty = False
        return True

    def sync_active_filament(self, metadata):
        head = self._head()
        self._wait_native_filament_quiescent()

        raw_material = str(metadata.get('material', '') or '').strip()
        material = ('Unknown' if not raw_material or
                    raw_material.upper() in ('NONE', 'UNKNOWN') else
                    raw_material)
        vendor = str(metadata.get('vendor', '') or 'generic').strip() or 'generic'
        subtype = str(metadata.get('subtype', '') or
                      metadata.get('profile_id', '') or
                      'generic').strip() or 'generic'
        color = str(metadata.get('color', '#FFFFFF')).lstrip('#').upper()
        if re.fullmatch(r'[0-9A-F]{6}', color) is None:
            color = 'FFFFFF'

        raw_colors = metadata.get('colors', [])
        if not isinstance(raw_colors, (list, tuple)):
            raw_colors = []
        color_list = [color]
        for item in raw_colors:
            item = str(item or '').lstrip('#').upper()
            if re.fullmatch(r'[0-9A-F]{6}', item) and item not in color_list:
                color_list.append(item)
            if len(color_list) == 5:
                break
        try:
            color_mode = int(metadata.get('color_mode', 0) or 0) & 0xFF
        except (TypeError, ValueError):
            color_mode = 0

        soft = material.upper().startswith('TPU') or material.upper() in ('TPE', 'FLEX')
        parameters = self.printer.lookup_object('filament_parameters', None)
        if parameters is not None and hasattr(parameters, 'get_is_soft'):
            try:
                soft = bool(parameters.get_is_soft(vendor, material, subtype))
            except Exception:
                logging.exception('BMCU could not query U1 soft-filament metadata')

        task, config = self._task_config(strict=True)
        desired = (
            vendor, material, subtype, bool(soft),
            (0xFF << 24) | int(color, 16), color + 'FF',
            len(color_list), 255, tuple(color_list), color_mode,
            False, 0, True, True,
        )
        current = self._projection_tuple(config, head)
        if current == desired:
            self._last_runtime_metadata = desired
            self._last_runtime_source = copy.deepcopy(metadata)
            self._native_cache_dirty = True
            logging.info(
                'BMCU Head %d filament projection already current: '
                'material=%s vendor=%s subtype=%s soft=%d',
                head + 1, material, vendor, subtype, 1 if soft else 0)
            return False

        snapshot = {key: copy.deepcopy(config[key][head])
                    for key in self._projection_fields()}
        material_changed = current[:4] != desired[:4]
        try:
            config['filament_vendor'][head] = vendor
            config['filament_type'][head] = material
            config['filament_sub_type'][head] = subtype
            config['filament_soft'][head] = bool(soft)
            config['filament_color'][head] = desired[4]
            config['filament_color_rgba'][head] = desired[5]
            config['filament_color_multi'][head] = {
                'nums': len(color_list), 'alpha': 255,
                'colors': list(color_list), 'mode': color_mode,
            }
            config['filament_official'][head] = False
            config['filament_sku'][head] = 0
            config['filament_exist'][head] = True
            config['filament_edit'][head] = True
            self._backup_head(task, head)
            if self._projection_tuple(config, head) != desired:
                raise EndpointError('U1 filament projection readback mismatch for head %d' % head)
            if material_changed:
                self._run('FLOW_RESET_K EXTRUDER=%d' % head, wait_moves=False)
        except Exception:
            for key, value in snapshot.items():
                config[key][head] = value
            try:
                self._backup_head(task, head)
            except Exception:
                logging.exception('BMCU could not restore U1 filament backup after projection failure')
            raise

        self._last_runtime_metadata = desired
        self._last_runtime_source = copy.deepcopy(metadata)
        self._native_cache_dirty = True
        logging.info(
            'BMCU projected Head %d filament: material=%s vendor=%s subtype=%s soft=%d',
            head + 1, material, vendor, subtype, 1 if soft else 0)
        return True

    def capture_active_filament_state(self):
        task, config = self._task_config(strict=True)
        head = self._head()
        return {
            'head': head,
            'projection': {
                key: copy.deepcopy(config[key][head])
                for key in self._projection_fields()
            },
            'projection_tuple': self._projection_tuple(config, head),
            'runtime_metadata': copy.deepcopy(self._last_runtime_metadata),
            'runtime_source': copy.deepcopy(self._last_runtime_source),
            'native_cache_dirty': bool(self._native_cache_dirty),
        }

    def restore_active_filament_state(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get('head') != self._head():
            raise EndpointError('invalid U1 filament projection snapshot')
        projection = snapshot.get('projection')
        target_tuple = snapshot.get('projection_tuple')
        if not isinstance(projection, dict) or not isinstance(target_tuple, tuple):
            raise EndpointError('invalid U1 filament projection snapshot')
        fields = self._projection_fields()
        if any(key not in projection for key in fields):
            raise EndpointError('incomplete U1 filament projection snapshot')

        task, config = self._task_config(strict=True)
        head = self._head()
        current_projection = {
            key: copy.deepcopy(config[key][head]) for key in fields
        }
        current_tuple = self._projection_tuple(config, head)
        current_runtime_metadata = copy.deepcopy(self._last_runtime_metadata)
        current_runtime_source = copy.deepcopy(self._last_runtime_source)
        current_cache_dirty = bool(self._native_cache_dirty)
        changed = (
            current_tuple != target_tuple or
            current_runtime_metadata != snapshot.get('runtime_metadata') or
            current_runtime_source != snapshot.get('runtime_source') or
            current_cache_dirty != bool(snapshot.get('native_cache_dirty', False))
        )
        try:
            if current_tuple != target_tuple:
                for key in fields:
                    config[key][head] = copy.deepcopy(projection[key])
                self._backup_head(task, head)
                if self._projection_tuple(config, head) != target_tuple:
                    raise EndpointError(
                        'U1 filament projection rollback readback mismatch for head %d' %
                        head)
            self._last_runtime_metadata = copy.deepcopy(
                snapshot.get('runtime_metadata'))
            self._last_runtime_source = copy.deepcopy(
                snapshot.get('runtime_source'))
            self._native_cache_dirty = bool(
                snapshot.get('native_cache_dirty', False))
        except Exception:
            for key in fields:
                config[key][head] = current_projection[key]
            try:
                self._backup_head(task, head)
            except Exception:
                logging.exception(
                    'BMCU could not restore current U1 projection after rollback failure')
            self._last_runtime_metadata = current_runtime_metadata
            self._last_runtime_source = current_runtime_source
            self._native_cache_dirty = current_cache_dirty
            raise
        return changed

    def clear_active_filament(self):
        task, config = self._task_config(strict=False)
        if task is None:
            previous_source = self._last_runtime_source
            self._last_runtime_source = None
            try:
                changed = self._clear_native_filament_cache()
            except Exception:
                self._last_runtime_source = previous_source
                raise
            self._last_runtime_metadata = None
            return changed

        head = self._head()
        empty = (
            'NONE', 'NONE', 'NONE', False, 0xFFFFFFFF, 'FFFFFFFF',
            1, 255, ('FFFFFF',), 0, False, 0, False, True,
        )
        snapshot = {key: copy.deepcopy(config[key][head])
                    for key in self._projection_fields()}
        previous_source = self._last_runtime_source
        previous_metadata = self._last_runtime_metadata
        previous_cache_dirty = self._native_cache_dirty
        projection_changed = self._projection_tuple(config, head) != empty
        self._last_runtime_source = None
        try:

            cache_changed = self._clear_native_filament_cache()

            config['filament_vendor'][head] = 'NONE'
            config['filament_type'][head] = 'NONE'
            config['filament_sub_type'][head] = 'NONE'
            config['filament_soft'][head] = False
            config['filament_color'][head] = 0xFFFFFFFF
            config['filament_color_rgba'][head] = 'FFFFFFFF'
            config['filament_color_multi'][head] = {
                'nums': 1, 'alpha': 255, 'colors': ['FFFFFF'], 'mode': 0,
            }
            config['filament_official'][head] = False
            config['filament_sku'][head] = 0
            config['filament_exist'][head] = False
            config['filament_edit'][head] = True
            self._backup_head(task, head)
            if self._projection_tuple(config, head) != empty:
                raise EndpointError(
                    'Snapmaker filament clear readback mismatch for head %d' % head)
        except Exception:
            for key, value in snapshot.items():
                config[key][head] = value
            try:
                self._backup_head(task, head)
            except Exception:
                logging.exception(
                    'BMCU could not restore Snapmaker filament backup after clear failure')
            self._native_cache_dirty = previous_cache_dirty
            self._last_runtime_source = previous_source
            self._last_runtime_metadata = previous_metadata
            raise

        self._last_runtime_metadata = None
        return bool(projection_changed or cache_changed)

    def runtime_filament_metadata(self):
        if self._last_runtime_source is None:
            return None
        return copy.deepcopy(self._last_runtime_source)

    def _native_hotend_sequences_enabled(self):
        return bool(self.get('u1_native_hotend_sequences', True))

    def _filament_context(self, material='', temperature_profile=None):
        head = self._head()
        task = self.printer.lookup_object('print_task_config', None)
        config = getattr(task, 'print_task_config', {}) if task is not None else {}

        task_material = ''
        task_vendor = 'generic'
        task_subtype = 'generic'
        task_soft = None
        if isinstance(config, dict):
            def item(name, fallback):
                values = config.get(name, [])
                if isinstance(values, (list, tuple)) and head < len(values):
                    return values[head]
                return fallback
            task_material = str(item('filament_type', '') or '').strip()
            task_vendor = str(item('filament_vendor', 'generic') or 'generic')
            task_subtype = str(item('filament_sub_type', 'generic') or 'generic')
            task_soft = bool(item('filament_soft', False))

        requested_material = str(material or '').strip()
        raw_material = requested_material or task_material or 'Unknown'
        if raw_material.upper() in ('NONE', 'UNKNOWN'):
            main_type = 'UNKNOWN'
        else:
            try:
                main_type = normalize_u1_material_name(raw_material)
            except EndpointError:
                logging.warning(
                    'BMCU invalid U1 material %r; using Snapmaker UNKNOWN profile',
                    raw_material)
                main_type = 'UNKNOWN'

        task_key = ''
        if task_material and task_material.upper() not in ('NONE', 'UNKNOWN'):
            try:
                task_key = normalize_u1_material_name(task_material)
            except EndpointError:
                task_key = ''

        vendor = task_vendor if task_key == main_type else 'generic'
        sub_type = task_subtype if task_key == main_type else 'generic'
        soft = (main_type.startswith('TPU') or main_type in ('TPE', 'FLEX'))
        if task_key == main_type and task_soft is not None:
            soft = bool(task_soft)

        parameters = self.printer.lookup_object('filament_parameters', None)
        material_load_temp = _coerce_finite_float(
            self.get('u1_load_temp', 250.0) or 250.0)
        material_unload_temp = _coerce_finite_float(
            self.get('u1_unload_temp', 250.0) or 250.0)
        material_flow_temp = 220.0
        material_load_temp = (material_load_temp if material_load_temp is not None
                              and 0.0 <= material_load_temp <= 350.0 else 250.0)
        material_unload_temp = (material_unload_temp if material_unload_temp is not None
                                and 0.0 <= material_unload_temp <= 350.0 else 250.0)
        if parameters is not None:
            try:
                candidate_load = _coerce_finite_float(
                    parameters.get_load_temp(vendor, main_type, sub_type))
                candidate_unload = _coerce_finite_float(
                    parameters.get_unload_temp(vendor, main_type, sub_type))
                candidate_flow = _coerce_finite_float(
                    parameters.get_flow_temp(vendor, main_type, sub_type))
                if (candidate_load is None or candidate_unload is None or
                        candidate_flow is None or
                        not 0.0 <= candidate_load <= 350.0 or
                        not 0.0 <= candidate_unload <= 350.0 or
                        not 0.0 <= candidate_flow <= 350.0):
                    raise ValueError(
                        'non-finite or out-of-range U1 filament temperature')
                material_load_temp = candidate_load
                material_unload_temp = candidate_unload
                material_flow_temp = candidate_flow

                soft = bool(soft or parameters.get_is_soft(
                    vendor, main_type, sub_type))
            except Exception:
                logging.exception(
                    'BMCU could not query U1 filament parameters; using safe defaults')

        nozzle = 0.4
        extruder = self.printer.lookup_object(
            str(self.get('extruder', 'extruder')), None)
        heater = getattr(extruder, 'heater', None) if extruder is not None else None
        if extruder is not None:
            candidate_nozzle = _coerce_finite_float(
                getattr(extruder, 'nozzle_diameter', nozzle))
            if candidate_nozzle is not None and 0.05 <= candidate_nozzle <= 2.0:
                nozzle = candidate_nozzle

        safe_minimum = _coerce_finite_float(
            getattr(heater, 'min_extrude_temp', None))
        safe_maximum = _coerce_finite_float(getattr(heater, 'max_temp', None))
        if safe_minimum is None:
            safe_minimum = float(self.get('min_bite_temp', 180.0) or 180.0)
        safe_minimum = max(0.0, safe_minimum)
        if safe_maximum is None:
            safe_maximum = 300.0

        material_load_temp = min(
            max(material_load_temp, safe_minimum), safe_maximum)
        material_unload_temp = min(
            max(material_unload_temp, safe_minimum), safe_maximum)
        material_flow_temp = min(
            max(material_flow_temp, safe_minimum), safe_maximum)

        print_state = self.manager._print_state()
        print_active = print_state in ('printing', 'paused', 'pause')
        print_temperature = None
        temperature_source = 'snapmaker_material_profile'
        profile = temperature_profile if isinstance(temperature_profile, dict) else {}
        first_physical_head_load = bool(
            print_active and profile.get('first_physical_head_load', False))
        load_temperature_mode = str(
            profile.get('load_temperature_mode', 'project') or
            'project').strip().lower()
        if load_temperature_mode not in ('default', 'project', 'custom'):
            load_temperature_mode = 'project'
        custom_load_temperature = _coerce_finite_float(
            profile.get('load_temperature'))
        if (custom_load_temperature is not None and
                not safe_minimum <= custom_load_temperature <= safe_maximum):
            custom_load_temperature = None
        try:
            candidate = _coerce_finite_float(profile.get('temperature'))

            if (print_active and candidate is not None and
                    safe_minimum <= candidate <= safe_maximum):
                print_temperature = candidate
                temperature_source = str(
                    profile.get('source', 'gcode_toolchange') or
                    'gcode_toolchange')
        except Exception:
            logging.exception(
                'BMCU could not validate Snapmaker slicer temperature profile')

        heater_target = self.capture_heater_target()
        if (heater_target is None or heater_target < 0.0 or
                heater_target > safe_maximum):
            heater_target = None

        if print_active:

            if print_temperature is not None:
                restore_target = print_temperature
                restore_source = temperature_source
            else:
                restore_target = material_flow_temp
                restore_source = 'snapmaker_flow_temperature'
            failure_restore_target = (
                heater_target if heater_target is not None else restore_target)
            if first_physical_head_load:

                load_temp = min(
                    max(material_load_temp, restore_target), safe_maximum)
            elif (load_temperature_mode == 'custom' and
                  custom_load_temperature is not None):
                load_temp = custom_load_temperature
            elif load_temperature_mode == 'default':
                load_temp = material_load_temp
            else:

                load_temp = restore_target
        else:

            restore_target = 0.0
            restore_source = 'stock_manual_load_off'
            failure_restore_target = 0.0
            load_temp = material_load_temp
        if print_temperature is not None:
            unload_temp = print_temperature
        elif print_active:
            unload_temp = material_flow_temp
        elif heater_target is not None and heater_target >= safe_minimum:
            unload_temp = heater_target
        else:
            unload_temp = material_unload_temp

        return {
            'vendor': vendor, 'material': main_type, 'subtype': sub_type,
            'soft': soft, 'load_temp': load_temp, 'unload_temp': unload_temp,
            'material_load_temp': material_load_temp,
            'material_unload_temp': material_unload_temp,
            'material_flow_temp': material_flow_temp,
            'load_restore_target': restore_target,
            'load_failure_target': failure_restore_target,
            'load_restore_source': restore_source,
            'load_restore_wait': bool(print_active),
            'first_physical_head_load': bool(first_physical_head_load),
            'load_temperature_mode': load_temperature_mode,
            'custom_load_temperature': custom_load_temperature,
            'physical_head': int(profile.get('physical_head', head)),
            'nozzle_diameter': nozzle,
            'temperature_source': temperature_source,
            'print_temperature': print_temperature,
            'heater_target': heater_target,
        }

    def _active_load_context(self, material='', temperature_profile=None):
        if isinstance(self._load_temperature_context, dict):
            return copy.deepcopy(self._load_temperature_context)
        return self._filament_context(material, temperature_profile)

    def finish_load_temperature(self, success=False):
        if self._load_temperature_context is None:
            return True
        context = copy.deepcopy(self._load_temperature_context)
        target = (self._load_restore_target if success
                  else self._load_previous_target)
        wait_for_restore = bool(
            success and context.get('load_restore_wait', False))
        load_temp = _coerce_finite_float(context.get('load_temp'))
        if target is None:
            self._load_temperature_context = None
            self._load_previous_target = None
            self._load_restore_target = None
            return True
        try:
            target = float(target)
            if (wait_for_restore and load_temp is not None and
                    abs(load_temp - target) > 0.5):

                self._run('M109 T%d S%.1f A0' % (self._head(), target))
                if load_temp > target + 0.5:

                    self._run('INNER_DISCARD_FILAMENT_BASE_DISCARD')
                    self._run(
                        'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=4',
                        wait_moves=False)
                    self._run('M400', wait_moves=False)
                    logging.info(
                        'BMCU Snapmaker Head %d post-cool nozzle wipe: '
                        '%.1fC -> %.1fC',
                        self._head() + 1, load_temp, target)
            else:

                self.restore_heater_target(target)
        except Exception:
            logging.exception(
                'BMCU could not restore U1 heater target after load')
            return False
        self._load_temperature_context = None
        self._load_previous_target = None
        self._load_restore_target = None
        return True

    def finish_load_position(self):

        if self.manager._print_state() not in ('printing', 'paused', 'pause'):
            return True
        try:
            self.move_to_safe_wait()
            logging.info(
                'BMCU Snapmaker Head %d completed load cleaning and moved '
                'through the stock XY idle egress before print motion',
                self._head() + 1)
            return True
        except Exception:
            logging.exception(
                'BMCU Snapmaker Head %d could not complete stock XY idle egress',
                self._head() + 1)
            return False

    def _ensure_xy_homed(self):
        if not bool(self.get('u1_auto_home_xy', True)):
            return
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None or not hasattr(toolhead, 'get_status'):
            return
        try:
            axes = str(toolhead.get_status(self.manager.reactor.monotonic()).get('homed_axes', ''))
        except Exception:
            return
        if 'x' not in axes or 'y' not in axes:
            self._run('G28 X Y')

    def _configured_extruder_name(self):
        name = str(self.get('extruder', '') or '').strip()
        if name:
            return name
        head = self._head()
        return 'extruder' if head == 0 else 'extruder%d' % head

    def _configured_extruder(self):
        extruder = self.printer.lookup_object(
            self._configured_extruder_name(), None)
        if extruder is not None:
            return extruder
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is not None:
            try:
                return toolhead.get_extruder()
            except Exception:
                pass
        return None

    def _parked_tip_tail_lane(self, require_active=False,
                              require_inactive=False, require_parked=False):

        if require_active and require_inactive:
            raise EndpointError(
                'parked Head tail lane cannot require active and inactive source')
        extruder_name = self._configured_extruder_name()
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None:
            raise EndpointError(
                'parked Head configured extruder %s is unavailable' %
                extruder_name)
        actual_name = None
        try:
            actual_name = str(extruder.get_name())
        except Exception:
            actual_name = str(getattr(extruder, 'name', '') or '')
        if actual_name != extruder_name:
            raise EndpointError(
                'parked Head extruder identity mismatch: configured %s, got %s' %
                (extruder_name, actual_name or 'unknown'))
        if not bool(self.get('u1_allow_nonstandard_topology', False)):
            try:
                extruder_index = int(getattr(extruder, 'extruder_index'))
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise EndpointError(
                    'parked Head extruder does not expose a physical U1 index')
            if extruder_index != self._head():
                raise EndpointError(
                    'parked Head physical index mismatch: endpoint %d, extruder %d' %
                    (self._head(), extruder_index))
        extruder_stepper = getattr(extruder, 'extruder_stepper', None)
        stepper = getattr(extruder_stepper, 'stepper', None)
        if stepper is None:
            raise EndpointError('parked Head extruder stepper is unavailable')
        for method_name in (
                'get_trapq', 'set_trapq', 'get_stepper_kinematics',
                'set_stepper_kinematics', 'get_commanded_position',
                'set_position', 'generate_steps'):
            if not callable(getattr(stepper, method_name, None)):
                raise EndpointError(
                    'parked Head stepper lacks required isolated-lane method %s' %
                    method_name)
        if stepper.get_trapq() is not extruder.get_trapq():
            raise EndpointError(
                'parked Head extruder stepper is attached to another motion queue')
        if stepper.get_stepper_kinematics() is None:
            raise EndpointError(
                'parked Head extruder kinematics are unavailable')
        mcu = stepper.get_mcu()
        if mcu is None:
            raise EndpointError('parked Head extruder MCU is unavailable')
        is_shutdown = getattr(mcu, 'is_shutdown', None)
        if callable(is_shutdown) and is_shutdown():
            raise EndpointError('parked Head extruder MCU is shutdown')
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None or not hasattr(toolhead, 'get_extruder'):
            raise EndpointError('U1 ToolHead is unavailable for parked-tail lane')
        generators = getattr(toolhead, 'step_generators', None)
        if not isinstance(generators, list):
            raise EndpointError(
                'U1 ToolHead step-generator registry is unavailable')
        generator_matches = []
        for index, handler in enumerate(generators):
            if (getattr(handler, '__self__', None) is stepper and
                    getattr(handler, '__func__', None) is
                    getattr(stepper.generate_steps, '__func__', None)):
                generator_matches.append((index, handler))
        if len(generator_matches) != 1:
            raise EndpointError(
                'parked Head step generator must be registered exactly once')
        generator_index, generator_handler = generator_matches[0]
        active = toolhead.get_extruder()
        if require_active and active is not extruder:
            raise EndpointError(
                'parked-tail preflight requires source Head %s to be active' %
                extruder_name)
        if require_inactive and active is extruder:
            raise EndpointError(
                'parked Head tail requires another Head to be active')
        if require_parked:
            self.verify_parked_for_prestage()
        return {
            'extruder': extruder,
            'stepper': stepper,
            'mcu': mcu,
            'toolhead': toolhead,
            'generator_index': generator_index,
            'generator_handler': generator_handler,
        }

    def prepare_load(self, material='', temperature_profile=None,
                     discard_position_prepared=False):
        self.manager._ensure_u1_takeover(self, save=True)
        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).prepare_load(material)
        if (self._load_temperature_context is not None and
                not self.finish_load_temperature(success=False)):
            raise EndpointError(
                'U1 previous load temperature target could not be restored')
        context = self._filament_context(material, temperature_profile)

        context['discard_position_prepared'] = bool(
            discard_position_prepared)
        self._load_previous_target = context.get('load_failure_target')
        self._load_restore_target = context.get('load_restore_target')
        self._load_temperature_context = copy.deepcopy(context)
        logging.info(
            'BMCU Snapmaker Head %d load: material=%s load=%.1fC '
            'working=%.1fC source=%s first_head_load=%d endpoint=%s',
            self._head() + 1, context.get('material', 'UNKNOWN'),
            context['load_temp'], context['load_restore_target'],
            context.get('load_restore_source', 'unknown'),
            1 if context.get('first_physical_head_load') else 0, self.name)

        self._run('M104 S%.1f' % context['load_temp'],
                  wait_moves=False)
        self.run_macro('prepare_load_macro', endpoint=self.name, material=material)

    def prepared_load_context_matches(self, material='',
                                      temperature_profile=None):

        if not isinstance(self._load_temperature_context, dict):
            return False
        try:
            expected = self._filament_context(material, temperature_profile)
        except Exception:
            return False
        actual = self._load_temperature_context
        for key in ('material', 'vendor', 'subtype',
                    'load_temperature_mode', 'load_restore_source'):
            if str(actual.get(key, '')).upper() != str(
                    expected.get(key, '')).upper():
                return False
        if bool(actual.get('soft')) != bool(expected.get('soft')):
            return False
        if bool(actual.get('first_physical_head_load')) != bool(
                expected.get('first_physical_head_load')):
            return False
        if int(actual.get('physical_head', -1)) != int(
                expected.get('physical_head', -2)):
            return False
        for key, tolerance in (
                ('load_temp', 0.5), ('load_restore_target', 0.5),
                ('load_failure_target', 0.5), ('nozzle_diameter', 0.01)):
            try:
                if abs(float(actual.get(key)) -
                       float(expected.get(key))) > tolerance:
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
        return True

    def ensure_bite_ready(self, material='', temperature_profile=None):
        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).ensure_bite_ready(material)
        context = self._active_load_context(material, temperature_profile)
        self._run('M109 S%.1f' % context['load_temp'])
        self.run_macro('before_bite_macro', endpoint=self.name, material=material)

    def record_load_coil_evidence(self, **values):

        if not isinstance(self._load_temperature_context, dict):
            raise EndpointError(
                'U1 coil evidence was reported without an active load context')
        allowed = {
            'coil_path_baseline', 'coil_bite_delta', 'coil_after_bite',
            'coil_before_capture', 'coil_after_capture', 'coil_capture_delta',
        }
        for key, value in values.items():
            if key not in allowed:
                raise EndpointError('unsupported U1 coil evidence key %s' % key)
            number = _coerce_finite_float(value)
            self._load_temperature_context[key] = (
                None if number is None else float(number))

    def record_load_path_advance(self, distance_mm):

        distance = _coerce_finite_float(distance_mm)
        if distance is None or distance < 0.0 or distance > 500.0:
            raise EndpointError(
                'U1 confirmed load-path advance must be 0..500 mm')
        if not isinstance(self._load_temperature_context, dict):
            raise EndpointError(
                'U1 load-path advance was reported without an active load context')
        self._load_temperature_context['confirmed_head_advance_mm'] = distance
        logging.info(
            'BMCU Snapmaker Head %d confirmed %.1f mm E advance after entry sensor',
            self._head() + 1, distance)
        return distance

    def prepare_load_position(self, material='', temperature_profile=None):

        if not self._native_hotend_sequences_enabled():
            return False
        context = self._active_load_context(material, temperature_profile)
        if bool(context.get('discard_position_prepared', False)):
            return False
        self._ensure_xy_homed()
        self._run('MOVE_TO_DISCARD_FILAMENT_POSITION')
        if isinstance(self._load_temperature_context, dict):
            self._load_temperature_context['discard_position_prepared'] = True
        logging.info(
            'BMCU Snapmaker Head %d reached discard position during load setup',
            self._head() + 1)
        return True

    def load_ready(self, material='', temperature_profile=None):

        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).load_ready(material)
        self.prepare_load_position(material, temperature_profile)
        context = self._active_load_context(material, temperature_profile)

        total_distance = _coerce_finite_float(
            self.get('u1_load_to_nozzle_mm', 70.0))
        if total_distance is None or not 1.0 <= total_distance <= 300.0:
            total_distance = 70.0
        confirmed_advance = _coerce_finite_float(
            context.get('confirmed_head_advance_mm', 0.0))
        if confirmed_advance is None or confirmed_advance < 0.0:
            confirmed_advance = 0.0
        remaining = max(0.0, total_distance - confirmed_advance)
        if remaining <= 0.0001:
            raise EndpointError(
                'U1_TOOLHEAD_PROOF_GEOMETRY_INVALID: Head %d has no final '
                'BEFORE_ON_USE movement (stock_total=%.1f mm, '
                'already_confirmed=%.1f mm)' %
                (self._head() + 1, total_distance, confirmed_advance))

        nozzle = float(context.get('nozzle_diameter', 0.4) or 0.4)
        soft = bool(context.get('soft', False))
        if nozzle < 0.3:
            speed = 60.0 if soft else 210.0
        else:
            speed = 200.0 if soft else 360.0

        profile = self._capture_signal_profile(
            context.get('material', material))

        presence_threshold = max(100.0, float(profile['threshold']) / 3.0)
        path_baseline = _coerce_finite_float(
            context.get('coil_path_baseline'))

        started = self.manager.reactor.monotonic()
        self.extrude(remaining, speed)
        final_end = self.capture_signal()
        elapsed = max(0.0, self.manager.reactor.monotonic() - started)

        path_delta = None
        if path_baseline is not None and final_end is not None:
            path_delta = abs(float(final_end) - float(path_baseline))

        coil_delta = path_delta
        coil_confirmed = bool(
            coil_delta is not None and coil_delta >= presence_threshold)

        self.config['_u1_last_nozzle_target_mm'] = float(total_distance)
        self.config['_u1_last_confirmed_head_advance_mm'] = float(
            confirmed_advance)
        self.config['_u1_last_nozzle_search_mm'] = float(remaining)
        self.config['_u1_last_coil_baseline'] = (
            None if path_baseline is None else float(path_baseline))
        self.config['_u1_last_coil_threshold'] = float(presence_threshold)
        self.config['_u1_last_coil_delta'] = (
            None if coil_delta is None else float(coil_delta))
        self.config['_u1_last_coil_capture_delta'] = None
        self.config['_u1_last_coil_final_delta'] = None
        self.config['_u1_last_coil_path_delta'] = (
            None if path_delta is None else float(path_delta))
        self.config['_u1_last_coil_confirmed'] = bool(coil_confirmed)
        self.config['_u1_last_toolhead_verify_elapsed_s'] = float(elapsed)

        logging.info(
            'BMCU Snapmaker Head %d BEFORE_ON_USE minimal coil check: '
            'stock_total=%.1fmm already_confirmed=%.1fmm remaining=%.1fmm '
            'speed=%.1fmm/min material=%s soft=%d nozzle=%.3f '
            'path_start=%s final_end=%s path_delta=%s gentle_threshold=%.1f '
            'confirmed=%d coil_queries=2 elapsed=%.3fs',
            self._head() + 1, total_distance, confirmed_advance, remaining,
            speed, context.get('material', 'UNKNOWN'), 1 if soft else 0,
            nozzle,
            'unavailable' if path_baseline is None else '%.1f' % path_baseline,
            'unavailable' if final_end is None else '%.1f' % final_end,
            'unavailable' if path_delta is None else '%.1f' % path_delta,
            presence_threshold, 1 if coil_confirmed else 0, elapsed)
        if getattr(self.manager, 'debug_enabled', False):
            self.manager._debug_log(
                'U1 %s minimal coil evidence path_start=%s final_end=%s '
                'path_delta=%s threshold=%.1f confirmed=%d '
                'boundary_queries_skipped=BITE,CAPTURE,FINAL_START',
                self.name, path_baseline, final_end, path_delta,
                presence_threshold, 1 if coil_confirmed else 0)
            live_context = (
                self._load_temperature_context
                if isinstance(self._load_temperature_context, dict) else {})
            debug_reads = live_context.get('_debug_coil_reads', [])
            if not isinstance(debug_reads, list):
                debug_reads = []
            self.manager._debug_log(
                'U1 %s coil query-cost samples=%s total_queries=%d '
                'note=stock_get_coil_freq_is_active_mcu_query',
                self.name, copy.deepcopy(debug_reads), len(debug_reads))

        if not coil_confirmed:
            raise EndpointError(
                'U1_TOOLHEAD_NOT_LOADED: Head %d saw no convincing downstream '
                'start-to-finish coil response during the normal BEFORE_ON_USE '
                'path (path_delta=%s, gentle_required>=%.1f). Exactly two '
                'read-only stock coil queries were used and no extra movement, '
                'dwell, retry or sampling loop was added; BMCU pressure and '
                'Head motion were stopped before prime.' %
                (self._head() + 1,
                 'unavailable' if path_delta is None else '%.1f' % path_delta,
                 presence_threshold))

        logging.info(
            'BMCU Snapmaker Head %d minimal coil evidence accepted; '
            'BEFORE_ON_USE may advance to prime', self._head() + 1)
        self.run_macro('load_ready_macro', endpoint=self.name, material=material)
        return {
            'coil_confirmed': bool(coil_confirmed),
            'coil_delta': None if coil_delta is None else float(coil_delta),
            'coil_capture_delta': None,
            'coil_final_delta': None,
            'coil_path_delta': None if path_delta is None else float(path_delta),
            'coil_threshold': float(presence_threshold),
            'coil_baseline': (
                None if path_baseline is None else float(path_baseline)),
            'remaining_mm': float(remaining),
            'elapsed_s': float(elapsed),
            'sampling_delay_s': 0.0,
            'extra_verification_motion_mm': 0.0,
        }

    def _native_preextrude(self, material='', refill=False, temperature_profile=None):
        context = self._active_load_context(material, temperature_profile)
        key = 'u1_refill_prime_length_mm' if refill else 'u1_prime_length_mm'
        length = float(self.get(key, 20.0) or 20.0)
        soft = bool(context['soft'])
        if soft:
            length = 10.0
        speed = 120.0 if soft else 360.0
        logging.info(
            'BMCU Snapmaker Head %d prime start: material=%s load=%.1fC '
            'working=%.1fC length=%.1fmm speed=%.1fmm/min soft=%d print_state=%s',
            self._head() + 1, context.get('material', 'UNKNOWN'),
            context['load_temp'], context['load_restore_target'], length, speed,
            1 if soft else 0, self.manager._print_state())
        state_name = 'BMCU_U1_PREEXTRUDE_%d' % self._head()
        extruder_name = self._configured_extruder_name()
        flow_applied = False
        saved = False
        try:
            self._run('SAVE_GCODE_STATE NAME=%s' % state_name,
                  wait_moves=False)
            saved = True
            self._run('SET_ACTION_CODE ACTION=PRINT_PREEXTRUDING',
                      wait_moves=False)
            script = render_u1_load_gcode({
                'load_temp': int(round(context['load_temp'])),
                'unload_temp': int(round(context['unload_temp'])),
                'soft': 1 if soft else 0,
                'nozzle': '%.3f' % context['nozzle_diameter'],
                'length': '%.3f' % length,
                'speed': '%.1f' % speed,
                'extruder': extruder_name,
                'state': 'refill' if refill else 'load',
            })

            flow_applied = 'INNER_APPLY_FLOW_K' in script and 'APPLY=1' in script
            self._run(script)
            flow_applied = False

            if self.manager._print_state() not in ('printing', 'paused'):
                self._run('MOVE_TO_XY_IDLE_POSITION_EXTRUDER')
            logging.info(
                'BMCU Snapmaker Head %d prime complete: material=%s length=%.1fmm',
                self._head() + 1, context.get('material', 'UNKNOWN'), length)
        finally:
            if flow_applied:
                try:
                    self._run(
                        'INNER_APPLY_FLOW_K EXTRUDER=%s APPLY=0' %
                        extruder_name, wait_moves=False)
                except Exception:
                    logging.exception(
                        'BMCU could not clear U1 flow compensation after pre-extrude failure')
            try:
                self._run('M107', wait_moves=False)
            except Exception:
                logging.exception('BMCU could not stop U1 fan after pre-extrude')
            if saved:
                try:
                    self._run(
                        'RESTORE_GCODE_STATE NAME=%s MOVE=0' % state_name,
                        wait_moves=False)
                except Exception:
                    logging.exception(
                        'BMCU could not restore U1 G-code state after pre-extrude')
            try:
                self._run('SET_ACTION_CODE ACTION=IDLE', wait_moves=False)
            except Exception:
                logging.exception(
                    'BMCU could not restore U1 action state after pre-extrude')

    def prime(self, material='', temperature_profile=None):
        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).prime(material)

        self._native_preextrude(material, refill=False, temperature_profile=temperature_profile)
        self.run_macro('wipe_macro', endpoint=self.name, material=material)
        self.run_macro('prime_macro', endpoint=self.name, material=material)

    def _tip_temperature(self, context, tip_profile):
        profile = (tip_profile if isinstance(tip_profile, dict)
                   else u1_tip_profile_defaults())
        profile = validate_u1_tip_profile(profile)
        if profile['temperature_mode'] == 'custom':
            return float(profile['temperature']), 'custom_tip_profile'
        if profile['temperature_mode'] == 'default':
            return float(context['material_unload_temp']), 'snapmaker_material_profile'
        current_target = _coerce_finite_float(context.get('heater_target'))
        if current_target is not None and 170.0 <= current_target <= 300.0:
            return current_target, 'current_heater_target'
        return float(context['unload_temp']), context['temperature_source']

    def prepare_unload(self, material='', temperature_profile=None,
                       tip_profile=None):
        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).prepare_unload(material)
        context = self._filament_context(material, temperature_profile)
        tip_temp, _source = self._tip_temperature(context, tip_profile)

        self._ensure_xy_homed()
        self._run('MOVE_TO_DISCARD_FILAMENT_POSITION\nM400',
                  wait_moves=False)

        self._run('M104 S%.1f' % tip_temp, wait_moves=False)
        self.run_macro(
            'prepare_unload_macro', endpoint=self.name, material=material)

    def _u1_part_cooling_fan_pwm(self):

        head = self._head()
        fan_name = 'fan' if head == 0 else 'fan_generic e%d_fan' % head
        fan = self.printer.lookup_object(fan_name, None)
        if fan is None:
            raise EndpointError(
                'U1 Head %d part-cooling fan object %s is unavailable' %
                (head + 1, fan_name))
        try:
            status = fan.get_status(self.manager.reactor.monotonic())
        except TypeError:
            status = fan.get_status()
        speed = _coerce_finite_float((status or {}).get('speed'))
        if speed is None or speed < 0.0 or speed > 1.0:
            raise EndpointError(
                'U1 Head %d part-cooling fan speed is unavailable' %
                (head + 1))
        return int(round(speed * 255.0))

    def _drop_tip_blob_after_tip_forming(self):

        required = (
            'INNER_CUTOFF_BASE_DISCARD',
            'INNER_DISCARD_FILAMENT_BASE_DISCARD',
            'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD',
        )
        missing = [name for name in required if not self._command_exists(name)]
        if missing:
            raise EndpointError(
                'U1 tip-forming blob release requires printer command(s): %s' %
                ', '.join(missing))
        previous_pwm = self._u1_part_cooling_fan_pwm()
        cleanup_pwm = max(previous_pwm, 178)
        fan_changed = cleanup_pwm != previous_pwm
        if fan_changed:
            self._run('M106 S%d' % cleanup_pwm, wait_moves=False)
        try:
            self._run(
                'INNER_CUTOFF_BASE_DISCARD\n'
                'INNER_DISCARD_FILAMENT_BASE_DISCARD\n'
                'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD\n'
                'G90\n'
                'M400')
        finally:
            if fan_changed:
                self._run('M106 S%d' % previous_pwm, wait_moves=False)
        logging.info(
            'BMCU Snapmaker Head %d cut and discard-shook tip blob before '
            'silicone brush clean; part-cooling PWM %d -> %d -> %d with no dwell',
            self._head() + 1, previous_pwm, cleanup_pwm, previous_pwm)

    def cut_or_form_tip(self, material='', temperature_profile=None,
                        tip_profile=None, park_at_marker=False):
        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).cut_or_form_tip(material)
        context = self._filament_context(material, temperature_profile)
        profile = validate_u1_tip_profile(
            tip_profile if isinstance(tip_profile, dict)
            else u1_tip_profile_defaults())
        tip_temp, source = self._tip_temperature(context, profile)
        logging.info(
            'BMCU Snapmaker tip-forming temperature %.1fC from %s on %s',
            tip_temp, source, self.name)
        self._ensure_xy_homed()
        mode = str(self.get('u1_unload_mode', 'fast') or 'fast').lower()
        if mode == 'native_full':
            self._run(
                'INNER_FILAMENT_UNLOAD TEMP=%d SOFT=%d NOZZLE_DIAMETER=%.3f' %
                (int(round(tip_temp)), 1 if context['soft'] else 0,
                 context['nozzle_diameter']))
            return {'parked_at_marker': False, 'variant': 'native_full'}
        if mode != 'fast':
            raise EndpointError('invalid U1 unload mode %s' % mode)

        render_values = {
            'tip_temp': int(round(tip_temp)),
            'unload_temp': int(round(tip_temp)),
            'soft': 1 if context['soft'] else 0,
            'nozzle_diameter': context['nozzle_diameter'],

            'head': self._head() + 1,
        }
        pre_script, post_script, variant = split_u1_tip_movements(
            profile, render_values)
        logging.info(
            'BMCU Snapmaker Head %d rendered bounded tip program [%s/%s] '
            'park_marker_available=%d park_requested=%d: %s | %s',
            self._head() + 1, context.get('material', 'UNKNOWN'), variant,
            1 if post_script.strip() else 0, 1 if park_at_marker else 0,
            ' | '.join(line.strip() for line in pre_script.splitlines()
                       if line.strip())[:800],
            ' | '.join(line.strip() for line in post_script.splitlines()
                       if line.strip())[:800])
        state_name = 'BMCU_U1_TIP_%d' % self._head()
        saved = False
        parked = False
        try:
            self._run('SAVE_GCODE_STATE NAME=%s' % state_name,
                  wait_moves=False)
            saved = True
            marker_available = bool(post_script.strip())
            if park_at_marker and not marker_available:
                logging.info(
                    'BMCU Snapmaker Head %d tip profile has no early park '
                    'marker; completing tip forming attached, then using stock '
                    'park before long pullback', self._head() + 1)
            parked_tail_plan = None
            parked_tail_ticket = None
            if park_at_marker and marker_available:

                self._parked_tip_tail_lane(require_active=True)
                full_tip_plan, marker_index, exact_variant = (
                    parse_u1_background_tip_program(profile, render_values))
                if exact_variant != variant:
                    raise EndpointError(
                        'background tip variant changed during exact-plan render')
                parked_tail_plan = parse_u1_parked_tip_tail(post_script)

                self._run(
                    'MOVE_TO_DISCARD_FILAMENT_POSITION\nM109 S%d' %
                    int(render_values['tip_temp']))
                self.verify_selected()
                parked_tail_ticket = self.queue_background_tip_program(
                    full_tip_plan, marker_index, base_tip_temp=tip_temp)
                try:

                    self.wait_background_tip_marker(parked_tail_ticket['id'])
                    self._drop_tip_blob_after_tip_forming()
                    self.park_selected_head_for_tip_retract()
                    parked = True
                    self.verify_parked_for_prestage()
                except Exception:

                    try:
                        self.wait_parked_tip_tail(
                            parked_tail_ticket['id'], poll_interval=0.100)
                    except Exception:
                        logging.exception(
                            'BMCU could not restore source Head exact tip lane '
                            'after pre-park blob/park failure')
                    raise
            else:
                combined, _variant = render_u1_tip_movements(
                    profile, render_values)
                self._run(combined)

                self._drop_tip_blob_after_tip_forming()
        finally:

            if parked_tail_ticket is None:
                try:
                    self._reset_tip_hotend_fan_override()
                except Exception:
                    logging.exception(
                        'BMCU could not restore U1 hotend fan after attached tip forming')
            if saved:
                try:
                    self._run(
                        'RESTORE_GCODE_STATE NAME=%s MOVE=0' % state_name,
                        wait_moves=False)
                except Exception:
                    logging.exception(
                        'BMCU could not restore U1 G-code state after tip forming')
        return {
            'parked_at_marker': parked,
            'marker_available': bool(post_script.strip()),
            'variant': variant,
            'parked_tip_tail_plan': copy.deepcopy(parked_tail_plan),
            'parked_tip_tail_ticket': copy.deepcopy(parked_tail_ticket),
        }

    def _tip_heater_context(self):

        extruder_name = str(
            self.get('heater', self.get('extruder', 'extruder')) or '').strip()
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None:
            raise EndpointError(
                'U1 Head %d heater/extruder %s is unavailable' %
                (self._head() + 1, extruder_name or '<empty>'))
        get_heater = getattr(extruder, 'get_heater', None)
        heater = get_heater() if callable(get_heater) else getattr(
            extruder, 'heater', None)
        pheaters = self.printer.lookup_object('heaters', None)
        if heater is None or pheaters is None:
            raise EndpointError(
                'U1 Head %d heater manager is unavailable' %
                (self._head() + 1))
        return extruder, heater, pheaters

    def _validate_tip_heater_target(self, target):

        target = float(target)
        if not math.isfinite(target):
            raise EndpointError('U1 tip heater target is invalid')
        _extruder, heater, _pheaters = self._tip_heater_context()
        min_temp = float(getattr(heater, 'min_temp', -273.15))
        max_temp = float(getattr(heater, 'max_temp', 9999.0))

        if target and (target < min_temp or target > max_temp):
            raise EndpointError(
                'U1 Head %d tip heater target %.1fC is outside %.1f..%.1fC' %
                (self._head() + 1, target, min_temp, max_temp))
        return target

    def _set_background_tip_heater_target(self, target):

        target = self._validate_tip_heater_target(target)
        _extruder, heater, pheaters = self._tip_heater_context()
        pheaters.set_temperature(heater, target, wait=False)
        return target

    def _tip_hotend_fan_object(self):
        name = 'heater_fan e%d_nozzle_fan' % self._head()
        fan = self.printer.lookup_object(name, None)
        required = ('probe_speed', 'fan_speed', 'original_fan_speed',
                    'last_speed', 'current_stepped_index', 'fan_timer')
        if fan is None or any(not hasattr(fan, attr) for attr in required):
            raise EndpointError(
                'U1 Head %d hotend fan object %s is unavailable or incompatible' %
                (self._head() + 1, name))
        return name, fan

    def _set_tip_hotend_fan_override(self, speed):

        speed = float(speed)
        if not math.isfinite(speed) or speed < 0.0 or speed > 1.0:
            raise EndpointError('U1 hotend fan override must be 0.0..1.0')
        name, fan = self._tip_hotend_fan_object()
        state = self._tip_hotend_fan_override_state
        if state is None:
            state = {
                'name': name, 'fan': fan,
                'probe_speed': float(fan.probe_speed),
                'fan_speed': float(fan.fan_speed),
                'original_fan_speed': fan.original_fan_speed,
            }
            self._tip_hotend_fan_override_state = state
        elif state.get('fan') is not fan:
            raise EndpointError('U1 source hotend fan changed during tip override')
        fan.probe_speed = speed

        if fan.original_fan_speed is None:
            fan.original_fan_speed = fan.fan_speed
        fan.fan_speed = speed
        fan.last_speed = -1
        fan.current_stepped_index = -1
        if fan.fan_timer is not None:
            self.manager.reactor.update_timer(fan.fan_timer, self.manager.reactor.NOW)
        logging.info(
            'BMCU Snapmaker Head %d hotend fan override -> %.0f%% (%s)',
            self._head() + 1, speed * 100.0, name)
        return speed

    def _reset_tip_hotend_fan_override(self):
        state = self._tip_hotend_fan_override_state
        if not isinstance(state, dict):
            return False
        fan = state.get('fan')
        try:
            if fan is None:
                raise EndpointError('U1 source hotend fan restore object is missing')
            fan.probe_speed = float(state['probe_speed'])
            fan.fan_speed = float(state['fan_speed'])
            fan.original_fan_speed = state['original_fan_speed']
            fan.last_speed = -1
            fan.current_stepped_index = -1
            if fan.fan_timer is not None:
                self.manager.reactor.update_timer(
                    fan.fan_timer, self.manager.reactor.NOW)
            logging.info(
                'BMCU Snapmaker Head %d hotend fan override restored to stock control',
                self._head() + 1)
            return True
        finally:
            self._tip_hotend_fan_override_state = None

    def queue_attached_tip_hotend_fan_event(self, speed=None, reset=False):

        if bool(reset) == (speed is not None):
            raise EndpointError(
                'attached tip fan event requires exactly SPEED or RESET')
        if speed is not None:
            speed = float(speed)
            if not math.isfinite(speed) or speed < 0.0 or speed > 1.0:
                raise EndpointError('attached tip fan SPEED must be 0.0..1.0')
        lane = self._parked_tip_tail_lane(require_active=True)
        toolhead = lane['toolhead']
        mcu = lane['mcu']
        reactor = self.manager.reactor
        event = {
            'head': self._head(), 'speed': speed, 'reset': bool(reset),
            'scheduled_print_time': None, 'applied': False, 'error': '',
        }

        def on_move_boundary(print_time):
            event['scheduled_print_time'] = float(print_time)

            def apply_timer(eventtime):
                try:
                    estimated = float(mcu.estimated_print_time(eventtime))
                    remaining = float(print_time) - estimated
                    if remaining > 0.002:
                        return eventtime + max(0.001, remaining - 0.001)
                    if reset:
                        self._reset_tip_hotend_fan_override()
                        applied_speed = None
                    else:
                        applied_speed = self._set_tip_hotend_fan_override(speed)
                    now = reactor.monotonic()
                    observed = float(mcu.estimated_print_time(now))
                    lag = max(0.0, observed - float(print_time))
                    event['applied'] = True
                    event['host_lag_s'] = lag
                    logging.info(
                        'BMCU Snapmaker Head %d attached tip hotend fan %s at '
                        'source print_time %.6f (host lag %.3fms)',
                        self._head() + 1,
                        ('RESET' if reset else '-> %.0f%%' % (applied_speed * 100.0)),
                        float(print_time), lag * 1000.0)
                except Exception as exc:
                    event['error'] = str(exc)
                    logging.exception(
                        'BMCU source Head attached tip hotend-fan event failed')
                return reactor.NEVER

            event['_timer'] = reactor.register_timer(apply_timer, reactor.NOW)

        toolhead.register_lookahead_callback(on_move_boundary)
        return event

    def queue_attached_tip_temperature_event(self, target):

        target = self._validate_tip_heater_target(target)
        lane = self._parked_tip_tail_lane(require_active=True)
        toolhead = lane['toolhead']
        mcu = lane['mcu']
        reactor = self.manager.reactor
        event = {
            'head': self._head(),
            'target': float(target),
            'scheduled_print_time': None,
            'applied': False,
            'error': '',
        }

        def on_move_boundary(print_time):
            event['scheduled_print_time'] = float(print_time)

            def apply_timer(eventtime):
                try:
                    estimated = float(mcu.estimated_print_time(eventtime))
                    remaining = float(print_time) - estimated
                    if remaining > 0.002:
                        return eventtime + max(0.001, remaining - 0.001)
                    applied = self._set_background_tip_heater_target(target)
                    cached_temp = self._heater_temperature()
                    now = reactor.monotonic()
                    observed = float(mcu.estimated_print_time(now))
                    lag = max(0.0, observed - float(print_time))
                    event['applied'] = True
                    event['applied_target'] = float(applied)
                    event['cached_temperature'] = cached_temp
                    event['host_lag_s'] = lag
                    logging.info(
                        'BMCU Snapmaker Head %d attached tip temperature -> '
                        '%.1fC at source print_time %.6f '
                        '(cached %.1fC, host lag %.3fms)',
                        self._head() + 1, applied, float(print_time),
                        float(cached_temp) if cached_temp is not None else -1.0,
                        lag * 1000.0)
                    if getattr(self.manager, 'debug_enabled', False):
                        self.manager._debug_log(
                            'U1 %s ATTACHED TIP TEMP target=%.1f cached_temp=%s '
                            'scheduled_print_time=%.6f observed_print_time=%.6f '
                            'host_lag_ms=%.3f',
                            self.name, applied,
                            ('unknown' if cached_temp is None else
                             '%.1f' % float(cached_temp)),
                            float(print_time), observed, lag * 1000.0)
                except Exception as exc:
                    event['error'] = str(exc)
                    logging.exception(
                        'BMCU source Head attached tip temperature event failed')
                return reactor.NEVER

            event['_timer'] = reactor.register_timer(apply_timer, reactor.NOW)

        toolhead.register_lookahead_callback(on_move_boundary)
        return event

    def _schedule_background_tip_temperature_events(self, ticket):
        events = ticket.get('_temperature_events') or []
        if not events:
            ticket['temperature_events_complete'] = True
            return
        reactor = self.manager.reactor
        mcu = ticket['_mcu']
        ticket['_temperature_event_index'] = 0
        ticket['temperature_event_error'] = ''

        def temperature_event_timer(eventtime):
            try:
                index = int(ticket.get('_temperature_event_index', 0))
                while index < len(events):
                    event = events[index]
                    estimated = float(mcu.estimated_print_time(eventtime))
                    remaining = float(event['print_time']) - estimated

                    if remaining > 0.002:
                        return eventtime + max(0.001, remaining - 0.001)
                    applied_target = self._set_background_tip_heater_target(
                        event['target'])

                    cached_temp = self._heater_temperature()
                    now = reactor.monotonic()
                    observed = float(mcu.estimated_print_time(now))
                    lag = max(0.0, observed - float(event['print_time']))
                    event['applied'] = True
                    event['applied_target'] = float(applied_target)
                    event['cached_temperature'] = cached_temp
                    event['host_lag_s'] = lag
                    logging.info(
                        'BMCU Snapmaker Head %d tip temperature event %d/%d '
                        '%s -> %.1fC at source print_time %.6f '
                        '(cached %.1fC, host lag %.3fms)',
                        self._head() + 1, index + 1, len(events),
                        event.get('kind', 'temp'), applied_target,
                        float(event['print_time']),
                        float(cached_temp) if cached_temp is not None else -1.0,
                        lag * 1000.0)
                    if getattr(self.manager, 'debug_enabled', False):
                        self.manager._debug_log(
                            'U1 %s TIP TEMP event=%d/%d kind=%s target=%.1f '
                            'cached_temp=%s scheduled_print_time=%.6f '
                            'observed_print_time=%.6f host_lag_ms=%.3f',
                            self.name, index + 1, len(events),
                            event.get('kind', 'temp'), applied_target,
                            ('unknown' if cached_temp is None else
                             '%.1f' % float(cached_temp)),
                            float(event['print_time']), observed, lag * 1000.0)
                    index += 1
                    ticket['_temperature_event_index'] = index
                    eventtime = now
                ticket['temperature_events_complete'] = True
                return reactor.NEVER
            except Exception as exc:
                ticket['temperature_event_error'] = str(exc)
                ticket['temperature_events_complete'] = False
                logging.exception(
                    'BMCU source Head tip temperature event failed')
                return reactor.NEVER

        timer = reactor.register_timer(temperature_event_timer, reactor.NOW)
        ticket['_temperature_event_timer'] = timer

    def _schedule_background_tip_fan_events(self, ticket):
        events = ticket.get('_fan_events') or []
        if not events:
            ticket['fan_events_complete'] = True
            return
        reactor = self.manager.reactor
        mcu = ticket['_mcu']
        ticket['_fan_event_index'] = 0
        ticket['fan_event_error'] = ''

        def fan_event_timer(eventtime):
            try:
                index = int(ticket.get('_fan_event_index', 0))
                while index < len(events):
                    event = events[index]
                    estimated = float(mcu.estimated_print_time(eventtime))
                    remaining = float(event['print_time']) - estimated
                    if remaining > 0.002:
                        return eventtime + max(0.001, remaining - 0.001)
                    if event['kind'] == 'fan_reset':
                        self._reset_tip_hotend_fan_override()
                        applied_text = 'RESET'
                    else:
                        speed = self._set_tip_hotend_fan_override(event['speed'])
                        applied_text = '%.0f%%' % (speed * 100.0)
                    now = reactor.monotonic()
                    observed = float(mcu.estimated_print_time(now))
                    lag = max(0.0, observed - float(event['print_time']))
                    event['applied'] = True
                    event['host_lag_s'] = lag
                    logging.info(
                        'BMCU Snapmaker Head %d tip hotend fan event %d/%d %s '
                        'at source print_time %.6f (host lag %.3fms)',
                        self._head() + 1, index + 1, len(events), applied_text,
                        float(event['print_time']), lag * 1000.0)
                    index += 1
                    ticket['_fan_event_index'] = index
                    eventtime = now
                ticket['fan_events_complete'] = True
                return reactor.NEVER
            except Exception as exc:
                ticket['fan_event_error'] = str(exc)
                ticket['fan_events_complete'] = False
                logging.exception('BMCU source Head tip hotend-fan event failed')
                return reactor.NEVER

        timer = reactor.register_timer(fan_event_timer, reactor.NOW)
        ticket['_fan_event_timer'] = timer

    @staticmethod
    def _parked_tip_tail_public(ticket):
        public_keys = (
            'id', 'head', 'start_print_time', 'marker_print_time',
            'end_print_time', 'duration_s', 'positive_duration_s',
            'negative_duration_s', 'pre_marker_duration_s',
            'post_marker_duration_s', 'marker_gap_s', 'scheduler_lead_s',
            'start_position', 'end_position', 'move_count',
            'restore_after_print_time', 'lane_restored', 'lane_fault',
            'cancelled_while_queued', 'temperature_event_count',
            'temperature_events_complete', 'temperature_event_error',
            'fan_event_count', 'fan_events_complete', 'fan_event_error')
        return {
            key: copy.deepcopy(ticket[key])
            for key in public_keys if key in ticket
        }

    def _restore_parked_tip_tail_lane(self, ticket):
        if ticket.get('_lane_restored'):
            return
        stepper = ticket.get('_stepper')
        extruder = ticket.get('_extruder')
        tail_kinematics = ticket.get('_tail_kinematics')
        tail_trapq = ticket.get('_tail_trapq')
        original_kinematics = ticket.get('_original_kinematics')
        original_trapq = ticket.get('_original_trapq')
        if (stepper is None or extruder is None or
                tail_kinematics is None or tail_trapq is None or
                original_kinematics is None or original_trapq is None):
            raise EndpointError(
                'parked Head isolated tail lane restore context is incomplete')
        if stepper.get_stepper_kinematics() is not tail_kinematics:
            raise EndpointError(
                'parked Head isolated tail kinematics changed before restore')
        if stepper.get_trapq() is not tail_trapq:
            raise EndpointError(
                'parked Head isolated tail motion queue changed before restore')

        replaced_kinematics = stepper.set_stepper_kinematics(
            original_kinematics)
        if replaced_kinematics is not tail_kinematics:
            raise EndpointError(
                'parked Head isolated tail kinematics restore mismatch')
        replaced_trapq = stepper.set_trapq(original_trapq)
        if replaced_trapq is not tail_trapq:
            raise EndpointError(
                'parked Head isolated tail motion queue restore mismatch')

        end_position = float(ticket['end_position'])
        stepper.set_position((end_position, 0.0, 0.0))
        extruder.last_position = end_position

        toolhead = ticket.get('_toolhead')
        generator_handler = ticket.get('_generator_handler')
        generator_index = int(ticket.get('_generator_index', -1))
        generators = getattr(toolhead, 'step_generators', None)
        if (not isinstance(generators, list) or generator_handler is None or
                generator_index < 0):
            raise EndpointError(
                'parked Head step-generator restore context is incomplete')
        for handler in generators:
            if (getattr(handler, '__self__', None) is stepper and
                    getattr(handler, '__func__', None) is
                    getattr(stepper.generate_steps, '__func__', None)):
                raise EndpointError(
                    'parked Head step generator was re-registered unexpectedly')
        generators.insert(min(generator_index, len(generators)),
                          generator_handler)

        temp_timer = ticket.get('_temperature_event_timer')
        if temp_timer is not None:
            try:
                self.manager.reactor.unregister_timer(temp_timer)
            except Exception:
                logging.exception(
                    'BMCU could not unregister completed tip temperature timer')
            ticket['_temperature_event_timer'] = None
        fan_timer = ticket.get('_fan_event_timer')
        if fan_timer is not None:
            try:
                self.manager.reactor.unregister_timer(fan_timer)
            except Exception:
                logging.exception(
                    'BMCU could not unregister completed tip hotend-fan timer')
            ticket['_fan_event_timer'] = None

        ticket['_lane_restored'] = True
        ticket['lane_restored'] = True

        ticket['_tail_kinematics'] = None
        ticket['_tail_trapq'] = None

    def queue_background_tip_program(self, operations, marker_index,
                                     base_tip_temp=None):

        if self._parked_tip_tail_ticket is not None:
            raise EndpointError(
                'source Head already has a queued background tip program')
        operations = copy.deepcopy(list(operations or []))
        try:
            marker_index = int(marker_index)
        except (TypeError, ValueError, OverflowError):
            raise EndpointError('background tip marker index is invalid')
        if not operations or marker_index <= 0 or marker_index >= len(operations):
            raise EndpointError('background tip program/marker is invalid')
        base_tip_temp = float(base_tip_temp)
        if not math.isfinite(base_tip_temp):
            raise EndpointError('background tip base temperature is invalid')

        lane = self._parked_tip_tail_lane(
            require_active=True, require_inactive=False, require_parked=False)
        extruder = lane['extruder']
        stepper = lane['stepper']
        toolhead = lane['toolhead']
        if bool(getattr(toolhead, 'is_calibrating_flow', False)):
            raise EndpointError(
                'background tip program is unavailable during flow calibration')
        max_velocity = float(getattr(extruder, 'max_e_velocity', 0.0) or 0.0)
        acceleration = float(getattr(extruder, 'max_e_accel', 0.0) or 0.0)
        if (not math.isfinite(max_velocity) or max_velocity <= 0.0 or
                not math.isfinite(acceleration) or acceleration <= 0.0):
            raise EndpointError('background tip extrusion limits are invalid')

        move_before_marker = False
        move_after_marker = False
        for index, operation in enumerate(operations):
            kind = str(operation.get('kind', '') or '')
            if kind == 'move':
                distance = float(operation.get('distance', 0.0))
                speed = float(operation.get('speed', 0.0))
                if (not math.isfinite(distance) or distance == 0.0 or
                        not math.isfinite(speed) or speed <= 0.0):
                    raise EndpointError(
                        'background tip contains an invalid E move')
                operation['distance'] = distance
                operation['speed'] = min(speed, max_velocity)
                if index < marker_index:
                    move_before_marker = True
                else:
                    move_after_marker = True
            elif kind == 'dwell':
                seconds = float(operation.get('seconds', 0.0))
                if not math.isfinite(seconds) or seconds < 0.0:
                    raise EndpointError(
                        'background tip contains an invalid dwell')
                operation['seconds'] = seconds
            elif kind in ('temp_set', 'temp_reset'):
                operation['target'] = self._validate_tip_heater_target(
                    _u1_tip_temperature_target(operation, base_tip_temp))
            elif kind == 'fan_set':
                speed = float(operation.get('speed', -1.0))
                if not math.isfinite(speed) or speed < 0.0 or speed > 1.0:
                    raise EndpointError(
                        'background tip contains an invalid hotend fan speed')
                operation['speed'] = speed
            elif kind == 'fan_reset':
                pass
            else:
                raise EndpointError(
                    'background tip contains an unknown operation')
        if not move_before_marker or not move_after_marker:
            raise EndpointError(
                'background tip requires E movement on both sides of the marker')

        reactor = self.manager.reactor
        mcu = lane['mcu']
        queue_host_started = reactor.monotonic()

        flush_host_started = reactor.monotonic()
        toolhead.flush_step_generation()
        flush_host_finished = reactor.monotonic()
        stock_start_time = float(toolhead.get_last_move_time())
        source_mcu_time = float(mcu.estimated_print_time(reactor.monotonic()))
        scheduler_lead = stock_start_time - source_mcu_time

        if scheduler_lead < 0.100:
            raise EndpointError(
                'background tip has insufficient stock scheduler lead %.3f ms' %
                (scheduler_lead * 1000.0))
        start_time = stock_start_time

        ffi_main, ffi_lib = chelper.get_ffi()
        tip_kinematics = ffi_main.gc(
            ffi_lib.extruder_stepper_alloc(),
            ffi_lib.extruder_stepper_free)
        tip_trapq = ffi_main.gc(
            ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        trapq_append = ffi_lib.trapq_append
        trapq_finalize = ffi_lib.trapq_finalize_moves
        original_kinematics = stepper.get_stepper_kinematics()
        original_trapq = stepper.get_trapq()
        generator_index = int(lane['generator_index'])
        generator_handler = lane['generator_handler']
        start_position = float(stepper.get_commanded_position())
        current_time = start_time
        current_position = start_position
        marker_print_time = None
        move_count = 0
        debug_segments = []
        temperature_events = []
        fan_events = []
        enable = None
        was_enabled = True
        generator_detached = False
        kinematics_attached = False
        trapq_attached = False
        motion_generated = False
        ticket = None
        try:
            stepper_enable = self.printer.lookup_object('stepper_enable', None)
            if stepper_enable is not None:
                enable = stepper_enable.lookup_enable(stepper.get_name())
                was_enabled = bool(enable.is_motor_enabled())
                if not was_enabled:
                    enable.motor_enable(max(start_time - 0.100, 0.0))
            vref_sw = getattr(extruder, 'vref_sw', None)
            if vref_sw is not None:
                vref_sw._set_pin(max(start_time - 0.050, 0.0), 1)

            generators = toolhead.step_generators
            if (generator_index >= len(generators) or
                    generators[generator_index] is not generator_handler):
                raise EndpointError(
                    'source Head step-generator registration changed before detach')
            generators.pop(generator_index)
            generator_detached = True

            replaced_kinematics = stepper.set_stepper_kinematics(
                tip_kinematics)
            kinematics_attached = True
            if replaced_kinematics is not original_kinematics:
                raise EndpointError(
                    'source Head kinematics changed during exact-tip attach')
            replaced_trapq = stepper.set_trapq(tip_trapq)
            trapq_attached = True
            if replaced_trapq is not original_trapq:
                raise EndpointError(
                    'source Head motion queue changed during exact-tip attach')
            stepper.set_position((start_position, 0.0, 0.0))

            for index, operation in enumerate(operations):
                if index == marker_index:
                    marker_print_time = current_time
                if operation['kind'] == 'dwell':
                    current_time += float(operation['seconds'])
                    continue
                if operation['kind'] in ('temp_set', 'temp_reset'):
                    temperature_events.append({
                        'print_time': float(current_time),
                        'target': float(operation['target']),
                        'kind': str(operation['kind']),
                        'mode': str(operation.get('mode', '') or ''),
                        'value': operation.get('value'),
                        'applied': False,
                        'host_lag_s': None,
                    })
                    continue
                if operation['kind'] in ('fan_set', 'fan_reset'):
                    fan_events.append({
                        'print_time': float(current_time),
                        'kind': str(operation['kind']),
                        'speed': operation.get('speed'),
                        'applied': False,
                        'host_lag_s': None,
                    })
                    continue
                segment = _u1_stock_e_move_profile(operation, acceleration)
                direction = 1.0 if segment['distance'] > 0.0 else -1.0
                segment_start = current_time
                trapq_append(
                    tip_trapq, current_time,
                    segment['accel_t'], segment['cruise_t'],
                    segment['decel_t'], current_position, 0.0, 0.0,
                    1.0, 0.0, 0.0,
                    direction * segment['start_v'],
                    direction * segment['cruise_v'],
                    direction * acceleration, 0xFFFFFFFF)
                duration = (segment['accel_t'] + segment['cruise_t'] +
                            segment['decel_t'])
                current_time += duration
                current_position += segment['distance']
                move_count += 1
                debug_segments.append({
                    'index': int(move_count),
                    'distance_mm': float(segment['distance']),
                    'requested_mm_s': float(operation['speed']),
                    'cruise_mm_s': float(segment['cruise_v']),
                    'start_v_mm_s': 0.0,
                    'end_v_mm_s': 0.0,
                    'duration_ms': float(duration * 1000.0),
                    'start_print_time': float(segment_start),
                    'end_print_time': float(current_time),
                })
            if marker_print_time is None:
                marker_print_time = current_time
            if move_count <= 0 or marker_print_time <= start_time or \
                    marker_print_time >= current_time:
                raise EndpointError(
                    'background tip did not produce a valid internal marker time')

            vref_sw = getattr(extruder, 'vref_sw', None)
            if vref_sw is not None:
                vref_sw._set_pin(current_time + 0.100, 0)
            if enable is not None and not was_enabled:
                enable.motor_disable(current_time + 0.150)
            restore_after_time = current_time + 0.250

            generate_host_started = reactor.monotonic()
            stepper.generate_steps(current_time + 0.001)
            generate_host_finished = reactor.monotonic()
            motion_generated = True
            trapq_finalize(
                tip_trapq, current_time + 99999.9,
                current_time + 99999.9)

            self._parked_tip_tail_generation += 1
            ticket = {
                'id': int(self._parked_tip_tail_generation),
                'head': self._head(),
                'start_print_time': start_time,
                'marker_print_time': marker_print_time,
                'end_print_time': current_time,
                'duration_s': max(0.0, current_time - start_time),
                'positive_duration_s': max(0.0, marker_print_time - start_time),
                'negative_duration_s': max(0.0, current_time - marker_print_time),
                'pre_marker_duration_s': max(0.0, marker_print_time - start_time),
                'post_marker_duration_s': max(0.0, current_time - marker_print_time),
                'marker_gap_s': 0.0,
                'scheduler_lead_s': max(0.0, scheduler_lead),
                'start_position': start_position,
                'end_position': current_position,
                'move_count': move_count,
                'restore_after_print_time': restore_after_time,
                'lane_restored': False,
                'lane_fault': '',
                'cancelled_while_queued': False,
                'temperature_event_count': len(temperature_events),
                'temperature_events_complete': not bool(temperature_events),
                '_temperature_events': temperature_events,
                '_temperature_event_timer': None,
                'fan_event_count': len(fan_events),
                'fan_events_complete': not bool(fan_events),
                'fan_event_error': '',
                '_fan_events': fan_events,
                '_fan_event_timer': None,
                '_temperature_base_target': float(base_tip_temp),
                '_stepper': stepper,
                '_extruder': extruder,
                '_mcu': mcu,
                '_toolhead': toolhead,
                '_generator_index': generator_index,
                '_generator_handler': generator_handler,
                '_original_kinematics': original_kinematics,
                '_original_trapq': original_trapq,
                '_tail_kinematics': tip_kinematics,
                '_tail_trapq': tip_trapq,
                '_lane_restored': False,
                '_debug_segments': debug_segments,
            }
            self._parked_tip_tail_ticket = ticket
            mcu_flush_host_started = reactor.monotonic()
            mcu.flush_moves(
                restore_after_time, max(0.0, start_time - 30.0))
            mcu_flush_host_finished = reactor.monotonic()
            ticket['debug_flush_step_generation_ms'] = max(
                0.0, (flush_host_finished - flush_host_started) * 1000.0)
            ticket['debug_generate_steps_ms'] = max(
                0.0, (generate_host_finished - generate_host_started) * 1000.0)
            ticket['debug_mcu_flush_ms'] = max(
                0.0, (mcu_flush_host_finished - mcu_flush_host_started) * 1000.0)
            ticket['debug_queue_total_ms'] = max(
                0.0, (mcu_flush_host_finished - queue_host_started) * 1000.0)
            try:
                self._schedule_background_tip_temperature_events(ticket)
            except Exception as exc:

                ticket['temperature_event_error'] = str(exc)
                ticket['temperature_events_complete'] = False
                logging.exception(
                    'BMCU could not schedule source Head tip temperature events')
            try:
                self._schedule_background_tip_fan_events(ticket)
            except Exception as exc:
                ticket['fan_event_error'] = str(exc)
                ticket['fan_events_complete'] = False
                logging.exception(
                    'BMCU could not schedule source Head tip hotend-fan events')
        except Exception as exc:
            if motion_generated:
                if ticket is not None:
                    ticket['lane_fault'] = str(exc)
                logging.exception(
                    'BMCU exact background tip failed after step commit; '
                    'source generator remains detached until Klipper restart')
                raise EndpointError(
                    'exact background tip failed after step commit; '
                    'Klipper restart is required: %s' % exc)
            try:
                if kinematics_attached and \
                        stepper.get_stepper_kinematics() is tip_kinematics:
                    stepper.set_stepper_kinematics(original_kinematics)
                if trapq_attached and stepper.get_trapq() is tip_trapq:
                    stepper.set_trapq(original_trapq)
                stepper.set_position((start_position, 0.0, 0.0))
                if generator_detached:
                    generators = toolhead.step_generators
                    duplicate = any(
                        getattr(handler, '__self__', None) is stepper and
                        getattr(handler, '__func__', None) is
                        getattr(stepper.generate_steps, '__func__', None)
                        for handler in generators)
                    if not duplicate:
                        generators.insert(
                            min(generator_index, len(generators)),
                            generator_handler)
            except Exception:
                logging.exception(
                    'BMCU could not roll back uncommitted exact background tip')
            raise

        logging.info(
            'BMCU queued Head %d complete signed tip program on one isolated '
            'source-MCU solver: %d moves, %d temperature events, %d fan events, %.3fs, '
            'pre-marker %.3fs, post-marker %.3fs, marker gap 0.000ms, '
            'scheduler lead %.3fms',
            self._head() + 1, move_count,
            int(ticket.get('temperature_event_count', 0) or 0),
            int(ticket.get('fan_event_count', 0) or 0),
            ticket['duration_s'], ticket['positive_duration_s'],
            ticket['negative_duration_s'],
            ticket['scheduler_lead_s'] * 1000.0)
        if getattr(self.manager, 'debug_enabled', False):
            self.manager._debug_log(
                'U1 %s TIP exact-single-lane marker_gap_ms=0.000 '
                'scheduler_lead_ms=%.3f pre_marker_ms=%.3f post_marker_ms=%.3f '
                'total_ms=%.3f flush_stepgen_ms=%.3f generate_steps_ms=%.3f '
                'mcu_flush_ms=%.3f queue_total_ms=%.3f segments=%s',
                self.name, ticket['scheduler_lead_s'] * 1000.0,
                ticket['positive_duration_s'] * 1000.0,
                ticket['negative_duration_s'] * 1000.0,
                ticket['duration_s'] * 1000.0,
                float(ticket.get('debug_flush_step_generation_ms', 0.0)),
                float(ticket.get('debug_generate_steps_ms', 0.0)),
                float(ticket.get('debug_mcu_flush_ms', 0.0)),
                float(ticket.get('debug_queue_total_ms', 0.0)),
                copy.deepcopy(ticket.get('_debug_segments', [])))
        return self._parked_tip_tail_public(ticket)

    def wait_background_tip_marker(self, ticket_id):

        ticket = self._parked_tip_tail_ticket
        if ticket is None or int(ticket.get('id', -1)) != int(ticket_id):
            raise EndpointError('background tip ticket is unavailable at marker wait')
        marker_time = float(ticket.get('marker_print_time', 0.0))
        if marker_time <= 0.0:
            raise EndpointError('background tip marker time is unavailable')
        mcu = ticket['_mcu']
        reactor = self.manager.reactor
        wait_started = reactor.monotonic()
        while True:
            now = reactor.monotonic()
            estimated = float(mcu.estimated_print_time(now))
            remaining = marker_time - estimated
            if remaining <= 0.0:
                break
            is_shutdown = getattr(mcu, 'is_shutdown', None)
            if callable(is_shutdown) and is_shutdown():
                ticket['lane_fault'] = (
                    'source MCU shutdown before background tip marker')
                raise EndpointError(
                    'source MCU shutdown before background tip marker')

            reactor.pause(now + max(0.001, remaining - 0.002))
        finished = reactor.monotonic()
        observed = float(mcu.estimated_print_time(finished))
        lag = max(0.0, observed - marker_time)
        if getattr(self.manager, 'debug_enabled', False):
            self.manager._debug_log(
                'U1 %s TIP marker reached marker_print_time=%.6f '
                'observed_print_time=%.6f host_lag_ms=%.3f wait_host_ms=%.3f '
                'note=post_marker_program_already_committed',
                self.name, marker_time, observed, lag * 1000.0,
                max(0.0, (finished - wait_started) * 1000.0))
        return {
            'id': int(ticket['id']),
            'marker_print_time': marker_time,
            'observed_print_time': observed,
            'host_lag_s': lag,
        }

    def queue_parked_tip_tail(self, operations, source_logically_active=False,
                              source_must_be_parked=True):

        raise EndpointError(
            'tail-only background tip scheduling is disabled; exact single-lane '
            'tip program must be queued before E motion')

    def wait_parked_tip_tail(self, ticket_id, cancel_check=None,
                             poll_interval=0.500):
        ticket = self._parked_tip_tail_ticket
        if ticket is None or int(ticket.get('id', -1)) != int(ticket_id):
            raise EndpointError('parked Head tip-tail ticket is unavailable')
        poll_interval = min(max(float(poll_interval), 0.100), 1.000)
        cancelled = False
        mcu = ticket['_mcu']
        restore_after = float(ticket.get(
            'restore_after_print_time', ticket['end_print_time'] + 0.250))
        while mcu.estimated_print_time(
                self.manager.reactor.monotonic()) < restore_after:
            is_shutdown = getattr(mcu, 'is_shutdown', None)
            if callable(is_shutdown) and is_shutdown():
                ticket['lane_fault'] = (
                    'source MCU shutdown before isolated tip tail completed')
                raise EndpointError(
                    'parked Head MCU shutdown before isolated tip tail completed')
            if callable(cancel_check) and cancel_check():
                cancelled = True
            self.manager.reactor.pause(
                self.manager.reactor.monotonic() + poll_interval)
        try:
            self._restore_parked_tip_tail_lane(ticket)
        except Exception as exc:
            ticket['lane_fault'] = str(exc)
            logging.exception(
                'BMCU could not restore isolated parked-tail lane; '
                'Klipper restart is required')
            raise EndpointError(
                'parked Head isolated tail restore failed; '
                'Klipper restart is required: %s' % exc)
        temperature_error = str(
            ticket.get('temperature_event_error', '') or '').strip()
        temperature_pending = bool(
            int(ticket.get('temperature_event_count', 0) or 0) and
            not ticket.get('temperature_events_complete'))
        fan_error = str(ticket.get('fan_event_error', '') or '').strip()
        fan_pending = bool(
            int(ticket.get('fan_event_count', 0) or 0) and
            not ticket.get('fan_events_complete'))

        try:
            self._reset_tip_hotend_fan_override()
        except Exception as exc:
            logging.exception(
                'BMCU could not restore source Head hotend fan after tip tail')
            if not fan_error:
                fan_error = str(exc)
        if self._parked_tip_tail_ticket is ticket:
            self._parked_tip_tail_ticket = None
        if temperature_error:
            raise EndpointError(
                'parked Head tip temperature program failed: %s' %
                temperature_error)
        if temperature_pending:
            raise EndpointError(
                'parked Head tip temperature program did not complete by the '
                'end of the source-MCU tip timeline')
        if fan_error:
            raise EndpointError(
                'parked Head tip hotend-fan program failed: %s' % fan_error)
        if fan_pending:
            raise EndpointError(
                'parked Head tip hotend-fan program did not complete by the '
                'end of the source-MCU tip timeline')
        ticket['cancelled_while_queued'] = cancelled
        result = self._parked_tip_tail_public(ticket)
        logging.info(
            'BMCU restored parked Head %d normal solver after isolated tip tail',
            self._head() + 1)
        return result

    def delegates_long_unload_to_feeder(self):
        mode = str(self.get('u1_unload_mode', 'fast') or 'fast').lower()
        return self._native_hotend_sequences_enabled() and mode == 'fast'

    def assist_unload(self, material='', maximum_mm=120.0):
        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).assist_unload(
                material, maximum_mm)
        limit = _coerce_finite_float(maximum_mm)
        if limit is None or limit < 0.0 or limit > 500.0:
            raise EndpointError('U1 toolhead unload assist must be 0..500 mm')
        sensor_before = self.sensor_detected('entry_sensor')

        if self.delegates_long_unload_to_feeder():
            return {
                'limit_mm': 0.0,
                'requested_limit_mm': limit,
                'moved_mm': 0.0,
                'sensor_before': sensor_before,
                'sensor_after': sensor_before,
                'sensor_cleared': sensor_before is False,
                'delegated_to_bmcu': True,
            }

        if sensor_before is None:
            if bool(self.get('u1_require_head_confirmation', True)):
                raise EndpointError(
                    'U1 toolhead filament sensor is unavailable during unload')
            return {
                'limit_mm': limit, 'moved_mm': 0.0,
                'sensor_before': None, 'sensor_after': None,
                'sensor_cleared': False,
            }
        if sensor_before is False:
            return {
                'limit_mm': limit, 'moved_mm': 0.0,
                'sensor_before': False, 'sensor_after': False,
                'sensor_cleared': True,
            }
        if limit <= 0.0:
            raise EndpointError(
                'U1 toolhead still holds filament and unload assist is disabled')

        chunk = _coerce_finite_float(
            self.get('u1_unload_assist_chunk_mm', 5.0))
        feed = _coerce_finite_float(
            self.get('u1_unload_assist_feed', 600.0))
        settle = _coerce_finite_float(
            self.get('u1_unload_assist_settle_s', 0.08))
        chunk = 5.0 if chunk is None else min(max(chunk, 1.0), 20.0)
        feed = 600.0 if feed is None else min(max(feed, 60.0), 1800.0)
        settle = 0.08 if settle is None else min(max(settle, 0.0), 0.5)

        moved = 0.0
        sensor_after = sensor_before
        while sensor_after is True and moved + 0.0001 < limit:
            step = min(chunk, limit - moved)
            self.extrude(-step, int(round(feed)))
            moved += step
            if settle > 0.0:
                self.manager.reactor.pause(
                    self.manager.reactor.monotonic() + settle)
            sensor_after = self.sensor_detected('entry_sensor')
            if sensor_after is None:
                raise EndpointError(
                    'U1 toolhead filament sensor became unavailable during unload')

        if sensor_after is not False:
            raise EndpointError(
                'U1 toolhead still detects filament after %.1f mm assist; '
                'BMCU pullback was not started' % moved)
        return {
            'limit_mm': limit, 'moved_mm': moved,
            'sensor_before': sensor_before, 'sensor_after': sensor_after,
            'sensor_cleared': True,
        }

    def _snap_tail_context(self, material='', temperature_profile=None):
        if not bool(self.get('snap_tail_handoff_enabled', True)):
            raise EndpointError(
                'Snapmaker detached-tail handoff is disabled on %s' %
                self.name)
        context = self._filament_context(material, temperature_profile)
        chunk = _coerce_finite_float(self.get('snap_tail_chunk_mm', 20.0))
        cleanup = _coerce_finite_float(
            self.get('snap_tail_cleanup_every_mm', 20.0))
        maximum = _coerce_finite_float(self.get('snap_tail_max_mm', 1500.0))
        hard_feed = _coerce_finite_float(self.get('snap_tail_feed', 360.0))
        soft_feed = _coerce_finite_float(
            self.get('snap_tail_soft_feed', 120.0))
        settle = _coerce_finite_float(self.get('snap_tail_settle_s', 0.05))
        values = {
            'chunk_mm': chunk,
            'cleanup_every_mm': cleanup,
            'max_mm': maximum,
            'feed': soft_feed if context['soft'] else hard_feed,
            'settle_s': settle,
        }
        limits = {
            'chunk_mm': (1.0, 50.0),
            'cleanup_every_mm': (5.0, 100.0),
            'max_mm': (50.0, 5000.0),
            'feed': (30.0, 1200.0),
            'settle_s': (0.0, 1.0),
        }
        for key, value in values.items():
            minimum, maximum_value = limits[key]
            if value is None or value < minimum or value > maximum_value:
                raise EndpointError(
                    '%s must be between %.1f and %.1f' %
                    (key, minimum, maximum_value))
        context.update(values)
        return context

    def _snap_tail_discard(self, final=False):

        required = (
            'INNER_CUTOFF_BASE_DISCARD',
            'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD',
            'INNER_DISCARD_FILAMENT_BASE_DISCARD',
        )
        missing = [name for name in required if not self._command_exists(name)]
        if missing:
            raise EndpointError(
                'Snapmaker tail discard macros are unavailable: %s' %
                ', '.join(missing))
        action = 4 if final else 1
        state_name = 'BMCU_SNAP_DISCARD_%s' % re.sub(
            r'[^A-Za-z0-9_]', '_', str(self.name or 'ENDPOINT').upper())
        self._run('SAVE_GCODE_STATE NAME=%s' % state_name,
                  wait_moves=False)
        try:
            self._run('\n'.join([
                'INNER_CUTOFF_BASE_DISCARD',
                'INNER_DISCARD_FILAMENT_BASE_DISCARD',
                'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=%d' % action,
                'M400',
            ]), wait_moves=False)
        finally:
            self._run(
                'RESTORE_GCODE_STATE NAME=%s MOVE=0' % state_name,
                wait_moves=False)

    def prepare_runout_tail_handoff(self, material='',
                                    temperature_profile=None,
                                    cancel_check=None,
                                    restore_heater=True):

        context = self._snap_tail_context(material, temperature_profile)
        if not self._command_exists('MOVE_TO_DISCARD_FILAMENT_POSITION'):
            raise EndpointError(
                'Snapmaker discard-position macro is unavailable')
        sensor_before = self.sensor_detected('entry_sensor')
        if sensor_before is None:
            raise EndpointError(
                'Snapmaker head filament sensor is unavailable during tail handoff')
        previous_target = self.capture_heater_target()
        state_name = 'BMCU_SNAP_TAIL_%s' % re.sub(
            r'[^A-Za-z0-9_]', '_', str(self.name or 'ENDPOINT').upper())
        total = 0.0
        since_cleanup = 0.0
        samples = []
        state_saved = False
        completed = False
        try:
            self._run('SAVE_GCODE_STATE NAME=%s' % state_name,
                  wait_moves=False)
            state_saved = True
            self._ensure_xy_homed()
            self._run('MOVE_TO_DISCARD_FILAMENT_POSITION')
            self._run('M109 S%d' % int(round(context['unload_temp'])))
            sensor = sensor_before
            while sensor is True and total + 0.0001 < context['max_mm']:
                if callable(cancel_check) and cancel_check():
                    raise EndpointError(
                        'Snapmaker detached-tail handoff was cancelled before sensor clear')
                step = min(context['chunk_mm'], context['max_mm'] - total)
                self.extrude(step, int(round(context['feed'])))
                total += step
                since_cleanup += step
                if context['settle_s'] > 0.0:
                    self.manager.reactor.pause(
                        self.manager.reactor.monotonic() +
                        context['settle_s'])
                sensor = self.sensor_detected('entry_sensor')
                if sensor is None:
                    raise EndpointError(
                        'Snapmaker head filament sensor became unavailable during tail handoff')
                samples.append({'total_mm': total, 'sensor': sensor})
                if (sensor is True and
                        since_cleanup + 0.0001 >=
                        context['cleanup_every_mm']):
                    self._snap_tail_discard(final=False)
                    since_cleanup = 0.0
            if sensor is not False:
                raise EndpointError(
                    'Snapmaker head sensor still detects the exhausted tail after %.1f mm' %
                    total)
            if callable(cancel_check) and cancel_check():
                raise EndpointError(
                    'Snapmaker detached-tail handoff was cancelled after sensor clear')
            if total > 0.0:

                self._snap_tail_discard(final=False)
            self._run('M400', wait_moves=False)
            completed = True
            return {
                'ok': True,
                'ready_for_follower': True,
                'sensor_before': sensor_before,
                'sensor_after': False,
                'sensor_clear_at_mm': total,
                'total_mm': total,
                'chunk_mm': context['chunk_mm'],
                'cleanup_every_mm': context['cleanup_every_mm'],
                'feed': context['feed'],
                'temperature': context['unload_temp'],
                'temperature_source': context['temperature_source'],
                'samples': samples[-16:],
            }
        finally:
            if (previous_target is not None and
                    (bool(restore_heater) or not completed)):
                try:
                    self.restore_heater_target(previous_target)
                except Exception:
                    logging.exception(
                        'BMCU could not restore Snapmaker heater target after tail handoff')
            if not completed:
                try:
                    self._run('M400', wait_moves=False)
                except Exception:
                    logging.exception(
                        'BMCU could not synchronize failed Snapmaker tail handoff')
            if state_saved:
                try:
                    self._run(
                        'RESTORE_GCODE_STATE NAME=%s MOVE=0' % state_name,
                        wait_moves=False)
                except Exception:
                    logging.exception(
                        'BMCU could not restore G-code state after Snapmaker tail handoff')

    def discard_runout_tail_chunk(self, final=False):
        self._snap_tail_discard(final=bool(final))

    def refill_prime(self, material='', exact_match=True, temperature_profile=None):
        if not self._native_hotend_sequences_enabled():
            return super(SnapmakerU1Endpoint, self).refill_prime(
                material, exact_match)

        self._native_preextrude(
            material, refill=True, temperature_profile=temperature_profile)
        self.run_macro('refill_prime_macro', endpoint=self.name, material=material)

    def capture_signal(self):

        extruder = self._configured_extruder()
        if extruder is None:
            return None
        started = self.manager.reactor.monotonic()
        value = None
        try:
            probe = getattr(extruder, 'binding_probe', None)
            sensor = getattr(probe, 'sensor', None) if probe is not None else None
            if sensor is not None and hasattr(sensor, 'get_coil_freq'):
                value = _coerce_finite_float(sensor.get_coil_freq())
        except Exception:
            logging.exception('BMCU could not read U1 inductance coil')
        finished = self.manager.reactor.monotonic()
        if (getattr(self.manager, 'debug_enabled', False) and
                isinstance(self._load_temperature_context, dict)):
            reads = self._load_temperature_context.setdefault(
                '_debug_coil_reads', [])
            if isinstance(reads, list) and len(reads) < 24:
                reads.append({
                    'seq': len(reads) + 1,
                    'value': None if value is None else float(value),
                    'host_ms': max(0.0, (finished - started) * 1000.0),
                })
        return value

    def _capture_signal_profile(self, material=''):
        material = str(material or '').upper()
        soft = material.startswith('TPU') or material in ('TPE', 'FLEX')
        default_threshold = 800.0 if soft else 1500.0
        threshold = _coerce_finite_float(self.get(
            'u1_coil_threshold_soft' if soft else 'u1_coil_threshold_hard',
            default_threshold) or default_threshold)
        if threshold is None:
            threshold = default_threshold

        return {'soft': bool(soft), 'threshold': float(threshold)}

    def capture_signal_ok(self, delta, material=''):
        delta = _coerce_finite_float(delta)
        if delta is None:
            return False
        profile = self._capture_signal_profile(material)
        return delta >= float(profile['threshold'])

    def select(self):
        head = self._head()
        self._ensure_xy_homed()

        self.manager._ensure_u1_takeover(self, save=True)
        macro = str(self.get('select_macro', '') or '').strip()
        if macro:
            self.run_macro('select_macro', endpoint=self.name, head=head)
        else:
            self._run('T%d A0' % head)

    def _active_extruder_name(self):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None or not hasattr(toolhead, 'get_extruder'):
            return ''
        try:
            return str(toolhead.get_extruder().get_name())
        except Exception:
            try:
                return str(toolhead.get_extruder().name)
            except Exception:
                return ''

    def _park_selected_head(self, purpose, timeout=None):
        head = self._head()
        self._ensure_xy_homed()
        expected = self._configured_extruder_name()
        active = self._active_extruder_name()
        if active != expected:
            raise EndpointError(
                'Snapmaker Head %d cannot be parked for %s: active extruder '
                'is %s, expected %s' %
                (head + 1, purpose, active or '<unknown>', expected))
        command = 'PARK_EXTRUDER' if head == 0 else 'PARK_EXTRUDER%d' % head
        logging.info(
            'BMCU Snapmaker Head %d stock park start for %s: %s',
            head + 1, purpose, command)
        self._run(command)
        self._run('M400', wait_moves=False)
        if timeout is None:
            timeout = float(self.get(
                'u1_prestage_park_timeout', 20.0) or 20.0)
        self.wait_parked_for_prestage(timeout=timeout)
        logging.info(
            'BMCU Snapmaker Head %d park confirmed for %s',
            head + 1, purpose)
        return True

    def park_selected_head_for_tip_retract(self, timeout=None):

        self._park_selected_head('tip-forming retract tail', timeout=timeout)
        expected = self._configured_extruder_name()
        active = self._active_extruder_name()
        if active != expected:
            raise EndpointError(
                'Snapmaker parked Head %d lost active extruder ownership '
                'before its tip-forming retract tail: active=%s expected=%s' %
                (self._head() + 1, active or '<unknown>', expected))
        return True

    def park_selected_head_for_pullback(self, timeout=None):

        return self._park_selected_head(
            'BMCU long pullback', timeout=timeout)

    def verify_parked_for_prestage(self):

        required = bool(self.get('u1_require_head_confirmation', True))
        extruder = self.printer.lookup_object(
            str(self.get('extruder', 'extruder')), None)
        if extruder is None:
            if required:
                raise EndpointError(
                    'Snapmaker parked head cannot be confirmed for prestage')
            return True
        try:
            if not hasattr(extruder, 'get_park_detector_status'):
                if required:
                    raise EndpointError(
                        'Snapmaker park detector is unavailable for prestage')
                return True
            status = extruder.get_park_detector_status()
            state = str((status or {}).get('state', '')).upper()
            if state != 'PARKED':
                raise EndpointError(
                    'Snapmaker head %d is not PARKED; prestage is blocked' %
                    self._head())
        except EndpointError:
            raise
        except Exception as exc:
            if required:
                raise EndpointError(
                    'Snapmaker parked head confirmation failed: %s' % exc)
        return True

    def wait_parked_for_prestage(self, timeout=0.0):

        timeout = _coerce_finite_float(timeout)
        timeout = 0.0 if timeout is None else min(max(timeout, 0.0), 60.0)
        deadline = self.manager.reactor.monotonic() + timeout
        last_error = None
        while True:
            try:
                return self.verify_parked_for_prestage()
            except EndpointError as exc:
                last_error = exc
            now = self.manager.reactor.monotonic()
            if now >= deadline:
                raise EndpointError(
                    'Snapmaker Head %d did not become PARKED within %.1f s: %s' %
                    (self._head() + 1, timeout, last_error))
            self.manager.reactor.pause(min(deadline, now + 0.05))

    def verify_prestage_safe(self):

        self.verify_parked_for_prestage()
        return super(SnapmakerU1Endpoint, self).verify_prestage_safe()

    def wait_prestage_safe(self, timeout=0.0):
        timeout = _coerce_finite_float(timeout)
        timeout = 0.0 if timeout is None else min(max(timeout, 0.0), 60.0)
        deadline = self.manager.reactor.monotonic() + timeout
        last_error = None
        while True:
            try:
                return self.verify_prestage_safe()
            except EndpointError as exc:
                last_error = exc
            now = self.manager.reactor.monotonic()
            if now >= deadline:
                raise EndpointError(
                    'Snapmaker head %d did not become safe for background '
                    'prestage: %s' % (self._head(), last_error))
            self.manager.reactor.pause(min(deadline, now + 0.05))

    def prepare_prestage(self):
        self.manager._ensure_u1_takeover(self, save=True)
        super(SnapmakerU1Endpoint, self).prepare_prestage()

    def prepare_selected_prestage(self):

        self.manager._ensure_u1_takeover(self, save=True)
        self.verify_selected()
        Endpoint.verify_prestage_safe(self)
        self.run_macro('prepare_prestage_macro', endpoint=self.name,
                       head=self.get('head_index', -1))

    def verify_selected(self):
        head = self._head()
        required = bool(self.get('u1_require_head_confirmation', True))
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            if required:
                raise EndpointError('U1 toolhead object is unavailable; selected head cannot be confirmed')
            return True
        try:
            extruder = toolhead.get_extruder()
            active_name = (extruder.get_name() if hasattr(extruder, 'get_name')
                           else str(extruder))
            active_name = str(active_name).strip()
            expected = str(self.get('extruder', 'extruder%d' % head)).strip()
            if expected and active_name != expected:
                raise EndpointError('U1 head %d selected but active extruder is %s' %
                                    (head, active_name))

            if hasattr(extruder, 'get_extruder_activate_status'):
                activation = extruder.get_extruder_activate_status()
                if not activation or not isinstance(activation[0], (list, tuple)):
                    raise EndpointError('U1 head %d park/grab status is unavailable' % head)
                confirmed_name = str(activation[0][0]).strip()
                state_code = int(activation[0][1])
                if state_code != 0 or (expected and confirmed_name != expected):
                    raise EndpointError(
                        'U1 head %d park/grab verification failed: %s' %
                        (head, activation[0]))
        except EndpointError:
            raise
        except Exception as exc:
            if required:
                raise EndpointError('U1 head %d confirmation failed: %s' % (head, exc))
        return True

    def supports_background_prestage(self):
        return bool(self.get('prestage_while_unselected', False))

    def capabilities(self):
        result = super(SnapmakerU1Endpoint, self).capabilities()
        module, channel = self.native_feeder_target()
        state = self._native_feeder_status()
        result.update({
            'u1_native_feeder_takeover': self.native_feeder_takeover_enabled(),
            'u1_require_feeder_confirmation': self.native_feeder_confirmation_required(),
            'u1_feeder_module': module,
            'u1_feeder_channel': channel,
            'u1_native_feeder_disabled': None if state is None else bool(state.get('disable_auto', False)),
            'u1_native_path': self.native_path_status(),
            'u1_sensor_takeover': False,
            'u1_sensor_mode': 'read_only',
            'cross_endpoint_refill': True,
            'cross_endpoint_refill_mode': 'snapmaker_u1',
            'u1_native_hotend_sequences': self._native_hotend_sequences_enabled(),
            'u1_unload_mode': str(self.get('u1_unload_mode', 'fast')).lower(),
            'u1_require_coil_confirmation': True,
            'u1_coil_confirmation_mode': 'passive_downstream_presence',
            'u1_require_head_confirmation': bool(self.get('u1_require_head_confirmation', True)),
            'u1_last_nozzle_search_mm': self.config.get('_u1_last_nozzle_search_mm'),
            'u1_last_coil_delta': self.config.get('_u1_last_coil_delta'),
            'u1_last_coil_capture_delta': self.config.get(
                '_u1_last_coil_capture_delta'),
            'u1_last_coil_final_delta': self.config.get(
                '_u1_last_coil_final_delta'),
            'u1_last_coil_path_delta': self.config.get(
                '_u1_last_coil_path_delta'),
            'u1_last_coil_confirmed': self.config.get('_u1_last_coil_confirmed'),
            'u1_last_coil_threshold': self.config.get('_u1_last_coil_threshold'),
            'u1_last_toolhead_verify_elapsed_s': self.config.get(
                '_u1_last_toolhead_verify_elapsed_s'),
            'editable_u1_tip_profiles': True,
        })
        return result

    def integration_check(self, require_runtime_ownership=True):
        checks = []

        def add(name, ok, detail='', required=True):
            checks.append({
                'name': str(name), 'ok': bool(ok), 'required': bool(required),
                'detail': str(detail or ''),
            })

        validation = self.validate()
        add('endpoint configuration', validation.get('valid', False),
            '; '.join(validation.get('errors', [])))

        head = self._head()
        try:
            _task, task_config = self._task_config(strict=True)
            add('U1 print_task_config schema', True,
                '4 physical heads, 32 logical tools, runtime backup API')
            managed_coherent = self._projection_color_coherent(task_config, head)
            add('managed head color coherence', managed_coherent,
                'filament_color, filament_color_rgba and filament_color_multi must agree')
            incoherent = [index for index in range(4)
                          if not self._projection_color_coherent(task_config, index)]
            add('all U1 head color coherence', not incoherent,
                'incoherent heads: %s' %
                (','.join(str(index) for index in incoherent) if incoherent else 'none'),
                required=False)
        except EndpointError as exc:
            task_config = None
            add('U1 print_task_config schema', False, exc)
            add('managed head color coherence', False, 'schema unavailable')

        feeder_state = None
        try:
            feeder_state = self._native_feeder_status(strict=True)
            add('native feeder status', feeder_state is not None,
                '%s Channel %d' % self.native_feeder_target())
        except Exception as exc:
            add('native feeder status', False, exc)
        if self.native_feeder_takeover_enabled():
            add('native feeder ownership',
                feeder_state is not None and bool(feeder_state.get('disable_auto', False)),
                'Snapmaker feeder must be disabled for the BMCU-managed head',
                required=bool(require_runtime_ownership))

        extruder = self.printer.lookup_object(str(self.get('extruder', 'extruder')), None)
        add('physical extruder object', extruder is not None, self.get('extruder', 'extruder'))
        add('head park detector', extruder is not None and
            hasattr(extruder, 'get_park_detector_status'),
            'required for safe parked-head prestage',
            required=self.supports_background_prestage() or
                     bool(self.get('u1_require_head_confirmation', True)))

        allow_nonstandard = bool(self.get('u1_allow_nonstandard_topology', False))
        expected_extruder = 'extruder' if head == 0 else 'extruder%d' % head
        configured_extruder = str(self.get('extruder', expected_extruder) or '').strip()
        add('stock U1 extruder mapping', configured_extruder == expected_extruder,
            '%s expected, configured %s' % (expected_extruder, configured_extruder),
            required=not allow_nonstandard)

        expected_module, expected_channel = self._FEEDER_MAP[head]
        configured_module, configured_channel = self.native_feeder_target()
        add('stock U1 feeder mapping',
            (configured_module, configured_channel) ==
            (expected_module, expected_channel),
            '%s/%d expected, configured %s/%d' %
            (expected_module, expected_channel,
             configured_module, configured_channel),
            required=not allow_nonstandard)

        sensor_name = str(self.get('motion_sensor', '') or
                          self.get('entry_sensor', '') or '').strip()
        normalized_sensor = _normalize_sensor_name(sensor_name) if sensor_name else ''
        expected_sensor = 'filament_motion_sensor e%d_filament' % head
        add('stock U1 filament sensor mapping',
            normalized_sensor == expected_sensor,
            '%s expected, configured %s' %
            (expected_sensor, normalized_sensor or 'none'),
            required=not allow_nonstandard)
        sensor = (self.printer.lookup_object(normalized_sensor, None)
                  if normalized_sensor else None)
        add('head filament sensor', sensor is not None, sensor_name or 'not configured')

        sensor_status = None
        if sensor is not None and hasattr(sensor, 'get_status'):
            try:
                sensor_status = sensor.get_status(self.manager.reactor.monotonic())
            except TypeError:
                sensor_status = sensor.get_status()
            except Exception as exc:
                add('head sensor read-only observer', False, exc, required=False)
        if sensor_status is not None:
            add('head sensor read-only observer', True,
                'stock callback/enabled state preserved (enabled=%s)' %
                sensor_status.get('enabled', 'unknown'), required=False)

        coil = None
        try:
            probe = getattr(extruder, 'binding_probe', None) if extruder is not None else None
            coil = getattr(probe, 'sensor', None) if probe is not None else None
        except Exception:
            coil = None
        add('inductance coil', coil is not None and hasattr(coil, 'get_coil_freq'),
            'required read-only toolhead-passage proof before U1 prime/ON_USE',
            required=self._native_hotend_sequences_enabled())

        native_sequences = self._native_hotend_sequences_enabled()
        add('filament parameters',
            self.printer.lookup_object('filament_parameters', None) is not None,
            'material-aware load/unload temperatures',
            required=native_sequences and not allow_nonstandard)
        detector = self.printer.lookup_object('filament_detect', None)
        add('RFID/material detector', detector is not None,
            'required to clear stale U1 slot/RFID cache',
            required=not allow_nonstandard)
        detector_status = getattr(detector, 'get_status', None) if detector is not None else None
        detector_state_ok = False
        detector_detail = 'status API unavailable'
        if callable(detector_status):
            try:
                try:
                    detector_info = detector_status(self.manager.reactor.monotonic())
                except TypeError:
                    detector_info = detector_status()
                states = detector_info.get('state') if isinstance(detector_info, dict) else None
                detector_state_ok = isinstance(states, (list, tuple)) and len(states) >= 4
                detector_detail = 'four-channel asynchronous detector state' if detector_state_ok else 'invalid state schema'
            except Exception as exc:
                detector_detail = str(exc)
        add('RFID detector state API', detector_state_ok, detector_detail,
            required=not allow_nonstandard)

        routed = [
            '%s Channel %d' % (device.name, channel)
            for device in getattr(self.manager, 'devices', [])
            for channel in range(4)
            if self.manager._channel_endpoint_name(device, channel) == self.name]
        add('BMCU Channel routes', len(routed) >= 1,
            ', '.join(routed) if routed else 'no BMCU Channel is routed to this head')

        required_ok = all(item['ok'] for item in checks if item['required'])
        return {
            'ok': required_ok, 'endpoint': self.name, 'head': head,
            'checks': checks,
        }

    def validate(self):
        result = super(SnapmakerU1Endpoint, self).validate()
        try:
            module, channel = self.native_feeder_target()
            head = self._head()
            allow_nonstandard = bool(self.get('u1_allow_nonstandard_topology', False))
            expected_module, expected_channel = self._FEEDER_MAP[head]
            expected_extruder = 'extruder' if head == 0 else 'extruder%d' % head
            expected_sensor = 'filament_motion_sensor e%d_filament' % head
            configured_extruder = str(self.get('extruder', expected_extruder) or '').strip()
            sensor_name = str(self.get('motion_sensor', '') or
                              self.get('entry_sensor', '') or '').strip()
            configured_sensor = _normalize_sensor_name(sensor_name) if sensor_name else ''
            topology_errors = []
            if configured_extruder != expected_extruder:
                topology_errors.append('extruder must be %s' % expected_extruder)
            if (module, channel) != (expected_module, expected_channel):
                topology_errors.append('feeder must be %s channel %d' %
                                       (expected_module, expected_channel))
            if configured_sensor != expected_sensor:
                topology_errors.append('filament sensor must be %s' % expected_sensor)
            if topology_errors:
                target = result['warnings'] if allow_nonstandard else result['errors']
                target.append('non-standard U1 topology: %s' % '; '.join(topology_errors))
            if self.native_feeder_takeover_enabled():
                obj = self.printer.lookup_object('filament_feed %s' % module, None)
                if obj is None:
                    result['warnings'].append(
                        'U1 feeder object filament_feed %s is not currently available' % module)
        except EndpointError as exc:
            result['errors'].append(str(exc))
        unload_mode = str(self.get('u1_unload_mode', 'fast') or 'fast').lower()
        if unload_mode not in ('fast', 'native_full'):
            result['errors'].append('invalid U1 unload mode %s' % unload_mode)
        for key, default, minimum, maximum in (
                ('u1_load_to_nozzle_mm', 70.0, 1.0, 300.0),
                ('u1_prime_length_mm', 20.0, 0.0, 100.0),
                ('u1_refill_prime_length_mm', 20.0, 0.0, 100.0),
                ('u1_rfid_quiesce_timeout', 3.0, 0.1, 10.0),
                ('u1_prestage_park_timeout', 20.0, 1.0, 60.0),
                ('u1_unload_assist_chunk_mm', 5.0, 1.0, 20.0),
                ('u1_unload_assist_feed', 600.0, 60.0, 1800.0),
                ('u1_unload_assist_settle_s', 0.08, 0.0, 0.5),
                ('snap_tail_chunk_mm', 20.0, 1.0, 50.0),
                ('snap_tail_cleanup_every_mm', 20.0, 5.0, 100.0),
                ('snap_tail_max_mm', 1500.0, 50.0, 5000.0),
                ('snap_tail_feed', 360.0, 30.0, 1200.0),
                ('snap_tail_soft_feed', 120.0, 30.0, 1200.0),
                ('snap_tail_settle_s', 0.05, 0.0, 1.0)):
            try:
                raw = self.get(key, default)
                value = float(default if raw is None else raw)
            except (TypeError, ValueError, OverflowError):
                result['errors'].append('%s must be numeric' % key)
                continue
            if not math.isfinite(value):
                result['errors'].append('%s must be a finite number' % key)
                continue
            if value < minimum or value > maximum:
                result['errors'].append('%s must be between %.1f and %.1f' %
                                        (key, minimum, maximum))
        result['valid'] = not result['errors']
        return result

_ENDPOINT_DRIVER_CLASSES = {
    'generic_single_extruder': Endpoint,
    'snapmaker_u1': SnapmakerU1Endpoint,
}

def register_endpoint_driver(name, driver_class):

    key = str(name or '').strip().lower()
    if not key or re.fullmatch(r'[a-z0-9][a-z0-9_]{0,63}', key) is None:
        raise ValueError('invalid endpoint driver name %r' % name)
    if not isinstance(driver_class, type) or not issubclass(driver_class, Endpoint):
        raise TypeError('endpoint driver must subclass Endpoint')
    existing = _ENDPOINT_DRIVER_CLASSES.get(key)
    if existing is not None and existing is not driver_class:
        raise ValueError('endpoint driver %s is already registered' % key)
    _ENDPOINT_DRIVER_CLASSES[key] = driver_class
    return driver_class

def endpoint_driver_names():
    return tuple(sorted(_ENDPOINT_DRIVER_CLASSES))

def create_endpoint(manager, name, config):
    driver = str(config.get('driver', 'generic_single_extruder')).strip().lower()
    driver_class = _ENDPOINT_DRIVER_CLASSES.get(driver)
    if driver_class is None:
        raise ValueError(
            'unknown endpoint driver %s; available: %s' %
            (driver or '<empty>', ', '.join(endpoint_driver_names())))
    return driver_class(manager, name, config)
