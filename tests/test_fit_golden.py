import threading

import pytest

from realmebuds import model, parsing, protocol
from realmebuds.client import BudsError, Buds, LinkClosed, VerifyFailed
from realmebuds.sim import connected_pair


@pytest.fixture
def pair():
    made = []

    def make(**kw):
        sock, sim = connected_pair(**kw)
        buds = Buds(sock, init_delay=0, request_timeout=1.5)
        buds.start()
        buds.initialise()
        made.append((buds, sim))
        return buds, sim

    yield make
    for buds, sim in made:
        buds.close()
        sim.close()


def sent(sim, cmd):
    return [p for p in sim.received if p.cmd == cmd]


# -- fit test ---------------------------------------------------------------


def test_fit_test_good_fit(pair):
    buds, sim = pair()
    assert buds.run_fit_test(timeout=3) == {1: 1, 2: 1}
    assert sim.fit_started == 1
    assert [p.payload for p in sent(sim, protocol.CMD_FIT_TEST)] == [b"\x01", b"\x00"]  # start, then stop
    # The Air7 Pro does not offer this event, so the client asks for it the way the app would.
    assert sim.registered_events == [protocol.EV_FIT_TEST]


def test_fit_test_does_not_register_when_the_event_is_offered(pair):
    buds, sim = pair(offered_codes=[1, 2, 3, 4], ack_subscribe=True)
    buds.run_fit_test(timeout=3)
    assert sim.registered_events == []


def test_fit_test_reports_each_bud(pair):
    buds, sim = pair(fit_result=(0, 6))
    result = buds.run_fit_test(timeout=3)
    assert result == {1: 0, 2: 6}
    assert "average" in model.fit_label(result[1]) and "poor" in model.fit_label(result[2])
    assert model.fit_label(1) == "good seal"


def test_fit_test_asks_to_retry(pair):
    buds, sim = pair(fit_result=(3, 3))
    with pytest.raises(BudsError, match="try again"):
        buds.run_fit_test(timeout=3)


def test_fit_test_needs_both_buds_in_the_ears(pair):
    buds, sim = pair()
    sim.wearing = {1: 1, 2: 3, 3: 0}
    with pytest.raises(BudsError, match="both earbuds"):
        buds.run_fit_test(timeout=3)
    assert sim.fit_started == 0


def test_fit_test_ignores_an_interrupted_status_and_keeps_waiting(pair):
    buds, sim = pair(fit_result=(5, 5))
    threading.Timer(0.4, sim.push, [bytes([4, 1, 1, 2, 1])]).start()
    assert buds.run_fit_test(timeout=3) == {1: 1, 2: 1}


def test_fit_test_without_any_result_says_so(pair):
    buds, sim = pair(fit_result=None)
    with pytest.raises(BudsError, match="no fit-test result"):
        buds.run_fit_test(timeout=0.5)


def test_fit_test_link_drop_is_reported(pair):
    buds, sim = pair(fit_result=None)
    threading.Timer(0.3, sim.drop_link).start()
    with pytest.raises(LinkClosed):
        buds.run_fit_test(timeout=3)


# -- Golden Sound ----------------------------------------------------------


def all_nodes(db=-25):
    return [(side, band, db) for side, band in model.HEARING_NODES]


def test_no_profile_reads_as_empty(pair):
    buds, sim = pair()
    rec = buds.read_hearing_record()
    assert rec is not None and rec.entries == () and rec.id == 0


def test_tone_is_preceded_by_a_stop_and_validated(pair):
    buds, sim = pair()
    buds.play_tone(1, 3, -41)
    assert sim.tones == [(1, 3, -41)] and sim.tones_stopped >= 1
    p = sent(sim, protocol.CMD_HEARING_PARAMS)
    assert p[-1].payload == bytes([3, 1, 1, 3, 0xD7])  # -41 as a signed byte
    with pytest.raises(ValueError):
        buds.play_tone(3, 1, -25)
    with pytest.raises(ValueError):
        buds.play_tone(1, 7, -25)
    with pytest.raises(ValueError):
        buds.play_tone(1, 1, -26)  # not one of the slider stops


def test_hearing_test_start_and_stop(pair):
    buds, sim = pair()
    buds.hearing_test_start()
    assert sim.test_active
    buds.hearing_test_stop()
    assert not sim.test_active


def test_ear_scan_returns_the_scan_and_stops_it(pair):
    buds, sim = pair()
    scan = buds.ear_scan(uid=1234, timeout=3)
    assert scan.uid == 1234 and scan.data == sim.ear_scan_bytes and len(scan.data) == 168
    payloads = [p.payload for p in sent(sim, protocol.CMD_HEARING_DETECT)]
    assert payloads[0] == bytes([4, 1]) + (1234).to_bytes(4, "big")
    assert payloads[-1] == bytes([4, 0]) + (1234).to_bytes(4, "big")


def test_ear_scan_needs_both_buds_in(pair):
    buds, sim = pair()
    sim.wearing = {1: 3, 2: 0, 3: 0}
    with pytest.raises(BudsError, match="both earbuds"):
        buds.ear_scan(uid=1, timeout=1)


def test_apply_uses_the_apps_order_and_is_verified(pair):
    buds, sim = pair()
    scan = buds.ear_scan(uid=777, timeout=3)
    entries = all_nodes(-35)
    entries[0] = (1, 1, 3)
    rec = buds.apply_golden_sound(entries, "My ears", 777, scan.data)
    order = [p.cmd for p in sim.received if p.cmd in (0x0411, 0x040E, 0x0415, 0x0403)]
    # restore data, [tone stops are 0x040E too], profile, scan data, feature switch
    assert order[0] == 0x0411 and order[-1] == 0x0403 and order.index(0x0415) < order.index(0x0403)
    assert rec.id == 777 and rec.name == "My ears" and rec.entries[0] == parsing.HearingEntry(1, 1, 3)
    assert sim.features[0x0B] is True
    assert sim.scan_data == sim.ear_scan_bytes
    restore = sent(sim, protocol.CMD_RESTORE_DATA)[0].payload
    assert restore == bytes([1, 1, 1, 0, 0x0B])
    assert buds.state.hearing_record == rec


def test_apply_without_scan_data_skips_that_write(pair):
    buds, sim = pair()
    buds.apply_golden_sound(all_nodes(), "x", 5, None)
    assert not sent(sim, protocol.CMD_EAR_SCAN_WRITE)


def test_apply_rejects_bad_profiles_before_sending_anything(pair):
    buds, sim = pair()
    with pytest.raises(ValueError):
        buds.apply_golden_sound(all_nodes()[:-1], "x", 5, None)  # a node missing
    with pytest.raises(ValueError):
        buds.apply_golden_sound(all_nodes(-26), "x", 5, None)  # not a storable value
    with pytest.raises(ValueError):
        buds.apply_golden_sound(all_nodes(), "x", 0, None)
    assert not sent(sim, protocol.CMD_HEARING_PARAMS)


def test_apply_that_does_not_stick_is_reported(pair):
    buds, sim = pair(ignore_commands={protocol.CMD_RESTORE_DATA})
    sim.ignore_commands.add(protocol.CMD_HEARING_PARAMS)
    with pytest.raises(VerifyFailed):
        buds.apply_golden_sound(all_nodes(), "x", 5, None)


def test_golden_sound_switch(pair):
    buds, sim = pair()
    buds.set_golden_sound(True)
    assert sim.features[0x0B] is True
    buds.set_golden_sound(False)
    assert sim.features[0x0B] is False


# -- parsing and model ------------------------------------------------------


def test_fit_and_record_parsing_edge_cases():
    assert parsing.parse_fit_test(bytes([1, 1, 2, 0])) == {1: 1, 2: 0}
    assert parsing.parse_hearing_record(b"\x00\x03\x0c") is None  # truncated
    assert parsing.parse_hearing_record(b"\x01\x03\x00\x00\x00\x00\x00") is None  # non-zero status
    assert parsing.parse_ear_scan_event(bytes([3, 0xA8, 0, 1, 2])) is None  # shorter than announced


def test_snapping_and_node_order_follow_the_app():
    assert model.HEARING_NODES[:6] == tuple((1, b) for b in range(1, 7))  # left ear first
    assert model.HEARING_NODES[6:] == tuple((2, b) for b in range(1, 7))
    assert len(model.HEARING_STOPS) == 25 and len(model.HEARING_RESULT_VALUES) == 12
    assert model.snap_hearing_value(-120) == -55 and model.snap_hearing_value(17) == 17
    assert model.snap_hearing_value(-20) in (-19, -25) and model.snap_hearing_value(2) == 3
    assert all(model.snap_hearing_value(v) in model.HEARING_RESULT_VALUES for v in model.HEARING_STOPS)
