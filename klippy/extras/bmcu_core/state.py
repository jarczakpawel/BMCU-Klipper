# SPDX-License-Identifier: GPL-3.0-or-later
import json
import logging
import math
import os
import re
import stat
import tempfile

STATE_VERSION = 1
STATE_SCHEMA = 'BMCU-KLIPPER-1.0.0'
PRINT_SESSION_SCHEMA = 1
U1_OWNERSHIP_SCHEMA = 1
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_STATE_DEVICES = 1024
MAX_STATE_ENDPOINTS = 256
MAX_U1_OWNERSHIP_RECORDS = 4
MAX_U1_TIP_PROFILES = 64
MAX_LIGHTING_PROFILES = 32
MOTION_POLICY_SCHEMA = 1
MAX_TOOL_MAPPINGS = 256
MAX_STATE_ENTRY_SCAN = 4096
MAX_BACKUPS_PER_TOOL = 255

DEFAULT_LIGHTING = {
    'system_color': '#FFFFFF',
    'system_brightness': 255,
    'filament_brightness': 96,
    'buffer_colors': {
        'minimum': '#0000FF',
        'neutral': '#FF8000',
        'maximum': '#FF0000',
    },
    'status_colors': {
        'idle': '#383532',
        'before_load': '#FFFF00',
        'loading': '#00D52A',
        'active': '#00B0FF',
        'before_unload': '#FFA000',
        'unloading': '#A02DFF',
        'redetect': '#FFFF00',
        'error': '#FF0000',
        'empty': '#000000',
        'pullback': '#A02DFF',
    },
}

DEFAULT_DEVICE_MOTION = {
    'schema': MOTION_POLICY_SCHEMA,
    'load_pressure_pct': 82.0,
    'load_speed_mms': 80.0,
    'pull_speed_mms': 80.0,
    'pull_speed_end_mms': 12.0,
    'jam_timeout_ms': 20000,
    'before_pullback_target_pct': 40.0,
}

def _clamp_float(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(float(minimum), min(float(maximum), value))

def _reject_json_constant(value):
    raise ValueError('non-finite JSON number: %s' % value)

def _clean_color(value, default='#FFFFFF'):
    value = str(value or default).strip().upper()
    if not value.startswith('#'):
        value = '#' + value
    if len(value) != 7 or any(ch not in '0123456789ABCDEF' for ch in value[1:]):
        return default
    return value

def _clean_lighting(raw, legacy_color='#FFFFFF'):
    raw = dict(raw) if isinstance(raw, dict) else {}
    defaults = DEFAULT_LIGHTING
    buffer_raw = raw.get('buffer_colors')
    if not isinstance(buffer_raw, dict):
        buffer_raw = {}
    status_raw = raw.get('status_colors')
    if not isinstance(status_raw, dict):
        status_raw = {}
    system_color = _clean_color(
        raw.get('system_color', legacy_color), defaults['system_color'])

    legacy_brightness = _clean_int(
        raw.get('system_brightness'), defaults['system_brightness'], 0, 255)
    if legacy_brightness != 255:
        rgb = system_color.lstrip('#')
        system_color = '#%02X%02X%02X' % tuple(
            (int(rgb[index:index + 2], 16) * legacy_brightness + 127) // 255
            for index in (0, 2, 4))
    status_colors = {
        key: (value if key == 'redetect' else _clean_color(status_raw.get(key), value))
        for key, value in defaults['status_colors'].items()
    }

    status_colors['pullback'] = status_colors['unloading']
    return {
        'system_color': system_color,
        'system_brightness': 255,
        'filament_brightness': _clean_int(
            raw.get('filament_brightness'),
            defaults['filament_brightness'], 0, 255),
        'buffer_colors': {
            key: _clean_color(buffer_raw.get(key), value)
            for key, value in defaults['buffer_colors'].items()
        },
        'status_colors': status_colors,
    }

def _clean_lighting_profile_name(value):
    value = _clean_text(value, '', 40)
    if (not value or value.upper() == 'DEFAULT' or
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._+()-]*', value) is None):
        return ''
    return value

def _clean_lighting_profiles(raw):
    result = {}
    for name, lighting in _bounded_object_entries(
            raw, MAX_LIGHTING_PROFILES).items():
        clean_name = _clean_lighting_profile_name(name)
        if clean_name and isinstance(lighting, dict):
            result[clean_name] = _clean_lighting(lighting)
    return result

def _lighting_equal(first, second):
    return _clean_lighting(first) == _clean_lighting(second)

def _clean_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('1', 'true', 'yes', 'on', 'enabled'):
            return True
        if normalized in ('0', 'false', 'no', 'off', 'disabled'):
            return False
    return bool(default)

def _clean_text(value, default='', maximum=64):
    value = str(value if value is not None else default).strip()
    value = ''.join(ch for ch in value if ch >= ' ' and ch != '\x7f')
    return value[:maximum]

def _clean_multiline_text(value, maximum=8192):
    value = str(value if value is not None else '')
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    value = ''.join(ch for ch in value if ch == '\n' or ch == '\t' or ch >= ' ')
    return value.encode('utf-8')[:maximum].decode('utf-8', 'ignore').strip()

def _clean_u1_tip_profile(raw):
    raw = dict(raw) if isinstance(raw, dict) else {}
    gcode = raw.get('gcode', raw.get('unload_gcode', ''))
    temperature_mode = raw.get(
        'temperature_mode',
        raw.get('unload_temperature_mode', raw.get('tip_temperature_mode', 'project')))
    temperature = raw.get(
        'temperature',
        raw.get('unload_temperature', raw.get('tip_temperature')))

    if 'mode' not in raw and 'movements' not in raw:
        return {
            'gcode': _clean_multiline_text(gcode, 8192),
            'temperature_mode': _clean_text(
                temperature_mode, 'project', 16).lower(),
            'temperature': temperature,
        }
    movement_defaults = {
        'rigid_standard': '', 'soft_standard': '',
        'rigid_fine': '', 'soft_fine': '',
    }
    movements = raw.get('movements')
    if isinstance(movements, dict):
        for key in movement_defaults:
            movement_defaults[key] = _clean_multiline_text(
                movements.get(key), 2048)
    return {
        'mode': _clean_text(raw.get('mode'), 'movements', 16).lower(),
        'gcode': _clean_multiline_text(gcode, 8192),
        'movements': movement_defaults,
        'temperature_mode': _clean_text(
            temperature_mode, 'project', 16).lower(),
        'temperature': temperature,
    }

def _clean_u1_tip_profiles(raw):
    if not isinstance(raw, dict):
        return {}
    result = {}
    if isinstance(raw.get('default'), dict):
        result['default'] = _clean_u1_tip_profile(raw['default'])
    materials = {}
    for name, profile in _bounded_object_entries(
            raw.get('materials'), MAX_U1_TIP_PROFILES).items():
        material = _clean_text(name, '', 40).upper()
        if (re.fullmatch(r'[A-Z0-9][A-Z0-9 ._+/-]*', material) is not None and
                isinstance(profile, dict)):
            materials[material] = _clean_u1_tip_profile(profile)
    if materials:
        result['materials'] = materials
    return result

def _clean_endpoint_name(value, default=''):
    value = _clean_text(value, default, 64)
    if value and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value) is None:
        return ''
    return value

