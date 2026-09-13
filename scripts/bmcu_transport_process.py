import fcntl
import os
import re
import stat

import bmcu_planner_process as process


def processes(bmcu_dir, socket_dir=None):
    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    directory = socket_dir or process.socket_directory(bmcu_dir)
    records = {}
    try:
        stored = process.read_record(os.path.join(bmcu_dir, 'transport-processes.json')).get('devices', {})
    except PermissionError:
        stored = {}
    if isinstance(stored, dict):
        for name, value in stored.items():
            if not isinstance(value, dict):
                continue
            record = dict(value, name=name)
            if process.alive(record, runtime, 'transport'):
                records[int(record['pid'])] = record
    for record in process.command_records(runtime, 'transport'):
        records.setdefault(int(record['pid']), record)
    sockets = {record['socket'] for record in records.values()}
    if os.path.lexists(directory):
        if os.path.islink(directory) or not os.path.isdir(directory):
            raise process.PlannerProcessError('unsafe BMCU transport socket directory')
        for name in os.listdir(directory):
            if name != 'u1-planner.sock.lock' and re.fullmatch(r'[A-Za-z0-9_.-]+\.sock\.lock', name):
                sockets.add(os.path.join(directory, name[:-5]))
    for path in sorted(sockets):
        owner = process.lock_owner(path, runtime, 'transport')
        if owner:
            records[int(owner['pid'])] = owner
    return list(records.values())


def stop_record(record, runtime):
    stopped = process.stop_record(record, runtime, 'transport')
    path = record['socket']
    descriptor = os.open(path + '.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise process.PlannerProcessError('unsafe BMCU transport lock')
        if os.geteuid() == 0:
            parent = os.stat(os.path.dirname(path))
            os.fchown(descriptor, parent.st_uid, parent.st_gid)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise process.PlannerProcessError('BMCU transport socket has a new live owner')
        for candidate in (path, path + '.ctl', record['status_file']):
            if not os.path.lexists(candidate):
                continue
            info = os.lstat(candidate)
            expected = stat.S_ISREG if candidate == record['status_file'] else stat.S_ISSOCK
            if not expected(info.st_mode):
                raise process.PlannerProcessError('unsafe BMCU transport path: %s' % candidate)
            os.unlink(candidate)
    finally:
        os.close(descriptor)
    return stopped


def stop(bmcu_dir, locked=False):
    descriptor = None
    try:
        if not locked:
            descriptor = os.open(bmcu_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
        records = processes(bmcu_dir)
        stopped = sum(int(process.stop_record(record, runtime, 'transport')) for record in records)
        for record in records:
            stop_record(record, runtime)
        return stopped
    finally:
        if descriptor is not None:
            os.close(descriptor)


def panel_processes(bmcu_dir):
    runtime = os.path.realpath(os.path.join(bmcu_dir, 'runtime'))
    try:
        stored = process.read_record(os.path.join(bmcu_dir, 'panel-process.json'))
    except PermissionError:
        stored = {}
    records = []
    for record in process.command_records(runtime, 'panel'):
        if record['state_dir'] != os.path.join(bmcu_dir, 'update'):
            continue
        record['server'] = record['daemon']
        if stored.get('pid') == record['pid'] and stored.get('server') == record['server']:
            record['digest'] = stored.get('digest', '')
            record['settings_digest'] = stored.get('settings_digest', '')
        records.append(record)
    return records


def stop_panel(bmcu_dir):
    runtime = os.path.join(bmcu_dir, 'runtime')
    return sum(int(process.stop_record(record, runtime, 'panel'))
               for record in panel_processes(bmcu_dir))
