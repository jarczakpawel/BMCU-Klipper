#!/usr/bin/env python3
import argparse
import os
import re
import tempfile
import stat
import time
from pathlib import Path

NAME_RE = re.compile(r'^[A-Za-z0-9_.-]+$')
UID_RE = re.compile(r'^(?:-|[0-9A-Fa-f]{24})$')
MIN_BAUD = 9600
MAX_BAUD = 2000000
MAX_CONFIG_BYTES = 4 * 1024 * 1024

class ConfigError(ValueError):
    pass

def _read_regular_text(path, allow_missing=False):
    path = Path(path)
    if not os.path.lexists(str(path)):
        if allow_missing:
            return '', None
        raise ConfigError('file not found: %s' % path)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise ConfigError('refusing unsafe file: %s' % path) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError('file is not regular: %s' % path)
        if info.st_size > MAX_CONFIG_BYTES:
            raise ConfigError('file is too large: %s' % path)
        chunks = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    raw = b''.join(chunks)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError('file is too large: %s' % path)
    try:
        return raw.decode('utf-8'), info
    except UnicodeDecodeError as exc:
        raise ConfigError('file is not valid UTF-8: %s' % path) from exc

def _verify_same_file(path, expected):
    try:
        current = os.lstat(str(path))
    except OSError as exc:
        raise ConfigError('configuration changed during update: %s' % path) from exc
    if ((current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino) or
            not stat.S_ISREG(current.st_mode)):
        raise ConfigError('configuration changed during update: %s' % path)

def _ensure_real_directory(path):
    path = Path(path)
    if os.path.lexists(str(path)):
        info = os.lstat(str(path))
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ConfigError('refusing unsafe backup directory: %s' % path)
    else:
        path.mkdir(parents=True, exist_ok=True)
    return path

def _is_section(line):
    stripped = line.strip()
    return stripped.startswith('[') and stripped.endswith(']')

def parse_device_record(text):
    parts = [part.strip() for part in str(text).split(',')]
    if len(parts) != 3:
        raise ConfigError('device line must be name, serial path and UID')
    if any(not part for part in parts):
        raise ConfigError('device line contains an empty field; use - for unknown UID')
    name, port, uid = parts[:3]
    if not NAME_RE.match(name):
        raise ConfigError('invalid BMCU name: %s' % name)
    if not port or any(ord(ch) < 32 for ch in port) or ',' in port:
        raise ConfigError('invalid serial path for %s' % name)
    if not UID_RE.match(uid):
        raise ConfigError('UID for %s must be - or 24 hexadecimal characters' % name)
    uid = uid.upper()
    if uid != '-' and uid in ('0' * 24, 'F' * 24):
        raise ConfigError('UID for %s cannot be all-zero or all-FF' % name)
    return {
        'name': name,
        'port': port,
        'uid': uid,
    }

def render_device_record(record):
    return ','.join([record['name'], record['port'], record.get('uid') or '-'])

def validate_records(records, source='configuration'):
    names = {}
    ports = {}
    uids = {}
    for record in records:
        name = record['name']
        port = record['port']
        uid = record.get('uid', '-').upper()
        if name in names:
            raise ConfigError('duplicate device name %s in %s' % (name, source))
        previous = ports.get(port)
        if previous is not None:
            previous_uid = previous.get('uid', '-').upper()
            if uid == '-' or previous_uid == '-':
                raise ConfigError(
                    'duplicate serial hint %s in %s requires hardware UIDs' %
                    (port, source))
        if uid != '-' and uid in uids:
            raise ConfigError('duplicate hardware UID %s in %s' % (uid, source))
        names[name] = record
        ports[port] = record
        if uid != '-':
            uids[uid] = record

def parse_detected(path):
    text, _info = _read_regular_text(path)
    lines = text.splitlines()
    in_bmcu = False
    in_devices = False
    found_section = False
    baud = None
    tool_count = None
    devices = []
    for line in lines:
        stripped = line.strip()
        if _is_section(line):
            in_bmcu = stripped.lower() == '[bmcu]'
            in_devices = False
            if in_bmcu:
                found_section = True
            continue
        if not in_bmcu:
            continue
        if stripped.startswith('tool_count:'):
            try:
                tool_count = int(stripped.split(':', 1)[1].strip())
            except ValueError:
                raise ConfigError('invalid tool_count in detected file')
            if tool_count != 4:
                raise ConfigError('detected tool_count must be 4')
            continue
        if stripped.startswith('baud:'):
            try:
                baud = int(stripped.split(':', 1)[1].strip())
            except ValueError:
                raise ConfigError('invalid baud in detected file')
            if baud < MIN_BAUD or baud > MAX_BAUD:
                raise ConfigError('detected baud is outside %d..%d' %
                                  (MIN_BAUD, MAX_BAUD))
            continue
        if stripped == 'devices:':
            in_devices = True
            continue
        if in_devices:
            if not stripped or stripped.startswith('#'):
                continue
            if not line.startswith((' ', '\t')):
                in_devices = False
                continue
            devices.append(parse_device_record(stripped))
    if not found_section:
        raise ConfigError('detected file does not contain a [bmcu] section')
    if baud is None:
        raise ConfigError('detected file does not contain baud')
    validate_records(devices, 'detected file')
    return tool_count, baud, devices

