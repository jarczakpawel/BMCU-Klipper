#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import print_function

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import sys
import tempfile
import time
import types
import urllib.request

sys.dont_write_bytecode = True

PRODUCT = 'BMCU-Klipper'
PRODUCT_VERSION = None
MAX_RELEASE_BYTES = 64 * 1024 * 1024
RUNTIME_SCRIPTS = (
    'apply_detected_devices.py', 'bmcu_cli.py', 'bmcu_doctor.py',
    'bmcu_host_bootstrap.py', 'bmcu_transportd.py', 'bmcu_plannerd.py',
    'bmcu_planner_process.py', 'bmcu_transport_process.py',
    'bmcu_isp.py', 'bmcu_runtime.py', 'bmcu_collect_logs.py',
    'bmcu_update.py', 'bmcu_vendor.py', 'detect_bmcu.py',
    'bmcu_platform.py', 'safe_file_ops.py',
    'safe_printer_cfg_include.py', 'moonraker_gcode.py',
)
UNINSTALL_NOTE = (b'BMCU-Klipper is managed by its installer.\n'
                  b'To remove it, run from an extracted BMCU-Klipper package:\n'
                  b'    sh ./uninstall\n')
PANEL_TOKEN_RE = re.compile(r'^[0-9a-f]{64}$')

lifecycle = None
transport_process = None
out = print
warn = print


class InstallError(RuntimeError):
    pass


def _read_release_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallError('cannot open package file %s: %s' % (path, exc))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError('package entry is not a regular file: %s' % path)
        if info.st_size > limit:
            raise InstallError('package file is too large: %s' % path)
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data) > limit:
            raise InstallError('package file is too large: %s' % path)
        return data
    finally:
        os.close(descriptor)


def load_release_snapshot(package):
    raw_package = os.path.abspath(package)
    if os.path.islink(raw_package):
        raise InstallError('package root must not be a symlink: %s' % raw_package)
    package = os.path.realpath(raw_package)
    if not os.path.isdir(package):
        raise InstallError('package root is not a real directory: %s' % package)
    snapshot = {}
    total = 0
    for current, directories, files in os.walk(package, topdown=True, followlinks=False):
        directories[:] = [name for name in directories
                          if name not in ('.git', '__pycache__', '.pio')]
        for directory in list(directories):
            info = os.lstat(os.path.join(current, directory))
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise InstallError('package contains an unsafe directory entry: %s' %
                                   os.path.join(current, directory))
        for filename in files:
            if filename.endswith('.pyc'):
                continue
            path = os.path.join(current, filename)
            relative = os.path.relpath(path, package).replace(os.sep, '/')
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise InstallError('package contains an unsafe file entry: %s' % relative)
            data = _read_release_regular(path, MAX_RELEASE_BYTES)
            total += len(data)
            if total > MAX_RELEASE_BYTES:
                raise InstallError('package is unexpectedly large')
            snapshot[relative] = data
    if not snapshot:
        raise InstallError('package is empty')
    return snapshot


