"""The Bluetooth LE (GATT) transport against a mock BlueZ on a private D-Bus (tests/fake_bluez.py).

Nothing here touches real Bluetooth. These tests show that realmebuds/ble.py speaks BlueZ's D-Bus
API consistently and that the protocol runs over it, not that real buds expose these services.
"""

import json
import socket
import threading
import types

import pytest

pytest.importorskip("gi")
from tests import fake_bluez  # noqa: E402

if not fake_bluez.have_dbus_daemon():
    pytest.skip("dbus-daemon is not installed, so there is no private bus to serve the fake BlueZ on", allow_module_level=True)

from realmebuds import ble, cli, protocol, transport  # noqa: E402
from realmebuds.client import Buds, LinkClosed  # noqa: E402
from realmebuds.sdp import SdpError  # noqa: E402
from realmebuds.session import connect_buds  # noqa: E402
from realmebuds.transport import Device, TransportError  # noqa: E402
from tests.fake_bluez import DEFAULT_SERVICE, NOTIFY_CHAR, WRITE_CHAR, FakeBluez  # noqa: E402

ADDR = "AA:BB:CC:DD:EE:FF"
OTHER_SERVICE = "0000079c-d102-11e1-9b23-00025b00a5a5"


@pytest.fixture(scope="module")
def bus():
    try:
        private = fake_bluez.PrivateBus()
    except (OSError, RuntimeError) as e:
        pytest.skip(f"could not start a private dbus-daemon: {e}")
    yield private
    private.stop()


@pytest.fixture
def env(bus, monkeypatch):
    monkeypatch.setenv(ble.DBUS_ADDRESS_ENV, bus.address)
    return bus


@pytest.fixture
def make(env):
    started: list[FakeBluez] = []

    def factory(**kw) -> FakeBluez:
        fake = FakeBluez(env.address, **kw).start()
        started.append(fake)
        return fake

    yield factory
    for fake in started:
        fake.stop()


@pytest.fixture
def fake(make):
    return make()


def frame(cmd=0x0204, seq=0x30, payload=b"\x01\x02\x46\x01\x4b"):
    return protocol.build_frame(cmd, seq, payload)


def read_all_packets(sock, count, timeout=3.0):
    framer = protocol.Framer()
    sock.settimeout(0.2)
    packets = []
    import time

    end = time.monotonic() + timeout
    while len(packets) < count and time.monotonic() < end:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        assert data, "link closed early"
        packets += framer.feed(data)
    return packets


# -- a whole session over the fake link -------------------------------------------------------


def test_full_session_runs_over_gatt(fake):
    sock = ble.connect_ble(ADDR)
    buds = Buds(sock, init_delay=0.05)
    buds.start()
    try:
        buds.initialise()
        buds.sync()
        assert buds.state.product_id == "064C12"
        assert buds.state.battery[1][0] == 70 and buds.state.anc == fake.buds.anc
        buds.set_anc_mode("off")
        assert fake.buds.anc == buds.state.anc == 1
        buds.set_anc_mode("max")
        assert buds.state.anc == fake.buds.anc
        # The fake buds saw exactly what we sent, reassembled from the GATT writes.
        assert any(p.cmd == protocol.CMD_ANC_WRITE for p in fake.buds.received)
        # Writes are ATT requests with the vendor's characteristic, in pieces that fit 20 bytes.
        assert {w[0] for w in fake.writes} == {WRITE_CHAR}
        assert {w[2] for w in fake.writes} == {"request"}
        assert max(len(w[1]) for w in fake.writes) <= 20
        assert fake.start_notify_calls == [NOTIFY_CHAR]
    finally:
        buds.close()
    assert fake.disconnect_calls == 1 and fake.stop_notify_calls == [NOTIFY_CHAR]


def test_replies_cut_into_tiny_notifications_still_work(make):
    fake = make(notify_chunk=3)
    buds = Buds(ble.connect_ble(ADDR), init_delay=0.05)
    buds.start()
    try:
        buds.initialise()
        buds.sync()
        assert buds.state.product_id == "064C12" and buds.state.gestures
    finally:
        buds.close()


