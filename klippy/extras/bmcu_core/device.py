# SPDX-License-Identifier: GPL-3.0-or-later
import errno
import logging
import math
import os
import socket
import struct
from collections import deque

from . import protocol, transport

SNAPSHOT_RETRY_INTERVAL = 1.5
SNAPSHOT_MAX_ATTEMPTS = 3

def _bounded_int(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s must be an integer' % label)
    if value < minimum or value > maximum:
        raise ValueError('%s must be within %d..%d' % (label, minimum, maximum))
    return value

def _bounded_float(value, label, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('%s must be numeric' % label) from exc
    if not math.isfinite(number):
        raise ValueError('%s must be finite' % label)
    if number < minimum or number > maximum:
        raise ValueError('%s must be within %.3g..%.3g' %
                         (label, minimum, maximum))
    return number

class PendingRequest(object):
    def __init__(self, cmd_id, expected, deadline, completion=None):
        self.cmd_id = cmd_id
        self.expected = set(expected)
        self.deadline = deadline
        self.response = None
        self.error = None
        self.completion = completion
        self.completed = False

    def wake(self):
        if self.completed:
            return
        self.completed = True
        if self.completion is not None:
            self.completion.complete(True)

    def set_response(self, value):
        self.response = value
        self.wake()

    def set_error(self, message):
        self.error = str(message)
        self.wake()

class BMCUDevice(object):
    def __init__(self, manager, name, port, expected_uid=''):
        self.manager = manager
        self.printer = manager.printer
        self.reactor = manager.reactor
        self.name = name
        self.port = port
        self.expected_uid = expected_uid.upper()
        self.uid = self.expected_uid
        self.baud = manager.baud
        self.serial = None
        self.ipc_connected = False
        self.transport_online = False
        self.transport_generation = 0
        self.socket_path = os.path.join(
            self.manager.transport_socket_dir, '%s.sock' % self.name)
        self.control_path = self.socket_path + '.ctl'
        self.fd_handle = None
        self._write_callback_registered = False
        self._reactor_quiesced = False
        self._reactor_quiesced_at = 0.0
        self._critical_rx_throttled = False
        self._transport_paused = False
        self._transport_paused_at = 0.0

        self._ipc_resume_grace_until = 0.0
        self.ipc_outage_since = 0.0
        self.ipc_outage_reason = ''
        self.ipc_outage_escalated = False

        self.last_known_physical_online = False
        self.tx_buffer = bytearray()
        self.decoder = transport.MessageDecoder()
        self.connected = False
        self.ready = False
        self.runtime_configured = False
        self.runtime_config_sync_pending = False
        self.ready_at = 0.0
        self.handshake_sent = False
        self.hello_validated = False
        self.hello_probe_validated = False
        self.hello_confirm_cmd_id = 0
        self.hello_candidate = None
        self.seq = 1
        self.cmd_id = 1
        self.pending = {}
        self.status = self._empty_status()

        self.status_revision = 0
        self.travel_meters = [0.0] * 4
        self._travel_last_position = [None] * 4
        self._travel_session_id = None
        self.caps = {}
        self.slots = [{} for _ in range(4)]
        self.calibration = [{} for _ in range(4)]
        self.motion_config = {}
        self.last_op = None
        self.last_error = ''
        self.firmware_compatible = None
        self.firmware_error = ''
        self.lighting_runtime_error = ''
        self.last_rx = 0.0
        self.last_tx = 0.0
        self.last_rx_seq = None
        self.last_connect_attempt = 0.0
        self.session_changed = False
        self.slot_sync_required = True
        self.missed_events = 0
        self.snapshot_cmd_id = 0
        self.snapshot_sent_at = 0.0
        self.snapshot_attempts = 0
        self.heartbeat_cmd_id = 0
        self.hello_cmd_id = 0
        self.hello_nonce = 0
        self.status_reconciled = False

        self._status_notifications = deque()
        self._operation_notifications = deque()
        self._identity_notifications = deque()
        self._connection_notifications = deque()

        self._decoded_packets = deque()

        self._decoded_packet_limit = 256
        self._last_manager_status_fingerprint = None
        self._last_manager_transition_fingerprint = None
        self._last_progress_notification_at = 0.0
        self.next_connect_at = 0.0
        self.reconnect_delay = manager.reconnect_interval
        self.suspended = False
        self.suspend_reason = ''
        self.stats = {
            'rx_bytes': 0, 'tx_bytes': 0, 'rx_packets': 0, 'tx_packets': 0,
            'connects': 0, 'reconnects': 0, 'decode_errors': 0,
            'packet_errors': 0, 'unknown_packets': 0, 'stale_packets': 0,
            'manager_callback_errors': 0,
            'serial_hangups': 0, 'rx_budget_yields': 0,
            'rx_queue_overflows': 0,
            'notification_queue_overflows': 0,
            'status_notifications_coalesced': 0,
            'status_notifications_suppressed': 0,
            'max_rx_callback_ms': 0.0,
            'max_manager_callback_ms': 0.0,
            'operation_notifications_coalesced': 0,
            'manager_work_yields': 0,
            'manager_work_busy_deferrals': 0,
            'critical_quiesce_entries': 0,
            'critical_quiesce_total_s': 0.0,
            'critical_transport_resets': 0,
            'transport_pause_entries': 0,
            'transport_pause_total_s': 0.0,
            'transport_pause_resets': 0,
            'ipc_outages': 0,
            'ipc_outage_recoveries': 0,
            'ipc_resume_grace_entries': 0,
            'physical_link_losses': 0,
        }

        self._packet_drain_timer = self.reactor.register_timer(
            self._drain_decoded_packets, self.reactor.NEVER)
        self._manager_work_timer = self.reactor.register_timer(
            self._drain_manager_work, self.reactor.NEVER)

    @staticmethod
    def _empty_status():
        return {
            'now_channel': 0xff,
            'route_state': [protocol.ROUTE_EMPTY] * 4,
            'route_state_name': ['EMPTY'] * 4,
            'loaded_mask': 0, 'uncertain_mask': 0, 'loaded_channels': [],
            'motion': [0] * 4, 'present': [0] * 4,
            'buffer_pct': [50] * 4, 'buffer_raw': [0.0] * 4,
            'meters': [0.0] * 4, 'travel_meters': [0.0] * 4,
            'motor_pwm': [0] * 4,
            'calibration_valid_mask': 0,
            'calibration_capture_mask': [0] * 4,
            'encoder_io_mask': 0, 'connected_mask': 0,
            'session_id': 0, 'event_counter': 0,
            'active_op_id': 0, 'active_op_type': 0,
            'active_op_channel': 0xff, 'active_op_state': 0,
            'active_op_reason': 'none', 'error_flags': 0,
            'nvm_fault': False, 'nvm_bad_page_mask': 0,
            'auto_calibration': {
                'active': False, 'stage': 0, 'progress': 0,
                'selected_mask': 0, 'done_mask': 0, 'state': 0,
                'failed': False, 'reason_code': 0, 'reason': 'none',
                'channel': 0xff,
            },
        }

    def _attach_travel_diagnostics(self, status):

        positions = list(status.get('meters', [0.0] * 4))
        session_id = int(status.get('session_id', 0) or 0)
        if session_id != self._travel_session_id:
            self._travel_session_id = session_id
            self.travel_meters = [0.0] * 4
            self._travel_last_position = [None] * 4
        for channel in range(4):
            try:
                current = float(positions[channel])
            except (IndexError, TypeError, ValueError, OverflowError):
                current = None
            previous = self._travel_last_position[channel]
            if current is not None and math.isfinite(current):
                if previous is not None:
                    delta = abs(current - previous)

                    if math.isfinite(delta) and delta <= 5.0:
                        self.travel_meters[channel] += delta
                self._travel_last_position[channel] = current
        status['travel_meters'] = list(self.travel_meters)
        return status

    def _next_cmd_id(self):
        value = self.cmd_id
        self.cmd_id = (self.cmd_id + 1) & 0xffffffff
        if self.cmd_id == 0:
            self.cmd_id = 1
        return value

    def _next_seq(self):
        value = self.seq
        self.seq = (self.seq + 1) & 0xffffffff
        return value

    def _accept_rx_seq(self, seq):
        seq = int(seq) & 0xffffffff
        if self.last_rx_seq is None:
            self.last_rx_seq = seq
            return True
        delta = (seq - self.last_rx_seq) & 0xffffffff
        if delta == 0 or delta >= 0x80000000:
            self.stats['stale_packets'] += 1
            return False
        self.last_rx_seq = seq
        return True

    def connect(self, eventtime=None):

        if self.suspended:
            return False
        if self.ipc_connected and self.serial is not None:
            return True
        now = self.reactor.monotonic() if eventtime is None else eventtime
        self.last_connect_attempt = now
        connection = None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(min(0.25, self.manager.reconnect_interval))
            connection.connect(self.socket_path)
            connection.setblocking(False)
            self.serial = connection
            self.decoder = transport.MessageDecoder()
            self.tx_buffer[:] = b''
            self._critical_rx_throttled = False
            try:
                self.fd_handle = self.reactor.register_fd(
                    connection.fileno(), self._fd_event, self._fd_write_event)
                self._write_callback_registered = True
            except TypeError:
                self.fd_handle = self.reactor.register_fd(
                    connection.fileno(), self._fd_event)
                self._write_callback_registered = False
            self.ipc_connected = True
            self.connected = False
            self.transport_online = False
            self.ready = False
            self.runtime_configured = False
            self.runtime_config_sync_pending = False
            self.ready_at = now
            self.handshake_sent = False
            self.hello_validated = False
            self.hello_probe_validated = False
            self.hello_confirm_cmd_id = 0
            self.hello_candidate = None
            self.last_rx = now
            self.last_tx = now
            self._ipc_resume_grace_until = (
                now + float(getattr(
                    self.manager, 'sidecar_ipc_resume_grace', 5.0)))
            if self.ipc_outage_since:
                self.stats['ipc_outage_recoveries'] += 1
            self.ipc_outage_since = 0.0
            self.ipc_outage_reason = ''
            self.ipc_outage_escalated = False
            self.last_rx_seq = None
            self.last_error = ''
            self.slot_sync_required = True
            self.snapshot_cmd_id = 0
            self.snapshot_sent_at = 0.0
            self.snapshot_attempts = 0
            self.heartbeat_cmd_id = 0
            self.status_reconciled = False
            self._status_notifications.clear()
            self._operation_notifications.clear()
            self._identity_notifications.clear()
            self._decoded_packets.clear()
            self.reactor.update_timer(
                self._packet_drain_timer, self.reactor.NEVER)
            self._last_manager_status_fingerprint = None
            self._last_manager_transition_fingerprint = None
            self._last_progress_notification_at = 0.0
            self.stats['connects'] += 1
            self.stats['reconnects'] = max(0, self.stats['connects'] - 1)
            self.reconnect_delay = self.manager.reconnect_interval
            self.next_connect_at = 0.0
            if self._reactor_quiesced or self._transport_paused:
                self._send_sidecar_control(transport.CTRL_PAUSE)
                self._set_fd_wake(False, readable=False)
            else:
                self._send_sidecar_control(transport.CTRL_RESUME)
                if self._connection_notifications:
                    self._schedule_manager_work(force=True)
            logging.info('BMCU %s attached to sidecar %s',
                         self.name, self.socket_path)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self.serial = None
            self.ipc_connected = False
            if self.next_connect_at <= now:
                self.next_connect_at = now + self.reconnect_delay
                self.reconnect_delay = min(
                    getattr(self.manager, 'reconnect_max', 30.0),
                    self.reconnect_delay * 2.0)
            return False

    def close(self, notify=True, reconnect_backoff=True,
              physical_disconnect=False, reason=''):
        was_ipc_open = bool(self.ipc_connected or self.serial is not None)
        was_physical_open = bool(
            self.connected or self.transport_online or
            self.last_known_physical_online)
        was_ready = bool(self.ready)
        was_reconciled = bool(self.status_reconciled)
        if self.fd_handle is not None:
            try:
                self.reactor.unregister_fd(self.fd_handle)
            except Exception:
                pass
        self.fd_handle = None
        self._write_callback_registered = False
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
        self.serial = None
        self.ipc_connected = False
        self.connected = False
        self.transport_online = False
        self.ready = False
        self.runtime_configured = False
        self.runtime_config_sync_pending = False
        self.handshake_sent = False
        self.hello_validated = False
        self.hello_probe_validated = False
        self.hello_confirm_cmd_id = 0
        self.hello_candidate = None
        self.snapshot_cmd_id = 0
        self.snapshot_sent_at = 0.0
        self.snapshot_attempts = 0
        self.heartbeat_cmd_id = 0
        self.hello_cmd_id = 0
        self.hello_nonce = 0
        self.status_reconciled = False
        self._status_notifications.clear()
        self._operation_notifications.clear()
        self._identity_notifications.clear()
        if not notify:
            self._connection_notifications.clear()
        self._decoded_packets.clear()
        self.decoder = transport.MessageDecoder()
        for timer in (self._packet_drain_timer, self._manager_work_timer):
            try:
                self.reactor.update_timer(timer, self.reactor.NEVER)
            except Exception:
                pass
        self._last_manager_status_fingerprint = None
        self._last_manager_transition_fingerprint = None
        self._last_progress_notification_at = 0.0
        self.tx_buffer[:] = b''
        if not self.suspended:
            now = self.reactor.monotonic()
            if reconnect_backoff:
                self.next_connect_at = now + self.reconnect_delay
                self.reconnect_delay = min(
                    getattr(self.manager, 'reconnect_max', 30.0),
                    self.reconnect_delay * 2.0)
            else:
                self.reconnect_delay = self.manager.reconnect_interval
                self.next_connect_at = now
        error_text = str(reason or (
            'physical BMCU link disconnected' if physical_disconnect else
            'BMCU sidecar IPC disconnected'))
        for pending in self.pending.values():
            pending.set_error(error_text)
        self.pending.clear()
        notify_edge = (was_physical_open if physical_disconnect
                       else was_ipc_open)
        if (notify and notify_edge and not bool(getattr(
                self.manager, '_klippy_disconnecting', False))):
            now = self.reactor.monotonic()
            if physical_disconnect:
                self.last_known_physical_online = False
                self.stats['physical_link_losses'] += 1
                self._queue_connection_notification(
                    False, was_ready=was_ready,
                    was_reconciled=was_reconciled,
                    physical_link=True, reason=error_text)
            else:

                if not self.ipc_outage_since:
                    self.ipc_outage_since = now
                    self.stats['ipc_outages'] += 1
                self.ipc_outage_reason = error_text
                self.ipc_outage_escalated = False

    def shutdown_reactor_work(self):

        self._decoded_packets.clear()
        self._status_notifications.clear()
        self._operation_notifications.clear()
        self._identity_notifications.clear()
        self._connection_notifications.clear()
        for timer in (self._packet_drain_timer, self._manager_work_timer):
            try:
                self.reactor.update_timer(timer, self.reactor.NEVER)
            except Exception:
                pass

    def _ipc_keepalive_expired(self, eventtime):
        if eventtime < self._ipc_resume_grace_until:
            return False
        if not self.last_rx:
            return False
        return (eventtime - self.last_rx >
                self.manager.connection_timeout * 2.0)

    def _arm_ipc_resume_grace(self, now):
        self._ipc_resume_grace_until = max(
            self._ipc_resume_grace_until,
            float(now) + float(getattr(
                self.manager, 'sidecar_ipc_resume_grace', 5.0)))
        self.stats['ipc_resume_grace_entries'] += 1

    def tick(self, eventtime, transport_only=False):

        if self.suspended or self._reactor_quiesced or self._transport_paused:
            return
        if not self.ipc_connected:
            if eventtime >= self.next_connect_at:
                self.connect(eventtime)
            return
        if self.tx_buffer:
            try:
                if self._write_callback_registered:
                    self._set_fd_wake(True)
                else:
                    self._flush_tx()
            except Exception as exc:
                self.last_error = str(exc)
                self.close()
                return
        if not self.connected:

            if self._ipc_keepalive_expired(eventtime):
                self.last_error = 'transport sidecar keepalive timeout'
                self.close(reason=self.last_error)
            return
        if not self.handshake_sent and eventtime >= self.ready_at:
            try:
                self.hello_nonce = struct.unpack('<I', os.urandom(4))[0]
                self.hello_cmd_id = self.send(
                    protocol.MSG_HELLO, struct.pack('<I', self.hello_nonce))
                self.handshake_sent = True
            except Exception as exc:
                self.last_error = str(exc)
                self.close()
                return
        if (self.hello_validated and self.snapshot_cmd_id and
                eventtime - self.snapshot_sent_at >= SNAPSHOT_RETRY_INTERVAL):
            if self.snapshot_attempts >= SNAPSHOT_MAX_ATTEMPTS:
                self.last_error = 'initial physical snapshot timeout'
                self.close()
                return
            try:
                self._request_initial_snapshot()
            except Exception as exc:
                self.last_error = str(exc)
                self.close()
                return
        if self._ipc_keepalive_expired(eventtime):
            self.last_error = 'transport sidecar keepalive timeout'
            self.close(reason=self.last_error)

    def _serial_hangup(self, reason):
        self.stats['serial_hangups'] += 1
        self.last_error = str(reason or 'transport sidecar hangup')
        logging.warning('BMCU %s %s; closing IPC socket',
                        self.name, self.last_error)
        self.close(reason=self.last_error)

    def _rx_limits(self):
        if self._reactor_quiesced:
            return (
                int(getattr(self.manager, 'critical_rx_budget_bytes', 128)),
                int(getattr(self.manager, 'critical_rx_budget_packets', 1)),
                float(getattr(self.manager, 'critical_rx_budget_ms', 0.15)))
        return (
            int(self.manager.rx_budget_bytes),
            int(self.manager.rx_budget_packets),
            float(self.manager.rx_budget_ms))

    def _dispatch_decoded_packets(self, eventtime, deadline, packet_budget):
        handled = 0
        while self._decoded_packets and handled < packet_budget:
            if self.reactor.monotonic() >= deadline:
                break
            packet = self._decoded_packets.popleft()
            handled += 1
            self.stats['rx_packets'] += 1
            if self._handle_packet(*packet):
                self.last_rx = eventtime
                self.heartbeat_cmd_id = 0
        return handled

    def _schedule_packet_drain(self, eventtime=None):
        if self._reactor_quiesced:
            return
        if not self._decoded_packets:
            return
        now = self.reactor.monotonic()
        interval = float(getattr(
            self.manager, 'reactor_yield_interval', 0.001))
        self._arm_timer_earliest(
            self._packet_drain_timer, now + interval)

    def _arm_timer_earliest(self, timer, waketime):

        waketime = float(waketime)
        current = getattr(timer, 'waketime', self.reactor.NEVER)
        try:
            current = float(current)
        except (TypeError, ValueError, OverflowError):
            current = self.reactor.NEVER
        if current <= waketime:
            return False
        self.reactor.update_timer(timer, waketime)
        return True

    def _drain_decoded_packets(self, eventtime):
        if (not self.ipc_connected or self._reactor_quiesced):
            return self.reactor.NEVER
        if not self._decoded_packets:
            return self.reactor.NEVER
        started = self.reactor.monotonic()
        _byte_budget, packet_budget, budget_ms = self._rx_limits()
        deadline = started + (budget_ms / 1000.0)
        try:
            self._dispatch_decoded_packets(
                eventtime, deadline, packet_budget)
        except Exception as exc:
            logging.exception('BMCU %s decoded-packet dispatch failed', self.name)
            self.last_error = str(exc)
            self.close()
            return self.reactor.NEVER
        if self._decoded_packets:
            self.stats['rx_budget_yields'] += 1
            interval = float(getattr(
                self.manager, 'reactor_yield_interval', 0.001))
            return self.reactor.monotonic() + interval
        return self.reactor.NEVER

    def _send_sidecar_control(self, command):
        try:
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                control.setblocking(False)
                control.sendto(bytes(command), self.control_path)
            finally:
                control.close()
            return True
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.EAGAIN,
                                 errno.EWOULDBLOCK, errno.ECONNREFUSED):
                logging.warning('BMCU %s sidecar control failed: %s',
                                self.name, exc)
            return False

    def _reset_link_handshake(self, now):
        self.ready = False
        self.runtime_configured = False
        self.runtime_config_sync_pending = False
        self.ready_at = now
        self.handshake_sent = False
        self.hello_validated = False
        self.hello_probe_validated = False
        self.hello_confirm_cmd_id = 0
        self.hello_candidate = None
        self.hello_cmd_id = 0
        self.snapshot_cmd_id = 0
        self.snapshot_sent_at = 0.0
        self.snapshot_attempts = 0
        self.heartbeat_cmd_id = 0
        self.last_rx_seq = None
        self.status_reconciled = False
        self.slot_sync_required = True

    def _handle_ipc_message(self, eventtime, message):
        op, arg1, arg2, arg3, payload = message
        self.last_rx = eventtime
        self._ipc_resume_grace_until = 0.0
        if op == transport.OP_PACKET:
            if len(self._decoded_packets) >= self._decoded_packet_limit:
                raise RuntimeError('BMCU IPC packet queue overflow')
            self._decoded_packets.append((
                int(arg1) & 0xff, int(arg2) & 0xffffffff,
                int(arg3) & 0xffffffff, payload))
            return
        if op == transport.OP_KEEPALIVE:
            return
        if op == transport.OP_ERROR:
            pending = self.pending.get(int(arg1) & 0xffffffff)
            message_text = payload.decode('utf-8', 'replace') or \
                'BMCU sidecar rejected command'
            if pending is not None:
                pending.set_error(message_text)
            else:
                self.last_error = message_text
            return
        if op != transport.OP_LINK:
            return
        online = bool(arg1)
        generation = int(arg2) & 0xffffffff
        error_text = payload.decode('utf-8', 'replace')
        was_online = bool(self.connected)
        was_known_physical_online = bool(self.last_known_physical_online)
        was_ready = bool(self.ready)
        was_reconciled = bool(self.status_reconciled)
        generation_changed = bool(
            self.transport_generation and
            generation != self.transport_generation)
        self.transport_generation = generation
        self.transport_online = online
        self.connected = online
        self.last_known_physical_online = online
        now = self.reactor.monotonic()
        if online:
            if not was_online or generation_changed:
                for pending in self.pending.values():
                    pending.set_error('BMCU transport session changed')
                self.pending.clear()
                self._reset_link_handshake(now)
                self.last_error = ''
            return
        self.ready = False
        self.runtime_configured = False
        self.runtime_config_sync_pending = False
        self.handshake_sent = False
        self.hello_validated = False
        self.status_reconciled = False
        if error_text:
            self.last_error = error_text
        for pending in self.pending.values():
            pending.set_error(error_text or 'device disconnected')
        self.pending.clear()
        if ((was_online or was_known_physical_online) and
                not bool(getattr(
                    self.manager, '_klippy_disconnecting', False))):
            self.stats['physical_link_losses'] += 1
            self._queue_connection_notification(
                False, was_ready=was_ready,
                was_reconciled=was_reconciled,
                physical_link=True,
                reason=error_text or 'physical BMCU link disconnected')

    def _fd_event(self, eventtime):
        if not self.serial:
            return
        if self._reactor_quiesced:
            self._set_fd_wake(False, readable=False)
            return
        started = self.reactor.monotonic()
        byte_budget, packet_budget, budget_ms = self._rx_limits()
        deadline = started + (budget_ms / 1000.0)
        try:
            handled = self._dispatch_decoded_packets(
                eventtime, deadline, packet_budget)
            remaining_packets = max(0, packet_budget - handled)
            remaining_bytes = byte_budget
            while (remaining_bytes > 0 and remaining_packets > 0 and
                   self.reactor.monotonic() < deadline):
                try:
                    data = os.read(
                        self.serial.fileno(), min(4096, remaining_bytes))
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    self._serial_hangup(
                        'transport sidecar hangup (%s)' %
                        os.strerror(exc.errno))
                    return
                if not data:
                    self._serial_hangup('transport sidecar hangup (EOF)')
                    return
                self.stats['rx_bytes'] += len(data)
                remaining_bytes -= len(data)
                messages = self.decoder.feed(data)
                for message in messages:
                    self._handle_ipc_message(eventtime, message)
                count = self._dispatch_decoded_packets(
                    eventtime, deadline, remaining_packets)
                remaining_packets -= count
                if len(data) < min(4096, byte_budget):
                    break
            if (remaining_bytes <= 0 or remaining_packets <= 0 or
                    self.reactor.monotonic() >= deadline):
                if self._decoded_packets or remaining_bytes <= 0:
                    self.stats['rx_budget_yields'] += 1
            self._schedule_packet_drain()
        except Exception as exc:
            logging.exception('BMCU %s sidecar receive failed', self.name)
            self.last_error = str(exc)
            self.close()
        finally:
            elapsed_ms = max(
                0.0, (self.reactor.monotonic() - started) * 1000.0)
            self.stats['max_rx_callback_ms'] = max(
                self.stats['max_rx_callback_ms'], elapsed_ms)
            if elapsed_ms >= getattr(self.manager, 'callback_warning_ms', 10.0):
                logging.warning(
                    'BMCU %s IPC callback took %.3f ms (budget %.3f ms)',
                    self.name, elapsed_ms, budget_ms)

    def _set_fd_wake(self, writable, readable=True):
        if self.fd_handle is None:
            return
        setter = getattr(self.reactor, 'set_fd_wake', None)
        if setter is not None:
            setter(
                self.fd_handle, bool(readable),
                bool(writable) and self._write_callback_registered)

    def set_reactor_quiesced(self, enabled):
        enabled = bool(enabled)
        if enabled == self._reactor_quiesced:
            return
        now = self.reactor.monotonic()
        if enabled:

            self._send_sidecar_control(transport.CTRL_PAUSE)
            self._reactor_quiesced = True
            self._reactor_quiesced_at = now
            self.stats['critical_quiesce_entries'] += 1
            self.reactor.update_timer(
                self._packet_drain_timer, self.reactor.NEVER)
            self.reactor.update_timer(
                self._manager_work_timer, self.reactor.NEVER)
            self._set_fd_wake(False, readable=False)
            return
        duration = max(0.0, now - self._reactor_quiesced_at)
        self.stats['critical_quiesce_total_s'] += duration
        self._reactor_quiesced_at = 0.0
        self._reactor_quiesced = False
        self._arm_ipc_resume_grace(now)
        if not self._transport_paused:
            self._send_sidecar_control(transport.CTRL_RESUME)
            if self.ipc_connected and self.fd_handle is not None:
                self._set_fd_wake(bool(self.tx_buffer), readable=True)
                self._schedule_packet_drain()
                self._schedule_manager_work()
        elif self.ipc_connected and self.fd_handle is not None:

            self._set_fd_wake(False, readable=True)
            urgent_disconnect = bool(
                self._connection_notifications and
                self._is_physical_disconnect_notification(
                    self._connection_notifications[0]))
            urgent_status = bool(
                self._status_notifications and
                len(self._status_notifications[0]) > 2 and
                self._status_notifications[0][2])
            if urgent_disconnect or urgent_status:
                self._schedule_manager_work(force=True)

    def set_transport_paused(self, enabled, required=False):
        enabled = bool(enabled)
        required = bool(required)
        if enabled == self._transport_paused:
            if (not enabled and required and not self._reactor_quiesced):
                self._send_sidecar_control(
                    transport.CTRL_RESUME_REQUIRED)
            return
        now = self.reactor.monotonic()
        self._transport_paused = enabled
        if enabled:
            self._transport_paused_at = now
            self.stats['transport_pause_entries'] += 1
            self._send_sidecar_control(transport.CTRL_PAUSE)
            self.reactor.update_timer(
                self._packet_drain_timer, self.reactor.NEVER)
            self.reactor.update_timer(
                self._manager_work_timer, self.reactor.NEVER)
            if self.ipc_connected and self.fd_handle is not None:
                self._set_fd_wake(False, readable=True)
            return
        duration = max(0.0, now - self._transport_paused_at) \
            if self._transport_paused_at else 0.0
        self.stats['transport_pause_total_s'] += duration
        self._transport_paused_at = 0.0
        self._arm_ipc_resume_grace(now)
        if not self._reactor_quiesced:
            self._send_sidecar_control(
                transport.CTRL_RESUME_REQUIRED if required else
                transport.CTRL_RESUME)
            if self.ipc_connected and self.fd_handle is not None:
                self._set_fd_wake(bool(self.tx_buffer), readable=True)
                self._schedule_packet_drain()
                self._schedule_manager_work()

    def _flush_tx(self):

        if not self.serial or not self.tx_buffer:
            self._set_fd_wake(False)
            return
        would_block = False
        try:
            written = os.write(self.serial.fileno(), self.tx_buffer[:4096])
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                written = 0
                would_block = True
            else:
                raise
        if written < 0:
            raise RuntimeError('negative-length BMCU IPC write')
        if written:
            del self.tx_buffer[:written]
        self._set_fd_wake(bool(self.tx_buffer) and not would_block)

    def _fd_write_event(self, eventtime):
        if self._reactor_quiesced or self._transport_paused:
            self._set_fd_wake(False, readable=False)
            return
        try:
            self._flush_tx()
        except Exception as exc:
            logging.exception('BMCU %s sidecar transmit failed', self.name)
            self.last_error = str(exc)
            self.close()

    def send(self, msg_type, payload=b'', cmd_id=None):
        if self._reactor_quiesced:
            waiter = getattr(
                self.manager, '_await_required_transport_release', None)
            recovered = False
            recovery_started = self.reactor.monotonic()
            if callable(waiter):
                recovered = bool(waiter(self))
            recovery_elapsed = max(
                0.0, self.reactor.monotonic() - recovery_started)
            pending = self.pending.get(cmd_id) if cmd_id is not None else None
            if pending is not None and recovery_elapsed:
                pending.deadline += recovery_elapsed
            if self._reactor_quiesced or not recovered:
                raise RuntimeError(
                    'BMCU %s transport could not resume after '
                    'printer-critical motion' % self.name)
        if self._transport_paused:
            raise RuntimeError(
                'BMCU %s control plane is deferred until printer motion ends' %
                self.name)
        if not self.ipc_connected or not self.serial:
            raise RuntimeError('BMCU %s transport sidecar is offline' % self.name)
        if not self.connected:
            raise RuntimeError('BMCU %s is offline' % self.name)
        if cmd_id is None:
            cmd_id = self._next_cmd_id()
        frame = transport.encode_message(
            transport.OP_SEND, int(msg_type), int(cmd_id), 0, payload)
        if len(self.tx_buffer) + len(frame) > self.manager.tx_queue_limit:
            raise RuntimeError('BMCU %s transmit queue overflow' % self.name)
        self.stats['tx_packets'] += 1
        self.stats['tx_bytes'] += len(frame)
        self.tx_buffer.extend(frame)
        self.last_tx = self.reactor.monotonic()
        if self._write_callback_registered:
            self._set_fd_wake(True)
        else:
            self._flush_tx()
        return cmd_id

    def _new_pending(self, cmd_id, expected, deadline):
        completion = None
        factory = getattr(self.reactor, 'completion', None)
        if callable(factory):
            completion = factory()
        return PendingRequest(cmd_id, expected, deadline, completion)

    def _wait_pending(self, pending, timeout_message):
        while pending.response is None and pending.error is None:
            now = self.reactor.monotonic()
            if now >= pending.deadline:
                raise RuntimeError(timeout_message)
            if pending.completion is not None:
                woke = pending.completion.wait(
                    pending.deadline, waketime_result=False)
                if not woke and pending.response is None and pending.error is None:
                    raise RuntimeError(timeout_message)
            else:

                self.reactor.pause(min(pending.deadline, now + 0.1))
        if pending.error:
            raise RuntimeError(pending.error)
        return pending.response

    def _control_plane_request_begin(self):
        enter = getattr(self.manager, '_control_plane_request_enter', None)
        entered = False
        if callable(enter):
            entered = bool(enter())
            if not entered:
                raise RuntimeError(
                    'BMCU control plane is unavailable during Klipper shutdown')
        self.manager._required_transport_users += 1
        try:
            self.set_transport_paused(False, required=True)
        except Exception:
            self._control_plane_request_end(entered)
            raise
        return entered

    def _control_plane_request_end(self, entered):
        self.manager._required_transport_users = max(
            0, self.manager._required_transport_users - 1)
        leave = getattr(self.manager, '_control_plane_request_leave', None)
        if entered and callable(leave):
            leave()

    def request(self, msg_type, payload=b'', expected=None, timeout=2.0):
        timeout = _bounded_float(timeout, 'request timeout', 0.01, 300.0)
        if expected is None:
            expected = (protocol.MSG_ACK,)
        control_plane_entered = self._control_plane_request_begin()
        pending = None
        cmd_id = None
        try:
            cmd_id = self._next_cmd_id()
            pending = self._new_pending(
                cmd_id, expected, self.reactor.monotonic() + timeout)
            self.pending[cmd_id] = pending
            self.send(msg_type, payload, cmd_id)
            return self._wait_pending(
                pending,
                'BMCU request timeout type=0x%02X id=%d' %
                (msg_type, cmd_id))
        finally:
            if pending is not None:
                self.pending.pop(cmd_id, None)
            self._control_plane_request_end(control_plane_entered)

    def wait_for_op(self, op_id, timeout=8.0):
        op_id = _bounded_int(op_id, 'operation id', 1, 0xffffffff)
        timeout = _bounded_float(timeout, 'operation timeout', 0.01, 300.0)
        deadline = self.reactor.monotonic() + timeout
        next_query = self.reactor.monotonic() + 1.0
        while self.reactor.monotonic() < deadline:
            if self.last_op and self.last_op.get('op_id') == op_id:
                return self.last_op
            now = self.reactor.monotonic()
            if now >= next_query:
                try:
                    result = self.query_operation(op_id, timeout=0.75)
                    if result and result.get('op_id') == op_id:
                        return result
                except Exception:
                    pass
                next_query = now + 1.0
            self.reactor.pause(min(deadline, now + 0.250))
        raise RuntimeError('BMCU operation %d timed out' % op_id)

    def _request_initial_snapshot(self):
        self.snapshot_cmd_id = self.send(protocol.MSG_GET_SNAPSHOT)
        self.snapshot_sent_at = self.reactor.monotonic()
        self.snapshot_attempts += 1

    def _complete_pending(self, msg_type, cmd_id, value):
        pending = self.pending.get(cmd_id)
        if pending is not None and msg_type in pending.expected:
            pending.set_response(value)

    @staticmethod
    def _manager_status_fingerprint(status):
        auto_cal = status.get('auto_calibration', {}) or {}
        return (
            int(status.get('session_id', 0) or 0),
            tuple(int(value) for value in status.get('present', [0] * 4)),
            tuple(int(value) for value in status.get('motion', [0] * 4)),
            tuple(int(value) for value in status.get(
                'route_state', [protocol.ROUTE_EMPTY] * 4)),
            int(status.get('encoder_io_mask', 0) or 0),
            int(status.get('connected_mask', 0) or 0),
            int(status.get('calibration_valid_mask', 0) or 0),
            int(status.get('active_op_id', 0) or 0),
            int(status.get('active_op_type', 0) or 0),
            int(status.get('active_op_state', 0) or 0),
            str(status.get('active_op_reason', '') or ''),
            int(status.get('error_flags', 0) or 0),
            bool(status.get('nvm_fault', False)),
            bool(auto_cal.get('active', False)),
            int(auto_cal.get('stage', 0) or 0),
            int(auto_cal.get('progress', 0) or 0),
            int(auto_cal.get('done_mask', 0) or 0),
            int(auto_cal.get('state', 0) or 0),
        )

    @staticmethod
    def _manager_transition_fingerprint(status):
        auto_cal = status.get('auto_calibration', {}) or {}
        return (
            int(status.get('session_id', 0) or 0),
            tuple(int(value) for value in status.get('present', [0] * 4)),
            tuple(int(value) for value in status.get('motion', [0] * 4)),
            tuple(int(value) for value in status.get(
                'route_state', [protocol.ROUTE_EMPTY] * 4)),
            int(status.get('encoder_io_mask', 0) or 0),
            int(status.get('connected_mask', 0) or 0),
            int(status.get('calibration_valid_mask', 0) or 0),
            int(status.get('active_op_id', 0) or 0),
            int(status.get('active_op_type', 0) or 0),
            int(status.get('active_op_state', 0) or 0),
            str(status.get('active_op_reason', '') or ''),
            int(status.get(
                'controller_fault_flags',
                int(status.get('error_flags', 0) or 0) & ~0x0002) or 0),
            bool(status.get('nvm_fault', False)),
            bool(auto_cal.get('active', False)),
            int(auto_cal.get('stage', 0) or 0),
            int(auto_cal.get('done_mask', 0) or 0),
            int(auto_cal.get('state', 0) or 0),
        )

    @staticmethod
    def _is_physical_disconnect_notification(item):
        try:
            online, details = item
        except Exception:
            return False
        return (not bool(online) and
                bool(dict(details or {}).get('physical_link', True)))

    def _schedule_manager_work(self, eventtime=None, delay=None, force=False):
        if (self._reactor_quiesced or
                bool(getattr(self.manager, '_critical_motion_active', False))):
            return
        if self._transport_paused and not force:
            return
        if not (self._connection_notifications or self._identity_notifications or
                self._operation_notifications or self._status_notifications):
            return

        now = self.reactor.monotonic()
        if delay is None:
            delay = float(getattr(
                self.manager, 'manager_work_yield_interval', 0.002))
        self._arm_timer_earliest(
            self._manager_work_timer, now + max(0.001, float(delay)))

    def _queue_connection_notification(self, online, **details):

        item = (bool(online), dict(details))
        if self._connection_notifications and self._connection_notifications[-1] == item:
            return
        if len(self._connection_notifications) >= 8:
            self._connection_notifications.popleft()
            self.missed_events += 1
        self._connection_notifications.append(item)
        self._schedule_manager_work()

    def _queue_status_notification(self, previous, current):
        fingerprint = self._manager_status_fingerprint(current)
        if fingerprint == self._last_manager_status_fingerprint:
            self.stats['status_notifications_suppressed'] += 1
            return
        transition = self._manager_transition_fingerprint(current)
        now = self.reactor.monotonic()
        transition_changed = (
            transition != self._last_manager_transition_fingerprint)
        print_active = False
        try:
            print_active = self.manager._print_state() in (
                'printing', 'paused', 'pause')
        except Exception:
            pass
        urgent = bool(transition_changed and print_active)
        self._last_manager_status_fingerprint = fingerprint
        self._last_manager_transition_fingerprint = transition

        progress_interval = max(
            0.05, float(getattr(
                self.manager, 'status_cache_interval', 0.25)))
        if (not transition_changed and
                now - self._last_progress_notification_at < progress_interval):
            if (self._status_notifications and
                    self._manager_transition_fingerprint(
                        self._status_notifications[-1][1]) == transition):
                queued_previous = self._status_notifications[-1][0]
                queued_urgent = bool(self._status_notifications[-1][2])
                self._status_notifications[-1] = (
                    queued_previous, current, queued_urgent or urgent)
                self.stats['status_notifications_coalesced'] += 1
            else:
                self.stats['status_notifications_suppressed'] += 1
            return
        self._last_progress_notification_at = now

        if (not transition_changed and self._status_notifications and
                self._manager_transition_fingerprint(
                    self._status_notifications[-1][1]) == transition):
            queued_previous = self._status_notifications[-1][0]
            queued_urgent = bool(self._status_notifications[-1][2])
            self._status_notifications[-1] = (
                queued_previous, current, queued_urgent or urgent)
            self.stats['status_notifications_coalesced'] += 1
        elif len(self._status_notifications) >= 16:
            self.stats['notification_queue_overflows'] += 1
            self.last_error = 'status notification queue overflow'
            logging.error('BMCU %s %s; closing sidecar IPC',
                          self.name, self.last_error)
            self.close()
            return
        else:
            self._status_notifications.append((previous, current, urgent))
        self._schedule_manager_work(force=urgent)

    def _schedule_device_identified(self, old_uid):
        if self._identity_notifications:
            self._identity_notifications[-1] = old_uid
        else:
            self._identity_notifications.append(old_uid)
        self._schedule_manager_work()

    def _queue_operation_notification(self, result):
        op_id = int(result.get('op_id', 0) or 0)
        if (self._operation_notifications and op_id and
                int(self._operation_notifications[-1].get('op_id', 0) or 0) ==
                op_id):
            self._operation_notifications[-1] = dict(result)
            self.stats['operation_notifications_coalesced'] += 1
        elif len(self._operation_notifications) >= 16:
            self.stats['notification_queue_overflows'] += 1
            self.last_error = 'operation notification queue overflow'
            logging.error('BMCU %s %s; closing sidecar IPC',
                          self.name, self.last_error)
            self.close()
            return
        else:
            self._operation_notifications.append(dict(result))
        self._schedule_manager_work()

    def _record_manager_callback_time(self, started):
        elapsed_ms = max(
            0.0, (self.reactor.monotonic() - started) * 1000.0)
        if elapsed_ms > self.stats['max_manager_callback_ms']:
            self.stats['max_manager_callback_ms'] = elapsed_ms
        if elapsed_ms >= getattr(self.manager, 'callback_warning_ms', 10.0):
            logging.warning(
                'BMCU %s manager callback took %.3f ms',
                self.name, elapsed_ms)

    def _drain_manager_work(self, eventtime):
        if self._reactor_quiesced:
            return self.reactor.NEVER
        urgent_disconnect = bool(
            self._connection_notifications and
            self._is_physical_disconnect_notification(
                self._connection_notifications[0]))
        urgent_status = bool(
            self._status_notifications and
            len(self._status_notifications[0]) > 2 and
            self._status_notifications[0][2])
        if self._transport_paused and not (urgent_disconnect or urgent_status):
            return self.reactor.NEVER
        if bool(getattr(self.manager, '_klippy_disconnecting', False)):
            self._connection_notifications.clear()
            self._identity_notifications.clear()
            self._operation_notifications.clear()
            self._status_notifications.clear()
            return self.reactor.NEVER
        busy_check = getattr(self.manager, '_printer_background_busy', None)
        if callable(busy_check) and busy_check(eventtime):

            if not (urgent_disconnect or urgent_status):
                self.stats['manager_work_busy_deferrals'] += 1
                return self.reactor.monotonic() + float(getattr(
                    self.manager, '_background_retry_interval', 0.250))
        started = self.reactor.monotonic()
        try:

            if self._connection_notifications:
                online, details = self._connection_notifications.popleft()
                callback = getattr(
                    self.manager, 'device_connection_changed', None)
                if callable(callback):
                    callback(self, online, **details)
            elif self._identity_notifications:
                old_uid = self._identity_notifications.popleft()
                if self.connected and self.hello_validated:
                    self.manager.device_identified(self, old_uid=old_uid)
            elif self._operation_notifications:
                result = self._operation_notifications.popleft()
                self.manager.operation_finished(self, result)
            elif self._status_notifications:
                previous, current, urgent = self._status_notifications.popleft()
                urgent_callback = getattr(
                    self.manager, 'device_urgent_status_changed', None)
                if urgent and callable(urgent_callback):
                    urgent_callback(self, previous, current)
                else:
                    queue_callback = getattr(
                        self.manager, 'queue_device_status_changed', None)
                    if callable(queue_callback):
                        queue_callback(self, previous, current)
                    else:
                        self.manager.device_status_changed(self, previous, current)
                if str(self.last_error or '').startswith(
                        'status reconciliation failed:'):
                    self.last_error = ''
            else:
                return self.reactor.NEVER
        except Exception as exc:
            self.stats['manager_callback_errors'] += 1
            self.last_error = 'manager reconciliation failed: %s' % exc
            logging.exception(
                'BMCU %s deferred manager reconciliation failed', self.name)
            callback = getattr(
                self.manager, 'device_status_callback_failed', None)
            if callable(callback):
                try:
                    callback(self, exc, self.status, self.status)
                except Exception:
                    logging.exception(
                        'BMCU %s callback-failure handler failed', self.name)
        finally:
            self._record_manager_callback_time(started)
        if (self._connection_notifications or self._identity_notifications or
                self._operation_notifications or self._status_notifications):
            self.stats['manager_work_yields'] += 1
            return self.reactor.monotonic() + float(getattr(
                self.manager, 'manager_work_yield_interval', 0.002))
        return self.reactor.NEVER

    def _response_expected(self, msg_type, cmd_id):
        pending = self.pending.get(cmd_id)
        if pending is not None and msg_type in pending.expected:
            return True
        self.stats['stale_packets'] += 1
        return False

    def _validate_hello_candidate(self, hello):
        old_uid = self.uid
        uid = hello['uid']
        if self.expected_uid and uid != self.expected_uid:
            raise RuntimeError('UID mismatch expected=%s got=%s' %
                               (self.expected_uid, uid))
        firmware_tuple = tuple(hello.get('firmware_tuple', (0, 0, 0)))
        protocol_ok = hello.get('protocol') == protocol.PROTO_VERSION
        firmware_ok = protocol.firmware_is_compatible(firmware_tuple)
        if not protocol_ok or not firmware_ok:
            reported = hello.get('firmware') or 'unknown'
            minimum = '.'.join(str(part) for part in protocol.MIN_COMPATIBLE_FIRMWARE)
            message = (
                'BMCU firmware %s is outside the supported %s..%s range; '
                'flash bundled firmware %s' %
                (reported, minimum, protocol.REQUIRED_FIRMWARE_TEXT,
                 protocol.REQUIRED_FIRMWARE_TEXT))
            self.firmware_compatible = False
            self.firmware_error = message
            logging.warning(
                'BMCU %s incompatible handshake firmware=%s protocol=%s',
                self.name, reported, hello.get('protocol'))
            raise RuntimeError(message)
        if int(hello.get('channels', 0)) != 4:
            raise RuntimeError('unsupported BMCU channel count %s; expected 4' %
                               hello.get('channels'))
        self.firmware_compatible = True
        self.firmware_error = ''
        return uid, old_uid

    def _finalize_hello(self, seq):
        hello = self.hello_candidate
        if not hello:
            raise RuntimeError('missing BMCU HELLO candidate')
        uid, old_uid = self._validate_hello_candidate(hello)
        old_session = self.status.get('session_id', 0)
        self.uid = uid
        if old_session and old_session != hello['session_id']:
            self.session_changed = True
            self.slot_sync_required = True
            self.runtime_configured = False
        self.last_rx_seq = int(seq) & 0xffffffff
        self.status['session_id'] = hello['session_id']
        self.caps.update(hello)

        self.hello_validated = True
        self._schedule_device_identified(old_uid)
        self.hello_probe_validated = False
        self.hello_confirm_cmd_id = 0
        self.hello_candidate = None
        self.reconnect_delay = self.manager.reconnect_interval
        self.next_connect_at = 0.0
        self._request_initial_snapshot()

    def _handle_packet(self, msg_type, seq, cmd_id, payload):
        try:
            confirm_ack = bool(
                msg_type == protocol.MSG_ACK and self.hello_confirm_cmd_id and
                cmd_id == self.hello_confirm_cmd_id)
            if (msg_type != protocol.MSG_HELLO_ACK and not self.hello_validated and
                    not confirm_ack):
                return False
            if (self.hello_validated and
                    msg_type not in (protocol.MSG_HELLO_ACK, protocol.MSG_STATUS) and
                    not self._accept_rx_seq(seq)):
                return False
            if msg_type == protocol.MSG_HELLO_ACK:
                if not self.hello_cmd_id or cmd_id != self.hello_cmd_id:
                    return False
                hello = protocol.parse_hello(payload)
                self._validate_hello_candidate(hello)
                self.hello_candidate = hello
                self.hello_probe_validated = True
                self.hello_cmd_id = 0
                self.hello_confirm_cmd_id = self.send(
                    protocol.MSG_SESSION_CONFIRM,
                    struct.pack('<I', int(hello['session_id']) & 0xffffffff))
                return True
            elif confirm_ack:
                if len(payload) != 7:
                    raise ValueError('invalid session confirmation ACK length')
                echo, ok, error = struct.unpack('<IBH', payload)
                if echo != cmd_id:
                    raise ValueError('session confirmation ACK command id mismatch')
                if not ok:
                    raise RuntimeError('BMCU session confirmation rejected error=%d' % error)
                self._finalize_hello(seq)
                return True
            elif msg_type == protocol.MSG_CAPS:
                if not self._response_expected(msg_type, cmd_id):
                    return False
                caps = protocol.parse_caps(payload)
                self.caps.update(caps)
                self._complete_pending(msg_type, cmd_id, caps)
                return True
            elif msg_type == protocol.MSG_SNAPSHOT:
                if not self.hello_validated or cmd_id != self.snapshot_cmd_id:
                    return False
                snapshot = protocol.parse_snapshot(payload)
                expected_session = int(self.caps.get(
                    'session_id', self.status.get('session_id', 0)) or 0)
                snapshot_session = int(snapshot['status'].get('session_id', 0) or 0)
                if expected_session and snapshot_session != expected_session:
                    self.last_error = (
                        'BMCU session changed during initial snapshot '
                        'expected=%d got=%d' %
                        (expected_session, snapshot_session))
                    self.close()
                    return False
                previous = self.status
                self.status = self._attach_travel_diagnostics(
                    snapshot['status'])
                self.status_revision += 1
                self.calibration = list(snapshot['calibration'])
                self.snapshot_cmd_id = 0
                self.snapshot_sent_at = 0.0
                self.snapshot_attempts = 0

                self._complete_pending(msg_type, cmd_id, snapshot)
                self._queue_status_notification(previous, self.status)
                return True
            elif msg_type == protocol.MSG_STATUS:
                if not self.hello_validated:
                    return False
                previous = self.status
                status = protocol.parse_status(payload)
                expected_session = int(self.caps.get(
                    'session_id', previous.get('session_id', 0)) or 0)
                current_session = int(status.get('session_id', 0) or 0)
                if expected_session and current_session != expected_session:
                    self.last_error = (
                        'BMCU session changed without a new HELLO '
                        'expected=%d got=%d' %
                        (expected_session, current_session))
                    self.close()
                    return False
                if not self._accept_rx_seq(seq):
                    return False
                self.status = self._attach_travel_diagnostics(status)
                self.status_revision += 1
                previous_session = int(previous.get('session_id', 0) or 0)
                if previous_session and previous_session == current_session:
                    old_counter = int(previous.get('event_counter', 0))
                    new_counter = int(self.status.get('event_counter', 0))
                    delta = (new_counter - old_counter) & 0xffffffff
                    if 1 < delta < 0x80000000:
                        self.missed_events += delta - 1

                self._complete_pending(msg_type, cmd_id, self.status)
                self._queue_status_notification(previous, self.status)
                return True
            elif msg_type == protocol.MSG_CALIBRATION:
                if not self._response_expected(msg_type, cmd_id):
                    return False
                calibration = protocol.parse_calibration(payload)
                self.calibration[calibration['channel']] = calibration
                self._complete_pending(msg_type, cmd_id, calibration)
                return True
            elif msg_type == protocol.MSG_OP_RESULT:
                result = protocol.parse_op_result(payload)
                previous = self.last_op
                self.last_op = result
                if not previous or previous.get('op_id') != result.get('op_id') or previous.get('state') != result.get('state'):
                    self._queue_operation_notification(result)
                self._complete_pending(msg_type, cmd_id, result)
                return True
            elif msg_type == protocol.MSG_NVM_DATA:
                if not self._response_expected(msg_type, cmd_id):
                    return False
                data = protocol.parse_nvm_data(payload)
                self._complete_pending(msg_type, cmd_id, data)
                return True
            elif msg_type == protocol.MSG_SLOT_INFO:
                if not self._response_expected(msg_type, cmd_id):
                    return False
                slot = protocol.parse_slot(payload)
                self.slots[slot['channel']] = slot
                self._complete_pending(msg_type, cmd_id, slot)
                return True
            elif msg_type == protocol.MSG_CONFIG_VAL:
                if not self._response_expected(msg_type, cmd_id):
                    return False
                config_value = protocol.parse_config_value(payload)
                self.motion_config[config_value['key']] = config_value['value']
                self._complete_pending(msg_type, cmd_id, config_value)
                return True
            elif msg_type == protocol.MSG_ACK:
                if not self._response_expected(msg_type, cmd_id):
                    return False
                if len(payload) != 7:
                    raise ValueError('invalid ACK length')
                echo, ok, error = struct.unpack('<IBH', payload)
                if echo != cmd_id:
                    raise ValueError('ACK command id mismatch')
                value = {'ok': bool(ok), 'error': error}
                if not ok:
                    pending = self.pending.get(cmd_id)
                    if pending:
                        pending.set_error('BMCU command rejected error=%d' % error)
                else:
                    self._complete_pending(msg_type, cmd_id, value)
                return True
            elif msg_type == protocol.MSG_ERROR:
                pending = self.pending.get(cmd_id)
                if pending is None:
                    self.stats['stale_packets'] += 1
                    return False
                if len(payload) != 7:
                    raise ValueError('invalid ERROR length')
                channel, error, details = struct.unpack('<BHI', payload)
                if (channel == 0xff and error == 22 and
                        protocol.MSG_OP_RESULT in pending.expected):
                    pending.set_error('BMCU operation result is no longer available')
                    return True
                message = 'BMCU error channel=%d code=%d details=%d' % (
                    channel, error, details)
                self.last_error = message
                pending.set_error(message)
                return True
            elif msg_type in (protocol.MSG_STATE_CHANGED, protocol.MSG_MOTION_DONE):
                self.stats['stale_packets'] += 1
                return False
            elif msg_type == protocol.MSG_PONG:
                heartbeat = bool(self.heartbeat_cmd_id and
                                 cmd_id == self.heartbeat_cmd_id)
                pending = self.pending.get(cmd_id)
                requested = pending is not None and msg_type in pending.expected
                if not heartbeat and not requested:
                    self.stats['stale_packets'] += 1
                    return False
                if len(payload) > 128:
                    raise ValueError('invalid PONG length')
                if heartbeat:
                    self.heartbeat_cmd_id = 0
                self._complete_pending(msg_type, cmd_id, payload)
                return True
            self.stats['unknown_packets'] += 1
            return False
        except Exception as exc:
            logging.warning('BMCU %s rejected packet type=0x%02X: %s',
                            self.name, msg_type, exc)
            self.stats['packet_errors'] += 1
            self.last_error = str(exc)
            if msg_type in (protocol.MSG_HELLO_ACK, protocol.MSG_ACK) and not self.hello_validated:
                self.close()
            return False

    def performance_status(self):
        values = dict(self.stats)
        values.update({
            'connected': bool(self.connected),
            'ipc_connected': bool(self.ipc_connected),
            'transport_online': bool(self.transport_online),
            'ready': bool(self.ready),
            'reactor_quiesced': bool(self._reactor_quiesced),
            'transport_paused': bool(self._transport_paused),
            'pending_requests': len(self.pending),
            'tx_queue_bytes': len(self.tx_buffer),
            'missed_events': int(self.missed_events),
            'last_rx_age_s': max(0.0, self.reactor.monotonic() - self.last_rx) if self.last_rx else 0.0,
            'last_tx_age_s': max(0.0, self.reactor.monotonic() - self.last_tx) if self.last_tx else 0.0,
            'ipc_outage_age_s': (max(0.0, self.reactor.monotonic() -
                                     self.ipc_outage_since)
                                 if self.ipc_outage_since else 0.0),
            'ipc_outage_reason': self.ipc_outage_reason,
            'ipc_outage_escalated': bool(self.ipc_outage_escalated),
            'last_known_physical_online': bool(
                self.last_known_physical_online),
            'ipc_resume_grace_remaining_s': max(
                0.0, self._ipc_resume_grace_until - self.reactor.monotonic()),
        })
        return values

    def refresh(self):
        return self.request(protocol.MSG_GET_STATUS, expected=(protocol.MSG_STATUS,), timeout=1.0)

    def set_motion(self, channel, motion):
        channel = _bounded_int(channel, 'channel', 0, 3)
        motion = _bounded_int(motion, 'motion', protocol.MOTION_IDLE,
                              protocol.MOTION_STOP_ON_USE)
        return self.request(protocol.MSG_SET_MOTION, bytes([channel, motion]), timeout=1.0)

    def mark_unloaded(self, channel):

        channel = _bounded_int(channel, 'channel', 0, 3)
        return self.request(protocol.MSG_MARK_UNLOADED, bytes([channel]), timeout=1.0)

    def mark_loaded(self, channel):

        channel = _bounded_int(channel, 'channel', 0, 3)
        return self.request(protocol.MSG_MARK_LOADED, bytes([channel]), timeout=1.0)

    def stop_all(self):
        return self.request(protocol.MSG_STOP_ALL, timeout=1.0)

    def reset_error(self):
        return self.request(protocol.MSG_RESET_ERROR, timeout=1.0)

    def update_prepare(self):
        return self.request(protocol.MSG_UPDATE_PREPARE, timeout=15.0)

    def update_cancel(self):
        return self.request(protocol.MSG_UPDATE_CANCEL, timeout=15.0)

    def suspend_for_update(self, reason='firmware_update'):
        self.suspended = True
        self.suspend_reason = str(reason or 'firmware_update')
        self.next_connect_at = float('inf')
        try:
            status = transport.control_request(
                self.control_path, transport.CTRL_RELEASE_SERIAL,
                pause=self.reactor.pause)
            if status.get('serial_open') or not status.get('serial_released'):
                raise RuntimeError('BMCU sidecar did not release the serial port')
        finally:
            self.close(notify=False)

    def resume_after_update(self):
        status = transport.control_request(
            self.control_path, transport.CTRL_RECONNECT_SERIAL,
            pause=self.reactor.pause)
        if status.get('serial_released'):
            raise RuntimeError('BMCU sidecar remains suspended')
        self.suspended = False
        self.suspend_reason = ''
        self.last_error = ''
        self.reconnect_delay = self.manager.reconnect_interval
        self.next_connect_at = self.reactor.monotonic() + max(
            1.0, self.manager.reconnect_interval)

    def nvm_read(self, offset, length, timeout=15.0):
        offset = _bounded_int(offset, 'NVM offset', 0, 4095)
        length = _bounded_int(length, 'NVM length', 1, 224)
        timeout = _bounded_float(timeout, 'NVM read timeout', 0.05, 30.0)
        if offset + length > 4096:
            raise ValueError('NVM read exceeds 4 KiB region')
        payload = struct.pack('<HH', offset, length)
        return self.request(protocol.MSG_NVM_READ, payload,
                            expected=(protocol.MSG_NVM_DATA,), timeout=timeout)

    def nvm_read_batch(self, chunks, timeout=15.0):
        timeout = _bounded_float(timeout, 'NVM batch timeout', 0.05, 30.0)
        chunks = list(chunks or ())
        if not chunks or len(chunks) > 4:
            raise ValueError('NVM batch must contain between 1 and 4 chunks')
        validated = []
        for offset, length in chunks:
            offset = _bounded_int(offset, 'NVM offset', 0, 4095)
            length = _bounded_int(length, 'NVM length', 1, 224)
            if offset + length > 4096:
                raise ValueError('NVM read exceeds 4 KiB region')
            validated.append((offset, length))
        entered = self._control_plane_request_begin()
        pending_items = []
        try:
            deadline = self.reactor.monotonic() + timeout
            for offset, length in validated:
                cmd_id = self._next_cmd_id()
                pending = self._new_pending(
                    cmd_id, (protocol.MSG_NVM_DATA,), deadline)
                self.pending[cmd_id] = pending
                pending_items.append((offset, length, cmd_id, pending))
                self.send(
                    protocol.MSG_NVM_READ,
                    struct.pack('<HH', offset, length), cmd_id)
            results = []
            for offset, length, cmd_id, pending in pending_items:
                results.append(self._wait_pending(
                    pending,
                    'BMCU NVM batch timeout offset=%d length=%d id=%d' %
                    (offset, length, cmd_id)))
            return results
        finally:
            for _offset, _length, cmd_id, _pending in pending_items:
                self.pending.pop(cmd_id, None)
            self._control_plane_request_end(entered)

    def calibration_point(self, channel, point):
        channel = _bounded_int(channel, 'channel', 0, 3)
        point = _bounded_int(point, 'calibration point', protocol.CAL_MIN, protocol.CAL_MAX)
        return self.request(protocol.MSG_CAL_CAPTURE, bytes([channel, point]),
                            expected=(protocol.MSG_CALIBRATION,), timeout=2.0)

    def calibration_commit(self, channel):
        channel = _bounded_int(channel, 'channel', 0, 3)
        return self.request(protocol.MSG_CAL_COMMIT, bytes([channel]),
                            expected=(protocol.MSG_CALIBRATION,), timeout=2.0)

    def calibration_get(self, channel):
        channel = _bounded_int(channel, 'channel', 0, 3)
        return self.request(protocol.MSG_GET_CALIBRATION, bytes([channel]),
                            expected=(protocol.MSG_CALIBRATION,), timeout=1.0)

    def query_operation(self, op_id=0, timeout=1.0):
        op_id = _bounded_int(op_id, 'operation id', 0, 0xffffffff)
        return self.request(protocol.MSG_GET_OP_RESULT, struct.pack('<I', op_id),
                            expected=(protocol.MSG_OP_RESULT,), timeout=timeout)

    def start_distance_operation(self, msg_type, channel, millimeters):
        if msg_type not in (protocol.MSG_TEST_ENCODER, protocol.MSG_CHANNEL_AUTOLOAD):
            raise ValueError('unsupported distance operation type')
        channel = _bounded_int(channel, 'channel', 0, 3)
        millimeters = _bounded_float(millimeters, 'distance', 5.0, 5000.0)
        payload = bytes([channel]) + struct.pack('<f', millimeters)
        control_plane_entered = self._control_plane_request_begin()
        pending = None
        op_id = None
        try:
            op_id = self._next_cmd_id()
            pending = self._new_pending(
                op_id, (protocol.MSG_ACK, protocol.MSG_OP_RESULT),
                self.reactor.monotonic() + 2.0)
            self.pending[op_id] = pending
            self.send(msg_type, payload, op_id)
            self._wait_pending(pending, 'operation start timeout')
            return op_id
        finally:
            if pending is not None:
                self.pending.pop(op_id, None)
            self._control_plane_request_end(control_plane_entered)

    def start_channel_retract(self, channel):
        channel = _bounded_int(channel, 'channel', 0, 3)
        control_plane_entered = self._control_plane_request_begin()
        pending = None
        op_id = None
        try:
            op_id = self._next_cmd_id()
            pending = self._new_pending(
                op_id, (protocol.MSG_ACK, protocol.MSG_OP_RESULT),
                self.reactor.monotonic() + 2.0)
            self.pending[op_id] = pending
            self.send(protocol.MSG_CHANNEL_RETRACT, bytes([channel]), op_id)
            self._wait_pending(pending, 'channel retract start timeout')
            return op_id
        finally:
            if pending is not None:
                self.pending.pop(op_id, None)
            self._control_plane_request_end(control_plane_entered)

    def start_auto_calibration(self, selected_mask=0x0f):
        selected_mask = _bounded_int(selected_mask, 'calibration channel mask', 1, 0x0f)
        control_plane_entered = self._control_plane_request_begin()
        pending = None
        op_id = None
        try:
            op_id = self._next_cmd_id()
            pending = self._new_pending(
                op_id, (protocol.MSG_ACK, protocol.MSG_OP_RESULT),
                self.reactor.monotonic() + 2.0)
            self.pending[op_id] = pending
            self.send(protocol.MSG_CAL_AUTO_START, bytes([selected_mask]), op_id)
            self._wait_pending(
                pending, 'automatic calibration start timeout')
            return op_id
        finally:
            if pending is not None:
                self.pending.pop(op_id, None)
            self._control_plane_request_end(control_plane_entered)

    def abort_operation(self):
        return self.request(protocol.MSG_ABORT_OP, timeout=1.0)

    def start_feed_operation(self, msg_type, channel, millimeters,
                             contact_pct=90, timeout_ms=45000):
        if msg_type not in (protocol.MSG_FEED_TO_CONTACT, protocol.MSG_FEED_DISTANCE):
            raise ValueError('unsupported feed operation type')
        channel = _bounded_int(channel, 'channel', 0, 3)
        millimeters = _bounded_float(millimeters, 'distance', 5.0, 5000.0)
        contact_pct = _bounded_int(contact_pct, 'contact percentage', 55, 98)
        timeout_ms = _bounded_int(timeout_ms, 'operation timeout ms', 250, 300000)
        payload = struct.pack('<BfBI', channel, millimeters, contact_pct, timeout_ms)
        control_plane_entered = self._control_plane_request_begin()
        pending = None
        op_id = None
        try:
            op_id = self._next_cmd_id()
            pending = self._new_pending(
                op_id, (protocol.MSG_ACK, protocol.MSG_OP_RESULT),
                self.reactor.monotonic() + 2.0)
            self.pending[op_id] = pending
            self.send(msg_type, payload, op_id)
            self._wait_pending(pending, 'feed operation start timeout')
            return op_id
        finally:
            if pending is not None:
                self.pending.pop(op_id, None)
            self._control_plane_request_end(control_plane_entered)

    def start_feed_to_contact(self, channel, maximum_mm, contact_pct, timeout_ms):
        return self.start_feed_operation(protocol.MSG_FEED_TO_CONTACT, channel,
                                         maximum_mm, contact_pct, timeout_ms)

    def start_feed_distance(self, channel, millimeters, safety_buffer_pct, timeout_ms):
        return self.start_feed_operation(protocol.MSG_FEED_DISTANCE, channel,
                                         millimeters, safety_buffer_pct, timeout_ms)

    def runtime_sync(self, values, rgb, policy=None, channel_retract_m=None,
                     channel_autoload_m=None):
        payload = protocol.pack_runtime_sync(
            values, rgb, policy, channel_retract_m, channel_autoload_m,
            include_channel_autoload=True)
        return self.request(protocol.MSG_RUNTIME_SYNC, payload, timeout=15.0)

    def set_slots(self, slots):
        payload = protocol.pack_slots(slots)
        return self.request(protocol.MSG_SET_SLOTS, payload, timeout=15.0)

    def config_get(self, key):
        key = _bounded_int(key, 'configuration key', 0, 0xffff)
        result = self.request(protocol.MSG_CONFIG_GET, struct.pack('<H', key),
                              expected=(protocol.MSG_CONFIG_VAL,), timeout=1.0)
        self.motion_config[int(key)] = float(result['value'])
        return float(result['value'])

    def config_set(self, key, value):
        key = _bounded_int(key, 'configuration key', 0, 0xffff)
        value = _bounded_float(value, 'configuration value', -1.0e9, 1.0e9)
        payload = struct.pack('<Hf', key, value)
        self.request(protocol.MSG_CONFIG_SET, payload, timeout=1.0)
        self.motion_config[key] = value
        return value

    def set_system_led(self, red, green, blue):
        red = _bounded_int(red, 'red', 0, 255)
        green = _bounded_int(green, 'green', 0, 255)
        blue = _bounded_int(blue, 'blue', 0, 255)
        payload = bytes([red, green, blue])
        return self.request(protocol.MSG_SET_SYSTEM_LED, payload, timeout=15.0)

    def set_lighting(self, config):
        payload = protocol.pack_lighting(config)
        return self.request(protocol.MSG_SET_LIGHTING, payload, timeout=15.0)

    def preview_led(self, target, red, green, blue):
        values = [
            _bounded_int(target, 'preview target', 0, 3),
            _bounded_int(red, 'red', 0, 255),
            _bounded_int(green, 'green', 0, 255),
            _bounded_int(blue, 'blue', 0, 255),
        ]
        return self.request(protocol.MSG_LED_PREVIEW, bytes(values), timeout=1.0)

    def set_slot(self, channel, color, name, tmin, tmax, material):
        payload = protocol.pack_slot(channel, color, name, tmin, tmax, material)
        return self.request(protocol.MSG_SET_SLOT_INFO, payload,
                            expected=(protocol.MSG_SLOT_INFO,), timeout=15.0)
