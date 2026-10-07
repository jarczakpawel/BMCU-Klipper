#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import print_function

import glob
import grp
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PRODUCT = 'BMCU-Klipper'
INCLUDE_BEGIN = '# BEGIN BMCU-KLIPPER AUTO-INCLUDE'
INCLUDE_END = '# END BMCU-KLIPPER AUTO-INCLUDE'
INCLUDES = (
    '[include bmcu/bmcu.cfg]',
    '[include bmcu/bmcu_macros.cfg]',
    '[include bmcu/bmcu_panel.cfg]',
)
MODULES = ('bmcu.py', 'bmcu_core', 'bmcu_panel.py')
MODULE_SIGNATURES = {
    'bmcu.py': (b'BMCUManager',),
    'bmcu_panel.py': (b'BMCUPanel',),
}
SYSTEMD_DROPIN_NAME = 'bmcu-klipper.conf'
SYSTEMD_DIR = '/etc/systemd/system'
MANAGED_HEADER_RE = re.compile(
    br'(?m)^# Managed by BMCU-Klipper(?: [0-9]+\.[0-9]+\.[0-9]+)?(?:\.| -)')

U1_KLIPPER_SERVICE = '/etc/init.d/S60klipper'
U1_LEGACY_BOOT_HOOK = '/etc/init.d/S59bmcu-klipper'
U1_HOOK_DIR = '/etc/hooks/klipper.d'
U1_BOOT_HOOK = '/etc/hooks/klipper.d/50-bmcu-klipper.sh'
U1_PERSISTENCE_MARKER = '/oem/.debug'
U1_RUNNER_DIR = '/oem/bmcu-klipper'
U1_RUNNER = '/oem/bmcu-klipper/run-host-bootstrap.py'
U1_RUNNER_MARKER = '/oem/bmcu-klipper/.managed-by-bmcu'
U1_SERIAL_RULE_DIR = '/etc/udev/rules.d'
U1_SERIAL_RULE = '/etc/udev/rules.d/99-bmcu-klipper.rules'
U1_SERIAL_RULE_LABEL = 'Snapmaker U1 CH340 access'
U1_LOG_DIR = '/oem/klippylogs'
U1_SERVICE_BEGIN = '# BEGIN BMCU-KLIPPER SERVICE-HOOKS'
U1_SERVICE_END = '# END BMCU-KLIPPER SERVICE-HOOKS'
U1_SERVICE_CALL_MARKER = (
    '# BMCU-KLIPPER PREPARE IMMEDIATELY BEFORE KLIPPER PRIVILEGE DROP')
U1_SERVICE_CALL = 'bmcu_prepare_klipper_start || true'
U1_SERVICE_CALL_RE = re.compile(
    r'^\s*bmcu_prepare_klipper_start(?:\s*\|\|\s*(?:exit\s+1|true|:))?\s*;?\s*$')
U1_FEEDER_MAP = {0: ('left', 1), 1: ('left', 0), 2: ('right', 0), 3: ('right', 1)}


U1_MAIN_STATES = {
    0: 'IDLE', 1: 'PRINTING', 2: 'XYZ_OFFSET_CALIBRATE', 3: 'BED_LEVELING',
    4: 'FLOW_CALIBRATION', 5: 'SHAPER_CALIBRATE', 6: 'UPGRADING',
    7: 'ABNORMAL', 8: 'SCREWS_TILT_ADJUST', 9: 'AUTO_LOAD',
    10: 'AUTO_UNLOAD', 11: 'MANUAL_LOAD',
    12: 'PARK_POINT_MANUAL_CALIBRATION', 13: 'HOMING_ORIGIN_CALIBRATION',
}
U1_ACTIONS = {
    0: 'IDLE', 1: 'HOMING', 2: 'DETECT_PLATE', 3: 'PREHEAT_CHAMBER',
}

TRUSTED_PATH = '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin'
CONTROLLABLE_BACKENDS = ('systemd', 'sysv', 'openrc', 'supervisor', 'runit', 's6')
MAX_TEXT = 16 * 1024 * 1024
MAX_HTTP = 4 * 1024 * 1024
BACKUP_ROOT_NAME = 'bmcu-backups'
LEGACY_UNINSTALL_BACKUP_RE = re.compile(r'^bmcu-uninstalled-\d{8}-\d{6}-\d+$')


class LifecycleError(RuntimeError):
    pass


def out(message=''):
    try:
        print(message)
        sys.stdout.flush()
    except Exception:
        pass


def warn(message):
    try:
        print('WARNING: %s' % message)
        sys.stdout.flush()
    except Exception:
        pass


def shield_from_hangup():
    for name in ('SIGHUP', 'SIGPIPE'):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, signal.SIG_IGN)
            except (OSError, ValueError):
                pass


def read_bytes(path, limit=MAX_TEXT):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise LifecycleError('not a regular file: %s' % path)
        if info.st_size > limit:
            raise LifecycleError('file is too large: %s' % path)
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data) > limit:
            raise LifecycleError('file is too large: %s' % path)
        return data
    finally:
        os.close(descriptor)


def read_optional(path, limit=MAX_TEXT):
    try:
        return read_bytes(path, limit)
    except (OSError, LifecycleError):
        return None


def read_json(path, limit=8 * 1024 * 1024):
    data = read_optional(path, limit)
    if data is None:
        return None
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    return value


