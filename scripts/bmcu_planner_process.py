import configparser
import fcntl
import json
import os
import signal
import stat
import time


class PlannerProcessError(RuntimeError):
    pass


def read_record(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, 'r') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PlannerProcessError('unsafe BMCU process record: %s' % path)
        try:
            record = json.loads(stream.read(8192))
        except ValueError:
            return {}
    return record if isinstance(record, dict) else {}


def alive(record, runtime, kind='planner'):
    if not isinstance(record, dict):
        return False
    try:
        pid = int(record.get('pid', 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    folder, filename = {
        'planner': ('scripts', 'bmcu_plannerd.py'),
        'transport': ('scripts', 'bmcu_transportd.py'),
        'panel': ('web', 'bmcu_panel_server.py'),
    }[kind]
    daemon = os.path.join(os.path.realpath(runtime), folder, filename)
    socket_path = str(record.get('state_dir' if kind == 'panel' else 'socket', '') or '')
    if (pid <= 1 or record.get('daemon') != daemon or
            not os.path.isabs(socket_path) or '\x00' in socket_path):
        return False
    try:
        os.kill(pid, 0)
        with open('/proc/%d/cmdline' % pid, 'rb') as stream:
            command = stream.read(8192).decode('utf-8', 'replace').split('\x00')
    except (FileNotFoundError, ProcessLookupError):
        return False
    if kind == 'panel':
        expected = {'--root': os.path.join(os.path.realpath(runtime), 'web'),
                    '--state-dir': socket_path}
    else:
        expected = {'--socket': socket_path}
    if kind == 'transport':
        for option, key in (('--name', 'name'), ('--port', 'port'), ('--status-file', 'status_file')):
            if not record.get(key):
                return False
            expected[option] = record[key]
        if record.get('uid'):
            expected['--expected-uid'] = record['uid']
    return bool(daemon in command and all(
        any(command[index] == option and command[index + 1] == value
            for index in range(len(command) - 1))
        for option, value in expected.items()))


def command_records(runtime, kind):
    folder, filename = ('web', 'bmcu_panel_server.py') if kind == 'panel' else (
        'scripts', 'bmcu_transportd.py')
    daemon = os.path.join(os.path.realpath(runtime), folder, filename)
    fields = (('--root', 'root'), ('--state-dir', 'state_dir')) if kind == 'panel' else (
        ('--name', 'name'), ('--port', 'port'), ('--socket', 'socket'),
        ('--status-file', 'status_file'), ('--expected-uid', 'uid'))
    records = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % entry, 'rb') as stream:
                command = stream.read(8192).decode('utf-8', 'replace').split('\x00')
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if daemon not in command:
            continue
        record = {'pid': int(entry), 'daemon': daemon}
        for option, key in fields:
            if option in command and command.index(option) + 1 < len(command):
                record[key] = command[command.index(option) + 1]
        if alive(record, runtime, kind):
            records.append(record)
    return records


def lock_owner(socket_path, runtime, kind='planner'):
    try:
        descriptor = os.open(
            socket_path + '.lock', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, 'r') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PlannerProcessError('unsafe BMCU %s lock file' % kind)
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return {}
            except BlockingIOError:
                pass
            try:
                stream.seek(0)
                owner = json.loads(stream.read(8192))
            except ValueError:
                owner = {}
            if alive(owner, runtime, kind) and owner.get('socket') == socket_path:
                return owner
            if time.monotonic() >= deadline:
                raise PlannerProcessError(
                    'BMCU %s lock is held but its owner cannot be verified' % kind)
            time.sleep(0.05)


def socket_directory(bmcu_dir):
    config = configparser.RawConfigParser(
        interpolation=None, strict=True, delimiters=(':', '='),
        comment_prefixes=('#', ';'), inline_comment_prefixes=('#', ';'))
    path = os.path.join(bmcu_dir, 'bmcu.cfg')
    if os.path.lexists(path):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'r') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PlannerProcessError('unsafe BMCU configuration')
            config.read_file(stream)
    directory = config.get(
        'bmcu', 'transport_socket_dir', fallback='/tmp/bmcu-transport').strip()
    if (not os.path.isabs(directory) or len(directory.encode('utf-8')) > 80 or
            any(ord(char) < 32 for char in directory)):
        raise PlannerProcessError('invalid BMCU transport socket directory')
    return directory


def processes(bmcu_dir, socket_path=None):
    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    try:
        record = read_record(os.path.join(bmcu_dir, 'planner-process.json'))
    except PermissionError:
        record = {}
    records = {}
    if alive(record, runtime):
        records[int(record['pid'])] = record
    if socket_path is None:
        socket_path = os.path.join(socket_directory(bmcu_dir), 'u1-planner.sock')
    sockets = {socket_path}
    sockets.update(item['socket'] for item in records.values())
    for path in sorted(sockets):
        owner = lock_owner(path, runtime)
        if not owner:
            continue
        previous = records.get(int(owner['pid']), {})
        if any(previous.get(key) != owner.get(key)
               for key in ('pid', 'daemon', 'socket', 'result_dir')):
            records[int(owner['pid'])] = owner
    return list(records.values())


def stop_record(record, runtime, kind='planner'):
    if not alive(record, runtime, kind):
        return False
    pid = int(record['pid'])
    for signum, timeout in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
        if not alive(record, runtime, kind):
            return True
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not alive(record, runtime, kind):
                return True
            time.sleep(0.05)
    if alive(record, runtime, kind):
        raise PlannerProcessError('BMCU %s did not stop' % kind)
    return True


def stop(bmcu_dir, locked=False):
    descriptor = None
    try:
        if not locked:
            descriptor = os.open(
                bmcu_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
        stopped = 0
        for record in processes(bmcu_dir):
            stopped += int(stop_record(record, runtime))
            path = record['socket']
            if os.path.lexists(path):
                if not stat.S_ISSOCK(os.lstat(path).st_mode):
                    raise PlannerProcessError('U1 planner socket path is not a socket')
                os.unlink(path)
        return stopped
    finally:
        if descriptor is not None:
            os.close(descriptor)
