#!/usr/bin/env python3

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from bmcu_vendor import ensure_vendor_path
ensure_vendor_path(__file__)

import argparse
import math
import os
import struct
import sys
import time

try:
    import serial
except ImportError as exc:
    raise SystemExit('pyserial is unavailable; reinstall the BMCU package because its private fallback is missing or damaged') from exc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from klippy.extras.bmcu_core import protocol

MAX_QUEUED_PACKETS = 128

def _validated_timeout(value, label='timeout'):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('%s must be finite and within 0.05..300 seconds' % label) from exc
    if not math.isfinite(result) or not 0.05 <= result <= 300.0:
        raise ValueError('%s must be finite and within 0.05..300 seconds' % label)
    return result

class Link:
    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.serial = None
        self.decoder = protocol.StreamDecoder()
        self.seq = 1
        self.cmd = 1
        self.queued = []
        self.last_rx_seq = None

    def open(self):
        self.decoder = protocol.StreamDecoder()
        self.seq = 1
        self.cmd = 1
        self.queued = []
        self.last_rx_seq = None
        connection = serial.Serial()
        connection.port = self.port
        connection.baudrate = self.baud
        connection.timeout = 0.03
        connection.write_timeout = 0.5
        connection.rtscts = False
        connection.dsrdtr = False
        connection.dtr = True
        connection.rts = False
        connection.open()
        try:
            connection.setDTR(True)
            connection.setRTS(False)
        except Exception:
            pass
        self.serial = connection
        time.sleep(1.5)
        connection.reset_input_buffer()

    def close(self):
        if self.serial:
            self.serial.close()
            self.serial = None

    def _next(self):
        value = self.cmd
        self.cmd = (self.cmd + 1) & 0xffffffff or 1
        return value

    def _accept_rx_seq(self, seq):
        seq = int(seq) & 0xffffffff
        if self.last_rx_seq is None:
            self.last_rx_seq = seq
            return True
        delta = (seq - self.last_rx_seq) & 0xffffffff
        if delta == 0 or delta >= 0x80000000:
            return False
        self.last_rx_seq = seq
        return True

    def _write_all(self, frame, timeout=1.0):
        if self.serial is None:
            raise RuntimeError('serial link is closed')
        deadline = time.monotonic() + _validated_timeout(timeout, 'write timeout')
        offset = 0
        while offset < len(frame):
            written = self.serial.write(frame[offset:])
            if written:
                offset += written
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError('serial write timeout')
            time.sleep(0.002)
        self.serial.flush()

    def _enqueue(self, packet):
        if len(self.queued) >= MAX_QUEUED_PACKETS:
            del self.queued[0]
        self.queued.append(packet)

    def _take_command_packet(self, cmd_id):
        for index, packet in enumerate(self.queued):
            if packet[2] == cmd_id:
                return self.queued.pop(index)
        return None

    def send(self, msg_type, payload=b'', cmd_id=None):
        if self.serial is None:
            raise RuntimeError('serial link is closed')
        if cmd_id is None:
            cmd_id = self._next()
        frame = protocol.encode_packet(msg_type, self.seq, cmd_id, payload)
        self.seq = ((self.seq + 1) & 0xffffffff) or 1
        self._write_all(frame)
        return cmd_id

    def read(self, timeout=1.0):
        read_timeout = _validated_timeout(timeout, 'read timeout')
        deadline = time.monotonic() + read_timeout
        while time.monotonic() < deadline:
            data = self.serial.read(4096)
            if not data:
                continue
            packets = [packet for packet in self.decoder.feed(data)
                       if self._accept_rx_seq(packet[1])]
            if packets:
                for packet in packets[1:]:
                    self._enqueue(packet)
                return packets[0]
        return None

    def request(self, msg_type, payload=b'', expected=(), timeout=2.0):
        request_timeout = _validated_timeout(timeout, 'request timeout')
        expected = set(expected or ())
        cmd_id = self.send(msg_type, payload)
        deadline = time.monotonic() + request_timeout
        while time.monotonic() < deadline:
            packet = self._take_command_packet(cmd_id)
            if packet is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                packet = self.read(max(0.05, min(0.25, remaining)))
            if packet is None:
                continue
            typ, seq, response_id, body = packet
            if typ == protocol.MSG_ERROR and response_id == cmd_id:
                if len(body) != 7:
                    raise RuntimeError('invalid device ERROR length')
                channel, code, details = struct.unpack('<BHI', body)
                raise RuntimeError('device error channel=%d code=%d details=%d' %
                                   (channel, code, details))
            if response_id != cmd_id:
                self._enqueue(packet)
                continue
            if typ == protocol.MSG_ACK:
                if len(body) != 7:
                    raise RuntimeError('invalid device ACK length')
                echo, ok, error = struct.unpack('<IBH', body)
                if echo != cmd_id:
                    raise RuntimeError('device ACK command id mismatch')
                if not ok:
                    raise RuntimeError('device command rejected error=%d' % error)
            if not expected or typ in expected:
                return typ, body
        raise TimeoutError('request timeout type=0x%02X id=%d' % (msg_type, cmd_id))

    def handshake(self):
        nonce = struct.unpack('<I', os.urandom(4))[0]
        _typ, body = self.request(
            protocol.MSG_HELLO, struct.pack('<I', nonce),
            expected=(protocol.MSG_HELLO_ACK,), timeout=3.0)
        hello = protocol.parse_hello(body)
        if hello['channels'] != 4:
            raise RuntimeError('unsupported BMCU channel count %s' % hello['channels'])
        if (hello['protocol'] != protocol.PROTO_VERSION or
                not protocol.firmware_is_compatible(
                    hello.get('firmware_tuple', (0, 0, 0)))):
            raise RuntimeError(
                'BMCU firmware is outside the supported %s..%s range; '
                'flash bundled firmware %s' %
                ('.'.join(str(part) for part in protocol.MIN_COMPATIBLE_FIRMWARE),
                 protocol.REQUIRED_FIRMWARE_TEXT,
                 protocol.REQUIRED_FIRMWARE_TEXT))
        self.request(
            protocol.MSG_SESSION_CONFIRM,
            struct.pack('<I', int(hello['session_id']) & 0xffffffff),
            expected=(protocol.MSG_ACK,), timeout=2.0)
        return hello

    def wait_operation(self, op_id, timeout=12.0):
        operation_timeout = _validated_timeout(timeout, 'operation timeout')
        deadline = time.monotonic() + operation_timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            packet = self.queued.pop(0) if self.queued else self.read(max(0.05, min(0.25, remaining)))
            if packet is None:
                continue
            typ, seq, cmd_id, body = packet
            if typ == protocol.MSG_OP_RESULT:
                result = protocol.parse_op_result(body)
                if result['op_id'] == op_id:
                    return result
        raise TimeoutError('operation %d did not finish' % op_id)

