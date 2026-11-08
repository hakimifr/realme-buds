"""Wire format of the OPPO/OnePlus/realme earbud RFCOMM protocol.

Frame layout (all multi-byte fields little-endian, except the length which is LEB128):

    AA  <TotalLen>  00 00  <Cmd:2>  <Seq:1>  <PayLen:2>  <Payload>

TotalLen counts every byte after itself, so it is always ``7 + PayLen``.
Replies carry ``cmd | 0x8000`` and echo the request's Seq. Pushes from the buds use
Seq 0xFF.
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass

HEADER = 0xAA
PUSH_SEQ = 0xFF
RESPONSE_BIT = 0x8000

# Service UUIDs the buds advertise for their control channel, in the order to try.
SERVICE_UUIDS = (
    "0000079a-d102-11e1-9b23-00025b00a5a5",
    "00001107-d102-11e1-9b23-00025b00a5a5",
)

# Queries
CMD_HANDSHAKE = 0x0100
CMD_PRODUCT_ID = 0x0103
CMD_FIRMWARE = 0x0105
CMD_BATTERY = 0x0106
CMD_GESTURE_TABLE = 0x0108
CMD_WEARING = 0x0109
CMD_ANC_QUERY = 0x010C
CMD_FEATURE_QUERY = 0x010D
CMD_EQ_QUERY = 0x010F
CMD_HEARING_RECORD = 0x0115
CMD_EAR_SCAN_READ = 0x011E
CMD_CUSTOM_EQ_LIST = 0x0122
CMD_DYNAMIC_AUDIO_SINGLE = 0x0124
CMD_DYNAMIC_AUDIO_BANDS = 0x012C
CMD_TAP_SENSITIVITY_QUERY = 0x0133

# Subscriptions and pushes
CMD_BROADCAST_CODES = 0x0200
CMD_SUBSCRIBE = 0x0205
CMD_PUSH = 0x0204
CMD_REGISTER_EVENT = 0x0201

# Writes
CMD_FIND = 0x0400
CMD_FIT_TEST = 0x0405
CMD_HEARING_DETECT = 0x040D
CMD_HEARING_PARAMS = 0x040E
CMD_RESTORE_DATA = 0x0411
CMD_EAR_SCAN_WRITE = 0x0415
CMD_GESTURE_WRITE = 0x0401
CMD_FEATURE_WRITE = 0x0403
CMD_ANC_WRITE = 0x0404
CMD_EQ_SELECT = 0x0406
CMD_CUSTOM_EQ_WRITE = 0x0418
CMD_DYNAMIC_AUDIO_WRITE = 0x041B
CMD_SPATIAL_SOURCE = 0x041E
CMD_TAP_SENSITIVITY_WRITE = 0x042D

# Requests the buds send to us
CMD_TIME_REQUEST = 0x0500
CMD_APP_LIST_REQUEST = 0x050C
CMD_APP_CAPS_REQUEST = 0x051C
CMD_SPATIAL_MODE_REQUEST = 0x051D

# Push event codes (first payload byte of a 0x0204 push)
EV_BATTERY = 0x01
EV_WEARING = 0x02
EV_ANC = 0x03
EV_FIT_TEST = 0x04
EV_GAME_MODE = 0x05
EV_HEARING_STATUS = 0x08
EV_EAR_SCAN = 0x0E
EV_DUAL_DEVICES = 0x06
EV_GESTURE = 0xF1
EV_DEVICES_INFO = 0xF2
EV_DIAGNOSTIC = 0xF4

# Ack statuses
STATUS_OK = 0x00
STATUS_NOT_WORN = 0x0E


# Bit of the handshake's capability bitmap that enables each command we use. Bits 3, 7, 8,
# 10, 34, 38, 43 and 54 are all set in a capture of a real Air7 Pro. Table taken from the
# public protocol notes (MIT/GPL projects cited in docs/PROTOCOL.md).
CAPABILITY_BIT = {
    0x0108: 3, 0x0401: 3,
    0x0400: 5,
    0x0405: 9,
    0x0115: 19, 0x040D: 19, 0x040E: 19,
    0x0411: 25,
    0x011E: 30, 0x0415: 30,
    0x010D: 7, 0x0403: 7,
    0x010C: 8, 0x0404: 8,
    0x010F: 10, 0x0406: 10,
    0x0122: 34, 0x0418: 34,
    0x0124: 38, 0x041B: 38,
    0x041E: 43,
    0x012C: 54,
}


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class Packet:
    cmd: int
    seq: int
    payload: bytes

    @property
    def is_response(self) -> bool:
        return bool(self.cmd & RESPONSE_BIT)

    @property
    def base_cmd(self) -> int:
        return self.cmd & ~RESPONSE_BIT & 0xFFFF

    @property
    def status(self) -> int | None:
        """Status byte of a reply (0 = success), None when the payload is empty."""
        return self.payload[0] if self.payload else None

    def hex(self) -> str:
        return build_frame(self.cmd, self.seq, self.payload).hex(" ").upper()


def encode_leb128(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def build_frame(cmd: int, seq: int, payload: bytes = b"") -> bytes:
    if not 0 <= seq <= 0xFF:
        raise ValueError("seq out of range")
    if len(payload) > 0xFFFF - 7:
        raise ValueError("payload too long")
    body = struct.pack("<HHBH", 0, cmd, seq, len(payload)) + payload
    return bytes([HEADER]) + encode_leb128(len(body)) + body


class Framer:
    """Reassembles packets from the RFCOMM byte stream.

    Bytes that cannot start a valid frame are dropped (and kept in ``discarded`` so a
    trace can show them) instead of raising, because a stream that lost sync must
    recover on the next ``AA``.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._fragments = bytearray()
        self.discarded = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self._buf += data
        out: list[Packet] = []
        while True:
            pkt = self._take()
            if pkt is None:
                return out
            out.append(pkt)

    def _drop_header_byte(self) -> None:
        self.discarded.append(self._buf[0])
        del self._buf[0]

    def _take(self) -> Packet | None:
        buf = self._buf
        while True:
            start = buf.find(HEADER)
            if start < 0:
                self.discarded += buf
                buf.clear()
                return None
            if start:
                self.discarded += buf[:start]
                del buf[:start]
            # LEB128 length, at most 3 bytes for a 16-bit payload.
            total = 0
            shift = 0
            pos = 1
            while True:
                if pos >= len(buf):
                    return None
                b = buf[pos]
                pos += 1
                total |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80 or shift > 21:
                    break
            if shift > 21 or total < 3:
                self._drop_header_byte()
                continue
            if len(buf) < pos + total:
                return None
            body = bytes(buf[pos : pos + total])
            # Body: <ctrl> 00 [<frag seq> when fragmented] <packet or packet slice>.
            # The low two bits of ctrl: 0 whole packet, 1 first, 2 middle, 3 last slice.
            ctrl, zero = body[0], body[1]
            frag = ctrl & 0x03
            header = 3 if frag else 2
            if zero != 0 or total < header:
                self._drop_header_byte()
                continue
            chunk = body[header:]
            if frag in (1, 2):
                if frag == 1:
                    self._fragments.clear()
                self._fragments += chunk
                del buf[: pos + total]
                continue
            if frag == 3:
                chunk = bytes(self._fragments) + chunk
                self._fragments.clear()
            if len(chunk) < 5:
                self._drop_header_byte()
                continue
            cmd, seq, paylen = struct.unpack_from("<HBH", chunk)
            if paylen != len(chunk) - 5:
                self._drop_header_byte()
                continue
            del buf[: pos + total]
            return Packet(cmd, seq, chunk[5:])


