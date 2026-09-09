#!/usr/bin/env python3

from __future__ import print_function

import argparse
import configparser
import fcntl
import glob
import hashlib
import json
import os
import pwd
import grp
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_EXTRAS = os.path.join(_ROOT, 'klippy', 'extras')
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)
from bmcu_core.release import PACKAGE_VERSION

PRODUCT = 'BMCU-Klipper'
VERSION = PACKAGE_VERSION
MODULES = ('bmcu.py', 'bmcu_core', 'bmcu_panel.py')
MAX_JSON = 1024 * 1024
MAX_CONFIG = 16 * 1024 * 1024
SAVE_CONFIG_PREFIX = '#*# <---------------------- SAVE_CONFIG'
INCLUDE_RE = re.compile(
    r'^\s*\[\s*include\s+([^\]]+)\]\s*(?:[#;].*)?$', re.IGNORECASE)

class BootstrapError(RuntimeError):
    pass

def inside(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except (AttributeError, ValueError):
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root).rstrip(os.sep) + os.sep
        return real_path == real_root[:-1] or real_path.startswith(real_root)

def read_metadata(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BootstrapError('unsafe installation metadata: %s' % path)
    if info.st_size > MAX_JSON:
        raise BootstrapError('installation metadata is too large: %s' % path)
    with open(path, 'r') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise BootstrapError('invalid installation metadata: %s' % path)
    if value.get('product') != PRODUCT or value.get('version') != VERSION:
        raise BootstrapError('installation metadata belongs to another product or version')
    return value

def acquire_lock(bmcu_dir):

    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_DIRECTORY', 0)
    descriptor = os.open(bmcu_dir, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise

def atomic_symlink(source, target):
    parent = os.path.dirname(target)
    fd, temporary = tempfile.mkstemp(prefix='.%s.' % os.path.basename(target), dir=parent)
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(source, temporary)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)

def validate_paths(metadata, metadata_path):
    bmcu_dir = os.path.dirname(os.path.dirname(os.path.realpath(metadata_path)))
    recorded_config = metadata.get('config_dir')
    if recorded_config:
        expected = os.path.join(os.path.realpath(recorded_config), 'bmcu')
        if os.path.realpath(bmcu_dir) != os.path.realpath(expected):
            raise BootstrapError('metadata location does not match recorded BMCU directory')
    klipper_dir = os.path.realpath(str(metadata.get('klipper_dir') or ''))
    if not klipper_dir or not os.path.isdir(klipper_dir):
        raise BootstrapError('recorded Klipper directory is unavailable')
    extras_dir = os.path.join(klipper_dir, 'klippy', 'extras')
    if not os.path.isdir(extras_dir):
        raise BootstrapError('Klipper extras directory is unavailable: %s' % extras_dir)
    source_dir = os.path.join(bmcu_dir, 'runtime', 'klippy', 'extras')
    for name in MODULES:
        source = os.path.join(source_dir, name)
        if name.endswith('.py'):
            if not os.path.isfile(source) or os.path.islink(source):
                raise BootstrapError('managed module source is unavailable: %s' % source)
        else:
            if not os.path.isdir(source) or os.path.islink(source):
                raise BootstrapError('managed module source is unavailable: %s' % source)
    return bmcu_dir, extras_dir, source_dir

def _read_xattrs(path):
    result = {}
    if not hasattr(os, 'listxattr'):
        return result
    try:
        for name in os.listxattr(path, follow_symlinks=False):
            try:
                result[name] = os.getxattr(path, name, follow_symlinks=False)
            except OSError:
                pass
    except OSError:
        pass
    return result

def _atomic_replace_regular(path, data, info, xattrs):
    parent = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix='.%s.' % os.path.basename(path), dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        try:
            os.chown(temporary, info.st_uid, info.st_gid)
        except PermissionError:
            pass
        if hasattr(os, 'setxattr'):
            for name, value in xattrs.items():
                try:
                    os.setxattr(temporary, name, value, follow_symlinks=False)
                except OSError:
                    pass
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _normal_line_after_save(line):
    value = line.strip()
    return bool(value) and not value.startswith('#*#')

def repair_printer_cfg(metadata, bmcu_dir):
    path = os.path.realpath(str(metadata.get('printer_cfg') or ''))
    config_dir = os.path.realpath(str(metadata.get('config_dir') or ''))
    if not path or not config_dir or not inside(path, config_dir):
        raise BootstrapError('recorded Klipper entry configuration is outside config_dir')
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BootstrapError('unsafe printer.cfg: %s' % path)
    if info.st_size > MAX_CONFIG:
        raise BootstrapError('printer.cfg is too large')
    with open(path, 'rb') as stream:
        original = stream.read(MAX_CONFIG + 1)
    if len(original) > MAX_CONFIG:
        raise BootstrapError('printer.cfg is too large')
    try:
        text = original.decode('utf-8')
    except UnicodeDecodeError:
        raise BootstrapError('printer.cfg is not UTF-8')

    begin = str(metadata.get('include_begin') or '# BEGIN BMCU-KLIPPER AUTO-INCLUDE')
    end = str(metadata.get('include_end') or '# END BMCU-KLIPPER AUTO-INCLUDE')
    includes = metadata.get('includes')
    if not isinstance(includes, list) or not includes or not all(isinstance(v, str) for v in includes):
        raise BootstrapError('installation metadata has no valid include list')
    known = set(value.strip() for value in includes)
    lines = text.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
            raise BootstrapError('managed BMCU include markers in printer.cfg are malformed')
        active = [line.strip() for line in lines[begins[0] + 1:ends[0]]
                  if line.strip() and not line.lstrip().startswith('#')]
        if active != includes:
            raise BootstrapError('managed BMCU include block was modified')
        outside = [line.strip() for i, line in enumerate(lines)
                   if line.strip() in known and not (begins[0] < i < ends[0])]
        if outside:
            raise BootstrapError('BMCU includes also exist outside the managed block')
        return 0

    manual = [line.strip() for line in lines if line.strip() in known]
    if manual:
        if len(manual) == len(includes) and set(manual) == known:
            return 0
        raise BootstrapError('printer.cfg contains an incomplete manual BMCU include set')

    save_index = -1
    for index, line in enumerate(lines):
        if line.strip().startswith(SAVE_CONFIG_PREFIX):
            save_index = index
            break
    if save_index >= 0:
        for line in lines[save_index + 1:]:
            if _normal_line_after_save(line):
                raise BootstrapError('printer.cfg has normal configuration after SAVE_CONFIG')
    insert_at = save_index if save_index >= 0 else len(lines)
    prefix = lines[:insert_at]
    while prefix and not prefix[-1].strip():
        prefix.pop()
    block = [
        '', '################################', '# BMCU-Klipper', begin,
        '################################',
    ] + list(includes) + [end, '']
    repaired_lines = prefix + block + lines[insert_at:]
    newline = '\r\n' if '\r\n' in text else '\n'
    repaired = (newline.join(repaired_lines).rstrip() + newline).encode('utf-8')
    _atomic_replace_regular(path, repaired, info, _read_xattrs(path))
    return 1

def _boot_id():
    try:
        with open('/proc/sys/kernel/random/boot_id', 'r') as stream:
            return stream.read(256).strip()
    except OSError:
        return ''

def _transport_config(path):
    values = {
        'enabled': True,
        'socket_dir': '/tmp/bmcu-transport',
        'baud': 115200,
        'heartbeat': 5.0,
        'connection_timeout': 15.0,
        'reconnect': 1.0,
        'status_interval': 1.00,
        'connect_settle': 1.5,
        'devices': [],
    }
    if not os.path.isfile(path) or os.path.islink(path):
        return values
    info = os.stat(path)
    if info.st_size > 1024 * 1024:
        raise BootstrapError('bmcu.cfg is too large')
    parser = configparser.RawConfigParser(
        interpolation=None, strict=True, delimiters=(':', '='),
        comment_prefixes=('#', ';'), inline_comment_prefixes=('#', ';'))
    with open(path, 'r') as stream:
        parser.read_file(stream)
    if not parser.has_section('bmcu'):
        raise BootstrapError('bmcu.cfg has no [bmcu] section')
    values['enabled'] = parser.getboolean(
        'bmcu', 'transport_sidecar', fallback=True)
    values['socket_dir'] = os.path.abspath(parser.get(
        'bmcu', 'transport_socket_dir',
        fallback='/tmp/bmcu-transport').strip())
    values['baud'] = parser.getint('bmcu', 'baud', fallback=115200)
    values['heartbeat'] = parser.getfloat(
        'bmcu', 'heartbeat_interval', fallback=5.0)
    values['connection_timeout'] = parser.getfloat(
        'bmcu', 'connection_timeout', fallback=15.0)
    values['reconnect'] = parser.getfloat(
        'bmcu', 'reconnect_interval', fallback=1.0)
    values['status_interval'] = parser.getfloat(
        'bmcu', 'sidecar_status_interval', fallback=1.00)
    values['connect_settle'] = parser.getfloat(
        'bmcu', 'connect_settle_time', fallback=1.5)
    raw = parser.get('bmcu', 'devices', fallback='')
    names = set()
    ports = {}
    uids = set()
    devices = []
    for line_number, line in enumerate(raw.replace(';', '\n').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [item.strip() for item in line.split(',')]
        if len(parts) not in (2, 3):
            raise BootstrapError(
                'invalid BMCU device line %d for transport sidecar' %
                line_number)
        name, port = parts[0], parts[1]
        uid = parts[2].upper() if len(parts) == 3 and parts[2] != '-' else ''
        if re.fullmatch(r'[A-Za-z0-9_.-]+', name) is None:
            raise BootstrapError('invalid BMCU sidecar device name %s' % name)
        if not os.path.isabs(port) or any(ord(char) < 32 for char in port):
            raise BootstrapError('invalid BMCU sidecar serial path for %s' % name)
        if uid and re.fullmatch(r'[0-9A-F]{24}', uid) is None:
            raise BootstrapError('invalid BMCU sidecar UID for %s' % name)
        if name in names:
            raise BootstrapError('duplicate BMCU sidecar device %s' % name)
        if uid and uid in uids:
            raise BootstrapError('duplicate BMCU sidecar UID %s' % uid)
        previous = ports.get(port)
        if previous is not None and (not uid or not previous[1]):
            raise BootstrapError(
                'duplicate BMCU serial hint %s requires hardware UIDs' % port)
        names.add(name)
        ports[port] = (name, uid)
        if uid:
            uids.add(uid)
        devices.append({'name': name, 'port': port, 'uid': uid})
    values['devices'] = devices
    if (not values['socket_dir'].startswith('/') or
            len(values['socket_dir'].encode('utf-8')) > 80 or
            any(ord(char) < 32 for char in values['socket_dir'])):
        raise BootstrapError('invalid BMCU transport_socket_dir')
    if not 9600 <= values['baud'] <= 2000000:
        raise BootstrapError('invalid BMCU sidecar baud')
    if not 1.0 <= values['heartbeat'] <= 10.0:
        raise BootstrapError('invalid BMCU sidecar heartbeat')
    if not 8.0 <= values['connection_timeout'] <= 120.0:
        raise BootstrapError('invalid BMCU sidecar connection timeout')
    if values['heartbeat'] >= values['connection_timeout']:
        raise BootstrapError('BMCU heartbeat must be below connection timeout')
    if not 0.1 <= values['reconnect'] <= 60.0:
        raise BootstrapError('invalid BMCU sidecar reconnect interval')
    if not 0.05 <= values['status_interval'] <= 10.0:
        raise BootstrapError('invalid BMCU sidecar status interval')
    if not 0.1 <= values['connect_settle'] <= 10.0:
        raise BootstrapError('invalid BMCU sidecar connect settle time')
    return values

def _read_transport_records(path):
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return {}
        with open(path, 'r') as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            return {}
        records = value.get('devices', {})
        return records if isinstance(records, dict) else {}
    except Exception:
        return {}

def _recorded_transport_alive(record, runtime):
    if not isinstance(record, dict):
        return False
    try:
        pid = int(record.get('pid', 0) or 0)
    except (TypeError, ValueError):
        return False
    daemon = os.path.realpath(str(record.get('daemon', '') or ''))
    if (pid <= 1 or os.path.basename(daemon) != 'bmcu_transportd.py' or
            not inside(daemon, runtime)):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    command = _process_cmdline(pid)
    return bool(command and 'bmcu_transportd.py' in command and daemon in command)

def _stop_transport_record(record, runtime):
    if not _recorded_transport_alive(record, runtime):
        return False
    pid = int(record['pid'])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True

def _atomic_transport_records(path, records):
    fd, temporary = tempfile.mkstemp(
        prefix='.transport-processes.', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'devices': records}, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _transport_preexec(user, uid, gid):
    def prepare():
        try:
            os.nice(19)
        except OSError:
            pass
        if os.geteuid() == 0:
            os.initgroups(user, gid)
            os.setgid(gid)
            os.setuid(uid)
    return prepare

def _wait_transport_started(process, socket_path, timeout=2.0):
    deadline = time.monotonic() + max(0.2, float(timeout))
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise BootstrapError(
                'BMCU transport sidecar exited during startup with code %d' %
                code)
        try:
            stream_info = os.stat(socket_path)
            control_info = os.stat(socket_path + '.ctl')
            if (stat.S_ISSOCK(stream_info.st_mode) and
                    stat.S_ISSOCK(control_info.st_mode)):
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise BootstrapError(
        'BMCU transport sidecar did not create its Unix sockets')

def _prepare_transport_log(path, uid, gid):

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        elif os.fstat(fd).st_uid != uid:
            raise BootstrapError('BMCU transport log belongs to another account')
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)

def _wait_transport_online(process, status_file, name, expected_uid, timeout):
    timeout = max(0.0, float(timeout or 0.0))
    if timeout <= 0.0:
        return
    deadline = time.monotonic() + timeout
    last = 'status file is not ready'
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BootstrapError(
                'preferred BMCU transport exited before becoming online')
        try:
            info = os.lstat(status_file)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise BootstrapError('unsafe BMCU transport status file')
            if info.st_size > MAX_JSON:
                raise BootstrapError('BMCU transport status file is too large')
            with open(status_file, 'r') as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError('invalid transport status')
            if str(value.get('name') or '') != str(name):
                raise ValueError('transport status belongs to another device')
            if int(value.get('pid', 0) or 0) != int(process.pid):
                raise ValueError('transport status PID is stale')
            uid = str(value.get('uid') or '').upper()
            expected = str(expected_uid or '').upper()
            if expected and uid and uid != expected:
                last = 'transport found foreign UID %s while scanning' % uid
                time.sleep(0.05)
                continue
            if (bool(value.get('online')) and bool(value.get('serial_open')) and
                    not bool(value.get('serial_released')) and
                    (not expected or uid == expected)):
                return
            last = str(value.get('last_error') or 'transport is not online yet')
        except BootstrapError:
            raise
        except Exception as exc:
            last = str(exc)
        time.sleep(0.05)
    raise BootstrapError(
        'preferred BMCU transport did not become online: %s' % last)

def ensure_transport_processes(bmcu_dir, metadata, preferred_name=None, preferred_online_timeout=0.0):

    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    daemon = os.path.realpath(os.path.join(
        runtime, 'scripts', 'bmcu_transportd.py'))
    config = _transport_config(os.path.join(bmcu_dir, 'bmcu.cfg'))
    record_path = os.path.join(bmcu_dir, 'transport-processes.json')
    previous = _read_transport_records(record_path)
    if not config['enabled']:
        for record in previous.values():
            _stop_transport_record(record, runtime)
        try:
            os.unlink(record_path)
        except OSError:
            pass
        return 0
    if not os.path.isfile(daemon) or not inside(daemon, runtime):
        raise BootstrapError('BMCU transport daemon is unavailable: %s' % daemon)

    user = str(metadata.get('user', '') or '')
    group = str(metadata.get('group', '') or '')
    try:
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        raise BootstrapError('invalid Klipper user/group for BMCU transport')
    socket_dir = config['socket_dir']
    if os.path.lexists(socket_dir):
        if os.path.islink(socket_dir) or not os.path.isdir(socket_dir):
            raise BootstrapError('unsafe BMCU transport socket directory')
    else:
        os.makedirs(socket_dir, 0o700)
    directory_info = os.stat(socket_dir)
    if directory_info.st_uid != uid or directory_info.st_gid != gid:
        if os.geteuid() != 0:
            raise BootstrapError(
                'BMCU transport socket directory belongs to another account')
        os.chown(socket_dir, uid, gid)
    try:
        os.chmod(socket_dir, 0o700)
    except PermissionError:
        raise BootstrapError(
            'BMCU transport socket directory permissions cannot be secured')
    secured_info = os.stat(socket_dir)
    if (secured_info.st_uid != uid or secured_info.st_gid != gid or
            stat.S_IMODE(secured_info.st_mode) != 0o700):
        raise BootstrapError('BMCU transport socket directory is not private')

    digest = _read_package_digest(os.path.join(runtime, 'package.sha256'))
    desired_names = set(item['name'] for item in config['devices'])
    for name, record in list(previous.items()):
        if name not in desired_names:
            _stop_transport_record(record, runtime)

    log_dir = '/oem/klippylogs'
    if not os.path.isdir(log_dir) or not os.access(log_dir, os.W_OK):
        log_dir = bmcu_dir
    records = {}
    started = 0
    preferred_name = str(preferred_name or '')
    ordered_devices = sorted(config['devices'], key=lambda item: (
        0 if preferred_name and str(item.get('name', '')) == preferred_name else 1,
        0 if str(item.get('port', '')).startswith('/dev/serial/by-path/') else
        1 if str(item.get('port', '')).startswith('/dev/serial/by-id/') else 2,
        str(item.get('name', ''))))
    config_dir = os.path.realpath(str(metadata.get('config_dir') or ''))
    non_bmcu_serials = _non_bmcu_serial_devices(metadata, bmcu_dir) if config_dir else set()
    excluded_serials = sorted(
        str(value) for value in non_bmcu_serials if value)
    for item in ordered_devices:
        name, port = item['name'], item['port']
        logfile = os.path.join(
            log_dir, 'bmcu-transport-%s.log' % name)
        _prepare_transport_log(logfile, uid, gid)
        socket_path = os.path.join(socket_dir, '%s.sock' % name)
        status_file = os.path.join(socket_dir, '%s.status.json' % name)
        if len(socket_path.encode('utf-8')) >= 100:
            raise BootstrapError(
                'BMCU transport socket path is too long for device %s' % name)
        scan_fallback = bool(item.get('uid'))
        payload = {
            'digest': digest, 'name': name, 'port': port,
            'uid': str(item.get('uid', '') or ''),
            'scan_fallback': scan_fallback,
            'socket': socket_path, 'status_file': status_file,
            'baud': config['baud'],
            'heartbeat': config['heartbeat'],
            'connection_timeout': config['connection_timeout'],
            'reconnect': config['reconnect'],
            'status_interval': config['status_interval'],
            'connect_settle': config['connect_settle'],
            'exclude_ports': excluded_serials,
            'logfile': logfile,
        }
        settings_digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':')).encode(
                'utf-8')).hexdigest()
        old = previous.get(name, {})
        current = (_recorded_transport_alive(old, runtime) and
                   str(old.get('settings_digest', '')) == settings_digest and
                   os.path.exists(socket_path) and
                   os.path.exists(socket_path + '.ctl'))
        if current:
            records[name] = old
            continue
        _stop_transport_record(old, runtime)
        for stale in (socket_path, socket_path + '.ctl', status_file):
            try:
                if os.path.lexists(stale):
                    os.unlink(stale)
            except OSError:
                pass
        command = [
            sys.executable, '-I', '-S', daemon,
            '--name', name, '--port', port, '--socket', socket_path,
            '--baud', str(config['baud']),
            '--heartbeat', str(config['heartbeat']),
            '--connection-timeout', str(config['connection_timeout']),
            '--reconnect', str(config['reconnect']),
            '--status-interval', str(config['status_interval']),
            '--connect-settle', str(config['connect_settle']),
            '--log-file', logfile, '--status-file', status_file,
        ]
        if item.get('uid'):
            command.extend(['--expected-uid', item['uid']])
        if scan_fallback:
            command.append('--scan-fallback')
            for excluded in excluded_serials:
                command.extend(['--exclude-port', excluded])
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True, cwd=bmcu_dir,
            preexec_fn=_transport_preexec(user, uid, gid))
        try:
            _wait_transport_started(process, socket_path)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            for stale in (socket_path, socket_path + '.ctl', status_file):
                try:
                    if os.path.lexists(stale):
                        os.unlink(stale)
                except OSError:
                    pass

            fallback = dict(records)
            for previous_name, previous_record in previous.items():
                if (previous_name in desired_names and
                        previous_name not in fallback and
                        _recorded_transport_alive(previous_record, runtime)):
                    fallback[previous_name] = previous_record
            _atomic_transport_records(record_path, fallback)
            raise
        records[name] = {
            'pid': int(process.pid), 'daemon': daemon,
            'settings_digest': settings_digest, 'socket': socket_path,
            'port': port, 'logfile': logfile, 'status_file': status_file,
        }

        _atomic_transport_records(record_path, records)
        started += 1
        if (preferred_name and name == preferred_name and
                float(preferred_online_timeout or 0.0) > 0.0):
            _wait_transport_online(
                process, status_file, name, item.get('uid', ''),
                preferred_online_timeout)
    _atomic_transport_records(record_path, records)
    return started