def print_status(status):
    print('session=%d active_op=%d loaded_mask=0x%X uncertain_mask=0x%X now=%s' % (
        status['session_id'], status['active_op_id'],
        int(status.get('loaded_mask', 0)), int(status.get('uncertain_mask', 0)),
        '-' if status['now_channel'] == 0xff else status['now_channel']))
    route_names = status.get('route_state_name', ['UNCERTAIN'] * 4)
    for channel in range(4):
        print('Channel %d: route=%s present=%d connected=%d buffer=%d%% raw=%.5f encoder_io=%d distance=%.2fmm pwm=%d motion=%d cal=%d' % (
            channel, route_names[channel], status['present'][channel],
            bool(status['connected_mask'] & (1 << channel)),
            status['buffer_pct'][channel], status['buffer_raw'][channel],
            bool(status['encoder_io_mask'] & (1 << channel)),
            status['meters'][channel] * 1000.0,
            status['motor_pwm'][channel], status['motion'][channel],
            bool(status['calibration_valid_mask'] & (1 << channel))))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('port')
    parser.add_argument('command', choices=('hello', 'status', 'cal-get', 'cal-min', 'cal-neutral', 'cal-max', 'cal-commit', 'encoder-test', 'autoload', 'op-get', 'stop'))
    parser.add_argument('--channel', type=int, choices=range(4), default=0)
    parser.add_argument('--mm', type=float)
    parser.add_argument('--op-id', type=int, default=0, help='operation ID for op-get; 0 returns the latest result')
    parser.add_argument('--baud', type=int, default=115200)
    args = parser.parse_args()
    if args.baud < 9600 or args.baud > 2000000:
        parser.error('--baud must be within 9600..2000000')
    if args.mm is not None and (not math.isfinite(args.mm) or not 5.0 <= args.mm <= 250.0):
        parser.error('--mm must be finite and within 5..250')

    link = Link(args.port, args.baud)
    link.open()
    try:
        hello = link.handshake()
        if args.command == 'hello':
            print(hello)
        elif args.command == 'status':
            _, body = link.request(protocol.MSG_GET_STATUS, expected=(protocol.MSG_STATUS,))
            print_status(protocol.parse_status(body))
        elif args.command == 'cal-get':
            _, body = link.request(protocol.MSG_GET_CALIBRATION, bytes([args.channel]), expected=(protocol.MSG_CALIBRATION,))
            print(protocol.parse_calibration(body))
        elif args.command in ('cal-min', 'cal-neutral', 'cal-max'):
            point = {'cal-min': protocol.CAL_MIN, 'cal-neutral': protocol.CAL_NEUTRAL, 'cal-max': protocol.CAL_MAX}[args.command]
            _, body = link.request(protocol.MSG_CAL_CAPTURE, bytes([args.channel, point]), expected=(protocol.MSG_CALIBRATION,))
            print(protocol.parse_calibration(body))
        elif args.command == 'cal-commit':
            _, body = link.request(protocol.MSG_CAL_COMMIT, bytes([args.channel]), expected=(protocol.MSG_CALIBRATION,))
            print(protocol.parse_calibration(body))
        elif args.command in ('encoder-test', 'autoload'):
            mm = args.mm if args.mm is not None else (50.0 if args.command == 'encoder-test' else 120.0)
            msg = protocol.MSG_TEST_ENCODER if args.command == 'encoder-test' else protocol.MSG_CHANNEL_AUTOLOAD
            op_id = link._next()
            link.send(msg, bytes([args.channel]) + struct.pack('<f', mm), op_id)

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                packet = link.read(0.25)
                if packet and packet[0] == protocol.MSG_ACK and packet[2] == op_id:
                    break
                if packet:
                    link.queued.append(packet)
            result = link.wait_operation(op_id)
            print(result)
            if not result['ok']:
                raise SystemExit(2)
        elif args.command == 'op-get':
            _, body = link.request(protocol.MSG_GET_OP_RESULT, struct.pack('<I', args.op_id),
                                   expected=(protocol.MSG_OP_RESULT,))
            print(protocol.parse_op_result(body))
        elif args.command == 'stop':
            link.request(protocol.MSG_STOP_ALL, expected=(protocol.MSG_ACK,))
            print('stopped')
    finally:
        link.close()

if __name__ == '__main__':
    main()