def mask_to_bytes(mask: int) -> bytes:
    """A bitmask in as few little-endian bytes as it needs (at least one)."""
    return mask.to_bytes(max(1, (mask.bit_length() + 7) // 8), "little")


def answer_for(pkt: Packet, now: float | None = None) -> tuple[int, bytes] | None:
    """The reply the buds expect to a request or push of theirs, or None.

    Some firmwares stop answering and reset the link when their time request goes
    unanswered, so this must be called for every packet that is not a reply to us.
    Returns ``(cmd, payload)``; the caller echoes the request's seq.
    """
    if pkt.is_response:
        return None
    cmd = pkt.cmd
    if cmd == CMD_TIME_REQUEST:
        return cmd | RESPONSE_BIT, b"\x00" + struct.pack("<I", int(now if now is not None else time.time()) & 0xFFFFFFFF)
    if cmd == CMD_APP_LIST_REQUEST:
        return cmd | RESPONSE_BIT, b"\x00\x01\x00"
    if cmd == CMD_APP_CAPS_REQUEST:
        return cmd | RESPONSE_BIT, b"\x00\x00\x00"
    if cmd == CMD_SPATIAL_MODE_REQUEST:
        return cmd | RESPONSE_BIT, b"\x01"
    if cmd == CMD_PUSH and pkt.payload:
        code = pkt.payload[0]
        if code == EV_DEVICES_INFO:
            return cmd | RESPONSE_BIT, bytes([0x00, EV_DEVICES_INFO])
        if code == EV_DIAGNOSTIC:
            try:
                req = json.loads(pkt.payload[1:].decode("utf-8"))
                echoed = {"cmd": req["cmd"]}
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                return cmd | RESPONSE_BIT, bytes([0x01, EV_DIAGNOSTIC])
            return cmd | RESPONSE_BIT, bytes([0x00, EV_DIAGNOSTIC]) + json.dumps(echoed, separators=(",", ":")).encode()
        return None
    if cmd >> 8 not in (0x01, 0x02, 0x03, 0x04, 0x05, 0x0F):
        return cmd | RESPONSE_BIT, b"\x01"
    return None
