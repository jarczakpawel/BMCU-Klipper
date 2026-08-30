#!/usr/bin/env python3

from __future__ import print_function

import argparse
import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import types

sys.dont_write_bytecode = True

PRODUCT = 'BMCU-Klipper'
VERSION = '1.0.0'
BEGIN = '# BEGIN BMCU-KLIPPER AUTO-INCLUDE'
END = '# END BMCU-KLIPPER AUTO-INCLUDE'
MODULES = ('bmcu.py', 'bmcu_core', 'bmcu_panel.py')
U1_LEGACY_BOOT_HOOK = '/etc/init.d/S59bmcu-klipper'
U1_KLIPPER_SERVICE = '/etc/init.d/S60klipper'
U1_BOOT_HOOK = '/etc/hooks/klipper.d/50-bmcu-klipper.sh'
U1_SERVICE_BEGIN = '# BEGIN BMCU-KLIPPER SERVICE-HOOKS'
U1_SERVICE_END = '# END BMCU-KLIPPER SERVICE-HOOKS'
U1_SERVICE_CALL_MARKER = '# BMCU-KLIPPER PREPARE IMMEDIATELY BEFORE KLIPPER PRIVILEGE DROP'
U1_SERVICE_CALL = 'bmcu_prepare_klipper_start || exit 1'
U1_RUNNER_DIR = '/oem/bmcu-klipper'
U1_RUNNER = '/oem/bmcu-klipper/run-host-bootstrap.py'
U1_RUNNER_MARKER = '/oem/bmcu-klipper/.managed-by-bmcu'
U1_SERIAL_RULE_DIR = '/etc/udev/rules.d'
U1_SERIAL_RULE = '/etc/udev/rules.d/99-bmcu-klipper.rules'
U1_SERIAL_RULE_MARKER = '# Managed by BMCU-Klipper 1.0.0 - Snapmaker U1 CH340 access'
U1_SERIAL_VENDOR = '1a86'
U1_SERIAL_PRODUCTS = ('5523', '7522', '7523', '7584', '55d4')
U1_PERSISTENCE_MARKER = '/oem/.debug'
MAX_FILE = 16 * 1024 * 1024
MAX_JSON = 1024 * 1024
MAX_RELEASE_BYTES = 64 * 1024 * 1024
UNINSTALL_BACKUP_RETENTION = 0
UNINSTALL_BACKUP_RE = re.compile(
    r'^bmcu-uninstalled-\d{8}-\d{6}-\d+$')
UNINSTALL_MARKER = 'BMCU-Klipper 1.0.0\n'
INCLUDE_RE = re.compile(
    r'^\s*\[\s*include\s+([^\]]+)\]\s*(?:#.*)?$', re.IGNORECASE)

class UninstallError(RuntimeError):
    pass

def _read_release_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UninstallError('cannot open package file %s: %s' % (path, exc))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UninstallError('package entry is not a regular file: %s' % path)
        if info.st_size > MAX_RELEASE_BYTES:
            raise UninstallError('package file is too large: %s' % path)
        chunks = []
        remaining = MAX_RELEASE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data) > MAX_RELEASE_BYTES:
            raise UninstallError('package file is too large: %s' % path)
        return data
    finally:
        os.close(descriptor)

def load_release_snapshot(package):
    raw_package = os.path.abspath(package)
    if os.path.islink(raw_package):
        raise UninstallError('package root must not be a symlink: %s' % raw_package)
    package = os.path.realpath(raw_package)
    if not os.path.isdir(package):
        raise UninstallError('package root is not a real directory: %s' % package)

    snapshot = {}
    total = 0
    for current, directories, files in os.walk(package, topdown=True, followlinks=False):
        if os.path.realpath(current) == package and '.git' in directories:
            directories.remove('.git')
        current_real = os.path.realpath(current)
        if current_real != package and not current_real.startswith(package + os.sep):
            raise UninstallError('package path escaped release root: %s' % current)
        for directory in list(directories):
            path = os.path.join(current, directory)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise UninstallError('package contains unsafe directory entry: %s' % path)
        for filename in files:
            path = os.path.join(current, filename)
            relative = os.path.relpath(path, package).replace(os.sep, '/')
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise UninstallError('package contains unsafe file entry: %s' % relative)
            data = _read_release_regular(path, MAX_RELEASE_BYTES)
            total += len(data)
            if total > MAX_RELEASE_BYTES:
                raise UninstallError('package is unexpectedly large')
            snapshot[relative] = data
    if not snapshot:
        raise UninstallError('package is empty')
    return snapshot

def load_package_module(name, snapshot, package):
    relative = 'scripts/%s.py' % name
    data = snapshot.get(relative)
    if data is None:
        raise UninstallError('package module is missing: %s' % relative)
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

def read_regular(path, limit=MAX_FILE):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    if nofollow:
        flags |= nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UninstallError(
            'required file is unavailable or unsafe: %s (%s)' % (path, exc))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UninstallError('refusing unsafe file: %s' % path)
        if info.st_size > limit:
            raise UninstallError('file is too large: %s' % path)
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
            raise UninstallError('file is too large: %s' % path)
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) !=
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
            raise UninstallError('file changed while it was being read: %s' % path)
        if len(data) != final.st_size:
            raise UninstallError(
                'file size changed while it was being read: %s' % path)
        return data, final
    finally:
        os.close(descriptor)

