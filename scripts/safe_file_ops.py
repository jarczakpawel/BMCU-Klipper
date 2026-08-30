#!/usr/bin/env python3
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

class SafeFileError(RuntimeError):
    pass

def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def ensure_directory(path):
    path = Path(path)
    if os.path.lexists(str(path)):
        mode = os.lstat(str(path)).st_mode
        if stat.S_ISLNK(mode):
            raise SafeFileError('refusing symlinked directory: %s' % path)
        if not stat.S_ISDIR(mode):
            raise SafeFileError('not a directory: %s' % path)
    else:
        path.mkdir(parents=True, exist_ok=True)
    return path

def require_regular_file(path):
    path = Path(path)
    try:
        info = os.lstat(str(path))
    except OSError as exc:
        raise SafeFileError('file not found: %s' % path) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SafeFileError('refusing symlinked file: %s' % path)
    if not stat.S_ISREG(info.st_mode):
        raise SafeFileError('not a regular file: %s' % path)
    return path, info

def _stat_identity(info):

    return (
        info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, info.st_size,
        getattr(info, 'st_mtime_ns', int(info.st_mtime * 1000000000)),
        getattr(info, 'st_ctime_ns', int(info.st_ctime * 1000000000)),
    )

def _open_verified_regular(path, expected):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise SafeFileError('could not safely open file: %s' % path) from exc
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode):
            raise SafeFileError('not a regular file: %s' % path)
        if _stat_identity(current) != _stat_identity(expected):
            raise SafeFileError('file changed during operation: %s' % path)
        return fd
    except Exception:
        os.close(fd)
        raise

def _verify_unchanged(path, expected):
    try:
        current = os.lstat(str(path))
    except OSError as exc:
        raise SafeFileError('file changed during operation: %s' % path) from exc
    if (not stat.S_ISREG(current.st_mode) or
            _stat_identity(current) != _stat_identity(expected)):
        raise SafeFileError('file changed during operation: %s' % path)

def read_regular_text(path, maximum=4 * 1024 * 1024, encoding='utf-8'):
    if maximum < 0:
        raise ValueError('maximum must be non-negative')
    path, info = require_regular_file(path)
    if info.st_size > maximum:
        raise SafeFileError('file is too large: %s' % path)
    fd = _open_verified_regular(path, info)
    try:
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    raw = b''.join(chunks)
    if len(raw) > maximum:
        raise SafeFileError('file is too large: %s' % path)
    try:
        return raw.decode(encoding), info
    except UnicodeDecodeError as exc:
        raise SafeFileError('file is not valid %s: %s' % (encoding, path)) from exc

def _require_expected_regular(path, expected):
    path = Path(path)
    if expected is None:
        return require_regular_file(path)
    _verify_unchanged(path, expected)
    return path, expected

def backup_file(path, backup_dir, label, expected_info=None):
    path, info = _require_expected_regular(path, expected_info)

    backup_dir = ensure_directory(backup_dir)
    guard_fd = _open_verified_regular(path, info)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    base = '%s.%s_%s_%d' % (path.name, label, stamp, os.getpid())
    destination = None
    fd = None
    for index in range(1000):
        suffix = '' if index == 0 else '_%d' % index
        candidate = backup_dir / (base + suffix)
        try:
            fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         stat.S_IMODE(info.st_mode))
            destination = candidate
            break
        except FileExistsError:
            continue
    if destination is None or fd is None:
        os.close(guard_fd)
        raise SafeFileError('could not allocate a unique backup name for %s' % path)
    source_fd = None
    try:
        source_fd = _open_verified_regular(path, info)
        try:
            os.fchown(fd, info.st_uid, info.st_gid)
        except PermissionError:
            pass
        with os.fdopen(fd, 'wb') as target, os.fdopen(source_fd, 'rb') as source:
            fd = None
            source_fd = None
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        _fsync_directory(backup_dir)
        return destination
    except Exception:
        if fd is not None:
            os.close(fd)
        if source_fd is not None:
            os.close(source_fd)
        try:
            os.unlink(str(destination))
        except OSError:
            pass
        raise
    finally:
        os.close(guard_fd)

def atomic_write_text(path, text, encoding='utf-8', expected_info=None):
    path, info = _require_expected_regular(path, expected_info)

    guard_fd = _open_verified_regular(path, info)
    try:
        directory = ensure_directory(path.parent)
    except Exception:
        os.close(guard_fd)
        raise
    fd, temporary = tempfile.mkstemp(
        prefix='.%s.bmcu-' % path.name, suffix='.tmp', dir=str(directory))
    try:
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        try:
            os.fchown(fd, info.st_uid, info.st_gid)
        except PermissionError:
            pass
        with os.fdopen(fd, 'w', encoding=encoding, newline='') as stream:
            fd = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _verify_unchanged(path, info)
        os.replace(temporary, str(path))
        _fsync_directory(directory)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    finally:
        os.close(guard_fd)

def atomic_create_text(path, text, mode=0o644, encoding='utf-8'):

    path = Path(path)
    if os.path.lexists(str(path)):
        raise SafeFileError('refusing to replace existing path: %s' % path)
    directory = ensure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(
        prefix='.%s.bmcu-' % path.name, suffix='.tmp', dir=str(directory))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding=encoding, newline='') as stream:
            fd = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, str(path))
        except FileExistsError as exc:
            raise SafeFileError('destination appeared during create: %s' % path) from exc
        os.unlink(temporary)
        temporary = None
        _fsync_directory(directory)
    except Exception:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise
