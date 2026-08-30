# SPDX-License-Identifier: GPL-3.0-or-later
import binascii
import math
import struct

PROTO_VERSION = 1
REQUIRED_FIRMWARE = (1, 0, 0)
REQUIRED_FIRMWARE_TEXT = '1.0.0'

MSG_HELLO = 0x01
MSG_PING = 0x02
MSG_GET_STATUS = 0x03
MSG_GET_CAPS = 0x04
MSG_GET_CALIBRATION = 0x05
MSG_GET_OP_RESULT = 0x06
MSG_GET_SNAPSHOT = 0x07
MSG_SESSION_CONFIRM = 0x08
MSG_SET_MOTION = 0x10
MSG_SET_ACTIVE_CH = 0x11
MSG_CAL_CAPTURE = 0x12
MSG_CAL_COMMIT = 0x13
MSG_TEST_ENCODER = 0x14
MSG_CHANNEL_AUTOLOAD = 0x15
MSG_ABORT_OP = 0x16
MSG_CLEAR_CALIBRATION = 0x17
MSG_FEED_TO_CONTACT = 0x18
MSG_FEED_DISTANCE = 0x19
MSG_MARK_UNLOADED = 0x1A
MSG_MARK_LOADED = 0x1B
MSG_CAL_AUTO_START = 0x1C
MSG_SET_SLOT_INFO = 0x20
MSG_GET_SLOT_INFO = 0x21
MSG_SET_SLOTS = 0x22
MSG_CHANNEL_RETRACT = 0x23
MSG_CONFIG_GET = 0x30
MSG_CONFIG_SET = 0x31
MSG_CONFIG_SAVE = 0x32
MSG_SET_SYSTEM_LED = 0x33
MSG_RUNTIME_SYNC = 0x34
MSG_SET_LIGHTING = 0x35
MSG_LED_PREVIEW = 0x36
MSG_STOP_ALL = 0x50
MSG_RESET_ERROR = 0x51
MSG_REBOOT = 0x53
MSG_UPDATE_PREPARE = 0x60
MSG_NVM_READ = 0x61
MSG_UPDATE_CANCEL = 0x62

MSG_HELLO_ACK = 0x81
MSG_PONG = 0x82
MSG_STATUS = 0x90
MSG_CAPS = 0x91
MSG_SLOT_INFO = 0x92
MSG_CONFIG_VAL = 0x93
MSG_CALIBRATION = 0x94
MSG_OP_RESULT = 0x95
MSG_NVM_DATA = 0x96
MSG_SNAPSHOT = 0x97
MSG_STATE_CHANGED = 0xA0
MSG_MOTION_DONE = 0xA1
MSG_JAM = 0xA2
MSG_ERROR = 0xA4
MSG_ACK = 0xB0

CONFIG_LOAD_PRESSURE_PCT = 0x0002

CONFIG_LOAD_PROFILE = CONFIG_LOAD_PRESSURE_PCT
CONFIG_LOAD_SPEED_MMS = 0x0003
CONFIG_PULL_SPEED_MMS = 0x0004
CONFIG_PULL_SPEED_END_MMS = 0x0005
CONFIG_JAM_TIMEOUT_MS = 0x0006
CONFIG_BEFORE_PULLBACK_TARGET_PCT = 0x0009

MOTION_IDLE = 0
MOTION_SEND_OUT = 1
MOTION_BEFORE_ON_USE = 2
MOTION_ON_USE = 3
MOTION_BEFORE_PULL_BACK = 4
MOTION_PULL_BACK = 5
MOTION_STOP_ON_USE = 6

CAL_MIN = 0
CAL_NEUTRAL = 1
CAL_MAX = 2
CAL_NAMES = {CAL_MIN: 'MIN', CAL_NEUTRAL: 'NEUTRAL', CAL_MAX: 'MAX'}

OP_ENCODER_TEST = 1
OP_CHANNEL_AUTOLOAD = 2
OP_FEED_TO_CONTACT = 3
OP_FEED_DISTANCE = 4
OP_BUFFER_CALIBRATION = 5
OP_CHANNEL_RETRACT = 6
OP_STATE_IDLE = 0
OP_STATE_RUNNING = 1
OP_STATE_DONE = 2
OP_STATE_FAILED = 3
OP_STATE_ABORTED = 4

