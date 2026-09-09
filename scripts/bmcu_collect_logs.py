#!/usr/bin/env python3

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import socket
import sys
import tarfile
import time
import urllib.request

MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024


def safe_name(value):
    value = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-._')
    return value[:120] or 'file'


def read_json(path):
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            return {}
        value = json.loads(raw.decode('utf-8'))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def request_json(base, path):
    try:
        url = base.rstrip('/') + path
        request = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            return None
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return None


def inside(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


def regular_file(path):
    try:
        return os.path.isfile(path) and not os.path.islink(path)
    except OSError:
        return False


def tail_bytes(path, maximum=MAX_LOG_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as stream:
            if size > maximum:
                stream.seek(-maximum, os.SEEK_END)
            return stream.read(maximum + 1), size > maximum
    except OSError:
        return b'', False


def add_bytes(archive, arcname, payload, mode=0o640):
    info = tarfile.TarInfo(arcname)
    info.size = len(payload)
    info.mode = mode
    info.mtime = int(time.time())
    archive.addfile(info, io.BytesIO(payload))


def add_json(archive, arcname, value):
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')
    add_bytes(archive, arcname, payload)


def add_log(archive, manifest, label, path):
    if not regular_file(path):
        return
    payload, truncated = tail_bytes(path)
    if not payload and os.path.getsize(path):
        return
    name = safe_name(label) + '.log'
    add_bytes(archive, 'logs/' + name, payload)
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        size = len(payload)
        mtime = 0
    manifest['logs'].append({
        'name': label,
        'source': os.path.realpath(path),
        'archive': 'logs/' + name,
        'size': int(size),
        'truncated_to_last_bytes': MAX_LOG_BYTES if truncated else 0,
        'modified_at': float(mtime),
    })


def installation_context(bmcu_dir):
    metadata = read_json(os.path.join(bmcu_dir, 'runtime', 'INSTALLATION.json'))
    config_dir = os.path.realpath(str(metadata.get('config_dir') or '')) if metadata.get('config_dir') else ''
    printer_data = os.path.dirname(config_dir) if config_dir else ''
    moonraker = str(metadata.get('moonraker_url') or 'http://127.0.0.1:7125')
    return metadata, config_dir, printer_data, moonraker


def moonraker_root(moonraker, name):
    payload = request_json(moonraker, '/server/files/roots')
    items = payload.get('result', []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return ''
    for item in items:
        if not isinstance(item, dict) or str(item.get('name', '')).lower() != name.lower():
            continue
        path = os.path.realpath(str(item.get('path') or ''))
        if os.path.isdir(path):
            return path
    return ''


def active_klippy_log():
    try:
        entries = os.listdir('/proc')
    except OSError:
        return ''
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % entry, 'rb') as stream:
                raw = stream.read(65536)
        except OSError:
            continue
        args = [os.fsdecode(value) for value in raw.split(b'\0') if value]
        if not any(value.endswith('/klippy.py') for value in args):
            continue
        for index, value in enumerate(args):
            candidate = ''
            if value == '-l' and index + 1 < len(args):
                candidate = args[index + 1]
            elif value.startswith('-l') and len(value) > 2:
                candidate = value[2:]
            if candidate and regular_file(candidate):
                return os.path.realpath(candidate)
    return ''


def log_candidates(bmcu_dir, printer_data, moonraker):
    result = []
    seen = set()
    labels = set()

    def add(label, path):
        path = os.path.realpath(path)
        if label in labels or path in seen or not regular_file(path):
            return
        labels.add(label)
        seen.add(path)
        result.append((label, path))

    active_log = active_klippy_log()
    if active_log:
        add('klipper', active_log)

    log_dirs = []
    registered_logs = moonraker_root(moonraker, 'logs')
    if registered_logs:
        log_dirs.append(registered_logs)
    if printer_data:
        log_dirs.append(os.path.join(printer_data, 'logs'))
    log_dirs.extend(('/oem/klippylogs', '/userdata/logs', bmcu_dir, '/tmp'))

    for directory in log_dirs:
        add('klipper', os.path.join(directory, 'klippy.log'))
        add('moonraker', os.path.join(directory, 'moonraker.log'))
        add('bmcu-panel', os.path.join(directory, 'bmcu-panel.log'))
        add('bmcu-planner', os.path.join(directory, 'bmcu-planner.log'))

    for directory in ('/oem/klippylogs', bmcu_dir):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if re.fullmatch(r'bmcu-transport-[A-Za-z0-9_.-]+\.log', name):
                add(name[:-4], os.path.join(directory, name))
    add('bmcu-update', os.path.join(bmcu_dir, 'update', 'last-update.jsonl'))
    return result


def moonraker_gcode_roots(moonraker, printer_data):
    roots = []
    registered = moonraker_root(moonraker, 'gcodes')
    if registered:
        roots.append(registered)
    if printer_data:
        fallback = os.path.realpath(os.path.join(printer_data, 'gcodes'))
        if os.path.isdir(fallback) and fallback not in roots:
            roots.append(fallback)
    return roots


def moonraker_last_filename(moonraker):
    payload = request_json(moonraker, '/printer/objects/query?print_stats&virtual_sdcard')
    try:
        status = payload['result']['status']
    except (TypeError, KeyError):
        return ''
    for obj in ('print_stats', 'virtual_sdcard'):
        value = status.get(obj, {}) if isinstance(status, dict) else {}
        filename = str(value.get('filename') or '').strip() if isinstance(value, dict) else ''
        if filename:
            return filename
    return ''


def resolve_last_gcode(moonraker, printer_data):
    roots = moonraker_gcode_roots(moonraker, printer_data)
    filename = moonraker_last_filename(moonraker)
    if filename:
        relative = filename.lstrip('/\\')
        for root in roots:
            candidate = os.path.realpath(os.path.join(root, relative))
            if inside(candidate, root) and regular_file(candidate) and candidate.lower().endswith('.gcode'):
                return candidate, 'moonraker-print-stats'

    payload = request_json(moonraker, '/server/files/list?root=gcodes')
    files = payload.get('result', []) if isinstance(payload, dict) else []
    if isinstance(files, list):
        candidates = []
        for item in files:
            if not isinstance(item, dict):
                continue
            relative = str(item.get('path') or '').lstrip('/\\')
            if not relative.lower().endswith('.gcode'):
                continue
            try:
                modified = float(item.get('modified') or 0)
            except (TypeError, ValueError):
                modified = 0
            candidates.append((modified, relative))
        for _modified, relative in sorted(candidates, reverse=True):
            for root in roots:
                candidate = os.path.realpath(os.path.join(root, relative))
                if inside(candidate, root) and regular_file(candidate):
                    return candidate, 'moonraker-latest-gcode'

    latest = None
    for root in roots:
        for current, directories, names in os.walk(root):
            directories[:] = [name for name in directories if not name.startswith('.')]
            for name in names:
                if not name.lower().endswith('.gcode'):
                    continue
                path = os.path.realpath(os.path.join(current, name))
                if not inside(path, root) or not regular_file(path):
                    continue
                try:
                    candidate = (os.path.getmtime(path), path)
                except OSError:
                    continue
                if latest is None or candidate[0] > latest[0]:
                    latest = candidate
    return (latest[1], 'filesystem-latest-gcode') if latest else ('', '')


def sanitized_panel_cfg(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as stream:
            text = stream.read(1024 * 1024)
    except OSError:
        return None
    return re.sub(r'(?mi)^(\s*access_token\s*:\s*).*$' , r'\1[REDACTED]', text).encode('utf-8')


def collect(args):
    bmcu_dir = os.path.realpath(os.path.expanduser(args.bmcu_dir))
    metadata, config_dir, printer_data, configured_moonraker = installation_context(bmcu_dir)
    moonraker = args.moonraker or configured_moonraker
    output = os.path.realpath(os.path.expanduser(args.output))
    os.makedirs(os.path.dirname(output), exist_ok=True)

    explicit = args.selection_explicit or any((
        args.include_bmcu_logs, args.include_klipper_log, args.include_moonraker_log,
        args.include_config, args.include_runtime, args.include_printer_status,
        args.include_last_gcode,
    ))
    selected = {
        'bmcu_logs': args.include_bmcu_logs,
        'klipper_log': args.include_klipper_log,
        'moonraker_log': args.include_moonraker_log,
        'config': args.include_config,
        'runtime': args.include_runtime,
        'printer_status': args.include_printer_status,
        'last_gcode': args.include_last_gcode,
    }
    if not explicit:
        selected.update({key: True for key in selected if key != 'last_gcode'})

    manifest = {
        'schema': 1,
        'created_at': int(time.time()),
        'platform': str(metadata.get('platform') or 'unknown'),
        'package_version': str(metadata.get('version') or 'unknown'),
        'hostname': socket.gethostname(),
        'python': sys.version.split()[0],
        'system': platform.platform(),
        'selected': selected,
        'last_gcode': None,
        'logs': [],
    }

    with tarfile.open(output, 'w:gz', compresslevel=6) as archive:
        for label, path in log_candidates(bmcu_dir, printer_data, moonraker):
            if label == 'klipper' and not selected['klipper_log']:
                continue
            if label == 'moonraker' and not selected['moonraker_log']:
                continue
            if label not in ('klipper', 'moonraker') and not selected['bmcu_logs']:
                continue
            add_log(archive, manifest, label, path)

        if selected['config']:
            installation_path = os.path.join(bmcu_dir, 'runtime', 'INSTALLATION.json')
            if regular_file(installation_path):
                archive.add(installation_path, arcname='bmcu/INSTALLATION.json', recursive=False)

            for name in ('bmcu.cfg', 'bmcu_macros.cfg'):
                path = os.path.join(bmcu_dir, name)
                if regular_file(path):
                    archive.add(path, arcname='bmcu/' + name, recursive=False)

            panel_cfg = sanitized_panel_cfg(os.path.join(bmcu_dir, 'bmcu_panel.cfg'))
            if panel_cfg is not None:
                add_bytes(archive, 'bmcu/bmcu_panel.cfg', panel_cfg)

        if selected['runtime']:
            for directory in ('/tmp/bmcu-transport',):
                try:
                    names = sorted(os.listdir(directory))
                except OSError:
                    continue
                for name in names:
                    if not name.endswith('.status.json'):
                        continue
                    path = os.path.join(directory, name)
                    if regular_file(path):
                        archive.add(path, arcname='runtime/' + safe_name(name), recursive=False)

        if selected['printer_status']:
            server_info = request_json(moonraker, '/server/info')
            if server_info is not None:
                add_json(archive, 'moonraker/server-info.json', server_info)
            objects = request_json(moonraker, '/printer/objects/query?toolhead&print_stats&virtual_sdcard&bmcu')
            if objects is not None:
                add_json(archive, 'moonraker/printer-status.json', objects)

        if selected['last_gcode']:
            gcode, source = resolve_last_gcode(moonraker, printer_data)
            if gcode:
                try:
                    size = os.path.getsize(gcode)
                    mtime = os.path.getmtime(gcode)
                    arcname = 'gcode/' + safe_name(os.path.basename(gcode))
                    archive.add(gcode, arcname=arcname, recursive=False)
                    manifest['last_gcode'] = {
                        'found': True,
                        'source': source,
                        'filename': os.path.basename(gcode),
                        'archive': arcname,
                        'size': int(size),
                        'modified_at': float(mtime),
                    }
                except OSError as exc:
                    manifest['last_gcode'] = {'found': False, 'reason': str(exc)}
            else:
                manifest['last_gcode'] = {'found': False, 'reason': 'no .gcode file found'}

        add_json(archive, 'manifest.json', manifest)

    os.chmod(output, 0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bmcu-dir', required=True)
    parser.add_argument('--moonraker', default='')
    parser.add_argument('--output', required=True)
    parser.add_argument('--selection-explicit', action='store_true')
    parser.add_argument('--include-bmcu-logs', action='store_true')
    parser.add_argument('--include-klipper-log', action='store_true')
    parser.add_argument('--include-moonraker-log', action='store_true')
    parser.add_argument('--include-config', action='store_true')
    parser.add_argument('--include-runtime', action='store_true')
    parser.add_argument('--include-printer-status', action='store_true')
    parser.add_argument('--include-last-gcode', action='store_true')
    args = parser.parse_args()
    collect(args)


if __name__ == '__main__':
    main()
