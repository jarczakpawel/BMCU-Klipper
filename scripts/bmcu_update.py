#!/usr/bin/env python3

from __future__ import annotations

import argparse
import binascii
import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
_ROOT = str(Path(__file__).resolve().parents[1])
_BMCU_DIR = str(Path(__file__).resolve().parents[2])
_EXTRAS = str(Path(_ROOT) / 'klippy' / 'extras')
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)

from bmcu_core.release import PACKAGE_VERSION, REQUIRED_FIRMWARE, REQUIRED_FIRMWARE_TEXT
from bmcu_core import transport
from bmcu_isp import FLASH_SIZE, flash_image
from bmcu_runtime import RuntimeClient
import bmcu_host_bootstrap as host_bootstrap

USER_AGENT = 'BMCU-Klipper-Updater/%s' % PACKAGE_VERSION

APP_SIZE = 60 * 1024
MAX_RAW_FIRMWARE = FLASH_SIZE
NVM_SIZE = 4 * 1024
MAX_MOONRAKER_RESPONSE = 1024 * 1024
MAX_TRANSACTION_BYTES = 64 * 1024
MAX_METADATA_BYTES = 64 * 1024
SHA256_RE = re.compile(r'^[0-9A-Fa-f]{64}$')
CRC32_RE = re.compile(r'^[0-9A-Fa-f]{8}$')
VERSION_RE = re.compile(r'^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$')
TRANSACTION_SCHEMA = 1

def _reject_json_constant(value):
    raise ValueError('non-standard JSON number: %s' % value)

REMOTE_VERSION_URL = 'https://raw.githubusercontent.com/jarczakpawel/BMCU-Klipper/main/version'
REMOTE_FIRMWARE_URL = 'https://raw.githubusercontent.com/jarczakpawel/BMCU-Klipper/main/firmware/firmware.bin'
MAX_VERSION_BYTES = 4096

