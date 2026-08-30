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

from bmcu_isp import FLASH_SIZE, flash_image

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

    def emit(self, kind, **values):
        item = {'type': kind, 'time': dt.datetime.now(dt.timezone.utc).isoformat()}
        item.update(values)
        if self.json_lines:
            print(json.dumps(item, separators=(',', ':'), sort_keys=True), flush=True)
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
        self.emit('progress', percent=int(percent), stage=str(stage), message=str(message))

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
        url, headers={'User-Agent': 'BMCU-Klipper-Updater/1.0.0'})
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
    reporter.log('INFO', 'Downloading BMCU firmware %s' % version)
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
            'User-Agent': 'BMCU-Klipper-Updater/1.0.0',
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

def managed_access_device(moonraker, device, action, recovery=False):
    action = str(action).upper()
    if action not in ('EXPORT', 'PREPARE', 'CANCEL', 'RESUME', 'QUIESCE', 'UNQUIESCE', 'STATUS'):
        raise ValueError('invalid managed-access action')
    device = str(device or '')
    if not re.match(r'^[A-Za-z0-9_.-]{1,64}$', device):
        raise RuntimeError('invalid BMCU device name')
    script = 'BMCU_UPDATE_ACCESS DEVICE=%s ACTION=%s' % (device, action)
    if action == 'PREPARE' and recovery:
        script += ' RECOVERY=1'
    return moonraker_gcode(moonraker, script)

def managed_access(args, action, recovery=False):
    if getattr(args, 'raw_port', False):
        raise RuntimeError('managed BMCU access is unavailable for a raw serial adapter')
    return managed_access_device(args.moonraker, args.device, action, recovery)

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
        port = str(item.get('port') or '')
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
    request = urllib.request.Request(url, headers={'User-Agent': 'BMCU-Klipper-Updater/1.0.0'})
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