def _clean_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))

def _clean_choice(value, default, choices):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)
    return value if value in choices else int(default)

def _clean_logical_tool(value):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return -1
    return value if 1 <= value <= 255 else -1

def _clean_u1_tool(value):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return -1
    return value if 0 <= value <= 31 else -1

def _bounded_object_entries(raw, maximum):
    cleaned = {}
    if not isinstance(raw, dict):
        return cleaned
    for scanned, (key, value) in enumerate(raw.items()):
        if scanned >= MAX_STATE_ENTRY_SCAN or len(cleaned) >= maximum:
            break
        if not isinstance(value, dict):
            continue
        name = _clean_text(key, '', 64)
        if name and name not in cleaned:
            cleaned[name] = value
    return cleaned

def _clean_channel_state(raw, channel):
    raw = dict(raw) if isinstance(raw, dict) else {}
    raw_primary = raw.get('color', '#FFFFFF')
    primary_valid = bool(_clean_color(raw_primary, ''))
    primary = _clean_color(raw_primary, '#FFFFFF')
    colors = []
    raw_colors = raw.get('colors', [])
    if not isinstance(raw_colors, (list, tuple)):
        raw_colors = []
    for value in list(raw_colors):
        color = _clean_color(value, '')
        if color and color not in colors:
            colors.append(color)
        if len(colors) == 5:
            break

    if not primary_valid:
        colors = ['#FFFFFF']
    elif primary != '#FFFFFF' or not colors:
        if primary in colors:
            colors.remove(primary)
        colors.insert(0, primary)
    colors = colors[:5] or ['#FFFFFF']
    spool = raw.get('spool_id')
    if spool is not None:
        try:
            spool = int(spool)
        except (TypeError, ValueError, OverflowError):
            spool = None
        if spool is not None and not 0 <= spool <= 0x7FFFFFFF:
            spool = None
    temperature_min = _clean_int(raw.get('temperature_min'), 170, 0, 500)
    temperature_max = _clean_int(raw.get('temperature_max'), 300, 0, 500)
    if temperature_min > temperature_max:
        temperature_min, temperature_max = 170, 300
    status = str(raw.get('encoder_status', 'UNTESTED') or 'UNTESTED').upper()
    if status not in ('UNTESTED', 'OK', 'FAULT'):
        status = 'UNTESTED'
    encoder_test = raw.get('encoder_test', {})
    if not isinstance(encoder_test, dict):
        encoder_test = {}
    clean_encoder_test = {}
    for key, value in list(encoder_test.items())[:16]:
        if not (isinstance(value, (str, int, float, bool)) or value is None):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        clean_encoder_test[str(key)[:32]] = value
    encoder_test = clean_encoder_test
    if (status == 'OK' and
            encoder_test.get('reason') == 'calibration_hardware_self_test'):
        status = 'UNTESTED'
        encoder_test = {}
    cleaned = {
        'name': _clean_text(raw.get('name'), '', 64),

        'endpoint': _clean_endpoint_name(raw.get('endpoint'), ''),
        'material': _clean_text(raw.get('material'), 'PLA', 48),
        'subtype': _clean_text(raw.get('subtype'), 'generic', 48),
        'color': colors[0],
        'colors': colors,
        'color_mode': _clean_int(raw.get('color_mode'), 0, 0, 255),
        'vendor': _clean_text(raw.get('vendor'), '', 48),
        'profile_id': _clean_text(raw.get('profile_id'), '', 64),
        'spool_id': spool,
        'temperature_min': temperature_min,
        'temperature_max': temperature_max,
        'encoder_status': status,
        'encoder_test': encoder_test,
        'refill_enabled': _clean_bool(raw.get('refill_enabled'), True),
        'refill_group': _clean_text(raw.get('refill_group'), '', 48),
        'refill_priority': _clean_int(raw.get('refill_priority'), channel, 0, 100000),

        'logical_tool': _clean_logical_tool(raw.get('logical_tool')),
        'unload_retract_mm': _clamp_float(
            raw.get('unload_retract_mm'), 200.0, 10.0, 2000.0),
        'autoload_mm': _clamp_float(
            raw.get('autoload_mm'), 120.0, 10.0, 1000.0),

        'path_length_mm': _clamp_float(
            raw.get('path_length_mm'), 0.0, 0.0, 5000.0),
        'path_length_endpoint': _clean_endpoint_name(
            raw.get('path_length_endpoint'), ''),
        'path_length_source': (lambda value: 'learned' if value in
                               ('learned', 'manual') else 'none')(
            _clean_text(raw.get('path_length_source'), 'none', 16).lower()),
        'path_measure_pending': _clean_bool(
            raw.get('path_measure_pending'), False),

        'tail_detached': _clean_bool(raw.get('tail_detached'), False),
        'tail_endpoint': _clean_endpoint_name(raw.get('tail_endpoint'), ''),
        'tail_path_length_mm': _clamp_float(
            raw.get('tail_path_length_mm'), 0.0, 0.0, 5000.0),
        'tail_follower_pending': _clean_bool(
            raw.get('tail_follower_pending'), False),
        'tail_follower_device': _clean_text(
            raw.get('tail_follower_device'), '', 64),
        'tail_follower_uid': _clean_uid(
            raw.get('tail_follower_uid', '')),
        'tail_follower_channel': _clean_int(
            raw.get('tail_follower_channel'), -1, -1, 3),
        'tail_follower_tool': _clean_int(
            raw.get('tail_follower_tool'), -1, -1, 1000000),

    }
    valid_path = bool(
        cleaned['path_length_mm'] > 0.0 and
        cleaned['path_length_endpoint'] and
        cleaned['path_length_source'] == 'learned')
    if not valid_path:
        cleaned['path_length_mm'] = 0.0
        cleaned['path_length_endpoint'] = ''
        cleaned['path_length_source'] = 'none'
    if not cleaned['tail_endpoint']:
        cleaned['tail_detached'] = False
    if not cleaned['tail_detached']:
        cleaned['tail_endpoint'] = ''
        cleaned['tail_path_length_mm'] = 0.0
        cleaned['tail_follower_pending'] = False
        cleaned['tail_follower_device'] = ''
        cleaned['tail_follower_uid'] = ''
        cleaned['tail_follower_channel'] = -1
        cleaned['tail_follower_tool'] = -1
    elif not cleaned['tail_follower_pending']:
        cleaned['tail_follower_device'] = ''
        cleaned['tail_follower_uid'] = ''
        cleaned['tail_follower_channel'] = -1
        cleaned['tail_follower_tool'] = -1
    elif (not cleaned['tail_follower_device'] and
          not cleaned['tail_follower_uid']):
        cleaned['tail_follower_pending'] = False
        cleaned['tail_follower_channel'] = -1
        cleaned['tail_follower_tool'] = -1
    return cleaned