class ReleaseUnavailable(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = str(reason)

class Reporter:
    def __init__(self, json_lines=False):
        self.json_lines = bool(json_lines)
        self.log_path = None
        self._last_dense_progress = None

    def set_log_path(self, path):
        self.log_path = Path(path)
        atomic_write(self.log_path, b'', 0o600)

    def emit(self, kind, **values):
        item = {'type': kind, 'time': dt.datetime.now(dt.timezone.utc).isoformat()}
        item.update(values)
        encoded = json.dumps(item, separators=(',', ':'), sort_keys=True)
        if self.log_path is not None:
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_CLOEXEC', 0)
            flags |= getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(str(self.log_path), flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    raise RuntimeError('firmware update log is unsafe')
                os.write(fd, (encoded + '\n').encode('utf-8'))
            finally:
                os.close(fd)
        if self.json_lines:
            print(encoded, flush=True)
        else:
            if kind == 'progress':
                print('[%3d%%] %-10s %s' % (item.get('percent', 0),
                                             item.get('stage', ''), item.get('message', '')), flush=True)
            elif kind == 'log':
                print('[%s] %s' % (item.get('level', 'INFO'), item.get('message', '')), flush=True)
            else:
                print(json.dumps(item, indent=2, sort_keys=True), flush=True)

    def log(self, level, message):
        self.emit('log', level=level, message=str(message))

    def progress(self, percent, stage, message=''):
        percent = int(percent)
        stage = str(stage)
        message = str(message)
        if stage in ('program', 'verify'):
            key = (stage, percent)
            if key == self._last_dense_progress:
                return
            self._last_dense_progress = key
        else:
            self._last_dense_progress = None
        self.emit('progress', percent=percent, stage=stage, message=message)

class UpdateLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except OSError as exc:
            raise RuntimeError('unsafe or unavailable update lock: %s' % exc) from exc
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            os.close(fd)
            raise RuntimeError('update lock must be a private regular file')
        self.handle = os.fdopen(fd, 'a+', encoding='utf-8')
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise RuntimeError('another BMCU update is already running')
        os.fchmod(self.handle.fileno(), 0o600)
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write('%d\n' % os.getpid())
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.%s.' % path.name, dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

def atomic_json(path, value, mode=0o600):
    payload = json.dumps(
        value, indent=2, sort_keys=True, allow_nan=False).encode('utf-8') + b'\n'
    atomic_write(path, payload, mode)

def digest(data):
    data = bytes(data)
    return {
        'size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'crc32': '%08X' % (binascii.crc32(data) & 0xFFFFFFFF),
    }

def _normalize_blob_descriptor(expected, label='artifact', maximum=None):
    if not isinstance(expected, dict):
        raise RuntimeError('%s descriptor must be an object' % label)
    size = expected.get('size')
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise RuntimeError('%s size must be a positive integer' % label)
    if maximum is not None and size > maximum:
        raise RuntimeError('%s size is outside 1..%d bytes' % (label, maximum))
    sha256 = expected.get('sha256')
    crc32 = expected.get('crc32')
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise RuntimeError('%s SHA-256 must be 64 hexadecimal characters' % label)
    if not isinstance(crc32, str) or not CRC32_RE.fullmatch(crc32):
        raise RuntimeError('%s CRC32 must be 8 hexadecimal characters' % label)
    return {
        'size': size,
        'sha256': sha256.lower(),
        'crc32': crc32.upper(),
    }

def verify_blob(data, expected, label='file'):
    expected = _normalize_blob_descriptor(expected, label)
    actual = digest(data)
    for key in ('size', 'sha256', 'crc32'):
        left = str(actual[key]).lower()
        right = str(expected[key]).lower()
        if left != right:
            raise RuntimeError('%s %s mismatch expected=%s got=%s' %
                               (label, key, expected[key], actual[key]))
    return actual

def _validate_final_scheme(initial_url, final_url, label):
    initial_scheme = urllib.parse.urlsplit(initial_url).scheme.lower()
    final_scheme = urllib.parse.urlsplit(final_url).scheme.lower()
    if initial_scheme == 'https' and final_scheme != 'https':
        raise RuntimeError('%s redirect downgraded to an unsafe scheme' % label)
    if initial_scheme == 'file' and final_scheme not in ('file', 'https'):
        raise RuntimeError('%s redirect uses an unsafe scheme' % label)
    if initial_scheme not in ('https', 'file'):
        raise RuntimeError('only HTTPS or file %s URLs are allowed' % label)

def _http_bytes(url, maximum, timeout, label):
    request = urllib.request.Request(
        url, headers={'User-Agent': USER_AGENT})
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseUnavailable('not_found', 'No online BMCU firmware was found') from exc
        raise
    except urllib.error.URLError as exc:
        raise ReleaseUnavailable(
            'network', 'The online BMCU firmware service could not be reached') from exc
    with response:
        _validate_final_scheme(url, response.geturl(), label)
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise RuntimeError('%s is too large' % label)
    return data

def remote_firmware_version():
    data = _http_bytes(REMOTE_VERSION_URL, MAX_VERSION_BYTES, 10, 'version file')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise RuntimeError('version file is not valid UTF-8') from exc
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
        if key.strip().lower() == 'firmware':
            value = value.strip()
            if not VERSION_RE.fullmatch(value):
                raise RuntimeError('invalid firmware version in version file')
            values = [int(part) for part in value.split('.')]
            if any(part > 255 for part in values):
                raise RuntimeError('invalid firmware version in version file')
            return value
    raise RuntimeError('version file has no firmware version')

def download_online_firmware(cache_dir, reporter):
    version = remote_firmware_version()
    remote_tuple = tuple(int(part) for part in version.split('.'))
    if remote_tuple < REQUIRED_FIRMWARE:
        raise ReleaseUnavailable(
            'outdated',
            'Published BMCU firmware %s is older than this package target %s; '
            'publish the matching release or flash the bundled/local firmware instead' %
            (version, REQUIRED_FIRMWARE_TEXT))
    reporter.log('INFO', 'Downloading fresh published BMCU firmware %s' % version)
    firmware = _http_bytes(REMOTE_FIRMWARE_URL, APP_SIZE, 60, 'firmware')
    if not firmware:
        raise RuntimeError('online firmware is empty')
    path = Path(cache_dir) / ('firmware-%s.bin' % version)
    atomic_write(path, firmware, 0o600)
    return version, path, firmware

def build_full_image(firmware, nvm):
    firmware, nvm = bytes(firmware), bytes(nvm)
    if not firmware or len(firmware) > APP_SIZE:
        raise ValueError('firmware must be 1..61440 bytes')
    if len(nvm) != NVM_SIZE:
        raise ValueError('NVM backup must be exactly 4096 bytes')
    result = firmware + b'\xFF' * (APP_SIZE - len(firmware)) + nvm
    if len(result) != FLASH_SIZE:
        raise AssertionError('full image size')
    return result

def safe_backup_dir(base, uid, timestamp):
    uid = ''.join(ch for ch in uid.upper() if ch in '0123456789ABCDEF')
    if len(uid) != 24:
        raise RuntimeError('invalid runtime UID')
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.is_dir():
        raise RuntimeError('backup root must be a real directory')
    uid_dir = base / uid
    if uid_dir.exists() and (uid_dir.is_symlink() or not uid_dir.is_dir()):
        raise RuntimeError('unsafe UID backup directory')
    uid_dir.mkdir(mode=0o700, exist_ok=True)
    name = timestamp.replace(':', '').replace('-', '')
    for attempt in range(1000):
        candidate = name if attempt == 0 else '%s-%03d' % (name, attempt)
        path = uid_dir / candidate
        try:
            path.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            continue
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError('backup path must be a real directory')
        return path
    raise RuntimeError('could not allocate a unique backup directory')

def read_regular_file(path, maximum=None):
    path = Path(path)
    if maximum is not None:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            raise ValueError('maximum must be a non-negative integer')
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise RuntimeError('unsafe or missing file: %s' % path) from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError('unsafe or missing file: %s' % path)
        if maximum is not None and file_stat.st_size > maximum:
            raise RuntimeError('file too large: %s' % path)
        chunks = []
        remaining = None if maximum is None else maximum + 1
        while remaining is None or remaining > 0:
            amount = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            chunk = os.read(fd, amount)
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        data = b''.join(chunks)
    finally:
        os.close(fd)
    if maximum is not None and len(data) > maximum:
        raise RuntimeError('file too large: %s' % path)
    return data

def read_json_file(path, maximum):
    try:
        value = json.loads(
            read_regular_file(path, maximum).decode('utf-8'),
            parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('invalid JSON file: %s' % path) from exc
    if not isinstance(value, dict):
        raise RuntimeError('JSON object required: %s' % path)
    return value

def moonraker_gcode(moonraker, script, timeout=20):
    url = moonraker.rstrip('/') + '/printer/gcode/script'
    data = json.dumps({'script': script}, separators=(',', ':')).encode('utf-8')
    request = urllib.request.Request(
        url, data=data, headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': USER_AGENT,
        }, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_MOONRAKER_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_MOONRAKER_RESPONSE + 1)
        try:
            details = json.loads(raw.decode('utf-8'))
            message = ((details.get('error') or {}).get('message') or
                       raw.decode('utf-8', 'replace'))
        except Exception:
            message = raw.decode('utf-8', 'replace')
        raise RuntimeError('Moonraker rejected updater access: %s' % message)
    if len(raw) > MAX_MOONRAKER_RESPONSE:
        raise RuntimeError('Moonraker response is too large')
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('invalid Moonraker response') from exc
    if not isinstance(value, dict):
        raise RuntimeError('invalid Moonraker response')
    return value

def managed_access_device(moonraker, device, action):
    action = str(action).upper()
    if action not in ('EXPORT', 'PREPARE', 'CANCEL', 'RESUME', 'DETACH', 'ATTACH', 'QUIESCE', 'UNQUIESCE', 'STATUS'):
        raise ValueError('invalid managed-access action')
    device = str(device or '')
    if not re.match(r'^[A-Za-z0-9_.-]{1,64}$', device):
        raise RuntimeError('invalid BMCU device name')
    script = 'BMCU_UPDATE_ACCESS DEVICE=%s ACTION=%s' % (device, action)
    return moonraker_gcode(moonraker, script)

def configured_device_for_port(args, port_identity):

    try:
        status = moonraker_bmcu_status(args.moonraker)
    except Exception:
        return ''
    matches = []
    for item in status.get('devices', []) if isinstance(status, dict) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '')
        port = str(item.get('transport_port') or item.get('port') or '')
        if not name or not port:
            continue
        try:
            candidate = freeze_serial_port(port)
        except Exception:
            continue
        if candidate['rdev'] == port_identity['rdev']:
            matches.append(name)
    matches = sorted(set(matches))
    if len(matches) > 1:
        raise RuntimeError('more than one configured BMCU claims the selected serial device')
    return matches[0] if matches else ''

def managed_device_names(moonraker):
    status = moonraker_bmcu_status(moonraker)
    devices = status.get('devices')
    if not isinstance(devices, list):
        raise RuntimeError('BMCU status has no device list')
    return sorted(set(
        str(item.get('name') or '') for item in devices
        if isinstance(item, dict) and str(item.get('name') or '')))

def service_detach_devices(args, devices):
    detached = []
    try:
        for name in devices:
            managed_access_device(args.moonraker, name, 'DETACH')
            detached.append(name)
    except Exception:
        for name in reversed(detached):
            try:
                managed_access_device(args.moonraker, name, 'ATTACH')
            except Exception:
                pass
        raise
    return detached

def service_attach_devices(args, devices):
    errors = []
    for name in reversed(list(devices or [])):
        try:
            managed_access_device(args.moonraker, name, 'ATTACH')
        except Exception as exc:
            errors.append('%s: %s' % (name, exc))
    if errors:
        raise RuntimeError('could not reconnect BMCU runtime after firmware service: %s' % '; '.join(errors))

def _host_transport_context():
    metadata_path = os.path.join(_ROOT, 'INSTALLATION.json')
    metadata = host_bootstrap.read_metadata(metadata_path)
    return metadata_path, metadata, _BMCU_DIR

def _sidecar_control_request(path, command, timeout=3.0):
    command = bytes(command)
    if len(command) != 1:
        raise RuntimeError('invalid BMCU sidecar control command')
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.bind('')
        connection.setblocking(False)
        connection.sendto(command, path)
        deadline = time.monotonic() + float(timeout)
        while True:
            try:
                data = connection.recv(4096)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('BMCU sidecar did not answer status request')
                time.sleep(0.020)
                continue
            try:
                value = json.loads(data.decode('utf-8'), parse_constant=_reject_json_constant)
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError('invalid BMCU sidecar status response') from exc
            if (not isinstance(value, dict) or
                    value.get('control') != command.decode('ascii')):
                raise RuntimeError('invalid BMCU sidecar status acknowledgement')
            return value

def stop_host_transports_except(keep=()):
    keep = set(str(name) for name in keep if name)
    _metadata_path, _metadata, bmcu_dir = _host_transport_context()
    lock = host_bootstrap.acquire_lock(bmcu_dir)
    try:
        runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
        record_path = os.path.join(bmcu_dir, 'transport-processes.json')
        records = host_bootstrap._read_transport_records(record_path)
        remaining = {}
        stopped = 0
        for name, record in records.items():
            if name in keep:
                remaining[name] = record
                continue
            if host_bootstrap._recorded_transport_alive(record, runtime):
                host_bootstrap._stop_transport_record(record, runtime)
                stopped += 1
        if records != remaining:
            host_bootstrap._atomic_transport_records(record_path, remaining)
        return stopped
    finally:
        os.close(lock)

def start_host_transports(preferred_device=None, preferred_online_timeout=0.0):
    metadata_path, _metadata, _bmcu_dir = _host_transport_context()
    return host_bootstrap.sync_transports(
        metadata_path, preferred_name=preferred_device,
        preferred_online_timeout=preferred_online_timeout)

def live_host_transport_status(device):
    device = str(device or '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', device):
        raise RuntimeError('invalid BMCU device name')
    _metadata_path, _metadata, bmcu_dir = _host_transport_context()
    record_path = os.path.join(bmcu_dir, 'transport-processes.json')
    records = host_bootstrap._read_transport_records(record_path)
    record = records.get(device)
    if not isinstance(record, dict):
        raise RuntimeError('no live host transport record for %s' % device)
    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    if not host_bootstrap._recorded_transport_alive(record, runtime):
        raise RuntimeError('host transport process is not alive for %s' % device)
    socket_path = str(record.get('socket') or '')
    if not socket_path:
        raise RuntimeError('host transport control socket is unavailable for %s' % device)
    status = _sidecar_control_request(
        socket_path + '.ctl', transport.CTRL_STATUS, timeout=3.0)
    if str(status.get('name') or '') != device:
        raise RuntimeError('host transport status belongs to another BMCU')
    if int(status.get('pid', 0) or 0) != int(record.get('pid', 0) or 0):
        raise RuntimeError('host transport process identity changed for %s' % device)
    port = str(status.get('port') or '')
    if not port.startswith('/dev/'):
        raise RuntimeError('host transport did not publish a live serial port for %s' % device)
    return status

def restore_service_runtime(args, devices):
    start_error = None
    try:
        start_host_transports()
    except Exception as exc:
        start_error = exc
    attach_error = None
    try:
        service_attach_devices(args, devices)
    except Exception as exc:
        attach_error = exc
    if start_error or attach_error:
        values = []
        if start_error:
            values.append('transport restart: %s' % start_error)
        if attach_error:
            values.append('Klipper attach: %s' % attach_error)
        raise RuntimeError('; '.join(values))

def raw_flash_guard_acquire(args, exclude=()):
    excluded = set(str(value) for value in exclude if value)
    status = moonraker_bmcu_status(args.moonraker)
    devices = status.get('devices')
    if not isinstance(devices, list):
        raise RuntimeError('BMCU status has no device list')
    names = sorted(set(
        str(item.get('name') or '') for item in devices
        if isinstance(item, dict) and str(item.get('name') or '') and
        str(item.get('name') or '') not in excluded))
    acquired = []
    try:
        for name in names:
            managed_access_device(args.moonraker, name, 'QUIESCE')
            acquired.append(name)
    except Exception:
        for name in reversed(acquired):
            try:
                managed_access_device(args.moonraker, name, 'UNQUIESCE')
            except Exception:
                pass
        raise
    return acquired

def raw_flash_guard_release(args, devices):
    errors = []
    for name in reversed(list(devices or [])):
        try:
            managed_access_device(args.moonraker, name, 'UNQUIESCE')
        except Exception as exc:
            errors.append('%s: %s' % (name, exc))
    if errors:
        raise RuntimeError('could not release serial flash guard: %s' % '; '.join(errors))

def printer_is_idle(moonraker, timeout=5):
    url = moonraker.rstrip('/') + '/printer/objects/query?print_stats'
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_MOONRAKER_RESPONSE + 1)
    if len(raw) > MAX_MOONRAKER_RESPONSE:
        raise RuntimeError('Moonraker response is too large')
    try:
        result = json.loads(
            raw.decode('utf-8'), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('invalid Moonraker response') from exc
    if not isinstance(result, dict):
        raise RuntimeError('invalid Moonraker response')
    state = (((result.get('result') or {}).get('status') or {}).get('print_stats') or {}).get('state', '')
    normalized = str(state).strip().lower()
    safe_states = {'standby', 'complete', 'cancelled', 'error'}
    return normalized in safe_states, state

def prepare_private_directory(path, mode=0o700):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(str(path)))
    path.mkdir(parents=True, exist_ok=True)
    info = os.lstat(str(path))
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError('private state path must be a real directory: %s' % path)
    if info.st_uid != os.geteuid():
        raise RuntimeError('private state directory has the wrong owner: %s' % path)
    os.chmod(str(path), mode)
    return path