def wait_serial_port_free(identity, timeout=5.0):
    deadline = time.monotonic() + float(timeout)
    flags = os.O_RDWR | getattr(os, 'O_NOCTTY', 0) | getattr(os, 'O_NONBLOCK', 0)
    flags |= getattr(os, 'O_CLOEXEC', 0)
    last = None
    while True:
        assert_frozen_serial_port(identity)
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
        'User-Agent': 'BMCU-Klipper-Updater/1.0.0',
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
        self.recovery_dir = prepare_private_directory(self.state_dir / 'recovery')
        self.backup_dir = prepare_private_directory(self.state_dir / 'backups')
        self.export_dir = prepare_private_directory(self.state_dir / 'exports')
        self.transaction_file = self.state_dir / 'transaction.json'
        self.installed_file = self.state_dir / 'installed.json'
        self.identity_file = self.state_dir / 'identity_map.json'

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
            raise RuntimeError('firmware recovery journal is invalid')
        required = ('firmware_path', 'firmware', 'port', 'mode', 'raw_port')
        if not all(key in value for key in required):
            raise RuntimeError('unsupported firmware transaction journal')
        normalized = dict(value)
        saved_port = normalized.get('port_identity')
        normalized['legacy_recovery'] = not isinstance(saved_port, dict)
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
                (self.args.device, token), timeout=30)
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

    def _backup_nvm(self, runtime_uid, data, metadata):
        timestamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        directory = safe_backup_dir(self.backup_dir, runtime_uid, timestamp)
        atomic_write(directory / 'nvm.bin', data, 0o600)
        record = dict(metadata)
        record.update({'schema': 1, 'saved_at': dt.datetime.now(dt.timezone.utc).isoformat()})
        atomic_json(directory / 'metadata.json', record)
        return directory

    def _prepare_image(self, source, runtime_uid):
        source = bytes(source)
        managed = not self.args.raw_port
        current_nvm = None
        backup_path = ''

        if managed:
            current_nvm, metadata = self._export_managed_nvm(runtime_uid)
            backup_path = str(self._backup_nvm(runtime_uid, current_nvm, metadata))

        if len(source) <= APP_SIZE:
            if self.args.replace_nvm:
                raise RuntimeError(
                    '--replace-nvm requires a complete 65536-byte image')
            if self.args.erase_nvm:
                target_nvm = b'\xFF' * NVM_SIZE
            elif managed:
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
        elif managed:
            target_nvm = current_nvm
        else:
            raise RuntimeError(
                'unknown firmware cannot preserve calibration NVM; '
                'confirm replacement by the complete 65536-byte image')
        return source[:APP_SIZE] + target_nvm, backup_path, digest(target_nvm)

    def _recovery_image_path(self, image):
        return self.recovery_dir / ('%s.bin' % digest(image)['sha256'])

    def _cancel_managed_before_erase(self):
        errors = []
        for action in ('CANCEL', 'RESUME'):
            try:
                managed_access(self.args, action)
                self.reporter.log(
                    'INFO', 'Firmware update mode cancelled and Klipper runtime restored')
                return True
            except Exception as exc:
                errors.append('%s: %s' % (action, exc))
        self.reporter.log(
            'ERROR', 'Could not cancel firmware update mode: %s' % '; '.join(errors))
        return False

    def _settle_previous_preparing_transaction(self):
        if not self.transaction_file.exists():
            return
        if self.transaction_file.is_symlink() or not self.transaction_file.is_file():
            raise RuntimeError('firmware recovery journal is unsafe')
        transaction = self._normalize_transaction(
            read_json_file(self.transaction_file, MAX_TRANSACTION_BYTES))
        if transaction.get('recovery_required') is True:
            raise RuntimeError(
                'an interrupted flash requires recovery before another update')
        image_path = str(transaction.get('firmware_path') or '')
        raw_port = bool(transaction.get('raw_port', False))
        raw_managed_device = str(transaction.get('raw_managed_device') or '')
        if not raw_port:
            self.args.raw_port = False
            self.args.port = str(transaction.get('port') or self.args.port)
            self.args.device = str(transaction.get('device') or self.args.device)
            self.args.mode = str(transaction.get('mode') or self.args.mode)
            if not self._cancel_managed_before_erase():
                raise RuntimeError(
                    'previous update stopped before erase but the managed port could not be restored')
        elif raw_managed_device:
            managed_access_device(
                self.args.moonraker, raw_managed_device, 'RESUME')
        self._clear_transaction()
        self._remove_file(image_path)
        self.reporter.log('INFO', 'Cleaned an interrupted pre-erase update transaction')

    def update(self):
        self._settle_previous_preparing_transaction()
        idle, state = printer_is_idle(self.args.moonraker)
        if not idle:
            raise RuntimeError('printer state is %s; firmware flash is blocked' % state)
        self.reporter.progress(0, 'prepare', 'Loading firmware file')
        label, _firmware_path, source, _artifact, _manifest = self.fetch_release()
        port_identity = freeze_serial_port(self.args.port)
        self.args.port = port_identity['resolved']
        managed = not self.args.raw_port
        runtime_uid = ''
        managed_status = None
        raw_managed_device = ''
        raw_guard_devices = []
        if managed:
            managed_status = managed_device_status(self.args)
            runtime_uid = managed_status['uid']
            managed_port = freeze_serial_port(str(managed_status.get('port') or ''))
            if self.args.mode == 'usb':
                if managed_port['rdev'] != port_identity['rdev']:
                    raise RuntimeError('USB update port does not belong to the selected BMCU')
            elif not self.args.confirm_ttl_target:
                raise RuntimeError('managed TTL flashing requires explicit --confirm-ttl-target')
        else:
            raw_managed_device = configured_device_for_port(
                self.args, port_identity)
            if self.args.mode == 'ttl' and not self.args.confirm_ttl_target:
                raise RuntimeError('raw TTL flashing requires explicit --confirm-ttl-target')

        recovery_path = None
        try:
            full_image, backup_path, nvm_digest = self._prepare_image(
                source, runtime_uid)
            image_digest = digest(full_image)
            recovery_path = self._recovery_image_path(full_image)
            atomic_write(recovery_path, full_image, 0o600)
            transaction = {
                'schema': TRANSACTION_SCHEMA, 'phase': 'preparing',
                'recovery_required': False,
                'firmware_path': str(recovery_path),
                'firmware': image_digest,
                'source_firmware': digest(source), 'display_name': label,
                'port': self.args.port, 'port_identity': port_identity,
                'mode': self.args.mode, 'device': self.args.device,
                'raw_port': bool(self.args.raw_port),
                'runtime_uid': runtime_uid,
                'raw_managed_device': raw_managed_device,
                'raw_guard_devices': [],
                'nvm': nvm_digest, 'nvm_backup': backup_path,
                'erase_started': False,
                'created_at': dt.datetime.now(
                    dt.timezone.utc).isoformat(),
            }
            self._record_transaction(transaction)
        except Exception:

            if managed:
                self._cancel_managed_before_erase()
            if recovery_path is not None:
                self._remove_file(recovery_path)
            raise
        if self.args.erase_nvm:
            nvm_message = 'clean calibration NVM selected'
        elif self.args.replace_nvm:
            nvm_message = 'full-image calibration NVM selected'
        else:
            nvm_message = 'verified calibration NVM preserved'
        self.reporter.progress(3, 'prepare', '%s ready - %s' % (label, nvm_message))
        erase_started = False
        prepared_attempted = False
        known_identities = self._load_identity_map()
        expected_isp = known_identities.get(runtime_uid, '') if runtime_uid else ''
        try:
            assert_frozen_serial_port(port_identity)
            if managed:
                prepared_attempted = True
                managed_access(self.args, 'PREPARE')
                transaction['phase'] = 'prepared'
                self._record_transaction(transaction)
                self.reporter.log('INFO', 'BMCU motors stopped and serial port released')
            else:
                if raw_managed_device:
                    prepared_attempted = True
                    managed_access_device(
                        self.args.moonraker, raw_managed_device,
                        'PREPARE', recovery=True)
                    transaction['phase'] = 'raw_port_released'
                    self._record_transaction(transaction)
                    self.reporter.log(
                        'INFO', 'Configured serial port released without using the old application firmware')
                raw_guard_devices = raw_flash_guard_acquire(
                    self.args, exclude=(raw_managed_device,))
                transaction['raw_guard_devices'] = list(raw_guard_devices)
                transaction['phase'] = 'raw_serial_guarded'
                self._record_transaction(transaction)
                if raw_guard_devices:
                    self.reporter.log(
                        'INFO', 'Paused BMCU UID fallback scanners while the raw serial target is flashed')
                self.reporter.log('WARN', 'Raw port selected; all filament must be removed manually')

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
                transaction['recovery_required'] = True
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
                progress_callback=lambda pct, stage, msg: self.reporter.progress(
                    4 + pct * 90 // 100, stage, msg), before_erase=before_erase)
            if runtime_uid:
                self._save_identity(runtime_uid, identity.uid_hex.upper())
            transaction['phase'] = 'flashed_verified'
            transaction['isp_uid'] = identity.uid_hex.upper()
            self._record_transaction(transaction)
            if managed:
                self.reporter.progress(95, 'reconnect', 'Releasing the port back to Klipper')
                managed_access(self.args, 'RESUME')
                transaction['phase'] = 'waiting_runtime'
                self._record_transaction(transaction)
                wait_managed_ready(self.args, runtime_uid, 60.0)
            elif raw_managed_device:
                self.reporter.progress(
                    95, 'register',
                    'Keeping the configured port released while the panel registers the new runtime')
                transaction['phase'] = 'raw_flash_waiting_registration'
                self._record_transaction(transaction)
            installed = {
                'schema': 1, 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'display_name': label, 'mode': self.args.mode, 'port': self.args.port,
                'raw_port': bool(self.args.raw_port), 'runtime_uid': runtime_uid,
                'isp_uid': identity.uid_hex.upper(), 'firmware': image_digest,
                'nvm': nvm_digest,
            }
            atomic_json(self.installed_file, installed)
            self._clear_transaction()
            self._remove_file(recovery_path)
            if managed:
                final_message = 'Firmware flashed, verified and reconnected'
                self.reporter.progress(100, 'done', final_message)
            else:
                final_message = 'Firmware flashed and verified - waiting for BMCU runtime registration'
                self.reporter.progress(98, 'register', final_message)
            self.reporter.emit('result', ok=True,
                               message=final_message,
                               display_name=label, mode=self.args.mode,
                               runtime_uid=runtime_uid, isp_uid=identity.uid_hex.upper(),
                               released_device=raw_managed_device,
                               released_devices=([raw_managed_device] if raw_managed_device else []) + list(raw_guard_devices),
                               firmware=image_digest, nvm=nvm_digest)
        except Exception as exc:
            erase_started = erase_started or bool(getattr(exc, 'erase_started', False))
            if raw_guard_devices:
                try:
                    raw_flash_guard_release(self.args, raw_guard_devices)
                except Exception as guard_exc:
                    self.reporter.log('ERROR', str(guard_exc))
            if not erase_started:
                cancelled = True
                if managed and prepared_attempted:
                    cancelled = self._cancel_managed_before_erase()
                elif raw_managed_device and prepared_attempted:
                    try:
                        managed_access_device(
                            self.args.moonraker, raw_managed_device, 'RESUME')
                    except Exception as resume_exc:
                        self.reporter.log(
                            'ERROR', 'Could not release the raw configured port back to Klipper: %s' % resume_exc)
                        cancelled = False
                if cancelled:
                    self._clear_transaction()
                    self._remove_file(recovery_path)
            else:
                self.reporter.log(
                    'ERROR', 'Erase started. Press Flash firmware again to retry safely; the exact full image is preserved.')
            raise

    def recover(self):
        idle, state = printer_is_idle(self.args.moonraker)
        if not idle:
            raise RuntimeError(
                'printer state is %s; firmware recovery is blocked' % state)
        if not self.transaction_file.is_file() or self.transaction_file.is_symlink():
            raise RuntimeError('no interrupted firmware flash exists')
        transaction = self._normalize_transaction(
            read_json_file(self.transaction_file, MAX_TRANSACTION_BYTES))
        if transaction.get('recovery_required') is not True:
            raise RuntimeError('no compatible interrupted firmware flash exists')
        image_path = Path(str(transaction.get('firmware_path') or ''))
        firmware = read_regular_file(image_path, FLASH_SIZE)
        verify_blob(firmware, transaction.get('firmware'), 'recovery firmware')
        legacy_recovery = bool(transaction.get('legacy_recovery', False))
        if legacy_recovery:
            if not firmware:
                raise RuntimeError('recovery image is empty')
        elif len(firmware) != FLASH_SIZE:
            raise RuntimeError('recovery image must be exactly 65536 bytes')

        raw_port = bool(transaction.get('raw_port', False))
        managed = not raw_port
        raw_managed_device = str(transaction.get('raw_managed_device') or '')
        self.args.raw_port = raw_port
        self.args.port = str(transaction.get('port') or '')
        self.args.device = str(transaction.get('device') or '')
        self.args.mode = str(transaction.get('mode') or '')
        runtime_uid = str(transaction.get('runtime_uid') or '').upper()
        expected_isp = str(transaction.get('isp_uid') or '').upper()
        saved_port = transaction.get('port_identity')
        current_port = freeze_serial_port(self.args.port)
        if legacy_recovery:
            self.reporter.log(
                'WARN', 'Recovering a pre-release transaction without a preserved serial identity')
        else:
            if not isinstance(saved_port, dict):
                raise RuntimeError('recovery transaction has no serial-port identity')
            if int(saved_port.get('rdev', -1)) != current_port['rdev']:
                raise RuntimeError('recovery serial device is not the original device')

        erase_started = False
        prepared_attempted = False
        raw_guard_devices = []
        try:
            if managed:
                prepared_attempted = True
                managed_access(self.args, 'PREPARE', recovery=True)
            elif raw_managed_device:
                prepared_attempted = True
                managed_access_device(
                    self.args.moonraker, raw_managed_device,
                    'PREPARE', recovery=True)
            if raw_port:
                raw_guard_devices = raw_flash_guard_acquire(
                    self.args, exclude=(raw_managed_device,))
                if raw_guard_devices:
                    self.reporter.log(
                        'INFO', 'Paused BMCU UID fallback scanners while the interrupted raw flash is retried')

            idle, state = printer_is_idle(self.args.moonraker)
            if not idle:
                raise RuntimeError(
                    'printer state changed to %s before recovery erase; '
                    'firmware recovery is blocked' % state)

            def before_erase(identity):
                nonlocal erase_started
                idle_now, state_now = printer_is_idle(self.args.moonraker)
                if not idle_now:
                    raise RuntimeError(
                        'printer state changed to %s at the final recovery '
                        'pre-erase gate; firmware recovery is blocked' % state_now)
                if expected_isp and identity.uid_hex.upper() != expected_isp:
                    raise RuntimeError(
                        'recovery ISP UID does not match the interrupted module')
                erase_started = True

            wait_serial_port_free(current_port)
            identity = flash_image(
                self.args.port, firmware, mode=self.args.mode,
                manual_timeout=self.args.manual_timeout,
                log_callback=self.reporter.log,
                progress_callback=self.reporter.progress,
                before_erase=before_erase)
            if expected_isp and identity.uid_hex.upper() != expected_isp:
                raise RuntimeError('recovery ISP UID changed unexpectedly')
            if runtime_uid:
                self._save_identity(runtime_uid, identity.uid_hex.upper())
            transaction['phase'] = 'recovery_flashed_verified'
            self._record_transaction(transaction)
            if managed:
                managed_access(self.args, 'RESUME')
                transaction['phase'] = 'recovery_waiting_runtime'
                self._record_transaction(transaction)
                if runtime_uid:
                    wait_managed_ready(self.args, runtime_uid, 60.0)
            elif raw_port:
                transaction['phase'] = 'retry_raw_flash_waiting_registration'
                self._record_transaction(transaction)
            self._clear_transaction()
            self._remove_file(image_path)
            final_message = (
                'Firmware retry flashed and verified - waiting for BMCU runtime registration'
                if raw_port else 'Firmware retry flashed, verified and reconnected')
            self.reporter.emit(
                'result', ok=True, recovered=True, message=final_message,
                mode=self.args.mode, port=self.args.port, raw_port=raw_port,
                runtime_uid=runtime_uid, isp_uid=identity.uid_hex.upper(),
                released_device=raw_managed_device,
                released_devices=([raw_managed_device] if raw_managed_device else []) +
                list(raw_guard_devices))
        except Exception as exc:
            erase_started = erase_started or bool(getattr(exc, 'erase_started', False))
            if raw_guard_devices:
                try:
                    raw_flash_guard_release(self.args, raw_guard_devices)
                except Exception as guard_exc:
                    self.reporter.log('ERROR', str(guard_exc))
            if managed and prepared_attempted and not erase_started:
                self._cancel_managed_before_erase()
            elif raw_managed_device and prepared_attempted and not erase_started:
                try:
                    managed_access_device(
                        self.args.moonraker, raw_managed_device, 'RESUME')
                except Exception as resume_exc:
                    self.reporter.log(
                        'ERROR', 'Could not release the raw configured port back to Klipper: %s' % resume_exc)
            elif erase_started:
                self.reporter.log(
                    'ERROR', 'Flash retry did not finish after erase; the exact image remains preserved for the next Flash firmware attempt.')
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
    parser.add_argument('command', choices=('check', 'update', 'recover'))
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
    parser.add_argument('--manual-timeout', type=float, default=120.0)
    parser.add_argument('--json-lines', action='store_true')
    return parser

def main(argv=None):
    args = build_parser().parse_args(argv)
    reporter = Reporter(args.json_lines)
    try:
        validate_cli_args(args)
        state_dir = prepare_private_directory(args.state_dir)
    except (ValueError, RuntimeError) as exc:
        reporter.emit('result', ok=False, error=str(exc), recovery_required=False)
        return 2
    try:
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
            else:
                updater.recover()
        return 0
    except Exception as exc:
        recovery_required = False
        transaction_path = state_dir / 'transaction.json'
        if transaction_path.is_file() and not transaction_path.is_symlink():
            try:
                recovery_required = bool(read_json_file(
                    transaction_path, MAX_TRANSACTION_BYTES).get('recovery_required'))
            except Exception:
                recovery_required = True
        reporter.emit('result', ok=False, error=str(exc),
                      recovery_required=recovery_required)
        return 1

if __name__ == '__main__':
    sys.exit(main())