def _clean_device_motion(raw, defaults=None):
    raw_values = dict(raw) if isinstance(raw, dict) else {}
    values = dict(DEFAULT_DEVICE_MOTION)
    if isinstance(defaults, dict):
        values.update(defaults)
    values.update(raw_values)

    if 'load_pressure_pct' not in raw_values and 'load_profile' in raw_values:
        try:
            legacy_profile = int(raw_values.get('load_profile', 0))
        except (TypeError, ValueError, OverflowError):
            legacy_profile = 0
        values['load_pressure_pct'] = {1: 95.0, 2: 75.0}.get(
            legacy_profile, 82.0)

    cleaned = {
        'schema': MOTION_POLICY_SCHEMA,
        'load_pressure_pct': _clamp_float(
            values.get('load_pressure_pct'), 82.0, 75.0, 95.0),
        'load_speed_mms': _clamp_float(values.get('load_speed_mms'), 80.0, 10.0, 120.0),
        'pull_speed_mms': _clamp_float(values.get('pull_speed_mms'), 80.0, 10.0, 120.0),
        'pull_speed_end_mms': _clamp_float(values.get('pull_speed_end_mms'), 12.0, 4.0, 40.0),
        'jam_timeout_ms': int(_clamp_float(values.get('jam_timeout_ms'), 20000, 1000, 120000)),
        'before_pullback_target_pct': _clamp_float(
            values.get('before_pullback_target_pct'), 40.0, 20.0, 60.0),
    }
    if 'loading_handoff_pct' in raw_values:
        cleaned['loading_handoff_pct'] = _clamp_float(
            raw_values.get('loading_handoff_pct'), 82.0, 60.0, 98.0)
    return cleaned