def freeze_serial_port(value):
    raw = str(value or '')
    if len(raw) > 240 or not raw.startswith('/dev/') or '..' in Path(raw).parts:
        raise RuntimeError('serial port must be an absolute /dev path')
    try:
        resolved = Path(raw).resolve(strict=True)
        resolved.relative_to(Path('/dev').resolve(strict=True))
        info = os.stat(str(resolved))
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError('serial port is unavailable: %s' % raw) from exc
    if not stat.S_ISCHR(info.st_mode):
        raise RuntimeError('serial port is not a character device: %s' % raw)
    return {
        'requested': raw, 'resolved': str(resolved),
        'rdev': int(info.st_rdev), 'device': int(info.st_dev), 'inode': int(info.st_ino),
    }

def assert_frozen_serial_port(identity):
    current = freeze_serial_port(identity['resolved'])
    if current['rdev'] != identity['rdev']:
        raise RuntimeError('serial device changed during firmware update')
    return current

def serial_port_holder_pids(identity):
    """Return Linux PIDs that currently hold the same character device.

    This is a second ownership check after the managed sidecar processes have
    been stopped. Failure to inspect an unrelated /proc entry is ignored; a
    positive match is never ignored.
    """
    proc = Path('/proc')
    if not proc.is_dir():
        return []
    target_rdev = int(identity['rdev'])
    holders = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        fd_dir = entry / 'fd'
        try:
            fds = list(fd_dir.iterdir())
        except (OSError, PermissionError):
            continue
        for fd_path in fds:
            try:
                info = os.stat(str(fd_path))
            except OSError:
                continue
            if stat.S_ISCHR(info.st_mode) and int(info.st_rdev) == target_rdev:
                holders.add(pid)
                break
    return sorted(holders)

def wait_serial_port_free(identity, timeout=5.0):
    deadline = time.monotonic() + float(timeout)
    flags = os.O_RDWR | getattr(os, 'O_NOCTTY', 0) | getattr(os, 'O_NONBLOCK', 0)
    flags |= getattr(os, 'O_CLOEXEC', 0)
    last = None
    while True:
        assert_frozen_serial_port(identity)
        holders = serial_port_holder_pids(identity)
        if holders:
            last = RuntimeError('serial device is still open by PID(s): %s' %
                                ','.join(str(pid) for pid in holders))
        else:
            try:
                fd = os.open(identity['resolved'], flags)
            except OSError as exc:
                last = exc
                if exc.errno not in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise RuntimeError('serial port could not be opened after release: %s' % exc)
            else:
                os.close(fd)
                return
        if time.monotonic() >= deadline:
            raise RuntimeError('serial port remained busy after BMCU transport release: %s' % last)
        time.sleep(0.05)

