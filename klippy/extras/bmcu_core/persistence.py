# SPDX-License-Identifier: GPL-3.0-or-later

import os
import queue
import stat
import tempfile
import threading
import time

_TIMEOUT = object()

class DurableWriteError(RuntimeError):
    pass

class _Job(object):
    def __init__(self, path, payload, mode, completion):
        self.path = path
        self.payload = payload
        self.mode = mode
        self.completion = completion
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.started = 0.0
        self.finished = 0.0

class DurableWriteWorker(object):

    def __init__(self, reactor, queue_limit=16):
        self.reactor = reactor
        self.queue = queue.Queue(maxsize=max(1, int(queue_limit)))
        self.closed = False
        self.stats = {
            'writes': 0,
            'failures': 0,
            'timeouts': 0,
            'max_write_ms': 0.0,
            'queue_overflows': 0,
        }
        self.thread = threading.Thread(
            target=self._run, name='bmcu-durable-writer')
        self.thread.daemon = True
        self.thread.start()

    @staticmethod
    def _write_file(path, payload, requested_mode):
        path = os.path.abspath(str(path or ''))
        directory = os.path.dirname(path)
        if not path or not directory or not os.path.isdir(directory):
            raise DurableWriteError('invalid durable-write path')
        if os.path.islink(directory):
            raise DurableWriteError('durable-write directory is a symlink')

        mode = int(requested_mode if requested_mode is not None else 0o600)
        owner = None
        if os.path.lexists(path):
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise DurableWriteError('durable-write target is not a regular file')
            mode = stat.S_IMODE(info.st_mode)
            owner = (int(info.st_uid), int(info.st_gid))

        fd, temp_path = tempfile.mkstemp(
            prefix='.bmcu-durable-', suffix='.tmp', dir=directory)
        try:
            os.fchmod(fd, mode)
            if owner is not None and hasattr(os, 'fchown'):
                try:
                    os.fchown(fd, owner[0], owner[1])
                except OSError:
                    pass
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass

    def _notify(self, job):
        job.event.set()
        completion = job.completion
        if completion is None:
            return
        async_complete = getattr(self.reactor, 'async_complete', None)
        try:
            if callable(async_complete):
                async_complete(completion, True)
                return
            register = getattr(self.reactor, 'register_async_callback', None)
            if callable(register):
                register(lambda eventtime: completion.complete(True))
        except Exception:

            pass

    def _run(self):
        while True:
            job = self.queue.get()
            if job is None:
                self.queue.task_done()
                return
            job.started = time.monotonic()
            try:
                self._write_file(job.path, job.payload, job.mode)
                job.result = True
                self.stats['writes'] += 1
            except Exception as exc:
                job.error = exc
                self.stats['failures'] += 1
            finally:
                job.finished = time.monotonic()
                elapsed_ms = max(0.0, (job.finished - job.started) * 1000.0)
                self.stats['max_write_ms'] = max(
                    float(self.stats['max_write_ms']), elapsed_ms)
                self._notify(job)
                self.queue.task_done()
                if self.closed and self.queue.empty():
                    return

    def _reactor_running(self):
        return bool(getattr(self.reactor, '_process', False) and
                    getattr(self.reactor, '_pipe_fds', None) is not None)

    def write(self, path, payload, mode=0o600, timeout=30.0):
        if self.closed:
            raise DurableWriteError('durable writer is closed')
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        if not isinstance(payload, (bytes, bytearray)):
            raise DurableWriteError('durable payload must be bytes or text')
        completion_factory = getattr(self.reactor, 'completion', None)
        completion = completion_factory() if callable(completion_factory) else None
        job = _Job(path, bytes(payload), mode, completion)
        try:
            self.queue.put_nowait(job)
        except queue.Full:
            self.stats['queue_overflows'] += 1
            raise DurableWriteError('durable writer queue is full')

        timeout = max(0.1, float(timeout))
        if self._reactor_running() and completion is not None:
            deadline = self.reactor.monotonic() + timeout
            result = completion.wait(deadline, waketime_result=_TIMEOUT)
            if result is _TIMEOUT and not job.event.is_set():
                self.stats['timeouts'] += 1
                raise DurableWriteError('durable write timed out')
        elif not job.event.wait(timeout):
            self.stats['timeouts'] += 1
            raise DurableWriteError('durable write timed out')

        if job.error is not None:
            if isinstance(job.error, DurableWriteError):
                raise job.error
            raise DurableWriteError(str(job.error))
        return bool(job.result)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except queue.Full:

            pass
