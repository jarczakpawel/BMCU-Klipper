#!/usr/bin/env python3

from __future__ import annotations

import argparse
import collections
import errno
import glob
import hashlib
import hmac
import http.cookies
import http.server
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

PROXY_PREFIX = '/moonraker'
ALLOWED_PROXY_PATHS = {'/printer/objects/query', '/printer/gcode/script'}
MAX_PROXY_REQUEST = 1024 * 1024
MAX_PROXY_RESPONSE = 4 * 1024 * 1024
PROXY_QUERY_TIMEOUT = 10.0
PROXY_COMMAND_TIMEOUT = 60.0
PROXY_MOTION_TIMEOUT = 600.0
MAX_UPDATE_REQUEST = 64 * 1024
MAX_FIRMWARE_BYTES = 64 * 1024
MAX_UPLOAD_NAME = 160
MAX_UPDATE_OUTPUT_LINE = 64 * 1024
MAX_UPDATE_LOG_MESSAGE = 2048
MAX_TRANSACTION_BYTES = 64 * 1024
MAX_HTTP_CONNECTIONS = 8
HTTP_CONNECTION_TIMEOUT = 15.0
MAX_PANEL_GCODE = 40 * 1024
MAX_ORCA_TEMPLATE_BYTES = 512 * 1024
DIAGNOSTICS_EXPORT_TIMEOUT = 180.0
REMOTE_VERSION_URL = 'https://raw.githubusercontent.com/jarczakpawel/BMCU-Klipper/main/version'
MAX_VERSION_BYTES = 4096
_RUNTIME_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_EXTRAS = os.path.join(_RUNTIME_ROOT, 'klippy', 'extras')
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)
from bmcu_core.release import (
    PACKAGE_VERSION, REQUIRED_FIRMWARE_TEXT, parse_release_versions)
REQUIRED_FIRMWARE_VERSION = REQUIRED_FIRMWARE_TEXT
ALLOWED_PANEL_COMMANDS = {
    'BMCU_APPLY_PRESET',
    'BMCU_CALIBRATE',
    'BMCU_CHANNEL_RETRACT',
    'BMCU_LOAD',
    'BMCU_LIGHTING',
    'BMCU_LIGHTING_PROFILE',
    'BMCU_LED_PREVIEW',
    'BMCU_SET_FILAMENT',
    'BMCU_SET_ENDPOINT',
    'BMCU_SET_PREFERENCES',
    'BMCU_SET_ROUTE',
    'BMCU_UNLOAD',
    'BMCU_TIP_PROFILE',
    'BMCU_U1_GCODE',
    'BMCU_REFILL_RESUME',
    'BMCU_RECONCILE',
    'BMCU_ROUTE_CONFIRM',
    'BMCU_ROUTE_RECOVER',
    'BMCU_BUFFER_MODE',
    'RESTART',
}
LONG_PANEL_COMMANDS = {
    'BMCU_CALIBRATE',
    'BMCU_CHANNEL_RETRACT',
    'BMCU_ROUTE_RECOVER',
    'BMCU_LOAD',
    'BMCU_UNLOAD',
    'BMCU_BUFFER_MODE',
    'BMCU_REFILL_RESUME',
}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirect())

def _reject_json_constant(value):
    raise ValueError('non-standard JSON number: %s' % value)

def _remote_release_versions():
    request = urllib.request.Request(
        REMOTE_VERSION_URL, headers={'User-Agent': 'BMCU-Klipper/%s' % PACKAGE_VERSION})
    with urllib.request.urlopen(request, timeout=6) as response:
        data = response.read(MAX_VERSION_BYTES + 1)
    if len(data) > MAX_VERSION_BYTES:
        raise ValueError('remote version file is too large')
    return parse_release_versions(data.decode('utf-8'))

def validate_moonraker_url(value):
    raw = str(value or '')
    if any(ord(ch) < 32 for ch in raw):
        raise ValueError('invalid Moonraker URL')
    parsed = urllib.parse.urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('invalid Moonraker URL') from exc
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(
            'Moonraker URL must be HTTP(S) without credentials, query or fragment')

    path = parsed.path.rstrip('/')
    netloc = parsed.hostname
    if ':' in netloc and not netloc.startswith('['):
        netloc = '[%s]' % netloc
    if port is not None:
        netloc += ':%d' % port
    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, '', ''))

def moonraker_websocket_config(value):
    parsed = urllib.parse.urlsplit(validate_moonraker_url(value))
    use_page_host = parsed.hostname.lower() in (
        '127.0.0.1', 'localhost', '::1', '0.0.0.0')
    path = parsed.path.rstrip('/') + '/websocket'
    return {
        'scheme': 'wss' if parsed.scheme == 'https' else 'ws',
        'host': '' if use_page_host else parsed.hostname,
        'port': parsed.port or (443 if parsed.scheme == 'https' else 80),
        'path': path or '/websocket',
        'use_page_host': use_page_host,
    }

def validate_panel_gcode(raw):
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('invalid Moonraker G-code JSON') from exc
    if not isinstance(value, dict) or set(value) != {'script'}:
        raise ValueError('G-code request must contain only script')
    script = value.get('script')
    if not isinstance(script, str) or not script.strip():
        raise ValueError('G-code script must not be empty')
    if len(script.encode('utf-8')) > MAX_PANEL_GCODE:
        raise ValueError('G-code script is too large')
    stripped = script.strip()
    if any(ch in stripped for ch in ('\n', '\r', ';', '\x00')):
        raise ValueError('only one BMCU command is allowed')
    command = stripped.split(None, 1)[0].upper()
    if command not in ALLOWED_PANEL_COMMANDS:
        raise ValueError('this BMCU command is not available from the panel')
    return value

def read_limited_text(path, maximum):
    path = pathlib.Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise ValueError('unsafe or missing file') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('unsafe or missing file')
        if info.st_size > maximum:
            raise ValueError('file is too large')
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
    finally:
        os.close(fd)
    if len(raw) > maximum:
        raise ValueError('file is too large')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('file is not valid UTF-8') from exc

ORCA_PER_TOOL_KEYS = (
    'default_nozzle_volume_type', 'deretraction_speed', 'extruder_colour',
    'extruder_offset', 'extruder_printable_height', 'extruder_type',
    'extruder_variant_list', 'long_retractions_when_cut', 'max_layer_height',
    'min_layer_height', 'nozzle_diameter', 'nozzle_flush_dataset',
    'nozzle_type', 'nozzle_volume', 'printer_extruder_id',
    'printer_extruder_variant', 'retract_before_wipe',
    'retract_length_toolchange', 'retract_lift_above',
    'retract_lift_below', 'retract_lift_enforce',
    'retract_restart_extra', 'retract_restart_extra_toolchange',
    'retract_when_changing_layer', 'retraction_distances_when_cut',
    'retraction_length', 'retraction_minimum_travel', 'retraction_speed',
    'travel_slope', 'wipe', 'wipe_distance', 'z_hop', 'z_hop_types',
)
SNAPMAKER_ORCA_PER_TOOL_KEYS = (
    'deretraction_speed', 'extruder_colour', 'extruder_offset',
    'long_retractions_when_cut', 'max_layer_height', 'min_layer_height',
    'nozzle_diameter', 'retract_before_wipe',
    'retract_length_toolchange', 'retract_lift_above', 'retract_lift_below',
    'retract_lift_enforce', 'retract_restart_extra',
    'retract_restart_extra_toolchange', 'retract_when_changing_layer',
    'retraction_distances_when_cut', 'retraction_length',
    'retraction_minimum_travel', 'retraction_speed', 'travel_slope',
    'wipe', 'wipe_distance', 'z_hop', 'z_hop_types', 'z_hop_when_prime',
)
ORCA_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')
PUBLIC_PROFILE_PRIVATE_KEYS = {
    'bbl_use_printhost', 'host_type', 'print_host', 'device_id',
    'device_name', 'device_sn', 'printer_sn', 'serial', 'serial_number',
    'user_id', 'user_name', 'access_token', 'refresh_token', 'auth_token',
    'api_key', 'apikey', 'password',
}
PUBLIC_PROFILE_PRIVATE_PREFIXES = ('printhost_', 'cloud_', 'mqtt_', 'account_')

def _sanitize_public_profile(profile):
    for key in list(profile):
        lowered = str(key).lower()
        if (lowered in PUBLIC_PROFILE_PRIVATE_KEYS or
                lowered.startswith(PUBLIC_PROFILE_PRIVATE_PREFIXES)):
            profile.pop(key, None)
    return profile
ORCA_PROFILE_NAME = 'Snapmaker U1 BMCU'

def _panel_host(value):
    raw = str(value or '').strip()
    if not raw or any(ord(ch) < 33 for ch in raw):
        return ''
    try:
        parsed = urllib.parse.urlsplit('//' + raw)
        host = parsed.hostname or ''
    except ValueError:
        return ''
    if not host or len(host) > 253:
        return ''
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', host):
        return ''
    return host

def _resize_orca_tools(profile, tool_count, colors, tools=None, keys=ORCA_PER_TOOL_KEYS):
    for key in keys:
        values = profile.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError('Orca profile template has invalid %s' % key)
        if key == 'printer_extruder_id':
            profile[key] = [str(index + 1) for index in range(tool_count)]
            continue
        resized = list(values[:tool_count])
        while len(resized) < tool_count:
            resized.append(values[-1])
        profile[key] = resized
    colour_values = profile.get('extruder_colour', [])
    mapped_tools = list(tools) if tools is not None else list(
        range(4, 4 + len(colors)))
    for tool, value in zip(mapped_tools, colors):
        if 4 <= int(tool) < tool_count and ORCA_COLOR_RE.fullmatch(value):
            colour_values[int(tool)] = value.upper()

