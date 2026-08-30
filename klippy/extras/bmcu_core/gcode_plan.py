# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import print_function

import os
import re
import stat
import time

PLAN_SCHEMA = 1
LOGICAL_TOOL_LIMIT = 32
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_PLAN_ENTRIES = 65536
MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024

_TOOL_PATTERN = re.compile(
    br'^\s*BMCU_TOOL_CHANGE\s+TOOL\s*=\s*([0-9]+)(?:\s|$)',
    re.IGNORECASE)
_HEADER_PATTERN = re.compile(
    br'^\s*;\s*(nozzle_temperature|'
    br'nozzle_temperature_initial_layer|'
    br'nozzle_temperature_range_low|'
    br'nozzle_temperature_range_high)\s*=\s*(.*?)\s*$',
    re.IGNORECASE)
_TEMP_COMMAND = re.compile(r'^M10[49](?:\s|$)', re.IGNORECASE)
_TEMP_PARAMETER = re.compile(
    r'(?:^|\s)([ST])\s*=?\s*([-+]?[0-9]+(?:\.[0-9]+)?)',
    re.IGNORECASE)

class PlanError(RuntimeError):
    pass

def _mtime_ns(info):
    value = getattr(info, 'st_mtime_ns', None)
    if value is not None:
        return int(value)
    return int(float(info.st_mtime) * 1000000000.0)

def stat_identity(info):
    return {
        'dev': int(info.st_dev),
        'ino': int(info.st_ino),
        'size': int(info.st_size),
        'mtime_ns': _mtime_ns(info),
    }

