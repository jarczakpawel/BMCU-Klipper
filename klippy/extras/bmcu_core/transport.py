# SPDX-License-Identifier: GPL-3.0-or-later

import struct

OP_SEND = 1
OP_PACKET = 2
OP_LINK = 3
OP_ERROR = 4
OP_KEEPALIVE = 5

CTRL_PAUSE = b'P'
CTRL_RESUME = b'R'
CTRL_RESUME_REQUIRED = b'Q'
CTRL_RELEASE_SERIAL = b'X'
CTRL_RECONNECT_SERIAL = b'C'
CTRL_SNAPSHOT = b'S'

_PREFIX = struct.Struct('<I')
_HEADER = struct.Struct('<BIII')
MAX_MESSAGE = 1024 * 1024

def encode_message(op, arg1=0, arg2=0, arg3=0, payload=b''):
    payload = bytes(payload)
    body = _HEADER.pack(
        int(op) & 0xff,
        int(arg1) & 0xffffffff,
        int(arg2) & 0xffffffff,
        int(arg3) & 0xffffffff) + payload
    if len(body) > MAX_MESSAGE:
        raise ValueError('BMCU IPC message is too large')
    return _PREFIX.pack(len(body)) + body

class MessageDecoder(object):
    def __init__(self, maximum=MAX_MESSAGE):
        self.maximum = int(maximum)
        self.buffer = bytearray()

    def clear(self):
        self.buffer[:] = b''

    def feed(self, data):
        self.buffer.extend(bytearray(data))
        messages = []
        while True:
            if len(self.buffer) < _PREFIX.size:
                break
            length = _PREFIX.unpack_from(self.buffer)[0]
            if length < _HEADER.size or length > self.maximum:
                self.clear()
                raise ValueError('invalid BMCU IPC frame length %d' % length)
            total = _PREFIX.size + length
            if len(self.buffer) < total:
                break
            body = bytes(self.buffer[_PREFIX.size:total])
            del self.buffer[:total]
            op, arg1, arg2, arg3 = _HEADER.unpack_from(body)
            messages.append((op, arg1, arg2, arg3, body[_HEADER.size:]))
        return messages