def _find_bmcu_section(lines):
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        if not _is_section(line):
            continue
        if line.strip().lower() == '[bmcu]':
            if start is not None:
                raise ConfigError('configuration contains more than one [bmcu] section')
            start = index
            continue
        if start is not None:
            end = index
            break
    return start, end

def _extract_devices(lines, section_start, section_end):
    if section_start is None:
        return [], [], None, None
    device_key = None
    block_end = None
    comments = []
    records = []
    index = section_start + 1
    while index < section_end:
        stripped = lines[index].strip()
        if stripped == 'devices:':
            if device_key is not None:
                raise ConfigError('[bmcu] contains more than one devices block')
            device_key = index
            index += 1
            while index < section_end:
                line = lines[index]
                value = line.strip()
                if not value or value.startswith('#'):
                    comments.append(line)
                    index += 1
                    continue
                if line.startswith((' ', '\t')):
                    records.append(parse_device_record(value))
                    index += 1
                    continue
                break
            block_end = index
            continue
        index += 1
    validate_records(records, 'existing configuration')
    return records, comments, device_key, block_end

def _next_name(used):
    index = 0
    while 'bmcu%d' % index in used:
        index += 1
    return 'bmcu%d' % index

def merge_records(existing, detected, replace=False):
    if replace:
        result = [dict(record) for record in detected]
        validate_records(result, 'replacement result')
        return result

    result = [dict(record) for record in existing]
    by_uid = {record['uid'].upper(): record for record in result
              if record.get('uid', '-') != '-'}
    used_names = {record['name'] for record in result}

    for detected_record in detected:
        uid = detected_record.get('uid', '-').upper()
        port = detected_record['port']
        target = by_uid.get(uid) if uid != '-' else None
        if target is None:
            same_port = [record for record in result if record['port'] == port]
            if uid != '-':
                unknown = [record for record in same_port
                           if record.get('uid', '-').upper() == '-']
                if len(unknown) == 1:
                    target = unknown[0]
            elif len(same_port) == 1:
                target = same_port[0]
            elif len(same_port) > 1:
                raise ConfigError(
                    'serial hint %s matches more than one UID-pinned BMCU' % port)
        if target is not None:
            target['port'] = port
            if uid != '-':
                target['uid'] = uid
                by_uid[uid] = target
            continue

        new_record = dict(detected_record)
        if new_record['name'] in used_names:
            new_record['name'] = _next_name(used_names)
        used_names.add(new_record['name'])
        result.append(new_record)
        if uid != '-':
            by_uid[uid] = new_record

    validate_records(result, 'merged configuration')
    return result

def render_updated_config(original_text, baud, detected_records, replace=False):
    lines = original_text.splitlines()
    section_start, section_end = _find_bmcu_section(lines)
    if section_start is None:
        base = list(lines)
        if base and base[-1].strip():
            base.append('')
        base.extend(['[bmcu]', 'baud: %d' % baud, 'devices:'])
        for record in detected_records:
            base.append('  ' + render_device_record(record))
        return '\n'.join(base) + '\n'

    existing, comments, device_key, block_end = _extract_devices(
        lines, section_start, section_end)
    merged = merge_records(existing, detected_records, replace=replace)

    output = list(lines)
    baud_index = None
    for index in range(section_start + 1, section_end):
        if output[index].strip().startswith('baud:'):
            baud_index = index
            break
    if baud_index is not None:
        output[baud_index] = 'baud: %d' % baud
    else:
        output.insert(section_start + 1, 'baud: %d' % baud)
        section_end += 1
        if device_key is not None:
            device_key += 1
            block_end += 1

    device_lines = ['devices:']
    device_lines.extend(comments)
    device_lines.extend('  ' + render_device_record(record) for record in merged)
    if device_key is None:
        insertion = section_end
        if insertion > 0 and output[insertion - 1].strip():
            device_lines.insert(0, '')
        output[insertion:insertion] = device_lines
    else:
        output[device_key:block_end] = device_lines
    return '\n'.join(output) + '\n'