def _read_planner_record(path):
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return {}
        with open(path, 'r') as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}

def _recorded_planner_alive(record, runtime):
    if not isinstance(record, dict):
        return False
    try:
        pid = int(record.get('pid', 0) or 0)
    except (TypeError, ValueError):
        return False
    daemon = os.path.realpath(str(record.get('daemon', '') or ''))
    if (pid <= 1 or os.path.basename(daemon) != 'bmcu_plannerd.py' or
            not inside(daemon, runtime)):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    command = _process_cmdline(pid)
    return bool(command and 'bmcu_plannerd.py' in command and daemon in command)

def _stop_planner_record(record, runtime):
    if not _recorded_planner_alive(record, runtime):
        return False
    pid = int(record['pid'])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True

def _atomic_planner_record(path, payload):
    fd, temporary = tempfile.mkstemp(
        prefix='.planner-process.', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _planner_paths_ready(socket_path, result_dir, uid, gid):
    try:
        socket_info = os.lstat(socket_path)
        result_info = os.lstat(result_dir)
    except OSError:
        return False
    return bool(
        stat.S_ISSOCK(socket_info.st_mode) and
        socket_info.st_uid == uid and socket_info.st_gid == gid and
        stat.S_IMODE(socket_info.st_mode) == 0o600 and
        stat.S_ISDIR(result_info.st_mode) and
        not os.path.islink(result_dir) and
        result_info.st_uid == uid and result_info.st_gid == gid and
        stat.S_IMODE(result_info.st_mode) == 0o700)

def _wait_planner_started(process, socket_path, timeout=2.0):
    deadline = time.monotonic() + max(0.2, float(timeout))
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise BootstrapError(
                'BMCU U1 planner exited during startup with code %d' % code)
        try:
            if stat.S_ISSOCK(os.stat(socket_path).st_mode):
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise BootstrapError('BMCU U1 planner did not create its Unix socket')

def ensure_planner_process(bmcu_dir, metadata):

    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    daemon = os.path.realpath(os.path.join(
        runtime, 'scripts', 'bmcu_plannerd.py'))
    record_path = os.path.join(bmcu_dir, 'planner-process.json')
    previous = _read_planner_record(record_path)
    config = _transport_config(os.path.join(bmcu_dir, 'bmcu.cfg'))
    enabled = (str(metadata.get('platform', '') or '') == 'snapmaker_u1' and
               bool(config.get('enabled', True)) and bool(config.get('devices')))
    socket_dir = config['socket_dir']
    socket_path = os.path.join(socket_dir, 'u1-planner.sock')
    result_dir = os.path.join(socket_dir, 'u1-plans')
    if not enabled:
        _stop_planner_record(previous, runtime)
        for stale in (record_path, socket_path):
            try:
                os.unlink(stale)
            except OSError:
                pass
        return 0
    if not os.path.isfile(daemon) or not inside(daemon, runtime):
        raise BootstrapError('BMCU U1 planner is unavailable: %s' % daemon)
    if len(socket_path.encode('utf-8')) >= 100:
        raise BootstrapError('BMCU U1 planner socket path is too long')
    user = str(metadata.get('user', '') or '')
    group = str(metadata.get('group', '') or '')
    try:
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        raise BootstrapError('invalid Klipper user/group for BMCU U1 planner')
    if (not os.path.isdir(socket_dir) or os.path.islink(socket_dir) or
            os.stat(socket_dir).st_uid != uid or
            os.stat(socket_dir).st_gid != gid or
            stat.S_IMODE(os.stat(socket_dir).st_mode) != 0o700):
        raise BootstrapError('BMCU U1 planner socket directory is not private')
    digest = _read_package_digest(os.path.join(runtime, 'package.sha256'))
    payload = {
        'digest': digest,
        'socket': socket_path,
        'result_dir': result_dir,
        'daemon': daemon,
    }
    settings_digest = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(',', ':')).encode(
            'utf-8')).hexdigest()
    current = (_recorded_planner_alive(previous, runtime) and
               str(previous.get('settings_digest', '')) == settings_digest and
               _planner_paths_ready(socket_path, result_dir, uid, gid))
    if current:
        return 0
    _stop_planner_record(previous, runtime)
    try:
        if os.path.lexists(socket_path):
            os.unlink(socket_path)
    except OSError:
        pass
    if os.path.lexists(result_dir):
        if os.path.islink(result_dir) or not os.path.isdir(result_dir):
            raise BootstrapError('unsafe BMCU U1 planner result directory')
    else:
        os.mkdir(result_dir, 0o700)
    if os.geteuid() == 0:
        os.chown(result_dir, uid, gid)
    os.chmod(result_dir, 0o700)
    log_dir = '/oem/klippylogs'
    if not os.path.isdir(log_dir) or not os.access(log_dir, os.W_OK):
        log_dir = bmcu_dir
    logfile = os.path.join(log_dir, 'bmcu-planner.log')
    _prepare_transport_log(logfile, uid, gid)
    command = [
        sys.executable, '-I', '-S', daemon,
        '--socket', socket_path,
        '--result-dir', result_dir,
    ]
    with open(logfile, 'ab', buffering=0) as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            close_fds=True, start_new_session=True, cwd=bmcu_dir,
            preexec_fn=_transport_preexec(user, uid, gid))
    try:
        _wait_planner_started(process, socket_path)
    except Exception:
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        raise
    _atomic_planner_record(record_path, {
        'pid': int(process.pid),
        'daemon': daemon,
        'socket': socket_path,
        'result_dir': result_dir,
        'settings_digest': settings_digest,
    })
    return 1