def moonraker_bmcu_status(moonraker, timeout=5):
    url = moonraker.rstrip('/') + '/printer/objects/query?bmcu'
    request = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': USER_AGENT,
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_MOONRAKER_RESPONSE + 1)
    if len(raw) > MAX_MOONRAKER_RESPONSE:
        raise RuntimeError('Moonraker response is too large')
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('invalid Moonraker BMCU status') from exc
    status = (((value.get('result') or {}).get('status') or {}).get('bmcu'))
    if not isinstance(status, dict):
        raise RuntimeError('Moonraker did not return BMCU status')
    return status

def managed_device_status(args):
    status = moonraker_bmcu_status(args.moonraker)
    devices = status.get('devices')
    if not isinstance(devices, list):
        raise RuntimeError('BMCU status has no device list')
    matches = [item for item in devices if isinstance(item, dict) and
               str(item.get('name', '')) == str(args.device)]
    if len(matches) != 1:
        raise RuntimeError('exactly one managed BMCU named %s is required' % args.device)
    item = dict(matches[0])
    uid = str(item.get('uid', '') or '').upper()
    if not re.fullmatch(r'[0-9A-F]{24}', uid):
        raise RuntimeError('%s has an invalid runtime UID' % args.device)
    item['uid'] = uid
    return item

def managed_runtime_port(args, status=None):
    item = managed_device_status(args) if status is None else status
    if not item.get('connected') or not item.get('ready') or item.get('update_suspended'):
        raise RuntimeError('%s is not ready for firmware access' % args.device)
    socket_path = str(item.get('transport_socket') or '')
    if not socket_path.startswith('/') or not socket_path.endswith('.sock'):
        raise RuntimeError('BMCU runtime has no transport identity; restart Klipper after updating')
    live = transport.control_request(socket_path + '.ctl', transport.CTRL_STATUS)
    if (live.get('name') != args.device or
            str(live.get('uid') or '').upper() != item['uid'] or
            int(live.get('session_id', 0)) != int(item.get('session_id', 0)) or
            not live.get('online') or not live.get('serial_open') or
            live.get('serial_released')):
        raise RuntimeError('BMCU serial identity changed; refresh the device before flashing')
    return freeze_serial_port(str(live.get('port') or ''))

def wait_managed_ready(args, runtime_uid, timeout=60.0):
    deadline = time.monotonic() + timeout
    last = ''
    while time.monotonic() < deadline:
        try:
            item = managed_device_status(args)
            if item['uid'] != runtime_uid:
                raise RuntimeError('reconnected BMCU UID changed')
            if (bool(item.get('connected')) and bool(item.get('ready')) and
                    bool(item.get('runtime_configured')) and
                    not bool(item.get('update_suspended'))):
                return item
            last = str(item.get('last_error') or item.get('suspend_reason') or 'not ready')
        except Exception as exc:
            last = str(exc)
        time.sleep(0.25)
    raise RuntimeError('BMCU did not return ready after firmware flash: %s' % last)

def verify_expected_local_file(data, args):
    if args.expected_size is not None and len(data) != args.expected_size:
        raise RuntimeError('uploaded firmware size changed before flashing')
    if args.expected_sha256:
        actual = hashlib.sha256(data).hexdigest()
        if actual != args.expected_sha256.lower():
            raise RuntimeError('uploaded firmware SHA-256 changed before flashing')

