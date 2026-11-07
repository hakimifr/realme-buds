"""Bytes captured from a real realme Buds Air7 Pro (user's `--trace status`, 2026-11-07).

These are the only tests here that come from real hardware, so they are the ones to
extend when something new is captured.
"""

import struct

from realmebuds import parsing, protocol
from realmebuds.protocol import Framer, Packet

# Pushes the buds sent unprompted right after the connection opened. Note the seq numbers:
# real pushes are numbered (0x18, 0x19, 0x1A), not 0xFF.
PUSH_WEARING = bytes.fromhex("AA 0F 00 00 04 02 18 08 00 02 03 01 03 02 03 03 04")
PUSH_BATTERY = bytes.fromhex("AA 0F 00 00 04 02 19 08 00 01 03 01 50 02 64 03 00")

HANDSHAKE_REPLY_AND_TIME_REQUEST = bytes.fromhex(
    "AA 10 00 00 00 81 01 09 00 00 BF 4F 5A E6 65 DC 40 04 AA 0B 00 00 00 05 1B 04 00 AC 01 00 00"
)
OUR_TIME_ANSWER = bytes.fromhex("AA 0C 00 00 00 85 1B 05 00 00 BE 6D EF 6A")
PRODUCT_ID_REPLY = bytes.fromhex("AA 0B 00 00 03 81 02 04 00 00 12 4C 06")
BROADCAST_CODES_REPLY = bytes.fromhex("AA 0B 00 00 00 82 03 04 00 00 02 02 01")
SUBSCRIBE_REQUEST = bytes.fromhex("AA 0A 00 00 05 02 04 03 00 02 01 02")


def test_numbered_pushes_parse_and_carry_state():
    wear, battery = Framer().feed(PUSH_WEARING + PUSH_BATTERY)
    assert (wear.cmd, wear.seq) == (0x0204, 0x18)
    assert wear.payload[0] == protocol.EV_WEARING
    assert parsing.parse_wearing(wear.payload[1:]) == {1: 3, 2: 3, 3: 4}
    assert battery.payload[0] == protocol.EV_BATTERY
    # Case level 0 means "not known" (lid closed), so it is left out.
    assert parsing.parse_battery(battery.payload[1:]) == {1: (80, False), 2: (100, False)}
    assert protocol.answer_for(wear) is None and protocol.answer_for(battery) is None


def test_handshake_reply_and_time_request_arrive_in_one_chunk():
    reply, request = Framer().feed(HANDSHAKE_REPLY_AND_TIME_REQUEST)
    assert reply == Packet(0x8100, 1, bytes.fromhex("00 BF 4F 5A E6 65 DC 40 04"))
    assert request == Packet(0x0500, 0x1B, bytes.fromhex("AC 01 00 00"))
    bits = parsing.parse_bitmap(reply.payload)
    assert len(bits) == 32 and 58 in bits
    # Everything the CLI and GUI use is advertised: gestures, features, ANC, EQ select,
    # custom EQ, bass engine (single and multi band) and the spatial source switch.
    assert {3, 7, 8, 10, 34, 38, 43, 54} <= bits
    assert 56 not in bits  # no batch-query support, which is fine: it is never used


def test_our_answer_to_the_time_request_matches_what_was_sent():
    _, request = Framer().feed(HANDSHAKE_REPLY_AND_TIME_REQUEST)
    cmd, payload = protocol.answer_for(request, now=0x6AEF6DBE)
    assert protocol.build_frame(cmd, request.seq, payload) == OUR_TIME_ANSWER


def test_product_id_and_broadcast_codes():
    (pid,) = Framer().feed(PRODUCT_ID_REPLY)
    assert parsing.parse_product_id(pid.payload) == "064C12"
    (codes,) = Framer().feed(BROADCAST_CODES_REPLY)
    assert parsing.parse_broadcast_codes(codes.payload) == [2, 1]  # wearing and battery only


def test_subscribe_request_is_count_first_with_only_offered_codes():
    assert protocol.build_frame(0x0205, 4, bytes([2, 1, 2])) == SUBSCRIBE_REQUEST
