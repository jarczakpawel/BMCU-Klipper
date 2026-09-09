#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import collections
import errno
import glob
import fcntl
import logging
import logging.handlers
import json
import os
import select
import signal
import socket
import stat
import struct
import sys
import termios
import time

HERE = os.path.dirname(os.path.realpath(__file__))
RUNTIME = os.path.dirname(HERE)
EXTRAS = os.path.join(RUNTIME, 'klippy', 'extras')
VENDOR = os.path.join(RUNTIME, 'vendor', 'pyserial-3.5-py2.py3-none-any.whl')
for entry in (EXTRAS, VENDOR):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from bmcu_core import protocol, transport

_INTERNAL_BASE = 0xF0000000
_MAX_SERIAL_TX = 256 * 1024
_MAX_CLIENT_TX = 512 * 1024
_MAX_RELIABLE = 1024
_MAX_EXTERNAL_PENDING = 2048
_EXTERNAL_TIMEOUT = 310.0
_COMPLETED_OPERATION_TTL = 310.0
_OPERATION_START_TYPES = frozenset((
    protocol.MSG_TEST_ENCODER, protocol.MSG_CHANNEL_AUTOLOAD,
    protocol.MSG_FEED_TO_CONTACT, protocol.MSG_FEED_DISTANCE,
    protocol.MSG_CHANNEL_RETRACT, protocol.MSG_CAL_AUTO_START,
))