def _clean_device_record(raw):
    raw = dict(raw) if isinstance(raw, dict) else {}
    channels = raw.get('channels')
    if not isinstance(channels, dict):
        channels = {}
    lighting = _clean_lighting(
        raw.get('lighting'), raw.get('system_led_color', '#FFFFFF'))
    profiles = _clean_lighting_profiles(raw.get('lighting_profiles'))
    active = str(raw.get('lighting_profile', '') or '').strip()
    if active.upper() == 'DEFAULT':
        active = 'DEFAULT'
    elif active not in profiles:
        active = ''
    if not active:
        if _lighting_equal(lighting, DEFAULT_LIGHTING):
            active = 'DEFAULT'
            lighting = _clean_lighting(DEFAULT_LIGHTING)
        else:

            migrated = 'Previous settings'
            suffix = 2
            while migrated in profiles:
                migrated = 'Previous settings %d' % suffix
                suffix += 1
            profiles[migrated] = copy_lighting = _clean_lighting(lighting)
            lighting = copy_lighting
            active = migrated
    elif active == 'DEFAULT':
        lighting = _clean_lighting(DEFAULT_LIGHTING)
    else:
        lighting = _clean_lighting(profiles[active])
    return {
        'name': _clean_text(raw.get('name'), '', 64),
        'port': _clean_text(raw.get('port'), '', 1024),
        'system_led_color': lighting['system_color'],
        'lighting': lighting,
        'lighting_profile': active,
        'lighting_profiles': profiles,
        'motion_config': _clean_device_motion(raw.get('motion_config')),
        'channels': {
            str(channel): _clean_channel_state(channels.get(str(channel)), channel)
            for channel in range(4)
        },
    }

def _read_bounded_regular_file(path, maximum):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('BMCU state is not a regular file')
        if info.st_size > maximum:
            raise ValueError('BMCU state file is too large')
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
    finally:
        os.close(fd)
    if len(raw) > maximum:
        raise ValueError('BMCU state file is too large')
    return raw

def _clean_uid(value):
    value = str(value or '').strip().upper()
    if (len(value) != 24 or any(ch not in '0123456789ABCDEF' for ch in value) or
            value in ('0' * 24, 'F' * 24)):
        return ''
    return value

def _clean_tool_mappings(raw, allow_native=False):
    cleaned = {}
    if not isinstance(raw, dict):
        return cleaned
    for scanned, (tool_text, mapping) in enumerate(raw.items()):
        if scanned >= MAX_STATE_ENTRY_SCAN or len(cleaned) >= MAX_TOOL_MAPPINGS:
            break
        try:
            tool = int(tool_text)
        except (TypeError, ValueError):
            continue
        if tool < 0 or tool > 255 or not isinstance(mapping, dict):
            continue
        if allow_native and mapping.get('native'):
            try:
                head = int(mapping.get('head', -1))
            except (TypeError, ValueError):
                continue
            if 0 <= head <= 3:
                cleaned[str(tool)] = {'native': True, 'head': head}
            continue
        if mapping.get('external') and tool == 0:
            endpoint = _clean_endpoint_name(mapping.get('endpoint'), '')
            entry = {'external': True}
            if endpoint:
                entry['endpoint'] = endpoint
            cleaned[str(tool)] = entry
            continue
        device_name = _clean_text(mapping.get('device'), '', 64)
        device_uid = _clean_uid(mapping.get('device_uid', ''))
        try:
            channel = int(mapping.get('channel', -1))
        except (TypeError, ValueError):
            continue
        if (device_name or device_uid) and 0 <= channel <= 3:
            entry = {'device': device_name, 'channel': channel}
            if device_uid:
                entry['device_uid'] = device_uid
            expected_endpoint = _clean_endpoint_name(
                mapping.get('expected_endpoint'), '')
            expected_material = _clean_text(
                mapping.get('expected_material'), '', 48)
            if expected_endpoint:
                entry['expected_endpoint'] = expected_endpoint
            if expected_material:
                entry['expected_material'] = expected_material
            expected_spool = mapping.get('expected_spool')
            if expected_spool is not None:
                try:
                    expected_spool = int(expected_spool)
                except (TypeError, ValueError, OverflowError):
                    expected_spool = None
                if expected_spool is not None and 0 <= expected_spool <= 0x7FFFFFFF:
                    entry['expected_spool'] = expected_spool
            cleaned[str(tool)] = entry
    return cleaned