def load_package_module(name, snapshot, package):
    relative = 'scripts/%s.py' % name
    data = snapshot.get(relative)
    if data is None:
        raise InstallError('package module is missing: %s' % relative)
    filename = os.path.join(package, 'scripts', name + '.py')
    module = types.ModuleType(name)
    module.__file__ = filename
    module.__package__ = ''
    module.__loader__ = None
    sys.modules[name] = module
    try:
        exec(compile(data, filename, 'exec'), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def snapshot_bytes(snapshot, relative):
    try:
        return snapshot[relative.replace(os.sep, '/')]
    except KeyError:
        raise InstallError('package file is missing: %s' % relative)


def release_versions_from_snapshot(snapshot):
    data = snapshot_bytes(snapshot, 'version')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('version file is not valid UTF-8')
    result = {}
    pattern = r'(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})'
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
        elif ':' in line:
            key, value = line.split(':', 1)
        else:
            continue
        key, value = key.strip().lower(), value.strip().lower()
        if key not in ('package', 'firmware'):
            continue
        if not re.fullmatch(pattern, value):
            raise InstallError('invalid %s version' % key)
        result[key] = value
    if 'package' not in result or 'firmware' not in result:
        raise InstallError('version file is incomplete')
    return result


def write_snapshot_file(snapshot, relative, destination, mode):
    data = snapshot_bytes(snapshot, relative)
    parent = os.path.dirname(destination)
    os.makedirs(parent, mode=0o750, exist_ok=True)
    with open(destination, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(destination, mode)


def populate_runtime(snapshot, runtime, installation):
    os.mkdir(runtime, 0o750)
    prefixes = ('klippy/', 'web/', 'vendor/')
    for relative in sorted(item for item in snapshot if item.startswith(prefixes)):
        destination = os.path.join(runtime, *relative.split('/'))
        mode = 0o750 if relative.endswith(('.py', '.sh')) else 0o640
        write_snapshot_file(snapshot, relative, destination, mode)
    for name in RUNTIME_SCRIPTS:
        write_snapshot_file(snapshot, 'scripts/' + name,
                            os.path.join(runtime, 'scripts', name), 0o750)
    write_snapshot_file(snapshot, 'version', os.path.join(runtime, 'version'), 0o640)
    digest = hashlib.sha256()
    for relative in sorted(snapshot):
        if relative == 'package.sha256':
            continue
        digest.update(relative.encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(snapshot[relative]).digest())
    lifecycle.atomic_write(os.path.join(runtime, 'package.sha256'),
                           (digest.hexdigest() + '\n').encode('ascii'), 0o640)
    lifecycle.atomic_write(os.path.join(runtime, '.managed-by-bmcu'),
                           (PRODUCT + '\n').encode('utf-8'), 0o640)
    lifecycle.atomic_write(os.path.join(runtime, 'INSTALLATION.json'),
                           (json.dumps(installation, indent=2, sort_keys=True) +
                            '\n').encode('utf-8'), 0o640)


def panel_token_from_bytes(data):
    text = data.decode('utf-8', 'replace')
    values = re.findall(r'(?m)^access_token:\s*([^\s#]+)\s*$', text)
    if len(values) != 1 or not PANEL_TOKEN_RE.match(values[0].lower()):
        return ''
    return values[0].lower()


def ensure_panel_token(data, token=''):
    text = data.decode('utf-8', 'replace')
    token = str(token or '').lower()
    if not PANEL_TOKEN_RE.match(token):
        token = os.urandom(32).hex()
    line = 'access_token: %s' % token
    if re.search(r'(?m)^access_token:\s*.*$', text):
        text = re.sub(r'(?m)^access_token:\s*.*$', line, text, count=1)
    else:
        text = text.rstrip('\r\n') + '\n' + line + '\n'
    return text.encode('utf-8'), token


def set_panel_option(data, option, value):
    text = data.decode('utf-8', 'replace')
    line = '%s: %s' % (option, value)
    if re.search(r'(?m)^%s:\s*.*$' % re.escape(option), text):
        text = re.sub(r'(?m)^%s:\s*.*$' % re.escape(option), line, text, count=1)
    else:
        text = re.sub(r'(?m)^\[bmcu_panel\]\s*$', '[bmcu_panel]\n' + line, text, count=1)
    return text.encode('utf-8')


def panel_option(data, option, default=''):
    match = re.search(r'(?m)^%s:\s*([^#;\r\n]*)' % re.escape(option),
                      data.decode('utf-8', 'replace'))
    return match.group(1).strip() if match else default


def migrate_lightweight_bmcu_cfg(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('bmcu.cfg is not UTF-8')
    match = re.search(r'(?ms)^\[bmcu\]\s*$.*?(?=^\[|\Z)', text)
    if match is None:
        raise InstallError('bmcu.cfg has no [bmcu] section')
    block = match.group(0)

    migrations = (
        ('manager_tick_interval', ('0.25',), '0.50'),
        ('manager_idle_interval', ('1.0',), '2.0'),
        ('rx_budget_bytes', ('4096', '1024'), '256'),
        ('rx_budget_packets', ('8',), '2'),
        ('rx_budget_ms', ('2.0',), '0.5'),
        ('status_cache_interval', ('0.25',), '1.0'),
        ('status_cache_idle_interval', ('2.0',), '5.0'),
        ('reactor_yield_interval', ('0.002',), '0.005'),
        ('manager_work_yield_interval', ('0.005',), '0.010'),
        ('critical_motion_release_delay', ('0.100',), '0.500'),
        ('required_runtime_sync_timeout', ('8.0', '15.0', '20.0'), '30.0'),
        ('sidecar_status_interval', ('0.50',), '1.00'),
        ('transport_retry_interval', ('0.50',), '5.00'),
    )
    for name, old_values, new_value in migrations:
        old_pattern = '|'.join(re.escape(value) for value in old_values)
        block = re.sub(
            r'(?m)^(\s*%s\s*:\s*)(?:%s)(\s*(?:#.*)?)$' %
            (re.escape(name), old_pattern),
            lambda match, value=new_value:
                match.group(1) + value + match.group(2),
            block, count=1)

    pressure_lines = list(re.finditer(
        r'(?m)^\s*load_pressure_pct\s*:\s*([^#\r\n]*)(?:#.*)?$', block))
    if len(pressure_lines) > 1:
        raise InstallError('bmcu.cfg contains duplicate load_pressure_pct options')
    legacy_lines = list(re.finditer(
        r'(?m)^(\s*)load_profile\s*:\s*([^#\r\n]*)(\s*(?:#.*)?)$', block))
    if len(legacy_lines) > 1:
        raise InstallError('bmcu.cfg contains duplicate load_profile options')
    if pressure_lines:
        block = re.sub(
            r'(?m)^\s*load_profile\s*:.*(?:\r?\n|$)', '', block)
    elif legacy_lines:
        match_profile = legacy_lines[0]
        raw_profile = match_profile.group(2).strip()
        legacy_map = {'0': '82', '1': '95', '2': '75'}
        if raw_profile not in legacy_map:
            raise InstallError(
                'bmcu.cfg has unsupported legacy load_profile value: %s' %
                (raw_profile or '<blank>'))
        replacement = (match_profile.group(1) + 'load_pressure_pct: ' +
                       legacy_map[raw_profile] + match_profile.group(3))
        block = (block[:match_profile.start()] + replacement +
                 block[match_profile.end():])
    else:
        line = 'load_pressure_pct: 82\n'
        anchor = re.search(r'(?m)^\s*pull_speed_end_mms\s*:.*$', block)
        if anchor is not None:
            block = block[:anchor.end()] + '\n' + line + block[anchor.end():]
        else:
            header_end = block.find('\n') + 1
            block = block[:header_end] + line + block[header_end:]

    for name in ('load_speed_mms', 'pull_speed_mms'):
        block = re.sub(
            r'(?m)^(\s*%s\s*:\s*)60(?:\.0)?(\s*(?:#.*)?)$' %
            re.escape(name),
            lambda match: match.group(1) + '80' + match.group(2),
            block, count=1)

    block = re.sub(
        r'(?m)^(\s*transport_sidecar\s*:\s*).*(\s*(?:#.*)?)$',
        lambda match: match.group(1) + 'True' + match.group(2),
        block, count=1)

    block = re.sub(
        r'(?mi)^\s*#\s*bmcu-debug-policy\s*:\s*production-v1\s*(?:\r?\n|$)',
        '', block)
    desired = (
        ('debug', 'False'),
        ('transport_sidecar', 'True'),
        ('transport_socket_dir', '/tmp/bmcu-transport'),
        ('sidecar_status_interval', '1.00'),
        ('manager_idle_interval', '2.0'),
        ('rx_budget_packets', '2'),
        ('rx_budget_ms', '0.5'),
        ('callback_warning_ms', '10.0'),
        ('status_cache_interval', '1.0'),
        ('status_cache_idle_interval', '5.0'),
        ('reactor_yield_interval', '0.005'),
        ('manager_work_yield_interval', '0.010'),
        ('critical_motion_release_delay', '0.500'),
        ('transport_min_buffer', '1.50'),
        ('transport_retry_interval', '5.00'),
        ('required_runtime_sync_timeout', '30.0'),
    )
    missing = [('%s: %s' % item) for item in desired
               if not re.search(r'(?m)^\s*%s\s*:' % re.escape(item[0]), block)]
    if missing:
        anchor = re.search(r'(?m)^\s*manager_tick_interval\s*:.*$', block)
        insertion = '\n'.join(missing) + '\n'
        if anchor is not None:
            position = anchor.end()
            block = block[:position] + '\n' + insertion + block[position:]
        else:
            header_end = block.find('\n') + 1
            block = block[:header_end] + insertion + block[header_end:]
    return (text[:match.start()] + block + text[match.end():]).encode('utf-8')


def port_available(port):
    for family, address in ((socket.AF_INET, ('0.0.0.0', int(port))),
                            (getattr(socket, 'AF_INET6', None), ('::', int(port)))):
        if family is None:
            continue
        listener = None
        try:
            listener = socket.socket(family, socket.SOCK_STREAM)

            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
        except OSError as exc:
            if family != socket.AF_INET and exc.errno in (97, 93, 99):
                continue
            return False
        finally:
            if listener is not None:
                listener.close()
    return True


def choose_panel_port(requested):
    if port_available(requested):
        return requested
    for candidate in range(requested + 1, min(requested + 40, 65535)):
        if port_available(candidate):
            out('Panel port %d is used by another program; using port %d.' %
                (requested, candidate))
            return candidate
    warn('no free panel port found near %d; the panel may not start' % requested)
    return requested


def build_config_payloads(snapshot, bmcu_dir, moonraker, panel_port, panel_enabled,
                          port_explicit):
    payloads = {}
    package_cfg = snapshot_bytes(snapshot, 'config/bmcu.cfg')
    current_cfg = lifecycle.read_optional(os.path.join(bmcu_dir, 'bmcu.cfg'))
    if current_cfg is None:
        payloads['bmcu.cfg'] = package_cfg
    else:
        try:
            payloads['bmcu.cfg'] = migrate_lightweight_bmcu_cfg(current_cfg)
        except InstallError as exc:
            if re.search(br'(?m)^\[bmcu\]', current_cfg):
                warn('bmcu.cfg was kept unchanged (%s)' % exc)
                payloads['bmcu.cfg'] = current_cfg
            else:
                warn('bmcu.cfg had no [bmcu] section and was replaced by the '
                     'default (a copy is in the backup)')
                payloads['bmcu.cfg'] = package_cfg
    payloads['bmcu_macros.cfg'] = snapshot_bytes(snapshot, 'config/bmcu_macros.cfg')
    current_panel = lifecycle.read_optional(os.path.join(bmcu_dir, 'bmcu_panel.cfg'))
    if current_panel is None or b'[bmcu_panel]' not in current_panel:
        panel = snapshot_bytes(snapshot, 'config/bmcu_panel.cfg')
        panel = set_panel_option(panel, 'enabled', 'True' if panel_enabled else 'False')
        panel = set_panel_option(panel, 'port', str(panel_port))
        panel = set_panel_option(panel, 'moonraker_url', moonraker)
        panel, token = ensure_panel_token(panel)
    else:
        panel, token = ensure_panel_token(current_panel,
                                          panel_token_from_bytes(current_panel))
        if port_explicit:
            panel = set_panel_option(panel, 'port', str(panel_port))
        if not panel_enabled:
            panel = set_panel_option(panel, 'enabled', 'False')
    payloads['bmcu_panel.cfg'] = panel
    payloads['UNINSTALL.txt'] = UNINSTALL_NOTE
    return payloads, token


def discovery_arguments(args):
    values = ['discover', '--format', 'json']
    if not sys.stdin.isatty():
        values.append('--non-interactive')
    for option, value in (
            ('--klipper-dir', args.klipper_dir), ('--config-dir', args.config_dir),
            ('--printer-cfg', args.printer_cfg), ('--python', args.python),
            ('--user', args.user), ('--moonraker-url', args.moonraker_url),
            ('--moonraker-conf', args.moonraker_conf),
            ('--service-backend', args.service_backend),
            ('--service-name', args.service_name)):
        if value:
            values.extend([option, str(value)])
    return values


def discover_plan(platform, args):
    values = discovery_arguments(args)
    try:
        return platform.build_plan(platform.build_parser().parse_args(values)).as_dict()
    except RuntimeError as exc:
        error = str(exc)
    if args.klipper_dir or args.config_dir or args.printer_cfg:
        raise InstallError(error)

    installs = lifecycle.find_installations()
    if len(installs) == 1:
        config_dir = installs[0][0]
        metadata = lifecycle.read_installation_metadata(
            os.path.join(config_dir, 'bmcu')) or {}
        klipper_dir = str(metadata.get('klipper_dir') or '')
        if klipper_dir and os.path.isfile(os.path.join(klipper_dir, 'klippy', 'klippy.py')):
            retry = values + ['--klipper-dir', klipper_dir, '--config-dir', config_dir]
            try:
                return platform.build_plan(
                    platform.build_parser().parse_args(retry)).as_dict()
            except RuntimeError as exc:
                error += '\n' + str(exc)
    raise InstallError(error)


def validate_target_python(target_python, user):
    code, output = lifecycle.run_as_user(
        [os.path.realpath(target_python), '-c',
         'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 42)'],
        user, timeout=60)
    if code == 42:
        raise InstallError('Klipper Python 3.7 or newer is required: %s' % target_python)
    if code != 0:
        raise InstallError('Klipper Python cannot run as %s: %s (%s)' %
                           (user, target_python, output.strip()[-300:]))


def acquire_lock(path):
    import fcntl
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise InstallError('another BMCU install or uninstall is already running')
    return descriptor


def inspect_existing(bmcu_dir):
    info = {'present': False, 'metadata': None, 'version': '', 'recognised': False}
    if not os.path.lexists(bmcu_dir):
        return info
    if os.path.islink(bmcu_dir) or not os.path.isdir(bmcu_dir):
        raise InstallError(
            '%s exists but is not a directory. Move it away and run the '
            'installer again.' % bmcu_dir)
    info['present'] = True
    metadata = lifecycle.read_installation_metadata(bmcu_dir)
    info['metadata'] = metadata
    if isinstance(metadata, dict):
        info['version'] = str(metadata.get('version') or '')
    markers = (
        os.path.join(bmcu_dir, 'runtime', '.managed-by-bmcu'),
        os.path.join(bmcu_dir, 'UNINSTALL.txt'),
        os.path.join(bmcu_dir, 'bmcu_panel.cfg'),
    )
    cfg = lifecycle.read_optional(os.path.join(bmcu_dir, 'bmcu.cfg'), 1024 * 1024)
    info['recognised'] = bool(
        metadata or any(os.path.exists(path) for path in markers) or
        (cfg is not None and re.search(br'(?m)^\[bmcu\]', cfg)) or
        not os.listdir(bmcu_dir))
    if not info['recognised']:
        raise InstallError(
            '%s exists but does not look like BMCU-Klipper. Move it away and '
            'run the installer again.' % bmcu_dir)
    return info


def arm_update_barrier(moonraker, is_u1):
    deadline = time.monotonic() + 900.0
    cleaned = cancelled_uninstall = waiting_announced = False
    while True:
        ok, message = lifecycle.send_gcode(moonraker, 'BMCU_PREPARE_UPDATE', timeout=300.0)
        if ok:
            out('BMCU motion is paused for the update.')
            return 'BMCU_PREPARE_UPDATE ACTION=CANCEL'
        lowered = message.lower()
        if 'unknown command' in lowered or 'not ready' in lowered or 'shutdown' in lowered:
            return None
        if 'uninstall is already prepared' in lowered and not cancelled_uninstall:
            cancelled_uninstall = True
            lifecycle.send_gcode(moonraker, 'BMCU_PREPARE_UNINSTALL ACTION=CANCEL', 30.0)
            continue
        if is_u1 and not cleaned and 'print transaction is retained' in lowered:
            cleaned = True
            done, _detail = lifecycle.send_gcode(
                moonraker, 'BMCU_PRINT_END UNLOAD=0 CLEAR=1', timeout=300.0)
            if done:
                out('Cleared a stale BMCU print transaction left by an earlier print.')
                continue
        if re.search(r'printer is (printing|paused|pause)\b', lowered):
            raise InstallError(
                'A print job is active. Finish or cancel it, then run the '
                'installer again. Nothing was changed.')
        busy = any(token in lowered for token in (
            'operation is active', 'is moving', 'still reports active',
            'in flight', 'activity changed', 'is suspended', 'background preparation',
            'motion controlled by bmcu', 'host is busy', 'refill handling is active'))
        if busy:
            if time.monotonic() < deadline:
                if not waiting_announced:
                    waiting_announced = True
                    out('Waiting for BMCU to finish its current operation...')
                time.sleep(5.0)
                continue
            raise InstallError('BMCU is still busy after 900 seconds. Nothing was changed.')
        out('Note: the running BMCU did not pause for the update (%s). Its state '
            'is kept as it is and reloaded after the update.' % message.strip()[:300])
        return None


def cancel_barrier(moonraker, command):
    if command:
        lifecycle.send_gcode(moonraker, command, timeout=30.0)


def configured_devices(bmcu_dir):
    data = lifecycle.read_optional(os.path.join(bmcu_dir, 'bmcu.cfg'), 1024 * 1024)
    names = []
    if not data:
        return names
    in_devices = False
    for raw in data.decode('utf-8', 'replace').splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(('#', ';')):
            continue
        if stripped.lower().startswith('devices:'):
            in_devices = True
            stripped = stripped.split(':', 1)[1].strip()
            if not stripped:
                continue
        elif in_devices and re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*[:=]', stripped):
            break
        elif stripped.startswith('['):
            in_devices = False
        if in_devices and ',' in stripped:
            name = stripped.split(',')[0].strip()
            if re.fullmatch(r'[A-Za-z0-9_.-]+', name):
                names.append(name)
    return names


def wait_panel(port, timeout):
    url = 'http://127.0.0.1:%d/bmcu-panel.html' % int(port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=10.0) as response:
                if response.getcode() == 200 and b'BMCU' in response.read(1024 * 1024):
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def verify_helpers(bmcu_dir, panel_enabled, panel_port, timeout=60.0):
    expected = set(configured_devices(bmcu_dir))
    deadline = time.monotonic() + timeout
    running = set()
    while expected and time.monotonic() < deadline:
        try:
            running = set(str(record.get('name') or '') for record in
                          transport_process.processes(bmcu_dir))
        except Exception:
            running = set()
        if expected <= running:
            break
        time.sleep(2.0)
    if expected:
        missing = sorted(expected - running)
        if missing:
            warn('BMCU transport helper not running yet for: %s (check the panel)' %
                 ', '.join(missing))
        else:
            out('BMCU transport helpers running for %d device(s).' % len(expected))
    if panel_enabled:
        if wait_panel(panel_port, max(10.0, deadline - time.monotonic() + 30.0)):
            return True
        warn('the BMCU panel is not answering on port %d yet' % panel_port)
    return False


def ensure_u1_heads(moonraker, config_dir, bmcu_dir):
    state = lifecycle.read_json(lifecycle.bmcu_state_file(config_dir, bmcu_dir))
    endpoints = state.get('endpoints') if isinstance(state, dict) else None
    if isinstance(endpoints, dict) and any(
            isinstance(value, dict) and value.get('driver') == 'snapmaker_u1'
            for value in endpoints.values()):
        return
    ok, message = lifecycle.send_gcode(moonraker, 'BMCU_SETUP AUTO=1 REPLACE=1', timeout=120.0)
    if ok:
        out('Snapmaker U1 heads are set up for BMCU routing.')
    else:
        warn('the Snapmaker U1 heads could not be set up automatically (%s); run '
             'BMCU_SETUP AUTO=1 REPLACE=1 in the Klipper console' % message.strip()[:300])


def panel_urls(port):
    urls = ['http://%s:%d/' % (socket.gethostname().strip() or 'printer-host', int(port))]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('192.0.2.1', 9))
        address = probe.getsockname()[0]
        if address and not address.startswith('127.'):
            urls.append('http://%s:%d/' % (address, int(port)))
    except OSError:
        pass
    finally:
        probe.close()
    return urls


class Installation(object):
    def __init__(self, plan, args, snapshot, existing):
        self.plan = plan
        self.args = args
        self.snapshot = snapshot
        self.existing = existing
        self.klipper_dir = plan['klipper_dir']
        self.config_dir = plan['config_dir']
        self.printer_cfg = plan['printer_cfg']
        self.user = plan['install_user']
        self.group = plan['install_group']
        self.target_python = os.path.realpath(plan['python'])
        self.moonraker = plan['moonraker_url']
        self.platform_id = plan['platform_id']
        self.is_u1 = self.platform_id == 'snapmaker_u1'
        self.service = plan.get('service') or {}
        self.controller = lifecycle.ServiceController(
            self.service, self.klipper_dir, self.printer_cfg)
        self.uid = pwd.getpwnam(self.user).pw_uid
        self.gid = grp.getgrnam(self.group).gr_gid
        self.bmcu_dir = os.path.join(self.config_dir, 'bmcu')
        self.runtime = os.path.join(self.bmcu_dir, 'runtime')
        self.extras = os.path.join(self.klipper_dir, 'klippy', 'extras')
        self.data_root = os.path.dirname(os.path.realpath(self.config_dir))
        self.metadata_path = os.path.join(self.runtime, 'INSTALLATION.json')
        self.bootstrap = os.path.join(self.runtime, 'scripts', 'bmcu_host_bootstrap.py')

        self.backup = None
        self.stage = ''
        self.created_bmcu_dir = False
        self.old_runtime = ''
        self.runtime_swapped = False
        self.links_before = None
        self.links_changed = False
        self.config_before = {}
        self.cfg_before = None
        self.cfg_changed = False
        self.dropin_path = ''
        self.dropin_before = None
        self.dropin_changed = False
        self.stale_dropins_before = []
        self.u1_files_before = None
        self.u1_runner_dir_existed = False
        self.klipper_stopped = False
        self.modified = False
        self.klipper_was_running = bool(self.controller.running_pids())

    def prepare(self):
        previous = self.existing.get('metadata') or {}
        panel_enabled = not self.args.no_panel
        port_explicit = self.args.panel_port is not None
        if port_explicit:
            panel_port = int(self.args.panel_port)
        else:
            current_panel = lifecycle.read_optional(
                os.path.join(self.bmcu_dir, 'bmcu_panel.cfg'))
            configured = panel_option(current_panel or b'', 'port', '')
            panel_port = int(configured) if configured.isdigit() else 8291
            if current_panel is None:
                panel_port = choose_panel_port(panel_port)
        self.payloads, panel_token = build_config_payloads(
            self.snapshot, self.bmcu_dir, self.moonraker, panel_port, panel_enabled,
            port_explicit)
        panel_data = self.payloads['bmcu_panel.cfg']
        self.panel_enabled = panel_option(panel_data, 'enabled', 'True').lower() not in (
            'false', '0', 'no', 'off')
        port_text = panel_option(panel_data, 'port', str(panel_port))
        self.panel_port = int(port_text) if port_text.isdigit() else panel_port

        if self.is_u1:
            boot_hook, boot_hook_type = '', 'snapmaker-inline-bootstrap'
        elif self.controller.backend == 'systemd':
            boot_hook = lifecycle.systemd_dropin_path(self.controller.unit)
            boot_hook_type = 'systemd-dropin'
        else:
            boot_hook, boot_hook_type = '', ''
        preexisting = previous.get('persistence_marker_preexisting')
        if preexisting is None and self.is_u1:
            preexisting = os.path.lexists(lifecycle.U1_PERSISTENCE_MARKER)
        self.installation = dict(
            product=PRODUCT, version=PRODUCT_VERSION, schema=1,
            klipper_dir=self.klipper_dir, config_dir=self.config_dir,
            printer_cfg=self.printer_cfg, user=self.user, group=self.group,
            python=self.target_python,
            service={
                'backend': self.controller.backend,
                'name': self.controller.name,
                'script': os.path.realpath(self.controller.script) if self.controller.script else '',
                'service_dir': (os.path.realpath(self.controller.service_dir)
                                if self.controller.service_dir else ''),
            },
            platform=self.platform_id, moonraker_url=self.moonraker,
            include_begin=lifecycle.INCLUDE_BEGIN, include_end=lifecycle.INCLUDE_END,
            includes=list(lifecycle.INCLUDES),
            panel_enabled=self.panel_enabled, panel_port=self.panel_port,
            module_strategy='persistent-runtime-links',
            boot_hook=boot_hook, boot_hook_type=boot_hook_type,
            u1_service_script=lifecycle.U1_KLIPPER_SERVICE if self.is_u1 else '',
            u1_service_hook_dir=lifecycle.U1_HOOK_DIR if self.is_u1 else '',
            u1_service_patch_begin=lifecycle.U1_SERVICE_BEGIN if self.is_u1 else '',
            persistence_marker=lifecycle.U1_PERSISTENCE_MARKER if self.is_u1 else '',
            persistence_marker_preexisting=preexisting if self.is_u1 else None,
            panel_token_sha256=hashlib.sha256(panel_token.encode('ascii')).hexdigest(),
            installed_at=time.strftime('%Y-%m-%d %H:%M:%S'),
            previous_version=self.existing.get('version') or '',
        )
        self.stage = tempfile.mkdtemp(prefix='.bmcu-stage-', dir=self.data_root)
        os.chmod(self.stage, 0o750)
        populate_runtime(self.snapshot, os.path.join(self.stage, 'runtime'),
                         self.installation)
        cfg = lifecycle.read_bytes(self.printer_cfg)
        self.cfg_before = cfg
        self.cfg_after = lifecycle.add_bmcu_include(cfg)

    def apply(self):
        self.backup = lifecycle.Backup(self.data_root, 'install')
        self.backup.save_file(self.printer_cfg, 'printer.cfg')
        out('Stopping Klipper (%s)...' % self.controller.describe())
        self.controller.stop()
        self.klipper_stopped = True
        lifecycle.stop_bmcu_helpers([self.bmcu_dir])
        for port in lifecycle.configured_serial_ports(self.bmcu_dir):
            holders = lifecycle.serial_holders(port)
            if holders:
                warn('%s is still opened by PID %s (not BMCU-Klipper)' %
                     (port, ', '.join(str(pid) for pid in holders)))

        self.modified = True
        self.links_before = lifecycle.read_module_links(self.extras)
        if not os.path.isdir(self.bmcu_dir):
            os.mkdir(self.bmcu_dir, 0o750)
            self.created_bmcu_dir = True
        if os.path.lexists(self.runtime):
            self.old_runtime = os.path.join(
                self.bmcu_dir, '.runtime-previous-%d' % os.getpid())
            os.rename(self.runtime, self.old_runtime)
        self._move_into_place(os.path.join(self.stage, 'runtime'), self.runtime)
        self.runtime_swapped = True

        for name, data in self.payloads.items():
            path = os.path.join(self.bmcu_dir, name)
            current = lifecycle.read_optional(path)
            self.config_before[name] = current
            if current == data:
                continue
            if current is not None:
                self.backup.save_file(path, 'bmcu-' + name)
            lifecycle.atomic_write(path, data, 0o640, self.uid, self.gid)
        legacy_macros = os.path.join(self.bmcu_dir, 'bmcu_panel_macros.cfg')
        legacy_data = lifecycle.read_optional(legacy_macros)
        if legacy_data is not None and not legacy_data.strip():
            os.unlink(legacy_macros)
        legacy_uninstaller = os.path.join(self.bmcu_dir, 'uninstall')
        legacy_data = lifecycle.read_optional(legacy_uninstaller)
        if legacy_data is not None and b'runtime/scripts/uninstall.py' in legacy_data:
            os.unlink(legacy_uninstaller)
        lifecycle.chown_tree(self.bmcu_dir, self.uid, self.gid)
        os.chmod(self.bmcu_dir, 0o750)

        source_dir = os.path.join(self.runtime, 'klippy', 'extras')
        self.links_changed = True
        lifecycle.install_module_links(self.extras, source_dir, self.uid, self.gid,
                                       self.backup)

        if self.is_u1:
            self.u1_runner_dir_existed = os.path.isdir(lifecycle.U1_RUNNER_DIR)
            self.u1_files_before = lifecycle.capture_files(lifecycle.U1_MANAGED_FILES)
            lifecycle.install_u1_integration(
                self.user, self.group, self.target_python, self.bootstrap,
                self.metadata_path, self.backup)
        elif self.controller.backend == 'systemd':
            self.dropin_path = lifecycle.systemd_dropin_path(self.controller.unit)
            self.dropin_before = lifecycle.read_optional(self.dropin_path)
            data = lifecycle.systemd_dropin_bytes(
                self.target_python, self.bootstrap, self.metadata_path)
            for stale in lifecycle.managed_dropins(self.bmcu_dir):
                if stale != self.dropin_path:
                    saved = self.backup.save_file(stale, 'stale-dropin.conf')
                    self.stale_dropins_before.append((stale, saved))
                    lifecycle.remove_path(stale)
            if self.dropin_before != data:
                parent = os.path.dirname(self.dropin_path)
                if not os.path.isdir(parent):
                    os.makedirs(parent, 0o755)
                self.dropin_changed = True
                lifecycle.atomic_write(self.dropin_path, data, 0o644, 0, 0)
            self.controller.daemon_reload()

        current = lifecycle.read_bytes(self.printer_cfg)
        if current != self.cfg_before:
            self.cfg_before = current
            self.cfg_after = lifecycle.add_bmcu_include(current)
        if self.cfg_after != current:
            self.cfg_changed = True
            lifecycle.atomic_write(self.printer_cfg, self.cfg_after)

        out('Starting Klipper...')
        if not self.controller.start():
            raise InstallError('Klipper did not start after the installation')
        self.klipper_stopped = False

    def _move_into_place(self, source, target):
        try:
            os.rename(source, target)
        except OSError:
            shutil.copytree(source, target, symlinks=True)
            shutil.rmtree(source, ignore_errors=True)

    def rollback(self):
        try:
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            previous = None
        try:
            return self._rollback()
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def _rollback(self):
        problems = []

        def attempt(label, function):
            try:
                function()
            except Exception as exc:
                problems.append('%s: %s' % (label, exc))

        out('Restoring the previous state...')
        if self.modified and not self.klipper_stopped:
            attempt('stop Klipper', self.controller.stop)
        if self.modified:
            attempt('stop BMCU helpers', lambda: lifecycle.stop_bmcu_helpers([self.bmcu_dir]))
        if self.cfg_changed:
            attempt('printer.cfg', lambda: lifecycle.atomic_write(
                self.printer_cfg, self.cfg_before))
        if self.links_changed and self.links_before is not None:
            attempt('Klipper module links', lambda: lifecycle.restore_module_links(
                self.extras, self.links_before, self.backup))
        if self.u1_files_before is not None:
            def restore_u1():
                lifecycle.restore_files(self.u1_files_before)
                if not self.u1_runner_dir_existed:
                    try:
                        os.rmdir(lifecycle.U1_RUNNER_DIR)
                    except OSError:
                        pass
                lifecycle.reload_udev()
            attempt('Snapmaker U1 boot integration', restore_u1)
        if self.dropin_changed or self.stale_dropins_before:
            def restore_dropin():
                if self.dropin_changed:
                    if self.dropin_before is None:
                        lifecycle.remove_path(self.dropin_path)
                    else:
                        lifecycle.atomic_write(
                            self.dropin_path, self.dropin_before, 0o644, 0, 0)
                for path, saved in self.stale_dropins_before:
                    if not saved or not os.path.isfile(saved):
                        raise InstallError('rollback backup is missing for %s' % path)
                    parent = os.path.dirname(path)
                    if not os.path.isdir(parent):
                        os.makedirs(parent, 0o755)
                    lifecycle.atomic_write(path, lifecycle.read_bytes(saved), 0o644, 0, 0)
                self.controller.daemon_reload()
            attempt('systemd drop-in', restore_dropin)
        if self.runtime_swapped or self.old_runtime:
            def restore_runtime():
                if self.created_bmcu_dir:
                    socket_dir = lifecycle.transport_socket_dir(self.bmcu_dir)
                    shutil.rmtree(self.bmcu_dir)
                    lifecycle.remove_socket_dir(socket_dir)
                    return
                if self.runtime_swapped and os.path.lexists(self.runtime):
                    shutil.rmtree(self.runtime)
                if self.old_runtime and os.path.lexists(self.old_runtime):
                    os.rename(self.old_runtime, self.runtime)
            attempt('BMCU runtime', restore_runtime)
        if not self.created_bmcu_dir:
            for name, data in self.config_before.items():
                path = os.path.join(self.bmcu_dir, name)
                if data is None:
                    attempt('remove %s' % name, lambda path=path: lifecycle.remove_path(path))
                else:
                    attempt('restore %s' % name, lambda path=path, data=data:
                            lifecycle.atomic_write(path, data))
        started = False
        if self.klipper_was_running:
            try:
                started = self.controller.start()
                if not started:
                    problems.append('start Klipper: service did not start')
            except Exception as exc:
                problems.append('start Klipper: %s' % exc)
        return started, problems

    def finish(self):
        for path in (self.old_runtime, self.stage):
            if path and os.path.lexists(path):
                shutil.rmtree(path, ignore_errors=True)
        for path in os.listdir(self.bmcu_dir):
            if path.startswith(('.runtime-before-repair-', '.runtime-failed-repair-',
                                '.runtime-previous-')):
                shutil.rmtree(os.path.join(self.bmcu_dir, path), ignore_errors=True)
        lifecycle.prune_backups(self.data_root, 'install', 3)


def explain_not_ready(snapshot):
    if snapshot.source == 'none':
        return 'Klipper did not answer (%s)' % (snapshot.error or 'not running')
    message = snapshot.message.strip() or snapshot.state
    return 'Klipper is %s: %s' % (snapshot.state, message.splitlines()[0][:300]
                                 if message else snapshot.state)


def failure_is_ours(snapshot):
    text = ('%s %s' % (snapshot.message, snapshot.error)).lower()
    return 'bmcu' in text or 'not a valid config section' in text


def parser():
    p = argparse.ArgumentParser(
        prog='install', description='Install, update or repair BMCU-Klipper.')
    p.add_argument('--klipper-dir', default='')
    p.add_argument('--config-dir', default='')
    p.add_argument('--printer-cfg', default='')
    p.add_argument('--python', default='')
    p.add_argument('--user', default='')
    p.add_argument('--moonraker-url', default='')
    p.add_argument('--moonraker-conf', default='')
    p.add_argument('--service-backend', default='')
    p.add_argument('--service-name', default='')
    p.add_argument('--panel-port', type=int, default=None)
    p.add_argument('--no-panel', action='store_true')
    p.add_argument('--force', action='store_true',
                   help='bypass transient activity, never an active print, firmware upgrade, or unverified running Klipper')
    p.add_argument('--assume-idle', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--no-detect', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--detect-port', action='append', default=[], help=argparse.SUPPRESS)
    p.add_argument('--probe-unused-ch340', action='store_true', help=argparse.SUPPRESS)
    return p


def main():
    global PRODUCT_VERSION, lifecycle, transport_process, out, warn
    if sys.version_info < (3, 7):
        raise InstallError('Python 3.7 or newer is required')
    args = parser().parse_args()
    if os.geteuid() != 0:
        raise InstallError('run the installer as root (sh ./install does this for you)')
    if args.panel_port is not None and not 1024 <= args.panel_port <= 65535:
        raise InstallError('panel port must be within 1024..65535')

    package = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    snapshot = load_release_snapshot(package)
    PRODUCT_VERSION = release_versions_from_snapshot(snapshot)['package']
    lifecycle = load_package_module('bmcu_lifecycle', snapshot, package)
    out, warn = lifecycle.out, lifecycle.warn
    lifecycle.shield_from_hangup()
    load_package_module('bmcu_planner_process', snapshot, package)
    transport_process = load_package_module('bmcu_transport_process', snapshot, package)
    platform = load_package_module('bmcu_platform', snapshot, package)
    lifecycle.trusted_root_path(sys.executable, require_executable=True)
    out('Package integrity: %d files verified' % (len(snapshot) - 1))

    plan = discover_plan(platform, args)
    service = plan.get('service') or {}
    controller = lifecycle.ServiceController(
        service, plan['klipper_dir'], plan['printer_cfg'])
    if not controller.controllable:
        raise InstallError(
            'the Klipper service could not be identified (%s). Run the installer '
            'with --service-backend <backend> --service-name <name>. Nothing '
            'was changed.' % (service.get('reason') or service.get('backend')))
    validate_target_python(plan['python'], plan['install_user'])

    out('')
    out('=== BMCU-Klipper %s installation ===' % PRODUCT_VERSION)
    out('Platform:       %s' % plan['platform_id'])
    out('Klipper:        %s' % plan['klipper_dir'])
    out('Configuration:  %s' % plan['printer_cfg'])
    out('Klipper user:   %s' % plan['install_user'])
    out('Service:        %s' % controller.describe())

    lock = acquire_lock(plan['config_dir'])
    try:
        return install(plan, args, snapshot)
    finally:
        os.close(lock)


def install(plan, args, snapshot):
    bmcu_dir = os.path.join(plan['config_dir'], 'bmcu')
    data_root = os.path.dirname(os.path.realpath(plan['config_dir']))
    selected_config = os.path.realpath(plan['config_dir'])
    selected_klipper = os.path.realpath(plan['klipper_dir'])
    for config_dir, _traces in lifecycle.find_installations((plan['config_dir'],)):
        if os.path.realpath(config_dir) == selected_config:
            continue
        metadata = lifecycle.read_installation_metadata(os.path.join(config_dir, 'bmcu'))
        if not isinstance(metadata, dict):
            continue
        other_klipper = str(metadata.get('klipper_dir') or '')
        if other_klipper and os.path.realpath(other_klipper) == selected_klipper:
            raise InstallError(
                'another BMCU-Klipper installation uses the same Klipper checkout: %s. '
                'Use separate Klipper checkouts or uninstall that BMCU-Klipper instance '
                'first. Nothing was changed.' % config_dir)
    linked = lifecycle.other_installation_links(
        os.path.join(plan['klipper_dir'], 'klippy', 'extras'), bmcu_dir)
    if linked:
        raise InstallError(
            'this Klipper checkout is already linked to another BMCU-Klipper '
            'installation: %s. Use separate Klipper checkouts or uninstall the '
            'other installation first. Nothing was changed.' % ', '.join(linked))
    existing = inspect_existing(bmcu_dir)
    if existing['present']:
        out('Existing:       BMCU-Klipper %s (settings are kept)' %
            (existing['version'] or 'unknown version'))
    elif os.path.lexists(os.path.join(plan['config_dir'], 'bmcu_state.json')):
        out('Existing:       BMCU settings from an earlier installation are kept')
    removed = lifecycle.remove_stale_staging(data_root)
    if removed:
        out('Removed %d leftover(s) of an interrupted installation.' % removed)

    state = lifecycle.wait_until_safe(
        plan['moonraker_url'], plan['klipper_dir'], plan['printer_cfg'],
        'sh ./install', force=args.force, is_u1=plan['platform_id'] == 'snapmaker_u1')
    out('Printer:        %s' % state.describe())

    job = Installation(plan, args, snapshot, existing)
    barrier = None
    try:
        job.prepare()
        state = lifecycle.wait_until_safe(
            plan['moonraker_url'], plan['klipper_dir'], plan['printer_cfg'],
            'sh ./install', force=args.force, is_u1=job.is_u1)
        if state.ready and existing['present']:
            barrier = arm_update_barrier(plan['moonraker_url'], job.is_u1)
    except BaseException:
        if job.stage:
            shutil.rmtree(job.stage, ignore_errors=True)
        raise

    try:
        job.apply()
    except BaseException as exc:
        if not job.klipper_stopped and barrier:
            cancel_barrier(plan['moonraker_url'], barrier)
        reason = str(exc) or exc.__class__.__name__
        started, problems = job.rollback()
        shutil.rmtree(job.stage, ignore_errors=True)
        raise InstallError(rollback_message(reason, started, problems, job))

    out('Waiting for Klipper to become ready...')
    ready_timeout = 300.0 if job.is_u1 else 180.0
    after = lifecycle.wait_ready(plan['moonraker_url'], plan['klipper_dir'],
                                 plan['printer_cfg'], ready_timeout, is_u1=job.is_u1)
    if not after.ready:
        if state.ready or failure_is_ours(after):
            reason = 'Klipper did not accept the new installation (%s)' % explain_not_ready(after)
            started, problems = job.rollback()
            shutil.rmtree(job.stage, ignore_errors=True)
            message = rollback_message(reason, started, problems, job)
            if started:
                check = lifecycle.wait_ready(plan['moonraker_url'], plan['klipper_dir'],
                                             plan['printer_cfg'], 120.0, is_u1=job.is_u1)
                if not check.ready:
                    message += ('\nKlipper does not become ready even without this '
                                'change (%s), so the cause is elsewhere. Fix that '
                                'first, then run the installer again.' %
                                explain_not_ready(check))
            raise InstallError(message)
        warn('Klipper was not ready before the installation and still is not '
             '(%s). BMCU-Klipper is installed; fix the Klipper problem shown in '
             'Mainsail/Fluidd.' % explain_not_ready(after))
    job.finish()
    if after.ready:
        if job.is_u1:
            ensure_u1_heads(plan['moonraker_url'], plan['config_dir'], bmcu_dir)
        verify_helpers(bmcu_dir, job.panel_enabled, job.panel_port)
    out('')
    if existing['present']:
        out('BMCU-Klipper %s is updated and running.' % PRODUCT_VERSION)
    else:
        out('BMCU-Klipper %s is installed and running.' % PRODUCT_VERSION)
    out('Configuration:  %s' % bmcu_dir)
    if job.panel_enabled:
        for url in panel_urls(job.panel_port):
            out('Panel:          %s' % url)
    out('BMCU hardware is optional at this point; connect or flash it in the panel.')
    if job.is_u1:
        out('After a Snapmaker firmware update, run this installer again.')
    return 0


def rollback_message(reason, started, problems, job):
    message = 'Installation failed: %s' % reason
    if not problems:
        message += '\nThe previous state was restored'
        if job.klipper_was_running:
            message += ' and Klipper was started again.' if started else '.'
        else:
            message += '; Klipper was left stopped because it was stopped before installation.'
    else:
        message += ('\nThe previous state could not be fully restored: %s' %
                    '; '.join(problems))
    if job.backup is not None:
        message += '\nBackup of the files that were changed: %s' % job.backup.path
    return message


def report_unexpected(exc):
    import traceback
    try:
        print('ERROR: unexpected failure: %s' % exc, file=sys.stderr)
        traceback.print_exc()
    except Exception:
        pass


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        try:
            print('\nInterrupted.', file=sys.stderr)
        except Exception:
            pass
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        if isinstance(exc, InstallError) or exc.__class__.__name__ == 'LifecycleError':
            try:
                print('ERROR: %s' % exc, file=sys.stderr)
            except Exception:
                pass
        else:
            report_unexpected(exc)
        raise SystemExit(1)
