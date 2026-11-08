"""Minimal Bluetooth SDP client, enough to ask "which RFCOMM channel is service X on?".

The buds do not use a fixed channel, and bluetoothd does not expose the channel of a
vendor service, so we ask the buds' SDP server directly over L2CAP (PSM 1) with one
ServiceSearchAttributeRequest, then connect a plain RFCOMM socket to that channel.
"""

from __future__ import annotations

import socket
import struct
import uuid as uuidlib
from typing import Any

SDP_PSM = 1
PDU_ERROR = 0x01
PDU_SSA_REQUEST = 0x06
PDU_SSA_RESPONSE = 0x07
ATTR_PROTOCOL_DESCRIPTOR_LIST = 0x0004
UUID_RFCOMM = 0x0003

# Data element type descriptors
T_NIL, T_UINT, T_INT, T_UUID, T_TEXT, T_BOOL, T_SEQ, T_ALT, T_URL = range(9)


class SdpError(Exception):
    pass


class Uuid(int):
    """An SDP UUID normalised to its 128-bit value."""


_BASE = uuidlib.UUID("00000000-0000-1000-8000-00805f9b34fb").int


def parse_element(buf: bytes, pos: int = 0) -> tuple[Any, int]:
    """Parse one SDP data element. Sequences become lists, UUIDs become :class:`Uuid`."""
    if pos >= len(buf):
        raise SdpError("truncated data element")
    head = buf[pos]
    kind = head >> 3
    size_idx = head & 7
    pos += 1
    if kind == T_NIL:
        return None, pos
    if size_idx <= 4:
        size = (1, 2, 4, 8, 16)[size_idx]
    else:
        extra = (1, 2, 4)[size_idx - 5]
        if pos + extra > len(buf):
            raise SdpError("truncated length")
        size = int.from_bytes(buf[pos : pos + extra], "big")
        pos += extra
    if pos + size > len(buf):
        raise SdpError("truncated data element body")
    body = buf[pos : pos + size]
    end = pos + size
    if kind == T_UINT:
        return int.from_bytes(body, "big"), end
    if kind == T_INT:
        return int.from_bytes(body, "big", signed=True), end
    if kind == T_UUID:
        raw = int.from_bytes(body, "big")
        if size == 16:
            return Uuid(raw), end
        if size in (2, 4):
            return Uuid(_BASE | (raw << 96)), end
        raise SdpError(f"bad uuid size {size}")
    if kind in (T_TEXT, T_URL):
        return bytes(body), end
    if kind == T_BOOL:
        return bool(body[0]), end
    if kind in (T_SEQ, T_ALT):
        items = []
        p = 0
        while p < len(body):
            item, p = parse_element(body, p)
            items.append(item)
        return items, end
    raise SdpError(f"unknown data element type {kind}")


def uuid_element(value: str | uuidlib.UUID) -> bytes:
    u = uuidlib.UUID(str(value))
    return b"\x1c" + u.bytes


def build_ssa_request(uuid: str, tid: int, continuation: bytes = b"\x00", max_bytes: int = 0xFFFF) -> bytes:
    pattern = b"\x35" + bytes([17]) + uuid_element(uuid)
    attrs = b"\x35\x03\x09" + struct.pack(">H", ATTR_PROTOCOL_DESCRIPTOR_LIST)
    params = pattern + struct.pack(">H", max_bytes) + attrs + continuation
    return struct.pack(">BHH", PDU_SSA_REQUEST, tid, len(params)) + params


def parse_ssa_response(pdu: bytes) -> tuple[bytes, bytes]:
    """Split a response PDU into (attribute list bytes, continuation state)."""
    if len(pdu) < 5:
        raise SdpError("short SDP reply")
    if pdu[0] == PDU_ERROR:
        code = struct.unpack_from(">H", pdu, 5)[0] if len(pdu) >= 7 else -1
        raise SdpError(f"SDP error reply (code {code})")
    if pdu[0] != PDU_SSA_RESPONSE:
        raise SdpError(f"unexpected SDP PDU 0x{pdu[0]:02X}")
    param_len = struct.unpack_from(">H", pdu, 3)[0]
    params = pdu[5 : 5 + param_len]
    if len(params) < 3:
        raise SdpError("short SDP parameters")
    count = struct.unpack_from(">H", params, 0)[0]
    data = params[2 : 2 + count]
    if len(data) != count:
        raise SdpError("SDP attribute list shorter than announced")
    cont = params[2 + count :]
    return data, cont


def rfcomm_channel_from_attributes(data: bytes) -> int | None:
    """Find the RFCOMM channel in a (complete) attribute-lists sequence."""
    lists, _ = parse_element(data)
    if not isinstance(lists, list):
        raise SdpError("attribute lists is not a sequence")
    for record in lists:
        if not isinstance(record, list):
            continue
        for i in range(0, len(record) - 1, 2):
            if record[i] != ATTR_PROTOCOL_DESCRIPTOR_LIST:
                continue
            for proto in record[i + 1]:
                if (
                    isinstance(proto, list)
                    and len(proto) >= 2
                    and proto[0] == Uuid(_BASE | (UUID_RFCOMM << 96))
                    and isinstance(proto[1], int)
                ):
                    return proto[1]
    return None


def find_rfcomm_channel(addr: str, uuid: str, timeout: float = 5.0, sock: socket.socket | None = None) -> int:
    """Ask ``addr`` over L2CAP which RFCOMM channel serves ``uuid``.

    ``sock`` is an already-connected SDP socket; only tests pass one.
    """
    own = sock is None
    if sock is None:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    try:
        if own:
            sock.settimeout(timeout)
            sock.connect((addr, SDP_PSM))
        collected = bytearray()
        cont = b"\x00"
        for tid in range(1, 17):
            sock.send(build_ssa_request(uuid, tid, cont))
            data, cont = parse_ssa_response(sock.recv(4096))
            collected += data
            if not cont or cont == b"\x00":
                break
        else:
            raise SdpError("SDP continuation did not finish")
    finally:
        sock.close()
    if not collected:
        raise SdpError(f"{addr} has no service {uuid}")
    # Continued responses each carry a slice of one sequence; the first slice has the header.
    channel = rfcomm_channel_from_attributes(bytes(collected))
    if channel is None:
        raise SdpError(f"service {uuid} on {addr} has no RFCOMM channel")
    return channel
