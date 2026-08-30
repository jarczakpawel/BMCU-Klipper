#!/usr/bin/env python3
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from bmcu_vendor import ensure_vendor_path
ensure_vendor_path(__file__)

import argparse
import binascii
import glob
import math
import os
import struct
import sys
import tempfile
import termios
import time

MSG_HELLO = 0x01
MSG_HELLO_ACK = 0x81
DEFAULT_BAUD = 115200
MIN_BAUD = 9600
MAX_BAUD = 2000000
BMCU_CHANNELS = 4
PROTO_VERSION = 1
REQUIRED_FIRMWARE = (1, 0, 0)
UID_HEX_LEN = 24
HELLO_ACK = struct.Struct('<12sBBBBIIBB')

def keep_bmcu_run(fd):
    try:
        attrs = termios.tcgetattr(fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass

def cobs_encode(data):
    out = bytearray()
    code_index = 0
    out.append(0)
    code = 1
    for b in data:
        if b == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1
    out[code_index] = code
    return bytes(out)

def cobs_decode(data):
    out = bytearray()
    idx = 0
    ln = len(data)
    while idx < ln:
        code = data[idx]
        if code == 0:
            raise ValueError('bad cobs')
        idx += 1
        end = idx + code - 1
        if end > ln:
            raise ValueError('truncated')
        out.extend(data[idx:end])
        idx = end
        if code != 0xff and idx < ln:
            out.append(0)
    return bytes(out)

def build_packet(msg_type, payload=b'', seq=1, cmd_id=1):
    body = struct.pack('<BBHII', PROTO_VERSION, msg_type, len(payload), seq, cmd_id) + payload
    body += struct.pack('<I', binascii.crc32(body) & 0xffffffff)
    return b'\x00' + cobs_encode(body) + b'\x00'

def parse_packet(raw):
    try:
        body = cobs_decode(raw)
    except Exception:
        return None
    if len(body) < 16:
        return None
    pkt, crc_raw = body[:-4], body[-4:]
    if (binascii.crc32(pkt) & 0xffffffff) != struct.unpack('<I', crc_raw)[0]:
        return None
    ver, typ, ln, seq, cmd_id = struct.unpack('<BBHII', pkt[:12])
    if ver != PROTO_VERSION or ln != len(pkt) - 12:
        return None
    return typ, cmd_id, pkt[12:]

def write_all(serial_obj, data, timeout=0.5):
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError('write timeout must be a positive finite number') from exc
    if timeout <= 0.0 or not math.isfinite(timeout):
        raise ValueError('write timeout must be a positive finite number')
    deadline = time.monotonic() + timeout
    offset = 0
    while offset < len(data):
        written = serial_obj.write(data[offset:])
        if written:
            offset += written
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError('serial write timeout')
        time.sleep(0.002)
    serial_obj.flush()

def parse_hello_identity(payload):
    if len(payload) != HELLO_ACK.size:
        return None
    uid, major, minor, patch, proto, _caps, session, channels, _profile = HELLO_ACK.unpack(payload)
    if (uid in (b'\x00' * 12, b'\xff' * 12) or
            (major, minor, patch) != REQUIRED_FIRMWARE or
            proto != PROTO_VERSION or session == 0 or
            channels != BMCU_CHANNELS):
        return None
    return uid.hex().upper()

def read_packets(serial_obj, deadline):
    buf = bytearray()
    while time.monotonic() < deadline:
        data = serial_obj.read(128)
        if not data:
            continue
        for value in data:
            if value == 0:
                if not buf:
                    continue
                packet = parse_packet(bytes(buf))
                buf.clear()
                if packet:
                    yield packet
            else:
                buf.append(value)
                if len(buf) > 512:
                    buf.clear()

def try_port(port, baud, verbose=False, attempts=10, settle_time=1.5, response_timeout=0.45):
    import serial
    serial_obj = None
    try:
        serial_obj = serial.Serial()
        serial_obj.port = port
        serial_obj.baudrate = baud
        serial_obj.bytesize = serial.EIGHTBITS
        serial_obj.parity = serial.PARITY_NONE
        serial_obj.stopbits = serial.STOPBITS_ONE
        serial_obj.timeout = 0.02
        serial_obj.write_timeout = 0.5
        serial_obj.dtr = True
        serial_obj.rts = False
        serial_obj.open()
        try:
            serial_obj.setDTR(True)
            serial_obj.setRTS(False)
            keep_bmcu_run(serial_obj.fileno())
        except Exception:
            pass
        time.sleep(float(settle_time))
        try:
            serial_obj.reset_input_buffer()
            serial_obj.reset_output_buffer()
        except Exception:
            pass
        for attempt in range(int(attempts)):
            cmd_id = attempt + 1
            frame = build_packet(MSG_HELLO, os.urandom(4), seq=cmd_id, cmd_id=cmd_id)
            if verbose:
                print('try %s baud %d hello %d' %
                      (port, baud, attempt + 1), file=sys.stderr)
            write_all(serial_obj, frame)
            for typ, rx_cmd_id, payload in read_packets(
                    serial_obj, time.monotonic() + float(response_timeout)):
                if typ == MSG_HELLO_ACK and rx_cmd_id == cmd_id:
                    uid = parse_hello_identity(payload)
                    if uid is not None:
                        return uid
        return None
    except Exception as exc:
        if verbose:
            print('%s baud %d: %s' % (port, baud, exc), file=sys.stderr)
        return None
    finally:
        if serial_obj is not None:
            try:
                keep_bmcu_run(serial_obj.fileno())
                serial_obj.setDTR(True)
                serial_obj.setRTS(False)
                serial_obj.close()
            except Exception:
                pass

def _path_rank(path):
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

def choose_ports(raw_ports, preserve_explicit=False, exists=os.path.exists,
                 realpath=os.path.realpath):

    selected = {}
    order = []
    for port in raw_ports:
        if not exists(port):
            continue
        real = realpath(port)
        if real not in selected:
            selected[real] = port
            order.append(real)
            continue
        if not preserve_explicit:
            current = selected[real]
            if (_path_rank(port), str(port)) < (_path_rank(current), str(current)):
                selected[real] = port
    if preserve_explicit:
        return [selected[real] for real in order]
    return sorted(selected.values(), key=lambda p: (_path_rank(p), str(p)))

def scan_candidates():
    return (glob.glob('/dev/serial/by-path/*') +
            glob.glob('/dev/serial/by-id/*') +
            glob.glob('/dev/ttyCH343USB*') +
            glob.glob('/dev/ttyUSB*') +
            glob.glob('/dev/ttyACM*'))

def stable_device_order(found):
    return sorted(found, key=lambda item: (str(item[1]).upper(), str(item[0])))

def validate_found(found):
    seen_uid = {}
    bauds = set()
    for port, uid, baud in found:
        uid = str(uid).upper()
        if (len(uid) != UID_HEX_LEN or
                any(c not in '0123456789ABCDEF' for c in uid) or
                uid in ('0' * UID_HEX_LEN, 'F' * UID_HEX_LEN)):
            raise ValueError('invalid BMCU hardware UID from %s: %s' % (port, uid))
        previous = seen_uid.get(uid)
        if previous is not None and previous != port:
            raise ValueError(
                'duplicate BMCU UID %s reported by %s and %s; refusing to '
                'write an ambiguous configuration' % (uid, previous, port))
        seen_uid[uid] = port
        bauds.add(int(baud))
    if len(bauds) > 1:
        raise ValueError(
            'detected BMCUs use different baud rates %s, but one [bmcu] '
            'section has one shared baud' % sorted(bauds))

def _fsync_directory(path):
    flags = getattr(os, 'O_DIRECTORY', 0) | os.O_RDONLY
    try:
        fd = os.open(path or '.', flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def atomic_write_text(path, text):
    target = os.path.abspath(path)
    if os.path.lexists(target) and os.path.islink(target):
        raise ValueError('refusing to replace symlinked output: %s' % target)
    directory = os.path.dirname(target) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.bmcu-detect-', suffix='.tmp',
                                     dir=directory, text=True)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(target) and os.path.islink(target):
            raise ValueError('refusing to replace symlinked output: %s' % target)
        os.replace(temporary, target)
        _fsync_directory(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

def render_detected_config(found, fallback_baud=DEFAULT_BAUD):
    found = stable_device_order(found)
    validate_found(found)
    baud = found[0][2] if found else fallback_baud
    lines = [
        '# Auto-generated by detect_bmcu.py',
        '# Apply with scripts/apply_detected_devices.py; existing devices are preserved by default.',
        '# Endpoint assignment is intentionally omitted; one-device setups default to extruder,',
        '# while multi-output printers must assign each physical BMCU Output explicitly.',
        '',
        '[bmcu]',
        'baud: %d' % baud,
        'devices:',
    ]
    for index, (port, uid, _baud) in enumerate(found):
        lines.append('  bmcu%d,%s,%s' % (index, port, uid))
    return '\n'.join(lines) + '\n'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--port', action='append', default=[],
                        help='Specific serial port to probe. Repeatable; spelling is preserved.')
    parser.add_argument('--scan-all', action='store_true',
                        help='Probe by-path, by-id, ttyUSB and ttyACM candidates. '
                             'Broad serial scans should be used deliberately.')
    parser.add_argument('--baud', action='append', type=int, default=[])
    parser.add_argument('--stop-after-first', action='store_true',
                        help='Stop scanning after the first detected BMCU')
    parser.add_argument('--quick', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    bauds = args.baud or [DEFAULT_BAUD]
    for baud in bauds:
        if baud < MIN_BAUD or baud > MAX_BAUD:
            parser.error('--baud must be between %d and %d' % (MIN_BAUD, MAX_BAUD))
    if len(set(bauds)) != len(bauds):
        parser.error('duplicate --baud value')

    if args.port:
        raw_ports = args.port
        preserve_explicit = True
    elif args.scan_all:
        raw_ports = scan_candidates()
        preserve_explicit = False
    else:
        print('No port specified. Use --port /dev/serial/by-path/... or --scan-all.',
              file=sys.stderr)
        return 2

    ports = choose_ports(raw_ports, preserve_explicit=preserve_explicit)
    if args.verbose:
        missing = [port for port in raw_ports if not os.path.exists(port)]
        for port in missing:
            print('skip missing port: %s' % port, file=sys.stderr)

    found = []
    for port in ports:
        for baud in bauds:
            uid = try_port(
                port, baud, args.verbose,
                attempts=4 if args.quick else 10,
                settle_time=0.75 if args.quick else 1.5,
                response_timeout=0.25 if args.quick else 0.45)
            if uid:
                found.append((port, uid, baud))
                print('BMCU %s UID=%s baud=%d' % (port, uid, baud))
                break
        if found and args.stop_after_first:
            break

    if not found:
        print('No BMCU found. Plug CH340 USB and run this script again.',
              file=sys.stderr)
        return 1
    try:
        text = render_detected_config(found, fallback_baud=bauds[0])
        atomic_write_text(args.output, text)
    except (OSError, ValueError) as exc:
        print('Detection result not written: %s' % exc, file=sys.stderr)
        return 3
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
