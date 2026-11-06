from realmebuds import parsing


def test_battery_with_and_without_status_byte():
    body = bytes.fromhex("03 01 64 02 5a 03 50")
    expected = {1: (100, False), 2: (90, False), 3: (80, False)}
    assert parsing.parse_battery(body) == expected
    assert parsing.parse_battery(b"\x00" + body) == expected


def test_battery_charging_bit_and_unknown_level():
    assert parsing.parse_battery(bytes.fromhex("02 01 c8 03 00")) == {1: (72, True)}


def test_wearing():
    assert parsing.parse_wearing(bytes.fromhex("00 03 01 03 02 07 03 00")) == {1: 3, 2: 7, 3: 0}


def test_features_skip_padding_slots():
    assert parsing.parse_features(bytes.fromhex("00 03 04 01 06 00 00 00")) == {4: True, 6: False}
    assert parsing.parse_features(b"\x01") == {}


def test_gesture_table():
    raw = bytes.fromhex("00 02 01 01 04 08 02 06 02 1d")
    assert parsing.parse_gesture_table(raw) == [
        parsing.GestureEntry(1, 1, 4, 8),
        parsing.GestureEntry(2, 6, 2, 0x1D),
    ]
    assert parsing.parse_gesture_table(bytes.fromhex("00 02 01 01 04 08")) == []  # truncated


def test_firmware_joins_version_type_2_values():
    text = b"1,2,138,2,2,138,3,1,01,3,2,105"
    assert parsing.parse_firmware(b"\x00\x04" + text) == "138.138.105"
    assert parsing.parse_firmware(b"\x00\x01weird") == "weird"


def test_anc_values():
    assert parsing.parse_anc_reply(bytes.fromhex("00 01 01 02")) == 0x02
    assert parsing.parse_anc_reply(bytes.fromhex("00 01 01 00 08")) == 0x0800
    assert parsing.parse_anc_reply(bytes.fromhex("00 02 01 0a")) is None
    assert parsing.parse_hold_cycle_reply(bytes.fromhex("00 02 01 0a")) == 0x0A


def test_product_id_and_capabilities():
    assert parsing.parse_product_id(bytes.fromhex("00 14 54 06")) == "065414"
    assert parsing.parse_bitmap(bytes.fromhex("00 05 01")) == {0, 2, 8}


def test_dynamic_audio_bands_are_signed():
    raw = bytes.fromhex("00 03 fb 05 fe 01 fb 05 00 02 fb 05 05 03")
    assert parsing.parse_dynamic_audio(raw) == [
        parsing.DynamicBand(1, -2, -5, 5),
        parsing.DynamicBand(2, 0, -5, 5),
        parsing.DynamicBand(3, 5, -5, 5),
    ]
    assert parsing.encode_dynamic_band(1, -2) == bytes.fromhex("fb 05 fe 01")


def test_custom_eq_round_trip():
    bands = [(62, 3), (250, -2), (1000, 0), (4000, 6), (8000, -6), (16000, 1)]
    payload = parsing.encode_custom_eq(2, 5, "Mine", bands)
    assert payload[:5] == bytes([2, 0xFA, 6, 5, 4])
    # A list reply holds the same fields, with the "active" flag where the action was.
    reply = b"\x00\x01" + b"\x01" + payload[1:]
    [eq] = parsing.parse_custom_eq_list(reply)
    assert (eq.id, eq.name, eq.active, eq.gain_min, eq.gain_max) == (5, "Mine", True, -6, 6)
    assert list(eq.bands) == bands