class TransportDaemon(object):
    def __init__(self, args):
        self.args = args
        self.running = True
        self.listener = None
        self.control = None
        self.client = None
        self.client_decoder = transport.MessageDecoder()
        self.client_tx = bytearray()
        self.client_paused = False
        self.client_control_only = False
        self.client_epoch = 0
        self.client_seq = 1

        self.external_pending = {}
        self.operation_wire_by_client = {}
        self.external_cmd = 1
        self.reliable = collections.deque()
        self.latest_status = None
        self.latest_snapshot = None
        self.latest_hello = None
        self.latest_caps = None
        self.last_status_sent = 0.0
        self.serial = None
        self.current_port = ''
        self.last_good_port = ''
        self.serial_candidate_cursor = 0
        self.foreign_uid_until = {}
        self.serial_decoder = protocol.StreamDecoder()
        self.serial_tx = bytearray()
        self.next_serial_connect = 0.0
        self.serial_settle_until = 0.0
        self.serial_generation = 0
        self.seq = 1
        self.internal_cmd = _INTERNAL_BASE
        self.internal_pending = {}
        self.session_id = 0
        self.runtime_uid = ''
        self.session_ready = False
        self.hello_sent = False
        self.handshake_phase = 'idle'
        self.handshake_started_at = 0.0
        self.next_handshake_action = 0.0
        self.heartbeat_cmd = 0
        self.heartbeat_sent_at = 0.0
        self.serial_lease_fd = None
        self.serial_lease_path = ''
        self.last_serial_rx = 0.0
        self.last_serial_tx = 0.0
        self.last_keepalive = 0.0
        self.serial_released = False
        self.pending_local_hello = []
        self.pending_local_snapshot = []
        self.latest_status_dict = None
        self.last_semantic_status = None
        self.status_file_due = 0.0

    def setup(self):
        for path in (self.args.socket, self.args.socket + '.ctl', self.args.status_file):
            try:
                if os.path.lexists(path):
                    os.unlink(path)
            except OSError:
                pass
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.setblocking(False)
        self.listener.bind(self.args.socket)
        os.chmod(self.args.socket, 0o600)
        self.listener.listen(1)
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.control.setblocking(False)
        self.control.bind(self.args.socket + '.ctl')
        os.chmod(self.args.socket + '.ctl', 0o600)

        self._write_status_file(force=True)
        self.next_serial_connect = time.monotonic()

    def cleanup(self):
        self._drop_client()
        self._close_serial('shutdown', notify=False)
        for sock in (self.control, self.listener):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        for path in (self.args.socket, self.args.socket + '.ctl', self.args.status_file):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _next_seq(self):
        value = self.seq
        self.seq = (self.seq + 1) & 0xffffffff
        return value

    def _next_internal_cmd(self):
        value = self.internal_cmd
        self.internal_cmd = (self.internal_cmd + 1) & 0xffffffff
        if self.internal_cmd < _INTERNAL_BASE:
            self.internal_cmd = _INTERNAL_BASE
        return value

    def _next_external_cmd(self):

        value = self.external_cmd
        self.external_cmd += 1
        if self.external_cmd >= _INTERNAL_BASE:
            self.external_cmd = 1
        return value

    def _remove_external(self, wire_cmd):
        record = self.external_pending.pop(wire_cmd, None)
        if record and record.get('kind') in ('operation', 'operation_complete'):
            key = (record.get('epoch'), record.get('client_cmd'))
            if self.operation_wire_by_client.get(key) == wire_cmd:
                self.operation_wire_by_client.pop(key, None)
        return record

    @staticmethod
    def _rewrite_op_result_id(payload, op_id):
        if len(payload) < 4:
            return payload
        return struct.pack('<I', int(op_id) & 0xffffffff) + payload[4:]

    @staticmethod
    def _semantic_status_fingerprint(status):
        auto_cal = status.get('auto_calibration', {}) or {}
        return (
            int(status.get('session_id', 0) or 0),
            tuple(int(v) for v in status.get('present', [0] * 4)),
            tuple(int(v) for v in status.get('motion', [0] * 4)),
            tuple(int(v) for v in status.get('route_state', [0] * 4)),
            int(status.get('connected_mask', 0) or 0),
            int(status.get('encoder_io_mask', 0) or 0),
            int(status.get('calibration_valid_mask', 0) or 0),
            int(status.get('active_op_id', 0) or 0),
            int(status.get('active_op_type', 0) or 0),
            int(status.get('active_op_state', 0) or 0),
            str(status.get('active_op_reason', '') or ''),
            int(status.get('error_flags', 0) or 0),
            bool(status.get('nvm_fault', False)),
            bool(auto_cal.get('active', False)),
            int(auto_cal.get('stage', 0) or 0),
            int(auto_cal.get('done_mask', 0) or 0),
            int(auto_cal.get('state', 0) or 0),
        )

    def _transport_status(self):
        return {
            'schema': 1, 'name': self.args.name, 'pid': os.getpid(),
            'online': bool(self.session_ready),
            'serial_released': bool(self.serial_released),
            'uid': self.runtime_uid,
            'serial_open': bool(self.serial is not None),
            'port': self.current_port or self.last_good_port or self.args.port,
            'expected_uid': self.args.expected_uid,
            'generation': int(self.serial_generation),
            'session_id': int(self.session_id),
            'updated_at': time.time(),
            'status': self.latest_status_dict,
        }

    def _write_status_file(self, force=False):
        path = self.args.status_file
        if not path:
            return
        now = time.monotonic()
        if not force and now < self.status_file_due:
            return
        self.status_file_due = now + 0.50
        value = self._transport_status()
        temporary = '%s.%d.tmp' % (path, os.getpid())
        try:
            with open(temporary, 'w') as stream:
                json.dump(value, stream, sort_keys=True, separators=(',', ':'))
                stream.write('\n')
                stream.flush()
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            logging.exception('could not publish live sidecar status')
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _queue_serial(self, msg_type, cmd_id, payload=b'', purpose=None):
        if self.serial is None:
            raise RuntimeError('serial transport is offline')
        frame = protocol.encode_packet(
            msg_type, self._next_seq(), cmd_id, payload)
        if len(self.serial_tx) + len(frame) > _MAX_SERIAL_TX:
            raise RuntimeError('sidecar serial transmit queue overflow')
        self.serial_tx.extend(frame)
        self.last_serial_tx = time.monotonic()
        if purpose is not None:
            self.internal_pending[cmd_id] = purpose

    def _queue_internal(self, msg_type, payload=b'', purpose='internal'):
        cmd_id = self._next_internal_cmd()
        self._queue_serial(msg_type, cmd_id, payload, purpose=purpose)
        return cmd_id

    def _queue_reliable(self, frame):
        if len(self.reliable) >= _MAX_RELIABLE:

            logging.error(
                'reliable IPC queue saturated; dropping stalled Klipper client')
            self._drop_client()
        if len(self.reliable) < _MAX_RELIABLE:
            self.reliable.append(frame)

    def _queue_client(self, op, arg1=0, arg2=0, arg3=0, payload=b'',
                      reliable=False, urgent=False, control=False):
        frame = transport.encode_message(op, arg1, arg2, arg3, payload)
        blocked = (self.client is None or
                   ((self.client_paused or
                     (self.client_control_only and not control)) and
                    not urgent))
        if reliable and blocked:
            self._queue_reliable(frame)
            return
        if blocked:
            return
        if len(self.client_tx) + len(frame) > _MAX_CLIENT_TX:
            if reliable:
                self._queue_reliable(frame)

            return
        self._append_client_frame(frame)

    def _append_client_frame(self, frame):
        if frame[4] == transport.OP_PACKET:
            frame = bytearray(frame)
            struct.pack_into('<I', frame, 9, self.client_seq)
            self.client_seq = (self.client_seq + 1) & 0xffffffff
            if self.client_seq == 0:
                self.client_seq = 1
        self.client_tx.extend(frame)

    def _queue_packet_client(self, msg_type, seq, cmd_id, payload,
                             reliable=False, urgent=False, control=False):

        self._queue_client(
            transport.OP_PACKET, msg_type, 0, cmd_id, payload,
            reliable=reliable, urgent=urgent, control=control)

    def _queue_link(self, online, error=''):
        self._queue_client(
            transport.OP_LINK, 1 if online else 0,
            self.serial_generation, self.session_id,
            str(error or '').encode('utf-8', 'replace'), reliable=True,
            urgent=not bool(online), control=True)

    def _flush_reliable(self, include_status=True):
        if self.client is None or self.client_paused:
            return
        while self.reliable:
            frame = self.reliable[0]
            if len(self.client_tx) + len(frame) > _MAX_CLIENT_TX:
                break
            self._append_client_frame(frame)
            self.reliable.popleft()
        if include_status and self.latest_status is not None:
            msg_type, seq, cmd_id, payload = self.latest_status
            self._queue_packet_client(msg_type, seq, cmd_id, payload)
            self.last_status_sent = time.monotonic()

    def _accept_client(self):
        conn, _address = self.listener.accept()
        conn.setblocking(False)
        self._drop_client()
        self.client = conn
        self.client_decoder.clear()
        self.client_tx[:] = b''
        self.client_epoch += 1
        self.client_seq = 1
        self.client_paused = False
        self.client_control_only = False
        self._queue_link(self.session_ready, '' if self.session_ready else 'BMCU transport is connecting')

    def _drop_client(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self.client_decoder.clear()
        self.client_tx[:] = b''

        old_epoch = self.client_epoch
        orphaned = False
        for wire, record in list(self.external_pending.items()):
            if record.get('epoch') != old_epoch:
                continue
            if record.get('kind') == 'operation':
                record['orphaned'] = True
                orphaned = True
            else:
                self._remove_external(wire)
        if orphaned and self.serial is not None and self.session_ready:
            try:
                self._queue_internal(
                    protocol.MSG_ABORT_OP, b'', 'orphan_abort')
                logging.warning(
                    'aborting BMCU operation orphaned by Klipper disconnect')
            except Exception:
                logging.exception('could not abort orphaned BMCU operation')
        self.pending_local_hello[:] = [
            value for value in self.pending_local_hello
            if value[0] != old_epoch]
        self.pending_local_snapshot[:] = [
            value for value in self.pending_local_snapshot
            if value[0] != old_epoch]
        self.reliable.clear()

    @staticmethod
    def _serial_path_rank(path):
        path = str(path)
        if path.startswith('/dev/serial/by-path/'):
            return 0
        if path.startswith('/dev/serial/by-id/'):
            return 1
        if path.startswith('/dev/ttyCH343USB'):
            return 2
        if path.startswith('/dev/ttyUSB'):
            return 3
        if path.startswith('/dev/ttyACM'):
            return 4
        return 5

    def _serial_lease_name(self, resolved):
        info = os.stat(resolved)
        if not stat.S_ISCHR(info.st_mode):
            raise RuntimeError('serial candidate is not a character device')
        return os.path.join(
            os.path.dirname(self.args.status_file),
            '.serial-lease-%d-%d.lock' %
            (os.major(info.st_rdev), os.minor(info.st_rdev)))

    def _acquire_serial_lease(self, resolved):
        if self.serial_lease_fd is not None:
            raise RuntimeError('serial lease is already held')
        path = self._serial_lease_name(resolved)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('unsafe serial lease file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, ('%s %d\n' % (self.args.name, os.getpid())).encode('ascii'))
            self.serial_lease_fd = fd
            self.serial_lease_path = path
            return True
        except Exception:
            os.close(fd)
            return False

    def _release_serial_lease(self):
        fd = self.serial_lease_fd
        self.serial_lease_fd = None
        self.serial_lease_path = ''
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _serial_candidates(self):
        paths = [self.args.port]
        if self.args.expected_uid and self.args.scan_fallback:
            for pattern in ('/dev/serial/by-path/*', '/dev/serial/by-id/*',
                            '/dev/ttyCH343USB*', '/dev/ttyUSB*', '/dev/ttyACM*'):
                paths.extend(glob.glob(pattern))
        grouped = {}
        excluded_real = set()
        for excluded in self.args.exclude_ports:
            try:
                if os.path.exists(excluded):
                    excluded_real.add(os.path.realpath(excluded))
            except OSError:
                pass
        primary_real = ''
        try:
            if os.path.exists(self.args.port):
                primary_real = os.path.realpath(self.args.port)
        except OSError:
            pass
        now = time.monotonic()
        self.foreign_uid_until = {
            key: value for key, value in self.foreign_uid_until.items()
            if value > now}
        for path in paths:
            try:
                resolved = os.path.realpath(path)
                info = os.stat(resolved)
            except OSError:
                continue
            if self.foreign_uid_until.get(resolved, 0.0) > now:
                continue
            if not stat.S_ISCHR(info.st_mode):
                continue
            tty = os.path.basename(resolved)
            if path != self.args.port and not tty.startswith(('ttyUSB', 'ttyCH343USB', 'ttyACM')):
                continue
            if resolved in excluded_real:
                continue
            aliases = grouped.setdefault(resolved, set())
            aliases.add(path)
        result = []
        for resolved, aliases in grouped.items():
            preferred = min(aliases, key=lambda value: (self._serial_path_rank(value), value))
            result.append((resolved, preferred))
        result.sort(key=lambda item: (
            0 if self.last_good_port and item[1] == self.last_good_port else
            1 if primary_real and item[0] == primary_real else 2,
            self._serial_path_rank(item[1]), item[1]))
        return [item[1] for item in result]

    def _open_serial(self):
        if self.serial_released or self.serial is not None:
            return
        import serial
        candidates = self._serial_candidates()
        if not candidates:
            raise RuntimeError('no serial candidate is present')
        last_error = None
        for offset in range(len(candidates)):
            index = (self.serial_candidate_cursor + offset) % len(candidates)
            port = candidates[index]
            try:
                resolved = os.path.realpath(port)
                if not self._acquire_serial_lease(resolved):
                    continue
            except OSError as exc:
                last_error = exc
                continue
            connection = serial.Serial()
            connection.port = port
            connection.baudrate = self.args.baud
            connection.timeout = 0
            connection.write_timeout = 0
            connection.rtscts = False
            connection.dsrdtr = False
            if hasattr(connection, 'exclusive'):
                try:
                    connection.exclusive = True
                except Exception:
                    pass
            connection.dtr = True
            connection.rts = False
            try:
                connection.open()
            except Exception as exc:
                last_error = exc
                try:
                    connection.close()
                except Exception:
                    pass
                self._release_serial_lease()
                continue
            self.serial_candidate_cursor = (index + 1) % len(candidates)
            try:
                connection.setDTR(True)
                connection.setRTS(False)
                attrs = termios.tcgetattr(connection.fileno())
                attrs[2] &= ~termios.HUPCL
                termios.tcsetattr(connection.fileno(), termios.TCSANOW, attrs)
            except Exception:
                pass
            try:
                os.set_blocking(connection.fileno(), False)
            except (AttributeError, OSError):
                pass
            self.serial = connection
            self.current_port = port
            self.serial_decoder = protocol.StreamDecoder()
            self.serial_tx[:] = b''
            self.serial_generation = (self.serial_generation + 1) & 0xffffffff
            self.seq = 1
            self.internal_pending.clear()
            self.session_id = 0
            self.session_ready = False
            self.hello_sent = False
            self.handshake_phase = 'idle'
            self.heartbeat_cmd = 0
            self.heartbeat_sent_at = 0.0
            now = time.monotonic()
            self.last_serial_rx = now
            self.last_serial_tx = now
            self.serial_settle_until = now + self.args.connect_settle
            self.handshake_started_at = self.serial_settle_until
            self.next_handshake_action = self.serial_settle_until
            logging.info('opened %s for %s', port, self.args.name)
            self._write_status_file(force=True)
            return
        raise RuntimeError('no serial candidate could be opened: %s' % last_error)

    def _close_serial(self, reason, notify=True):
        was_open = self.serial is not None or self.session_ready
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
        self.serial = None
        self._release_serial_lease()
        self.current_port = ''
        self.serial_tx[:] = b''
        self.serial_decoder = protocol.StreamDecoder()
        self.internal_pending.clear()
        self.external_pending.clear()
        self.operation_wire_by_client.clear()
        self.reliable.clear()
        self.session_ready = False
        self.hello_sent = False
        self.handshake_phase = 'idle'
        self.handshake_started_at = 0.0
        self.next_handshake_action = 0.0
        self.heartbeat_cmd = 0
        self.heartbeat_sent_at = 0.0
        self.session_id = 0
        self.runtime_uid = ''
        self.latest_hello = None
        self.latest_status = None
        self.latest_snapshot = None
        self.latest_caps = None
        self.pending_local_hello[:] = []
        self.pending_local_snapshot[:] = []
        if not self.serial_released:
            self.next_serial_connect = time.monotonic() + self.args.reconnect
        self.latest_status_dict = None
        self.last_semantic_status = None
        self._write_status_file(force=True)
        if notify and was_open:
            logging.warning('serial offline: %s', reason)
            self._queue_link(False, reason)

    def _start_handshake(self):
        for cmd_id, purpose in list(self.internal_pending.items()):
            if purpose in ('hello', 'confirm'):
                self.internal_pending.pop(cmd_id, None)
        nonce = struct.unpack('<I', os.urandom(4))[0]
        self._queue_internal(
            protocol.MSG_HELLO, struct.pack('<I', nonce), 'hello')
        self.hello_sent = True
        self.handshake_phase = 'wait_hello'
        self.next_handshake_action = time.monotonic() + 0.60

    def _finish_ready(self):
        self.session_ready = True
        self.handshake_phase = 'ready'
        self.handshake_started_at = 0.0
        self.next_handshake_action = 0.0
        if self.current_port:
            self.last_good_port = self.current_port
        self.heartbeat_cmd = 0
        self.heartbeat_sent_at = 0.0
        self._queue_internal(protocol.MSG_UPDATE_CANCEL, b'', 'resume_update')
        self._queue_internal(protocol.MSG_GET_SNAPSHOT, b'', 'snapshot')
        self._queue_link(True)
        self._write_status_file(force=True)
        pending = list(self.pending_local_hello)
        self.pending_local_hello[:] = []
        for epoch, cmd_id in pending:
            if epoch == self.client_epoch:
                self._synthetic_hello(cmd_id)

    def _queue_pending_hello(self, cmd_id):
        self.pending_local_hello[:] = [
            item for item in self.pending_local_hello
            if item[0] != self.client_epoch]
        self.pending_local_hello.append((self.client_epoch, cmd_id))
        if len(self.pending_local_hello) > 128:
            del self.pending_local_hello[:-128]

    def _synthetic_hello(self, cmd_id):
        if self.latest_hello is None:
            self._queue_pending_hello(cmd_id)
            return
        self._queue_packet_client(
            protocol.MSG_HELLO_ACK, 0, cmd_id, self.latest_hello,
            reliable=True, control=True)

    def _synthetic_ack(self, cmd_id, ok=True, error=0):
        payload = struct.pack('<IBH', cmd_id, 1 if ok else 0, int(error))
        self._queue_packet_client(
            protocol.MSG_ACK, 0, cmd_id, payload, reliable=True, control=True)

    def _queue_pending_snapshot(self, cmd_id):
        self.pending_local_snapshot[:] = [
            value for value in self.pending_local_snapshot
            if value[0] != self.client_epoch]
        self.pending_local_snapshot.append((self.client_epoch, cmd_id))

    def _handle_client_message(self, message):
        op, arg1, arg2, _arg3, payload = message
        if op != transport.OP_SEND:
            return
        msg_type = int(arg1) & 0xff
        cmd_id = int(arg2) & 0xffffffff
        if msg_type == protocol.MSG_HELLO:
            if self.session_ready and self.latest_hello is not None:
                self._synthetic_hello(cmd_id)
            else:
                self._queue_pending_hello(cmd_id)
            return
        if msg_type == protocol.MSG_SESSION_CONFIRM:
            valid = (self.session_ready and len(payload) == 4 and
                     struct.unpack('<I', payload)[0] == self.session_id)
            self._synthetic_ack(cmd_id, valid, 0 if valid else 1)
            return

        if self.serial is None or not self.session_ready:
            self._queue_client(
                transport.OP_ERROR, cmd_id, 0, 0,
                b'BMCU transport is offline', reliable=True, control=True)
            return
        if msg_type == protocol.MSG_GET_SNAPSHOT:
            self._queue_pending_snapshot(cmd_id)
            if 'snapshot' not in self.internal_pending.values():
                self._queue_internal(
                    protocol.MSG_GET_SNAPSHOT, b'', 'snapshot')
            return
        if len(self.external_pending) >= _MAX_EXTERNAL_PENDING:
            self._queue_client(
                transport.OP_ERROR, cmd_id, 0, 0,
                b'BMCU sidecar command map is full',
                reliable=True, control=True)
            return

        if msg_type == protocol.MSG_GET_OP_RESULT and len(payload) == 4:
            client_op = struct.unpack('<I', payload)[0]
            target_wire = self.operation_wire_by_client.get(
                (self.client_epoch, client_op))
            completed = self.external_pending.get(target_wire)
            if (completed is not None and
                    completed.get('kind') == 'operation_complete' and
                    completed.get('result_payload') is not None):
                self._queue_packet_client(
                    protocol.MSG_OP_RESULT, 0, cmd_id,
                    completed.get('result_payload'), reliable=True,
                    urgent=True, control=True)
                return

        wire_cmd = self._next_external_cmd()
        record = {
            'epoch': self.client_epoch,
            'client_cmd': cmd_id,
            'deadline': time.monotonic() + _EXTERNAL_TIMEOUT,
            'kind': 'request',
            'msg_type': msg_type,
        }
        if msg_type in _OPERATION_START_TYPES:
            record['kind'] = 'operation'
            self.operation_wire_by_client[(self.client_epoch, cmd_id)] = wire_cmd
            logging.info('operation map start client=%d wire=%d type=0x%02X',
                         cmd_id, wire_cmd, msg_type)
        elif msg_type == protocol.MSG_GET_OP_RESULT and len(payload) == 4:
            client_op = struct.unpack('<I', payload)[0]
            target_wire = self.operation_wire_by_client.get(
                (self.client_epoch, client_op))
            record['kind'] = 'operation_query'
            record['target_client_op'] = client_op
            record['target_wire_op'] = target_wire or client_op
            if target_wire:
                payload = struct.pack('<I', target_wire)
        self.external_pending[wire_cmd] = record
        self._queue_serial(msg_type, wire_cmd, payload)

    def _handle_control(self, data):
        if not data:
            return
        command = data[:1]
        if command == transport.CTRL_PAUSE:
            self.client_paused = True
            self.client_control_only = False
            return
        if command == transport.CTRL_RESUME_REQUIRED:

            self.client_paused = False
            self.client_control_only = True
            self._queue_link(
                self.session_ready, '' if self.session_ready else
                'BMCU transport is connecting')
            self._flush_reliable(include_status=False)
            return
        if command == transport.CTRL_RESUME:
            self.client_paused = False
            self.client_control_only = False
            self._queue_link(self.session_ready, '' if self.session_ready else 'BMCU transport is connecting')
            self._flush_reliable()
            return
        if command == transport.CTRL_RELEASE_SERIAL:
            self.serial_released = True
            self._close_serial('released for firmware update')
            return
        if command == transport.CTRL_RECONNECT_SERIAL:
            self.serial_released = False
            self.next_serial_connect = time.monotonic()
            return
        if command == transport.CTRL_SNAPSHOT and self.session_ready and self.serial is not None:
            self._queue_internal(protocol.MSG_GET_SNAPSHOT, b'', 'snapshot')

    def _handle_serial_packet(self, msg_type, seq, cmd_id, payload):
        self.last_serial_rx = time.monotonic()
        purpose = self.internal_pending.get(cmd_id)
        if purpose == 'hello' and msg_type == protocol.MSG_HELLO_ACK:
            hello = protocol.parse_hello(payload)
            if self.args.expected_uid and hello['uid'] != self.args.expected_uid:
                try:
                    if self.current_port:
                        self.foreign_uid_until[os.path.realpath(self.current_port)] = time.monotonic() + 5.0
                except OSError:
                    pass
                raise RuntimeError('UID mismatch expected=%s got=%s' %
                                   (self.args.expected_uid, hello['uid']))
            self.latest_hello = payload
            self.runtime_uid = hello['uid']
            self.session_id = int(hello['session_id'])
            self.internal_pending.pop(cmd_id, None)
            for pending_cmd, pending_purpose in list(self.internal_pending.items()):
                if pending_purpose == 'hello':
                    self.internal_pending.pop(pending_cmd, None)
            confirm = struct.pack('<I', self.session_id)
            self._queue_internal(protocol.MSG_SESSION_CONFIRM, confirm, 'confirm')
            self.handshake_phase = 'wait_confirm'
            self.next_handshake_action = time.monotonic() + 1.0
            return
        if purpose == 'confirm' and msg_type == protocol.MSG_ACK:
            echo, ok, error = struct.unpack('<IBH', payload)
            if echo != cmd_id or not ok:
                raise RuntimeError('session confirmation failed error=%d' % error)
            self.internal_pending.pop(cmd_id, None)
            self._finish_ready()
            return
        if purpose == 'heartbeat' and msg_type == protocol.MSG_PONG:
            self.internal_pending.pop(cmd_id, None)
            self.heartbeat_cmd = 0
            self.heartbeat_sent_at = 0.0
            return
        if purpose == 'orphan_abort' and msg_type in (
                protocol.MSG_ACK, protocol.MSG_ERROR):
            self.internal_pending.pop(cmd_id, None)
            return
        if purpose == 'resume_update' and msg_type in (
                protocol.MSG_ACK, protocol.MSG_ERROR):
            if msg_type != protocol.MSG_ACK or len(payload) != 7:
                raise RuntimeError('could not leave firmware-update mode after reconnect')
            echo, ok, error = struct.unpack('<IBH', payload)
            if echo != cmd_id or not ok:
                raise RuntimeError('firmware-update cancellation failed error=%d' % error)
            self.internal_pending.pop(cmd_id, None)
            return
        if purpose == 'snapshot' and msg_type == protocol.MSG_SNAPSHOT:
            self.internal_pending.pop(cmd_id, None)
            self.latest_snapshot = payload
            try:
                self.latest_status_dict = protocol.parse_snapshot(payload)['status']
                self.last_semantic_status = self._semantic_status_fingerprint(
                    self.latest_status_dict)
                self._write_status_file(force=True)
            except Exception:
                logging.exception('invalid snapshot while publishing sidecar state')
            pending = list(self.pending_local_snapshot)
            self.pending_local_snapshot[:] = []
            for epoch, external_cmd in pending:
                if epoch == self.client_epoch:
                    self._queue_packet_client(
                        protocol.MSG_SNAPSHOT, seq, external_cmd, payload,
                        reliable=True, control=True)
            return
        if purpose == 'caps' and msg_type == protocol.MSG_CAPS:
            self.internal_pending.pop(cmd_id, None)
            self.latest_caps = payload
            return

        if msg_type == protocol.MSG_STATUS:
            self.latest_status = (msg_type, seq, 0, payload)
            now = time.monotonic()
            semantic_changed = False
            try:
                self.latest_status_dict = protocol.parse_status(payload)
                fingerprint = self._semantic_status_fingerprint(
                    self.latest_status_dict)
                semantic_changed = fingerprint != self.last_semantic_status
                self.last_semantic_status = fingerprint

                self._write_status_file(force=semantic_changed)
            except Exception:
                logging.exception('invalid status while publishing sidecar state')
            record = self.external_pending.get(cmd_id)
            if record is not None and record.get('kind') == 'request':
                self._remove_external(cmd_id)
                if record.get('epoch') == self.client_epoch:
                    self._queue_packet_client(
                        msg_type, seq, record.get('client_cmd'), payload,
                        reliable=True, control=True)
                    self.last_status_sent = now
            elif semantic_changed:

                self._queue_packet_client(
                    msg_type, seq, 0, payload, reliable=True,
                    urgent=True, control=True)
                self.last_status_sent = now
            elif (not self.client_paused and not self.client_control_only and
                  now - self.last_status_sent >= self.args.status_interval):
                self._queue_packet_client(msg_type, seq, 0, payload)
                self.last_status_sent = now
            return

        record = self.external_pending.get(cmd_id)
        if msg_type == protocol.MSG_OP_RESULT:
            if record is not None and record.get('kind') in (
                    'operation', 'operation_complete'):
                client_op = record.get('client_cmd')
                translated = self._rewrite_op_result_id(payload, client_op)

                first_result = record.get('kind') == 'operation'
                record['kind'] = 'operation_complete'
                record['deadline'] = time.monotonic() + _COMPLETED_OPERATION_TTL
                record['result_payload'] = translated
                if first_result and record.get('epoch') == self.client_epoch:
                    self._queue_packet_client(
                        msg_type, seq, client_op, translated,
                        reliable=True, urgent=True, control=True)
                if first_result:
                    logging.info('operation map finish client=%d wire=%d',
                                 client_op, cmd_id)
                if record.get('orphaned'):
                    self._remove_external(cmd_id)
                return
            if record is not None and record.get('kind') == 'operation_query':
                client_op = record.get('target_client_op')
                translated = self._rewrite_op_result_id(payload, client_op)
                self._remove_external(cmd_id)
                if record.get('epoch') == self.client_epoch:
                    self._queue_packet_client(
                        msg_type, seq, record.get('client_cmd'), translated,
                        reliable=True, urgent=True, control=True)
                return

        if record is not None:
            kind = record.get('kind')

            if kind in ('operation', 'operation_complete') and msg_type == protocol.MSG_ACK:
                ok = 1
                if len(payload) == 7:
                    _wire_echo, ok, error = struct.unpack('<IBH', payload)
                    payload = struct.pack(
                        '<IBH', record.get('client_cmd'), ok, error)

                if (kind == 'operation' and
                        record.get('epoch') == self.client_epoch):
                    self._queue_packet_client(
                        msg_type, seq, record.get('client_cmd'), payload,
                        reliable=True, control=True)

                if not ok and kind == 'operation':
                    self._remove_external(cmd_id)
                return
            self._remove_external(cmd_id)
            if record.get('epoch') == self.client_epoch:
                if msg_type == protocol.MSG_ACK and len(payload) == 7:
                    _wire_echo, ok, error = struct.unpack('<IBH', payload)
                    payload = struct.pack(
                        '<IBH', record.get('client_cmd'), ok, error)
                self._queue_packet_client(
                    msg_type, seq, record.get('client_cmd'), payload,
                    reliable=True, control=True)
            return

        if msg_type in (protocol.MSG_JAM, protocol.MSG_ERROR,
                        protocol.MSG_OP_RESULT):
            self._queue_packet_client(
                msg_type, seq, cmd_id, payload, reliable=True,
                urgent=True, control=True)

    def _read_serial(self):
        data = os.read(self.serial.fileno(), 4096)
        if not data:
            raise OSError(errno.EIO, 'serial EOF')
        previous = self.serial_decoder.errors
        packets = self.serial_decoder.feed(data)
        if self.serial_decoder.incompatible_version is not None:
            raise RuntimeError('incompatible BMCU protocol version')
        if self.serial_decoder.errors != previous:
            logging.warning('discarded %d malformed serial frame(s)',
                            self.serial_decoder.errors - previous)
        for packet in packets:
            self._handle_serial_packet(*packet)

    def _read_client(self):
        data = self.client.recv(65536)
        if not data:
            self._drop_client()
            return
        for message in self.client_decoder.feed(data):
            self._handle_client_message(message)

    def _flush_serial(self):
        if not self.serial_tx:
            return
        try:
            written = os.write(self.serial.fileno(), self.serial_tx[:4096])
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise
        if written:
            del self.serial_tx[:written]

    def _flush_client(self):
        if self.client is None or not self.client_tx:
            return
        try:
            written = self.client.send(self.client_tx[:65536])
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise
        if written:
            del self.client_tx[:written]
            self._flush_reliable(include_status=False)

    def _periodic(self, now):
        if self.serial is None and not self.serial_released and now >= self.next_serial_connect:
            try:
                self._open_serial()
            except Exception as exc:
                logging.warning('serial connect failed: %s', exc)
                self.next_serial_connect = now + self.args.reconnect
        if self.serial is not None:
            if not self.session_ready:
                if (now >= self.serial_settle_until and
                        self.handshake_started_at and
                        now - self.handshake_started_at >= self.args.connection_timeout):
                    self._close_serial('HELLO/session handshake timeout')
                elif now >= self.next_handshake_action:
                    try:
                        self._start_handshake()
                    except Exception as exc:
                        self._close_serial(str(exc))
            if self.session_ready:
                heartbeat_retry = max(1.0, min(
                    self.args.heartbeat * 0.5,
                    self.args.connection_timeout * 0.25))
                if (self.heartbeat_cmd and self.heartbeat_sent_at and
                        now - self.heartbeat_sent_at >= heartbeat_retry):
                    self.internal_pending.pop(self.heartbeat_cmd, None)
                    self.heartbeat_cmd = 0
                    self.heartbeat_sent_at = 0.0

                if (now - self.last_serial_tx >= self.args.heartbeat and
                        not self.heartbeat_cmd):
                    try:
                        self.heartbeat_cmd = self._queue_internal(
                            protocol.MSG_PING, b'B', 'heartbeat')
                        self.heartbeat_sent_at = now
                    except Exception as exc:
                        self._close_serial(str(exc))
                if now - self.last_serial_rx > self.args.connection_timeout:
                    self._close_serial('receive timeout after heartbeat retries')
        if self.external_pending:
            expired = [wire for wire, value in self.external_pending.items()
                       if value.get('deadline', 0.0) <= now]
            for wire in expired:
                record = self._remove_external(wire)
                if record and record.get('kind') == 'operation':
                    logging.error('operation map expired client=%s wire=%s',
                                  record.get('client_cmd'), wire)
        if self.client is not None and not self.client_paused:

            keepalive_interval = 10.0 if self.client_control_only else 2.0
            if now - self.last_keepalive >= keepalive_interval:
                self._queue_client(
                    transport.OP_KEEPALIVE, 0, self.serial_generation,
                    self.session_id, control=True)
                self.last_keepalive = now

    def run(self):
        try:
            self.setup()
            while self.running:
                now = time.monotonic()
                self._periodic(now)
                reads = [self.listener, self.control]
                writes = []
                if self.client is not None:
                    reads.append(self.client)
                    if self.client_tx:
                        writes.append(self.client)
                if self.serial is not None:
                    reads.append(self.serial.fileno())
                    if self.serial_tx:
                        writes.append(self.serial.fileno())
                try:
                    readable, writable, _errors = select.select(
                        reads, writes, [], 0.25)
                except InterruptedError:
                    continue
                for item in readable:
                    try:
                        if item is self.listener:
                            self._accept_client()
                        elif item is self.control:
                            data, address = self.control.recvfrom(64)
                            self._handle_control(data)
                            if address:
                                reply = self._transport_status()
                                reply.pop('status', None)
                                reply['control'] = data[:1].decode('ascii')
                                self.control.sendto(json.dumps(
                                    reply, separators=(',', ':')).encode('utf-8'), address)
                        elif item is self.client:
                            self._read_client()
                        else:
                            self._read_serial()
                    except Exception as exc:
                        if item is self.client:
                            logging.warning('client disconnected: %s', exc)
                            self._drop_client()
                        elif item is self.listener or item is self.control:
                            logging.exception('local IPC failure')
                        else:
                            self._close_serial(str(exc))
                for item in writable:
                    try:
                        if item is self.client:
                            self._flush_client()
                        else:
                            self._flush_serial()
                    except Exception as exc:
                        if item is self.client:
                            logging.warning('client transmit failed: %s', exc)
                            self._drop_client()
                        else:
                            self._close_serial(str(exc))
        finally:
            self.cleanup()

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    parser.add_argument('--port', required=True)
    parser.add_argument('--expected-uid', default='')
    parser.add_argument('--scan-fallback', action='store_true')
    parser.add_argument('--exclude-port', action='append', default=[])
    parser.add_argument('--socket', required=True)
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--heartbeat', type=float, default=5.0)
    parser.add_argument('--connection-timeout', type=float, default=15.0)
    parser.add_argument('--reconnect', type=float, default=1.0)
    parser.add_argument('--status-interval', type=float, default=0.5)
    parser.add_argument('--connect-settle', type=float, default=1.5)
    parser.add_argument('--log-file', required=True)
    parser.add_argument('--status-file', required=True)
    args = parser.parse_args()
    args.expected_uid = str(args.expected_uid or '').strip().upper()
    args.exclude_ports = tuple(str(value) for value in (args.exclude_port or []) if value)
    if args.expected_uid and (len(args.expected_uid) != 24 or
                              any(ch not in '0123456789ABCDEF' for ch in args.expected_uid)):
        parser.error('--expected-uid must be 24 hexadecimal characters')
    return args

def main():
    args = parse_args()
    handler = logging.handlers.RotatingFileHandler(
        args.log_file, maxBytes=1024 * 1024, backupCount=1)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    state = {'running': True, 'daemon': None}

    def stop(_signum, _frame):
        state['running'] = False
        daemon = state.get('daemon')
        if daemon is not None:
            daemon.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while state['running']:
        daemon = TransportDaemon(args)
        state['daemon'] = daemon
        try:
            daemon.run()
        except Exception:
            logging.exception('BMCU transport cycle failed; rebuilding sidecar')
            if state['running']:
                time.sleep(min(1.0, max(0.1, args.reconnect)))
        finally:
            state['daemon'] = None

if __name__ == '__main__':
    main()