def test_the_buds_time_request_is_answered_over_gatt(fake):
    buds = Buds(ble.connect_ble(ADDR), init_delay=0.05)
    buds.start()
    try:
        assert fake.buds.time_answered.wait(3)
    finally:
        buds.close()


# -- writes -----------------------------------------------------------------------------------


def test_a_frame_longer_than_the_att_payload_is_split_into_consecutive_writes(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    data = frame(payload=bytes(range(60)))
    assert len(data) > 60
    sock.sendall(data)
    sizes = [len(v) for _, v, _ in fake.writes]
    assert sizes == [20, 20, 20, len(data) - 60]
    assert fake.written() == data
    sock.close()


def test_a_short_frame_is_one_write(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    sock.sendall(frame(payload=b"\x00"))
    assert len(fake.writes) == 1
    sock.close()


def test_write_without_response_is_used_when_requests_are_not_offered(make):
    layout = [(DEFAULT_SERVICE, [(WRITE_CHAR, ["write-without-response"]), (NOTIFY_CHAR, ["notify"])])]
    fake = make(layout=layout, attach_buds=False)
    sock = ble.connect_ble(ADDR)
    assert sock.write_type == "command"
    data = frame(payload=bytes(range(50)))
    sock.sendall(data)
    assert {k for _, _, k in fake.writes} == {"command"} and fake.written() == data
    sock.close()


def test_mtu_from_bluez_sets_the_chunk_size(make):
    fake = make(mtu=100, att_payload=97, attach_buds=False)
    sock = ble.connect_ble(ADDR)
    assert sock.mtu_payload == 97
    data = frame(payload=bytes(150))
    sock.sendall(data)
    assert [len(v) for _, v, _ in fake.writes] == [97, len(data) - 97]
    sock.close()


def test_an_unusably_small_mtu_value_keeps_the_default(make):
    make(mtu=23, attach_buds=False)
    sock = ble.connect_ble(ADDR)
    assert sock.mtu_payload == 20
    sock.close()


def test_a_failed_write_is_a_connection_error_the_client_understands(make):
    fake = make(attach_buds=False)
    fake.fail_writes = ("org.bluez.Error.Failed", "Operation failed with ATT error: 0x0e")
    sock = ble.connect_ble(ADDR)
    with pytest.raises(OSError, match="write failed"):
        sock.sendall(frame())
    sock.close()
    with pytest.raises(OSError):
        sock.sendall(frame())


def test_a_characteristic_that_cannot_be_written_is_refused(make):
    layout = [(DEFAULT_SERVICE, [(WRITE_CHAR, ["read"]), (NOTIFY_CHAR, ["notify"])])]
    make(layout=layout)
    with pytest.raises(TransportError, match="cannot be written"):
        ble.connect_ble(ADDR)


# -- reading ----------------------------------------------------------------------------------


def test_notifications_arrive_in_order(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    sock.settimeout(2)
    for chunk in (b"\xaa\x01", b"\x02\x03", b"\x04"):
        fake.notify(chunk)
    got = b""
    while len(got) < 5:
        got += sock.recv(4096)
    assert got == b"\xaa\x01\x02\x03\x04"
    sock.close()


def test_a_frame_split_across_notifications_is_reassembled_by_the_framer(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    data = frame(payload=bytes(range(40)))
    for i in range(0, len(data), 7):
        fake.notify(data[i : i + 7])
    (pkt,) = read_all_packets(sock, 1)
    assert pkt.cmd == 0x0204 and pkt.payload == bytes(range(40))
    sock.close()


def test_two_frames_in_one_notification(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    a, b = frame(seq=1, payload=b"\x01"), frame(seq=2, payload=b"\x02\x03")
    fake.notify(a + b)
    first, second = read_all_packets(sock, 2)
    assert (first.seq, second.seq) == (1, 2)
    sock.close()


def test_recv_hands_out_at_most_bufsize_and_keeps_the_rest(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    sock.settimeout(2)
    fake.notify(b"abcdef")
    assert sock.recv(4) == b"abcd"
    assert sock.recv(4) == b"ef"
    sock.close()


def test_recv_times_out_like_a_socket(make):
    make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    sock.settimeout(0.1)
    assert sock.gettimeout() == 0.1
    with pytest.raises(socket.timeout):
        sock.recv(4096)
    sock.settimeout(0)
    with pytest.raises(socket.timeout):
        sock.recv(4096)
    sock.close()


def test_a_characteristic_we_did_not_subscribe_to_stays_silent(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    write_path = next(p for p, c in fake.chars.items() if c.uuid == WRITE_CHAR)
    fake.notify_on(write_path, b"not for us")
    fake.notify(b"\xaa")
    sock.settimeout(1)
    assert sock.recv(10) == b"\xaa"  # the signal for the other characteristic was ignored, not queued
    sock.close()


# -- the link goes away -----------------------------------------------------------------------


def test_disconnect_closes_the_socket(fake):
    sock = ble.connect_ble(ADDR)
    sock.settimeout(2)
    fake.drop_link()
    while sock.recv(4096):  # drain whatever arrived first
        pass
    assert sock.recv(4096) == b""
    with pytest.raises(OSError):
        sock.sendall(frame())
    sock.close()


def test_a_blocked_recv_wakes_up_when_the_link_drops(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    sock.settimeout(None)
    result = []
    t = threading.Thread(target=lambda: result.append(sock.recv(10)))
    t.start()
    fake.drop_link()
    t.join(3)
    assert result == [b""]
    sock.close()


def test_the_client_notices_a_dropped_link(fake):
    buds = Buds(ble.connect_ble(ADDR), init_delay=0.05)
    events = []
    buds.add_listener(lambda ev, st: events.append(ev))
    buds.start()
    try:
        buds.initialise()
        fake.drop_link()
        assert fake.wait_for(lambda: not buds.state.connected)
        assert "disconnected" in events
        with pytest.raises(LinkClosed):
            buds.request(protocol.CMD_BATTERY)
    finally:
        buds.close()


def test_the_device_being_removed_closes_the_socket(make):
    fake = make(attach_buds=False)
    sock = ble.connect_ble(ADDR)
    sock.settimeout(2)
    fake.remove_device()
    assert sock.recv(10) == b""
    sock.close()


def test_close_is_idempotent_and_ends_reads(fake):
    sock = ble.connect_ble(ADDR)
    sock.shutdown(socket.SHUT_RDWR)
    sock.close()
    sock.close()
    sock.settimeout(1)
    assert sock.recv(10) == b""
    assert fake.disconnect_calls == 1


def test_close_leaves_a_device_alone_that_was_already_connected(make):
    fake = make(connected=True)
    sock = ble.connect_ble(ADDR)
    assert fake.connect_calls == 0
    sock.close()
    assert fake.disconnect_calls == 0 and fake.stop_notify_calls == [NOTIFY_CHAR]


# -- finding the services ---------------------------------------------------------------------


def test_waits_for_services_to_resolve(make):
    fake = make(resolve_delay=0.3)
    sock = ble.connect_ble(ADDR)
    assert fake.connect_calls == 1 and fake.start_notify_calls
    sock.close()


def test_times_out_when_services_never_resolve(make):
    fake = make()
    fake.never_resolve = True
    with pytest.raises(TransportError, match="timed out waiting for BlueZ to read the GATT services"):
        ble.connect_ble(ADDR, timeout=0.5)
    assert fake.disconnect_calls == 1  # a half-open link is not left behind


def test_missing_service_says_the_buds_may_be_classic_only(make):
    make(layout=[("0000180f-0000-1000-8000-00805f9b34fb", [("00002a19-0000-1000-8000-00805f9b34fb", ["read", "notify"])])])
    with pytest.raises(TransportError) as info:
        ble.connect_ble(ADDR)
    text = str(info.value)
    assert "Classic-only" in text and DEFAULT_SERVICE in text and "0000180f" in text and "--service-uuid" in text


def test_a_service_without_the_write_characteristic_is_reported(make):
    make(layout=[(DEFAULT_SERVICE, [(NOTIFY_CHAR, ["notify"])])])
    with pytest.raises(TransportError, match="no write characteristic"):
        ble.connect_ble(ADDR)


def test_a_service_without_anything_to_listen_on_is_reported(make):
    make(layout=[(DEFAULT_SERVICE, [(WRITE_CHAR, ["write"])])])
    with pytest.raises(TransportError, match="no characteristic to receive replies"):
        ble.connect_ble(ADDR)


def test_service_uuid_override_selects_another_service(make):
    # Writes go to 0100079a inside the model's service, notifications come from 0200079a in the default one.
    layout = [
        (OTHER_SERVICE, [(WRITE_CHAR, ["write"])]),
        (DEFAULT_SERVICE, [(NOTIFY_CHAR, ["notify"])]),
    ]
    fake = make(layout=layout)
    with pytest.raises(TransportError):
        ble.connect_ble(ADDR, DEFAULT_SERVICE)  # the default service has no write characteristic here
    buds = Buds(ble.connect_ble(ADDR, OTHER_SERVICE.upper()), init_delay=0.05)
    buds.start()
    try:
        buds.initialise()
        assert buds.state.product_id == "064C12"
    finally:
        buds.close()
    assert fake.start_notify_calls.count(NOTIFY_CHAR) >= 1


def test_without_the_known_notify_characteristic_any_notifying_one_in_the_service_is_used(make):
    layout = [(DEFAULT_SERVICE, [(WRITE_CHAR, ["write"]), ("0300079a-d102-11e1-9b23-00025b00a5a5", ["notify"])])]
    fake = make(layout=layout, attach_buds=False)
    sock = ble.connect_ble(ADDR)
    sock.settimeout(2)
    fake.notify(b"\xaa", uuid="0300079a-d102-11e1-9b23-00025b00a5a5")
    assert sock.recv(10) == b"\xaa"
    sock.close()


def test_frames_from_two_notifying_characteristics_do_not_mix(make):
    layout = [
        (OTHER_SERVICE, [(WRITE_CHAR, ["write"]), (NOTIFY_CHAR, ["notify"])]),
        (DEFAULT_SERVICE, [(NOTIFY_CHAR, ["notify"])]),
    ]
    fake = make(layout=layout, attach_buds=False)
    sock = ble.connect_ble(ADDR, OTHER_SERVICE)
    assert len(fake.start_notify_calls) == 2
    a, b = frame(seq=1, payload=bytes(range(30))), frame(seq=2, payload=bytes(range(100, 130)))
    # Each frame comes in two halves and the halves interleave between the characteristics.
    notify_a = lambda d: fake.notify(d, nth=0)  # noqa: E731
    notify_b = lambda d: fake.notify(d, nth=1)  # noqa: E731
    notify_a(a[:10])
    notify_b(b[:10])
    notify_a(a[10:])
    notify_b(b[10:])
    packets = read_all_packets(sock, 2)
    assert sorted((p.seq, p.payload) for p in packets) == [(1, bytes(range(30))), (2, bytes(range(100, 130)))]
    sock.close()


# -- BlueZ errors become advice ---------------------------------------------------------------


def test_unknown_address_says_how_to_pair(make):
    make()
    with pytest.raises(TransportError, match="does not know a device 11:22:33:44:55:66.*[Pp]air"):
        ble.connect_ble("11:22:33:44:55:66")


@pytest.mark.parametrize(
    "error, expected",
    [
        (("org.bluez.Error.NotReady", "Resource Not Ready"), "powered on"),
        (("org.bluez.Error.Failed", "le-connection-abort-by-local"), "le-connection-abort-by-local"),
        (("org.bluez.Error.InProgress", "In Progress"), "busy"),
        (("org.bluez.Error.AuthenticationFailed", "x"), "pair"),
    ],
)
def test_connect_errors_are_explained(make, error, expected):
    make(connect_error=error)
    with pytest.raises(TransportError, match=expected) as info:
        ble.connect_ble(ADDR)
    assert "could not connect to the buds" in str(info.value)


def test_no_bluez_on_the_bus_is_explained(env):
    with pytest.raises(TransportError, match="bluetoothd"):
        ble.connect_ble(ADDR)


def test_unreachable_bus_is_explained(monkeypatch):
    monkeypatch.setenv(ble.DBUS_ADDRESS_ENV, "unix:path=/nonexistent/bus")
    with pytest.raises(TransportError, match="cannot reach the system D-Bus"):
        ble.connect_ble(ADDR)


def test_a_bad_service_uuid_is_refused_before_anything_happens(make):
    fake = make()
    with pytest.raises(TransportError, match="not a UUID"):
        ble.connect_ble(ADDR, "not-a-uuid")
    assert fake.managed_calls == 0 and fake.connect_calls == 0


def test_short_uuids_are_expanded():
    assert ble.normalise_uuid("180F") == "0000180f-0000-1000-8000-00805f9b34fb"


# -- choosing the transport -------------------------------------------------------------------


@pytest.fixture
def no_sdp(monkeypatch):
    def fail(addr, uuid, timeout=0):
        raise SdpError("the buds have no such service")

    monkeypatch.setattr(transport, "find_rfcomm_channel", fail)


def test_ble_is_forced_by_transport_ble(fake, monkeypatch):
    monkeypatch.setattr(transport, "connect_rfcomm", lambda *a, **k: pytest.fail("RFCOMM must not be tried"))
    sock = transport.connect(ADDR, "ble")
    assert isinstance(sock, ble.GattSocket)
    sock.close()


def test_rfcomm_is_forced_by_transport_rfcomm(fake, no_sdp):
    with pytest.raises(TransportError, match="could not find the control service"):
        transport.connect(ADDR, "rfcomm")
    assert fake.managed_calls == 0  # BLE was not tried


def test_auto_falls_back_to_ble_when_sdp_says_the_service_is_absent(fake, no_sdp):
    sock = transport.connect(ADDR, "auto")
    assert isinstance(sock, ble.GattSocket)
    sock.close()


def test_auto_falls_back_to_ble_when_the_rfcomm_connect_fails(fake, monkeypatch):
    monkeypatch.setattr(transport, "find_rfcomm_channel", lambda *a, **k: 7)

    class Refusing:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            raise ConnectionRefusedError("refused")

        def close(self):
            pass

    # Only transport's view of the socket module is replaced, GLib's own threads keep the real one.
    fake_socket_module = types.SimpleNamespace(
        socket=Refusing, AF_BLUETOOTH=31, SOCK_STREAM=1, BTPROTO_RFCOMM=3, timeout=socket.timeout
    )
    monkeypatch.setattr(transport, "socket", fake_socket_module)
    sock = transport.connect(ADDR, "auto")
    assert isinstance(sock, ble.GattSocket)
    sock.close()


def test_auto_prefers_rfcomm_when_it_works(fake, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(transport, "connect_rfcomm", lambda *a, **k: sentinel)
    assert transport.connect(ADDR, "auto") is sentinel
    assert fake.managed_calls == 0


def test_auto_reports_both_reasons_when_neither_works(make, no_sdp):
    make(layout=[])
    with pytest.raises(TransportError) as info:
        transport.connect(ADDR, "auto")
    text = str(info.value)
    assert "RFCOMM" in text and "the buds have no such service" in text
    assert "Bluetooth LE" in text and "Classic-only" in text


def test_auto_with_no_bluez_reports_both_reasons(env, no_sdp):
    with pytest.raises(TransportError) as info:
        transport.connect(ADDR, "auto")
    assert "the buds have no such service" in str(info.value) and "bluetoothd" in str(info.value)


def test_unknown_transport_is_refused():
    with pytest.raises(TransportError, match="unknown transport"):
        transport.connect(ADDR, "carrier-pigeon")


def test_connect_rfcomm_is_still_there():
    assert callable(transport.connect_rfcomm)


# -- through connect_buds, the CLI and profile hints ---------------------------------------------


@pytest.fixture
def known_device(monkeypatch):
    import realmebuds.session as session

    monkeypatch.setattr(session, "pick_device", lambda addr=None: Device(addr or ADDR, "realme Buds Air7 Pro", True, True, True))


def test_connect_buds_over_ble(fake, known_device):
    buds = connect_buds(ADDR, transport="ble")
    try:
        assert buds.state.product_id == "064C12" and buds.state.connected
        assert buds.active_profile.key == "realme-buds-air7-pro"
        assert isinstance(buds.sock, ble.GattSocket)
    finally:
        buds.close()


def test_connect_buds_auto_falls_back_to_ble(fake, known_device, no_sdp):
    buds = connect_buds(ADDR)
    try:
        assert isinstance(buds.sock, ble.GattSocket) and buds.state.product_id == "064C12"
    finally:
        buds.close()


def test_connect_buds_passes_the_service_uuid(make, known_device):
    layout = [(OTHER_SERVICE, [(WRITE_CHAR, ["write"]), (NOTIFY_CHAR, ["notify"])])]
    make(layout=layout)
    buds = connect_buds(ADDR, transport="ble", service_uuid=OTHER_SERVICE)
    buds.close()
    with pytest.raises(TransportError, match="Classic-only"):
        connect_buds(ADDR, transport="ble")


def test_a_forced_profile_can_choose_the_transport_and_service(make, known_device, monkeypatch, tmp_path):
    from tests.test_profiles import FICTIONAL

    layout = [(OTHER_SERVICE, [(WRITE_CHAR, ["write"]), (NOTIFY_CHAR, ["notify"])])]
    make(layout=layout)
    monkeypatch.setattr(transport, "connect_rfcomm", lambda *a, **k: pytest.fail("the profile says BLE"))
    f = tmp_path / "p.json"
    f.write_text(json.dumps({**FICTIONAL, "transport": {"preferred": "ble", "service_uuid": OTHER_SERVICE}}))
    buds = connect_buds(ADDR, profile_file=str(f))
    try:
        assert isinstance(buds.sock, ble.GattSocket) and buds.active_profile.key == "test-buds-x"
    finally:
        buds.close()


def test_an_explicit_transport_beats_the_profile_hint(fake, known_device, monkeypatch, tmp_path):
    from tests.test_profiles import FICTIONAL

    f = tmp_path / "p.json"
    f.write_text(json.dumps({**FICTIONAL, "transport": {"preferred": "ble"}}))
    monkeypatch.setattr(transport, "find_rfcomm_channel", lambda *a, **k: (_ for _ in ()).throw(SdpError("no sdp")))
    with pytest.raises(TransportError, match="could not find the control service"):
        connect_buds(ADDR, profile_file=str(f), transport="rfcomm")


def test_cli_status_over_ble(fake, known_device, capsys):
    assert cli.main(["--transport", "ble", "--addr", ADDR, "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["battery"]["left"]["percent"] == 70


def test_cli_reports_a_missing_service_as_an_error(make, known_device, capsys):
    make(layout=[])
    assert cli.main(["--transport", "ble", "--addr", ADDR, "--service-uuid", OTHER_SERVICE, "status"]) == 1
    assert "Classic-only" in capsys.readouterr().err


def test_cli_flags_are_parsed():
    args = cli.build_parser().parse_args(["--transport", "ble", "--service-uuid", OTHER_SERVICE, "status"])
    assert (args.transport, args.service_uuid) == ("ble", OTHER_SERVICE)
    default = cli.build_parser().parse_args(["status"])
    assert (default.transport, default.service_uuid) == ("auto", None)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--transport", "wifi", "status"])
