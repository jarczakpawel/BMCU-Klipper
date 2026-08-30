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
import struct
import time
from dataclasses import dataclass

MAGIC_REQ = b'\x57\xAB'
MAGIC_RSP = b'\x55\xAA'
CMD_IDENTIFY = 0xA1
CMD_ISP_END = 0xA2
CMD_ISP_KEY = 0xA3
CMD_ERASE = 0xA4
CMD_PROGRAM = 0xA5
CMD_VERIFY = 0xA6
CMD_READ_CFG = 0xA7
CMD_WRITE_CFG = 0xA8
CMD_SET_BAUD = 0xC5
BMCU_DEVICE_ID = 0x31
BMCU_DEVICE_TYPE = 0x19
BMCU_CFG_MASK = 0x1F
CFG_MASK_RDPR_USER_DATA_WPR = 0x07
FLASH_SIZE = 64 * 1024
CHUNK = 56
DEFAULT_VID = 0x1A86
DEFAULT_PID = 0x7523
MAX_ISP_FRAME = 4096
MAX_ISP_RX = 8192
ERASE_TIMEOUT = 60.0

def _integer(value, name, minimum, maximum):
    if isinstance(value, bool):
        raise ValueError('%s must be an integer' % name)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('%s must be an integer' % name) from exc
    if result < minimum or result > maximum:
        raise ValueError('%s must be within %d..%d' % (name, minimum, maximum))
    return result

def _u16(x):
    return struct.pack('<H', _integer(x, 'uint16', 0, 0xFFFF))

def _u32(x):
    return struct.pack('<I', _integer(x, 'uint32', 0, 0xFFFFFFFF))

def _pack(payload):
    return MAGIC_REQ + payload + bytes([sum(payload) & 0xFF])

def build_identify():
    data = bytes([BMCU_DEVICE_ID, BMCU_DEVICE_TYPE]) + b'MCU ISP & WCH.CN'
    return _pack(bytes([CMD_IDENTIFY]) + _u16(len(data)) + data)

def build_read_cfg(mask=BMCU_CFG_MASK):
    data = bytes([_integer(mask, 'mask', 0, 0xFF), 0])
    return _pack(bytes([CMD_READ_CFG]) + _u16(len(data)) + data)

def build_write_cfg(mask, data):
    data = bytes(data)
    payload = bytes([CMD_WRITE_CFG]) + _u16(2 + len(data)) + bytes([_integer(mask, 'mask', 0, 0xFF), 0]) + data
    return _pack(payload)

def build_isp_key(seed):
    seed = bytes(seed)
    return _pack(bytes([CMD_ISP_KEY]) + _u16(len(seed)) + seed)

