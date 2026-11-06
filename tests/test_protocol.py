import json
import struct

from realmebuds import protocol as p
from realmebuds.model import ANC_MAX, ANC_TRANSPARENCY


def test_golden_frames_from_public_captures():
    # Frames published in the QuickBuds / realme-buds-control protocol notes.
    assert p.build_frame(0x0100, 1).hex(" ") == "aa 07 00 00 00 01 01 00 00"
    assert p.build_frame(0x0404, 6, b"\x01\x01\x01").hex(" ") == "aa 0a 00 00 04 04 06 03 00 01 01 01"
    pkts = p.Framer().feed(bytes.fromhex("aa 0c 00 00 04 02 ff 05 00 03 01 01 08 00"))
    assert pkts == [p.Packet(0x0204, 0xFF, bytes.fromhex("03 01 01 08 00"))]


def test_length_is_leb128_for_large_payloads():
    frame = p.build_frame(0x8105, 3, bytes(124))
    assert frame[:3] == bytes.fromhex("aa 83 01")  # 131
    assert p.Framer().feed(frame)[0].payload == bytes(124)
    frame = p.build_frame(0x8122, 3, bytes(250))
    assert frame[:3] == bytes.fromhex("aa 81 02")  # 257


def test_framer_handles_byte_at_a_time_and_back_to_back_frames():
    a = p.build_frame(0x0106, 1)
    b = p.build_frame(0x8106, 1, b"\x00\x01\x01\x46")
    f = p.Framer()
    got = []
    for byte in a + b:
        got += f.feed(bytes([byte]))
    assert [x.cmd for x in got] == [0x0106, 0x8106]
    assert f.feed(a + b + a) and len(p.Framer().feed(a + b + a)) == 3


def test_framer_resyncs_after_garbage_and_keeps_it_for_the_trace():
    f = p.Framer()
    good = p.build_frame(0x0106, 1)
    got = f.feed(b"\x00\x11\x22" + good)
    assert len(got) == 1
    assert bytes(f.discarded) == b"\x00\x11\x22"


def test_framer_skips_a_false_header_byte_inside_noise():
    good = p.build_frame(0x0106, 7)
    # An 0xAA followed by a length that cannot be a valid frame must not eat the real one.
    got = p.Framer().feed(b"\xaa\x02\x00\x00" + good)
    assert [x.cmd for x in got] == [0x0106]


def test_fragmented_reply_is_reassembled():
    packet = struct.pack("<HBH", 0x8122, 9, 20) + bytes(range(20))
    chunks = [packet[i : i + 8] for i in range(0, len(packet), 8)]
    stream = b""
    for i, chunk in enumerate(chunks):
        kind = 1 if i == 0 else 3 if i == len(chunks) - 1 else 2
        body = bytes([kind, 0, i]) + chunk
        stream += b"\xaa" + p.encode_leb128(len(body)) + body
    got = p.Framer().feed(stream)
    assert got == [p.Packet(0x8122, 9, bytes(range(20)))]


def test_packet_helpers():
    pkt = p.Packet(0x8404, 6, b"\x0e")
    assert pkt.is_response and pkt.base_cmd == 0x0404 and pkt.status == 0x0E
    assert p.Packet(0x0204, 0xFF, b"").status is None


def test_mask_to_bytes_uses_as_few_bytes_as_needed():
    assert p.mask_to_bytes(ANC_MAX) == b"\x08"
    assert p.mask_to_bytes(ANC_MAX | ANC_TRANSPARENCY) == b"\x0a"
    assert p.mask_to_bytes(0x0800) == b"\x00\x08"
    assert p.mask_to_bytes(0) == b"\x00"


def test_answer_to_time_request():
    cmd, payload = p.answer_for(p.Packet(0x0500, 4, b"\x00\x00\x00\x00"), now=1_700_000_000)
    assert cmd == 0x8500
    assert payload == b"\x00" + struct.pack("<I", 1_700_000_000)


def test_answers_to_other_requests_from_the_buds():
    assert p.answer_for(p.Packet(0x050C, 1, b""))[1] == b"\x00\x01\x00"
    assert p.answer_for(p.Packet(0x051C, 1, b""))[1] == b"\x00\x00\x00"
    assert p.answer_for(p.Packet(0x051D, 1, b""))[1] == b"\x01"
    assert p.answer_for(p.Packet(0x0204, 255, b"\xf2\x00"))[1] == b"\x00\xf2"
    cmd, payload = p.answer_for(p.Packet(0x0204, 255, b"\xf4" + json.dumps({"cmd": 77}).encode()))
    assert payload == b'\x00\xf4{"cmd":77}'
    assert p.answer_for(p.Packet(0x0204, 255, b"\xf4not json"))[1] == b"\x01\xf4"


def test_no_answer_to_plain_pushes_replies_or_event_requests():
    assert p.answer_for(p.Packet(0x0204, 255, b"\x03\x01\x01\x08\x00")) is None
    assert p.answer_for(p.Packet(0x8106, 1, b"\x00")) is None
    assert p.answer_for(p.Packet(0x0501, 1, b"")) is None
    assert p.answer_for(p.Packet(0x0504, 1, b"\x01")) is None


def test_unknown_command_group_gets_status_1():
    assert p.answer_for(p.Packet(0x0900, 2, b""))[1] == b"\x01"
