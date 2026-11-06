import socket
import struct
import threading

import pytest

from realmebuds import sdp, transport
from realmebuds.protocol import SERVICE_UUIDS


def seq(*items: bytes) -> bytes:
    body = b"".join(items)
    return b"\x35" + bytes([len(body)]) + body


def uuid16(v: int) -> bytes:
    return b"\x19" + struct.pack(">H", v)


def uint8(v: int) -> bytes:
    return b"\x08" + bytes([v])


def uint16(v: int) -> bytes:
    return b"\x09" + struct.pack(">H", v)


def attribute_lists(channel: int) -> bytes:
    protocols = seq(seq(uuid16(0x0100)), seq(uuid16(0x0003), uint8(channel)))
    return seq(seq(uint16(0x0004), protocols))


def response(data: bytes, cont: bytes = b"\x00", tid: int = 1) -> bytes:
    params = struct.pack(">H", len(data)) + data + cont
    return struct.pack(">BHH", 0x07, tid, len(params)) + params


def test_request_matches_the_layout_other_clients_send():
    req = sdp.build_ssa_request(SERVICE_UUIDS[0], tid=1)
    assert req[:5] == bytes.fromhex("06 0001 001b")
    assert req[5:8] == bytes.fromhex("35 11 1c")
    assert req[8:24] == bytes.fromhex("0000079a-d102-11e1-9b23-00025b00a5a5".replace("-", ""))
    assert req[24:] == bytes.fromhex("ffff 35 03 09 0004 00")


def test_finds_rfcomm_channel():
    assert sdp.rfcomm_channel_from_attributes(attribute_lists(15)) == 15


def test_ignores_records_without_rfcomm():
    only_l2cap = seq(seq(uint16(0x0004), seq(seq(uuid16(0x0100)))))
    assert sdp.rfcomm_channel_from_attributes(only_l2cap) is None


def test_long_length_forms_and_128_bit_uuids_parse():
    uuid128 = b"\x1c" + bytes.fromhex("0000079a-d102-11e1-9b23-00025b00a5a5".replace("-", ""))
    big = b"\x36" + struct.pack(">H", len(uuid128) + 2) + uuid128 + b"\x08\x05"
    value, end = sdp.parse_element(big)
    assert end == len(big) and isinstance(value[0], sdp.Uuid) and value[1] == 5


def test_error_reply_and_truncation_are_reported():
    with pytest.raises(sdp.SdpError):
        sdp.parse_ssa_response(struct.pack(">BHHH", 0x01, 1, 2, 3))
    with pytest.raises(sdp.SdpError):
        sdp.parse_element(b"\x35\x10\x08")


def test_lookup_follows_continuation_state():
    data = attribute_lists(22)
    first, second = data[:7], data[7:]
    ours, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    requests = []

    def serve():
        requests.append(server.recv(4096))
        server.send(response(first, cont=b"\x02\xab\xcd", tid=1))
        requests.append(server.recv(4096))
        server.send(response(second, tid=2))

    t = threading.Thread(target=serve)
    t.start()
    assert sdp.find_rfcomm_channel("AA:BB:CC:DD:EE:FF", SERVICE_UUIDS[0], sock=ours) == 22
    t.join()
    assert requests[1].endswith(b"\x02\xab\xcd")  # the continuation state is echoed back


def test_lookup_without_service_raises():
    ours, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    threading.Thread(target=lambda: (server.recv(4096), server.send(response(b"\x35\x00")))).start()
    with pytest.raises(sdp.SdpError):
        sdp.find_rfcomm_channel("AA:BB:CC:DD:EE:FF", SERVICE_UUIDS[0], sock=ours)


DEVICES = """Device A8:E6:E8:92:C1:25 realme Buds Air7 Pro
Device 11:22:33:44:55:66 CX Wireless mouse
[NEW] Device not-an-address ignored
"""

INFO_BUDS = """Device A8:E6:E8:92:C1:25 (public)
\tName: realme Buds Air7 Pro
\tAlias: realme Buds Air7 Pro
\tPaired: yes
\tConnected: yes
\tUUID: Audio Sink                (0000110b-0000-1000-8000-00805f9b34fb)
\tUUID: Vendor specific           (0000079a-d102-11e1-9b23-00025b00a5a5)
"""

INFO_MOUSE = """Device 11:22:33:44:55:66 (public)
\tAlias: CX Wireless mouse
\tPaired: yes
\tConnected: no
"""


def test_parse_device_list():
    assert transport.parse_device_list(DEVICES) == [
        ("A8:E6:E8:92:C1:25", "realme Buds Air7 Pro"),
        ("11:22:33:44:55:66", "CX Wireless mouse"),
    ]


def test_parse_device_info():
    d = transport.parse_device_info(INFO_BUDS, "a8:e6:e8:92:c1:25")
    assert (d.addr, d.name, d.paired, d.connected, d.has_control_service) == (
        "A8:E6:E8:92:C1:25", "realme Buds Air7 Pro", True, True, True)
    m = transport.parse_device_info(INFO_MOUSE, "11:22:33:44:55:66")
    assert not m.connected and not m.looks_like_realme_buds()


def _patch_bluetoothctl(monkeypatch, connected=True):
    def fake(*args, timeout=5.0):
        if args == ("devices",):
            return DEVICES
        if args[0] == "info":
            if args[1].startswith("A8"):
                return INFO_BUDS if connected else INFO_BUDS.replace("Connected: yes", "Connected: no")
            return INFO_MOUSE
        raise AssertionError(args)

    monkeypatch.setattr(transport, "_bluetoothctl", fake)


def test_pick_device_finds_the_connected_buds(monkeypatch):
    _patch_bluetoothctl(monkeypatch)
    assert transport.pick_device().addr == "A8:E6:E8:92:C1:25"


def test_pick_device_explains_when_buds_are_not_connected_here(monkeypatch):
    _patch_bluetoothctl(monkeypatch, connected=False)
    with pytest.raises(transport.TransportError, match="no earbuds are connected to this computer"):
        transport.pick_device()


def test_pick_device_validates_explicit_address():
    with pytest.raises(transport.TransportError):
        transport.pick_device("nonsense")
    assert transport.pick_device("a8:e6:e8:92:c1:25").addr == "A8:E6:E8:92:C1:25"
