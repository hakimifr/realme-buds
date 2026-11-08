"""Decoders for reply and push payloads. Pure functions, no I/O, easy to test."""

from __future__ import annotations

from typing import NamedTuple


class GestureEntry(NamedTuple):
    dev: int
    btn: int
    act: int
    fn: int


def _pairs(data: bytes) -> list[tuple[int, int]] | None:
    """``<count> (<a> <b>)*`` exactly filling ``data``, else None."""
    if not data:
        return None
    count = data[0]
    if len(data) != 1 + 2 * count:
        return None
    return [(data[1 + 2 * i], data[2 + 2 * i]) for i in range(count)]


def parse_pairs(payload: bytes) -> list[tuple[int, int]]:
    """Pair lists come with or without a leading status byte; accept both."""
    for off in (0, 1):
        pairs = _pairs(payload[off:])
        if pairs is not None:
            return pairs
    return []


def parse_battery(payload: bytes) -> dict[int, tuple[int, bool]]:
    """Component (1 left, 2 right, 3 case) -> (percent, charging). Level 0 means unknown."""
    out = {}
    for comp, raw in parse_pairs(payload):
        level = raw & 0x7F
        if level == 0 or level > 100:
            continue
        out[comp] = (level, bool(raw & 0x80))
    return out


def parse_wearing(payload: bytes) -> dict[int, int]:
    return dict(parse_pairs(payload))


def parse_features(payload: bytes) -> dict[int, bool]:
    """Reply to 0x010D: ``00 <count> (<id> <value>)*``. Id 0 pads slots the buds lack."""
    if len(payload) >= 2 and payload[0] == 0:
        pairs = _pairs(payload[1:])
        if pairs is not None:
            return {fid: bool(val) for fid, val in pairs if fid}
    return {}


def s8(byte: int) -> int:
    return byte - 256 if byte > 127 else byte


def u8(value: int) -> int:
    return max(-128, min(127, int(value))) & 0xFF


class DynamicBand(NamedTuple):
    band: int  # 1 low, 2 mid, 3 high
    value: int
    lo: int
    hi: int


def parse_dynamic_audio(payload: bytes) -> list[DynamicBand]:
    """Reply to 0x012C (or 0x0124): ``00 <count> (<lo> <hi> <value> <band>)*``, signed bytes."""
    if len(payload) < 2 or payload[0] != 0:
        return []
    count = payload[1]
    body = payload[2:]
    out = []
    for i in range(count):
        chunk = body[4 * i : 4 * i + 4]
        if len(chunk) < 4:
            break
        lo, hi, value, band = chunk
        out.append(DynamicBand(band, s8(value), s8(lo), s8(hi)))
    return out


def encode_dynamic_band(band: int, value: int, lo: int = -5, hi: int = 5) -> bytes:
    """Payload of 0x041B for one band."""
    return bytes([u8(lo), u8(hi), u8(value), band])


class CustomEq(NamedTuple):
    id: int
    name: str
    active: bool
    gain_min: int
    gain_max: int
    bands: tuple[tuple[int, int], ...]  # (centre Hz, gain dB)


def parse_custom_eq_list(payload: bytes) -> list[CustomEq]:
    """Reply to 0x0122: ``00 <count>`` then per preset
    ``<active> <gmin> <gmax> <id> <nameLen> <name> <nBands> (<hz u16 LE> <gain s8>)*``."""
    if len(payload) < 2 or payload[0] != 0:
        return []
    count = payload[1]
    pos = 2
    out = []
    for _ in range(count):
        if pos + 5 > len(payload):
            break
        active, gmin, gmax, eq_id, name_len = payload[pos : pos + 5]
        pos += 5
        name = payload[pos : pos + name_len].decode("utf-8", "replace")
        pos += name_len
        if pos >= len(payload):
            break
        n = payload[pos]
        pos += 1
        bands = []
        for _ in range(n):
            if pos + 3 > len(payload):
                break
            bands.append((int.from_bytes(payload[pos : pos + 2], "little"), s8(payload[pos + 2])))
            pos += 3
        out.append(CustomEq(eq_id, name, bool(active), s8(gmin), s8(gmax), tuple(bands)))
    return out


def encode_custom_eq(action: int, eq_id: int, name: str, bands: list[tuple[int, int]], gain_range=(-6, 6)) -> bytes:
    """Payload of 0x0418. Action 1 create (buds assign the id), 2 save and select, 3 delete."""
    raw = name.encode("utf-8")
    out = bytearray([action, u8(gain_range[0]), u8(gain_range[1]), eq_id, len(raw)])
    out += raw
    out.append(len(bands))
    for hz, gain in bands:
        out += int(hz).to_bytes(2, "little")
        out.append(u8(gain))
    return bytes(out)


def parse_gesture_table(payload: bytes) -> list[GestureEntry]:
    """Reply to 0x0108: ``<status> <count> (<dev> <btn> <act> <fn>)*``."""
    if len(payload) < 2 or payload[0] != 0:
        return []
    count = payload[1]
    body = payload[2 : 2 + 4 * count]
    if len(body) != 4 * count:
        return []
    return [GestureEntry(*body[i : i + 4]) for i in range(0, len(body), 4)]