def fsync_dir(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _copy_xattrs(source_path, target_path):
    if not hasattr(os, 'listxattr'):
        return
    try:
        names = os.listxattr(source_path, follow_symlinks=False)
    except (OSError, TypeError):
        return
    for name in names:
        try:
            os.setxattr(target_path, name,
                        os.getxattr(source_path, name, follow_symlinks=False),
                        follow_symlinks=False)
        except (OSError, TypeError):
            pass


def atomic_write(path, data, mode=None, uid=None, gid=None):
    if isinstance(data, str):
        data = data.encode('utf-8')
    parent = os.path.dirname(os.path.abspath(path))
    existing = None
    try:
        existing = os.lstat(path)
        if stat.S_ISLNK(existing.st_mode):
            raise LifecycleError('refusing to replace a symbolic link: %s' % path)
        if not stat.S_ISREG(existing.st_mode):
            raise LifecycleError('refusing to replace a special file: %s' % path)
    except FileNotFoundError:
        existing = None
    descriptor, temporary = tempfile.mkstemp(
        prefix='.%s.bmcu-' % os.path.basename(path), dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is None:
            mode = stat.S_IMODE(existing.st_mode) if existing else 0o644
        if uid is None:
            uid = existing.st_uid if existing else -1
        if gid is None:
            gid = existing.st_gid if existing else -1
        os.chmod(temporary, mode)
        if uid != -1 or gid != -1:
            try:
                os.chown(temporary, uid, gid)
            except OSError:
                pass
        if existing is not None:
            _copy_xattrs(path, temporary)
        os.replace(temporary, path)
        temporary = None
        fsync_dir(parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def file_owner(path):
    try:
        info = os.stat(path)
        return info.st_uid, info.st_gid
    except OSError:
        return -1, -1


def remove_path(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    fsync_dir(os.path.dirname(path))
    return True


def chown_tree(path, uid, gid):
    for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files + dirs:
            try:
                os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)
            except OSError:
                pass
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except OSError:
        pass


def inside(path, root):
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
        return os.path.commonpath((real_path, real_root)) == real_root
    except ValueError:
        return False


def trusted_root_path(path, require_executable=False, require_directory=False):
    if not path or not os.path.isabs(path):
        raise LifecycleError('system path must be absolute: %s' % path)
    resolved = os.path.realpath(path)
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise LifecycleError('system path is unavailable: %s (%s)' % (path, exc))
    if require_directory and not stat.S_ISDIR(info.st_mode):
        raise LifecycleError('system path is not a directory: %s' % path)
    if not require_directory:
        if not stat.S_ISREG(info.st_mode):
            raise LifecycleError('system program is not a regular file: %s' % path)
        if require_executable and not os.access(resolved, os.X_OK):
            raise LifecycleError('system program is not executable: %s' % path)
    current = resolved
    while True:
        current_info = os.stat(current)
        if current_info.st_uid != 0 or stat.S_IMODE(current_info.st_mode) & 0o022:
            raise LifecycleError(
                'refusing to run a program through a non-root-owned or '
                'writable path: %s' % current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return resolved


def trusted_program(name):
    errors = []
    for directory in TRUSTED_PATH.split(':'):
        candidate = os.path.join(directory, name)
        if not os.path.exists(candidate):
            continue
        try:
            return trusted_root_path(candidate, require_executable=True)
        except LifecycleError as exc:
            errors.append(str(exc))
    if errors:
        raise LifecycleError(errors[0])
    raise LifecycleError('required system program is unavailable: %s' % name)


def trusted_program_path(name):
    for directory in TRUSTED_PATH.split(':'):
        candidate = os.path.join(directory, name)
        if not os.path.exists(candidate):
            continue
        try:
            trusted_root_path(candidate, require_executable=True)
            return candidate
        except LifecycleError:
            continue
    return trusted_program(name)


def optional_program(name):
    try:
        return trusted_program(name)
    except LifecycleError:
        return ''


def safe_env(home='/root'):
    return {
        'PATH': TRUSTED_PATH,
        'HOME': home or '/',
        'LANG': 'C',
        'LC_ALL': 'C',
        'PYTHONDONTWRITEBYTECODE': '1',
    }


def run(command, timeout=180, env=None, capture=False):
    try:
        result = subprocess.run(
            command, env=(safe_env() if env is None else env),
            stdin=subprocess.DEVNULL,
            stdout=(subprocess.PIPE if capture else None),
            stderr=(subprocess.STDOUT if capture else None),
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, 'timed out after %ds' % timeout
    except OSError as exc:
        return 127, str(exc)
    output = ''
    if capture and result.stdout:
        output = result.stdout.decode('utf-8', 'replace')
    return result.returncode, output


def supplementary_groups(user, primary_gid):
    values = {int(primary_gid)}
    for entry in grp.getgrall():
        if user in entry.gr_mem:
            values.add(int(entry.gr_gid))
    return sorted(values)


def run_as_user(command, user, timeout=180, capture=True):
    info = pwd.getpwnam(user)
    uid, gid = info.pw_uid, info.pw_gid
    groups = supplementary_groups(user, gid)

    def demote():
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
        os.umask(0o022)

    try:
        result = subprocess.run(
            command, env=safe_env(info.pw_dir), preexec_fn=demote,
            stdin=subprocess.DEVNULL,
            stdout=(subprocess.PIPE if capture else None),
            stderr=(subprocess.STDOUT if capture else None),
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, 'timed out after %ds' % timeout
    except OSError as exc:
        return 127, str(exc)
    output = ''
    if capture and result.stdout:
        output = result.stdout.decode('utf-8', 'replace')
    return result.returncode, output


def read_cmdline(pid):
    try:
        with open('/proc/%d/cmdline' % int(pid), 'rb') as stream:
            raw = stream.read(256 * 1024)
    except OSError:
        return []
    return [part.decode('utf-8', 'replace') for part in raw.split(b'\0') if part]


def process_cwd(pid):
    try:
        return os.readlink('/proc/%d/cwd' % int(pid))
    except OSError:
        return '/'


def process_is_python(pid, argv):
    names = []
    try:
        names.append(os.path.basename(os.path.realpath(
            os.readlink('/proc/%d/exe' % int(pid)))).lower())
    except OSError:
        pass
    if argv:
        names.append(os.path.basename(argv[0]).lower())
    return any(name.startswith('python') or name.startswith('pypy') for name in names)


def all_pids():
    try:
        return [int(item) for item in os.listdir('/proc') if item.isdigit()]
    except OSError:
        return []


def own_process_chain():
    chain = set()
    pid = os.getpid()
    for _ in range(64):
        if pid <= 1 or pid in chain:
            break
        chain.add(pid)
        try:
            with open('/proc/%d/stat' % pid, 'r') as stream:
                fields = stream.read().rsplit(')', 1)[1].split()
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
    return chain


def klippy_pids(klipper_dir, printer_cfg):
    klippy_py = os.path.realpath(os.path.join(klipper_dir, 'klippy', 'klippy.py'))
    klippy_pkg = os.path.realpath(os.path.join(klipper_dir, 'klippy'))
    cfg_real = os.path.realpath(printer_cfg)
    mine = own_process_chain()
    values = []
    for pid in all_pids():
        if pid in mine:
            continue
        argv = read_cmdline(pid)
        if not argv or not process_is_python(pid, argv):
            continue
        cwd = process_cwd(pid)

        def resolved(arg):
            return os.path.realpath(arg if os.path.isabs(arg) else os.path.join(cwd, arg))

        script = any(resolved(arg) in (klippy_py, klippy_pkg)
                     for arg in argv if 'klippy' in arg)
        config = any(resolved(arg) == cfg_real for arg in argv if arg.endswith('.cfg'))
        if script and config:
            values.append(pid)
    return sorted(set(values))


def klippy_api_socket(klipper_dir, printer_cfg):
    for pid in klippy_pids(klipper_dir, printer_cfg):
        argv = read_cmdline(pid)
        for index, token in enumerate(argv):
            if token in ('-a', '--api-server') and index + 1 < len(argv):
                return argv[index + 1]
            if token.startswith('--api-server='):
                return token.split('=', 1)[1]
    data_root = os.path.dirname(os.path.dirname(os.path.realpath(printer_cfg)))
    for candidate in (os.path.join(data_root, 'comms', 'klippy.sock'),
                      '/tmp/klippy_uds'):
        try:
            if stat.S_ISSOCK(os.stat(candidate).st_mode):
                return candidate
        except OSError:
            pass
    return ''


def pid_alive(pid):
    try:
        with open('/proc/%d/stat' % int(pid), 'r') as stream:
            data = stream.read()
    except OSError:
        return False
    fields = data.rsplit(')', 1)[-1].split()
    return bool(fields) and fields[0] != 'Z'


def wait_pids_gone(pids, timeout):
    deadline = time.monotonic() + float(timeout)
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if pid_alive(pid)]
        if remaining:
            time.sleep(0.2)
    return [pid for pid in remaining if pid_alive(pid)]


def terminate(pids, grace=8.0):
    pids = [pid for pid in pids if pid > 1]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    remaining = wait_pids_gone(pids, grace)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return wait_pids_gone(remaining, 5.0)


def bmcu_helper_pids(roots):
    prefixes = []
    for root in roots:
        if root:
            prefixes.append(os.path.realpath(root).rstrip('/') + '/')
            prefixes.append(os.path.abspath(root).rstrip('/') + '/')
    if not prefixes:
        return []
    mine = own_process_chain()
    found = []
    for pid in all_pids():
        if pid in mine:
            continue
        argv = read_cmdline(pid)
        if not argv or not process_is_python(pid, argv):
            continue
        if any(os.path.basename(arg) == 'klippy.py' for arg in argv):
            continue
        if any(arg.startswith(prefix) for arg in argv for prefix in prefixes):
            found.append(pid)
    return sorted(set(found))


def stop_bmcu_helpers(roots):
    pids = bmcu_helper_pids(roots)
    if not pids:
        return 0
    remaining = terminate(pids)
    if remaining:
        raise LifecycleError('BMCU helper processes did not exit: %s' %
                             ', '.join(str(pid) for pid in remaining))
    return len(pids)


def serial_holders(device):
    try:
        real_device = os.path.realpath(device)
    except OSError:
        return []
    holders = []
    for pid in all_pids():
        fd_dir = '/proc/%d/fd' % pid
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            continue
        for entry in entries:
            try:
                if os.path.realpath(os.path.join(fd_dir, entry)) == real_device:
                    holders.append(pid)
                    break
            except OSError:
                continue
    return holders


class ServiceController(object):
    def __init__(self, service, klipper_dir, printer_cfg):
        service = service or {}
        self.backend = str(service.get('backend') or 'none')
        self.name = str(service.get('name') or '')
        self.script = str(service.get('script') or '')
        self.service_dir = str(service.get('service_dir') or '')
        self.klipper_dir = klipper_dir
        self.printer_cfg = printer_cfg

    @property
    def controllable(self):
        return self.backend in CONTROLLABLE_BACKENDS

    def describe(self):
        if self.backend == 'systemd':
            return 'systemd %s' % self.unit
        if self.backend in ('sysv', 'openrc'):
            return '%s %s' % (self.backend, self.script or self.name)
        return '%s %s' % (self.backend, self.name)

    @property
    def unit(self):
        name = self.name or 'klipper'
        return name if name.endswith('.service') else name + '.service'

    def command(self, action):
        if self.backend == 'systemd':
            return [trusted_program('systemctl'), action, self.unit]
        if self.backend == 'sysv':
            return [trusted_root_path(self.script, require_executable=True), action]
        if self.backend == 'openrc':
            script = self.script or os.path.join('/etc/init.d', self.name)
            trusted_root_path(script, require_executable=True)
            return [trusted_program('rc-service'), self.name, action]
        if self.backend == 'supervisor':
            return [trusted_program('supervisorctl'), action, self.name]
        if self.backend == 'runit':
            target = self.service_dir or self.name
            return [trusted_program('sv'), action, target]
        if self.backend == 's6':
            return [trusted_program('s6-svc'),
                    '-d' if action == 'stop' else '-u', self.service_dir]
        raise LifecycleError('Klipper service control is unavailable')

    def running_pids(self):
        return klippy_pids(self.klipper_dir, self.printer_cfg)

    def stop(self):
        if self.controllable:
            run(self.command('stop'), timeout=180, capture=True)
        pids = self.running_pids()
        if pids:
            remaining = wait_pids_gone(pids, 30.0)
            if remaining:
                remaining = terminate(remaining, grace=10.0)
            if remaining:
                raise LifecycleError(
                    'Klipper did not stop (PIDs: %s)' %
                    ', '.join(str(pid) for pid in remaining))

    def daemon_reload(self):
        if self.backend != 'systemd':
            return
        program = optional_program('systemctl')
        if program:
            run([program, 'daemon-reload'], timeout=120, capture=True)

    def start(self):
        if not self.controllable:
            return False
        if self.running_pids():
            return True
        if self.backend == 'systemd':
            program = optional_program('systemctl')
            if program:
                run([program, 'reset-failed', self.unit], timeout=60, capture=True)
        code, output = run(self.command('start'), timeout=300, capture=True)
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            if self.running_pids():
                return True
            time.sleep(0.5)
        if code != 0:
            warn('Klipper service start returned %d: %s' %
                 (code, output.strip()[-400:]))
        return bool(self.running_pids())


GENERIC_STATE_QUERY = (
    ('webhooks', 'state,state_message'),
    ('print_stats', 'state'),
    ('pause_resume', 'is_paused'),
    ('virtual_sdcard', 'is_active'),
    ('idle_timeout', 'state'),
)

U1_STATE_QUERY = (
    ('webhooks', 'state,state_message'),
    ('machine_state_manager', 'main_state,action_code'),
)


def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, timeout=20.0, data=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'BMCU-Klipper-Installer'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode('utf-8')
    request = urllib.request.Request(
        url, data=data, headers=headers, method='POST' if data is not None else 'GET')
    with _no_proxy_opener().open(request, timeout=timeout) as response:
        raw = response.read(MAX_HTTP + 1)
    if len(raw) > MAX_HTTP:
        raise LifecycleError('Moonraker response is too large')
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict):
        raise LifecycleError('invalid Moonraker response')
    return value


def http_error_text(exc):
    if not isinstance(exc, urllib.error.HTTPError):
        return str(exc)
    raw = b''
    try:
        raw = exc.read(64 * 1024)
    except Exception:
        pass
    if raw:
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            error = payload.get('error')
            if isinstance(error, dict):
                message = str(error.get('message') or '').strip()
                if message:
                    return message
            elif error:
                return str(error)
        text = raw.decode('utf-8', 'replace').strip()
        if text:
            return text
    return 'HTTP %s %s' % (exc.code, exc.reason)


def moonraker_status(base, timeout=20.0, is_u1=False):
    fields = U1_STATE_QUERY if is_u1 else GENERIC_STATE_QUERY
    query = '&'.join('%s=%s' % (name, value) for name, value in fields)
    value = http_json(base.rstrip('/') + '/printer/objects/query?' + query, timeout)
    status = (value.get('result') or {}).get('status')
    if not isinstance(status, dict):
        raise LifecycleError('Moonraker returned no printer status')
    return status


def klippy_api_request(path, method, params, timeout=10.0):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
        payload = json.dumps({'id': 4242, 'method': method, 'params': params})
        connection.sendall(payload.encode('utf-8') + b'\x03')
        buffer = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b'\x03' in buffer:
                message, buffer = buffer.split(b'\x03', 1)
                try:
                    value = json.loads(message.decode('utf-8'))
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get('id') == 4242:
                    if 'error' in value:
                        error = value['error']
                        raise LifecycleError(
                            str(error.get('message') if isinstance(error, dict) else error))
                    return value.get('result')
            if len(buffer) > MAX_HTTP:
                break
        raise LifecycleError('Klipper API did not answer')
    finally:
        connection.close()


def klippy_status(socket_path, timeout=10.0, is_u1=False):
    fields = U1_STATE_QUERY if is_u1 else GENERIC_STATE_QUERY
    objects = dict((name, value.split(',')) for name, value in fields)
    result = klippy_api_request(
        socket_path, 'objects/query', {'objects': objects}, timeout)
    status = (result or {}).get('status')
    if not isinstance(status, dict):
        raise LifecycleError('Klipper API returned no printer status')
    return status


def _u1_name(value, table):
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return table.get(int(value), str(int(value)))
    text = str(value or '').strip().upper()
    if text.isdigit():
        return table.get(int(text), text)
    return text


class PrinterSnapshot(object):
    def __init__(self):
        self.source = 'none'
        self.klippy_running = False
        self.state = 'unavailable'
        self.message = ''
        self.print_state = ''
        self.paused = False
        self.sd_active = False
        self.toolhead_busy = False
        self.u1_main = ''
        self.u1_action = ''
        self.error = ''

    @property
    def ready(self):
        return self.state == 'ready'

    def describe(self):
        if self.source == 'none':
            if self.klippy_running:
                return 'Klipper is running but not answering (%s)' % (
                    self.error or 'no API')
            return 'Klipper is not running'
        text = 'Klipper %s' % self.state
        if self.message and self.state != 'ready':
            first = self.message.strip().splitlines()[0][:200]
            text += ' (%s)' % first
        if self.u1_main:
            text += ', Snapmaker state %s/%s' % (self.u1_main, self.u1_action or 'IDLE')
        return text

    def job_active(self):
        if self.u1_main == 'UPGRADING':
            return 'the Snapmaker U1 firmware is upgrading'
        if self.u1_main == 'PRINTING' and self.state not in ('shutdown', 'error'):
            return 'the Snapmaker U1 is printing'
        if self.state in ('shutdown', 'error'):
            return ''
        if self.print_state in ('printing', 'paused'):
            return 'a print job is %s' % self.print_state
        if self.paused:
            return 'a print job is paused'
        if self.sd_active and self.state == 'ready':
            return 'a print job is running from virtual SD'
        return ''

    def transient_busy(self):
        if self.state != 'ready':
            return ''
        if self.u1_main and self.u1_main not in ('IDLE', 'ABNORMAL'):
            return 'the Snapmaker U1 is busy (%s)' % self.u1_main
        if self.u1_main == 'IDLE' and self.u1_action and self.u1_action != 'IDLE':
            return 'the Snapmaker U1 is busy (%s)' % self.u1_action
        if self.toolhead_busy:
            return 'Klipper is executing G-code'
        return ''


def _apply_status(snapshot, status):
    webhooks = status.get('webhooks')
    if isinstance(webhooks, dict):
        snapshot.state = str(webhooks.get('state') or 'unknown').strip().lower()
        snapshot.message = str(webhooks.get('state_message') or '')
    stats = status.get('print_stats')
    if isinstance(stats, dict):
        snapshot.print_state = str(stats.get('state') or '').strip().lower()
    pause = status.get('pause_resume')
    if isinstance(pause, dict):
        snapshot.paused = bool(pause.get('is_paused'))
    sdcard = status.get('virtual_sdcard')
    if isinstance(sdcard, dict):
        snapshot.sd_active = bool(sdcard.get('is_active'))
    idle = status.get('idle_timeout')
    if isinstance(idle, dict):
        snapshot.toolhead_busy = str(idle.get('state') or '').strip().lower() == 'printing'
    machine = status.get('machine_state_manager')
    if isinstance(machine, dict):
        snapshot.u1_main = _u1_name(machine.get('main_state'), U1_MAIN_STATES)
        snapshot.u1_action = _u1_name(machine.get('action_code'), U1_ACTIONS)


def printer_snapshot(moonraker_url, klipper_dir, printer_cfg, timeout=20.0, is_u1=False):
    snapshot = PrinterSnapshot()
    snapshot.klippy_running = bool(klippy_pids(klipper_dir, printer_cfg))
    errors = []
    if moonraker_url:
        try:
            status = moonraker_status(moonraker_url, timeout, is_u1)
            snapshot.source = 'moonraker'
            if 'webhooks' not in status:
                raise LifecycleError('Moonraker returned no Klipper state')
            _apply_status(snapshot, status)
            if is_u1 and snapshot.ready and not snapshot.u1_main:
                raise LifecycleError('Moonraker returned no Snapmaker machine state')
            return snapshot
        except urllib.error.HTTPError as exc:
            errors.append('Moonraker: %s' % http_error_text(exc))
        except Exception as exc:
            errors.append('Moonraker: %s' % exc)
    if snapshot.klippy_running:
        path = klippy_api_socket(klipper_dir, printer_cfg)
        if path:
            try:
                status = klippy_status(path, min(timeout, 10.0), is_u1)
                snapshot = PrinterSnapshot()
                snapshot.klippy_running = True
                snapshot.source = 'klippy-api'
                _apply_status(snapshot, status)
                if is_u1 and snapshot.ready and not snapshot.u1_main:
                    raise LifecycleError('Klipper API returned no Snapmaker machine state')
                return snapshot
            except Exception as exc:
                errors.append('Klipper API: %s' % exc)
    snapshot.source = 'none'
    snapshot.state = 'unavailable'
    snapshot.error = '; '.join(errors)
    return snapshot


def wait_until_safe(moonraker_url, klipper_dir, printer_cfg, command_name,
                    force=False, transient_timeout=180.0, request_timeout=30.0,
                    is_u1=False):
    started = time.monotonic()
    announced = set()
    while True:
        snapshot = printer_snapshot(moonraker_url, klipper_dir, printer_cfg,
                                    timeout=request_timeout, is_u1=is_u1)
        job = snapshot.job_active()
        if job:
            raise LifecycleError(
                '%s. Finish or cancel it on the printer, then run %s again. '
                'Nothing was changed.' % (job[0].upper() + job[1:], command_name))
        if snapshot.source == 'none' and snapshot.klippy_running:
            if time.monotonic() - started > transient_timeout:
                raise LifecycleError(
                    'Klipper is running but printer activity cannot be verified%s. '
                    'Fix Moonraker or the Klipper API, or stop Klipper and run %s '
                    'again. Nothing was changed.' % (
                        ': %s' % snapshot.error if snapshot.error else '', command_name))
            if 'unverified' not in announced:
                announced.add('unverified')
                out('Waiting: Klipper is running but not answering yet...')
            time.sleep(2.0)
            continue
        busy = snapshot.transient_busy()
        if not busy:
            return snapshot
        if force:
            warn('%s; continuing because --force was given' % busy)
            return snapshot
        limit = transient_timeout
        if time.monotonic() - started > limit:
            raise LifecycleError(
                '%s for more than %d seconds. Wait until it finishes (or run '
                'FIRMWARE_RESTART if it is stuck), then run %s again. Nothing '
                'was changed.' % (busy[0].upper() + busy[1:], int(limit), command_name))
        if busy not in announced:
            announced.add(busy)
            out('Waiting: %s...' % busy)
        time.sleep(2.0)


def wait_ready(moonraker_url, klipper_dir, printer_cfg, timeout, is_u1=False):
    deadline = time.monotonic() + float(timeout)
    snapshot = PrinterSnapshot()
    while time.monotonic() < deadline:
        snapshot = printer_snapshot(moonraker_url, klipper_dir, printer_cfg,
                                    timeout=30.0 if is_u1 else 15.0, is_u1=is_u1)
        if snapshot.ready:
            return snapshot
        time.sleep(2.0)
    return snapshot


def send_gcode(moonraker_url, script, timeout=60.0):
    if not moonraker_url:
        return False, 'Moonraker is not configured'
    try:
        value = http_json(moonraker_url.rstrip('/') + '/printer/gcode/script',
                          timeout=timeout, data={'script': script})
    except urllib.error.HTTPError as exc:
        return False, http_error_text(exc)
    except Exception as exc:
        return False, str(exc)
    if 'error' in value:
        error = value['error']
        return False, str(error.get('message') if isinstance(error, dict) else error)
    return True, str(value.get('result', ''))


def request_klipper_restart(moonraker_url):
    if not moonraker_url:
        return False
    try:
        http_json(moonraker_url.rstrip('/') + '/printer/restart', timeout=20.0, data={})
        return True
    except Exception:
        return False


_INCLUDE_RE = re.compile(r'^\s*\[\s*include\s+([^\]]+)\]\s*(?:[#;].*)?$', re.IGNORECASE)
_RULE_RE = re.compile(r'^#{8,}\s*$')
_LABEL_RE = re.compile(r'^#\s*BMCU-Klipper\s*$', re.IGNORECASE)
_SAVE_RE = re.compile(r'^#\*# <-+ SAVE_CONFIG -+>')


def _decode_cfg(data):
    return data.decode('utf-8', 'surrogateescape')


def _encode_cfg(text):
    return text.encode('utf-8', 'surrogateescape')


def is_bmcu_include(line):
    match = _INCLUDE_RE.match(line.rstrip('\r\n'))
    if not match:
        return False
    target = match.group(1).strip().strip('"\'').replace('\\', '/')
    while target.startswith('./'):
        target = target[2:]
    target = target.lower()
    return target == 'bmcu' or target.startswith('bmcu/')


def strip_bmcu_includes(data):
    text = _decode_cfg(data)
    lines = text.splitlines(True)
    stripped = [line.strip() for line in lines]
    upper = [value.upper() for value in stripped]
    save_index = next((index for index, value in enumerate(stripped)
                       if _SAVE_RE.match(value)), len(lines))
    remove = set()
    index = 0
    while index < save_index:
        if upper[index] == INCLUDE_BEGIN.upper():
            end = None
            for probe in range(index + 1, save_index):
                if upper[probe] == INCLUDE_END.upper():
                    end = probe
                    break
                if upper[probe] == INCLUDE_BEGIN.upper():
                    break
                value = stripped[probe]
                if value and not (is_bmcu_include(value) or value.startswith('#')):
                    break
            if end is None:
                remove.add(index)
                probe = index + 1
                while probe < save_index and (
                        is_bmcu_include(stripped[probe]) or
                        _RULE_RE.match(stripped[probe])):
                    remove.add(probe)
                    probe += 1
            else:
                remove.update(range(index, end + 1))
            previous = index - 1
            if previous >= 0 and _LABEL_RE.match(stripped[previous]):
                remove.add(previous)
                previous -= 1
                if previous >= 0 and _RULE_RE.match(stripped[previous]):
                    remove.add(previous)
                    previous -= 1

            index = (end + 1) if end is not None else index + 1
            continue
        if upper[index] == INCLUDE_END.upper():
            remove.add(index)
        elif is_bmcu_include(stripped[index]):
            remove.add(index)
            previous = index - 1
            if previous >= 0 and _LABEL_RE.match(stripped[previous]):
                remove.add(previous)
                if previous - 1 >= 0 and _RULE_RE.match(stripped[previous - 1]):
                    remove.add(previous - 1)
        index += 1
    if not remove:
        return data, 0
    kept = [line for position, line in enumerate(lines) if position not in remove]
    return _encode_cfg(''.join(kept)), len(remove)


def add_bmcu_include(data):
    cleaned, _removed = strip_bmcu_includes(data)
    text = _decode_cfg(cleaned)
    newline = '\r\n' if '\r\n' in text and '\n' not in text.replace('\r\n', '') else '\n'
    block = newline.join((INCLUDE_BEGIN,) + INCLUDES + (INCLUDE_END,)) + newline
    lines = text.splitlines(True)
    save_index = next((index for index, line in enumerate(lines)
                       if _SAVE_RE.match(line.strip())), None)
    if save_index is None:
        head, tail = text, ''
    else:
        head = ''.join(lines[:save_index])
        tail = ''.join(lines[save_index:])
    if head and not head.endswith(('\n', '\r')):
        head += newline
    return _encode_cfg(head + block + tail)


def has_bmcu_include(data):
    text = _decode_cfg(data)
    return any(is_bmcu_include(line) or line.strip().upper() in (
        INCLUDE_BEGIN.upper(), INCLUDE_END.upper())
        for line in text.splitlines())


def config_mentions_bmcu_sections(config_dir, bmcu_dir):
    hits = []
    pattern = re.compile(r'^\s*\[\s*bmcu(?:_panel)?(?:\s[^\]]*)?\]', re.IGNORECASE | re.MULTILINE)
    for root, dirs, files in os.walk(config_dir):
        dirs[:] = [name for name in dirs
                   if not os.path.realpath(os.path.join(root, name)).startswith(
                       os.path.realpath(bmcu_dir))]
        for name in files:
            if not name.lower().endswith('.cfg'):
                continue
            path = os.path.join(root, name)
            data = read_optional(path, 4 * 1024 * 1024)
            if data and pattern.search(_decode_cfg(data)):
                hits.append(path)
    return sorted(hits)


def _looks_like_bmcu_module(path, name):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        return ('/bmcu/' in target or 'bmcu' in os.path.basename(target.rstrip('/')).lower())
    if name == 'bmcu_core':
        if not stat.S_ISDIR(info.st_mode):
            return False
        manager = os.path.join(path, 'manager.py')
        data = read_optional(manager, 4 * 1024 * 1024)
        return bool(data and b'BMCUManager' in data)
    data = read_optional(path, 1024 * 1024)
    return bool(data and any(token in data for token in MODULE_SIGNATURES.get(name, ())))


def _owned_by_other_installation(path, bmcu_dir):
    if not bmcu_dir or not os.path.islink(path):
        return False
    target = os.path.realpath(path)
    if inside(target, bmcu_dir) or not os.path.exists(target):
        return False
    marker = target
    while marker not in ('/', ''):
        if os.path.basename(marker) == 'runtime' and os.path.isfile(
                os.path.join(marker, '.managed-by-bmcu')):
            return True
        marker = os.path.dirname(marker)
    return False


def remove_module_links(extras_dir, backup=None, bmcu_dir=''):
    removed, foreign = [], []
    for name in MODULES:
        path = os.path.join(extras_dir, name)
        if not os.path.lexists(path):
            continue
        if (not _looks_like_bmcu_module(path, name) or
                _owned_by_other_installation(path, bmcu_dir)):
            foreign.append(path)
            continue
        if not os.path.islink(path) and backup is not None:
            backup.save_tree(path, 'klipper-extras-' + name)
        remove_path(path)
        removed.append(path)

    cache = os.path.join(extras_dir, '__pycache__')
    for pattern in ('bmcu.*.pyc', 'bmcu_panel.*.pyc'):
        for path in glob.glob(os.path.join(cache, pattern)):
            try:
                os.unlink(path)
            except OSError:
                pass
    try:
        os.rmdir(cache)
    except OSError:
        pass
    return removed, foreign


def other_installation_links(extras_dir, bmcu_dir):
    return [os.path.join(extras_dir, name) for name in MODULES
            if _owned_by_other_installation(os.path.join(extras_dir, name), bmcu_dir)]


def install_module_links(extras_dir, source_dir, uid=-1, gid=-1, backup=None):
    foreign = []
    for name in MODULES:
        path = os.path.join(extras_dir, name)
        if os.path.lexists(path) and not _looks_like_bmcu_module(path, name):
            foreign.append(path)
    if foreign:
        raise LifecycleError(
            'Klipper already contains a module with a BMCU name that does not '
            'belong to BMCU-Klipper: %s. Rename or remove it, then run the '
            'installer again.' % ', '.join(foreign))
    for name in MODULES:
        path = os.path.join(extras_dir, name)
        source = os.path.join(source_dir, name)
        if os.path.lexists(path) and not os.path.islink(path):
            if backup is not None:
                backup.save_tree(path, 'klipper-extras-' + name)
            remove_path(path)
        temporary = os.path.join(extras_dir, '.%s.bmcu-link-%d' % (name, os.getpid()))
        if os.path.lexists(temporary):
            os.unlink(temporary)
        os.symlink(source, temporary)
        try:
            os.lchown(temporary, uid, gid)
        except OSError:
            pass
        os.replace(temporary, path)
    fsync_dir(extras_dir)


def read_module_links(extras_dir):
    links = {}
    for name in MODULES:
        path = os.path.join(extras_dir, name)
        if os.path.islink(path):
            links[name] = os.readlink(path)
        elif os.path.lexists(path):
            links[name] = None
    return links


def restore_module_links(extras_dir, links, backup=None):
    for name in MODULES:
        path = os.path.join(extras_dir, name)
        target = links.get(name, False)
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.lexists(path) and target is not None:
            continue
        if target is None:
            saved = os.path.join(backup.path, 'klipper-extras-' + name) if backup else ''
            if saved and os.path.lexists(saved) and not os.path.lexists(path):
                if os.path.isdir(saved) and not os.path.islink(saved):
                    shutil.copytree(saved, path, symlinks=True)
                else:
                    shutil.copy2(saved, path, follow_symlinks=False)
        elif target:
            os.symlink(target, path)
    fsync_dir(extras_dir)


class Backup(object):
    def __init__(self, data_root, kind):
        self.root = os.path.join(data_root, BACKUP_ROOT_NAME)
        if not os.path.isdir(self.root):
            os.makedirs(self.root, 0o750)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        self.path = os.path.join(self.root, '%s-%s-%d' % (kind, stamp, os.getpid()))
        os.mkdir(self.path, 0o750)
        self.kind = kind
        self.files = {}
        self._write_manifest()

    def _write_manifest(self):
        manifest = {
            'product': PRODUCT, 'kind': self.kind,
            'created': time.strftime('%Y-%m-%d %H:%M:%S'),
            'files': self.files,
        }
        atomic_write(os.path.join(self.path, 'MANIFEST.json'),
                     json.dumps(manifest, indent=2, sort_keys=True) + '\n', 0o640)

    def save_file(self, source, name=None):
        if not os.path.isfile(source) or os.path.islink(source):
            return None
        name = name or os.path.basename(source)
        target = os.path.join(self.path, name)
        counter = 1
        while os.path.lexists(target):
            counter += 1
            target = os.path.join(self.path, '%s.%d' % (name, counter))
        shutil.copy2(source, target, follow_symlinks=False)
        self.files[os.path.basename(target)] = os.path.abspath(source)
        self._write_manifest()
        return target

    def save_bytes(self, name, data, original=''):
        target = os.path.join(self.path, name)
        atomic_write(target, data, 0o640)
        self.files[name] = original
        self._write_manifest()
        return target

    def save_tree(self, source, name):
        target = os.path.join(self.path, name)
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, target, symlinks=True)
        elif os.path.lexists(source):
            shutil.copy2(source, target, follow_symlinks=False)
        self.files[name] = os.path.abspath(source)
        self._write_manifest()
        return target

    def discard(self):
        shutil.rmtree(self.path, ignore_errors=True)
        try:
            os.rmdir(self.root)
        except OSError:
            pass


def prune_backups(data_root, kind, keep):
    root = os.path.join(data_root, BACKUP_ROOT_NAME)
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return 0
    removed = 0
    matching = [name for name in names if name.startswith(kind + '-') and
                os.path.isfile(os.path.join(root, name, 'MANIFEST.json'))]
    for name in matching[keep:]:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        removed += 1
    return removed


def prune_legacy_uninstall_backups(data_root):
    removed = 0
    try:
        names = os.listdir(data_root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(data_root, name)
        if not LEGACY_UNINSTALL_BACKUP_RE.match(name) or os.path.islink(path):
            continue
        marker = read_optional(os.path.join(path, 'UNINSTALL.txt'), 64 * 1024)
        if marker is None or not marker.startswith(b'BMCU-Klipper'):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def remove_stale_staging(data_root):
    removed = 0
    for pattern in ('.bmcu-install-*', '.bmcu-repair-*', '.bmcu-stage-*'):
        for path in glob.glob(os.path.join(data_root, pattern)):
            if os.path.isdir(path) and not os.path.islink(path):
                if os.path.isdir(os.path.join(path, 'runtime')) or not os.listdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
    return removed


def _systemd_word(value):
    value = str(value)
    if re.fullmatch(r'[A-Za-z0-9_./+:@=,-]+', value):
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def systemd_dropin_path(unit):
    unit = unit if unit.endswith('.service') else unit + '.service'
    return os.path.join(SYSTEMD_DIR, unit + '.d', SYSTEMD_DROPIN_NAME)


def systemd_dropin_bytes(target_python, bootstrap, metadata_path):
    text = (
        '# Managed by BMCU-Klipper.\n'
        '[Service]\n'
        'ExecStartPre=-%s -I %s --repair --no-processes --metadata %s --quiet\n' % (
            _systemd_word(target_python), _systemd_word(bootstrap),
            _systemd_word(metadata_path)))
    return text.encode('utf-8')


def managed_dropins(bmcu_dir=None):
    found = []
    for path in glob.glob(os.path.join(SYSTEMD_DIR, '*.d', SYSTEMD_DROPIN_NAME)):
        data = read_optional(path, 64 * 1024)
        if data is None or MANAGED_HEADER_RE.search(data[:1024]) is None:
            continue
        if bmcu_dir:
            text = data.decode('utf-8', 'replace')
            roots = {os.path.realpath(bmcu_dir), os.path.abspath(bmcu_dir)}
            if not any(root + '/' in text for root in roots):
                continue
        found.append(path)
    return sorted(found)


def remove_systemd_dropins(bmcu_dir):
    removed = []
    for path in managed_dropins(bmcu_dir):
        try:
            os.unlink(path)
            removed.append(path)
            try:
                os.rmdir(os.path.dirname(path))
            except OSError:
                pass
        except OSError as exc:
            warn('could not remove %s: %s' % (path, exc))
    if removed:
        program = optional_program('systemctl')
        if program:
            run([program, 'daemon-reload'], timeout=120, capture=True)
    return removed


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def u1_inline_block(system_python, runner_path, newline='\n'):
    lines = [
        U1_SERVICE_BEGIN,
        'bmcu_prepare_klipper_start()',
        '{',
        '  bmcu_runner=%s' % shell_quote(runner_path),
        '  if [ -f "$bmcu_runner" ]; then',
        '    %s -I "$bmcu_runner" || echo "BMCU-Klipper host bootstrap failed; Klipper starts without BMCU repair" >&2' % shell_quote(system_python),
        '  fi',
        '  return 0',
        '}',
        U1_SERVICE_END,
        '',
    ]
    return newline.join(lines)


def u1_unpatch_service(text):
    lines = text.splitlines(True)
    remove = set()
    index = 0
    while index < len(lines):
        value = lines[index].strip()
        if value == U1_SERVICE_BEGIN:
            end = None
            for probe in range(index + 1, len(lines)):
                probe_value = lines[probe].strip()
                if probe_value == U1_SERVICE_END:
                    end = probe
                    break
                if probe_value == U1_SERVICE_BEGIN:
                    break
            if end is None:
                remove.add(index)
                probe = index + 1
                while probe < len(lines):
                    remove.add(probe)
                    if lines[probe].strip() == '}':
                        break
                    probe += 1
                index = probe + 1
                continue
            remove.update(range(index, end + 1))
            if end + 1 < len(lines) and not lines[end + 1].strip():
                remove.add(end + 1)
            index = end + 1
            continue
        if value == U1_SERVICE_END or value == U1_SERVICE_CALL_MARKER:
            remove.add(index)
        elif U1_SERVICE_CALL_RE.match(lines[index].rstrip('\r\n')):
            remove.add(index)
        index += 1
    if not remove:
        return text, False
    return ''.join(line for position, line in enumerate(lines)
                   if position not in remove), True


_U1_LAUNCH_RE = re.compile(
    r'^(?P<indent>[ \t]*)start-stop-daemon\b(?=.*(?:[ \t]-S(?:[ \t]|$)|[ \t]--start(?:[ \t]|$)))')


def u1_patch_service(text, system_python, runner_path):
    cleaned, _changed = u1_unpatch_service(text)
    newline = '\r\n' if '\r\n' in cleaned else '\n'
    lines = cleaned.splitlines(True)
    insert_at = None
    for index, line in enumerate(lines):
        if re.match(r'^\s*log\s*\(\s*\)', line):
            insert_at = index
            break
    if insert_at is None:
        raise LifecycleError(
            'Snapmaker U1 %s has an unknown layout (log function not found); '
            'refusing to patch it' % U1_KLIPPER_SERVICE)
    patched = []
    launches = 0
    for position, line in enumerate(lines):
        if position == insert_at:
            patched.append(u1_inline_block(system_python, runner_path, newline) + newline)
        match = _U1_LAUNCH_RE.match(line)
        continued = position > 0 and lines[position - 1].rstrip('\r\n').endswith('\\')
        if match and not continued:
            indent = match.group('indent')
            patched.append(indent + U1_SERVICE_CALL_MARKER + newline)
            patched.append(indent + U1_SERVICE_CALL + newline)
            launches += 1
        patched.append(line)
    if insert_at >= len(lines):
        patched.append(u1_inline_block(system_python, runner_path, newline) + newline)
    if not launches:
        raise LifecycleError(
            'Snapmaker U1 %s has an unknown layout (no Klipper launch found); '
            'BMCU cannot be started automatically on this firmware' % U1_KLIPPER_SERVICE)
    result = ''.join(patched)
    power = re.search(
        r'(?m)^\s*["\']?\$LAVA_IO["\']?[ \t]+set[ \t]+.*(?:MAIN_MCU_POWER|HEAD_MCU_POWER)=1',
        result)
    call = result.find(U1_SERVICE_CALL_MARKER)
    if power is not None and (call < 0 or call <= power.start()):
        raise LifecycleError(
            'Snapmaker U1 serial repair would run before hardware power-up')
    return result


def shell_syntax_ok(text):
    shell = optional_program('sh') or '/bin/sh'
    descriptor, temporary = tempfile.mkstemp(prefix='.bmcu-sh-check-', suffix='.sh')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(text)
        code, _output = run([shell, '-n', temporary], timeout=30, capture=True)
        return code == 0
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def u1_runner_bytes(user, target_python, bootstrap, metadata_path):
    info = pwd.getpwnam(user)
    groups = supplementary_groups(user, info.pw_gid)
    text = '''#!/usr/bin/python3
import glob
import os
import stat
import sys

UID = %r
GID = %r
GROUPS = %r
PYTHON = %r
BOOTSTRAP = %r
METADATA = %r
HOME = %r


def report(message):
    try:
        sys.stderr.write('BMCU-Klipper: %%s\\n' %% message)
    except Exception:
        pass


if os.geteuid() != 0:
    report('U1 bootstrap runner must start as root')
    raise SystemExit(0)
if not (os.path.isfile(BOOTSTRAP) and os.path.isfile(METADATA) and
        os.path.isfile(PYTHON)):
    raise SystemExit(0)


def grant_serial_access():
    seen = set()
    matched = []
    for path in sorted(set(
            glob.glob('/dev/serial/by-id/*') +
            glob.glob('/dev/serial/by-path/*') +
            glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyCH343USB*'))):
        resolved = os.path.realpath(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        if not os.path.basename(resolved).startswith(('ttyUSB', 'ttyCH343USB')):
            continue
        try:
            if not stat.S_ISCHR(os.stat(resolved).st_mode):
                continue
            os.chown(resolved, 0, GID)
            os.chmod(resolved, 0o660)
            matched.append(resolved)
        except OSError as exc:
            report('serial permission repair failed for %%s: %%s' %% (resolved, exc))
    return matched


MATCHED_SERIAL = grant_serial_access()
try:
    os.setgroups(GROUPS)
    os.setgid(GID)
    os.setuid(UID)
except OSError as exc:
    report('cannot drop privileges: %%s' %% exc)
    raise SystemExit(0)
for serial_path in MATCHED_SERIAL:
    if not os.access(serial_path, os.R_OK | os.W_OK):
        report('serial device is not accessible by Klipper: %%s' %% serial_path)
environment = {
    'PATH': '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin',
    'HOME': HOME,
    'LANG': 'C',
    'LC_ALL': 'C',
    'PYTHONDONTWRITEBYTECODE': '1',
}
try:
    os.execve(
        PYTHON,
        [PYTHON, '-I', BOOTSTRAP, '--repair', '--metadata', METADATA, '--quiet'],
        environment)
except OSError as exc:
    report('cannot start host bootstrap: %%s' %% exc)
    raise SystemExit(0)
''' % (int(info.pw_uid), int(info.pw_gid), list(groups),
       os.path.realpath(target_python), os.path.realpath(bootstrap),
       os.path.realpath(metadata_path), info.pw_dir)
    return text.encode('utf-8')


def u1_serial_rule_bytes(group):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_.-]*$', str(group or '')):
        raise LifecycleError('unsafe serial group name: %s' % group)
    return (
        '# Managed by %s - %s\n' % (PRODUCT, U1_SERIAL_RULE_LABEL) +
        'SUBSYSTEM=="tty", KERNEL=="ttyUSB*", GROUP="%s", MODE="0660"\n' % group +
        'SUBSYSTEM=="tty", KERNEL=="ttyCH343USB*", GROUP="%s", MODE="0660"\n' % group
    ).encode('utf-8')


def _is_managed(path):
    data = read_optional(path, 256 * 1024)
    return bool(data is not None and
                (MANAGED_HEADER_RE.search(data[:2048]) is not None or
                 data.strip().startswith(PRODUCT.encode('ascii'))))


def grant_serial_access(gid):
    changed = []
    for path in sorted(set(glob.glob('/dev/serial/by-id/*') +
                           glob.glob('/dev/serial/by-path/*') +
                           glob.glob('/dev/ttyUSB*') +
                           glob.glob('/dev/ttyCH343USB*'))):
        resolved = os.path.realpath(path)
        if resolved in changed:
            continue
        if not os.path.basename(resolved).startswith(('ttyUSB', 'ttyCH343USB')):
            continue
        try:
            if not stat.S_ISCHR(os.stat(resolved).st_mode):
                continue
            os.chown(resolved, 0, int(gid))
            os.chmod(resolved, 0o660)
            changed.append(resolved)
        except OSError as exc:
            warn('serial permission repair failed for %s: %s' % (resolved, exc))
    return changed


def reload_udev():
    program = optional_program('udevadm')
    if program:
        run([program, 'control', '--reload-rules'], timeout=60, capture=True)


U1_MANAGED_FILES = (
    U1_KLIPPER_SERVICE, U1_RUNNER, U1_RUNNER_MARKER, U1_SERIAL_RULE,
    U1_BOOT_HOOK, U1_LEGACY_BOOT_HOOK, U1_PERSISTENCE_MARKER,
)


def capture_files(paths):
    saved = {}
    for path in paths:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            saved[path] = None
            continue
        if stat.S_ISREG(info.st_mode):
            saved[path] = (read_bytes(path), stat.S_IMODE(info.st_mode),
                           info.st_uid, info.st_gid)
    return saved


def restore_files(saved):
    for path, value in saved.items():
        if value is None:
            if os.path.isfile(path) and not os.path.islink(path):
                os.unlink(path)
            continue
        data, mode, uid, gid = value
        if read_optional(path) != data:
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent, 0o755)
            atomic_write(path, data, mode, uid, gid)


def u1_runner_has_managed_files():
    if os.path.lexists(U1_RUNNER_MARKER) and _is_managed(U1_RUNNER_MARKER):
        return True
    data = read_optional(U1_RUNNER, 256 * 1024)
    return bool(data and b'BMCU-Klipper' in data)


def install_u1_integration(user, group, target_python, bootstrap, metadata_path,
                           backup):
    if not os.path.isfile(U1_KLIPPER_SERVICE):
        raise LifecycleError('Snapmaker U1 Klipper service is missing: %s' % U1_KLIPPER_SERVICE)
    persistence_dir = os.path.dirname(U1_PERSISTENCE_MARKER)
    if not os.path.isdir(persistence_dir):
        raise LifecycleError('Snapmaker U1 persistence directory is missing: %s' %
                             persistence_dir)
    try:
        service_text = read_bytes(U1_KLIPPER_SERVICE).decode('utf-8')
    except UnicodeDecodeError:
        raise LifecycleError('%s is not UTF-8' % U1_KLIPPER_SERVICE)
    patched = u1_patch_service(service_text, trusted_program_path('python3'), U1_RUNNER)
    if not shell_syntax_ok(patched):
        raise LifecycleError('patched %s failed the shell syntax check' % U1_KLIPPER_SERVICE)
    backup.save_file(U1_KLIPPER_SERVICE, 'S60klipper.before-install')

    if not os.path.lexists(U1_PERSISTENCE_MARKER):
        descriptor = os.open(U1_PERSISTENCE_MARKER,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        fsync_dir(persistence_dir)

    if os.path.isdir('/etc/udev'):
        if os.path.lexists(U1_SERIAL_RULE) and not _is_managed(U1_SERIAL_RULE):
            warn('%s exists and is not managed by BMCU-Klipper; leaving it alone' %
                 U1_SERIAL_RULE)
        else:
            if not os.path.isdir(U1_SERIAL_RULE_DIR):
                os.makedirs(U1_SERIAL_RULE_DIR, 0o755)
            atomic_write(U1_SERIAL_RULE, u1_serial_rule_bytes(group), 0o644, 0, 0)
            reload_udev()
    grant_serial_access(grp.getgrnam(group).gr_gid)

    if os.path.lexists(U1_RUNNER_DIR) and (
            os.path.islink(U1_RUNNER_DIR) or not os.path.isdir(U1_RUNNER_DIR)):
        remove_path(U1_RUNNER_DIR)
    if not os.path.isdir(U1_RUNNER_DIR):
        os.mkdir(U1_RUNNER_DIR, 0o700)
    os.chown(U1_RUNNER_DIR, 0, 0)
    os.chmod(U1_RUNNER_DIR, 0o700)
    atomic_write(U1_RUNNER_MARKER, (PRODUCT + '\n').encode('ascii'), 0o600, 0, 0)
    atomic_write(U1_RUNNER, u1_runner_bytes(user, target_python, bootstrap, metadata_path),
                 0o700, 0, 0)

    if patched != service_text:
        atomic_write(U1_KLIPPER_SERVICE, patched.encode('utf-8'))
    for legacy in (U1_BOOT_HOOK, U1_LEGACY_BOOT_HOOK):
        if os.path.lexists(legacy) and _is_managed(legacy):
            backup.save_file(legacy)
            remove_path(legacy)


def remove_u1_integration(backup=None):
    removed, problems = [], []
    if os.path.isfile(U1_KLIPPER_SERVICE):
        try:
            data = read_bytes(U1_KLIPPER_SERVICE)
            text = data.decode('utf-8', 'surrogateescape')
            cleaned, changed = u1_unpatch_service(text)
            if changed:
                if backup is not None:
                    backup.save_file(U1_KLIPPER_SERVICE, 'S60klipper.before-uninstall')
                if not shell_syntax_ok(cleaned):
                    problems.append('%s would not pass the shell syntax check after '
                                    'removing BMCU lines; left unchanged' % U1_KLIPPER_SERVICE)
                else:
                    atomic_write(U1_KLIPPER_SERVICE, cleaned.encode('utf-8', 'surrogateescape'))
                    removed.append('BMCU launch hook in %s' % U1_KLIPPER_SERVICE)
        except Exception as exc:
            problems.append('cannot clean %s: %s' % (U1_KLIPPER_SERVICE, exc))
    if os.path.lexists(U1_RUNNER_DIR):
        try:
            if os.path.islink(U1_RUNNER_DIR) or not os.path.isdir(U1_RUNNER_DIR):
                problems.append('%s is unsafe and was left alone' % U1_RUNNER_DIR)
            elif u1_runner_has_managed_files():
                data = read_optional(U1_RUNNER, 256 * 1024)
                if data and b'BMCU-Klipper' in data:
                    remove_path(U1_RUNNER)
                if os.path.lexists(U1_RUNNER_MARKER) and _is_managed(U1_RUNNER_MARKER):
                    remove_path(U1_RUNNER_MARKER)
                try:
                    os.rmdir(U1_RUNNER_DIR)
                except OSError:
                    pass
                removed.append(U1_RUNNER_DIR)
            else:
                problems.append('%s does not contain managed BMCU-Klipper files; left alone' %
                                U1_RUNNER_DIR)
        except Exception as exc:
            problems.append('cannot remove %s: %s' % (U1_RUNNER_DIR, exc))
    if os.path.lexists(U1_SERIAL_RULE):
        if _is_managed(U1_SERIAL_RULE):
            try:
                remove_path(U1_SERIAL_RULE)
                removed.append(U1_SERIAL_RULE)
                reload_udev()
            except Exception as exc:
                problems.append('cannot remove %s: %s' % (U1_SERIAL_RULE, exc))
    for legacy in (U1_BOOT_HOOK, U1_LEGACY_BOOT_HOOK):
        if os.path.lexists(legacy) and _is_managed(legacy):
            try:
                remove_path(legacy)
                removed.append(legacy)
            except Exception as exc:
                problems.append('cannot remove %s: %s' % (legacy, exc))
    for path in glob.glob(os.path.join(U1_LOG_DIR, 'bmcu-*.log*')):
        try:
            os.unlink(path)
        except OSError:
            pass
    return removed, problems


def _head_index(name, record):
    try:
        head = int(record.get('head_index', -1))
    except (TypeError, ValueError, OverflowError):
        head = -1
    if 0 <= head <= 3:
        return head
    match = re.search(r'(\d+)$', str(name))
    if match and 0 <= int(match.group(1)) <= 3:
        return int(match.group(1))
    return -1


def _feeder_target(head, endpoint):
    module, channel = U1_FEEDER_MAP[head]
    if isinstance(endpoint, dict):
        module = str(endpoint.get('u1_feeder_module', module) or module).strip()
        try:
            channel = int(endpoint.get('u1_feeder_channel', channel))
        except (TypeError, ValueError, OverflowError):
            channel = U1_FEEDER_MAP[head][1]
    if module not in ('left', 'right') or channel not in (0, 1):
        module, channel = U1_FEEDER_MAP[head]
    return module, channel


def u1_offline_restore(config_dir, state, backup):
    changes, notes, loaded_heads = [], [], []
    if not isinstance(state, dict):
        return changes, notes, loaded_heads
    snap_dir = os.path.join(config_dir, 'snapmaker')
    endpoints = state.get('endpoints') if isinstance(state.get('endpoints'), dict) else {}
    ownership = state.get('u1_ownership') if isinstance(state.get('u1_ownership'), dict) else {}

    desired = {}
    for name, record in ownership.items():
        if not isinstance(record, dict):
            continue
        head = _head_index(name, record)
        if head < 0:
            continue
        held = bool(record.get('persistent_hold') or record.get('native_auto_override_active') or
                    record.get('generation_open'))
        route = str(record.get('route_state', 'EMPTY') or 'EMPTY').upper()
        if route != 'EMPTY':
            loaded_heads.append(head)
        if not held:
            continue
        if not record.get('baseline_captured'):
            notes.append('Head %d: the pre-BMCU stock auto-feed state is unknown; '
                         'it was left unchanged' % (head + 1))
            continue
        enabled = not bool(record.get('baseline_disabled', False))
        module, channel = _feeder_target(head, endpoints.get(name))
        desired[(module, channel)] = (enabled, head)

    for (module, channel), (enabled, head) in sorted(desired.items()):
        path = os.path.join(snap_dir, '%s_filament_feed.json' % module)
        config = read_json(path)
        if not isinstance(config, dict):
            notes.append('Head %d: stock feeder settings file %s is missing; '
                         'enable auto feed for this head in the printer menu' %
                         (head + 1, path))
            continue
        modes = config.get('auto_mode')
        if not isinstance(modes, list) or len(modes) != 2:
            notes.append('Head %d: %s has no valid auto_mode state; it was left '
                         'unchanged' % (head + 1, path))
            continue
        if bool(modes[channel]) == bool(enabled):
            continue
        backup.save_file(path, os.path.basename(path) + '.before-uninstall')
        modes = list(modes)
        modes[channel] = bool(enabled)
        config['auto_mode'] = modes
        atomic_write(path, json.dumps(config, indent=4) + '\n')
        changes.append('Head %d: stock feeder auto feed %s' %
                       (head + 1, 'enabled' if enabled else 'kept disabled (as before BMCU)'))

    session = state.get('print_session') if isinstance(state.get('print_session'), dict) else {}
    original = session.get('u1_original') if isinstance(session.get('u1_original'), dict) else {}
    map_backup = session.get('u1_map_backup') if isinstance(session.get('u1_map_backup'), dict) else {}
    used_backup = session.get('u1_used_backup') if isinstance(session.get('u1_used_backup'), dict) else {}
    end_backup = (session.get('u1_end_unload_backup')
                  if isinstance(session.get('u1_end_unload_backup'), dict) else {})
    if original or map_backup or used_backup or end_backup:
        path = os.path.join(snap_dir, 'print_task.json')
        config = read_json(path)
        if not isinstance(config, dict):
            notes.append('Snapmaker print map file %s is missing; the next print '
                         'job sets a new map' % path)
        else:
            before = json.dumps(config, sort_keys=True)
            if original:
                live = original.get('live') if isinstance(original.get('live'), dict) else {}
                reprint = original.get('reprint') if isinstance(original.get('reprint'), dict) else {}
                if not session.get('stock_reset_observed'):
                    for key, values in live.items():
                        if isinstance(values, list):
                            config[key] = values
                reprint_info = config.get('reprint_info')
                if isinstance(reprint_info, dict):
                    for key, values in reprint.items():
                        if isinstance(values, list):
                            reprint_info[key] = values
            else:
                def patch_list(key, length, updates, convert):
                    values = config.get(key)
                    if not isinstance(values, list) or len(values) != length:
                        return
                    for raw_index, raw_value in updates.items():
                        try:
                            position = int(raw_index)
                            values[position] = convert(raw_value)
                        except (TypeError, ValueError, IndexError):
                            continue
                patch_list('extruder_map_table', 32, map_backup,
                           lambda value: int(value) if 0 <= int(value) < 4 else 0)
                patch_list('extruders_used', 4, used_backup, bool)
                patch_list('end_unload_filament', 4, end_backup, bool)
            if json.dumps(config, sort_keys=True) != before:
                backup.save_file(path, 'print_task.json.before-uninstall')
                atomic_write(path, json.dumps(config, indent=4) + '\n')
                changes.append('Snapmaker print map restored to its pre-BMCU state')
    return changes, notes, sorted(set(loaded_heads))


CONFIG_DIR_PATTERNS = (
    '/home/*/printer_data/config',
    '/home/*/printer_*_data/config',
    '/home/*/*_data/config',
    '/home/*/klipper_config',
    '/root/printer_data/config',
    '/root/printer_*_data/config',
    '/root/klipper_config',
    '/usr/data/printer_data/config',
    '/usr/share/printer_data/config',
    '/data/printer_data/config',
    '/userdata/*/config',
    '/userdata/*/printer_data/config',
)


def installation_traces(config_dir):
    config_dir = os.path.abspath(config_dir)
    traces = []
    bmcu_dir = os.path.join(config_dir, 'bmcu')
    if os.path.lexists(bmcu_dir):
        traces.append('BMCU directory')
    printer_cfg = os.path.join(config_dir, 'printer.cfg')
    data = read_optional(printer_cfg)
    if data is not None and has_bmcu_include(data):
        traces.append('printer.cfg include')
    if os.path.lexists(os.path.join(config_dir, 'bmcu_state.json')):
        traces.append('BMCU state')
    return traces


def find_installations(extra_dirs=()):
    seen = set()
    found = []
    for pattern in tuple(extra_dirs) + CONFIG_DIR_PATTERNS:
        for path in glob.glob(pattern):
            if not os.path.isdir(path):
                continue
            key = os.path.realpath(path)
            if key in seen:
                continue
            seen.add(key)
            traces = installation_traces(path)
            if traces:
                found.append((os.path.abspath(path), traces))
    return found


def read_installation_metadata(bmcu_dir):
    for path in (os.path.join(bmcu_dir, 'runtime', 'INSTALLATION.json'),
                 os.path.join(bmcu_dir, 'INSTALLATION.json')):
        value = read_json(path, 1024 * 1024)
        if isinstance(value, dict) and value.get('product') in (None, PRODUCT):
            return value
    return None


def bmcu_state_file(config_dir, bmcu_dir):
    default = os.path.join(config_dir, 'bmcu_state.json')
    data = read_optional(os.path.join(bmcu_dir, 'bmcu.cfg'), 1024 * 1024)
    if data:
        match = re.search(r'(?m)^\s*state_file\s*[:=]\s*([^#;\r\n]+)',
                          data.decode('utf-8', 'replace'))
        if match:
            value = os.path.expanduser(match.group(1).strip())
            if os.path.isabs(value):
                return value
    return default


def transport_socket_dir(bmcu_dir):
    data = read_optional(os.path.join(bmcu_dir, 'bmcu.cfg'), 1024 * 1024)
    if data:
        match = re.search(r'(?m)^\s*transport_socket_dir\s*[:=]\s*([^#;\r\n]+)',
                          data.decode('utf-8', 'replace'))
        if match and os.path.isabs(match.group(1).strip()):
            return match.group(1).strip()
    return '/tmp/bmcu-transport'


def configured_serial_ports(bmcu_dir):
    data = read_optional(os.path.join(bmcu_dir, 'bmcu.cfg'), 1024 * 1024)
    ports = []
    if not data:
        return ports
    for line in data.decode('utf-8', 'replace').splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) in (2, 3) and parts[1].startswith('/dev/'):
            ports.append(parts[1])
    return sorted(set(ports))


def remove_socket_dir(path):
    if not path or not os.path.isdir(path) or os.path.islink(path):
        return False
    if not path.startswith(('/tmp/', '/run/', '/var/run/', '/dev/shm/')):
        return False
    allowed = re.compile(r'^(?:[A-Za-z0-9_.-]+\.(?:sock|ctl|lock|status|json|log)|'
                         r'[A-Za-z0-9_.-]+\.sock\.(?:ctl|lock)|u1-plans|\..*)$')
    for name in os.listdir(path):
        if not allowed.match(name):
            return False
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)