def _fsync_directory(path):
    flags = getattr(os, 'O_DIRECTORY', 0) | os.O_RDONLY
    try:
        fd = os.open(path or '.', flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def atomic_replace_with_backup(path, text, backup_dir=None, expected_info=None):
    target = os.path.abspath(str(path))
    directory = _ensure_real_directory(os.path.dirname(target) or '.')
    info = None
    if os.path.lexists(target):
        current = os.lstat(target)
        if stat.S_ISLNK(current.st_mode):
            raise ConfigError('refusing to replace symlinked configuration: %s' % target)
        if not stat.S_ISREG(current.st_mode):
            raise ConfigError('configuration is not a regular file: %s' % target)
        if expected_info is not None:
            _verify_same_file(target, expected_info)
            info = expected_info
        else:
            info = current

        backup_root = _ensure_real_directory(
            os.path.abspath(str(backup_dir)) if backup_dir else
            os.path.join(directory, 'backups'))
        stamp = time.strftime('%Y%m%d_%H%M%S')
        backup = os.path.join(backup_root, os.path.basename(target) +
                              '.before_detect_' + stamp)
        suffix = 1
        while os.path.lexists(backup):
            backup = os.path.join(
                backup_root, os.path.basename(target) +
                '.before_detect_' + stamp + '_%d' % suffix)
            suffix += 1
        source_flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
                        getattr(os, 'O_NOFOLLOW', 0))
        source_fd = os.open(target, source_flags)
        backup_fd = None
        try:
            opened = os.fstat(source_fd)
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or
                    not stat.S_ISREG(opened.st_mode)):
                raise ConfigError('configuration changed during backup: %s' % target)
            backup_fd = os.open(
                backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IMODE(info.st_mode))
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                os.write(backup_fd, chunk)
            os.fsync(backup_fd)
        finally:
            os.close(source_fd)
            if backup_fd is not None:
                os.close(backup_fd)
        _fsync_directory(str(backup_root))

    fd, temporary = tempfile.mkstemp(prefix='.bmcu-apply-', suffix='.tmp',
                                     dir=str(directory), text=True)
    try:
        if info is not None:
            os.fchmod(fd, stat.S_IMODE(info.st_mode))
            try:
                os.fchown(fd, info.st_uid, info.st_gid)
            except PermissionError:
                pass
        else:
            os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if info is not None:
            _verify_same_file(target, info)
        elif os.path.lexists(target):
            raise ConfigError('configuration appeared during update: %s' % target)
        os.replace(temporary, target)
        _fsync_directory(str(directory))
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

def render_removed_device(original_text, name):
    if not NAME_RE.match(str(name or '')):
        raise ConfigError('invalid BMCU name: %s' % name)
    lines = original_text.splitlines()
    section_start, section_end = _find_bmcu_section(lines)
    if section_start is None:
        raise ConfigError('missing [bmcu] section')
    existing, comments, device_key, block_end = _extract_devices(
        lines, section_start, section_end)
    if device_key is None:
        raise ConfigError('missing devices block')
    matches = [record for record in existing if record['name'] == name]
    if len(matches) != 1:
        raise ConfigError('configured BMCU %s was not found exactly once' % name)
    remaining = [record for record in existing if record['name'] != name]
    output = list(lines)
    device_lines = ['devices:']
    device_lines.extend(comments)
    device_lines.extend('  ' + render_device_record(record) for record in remaining)
    output[device_key:block_end] = device_lines
    return '\n'.join(output) + '\n'

def remove_device(cfg_path, name, dry_run=False, backup_dir=None):
    path = Path(cfg_path)
    original, original_info = _read_regular_text(path)
    updated = render_removed_device(original, name)
    if dry_run:
        return updated
    atomic_replace_with_backup(
        path, updated, backup_dir=backup_dir, expected_info=original_info)
    return updated

def update_cfg(cfg_path, baud, devices, replace=False, dry_run=False,
               backup_dir=None):
    path = Path(cfg_path)
    original, original_info = _read_regular_text(path, allow_missing=True)
    updated = render_updated_config(original, baud, devices, replace=replace)
    if dry_run:
        return updated
    atomic_replace_with_backup(
        path, updated, backup_dir=backup_dir, expected_info=original_info)
    return updated

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cfg', required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--detected')
    source.add_argument('--remove')
    parser.add_argument('--replace', action='store_true',
                        help='Replace all existing device lines instead of merging by UID/port.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the resulting configuration without writing it.')
    parser.add_argument('--backup-dir')
    args = parser.parse_args()
    try:
        if args.remove:
            if args.replace:
                raise ConfigError('--replace cannot be used with --remove')
            updated = remove_device(
                args.cfg, args.remove, dry_run=args.dry_run,
                backup_dir=args.backup_dir)
            count = 1
            action = 'Removed'
        else:
            _tool_count, baud, devices = parse_detected(args.detected)
            if not devices:
                print('No detected devices to apply.')
                return 1
            updated = update_cfg(args.cfg, baud, devices, replace=args.replace,
                                 dry_run=args.dry_run, backup_dir=args.backup_dir)
            count = len(devices)
            action = 'Replaced with' if args.replace else 'Merged'
    except (ConfigError, OSError) as exc:
        print('BMCU configuration was not updated: %s' % exc)
        return 2
    if args.dry_run:
        print(updated, end='')
    elif args.remove:
        print('%s BMCU %s from %s' % (action, args.remove, args.cfg))
    else:
        print('%s %d detected BMCU device(s) in %s' %
              (action, count, args.cfg))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