def _orca_start_code(code, tool_count):
    source = str(code or '').splitlines()
    clean = []
    skipping = False
    for line in source:
        stripped = line.strip()
        if stripped.startswith('BMCU_PRINT_BEGIN'):
            skipping = True
            continue
        if skipping:
            if stripped == 'BMCU_PRINT_COMMIT':
                skipping = False
            continue
        clean.append(line)
    if skipping:
        raise ValueError('Orca template contains an incomplete BMCU start block')
    for index, line in enumerate(clean):
        stripped = line.strip()
        for head in range(4):
            if stripped == 'SM_PRINT_AUTO_FEED EXTRUDER=%d' % head:
                clean[index] = (
                    'BMCU_AUTO_FEED EXTRUDER=%d '
                    'INITIAL_TOOL={initial_extruder} REQUIRE_PLAN=1' % head)
    if not any(line.strip() == 'PRINT_START' for line in clean):
        raise ValueError('Orca template does not contain PRINT_START')
    position = next(
        (index for index, line in enumerate(clean)
         if line.strip() and not line.lstrip().startswith(';')),
        len(clean))
    block = ['BMCU_PRINT_BEGIN SCHEMA=1 RESET=1']
    for tool in range(tool_count):
        if tool < 4:
            block.append(
                '{if is_extruder_used[%d]}BMCU_PRINT_MAP TOOL=%d{endif}' %
                (tool, tool))
        else:
            block.append(
                '{if is_extruder_used[%d]}BMCU_PRINT_MAP TOOL=%d '
                'MATERIAL="{filament_type[%d]}" COLOR="{filament_colour[%d]}"{endif}' %
                (tool, tool, tool, tool))
    block.extend(['BMCU_PRINT_COMMIT', ''])
    clean[position:position] = block
    return '\n'.join(clean)

def _orca_change_code(code):
    code = str(code or '')
    inside_expression = False
    for number, line in enumerate(code.splitlines(), 1):
        stripped = line.strip()
        if stripped == '{':
            inside_expression = True
            continue
        if stripped == '}':
            inside_expression = False
            continue
        if inside_expression and stripped.startswith(';'):
            raise ValueError(
                'Orca change-filament template has a raw G-code comment '
                'inside its expression block on line %d' % number)
    required = (
        'if previous_extruder < 4 then',
        'MOVE_TO_XY_IDLE_POSITION_EXTRUDER',
        'M104 S" + temperature[next_extruder]',
        'BMCU_TOOL_CHANGE TOOL=" + next_extruder',
    )
    missing = [value for value in required if value not in code]
    if missing:
        raise ValueError(
            'Orca change-filament template is missing required policy fragments: %s' %
            ', '.join(missing))
    if code.count('MOVE_TO_XY_IDLE_POSITION_EXTRUDER') != 1:
        raise ValueError(
            'Orca direct policy must contain exactly one conditional idle move')
    if code.count('BMCU_TOOL_CHANGE TOOL=') != 1:
        raise ValueError(
            'Orca direct policy must contain exactly one BMCU tool change')
    if 'M109 S" + ' in code:
        raise ValueError(
            'Orca direct policy must not block on M109 before BMCU tool change')
    condition = code.index('if previous_extruder < 4 then')
    idle = code.index('MOVE_TO_XY_IDLE_POSITION_EXTRUDER')
    endif = code.index('endif', idle)
    if not condition < idle < endif:
        raise ValueError(
            'Orca idle move is not guarded by the physical-source condition')
    return code

def _orca_end_code(code):
    lines = [line for line in str(code or '').splitlines()
             if not line.strip().startswith('BMCU_PRINT_END')]
    try:
        position = next(index for index, line in enumerate(lines)
                        if line.strip() == 'PRINT_END')
    except StopIteration as exc:
        raise ValueError('Orca template does not contain PRINT_END') from exc
    lines.insert(position, 'BMCU_PRINT_END MODE=AUTO CLEAR=0')
    return '\n'.join(lines)

