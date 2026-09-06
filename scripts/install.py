#!/usr/bin/env python3

from __future__ import print_function

import argparse
import errno
import fcntl
import glob
import hashlib
import json
import os
import pwd
import grp
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import types

sys.dont_write_bytecode = True

PRODUCT = 'BMCU-Klipper'
PRODUCT_VERSION = None
OWNERSHIP_MARKER = PRODUCT
OWNER_RE = re.compile(
    r'^%s(?: [0-9]+\.[0-9]+\.[0-9]+)?$' % re.escape(PRODUCT))
MANAGED_HEADER_RE = re.compile(
    (r'(?m)^# Managed by %s(?: [0-9]+\.[0-9]+\.[0-9]+)?(?:\.| -)' %
     re.escape(PRODUCT)).encode('ascii'))
BEGIN = '# BEGIN BMCU-KLIPPER AUTO-INCLUDE'
END = '# END BMCU-KLIPPER AUTO-INCLUDE'
INCLUDES = (
    '[include bmcu/bmcu.cfg]',
    '[include bmcu/bmcu_macros.cfg]',
    '[include bmcu/bmcu_panel.cfg]',
)
MAX_CONFIG = 16 * 1024 * 1024
MAX_RELEASE_BYTES = 64 * 1024 * 1024
MODULES = ('bmcu.py', 'bmcu_core', 'bmcu_panel.py')
U1_LEGACY_BOOT_HOOK = '/etc/init.d/S59bmcu-klipper'
U1_KLIPPER_SERVICE = '/etc/init.d/S60klipper'
U1_HOOK_DIR = '/etc/hooks/klipper.d'
U1_BOOT_HOOK = '/etc/hooks/klipper.d/50-bmcu-klipper.sh'
U1_PERSISTENCE_MARKER = '/oem/.debug'
U1_RUNNER_DIR = '/oem/bmcu-klipper'
U1_RUNNER = '/oem/bmcu-klipper/run-host-bootstrap.py'
U1_RUNNER_MARKER = '/oem/bmcu-klipper/.managed-by-bmcu'
U1_SERIAL_RULE_DIR = '/etc/udev/rules.d'
U1_SERIAL_RULE = '/etc/udev/rules.d/99-bmcu-klipper.rules'
U1_SERIAL_RULE_LABEL = 'Snapmaker U1 CH340 access'
U1_SERIAL_RULE_MARKER = '# Managed by %s - %s' % (
    OWNERSHIP_MARKER, U1_SERIAL_RULE_LABEL)
U1_SERIAL_VENDOR = '1a86'
U1_SERIAL_PRODUCTS = ('5523', '7522', '7523', '7584', '55d4')
U1_SERVICE_BEGIN = '# BEGIN BMCU-KLIPPER SERVICE-HOOKS'
U1_SERVICE_END = '# END BMCU-KLIPPER SERVICE-HOOKS'
U1_SERVICE_CALL_MARKER = '# BMCU-KLIPPER PREPARE IMMEDIATELY BEFORE KLIPPER PRIVILEGE DROP'
U1_SERVICE_CALL = 'bmcu_prepare_klipper_start || exit 1'
SYSTEMD_DROPIN_NAME = 'bmcu-klipper.conf'
RUNTIME_SCRIPTS = (
    'apply_detected_devices.py', 'bmcu_cli.py', 'bmcu_doctor.py',
    'bmcu_host_bootstrap.py', 'bmcu_transportd.py', 'bmcu_plannerd.py',
    'bmcu_isp.py', 'bmcu_runtime.py', 'bmcu_collect_logs.py',
    'bmcu_update.py', 'bmcu_vendor.py', 'detect_bmcu.py',
    'bmcu_platform.py', 'safe_file_ops.py',
    'safe_printer_cfg_include.py', 'moonraker_gcode.py',
)

UNINSTALL_NOTE = b'Run uninstall from an extracted BMCU-Klipper package:\n    sh ./uninstall\n'

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
        if info.st_size > MAX_RELEASE_BYTES:
            raise InstallError('package file is too large: %s' % path)
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
        if os.path.realpath(current) == package and '.git' in directories:
            directories.remove('.git')
        current_real = os.path.realpath(current)
        if current_real != package and not current_real.startswith(package + os.sep):
            raise InstallError('package path escaped release root: %s' % current)
        for directory in list(directories):
            path = os.path.join(current, directory)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise InstallError('package contains unsafe directory entry: %s' % path)
        for filename in files:
            path = os.path.join(current, filename)
            relative = os.path.relpath(path, package).replace(os.sep, '/')
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise InstallError('package contains unsafe file entry: %s' % relative)
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
    key = relative.replace(os.sep, '/')
    try:
        return snapshot[key]
    except KeyError:
        raise InstallError('package file is missing: %s' % key)

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
    descriptor, temporary = tempfile.mkstemp(
        prefix='.%s.' % os.path.basename(destination), dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def regular_file(path):
    try:
        info = os.lstat(path)
        return stat.S_ISREG(info.st_mode)
    except OSError:
        return False

def _open_regular_fd(path, limit, error_type):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    if nofollow:
        flags |= nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise error_type('required file is unavailable or unsafe: %s (%s)' %
                         (path, exc))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise error_type('refusing unsafe file: %s' % path)
        if info.st_size > limit:
            raise error_type('file is too large: %s' % path)
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
            raise error_type('file is too large: %s' % path)
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) !=
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
            raise error_type('file changed while it was being read: %s' % path)
        if len(data) != final.st_size:
            raise error_type('file size changed while it was being read: %s' % path)
        return data, final
    finally:
        os.close(descriptor)

def read_regular(path):
    return _open_regular_fd(path, MAX_CONFIG, InstallError)