def _clean_u1_ownership(raw):

    cleaned = {}
    if not isinstance(raw, dict):
        return cleaned
    for endpoint_name, record in list(raw.items())[:MAX_STATE_ENTRY_SCAN]:
        if len(cleaned) >= MAX_U1_OWNERSHIP_RECORDS:
            break
        endpoint = _clean_endpoint_name(endpoint_name, '')
        if not endpoint or not isinstance(record, dict):
            continue
        try:
            channel = int(record.get('channel', -1))
        except (TypeError, ValueError, OverflowError):
            channel = -1
        clean_record = {
            'head_index': _clean_int(
                record.get('head_index'), -1, -1, 3),
            'baseline_captured': _clean_bool(
                record.get('baseline_captured'), False),
            'baseline_disabled': _clean_bool(
                record.get('baseline_disabled'), False),
            'persistent_hold': _clean_bool(
                record.get('persistent_hold'), False),

            'tail_detached': _clean_bool(
                record.get('tail_detached'), False),

            'tail_sensor_cleared': _clean_bool(
                record.get('tail_sensor_cleared'), False),

            'follower_pending': _clean_bool(
                record.get('follower_pending'), False),
            'follower_kind': (lambda value: value if value in
                              ('bmcu', 'native') else 'bmcu')(
                _clean_text(record.get('follower_kind'), 'bmcu', 16).lower()),
            'follower_device': _clean_text(
                record.get('follower_device'), '', 64),
            'follower_uid': _clean_uid(
                record.get('follower_uid', '')),
            'follower_channel': _clean_int(
                record.get('follower_channel'), -1, -1, 3),

            'follower_tool': _clean_u1_tool(
                record.get('follower_tool')),
            'generation': _clean_int(
                record.get('generation'), 0, 0, 0x7FFFFFFF),
            'generation_open': _clean_bool(
                record.get('generation_open'), False),
            'device': _clean_text(record.get('device'), '', 64),
            'device_uid': _clean_uid(record.get('device_uid', '')),
            'channel': channel if 0 <= channel <= 3 else -1,
            'route_state': (lambda value: value if value in (
                'EMPTY', 'LOADED', 'UNCERTAIN') else 'UNCERTAIN')(
                    _clean_text(record.get('route_state'), 'EMPTY', 24).upper()),
            'reason': _clean_text(record.get('reason'), '', 160),
        }

        if not clean_record['tail_detached']:
            clean_record['tail_sensor_cleared'] = False
            clean_record['follower_pending'] = False
            clean_record['follower_kind'] = 'bmcu'
            clean_record['follower_device'] = ''
            clean_record['follower_uid'] = ''
            clean_record['follower_channel'] = -1
            clean_record['follower_tool'] = -1
        elif not clean_record['follower_pending']:
            clean_record['follower_kind'] = 'bmcu'
            clean_record['follower_device'] = ''
            clean_record['follower_uid'] = ''
            clean_record['follower_channel'] = -1
            clean_record['follower_tool'] = -1
        elif (clean_record['follower_kind'] == 'bmcu' and
                not clean_record['follower_device'] and
                not clean_record['follower_uid']):
            clean_record['follower_pending'] = False
            clean_record['follower_channel'] = -1
            clean_record['follower_tool'] = -1
        elif clean_record['follower_kind'] == 'native':
            clean_record['follower_device'] = ''
            clean_record['follower_uid'] = ''
            clean_record['follower_channel'] = -1
        cleaned[endpoint] = clean_record
    return cleaned

def _clean_u1_cross_refill_pending(raw):

    if not isinstance(raw, dict) or not raw:
        return {}
    kind = _clean_text(raw.get('kind'), '', 24).lower()
    if kind != 'bmcu_cross':
        return {}
    try:
        source_head = int(raw.get('source_head', -1))
        replacement_head = int(raw.get('replacement_head', -1))
        source_tool = int(raw.get('source_tool', -1))
    except (TypeError, ValueError, OverflowError):
        return {}
    endpoint = _clean_endpoint_name(raw.get('replacement_endpoint'), '')
    phase = _clean_text(raw.get('phase'), '', 32).lower()
    if (source_head not in range(4) or replacement_head not in range(4) or
            source_head == replacement_head or source_tool not in range(32) or
            not endpoint):
        return {}
    source_endpoint = _clean_endpoint_name(raw.get('source_endpoint'), '')
    source_uid = _clean_uid(raw.get('source_device_uid', ''))
    replacement_uid = _clean_uid(raw.get('replacement_device_uid', ''))
    try:
        source_channel = int(raw.get('source_channel', -1))
        replacement_channel = int(raw.get('replacement_channel', -1))
    except (TypeError, ValueError, OverflowError):
        return {}
    allowed = ('loading', 'load_failed', 'captured', 'map_committing',
               'commit_failed', 'committed', 'resume_preparing',
               'resume_ready', 'resume_failed')
    if (phase not in allowed or not source_endpoint or not source_uid or
            not replacement_uid or source_channel not in range(4) or
            replacement_channel not in range(4)):
        return {}
    return {
        'kind': 'bmcu_cross',
        'source_head': source_head,
        'replacement_head': replacement_head,
        'source_tool': source_tool,
        'source_endpoint': source_endpoint,
        'replacement_endpoint': endpoint,
        'source_device_uid': source_uid,
        'source_channel': source_channel,
        'replacement_device_uid': replacement_uid,
        'replacement_channel': replacement_channel,
        'phase': phase,
        'error': _clean_text(raw.get('error'), '', 240),
    }