ROUTE_EMPTY = 0
ROUTE_LOADED = 1
ROUTE_UNCERTAIN = 2
ROUTE_NAMES = {ROUTE_EMPTY: 'EMPTY', ROUTE_LOADED: 'LOADED', ROUTE_UNCERTAIN: 'UNCERTAIN'}

CAP_CHANNEL_RETRACT = 1 << 21
CAP_LOAD_PRESSURE_PCT = 1 << 22
CAP_LED_PREVIEW = 1 << 23
CAP_LED_FILAMENT_PREVIEW = 1 << 24

OP_REASON = {
    0: 'none', 1: 'target', 2: 'timeout', 3: 'no_filament',
    4: 'encoder_io', 5: 'buffer_limit', 6: 'aborted', 7: 'busy',
    8: 'not_calibrated', 9: 'motion_stopped', 10: 'contact',
    11: 'distance_limit', 12: 'calibration_range', 13: 'nvm_write',
    14: 'sensor',
}

_HEADER = struct.Struct('<BBHII')
_STATUS = struct.Struct('<B4B4B4B4BB4BBBH4f4f4hIII4B6B')
_HELLO_ACK = struct.Struct('<12sBBBBIIBB')
_CAPS = struct.Struct('<BBBBBBBBI12s16s')
_CALIBRATION = struct.Struct('<BBBbfffff')
_OP_RESULT = struct.Struct('<IBBBBffI')
_SLOT = struct.Struct('<BBBBBHH20s8s')
_RUNTIME_SYNC_LEGACY = struct.Struct('<10f3B4B')
_RUNTIME_SYNC_CHANNEL_AUTOLOAD = struct.Struct('<14f3B4B')

def crc32(data):
    return binascii.crc32(data) & 0xffffffff

def cobs_encode(data):
    out = bytearray([0])
    code_index = 0
    code = 1
    for value in bytearray(data):
        if value == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(value)
            code += 1
            if code == 0xff:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1
    out[code_index] = code
    return bytes(out)

def cobs_decode(data):
    src = bytearray(data)
    out = bytearray()
    index = 0
    while index < len(src):
        code = src[index]
        if code == 0:
            raise ValueError('zero byte in COBS frame')
        index += 1
        end = index + code - 1
        if end > len(src):
            raise ValueError('truncated COBS frame')
        out.extend(src[index:end])
        index = end
        if code != 0xff and index < len(src):
            out.append(0)
    return bytes(out)

class ProtocolVersionError(ValueError):
    def __init__(self, version):
        self.version = int(version)
        ValueError.__init__(self, 'unsupported protocol version')

def encode_packet(msg_type, seq, cmd_id, payload=b''):
    payload = bytes(payload)
    header = _HEADER.pack(PROTO_VERSION, msg_type, len(payload), seq, cmd_id)
    body = header + payload
    return b'\x00' + cobs_encode(body + struct.pack('<I', crc32(body))) + b'\x00'

def decode_packet(decoded):
    if len(decoded) < _HEADER.size + 4:
        raise ValueError('short packet')
    body, stored_crc = decoded[:-4], struct.unpack_from('<I', decoded, len(decoded) - 4)[0]
    if crc32(body) != stored_crc:
        raise ValueError('CRC mismatch')
    version, msg_type, length, seq, cmd_id = _HEADER.unpack_from(body)
    if version != PROTO_VERSION:
        raise ProtocolVersionError(version)
    payload = body[_HEADER.size:]
    if len(payload) != length:
        raise ValueError('payload length mismatch')
    return msg_type, seq, cmd_id, payload

class StreamDecoder(object):
    def __init__(self, max_frame=256):
        self._buffer = bytearray()
        self._discarding = False
        self.max_frame = max_frame
        self.errors = 0
        self.incompatible_version = None

    def feed(self, data):
        packets = []
        for value in bytearray(data):
            if value == 0:
                if self._discarding:
                    self._discarding = False
                    self._buffer[:] = b''
                    continue
                if self._buffer:
                    frame = bytes(self._buffer)
                    self._buffer[:] = b''
                    try:
                        packets.append(decode_packet(cobs_decode(frame)))
                    except ProtocolVersionError as exc:
                        self.incompatible_version = exc.version
                        self.errors += 1
                    except (ValueError, struct.error):
                        self.errors += 1
                continue
            if self._discarding:
                continue
            if len(self._buffer) >= self.max_frame:
                self._buffer[:] = b''
                self._discarding = True
                self.errors += 1
                continue
            self._buffer.append(value)
        return packets

def _require_finite(values, label):
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError('%s contains a non-finite number' % label)

