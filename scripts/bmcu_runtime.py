#!/usr/bin/env python3

from __future__ import annotations

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from bmcu_vendor import ensure_vendor_path
ensure_vendor_path(__file__)

import math
import os
import sys
import time
import struct
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
EXTRAS = ROOT / 'klippy' / 'extras'
if str(EXTRAS) not in sys.path:
    sys.path.insert(0, str(EXTRAS))

from bmcu_core import protocol

class RuntimeErrorReply(RuntimeError):
    pass

class RuntimeClient:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0):
        self.port = str(port)
        if not self.port or '\x00' in self.port or len(self.port) > 4096:
            raise ValueError('invalid serial port')
        self.baud = int(baud)
        self.timeout = float(timeout)
        if self.baud < 9600 or self.baud > 2000000:
            raise ValueError('baud must be within 9600..2000000')
        if not math.isfinite(self.timeout) or not 0.05 <= self.timeout <= 300.0:
            raise ValueError('timeout must be finite and within 0.05..300 seconds')
        self.serial = None
        self.decoder = protocol.StreamDecoder(max_frame=1024)
        self.seq = 1
        self.cmd_id = 1
        self.hello = None
        self.caps = None
        self.last_rx_seq = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def open(self):
        if self.serial is not None:
            return

        self.decoder = protocol.StreamDecoder(max_frame=1024)
        self.seq = 1
        self.cmd_id = 1
        self.hello = None
        self.caps = None
        self.last_rx_seq = None
        import serial
        kwargs = dict(port=self.port, baudrate=self.baud, timeout=0,
                      write_timeout=1.0, rtscts=False, dsrdtr=False)
        if os.name == 'posix':
            kwargs['exclusive'] = True
        try:
            self.serial = serial.Serial(**kwargs)
        except TypeError:
            kwargs.pop('exclusive', None)
            self.serial = serial.Serial(**kwargs)
        try:
            self.serial.dtr = True
            self.serial.rts = False
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
        except Exception:
            pass

    def close(self):
        if self.serial is not None:
            try:
                self.serial.close()
            finally:
                self.serial = None

    def _next_seq(self):
        value = self.seq
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        return value

    def _next_cmd(self):
        value = self.cmd_id
        self.cmd_id = (self.cmd_id + 1) & 0xFFFFFFFF
        if self.cmd_id == 0:
            self.cmd_id = 1
        return value

    def _accept_rx_seq(self, seq):
        seq = int(seq) & 0xFFFFFFFF
        if self.last_rx_seq is None:
            self.last_rx_seq = seq
            return True
        delta = (seq - self.last_rx_seq) & 0xFFFFFFFF
        if delta == 0 or delta >= 0x80000000:
            return False
        self.last_rx_seq = seq
        return True

    def _write_all(self, data: bytes):
        deadline = time.monotonic() + self.timeout
        offset = 0
        while offset < len(data):
            written = self.serial.write(data[offset:])
            if written:
                offset += written
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError('runtime serial write timeout')
            time.sleep(0.002)
        self.serial.flush()

    def request(self, msg_type: int, payload: bytes = b'', expected=None,
                timeout: Optional[float] = None):
        if self.serial is None:
            raise RuntimeError('runtime serial port is closed')
        try:
            request_timeout = self.timeout if timeout is None else float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                'request timeout must be finite and within 0.05..300 seconds') from exc
        if not math.isfinite(request_timeout) or not 0.05 <= request_timeout <= 300.0:
            raise ValueError('request timeout must be finite and within 0.05..300 seconds')
        expected = set(expected or (protocol.MSG_ACK,))
        cmd_id = self._next_cmd()
        self._write_all(protocol.encode_packet(msg_type, self._next_seq(), cmd_id, payload))
        deadline = time.monotonic() + request_timeout
        while time.monotonic() < deadline:
            waiting = getattr(self.serial, 'in_waiting', 0)
            data = self.serial.read(waiting or 1)
            if not data:
                time.sleep(0.002)
                continue
            for packet_type, packet_seq, packet_cmd, packet_payload in self.decoder.feed(data):
                if not self._accept_rx_seq(packet_seq):
                    continue
                if packet_cmd != cmd_id:
                    continue
                if packet_type == protocol.MSG_ERROR:
                    if len(packet_payload) != 7:
                        raise RuntimeErrorReply('invalid BMCU ERROR length')
                    channel, code, details = struct.unpack('<BHI', packet_payload)
                    raise RuntimeErrorReply(
                        'BMCU error channel=%d code=%d details=%d' %
                        (channel, code, details))
                if packet_type == protocol.MSG_ACK:
                    if len(packet_payload) != 7:
                        raise RuntimeErrorReply('invalid BMCU ACK length')
                    echo, ok, error = struct.unpack('<IBH', packet_payload)
                    if echo != cmd_id:
                        raise RuntimeErrorReply('BMCU ACK command id mismatch')
                    if not ok:
                        raise RuntimeErrorReply('BMCU command rejected error=%d' % error)
                    if packet_type in expected:
                        return {'ok': True, 'error': error}
                if packet_type in expected:
                    return packet_payload
        raise TimeoutError('runtime request timeout type=0x%02X id=%d' % (msg_type, cmd_id))

    def handshake(self):
        nonce = struct.unpack('<I', os.urandom(4))[0]
        payload = self.request(
            protocol.MSG_HELLO, struct.pack('<I', nonce),
            expected=(protocol.MSG_HELLO_ACK,), timeout=3.0)
        self.hello = protocol.parse_hello(payload)
        if self.hello['channels'] != 4:
            raise RuntimeError('unsupported BMCU channel count %s' % self.hello['channels'])
        if (self.hello['protocol'] != protocol.PROTO_VERSION or
                tuple(self.hello.get('firmware_tuple', (0, 0, 0))) !=
                protocol.REQUIRED_FIRMWARE):
            raise RuntimeError(
                'BMCU firmware does not match host %s; flash bundled firmware %s' %
                (protocol.REQUIRED_FIRMWARE_TEXT,
                 protocol.REQUIRED_FIRMWARE_TEXT))
        self.request(protocol.MSG_SESSION_CONFIRM,
                     struct.pack('<I', int(self.hello['session_id']) & 0xFFFFFFFF),
                     timeout=2.0)
        caps_payload = self.request(protocol.MSG_GET_CAPS,
                                    expected=(protocol.MSG_CAPS,), timeout=2.0)
        self.caps = protocol.parse_caps(caps_payload)
        if self.hello['uid'] != self.caps['uid']:
            raise RuntimeError('HELLO/CAPS UID mismatch')
        if self.hello['firmware'] != self.caps['firmware']:
            raise RuntimeError('HELLO/CAPS firmware mismatch')
        if self.caps['channels'] != self.hello['channels']:
            raise RuntimeError('HELLO/CAPS channel count mismatch')
        if self.caps['protocol'] != self.hello['protocol']:
            raise RuntimeError('HELLO/CAPS protocol mismatch')
        return dict(self.hello)

    def prepare_update(self):
        if self.hello is None:
            self.handshake()
        return self.request(protocol.MSG_UPDATE_PREPARE, timeout=3.0)

    def cancel_update(self):
        return self.request(protocol.MSG_UPDATE_CANCEL, timeout=2.0)

    def export_nvm(self, chunk_size: int = 224):
        if chunk_size < 16 or chunk_size > 224:
            raise ValueError('chunk_size must be 16..224')
        self.prepare_update()
        image = bytearray(4096)
        expected_crc = None
        for offset in range(0, 4096, chunk_size):
            amount = min(chunk_size, 4096 - offset)
            raw = self.request(protocol.MSG_NVM_READ, struct.pack('<HH', offset, amount),
                               expected=(protocol.MSG_NVM_DATA,), timeout=3.0)
            item = protocol.parse_nvm_data(raw)
            if item['offset'] != offset or item['length'] != amount:
                raise RuntimeError('NVM chunk mismatch at %d' % offset)
            if expected_crc is None:
                expected_crc = item['total_crc']
            elif item['total_crc'] != expected_crc:
                raise RuntimeError('NVM changed during backup')
            image[offset:offset + amount] = item['data']
        actual_crc = protocol.crc32(image)
        if expected_crc != actual_crc:
            raise RuntimeError('NVM CRC mismatch expected=%08X got=%08X' %
                               (expected_crc, actual_crc))
        return bytes(image), actual_crc
