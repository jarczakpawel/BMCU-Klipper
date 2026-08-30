#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import print_function

import argparse
import json
import os
import re
import signal
import socket
import stat
import sys
import tempfile
import time

_RUNTIME = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_EXTRAS = os.path.join(_RUNTIME, 'klippy', 'extras')
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)

from bmcu_core.gcode_plan import PLAN_SCHEMA, PlanError, scan_file

_REQUEST_ID = re.compile(r'^[0-9a-f]{32}$')
_STOP = False

def _stop(_signum, _frame):
    global _STOP
    _STOP = True

def _safe_socket_path(path):
    path = os.path.abspath(str(path or ''))
    if (not path.startswith('/') or '\x00' in path or
            any(ord(ch) < 32 for ch in path) or
            len(path.encode('utf-8')) >= 100):
        raise ValueError('invalid planner socket path')
    return path

def _safe_result_dir(path):
    path = os.path.abspath(str(path or ''))
    if (not path.startswith('/') or '\x00' in path or
            any(ord(ch) < 32 for ch in path)):
        raise ValueError('invalid planner result directory')
    return path

def _unlink_socket(path):
    if not os.path.lexists(path):
        return
    info = os.lstat(path)
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError('planner socket path is occupied by a non-socket')
    os.unlink(path)

def _atomic_result(directory, request_id, payload):
    target = os.path.join(directory, request_id + '.json')
    fd, temporary = tempfile.mkstemp(prefix='.' + request_id + '.', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
            stream.write('\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _cleanup_results(directory, max_age=300.0):
    cutoff = time.time() - float(max_age)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not re.match(r'^[0-9a-f]{32}\.json$', name):
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_mtime < cutoff):
                os.unlink(path)
        except OSError:
            pass

def _parse_request(raw):
    if len(raw) > 8192:
        raise PlanError('planner request is too large')
    try:
        request = json.loads(raw.decode('utf-8'))
    except Exception:
        raise PlanError('planner request is not valid JSON')
    if not isinstance(request, dict):
        raise PlanError('planner request must be an object')
    request_id = str(request.get('id', '') or '')
    if _REQUEST_ID.fullmatch(request_id) is None:
        raise PlanError('invalid planner request id')
    expected = request.get('identity')
    if not isinstance(expected, dict):
        raise PlanError('planner request has no G-code identity')
    return request_id, str(request.get('path', '') or ''), expected

def main():
    parser = argparse.ArgumentParser(prog='bmcu_plannerd')
    parser.add_argument('--socket', required=True)
    parser.add_argument('--result-dir', required=True)
    args = parser.parse_args()

    socket_path = _safe_socket_path(args.socket)
    result_dir = _safe_result_dir(args.result_dir)
    parent = os.path.dirname(socket_path)
    if not os.path.isdir(parent) or os.path.islink(parent):
        raise RuntimeError('planner socket directory is unavailable')
    parent_info = os.stat(parent)
    if stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise RuntimeError('planner socket directory is not private')
    if os.path.lexists(result_dir):
        if os.path.islink(result_dir) or not os.path.isdir(result_dir):
            raise RuntimeError('planner result path is unsafe')
    else:
        os.mkdir(result_dir, 0o700)
    os.chmod(result_dir, 0o700)
    _cleanup_results(result_dir, 0.0)
    _unlink_socket(socket_path)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        listener.bind(socket_path)
        os.chmod(socket_path, 0o600)
        listener.settimeout(1.0)
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        print('BMCU U1 source planner ready: %s' % socket_path,
              file=sys.stderr, flush=True)
        requests = 0
        while not _STOP:
            try:
                raw = listener.recv(8193)
            except socket.timeout:
                continue
            except InterruptedError:
                continue
            request_id = ''
            started = time.monotonic()
            try:
                request_id, path, expected = _parse_request(raw)
                result = scan_file(path, expected)
                payload = {
                    'ok': True,
                    'request_id': request_id,
                    'schema': PLAN_SCHEMA,
                    'result': result,
                }
            except Exception as exc:
                if not request_id:
                    continue
                payload = {
                    'ok': False,
                    'request_id': request_id,
                    'schema': PLAN_SCHEMA,
                    'error': str(exc),
                }
            payload['daemon_ms'] = round(
                (time.monotonic() - started) * 1000.0, 3)
            try:
                _atomic_result(result_dir, request_id, payload)
            except OSError as exc:
                print('bmcu_plannerd: cannot publish result %s: %s' %
                      (request_id, exc), file=sys.stderr, flush=True)
            requests += 1
            if requests % 32 == 0:
                _cleanup_results(result_dir)
    finally:
        listener.close()
        try:
            _unlink_socket(socket_path)
        except Exception:
            pass
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('bmcu_plannerd: %s' % exc, file=sys.stderr)
        sys.exit(1)