def _require_int_range(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s must be an integer' % label)
    if value < minimum or value > maximum:
        raise ValueError('%s must be within %d..%d' % (label, minimum, maximum))
    return value

def _encode_utf8_field(value, maximum, label):
    if not isinstance(value, str):
        raise ValueError('%s must be text' % label)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError('%s contains control characters' % label)
    encoded = bytearray()
    for character in value:
        item = character.encode('utf-8')
        if len(encoded) + len(item) > maximum:
            break
        encoded.extend(item)
    return bytes(encoded)

def parse_nvm_data(payload):
    if len(payload) < 8:
        raise ValueError('short NVM data')
    offset, length, total_crc = struct.unpack_from('<HHI', payload)
    data = payload[8:]
    if len(data) != length:
        raise ValueError('NVM data length mismatch')
    if length > 224:
        raise ValueError('NVM response exceeds 224-byte protocol chunk')
    if offset + length > 4096:
        raise ValueError('NVM data outside 4 KiB region')
    return {'offset': offset, 'length': length, 'total_crc': total_crc, 'data': data}

def parse_config_value(payload):
    if len(payload) != 6:
        raise ValueError('invalid config value length')
    key, value = struct.unpack_from('<Hf', payload)
    _require_finite((value,), 'config value')
    return {'key': key, 'value': value}

def parse_hello(payload):
    if len(payload) != _HELLO_ACK.size:
        raise ValueError('invalid hello length')
    uid, major, minor, patch, proto, caps, session, channels, load_pressure_pct = _HELLO_ACK.unpack_from(payload)
    if uid in (b'\x00' * 12, b'\xff' * 12):
        raise ValueError('invalid hardware UID')
    if session == 0:
        raise ValueError('invalid zero session id')
    return {
        'uid': uid.hex().upper(), 'firmware': '%d.%d.%d' % (major, minor, patch),
        'firmware_tuple': (major, minor, patch), 'protocol': proto, 'capabilities': caps, 'session_id': session,
        'channels': channels, 'load_pressure_pct': load_pressure_pct,
        'profile': load_pressure_pct,
    }

def parse_caps(payload):
    if len(payload) != _CAPS.size:
        raise ValueError('invalid caps length')
    values = _CAPS.unpack_from(payload)
    if values[9] in (b'\x00' * 12, b'\xff' * 12):
        raise ValueError('invalid capabilities hardware UID')
    return {
        'channels': values[0], 'hardware_variant': values[1],
        'has_rgb': bool(values[2]), 'firmware': '%d.%d.%d' % values[3:6],
        'protocol': values[6], 'capabilities': values[8],
        'uid': values[9].hex().upper(),
        'hardware_name': values[10].split(b'\0', 1)[0].decode('ascii', 'replace'),
    }

def parse_status(payload):
    if len(payload) != _STATUS.size:
        raise ValueError('invalid status payload length %d' % len(payload))
    v = _STATUS.unpack(payload)
    pos = 0
    now_channel = v[pos]; pos += 1
    route_state = list(v[pos:pos + 4]); pos += 4
    motion = list(v[pos:pos + 4]); pos += 4
    present = list(v[pos:pos + 4]); pos += 4
    buffer_pct = list(v[pos:pos + 4]); pos += 4
    cal_valid_mask = v[pos]; pos += 1
    capture_mask = list(v[pos:pos + 4]); pos += 4
    encoder_mask, connected_mask, error_flags = v[pos], v[pos + 1], v[pos + 2]
    pos += 3
    meters = list(v[pos:pos + 4]); pos += 4
    buffer_raw = list(v[pos:pos + 4]); pos += 4
    _require_finite(meters + buffer_raw, 'status')
    pwm = list(v[pos:pos + 4]); pos += 4
    session_id, event_counter, active_op_id = v[pos], v[pos + 1], v[pos + 2]
    pos += 3
    op_type, op_channel, op_state, op_reason = v[pos:pos + 4]
    pos += 4
    auto_stage, auto_progress, auto_mask, auto_done_mask, auto_state, auto_reason = v[pos:pos + 6]

    if any(value not in ROUTE_NAMES for value in route_state):
        raise ValueError('status contains an invalid per-Channel route state')
    if now_channel not in (0, 1, 2, 3, 0xff):
        raise ValueError('invalid active channel %d' % now_channel)
    if any(value not in (MOTION_IDLE, MOTION_SEND_OUT, MOTION_BEFORE_ON_USE,
                         MOTION_ON_USE, MOTION_BEFORE_PULL_BACK,
                         MOTION_PULL_BACK, MOTION_STOP_ON_USE) for value in motion):
        raise ValueError('status contains an invalid motion state')
    if any(value not in (0, 1) for value in present):
        raise ValueError('status contains an invalid filament-present flag')
    if any(value > 100 for value in buffer_pct):
        raise ValueError('status contains an invalid buffer percentage')
    if cal_valid_mask & ~0x0f or encoder_mask & ~0x0f or connected_mask & ~0x0f:
        raise ValueError('status contains an invalid channel mask')
    if any(value & ~0x07 for value in capture_mask):
        raise ValueError('status contains an invalid calibration capture mask')
    if op_type not in (0, OP_ENCODER_TEST, OP_CHANNEL_AUTOLOAD,
                        OP_FEED_TO_CONTACT, OP_FEED_DISTANCE,
                        OP_BUFFER_CALIBRATION, OP_CHANNEL_RETRACT):
        raise ValueError('status contains an invalid operation type')
    if op_channel not in (0, 1, 2, 3, 0xff):
        raise ValueError('status contains an invalid operation channel')
    if op_state not in (OP_STATE_IDLE, OP_STATE_RUNNING, OP_STATE_DONE,
                         OP_STATE_FAILED, OP_STATE_ABORTED):
        raise ValueError('status contains an invalid operation state')
    if auto_stage not in range(0, 7):
        raise ValueError('status contains an invalid automatic calibration stage')
    if auto_progress > 100 or auto_mask & ~0x0f or auto_done_mask & ~0x0f:
        raise ValueError('status contains invalid automatic calibration progress')
    if auto_state not in (OP_STATE_IDLE, OP_STATE_RUNNING, OP_STATE_DONE,
                           OP_STATE_FAILED, OP_STATE_ABORTED):
        raise ValueError('status contains an invalid automatic calibration state')

    loaded_mask = 0
    uncertain_mask = 0
    for channel, value in enumerate(route_state):
        if value == ROUTE_LOADED:
            loaded_mask |= 1 << channel
        elif value == ROUTE_UNCERTAIN:
            uncertain_mask |= 1 << channel
    auto_channel = op_channel if op_type == OP_BUFFER_CALIBRATION else 0xff
    return {
        'now_channel': now_channel,
        'route_state': route_state,
        'route_state_name': [ROUTE_NAMES[value] for value in route_state],
        'loaded_mask': loaded_mask,
        'uncertain_mask': uncertain_mask,
        'loaded_channels': [channel for channel in range(4) if loaded_mask & (1 << channel)],
        'motion': motion, 'present': present, 'buffer_pct': buffer_pct,
        'calibration_valid_mask': cal_valid_mask,
        'calibration_capture_mask': capture_mask,
        'encoder_io_mask': encoder_mask, 'connected_mask': connected_mask,
        'error_flags': error_flags,

        'route_uncertain_flag': bool(error_flags & 0x0002),
        'controller_fault_flags': int(error_flags & ~0x0002),
        'nvm_fault': bool(error_flags & 0x0001),
        'nvm_bad_page_mask': (error_flags >> 2) & 0x3fff,
        'meters': meters, 'buffer_raw': buffer_raw,
        'motor_pwm': pwm, 'session_id': session_id, 'event_counter': event_counter,
        'active_op_id': active_op_id, 'active_op_type': op_type,
        'active_op_channel': op_channel, 'active_op_state': op_state,
        'active_op_reason': OP_REASON.get(op_reason, 'reason_%d' % op_reason),
        'auto_calibration': {
            'active': auto_state == OP_STATE_RUNNING,
            'stage': auto_stage,
            'progress': auto_progress,
            'selected_mask': auto_mask,
            'done_mask': auto_done_mask,
            'state': auto_state,
            'failed': auto_state in (OP_STATE_FAILED, OP_STATE_ABORTED),
            'reason_code': auto_reason,
            'reason': OP_REASON.get(auto_reason, 'reason_%d' % auto_reason),
            'channel': auto_channel,
        },
    }

def parse_calibration(payload):
    if len(payload) != _CALIBRATION.size:
        raise ValueError('invalid calibration length')
    ch, mask, valid, polarity, current, offset, minimum, neutral, maximum = _CALIBRATION.unpack_from(payload)
    _require_finite(
        (current, offset, minimum, neutral, maximum), 'calibration')
    if ch >= 4 or mask & ~0x07 or valid not in (0, 1) or polarity not in (-1, 1):
        raise ValueError('invalid calibration metadata')
    if valid and not (minimum < neutral < maximum):
        raise ValueError('invalid calibrated buffer ordering')
    return {
        'channel': ch, 'capture_mask': mask, 'valid': bool(valid),
        'polarity': polarity, 'current_raw': current, 'offset': offset,
        'minimum': minimum, 'neutral': neutral, 'maximum': maximum,
    }

def parse_op_result(payload):
    if len(payload) != _OP_RESULT.size:
        raise ValueError('invalid operation result length')
    op_id, op_type, ch, state, reason, target_mm, measured_mm, duration_ms = _OP_RESULT.unpack_from(payload)
    _require_finite((target_mm, measured_mm), 'operation result')
    if op_type not in (0, OP_ENCODER_TEST, OP_CHANNEL_AUTOLOAD,
                        OP_FEED_TO_CONTACT, OP_FEED_DISTANCE,
                        OP_BUFFER_CALIBRATION, OP_CHANNEL_RETRACT):
        raise ValueError('invalid operation result type')
    if ch not in (0, 1, 2, 3, 0xff):
        raise ValueError('invalid operation result channel')
    if state not in (OP_STATE_IDLE, OP_STATE_RUNNING, OP_STATE_DONE,
                      OP_STATE_FAILED, OP_STATE_ABORTED):
        raise ValueError('invalid operation result state')
    return {
        'op_id': op_id, 'type': op_type, 'channel': ch, 'state': state,
        'reason_code': reason, 'reason': OP_REASON.get(reason, 'reason_%d' % reason),
        'target_mm': target_mm, 'measured_mm': measured_mm,
        'duration_ms': duration_ms, 'ok': state == OP_STATE_DONE,
    }

def parse_slot(payload):
    if len(payload) != _SLOT.size:
        raise ValueError('invalid slot length')
    ch, r, g, b, a, tmin, tmax, name, material = _SLOT.unpack_from(payload)
    if ch >= 4 or tmin > tmax:
        raise ValueError('invalid slot metadata')
    return {
        'channel': ch, 'color': '#%02X%02X%02X' % (r, g, b), 'alpha': a,
        'temperature_min': tmin, 'temperature_max': tmax,
        'name': name.split(b'\0', 1)[0].decode('utf-8', 'replace'),
        'material': material.split(b'\0', 1)[0].decode('ascii', 'replace'),
    }

def pack_slot(channel, color, name, tmin, tmax, material):
    channel = _require_int_range(channel, 'slot channel', 0, 3)
    if not isinstance(color, str):
        raise ValueError('color must be text')
    color = color.strip().lstrip('#')
    if len(color) != 6 or any(ch not in '0123456789abcdefABCDEF' for ch in color):
        raise ValueError('color must be RRGGBB')
    tmin = _require_int_range(tmin, 'minimum temperature', 0, 65535)
    tmax = _require_int_range(tmax, 'maximum temperature', 0, 65535)
    if tmin > tmax:
        raise ValueError('minimum temperature exceeds maximum temperature')
    if not isinstance(material, str):
        raise ValueError('material must be text')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in material):
        raise ValueError('material contains control characters')
    try:
        material_bytes = material.encode('ascii')
    except UnicodeEncodeError as exc:
        raise ValueError('material must contain ASCII characters only') from exc
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    name_bytes = _encode_utf8_field(name, 20, 'slot name')
    return _SLOT.pack(channel, r, g, b, 0xff, tmin, tmax,
                      name_bytes.ljust(20, b'\0'),
                      material_bytes[:8].ljust(8, b'\0'))