def _clean_u1_original(raw):

    if not isinstance(raw, dict):
        return {}

    def clean_int_list(value, length, minimum, maximum):
        if not isinstance(value, list) or len(value) != length:
            return None
        cleaned = []
        for item in value:
            if isinstance(item, bool):
                return None
            try:
                item = int(item)
            except (TypeError, ValueError, OverflowError):
                return None
            if item < minimum or item > maximum:
                return None
            cleaned.append(item)
        return cleaned

    def clean_bool_list(value, length):
        if not isinstance(value, list) or len(value) != length:
            return None
        if any(not isinstance(item, bool) for item in value):
            return None
        return list(value)

    result = {'live': {}, 'reprint': {}}
    groups = (
        ('extruder_map_table', 32, 'int', 0, 3),
        ('extruders_used', 4, 'bool', 0, 1),
        ('end_unload_filament', 4, 'bool', 0, 1),
        ('flow_calib_extruders', 4, 'bool', 0, 1),
        ('extruders_replenished', 4, 'int', 0, 3),
    )
    for section in ('live', 'reprint'):
        source = raw.get(section)
        if not isinstance(source, dict):
            source = {}
        for key, length, kind, minimum, maximum in groups:
            if section == 'reprint' and key == 'extruders_replenished':
                continue
            value = source.get(key)
            cleaned = (clean_bool_list(value, length) if kind == 'bool'
                       else clean_int_list(value, length, minimum, maximum))
            if cleaned is not None:
                result[section][key] = cleaned
    required_live = {
        'extruder_map_table', 'extruders_used', 'end_unload_filament',
        'flow_calib_extruders', 'extruders_replenished'}
    required_reprint = {
        'extruder_map_table', 'extruders_used', 'end_unload_filament',
        'flow_calib_extruders'}
    if (set(result['live']) != required_live or
            set(result['reprint']) != required_reprint):
        return {}
    return result

def default_state():
    return {
        'schema': STATE_SCHEMA,
        'version': STATE_VERSION,
        'devices': {},
        'endpoints': {
            'extruder': {
                'driver': 'generic_single_extruder',
                'extruder': 'extruder',
                'toolhead_prepare_macro': '',
                'before_pullback_macro': '',
                'select_macro': '',
                'verify_selected_macro': '',
                'deselect_macro': '',
                'expected_active_extruder': '',
                'require_select_macro': False,
                'verify_active_extruder': False,
                'entry_sensor': '',
                'post_gears_sensor': '',
                'motion_sensor': '',
                'sensor_policy': 'managed',
                'tail_tracking_mode': 'auto',
                'shared_path_group': 'extruder',
                'head_index': -1,
                'prestage_while_unselected': False,
                'prestage_distance_mm': 0.0,
                'max_route_mm': 1500.0,
                'final_search_mm': 250.0,
                'contact_buffer_pct': 82.0,
                'contact_timeout': 45.0,
                'prestage_buffer_limit_pct': 82.0,
                'prestage_timeout': 45.0,
                'auto_refill_enabled': False,
                'tail_runout_enabled': True,
                'refill_mode': 'pause',
                'refill_match': 'exact',
                'tail_to_output_mm': 0.0,
                'sensor_tail_remaining_mm': 0.0,
                'tail_reserve_mm': 20.0,
                'refill_contact_buffer_pct': 82.0,
                'refill_timeout': 75.0,
                'refill_runout_debounce': 0.4,
                'refill_pause_macro': '',
                'refill_resume_macro': '',
            }
        },

        'tools': {},

        'preferences': {
            'schema': 1,
            'leave_final_filament_loaded': False,
        },

        'u1_tip_profiles': {},

        'u1_ownership': {},
        'u1_ownership_schema': U1_OWNERSHIP_SCHEMA,
        'print_session': {'schema': PRINT_SESSION_SCHEMA,
                          'active': False, 'plan_open': False,
                          'plan_schema': 1, 'job_id': '', 'tools': {}, 'backups': {},
                          'u1_map_backup': {}, 'u1_used_backup': {},
                          'u1_end_unload_backup': {},
                          'u1_original': {},
                          'transaction_phase': '',
                          'loaded_routes': [],
                          'route_journal_initialized': False,
                          'terminal_unload_pending': False,
                          'stock_reset_observed': False,

                          'u1_prepared_heads': [],
                          'u1_cross_refill_pending': {}},
    }