class Updater:

    def __init__(self, args, reporter):
        self.args = args
        self.reporter = reporter
        self.state_dir = prepare_private_directory(args.state_dir)
        self.cache_dir = prepare_private_directory(self.state_dir / 'cache')
        self.preserved_dir = prepare_private_directory(self.state_dir / 'preserved')
        self.backup_dir = prepare_private_directory(self.state_dir / 'backups')
        self.export_dir = prepare_private_directory(self.state_dir / 'exports')
        self.transaction_file = self.state_dir / 'transaction.json'
        self.installed_file = self.state_dir / 'installed.json'
        self.identity_file = self.state_dir / 'identity_map.json'

    def _isp_progress(self, percent, stage, message=''):
        if self.args.mode == 'ttl' and str(stage) == 'done':
            self.reporter.progress(
                98, 'ttl-reset',
                'Firmware verified - PRESS RESET once on the BMCU to start the application')
            return
        self.reporter.progress(percent, stage, message)

    def _mapped_isp_progress(self, percent, stage, message=''):
        mapped = 4 + int(percent) * 90 // 100
        if self.args.mode == 'ttl' and str(stage) == 'done':
            self.reporter.progress(
                94, 'ttl-reset',
                'Firmware verified - PRESS RESET once on the BMCU to start the application')
            return
        self.reporter.progress(mapped, stage, message)

    def fetch_release(self):
        if self.args.firmware:
            firmware = read_regular_file(self.args.firmware, FLASH_SIZE)
            if not firmware:
                raise RuntimeError('local firmware must contain 1..65536 bytes')
            verify_expected_local_file(firmware, self.args)
            label = Path(self.args.firmware).name
            return label, Path(self.args.firmware), firmware, digest(firmware), None
        label, path, firmware = download_online_firmware(
            self.cache_dir, self.reporter)
        return label, path, firmware, digest(firmware), None

    def _record_transaction(self, value):
        atomic_json(self.transaction_file, value)

    def _normalize_transaction(self, value):
        if not isinstance(value, dict):
            raise RuntimeError('firmware transaction journal is invalid')
        required = ('firmware_path', 'firmware', 'port', 'mode', 'raw_port')
        if not all(key in value for key in required):
            raise RuntimeError('unsupported firmware transaction journal')
        normalized = dict(value)
        normalized['schema'] = TRANSACTION_SCHEMA
        if value != normalized:
            self._record_transaction(normalized)
        return normalized

    def _clear_transaction(self):
        try:
            self.transaction_file.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _remove_file(path):
        if not path:
            return
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass

    def _load_identity_map(self):
        if not self.identity_file.exists():
            return {}
        value = read_json_file(self.identity_file, MAX_METADATA_BYTES)
        if value.get('schema') != 1 or not isinstance(value.get('devices'), dict):
            raise RuntimeError('invalid firmware identity map')
        result = {}
        for runtime_uid, isp_uid in value['devices'].items():
            runtime_uid = str(runtime_uid).upper()
            isp_uid = str(isp_uid).upper()
            if (re.fullmatch(r'[0-9A-F]{24}', runtime_uid) and
                    re.fullmatch(r'[0-9A-F]+', isp_uid)):
                result[runtime_uid] = isp_uid
        return result

    def _save_identity(self, runtime_uid, isp_uid):
        if not runtime_uid:
            return
        values = self._load_identity_map()
        known = values.get(runtime_uid)
        if known and known != isp_uid:
            raise RuntimeError('physical ISP UID does not match this BMCU runtime UID')
        values[runtime_uid] = isp_uid
        atomic_json(self.identity_file, {'schema': 1, 'devices': values})

    def _export_managed_nvm(self, runtime_uid):
        token = secrets.token_hex(16)
        binary = self.export_dir / (token + '.bin')
        metadata = self.export_dir / (token + '.json')
        if binary.exists() or metadata.exists():
            raise RuntimeError('NVM export token collision')
        try:
            moonraker_gcode(
                self.args.moonraker,
                'BMCU_UPDATE_ACCESS DEVICE=%s ACTION=EXPORT TOKEN=%s' %
                (self.args.device, token), timeout=75)
            data = read_regular_file(binary, NVM_SIZE)
            meta = read_json_file(metadata, MAX_METADATA_BYTES)
            if meta.get('schema') != 1 or str(meta.get('uid', '')).upper() != runtime_uid:
                raise RuntimeError('NVM export belongs to a different BMCU')
            verify_blob(data, {
                'size': int(meta.get('size', 0)),
                'sha256': str(meta.get('sha256', '')),
                'crc32': str(meta.get('crc32', '')),
            }, 'BMCU NVM export')
            if len(data) != NVM_SIZE:
                raise RuntimeError('BMCU NVM export must contain exactly 4096 bytes')
            return data, meta
        finally:
            self._remove_file(binary)
            self._remove_file(metadata)

    @staticmethod
    def _is_managed_nvm_read_timeout(exc):
        text = str(exc or '')
        return ('NVM export failed:' in text and
                'request timeout type=0x61' in text)

    def _export_managed_nvm_direct(self, runtime_uid):
        status = managed_device_status(self.args)
        if status['uid'] != runtime_uid:
            raise RuntimeError(
                'managed BMCU UID changed before direct NVM export')
        runtime_port = managed_runtime_port(self.args, status)
        selected_port = freeze_serial_port(self.args.port)
        if runtime_port['rdev'] != selected_port['rdev']:
            raise RuntimeError(
                'managed USB port changed before direct NVM export')

        self.reporter.log(
            'WARN',
            'Klipper NVM export timed out after bounded retries; '
            'switching to direct runtime NVM export on the verified live port')

        stopped = stop_host_transports_except(())
        if stopped:
            self.reporter.log(
                'INFO', 'Stopped target BMCU transport for direct NVM export')
        assert_frozen_serial_port(runtime_port)
        wait_serial_port_free(runtime_port, timeout=8.0)

        client = RuntimeClient(
            runtime_port['resolved'], baud=115200, timeout=4.0)
        cancel_sent = False
        session_ready = False
        try:
            client.open()
            time.sleep(1.5)
            try:
                client.serial.reset_input_buffer()
            except Exception:
                pass
            hello = client.handshake()
            session_ready = True
            direct_uid = str(hello.get('uid') or '').upper()
            if direct_uid != runtime_uid:
                raise RuntimeError(
                    'direct NVM export opened a different BMCU UID %s' %
                    (direct_uid or '<missing>'))
            data, crc = client.export_nvm(chunk_size=224, retries=3)
            if len(data) != NVM_SIZE:
                raise RuntimeError(
                    'direct BMCU NVM export must contain exactly 4096 bytes')
            client.cancel_update()
            cancel_sent = True
        finally:
            if client.serial is not None and session_ready and not cancel_sent:
                try:
                    client.cancel_update()
                    cancel_sent = True
                except Exception as exc:
                    self.reporter.log(
                        'ERROR',
                        'Direct NVM export could not cancel update mode: %s' %
                        exc)
            client.close()

        metadata = {
            'schema': 1, 'uid': runtime_uid, 'size': len(data),
            'crc32': '%08X' % (int(crc) & 0xffffffff),
            'sha256': hashlib.sha256(data).hexdigest(),
            'device': self.args.device,
            'port': runtime_port['requested'],
            'transport': 'direct-runtime-fallback',
        }
        verify_blob(data, {
            'size': metadata['size'],
            'sha256': metadata['sha256'],
            'crc32': metadata['crc32'],
        }, 'direct BMCU NVM export')
        self.reporter.log(
            'INFO',
            'Direct runtime NVM export verified: 4096 bytes CRC32=%s SHA256=%s' %
            (metadata['crc32'], metadata['sha256']))
        return data, metadata

    def _backup_nvm(self, runtime_uid, data, metadata):
        timestamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        directory = safe_backup_dir(self.backup_dir, runtime_uid, timestamp)
        atomic_write(directory / 'nvm.bin', data, 0o600)
        record = dict(metadata)
        record.update({'schema': 1, 'saved_at': dt.datetime.now(dt.timezone.utc).isoformat()})
        atomic_json(directory / 'metadata.json', record)
        return directory

    def _prepare_image(self, source, runtime_uid, preserved_nvm=None, preserved_metadata=None):
        source = bytes(source)
        managed = not self.args.raw_port
        current_nvm = None
        backup_path = ''

        if preserved_nvm is not None:
            current_nvm = bytes(preserved_nvm)
            if len(current_nvm) != NVM_SIZE:
                raise RuntimeError('preserved calibration NVM must contain exactly 4096 bytes')
            metadata = dict(preserved_metadata or {})
            metadata.update({
                'schema': 1, 'size': len(current_nvm),
                'crc32': '%08X' % (binascii.crc32(current_nvm) & 0xffffffff),
                'sha256': hashlib.sha256(current_nvm).hexdigest(),
                'transport': 'interrupted-flash-nvm-salvage',
            })
            if runtime_uid:
                metadata['uid'] = runtime_uid
                backup_path = str(
                    self._backup_nvm(runtime_uid, current_nvm, metadata))
            self.reporter.log(
                'INFO',
                'Using preserved calibration NVM from the interrupted flash; firmware application bytes come from the newly selected/downloaded source')
        elif managed:
            try:
                current_nvm, metadata = self._export_managed_nvm(runtime_uid)
            except Exception as exc:
                if not self._is_managed_nvm_read_timeout(exc):
                    raise
                current_nvm, metadata = self._export_managed_nvm_direct(
                    runtime_uid)
            backup_path = str(
                self._backup_nvm(runtime_uid, current_nvm, metadata))

        if len(source) <= APP_SIZE:
            if self.args.replace_nvm:
                raise RuntimeError(
                    '--replace-nvm requires a complete 65536-byte image')
            if self.args.erase_nvm:
                target_nvm = b'\xFF' * NVM_SIZE
            elif managed or preserved_nvm is not None:
                target_nvm = current_nvm
            else:
                raise RuntimeError(
                    'unknown firmware cannot export calibration NVM; '
                    'explicit NVM erase confirmation is required')
            return (build_full_image(source, target_nvm), backup_path,
                    digest(target_nvm))

        if len(source) != FLASH_SIZE:
            raise RuntimeError('firmware must contain 1..61440 or exactly 65536 bytes')
        if self.args.erase_nvm:
            target_nvm = b'\xFF' * NVM_SIZE
        elif self.args.replace_nvm:
            target_nvm = source[APP_SIZE:]
        elif managed or preserved_nvm is not None:
            target_nvm = current_nvm
        else:
            raise RuntimeError(
                'unknown firmware cannot preserve calibration NVM; '
                'confirm replacement by the complete 65536-byte image')
        return source[:APP_SIZE] + target_nvm, backup_path, digest(target_nvm)

    def _preserved_image_path(self, image):
        return self.preserved_dir / ('%s.bin' % digest(image)['sha256'])

    def _cancel_managed_before_erase(self, device=None):
        target = str(device or self.args.device or '')
        errors = []
        for action in ('CANCEL', 'RESUME'):
            try:
                managed_access_device(self.args.moonraker, target, action)
                self.reporter.log(
                    'INFO', 'Firmware update mode cancelled and Klipper runtime restored'
                    if action == 'CANCEL' else
                    'Serial reconnect scheduled; runtime readiness is not yet confirmed')
                return True
            except Exception as exc:
                errors.append('%s: %s' % (action, exc))
        self.reporter.log(
            'ERROR', 'Could not cancel firmware update mode for %s: %s' %
            (target or '<unknown>', '; '.join(errors)))
        return False

    def _settle_previous_preparing_transaction(self):
        if not self.transaction_file.exists():
            return None
        if self.transaction_file.is_symlink() or not self.transaction_file.is_file():
            raise RuntimeError('firmware transaction journal is unsafe')
        transaction = self._normalize_transaction(
            read_json_file(self.transaction_file, MAX_TRANSACTION_BYTES))
        image_path = Path(str(transaction.get('firmware_path') or ''))
        previous_raw = bool(transaction.get('raw_port', False))
        previous_device = str(transaction.get('device') or '')
        previous_raw_managed = str(transaction.get('raw_managed_device') or '')
        erase_started = bool(
            transaction.get('erase_started') or
            str(transaction.get('phase') or '') == 'erase_started' or
            transaction.get('recovery_required') is True)

        if erase_started:
            full_image = read_regular_file(image_path, FLASH_SIZE)
            if len(full_image) != FLASH_SIZE:
                raise RuntimeError(
                    'interrupted flash calibration image must be exactly 65536 bytes')
            verify_blob(full_image, transaction.get('firmware'),
                        'interrupted flash preserved image')
            nvm = full_image[APP_SIZE:]
            if len(nvm) != NVM_SIZE:
                raise RuntimeError('interrupted flash calibration NVM is invalid')
            expected_nvm = transaction.get('nvm')
            if isinstance(expected_nvm, dict):
                verify_blob(nvm, expected_nvm, 'interrupted flash calibration NVM')
            if transaction.get('service_active') or transaction.get('service_devices'):
                try:
                    start_host_transports()
                except Exception as exc:
                    self.reporter.log(
                        'WARNING',
                        'Could not restore every transport before fresh retry: %s' % exc)
            self.reporter.log(
                'WARNING',
                'Interrupted post-erase flash detected. The next update will use only the preserved 4096-byte calibration NVM; firmware application bytes will be freshly selected/downloaded.')
            return {
                'transaction': transaction,
                'image_path': str(image_path),
                'nvm': nvm,
                'metadata': {
                    'schema': 1,
                    'uid': str(transaction.get('runtime_uid') or '').upper(),
                    'size': len(nvm),
                    'crc32': '%08X' % (binascii.crc32(nvm) & 0xffffffff),
                    'sha256': hashlib.sha256(nvm).hexdigest(),
                },
            }

        if transaction.get('service_active') or transaction.get('service_devices'):
            start_host_transports()

        if not previous_raw:
            if not previous_device or not self._cancel_managed_before_erase(previous_device):
                raise RuntimeError(
                    'previous update stopped before erase but its managed BMCU could not be restored')
        elif previous_raw_managed:
            managed_access_device(
                self.args.moonraker, previous_raw_managed, 'RESUME')

        self._clear_transaction()
        self._remove_file(image_path)
        self.reporter.log('INFO', 'Cleaned an interrupted pre-erase update transaction')
        return None

    @staticmethod
    def _validate_interrupted_target(interrupted, args, port_identity, runtime_uid):
        if not interrupted:
            return
        transaction = interrupted['transaction']
        if bool(transaction.get('raw_port', False)) != bool(args.raw_port):
            raise RuntimeError('interrupted flash belongs to a different target type')
        if str(transaction.get('mode') or '') != str(args.mode or ''):
            raise RuntimeError('interrupted flash belongs to a different bootloader mode')
        if not args.raw_port:
            if str(transaction.get('device') or '') != str(args.device or ''):
                raise RuntimeError('interrupted flash belongs to a different BMCU connection')
            saved_uid = str(transaction.get('runtime_uid') or '').upper()
            if runtime_uid and saved_uid and runtime_uid != saved_uid:
                raise RuntimeError('interrupted flash belongs to a different BMCU UID')
        saved_port = transaction.get('port_identity')
        if isinstance(saved_port, dict):
            saved_requested = str(saved_port.get('requested') or transaction.get('port') or '')
            if args.raw_port and saved_requested and str(port_identity.get('requested') or '') != saved_requested:
                raise RuntimeError('interrupted raw flash belongs to a different serial port')

    def update(self):
        requested_port = str(self.args.port or '')
        interrupted = self._settle_previous_preparing_transaction()
        idle, state = printer_is_idle(self.args.moonraker)
        if not idle:
            raise RuntimeError('printer state is %s; firmware flash is blocked' % state)
        self.reporter.progress(0, 'prepare', 'Preparing firmware update - BMCU connections may briefly disconnect while calibration is backed up')
        label, _firmware_path, source, _artifact, _manifest = self.fetch_release()
        managed = not self.args.raw_port
        runtime_uid = ''
        raw_managed_device = ''
        service_devices = []
        service_active = False
        update_exported = False

        if managed:
            managed_status = None
            try:
                managed_status = managed_device_status(self.args)
                runtime_uid = managed_status['uid']
            except Exception:
                previous = (interrupted or {}).get('transaction', {})
                if (not interrupted or
                        str(previous.get('device') or '') != str(self.args.device or '')):
                    raise
                runtime_uid = str(previous.get('runtime_uid') or '').upper()
                if not runtime_uid:
                    raise RuntimeError(
                        'interrupted managed flash has no preserved runtime UID')
                self.reporter.log(
                    'WARNING',
                    'BMCU runtime is unavailable after the interrupted flash; using the preserved target UID and final ISP UID gate for this fresh update')
            if self.args.mode == 'usb':
                if managed_status is not None:
                    port_identity = managed_runtime_port(self.args, managed_status)
                else:
                    fallback_port = requested_port or str(
                        interrupted['transaction'].get('port') or '')
                    port_identity = freeze_serial_port(fallback_port)
                if requested_port:
                    try:
                        requested_identity = freeze_serial_port(requested_port)
                    except RuntimeError:
                        requested_identity = None
                    if (requested_identity is not None and
                            requested_identity['rdev'] != port_identity['rdev']):
                        raise RuntimeError(
                            'selected serial port does not match the verified live BMCU transport')
                self.args.port = port_identity['requested']
                self.reporter.log(
                    'INFO', 'Managed USB flash port selected from verified live sidecar: %s' %
                    port_identity['requested'])
            else:
                if not self.args.confirm_ttl_target:
                    raise RuntimeError(
                        'managed TTL flashing requires explicit --confirm-ttl-target')
                ttl_port = requested_port or str(self.args.port or '')
                port_identity = freeze_serial_port(ttl_port)
                if managed_status is not None:
                    live_identity = managed_runtime_port(self.args, managed_status)
                    if port_identity['rdev'] != live_identity['rdev']:
                        raise RuntimeError(
                            'selected TTL serial port does not belong to the selected BMCU connection')
                self.args.port = port_identity['requested']
                self.reporter.log(
                    'INFO', 'Managed TTL port verified against selected BMCU live transport: %s' %
                    port_identity['requested'])
            service_devices = managed_device_names(self.args.moonraker)
            if self.args.device not in service_devices:
                service_devices.append(self.args.device)
                service_devices.sort()
        else:
            port_identity = freeze_serial_port(self.args.port)
            self.args.port = port_identity['requested']
            raw_managed_device = configured_device_for_port(
                self.args, port_identity)
            if self.args.mode == 'ttl' and not self.args.confirm_ttl_target:
                raise RuntimeError('raw TTL flashing requires explicit --confirm-ttl-target')
            try:
                service_devices = managed_device_names(self.args.moonraker)
            except Exception:
                service_devices = []

        self._validate_interrupted_target(
            interrupted, self.args, port_identity, runtime_uid)
        preserved_nvm = interrupted['nvm'] if interrupted else None
        preserved_metadata = interrupted['metadata'] if interrupted else None
        previous_image_path = interrupted['image_path'] if interrupted else ''

        preserved_path = None
        transaction = None
        try:
            if managed:
                self.reporter.progress(
                    1, 'prepare',
                    'Preparing BMCU - pausing other BMCU connections before calibration backup')
                stopped = stop_host_transports_except((self.args.device,))
                service_active = True
                if stopped:
                    self.reporter.log(
                        'INFO',
                        'Stopped %d non-target BMCU transport process(es) before NVM backup' %
                        stopped)
                else:
                    self.reporter.log(
                        'INFO', 'No non-target BMCU transport process was active before NVM backup')
            else:
                stopped = stop_host_transports_except(())
                service_active = True
                if stopped:
                    self.reporter.log(
                        'INFO',
                        'Stopped %d BMCU transport process(es) before raw firmware flash' %
                        stopped)

            if managed:
                self.reporter.progress(
                    2, 'prepare',
                    'Backing up BMCU calibration NVM - communication can pause briefly')
            nvm_started = time.monotonic()
            full_image, backup_path, nvm_digest = self._prepare_image(
                source, runtime_uid, preserved_nvm, preserved_metadata)
            if managed:
                self.reporter.log(
                    'INFO', 'Calibration NVM preservation completed in %.3f s' %
                    max(0.0, time.monotonic() - nvm_started))
            update_exported = bool(managed and preserved_nvm is None)
            image_digest = digest(full_image)
            preserved_path = self._preserved_image_path(full_image)
            atomic_write(preserved_path, full_image, 0o600)
            transaction = {
                'schema': TRANSACTION_SCHEMA, 'phase': 'preparing',
                'firmware_path': str(preserved_path),
                'firmware': image_digest,
                'source_firmware': digest(source), 'display_name': label,
                'port': self.args.port, 'port_identity': port_identity,
                'mode': self.args.mode, 'device': self.args.device,
                'raw_port': bool(self.args.raw_port),
                'runtime_uid': runtime_uid,
                'raw_managed_device': raw_managed_device,
                'raw_guard_devices': [],
                'service_devices': list(service_devices),
                'service_active': bool(service_active),
                'service_mode': 'process_only',
                'nvm': nvm_digest, 'nvm_backup': backup_path,
                'erase_started': False,
                'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            self._record_transaction(transaction)
            if previous_image_path and previous_image_path != str(preserved_path):
                self._remove_file(previous_image_path)
        except Exception:
            restore_ok = True
            if service_active:
                try:
                    start_host_transports()
                    service_active = False
                except Exception as restore_exc:
                    restore_ok = False
                    self.reporter.log(
                        'ERROR', 'Could not restart BMCU transport processes: %s' %
                        restore_exc)
            if managed and restore_ok:
                self._cancel_managed_before_erase()
            if (preserved_path is not None and
                    str(preserved_path) != str(previous_image_path or '')):
                self._remove_file(preserved_path)
            raise

        if self.args.erase_nvm:
            nvm_message = 'clean calibration NVM selected'
        elif self.args.replace_nvm:
            nvm_message = 'full-image calibration NVM selected'
        else:
            nvm_message = 'verified calibration NVM preserved'
        self.reporter.progress(3, 'prepare', '%s ready - %s' % (label, nvm_message))
        erase_started = False
        flash_verified = False
        known_identities = self._load_identity_map()
        expected_isp = known_identities.get(runtime_uid, '') if runtime_uid else ''
        if runtime_uid and not expected_isp:
            expected_isp = runtime_uid[:16]
        try:
            assert_frozen_serial_port(port_identity)

            stopped = stop_host_transports_except(())
            service_active = True
            transaction['phase'] = 'isp_port_exclusive'
            transaction['service_active'] = True
            self._record_transaction(transaction)
            self.reporter.log(
                'INFO',
                'BMCU transport processes stopped; target serial port is exclusive for direct WCH ISP')

            idle, state = printer_is_idle(self.args.moonraker)
            if not idle:
                raise RuntimeError(
                    'printer state changed to %s before erase; firmware flash is blocked' %
                    state)

            def before_erase(identity):
                nonlocal erase_started
                idle_now, state_now = printer_is_idle(self.args.moonraker)
                if not idle_now:
                    raise RuntimeError(
                        'printer state changed to %s at the final pre-erase gate; '
                        'firmware flash is blocked' % state_now)
                if expected_isp and identity.uid_hex.upper() != expected_isp:
                    raise RuntimeError('ISP UID does not match the selected BMCU')
                erase_started = True
                transaction['phase'] = 'erase_started'
                transaction['erase_started'] = True
                transaction['isp_uid'] = identity.uid_hex.upper()
                transaction['erase_started_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
                self._record_transaction(transaction)

            assert_frozen_serial_port(port_identity)
            wait_serial_port_free(port_identity)
            identity = flash_image(
                self.args.port, full_image, mode=self.args.mode,
                manual_timeout=self.args.manual_timeout,
                log_callback=self.reporter.log,
                progress_callback=self._mapped_isp_progress, before_erase=before_erase,
                expected_isp_uid=expected_isp)
            flash_verified = True
            erase_started = False
            if runtime_uid:
                self._save_identity(runtime_uid, identity.uid_hex.upper())
            transaction['phase'] = 'flashed_verified'
            transaction['isp_uid'] = identity.uid_hex.upper()
            transaction['erase_started'] = False
            transaction['verified_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            self._record_transaction(transaction)

            reconnect_started = time.monotonic()
            if managed and self.args.mode == 'ttl':
                self.reporter.log(
                    'ACTION',
                    'TTL firmware verified. PRESS RESET once now to start the BMCU application. Waiting for runtime reconnect...')
                self.reporter.progress(
                    95, 'ttl-reset',
                    'Firmware verified - PRESS RESET once to start the BMCU application; waiting for reconnect')
            else:
                self.reporter.progress(95, 'reconnect', 'Restarting BMCU transport processes')

            reconnect_error = None
            try:
                if managed:
                    start_host_transports(
                        preferred_device=self.args.device,
                        preferred_online_timeout=(180.0 if self.args.mode == 'ttl' else 35.0))
                else:
                    start_host_transports()
                service_active = False
                transaction['service_active'] = False
                transaction['phase'] = 'waiting_runtime'
                self._record_transaction(transaction)
                if managed:
                    wait_managed_ready(
                        self.args, runtime_uid,
                        180.0 if self.args.mode == 'ttl' else 75.0)
                self.reporter.log(
                    'INFO', 'BMCU runtime reconnect completed in %.3f s' %
                    max(0.0, time.monotonic() - reconnect_started))
            except Exception as runtime_exc:
                reconnect_error = runtime_exc
                self.reporter.log(
                    'WARNING',
                    'Firmware is already flashed and verified, but BMCU runtime reconnect is still pending: %s' %
                    runtime_exc)
                try:
                    start_host_transports()
                    service_active = False
                except Exception as restart_exc:
                    self.reporter.log(
                        'WARNING', 'Could not finish restarting all BMCU transports after verified flash: %s' %
                        restart_exc)

            installed = {
                'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'display_name': label, 'mode': self.args.mode, 'port': self.args.port,
                'raw_port': bool(self.args.raw_port), 'runtime_uid': runtime_uid,
                'isp_uid': identity.uid_hex.upper(), 'firmware': image_digest,
                'nvm': nvm_digest, 'runtime_confirmed': reconnect_error is None,
            }
            atomic_json(self.installed_file, installed)
            self._clear_transaction()
            self._remove_file(preserved_path)
            raw_ttl_reset_pending = bool(self.args.mode == 'ttl' and not managed)
            if reconnect_error is None and not raw_ttl_reset_pending:
                final_message = (
                    'Firmware verified and BMCU reconnected after RESET'
                    if self.args.mode == 'ttl' else
                    'Firmware flashed, verified and BMCU transports restarted')
            else:
                final_message = (
                    'Firmware flashed and verified. PRESS RESET once on the BMCU to start the application.')
            if self.args.mode == 'ttl' and (reconnect_error is not None or raw_ttl_reset_pending):
                self.reporter.progress(
                    99, 'ttl-reset',
                    'Firmware verified - PRESS RESET once on the BMCU to start the application')
            else:
                self.reporter.progress(100, 'done', final_message)
            self.reporter.emit(
                'result', ok=True, message=final_message,
                runtime_reconnect_pending=(reconnect_error is not None or raw_ttl_reset_pending),
                display_name=label, mode=self.args.mode,
                port=self.args.port, raw_port=bool(self.args.raw_port),
                runtime_uid=runtime_uid, isp_uid=identity.uid_hex.upper(),
                released_device=raw_managed_device,
                released_devices=list(service_devices),
                firmware=image_digest, nvm=nvm_digest)
        except Exception as exc:
            erase_started = erase_started or bool(getattr(exc, 'erase_started', False))
            restore_ok = True
            if service_active:
                try:
                    start_host_transports()
                    service_active = False
                    if transaction is not None:
                        transaction['service_active'] = False
                        self._record_transaction(transaction)
                except Exception as runtime_exc:
                    restore_ok = False
                    self.reporter.log(
                        'ERROR', 'Could not restart BMCU transport processes: %s' %
                        runtime_exc)
            if flash_verified:
                if transaction is not None:
                    transaction['phase'] = 'flashed_verified'
                    transaction['erase_started'] = False
                    transaction['service_active'] = bool(service_active)
                    try:
                        self._record_transaction(transaction)
                    except Exception:
                        pass
                self._clear_transaction()
                self._remove_file(preserved_path)
                self.reporter.log(
                    'WARNING',
                    'Firmware was already flashed and verified; a post-flash finalization step failed: %s' %
                    exc)
            elif not erase_started:
                cancelled = restore_ok
                if managed and update_exported and cancelled:
                    cancelled = self._cancel_managed_before_erase()
                if cancelled:
                    self._clear_transaction()
                    self._remove_file(preserved_path)
            else:
                self.reporter.log(
                    'ERROR',
                    'Erase started. Start a fresh local or online flash. Only the preserved 4096-byte calibration NVM will be reused; firmware application bytes will come from the newly selected/downloaded source.')
            raise

def validate_cli_args(args):
    value = float(args.manual_timeout)
    if not math.isfinite(value) or not 1.0 <= value <= 3600.0:
        raise ValueError('--manual-timeout must be finite and within 1..3600 seconds')
    args.manual_timeout = value
    device = str(args.device or '')
    if args.raw_port:
        args.device = 'raw_ch340'
    elif not re.match(r'^[A-Za-z0-9_.-]{1,64}$', device):
        raise ValueError('invalid BMCU device name')
    if args.erase_nvm and args.replace_nvm:
        raise ValueError('--erase-nvm and --replace-nvm are mutually exclusive')
    if args.expected_size is not None and not 1 <= args.expected_size <= FLASH_SIZE:
        raise ValueError('--expected-size must be within 1..65536')
    if args.expected_sha256 and not SHA256_RE.fullmatch(args.expected_sha256):
        raise ValueError('--expected-sha256 must be 64 hexadecimal characters')
    parsed = urllib.parse.urlsplit(str(args.moonraker or ''))
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment):
        raise ValueError('--moonraker must be an HTTP(S) base URL without credentials, query or fragment')

def build_parser():
    parser = argparse.ArgumentParser(prog='bmcu_update.py')
    parser.add_argument('command', choices=('check', 'update'))
    parser.add_argument('--port', default='/dev/ttyUSB0')
    parser.add_argument('--mode', choices=('usb', 'ttl'), default='usb')
    parser.add_argument('--variant', choices=('universal',), default='universal')
    parser.add_argument('--firmware', default='')
    parser.add_argument('--state-dir', default='~/printer_data/config/bmcu/update')
    parser.add_argument('--moonraker', default='http://127.0.0.1:7125')
    parser.add_argument('--device', default='bmcu0')
    parser.add_argument('--raw-port', action='store_true')
    parser.add_argument('--erase-nvm', action='store_true')
    parser.add_argument('--replace-nvm', action='store_true')
    parser.add_argument('--confirm-ttl-target', action='store_true')
    parser.add_argument('--expected-size', type=int, default=None)
    parser.add_argument('--expected-sha256', default='')
    parser.add_argument('--manual-timeout', type=float, default=180.0)
    parser.add_argument('--json-lines', action='store_true')
    return parser

def main(argv=None):
    args = build_parser().parse_args(argv)
    reporter = Reporter(args.json_lines)
    try:
        validate_cli_args(args)
        state_dir = prepare_private_directory(args.state_dir)
    except (ValueError, RuntimeError) as exc:
        reporter.emit('result', ok=False, error=str(exc), interrupted_flash=False)
        return 2
    try:
        reporter.set_log_path(state_dir / 'last-update.jsonl')
        with UpdateLock(state_dir / 'update.lock'):
            updater = Updater(args, reporter)
            if args.command == 'check':
                try:
                    version, path, firmware, artifact, manifest = updater.fetch_release()
                except ReleaseUnavailable as exc:
                    reporter.emit('result', ok=True, available=False,
                                  reason=exc.reason, message=str(exc))
                    return 0
                reporter.emit('result', ok=True, available=True, version=version,
                              path=str(path), firmware=digest(firmware))
            elif args.command == 'update':
                updater.update()
        return 0
    except Exception as exc:
        interrupted_flash = False
        transaction_path = state_dir / 'transaction.json'
        if transaction_path.is_file() and not transaction_path.is_symlink():
            try:
                journal = read_json_file(transaction_path, MAX_TRANSACTION_BYTES)
                interrupted_flash = bool(
                    journal.get('erase_started') or
                    journal.get('recovery_required') is True or
                    str(journal.get('phase') or '') == 'erase_started')
            except Exception:
                interrupted_flash = True
        reporter.emit('result', ok=False, error=str(exc),
                      interrupted_flash=interrupted_flash)
        return 1

if __name__ == '__main__':
    sys.exit(main())