def pack_runtime_sync(values, rgb, policy=None, channel_retract_m=None,
                      channel_autoload_m=None, include_channel_autoload=True):
    channel_retract_m = list(channel_retract_m or (0.200, 0.200, 0.200, 0.200))
    channel_autoload_m = list(channel_autoload_m or (0.120, 0.120, 0.120, 0.120))
    if len(channel_retract_m) != 4:
        raise ValueError('runtime sync requires four Channel unload distances')
    if len(channel_autoload_m) != 4:
        raise ValueError('runtime sync requires four Channel autoload distances')
    ordered = (
        float(values[CONFIG_LOAD_PRESSURE_PCT]),
        float(values[CONFIG_LOAD_SPEED_MMS]),
        float(values[CONFIG_PULL_SPEED_MMS]),
        float(values[CONFIG_PULL_SPEED_END_MMS]),
        float(values[CONFIG_JAM_TIMEOUT_MS]),
        float(values[CONFIG_BEFORE_PULLBACK_TARGET_PCT]),
    ) + tuple(float(value) for value in channel_retract_m)
    _require_finite(ordered, 'runtime sync')
    for value in channel_retract_m:
        if value < 0.010 or value > 2.000:
            raise ValueError('Channel unload distance must be within 10..2000 mm')
    if include_channel_autoload:
        _require_finite(channel_autoload_m, 'Channel autoload distance')
        for value in channel_autoload_m:
            if value < 0.010 or value > 1.000:
                raise ValueError('Channel autoload distance must be within 10..1000 mm')
        ordered += tuple(float(value) for value in channel_autoload_m)
    if not isinstance(rgb, (tuple, list)) or len(rgb) != 3:
        raise ValueError('runtime RGB must contain exactly three values')
    red, green, blue = tuple(
        _require_int_range(value, 'runtime RGB value', 0, 255) for value in rgb)
    policy = dict(policy or {})
    standalone = 1 if policy.get('standalone', True) else 0
    autonomous_assist = 1 if policy.get('autonomous_assist', standalone) else 0
    autonomous_unload = 1 if policy.get('autonomous_unload', standalone) else 0
    policy_version = 1
    codec = (_RUNTIME_SYNC_CHANNEL_AUTOLOAD if include_channel_autoload
             else _RUNTIME_SYNC_LEGACY)
    return codec.pack(*(ordered + (
        red, green, blue, standalone, autonomous_assist,
        autonomous_unload, policy_version)))