def _panel_config(path):
    values = {
        'enabled': True,
        'host': '0.0.0.0',
        'port': 8291,
        'moonraker_url': 'http://127.0.0.1:7125',
        'access_token': '',
    }
    if not os.path.isfile(path) or os.path.islink(path):
        return values
    info = os.stat(path)
    if info.st_size > 1024 * 1024:
        raise BootstrapError('bmcu_panel.cfg is too large')
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    with open(path, 'r') as stream:
        parser.read_file(stream)
    if not parser.has_section('bmcu_panel'):
        raise BootstrapError('bmcu_panel.cfg has no [bmcu_panel] section')
    values['enabled'] = parser.getboolean(
        'bmcu_panel', 'enabled', fallback=True)
    values['host'] = parser.get(
        'bmcu_panel', 'host', fallback='0.0.0.0').strip()
    values['port'] = parser.getint(
        'bmcu_panel', 'port', fallback=8291)
    values['moonraker_url'] = parser.get(
        'bmcu_panel', 'moonraker_url',
        fallback='http://127.0.0.1:7125').strip()
    values['access_token'] = parser.get(
        'bmcu_panel', 'access_token', fallback='').strip()
    if (not values['host'] or len(values['host']) > 255 or
            any(char in values['host'] for char in '\r\n\0')):
        raise BootstrapError('invalid BMCU panel host')
    if not 1024 <= int(values['port']) <= 65535:
        raise BootstrapError('invalid BMCU panel port')
    if (not values['moonraker_url'].startswith(('http://', 'https://')) or
            len(values['moonraker_url']) > 2048 or
            any(char in values['moonraker_url'] for char in '\r\n\0')):
        raise BootstrapError('invalid BMCU Moonraker URL')
    if (len(values['access_token']) > 512 or
            any(char in values['access_token'] for char in '\r\n\0')):
        raise BootstrapError('invalid BMCU panel access token')
    return values