def build_orca_bundle(root, bmcu_count, colors, host, tools=None):
    try:
        count = int(bmcu_count)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid BMCU count') from exc
    if count < 1 or count > 7:
        raise ValueError('BMCU count must be between 1 and 7')
    normalized_tools = None
    if tools is not None:
        normalized_tools = []
        for value in tools:
            try:
                tool = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError('invalid slot mapping') from exc
            if tool < 4 or tool > 31 or tool in normalized_tools:
                raise ValueError('Slots must be unique in 5-32 (T4-T31)')
            normalized_tools.append(tool)
        if len(normalized_tools) != count * 4:
            raise ValueError('slot count must match configured BMCU channels')
    tool_count = (max(normalized_tools) + 1 if normalized_tools else
                  4 + count * 4)
    normalized_colors = []
    for value in colors:
        value = str(value or '').strip()
        if not ORCA_COLOR_RE.fullmatch(value):
            raise ValueError('invalid Orca extruder colour')
        normalized_colors.append(value.upper())
    if len(normalized_colors) > count * 4:
        raise ValueError('too many Orca extruder colours')
    if normalized_tools is not None and len(normalized_colors) != len(normalized_tools):
        raise ValueError('one Orca colour is required for every BMCU slot')

    template_path = pathlib.Path(root, 'orca', 'snapmaker_u1_bmcu_template.json')
    profile = json.loads(read_limited_text(
        template_path, MAX_ORCA_TEMPLATE_BYTES),
        parse_constant=_reject_json_constant)
    if not isinstance(profile, dict):
        raise ValueError('invalid Orca profile template')

    profile_name = ORCA_PROFILE_NAME
    profile['name'] = profile_name
    profile['printer_settings_id'] = profile_name
    profile['from'] = 'User'

    profile.pop('print_host', None)
    profile['printer_notes'] = 'Generated by BMCU-Klipper'
    _resize_orca_tools(profile, tool_count, normalized_colors, normalized_tools)
    profile['machine_start_gcode'] = _orca_start_code(
        profile.get('machine_start_gcode'), tool_count)
    profile['machine_end_gcode'] = _orca_end_code(
        profile.get('machine_end_gcode'))
    profile['change_filament_gcode'] = _orca_change_code(
        profile.get('change_filament_gcode'))

    process_template_path = pathlib.Path(
        root, 'orca', 'snapmaker_u1_bmcu_process.json')
    filament_template_path = pathlib.Path(
        root, 'orca', 'snapmaker_u1_bmcu_filament.json')
    process_profile = json.loads(read_limited_text(
        process_template_path, MAX_ORCA_TEMPLATE_BYTES),
        parse_constant=_reject_json_constant)
    filament_profile = json.loads(read_limited_text(
        filament_template_path, MAX_ORCA_TEMPLATE_BYTES),
        parse_constant=_reject_json_constant)
    if not isinstance(process_profile, dict) or not isinstance(filament_profile, dict):
        raise ValueError('invalid Orca process/filament template')
    process_profile['compatible_printers'] = [profile_name]
    filament_profile['compatible_printers'] = [profile_name]

    profile_path = 'printer/%s.json' % profile_name
    process_path = 'process/%s.json' % process_profile['name']
    filament_path = 'filament/%s.json' % filament_profile['name']
    bundle = {
        'bundle_id': '_BMCU_OrcaSlicer_Snapmaker_U1',
        'bundle_type': 'printer config bundle',
        'filament_config': [filament_path],
        'printer_config': [profile_path],
        'printer_preset_name': profile_name,
        'process_config': [process_path],
        'version': '00.00.00.00',
    }
    profile_raw = (json.dumps(
        profile, ensure_ascii=False, indent='\t') + '\n').encode('utf-8')
    process_raw = (json.dumps(
        process_profile, ensure_ascii=False, indent='\t') + '\n').encode('utf-8')
    filament_raw = (json.dumps(
        filament_profile, ensure_ascii=False, indent='\t') + '\n').encode('utf-8')
    bundle_raw = json.dumps(
        bundle, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(profile_path, profile_raw)
        archive.writestr(process_path, process_raw)
        archive.writestr(filament_path, filament_raw)
        archive.writestr('bundle_structure.json', bundle_raw)
    return output.getvalue(), profile_raw, tool_count

def build_snapmaker_orca_bundle(root, bmcu_count, colors, host, tools=None):
    try:
        count = int(bmcu_count)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid BMCU count') from exc
    if count < 1 or count > 7:
        raise ValueError('BMCU count must be between 1 and 7')
    normalized_tools = None
    if tools is not None:
        normalized_tools = []
        for value in tools:
            try:
                tool = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError('invalid slot mapping') from exc
            if tool < 4 or tool > 31 or tool in normalized_tools:
                raise ValueError('Slots must be unique in 5-32 (T4-T31)')
            normalized_tools.append(tool)
        if len(normalized_tools) != count * 4:
            raise ValueError('slot count must match configured BMCU channels')
    tool_count = (max(normalized_tools) + 1 if normalized_tools else
                  4 + count * 4)
    normalized_colors = []
    for value in colors:
        value = str(value or '').strip()
        if not ORCA_COLOR_RE.fullmatch(value):
            raise ValueError('invalid Orca extruder colour')
        normalized_colors.append(value.upper())
    if len(normalized_colors) > count * 4:
        raise ValueError('too many Orca extruder colours')
    if normalized_tools is not None and len(normalized_colors) != len(normalized_tools):
        raise ValueError('one Orca colour is required for every BMCU slot')

    profile = json.loads(read_limited_text(
        pathlib.Path(root, 'orca', 'snapmaker_orca_bmcu_template.json'),
        MAX_ORCA_TEMPLATE_BYTES), parse_constant=_reject_json_constant)
    process_profile = json.loads(read_limited_text(
        pathlib.Path(root, 'orca', 'snapmaker_u1_bmcu_process.json'),
        MAX_ORCA_TEMPLATE_BYTES), parse_constant=_reject_json_constant)
    filament_profile = json.loads(read_limited_text(
        pathlib.Path(root, 'orca', 'snapmaker_u1_bmcu_filament.json'),
        MAX_ORCA_TEMPLATE_BYTES), parse_constant=_reject_json_constant)
    if not all(isinstance(value, dict) for value in
               (profile, process_profile, filament_profile)):
        raise ValueError('invalid Snapmaker Orca profile template')
    _sanitize_public_profile(profile)
    _sanitize_public_profile(process_profile)
    _sanitize_public_profile(filament_profile)

    profile_name = ORCA_PROFILE_NAME
    process_name = '0.20 Standard @Snapmaker U1 BMCU'
    filament_name = 'Snapmaker PLA @U1 BMCU'
    profile['name'] = profile_name
    profile['from'] = 'User'
    profile['printer_settings_id'] = profile_name
    profile['setting_id'] = 'BMCU_SNAPMAKER_U1_04'
    profile['default_print_profile'] = process_name
    profile['default_filament_profile'] = [filament_name]
    profile.pop('print_host', None)
    profile['printer_notes'] = 'Generated by BMCU-Klipper'
    _resize_orca_tools(
        profile, tool_count, normalized_colors, normalized_tools,
        keys=SNAPMAKER_ORCA_PER_TOOL_KEYS)
    profile['machine_start_gcode'] = _orca_start_code(
        profile.get('machine_start_gcode'), tool_count)
    profile['machine_end_gcode'] = _orca_end_code(
        profile.get('machine_end_gcode'))
    profile['change_filament_gcode'] = _orca_change_code(
        profile.get('change_filament_gcode'))

    process_profile['type'] = 'process'
    process_profile['name'] = process_name
    process_profile['from'] = 'User'
    process_profile['instantiation'] = 'true'
    process_profile['setting_id'] = 'BMCU_SNAPMAKER_U1_PROCESS_020'
    process_profile['compatible_printers'] = [profile_name]
    filament_profile['type'] = 'filament'
    filament_profile['name'] = filament_name
    filament_profile['from'] = 'User'
    filament_profile['instantiation'] = 'true'
    filament_profile['setting_id'] = 'BMCU_SNAPMAKER_U1_FILAMENT_PLA'
    filament_profile['compatible_printers'] = [profile_name]

    profile_path = 'printer/%s.json' % profile_name
    process_path = 'process/%s.json' % process_name
    filament_path = 'filament/%s.json' % filament_name
    bundle = {
        'bundle_id': '_Snapmaker_U1_BMCU',
        'bundle_type': 'printer config bundle',
        'filament_config': [filament_path],
        'printer_config': [profile_path],
        'printer_preset_name': profile_name,
        'process_config': [process_path],
        'version': '00.00.00.00',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            profile_path,
            (json.dumps(profile, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        archive.writestr(
            process_path,
            (json.dumps(process_profile, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        archive.writestr(
            filament_path,
            (json.dumps(filament_profile, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        archive.writestr(
            'bundle_structure.json',
            json.dumps(bundle, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    return output.getvalue(), tool_count


def safe_upload_name(value):
    value = pathlib.PurePath(str(value or 'firmware.bin')).name
    value = ''.join(ch if ch.isalnum() or ch in '._- ' else '_' for ch in value)
    value = value.strip(' .')[:MAX_UPLOAD_NAME]
    if not value.lower().endswith('.bin'):
        raise ValueError('select a .bin firmware file')
    return value or 'firmware.bin'

def validate_serial_port(value, require_device=True):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError('invalid serial port')
    candidate = pathlib.Path(value)
    if not candidate.is_absolute() or len(candidate.parts) < 3 or candidate.parts[1] != 'dev':
        raise ValueError('serial port must be an absolute /dev path')
    if '..' in candidate.parts:
        raise ValueError('invalid serial port path')
    if not require_device:
        return str(candidate)
    try:
        resolved = candidate.resolve(strict=True)
        device_root = pathlib.Path('/dev').resolve(strict=True)
        resolved.relative_to(device_root)
        mode = resolved.stat().st_mode
    except (OSError, RuntimeError, ValueError):
        raise ValueError('serial port does not resolve to a device')
    if not stat.S_ISCHR(mode):
        raise ValueError('serial port is not a character device')
    return str(candidate)

def _sysfs_token(path):
    try:
        with open(path, 'r', encoding='ascii', errors='strict') as stream:
            return stream.read(32).strip().lower()
    except (OSError, UnicodeError):
        return ''

def usb_serial_identity(resolved_device):

    tty = pathlib.Path(str(resolved_device)).name
    try:
        current = pathlib.Path('/sys/class/tty', tty, 'device').resolve(strict=True)
    except (OSError, RuntimeError):
        usb_ttl = tty.startswith(('ttyUSB', 'ttyCH343USB', 'ttyACM'))
        return {'usb_vid': '', 'usb_pid': '', 'driver': '', 'drivers': [],
                'ch340': False, 'usb_ttl': usb_ttl}

    drivers = []
    usb_vid = ''
    usb_pid = ''
    for parent in (current,) + tuple(current.parents):
        try:
            driver = pathlib.Path(parent, 'driver').resolve(strict=True).name.lower()
            if driver and driver not in drivers:
                drivers.append(driver)
        except (OSError, RuntimeError):
            pass
        vendor = _sysfs_token(parent / 'idVendor')
        product = _sysfs_token(parent / 'idProduct')
        if vendor and product and not usb_vid:
            usb_vid, usb_pid = vendor, product

    ch340 = any(driver == 'ch341' or driver.startswith('ch341-') for driver in drivers) or (
        usb_vid == '1a86' and usb_pid in {'5523', '7522', '7523', '7584', '55d4'})
    usb_ttl = tty.startswith(('ttyUSB', 'ttyCH343USB', 'ttyACM')) or any(
        driver in {'ch341', 'ch341-uart', 'cp210x', 'ftdi_sio', 'pl2303', 'ch343'}
        for driver in drivers)
    return {
        'usb_vid': usb_vid,
        'usb_pid': usb_pid,
        'driver': drivers[0] if drivers else '',
        'drivers': drivers,
        'ch340': bool(ch340),
        'usb_ttl': bool(usb_ttl),
    }

def _serial_path_rank(path):
    path = str(path)
    if path.startswith('/dev/serial/by-path/'):
        return 0
    if path.startswith('/dev/serial/by-id/'):
        return 1
    if path.startswith('/dev/ttyCH343USB'):
        return 2
    if path.startswith('/dev/ttyUSB'):
        return 3
    if path.startswith('/dev/ttyACM'):
        return 4
    return 5

def serial_port_candidates(excluded=None):

    patterns = (
        '/dev/serial/by-path/*',
        '/dev/serial/by-id/*',
        '/dev/ttyUSB*',
        '/dev/ttyACM*',
        '/dev/ttyCH343USB*',
    )
    grouped = {}
    for pattern in patterns:
        for candidate in sorted(pathlib.Path('/').glob(pattern.lstrip('/'))):
            path = str(candidate)
            try:
                validated = validate_serial_port(path, require_device=True)
                resolved = str(pathlib.Path(validated).resolve(strict=True))
            except Exception:
                continue
            record = grouped.get(resolved)
            if record is None:
                identity = usb_serial_identity(resolved)
                record = {
                    'path': path,
                    'device': resolved,
                    'label': pathlib.Path(path).name,
                    'aliases': [],
                    **identity,
                }
                grouped[resolved] = record
            if path not in record['aliases']:
                record['aliases'].append(path)
    excluded = set(str(value) for value in (excluded or ()) if value)
    excluded_real = set()
    for value in excluded:
        try:
            if os.path.exists(value):
                excluded_real.add(os.path.realpath(value))
        except OSError:
            pass
    result = []
    for record in grouped.values():
        if record['device'] in excluded_real or any(
                alias in excluded for alias in record['aliases']):
            continue
        preferred = min(record['aliases'], key=lambda value: (_serial_path_rank(value), value))
        record['path'] = preferred
        record['label'] = pathlib.Path(preferred).name
        record['aliases'].sort(key=lambda value: (_serial_path_rank(value), value))
        result.append(record)
    return sorted(result, key=lambda item: (_serial_path_rank(item['path']), item['path']))

def protected_serial_devices(bmcu_dir):
    metadata_path = pathlib.Path(bmcu_dir, 'runtime', 'INSTALLATION.json')
    try:
        metadata = json.loads(read_limited_text(str(metadata_path), 1024 * 1024))
    except Exception:
        return set()
    printer_cfg = os.path.realpath(str(metadata.get('printer_cfg') or ''))
    if not printer_cfg:
        return set()
    bmcu_root = os.path.realpath(str(bmcu_dir))
    include_re = re.compile(
        r'^\s*\[\s*include\s+([^\]]+)\]\s*(?:[#;].*)?$', re.IGNORECASE)
    serial_re = re.compile(
        r'/dev/(?:serial/(?:by-id|by-path)/[^\s#;,\]\)]+|'
        r'ttyUSB\d+|ttyACM\d+|ttyCH343USB\d+)')
    pending = [os.path.abspath(printer_cfg)]
    visited = set()
    used = set()
    while pending:
        path = pending.pop(0)
        absolute = os.path.abspath(path)
        if absolute in visited:
            continue
        visited.add(absolute)
        try:
            text = read_limited_text(absolute, 16 * 1024 * 1024)
        except Exception:
            continue
        real_path = os.path.realpath(absolute)
        try:
            is_bmcu = os.path.commonpath((real_path, bmcu_root)) == bmcu_root
        except ValueError:
            is_bmcu = False
        if not is_bmcu:
            for line in text.splitlines():
                active = line.split('#', 1)[0].split(';', 1)[0]
                for match in serial_re.finditer(active):
                    value = match.group(0)
                    used.add(value)
                    if os.path.exists(value):
                        used.add(os.path.realpath(value))
        directory = os.path.dirname(absolute)
        includes = []
        for line in text.splitlines():
            active = line.split('#', 1)[0]
            match = include_re.match(active)
            if not match:
                continue
            include_glob = os.path.join(directory, match.group(1).strip())
            includes.extend(sorted(glob.glob(include_glob)))
        pending[0:0] = includes
    return used

class UpdateJobs:
    def __init__(self, updater, state_dir, moonraker):
        updater_path = pathlib.Path(updater)
        if updater_path.is_symlink() or not updater_path.is_file():
            raise ValueError('firmware updater must be a regular file')
        self.updater = str(updater_path.resolve())
        state_path = pathlib.Path(state_dir).expanduser()
        state_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if state_path.is_symlink() or not state_path.is_dir():
            raise ValueError('firmware update state directory is unsafe')
        state_path = state_path.resolve(strict=True)
        info = state_path.stat()
        if info.st_uid != os.geteuid():
            raise ValueError('firmware update state directory has the wrong owner')
        os.chmod(str(state_path), 0o700)
        self.state_dir = str(state_path)
        self.upload_dir = state_path / 'uploads'
        self.upload_dir.mkdir(mode=0o700, exist_ok=True)
        if self.upload_dir.is_symlink() or not self.upload_dir.is_dir():
            raise ValueError('firmware upload directory is unsafe')
        upload_info = self.upload_dir.stat()
        if upload_info.st_uid != os.geteuid():
            raise ValueError('firmware upload directory has the wrong owner')
        os.chmod(str(self.upload_dir), 0o700)
        self.moonraker = validate_moonraker_url(moonraker)
        self.scripts_dir = updater_path.parent.resolve(strict=True)
        self.bmcu_dir = state_path.parent
        self.bmcu_cfg = self.bmcu_dir / 'bmcu.cfg'
        self.backup_dir = self.bmcu_dir / 'backups'
        self.backup_dir.mkdir(mode=0o700, exist_ok=True)
        if self.backup_dir.is_symlink() or not self.backup_dir.is_dir():
            raise ValueError('BMCU configuration backup directory is unsafe')
        backup_info = self.backup_dir.stat()
        if backup_info.st_uid != os.geteuid():
            raise ValueError('BMCU configuration backup directory has the wrong owner')
        os.chmod(str(self.backup_dir), 0o700)
        self.lock = threading.RLock()
        self.status = {
            'running': False, 'job_id': '', 'command': '', 'percent': 0,
            'stage': 'idle', 'message': '', 'logs': [], 'result': None,
            'started_at': None, 'finished_at': None,
        }

    def snapshot(self):
        with self.lock:
            value = dict(self.status)
            value['logs'] = list(self.status['logs'])
            transaction = pathlib.Path(self.state_dir, 'transaction.json')
            value['recovery_required'] = False
            value['recovery_phase'] = ''
            if transaction.is_file() and not transaction.is_symlink():
                try:
                    journal = json.loads(
                        read_limited_text(transaction, MAX_TRANSACTION_BYTES),
                        parse_constant=_reject_json_constant)
                    value['recovery_required'] = bool(
                        journal.get('recovery_required'))
                    value['recovery_phase'] = str(journal.get('phase', ''))[:64]
                except Exception:
                    value['recovery_required'] = True
                    value['recovery_phase'] = 'invalid_journal'
            return value

    def _append_log(self, level, message, job_id=None):
        with self.lock:
            if job_id is not None and self.status.get('job_id') != job_id:
                return
            logs = collections.deque(self.status.get('logs', []), maxlen=300)
            logs.append({
                'level': str(level)[:16],
                'message': str(message)[:MAX_UPDATE_LOG_MESSAGE],
                'time': time.time(),
            })
            self.status['logs'] = list(logs)

    @staticmethod
    def _file_identity(path, expected=None):
        path = pathlib.Path(path)
        flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
                 getattr(os, 'O_NOFOLLOW', 0))
        fd = os.open(str(path), flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('uploaded firmware is not a regular file')
            digest_value = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                digest_value.update(chunk)
                size += len(chunk)
                if size > MAX_FIRMWARE_BYTES:
                    raise ValueError('uploaded firmware is too large')
        finally:
            os.close(fd)
        value = {
            'dev': int(info.st_dev), 'ino': int(info.st_ino),
            'size': int(size), 'sha256': digest_value.hexdigest(),
        }
        if expected:
            for key in ('dev', 'ino', 'size', 'sha256'):
                if value[key] != expected.get(key):
                    raise ValueError('uploaded firmware changed before flashing')
        return value

    def store_upload(self, data, original_name):
        data = bytes(data)
        if not data or len(data) > MAX_FIRMWARE_BYTES:
            raise ValueError('firmware file must contain 1..%d bytes' % MAX_FIRMWARE_BYTES)
        display_name = safe_upload_name(original_name)
        upload_id = uuid.uuid4().hex
        path = self.upload_dir / ('%s.bin' % upload_id)
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0))
        fd = os.open(str(path), flags, 0o600)
        try:
            view = memoryview(data)
            written = 0
            while written < len(view):
                amount = os.write(fd, view[written:])
                if amount <= 0:
                    raise OSError('short firmware upload write')
                written += amount
            os.fsync(fd)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        identity = self._file_identity(path)
        return {
            'id': upload_id, 'path': str(path), 'name': display_name,
            **identity,
        }

    def _safe_remove_upload(self, path, identity):
        if not path:
            return
        try:
            current = self._file_identity(path, expected=identity)
            if current:
                pathlib.Path(path).unlink()
        except FileNotFoundError:
            return
        except Exception as exc:
            self._append_log('WARN',
                'temporary firmware upload was retained because it changed: %s' % exc)

    def start(self, command, port='', mode='usb', variant='universal', device='bmcu0',
              firmware='', upload=None, raw_port=False, erase_nvm=False,
              replace_nvm=False, confirm_flash_target=False, confirm_ttl_target=False):
        if command not in ('update', 'recover'):
            raise ValueError('invalid update command')
        raw_port = bool(raw_port)
        upload_info = None
        firmware_path = ''
        if command == 'update':
            port = validate_serial_port(port, require_device=True)
            if mode not in ('usb', 'ttl'):
                raise ValueError('invalid flash mode')
            if variant != 'universal':
                raise ValueError('invalid firmware variant')
            if raw_port:
                matching = [candidate for candidate in serial_port_candidates(
                            protected_serial_devices(self.bmcu_dir))
                            if port == candidate.get('path') or
                            port == candidate.get('device') or
                            port in candidate.get('aliases', [])]
                if not matching:
                    raise ValueError('raw firmware flashing requires a detected serial adapter')
                if mode == 'usb' and not matching[0].get('usb_ttl'):
                    raise ValueError('USB automatic flashing requires a detected USB serial adapter')
                device = 'raw_ch340'
            elif not isinstance(device, str) or not device or len(device) > 64 or not all(
                    ch.isalnum() or ch in '_.-' for ch in device):
                raise ValueError('invalid BMCU device name')
            if firmware:
                candidate = pathlib.Path(firmware).resolve(strict=True)
                try:
                    candidate.relative_to(self.upload_dir.resolve(strict=True))
                except ValueError:
                    raise ValueError('local firmware path is outside the upload directory')
                upload_info = dict(upload or {})
                self._file_identity(candidate, expected=upload_info)
                firmware_path = str(candidate)
            if not confirm_flash_target:
                raise ValueError('firmware flashing requires physical-target confirmation')
            if erase_nvm and replace_nvm:
                raise ValueError('erase-NVM and replace-NVM confirmations are mutually exclusive')
            if firmware_path:
                upload_size = int(upload_info.get('size', 0) or 0)
                if upload_size < 1 or (upload_size > 61440 and upload_size != 65536):
                    raise ValueError(
                        'firmware must contain 1..61440 or exactly 65536 bytes')
                if raw_port and upload_size < 65536 and not erase_nvm:
                    raise ValueError(
                        'unknown firmware requires a clean calibration NVM area')
                if raw_port and upload_size == 65536 and not replace_nvm:
                    raise ValueError(
                        'unknown firmware requires confirmation that the full image replaces NVM')
            elif raw_port and not erase_nvm:
                raise ValueError(
                    'online firmware on an unknown target requires a clean calibration NVM area')
            if mode == 'ttl' and not confirm_ttl_target:
                raise ValueError('TTL flashing requires physical-target confirmation')
        else:
            port = ''
            mode = 'usb'
            variant = 'universal'
            device = 'bmcu0'
            raw_port = False
            erase_nvm = False
            replace_nvm = False
            confirm_flash_target = False
            confirm_ttl_target = False

        with self.lock:
            if self.status.get('running'):
                raise RuntimeError('another update job is already running')
            job_id = uuid.uuid4().hex
            self.status = {
                'running': True, 'job_id': job_id, 'command': command,
                'source': 'local' if firmware_path else 'online', 'raw_port': raw_port,
                'upload': ({key: value for key, value in (upload_info or {}).items()
                            if key not in ('path', 'dev', 'ino')}),
                'percent': 0, 'stage': 'starting', 'message': '', 'logs': [],
                'result': None, 'started_at': time.time(), 'finished_at': None,
            }
        thread = threading.Thread(
            target=self._run,
            args=(job_id, command, port, mode, variant, device,
                  firmware_path, upload_info, raw_port, erase_nvm,
                  replace_nvm, confirm_ttl_target), daemon=True)
        thread.start()
        return job_id

    def restart_klipper_once(self, job_id):
        job_id = str(job_id or '')
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('invalid firmware update job id')
        with self.lock:
            if self.status.get('job_id') != job_id:
                raise ValueError('firmware update job is no longer current')
            if self.status.get('running'):
                raise RuntimeError('firmware update is still running')
            result = self.status.get('result')
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise RuntimeError('firmware update did not finish successfully')
            if not result.get('restart_required') or result.get('restart_requested'):
                return False
            result['restart_requested'] = True
        try:
            body = json.dumps({'script': 'RESTART'}, separators=(',', ':')).encode('utf-8')
            request = urllib.request.Request(
                self.moonraker.rstrip('/') + '/printer/gcode/script',
                data=body,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                method='POST')
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = response.read(MAX_PROXY_RESPONSE + 1)
            if len(payload) > MAX_PROXY_RESPONSE:
                raise RuntimeError('Moonraker response is too large')
        except Exception:
            with self.lock:
                if self.status.get('job_id') == job_id:
                    current = self.status.get('result')
                    if isinstance(current, dict):
                        current['restart_requested'] = False
            raise
        with self.lock:
            if self.status.get('job_id') == job_id:
                current = self.status.get('result')
                if isinstance(current, dict):
                    current['restart_required'] = False
                    current['restart_requested'] = True
        return True

    def _managed_access(self, device, action):
        device = str(device or '')
        action = str(action or '').upper()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', device):
            raise RuntimeError('invalid managed BMCU name')
        if action not in ('RESUME', 'UNQUIESCE'):
            raise RuntimeError('invalid managed BMCU action')
        url = self.moonraker.rstrip('/') + '/printer/gcode/script'
        body = json.dumps({
            'script': 'BMCU_UPDATE_ACCESS DEVICE=%s ACTION=%s' %
                      (device, action),
        }, separators=(',', ':')).encode('utf-8')
        request = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': 'application/json',
                     'Accept': 'application/json'},
            method='POST')
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read(MAX_PROXY_RESPONSE + 1)
        if len(payload) > MAX_PROXY_RESPONSE:
            raise RuntimeError('Moonraker response is too large')

    def _sync_host_transports(self, job_id):
        bootstrap = self.scripts_dir / 'bmcu_host_bootstrap.py'
        metadata = self.scripts_dir.parent / 'INSTALLATION.json'
        for path, label in ((bootstrap, 'host bootstrap'),
                            (metadata, 'installation metadata')):
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('%s is missing or unsafe: %s' % (label, path))
        result = subprocess.run(
            [sys.executable, '-I', '-S', str(bootstrap), '--sync-transports',
             '--metadata', str(metadata), '--quiet'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=30, close_fds=True,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                 'LANG': 'C', 'LC_ALL': 'C'})
        if result.returncode != 0:
            raise RuntimeError(
                'BMCU host transport synchronization failed: %s' %
                ((result.stdout or '').strip()[-2048:] or
                 'host bootstrap returned %d' % result.returncode))
        self._append_log(
            'INFO', 'Host transport map synchronized for the updated BMCU set',
            job_id)

    def _adopt_raw_port(self, port, job_id, released_device='', released_devices=None, mode='ttl'):

        released_devices = [str(value) for value in (released_devices or []) if str(value)]
        if released_device and released_device not in released_devices:
            released_devices.append(released_device)
        if mode == 'ttl':
            self._append_log(
                'ACTION',
                'TTL flash verified. Press the physical RESET button on the BMCU now. Waiting for the BMCU-Klipper runtime HELLO.',
                job_id)
            with self.lock:
                if self.status.get('job_id') == job_id:
                    self.status['percent'] = 99
                    self.status['stage'] = 'ttl-reset'
                    self.status['message'] = 'Flash verified - press RESET on the BMCU now'
        candidate_deadline = time.monotonic() + (90.0 if mode == 'ttl' else 15.0)
        candidates = []
        while time.monotonic() < candidate_deadline:
            candidates = [candidate for candidate in serial_port_candidates(
                          protected_serial_devices(self.bmcu_dir))
                          if (candidate.get('usb_ttl') if mode == 'usb' else True) and
                          (port == candidate.get('path') or
                           port == candidate.get('device') or
                           port in candidate.get('aliases', []))]
            if len(candidates) == 1:
                break
            if len(candidates) > 1:
                raise RuntimeError(
                    'flashed serial adapter is represented by more than one physical candidate')
            time.sleep(0.25)
        if len(candidates) != 1:
            raise RuntimeError(
                'flashed serial adapter is not present for registration')
        preferred_port = str(candidates[0]['path'])
        detector = self.scripts_dir / 'detect_bmcu.py'
        applier = self.scripts_dir / 'apply_detected_devices.py'
        for script in (detector, applier):
            if script.is_symlink() or not script.is_file():
                raise RuntimeError('device registration helper is missing or unsafe: %s' % script)
        token = uuid.uuid4().hex
        detected = pathlib.Path(self.state_dir, 'detected-%s.cfg' % token)
        try:
            deadline = time.monotonic() + (90.0 if mode == 'ttl' else 35.0)
            last = ''
            while time.monotonic() < deadline:
                command = [
                    sys.executable, '-I', '-S', str(detector),
                    '--port', preferred_port, '--output', str(detected),
                    '--stop-after-first']
                try:
                    result = subprocess.run(
                        command, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True,
                        timeout=8, close_fds=True,
                        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                             'LANG': 'C', 'LC_ALL': 'C'})
                    last = (result.stdout or '').strip()[-1024:]
                    if result.returncode == 0 and detected.is_file():
                        break
                except subprocess.TimeoutExpired:
                    last = 'runtime detection timed out'
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    'firmware was flashed, but the BMCU-Klipper runtime did not answer: %s' %
                    (last or 'no HELLO response'))

            before_cfg = self.bmcu_cfg.read_bytes() if self.bmcu_cfg.is_file() else b''
            result = subprocess.run(
                [sys.executable, '-I', '-S', str(applier),
                 '--cfg', str(self.bmcu_cfg),
                 '--detected', str(detected),
                 '--backup-dir', str(self.backup_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=12, close_fds=True,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                     'LANG': 'C', 'LC_ALL': 'C'})
            if result.returncode != 0:
                raise RuntimeError(
                    'firmware was flashed, but bmcu.cfg was not updated: %s' %
                    ((result.stdout or '').strip()[-1024:] or
                     'configuration helper failed'))
            after_cfg = self.bmcu_cfg.read_bytes()
            config_changed = before_cfg != after_cfg
            raw = detected.read_text(encoding='utf-8')
            match = re.search(
                r'(?m)^\s*bmcu\d+\s*,\s*[^,]+\s*,\s*([0-9A-Fa-f]{24})\s*$',
                raw)
            uid = match.group(1).upper() if match else ''
            if config_changed:
                self._append_log(
                    'INFO', 'New BMCU registered in bmcu.cfg; synchronizing host transports before Klipper restart',
                    job_id)
                self._sync_host_transports(job_id)
            else:
                for name in released_devices:
                    action = 'RESUME' if name == released_device else 'UNQUIESCE'
                    self._managed_access(name, action)
                self._append_log(
                    'INFO', 'BMCU was already configured; Klipper remains running',
                    job_id)
            return {
                'adopted': True,
                'restart_required': bool(config_changed),
                'runtime_uid': uid,
                'configured_port': preferred_port,
            }
        finally:
            try:
                detected.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _reap_process(process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        else:
            process.wait(timeout=0)

    def _run(self, job_id, command, port, mode, variant, device,
             firmware_path='', upload_info=None, raw_port=False,
             erase_nvm=False, replace_nvm=False, confirm_ttl_target=False):
        args = [sys.executable, '-I', '-S', self.updater, command, '--json-lines',
                '--state-dir', self.state_dir, '--moonraker', self.moonraker]
        if command == 'update':
            args.extend(['--port', port, '--mode', mode, '--variant', variant,
                         '--device', device])
            if firmware_path:
                args.extend(['--firmware', firmware_path,
                             '--expected-size', str(upload_info['size']),
                             '--expected-sha256', upload_info['sha256']])
            if raw_port:
                args.append('--raw-port')
            if erase_nvm:
                args.append('--erase-nvm')
            if replace_nvm:
                args.append('--replace-nvm')
            if confirm_ttl_target:
                args.append('--confirm-ttl-target')
        process = None
        try:
            if firmware_path:
                self._file_identity(firmware_path, expected=upload_info)
            process = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, close_fds=True,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                     'LANG': 'C', 'LC_ALL': 'C'})
            while True:
                line = process.stdout.readline(MAX_UPDATE_OUTPUT_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_UPDATE_OUTPUT_LINE:
                    raise RuntimeError('updater produced an oversized output line')
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line, parse_constant=_reject_json_constant)
                except Exception:
                    self._append_log('INFO', line, job_id)
                    continue
                if not isinstance(item, dict):
                    self._append_log('WARN', 'updater emitted a non-object JSON event', job_id)
                    continue
                kind = item.get('type')
                with self.lock:
                    if self.status.get('job_id') != job_id:
                        return
                    if kind == 'progress':
                        percent = item.get('percent', 0)
                        if isinstance(percent, bool):
                            raise ValueError('invalid updater progress percentage')
                        self.status['percent'] = max(0, min(100, int(percent)))
                        self.status['stage'] = str(item.get('stage', ''))[:64]
                        self.status['message'] = str(item.get('message', ''))[:1024]
                    elif kind == 'result':
                        self.status['result'] = item
                if kind == 'log':
                    self._append_log(item.get('level', 'INFO'), item.get('message', ''), job_id)
            code = process.wait(timeout=5)
            adoption = None
            adoption_error = ''
            with self.lock:
                current_result = dict(self.status.get('result') or {})
            result_raw_port = bool(current_result.get('raw_port', raw_port))
            result_port = str(current_result.get('port') or port)
            result_mode = str(current_result.get('mode') or mode)
            if (code == 0 and command in ('update', 'recover') and result_raw_port and
                    current_result.get('ok') is True):
                try:
                    adoption = self._adopt_raw_port(
                        result_port, job_id,
                        released_device=str(current_result.get('released_device') or ''),
                        released_devices=current_result.get('released_devices') or [],
                        mode=result_mode)
                except Exception as exc:
                    adoption_error = str(exc)
                    self._append_log('WARN', adoption_error, job_id)
                    released_device = str(current_result.get('released_device') or '')
                    released_devices = [str(value) for value in
                                        (current_result.get('released_devices') or []) if str(value)]
                    if released_device and released_device not in released_devices:
                        released_devices.append(released_device)
                    for name in released_devices:
                        try:
                            action = 'RESUME' if name == released_device else 'UNQUIESCE'
                            self._managed_access(name, action)
                        except Exception as resume_exc:
                            self._append_log(
                                'ERROR',
                                'BMCU serial guard remained active for %s; restart Klipper manually: %s' %
                                (name, resume_exc), job_id)
            with self.lock:
                if self.status.get('job_id') == job_id:
                    if self.status.get('result') is None:
                        error = ''
                        if code != 0:
                            for entry in reversed(self.status.get('logs') or []):
                                message = str(entry.get('message') or '').strip()
                                if message and message != 'Traceback (most recent call last):':
                                    error = message
                                    break
                            if not error:
                                error = 'updater exited with %d' % code
                        self.status['result'] = {
                            'ok': code == 0,
                            'error': error,
                        }
                    if adoption:
                        self.status['result'].update(adoption)
                        self.status['result']['message'] = (
                            'Firmware flashed. Host transport registered; Klipper is restarting to activate the new BMCU.'
                            if adoption.get('restart_required') else
                            'Firmware flashed. The configured BMCU will reconnect without restarting Klipper.')
                    elif adoption_error and self.status['result'].get('ok') is True:
                        self.status['result']['adopted'] = False
                        self.status['result']['adoption_error'] = adoption_error
                        self.status['result']['message'] = (
                            'Firmware flashed successfully, but automatic BMCU registration failed: %s' %
                            adoption_error)
                    self.status['running'] = False
                    self.status['finished_at'] = time.time()
                    if code == 0:
                        self.status['percent'] = 100
                        self.status['stage'] = 'done'
        except Exception as exc:
            with self.lock:
                same_job = self.status.get('job_id') == job_id
            if same_job:
                self._append_log('ERROR', str(exc), job_id)
                with self.lock:
                    if self.status.get('job_id') == job_id:
                        self.status['running'] = False
                        self.status['result'] = {'ok': False, 'error': str(exc)}
                        self.status['finished_at'] = time.time()
        finally:
            try:
                self._reap_process(process)
            except Exception as exc:
                self._append_log('ERROR', 'could not reap updater: %s' % exc, job_id)
            if firmware_path:
                self._safe_remove_upload(firmware_path, upload_info or {})

class PanelHandler(http.server.SimpleHTTPRequestHandler):
    server_version = 'BMCUPanel/%s' % PACKAGE_VERSION
    moonraker = 'http://127.0.0.1:7125'
    jobs = None
    package_build_id = 'unpackaged'
    access_token = ''
    cookie_name = 'bmcu_session'
    bmcu_dir = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(HTTP_CONNECTION_TIMEOUT)

    def log_request(self, code='-', size='-'):

        try:
            numeric = int(code)
        except (TypeError, ValueError):
            numeric = 500
        if numeric < 400:
            return
        path = urllib.parse.urlsplit(self.path).path
        self.log_message('"%s %s %s" %s %s', self.command, path,
                         self.request_version, str(code), str(size))

    def _has_session(self):
        raw = self.headers.get('Cookie', '')
        try:
            cookie = http.cookies.SimpleCookie()
            cookie.load(raw)
            morsel = cookie.get(self.cookie_name)
            value = morsel.value if morsel is not None else ''
        except Exception:
            value = ''
        return hmac.compare_digest(value, self.access_token)

    def _bootstrap_session(self):
        if self.command not in ('GET', 'HEAD'):
            return False
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in ('', '/'):
            return False
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        values = query.get('access', [])
        if len(values) != 1 or not hmac.compare_digest(values[0], self.access_token):
            return False
        self.send_response(303)
        self.send_header('Location', '/')
        self.send_header('Set-Cookie', '%s=%s; Path=/; HttpOnly; SameSite=Strict' %
                         (self.cookie_name, self.access_token))
        self.send_header('Content-Length', '0')
        self.end_headers()
        return True

    def _require_session(self):

        return True

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; base-uri 'none'; object-src 'none'; "
                         "frame-ancestors 'none'; form-action 'self'; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self' ws: wss:")
        super().end_headers()

    @staticmethod
    def _peer_gone(exc):
        return (isinstance(exc, (BrokenPipeError, ConnectionResetError,
                                ConnectionAbortedError)) or
                isinstance(exc, OSError) and
                getattr(exc, 'errno', None) in (
                    errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED))

    def _send_payload(self, status, content_type, payload):
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)
            return True
        except Exception as exc:
            if self._peer_gone(exc):
                self.close_connection = True
                return False
            raise

    def _json(self, status, value):
        payload = json.dumps(
            value, separators=(',', ':'), sort_keys=True, allow_nan=False).encode('utf-8')
        return self._send_payload(
            status, 'application/json; charset=utf-8', payload)

    def _json_error(self, status, message):
        return self._json(status, {'error': {'message': str(message)}})

    def _content_length(self, maximum):
        raw_value = self.headers.get('Content-Length', '0') or '0'
        try:
            length = int(raw_value, 10)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('invalid Content-Length')
        if length < 0:
            raise ValueError('negative Content-Length')
        if length > maximum:
            raise OverflowError('request body is too large')
        return length

    def _read_json(self):
        length = self._content_length(MAX_UPDATE_REQUEST)
        raw = self.rfile.read(length) if length else b'{}'
        value = json.loads(
            raw.decode('utf-8'), parse_constant=_reject_json_constant)
        if not isinstance(value, dict):
            raise ValueError('JSON object required')
        return value

    def _moonraker_gcode(self, script, timeout=30):
        body = json.dumps({'script': str(script)}, separators=(',', ':')).encode('utf-8')
        request = urllib.request.Request(
            self.moonraker.rstrip('/') + '/printer/gcode/script',
            data=body,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST')
        try:
            with NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
                payload = response.read(MAX_PROXY_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_PROXY_RESPONSE + 1)
            message = ''
            try:
                value = json.loads(payload.decode('utf-8'), parse_constant=_reject_json_constant)
                message = str(value.get('error', {}).get('message', '') or '')
            except Exception:
                pass
            raise RuntimeError(message or 'Moonraker rejected BMCU command')
        if len(payload) > MAX_PROXY_RESPONSE:
            raise RuntimeError('Moonraker response is too large')
        return True

    def _device_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/api/devices/forget':
            return False
        if self.command != 'POST':
            self._json_error(405, 'BMCU removal requires POST')
            return True
        try:
            if not self._same_origin_post():
                self._json_error(403, 'cross-origin BMCU removal is blocked')
                return True
            content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json':
                self._json_error(415, 'BMCU removal requires application/json')
                return True
            value = self._read_json()
            if set(value) != {'device'}:
                raise ValueError('device is required')
            device = str(value.get('device', '') or '')
            if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', device) is None:
                raise ValueError('invalid BMCU device name')
            if self.jobs is not None and self.jobs.snapshot().get('running'):
                raise RuntimeError('firmware update is running')

            scripts_dir = (self.jobs.scripts_dir if self.jobs is not None else
                           pathlib.Path(self.bmcu_dir, 'runtime', 'scripts'))
            bmcu_cfg = (self.jobs.bmcu_cfg if self.jobs is not None else
                        pathlib.Path(self.bmcu_dir, 'bmcu.cfg'))
            backup_dir = (self.jobs.backup_dir if self.jobs is not None else
                          pathlib.Path(self.bmcu_dir, 'backups'))
            applier = pathlib.Path(scripts_dir, 'apply_detected_devices.py')
            if applier.is_symlink() or not applier.is_file():
                raise RuntimeError('BMCU configuration helper is unavailable')

            self._moonraker_gcode(
                'BMCU_FORGET_DEVICE DEVICE=%s ACTION=PREPARE' % device)
            prepared = True
            try:
                result = subprocess.run(
                    [sys.executable, '-I', '-S', str(applier),
                     '--cfg', str(bmcu_cfg), '--remove', device,
                     '--backup-dir', str(backup_dir)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=12, close_fds=True,
                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                         'LANG': 'C', 'LC_ALL': 'C'})
                if result.returncode != 0:
                    raise RuntimeError(
                        (result.stdout or '').strip()[-1024:] or
                        'BMCU configuration helper failed')
            except Exception:
                if prepared:
                    try:
                        self._moonraker_gcode(
                            'BMCU_FORGET_DEVICE DEVICE=%s ACTION=CANCEL' % device)
                    except Exception:
                        pass
                raise
            transport_warning = ''
            try:
                if self.jobs is not None:
                    self.jobs._sync_host_transports(None)
                else:
                    bootstrap = pathlib.Path(scripts_dir, 'bmcu_host_bootstrap.py')
                    metadata = pathlib.Path(scripts_dir.parent, 'INSTALLATION.json')
                    if bootstrap.is_symlink() or not bootstrap.is_file():
                        raise RuntimeError('BMCU host bootstrap is unavailable')
                    if metadata.is_symlink() or not metadata.is_file():
                        raise RuntimeError('BMCU installation metadata is unavailable')
                    result = subprocess.run(
                        [sys.executable, '-I', '-S', str(bootstrap), '--sync-transports',
                         '--metadata', str(metadata), '--quiet'],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, timeout=30, close_fds=True,
                        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                             'LANG': 'C', 'LC_ALL': 'C'})
                    if result.returncode != 0:
                        raise RuntimeError(
                            'BMCU host transport synchronization failed: %s' %
                            ((result.stdout or '').strip()[-2048:] or
                             'host bootstrap returned %d' % result.returncode))
            except Exception as exc:
                transport_warning = str(exc)[:2048]
            payload = {
                'ok': True, 'device': device, 'restart_required': True,
            }
            if transport_warning:
                payload['warning'] = transport_warning
            self._json(200, payload)
        except RuntimeError as exc:
            self._json_error(409, exc)
        except Exception as exc:
            self._json_error(400, exc)
        return True

    def _version_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/api/version':
            return False
        if self.command != 'GET':
            self._json_error(405, 'version check requires GET')
            return True
        try:
            value = _remote_release_versions()
            self._json(200, {'ok': True, **value})
        except Exception:
            self._json(200, {'ok': False})
        return True

    def _config_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/api/config':
            return False
        if self.command != 'GET':
            self._json_error(405, 'panel config is read-only')
            return True
        self._json(200, {
            'moonraker_websocket': moonraker_websocket_config(self.moonraker),
            'firmware_update_available': self.jobs is not None,
            'package_version': PACKAGE_VERSION,
            'package_build_id': self.package_build_id,
            'required_firmware_version': REQUIRED_FIRMWARE_VERSION,
            'serial_ports': serial_port_candidates(
                protected_serial_devices(self.bmcu_dir)),
        })
        return True

    def _installation_metadata(self):
        path = os.path.join(self.bmcu_dir, 'runtime', 'INSTALLATION.json')
        try:
            text = read_limited_text(path, 1024 * 1024)
            value = json.loads(text, parse_constant=_reject_json_constant)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _printer_data_dir(self):
        config_dir = str(self._installation_metadata().get('config_dir') or '')
        if not config_dir:
            return ''
        return os.path.dirname(os.path.realpath(config_dir))

    def _stream_file(self, path, content_type, filename):
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('file is unavailable')
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % filename)
            self.send_header('Content-Length', str(info.st_size))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if self.command != 'HEAD':
                while True:
                    chunk = os.read(fd, 256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return True
        except Exception as exc:
            if self._peer_gone(exc):
                self.close_connection = True
                return False
            raise
        finally:
            os.close(fd)

    def _diagnostics_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/api/diagnostics/export':
            return False
        if self.command != 'GET':
            self._json_error(405, 'diagnostics export requires GET')
            return True
        output = ''
        try:
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {'include'}:
                raise ValueError('unknown diagnostics option')
            values = query.get('include', [''])
            if len(values) != 1:
                raise ValueError('invalid diagnostics selection')
            selected = {value for value in values[0].split(',') if value}
            allowed = {
                'bmcu_logs': '--include-bmcu-logs',
                'klipper_log': '--include-klipper-log',
                'moonraker_log': '--include-moonraker-log',
                'config': '--include-config',
                'runtime': '--include-runtime',
                'printer_status': '--include-printer-status',
                'last_gcode': '--include-last-gcode',
            }
            unknown = selected - set(allowed)
            if unknown:
                raise ValueError('unknown diagnostics selection')

            runtime = os.path.realpath(os.path.join(self.bmcu_dir, 'runtime'))
            collector = os.path.realpath(os.path.join(runtime, 'scripts', 'bmcu_collect_logs.py'))
            if (os.path.commonpath((collector, runtime)) != runtime or
                    os.path.islink(collector) or not os.path.isfile(collector)):
                raise RuntimeError('diagnostics collector is unavailable')

            output = os.path.join(
                self.bmcu_dir,
                '.bmcu-diagnostics-%d-%s.tar.gz' % (os.getpid(), uuid.uuid4().hex[:12]))
            command = [
                sys.executable, '-I', '-S', collector,
                '--bmcu-dir', self.bmcu_dir,
                '--moonraker', self.moonraker,
                '--output', output,
                '--selection-explicit',
            ]
            command.extend(allowed[key] for key in allowed if key in selected)
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=DIAGNOSTICS_EXPORT_TIMEOUT,
                close_fds=True,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                     'LANG': 'C', 'LC_ALL': 'C'})
            if result.returncode != 0 or not os.path.isfile(output):
                raise RuntimeError(
                    (result.stdout or '').strip()[-2048:] or
                    'diagnostics collector failed')
            filename = 'bmcu-diagnostics-%s.tar.gz' % time.strftime('%Y%m%d-%H%M%S')
            self._stream_file(output, 'application/gzip', filename)
        except subprocess.TimeoutExpired:
            self._json_error(504, 'diagnostics export timed out')
        except RuntimeError as exc:
            self._json_error(500, exc)
        except ValueError as exc:
            self._json_error(400, exc)
        except OSError as exc:
            self._json_error(500, exc)
        finally:
            if output:
                try:
                    os.unlink(output)
                except OSError:
                    pass
        return True

    def _orca_profile_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/api/orca/profile':
            return False
        if self.command != 'GET':
            self._json_error(405, 'Orca profile download requires GET')
            return True
        try:
            query = urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=False)
            if set(query) - {'bmcu_count', 'tools', 'colors', 'format'}:
                raise ValueError('unknown Orca profile parameter')
            count_values = query.get('bmcu_count', [])
            if len(count_values) != 1:
                raise ValueError('bmcu_count is required')
            color_text = query.get('colors', [''])
            if len(color_text) != 1:
                raise ValueError('invalid colors parameter')
            colors = [value for value in color_text[0].split(',') if value]
            tool_text = query.get('tools', [None])
            if len(tool_text) != 1:
                raise ValueError('invalid tools parameter')
            tools = (None if tool_text[0] is None else
                     [value for value in tool_text[0].split(',') if value])
            format_values = query.get('format', ['json'])
            if len(format_values) != 1 or format_values[0] not in ('json', 'bundle'):
                raise ValueError('format must be json or bundle')
            bundle_payload, profile_payload, _tool_count = build_orca_bundle(
                self.directory, count_values[0], colors,
                self.headers.get('Host', ''), tools=tools)
            if format_values[0] == 'bundle':
                payload = bundle_payload
                filename = '%s.orca_printer' % ORCA_PROFILE_NAME
                content_type = 'application/octet-stream'
            else:
                payload = profile_payload
                filename = '%s.json' % ORCA_PROFILE_NAME
                content_type = 'application/json; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header(
                'Content-Disposition', 'attachment; filename="%s"' % filename)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            self._json_error(400, exc)
        return True

    def _snapmaker_orca_profile_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/api/snapmaker-orca/profile':
            return False
        if self.command != 'GET':
            self._json_error(405, 'Snapmaker Orca profile download requires GET')
            return True
        try:
            query = urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=False)
            if set(query) - {'bmcu_count', 'tools', 'colors'}:
                raise ValueError('unknown Snapmaker Orca profile parameter')
            count_values = query.get('bmcu_count', [])
            if len(count_values) != 1:
                raise ValueError('bmcu_count is required')
            color_text = query.get('colors', [''])
            if len(color_text) != 1:
                raise ValueError('invalid colors parameter')
            colors = [value for value in color_text[0].split(',') if value]
            tool_text = query.get('tools', [None])
            if len(tool_text) != 1:
                raise ValueError('invalid tools parameter')
            tools = (None if tool_text[0] is None else
                     [value for value in tool_text[0].split(',') if value])
            payload, _tool_count = build_snapmaker_orca_bundle(
                self.directory, count_values[0], colors,
                self.headers.get('Host', ''), tools=tools)
            filename = '%s.orca_printer' % ORCA_PROFILE_NAME
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header(
                'Content-Disposition', 'attachment; filename="%s"' % filename)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            self._json_error(400, exc)
        return True

    def _update_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith('/api/update/'):
            return False
        if self.jobs is None:
            self._json_error(503, 'firmware updater is not configured')
            return True
        action = parsed.path.rsplit('/', 1)[-1]
        if self.command == 'GET' and action == 'status':
            self._json(200, self.jobs.snapshot())
            return True
        if self.command == 'GET' and action == 'ports':
            self._json(200, {'ports': serial_port_candidates(
                protected_serial_devices(self.bmcu_dir))})
            return True
        if self.command == 'POST' and action == 'restart':
            try:
                if not self._same_origin_post():
                    self._json_error(403, 'cross-origin firmware restart is blocked')
                    return True
                value = self._read_json()
                if set(value) - {'job_id'}:
                    raise ValueError('unknown firmware restart field')
                triggered = self.jobs.restart_klipper_once(str(value.get('job_id', '')))
                self._json(200, {'ok': True, 'triggered': triggered})
            except RuntimeError as exc:
                self._json_error(409, exc)
            except Exception as exc:
                self._json_error(400, exc)
            return True
        if self.command == 'POST' and action == 'recover':
            try:
                if not self._same_origin_post():
                    self._json_error(403, 'cross-origin firmware recovery is blocked')
                    return True
                job_id = self.jobs.start('recover')
                self._json(202, {'ok': True, 'job_id': job_id})
            except RuntimeError as exc:
                self._json_error(409, exc)
            except Exception as exc:
                self._json_error(400, exc)
            return True
        if self.command == 'POST' and action == 'online':
            try:
                if not self._same_origin_post():
                    self._json_error(403, 'cross-origin firmware update is blocked')
                    return True
                content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if content_type != 'application/json':
                    self._json_error(415, 'online firmware update requires application/json')
                    return True
                value = self._read_json()
                allowed = {'port', 'mode', 'device', 'raw', 'flash_target', 'erase_nvm',
                           'replace_nvm', 'ttl_target'}
                if set(value) - allowed:
                    raise ValueError('unknown online firmware update field')
                job_id = self.jobs.start(
                    'update', str(value.get('port', '')), str(value.get('mode', 'usb')),
                    'universal', str(value.get('device', 'bmcu0')), firmware='', upload=None,
                    raw_port=bool(value.get('raw')), erase_nvm=bool(value.get('erase_nvm')),
                    replace_nvm=bool(value.get('replace_nvm')),
                    confirm_flash_target=bool(value.get('flash_target')),
                    confirm_ttl_target=bool(value.get('ttl_target')))
                self._json(202, {'ok': True, 'job_id': job_id, 'source': 'online'})
            except RuntimeError as exc:
                self._json_error(409, exc)
            except Exception as exc:
                self._json_error(400, exc)
            return True
        if self.command != 'POST' or action != 'upload':
            self._json_error(404, 'unknown update API route')
            return True
        try:
            if not self._same_origin_post():
                self._json_error(403, 'cross-origin firmware update is blocked')
                return True
            content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/octet-stream':
                self._json_error(415, 'firmware upload requires application/octet-stream')
                return True
            length = self._content_length(MAX_FIRMWARE_BYTES)
            if length < 1:
                raise ValueError('firmware upload is empty')
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError('firmware upload was truncated')
            upload = self.jobs.store_upload(
                data, self.headers.get('X-BMCU-Filename', 'firmware.bin'))
            try:
                def flag(name):
                    value = str(self.headers.get(name, '0')).strip()
                    if value not in ('0', '1'):
                        raise ValueError('invalid %s flag' % name)
                    return value == '1'
                job_id = self.jobs.start(
                    'update',
                    str(self.headers.get('X-BMCU-Port', '')),
                    str(self.headers.get('X-BMCU-Mode', 'usb')),
                    str(self.headers.get('X-BMCU-Variant', 'universal')),
                    str(self.headers.get('X-BMCU-Device', 'bmcu0')),
                    firmware=upload['path'], upload=upload,
                    raw_port=flag('X-BMCU-Raw'),
                    confirm_flash_target=flag('X-BMCU-Flash-Target'),
                    erase_nvm=flag('X-BMCU-Erase-NVM'),
                    replace_nvm=flag('X-BMCU-Replace-NVM'),
                    confirm_ttl_target=flag('X-BMCU-TTL-Target'))
            except Exception:
                self.jobs._safe_remove_upload(upload['path'], upload)
                raise
            self._json(202, {
                'ok': True, 'job_id': job_id,
                'upload': {key: value for key, value in upload.items()
                           if key not in ('path', 'dev', 'ino')},
            })
        except OverflowError as exc:
            self._json_error(413, exc)
        except RuntimeError as exc:
            self._json_error(409, exc)
        except Exception as exc:
            self._json_error(400, exc)
        return True

    def _transport_status_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != '/api/transport-status':
            return False
        if self.command != 'GET':
            self._json_error(405, 'transport status is read-only')
            return True
        result = {'schema': 1, 'devices': []}
        record_path = pathlib.Path(self.bmcu_dir, 'transport-processes.json')
        try:
            if not record_path.is_file() or record_path.is_symlink():
                self._json(200, result)
                return True
            if record_path.stat().st_size > 1024 * 1024:
                raise ValueError('transport process metadata is too large')
            records = json.loads(record_path.read_text(encoding='utf-8'))
            devices = records.get('devices', {}) if isinstance(records, dict) else {}
            if not isinstance(devices, dict):
                devices = {}
            for name, record in sorted(devices.items()):
                if not re.fullmatch(r'bmcu\d+', str(name)) or not isinstance(record, dict):
                    continue
                pid = int(record.get('pid', 0) or 0)
                daemon_path = os.path.realpath(str(record.get('daemon', '') or ''))
                process_online = False
                if pid > 1 and daemon_path:
                    try:
                        os.kill(pid, 0)
                        raw_cmdline = pathlib.Path(
                            '/proc/%d/cmdline' % pid).read_bytes()
                        if len(raw_cmdline) <= 64 * 1024:
                            argv = [part.decode('utf-8', 'replace')
                                    for part in raw_cmdline.split(b'\0') if part]
                            process_online = (
                                daemon_path in [os.path.realpath(value) for value in argv
                                                if value.startswith('/')] and
                                '--name' in argv and name in argv)
                    except (OSError, ValueError):
                        process_online = False
                status_path = pathlib.Path(str(record.get('status_file', '') or ''))
                socket_path = pathlib.Path(str(record.get('socket', '') or ''))
                socket_online = False
                try:
                    socket_online = (
                        not socket_path.is_symlink() and socket_path.is_socket())
                except OSError:
                    socket_online = False
                value = {
                    'schema': 1, 'name': name, 'online': False,
                    'process_online': process_online,
                    'socket_online': socket_online,
                    'updated_at': 0.0, 'status': None,
                }
                try:
                    if (status_path.is_file() and not status_path.is_symlink() and
                            status_path.stat().st_size <= 1024 * 1024 and
                            status_path.parent == socket_path.parent):
                        loaded = json.loads(status_path.read_text(encoding='utf-8'))
                        if (isinstance(loaded, dict) and
                                loaded.get('name') == name and
                                int(loaded.get('pid', 0) or 0) == pid):
                            value.update(loaded)
                    value['process_online'] = process_online
                    value['socket_online'] = socket_online
                    value['online'] = bool(
                        value.get('online') and process_online and
                        value['socket_online'])
                except Exception as exc:
                    value['error'] = str(exc)[:160]
                    value['online'] = False
                result['devices'].append(value)
        except Exception as exc:
            result['error'] = str(exc)[:240]
        self._json(200, result)
        return True

    def _proxy_target(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith(PROXY_PREFIX + '/'):
            return None
        upstream_path = parsed.path[len(PROXY_PREFIX):]
        if upstream_path not in ALLOWED_PROXY_PATHS:
            return False
        query = ('?' + parsed.query) if parsed.query else ''
        return self.moonraker.rstrip('/') + upstream_path + query

    def _same_origin_post(self):
        source = self.headers.get('Origin') or self.headers.get('Referer')
        if not source:
            return False
        parsed = urllib.parse.urlsplit(source)
        host = self.headers.get('Host', '').strip().lower()
        return (parsed.scheme in ('http', 'https') and not parsed.username and
                not parsed.password and parsed.netloc.lower() == host)

    def _proxy(self):
        target = self._proxy_target()
        if target is None:
            return False
        if target is False:
            self._json_error(403, 'Moonraker route is not allowed')
            return True
        if self.command == 'POST':
            if not self._same_origin_post():
                self._json_error(403, 'cross-origin Moonraker control is blocked')
                return True
            content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json':
                self._json_error(415, 'Moonraker control requires application/json')
                return True
        try:
            length = self._content_length(MAX_PROXY_REQUEST)
        except OverflowError as exc:
            self._json_error(413, exc)
            return True
        except ValueError as exc:
            self._json_error(400, exc)
            return True
        body = self.rfile.read(length) if length else None
        upstream_path = urllib.parse.urlsplit(target).path
        if upstream_path == '/printer/objects/query' and self.command != 'GET':
            self._json_error(405, 'object query is read-only')
            return True
        proxy_timeout = PROXY_QUERY_TIMEOUT
        if upstream_path == '/printer/gcode/script':
            if self.command != 'POST':
                self._json_error(405, 'G-code script requires POST')
                return True
            try:
                command_payload = validate_panel_gcode(body or b'{}')
            except ValueError as exc:
                self._json_error(403, exc)
                return True
            command = command_payload['script'].strip().split(None, 1)[0].upper()
            proxy_timeout = (PROXY_MOTION_TIMEOUT if command in LONG_PANEL_COMMANDS
                             else PROXY_COMMAND_TIMEOUT)
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = self.headers.get('Content-Type', 'application/json')
        request = urllib.request.Request(target, data=body, headers=headers, method=self.command)
        try:
            with NO_REDIRECT_OPENER.open(request, timeout=proxy_timeout) as response:
                payload = response.read(MAX_PROXY_RESPONSE + 1)
                if len(payload) > MAX_PROXY_RESPONSE:
                    self._json_error(502, 'Moonraker response is too large')
                    return True
                self._send_payload(
                    response.status,
                    response.headers.get('Content-Type', 'application/json'),
                    payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_PROXY_RESPONSE + 1)
            if len(payload) > MAX_PROXY_RESPONSE:
                self._json_error(502, 'Moonraker error response is too large')
                return True
            self._send_payload(
                exc.code,
                exc.headers.get('Content-Type', 'application/json'),
                payload)
        except Exception as exc:
            if self._peer_gone(exc):
                self.close_connection = True
            else:
                self._json_error(502, 'Moonraker is unavailable: %s' % exc)
        return True

    def list_directory(self, path):
        self._json_error(404, 'directory listing is disabled')
        return None

    def do_GET(self):
        if not self._require_session():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/favicon.ico':
            self.send_response(204)
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if (self._config_api() or self._version_api() or
                self._orca_profile_api() or self._snapmaker_orca_profile_api() or
                self._update_api() or self._device_api() or
                self._transport_status_api() or self._diagnostics_api() or
                self._proxy()):
            return
        if parsed.path in ('', '/'):
            primary = pathlib.Path(self.directory, 'bmcu-panel.html')
            self.path = '/bmcu-panel.html' if primary.is_file() else '/index.html'
        super().do_GET()

    def do_HEAD(self):
        if not self._require_session():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ('', '/'):
            primary = pathlib.Path(self.directory, 'bmcu-panel.html')
            self.path = '/bmcu-panel.html' if primary.is_file() else '/index.html'
        super().do_HEAD()

    def do_POST(self):
        if not self._require_session():
            return
        if (self._update_api() or self._device_api() or
                self._diagnostics_api() or self._proxy()):
            return
        self._json_error(404, 'not found')

class ReusableThreadingTCPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_HTTP_CONNECTIONS

    def __init__(self, *args, **kwargs):
        self._connection_slots = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(False):
            self.close_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

def make_handler(root, moonraker, jobs=None, access_token=None, bmcu_dir=None):
    class ConfiguredPanelHandler(PanelHandler):
        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, directory=root, **handler_kwargs)
    ConfiguredPanelHandler.moonraker = validate_moonraker_url(moonraker)
    ConfiguredPanelHandler.jobs = jobs
    ConfiguredPanelHandler.bmcu_dir = os.path.realpath(str(bmcu_dir or ''))
    digest_path = os.path.join(
        os.path.dirname(os.path.abspath(root)), 'package.sha256')
    build_id = 'unpackaged'
    try:
        with open(digest_path, 'r', encoding='ascii') as stream:
            digest = stream.read(256).strip().split()[0]
        if re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            build_id = digest.lower()[:16]
    except (IOError, OSError, IndexError):
        pass
    ConfiguredPanelHandler.package_build_id = build_id
    return ConfiguredPanelHandler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8291)
    parser.add_argument('--root', default=None)
    parser.add_argument('--moonraker', default='http://127.0.0.1:7125')
    parser.add_argument('--updater', default='')
    parser.add_argument('--state-dir', default='~/printer_data/config/bmcu/update')
    parser.add_argument('--access-token', default='')
    args = parser.parse_args()

    root = args.root or os.path.dirname(os.path.abspath(__file__))
    moonraker = validate_moonraker_url(args.moonraker)
    jobs = None
    if args.updater:
        jobs = UpdateJobs(args.updater, args.state_dir, moonraker)
    bmcu_dir = os.path.dirname(os.path.realpath(os.path.expanduser(args.state_dir)))
    handler = make_handler(root, moonraker, jobs, args.access_token, bmcu_dir)
    with ReusableThreadingTCPServer((args.host, args.port), handler) as httpd:
        print('BMCU panel: http://%s:%d/ (Moonraker: %s)' %
              (args.host, args.port, moonraker), flush=True)
        httpd.serve_forever()

if __name__ == '__main__':
    main()