class StateStore(object):
    def __init__(self, path, writer=None):
        self.path = path
        self.writer = writer
        self.data = default_state()
        self._sanitized_device_ids = set()
        self.load()

    def load(self):
        try:
            raw = _read_bounded_regular_file(self.path, MAX_STATE_BYTES)
            loaded = json.loads(raw.decode('utf-8'), parse_constant=_reject_json_constant)
            if not isinstance(loaded, dict):
                logging.warning('BMCU state root is not an object; defaults are used')
                return

            if (loaded.get('schema') != STATE_SCHEMA or
                    loaded.get('version') != STATE_VERSION):
                logging.warning(
                    'BMCU state is not the final 1.0.0 format; fresh defaults are used')
                return

            base = default_state()
            merged = {
                'schema': STATE_SCHEMA,
                'version': STATE_VERSION,
                'devices': loaded.get('devices', base['devices']),
                'endpoints': loaded.get('endpoints', base['endpoints']),
                'tools': loaded.get('tools', base['tools']),
                'preferences': loaded.get('preferences', base['preferences']),
                'u1_tip_profiles': loaded.get(
                    'u1_tip_profiles', loaded.get(
                        'u1_material_profiles', base['u1_tip_profiles'])),
                'u1_ownership': loaded.get(
                    'u1_ownership', base['u1_ownership']),
                'u1_ownership_schema': _clean_int(
                    loaded.get('u1_ownership_schema'), 0, 0,
                    U1_OWNERSHIP_SCHEMA),
                'print_session': loaded.get('print_session', base['print_session']),
            }
            for key in ('devices', 'endpoints', 'tools', 'preferences',
                        'u1_tip_profiles', 'u1_ownership', 'print_session'):
                if not isinstance(merged.get(key), dict):
                    logging.warning('BMCU state section %s is invalid; defaults are used', key)
                    merged[key] = base[key]

            merged['devices'] = {
                key: _clean_device_record(value)
                for key, value in _bounded_object_entries(
                    merged['devices'], MAX_STATE_DEVICES).items()
            }
            merged['endpoints'] = {
                name: values for name, values in _bounded_object_entries(
                    merged['endpoints'], MAX_STATE_ENDPOINTS).items()
                if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) is not None
            }
            for endpoint_values in merged['endpoints'].values():
                for key in ('u1_load_gcode', 'u1_unload_gcode', 'u1_wait_gcode'):
                    if key in endpoint_values:
                        endpoint_values[key] = _clean_multiline_text(
                            endpoint_values.get(key), 8192)
                if str(endpoint_values.get('driver', '') or '').lower() == 'snapmaker_u1':

                    endpoint_values['u1_sensor_takeover'] = False
                    endpoint_values['u1_require_coil_confirmation'] = True
                    endpoint_values.pop('u1_load_search_max_mm', None)
                    endpoint_values.pop('u1_load_to_nozzle_feed', None)
                    try:
                        load_to_nozzle = float(
                            endpoint_values.get('u1_load_to_nozzle_mm', 70.0))
                    except (TypeError, ValueError, OverflowError):
                        load_to_nozzle = 70.0
                    if load_to_nozzle <= 0.0:
                        endpoint_values['u1_load_to_nozzle_mm'] = 70.0
                    try:
                        parked_precharge = float(endpoint_values.get(
                            'prestage_buffer_limit_pct', 63.0))
                    except (TypeError, ValueError, OverflowError):
                        parked_precharge = 63.0
                    endpoint_values['prestage_buffer_limit_pct'] = min(
                        63.0, max(55.0, parked_precharge))
            preferences = merged.get('preferences')
            if not isinstance(preferences, dict):
                preferences = {}
            merged['preferences'] = {
                'schema': 1,
                'leave_final_filament_loaded': _clean_bool(
                    preferences.get('leave_final_filament_loaded'), False),
            }
            merged['u1_tip_profiles'] = _clean_u1_tip_profiles(
                merged.get('u1_tip_profiles'))

            merged['tools'] = {}
            merged['u1_ownership'] = _clean_u1_ownership(
                merged.get('u1_ownership'))
            if not merged['endpoints']:
                merged['endpoints'] = default_state()['endpoints']
            for device in merged['devices'].values():
                for channel in device.get('channels', {}).values():
                    endpoint_name = channel.get('endpoint', '')
                    if endpoint_name and endpoint_name not in merged['endpoints']:

                        channel['endpoint'] = ''

            session = merged.get('print_session')
            if not isinstance(session, dict):
                session = {'schema': PRINT_SESSION_SCHEMA,
                           'active': False, 'plan_open': False,
                           'plan_schema': 1, 'job_id': '', 'tools': {}, 'backups': {},
                           'u1_map_backup': {}, 'u1_used_backup': {},
                           'u1_end_unload_backup': {},
                           'u1_cross_refill_pending': {}}
            session['schema'] = _clean_int(
                session.get('schema'), 0, 0, PRINT_SESSION_SCHEMA)
            session['active'] = bool(session.get('active', False))
            session['plan_open'] = bool(session.get('plan_open', False))
            session['plan_schema'] = 1
            session['job_id'] = str(session.get('job_id', '') or '')[:128]
            for key in ('tools', 'backups', 'u1_map_backup', 'u1_used_backup',
                        'u1_end_unload_backup'):
                if not isinstance(session.get(key), dict):
                    session[key] = {}

            session['tools'] = _clean_tool_mappings(session['tools'], allow_native=True)

            clean_backups = {}
            for scanned, (source_text, items) in enumerate(session['backups'].items()):
                if scanned >= MAX_STATE_ENTRY_SCAN or len(clean_backups) >= MAX_TOOL_MAPPINGS:
                    break
                try:
                    source = int(source_text)
                except (TypeError, ValueError):
                    continue
                if source < 0 or source > 255 or not isinstance(items, list):
                    continue
                clean_items = []
                seen_tools = set()
                for item in items[:MAX_STATE_ENTRY_SCAN]:
                    if len(clean_items) >= MAX_BACKUPS_PER_TOOL or not isinstance(item, dict):
                        continue
                    try:
                        tool = int(item.get('tool', -1))
                        priority = int(item.get('priority', 100))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if 0 <= tool <= 255 and tool != source and tool not in seen_tools:
                        seen_tools.add(tool)
                        clean_items.append({
                            'tool': tool,
                            'priority': _clean_int(priority, 100, 0, 100000),
                        })
                clean_backups[str(source)] = clean_items
            session['backups'] = clean_backups

            clean_map_backup = {}
            for scanned, (tool_text, head_value) in enumerate(session['u1_map_backup'].items()):
                if scanned >= MAX_STATE_ENTRY_SCAN:
                    break
                try:
                    tool = int(tool_text)
                    head = int(head_value)
                except (TypeError, ValueError):
                    continue
                if 0 <= tool < 32 and 0 <= head < 4:
                    clean_map_backup[str(tool)] = head
            session['u1_map_backup'] = clean_map_backup

            clean_used_backup = {}
            for scanned, (head_text, value) in enumerate(session['u1_used_backup'].items()):
                if scanned >= MAX_STATE_ENTRY_SCAN:
                    break
                try:
                    head = int(head_text)
                except (TypeError, ValueError):
                    continue
                if 0 <= head < 4:
                    clean_used_backup[str(head)] = bool(value)
            session['u1_used_backup'] = clean_used_backup

            clean_end_unload_backup = {}
            for scanned, (head_text, value) in enumerate(
                    session['u1_end_unload_backup'].items()):
                if scanned >= MAX_STATE_ENTRY_SCAN:
                    break
                try:
                    head = int(head_text)
                except (TypeError, ValueError):
                    continue
                if 0 <= head < 4:
                    clean_end_unload_backup[str(head)] = bool(value)
            session['u1_end_unload_backup'] = clean_end_unload_backup
            session['u1_original'] = _clean_u1_original(
                session.get('u1_original'))
            phase = _clean_text(
                session.get('transaction_phase'), '', 24).lower()
            if phase not in ('', 'applying', 'active', 'restoring',
                             'recovery'):
                phase = 'recovery'
            session['transaction_phase'] = phase
            loaded_routes = []
            raw_routes = session.get('loaded_routes', [])
            if isinstance(raw_routes, list):
                for value in raw_routes[:MAX_STATE_ENTRY_SCAN]:
                    value = _clean_text(value, '', 128)
                    valid_route = re.fullmatch(
                        r'uid:[0-9A-Fa-f]{24}:[0-3]', value)
                    if value and value not in loaded_routes and valid_route:
                        parts = value.split(':')
                        value = 'uid:%s:%s' % (parts[1].upper(), parts[2])
                        loaded_routes.append(value)
                    if len(loaded_routes) >= MAX_TOOL_MAPPINGS:
                        break
            session['loaded_routes'] = loaded_routes
            session['route_journal_initialized'] = bool(
                session.get('route_journal_initialized', False))
            session['terminal_unload_pending'] = bool(
                session.get('terminal_unload_pending', False))
            session['stock_reset_observed'] = bool(
                session.get('stock_reset_observed', False))
            prepared_heads = []
            raw_prepared_heads = session.get('u1_prepared_heads', [])
            if isinstance(raw_prepared_heads, list):
                for value in raw_prepared_heads[:4]:
                    try:
                        head = int(value)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if head in range(4) and head not in prepared_heads:
                        prepared_heads.append(head)
            session['u1_prepared_heads'] = sorted(prepared_heads)
            session['u1_cross_refill_pending'] = (
                _clean_u1_cross_refill_pending(
                    session.get('u1_cross_refill_pending')))
            session['schema'] = PRINT_SESSION_SCHEMA
            merged['print_session'] = session
            merged['schema'] = STATE_SCHEMA
            merged['version'] = STATE_VERSION
            self.data = merged
            self._sanitized_device_ids.clear()
        except FileNotFoundError:
            return
        except (IOError, OSError, ValueError, TypeError):
            logging.warning('BMCU state could not be loaded; defaults are used')
            return

    def save(self):
        payload = json.dumps(
            self.data, separators=(',', ':'), sort_keys=True,
            allow_nan=False) + '\n'
        if len(payload.encode('utf-8')) > MAX_STATE_BYTES:
            raise ValueError('BMCU state file would exceed the %d-byte limit' %
                             MAX_STATE_BYTES)
        directory = os.path.dirname(self.path)
        if os.path.lexists(self.path):
            info = os.lstat(self.path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError('BMCU state path must be a regular non-symlink file')
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        if self.writer is not None:
            return self.writer.write(
                self.path, payload, mode=0o600, timeout=30.0)
        fd, temp_path = tempfile.mkstemp(prefix='.bmcu-state-', suffix='.json', dir=directory or '.')
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)

            try:
                dir_fd = os.open(
                    directory or '.', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
        return True

    def ensure_device(self, uid, name, port, motion_defaults=None):
        device = self.data['devices'].setdefault(uid, {
            'name': name, 'port': port,
            'system_led_color': '#FFFFFF',
            'lighting': _clean_lighting({}),
            'lighting_profile': 'DEFAULT',
            'lighting_profiles': {},
            'channels': {}
        })
        device['name'] = name
        device['port'] = port
        cleaned_device = _clean_device_record(device)
        device['lighting'] = cleaned_device['lighting']
        device['lighting_profile'] = cleaned_device['lighting_profile']
        device['lighting_profiles'] = cleaned_device['lighting_profiles']
        device['system_led_color'] = device['lighting']['system_color']
        device['motion_config'] = _clean_device_motion(
            device.get('motion_config'), motion_defaults)
        marker = id(device)
        if marker not in self._sanitized_device_ids:
            channels = device.get('channels')
            if not isinstance(channels, dict):
                channels = {}
            device['channels'] = {
                str(channel): _clean_channel_state(channels.get(str(channel)), channel)
                for channel in range(4)
            }
            self._sanitized_device_ids.add(marker)
        return device