def directory_identity(path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise UninstallError('directory is unavailable: %s (%s)' % (path, exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UninstallError('path is not a real directory: %s' % path)
    return info.st_dev, info.st_ino

def assert_directory_identity(path, expected):
    if directory_identity(path) != expected:
        raise UninstallError('directory changed during uninstall: %s' % path)

def read_optional_json(path):
    if not os.path.lexists(path):
        return None
    data, _info = read_regular(path, MAX_JSON)
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UninstallError('invalid BMCU installation metadata: %s' % exc)
    if not isinstance(value, dict):
        raise UninstallError('BMCU installation metadata is not a JSON object')
    return value

def read_xattrs(path):
    values = {}
    if not hasattr(os, 'listxattr'):
        return values
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except (OSError, TypeError):
        return values
    for name in names:
        try:
            values[name] = os.getxattr(path, name, follow_symlinks=False)
        except (OSError, TypeError):
            pass
    return values

def _same_file_info(current, expected):
    return ((current.st_dev, current.st_ino, current.st_size,
             current.st_mtime_ns, stat.S_IMODE(current.st_mode),
             current.st_uid, current.st_gid) ==
            (expected.st_dev, expected.st_ino, expected.st_size,
             expected.st_mtime_ns, stat.S_IMODE(expected.st_mode),
             expected.st_uid, expected.st_gid))

def atomic_write(path, data, info, xattrs=None, expected=None):
    parent = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(
        prefix='.%s.' % os.path.basename(path), dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.chown(temporary, info.st_uid, info.st_gid)
        if xattrs and hasattr(os, 'setxattr'):
            for name, value in xattrs.items():
                try:
                    os.setxattr(temporary, name, value, follow_symlinks=False)
                except (OSError, TypeError):
                    pass
        current_data, current_info = read_regular(path)
        if not _same_file_info(current_info, info):
            raise UninstallError(
                'file changed before atomic replacement; refusing to overwrite: %s' %
                path)
        if expected is not None and current_data != expected:
            raise UninstallError(
                'file contents changed before atomic replacement; refusing to overwrite: %s' %
                path)
        os.replace(temporary, path)
        directory_fd = os.open(
            parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _written, written_info = read_regular(path)
        return written_info
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def unlink_regular_expected(path, expected_data, expected_info):
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    directory_fd = os.open(
        parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
        getattr(os, 'O_CLOEXEC', 0))
    descriptor = None
    try:
        flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
                 getattr(os, 'O_NOFOLLOW', 0))
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not _same_file_info(
                info, expected_info):
            raise UninstallError(
                'file changed before removal; refusing to unlink: %s' % path)
        chunks = []
        remaining = len(expected_data) + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if b''.join(chunks) != expected_data:
            raise UninstallError(
                'file contents changed before removal; refusing to unlink: %s' %
                path)
        final = os.fstat(descriptor)
        if not _same_file_info(final, expected_info):
            raise UninstallError(
                'file changed while validating removal: %s' % path)
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)

def restore_removed_regular(path, data, info, xattrs=None):
    if os.path.lexists(path):
        raise UninstallError('refusing to restore over an occupied path: %s' % path)
    write_new_bytes(path, data, stat.S_IMODE(info.st_mode))
    try:
        os.chown(path, info.st_uid, info.st_gid)
        os.chmod(path, stat.S_IMODE(info.st_mode))
        for name, value in (xattrs or {}).items():
            try:
                os.setxattr(path, name, value, follow_symlinks=False)
            except (OSError, TypeError):
                pass
        parent_fd = os.open(
            os.path.dirname(path),
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        restored, restored_info = read_regular(path, MAX_JSON)
        if (restored != data or
                stat.S_IMODE(restored_info.st_mode) != stat.S_IMODE(info.st_mode) or
                restored_info.st_uid != info.st_uid or
                restored_info.st_gid != info.st_gid):
            raise UninstallError('restored file metadata differs: %s' % path)
    except Exception:
        try:
            current, current_info = read_regular(path, MAX_JSON)
            if current == data:
                unlink_regular_expected(path, current, current_info)
        except Exception:
            pass
        raise

def query_json(url, timeout=3.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url, headers={'User-Agent': 'BMCU-Uninstaller/1.0.0'})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise UninstallError('Moonraker response is too large')
    return json.loads(raw.decode('utf-8'))

def printer_state(base):
    payload = query_json(base.rstrip('/') + '/printer/info')
    result = payload.get('result') if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise UninstallError('invalid Moonraker /printer/info response')
    return (str(result.get('state') or '').strip().lower(),
            str(result.get('state_message') or '').strip())

def printer_idle(base, assume_idle=False):
    payload = query_json(
        base.rstrip('/') +
        '/printer/objects/query?print_stats&pause_resume&idle_timeout')
    result = payload.get('result') if isinstance(payload, dict) else None
    status = result.get('status') if isinstance(result, dict) else None
    if not isinstance(status, dict):
        raise UninstallError('Moonraker did not return printer status objects')

    pause = status.get('pause_resume')
    if isinstance(pause, dict) and bool(pause.get('is_paused')):
        return False, 'paused'

    stats = status.get('print_stats')
    print_state = str(stats.get('state') or '').strip().lower() \
        if isinstance(stats, dict) else ''
    if print_state in ('printing', 'paused'):
        return False, print_state

    idle_timeout = status.get('idle_timeout')
    idle_state = str(idle_timeout.get('state') or '').strip().lower() \
        if isinstance(idle_timeout, dict) else ''
    if idle_state == 'printing':
        return False, idle_state

    if print_state in ('standby', 'complete', 'cancelled', 'error'):
        return True, print_state
    if idle_state in ('idle', 'ready'):
        return True, idle_state
    if assume_idle:
        return True, 'explicit --assume-idle'
    raise UninstallError(
        'printer activity could not be verified; stop all motion and heating '
        'before uninstalling')

def wait_ready(base, timeout=60):
    deadline = time.time() + timeout
    last = ('unknown', '')
    while time.time() < deadline:
        try:
            last = printer_state(base)
            if last[0] == 'ready':
                return True, last

        except Exception as exc:
            last = ('unavailable', str(exc))
        time.sleep(1)
    return False, last

TRUSTED_PATH = '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin'

def _trusted_root_path(path, require_executable=False, require_directory=False):
    if not path or not os.path.isabs(path):
        raise UninstallError('system path must be absolute: %s' % path)
    resolved = os.path.realpath(path)
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise UninstallError('system path is unavailable: %s (%s)' % (path, exc))
    if require_directory:
        if not stat.S_ISDIR(info.st_mode):
            raise UninstallError('system path is not a directory: %s' % path)
    else:
        if not stat.S_ISREG(info.st_mode):
            raise UninstallError('system program is not a regular file: %s' % path)
        if require_executable and not os.access(resolved, os.X_OK):
            raise UninstallError('system program is not executable: %s' % path)
    current = resolved
    paths = []
    while True:
        paths.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for current in reversed(paths):
        try:
            current_info = os.stat(current)
        except OSError as exc:
            raise UninstallError('cannot validate system path %s: %s' % (current, exc))
        if current_info.st_uid != 0 or stat.S_IMODE(current_info.st_mode) & 0o022:
            raise UninstallError(
                'refusing root control through non-root-owned or writable path: %s' % current)
    return resolved

def trusted_program(name):
    candidates = []
    for directory in TRUSTED_PATH.split(':'):
        candidate = os.path.join(directory, name)
        if candidate not in candidates and os.path.exists(candidate):
            candidates.append(candidate)
    errors = []
    for candidate in candidates:
        try:
            return _trusted_root_path(candidate, require_executable=True)
        except UninstallError as exc:
            errors.append(str(exc))
    if errors:
        raise UninstallError(errors[0])
    raise UninstallError('required system program is unavailable: %s' % name)

def safe_command_env(home='/root'):
    return {
        'PATH': TRUSTED_PATH,
        'HOME': home or '/',
        'LANG': 'C',
        'LC_ALL': 'C',
        'PYTHONDONTWRITEBYTECODE': '1',
    }

def service_command(service, action):
    backend = service.get('backend', 'none')
    name = service.get('name', '')
    script = service.get('script', '')
    directory = service.get('service_dir', '')
    if backend == 'systemd':
        unit = name if name.endswith('.service') else name + '.service'
        return [trusted_program('systemctl'), action, unit]
    if backend == 'sysv':
        return [_trusted_root_path(script, require_executable=True), action]
    if backend == 'openrc':
        init_script = script or os.path.join('/etc/init.d', name)
        _trusted_root_path(init_script, require_executable=True)
        return [trusted_program('rc-service'), name, action]
    if backend == 'supervisor':
        return [trusted_program('supervisorctl'), action, name]
    if backend == 'runit':
        target = directory or name
        if os.path.isabs(target):
            _trusted_root_path(target, require_directory=True)
        return [trusted_program('sv'), action, target]
    if backend == 's6':
        _trusted_root_path(directory, require_directory=True)
        return [trusted_program('s6-svc'), '-d' if action == 'stop' else '-u', directory]
    raise UninstallError('controllable Klipper service was not detected')

def run(command, check=True, timeout=60):
    try:
        result = subprocess.run(
            command, env=safe_command_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        if check:
            raise UninstallError(
                'command timed out after %ds: %s' %
                (timeout, ' '.join(command)))
        return 124
    if check and result.returncode:
        raise UninstallError(
            'command failed (%d): %s' %
            (result.returncode, ' '.join(command)))
    return result.returncode

def read_proc_cmdline(pid):
    try:
        with open('/proc/%d/cmdline' % int(pid), 'rb') as stream:
            raw = stream.read(1024 * 1024)
    except OSError:
        return []
    return [part.decode('utf-8', 'replace') for part in raw.split(b'\0') if part]

def process_is_python(pid, argv):
    names = []
    try:
        names.append(os.path.basename(os.path.realpath(
            os.readlink('/proc/%d/exe' % pid))).lower())
    except OSError:
        pass
    try:
        status, _info = read_regular('/proc/%d/status' % pid, 1024 * 1024)
        for line in status.decode('utf-8', 'replace').splitlines():
            if line.startswith('Name:'):
                names.append(line.split(':', 1)[1].strip().lower())
                break
    except UninstallError:
        pass
    if argv:
        names.append(os.path.basename(str(argv[0])).lower())
    return any(name.startswith('python') or name.startswith('pypy')
               for name in names)

def matching_klippy_pids(klipper_dir, printer_cfg):
    klippy_py = os.path.realpath(os.path.join(klipper_dir, 'klippy', 'klippy.py'))
    cfg_real = os.path.realpath(printer_cfg)
    values = []
    try:
        entries = os.listdir('/proc')
    except OSError:
        return values
    for item in entries:
        if not item.isdigit():
            continue
        pid = int(item)
        argv = read_proc_cmdline(pid)
        if not argv:
            continue
        if not process_is_python(pid, argv):
            continue
        try:
            cwd = os.readlink('/proc/%d/cwd' % pid)
        except OSError:
            cwd = '/'
        def resolved_arg(arg):
            return os.path.realpath(arg if os.path.isabs(arg) else os.path.join(cwd, arg))
        script_match = any(resolved_arg(arg) == klippy_py for arg in argv if 'klippy.py' in arg)
        cfg_match = any(resolved_arg(arg) == cfg_real for arg in argv if arg.endswith('.cfg'))
        if script_match and cfg_match:
            values.append(pid)
    return sorted(set(values))

def wait_pids_gone(pids, timeout):
    deadline = time.time() + float(timeout)
    remaining = list(pids)
    while time.time() < deadline:
        remaining = [pid for pid in remaining if os.path.exists('/proc/%d' % pid)]
        if not remaining:
            return []
        time.sleep(0.2)
    return [pid for pid in remaining if os.path.exists('/proc/%d' % pid)]

def wait_single_klippy(klipper_dir, printer_cfg, timeout=10.0):

    deadline = time.time() + float(timeout)
    pids = []
    while time.time() < deadline:
        pids = matching_klippy_pids(klipper_dir, printer_cfg)
        if len(pids) == 1 or len(pids) > 1:
            return pids
        time.sleep(0.1)
    return matching_klippy_pids(klipper_dir, printer_cfg)

def stop_service_strict(service, klipper_dir, printer_cfg):
    run(service_command(service, 'stop'))
    remaining = wait_pids_gone(matching_klippy_pids(klipper_dir, printer_cfg), 12.0)
    if remaining:
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        remaining = wait_pids_gone(remaining, 5.0)
    if remaining:
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        remaining = wait_pids_gone(remaining, 3.0)
    if remaining:
        raise UninstallError('Klipper did not stop completely; remaining PIDs: %s' % ', '.join(map(str, remaining)))

def start_service_strict(service, klipper_dir, printer_cfg):
    if matching_klippy_pids(klipper_dir, printer_cfg):
        raise UninstallError('refusing to start a second Klipper process')
    run(service_command(service, 'start'))

def managed_boot_hook(path):
    if not os.path.lexists(path):
        return False
    try:
        data, _info = read_regular(path, MAX_FILE)
    except (OSError, UninstallError):
        return False
    return b'Managed by BMCU-Klipper 1.0.0' in data[:1024]

def systemd_unit_name(service):
    name = str((service or {}).get('name') or 'klipper')
    return name if name.endswith('.service') else name + '.service'

def expected_systemd_hook(service):
    unit = systemd_unit_name(service)
    return os.path.join(
        '/etc/systemd/system', unit + '.d', 'bmcu-klipper.conf')

def hook_snapshot(metadata, service, platform_id):
    path = str((metadata or {}).get('boot_hook') or '')
    kind = str((metadata or {}).get('boot_hook_type') or '')
    if not path:

        return None
    if not kind:
        if path == U1_LEGACY_BOOT_HOOK:
            kind = 'snapmaker-u1-sysv'
        elif path == U1_BOOT_HOOK:
            kind = 'snapmaker-u1-service-hook'
    if kind == 'snapmaker-u1-sysv':
        if platform_id != 'snapmaker_u1':
            raise UninstallError('legacy U1 hook recorded for a non-U1 installation')
        if path != U1_LEGACY_BOOT_HOOK:
            raise UninstallError('unexpected recorded legacy U1 boot hook: %s' % path)
    elif kind == 'snapmaker-u1-service-hook':
        if platform_id != 'snapmaker_u1':
            raise UninstallError('U1 service hook recorded for a non-U1 installation')
        if path != U1_BOOT_HOOK:
            raise UninstallError('unexpected recorded Snapmaker U1 service hook: %s' % path)
    elif kind == 'systemd-dropin':
        if (service or {}).get('backend') != 'systemd':
            raise UninstallError('systemd hook recorded for a non-systemd service')
        expected = expected_systemd_hook(service)
        if os.path.realpath(path) != os.path.realpath(expected):
            raise UninstallError(
                'recorded systemd hook does not match the detected Klipper '
                'service: %s' % path)
        path = expected
    else:
        raise UninstallError('unexpected recorded BMCU boot hook type: %s' % kind)
    if not os.path.lexists(path):
        return (path, None, kind, None, None)
    if not managed_boot_hook(path):
        raise UninstallError('refusing to remove foreign BMCU boot hook: %s' % path)
    data, info = read_regular(path, MAX_FILE)
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise UninstallError(
            'managed BMCU boot hook has unsafe ownership or mode: %s' % path)
    return (path, data, kind, info, read_xattrs(path))

def write_new_bytes(path, data, mode=0o640):
    parent = os.path.dirname(path)
    if os.path.lexists(path):
        raise UninstallError('refusing to replace an existing backup file: %s' % path)
    fd, temporary = tempfile.mkstemp(
        prefix='.%s.' % os.path.basename(path), dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise UninstallError('backup path appeared during creation: %s' % path)
        except OSError as exc:
            raise UninstallError('cannot create backup without replacing an existing path: %s (%s)' % (path, exc))
        os.unlink(temporary)
        directory_fd = os.open(
            parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def restore_hook(snapshot):
    if not snapshot or snapshot[1] is None:
        return
    path, data, kind, original_info, original_xattrs = snapshot
    if os.path.lexists(path):
        current, _current_info = read_regular(path, MAX_FILE)
        if current == data:
            return
        raise UninstallError(
            'BMCU boot hook path became occupied during rollback: %s' % path)
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.%s.' % os.path.basename(path), dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(original_info.st_mode))
        os.chown(temporary, original_info.st_uid, original_info.st_gid)
        for name, value in (original_xattrs or {}).items():
            try:
                os.setxattr(temporary, name, value, follow_symlinks=False)
            except (OSError, TypeError):
                pass
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def managed_u1_serial_rule(data):
    if not isinstance(data, (bytes, bytearray)):
        return False
    try:
        text = bytes(data).decode('utf-8')
    except UnicodeDecodeError:
        return False
    return (text.startswith(U1_SERIAL_RULE_MARKER + '\n') and
            'MODE="0660"' in text and
            (('KERNEL=="ttyUSB*"' in text and 'KERNEL=="ttyCH343USB*"' in text) or
             ('ATTRS{idVendor}=="%s"' % U1_SERIAL_VENDOR in text and
              'ATTRS{idProduct}=="7523"' in text)))

def reload_u1_serial_rules():
    udevadm = shutil.which('udevadm')
    if not udevadm:
        return False
    run([udevadm, 'control', '--reload-rules'])
    return True

def reload_boot_manager(snapshot):
    if snapshot and len(snapshot) > 2 and snapshot[2] == 'systemd-dropin':
        run([trusted_program('systemctl'), 'daemon-reload'])

def _remove_u1_service_block(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise UninstallError('Snapmaker U1 S60klipper is not UTF-8')
    begin_count = len(re.findall(
        r'(?m)^' + re.escape(U1_SERVICE_BEGIN) + r'\r?$', text))
    end_count = len(re.findall(
        r'(?m)^' + re.escape(U1_SERVICE_END) + r'\r?$', text))
    if begin_count != end_count or begin_count > 1:
        raise UninstallError(
            'Snapmaker U1 S60klipper contains partial or duplicate BMCU markers')
    cleaned = text
    removed = False
    if begin_count:
        pattern = re.compile(
            r'(?ms)^' + re.escape(U1_SERVICE_BEGIN) + r'\r?\n.*?^' +
            re.escape(U1_SERVICE_END) + r'\r?\n(?:\r?\n)?')
        cleaned, count = pattern.subn('', cleaned, count=1)
        if count != 1:
            raise UninstallError('Snapmaker U1 BMCU service block is malformed')
        removed = True
    call_pattern = re.compile(
        r'(?m)^(?P<indent>[ \t]*)' + re.escape(U1_SERVICE_CALL_MARKER) +
        r'\r?\n(?P=indent)' + re.escape(U1_SERVICE_CALL) + r'\r?\n')
    cleaned, call_count = call_pattern.subn('', cleaned)
    removed = removed or bool(call_count)
    if (U1_SERVICE_CALL_MARKER in cleaned or
            re.search(r'(?m)^\s*' + re.escape(U1_SERVICE_CALL) + r'\s*$', cleaned)):
        raise UninstallError(
            'Snapmaker U1 S60klipper contains an incomplete BMCU launch call')
    return cleaned.encode('utf-8'), removed

def u1_integration_snapshot(platform_id, primary_boot_hook=None):
    if platform_id != 'snapmaker_u1':
        return None
    result = {
        'service_data': None,
        'service_cleaned': None,
        'service_info': None,
        'service_xattrs': None,
        'service_has_block': False,
        'legacy_data': None,
        'legacy_info': None,
        'legacy_xattrs': None,
        'runner_data': None,
        'runner_info': None,
        'runner_marker_data': None,
        'runner_marker_info': None,
        'runner_present': False,
        'serial_rule_data': None,
        'serial_rule_info': None,
        'serial_rule_xattrs': None,
        'serial_rule_present': False,
    }
    if os.path.lexists(U1_KLIPPER_SERVICE):
        data, info = read_regular(U1_KLIPPER_SERVICE)
        cleaned, has_block = _remove_u1_service_block(data)
        result.update(
            service_data=data, service_cleaned=cleaned,
            service_info=info,
            service_xattrs=read_xattrs(U1_KLIPPER_SERVICE),
            service_has_block=has_block)

    primary_path = str(primary_boot_hook or '')
    if (os.path.lexists(U1_LEGACY_BOOT_HOOK) and
            primary_path != U1_LEGACY_BOOT_HOOK):
        if not managed_boot_hook(U1_LEGACY_BOOT_HOOK):
            raise UninstallError(
                'legacy U1 BMCU hook path is occupied by a foreign file: %s' %
                U1_LEGACY_BOOT_HOOK)
        legacy_data, legacy_info = read_regular(U1_LEGACY_BOOT_HOOK, MAX_FILE)
        if (legacy_info.st_uid != 0 or
                stat.S_IMODE(legacy_info.st_mode) & 0o022):
            raise UninstallError(
                'legacy U1 BMCU hook has unsafe ownership or mode')
        result.update(
            legacy_data=legacy_data, legacy_info=legacy_info,
            legacy_xattrs=read_xattrs(U1_LEGACY_BOOT_HOOK))
    if os.path.lexists(U1_SERIAL_RULE):
        serial_rule_data, serial_rule_info = read_regular(
            U1_SERIAL_RULE, MAX_FILE)
        if not managed_u1_serial_rule(serial_rule_data):
            raise UninstallError(
                'Snapmaker U1 serial rule path is occupied by a foreign file: %s' %
                U1_SERIAL_RULE)
        if (serial_rule_info.st_uid != 0 or serial_rule_info.st_gid != 0 or
                stat.S_IMODE(serial_rule_info.st_mode) != 0o644):
            raise UninstallError(
                'Snapmaker U1 serial rule has unsafe ownership or mode')
        result.update(
            serial_rule_present=True, serial_rule_data=serial_rule_data,
            serial_rule_info=serial_rule_info,
            serial_rule_xattrs=read_xattrs(U1_SERIAL_RULE))
    if os.path.lexists(U1_RUNNER_DIR):
        if os.path.islink(U1_RUNNER_DIR) or not os.path.isdir(U1_RUNNER_DIR):
            raise UninstallError(
                'Snapmaker U1 BMCU runner path is unsafe: %s' % U1_RUNNER_DIR)
        directory_info = os.stat(U1_RUNNER_DIR)
        if (directory_info.st_uid != 0 or directory_info.st_gid != 0 or
                stat.S_IMODE(directory_info.st_mode) != 0o700):
            raise UninstallError(
                'Snapmaker U1 runner directory has unsafe ownership or mode')
        entries = set(os.listdir(U1_RUNNER_DIR))
        expected_entries = {
            os.path.basename(U1_RUNNER), os.path.basename(U1_RUNNER_MARKER)}
        if entries != expected_entries:
            raise UninstallError(
                'Snapmaker U1 runner directory contains unexpected files')
        marker_data, marker_info = read_regular(U1_RUNNER_MARKER, MAX_JSON)
        runner_data, runner_info = read_regular(U1_RUNNER, MAX_FILE)
        if marker_data.decode('utf-8', 'replace').strip() != 'BMCU-Klipper 1.0.0':
            raise UninstallError('Snapmaker U1 runner directory is not BMCU-owned')
        if (marker_info.st_uid != 0 or marker_info.st_gid != 0 or
                runner_info.st_uid != 0 or runner_info.st_gid != 0 or
                stat.S_IMODE(marker_info.st_mode) != 0o600 or
                stat.S_IMODE(runner_info.st_mode) != 0o700):
            raise UninstallError('Snapmaker U1 runner has unsafe ownership or mode')
        result.update(
            runner_present=True, runner_data=runner_data,
            runner_info=runner_info,
            runner_marker_data=marker_data,
            runner_marker_info=marker_info)
    return result

def _restore_u1_service(snapshot):
    if not snapshot.get('service_has_block'):
        return
    current, info = read_regular(U1_KLIPPER_SERVICE)
    original = snapshot.get('service_data')
    cleaned = snapshot.get('service_cleaned')
    if current == original:
        return
    if current != cleaned:
        raise UninstallError(
            'Snapmaker U1 S60klipper changed during rollback; refusing to overwrite it')
    atomic_write(
        U1_KLIPPER_SERVICE, original, info,
        read_xattrs(U1_KLIPPER_SERVICE), expected=current)

def _restore_u1_legacy(snapshot):
    legacy = snapshot.get('legacy_data')
    if legacy is None:
        return
    if os.path.lexists(U1_LEGACY_BOOT_HOOK):
        current, _current_info = read_regular(U1_LEGACY_BOOT_HOOK, MAX_FILE)
        if current == legacy:
            return
        raise UninstallError(
            'legacy U1 hook path became occupied during rollback')
    parent = os.path.dirname(U1_LEGACY_BOOT_HOOK)
    fd, temporary = tempfile.mkstemp(
        prefix='.S59bmcu-klipper.', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(legacy)
            stream.flush()
            os.fsync(stream.fileno())
        info = snapshot.get('legacy_info')
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.chown(temporary, info.st_uid, info.st_gid)
        for name, value in (snapshot.get('legacy_xattrs') or {}).items():
            try:
                os.setxattr(temporary, name, value, follow_symlinks=False)
            except (OSError, TypeError):
                pass
        os.replace(temporary, U1_LEGACY_BOOT_HOOK)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _restore_u1_runner(snapshot):
    if not snapshot.get('runner_present'):
        return
    if os.path.lexists(U1_RUNNER_DIR):
        if os.path.islink(U1_RUNNER_DIR) or not os.path.isdir(U1_RUNNER_DIR):
            raise UninstallError('cannot restore occupied U1 runner path')
        entries = set(os.listdir(U1_RUNNER_DIR))
        allowed = {os.path.basename(U1_RUNNER),
                   os.path.basename(U1_RUNNER_MARKER)}
        if not entries.issubset(allowed):
            raise UninstallError(
                'U1 runner directory contains foreign files during rollback')
    else:
        os.mkdir(U1_RUNNER_DIR, 0o700)
    os.chown(U1_RUNNER_DIR, 0, 0)
    os.chmod(U1_RUNNER_DIR, 0o700)
    for target, data, mode in (
            (U1_RUNNER_MARKER, snapshot.get('runner_marker_data'), 0o600),
            (U1_RUNNER, snapshot.get('runner_data'), 0o700)):
        if os.path.lexists(target):
            current, current_info = read_regular(target, MAX_FILE)
            if current != data:
                raise UninstallError(
                    'U1 runner file changed during rollback: %s' % target)
            if (current_info.st_uid != 0 or current_info.st_gid != 0 or
                    stat.S_IMODE(current_info.st_mode) != mode):
                os.chown(target, 0, 0)
                os.chmod(target, mode)
            continue
        fd, temporary = tempfile.mkstemp(
            prefix='.%s.' % os.path.basename(target), dir=U1_RUNNER_DIR)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.chown(temporary, 0, 0)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    directory_fd = os.open(U1_RUNNER_DIR, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

def _restore_u1_serial_rule(snapshot):
    if not snapshot.get('serial_rule_present'):
        return
    data = snapshot.get('serial_rule_data')
    if os.path.lexists(U1_SERIAL_RULE):
        current, current_info = read_regular(U1_SERIAL_RULE, MAX_FILE)
        if current != data:
            raise UninstallError(
                'Snapmaker U1 serial rule changed during rollback')
        if (current_info.st_uid != 0 or current_info.st_gid != 0 or
                stat.S_IMODE(current_info.st_mode) != 0o644):
            os.chown(U1_SERIAL_RULE, 0, 0)
            os.chmod(U1_SERIAL_RULE, 0o644)
        return
    os.makedirs(U1_SERIAL_RULE_DIR, mode=0o755, exist_ok=True)
    info = snapshot.get('serial_rule_info')
    fd, temporary = tempfile.mkstemp(
        prefix='.99-bmcu-klipper.', dir=U1_SERIAL_RULE_DIR)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.chown(temporary, info.st_uid, info.st_gid)
        for name, value in (snapshot.get('serial_rule_xattrs') or {}).items():
            try:
                os.setxattr(temporary, name, value, follow_symlinks=False)
            except (OSError, TypeError):
                pass
        os.replace(temporary, U1_SERIAL_RULE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    reload_u1_serial_rules()

def restore_u1_integration(snapshot):
    if not snapshot:
        return
    _restore_u1_service(snapshot)
    _restore_u1_legacy(snapshot)
    _restore_u1_runner(snapshot)
    _restore_u1_serial_rule(snapshot)

def remove_u1_integration(snapshot):
    if not snapshot:
        return False

    if snapshot.get('service_has_block'):
        current, _current_info = read_regular(U1_KLIPPER_SERVICE)
        if current != snapshot.get('service_data'):
            raise UninstallError(
                'managed U1 S60klipper changed before removal')
    legacy = snapshot.get('legacy_data')
    if legacy is not None and os.path.lexists(U1_LEGACY_BOOT_HOOK):
        current_legacy, _legacy_info = read_regular(
            U1_LEGACY_BOOT_HOOK, MAX_FILE)
        if current_legacy != legacy:
            raise UninstallError(
                'legacy U1 BMCU hook was modified before removal')
    if snapshot.get('runner_present'):
        if (os.path.islink(U1_RUNNER_DIR) or
                not os.path.isdir(U1_RUNNER_DIR)):
            raise UninstallError('Snapmaker U1 runner path changed before removal')
        entries = set(os.listdir(U1_RUNNER_DIR))
        expected_entries = {
            os.path.basename(U1_RUNNER), os.path.basename(U1_RUNNER_MARKER)}
        if entries != expected_entries:
            raise UninstallError(
                'Snapmaker U1 runner directory changed before removal')
        current_marker, _marker_info = read_regular(U1_RUNNER_MARKER, MAX_JSON)
        current_runner, _runner_info = read_regular(U1_RUNNER, MAX_FILE)
        if (current_marker != snapshot.get('runner_marker_data') or
                current_runner != snapshot.get('runner_data')):
            raise UninstallError('Snapmaker U1 runner changed before removal')

    if snapshot.get('serial_rule_present'):
        current_rule, _current_rule_info = read_regular(
            U1_SERIAL_RULE, MAX_FILE)
        if current_rule != snapshot.get('serial_rule_data'):
            raise UninstallError(
                'Snapmaker U1 serial rule changed before removal')

    service_removed = False
    legacy_removed = False
    runner_removed = False
    serial_rule_removed = False
    try:
        if snapshot.get('service_has_block'):
            current, info = read_regular(U1_KLIPPER_SERVICE)
            atomic_write(
                U1_KLIPPER_SERVICE, snapshot.get('service_cleaned'), info,
                read_xattrs(U1_KLIPPER_SERVICE), expected=current)
            service_removed = True
        if legacy is not None and os.path.lexists(U1_LEGACY_BOOT_HOOK):
            unlink_regular_expected(
                U1_LEGACY_BOOT_HOOK, legacy,
                snapshot.get('legacy_info'))
            legacy_removed = True
        if snapshot.get('runner_present'):
            unlink_regular_expected(
                U1_RUNNER, snapshot.get('runner_data'),
                snapshot.get('runner_info'))
            unlink_regular_expected(
                U1_RUNNER_MARKER, snapshot.get('runner_marker_data'),
                snapshot.get('runner_marker_info'))
            os.rmdir(U1_RUNNER_DIR)
            runner_removed = True
        if snapshot.get('serial_rule_present'):
            unlink_regular_expected(
                U1_SERIAL_RULE, snapshot.get('serial_rule_data'),
                snapshot.get('serial_rule_info'))
            serial_rule_removed = True
            reload_u1_serial_rules()
        return bool(service_removed or legacy_removed or runner_removed or
                    serial_rule_removed)
    except Exception as original_error:
        rollback_errors = []
        try:
            if service_removed:
                _restore_u1_service(snapshot)
        except Exception as exc:
            rollback_errors.append('S60klipper restore: %s' % exc)
        try:
            if legacy_removed:
                _restore_u1_legacy(snapshot)
        except Exception as exc:
            rollback_errors.append('legacy hook restore: %s' % exc)
        try:
            if snapshot.get('runner_present'):
                _restore_u1_runner(snapshot)
        except Exception as exc:
            rollback_errors.append('runner restore: %s' % exc)
        try:
            if serial_rule_removed:
                _restore_u1_serial_rule(snapshot)
        except Exception as exc:
            rollback_errors.append('serial rule restore: %s' % exc)
        if rollback_errors:
            raise UninstallError(
                'Snapmaker U1 integration removal failed and local rollback '
                'was incomplete: %s; rollback: %s' %
                (original_error, '; '.join(rollback_errors)))
        raise UninstallError(str(original_error))

def acquire_lock(printer_cfg):
    directory = os.path.dirname(os.path.realpath(printer_cfg))
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
             getattr(os, 'O_DIRECTORY', 0))
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise UninstallError(
            'cannot lock printer config directory %s: %s' % (directory, exc))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        raise UninstallError(
            'another BMCU install or uninstall is already running')

def inside(path, root):
    try:
        return os.path.commonpath(
            (os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False

def include_is_bmcu(line, config_dir, bmcu_dir):
    stripped = line.rstrip('\r\n')
    match = INCLUDE_RE.match(stripped)
    if not match:
        return False
    target = match.group(1).strip().strip('"\'').replace('\\', '/')
    while target.startswith('./'):
        target = target[2:]
    if target.lower() == 'bmcu' or target.lower().startswith('bmcu/'):
        return True
    if os.path.isabs(target):
        wildcard = min(
            [index for index in (target.find('*'), target.find('?'), target.find('['))
             if index >= 0] or [len(target)])
        fixed = target[:wildcard].rstrip('/')
        return bool(fixed) and inside(fixed, bmcu_dir)
    absolute = os.path.join(config_dir, target)
    wildcard = min(
        [index for index in (absolute.find('*'), absolute.find('?'), absolute.find('['))
         if index >= 0] or [len(absolute)])
    fixed = absolute[:wildcard].rstrip('/')
    return bool(fixed) and inside(fixed, bmcu_dir)

def clean_printer_cfg(original, config_dir, bmcu_dir):
    try:
        text = original.decode('utf-8')
    except UnicodeDecodeError:
        raise UninstallError('printer.cfg is not UTF-8')
    lines = text.splitlines(keepends=True)
    remove = set()
    begin_indexes = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        normalized = stripped.upper()
        if normalized == BEGIN.upper():
            begin_indexes.append(index)
            remove.add(index)
        elif normalized == END.upper() or include_is_bmcu(
                line, config_dir, bmcu_dir):
            remove.add(index)
        if normalized == '# BMCU-KLIPPER':
            remove.add(index)
    for index in begin_indexes:
        previous = index - 1
        if previous >= 0 and lines[previous].strip().upper() == '# BMCU-KLIPPER':
            remove.add(previous)
            previous -= 1
        if previous >= 0 and lines[previous].strip() == '################################':
            remove.add(previous)
        following = index + 1
        if (following < len(lines) and
                lines[following].strip() == '################################' and
                following + 1 < len(lines) and
                include_is_bmcu(lines[following + 1], config_dir, bmcu_dir)):
            remove.add(following)
    cleaned = ''.join(
        line for index, line in enumerate(lines) if index not in remove)
    return cleaned.encode('utf-8'), len(remove)

def add_bmcu_include(original, config_dir, bmcu_dir):
    cleaned, _removed = clean_printer_cfg(original, config_dir, bmcu_dir)
    newline = (b'\r\n' if b'\r\n' in cleaned and
               b'\n' not in cleaned.replace(b'\r\n', b'') else b'\n')
    lines = (BEGIN,) + (
        '[include bmcu/bmcu.cfg]',
        '[include bmcu/bmcu_macros.cfg]',
        '[include bmcu/bmcu_panel.cfg]',
    ) + (END,)
    block = newline.join(line.encode('utf-8') for line in lines) + newline
    marker = re.search(
        br'(?m)^#\*# <---------------------- SAVE_CONFIG ---------------------->',
        cleaned)
    index = marker.start() if marker else len(cleaned)
    if index and cleaned[index - 1:index] not in (b'\n', b'\r'):
        block = newline + block
    return cleaned[:index] + block + cleaned[index:]

def parser():
    value = argparse.ArgumentParser(prog='BMCU-Klipper-1.0.0 uninstaller')
    value.add_argument('--klipper-dir', default='')
    value.add_argument('--config-dir', default='')
    value.add_argument('--printer-cfg', default='')
    value.add_argument('--python', default='')
    value.add_argument('--user', default='')
    value.add_argument('--moonraker-url', default='')
    value.add_argument('--moonraker-conf', default='')
    value.add_argument('--service-backend', default='')
    value.add_argument('--service-name', default='')
    value.add_argument('--assume-idle', action='store_true', help=argparse.SUPPRESS)
    value.add_argument('--host-recovery', action='store_true', help=argparse.SUPPRESS)
    value.add_argument('--confirm-paths-empty', action='store_true', help=argparse.SUPPRESS)
    return value

def discover(platform, args):
    values = ['discover', '--format', 'json', '--non-interactive']
    for option, value in (
        ('--klipper-dir', args.klipper_dir),
        ('--config-dir', args.config_dir),
        ('--printer-cfg', args.printer_cfg),
        ('--python', args.python),
        ('--user', args.user),
        ('--moonraker-url', args.moonraker_url),
        ('--moonraker-conf', args.moonraker_conf),
        ('--service-backend', args.service_backend),
        ('--service-name', args.service_name),
    ):
        if value:
            values.extend([option, str(value)])
    try:
        return platform.build_plan(
            platform.build_parser().parse_args(values)).as_dict()
    except RuntimeError as exc:
        raise UninstallError(str(exc))

def normalized_service(service):
    value = service or {}
    return {
        'backend': str(value.get('backend') or ''),
        'name': str(value.get('name') or ''),
        'script': os.path.realpath(str(value.get('script') or ''))
                  if value.get('script') else '',
        'service_dir': os.path.realpath(str(value.get('service_dir') or ''))
                       if value.get('service_dir') else '',
    }

def validate_metadata(metadata, config_dir, printer_cfg, klipper_dir, service=None, target_user=None, target_python=None):
    if metadata is None:
        return
    product = metadata.get('product')
    version = metadata.get('version')
    if product not in (None, PRODUCT):
        raise UninstallError('installation metadata belongs to another product')
    if version not in (None, VERSION):
        raise UninstallError('installation metadata belongs to another version')
    for key, detected in (
        ('config_dir', config_dir),
        ('printer_cfg', printer_cfg),
        ('klipper_dir', klipper_dir),
        ('python', target_python),
    ):
        recorded = metadata.get(key)
        if recorded and detected and os.path.realpath(str(recorded)) != os.path.realpath(str(detected)):
            raise UninstallError(
                'detected %s differs from BMCU installation metadata' % key)
    recorded_user = metadata.get('user')
    if recorded_user and target_user and str(recorded_user) != str(target_user):
        raise UninstallError('detected Klipper user differs from BMCU installation metadata')
    recorded_service = metadata.get('service')
    if recorded_service is not None:
        if not isinstance(recorded_service, dict):
            raise UninstallError('BMCU service metadata is invalid')
        if normalized_service(recorded_service) != normalized_service(service):
            raise UninstallError(
                'detected Klipper service differs from BMCU installation metadata')

def validate_root_run_installation(plan):
    target_user = str(plan.get('install_user') or '')
    try:
        uid = pwd.getpwnam(target_user).pw_uid
    except KeyError:
        raise UninstallError('detected Klipper user does not exist: %s' % target_user)
    if int(uid) != 0:
        return
    paths = (
        (plan['klipper_dir'], True),
        (plan['config_dir'], True),
        (plan['printer_cfg'], False),
        (plan['python'], False),
    )
    for path, is_directory in paths:
        resolved = os.path.realpath(path)
        try:
            info = os.stat(resolved)
        except OSError as exc:
            raise UninstallError(
                'root-run Klipper path is unavailable: %s (%s)' %
                (path, exc))
        if is_directory and not stat.S_ISDIR(info.st_mode):
            raise UninstallError('root-run Klipper path is not a directory: %s' % path)
        if not is_directory and not stat.S_ISREG(info.st_mode):
            raise UninstallError('root-run Klipper path is not a regular file: %s' % path)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise UninstallError(
                'root-run Klipper requires root-owned, non-writable paths; '
                'refusing unsafe path: %s' % path)

def validate_ownership(bmcu_dir, runtime, metadata):
    if os.path.islink(bmcu_dir) or not os.path.isdir(bmcu_dir):
        raise UninstallError('BMCU directory is missing or unsafe: %s' % bmcu_dir)
    owned = False
    for marker in (
        os.path.join(runtime, '.managed-by-bmcu'),
        os.path.join(bmcu_dir, '.managed-by-bmcu'),
    ):
        if not os.path.lexists(marker):
            continue
        data, _info = read_regular(marker, MAX_JSON)
        if data.decode('utf-8', 'replace').strip() != 'BMCU-Klipper 1.0.0':
            raise UninstallError('unexpected BMCU ownership marker: %s' % marker)
        owned = True
    if metadata is not None:
        owned = True
    if not owned:
        raise UninstallError(
            'BMCU directory has no BMCU-Klipper 1.0.0 ownership marker')

def module_snapshot(klipper_dir, bmcu_dir):
    extras = os.path.join(klipper_dir, 'klippy', 'extras')
    snapshot = {}
    for name in MODULES:
        path = os.path.join(extras, name)
        if not os.path.lexists(path):
            snapshot[path] = None
            continue
        if not os.path.islink(path):
            raise UninstallError(
                'refusing to remove non-symlink Klipper module: %s' % path)
        raw_target = os.readlink(path)
        resolved = os.path.realpath(os.path.join(os.path.dirname(path), raw_target))
        if not inside(resolved, bmcu_dir):
            raise UninstallError('foreign Klipper module at %s' % path)
        info = os.lstat(path)
        snapshot[path] = (raw_target, info)
    return snapshot

def assert_module_snapshot(snapshot):
    for path, record in snapshot.items():
        if record is None:
            if os.path.lexists(path):
                raise UninstallError(
                    'Klipper module path changed during uninstall: %s' % path)
            continue
        try:
            info = os.lstat(path)
        except OSError:
            raise UninstallError(
                'Klipper module link disappeared during uninstall: %s' % path)
        if (not stat.S_ISLNK(info.st_mode) or
                os.readlink(path) != record[0] or
                (info.st_dev, info.st_ino, info.st_uid, info.st_gid) !=
                (record[1].st_dev, record[1].st_ino,
                 record[1].st_uid, record[1].st_gid)):
            raise UninstallError(
                'Klipper module link changed during uninstall: %s' % path)

def unlink_symlink_expected(path, record):
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    directory_fd = os.open(
        parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
        getattr(os, 'O_CLOEXEC', 0))
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        expected_info = record[1]
        if (not stat.S_ISLNK(info.st_mode) or
                (info.st_dev, info.st_ino, info.st_uid, info.st_gid) !=
                (expected_info.st_dev, expected_info.st_ino,
                 expected_info.st_uid, expected_info.st_gid) or
                os.readlink(name, dir_fd=directory_fd) != record[0]):
            raise UninstallError(
                'Klipper module link changed before removal: %s' % path)
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

def restore_symlink(path, record):
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    directory_fd = os.open(
        parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
        getattr(os, 'O_CLOEXEC', 0))
    try:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise UninstallError(
                'cannot restore occupied module path: %s' % path)
        os.symlink(record[0], name, dir_fd=directory_fd)
        try:
            os.chown(
                name, record[1].st_uid, record[1].st_gid,
                dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            pass
        os.fsync(directory_fd)
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISLNK(info.st_mode) or
                os.readlink(name, dir_fd=directory_fd) != record[0]):
            raise UninstallError(
                'restored Klipper module link is inconsistent: %s' % path)
    finally:
        os.close(directory_fd)

def validate_state(state_file):
    if not os.path.lexists(state_file):
        return False
    info = os.lstat(state_file)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UninstallError('BMCU state path is unsafe: %s' % state_file)
    return True

def managed_uninstall_backup(path):

    name = os.path.basename(path)
    if not UNINSTALL_BACKUP_RE.fullmatch(name):
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        return False
    marker = os.path.join(path, 'UNINSTALL.txt')
    printer_cfg = os.path.join(path, 'printer.cfg.before-uninstall')
    bmcu = os.path.join(path, 'bmcu')
    try:
        marker_data, marker_info = read_regular(marker, 64 * 1024)
        cfg_info = os.lstat(printer_cfg)
        bmcu_info = os.lstat(bmcu)
    except (OSError, UninstallError):
        return False
    if not marker_data.startswith(UNINSTALL_MARKER.encode('utf-8')):
        return False
    if marker_info.st_uid != 0 or stat.S_IMODE(marker_info.st_mode) & 0o022:
        return False
    if stat.S_ISLNK(cfg_info.st_mode) or not stat.S_ISREG(cfg_info.st_mode):
        return False
    if cfg_info.st_uid != 0 or stat.S_IMODE(cfg_info.st_mode) & 0o022:
        return False
    if stat.S_ISLNK(bmcu_info.st_mode) or not stat.S_ISDIR(bmcu_info.st_mode):
        return False
    return True

def orphan_boot_hook_snapshot(service, platform_id):
    if platform_id == 'snapmaker_u1':
        path = U1_BOOT_HOOK
        kind = 'snapmaker-u1-service-hook'
    elif (service or {}).get('backend') == 'systemd':
        path = expected_systemd_hook(service)
        kind = 'systemd-dropin'
    else:
        return None
    if not os.path.lexists(path):
        return (path, None, kind, None, None)
    if not managed_boot_hook(path):
        raise UninstallError('refusing to remove foreign BMCU boot hook: %s' % path)
    data, info = read_regular(path, MAX_FILE)
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise UninstallError('managed BMCU boot hook has unsafe ownership or mode: %s' % path)
    return (path, data, kind, info, read_xattrs(path))

def cleanup_missing_tree(plan, service, config_dir, printer_cfg, klipper_dir,
                         moonraker, bmcu_dir):

    if os.path.lexists(bmcu_dir):
        raise UninstallError('BMCU directory appeared during residual cleanup: %s' % bmcu_dir)
    transaction_lock = acquire_lock(printer_cfg)
    original_cfg, cfg_info = read_regular(printer_cfg)
    cfg_xattrs = read_xattrs(printer_cfg)
    cleaned_cfg, removed_references = clean_printer_cfg(
        original_cfg, config_dir, bmcu_dir)
    links = module_snapshot(klipper_dir, bmcu_dir)
    state_file = os.path.join(config_dir, 'bmcu_state.json')
    state_exists = validate_state(state_file)
    state_snapshot = None
    state_xattrs = None
    if state_exists:
        state_data, state_info = read_regular(state_file, MAX_JSON)
        if state_info.st_uid not in (0, pwd.getpwnam(plan['install_user']).pw_uid):
            raise UninstallError('orphaned BMCU state has unexpected owner: %s' % state_file)
        if stat.S_IMODE(state_info.st_mode) & 0o022:
            raise UninstallError('orphaned BMCU state is group/world writable: %s' % state_file)
        state_snapshot = (state_data, state_info)
        state_xattrs = read_xattrs(state_file)
    boot_hook = orphan_boot_hook_snapshot(service, plan.get('platform_id'))
    u1_snapshot = u1_integration_snapshot(
        plan.get('platform_id'), boot_hook[0] if boot_hook else None)
    has_links = any(record is not None for record in links.values())
    has_boot_hook = bool(boot_hook and boot_hook[1] is not None)
    has_u1 = bool(u1_snapshot and (
        u1_snapshot.get('service_has_block') or
        u1_snapshot.get('legacy_data') is not None or
        u1_snapshot.get('runner_present')))
    if not (cleaned_cfg != original_cfg or has_links or state_exists or
            has_boot_hook or has_u1):
        print('BMCU-Klipper 1.0.0 is already removed; nothing to do.')
        return 0

    host_state = 'unavailable'
    try:
        host_state, host_message = printer_state(moonraker)
    except Exception as exc:
        if matching_klippy_pids(klipper_dir, printer_cfg):
            raise UninstallError(
                'Moonraker activity cannot be verified while Klipper is running; '
                'stop the print and restore Moonraker before uninstall: %s' % exc)
        print('Moonraker unavailable and Klipper is not running; continuing residual cleanup.')
    else:
        if host_state == 'ready':
            is_idle, job_state = printer_idle(moonraker, False)
            if not is_idle:
                raise UninstallError('printer is not idle: %s' % job_state)
        elif host_state in ('shutdown', 'error', 'startup'):
            print('Klipper is %s; continuing residual cleanup.' % host_state)
        else:
            raise UninstallError('unexpected Klipper state before cleanup: %s - %s' %
                                 (host_state, host_message))

    service_stopped = False
    cfg_changed = False
    removed_links = []
    hook_removed = False
    u1_removed = False
    state_removed = False
    try:
        stop_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = True
        current_cfg, _ = read_regular(printer_cfg)
        if current_cfg != original_cfg:
            raise UninstallError('printer.cfg changed before residual cleanup')
        assert_module_snapshot(links)
        if state_snapshot is not None:
            current_state, current_info = read_regular(state_file, MAX_JSON)
            if current_state != state_snapshot[0] or not _same_file_info(current_info, state_snapshot[1]):
                raise UninstallError('orphaned BMCU state changed before cleanup')
        if cleaned_cfg != original_cfg:
            atomic_write(printer_cfg, cleaned_cfg, cfg_info, cfg_xattrs,
                         expected=original_cfg)
            cfg_changed = True
        for path, record in links.items():
            if record is not None:
                unlink_symlink_expected(path, record)
                removed_links.append((path, record))
        if has_boot_hook:
            unlink_regular_expected(boot_hook[0], boot_hook[1], boot_hook[3])
            hook_removed = True
            reload_boot_manager(boot_hook)
            try:
                os.rmdir(os.path.dirname(boot_hook[0]))
            except OSError:
                pass
        u1_removed = remove_u1_integration(u1_snapshot)
        if state_snapshot is not None:
            unlink_regular_expected(state_file, state_snapshot[0], state_snapshot[1])
            state_removed = True
        start_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = False
        ready, last = wait_ready(moonraker)
        if not ready:
            raise UninstallError('Klipper did not become ready after residual cleanup: %s - %s' % last)
        pids = wait_single_klippy(klipper_dir, printer_cfg)
        if len(pids) != 1:
            raise UninstallError('expected one Klipper process after cleanup, found %d' % len(pids))
        print('BMCU-Klipper 1.0.0 residual host state removed safely.')
        print('Removed printer.cfg references: %d' % removed_references)
        print('Removed Klipper module links: %d' % len(removed_links))
        return 0
    except Exception as original_error:
        rollback_errors = []
        try:
            if not service_stopped:
                stop_service_strict(service, klipper_dir, printer_cfg)
                service_stopped = True
        except Exception as exc:
            rollback_errors.append('Klipper stop: %s' % exc)
        try:
            if state_removed:
                restore_removed_regular(
                    state_file, state_snapshot[0], state_snapshot[1], state_xattrs)
        except Exception as exc:
            rollback_errors.append('state restore: %s' % exc)
        try:
            if u1_removed:
                restore_u1_integration(u1_snapshot)
            if hook_removed:
                restore_hook(boot_hook)
                reload_boot_manager(boot_hook)
        except Exception as exc:
            rollback_errors.append('boot integration restore: %s' % exc)
        for path, record in reversed(removed_links):
            try:
                restore_symlink(path, record)
            except Exception as exc:
                rollback_errors.append('module link restore %s: %s' % (path, exc))
        try:
            if cfg_changed:
                current_cfg, current_info = read_regular(printer_cfg)
                if current_cfg != cleaned_cfg:
                    raise UninstallError('printer.cfg changed during cleanup rollback')
                atomic_write(printer_cfg, original_cfg, current_info,
                             read_xattrs(printer_cfg), expected=current_cfg)
        except Exception as exc:
            rollback_errors.append('printer.cfg restore: %s' % exc)
        if not rollback_errors:
            try:
                start_service_strict(service, klipper_dir, printer_cfg)
                service_stopped = False
                ready, last = wait_ready(moonraker, 45)
                if not ready:
                    rollback_errors.append('Klipper not ready after rollback: %s - %s' % last)
            except Exception as exc:
                rollback_errors.append('Klipper restart: %s' % exc)
        message = str(original_error)
        if rollback_errors:
            message += '; rollback incomplete: ' + '; '.join(rollback_errors)
        raise UninstallError(message)

def prune_uninstall_backups(data_root, keep=UNINSTALL_BACKUP_RETENTION):

    try:
        keep = int(keep)
    except (TypeError, ValueError, OverflowError):
        raise UninstallError('invalid uninstall backup retention')
    if keep < 0:
        raise UninstallError('uninstall backup retention cannot be negative')
    backups = []
    try:
        names = os.listdir(data_root)
    except OSError as exc:
        raise UninstallError(
            'cannot inspect uninstall backups in %s: %s' % (data_root, exc))
    for name in names:
        if not UNINSTALL_BACKUP_RE.fullmatch(name):
            continue
        path = os.path.join(data_root, name)
        if managed_uninstall_backup(path):
            backups.append(path)
    backups.sort(key=os.path.basename, reverse=True)
    removed = []
    for path in backups[keep:]:
        identity = directory_identity(path)
        if not managed_uninstall_backup(path):
            continue
        assert_directory_identity(path, identity)
        shutil.rmtree(path)
        removed.append(path)
    if removed:
        directory_fd = os.open(data_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return removed

def _read_managed_process_json(path, label):
    if not os.path.lexists(path):
        return {}
    if os.path.islink(path) or not os.path.isfile(path):
        raise UninstallError('%s process record is unsafe' % label)
    try:
        data, _info = read_regular(path, MAX_JSON)
        value = json.loads(data.decode('utf-8'))
    except Exception as exc:
        raise UninstallError('cannot read %s process record: %s' % (label, exc))
    if not isinstance(value, dict):
        raise UninstallError('%s process record is malformed' % label)
    return value

def _stop_recorded_worker(record, runtime, daemon_name, label):
    if not isinstance(record, dict):
        return False
    try:
        pid = int(record.get('pid', 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    daemon = os.path.realpath(str(record.get('daemon', '') or ''))
    if (pid <= 1 or os.path.basename(daemon) != daemon_name or
            not inside(daemon, runtime)):
        return False
    argv = read_proc_cmdline(pid)
    if not argv:
        return False
    command = ' '.join(argv)
    if daemon not in command or daemon_name not in command:

        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise UninstallError('cannot stop %s: %s' % (label, exc))
    remaining = wait_pids_gone([pid], 3.0)
    if remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise UninstallError('cannot kill %s: %s' % (label, exc))
        remaining = wait_pids_gone(remaining, 1.0)
    if remaining:
        raise UninstallError('%s did not stop' % label)
    return True

def stop_managed_bmcu_workers(bmcu_dir):

    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    transport_path = os.path.join(bmcu_dir, 'transport-processes.json')
    transport_record = _read_managed_process_json(
        transport_path, 'BMCU transport')
    devices = transport_record.get('devices', {})
    if devices and not isinstance(devices, dict):
        raise UninstallError('BMCU transport process record is malformed')
    stopped = 0
    for name, record in sorted((devices or {}).items()):
        if _stop_recorded_worker(
                record, runtime, 'bmcu_transportd.py',
                'BMCU transport %s' % name):
            stopped += 1
    planner_path = os.path.join(bmcu_dir, 'planner-process.json')
    planner_record = _read_managed_process_json(planner_path, 'U1 planner')
    if planner_record and _stop_recorded_worker(
            planner_record, runtime, 'bmcu_plannerd.py', 'U1 source planner'):
        stopped += 1
    return stopped

def stop_managed_panel_process(bmcu_dir):

    pidfile = os.path.join(bmcu_dir, 'panel-process.json')
    if not os.path.isfile(pidfile) or os.path.islink(pidfile):
        return False
    try:
        with open(pidfile, 'r') as stream:
            record = json.load(stream)
        pid = int(record.get('pid', 0) or 0)
    except Exception:
        return False
    if pid <= 1:
        return False
    command_path = '/proc/%d/cmdline' % pid
    try:
        with open(command_path, 'rb') as stream:
            command = stream.read(8192).replace(b'\0', b' ').decode(
                'utf-8', 'replace')
    except Exception:
        return False
    expected = os.path.realpath(os.path.join(
        bmcu_dir, 'runtime', 'web', 'bmcu_panel_server.py'))
    if 'bmcu_panel_server.py' not in command or expected not in command:
        raise UninstallError(
            'panel pid file points to a foreign process; refusing to stop pid %d' % pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return True
            raise
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
    return True

def main():
    if sys.version_info < (3, 7):
        raise UninstallError('Python 3.7 or newer is required')
    args = parser().parse_args()
    if os.geteuid() != 0:
        raise UninstallError('run the uninstaller as root or through sudo')
    _trusted_root_path(sys.executable, require_executable=True)

    package = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    snapshot = load_release_snapshot(package)
    platform = load_package_module('bmcu_platform', snapshot, package)
    moonraker_gcode = load_package_module('moonraker_gcode', snapshot, package)
    print('Package integrity: %d files verified' % (len(snapshot) - 1))
    plan = discover(platform, args)
    validate_root_run_installation(plan)

    service = plan.get('service') or {}
    if service.get('backend') in ('none', 'process-only', ''):
        raise UninstallError('controllable Klipper service was not detected')

    config_dir = plan['config_dir']
    printer_cfg = plan['printer_cfg']
    klipper_dir = plan['klipper_dir']
    moonraker = plan['moonraker_url']
    bmcu_dir = os.path.join(config_dir, 'bmcu')
    runtime = os.path.join(bmcu_dir, 'runtime')
    if not os.path.lexists(bmcu_dir):
        return cleanup_missing_tree(
            plan, service, config_dir, printer_cfg, klipper_dir,
            moonraker, bmcu_dir)
    if os.path.islink(bmcu_dir) or not os.path.isdir(bmcu_dir):
        raise UninstallError('BMCU directory is unsafe: %s' % bmcu_dir)
    metadata = None
    for metadata_path in (
        os.path.join(runtime, 'INSTALLATION.json'),
        os.path.join(bmcu_dir, 'INSTALLATION.json'),
    ):
        metadata = read_optional_json(metadata_path)
        if metadata is not None:
            break

    validate_metadata(
        metadata, config_dir, printer_cfg, klipper_dir, service,
        plan.get('install_user'), plan.get('python'))
    validate_ownership(bmcu_dir, runtime, metadata)
    bmcu_identity = directory_identity(bmcu_dir)
    boot_hook = hook_snapshot(
        metadata, service, plan.get('platform_id'))
    u1_snapshot = u1_integration_snapshot(
        plan.get('platform_id'), boot_hook[0] if boot_hook else None)
    transaction_lock = acquire_lock(printer_cfg)

    original_cfg, cfg_info = read_regular(printer_cfg)
    cfg_xattrs = read_xattrs(printer_cfg)
    cleaned_cfg, removed_references = clean_printer_cfg(
        original_cfg, config_dir, bmcu_dir)
    links = module_snapshot(klipper_dir, bmcu_dir)
    state_file = os.path.join(config_dir, 'bmcu_state.json')
    state_exists = validate_state(state_file)
    state_snapshot = None
    if state_exists:
        state_data, state_info = read_regular(state_file, MAX_JSON)
        state_snapshot = (state_data, state_info.st_dev, state_info.st_ino)

    host_state = 'unavailable'
    host_message = ''
    try:
        host_state, host_message = printer_state(moonraker)
    except Exception as exc:
        running = matching_klippy_pids(klipper_dir, printer_cfg)
        if running:
            raise UninstallError(
                'Moonraker activity cannot be verified while Klipper is running; '
                'stop the print and restore Moonraker before uninstall: %s' % exc)
        print('Moonraker unavailable and Klipper is not running; continuing host-only uninstall.')
    else:
        if host_state == 'ready':
            try:
                is_idle, job_state = printer_idle(moonraker, False)
            except Exception as exc:
                raise UninstallError(
                    'printer activity cannot be verified; no files removed: %s' % exc)
            if not is_idle:
                raise UninstallError('printer is not idle: %s' % job_state)
        elif host_state in ('shutdown', 'error', 'startup'):
            print('Klipper is %s; continuing host-only uninstall without BMCU commands.' % host_state)
        else:
            raise UninstallError(
                'unexpected Klipper state before uninstall: %s - %s' %
                (host_state, host_message))

    current_cfg, _current_info = read_regular(printer_cfg)
    if current_cfg != original_cfg:
        raise UninstallError('printer.cfg changed during uninstall; no files removed')
    assert_module_snapshot(links)
    validate_ownership(bmcu_dir, runtime, metadata)
    state_exists = validate_state(state_file)
    if bool(state_snapshot) != bool(state_exists):
        raise UninstallError('BMCU state presence changed during uninstall')
    if state_snapshot is not None:
        state_data, state_info = read_regular(state_file, MAX_JSON)
        if (state_data != state_snapshot[0] or
                state_info.st_dev != state_snapshot[1] or
                state_info.st_ino != state_snapshot[2]):
            raise UninstallError('BMCU state changed during uninstall')

    if host_state == 'ready':
        current_state, current_message = printer_state(moonraker)
        if current_state != 'ready':
            raise UninstallError(
                'Klipper state changed during uninstall: %s - %s' %
                (current_state, current_message))
        is_idle, job_state = printer_idle(moonraker, False)
        if not is_idle:
            raise UninstallError('printer started a job during uninstall: %s' % job_state)

    data_root = os.path.dirname(os.path.realpath(config_dir))
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = os.path.join(
        data_root, 'bmcu-uninstalled-%s-%d' % (stamp, os.getpid()))
    backup_bmcu = os.path.join(backup, 'bmcu')
    backup_state = os.path.join(backup, 'bmcu_state.json')

    service_stopped = False
    cfg_changed = False
    removed_links = []
    directory_moved = False
    state_moved = False
    backup_created = False
    backup_identity = None
    backup_bmcu_identity = None
    hook_removed = False
    u1_integration_removed = False
    try:
        stop_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = True
        stop_managed_bmcu_workers(bmcu_dir)
        stop_managed_panel_process(bmcu_dir)

        current_cfg, _current_info = read_regular(printer_cfg)
        if current_cfg != original_cfg:
            raise UninstallError('printer.cfg changed after Klipper stop')
        assert_module_snapshot(links)
        validate_ownership(bmcu_dir, runtime, metadata)
        assert_directory_identity(bmcu_dir, bmcu_identity)

        os.mkdir(backup, 0o750)
        backup_identity = directory_identity(backup)
        backup_created = True
        assert_directory_identity(backup, backup_identity)
        write_new_bytes(
            os.path.join(backup, 'printer.cfg.before-uninstall'),
            original_cfg, 0o640)

        if cleaned_cfg != original_cfg:
            atomic_write(
                printer_cfg, cleaned_cfg, cfg_info, cfg_xattrs,
                expected=original_cfg)
            cfg_changed = True

        for path, record in links.items():
            if record is None:
                continue
            unlink_symlink_expected(path, record)
            removed_links.append(path)

        if boot_hook and boot_hook[1] is not None:
            if not managed_boot_hook(boot_hook[0]):
                raise UninstallError('BMCU boot hook changed during uninstall')
            current_hook, hook_info = read_regular(boot_hook[0], MAX_FILE)
            if current_hook != boot_hook[1]:
                raise UninstallError('BMCU boot hook was modified during uninstall')
            unlink_regular_expected(
                boot_hook[0], current_hook, boot_hook[3])
            hook_removed = True
            reload_boot_manager(boot_hook)
            try:
                os.rmdir(os.path.dirname(boot_hook[0]))
            except OSError:
                pass

        u1_integration_removed = remove_u1_integration(u1_snapshot)

        assert_directory_identity(bmcu_dir, bmcu_identity)
        assert_directory_identity(backup, backup_identity)
        os.rename(bmcu_dir, backup_bmcu)
        backup_bmcu_identity = directory_identity(backup_bmcu)
        if backup_bmcu_identity != bmcu_identity:
            raise UninstallError('preserved BMCU directory identity is inconsistent')
        directory_moved = True

        if state_exists and os.path.isfile(state_file):
            state_data, state_info = read_regular(state_file, MAX_JSON)
            if (state_snapshot is None or state_data != state_snapshot[0] or
                    state_info.st_dev != state_snapshot[1] or
                    state_info.st_ino != state_snapshot[2]):
                raise UninstallError('BMCU state changed after Klipper stop')
            os.rename(state_file, backup_state)
            state_moved = True

        start_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = False
        ready, last = wait_ready(moonraker)
        if not ready:
            raise UninstallError(
                'Klipper did not become ready after uninstall: %s - %s' % last)
        pids = wait_single_klippy(klipper_dir, printer_cfg)
        if len(pids) != 1:
            raise UninstallError(
                'expected one Klipper process after uninstall, found %d' %
                len(pids))

        assert_directory_identity(backup, backup_identity)
        assert_directory_identity(backup_bmcu, backup_bmcu_identity)
        marker_path = os.path.join(backup, 'UNINSTALL.txt')
        marker_text = (
            UNINSTALL_MARKER +
            'Removed printer.cfg references: %d\n' % removed_references +
            'Removed Klipper module links: %d\n' % len(removed_links) +
            'Removed BMCU boot repair hook: %s\n' %
            ('yes' if hook_removed else 'no') +
            'Removed managed U1 S60 integration: %s\n' %
            ('yes' if u1_integration_removed else 'no') +
            'Host-only uninstall: yes\n' +
            'Temporary rollback state: %s\n' %
            ('yes' if state_moved else 'no'))
        write_new_bytes(marker_path, marker_text.encode('utf-8'), 0o640)
        backup_fd = os.open(
            backup, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(backup_fd)
        finally:
            os.close(backup_fd)

        marker_removed = False
        marker_warning = ''
        if (plan.get('platform_id') == 'snapmaker_u1' and
                metadata.get('persistence_marker_preexisting') is False):
            try:
                if os.path.lexists(U1_PERSISTENCE_MARKER):
                    marker_data, marker_info = read_regular(
                        U1_PERSISTENCE_MARKER, 4096)
                    if (marker_data or marker_info.st_uid != 0 or
                            marker_info.st_gid != 0 or
                            stat.S_IMODE(marker_info.st_mode) & 0o022):
                        raise UninstallError(
                            'package-created /oem/.debug was modified; preserving it')
                    unlink_regular_expected(
                        U1_PERSISTENCE_MARKER, marker_data, marker_info)
                    marker_removed = True
            except Exception as exc:
                marker_warning = str(exc)

        cleanup_warning = ''
        try:
            pruned_backups = prune_uninstall_backups(data_root, keep=0)
        except Exception as exc:
            pruned_backups = []
            cleanup_warning = str(exc)

        print('BMCU-Klipper 1.0.0 removed safely.')
        if marker_removed:
            print('Removed package-created Snapmaker persistence marker: %s' %
                  U1_PERSISTENCE_MARKER)
        elif marker_warning:
            print('WARNING: %s' % marker_warning)
        if cleanup_warning or os.path.lexists(backup):
            print('WARNING: temporary BMCU rollback data remains at %s%s' %
                  (backup, (': ' + cleanup_warning) if cleanup_warning else ''))
        else:
            print('All managed BMCU host data and verified uninstall backups were removed.')
        if pruned_backups:
            print('Removed verified BMCU uninstall backups: %d' % len(pruned_backups))
        return 0

    except Exception as original_error:
        rollback_errors = []
        try:
            if not service_stopped:
                stop_service_strict(service, klipper_dir, printer_cfg)
                service_stopped = True
        except Exception as exc:
            rollback_errors.append('Klipper stop: %s' % exc)

        try:
            if directory_moved:
                if (os.path.isdir(backup_bmcu) and
                        not os.path.islink(backup_bmcu) and
                        not os.path.lexists(bmcu_dir)):
                    assert_directory_identity(
                        backup_bmcu, backup_bmcu_identity)
                    os.rename(backup_bmcu, bmcu_dir)
                    assert_directory_identity(bmcu_dir, bmcu_identity)
                else:
                    raise UninstallError(
                        'preserved BMCU directory cannot be restored safely')
        except Exception as exc:
            rollback_errors.append('BMCU directory restore: %s' % exc)

        try:
            if state_moved:
                if (os.path.isfile(backup_state) and
                        not os.path.islink(backup_state) and
                        not os.path.lexists(state_file)):
                    os.rename(backup_state, state_file)
                else:
                    raise UninstallError(
                        'preserved BMCU state cannot be restored safely')
        except Exception as exc:
            rollback_errors.append('BMCU state restore: %s' % exc)

        try:
            if u1_integration_removed:
                restore_u1_integration(u1_snapshot)
            if hook_removed:
                restore_hook(boot_hook)
                reload_boot_manager(boot_hook)
        except Exception as exc:
            rollback_errors.append('boot integration restore: %s' % exc)

        try:
            if cfg_changed:
                current_cfg, current_info = read_regular(printer_cfg)
                if current_cfg != cleaned_cfg:
                    raise UninstallError(
                        'printer.cfg changed after BMCU removal; refusing to '
                        'overwrite concurrent changes during rollback')
                atomic_write(
                    printer_cfg, original_cfg, current_info,
                    read_xattrs(printer_cfg), expected=current_cfg)
        except Exception as exc:
            rollback_errors.append(
                'printer.cfg restoration: %s' % exc)

        try:
            for path in removed_links:
                record = links[path]
                restore_symlink(path, record)
        except Exception as exc:
            rollback_errors.append('Klipper module restore: %s' % exc)

        if not rollback_errors:
            try:
                start_service_strict(service, klipper_dir, printer_cfg)
                service_stopped = False
                ready, last = wait_ready(moonraker, 45)
                if not ready:
                    rollback_errors.append(
                        'Klipper not ready after rollback: %s - %s' % last)
                else:
                    pids = wait_single_klippy(klipper_dir, printer_cfg)
                    if len(pids) != 1:
                        rollback_errors.append(
                            'expected one Klipper process after rollback, found %d' %
                            len(pids))
            except Exception as exc:
                rollback_errors.append('Klipper restart: %s' % exc)

        if not rollback_errors:
            try:
                if backup_created and os.path.isdir(backup):
                    assert_directory_identity(backup, backup_identity)
                    shutil.rmtree(backup)
            except Exception as exc:
                rollback_errors.append('temporary backup cleanup: %s' % exc)

        message = str(original_error)
        if rollback_errors:
            message += (
                '\nRollback incomplete; Klipper was left stopped and the '
                'uninstall backup was preserved. Problems: ' +
                '; '.join(rollback_errors))
        raise UninstallError(message)
    finally:
        try:
            os.close(transaction_lock)
        except OSError:
            pass

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except UninstallError as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        raise SystemExit(1)