def parse_firmware(payload: bytes) -> str:
    """Reply to 0x0105: ``00 <count>`` then ``deviceType,versionType,version`` triples as text.

    The triples whose versionType is 2 are the version numbers (left, right, case),
    joined by dots like the vendor app does. Falls back to the raw text.
    """
    if len(payload) < 2:
        return ""
    text = payload[2:].decode("utf-8", errors="replace").strip("\x00")
    parts = text.split(",")
    if len(parts) % 3 == 0 and parts and all(parts):
        versions = [parts[i + 2] for i in range(0, len(parts), 3) if parts[i + 1] == "2"]
        if versions:
            return ".".join(versions)
    return text


def parse_value_le(data: bytes) -> int:
    return int.from_bytes(data, "little")


def parse_anc_reply(payload: bytes) -> int | None:
    """Reply to ``0x010C 01 01``: ``00 01 01 <value LE>``."""
    if len(payload) >= 4 and payload[0] == 0 and payload[1:3] == b"\x01\x01":
        return parse_value_le(payload[3:])
    return None


def parse_hold_cycle_reply(payload: bytes) -> int | None:
    """Reply to ``0x010C 02 01``: ``00 02 01 <mask LE>``."""
    if len(payload) >= 4 and payload[0] == 0 and payload[1:3] == b"\x02\x01":
        return parse_value_le(payload[3:])
    return None


def parse_broadcast_codes(payload: bytes) -> list[int]:
    """Reply to 0x0200: ``00 <count> <codes>``."""
    if len(payload) >= 2 and payload[0] == 0:
        return list(payload[2 : 2 + payload[1]])
    return []


def parse_product_id(payload: bytes) -> str | None:
    """Reply to 0x0103: ``00`` + 3 bytes little-endian, shown as 6 hex digits."""
    if len(payload) >= 4 and payload[0] == 0:
        return f"{int.from_bytes(payload[1:4], 'little'):06X}"
    return None


def parse_bitmap(payload: bytes) -> set[int]:
    """Reply to the handshake: ``00`` then bytes read LSB first; returns the set bits."""
    bits = set()
    for i, byte in enumerate(payload[1:]):
        for b in range(8):
            if byte >> b & 1:
                bits.add(i * 8 + b)
    return bits


def parse_fit_test(body: bytes) -> dict[int, int]:
    """Push body after the event code: ``<side> <status> <side> <status>`` (1 left, 2 right)."""
    out = {}
    for i in range(0, len(body) - 1, 2):
        out[body[i]] = body[i + 1]
    return out


class HearingEntry(NamedTuple):
    side: int  # 1 left, 2 right
    band: int  # 1..6
    db: int


class HearingRecord(NamedTuple):
    entries: tuple[HearingEntry, ...]
    id: int
    name: str


def parse_hearing_record(payload: bytes) -> HearingRecord | None:
    """Reply to 0x0115: ``00 <type> <count> (<side> <band> <db s8>)*count <id u32 BE> <name>``."""
    if len(payload) < 3 or payload[0] != 0:
        return None
    count = payload[2]
    body = payload[3:]
    if len(body) < 3 * count + 4:
        return None
    entries = tuple(HearingEntry(body[3 * i], body[3 * i + 1], s8(body[3 * i + 2])) for i in range(count))
    uid = int.from_bytes(body[3 * count : 3 * count + 4], "big")
    name = body[3 * count + 4 :].decode("utf-8", errors="replace")
    return HearingRecord(entries, uid, name)


def encode_hearing_profile(kind: int, entries, uid: int = 0, name: str = "") -> bytes:
    """Payload of 0x040E types 1 (audition) and 3 (apply): ``<kind> <count> entries <id u32 BE> <name>``."""
    out = bytearray([kind, len(entries)])
    for e in entries:
        out += bytes([e[0], e[1], u8(e[2])])
    out += int(uid).to_bytes(4, "big")
    out += name.encode("utf-8")
    return bytes(out)


def encode_tone(side: int, band: int, db: int) -> bytes:
    """Payload of 0x040E that plays one test tone."""
    return bytes([3, 1, side, band, u8(db)])


def encode_ear_scan_start(action: int, uid: int) -> bytes:
    """Payload of 0x040D type 4: ``04 <1 start | 0 stop> <id u32 BE>``."""
    return bytes([4, action]) + int(uid).to_bytes(4, "big")


def encode_ear_scan_data(kind: int, uid: int, data: bytes) -> bytes:
    """Payload of 0x0415: ``<kind> <len u16 LE> <data> <id u32 BE>``."""
    return bytes([kind]) + len(data).to_bytes(2, "little") + data + int(uid).to_bytes(4, "big")


class EarScan(NamedTuple):
    uid: int
    data: bytes


def parse_ear_scan_event(body: bytes) -> EarScan | None:
    """Push body after event code 0x0E: ``<action> <len u16 LE> <data> <id u32 BE>``."""
    if len(body) < 3:
        return None
    n = int.from_bytes(body[1:3], "little")
    if len(body) < 3 + n + 4:
        return None
    return EarScan(int.from_bytes(body[3 + n : 7 + n], "big"), bytes(body[3 : 3 + n]))