def _panel_settings_digest(settings):

    payload = {
        'enabled': bool(settings.get('enabled', True)),
        'host': str(settings.get('host', '0.0.0.0')),
        'port': int(settings.get('port', 8291)),
        'moonraker_url': str(settings.get(
            'moonraker_url', 'http://127.0.0.1:7125')),

        'access_token_sha256': hashlib.sha256(
            str(settings.get('access_token', '')).encode('utf-8')).hexdigest(),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

def _lower_panel_priority(pid):

    try:
        if hasattr(os, 'setpriority') and hasattr(os, 'PRIO_PROCESS'):
            os.setpriority(os.PRIO_PROCESS, int(pid), 10)
            return True
    except (OSError, ValueError):
        pass
    return False

def _wait_panel_started(process, timeout=2.0):

    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise BootstrapError(
                'external panel exited during startup with code %d' % code)
        time.sleep(0.05)

        if time.monotonic() + 0.05 >= deadline:
            return

def _read_package_digest(path):
    try:
        with open(path, 'r') as stream:
            value = stream.read(256).strip().split()[0]
        if len(value) == 64:
            int(value, 16)
            return value.lower()
    except Exception:
        pass
    return 'unknown'

def _read_panel_record(path):
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return {}
        with open(path, 'r') as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            return {}
        value['pid'] = int(value.get('pid', 0) or 0)
        return value
    except Exception:
        return {}

def _process_cmdline(pid):
    try:
        with open('/proc/%d/cmdline' % int(pid), 'rb') as stream:
            return stream.read(8192).replace(b'\0', b' ').decode(
                'utf-8', 'replace')
    except Exception:
        return ''

def _recorded_panel_alive(record):
    pid = int(record.get('pid', 0) or 0)
    server = os.path.realpath(str(record.get('server', '') or ''))
    if pid <= 1 or not server or os.path.basename(server) != 'bmcu_panel_server.py':
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    command = _process_cmdline(pid)
    return bool(command and 'bmcu_panel_server.py' in command and server in command)

def _stop_recorded_panel(record):
    if not _recorded_panel_alive(record):
        return False
    pid = int(record['pid'])
    try:
        os.kill(pid, 15)
    except OSError:
        return False
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    return True

def _atomic_panel_record(path, payload):
    fd, temporary = tempfile.mkstemp(prefix='.panel-process.', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def ensure_panel_process(bmcu_dir):

    runtime = os.path.join(bmcu_dir, 'runtime')
    settings = _panel_config(os.path.join(bmcu_dir, 'bmcu_panel.cfg'))
    pidfile = os.path.join(bmcu_dir, 'panel-process.json')
    record = _read_panel_record(pidfile)
    if not settings['enabled']:
        _stop_recorded_panel(record)
        try:
            os.unlink(pidfile)
        except OSError:
            pass
        return 0

    server = os.path.realpath(os.path.join(runtime, 'web', 'bmcu_panel_server.py'))
    root = os.path.realpath(os.path.join(runtime, 'web'))
    updater = os.path.realpath(os.path.join(runtime, 'scripts', 'bmcu_update.py'))
    digest = _read_package_digest(os.path.join(runtime, 'package.sha256'))
    settings_digest = _panel_settings_digest(settings)
    if not os.path.isfile(server) or not inside(server, runtime):
        raise BootstrapError('panel server is unavailable: %s' % server)
    if (not os.path.isdir(root) or not inside(root, runtime) or
            (os.path.isfile(updater) and not inside(updater, runtime))):
        raise BootstrapError('panel runtime paths are unsafe')

    current = (_recorded_panel_alive(record) and
               os.path.realpath(str(record.get('server', '') or '')) == server and
               str(record.get('digest', '') or '') == digest and
               str(record.get('settings_digest', '') or '') == settings_digest)
    if current:
        return 0
    _stop_recorded_panel(record)

    state_dir = os.path.join(bmcu_dir, 'update')
    if not os.path.isdir(state_dir):
        os.makedirs(state_dir, 0o750)
    log_dir = '/oem/klippylogs'
    if not os.path.isdir(log_dir) or not os.access(log_dir, os.W_OK):
        log_dir = bmcu_dir
    logfile = os.path.join(log_dir, 'bmcu-panel.log')
    command = [

        sys.executable, '-I', '-S', server,
        '--host', settings['host'],
        '--port', str(settings['port']),
        '--root', root,
        '--moonraker', settings['moonraker_url'],
        '--state-dir', state_dir,
        '--access-token', settings['access_token'],
    ]
    if os.path.isfile(updater):
        command.extend(['--updater', updater])
    with open(logfile, 'ab', buffering=0) as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            close_fds=True, start_new_session=True, cwd=bmcu_dir)
    _lower_panel_priority(process.pid)
    _wait_panel_started(process)
    _atomic_panel_record(pidfile, {
        'pid': int(process.pid),
        'digest': digest,
        'settings_digest': settings_digest,
        'port': int(settings['port']),
        'server': server,
    })
    return 1

def write_boot_stamp(bmcu_dir, changed_links, changed_include, metadata_path):
    path = os.path.join(bmcu_dir, 'HOST_BOOT.json')
    payload = {
        'product': PRODUCT,
        'version': VERSION,
        'boot_id': _boot_id(),
        'timestamp': int(time.time()),
        'pid': int(os.getpid()),
        'repaired_links': int(changed_links),
        'repaired_include': int(changed_include),
        'metadata': os.path.realpath(metadata_path),
    }
    fd, temporary = tempfile.mkstemp(prefix='.HOST_BOOT.', dir=bmcu_dir)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory_fd = os.open(bmcu_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _usb_serial_identity(path):
    tty = os.path.basename(os.path.realpath(path))
    try:
        current = os.path.realpath(os.path.join('/sys/class/tty', tty, 'device'))
    except OSError:
        return '', '', ''
    driver = ''
    vendor = product = ''
    for _ in range(10):
        if not driver:
            try:
                driver = os.path.basename(
                    os.path.realpath(os.path.join(current, 'driver'))).lower()
            except OSError:
                pass
        try:
            with open(os.path.join(current, 'idVendor'), 'r') as stream:
                vendor = stream.read(32).strip().lower()
            with open(os.path.join(current, 'idProduct'), 'r') as stream:
                product = stream.read(32).strip().lower()
        except OSError:
            pass
        if driver and vendor and product:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return vendor, product, driver

def _serial_path_rank(path):
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

def _physical_serial_candidates():
    grouped = {}
    patterns = (
        '/dev/serial/by-path/*', '/dev/serial/by-id/*',
        '/dev/ttyUSB*', '/dev/ttyCH343USB*', '/dev/ttyACM*')
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            try:
                resolved = os.path.realpath(path)
                info = os.stat(resolved)
            except OSError:
                continue
            if not stat.S_ISCHR(info.st_mode):
                continue
            tty = os.path.basename(resolved)
            if not tty.startswith(('ttyUSB', 'ttyCH343USB', 'ttyACM')):
                continue
            aliases = grouped.setdefault(resolved, set())
            aliases.add(path)
    result = []
    for resolved, aliases in grouped.items():
        preferred = min(aliases, key=lambda value: (_serial_path_rank(value), value))
        result.append({
            'path': preferred,
            'device': resolved,
            'aliases': sorted(aliases),
        })
    return sorted(result, key=lambda item: (_serial_path_rank(item['path']), item['path']))

def _active_config_files(printer_cfg):
    pending = [os.path.abspath(printer_cfg)]
    visited = set()
    while pending:
        path = pending.pop(0)
        absolute = os.path.abspath(path)
        if absolute in visited:
            continue
        visited.add(absolute)
        try:
            info = os.stat(absolute)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG:
                continue
            with open(absolute, 'r', errors='ignore') as stream:
                text = stream.read(MAX_CONFIG + 1)
        except OSError:
            continue
        if len(text) > MAX_CONFIG:
            continue
        yield absolute, text
        directory = os.path.dirname(absolute)
        includes = []
        for line in text.splitlines():
            active = line.split('#', 1)[0]
            match = INCLUDE_RE.match(active)
            if not match:
                continue
            include_spec = match.group(1).strip()
            include_glob = os.path.join(directory, include_spec)
            matches = sorted(glob.glob(include_glob))
            if not matches and not glob.has_magic(include_glob):
                continue
            includes.extend(matches)
        pending[0:0] = includes

def _non_bmcu_serial_devices(metadata, bmcu_dir):
    used = set()
    pattern = re.compile(
        r'/dev/(?:serial/(?:by-id|by-path)/[^\s#;,\]\)]+|'
        r'ttyUSB\d+|ttyACM\d+|ttyCH343USB\d+)')
    printer_cfg = os.path.realpath(str(metadata.get('printer_cfg') or ''))
    bmcu_root = os.path.realpath(bmcu_dir)
    if not printer_cfg:
        return used
    for path, text in _active_config_files(printer_cfg):
        real_path = os.path.realpath(path)
        if real_path == bmcu_root or inside(real_path, bmcu_root):
            continue
        for line in text.splitlines():
            active = line.split('#', 1)[0].split(';', 1)[0]
            for match in pattern.finditer(active):
                value = match.group(0)
                used.add(value)
                if os.path.exists(value):
                    used.add(os.path.realpath(value))
    return used

def quiesce_existing_transport_processes(bmcu_dir):
    runtime = os.path.join(bmcu_dir, 'runtime')
    record_path = os.path.join(bmcu_dir, 'transport-processes.json')
    records = _read_transport_records(record_path)
    stopped = 0
    for record in records.values():
        if _recorded_transport_alive(record, runtime):
            _stop_transport_record(record, runtime)
            stopped += 1
    if records:
        _atomic_transport_records(record_path, {})
    return stopped

def auto_discover_generic_bmcu(bmcu_dir, metadata, quiet=False):
    config_dir = os.path.realpath(str(metadata.get('config_dir') or ''))
    if not config_dir or not os.path.isdir(config_dir):
        return 0
    bmcu_cfg = os.path.join(bmcu_dir, 'bmcu.cfg')
    used = _non_bmcu_serial_devices(metadata, bmcu_dir)
    candidates = []
    for candidate in _physical_serial_candidates():
        paths = set(candidate['aliases']) | {candidate['path'], candidate['device']}
        if paths & used:
            continue
        candidates.append(candidate['path'])
    if not candidates:
        return 0
    runtime_scripts = os.path.join(bmcu_dir, 'runtime', 'scripts')
    detector = os.path.join(runtime_scripts, 'detect_bmcu.py')
    applier = os.path.join(runtime_scripts, 'apply_detected_devices.py')
    if not (os.path.isfile(detector) and os.path.isfile(applier)):
        return 0
    fd, detected = tempfile.mkstemp(prefix='.bmcu-autodetect-', suffix='.cfg', dir=bmcu_dir)
    os.close(fd)
    try:
        os.unlink(detected)
        command = [sys.executable, '-I', '-S', detector, '--output', detected, '--quick']
        for port in candidates:
            command.extend(['--port', port])
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, close_fds=True,
            timeout=max(8.0, 3.0 * len(candidates)))
        if result.returncode == 1:
            return 0
        if result.returncode != 0 or not os.path.isfile(detected):
            if not quiet:
                message = (result.stdout or '').strip().splitlines()
                print('WARNING: BMCU auto-detection skipped: %s' %
                      (message[-1] if message else 'probe failed'), file=sys.stderr)
            return 0
        before = b''
        try:
            with open(bmcu_cfg, 'rb') as stream:
                before = stream.read(MAX_CONFIG + 1)
        except OSError:
            pass
        backup_dir = os.path.join(bmcu_dir, 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        applied = subprocess.run(
            [sys.executable, '-I', '-S', applier,
             '--cfg', bmcu_cfg, '--detected', detected,
             '--backup-dir', backup_dir],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, close_fds=True, timeout=10.0)
        if applied.returncode != 0:
            if not quiet:
                message = (applied.stdout or '').strip().splitlines()
                print('WARNING: detected BMCU was not registered: %s' %
                      (message[-1] if message else 'configuration update failed'),
                      file=sys.stderr)
            return 0
        with open(bmcu_cfg, 'rb') as stream:
            after = stream.read(MAX_CONFIG + 1)
        changed = int(before != after)
        if changed and not quiet:
            print('BMCU runtime device map updated from hardware UID discovery.')
        return changed
    except (OSError, subprocess.SubprocessError) as exc:
        if not quiet:
            print('WARNING: BMCU auto-detection skipped: %s' % exc, file=sys.stderr)
        return 0
    finally:
        try:
            os.unlink(detected)
        except OSError:
            pass

def repair(metadata_path, quiet=False, repair_config=True):
    metadata = read_metadata(metadata_path)
    bmcu_dir, extras_dir, source_dir = validate_paths(metadata, metadata_path)
    lock = acquire_lock(bmcu_dir)
    changed_include = 0
    changed = 0
    link_changes = []
    try:

        for name in MODULES:
            source = os.path.join(source_dir, name)
            target = os.path.join(extras_dir, name)
            if not os.path.lexists(target):
                continue
            if not os.path.islink(target):
                raise BootstrapError(
                    'Klipper module path is occupied by a non-BMCU file: %s' % target)
            resolved = os.path.realpath(target)
            if resolved != os.path.realpath(source) and not inside(resolved, bmcu_dir):
                raise BootstrapError(
                    'Klipper module path contains a foreign link: %s' % target)

        for name in MODULES:
            source = os.path.join(source_dir, name)
            target = os.path.join(extras_dir, name)
            if not os.path.lexists(target):
                atomic_symlink(source, target)
                link_changes.append((target, None, source))
                changed += 1
                continue
            old_target = os.readlink(target)
            resolved = os.path.realpath(target)
            if resolved == os.path.realpath(source):
                continue
            atomic_symlink(source, target)
            link_changes.append((target, old_target, source))
            changed += 1
        if repair_config:
            changed_include = repair_printer_cfg(metadata, bmcu_dir)
    except Exception:
        rollback_errors = []
        for target, old_target, installed_source in reversed(link_changes):
            try:
                if (not os.path.islink(target) or
                        os.path.realpath(target) != os.path.realpath(installed_source)):
                    raise BootstrapError(
                        'managed link changed during bootstrap rollback: %s' % target)
                if old_target is None:
                    os.unlink(target)
                else:
                    atomic_symlink(old_target, target)
            except Exception as exc:
                rollback_errors.append('%s: %s' % (target, exc))
        if rollback_errors:
            raise BootstrapError(
                'host bootstrap failed and link rollback was incomplete: %s' %
                '; '.join(rollback_errors))
        raise
    finally:
        os.close(lock)
    discovery_changed = 0
    transport_changed = ensure_transport_processes(bmcu_dir, metadata)
    planner_changed = ensure_planner_process(bmcu_dir, metadata)
    panel_changed = 0
    try:
        panel_changed = ensure_panel_process(bmcu_dir)
    except Exception as exc:
        if not quiet:
            print('WARNING: BMCU external panel was not started: %s' % exc,
                  file=sys.stderr)
    try:
        write_boot_stamp(bmcu_dir, changed, changed_include, metadata_path)
    except OSError as exc:
        if not quiet:
            print('WARNING: BMCU host boot stamp was not written: %s' % exc,
                  file=sys.stderr)
    if not quiet:
        print('BMCU host ready (%d link(s) restored, %d config update(s), '
              '%d device discovery update(s), %d transport start(s), '
              '%d planner start(s), %d panel start(s)).' %
              (changed, changed_include, discovery_changed, transport_changed,
               planner_changed, panel_changed))
    return changed

def sync_transports(metadata_path, preferred_name=None, preferred_online_timeout=0.0):
    metadata = read_metadata(metadata_path)
    bmcu_dir, _extras_dir, _source_dir = validate_paths(metadata, metadata_path)
    lock = acquire_lock(bmcu_dir)
    try:
        return ensure_transport_processes(
            bmcu_dir, metadata, preferred_name=preferred_name,
            preferred_online_timeout=preferred_online_timeout)
    finally:
        os.close(lock)

def remove(metadata_path, quiet=False):
    metadata = read_metadata(metadata_path)
    bmcu_dir, extras_dir, _source_dir = validate_paths(metadata, metadata_path)
    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    record_path = os.path.join(bmcu_dir, 'transport-processes.json')
    records = _read_transport_records(record_path)
    for record in records.values():
        _stop_transport_record(record, runtime)
        for socket_path in (str(record.get('socket', '') or ''),
                            str(record.get('socket', '') or '') + '.ctl'):
            if socket_path:
                try:
                    os.unlink(socket_path)
                except OSError:
                    pass
    try:
        os.unlink(record_path)
    except OSError:
        pass
    planner_record_path = os.path.join(bmcu_dir, 'planner-process.json')
    planner_record = _read_planner_record(planner_record_path)
    _stop_planner_record(planner_record, runtime)
    for stale in (str(planner_record.get('socket', '') or ''),
                  planner_record_path):
        if not stale:
            continue
        try:
            os.unlink(stale)
        except OSError:
            pass
    lock = acquire_lock(bmcu_dir)
    removed = 0
    try:
        for name in MODULES:
            target = os.path.join(extras_dir, name)
            if not os.path.lexists(target):
                continue
            if not os.path.islink(target):
                raise BootstrapError(
                    'refusing to remove non-symlink Klipper module: %s' % target)
            resolved = os.path.realpath(target)
            if not inside(resolved, bmcu_dir):
                raise BootstrapError('refusing to remove foreign Klipper module: %s' % target)
            os.unlink(target)
            removed += 1
    finally:
        os.close(lock)
    if not quiet:
        print('BMCU host modules removed (%d links).' % removed)
    return removed

def default_metadata():
    scripts_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(os.path.dirname(scripts_dir), 'INSTALLATION.json')

def main():
    parser = argparse.ArgumentParser(prog='bmcu_host_bootstrap')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--repair', action='store_true')
    action.add_argument('--remove', action='store_true')
    action.add_argument('--sync-transports', action='store_true')
    parser.add_argument('--metadata', default=default_metadata())
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--links-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.remove:
        remove(args.metadata, args.quiet)
    elif args.sync_transports:
        sync_transports(args.metadata)
    else:
        repair(args.metadata, args.quiet, not args.links_only)
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (BootstrapError, OSError, ValueError, json.JSONDecodeError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        raise SystemExit(1)