def build_erase(sectors):
    sectors = _integer(sectors, 'sectors', 1, FLASH_SIZE // 1024)
    return _pack(bytes([CMD_ERASE]) + _u16(4) + _u32(sectors))

def build_set_baud(baud):
    baud = _integer(baud, 'baud', 9600, 2000000)
    return _pack(bytes([CMD_SET_BAUD]) + _u16(4) + _u32(baud))

def build_program(address, data, padding=0):
    address = _integer(address, 'address', 0, FLASH_SIZE)
    padding = _integer(padding, 'padding', 0, 0xFF)
    data = bytes(data)
    if len(data) > CHUNK or address + len(data) > FLASH_SIZE:
        raise ValueError('program range is outside flash or exceeds one chunk')
    payload = bytes([CMD_PROGRAM]) + _u16(5 + len(data)) + _u32(address) + bytes([padding]) + data
    return _pack(payload)

def build_verify(address, data, padding=0):
    address = _integer(address, 'address', 0, FLASH_SIZE)
    padding = _integer(padding, 'padding', 0, 0xFF)
    data = bytes(data)
    if len(data) > CHUNK or address + len(data) > FLASH_SIZE:
        raise ValueError('verify range is outside flash or exceeds one chunk')
    payload = bytes([CMD_VERIFY]) + _u16(5 + len(data)) + _u32(address) + bytes([padding]) + data
    return _pack(payload)

def build_isp_end(reason):
    return _pack(bytes([CMD_ISP_END]) + _u16(1) + bytes([_integer(reason, 'reason', 0, 0xFF)]))

def calc_xor_key_seed(seed, uid_chk, chip_id):
    if len(seed) < 8:
        raise ValueError('seed too short')
    a, b = len(seed) // 5, len(seed) // 7
    k0 = seed[b * 4] ^ uid_chk
    values = [k0, seed[a] ^ uid_chk, seed[b] ^ uid_chk,
              seed[b * 6] ^ uid_chk, seed[b * 3] ^ uid_chk,
              seed[a * 3] ^ uid_chk, seed[b * 5] ^ uid_chk,
              (k0 + chip_id) & 0xFF]
    return bytes(values)

def calc_xor_key_uid(uid8, chip_id):
    total = sum(uid8) & 0xFF
    key = bytearray([total] * 8)
    key[7] = (key[7] + chip_id) & 0xFF
    return bytes(key)

def xor_crypt(data, key8):
    return bytes(value ^ key8[index & 7] for index, value in enumerate(data))

@dataclass(frozen=True)
class IspIdentity:
    chip_id: int
    chip_type: int
    uid8: bytes

    @property
    def uid_hex(self):
        return self.uid8.hex().upper()

class WchIsp:
    def __init__(self, port, baud=115200, parity='N', trace=False, serial_factory=None):
        self.port = port
        self.baud = int(baud)
        self.parity = parity
        self.trace = bool(trace)
        self.serial_factory = serial_factory
        self.ser = None
        self.rx = bytearray()

    def _write_all(self, packet, timeout=5.0):
        deadline = time.monotonic() + float(timeout)
        offset = 0
        while offset < len(packet):
            written = self.ser.write(packet[offset:])
            if written:
                offset += int(written)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError('timeout writing ISP packet')
            time.sleep(0.002)

    def open(self):
        import serial
        parity_map = {'N': serial.PARITY_NONE, 'E': serial.PARITY_EVEN, 'O': serial.PARITY_ODD}
        factory = self.serial_factory or serial.Serial
        kwargs = dict(port=self.port, baudrate=self.baud, timeout=0, write_timeout=1.0,
                      rtscts=False, dsrdtr=False, bytesize=serial.EIGHTBITS,
                      parity=parity_map[self.parity], stopbits=serial.STOPBITS_ONE)
        if os.name == 'posix':
            kwargs['exclusive'] = True
        try:
            self.ser = factory(**kwargs)
        except TypeError:
            kwargs.pop('exclusive', None)
            self.ser = factory(**kwargs)

    def close(self):
        if self.ser is not None:
            try:
                self.ser.dtr = True
                self.ser.rts = True
            except Exception:
                pass
            try:
                self.ser.close()
            finally:
                self.ser = None
        self.rx.clear()

    def set_baud(self, baud):
        self.baud = int(baud)
        self.ser.baudrate = self.baud

    def flush(self):
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.rx.clear()

    def recv(self, expect_cmd, timeout):
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            waiting = getattr(self.ser, 'in_waiting', 0)
            if waiting:
                self.rx.extend(self.ser.read(min(int(waiting), MAX_ISP_RX)))
                if len(self.rx) > MAX_ISP_RX:

                    marker = self.rx.rfind(MAGIC_RSP)
                    self.rx[:] = self.rx[marker:] if marker >= 0 else b''
            index = self.rx.find(MAGIC_RSP)
            if index >= 0:
                if index:
                    del self.rx[:index]
                if len(self.rx) >= 7:
                    command = self.rx[2]
                    length = self.rx[4] | (self.rx[5] << 8)
                    total = 2 + 4 + length + 1
                    if total > MAX_ISP_FRAME:
                        del self.rx[:2]
                        continue
                    if len(self.rx) >= total:
                        frame = bytes(self.rx[:total])
                        del self.rx[:total]
                        payload = frame[2:-1]
                        if (sum(payload) & 0xFF) != frame[-1]:
                            continue
                        if command != expect_cmd:
                            continue
                        code = frame[3]
                        data = frame[6:-1]
                        if self.trace:
                            print('RX cmd=0x%02X code=0x%02X data=%s' %
                                  (command, code, data.hex()))
                        return code, data
            time.sleep(0.002)
        raise TimeoutError('timeout waiting for ISP cmd=0x%02X' % expect_cmd)

    def txrx(self, packet, expect_cmd, timeout):
        if self.trace:
            print('TX ' + packet.hex())
        self._write_all(packet)
        self.ser.flush()
        return self.recv(expect_cmd, timeout)

def set_lines(isp, boot_is_dtr, boot_value, reset_value):
    if boot_is_dtr:
        isp.ser.dtr = boot_value
        isp.ser.rts = reset_value
    else:
        isp.ser.rts = boot_value
        isp.ser.dtr = reset_value

def pulse_reset(isp, boot_is_dtr, reset_assert):
    if boot_is_dtr:
        isp.ser.rts = not reset_assert
        time.sleep(0.02)
        isp.ser.rts = reset_assert
    else:
        isp.ser.dtr = not reset_assert
        time.sleep(0.02)
        isp.ser.dtr = reset_assert

def autodi_try(isp, identify_packet):
    for boot_is_dtr in (True, False):
        for boot_assert in (True, False):
            for reset_assert in (True, False):
                try:
                    isp.flush()
                    set_lines(isp, boot_is_dtr, boot_assert, reset_assert)
                    time.sleep(0.02)
                    pulse_reset(isp, boot_is_dtr, reset_assert)
                    time.sleep(0.06)
                    isp.flush()
                    code, data = isp.txrx(identify_packet, CMD_IDENTIFY, 0.6)
                    if code == 0 and len(data) >= 2:
                        return boot_is_dtr, boot_assert, reset_assert
                except Exception:
                    continue
    return None

def _log(callback, level, message):
    if callback:
        callback(level, message)

def _progress(callback, percent, stage, message=''):
    if callback:
        callback(max(0, min(100, int(percent))), stage, message)

def _wait_manual_isp(isp, identify_packet, timeout, log_callback,
                     progress_callback=None, percent=2, reentry=False):
    if reentry:
        message = ('TTL: enter the bootloader again - hold BOOT, tap RESET, '
                   'then release BOOT. Waiting for ISP...')
    else:
        message = ('TTL: hold BOOT, tap RESET, then release BOOT. '
                   'Waiting for ISP...')
    _log(log_callback, 'ACTION', message)
    _progress(progress_callback, percent, 'ttl', message)
    deadline = time.monotonic() + float(timeout)
    last_error = None
    while time.monotonic() < deadline:
        try:
            isp.flush()
            code, data = isp.txrx(identify_packet, CMD_IDENTIFY, 0.8)
            if code == 0 and len(data) >= 2:
                _log(log_callback, 'INFO', 'TTL bootloader detected')
                return data
            last_error = RuntimeError('bad identify response')
        except Exception as exc:
            last_error = exc
        time.sleep(0.15)
    raise RuntimeError('manual ISP entry timed out: %s' % last_error)

def _normalized_mode(mode):
    mode = str(mode)
    if mode not in ('usb', 'ttl'):
        raise ValueError('mode must be usb or ttl')
    return mode

def _program_chunks(image):
    chunks = []
    size = len(image)
    for address in range(0, size, CHUNK):
        plain = image[address:address + CHUNK]

        if len(plain) < CHUNK and address + CHUNK <= FLASH_SIZE:
            plain += b'\xFF' * (CHUNK - len(plain))
        chunks.append((address, plain))
    return chunks

def flash_image(port, image, mode='usb', baud=115200, fast_baud=1_000_000,
                manual_timeout=120.0, trace=False, log_callback=None,
                progress_callback=None, before_erase=None, expected_isp_uid=''):

    image = bytes(image)
    if not 1 <= len(image) <= FLASH_SIZE:
        raise ValueError('firmware image must contain 1..65536 bytes')
    mode = _normalized_mode(mode)
    baud = int(baud)
    fast_baud = int(fast_baud)
    manual_timeout = float(manual_timeout)
    if not 9600 <= baud <= 2000000 or not 9600 <= fast_baud <= 2000000:
        raise ValueError('ISP baud must be within 9600..2000000')
    if not math.isfinite(manual_timeout) or not 1.0 <= manual_timeout <= 3600.0:
        raise ValueError('manual_timeout must be finite and within 1..3600 seconds')

    isp = WchIsp(port, baud=baud, trace=trace)
    identify_packet = build_identify()
    autodi = None
    erase_started = False
    try:
        isp.open()
        _progress(progress_callback, 1, 'isp', 'Opening serial port')
        if mode == 'usb':
            _log(log_callback, 'INFO', 'USB: entering bootloader automatically with DTR/RTS')
            _progress(progress_callback, 2, 'usb', 'Entering bootloader automatically')
            autodi = autodi_try(isp, identify_packet)
            if autodi is None:
                raise RuntimeError('USB AutoDI failed - check the USB/DFU connection')
            code, data = isp.txrx(identify_packet, CMD_IDENTIFY, 1.0)
        else:
            data = _wait_manual_isp(
                isp, identify_packet, manual_timeout, log_callback,
                progress_callback=progress_callback, percent=2)
            code = 0
        if code != 0 or len(data) < 2:
            raise RuntimeError('identify failed')
        chip_id, chip_type = data[0], data[1]
        if (chip_id, chip_type) != (BMCU_DEVICE_ID, BMCU_DEVICE_TYPE):
            raise RuntimeError('unexpected chip 0x%02X/0x%02X' % (chip_id, chip_type))

        code, cfg = isp.txrx(build_read_cfg(), CMD_READ_CFG, 1.2)
        if code != 0 or len(cfg) < 14:
            raise RuntimeError('read_cfg failed')
        cfg12 = bytearray(cfg[2:14])
        uid = bytes(cfg[-8:]) if len(cfg) >= 8 else b''
        identity = IspIdentity(chip_id, chip_type, uid)
        if expected_isp_uid and identity.uid_hex != expected_isp_uid.upper():
            raise RuntimeError('ISP UID mismatch expected=%s got=%s' %
                               (expected_isp_uid.upper(), identity.uid_hex))
        _log(log_callback, 'INFO', 'ISP UID ' + identity.uid_hex)
        _progress(progress_callback, 4, 'isp', 'BMCU bootloader identified')

        cfg_a = bytearray(cfg12)
        cfg_a[0:4] = b'\xA5\x5A\x3F\xC0'
        cfg_a[4:8] = b'\x00\xFF\x00\xFF'
        cfg_a[8:12] = b'\xFF\xFF\xFF\xFF'
        code, _ = isp.txrx(build_write_cfg(CFG_MASK_RDPR_USER_DATA_WPR, bytes(cfg_a)),
                           CMD_WRITE_CFG, 2.0)
        if code != 0:
            raise RuntimeError('write_cfg step1 failed')
        code, cfg = isp.txrx(build_read_cfg(), CMD_READ_CFG, 1.2)
        if code != 0 or len(cfg) < 14:
            raise RuntimeError('read_cfg after step1 failed')
        try:
            isp.txrx(build_isp_end(1), CMD_ISP_END, 1.2)
        except Exception:
            pass

        if mode == 'usb':
            _progress(progress_callback, 6, 'usb', 'Re-entering bootloader automatically')
            deadline = time.monotonic() + 3.0
            last_error = None
            while True:
                try:
                    isp.flush()
                    set_lines(isp, autodi[0], autodi[1], autodi[2])
                    time.sleep(0.02)
                    pulse_reset(isp, autodi[0], autodi[2])
                    time.sleep(0.08)
                    isp.flush()
                    code, data = isp.txrx(identify_packet, CMD_IDENTIFY, 0.8)
                    if code == 0 and len(data) >= 2:
                        break
                    last_error = RuntimeError('bad identify response')
                except Exception as exc:
                    last_error = exc
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        'USB failed to re-enter ISP after option step1: %s' % last_error)
                time.sleep(0.1)
        else:
            _wait_manual_isp(
                isp, identify_packet, manual_timeout, log_callback,
                progress_callback=progress_callback, percent=6, reentry=True)

        for _ in range(2):
            code, data = isp.txrx(identify_packet, CMD_IDENTIFY, 1.0)
            if code != 0 or len(data) < 2:
                raise RuntimeError('identify after re-entry failed')
            code, cfg = isp.txrx(build_read_cfg(), CMD_READ_CFG, 1.2)
            if code != 0 or len(cfg) < 14:
                raise RuntimeError('read_cfg after re-entry failed')
        cfg12 = bytearray(cfg[2:14])
        uid2 = bytes(cfg[-8:]) if len(cfg) >= 8 else b''
        if uid and uid2 and uid2 != uid:
            raise RuntimeError('ISP UID changed after re-entry')

        cfg_b = bytearray(cfg12)
        cfg_b[0:4] = b'\xFF\xFF\x3F\xC0'
        cfg_b[4:8] = b'\x00\x00\x00\x00'
        cfg_b[8:12] = b'\xFF\xFF\xFF\xFF'
        code, _ = isp.txrx(build_write_cfg(CFG_MASK_RDPR_USER_DATA_WPR, bytes(cfg_b)),
                           CMD_WRITE_CFG, 2.0)
        if code != 0:
            raise RuntimeError('write_cfg step2 failed')
        code, cfg = isp.txrx(build_read_cfg(), CMD_READ_CFG, 1.2)
        if code != 0 or len(cfg) < 14:
            raise RuntimeError('read_cfg after step2 failed')
        cfg12 = bytearray(cfg[2:14])
        wpr = bytes(cfg12[8:12])

        seed = b'\x00' * 0x1E
        code, key_response = isp.txrx(build_isp_key(seed), CMD_ISP_KEY, 1.2)
        if code != 0 or not key_response:
            raise RuntimeError('isp_key failed')
        boot_sum = key_response[0]
        uid_chk = cfg[2]
        candidates = []
        if len(uid2) == 8:
            candidates.append(('uid', calc_xor_key_uid(uid2, chip_id)))
        candidates.append(('seed', calc_xor_key_seed(seed, uid_chk, chip_id)))
        candidates = [item for item in candidates if (sum(item[1]) & 0xFF) == boot_sum]
        if not candidates:
            raise RuntimeError('ISP XOR key checksum mismatch')
        candidates.sort(key=lambda item: 0 if item[0] == 'uid' else 1)
        xor_key = candidates[0][1]

        if wpr != b'\xFF\xFF\xFF\xFF':
            cfg12[0:2] = b'\xA5\x5A'
            cfg12[8:12] = b'\xFF\xFF\xFF\xFF'
            code, _ = isp.txrx(build_write_cfg(CFG_MASK_RDPR_USER_DATA_WPR, bytes(cfg12)),
                               CMD_WRITE_CFG, 2.0)
            if code != 0:
                raise RuntimeError('write_cfg unprotect failed')
            time.sleep(0.08)
            code, cfg_check = isp.txrx(build_read_cfg(), CMD_READ_CFG, 1.2)
            if code != 0 or len(cfg_check) < 14 or bytes(cfg_check[10:14]) != b'\xFF' * 4:
                raise RuntimeError('write protection remains active')

        if before_erase:
            before_erase(identity)
        _progress(progress_callback, 8, 'erase', 'Erasing all 64 KiB')
        erase_started = True
        code, _ = isp.txrx(build_erase(64), CMD_ERASE, ERASE_TIMEOUT)
        if code != 0:
            raise RuntimeError('full-chip erase failed')

        for address, length in ((FLASH_SIZE - CHUNK, CHUNK), (FLASH_SIZE - 16, 16)):
            encrypted = xor_crypt(b'\xFF' * length, xor_key)
            code, _ = isp.txrx(build_verify(address, encrypted), CMD_VERIFY, 2.0)
            if code != 0:
                raise RuntimeError('erase verification failed at 0x%04X' % address)

        code, _ = isp.txrx(build_set_baud(int(fast_baud)), CMD_SET_BAUD, 1.2)
        if code != 0:
            raise RuntimeError('set_baud failed')
        time.sleep(0.03)
        isp.set_baud(int(fast_baud))
        isp.flush()

        chunks = _program_chunks(image)
        total = len(chunks)
        _progress(progress_callback, 10, 'program',
                  'Programming %d-byte firmware image' % len(image))
        for index, (address, plain) in enumerate(chunks, 1):
            code, _ = isp.txrx(build_program(address, xor_crypt(plain, xor_key)),
                               CMD_PROGRAM, 5.0)
            if code != 0:
                raise RuntimeError('program failed at 0x%04X' % address)
            _progress(progress_callback, 10 + index * 42 // total, 'program',
                      'Programming %d/%d' % (index, total))
        flush_address = chunks[-1][0] + len(chunks[-1][1])
        code, _ = isp.txrx(build_program(flush_address, b''), CMD_PROGRAM, 5.0)
        if code != 0:
            raise RuntimeError('program flush failed')

        code, key_response2 = isp.txrx(build_isp_key(seed), CMD_ISP_KEY, 1.2)
        if code != 0 or not key_response2 or key_response2[0] != boot_sum:
            raise RuntimeError('isp_key before verify failed')

        _progress(progress_callback, 53, 'verify', 'Verifying programmed firmware')
        for index, (address, plain) in enumerate(chunks, 1):
            code, _ = isp.txrx(build_verify(address, xor_crypt(plain, xor_key)),
                               CMD_VERIFY, 2.0)
            if code != 0:
                raise RuntimeError('verify failed at 0x%04X' % address)
            _progress(progress_callback, 53 + index * 45 // total, 'verify',
                      'Verifying %d/%d' % (index, total))

        try:
            isp.txrx(build_isp_end(0), CMD_ISP_END, 1.2)
        except Exception:
            pass
        if autodi is not None:
            set_lines(isp, autodi[0], not autodi[1], autodi[2])
            time.sleep(0.02)
            pulse_reset(isp, autodi[0], autodi[2])
        _progress(progress_callback, 100, 'done', 'Flash and verify completed')
        return identity
    except Exception as exc:
        setattr(exc, 'erase_started', erase_started)
        raise
    finally:
        isp.close()