def _same_identity(info, expected):
    if not isinstance(expected, dict):
        return True
    actual = stat_identity(info)
    for key in ('dev', 'ino', 'size', 'mtime_ns'):
        try:
            wanted = int(expected[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise PlanError('invalid G-code identity field %s' % key)
        if actual[key] != wanted:
            return False
    return True

def parse_temperature_list(raw_value):
    values = []
    for token in str(raw_value or '').split(','):
        token = token.strip()
        if not token:
            values.append(None)
            continue
        try:
            value = float(token)
        except (TypeError, ValueError, OverflowError):
            values.append(None)
            continue
        values.append(value if 0.0 < value <= 350.0 else None)
    return values

def parse_temperature_command(raw_line, current_tool=None):
    if isinstance(raw_line, bytes):
        line = raw_line.decode('ascii', 'ignore')
    else:
        line = str(raw_line or '')
    line = line.split(';', 1)[0].strip()
    if not _TEMP_COMMAND.match(line):
        return None
    parameters = {}
    for match in _TEMP_PARAMETER.finditer(line):
        parameters[match.group(1).upper()] = match.group(2)
    if 'S' not in parameters:
        return None
    try:
        temperature = float(parameters['S'])
        tool = (int(float(parameters['T']))
                if 'T' in parameters else int(current_tool))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= tool < LOGICAL_TOOL_LIMIT:
        return None
    if not 0.0 <= temperature <= 350.0:
        return None
    return tool, temperature

def temperature_candidate_valid(temperature):
    try:
        value = float(temperature)
    except (TypeError, ValueError, OverflowError):
        return False
    return 150.0 <= value <= 350.0

def _temperature_defaults(header_values):
    defaults = header_values.get('nozzle_temperature', [])
    initial = header_values.get('nozzle_temperature_initial_layer', [])
    lows = header_values.get('nozzle_temperature_range_low', [])
    highs = header_values.get('nozzle_temperature_range_high', [])
    profiles = {}
    for tool in range(LOGICAL_TOOL_LIMIT):
        normal_temp = defaults[tool] if tool < len(defaults) else None
        initial_temp = initial[tool] if tool < len(initial) else None
        fallback = normal_temp if normal_temp is not None else initial_temp
        if fallback is None:
            continue
        profile = {
            'temperature': float(fallback),
            'source': 'gcode_header',
        }
        if initial_temp is not None:
            profile['initial_temperature'] = float(initial_temp)
        if tool < len(lows) and lows[tool] is not None:
            profile['temperature_min'] = float(lows[tool])
        if tool < len(highs) and highs[tool] is not None:
            profile['temperature_max'] = float(highs[tool])
        profiles[str(tool)] = profile
    return profiles

def _select_temperature(item, after_candidate):
    candidates = []
    before_candidate = item.pop('_before_temperature', None)
    if before_candidate is not None:
        candidates.append(before_candidate)
    if after_candidate is not None:
        candidates.append(after_candidate)
    if not candidates:
        return
    selected = min(
        candidates,
        key=lambda value: (
            abs(int(value[0]) - int(item['offset'])),
            1 if int(value[0]) > int(item['offset']) else 0))
    item['temperature'] = float(selected[1])
    item['temperature_source'] = 'gcode_toolchange'

def _apply_defaults(plan, defaults):
    seen_tools = set()
    for item in plan:
        tool = int(item['tool'])
        profile = defaults.get(str(tool), {})
        if item.get('temperature') is None:
            fallback = None
            if tool not in seen_tools:
                fallback = profile.get('initial_temperature')
            if fallback is None:
                fallback = profile.get('temperature')
            if fallback is not None:
                item['temperature'] = float(fallback)
                item['temperature_source'] = 'gcode_header'
        for key in ('temperature_min', 'temperature_max'):
            value = profile.get(key)
            if value is not None:
                item[key] = float(value)
        seen_tools.add(tool)

def scan_file(path, expected_identity=None):
    path = os.path.abspath(str(path or ''))
    if not path or '\x00' in path or any(ord(ch) < 32 for ch in path):
        raise PlanError('invalid G-code path')
    flags = os.O_RDONLY
    if hasattr(os, 'O_CLOEXEC'):
        flags |= os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PlanError('cannot open G-code: %s' % exc)
    started = time.monotonic()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PlanError('G-code is not a regular file')
        if before.st_size < 0 or before.st_size > MAX_FILE_BYTES:
            raise PlanError('G-code size is outside the planner limit')
        if not _same_identity(before, expected_identity):
            raise PlanError('G-code changed before source planning')

        plan = []
        selected_tool = None
        offset = 0
        header_values = {}

        interval_first = [None] * LOGICAL_TOOL_LIMIT
        interval_last = [None] * LOGICAL_TOOL_LIMIT

        with os.fdopen(fd, 'rb', buffering=256 * 1024) as stream:
            fd = -1
            while True:
                raw_line = stream.readline(MAX_LINE_BYTES + 1)
                if not raw_line:
                    break
                if len(raw_line) > MAX_LINE_BYTES:
                    raise PlanError(
                        'G-code line exceeds %d bytes' % MAX_LINE_BYTES)
                stripped = raw_line.rstrip(b'\r\n')
                header_match = _HEADER_PATTERN.match(stripped)
                if header_match:
                    key = header_match.group(1).decode(
                        'ascii', 'ignore').lower()
                    value = header_match.group(2).decode('ascii', 'ignore')
                    header_values[key] = parse_temperature_list(value)

                parsed = parse_temperature_command(raw_line, selected_tool)
                if parsed is not None:
                    tool, temperature = parsed
                    if temperature_candidate_valid(temperature):
                        candidate = (int(offset), float(temperature))
                        if interval_first[tool] is None:
                            interval_first[tool] = candidate
                        interval_last[tool] = candidate

                tool_match = _TOOL_PATTERN.match(raw_line)
                if tool_match:
                    tool = int(tool_match.group(1))
                    if 0 <= tool < LOGICAL_TOOL_LIMIT:
                        if len(plan) >= MAX_PLAN_ENTRIES:
                            raise PlanError('G-code has too many tool changes')
                        if plan:
                            previous = plan[-1]
                            _select_temperature(
                                previous,
                                interval_first[int(previous['tool'])])
                        item = {
                            'offset': int(offset),
                            'tool': int(tool),
                            '_before_temperature': interval_last[tool],
                        }
                        plan.append(item)
                        selected_tool = tool
                        interval_first = [None] * LOGICAL_TOOL_LIMIT
                        interval_last = [None] * LOGICAL_TOOL_LIMIT
                offset += len(raw_line)
            if plan:
                previous = plan[-1]
                _select_temperature(
                    previous, interval_first[int(previous['tool'])])
            after = os.fstat(stream.fileno())

        if not _same_identity(after, stat_identity(before)):
            raise PlanError('G-code changed during source planning')
        if offset != int(after.st_size):
            raise PlanError('G-code size changed during source planning')
        defaults = _temperature_defaults(header_values)
        _apply_defaults(plan, defaults)
        return {
            'schema': PLAN_SCHEMA,
            'path': path,
            'identity': stat_identity(after),
            'plan': plan,
            'defaults': defaults,
            'elapsed_ms': round((time.monotonic() - started) * 1000.0, 3),
        }
    finally:
        if fd >= 0:
            os.close(fd)