LIGHTING_BUFFER_KEYS = ('minimum', 'neutral', 'maximum')
LIGHTING_STATUS_KEYS = (
    'idle', 'before_load', 'loading', 'active',
    'before_unload', 'unloading', 'redetect', 'error', 'empty', 'pullback')

def _pack_color(value, name):
    if not isinstance(value, str):
        raise ValueError('%s must be text' % name)
    value = value.strip().lstrip('#')
    if len(value) != 6 or any(ch not in '0123456789abcdefABCDEF' for ch in value):
        raise ValueError('%s must be RRGGBB' % name)
    return bytes((int(value[0:2], 16), int(value[2:4], 16),
                  int(value[4:6], 16)))

def pack_lighting(config):

    config = dict(config or {})
    payload = bytearray((1,))
    payload.extend(_pack_color(config.get('system_color', '#FFFFFF'),
                               'system colour'))
    payload.extend((
        _require_int_range(config.get('system_brightness', 255),
                           'system brightness', 0, 255),
        _require_int_range(config.get('filament_brightness', 96),
                           'filament brightness', 0, 255),
    ))
    buffer_colors = dict(config.get('buffer_colors') or {})
    for key in LIGHTING_BUFFER_KEYS:
        payload.extend(_pack_color(buffer_colors.get(key, '#000000'),
                                   'buffer %s colour' % key))
    status_colors = dict(config.get('status_colors') or {})
    for key in LIGHTING_STATUS_KEYS:
        payload.extend(_pack_color(status_colors.get(key, '#000000'),
                                   'status %s colour' % key))
    if len(payload) != 45:
        raise ValueError('invalid lighting payload size %d' % len(payload))
    return bytes(payload)

def pack_slots(slots):
    if len(slots) != 4:
        raise ValueError('exactly four slots are required')
    payload = bytearray()
    for channel, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ValueError('each slot must be an object')
        payload.extend(pack_slot(
            channel, slot.get('color', '#FFFFFF'),
            slot.get('name', 'Channel %d' % (channel + 1)),
            slot.get('temperature_min', 0), slot.get('temperature_max', 0),
            slot.get('material', '')))
    return bytes(payload)

def parse_snapshot(payload):
    status_size = _STATUS.size
    expected = status_size + 4 * _CALIBRATION.size
    if len(payload) != expected:
        raise ValueError('invalid snapshot payload length %d' % len(payload))
    status = parse_status(payload[:status_size])
    calibrations = []
    offset = status_size
    for channel in range(4):
        calibration = parse_calibration(payload[offset:offset + _CALIBRATION.size])
        if calibration['channel'] != channel:
            raise ValueError('snapshot calibration channel order mismatch')
        calibrations.append(calibration)
        offset += _CALIBRATION.size
    return {'status': status, 'calibration': calibrations}