def read_small_regular(path, limit=4096):
    return _open_regular_fd(path, limit, InstallError)

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
    fd, tmp = tempfile.mkstemp(prefix='.%s.' % os.path.basename(path), dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, stat.S_IMODE(info.st_mode))
        try:
            os.chown(tmp, info.st_uid, info.st_gid)
        except PermissionError:
            pass
        if xattrs and hasattr(os, 'setxattr'):
            for name, value in xattrs.items():
                try:
                    os.setxattr(tmp, name, value, follow_symlinks=False)
                except (OSError, TypeError):
                    pass
        current_data, current_info = read_regular(path)
        if not _same_file_info(current_info, info):
            raise InstallError(
                'file changed before atomic replacement; refusing to overwrite: %s' %
                path)
        if expected is not None and current_data != expected:
            raise InstallError(
                'file contents changed before atomic replacement; refusing to overwrite: %s' %
                path)
        os.replace(tmp, path)
        directory_fd = os.open(
            parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _written, written_info = read_regular(path)
        return written_info
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

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
            raise InstallError(
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
            raise InstallError(
                'file contents changed before removal; refusing to unlink: %s' %
                path)
        final = os.fstat(descriptor)
        if not _same_file_info(final, expected_info):
            raise InstallError(
                'file changed while validating removal: %s' % path)
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)

def restore_removed_regular(path, data, info, xattrs=None):
    if os.path.lexists(path):
        raise InstallError('refusing to restore over an occupied path: %s' % path)
    write_new_atomic(path, data, stat.S_IMODE(info.st_mode))
    try:
        os.chown(path, info.st_uid, info.st_gid)
        os.chmod(path, stat.S_IMODE(info.st_mode))
        if xattrs and hasattr(os, 'setxattr'):
            for name, value in xattrs.items():
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
        restored, restored_info = read_regular(path)
        if (restored != data or restored_info.st_size != info.st_size or
                stat.S_IMODE(restored_info.st_mode) !=
                stat.S_IMODE(info.st_mode) or
                restored_info.st_uid != info.st_uid or
                restored_info.st_gid != info.st_gid):
            raise InstallError('restored file metadata differs: %s' % path)
    except Exception:
        try:
            current, current_info = read_regular(path)
            if current == data:
                unlink_regular_expected(path, current, current_info)
        except Exception:
            pass
        raise

def include_bytes(newline, leading_newline=False):
    block = newline.join(line.encode('utf-8') for line in ((BEGIN,) + INCLUDES + (END,))) + newline
    return (newline if leading_newline else b'') + block

def clean_bmcu_references(original):
    try:
        text = original.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('printer.cfg is not UTF-8')
    lines = text.splitlines(keepends=True)
    normalized = [line.strip().upper() for line in lines]
    begins = [index for index, value in enumerate(normalized)
              if value == BEGIN.upper()]
    ends = [index for index, value in enumerate(normalized)
            if value == END.upper()]
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
            raise InstallError(
                'managed BMCU include markers in printer.cfg are malformed')

    remove = set()
    if begins:
        begin, end = begins[0], ends[0]
        remove.update(range(begin, end + 1))
        previous = begin - 1
        if (previous >= 0 and
                normalized[previous] == '# BMCU-KLIPPER'):
            remove.add(previous)
            previous -= 1
        if (previous >= 0 and
                lines[previous].strip() == '################################'):
            remove.add(previous)

    include_re = re.compile(
        r'^\s*\[\s*include\s+([^\]]+)\]\s*(?:#.*)?$', re.IGNORECASE)
    for index, line in enumerate(lines):
        if index in remove:
            continue
        match = include_re.match(line.rstrip('\r\n'))
        if not match:
            continue
        target = match.group(1).strip().strip('"\'').replace('\\', '/')
        while target.startswith('./'):
            target = target[2:]
        if target.lower() == 'bmcu' or target.lower().startswith('bmcu/'):
            remove.add(index)
            previous = index - 1
            if (previous >= 0 and
                    normalized[previous] == '# BMCU-KLIPPER'):
                remove.add(previous)
                if (previous - 1 >= 0 and
                        lines[previous - 1].strip() == '################################'):
                    remove.add(previous - 1)

    cleaned = ''.join(
        line for index, line in enumerate(lines) if index not in remove)
    return cleaned.encode('utf-8'), len(remove)

def remove_installed_include(current, inserted):
    if current.count(inserted) == 1:
        return current.replace(inserted, b'', 1)
    cleaned, removed = clean_bmcu_references(current)
    if not removed:
        raise InstallError(
            'installer-owned BMCU include is no longer present; printer.cfg was not overwritten')
    return cleaned

def add_include(original):
    try:
        text = original.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('printer.cfg is not UTF-8')
    if (BEGIN.lower() in text.lower() or END.lower() in text.lower() or
            re.search(r'(?im)^\s*\[include\s+bmcu/', text)):
        raise InstallError('an existing BMCU include was found; clean install aborted')

    newline = b'\r\n' if b'\r\n' in original and b'\n' not in original.replace(b'\r\n', b'') else b'\n'
    marker = re.search(
        br'(?m)^#\*# <---------------------- SAVE_CONFIG ---------------------->',
        original)
    index = marker.start() if marker else len(original)
    leading_newline = bool(index and original[index - 1:index] not in (b'\n', b'\r'))
    inserted = include_bytes(newline, leading_newline)
    return original[:index] + inserted + original[index:], inserted

TRUSTED_PATH = '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin'

def _trusted_root_path(path, require_executable=False, require_directory=False):
    if not path or not os.path.isabs(path):
        raise InstallError('system path must be absolute: %s' % path)
    resolved = os.path.realpath(path)
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise InstallError('system path is unavailable: %s (%s)' % (path, exc))
    if require_directory:
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError('system path is not a directory: %s' % path)
    else:
        if not stat.S_ISREG(info.st_mode):
            raise InstallError('system program is not a regular file: %s' % path)
        if require_executable and not os.access(resolved, os.X_OK):
            raise InstallError('system program is not executable: %s' % path)
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
            raise InstallError('cannot validate system path %s: %s' % (current, exc))
        if current_info.st_uid != 0 or stat.S_IMODE(current_info.st_mode) & 0o022:
            raise InstallError(
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
        except InstallError as exc:
            errors.append(str(exc))
    if errors:
        raise InstallError(errors[0])
    raise InstallError('required system program is unavailable: %s' % name)

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
    raise InstallError('controllable Klipper service was not detected')

def run(cmd, check=True, env=None, timeout=60):
    try:
        result = subprocess.run(
            cmd, env=(safe_command_env() if env is None else env), timeout=timeout)
    except subprocess.TimeoutExpired:
        if check:
            raise InstallError('command timed out after %ds: %s' % (timeout, ' '.join(cmd)))
        return 124
    if check and result.returncode:
        raise InstallError('command failed (%d): %s' % (result.returncode, ' '.join(cmd)))
    return result.returncode

def acquire_lock(printer_cfg):
    directory = os.path.dirname(os.path.realpath(printer_cfg))
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_DIRECTORY', 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise InstallError('cannot open printer config directory lock %s: %s' % (directory, exc))
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise InstallError('printer config parent is not a directory: %s' % directory)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('another BMCU install or uninstall is already running for this printer')
        return descriptor
    except Exception:
        os.close(descriptor)
        raise

def supplementary_groups(user, primary_gid):
    values = {int(primary_gid)}
    for entry in grp.getgrall():
        if user in entry.gr_mem:
            values.add(int(entry.gr_gid))
    return sorted(values)

def run_as_user(cmd, user, uid, gid, check=False, env=None, timeout=120):
    groups = supplementary_groups(user, gid)

    def demote():
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
        os.umask(0o077)

    try:
        result = subprocess.run(
            cmd,
            env=(safe_command_env(pwd.getpwnam(user).pw_dir)
                 if env is None else env),
            preexec_fn=demote,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        if check:
            raise InstallError(
                'command timed out after %ds as %s: %s' %
                (timeout, user, ' '.join(cmd)))
        return 124
    if check and result.returncode:
        raise InstallError(
            'command failed as %s (%d): %s' %
            (user, result.returncode, ' '.join(cmd)))
    return result.returncode

def query_json(url, timeout=3.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={'User-Agent': 'BMCU-Installer/%s' % PRODUCT_VERSION})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise InstallError('Moonraker response is too large')
    return json.loads(raw.decode('utf-8'))

def printer_state(base):
    info = query_json(base.rstrip('/') + '/printer/info')
    result = info.get('result') if isinstance(info, dict) else None
    if not isinstance(result, dict):
        raise InstallError('invalid Moonraker /printer/info response')
    return str(result.get('state') or '').strip().lower(), str(result.get('state_message') or '')

def printer_idle(base, assume_idle=False):
    data = query_json(
        base.rstrip('/') +
        '/printer/objects/query?print_stats&pause_resume&idle_timeout')
    result = data.get('result') if isinstance(data, dict) else None
    status = result.get('status') if isinstance(result, dict) else None
    if not isinstance(status, dict):
        raise InstallError('Moonraker did not return printer status objects')

    pause = status.get('pause_resume')
    if isinstance(pause, dict) and bool(pause.get('is_paused')):
        return False, 'paused'

    stats = status.get('print_stats')
    print_state = str(stats.get('state') or '').strip().lower() if isinstance(stats, dict) else ''
    if print_state in ('printing', 'paused'):
        return False, print_state

    idle_timeout = status.get('idle_timeout')
    idle_state = str(idle_timeout.get('state') or '').strip().lower() if isinstance(idle_timeout, dict) else ''
    if idle_state == 'printing':
        return False, idle_state

    if print_state in ('standby', 'complete', 'cancelled', 'error'):
        return True, print_state
    if idle_state in ('idle', 'ready'):
        return True, idle_state

    if assume_idle:
        return True, 'explicit --assume-idle'
    raise InstallError(
        'printer activity could not be verified; configure print_stats or '
        'rerun with --assume-idle only after stopping all motion and heating')

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

def bmcu_status(base):
    data = query_json(base.rstrip('/') + '/printer/objects/query?bmcu')
    result = data.get('result') if isinstance(data, dict) else None
    status = result.get('status') if isinstance(result, dict) else None
    value = status.get('bmcu') if isinstance(status, dict) else None
    if not isinstance(value, dict):
        raise InstallError('Moonraker did not return BMCU status')
    return value

def wait_bmcu_ready(base, expected, timeout=45):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = bmcu_status(base)
            devices = last.get('devices', [])
            if (isinstance(devices, list) and len(devices) == expected and
                    all(bool(device.get('ready')) for device in devices)):
                return True, last
        except Exception as exc:
            last = {'error': str(exc)}
        time.sleep(1)
    return False, last

PANEL_TOKEN_RE = re.compile(r'^[0-9a-f]{64}$')

def panel_token_from_bytes(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('bmcu_panel.cfg is not UTF-8')
    values = re.findall(r'(?m)^access_token:\s*([^\s#]+)\s*$', text)
    if len(values) != 1 or not PANEL_TOKEN_RE.match(values[0].lower()):
        return ''
    return values[0].lower()

def ensure_panel_token(data, token=''):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('bmcu_panel.cfg is not UTF-8')
    token = str(token or '').lower()
    if not PANEL_TOKEN_RE.match(token):
        token = os.urandom(32).hex()
    line = 'access_token: %s' % token
    if re.search(r'(?m)^access_token:\s*.*$', text):
        text = re.sub(r'(?m)^access_token:\s*.*$', line, text, count=1)
    else:
        text = text.rstrip('\r\n') + '\n' + line + '\n'
    return text.encode('utf-8'), token

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
        ('required_runtime_sync_timeout', ('8.0',), '15.0'),
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
        ('required_runtime_sync_timeout', '15.0'),
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

def panel_cookie_name(token):
    return 'bmcu_session_' + hashlib.sha256(token.encode('ascii')).hexdigest()[:16]

def panel_url(port, token=''):
    host = socket.gethostname().strip() or 'printer-host'
    return 'http://%s:%d/' % (host, int(port))

def panel_ip_url(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('192.0.2.1', 9))
        address = str(sock.getsockname()[0] or '').strip()
    except OSError:
        address = ''
    finally:
        sock.close()
    if not address or address.startswith('127.') or address == '0.0.0.0':
        return ''
    return 'http://%s:%d/' % (address, int(port))

def wait_panel(port, token, timeout=30):

    url = 'http://127.0.0.1:%d/bmcu-panel.html' % int(port)
    deadline = time.time() + timeout
    last = ''
    while time.time() < deadline:
        try:
            request = urllib.request.Request(
                url, headers={'Accept': 'text/html', 'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(request, timeout=3.0) as response:
                payload = response.read(1024 * 1024)
                if response.getcode() == 200 and b'<title>BMCU Control</title>' in payload:
                    return True, 'ready'
                last = 'unexpected HTTP response'
        except Exception as exc:
            last = str(exc)
        time.sleep(0.5)
    return False, last or 'panel did not answer'

def panel_port_available(port):
    addresses = [(socket.AF_INET, ('0.0.0.0', int(port)))]
    if getattr(socket, 'has_ipv6', False):
        addresses.append((socket.AF_INET6, ('::', int(port))))
    for family, address in addresses:
        listener = None
        try:
            listener = socket.socket(family, socket.SOCK_STREAM)
            listener.bind(address)
        except OSError as exc:

            if family == socket.AF_INET6 and exc.errno in (
                    getattr(errno, 'EAFNOSUPPORT', 97),
                    getattr(errno, 'EPROTONOSUPPORT', 93),
                    getattr(errno, 'EADDRNOTAVAIL', 99)):
                continue
            return False
        finally:
            if listener is not None:
                listener.close()
    return True

def validate_serial_port(path):
    if not os.path.isabs(path) or not os.path.realpath(path).startswith('/dev/'):
        raise InstallError('serial port must resolve inside /dev: %s' % path)
    try:
        info = os.stat(path)
    except OSError as exc:
        raise InstallError('serial port is unavailable: %s (%s)' % (path, exc))
    if not stat.S_ISCHR(info.st_mode):
        raise InstallError('serial port is not a character device: %s' % path)
    return path

def assert_preflight_unchanged(printer_cfg, original_cfg, bmcu_dir, state_file, targets,
                              expected_orphan_state=None):
    current, _info = read_regular(printer_cfg)
    if current != original_cfg:
        raise InstallError('printer.cfg changed during installation; no persistent changes made')
    if os.path.lexists(bmcu_dir):
        raise InstallError('BMCU directory appeared during installation; no persistent changes made')
    if expected_orphan_state is None:
        if os.path.lexists(state_file):
            raise InstallError('BMCU state appeared during installation; no persistent changes made')
    else:
        state_data, state_info = read_regular(state_file)
        if (state_data != expected_orphan_state[0] or
                not _same_file_info(state_info, expected_orphan_state[1])):
            raise InstallError('existing BMCU state changed during installation; no persistent changes made')
    for target in targets:
        if os.path.lexists(target):
            raise InstallError('Klipper module path became occupied during installation: %s' % target)

def safe_candidates(config_dir):
    used = set()
    for current, directories, files in os.walk(
            config_dir, topdown=True, followlinks=False):
        safe_directories = []
        for name in directories:
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            if not name.lower().endswith('.cfg'):
                continue
            path = os.path.join(current, name)
            try:
                data, _info = read_regular(path)
            except InstallError:
                continue
            text = data.decode('utf-8', 'ignore')
            for match in re.finditer(
                    r'/dev/(?:serial/(?:by-id|by-path)/[^\s#]+|'
                    r'ttyUSB\d+|ttyACM\d+|ttyCH343USB\d+)', text):
                used.add(os.path.realpath(match.group(0)))
    raw = (glob.glob('/dev/serial/by-path/*') + glob.glob('/dev/serial/by-id/*') +
           glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyCH343USB*'))
    selected = {}
    for path in raw:
        resolved = os.path.realpath(path)
        if resolved in used:
            continue
        base = os.path.basename(path).lower()
        tty = os.path.basename(resolved)
        safe = tty.startswith(('ttyUSB', 'ttyCH343USB')) or any(
            token in base for token in ('1a86', 'ch340', 'ch341', 'usb_serial'))
        current = os.path.realpath(os.path.join('/sys/class/tty', tty, 'device'))
        for _ in range(8):
            try:
                vendor, _info = read_small_regular(
                    os.path.join(current, 'idVendor'), 64)
                if vendor.decode('ascii', 'ignore').strip().lower() == '1a86':
                    safe = True
            except InstallError:
                pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        if not safe:
            continue
        rank = 0 if '/by-path/' in path else 1 if '/by-id/' in path else 2
        previous = selected.get(resolved)
        if previous is None or rank < previous[0]:
            selected[resolved] = (rank, path)
    return [value[1] for value in sorted(selected.values())]

def confirm_serial_probe(port):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    print('\nUnused USB-TTL serial device found: %s' % port)
    print('The installer must send a read-only BMCU identification request.')
    try:
        answer = input('Probe this device? [y/N]: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print('')
        return False
    return answer in ('y', 'yes')

def _validate_plain_tree(path):
    root = os.path.realpath(path)
    if os.path.islink(path) or not os.path.isdir(path):
        raise InstallError('staging root is unsafe: %s' % path)
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_info = os.lstat(current)
        if not stat.S_ISDIR(current_info.st_mode):
            raise InstallError('staging entry is not a directory: %s' % current)
        for name in list(dirs) + list(files):
            entry = os.path.join(current, name)
            info = os.lstat(entry)
            if stat.S_ISLNK(info.st_mode):
                raise InstallError('staging contains a symbolic link: %s' % entry)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise InstallError('staging contains a special file: %s' % entry)

def directory_identity(path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise InstallError('staging directory is unavailable: %s (%s)' %
                           (path, exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallError('staging path is not a real directory: %s' % path)
    return info.st_dev, info.st_ino

def assert_directory_identity(path, expected):
    if directory_identity(path) != expected:
        raise InstallError(
            'staging directory changed during preparation; no changes made: %s' %
            path)

def prepare_detection_tree(path, gid):

    _validate_plain_tree(path)
    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            entry = os.path.join(current, name)
            mode = 0o750 if name.endswith(('.py', '.sh')) else 0o640
            os.chmod(entry, mode)
            os.chown(entry, 0, gid, follow_symlinks=False)
        for name in dirs:
            entry = os.path.join(current, name)
            os.chmod(entry, 0o750)
            os.chown(entry, 0, gid, follow_symlinks=False)
        os.chmod(current, 0o750)
        os.chown(current, 0, gid, follow_symlinks=False)
    _validate_plain_tree(path)

def remove_detection_sandbox(path, expected):
    if not path:
        return
    assert_directory_identity(path, expected)
    os.chown(path, 0, 0, follow_symlinks=False)
    os.chmod(path, 0o700)
    shutil.rmtree(path)

def seal_staging(path):

    _validate_plain_tree(path)
    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            entry = os.path.join(current, name)
            mode = 0o750 if name.endswith(('.py', '.sh')) else 0o640
            os.chmod(entry, mode)
            os.chown(entry, 0, 0, follow_symlinks=False)
        for name in dirs:
            entry = os.path.join(current, name)
            os.chmod(entry, 0o750)
            os.chown(entry, 0, 0, follow_symlinks=False)
        os.chmod(current, 0o750)
        os.chown(current, 0, 0, follow_symlinks=False)
    _validate_plain_tree(path)
    root_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(root_fd)
    finally:
        os.close(root_fd)

def validate_root_run_installation(plan, target_python, uid):
    if int(uid) != 0:
        return
    paths = (
        (plan['klipper_dir'], True),
        (plan['config_dir'], True),
        (plan['printer_cfg'], False),
        (target_python, False),
    )
    for path, is_directory in paths:
        resolved = os.path.realpath(path)
        try:
            info = os.stat(resolved)
        except OSError as exc:
            raise InstallError(
                'root-run Klipper path is unavailable: %s (%s)' %
                (path, exc))
        if is_directory and not stat.S_ISDIR(info.st_mode):
            raise InstallError('root-run Klipper path is not a directory: %s' % path)
        if not is_directory and not stat.S_ISREG(info.st_mode):
            raise InstallError('root-run Klipper path is not a regular file: %s' % path)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise InstallError(
                'root-run Klipper requires root-owned, non-writable paths; '
                'refusing unsafe path: %s' % path)

def chown_tree(path, uid, gid):
    _validate_plain_tree(path)
    for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            os.chown(os.path.join(root, name), uid, gid,
                     follow_symlinks=False)
        for name in dirs:
            os.chown(os.path.join(root, name), uid, gid,
                     follow_symlinks=False)
        os.chown(root, uid, gid, follow_symlinks=False)
    _validate_plain_tree(path)

def symlink_inside(path, root):
    return os.path.islink(path) and os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)

def symlink_record(path):
    info = os.lstat(path)
    if not stat.S_ISLNK(info.st_mode):
        raise InstallError('managed module path is not a symlink: %s' % path)
    return (os.readlink(path), info)

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
            raise InstallError(
                'managed module link changed before rollback: %s' % path)
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

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
        status, _info = _open_regular_fd(
            '/proc/%d/status' % pid, 1024 * 1024, InstallError)
        for line in status.decode('utf-8', 'replace').splitlines():
            if line.startswith('Name:'):
                names.append(line.split(':', 1)[1].strip().lower())
                break
    except InstallError:
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
        if pid == os.getpid():
            continue
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
    pids = matching_klippy_pids(klipper_dir, printer_cfg)
    remaining = wait_pids_gone(pids, 12.0)
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
        raise InstallError('Klipper did not stop completely; remaining PIDs: %s' % ', '.join(map(str, remaining)))

def start_service_strict(service, klipper_dir, printer_cfg):
    if matching_klippy_pids(klipper_dir, printer_cfg):
        raise InstallError('refusing to start a second Klipper process')
    run(service_command(service, 'start'))

def configured_serial_ports(bmcu_dir):
    path = os.path.join(bmcu_dir, 'bmcu.cfg')
    values = []
    try:
        data, _info = read_regular(path)
        lines = data.decode('utf-8', 'replace').splitlines()
    except (OSError, InstallError):
        return values
    in_devices = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.lower().startswith('devices:'):
            in_devices = True
            continue
        if in_devices and re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*:', stripped):
            break
        if not in_devices or ',' not in stripped:
            continue
        parts = [part.strip() for part in stripped.split(',')]
        if len(parts) >= 2 and parts[1].startswith('/dev/'):
            values.append(parts[1])
    return sorted(set(values))

def configured_transport_devices(bmcu_dir):

    path = os.path.join(bmcu_dir, 'bmcu.cfg')
    devices = []
    try:
        data, _info = read_regular(path)
        lines = data.decode('utf-8', 'replace').splitlines()
    except (OSError, InstallError):
        return devices
    in_devices = False
    seen_names = set()
    seen_ports = set()
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith(('#', ';')):
            continue
        if stripped.lower().startswith('devices:'):
            in_devices = True
            remainder = stripped.split(':', 1)[1].strip()
            if not remainder:
                continue
            stripped = remainder
        elif in_devices and re.match(
                r'^[A-Za-z_][A-Za-z0-9_]*\s*[:=]', stripped):
            break
        if not in_devices or ',' not in stripped:
            continue
        parts = [part.strip() for part in stripped.split(',')]
        if len(parts) not in (2, 3):
            continue
        name, port = parts[0], parts[1]
        if (re.fullmatch(r'[A-Za-z0-9_.-]+', name) is None or
                not port.startswith('/dev/')):
            continue
        canonical = os.path.realpath(port) if os.path.exists(port) else port
        if name in seen_names or canonical in seen_ports:
            continue
        seen_names.add(name)
        seen_ports.add(canonical)
        devices.append((name, port))
    return devices

def _load_transport_records(bmcu_dir):
    path = os.path.join(bmcu_dir, 'transport-processes.json')
    if os.path.islink(path) or not os.path.isfile(path):
        return {}
    try:
        data, _info = read_regular(path)
        value = json.loads(data.decode('utf-8'))
    except Exception as exc:
        raise InstallError(
            'cannot read managed BMCU transport records: %s' % exc)
    records = value.get('devices', {}) if isinstance(value, dict) else None
    if not isinstance(records, dict):
        raise InstallError('managed BMCU transport records are malformed')
    return records

def _managed_transport_record(record, bmcu_dir, require_alive=True):
    if not isinstance(record, dict):
        return None
    try:
        pid = int(record.get('pid', 0) or 0)
    except (TypeError, ValueError):
        return None
    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    daemon = os.path.realpath(str(record.get('daemon', '') or ''))
    socket_path = str(record.get('socket', '') or '')
    if (pid <= 1 or os.path.basename(daemon) != 'bmcu_transportd.py' or
            not daemon.startswith(runtime + os.sep)):
        return None
    argv = read_proc_cmdline(pid)
    command = ' '.join(argv)
    alive = bool(command and daemon in command and
                 'bmcu_transportd.py' in command)
    if require_alive and not alive:
        return None
    return pid, daemon, socket_path, alive

def stop_managed_transport_processes(bmcu_dir):

    try:
        records = _load_transport_records(bmcu_dir)
    except InstallError:

        return 0
    stopped = 0
    sockets = []
    pids = []
    for name, record in records.items():
        validated = _managed_transport_record(
            record, bmcu_dir, require_alive=False)
        if validated is None:
            continue
        pid, _daemon, socket_path, alive = validated
        if socket_path:
            sockets.extend((socket_path, socket_path + '.ctl'))
        if not alive:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            pids.append(pid)
            stopped += 1
        except ProcessLookupError:
            pass
    remaining = wait_pids_gone(pids, 3.0)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    remaining = wait_pids_gone(remaining, 1.0)
    if remaining:
        raise InstallError(
            'managed BMCU transport did not stop: %s' %
            ', '.join(map(str, remaining)))
    for socket_path in sockets:
        try:
            if os.path.lexists(socket_path):
                os.unlink(socket_path)
        except OSError:
            pass
    return stopped

def verify_managed_transport_processes(bmcu_dir):

    expected = dict(configured_transport_devices(bmcu_dir))
    records = _load_transport_records(bmcu_dir)
    if set(records) != set(expected):
        missing = sorted(set(expected) - set(records))
        unexpected = sorted(set(records) - set(expected))
        details = []
        if missing:
            details.append('missing: %s' % ', '.join(missing))
        if unexpected:
            details.append('unexpected: %s' % ', '.join(unexpected))
        raise InstallError(
            'BMCU transport sidecar set does not match configured devices (%s)' %
            '; '.join(details))
    for name, port in sorted(expected.items()):
        record = records.get(name)
        validated = _managed_transport_record(record, bmcu_dir)
        if validated is None:
            raise InstallError(
                'BMCU transport sidecar is not alive for %s' % name)
        _pid, _daemon, socket_path, _alive = validated
        if str(record.get('port', '') or '') != port:
            raise InstallError(
                'BMCU transport sidecar port mismatch for %s' % name)
        control_path = socket_path + '.ctl'
        if (not socket_path or os.path.islink(socket_path) or
                os.path.islink(control_path) or
                not os.path.exists(socket_path) or
                not stat.S_ISSOCK(os.stat(socket_path).st_mode) or
                not os.path.exists(control_path) or
                not stat.S_ISSOCK(os.stat(control_path).st_mode)):
            raise InstallError(
                'BMCU transport sidecar sockets are unavailable for %s' % name)

    return len(expected)

def _load_planner_record(bmcu_dir):
    path = os.path.join(bmcu_dir, 'planner-process.json')
    if os.path.islink(path) or not os.path.isfile(path):
        return {}
    try:
        data, _info = read_regular(path)
        value = json.loads(data.decode('utf-8'))
    except Exception as exc:
        raise InstallError('cannot read managed U1 planner record: %s' % exc)
    if not isinstance(value, dict):
        raise InstallError('managed U1 planner record is malformed')
    return value

def _managed_planner_record(record, bmcu_dir, require_alive=True):
    if not isinstance(record, dict):
        return None
    try:
        pid = int(record.get('pid', 0) or 0)
    except (TypeError, ValueError):
        return None
    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    daemon = os.path.realpath(str(record.get('daemon', '') or ''))
    socket_path = str(record.get('socket', '') or '')
    if (pid <= 1 or os.path.basename(daemon) != 'bmcu_plannerd.py' or
            not daemon.startswith(runtime + os.sep)):
        return None
    argv = read_proc_cmdline(pid)
    command = ' '.join(argv)
    alive = bool(command and daemon in command and 'bmcu_plannerd.py' in command)
    if require_alive and not alive:
        return None
    return pid, daemon, socket_path, alive

def stop_managed_planner_process(bmcu_dir):
    try:
        record = _load_planner_record(bmcu_dir)
    except InstallError:
        return 0
    validated = _managed_planner_record(record, bmcu_dir, require_alive=False)
    if validated is None:
        return 0
    pid, _daemon, socket_path, alive = validated
    if alive:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            alive = False
        if alive:
            remaining = wait_pids_gone([pid], 3.0)
            if remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                remaining = wait_pids_gone(remaining, 1.0)
            if remaining:
                raise InstallError('managed U1 planner did not stop')
    if socket_path:
        try:
            if os.path.lexists(socket_path):
                os.unlink(socket_path)
        except OSError:
            pass
    return 1 if alive else 0

def verify_managed_planner_process(bmcu_dir):
    metadata = load_managed_metadata(bmcu_dir)
    if str(metadata.get('platform', '') or '') != 'snapmaker_u1':
        return 0
    if not configured_transport_devices(bmcu_dir):
        return 0
    record = _load_planner_record(bmcu_dir)
    validated = _managed_planner_record(record, bmcu_dir)
    if validated is None:
        raise InstallError('BMCU U1 source planner is not alive')
    _pid, _daemon, socket_path, _alive = validated
    result_dir = str(record.get('result_dir', '') or '')
    if (not socket_path or os.path.islink(socket_path) or
            not os.path.exists(socket_path) or
            not stat.S_ISSOCK(os.stat(socket_path).st_mode)):
        raise InstallError('BMCU U1 source planner socket is unavailable')
    if (not result_dir or os.path.islink(result_dir) or
            not os.path.isdir(result_dir) or
            stat.S_IMODE(os.stat(result_dir).st_mode) != 0o700):
        raise InstallError('BMCU U1 source planner result directory is unsafe')
    return 1

def device_holder_pids(device):
    real_device = os.path.realpath(device)
    holders = []
    try:
        entries = os.listdir('/proc')
    except OSError:
        return holders
    for item in entries:
        if not item.isdigit():
            continue
        pid = int(item)
        fd_dir = '/proc/%d/fd' % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.path.realpath(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target == real_device:
                holders.append(pid)
                break
    return sorted(set(holders))

def release_managed_serial_holders(bmcu_dir):
    managed_names = (
        'klippy.py', 'detect_bmcu.py', 'bmcu_isp.py', 'bmcu_update.py',
        'bmcu_runtime.py', 'bmcu_cli.py', 'bmcu_doctor.py',
        'bmcu_transportd.py')
    for port in configured_serial_ports(bmcu_dir):
        remaining = device_holder_pids(port)
        for pid in remaining:
            argv = read_proc_cmdline(pid)
            text = ' '.join(argv)
            if not any(name in text for name in managed_names):
                raise InstallError(
                    'BMCU serial port %s is held by foreign PID %d: %s' %
                    (port, pid, text or '<unknown>'))
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
            remaining = wait_pids_gone(remaining, 2.0)
        if remaining:
            raise InstallError(
                'BMCU serial port %s remains busy after stopping managed helpers: %s' %
                (port, ', '.join(map(str, remaining))))

def stop_managed_panel_process(bmcu_dir):

    pidfile = os.path.join(bmcu_dir, 'panel-process.json')
    try:
        if os.path.islink(pidfile) or not os.path.isfile(pidfile):
            return False
        with open(pidfile, 'r') as stream:
            record = json.load(stream)
        pid = int(record.get('pid', 0) or 0)
        server = os.path.realpath(str(record.get('server', '') or ''))
    except Exception:
        return False
    if (pid <= 1 or os.path.basename(server) != 'bmcu_panel_server.py' or
            not os.path.commonpath((server, os.path.realpath(bmcu_dir))) ==
            os.path.realpath(bmcu_dir)):
        return False
    argv = read_proc_cmdline(pid)
    command = ' '.join(argv)
    if not command:
        return False
    if 'bmcu_panel_server.py' not in command or server not in command:
        raise InstallError(
            'panel pid file points to a foreign process; refusing pid %d' % pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    remaining = wait_pids_gone([pid], 3.0)
    if remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        remaining = wait_pids_gone([pid], 1.0)
    if remaining:
        raise InstallError('managed BMCU panel did not stop: pid %d' % pid)
    return True

def write_new_atomic(path, data, mode=0o755):
    parent = os.path.dirname(path)
    if os.path.lexists(path):
        raise InstallError('refusing to replace an existing path: %s' % path)
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
            raise InstallError('path appeared during atomic creation: %s' % path)
        except OSError as exc:
            raise InstallError('cannot create file without replacing an existing path: %s (%s)' % (path, exc))
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

def ensure_root_directory(path, mode=0o755):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent != path:
        _trusted_root_path(parent, require_directory=True)
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
            raise InstallError('system directory path is occupied: %s' % path)
    else:
        os.mkdir(path, mode)
        os.chown(path, 0, 0)
        _sync_path(parent)
    info = os.stat(path)
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise InstallError(
            'system directory is not root-owned or is writable: %s' % path)
    return path

def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"

def ownership_value_matches(value):
    return OWNER_RE.fullmatch(str(value or '')) is not None

def ownership_marker_matches(data):
    if not isinstance(data, (bytes, bytearray)):
        return False
    return ownership_value_matches(
        bytes(data).decode('utf-8', 'replace').strip())

def u1_serial_rule_marker_matches(first_line):
    prefix = '# Managed by '
    suffix = ' - ' + U1_SERIAL_RULE_LABEL
    if not first_line.startswith(prefix) or not first_line.endswith(suffix):
        return False
    return ownership_value_matches(first_line[len(prefix):-len(suffix)])

def u1_serial_rule_bytes(target_group):
    group = str(target_group or '').strip()
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_.-]*$', group):
        raise InstallError('unsafe Snapmaker U1 serial group name: %s' % group)
    return (
        U1_SERIAL_RULE_MARKER + '\n' +
        'SUBSYSTEM=="tty", KERNEL=="ttyUSB*", GROUP="%s", MODE="0660"\n' % group +
        'SUBSYSTEM=="tty", KERNEL=="ttyCH343USB*", GROUP="%s", MODE="0660"\n' % group
    ).encode('utf-8')

def managed_u1_serial_rule(data):
    if not isinstance(data, (bytes, bytearray)):
        return False
    try:
        text = bytes(data).decode('utf-8')
    except UnicodeDecodeError:
        return False
    first_line = text.splitlines()[0] if text.splitlines() else ''
    return (u1_serial_rule_marker_matches(first_line) and
            'MODE="0660"' in text and
            (('KERNEL=="ttyUSB*"' in text and 'KERNEL=="ttyCH343USB*"' in text) or
             ('ATTRS{idVendor}=="%s"' % U1_SERIAL_VENDOR in text and
              'ATTRS{idProduct}=="7523"' in text)))

def grant_u1_serial_access(gid):
    changed = []
    candidates = (glob.glob('/dev/serial/by-id/*') +
                  glob.glob('/dev/serial/by-path/*') +
                  glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyCH343USB*'))
    for path in sorted(set(candidates)):
        resolved = os.path.realpath(path)
        if resolved in changed:
            continue
        tty = os.path.basename(resolved)
        if not tty.startswith(('ttyUSB', 'ttyCH343USB')):
            continue
        try:
            info = os.stat(resolved)
        except OSError:
            continue
        if not stat.S_ISCHR(info.st_mode):
            continue
        os.chown(resolved, 0, int(gid))
        os.chmod(resolved, 0o660)
        changed.append(resolved)
    return changed

def reload_u1_serial_rules():
    udevadm = shutil.which('udevadm')
    if not udevadm:
        return False
    run([udevadm, 'control', '--reload-rules'])
    return True

def u1_runner_bytes(target_user, uid, gid, target_python, metadata_path):
    bootstrap = os.path.join(
        os.path.dirname(metadata_path), 'scripts', 'bmcu_host_bootstrap.py')
    groups = supplementary_groups(target_user, gid)
    home = pwd.getpwnam(target_user).pw_dir
    text = """#!/usr/bin/python3
# Managed by BMCU-Klipper. Generated root-owned privilege dropper.
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

if os.geteuid() != 0:
    raise SystemExit('BMCU U1 bootstrap runner must start as root')

# S60klipper powers the U1 hardware before invoking this runner. Repair the
# freshly enumerated USB-TTL device immediately before start-stop-daemon drops to
# the normal Klipper user. Do not hide a chmod/chown failure: otherwise Klipper
# can start successfully while BMCU remains offline with EACCES.
def grant_serial_access():
    seen = set()
    matched = []
    failures = []
    for path in sorted(set(
            glob.glob('/dev/serial/by-id/*') +
            glob.glob('/dev/serial/by-path/*') +
            glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyCH343USB*'))):
        resolved = os.path.realpath(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        tty = os.path.basename(resolved)
        if not tty.startswith(('ttyUSB', 'ttyCH343USB')):
            continue
        try:
            info = os.stat(resolved)
            if not stat.S_ISCHR(info.st_mode):
                continue
            os.chown(resolved, 0, GID)
            os.chmod(resolved, 0o660)
            info = os.stat(resolved)
            mode = stat.S_IMODE(info.st_mode)
            if info.st_gid != GID or mode != 0o660:
                raise OSError(
                    'permission repair did not persist (gid=%%d mode=%%04o)' %%
                    (info.st_gid, mode))
            matched.append(resolved)
        except OSError as exc:
            failures.append('%%s: %%s' %% (resolved, exc))
    if failures:
        raise SystemExit(
            'BMCU U1 serial permission repair failed: ' + '; '.join(failures))
    return matched

MATCHED_SERIAL = grant_serial_access()
os.setgroups(GROUPS)
os.setgid(GID)
os.setuid(UID)
for serial_path in MATCHED_SERIAL:
    if not os.access(serial_path, os.R_OK | os.W_OK):
        raise SystemExit(
            'BMCU U1 serial device is not readable/writable by Klipper user: %%s' %%
            serial_path)
environment = {
    'PATH': '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin',
    'HOME': HOME,
    'LANG': 'C',
    'LC_ALL': 'C',
    'PYTHONDONTWRITEBYTECODE': '1',
}
os.execve(
    PYTHON,
    [PYTHON, '-I', BOOTSTRAP, '--repair', '--metadata', METADATA, '--quiet'],
    environment)
""" % (int(uid), int(gid), list(groups), os.path.realpath(target_python),
         os.path.realpath(bootstrap), os.path.realpath(metadata_path), home)
    return text.encode('utf-8')

def u1_inline_block(system_python, runner_path, newline='\n'):
    lines = [
        U1_SERVICE_BEGIN,
        '# Managed by BMCU-Klipper - called only at each actual Klipper launch.',
        '# The call sites are injected immediately before start-stop-daemon -S,',
        '# after Snapmaker has powered and re-enumerated the U1 USB hardware.',
        'bmcu_prepare_klipper_start()',
        '{',
        '  if ! %s -I %s; then' % (
            shell_quote(system_python), shell_quote(runner_path)),
        '    echo "BMCU-Klipper host bootstrap failed" >&2',
        '    return 1',
        '  fi',
        '}',
        U1_SERVICE_END,
        '',
    ]
    return newline.join(lines)

def managed_boot_hook(path):
    if not os.path.lexists(path):
        return False
    try:
        data, _info = read_regular(path)
    except (OSError, InstallError):
        return False
    return MANAGED_HEADER_RE.search(data[:1024]) is not None

def systemd_unit_name(service):
    name = str((service or {}).get('name') or 'klipper')
    return name if name.endswith('.service') else name + '.service'

def planned_boot_hook(platform_id, service):
    if platform_id == 'snapmaker_u1':
        return '', 'snapmaker-inline-bootstrap'
    if (service or {}).get('backend') == 'systemd':
        unit = systemd_unit_name(service)
        return os.path.join('/etc/systemd/system', unit + '.d', SYSTEMD_DROPIN_NAME), 'systemd-dropin'
    return '', ''

def systemd_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'

def systemd_hook_bytes(target_python, metadata_path):
    script = os.path.join(os.path.dirname(metadata_path), 'scripts', 'bmcu_host_bootstrap.py')
    text = """# Managed by BMCU-Klipper - repair persistent Klipper modules before Klipper starts.
[Service]
ExecStartPre=%s -I %s --repair --metadata %s --quiet
""" % (
        systemd_quote(target_python),
        systemd_quote(script),
        systemd_quote(metadata_path),
    )
    return text.encode('utf-8')

def _u1_generic_hook_loop(text):
    compact = re.sub(r'\s+', ' ', text)
    return ('/etc/hooks/klipper.d/*.sh' in text and
            '. "$hook"' in text and
            'for hook in ' in compact)

def _u1_patch_service_data(data, system_python, runner_path):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('Snapmaker U1 Klipper service is not UTF-8')
    cleaned_data, _removed = _remove_u1_service_block(data)
    try:
        cleaned = cleaned_data.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('Snapmaker U1 Klipper service is not UTF-8')
    newline = '\r\n' if '\r\n' in cleaned else '\n'
    lines = cleaned.splitlines(True)

    insert_at = None
    for index, line in enumerate(lines):
        if re.match(r'^\s*log\s*\(\s*\)\s*', line):
            insert_at = index
            break
    if insert_at is None:
        raise InstallError(
            'Snapmaker U1 S60klipper has an unknown layout; refusing an unsafe boot patch')
    block = u1_inline_block(system_python, runner_path, newline) + newline
    lines.insert(insert_at, block)

    launch_re = re.compile(r'^(?P<indent>[ \t]*)start-stop-daemon[ \t]+-S(?:[ \t]|$)')
    patched_lines = []
    launch_count = 0
    for line in lines:
        match = launch_re.match(line)
        if match:
            indent = match.group('indent')
            patched_lines.append(indent + U1_SERVICE_CALL_MARKER + newline)
            patched_lines.append(indent + U1_SERVICE_CALL + newline)
            launch_count += 1
        patched_lines.append(line)
    if launch_count == 0:
        raise InstallError(
            'Snapmaker U1 S60klipper contains no Klipper start-stop-daemon launch')

    patched_text = ''.join(patched_lines)

    power_match = re.search(
        r'(?m)^\s*["\']?\$LAVA_IO["\']?[ \t]+set[ \t]+.*(?:MAIN_MCU_POWER|HEAD_MCU_POWER)=1',
        patched_text)
    first_call = patched_text.find(U1_SERVICE_CALL_MARKER)
    if power_match is not None and first_call <= power_match.start():
        raise InstallError(
            'Snapmaker U1 serial repair would run before hardware power-up')
    patched = patched_text.encode('utf-8')
    return patched, patched != data

def _remove_u1_service_block(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise InstallError('Snapmaker U1 S60klipper is not UTF-8')
    begin_count = len(re.findall(
        r'(?m)^' + re.escape(U1_SERVICE_BEGIN) + r'\r?$', text))
    end_count = len(re.findall(
        r'(?m)^' + re.escape(U1_SERVICE_END) + r'\r?$', text))
    if begin_count != end_count or begin_count > 1:
        raise InstallError(
            'Snapmaker U1 S60klipper contains partial or duplicate BMCU markers')
    cleaned = text
    removed = False
    if begin_count:
        pattern = re.compile(
            r'(?ms)^' + re.escape(U1_SERVICE_BEGIN) + r'\r?\n.*?^' +
            re.escape(U1_SERVICE_END) + r'\r?\n(?:\r?\n)?')
        cleaned, count = pattern.subn('', cleaned, count=1)
        if count != 1:
            raise InstallError('Snapmaker U1 BMCU service block is malformed')
        removed = True

    call_pattern = re.compile(
        r'(?m)^(?P<indent>[ \t]*)' + re.escape(U1_SERVICE_CALL_MARKER) +
        r'\r?\n(?P=indent)' + re.escape(U1_SERVICE_CALL) + r'\r?\n')
    cleaned, call_count = call_pattern.subn('', cleaned)
    removed = removed or bool(call_count)
    if (U1_SERVICE_CALL_MARKER in cleaned or
            re.search(r'(?m)^\s*' + re.escape(U1_SERVICE_CALL) + r'\s*$', cleaned)):
        raise InstallError(
            'Snapmaker U1 S60klipper contains an incomplete BMCU launch call')
    return cleaned.encode('utf-8'), removed

def _sync_path(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

def _managed_u1_runner_dir():
    if not os.path.lexists(U1_RUNNER_DIR):
        return False
    try:
        directory_info = os.lstat(U1_RUNNER_DIR)
    except OSError:
        return False
    if (stat.S_ISLNK(directory_info.st_mode) or
            not stat.S_ISDIR(directory_info.st_mode) or
            directory_info.st_uid != 0 or directory_info.st_gid != 0 or
            stat.S_IMODE(directory_info.st_mode) != 0o700):
        return False
    allowed = {os.path.basename(U1_RUNNER_MARKER), os.path.basename(U1_RUNNER)}
    try:
        if not set(os.listdir(U1_RUNNER_DIR)).issubset(allowed):
            return False
        marker_data, marker_info = read_regular(U1_RUNNER_MARKER)
    except Exception:
        return False
    if (marker_info.st_uid != 0 or marker_info.st_gid != 0 or
            stat.S_IMODE(marker_info.st_mode) != 0o600 or
            not ownership_marker_matches(marker_data)):
        return False
    if os.path.lexists(U1_RUNNER):
        try:
            _runner_data, runner_info = read_regular(U1_RUNNER)
        except Exception:
            return False
        if (runner_info.st_uid != 0 or runner_info.st_gid != 0 or
                stat.S_IMODE(runner_info.st_mode) != 0o700):
            return False
    return True

def install_boot_repair(platform_id, service, target_user, uid, gid,
                        target_python, metadata_path):
    if platform_id == 'snapmaker_u1':
        persistence_dir = os.path.dirname(U1_PERSISTENCE_MARKER)
        if not os.path.isdir(persistence_dir):
            raise InstallError(
                'Snapmaker U1 persistence directory is unavailable: %s' % persistence_dir)
        if (os.path.lexists(U1_PERSISTENCE_MARKER) and
                not regular_file(U1_PERSISTENCE_MARKER)):
            raise InstallError(
                'Snapmaker U1 persistence marker path is unsafe: %s' %
                U1_PERSISTENCE_MARKER)

        service_data, service_info = read_regular(U1_KLIPPER_SERVICE)
        service_xattrs = read_xattrs(U1_KLIPPER_SERVICE)
        system_python = trusted_program('python3')
        serial_rule_supported = os.path.isdir('/etc/udev')
        serial_rule_data = u1_serial_rule_bytes(target_group=grp.getgrgid(gid).gr_name)
        serial_rule_previous = None
        serial_rule_previous_info = None
        serial_rule_previous_xattrs = None
        serial_rule_dir_created = False
        if serial_rule_supported and os.path.lexists(U1_SERIAL_RULE):
            serial_rule_previous, serial_rule_previous_info = read_regular(
                U1_SERIAL_RULE)
            serial_rule_previous_xattrs = read_xattrs(U1_SERIAL_RULE)
            if not managed_u1_serial_rule(serial_rule_previous):
                raise InstallError(
                    'Snapmaker U1 serial rule path is occupied by a foreign file: %s' %
                    U1_SERIAL_RULE)
            if (serial_rule_previous_info.st_uid != 0 or
                    serial_rule_previous_info.st_gid != 0 or
                    stat.S_IMODE(serial_rule_previous_info.st_mode) != 0o644):
                raise InstallError(
                    'Snapmaker U1 serial rule has unsafe ownership or mode')
        runner_data = u1_runner_bytes(
            target_user, uid, gid, target_python, metadata_path)
        patched_service, service_changed = _u1_patch_service_data(
            service_data, system_python, U1_RUNNER)

        old_hook_previous = None
        old_hook_previous_info = None
        old_hook_previous_xattrs = None
        if os.path.lexists(U1_BOOT_HOOK):
            if not managed_boot_hook(U1_BOOT_HOOK):
                raise InstallError(
                    'old BMCU hook path is occupied by a foreign file: %s' %
                    U1_BOOT_HOOK)
            old_hook_previous, old_hook_previous_info = read_regular(
                U1_BOOT_HOOK)
            old_hook_previous_xattrs = read_xattrs(U1_BOOT_HOOK)
        legacy_previous = None
        legacy_previous_info = None
        legacy_previous_xattrs = None
        if os.path.lexists(U1_LEGACY_BOOT_HOOK):
            if not managed_boot_hook(U1_LEGACY_BOOT_HOOK):
                raise InstallError(
                    'legacy BMCU U1 hook path is occupied by a foreign file: %s' %
                    U1_LEGACY_BOOT_HOOK)
            legacy_previous, legacy_previous_info = read_regular(
                U1_LEGACY_BOOT_HOOK)
            legacy_previous_xattrs = read_xattrs(U1_LEGACY_BOOT_HOOK)

        runner_dir_created = False
        runner_previous = None
        runner_previous_info = None
        runner_previous_xattrs = None
        runner_marker_previous = None
        runner_marker_previous_info = None
        runner_marker_previous_xattrs = None
        if os.path.lexists(U1_RUNNER_DIR):
            if not _managed_u1_runner_dir():
                raise InstallError(
                    'Snapmaker U1 runner path is occupied by a foreign directory: %s' %
                    U1_RUNNER_DIR)
            if os.path.lexists(U1_RUNNER):
                runner_previous, runner_previous_info = read_regular(U1_RUNNER)
                runner_previous_xattrs = read_xattrs(U1_RUNNER)
            runner_marker_previous, runner_marker_previous_info = read_regular(
                U1_RUNNER_MARKER)
            runner_marker_previous_xattrs = read_xattrs(U1_RUNNER_MARKER)

        marker_created = False
        record = {
            'hook': '', 'kind': 'snapmaker-inline-bootstrap',
            'hook_previous': None, 'marker_created': False,
            'u1_service_changed': service_changed,
            'u1_service_previous': service_data if service_changed else None,
            'u1_service_installed': patched_service if service_changed else None,
            'old_hook_previous': old_hook_previous,
            'old_hook_previous_info': old_hook_previous_info,
            'old_hook_previous_xattrs': old_hook_previous_xattrs,
            'legacy_previous': legacy_previous,
            'legacy_previous_info': legacy_previous_info,
            'legacy_previous_xattrs': legacy_previous_xattrs,
            'runner_dir_created': False,
            'runner_previous': runner_previous,
            'runner_previous_info': runner_previous_info,
            'runner_previous_xattrs': runner_previous_xattrs,
            'runner_marker_previous': runner_marker_previous,
            'runner_marker_previous_info': runner_marker_previous_info,
            'runner_marker_previous_xattrs': runner_marker_previous_xattrs,
            'runner_installed': runner_data,
            'runner_marker_installed': (OWNERSHIP_MARKER + '\n').encode('utf-8'),
            'serial_rule_supported': serial_rule_supported,
            'serial_rule_previous': serial_rule_previous,
            'serial_rule_previous_info': serial_rule_previous_info,
            'serial_rule_previous_xattrs': serial_rule_previous_xattrs,
            'serial_rule_installed': serial_rule_data,
            'serial_rule_dir_created': False,
            'serial_rule_written': False,
            'runner_written': False,
            'runner_marker_written': False,
            'u1_service_written': False,
            'old_hook_removed': False,
            'legacy_removed': False,
        }
        try:
            if os.path.lexists(U1_PERSISTENCE_MARKER):
                if not regular_file(U1_PERSISTENCE_MARKER):
                    raise InstallError(
                        'Snapmaker U1 persistence marker changed to an '
                        'unsafe path before installation')
            else:
                descriptor = os.open(
                    U1_PERSISTENCE_MARKER,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                    getattr(os, 'O_CLOEXEC', 0) |
                    getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.fsync(descriptor)
                os.close(descriptor)
                marker_created = True
                record['marker_created'] = True
            _sync_path(persistence_dir)

            if serial_rule_supported:
                if not os.path.exists(U1_SERIAL_RULE_DIR):
                    ensure_root_directory(U1_SERIAL_RULE_DIR, 0o755)
                    serial_rule_dir_created = True
                    record['serial_rule_dir_created'] = True
                if serial_rule_previous is None:
                    write_new_atomic(U1_SERIAL_RULE, serial_rule_data, 0o644)
                else:
                    atomic_write(
                        U1_SERIAL_RULE, serial_rule_data,
                        serial_rule_previous_info,
                        serial_rule_previous_xattrs,
                        expected=serial_rule_previous)
                os.chown(U1_SERIAL_RULE, 0, 0)
                os.chmod(U1_SERIAL_RULE, 0o644)
                record['serial_rule_written'] = True
                reload_u1_serial_rules()
            grant_u1_serial_access(gid)

            if not os.path.exists(U1_RUNNER_DIR):
                os.mkdir(U1_RUNNER_DIR, 0o700)
                runner_dir_created = True
                record['runner_dir_created'] = True
            os.chown(U1_RUNNER_DIR, 0, 0)
            os.chmod(U1_RUNNER_DIR, 0o700)
            marker_data = (OWNERSHIP_MARKER + '\n').encode('utf-8')
            if runner_marker_previous is None:
                write_new_atomic(U1_RUNNER_MARKER, marker_data, 0o600)
            else:
                atomic_write(
                    U1_RUNNER_MARKER, marker_data,
                    runner_marker_previous_info,
                    runner_marker_previous_xattrs,
                    expected=runner_marker_previous)
            record['runner_marker_written'] = True
            if runner_previous is None:
                write_new_atomic(U1_RUNNER, runner_data, 0o700)
            else:
                atomic_write(
                    U1_RUNNER, runner_data, runner_previous_info,
                    runner_previous_xattrs, expected=runner_previous)
            record['runner_written'] = True
            os.chown(U1_RUNNER_MARKER, 0, 0)
            os.chmod(U1_RUNNER_MARKER, 0o600)
            os.chown(U1_RUNNER, 0, 0)
            os.chmod(U1_RUNNER, 0o700)
            _sync_path(U1_RUNNER_DIR)

            if service_changed:
                atomic_write(
                    U1_KLIPPER_SERVICE, patched_service,
                    service_info, service_xattrs, expected=service_data)
                record['u1_service_written'] = True

            if old_hook_previous is not None:
                unlink_regular_expected(
                    U1_BOOT_HOOK, old_hook_previous,
                    old_hook_previous_info)
                record['old_hook_removed'] = True
                try:
                    os.rmdir(U1_HOOK_DIR)
                except OSError:
                    pass
            if legacy_previous is not None:
                unlink_regular_expected(
                    U1_LEGACY_BOOT_HOOK, legacy_previous,
                    legacy_previous_info)
                record['legacy_removed'] = True
            _sync_path('/etc/init.d')
            return record
        except Exception as original_error:
            try:
                rollback_boot_repair(record)
            except Exception as rollback_error:
                raise InstallError(
                    'Snapmaker U1 integration failed and its local rollback '
                    'was incomplete: %s; rollback: %s' %
                    (original_error, rollback_error))
            raise

    path, kind = planned_boot_hook(platform_id, service)
    if not path:
        return {'hook': '', 'kind': '', 'hook_previous': None, 'marker_created': False}
    _trusted_root_path('/etc/systemd/system', require_directory=True)
    parent = os.path.dirname(path)
    parent_created = not os.path.exists(parent)
    ensure_root_directory(parent, 0o755)
    data = systemd_hook_bytes(target_python, metadata_path)
    previous = None
    if os.path.lexists(path):
        if not managed_boot_hook(path):
            raise InstallError('BMCU boot hook path is occupied by a foreign file: %s' % path)
        previous, _previous_info = read_regular(path)
    record = {
        'hook': path, 'kind': kind, 'hook_previous': previous,
        'hook_installed': data, 'hook_parent_created': parent_created,
        'hook_written': False,
        'marker_created': False,
    }
    try:
        if previous is None:
            write_new_atomic(path, data, 0o644)
        else:
            previous_data, previous_info = read_regular(path)
            if previous_data != previous:
                raise InstallError('managed systemd hook changed before update')
            atomic_write(
                path, data, previous_info, read_xattrs(path),
                expected=previous_data)
        record['hook_written'] = True
        run([trusted_program('systemctl'), 'daemon-reload'])
        return record
    except Exception as original_error:
        if os.path.lexists(path):
            try:
                current, _current_info = read_regular(path)
                if current == data:
                    record['hook_written'] = True
            except Exception:
                pass
        try:
            rollback_boot_repair(record)
        except Exception as rollback_error:
            raise InstallError(
                'systemd integration failed and its local rollback was '
                'incomplete: %s; rollback: %s' %
                (original_error, rollback_error))
        raise

def verify_boot_repair(platform_id, service, metadata_path):
    if platform_id == 'snapmaker_u1':
        if not regular_file(U1_PERSISTENCE_MARKER):
            raise InstallError('Snapmaker U1 persistence marker was not installed')
        service_data, _info = read_regular(U1_KLIPPER_SERVICE)
        try:
            metadata_data, _metadata_info = read_regular(metadata_path)
            metadata = json.loads(metadata_data.decode('utf-8'))
            target_user = str(metadata['user'])
            target_group = str(metadata['group'])
            target_python = os.path.realpath(str(metadata['python']))
            user_info = pwd.getpwnam(target_user)
            group_info = grp.getgrnam(target_group)
        except (KeyError, ValueError, UnicodeDecodeError, TypeError,
                OSError) as exc:
            raise InstallError(
                'managed installation metadata is invalid while verifying '
                'Snapmaker U1 integration: %s' % exc)

        system_python = trusted_program('python3')
        expected_service, changed = _u1_patch_service_data(
            service_data, system_python, U1_RUNNER)
        if changed or expected_service != service_data:
            raise InstallError(
                'Snapmaker U1 direct BMCU bootstrap block differs from '
                'the expected managed content')
        try:
            service_text = service_data.decode('utf-8')
        except UnicodeDecodeError:
            raise InstallError('Snapmaker U1 S60klipper is not UTF-8 after integration')
        block_match = re.search(
            r'(?ms)^' + re.escape(U1_SERVICE_BEGIN) + r'\r?\n(.*?)^' +
            re.escape(U1_SERVICE_END), service_text)
        if not block_match or U1_RUNNER not in block_match.group(1):
            raise InstallError('Snapmaker U1 direct BMCU bootstrap path is incorrect')
        if _u1_generic_hook_loop(block_match.group(1)):
            raise InstallError('Snapmaker U1 still depends on the volatile hook directory')
        if not _managed_u1_runner_dir():
            raise InstallError('Snapmaker U1 privilege-drop runner is missing')
        runner_data, runner_info = read_regular(U1_RUNNER)
        expected_runner = u1_runner_bytes(
            target_user, user_info.pw_uid, group_info.gr_gid,
            target_python, metadata_path)
        if runner_data != expected_runner:
            raise InstallError(
                'Snapmaker U1 privilege-drop runner differs from the '
                'expected managed content')
        if runner_info.st_uid != 0 or runner_info.st_gid != 0 or \
                stat.S_IMODE(runner_info.st_mode) != 0o700:
            raise InstallError('Snapmaker U1 privilege-drop runner has unsafe ownership or mode')
        if os.path.isdir('/etc/udev'):
            rule_data, rule_info = read_regular(U1_SERIAL_RULE)
            expected_rule = u1_serial_rule_bytes(target_group)
            if rule_data != expected_rule:
                raise InstallError('Snapmaker U1 serial access rule differs from expected content')
            if (rule_info.st_uid != 0 or rule_info.st_gid != 0 or
                    stat.S_IMODE(rule_info.st_mode) != 0o644):
                raise InstallError('Snapmaker U1 serial access rule has unsafe ownership or mode')
        grant_u1_serial_access(group_info.gr_gid)
        run(['/bin/sh', '-n', U1_KLIPPER_SERVICE])
        return
    path, kind = planned_boot_hook(platform_id, service)
    if kind == 'systemd-dropin':
        if not managed_boot_hook(path):
            raise InstallError('managed systemd pre-start repair drop-in is missing')
        data, info = read_regular(path)
        try:
            metadata_data, _metadata_info = read_regular(metadata_path)
            metadata = json.loads(metadata_data.decode('utf-8'))
            target_python = os.path.realpath(str(metadata['python']))
        except (KeyError, ValueError, UnicodeDecodeError, TypeError) as exc:
            raise InstallError(
                'managed installation metadata is invalid while verifying '
                'systemd integration: %s' % exc)
        if data != systemd_hook_bytes(target_python, metadata_path):
            raise InstallError('managed systemd pre-start repair drop-in differs from expected content')
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise InstallError('managed systemd pre-start repair drop-in has unsafe ownership or mode')

def rollback_boot_repair(record):
    if not isinstance(record, dict):
        return
    kind = record.get('kind') or ''
    if kind == 'snapmaker-inline-bootstrap':
        service_previous = record.get('u1_service_previous')
        if service_previous is not None and record.get('u1_service_written'):
            current_data, info = read_regular(U1_KLIPPER_SERVICE)
            if current_data != record.get('u1_service_installed'):
                raise InstallError(
                    'Snapmaker U1 service changed during rollback; refusing '
                    'to overwrite it')
            atomic_write(
                U1_KLIPPER_SERVICE, service_previous,
                info, read_xattrs(U1_KLIPPER_SERVICE),
                expected=current_data)
        old_hook_previous = record.get('old_hook_previous')
        if old_hook_previous is not None and record.get('old_hook_removed'):
            if os.path.lexists(U1_BOOT_HOOK):
                raise InstallError(
                    'old U1 hook path became occupied during rollback: %s' %
                    U1_BOOT_HOOK)
            os.makedirs(U1_HOOK_DIR, mode=0o755, exist_ok=True)
            restore_removed_regular(
                U1_BOOT_HOOK, old_hook_previous,
                record.get('old_hook_previous_info'),
                record.get('old_hook_previous_xattrs'))
        legacy_previous = record.get('legacy_previous')
        if legacy_previous is not None and record.get('legacy_removed'):
            if os.path.lexists(U1_LEGACY_BOOT_HOOK):
                raise InstallError(
                    'legacy U1 hook path became occupied during rollback: %s' %
                    U1_LEGACY_BOOT_HOOK)
            restore_removed_regular(
                U1_LEGACY_BOOT_HOOK, legacy_previous,
                record.get('legacy_previous_info'),
                record.get('legacy_previous_xattrs'))
        if record.get('serial_rule_written'):
            current_rule, current_rule_info = read_regular(U1_SERIAL_RULE)
            if current_rule != record.get('serial_rule_installed'):
                raise InstallError('Snapmaker U1 serial rule changed during rollback')
            previous_rule = record.get('serial_rule_previous')
            if previous_rule is None:
                unlink_regular_expected(
                    U1_SERIAL_RULE, current_rule, current_rule_info)
            else:
                atomic_write(
                    U1_SERIAL_RULE, previous_rule, current_rule_info,
                    record.get('serial_rule_previous_xattrs'),
                    expected=current_rule)
            reload_u1_serial_rules()
            if record.get('serial_rule_dir_created'):
                try:
                    os.rmdir(U1_SERIAL_RULE_DIR)
                except OSError:
                    pass
        runner_previous = record.get('runner_previous')
        marker_previous = record.get('runner_marker_previous')
        if record.get('runner_written') and os.path.lexists(U1_RUNNER):
            current_runner, _current_runner_info = read_regular(U1_RUNNER)
            if current_runner != record.get('runner_installed'):
                raise InstallError(
                    'Snapmaker U1 runner changed during rollback')
        if record.get('runner_marker_written') and os.path.lexists(U1_RUNNER_MARKER):
            current_marker, _current_marker_info = read_regular(U1_RUNNER_MARKER)
            if current_marker != record.get('runner_marker_installed'):
                raise InstallError(
                    'Snapmaker U1 runner marker changed during rollback')
        if record.get('runner_written') and runner_previous is not None:
            os.makedirs(U1_RUNNER_DIR, mode=0o700, exist_ok=True)
            current_runner, current_runner_info = read_regular(U1_RUNNER)
            atomic_write(
                U1_RUNNER, runner_previous, current_runner_info,
                record.get('runner_previous_xattrs'),
                expected=current_runner)
        elif record.get('runner_written') and os.path.lexists(U1_RUNNER):
            current_runner, current_runner_info = read_regular(U1_RUNNER)
            unlink_regular_expected(
                U1_RUNNER, current_runner, current_runner_info)
        if record.get('runner_marker_written') and marker_previous is not None:
            os.makedirs(U1_RUNNER_DIR, mode=0o700, exist_ok=True)
            current_marker, current_marker_info = read_regular(U1_RUNNER_MARKER)
            atomic_write(
                U1_RUNNER_MARKER, marker_previous, current_marker_info,
                record.get('runner_marker_previous_xattrs'),
                expected=current_marker)
        elif record.get('runner_marker_written') and os.path.lexists(U1_RUNNER_MARKER):
            current_marker, current_marker_info = read_regular(U1_RUNNER_MARKER)
            unlink_regular_expected(
                U1_RUNNER_MARKER, current_marker, current_marker_info)
        if record.get('runner_dir_created'):
            try:
                os.rmdir(U1_RUNNER_DIR)
            except OSError:
                pass
        if record.get('marker_created'):
            if not os.path.lexists(U1_PERSISTENCE_MARKER):
                raise InstallError(
                    'package-created Snapmaker persistence marker disappeared during rollback')
            marker_data, marker_info = read_regular(U1_PERSISTENCE_MARKER)
            if (marker_data or marker_info.st_uid != 0 or marker_info.st_gid != 0 or
                    stat.S_IMODE(marker_info.st_mode) & 0o022):
                raise InstallError(
                    'package-created Snapmaker persistence marker changed during rollback')
            unlink_regular_expected(
                U1_PERSISTENCE_MARKER, marker_data, marker_info)
            _sync_path(os.path.dirname(U1_PERSISTENCE_MARKER))
        return
    hook = record.get('hook')
    previous = record.get('hook_previous')
    if hook:
        if not record.get('hook_written'):
            if os.path.lexists(hook):
                current, _current_info = read_regular(hook)
                if previous is None or current != previous:
                    raise InstallError(
                        'systemd hook changed during rollback preflight')
            elif previous is not None:
                raise InstallError(
                    'previous systemd hook disappeared during rollback')
        elif os.path.lexists(hook):
            current, current_info = read_regular(hook)
            if current != record.get('hook_installed'):
                raise InstallError(
                    'systemd BMCU hook changed during rollback; refusing to '
                    'overwrite it')
            if previous is None:
                unlink_regular_expected(hook, current, current_info)
            else:
                atomic_write(
                    hook, previous, current_info, read_xattrs(hook),
                    expected=current)
        elif previous is not None:
            write_new_atomic(hook, previous, 0o644)
    if kind == 'systemd-dropin':
        run([trusted_program('systemctl'), 'daemon-reload'])
        if record.get('hook_parent_created'):
            try:
                os.rmdir(os.path.dirname(hook))
            except OSError:
                pass

def populate_runtime(snapshot, runtime, installation):
    os.mkdir(runtime, 0o750)
    prefixes = ('klippy/', 'web/', 'vendor/')
    selected = [relative for relative in snapshot
                if relative.startswith(prefixes)]
    for relative in sorted(selected):
        destination = os.path.join(runtime, *relative.split('/'))
        mode = 0o750 if relative.endswith(('.py', '.sh')) else 0o640
        write_snapshot_file(snapshot, relative, destination, mode)
    for name in RUNTIME_SCRIPTS:
        relative = 'scripts/' + name
        write_snapshot_file(
            snapshot, relative,
            os.path.join(runtime, 'scripts', name), 0o750)
    write_snapshot_file(snapshot, 'version', os.path.join(runtime, 'version'), 0o640)
    write_new_atomic(
        os.path.join(runtime, '.managed-by-bmcu'),
        (OWNERSHIP_MARKER + '\n').encode('utf-8'), 0o640)
    metadata = (json.dumps(
        installation, indent=2, sort_keys=True) + '\n').encode('utf-8')
    write_new_atomic(
        os.path.join(runtime, 'INSTALLATION.json'), metadata, 0o640)

def legacy_installed_uninstaller_bytes(target_python):
    return (
        '#!/bin/sh\nset -eu\n'
        'BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'PY=${PYTHON_BIN:-%s}\n'
        'exec "$PY" "$BASE/runtime/scripts/uninstall.py" "$@"\n' %
        os.path.realpath(target_python)).encode('utf-8')

def remove_legacy_installed_uninstaller(bmcu_dir, target_python):
    path = os.path.join(bmcu_dir, 'uninstall')
    if not os.path.lexists(path):
        return None
    data, info = read_regular(path)
    expected = legacy_installed_uninstaller_bytes(target_python)
    if data != expected:
        raise InstallError(
            'legacy installed uninstaller is modified or foreign; refusing to '
            'delete it automatically: %s' % path)
    snapshot = {
        'path': path, 'data': data, 'info': info, 'xattrs': read_xattrs(path)}
    current, current_info = read_regular(path)
    if current != data or not _same_file_info(current_info, info):
        raise InstallError('legacy installed uninstaller changed before removal')
    unlink_regular_expected(path, current, current_info)
    return snapshot

def restore_legacy_installed_uninstaller(snapshot):
    if not snapshot:
        return
    path = snapshot['path']
    if os.path.lexists(path):
        raise InstallError(
            'legacy uninstaller path became occupied during rollback: %s' % path)
    restore_removed_regular(
        path, snapshot['data'], snapshot['info'], snapshot.get('xattrs'))

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

def validate_repair_identity(metadata, plan, service, target_user, target_group,
                             target_python):
    checks = (
        ('klipper_dir', plan['klipper_dir'], True),
        ('config_dir', plan['config_dir'], True),
        ('printer_cfg', plan['printer_cfg'], True),
        ('python', target_python, True),
        ('user', target_user, False),
        ('group', target_group, False),
        ('platform', plan['platform_id'], False),
    )
    for key, current, is_path in checks:
        recorded = metadata.get(key)
        if not recorded:
            continue
        left = os.path.realpath(str(recorded)) if is_path else str(recorded)
        right = os.path.realpath(str(current)) if is_path else str(current)
        if left != right:
            raise InstallError(
                'existing BMCU installation belongs to a different %s; '
                'refusing to rewire it' % key)
    recorded_service = metadata.get('service')
    if recorded_service is not None:
        if not isinstance(recorded_service, dict):
            raise InstallError('existing BMCU service metadata is invalid')
        if normalized_service(recorded_service) != normalized_service(service):
            raise InstallError(
                'detected Klipper service differs from the service recorded '
                'for this BMCU installation')

def load_managed_metadata(bmcu_dir):
    path = os.path.join(bmcu_dir, 'runtime', 'INSTALLATION.json')
    data, _info = read_regular(path)
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError(
            'existing BMCU installation metadata is invalid: %s' % exc)
    if (not isinstance(value, dict) or
            value.get('product') != 'BMCU-Klipper' or
            not isinstance(value.get('version'), str) or
            not value.get('version')):
        raise InstallError(
            'existing BMCU directory is not a managed BMCU-Klipper installation')
    marker = os.path.join(bmcu_dir, 'runtime', '.managed-by-bmcu')
    marker_data, _marker_info = read_regular(marker)
    if not ownership_marker_matches(marker_data):
        raise InstallError(
            'existing BMCU installation has no valid ownership marker')
    return value

def ensure_include(original):
    cleaned, _removed = clean_bmcu_references(original)
    return add_include(cleaned)[0]

def replace_managed_config(path, data, uid, gid, mode=0o640):

    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise InstallError('managed config directory is unavailable: %s' % parent)
    existed = os.path.lexists(path)
    snapshot = {'path': path, 'existed': existed}
    if existed:
        original, info = read_regular(path)
        snapshot.update(data=original, info=info, xattrs=read_xattrs(path))
        installed_info = atomic_write(
            path, data, info, snapshot['xattrs'], expected=original)
        snapshot.update(installed_data=data, installed_info=installed_info)
        return snapshot

    write_new_atomic(path, data, mode)
    try:
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        directory_fd = os.open(
            parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            current, current_info = read_regular(path)
            if current == data:
                unlink_regular_expected(path, current, current_info)
        except Exception:
            pass
        raise
    installed, installed_info = read_regular(path)
    if installed != data:
        raise InstallError(
            'managed config changed immediately after creation: %s' % path)
    snapshot.update(installed_data=data, installed_info=installed_info)
    return snapshot

def rollback_managed_config(snapshot):
    path = snapshot['path']
    installed_data = snapshot.get('installed_data')
    installed_info = snapshot.get('installed_info')
    current, current_info = read_regular(path)
    if current != installed_data or not _same_file_info(current_info, installed_info):
        raise InstallError(
            'managed config changed during rollback; refusing to overwrite: %s' %
            path)
    if snapshot.get('existed'):
        atomic_write(
            path, snapshot['data'], current_info, snapshot.get('xattrs'),
            expected=current)
        return
    if os.path.lexists(path):
        unlink_regular_expected(path, current, current_info)

def remove_legacy_panel_macros(path):
    if not os.path.lexists(path):
        return None
    data, info = read_regular(path)
    if data.strip():
        print('Preserving non-empty legacy bmcu_panel_macros.cfg; it is no longer included automatically.')
        return None
    snapshot = {
        'path': path,
        'data': data,
        'info': info,
        'xattrs': read_xattrs(path),
    }
    unlink_regular_expected(path, data, info)
    return snapshot

def restore_legacy_panel_macros(snapshot):
    if not snapshot:
        return
    restore_removed_regular(
        snapshot['path'], snapshot['data'], snapshot['info'], snapshot.get('xattrs'))

def repair_existing(snapshot, plan, service, target_user, target_group, target_python,
                    uid, gid, bmcu_dir, state_file, printer_cfg, moonraker,
                    assume_idle=False):
    metadata = load_managed_metadata(bmcu_dir)
    validate_repair_identity(
        metadata, plan, service, target_user, target_group, target_python)
    original_cfg, cfg_info = read_regular(printer_cfg)
    cfg_xattrs = read_xattrs(printer_cfg)
    prepared_cfg = ensure_include(original_cfg)
    klipper_dir = plan['klipper_dir']
    extras_dir = os.path.join(klipper_dir, 'klippy', 'extras')
    if not os.path.isdir(extras_dir):
        raise InstallError('Klipper extras directory is unavailable: %s' % extras_dir)

    for name in MODULES:
        target = os.path.join(extras_dir, name)
        if not os.path.lexists(target):
            continue
        if not os.path.islink(target):
            raise InstallError('Klipper module path is occupied by a foreign file: %s' % target)
        resolved = os.path.realpath(target)
        if not resolved.startswith(os.path.realpath(bmcu_dir) + os.sep):
            raise InstallError('Klipper module path contains a foreign link: %s' % target)

    data_root = os.path.dirname(os.path.realpath(plan['config_dir']))
    stage = tempfile.mkdtemp(prefix='.bmcu-repair-', dir=data_root)
    stage_identity = directory_identity(stage)
    old_runtime = os.path.join(bmcu_dir, '.runtime-before-repair-%d' % os.getpid())
    runtime_final = os.path.join(bmcu_dir, 'runtime')
    original_runtime_identity = directory_identity(runtime_final)
    runtime_stage = os.path.join(stage, 'runtime')
    previous_hook = {}
    runtime_swapped = False
    replacement_runtime_identity = None
    cfg_changed = False
    service_stopped = False
    transaction_started = False
    failed_runtime = os.path.join(
        bmcu_dir, '.runtime-failed-repair-%d' % os.getpid())
    managed_config_snapshots = []
    legacy_uninstaller_snapshot = None
    legacy_panel_macros_snapshot = None
    panel_cfg_path = os.path.join(bmcu_dir, 'bmcu_panel.cfg')
    panel_cfg_data, _panel_cfg_info = read_regular(panel_cfg_path)
    panel_token = panel_token_from_bytes(panel_cfg_data)
    panel_cfg_migrated, panel_token = ensure_panel_token(panel_cfg_data, panel_token)
    managed_config_payloads = {'UNINSTALL.txt': UNINSTALL_NOTE}
    if panel_cfg_migrated != panel_cfg_data:
        managed_config_payloads['bmcu_panel.cfg'] = panel_cfg_migrated
    bmcu_cfg_path = os.path.join(bmcu_dir, 'bmcu.cfg')
    bmcu_cfg_data, _bmcu_cfg_info = read_regular(bmcu_cfg_path)
    bmcu_cfg_migrated = migrate_lightweight_bmcu_cfg(bmcu_cfg_data)
    if bmcu_cfg_migrated != bmcu_cfg_data:
        managed_config_payloads['bmcu.cfg'] = bmcu_cfg_migrated
    managed_config_payloads['bmcu_macros.cfg'] = snapshot_bytes(
        snapshot, 'config/bmcu_macros.cfg')
    try:
        installation = dict(metadata)
        boot_hook_path, boot_hook_type = planned_boot_hook(plan['platform_id'], service)
        installation.update(
            product=PRODUCT, version=PRODUCT_VERSION, schema=1,
            klipper_dir=plan['klipper_dir'], config_dir=plan['config_dir'],
            printer_cfg=printer_cfg, user=target_user, group=target_group,
            python=os.path.realpath(target_python),
            service=normalized_service(service),
            platform=plan['platform_id'], moonraker_url=moonraker,
            include_begin=BEGIN, include_end=END, includes=list(INCLUDES),
            module_strategy='persistent-runtime-links',
            boot_hook=boot_hook_path, boot_hook_type=boot_hook_type,
            u1_service_script=U1_KLIPPER_SERVICE if plan['platform_id'] == 'snapmaker_u1' else '',
            u1_service_hook_dir=U1_HOOK_DIR if plan['platform_id'] == 'snapmaker_u1' else '',
            u1_service_patch_begin=U1_SERVICE_BEGIN if plan['platform_id'] == 'snapmaker_u1' else '',
            persistence_marker=U1_PERSISTENCE_MARKER if plan['platform_id'] == 'snapmaker_u1' else '',
            panel_token_sha256=hashlib.sha256(panel_token.encode('ascii')).hexdigest(),
        )
        populate_runtime(snapshot, runtime_stage, installation)
        seal_staging(stage)

        try:
            host_state, host_message = printer_state(moonraker)
        except Exception as exc:
            if not assume_idle:
                raise InstallError(
                    'printer activity cannot be verified for update; '
                    'rerun with --assume-idle only after stopping motion and '
                    'heating: %s' % exc)
            print('Printer status unavailable; using explicit --assume-idle.')
            host_state, host_message = 'unavailable', str(exc)
        if host_state == 'ready':
            is_idle, job_state = printer_idle(moonraker, assume_idle)
            if not is_idle:
                raise InstallError('printer is not idle: %s' % job_state)
        elif host_state in ('shutdown', 'error', 'startup', 'unavailable'):
            print('Klipper is %s; continuing with the managed update.' % host_state)
        else:
            raise InstallError(
                'unexpected Klipper state during update: %s - %s' %
                (host_state, host_message))

        current_cfg, _current_info = read_regular(printer_cfg)
        if current_cfg != original_cfg:
            raise InstallError('printer.cfg changed during update; no changes made')
        validate_repair_identity(
            load_managed_metadata(bmcu_dir), plan, service,
            target_user, target_group, target_python)
        assert_directory_identity(stage, stage_identity)
        assert_directory_identity(runtime_final, original_runtime_identity)

        stop_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = True
        transaction_started = True
        stop_managed_transport_processes(bmcu_dir)
        stop_managed_planner_process(bmcu_dir)
        stop_managed_panel_process(bmcu_dir)
        release_managed_serial_holders(bmcu_dir)
        if os.path.lexists(old_runtime):
            raise InstallError('temporary runtime backup path is occupied: %s' % old_runtime)
        assert_directory_identity(stage, stage_identity)
        assert_directory_identity(runtime_final, original_runtime_identity)
        os.rename(runtime_final, old_runtime)
        assert_directory_identity(old_runtime, original_runtime_identity)
        os.rename(runtime_stage, runtime_final)
        replacement_runtime_identity = directory_identity(runtime_final)
        chown_tree(runtime_final, uid, gid)
        assert_directory_identity(runtime_final, replacement_runtime_identity)
        runtime_swapped = True

        for name in ('bmcu.cfg', 'bmcu_macros.cfg', 'bmcu_panel.cfg',
                     'UNINSTALL.txt'):
            if name not in managed_config_payloads:
                continue
            target = os.path.join(bmcu_dir, name)
            managed_config_snapshots.append(replace_managed_config(
                target, managed_config_payloads[name], uid, gid))
        legacy_panel_macros_snapshot = remove_legacy_panel_macros(
            os.path.join(bmcu_dir, 'bmcu_panel_macros.cfg'))
        legacy_uninstaller_snapshot = remove_legacy_installed_uninstaller(
            bmcu_dir, target_python)

        bootstrap = os.path.join(runtime_final, 'scripts', 'bmcu_host_bootstrap.py')
        metadata_path = os.path.join(runtime_final, 'INSTALLATION.json')
        run_as_user(
            [target_python, '-I', bootstrap, '--repair', '--links-only', '--metadata', metadata_path],
            target_user, uid, gid, check=True)
        previous_hook = install_boot_repair(
            plan['platform_id'], service, target_user, uid, gid,
            target_python, metadata_path)
        verify_boot_repair(plan['platform_id'], service, metadata_path)
        if prepared_cfg != original_cfg:
            atomic_write(
                printer_cfg, prepared_cfg, cfg_info, cfg_xattrs,
                expected=original_cfg)
            cfg_changed = True

        start_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = False
        ready, last = wait_ready(moonraker, 75)
        if not ready:
            raise InstallError('Klipper rejected the updated BMCU installation: %s - %s' % last)
        pids = wait_single_klippy(klipper_dir, printer_cfg)
        if len(pids) != 1:
            raise InstallError('expected one Klipper process after update, found %d' % len(pids))
        transport_count = verify_managed_transport_processes(bmcu_dir)
        if transport_count:
            print('Verified %d isolated BMCU transport sidecar(s).' %
                  transport_count)
        planner_count = verify_managed_planner_process(bmcu_dir)
        if planner_count:
            print('Verified isolated Snapmaker U1 source planner.')

        expected_devices = len(configured_serial_ports(bmcu_dir))
        if expected_devices:
            print('Preserved %d configured BMCU serial device(s). Hardware and firmware status is handled in the panel.' % expected_devices)

        panel_enabled = bool(installation.get('panel_enabled', True))
        panel_port = int(installation.get('panel_port', 8291))
        if panel_enabled:
            panel_ready, panel_error = wait_panel(panel_port, panel_token, timeout=30)
            if not panel_ready:
                raise InstallError(
                    'external BMCU panel process did not start on port %d: %s' %
                    (panel_port, panel_error))
        assert_directory_identity(old_runtime, original_runtime_identity)
        shutil.rmtree(old_runtime)
        assert_directory_identity(stage, stage_identity)
        shutil.rmtree(stage)
        print('\nBMCU-Klipper %s host installation is ready and persistent.' % PRODUCT_VERSION)
        if panel_enabled:
            print('Panel URL: %s' % panel_url(panel_port))
            ip_url = panel_ip_url(panel_port)
            if ip_url and ip_url != panel_url(panel_port):
                print('Panel IP URL: %s' % ip_url)
        if plan['platform_id'] == 'snapmaker_u1':
            print('Snapmaker persistent bootstrap embedded in: %s' % U1_KLIPPER_SERVICE)
            print('USB-TTL access is repaired after hardware power-up, immediately before Klipper launches as lava.')
            print('Global persistence marker preserved: %s' % U1_PERSISTENCE_MARKER)
        return 0
    except Exception as original_error:
        if not transaction_started:
            shutil.rmtree(stage, ignore_errors=True)
            raise InstallError(str(original_error))
        rollback_errors = []
        try:
            if not service_stopped:
                stop_service_strict(service, klipper_dir, printer_cfg)
                service_stopped = True
        except Exception as exc:
            rollback_errors.append('Klipper stop during rollback: %s' % exc)
        try:
            rollback_boot_repair(previous_hook)
        except Exception as exc:
            rollback_errors.append('Snapmaker boot hook rollback: %s' % exc)
        for snapshot in reversed(managed_config_snapshots):
            try:
                rollback_managed_config(snapshot)
            except Exception as exc:
                rollback_errors.append(
                    'managed config rollback for %s: %s' %
                    (snapshot.get('path', 'unknown'), exc))
        try:
            restore_legacy_panel_macros(legacy_panel_macros_snapshot)
        except Exception as exc:
            rollback_errors.append(
                'legacy panel macros rollback: %s' % exc)
        try:
            restore_legacy_installed_uninstaller(
                legacy_uninstaller_snapshot)
        except Exception as exc:
            rollback_errors.append(
                'legacy installed uninstaller rollback: %s' % exc)
        try:
            if runtime_swapped:
                stop_managed_transport_processes(bmcu_dir)
                stop_managed_planner_process(bmcu_dir)
                if os.path.lexists(failed_runtime):
                    raise InstallError(
                        'failed-runtime preservation path is occupied: %s' %
                        failed_runtime)
                if os.path.isdir(runtime_final) and not os.path.islink(runtime_final):
                    assert_directory_identity(
                        runtime_final, replacement_runtime_identity)
                    os.rename(runtime_final, failed_runtime)
                elif os.path.lexists(runtime_final):
                    raise InstallError(
                        'replacement runtime changed during rollback: %s' %
                        runtime_final)
                if not os.path.isdir(old_runtime) or os.path.islink(old_runtime):
                    raise InstallError(
                        'previous runtime backup is unavailable: %s' % old_runtime)
                assert_directory_identity(old_runtime, original_runtime_identity)
                os.rename(old_runtime, runtime_final)
                assert_directory_identity(runtime_final, original_runtime_identity)
                old_bootstrap = os.path.join(runtime_final, 'scripts', 'bmcu_host_bootstrap.py')
                if os.path.isfile(old_bootstrap):
                    run_as_user(
                        [target_python, '-I', old_bootstrap, '--repair', '--links-only', '--metadata',
                         os.path.join(runtime_final, 'INSTALLATION.json')],
                        target_user, uid, gid, check=True)
                else:
                    raise InstallError(
                        'previous runtime has no managed bootstrap: %s' %
                        old_bootstrap)
        except Exception as exc:
            rollback_errors.append('runtime rollback: %s' % exc)
        try:
            if cfg_changed:
                current_cfg, current_info = read_regular(printer_cfg)
                atomic_write(
                    printer_cfg, original_cfg, current_info,
                    read_xattrs(printer_cfg), expected=current_cfg)
        except Exception as exc:
            rollback_errors.append('printer.cfg rollback: %s' % exc)
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
                rollback_errors.append('Klipper restart after rollback: %s' % exc)
        if not rollback_errors:
            if os.path.isdir(failed_runtime) and not os.path.islink(failed_runtime):
                shutil.rmtree(failed_runtime)
            shutil.rmtree(stage, ignore_errors=True)
        message = str(original_error)
        if rollback_errors:
            message += (
                '\nRollback incomplete; Klipper was left stopped and recovery '
                'copies were preserved. Problems: ' + '; '.join(rollback_errors))
        raise InstallError(message)

def parser():
    p = argparse.ArgumentParser(prog='BMCU-Klipper installer')
    p.add_argument('--klipper-dir', default='')
    p.add_argument('--config-dir', default='')
    p.add_argument('--printer-cfg', default='')
    p.add_argument('--python', default='')
    p.add_argument('--user', default='')
    p.add_argument('--moonraker-url', default='')
    p.add_argument('--moonraker-conf', default='')
    p.add_argument('--service-backend', default='')
    p.add_argument('--service-name', default='')
    p.add_argument('--panel-port', type=int, default=8291)
    p.add_argument('--no-panel', action='store_true')

    p.add_argument('--no-detect', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--detect-port', action='append', default=[], help=argparse.SUPPRESS)
    p.add_argument('--probe-unused-ch340', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--assume-idle', action='store_true')
    return p

def main():
    if sys.version_info < (3, 7):
        raise InstallError('Python 3.7 or newer is required')
    args = parser().parse_args()
    if os.geteuid() != 0:
        raise InstallError('run the installer as root or through sudo')
    _trusted_root_path(sys.executable, require_executable=True)
    if not 1024 <= args.panel_port <= 65535:
        raise InstallError('panel port must be within 1024..65535')

    package = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    snapshot = load_release_snapshot(package)
    global PRODUCT_VERSION
    PRODUCT_VERSION = release_versions_from_snapshot(snapshot)['package']
    platform = load_package_module('bmcu_platform', snapshot, package)
    print('Package integrity: %d files verified' % (len(snapshot) - 1))

    discovery = ['discover', '--format', 'json', '--non-interactive']
    for option, value in (
        ('--klipper-dir', args.klipper_dir), ('--config-dir', args.config_dir),
        ('--printer-cfg', args.printer_cfg), ('--python', args.python),
        ('--user', args.user), ('--moonraker-url', args.moonraker_url),
        ('--moonraker-conf', args.moonraker_conf),
        ('--service-backend', args.service_backend), ('--service-name', args.service_name)):
        if value:
            discovery.extend([option, value])
    try:
        plan = platform.build_plan(platform.build_parser().parse_args(discovery)).as_dict()
    except RuntimeError as exc:
        raise InstallError(str(exc))

    service = plan.get('service') or {}
    if service.get('backend') in ('none', 'process-only', ''):
        raise InstallError('controllable Klipper service was not detected; no changes made')
    klipper_dir = plan['klipper_dir']
    config_dir = plan['config_dir']
    printer_cfg = plan['printer_cfg']
    target_user = plan['install_user']
    target_group = plan['install_group']
    target_python = plan['python']
    moonraker = plan['moonraker_url']
    transaction_lock = acquire_lock(printer_cfg)
    uid = pwd.getpwnam(target_user).pw_uid
    gid = grp.getgrnam(target_group).gr_gid
    validate_root_run_installation(plan, target_python, uid)

    print('\n=== BMCU-Klipper %s installation ===' % PRODUCT_VERSION)
    print('Platform:       %s' % plan['platform_id'])
    print('Klipper:        %s' % klipper_dir)
    print('Configuration:  %s' % printer_cfg)
    print('Klipper user:   %s' % target_user)
    print('Service:        %s %s' % (service.get('backend'), service.get('name')))

    bmcu_dir = os.path.join(config_dir, 'bmcu')
    state_file = os.path.join(config_dir, 'bmcu_state.json')
    extras_dir = os.path.join(klipper_dir, 'klippy', 'extras')
    targets = [os.path.join(extras_dir, name) for name in MODULES]

    if os.path.lexists(bmcu_dir):
        if os.path.islink(bmcu_dir) or not os.path.isdir(bmcu_dir):
            raise InstallError('existing BMCU path is unsafe: %s' % bmcu_dir)
        return repair_existing(
            snapshot, plan, service, target_user, target_group, target_python,
            uid, gid, bmcu_dir, state_file, printer_cfg, moonraker,
            args.assume_idle)

    original_cfg, cfg_info = read_regular(printer_cfg)
    cfg_xattrs = read_xattrs(printer_cfg)
    prepared_cfg, inserted_include = add_include(original_cfg)
    if os.path.lexists(bmcu_dir):
        raise InstallError('BMCU directory already exists; clean first install aborted: %s' % bmcu_dir)
    orphan_state = None
    if os.path.lexists(state_file):
        state_data, state_info = read_regular(state_file)
        if (state_info.st_uid not in (0, uid) or
                stat.S_IMODE(state_info.st_mode) & 0o022):
            raise InstallError('existing BMCU state has unsafe ownership or mode: %s' % state_file)
        orphan_state = (state_data, state_info, read_xattrs(state_file))
        print('Existing BMCU routing and settings will be preserved.')
    for target in targets:
        if os.path.lexists(target):
            raise InstallError('Klipper module name is already occupied; no changes made: %s' % target)
    host_state, host_message = printer_state(moonraker)
    if host_state != 'ready':
        raise InstallError('Klipper must be ready before installation: %s - %s' % (host_state, host_message))
    is_idle, job_state = printer_idle(moonraker, args.assume_idle)
    if not is_idle:
        raise InstallError('printer is not idle: %s' % job_state)
    if not args.no_panel and not panel_port_available(args.panel_port):
        raise InstallError('panel port %d is already in use; no changes made' % args.panel_port)

    data_root = os.path.dirname(os.path.realpath(config_dir))
    stage = ''
    activated = False
    activated_identity = None
    cfg_changed = False
    service_stopped = False
    transaction_started = False
    symlinks_created = []
    u1_record = {}
    try:
        stage = tempfile.mkdtemp(prefix='.bmcu-install-', dir=data_root)
        os.chmod(stage, 0o750)
        if os.stat(stage).st_dev != os.stat(os.path.realpath(config_dir)).st_dev:
            raise InstallError(
                'staging and configuration directories are on different filesystems')
        runtime = os.path.join(stage, 'runtime')
        panel_token = os.urandom(32).hex()
        boot_hook_path, boot_hook_type = planned_boot_hook(plan['platform_id'], service)
        installation = dict(
            product=PRODUCT, version=PRODUCT_VERSION, schema=1,
            klipper_dir=klipper_dir, config_dir=config_dir,
            printer_cfg=printer_cfg, user=target_user, group=target_group,
            python=os.path.realpath(target_python),
            service=normalized_service(service),
            platform=plan['platform_id'], moonraker_url=moonraker,
            include_begin=BEGIN, include_end=END, includes=list(INCLUDES),
            panel_enabled=not args.no_panel, panel_port=args.panel_port,
            module_strategy='persistent-runtime-links',
            boot_hook=boot_hook_path, boot_hook_type=boot_hook_type,
            u1_service_script=U1_KLIPPER_SERVICE if plan['platform_id'] == 'snapmaker_u1' else '',
            u1_service_hook_dir=U1_HOOK_DIR if plan['platform_id'] == 'snapmaker_u1' else '',
            u1_service_patch_begin=U1_SERVICE_BEGIN if plan['platform_id'] == 'snapmaker_u1' else '',
            persistence_marker=U1_PERSISTENCE_MARKER if plan['platform_id'] == 'snapmaker_u1' else '',
            persistence_marker_preexisting=(
                os.path.lexists(U1_PERSISTENCE_MARKER)
                if plan['platform_id'] == 'snapmaker_u1' else None),
            panel_token_sha256=hashlib.sha256(panel_token.encode('ascii')).hexdigest())
        populate_runtime(snapshot, runtime, installation)
        for name in ('bmcu.cfg', 'bmcu_macros.cfg', 'bmcu_panel.cfg'):
            write_snapshot_file(
                snapshot, 'config/' + name, os.path.join(stage, name), 0o640)

        panel_path = os.path.join(stage, 'bmcu_panel.cfg')
        panel_data, panel_info = read_regular(panel_path)
        try:
            panel_text = panel_data.decode('utf-8')
        except UnicodeDecodeError:
            raise InstallError('bmcu_panel.cfg in the release is not UTF-8')
        panel_text = re.sub(
            r'(?m)^enabled:\s*.*$',
            'enabled: %s' % ('False' if args.no_panel else 'True'), panel_text)
        panel_text = re.sub(
            r'(?m)^port:\s*.*$', 'port: %d' % args.panel_port, panel_text)
        panel_text = re.sub(
            r'(?m)^moonraker_url:\s*.*$',
            'moonraker_url: %s' % moonraker, panel_text)
        panel_text = re.sub(
            r'(?m)^access_token:\s*.*$',
            'access_token: %s' % panel_token, panel_text)
        atomic_write(
            panel_path, panel_text.encode('utf-8'), panel_info,
            expected=panel_data)

        uninstall_note = os.path.join(stage, 'UNINSTALL.txt')
        write_new_atomic(uninstall_note, UNINSTALL_NOTE, 0o640)

        stage_identity = directory_identity(stage)
        if args.detect_port or args.probe_unused_ch340 or args.no_detect:
            print('Serial detection options are no longer used during installation.')
        print('BMCU hardware detection and firmware flashing are handled in the panel after installation.')

        assert_directory_identity(stage, stage_identity)
        seal_staging(stage)

        host_state, host_message = printer_state(moonraker)
        if host_state != 'ready':
            raise InstallError(
                'Klipper state changed during installation: %s - %s' %
                (host_state, host_message))
        is_idle, job_state = printer_idle(moonraker, args.assume_idle)
        if not is_idle:
            raise InstallError(
                'printer started a job during installation: %s' % job_state)
        if not args.no_panel and not panel_port_available(args.panel_port):
            raise InstallError(
                'panel port %d became occupied during installation; no changes made' %
                args.panel_port)
        assert_directory_identity(stage, stage_identity)
        assert_preflight_unchanged(
            printer_cfg, original_cfg, bmcu_dir, state_file, targets,
            orphan_state)
        stop_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = True
        transaction_started = True
        release_managed_serial_holders(stage)
        assert_directory_identity(stage, stage_identity)
        assert_preflight_unchanged(
            printer_cfg, original_cfg, bmcu_dir, state_file, targets,
            orphan_state)

        os.rename(stage, bmcu_dir)
        activated_identity = directory_identity(bmcu_dir)
        if activated_identity != stage_identity:
            raise InstallError('activated BMCU directory identity is inconsistent')
        activated = True
        chown_tree(bmcu_dir, uid, gid)
        assert_directory_identity(bmcu_dir, activated_identity)
        runtime_root = os.path.join(bmcu_dir, 'runtime')
        metadata_path = os.path.join(runtime_root, 'INSTALLATION.json')
        u1_record = install_boot_repair(
            plan['platform_id'], service, target_user, uid, gid,
            target_python, metadata_path)
        verify_boot_repair(plan['platform_id'], service, metadata_path)
        bootstrap = os.path.join(runtime_root, 'scripts', 'bmcu_host_bootstrap.py')
        run_as_user(
            [target_python, '-I', bootstrap, '--repair', '--links-only', '--metadata', metadata_path],
            target_user, uid, gid, check=True)
        for target in targets:
            if symlink_inside(target, bmcu_dir):
                symlinks_created.append((target, symlink_record(target)))
        atomic_write(
            printer_cfg, prepared_cfg, cfg_info, cfg_xattrs,
            expected=original_cfg)
        cfg_changed = True
        start_service_strict(service, klipper_dir, printer_cfg)
        service_stopped = False
        ready, last = wait_ready(moonraker)
        if not ready:
            raise InstallError('Klipper rejected BMCU configuration: %s - %s' % last)
        pids = wait_single_klippy(klipper_dir, printer_cfg)
        if len(pids) != 1:
            raise InstallError(
                'expected one Klipper process after installation, found %d' %
                len(pids))
        transport_count = verify_managed_transport_processes(bmcu_dir)
        if transport_count:
            print('Verified %d isolated BMCU transport sidecar(s).' %
                  transport_count)
        planner_count = verify_managed_planner_process(bmcu_dir)
        if planner_count:
            print('Verified isolated Snapmaker U1 source planner.')

        if not args.no_panel:
            panel_ready, panel_error = wait_panel(args.panel_port, panel_token, timeout=30)
            if not panel_ready:
                raise InstallError(
                    'external BMCU panel process did not start on port %d: %s' %
                    (args.panel_port, panel_error))

    except Exception as original_error:

        if not transaction_started:
            if stage and os.path.isdir(stage) and not os.path.islink(stage):
                shutil.rmtree(stage, ignore_errors=True)
            raise InstallError(str(original_error))
        rollback_errors = []
        try:
            if not service_stopped:
                stop_service_strict(service, klipper_dir, printer_cfg)
                service_stopped = True
        except Exception as exc:
            rollback_errors.append('Klipper stop: %s' % exc)
        if cfg_changed:
            try:
                current_cfg, current_info = read_regular(printer_cfg)
                current_xattrs = read_xattrs(printer_cfg)
                rolled_back_cfg = remove_installed_include(
                    current_cfg, inserted_include)
                atomic_write(
                    printer_cfg, rolled_back_cfg, current_info, current_xattrs,
                    expected=current_cfg)
            except Exception as exc:
                rollback_errors.append(
                    'BMCU include removal from printer.cfg: %s' % exc)
        for target, record in reversed(symlinks_created):
            try:
                unlink_symlink_expected(target, record)
            except Exception as exc:
                rollback_errors.append(
                    'module link removal %s: %s' % (target, exc))
        try:
            rollback_boot_repair(u1_record)
        except Exception as exc:
            rollback_errors.append('boot integration rollback: %s' % exc)
        if not rollback_errors:
            try:
                if activated:
                    if os.path.isdir(bmcu_dir) and not os.path.islink(bmcu_dir):
                        assert_directory_identity(bmcu_dir, activated_identity)
                        stop_managed_transport_processes(bmcu_dir)
                        stop_managed_planner_process(bmcu_dir)
                        stop_managed_panel_process(bmcu_dir)
                        shutil.rmtree(bmcu_dir)
                    elif os.path.lexists(bmcu_dir):
                        raise InstallError(
                            'activated BMCU directory changed during rollback: %s' %
                            bmcu_dir)
                elif stage and os.path.isdir(stage) and not os.path.islink(stage):
                    shutil.rmtree(stage)
            except Exception as exc:
                rollback_errors.append('staging cleanup: %s' % exc)
        if orphan_state is not None:
            try:
                restore_required = True
                if os.path.lexists(state_file):
                    current_data, current_info = read_regular(state_file)
                    if (current_data == orphan_state[0] and
                            stat.S_IMODE(current_info.st_mode) ==
                            stat.S_IMODE(orphan_state[1].st_mode) and
                            current_info.st_uid == orphan_state[1].st_uid and
                            current_info.st_gid == orphan_state[1].st_gid):
                        restore_required = False
                    else:
                        if (current_info.st_uid not in (0, uid) or
                                stat.S_IMODE(current_info.st_mode) & 0o022):
                            raise InstallError(
                                'generated BMCU state is unsafe during rollback: %s' %
                                state_file)
                        unlink_regular_expected(
                            state_file, current_data, current_info)
                if restore_required:
                    restore_removed_regular(
                        state_file, orphan_state[0], orphan_state[1],
                        orphan_state[2])
            except Exception as exc:
                rollback_errors.append('BMCU state restoration: %s' % exc)
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
        message = str(original_error)
        if rollback_errors:
            message += (
                '\nRollback incomplete; Klipper was left stopped and the '
                'installation tree was preserved for recovery. Problems: ' +
                '; '.join(rollback_errors))
        raise InstallError(message)

    print('\nInstallation complete: BMCU-Klipper %s' % PRODUCT_VERSION)
    print('Configuration: %s' % bmcu_dir)
    if not args.no_panel:
        print('Panel URL: %s' % panel_url(args.panel_port))
        ip_url = panel_ip_url(args.panel_port)
        if ip_url and ip_url != panel_url(args.panel_port):
            print('Panel IP URL: %s' % ip_url)
        print('BMCU hardware is optional. Connect or flash it later in Settings.')
    if plan['platform_id'] == 'snapmaker_u1':
        print('USB-TTL access is repaired after hardware power-up, immediately before Klipper launches as lava.')
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        raise SystemExit(1)
