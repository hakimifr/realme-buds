import threading
import time

import pytest

from realmebuds import model, protocol
from realmebuds.client import Buds, LinkClosed, Nack, VerifyFailed
from realmebuds.sim import connected_pair


@pytest.fixture
def pair():
    made = []

    def make(**sim_kwargs):
        sock, sim = connected_pair(**sim_kwargs)
        buds = Buds(sock, init_delay=0, request_timeout=1.5)
        buds.start()
        made.append((buds, sim))
        return buds, sim

    yield make
    for buds, sim in made:
        buds.close()
        sim.close()


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_initialise_and_sync_fill_the_state(pair):
    buds, sim = pair()
    buds.initialise()
    errors = buds.sync()
    st = buds.state
    assert errors == {}
    assert st.firmware == "138.138.105"
    assert st.product_id == "065414"
    assert st.battery == {1: (70, False), 2: (68, False)}
    assert st.wearing == {1: 3, 2: 3, 3: 0}
    assert st.anc == model.ANC_TRANSPARENCY
    assert st.features[0x04] is True and st.features[0x1B] is False
    assert len(st.gestures) == 8
    assert st.hold_cycle == model.ANC_MAX | model.ANC_TRANSPARENCY
    assert st.eq == 1
    assert [b.band for b in st.dynamic_audio] == [1, 2, 3]


def test_subscribe_payload_has_count_first_and_only_offered_codes(pair):
    # Behaviour of the real Air7 Pro: offers wearing and battery, never acks the subscription.
    buds, sim = pair()
    buds.initialise()
    sub = next(p for p in sim.received if p.cmd == protocol.CMD_SUBSCRIBE)
    assert sub.payload == bytes([2, 1, 2])
    assert buds.needs_polling


def test_firmware_that_offers_everything_and_acks(pair):
    buds, sim = pair(offered_codes=[1, 2, 3, 5], ack_subscribe=True)
    buds.initialise()
    sub = next(p for p in sim.received if p.cmd == protocol.CMD_SUBSCRIBE)
    assert sub.payload == bytes([4, 1, 2, 3, 5])
    assert not buds.needs_polling


def test_poll_picks_up_changes_made_on_the_buds_without_a_push(pair):
    buds, sim = pair()
    buds.initialise()
    buds.sync()
    sim.anc = model.ANC_MAX  # touched on the bud; nothing is pushed
    sim.features[0x06] = True
    buds.poll()
    assert buds.state.anc == model.ANC_MAX and buds.state.features[0x06] is True


def test_buds_time_request_is_answered(pair):
    buds, sim = pair()
    assert sim.time_answered.wait(2), "buds never got an answer to their time request"


def test_set_anc_updates_buds_and_state(pair):
    buds, sim = pair()
    buds.initialise()
    buds.set_anc(model.ANC_SMART)
    assert sim.anc == model.ANC_SMART
    assert buds.state.anc == model.ANC_SMART


def test_anc_refused_when_no_bud_is_worn(pair):
    buds, sim = pair(worn=False)
    buds.initialise()
    with pytest.raises(Nack) as e:
        buds.set_anc(model.ANC_OFF)
    assert e.value.status == protocol.STATUS_NOT_WORN
    assert "ear" in str(e.value)


def test_push_from_buds_updates_state_and_notifies(pair):
    buds, sim = pair()
    buds.initialise()
    events = []
    buds.add_listener(lambda ev, st: events.append(ev))
    sim.press_anc_button(model.ANC_MAX)
    assert wait_for(lambda: buds.state.anc == model.ANC_MAX)
    sim.set_battery(40, 41, 90)
    assert wait_for(lambda: buds.state.battery.get(3) == (90, False))
    assert "anc" in events and "battery" in events


def test_game_mode_push_updates_features(pair):
    buds, sim = pair()
    buds.initialise()
    buds.set_feature(0x06, True)
    assert wait_for(lambda: buds.state.features.get(0x06) is True)
    assert sim.features[0x06] is True


def test_set_feature_verifies_against_the_buds(pair):
    buds, sim = pair()
    buds.initialise()
    assert buds.set_spatial(True) is True
    assert sim.features[0x1B] is True
    assert any(p.cmd == protocol.CMD_SPATIAL_SOURCE and p.payload == b"\x00" for p in sim.received)


def test_unknown_feature_is_rejected(pair):
    buds, sim = pair()
    buds.initialise()
    with pytest.raises(Nack):
        buds.set_feature(0x30, True)


def test_gesture_remap_writes_only_the_asked_slot_and_reads_back(pair):
    buds, sim = pair()
    buds.initialise()
    buds.set_gesture(model.SIDE_LEFT, 2, 0x03)
    left_double = next(e for e in sim.gestures if (e.dev, e.btn, e.act) == (1, 1, 2))
    right_double = next(e for e in sim.gestures if (e.dev, e.btn, e.act) == (2, 1, 2))
    call_row = next(e for e in sim.gestures if (e.dev, e.btn, e.act) == (1, 6, 2))
    assert left_double.fn == 0x03
    assert right_double.fn == 0x01  # other bud untouched
    assert call_row.fn == 0x00  # on-call group untouched
    write = next(p for p in sim.received if p.cmd == protocol.CMD_GESTURE_WRITE)
    assert write.payload == bytes([1, 1, 1, 2, 3])


def test_gesture_write_that_does_not_take_is_reported(pair):
    buds, sim = pair(ignore_gesture_writes=True)
    buds.initialise()
    with pytest.raises(VerifyFailed):
        buds.set_gesture(model.SIDE_RIGHT, 3, 0x06)


def test_gesture_on_a_missing_slot_is_an_error(pair):
    buds, sim = pair()
    buds.initialise()
    with pytest.raises(Exception, match="no Single tap"):
        buds.set_gesture(model.SIDE_LEFT, 1, 0x01)


def test_hold_cycle(pair):
    buds, sim = pair()
    buds.initialise()
    buds.set_hold_cycle(model.ANC_OFF | model.ANC_TRANSPARENCY | model.ANC_MAX)
    assert sim.hold_cycle == 0x0B
    with pytest.raises(ValueError):
        buds.set_hold_cycle(0)


def test_eq_preset_and_push(pair):
    buds, sim = pair()
    buds.initialise()
    buds.set_eq_preset(3)
    assert sim.eq == 3 and buds.state.eq == 3


def test_custom_eq_create_modify_delete(pair):
    buds, sim = pair()
    buds.initialise()
    bands = [(62, 2), (250, 0), (1000, 0), (4000, 1), (8000, 0), (16000, -3)]
    eqs = buds.save_custom_eq(None, "Mine", bands)
    assert [(e.name, list(e.bands)) for e in eqs] == [("Mine", bands)]
    new_id = eqs[0].id
    assert buds.state.eq == new_id
    bands[0] = (62, 4)
    eqs = buds.save_custom_eq(new_id, "Mine", bands)
    assert eqs[0].bands[0] == (62, 4)
    assert buds.delete_custom_eq(new_id) == []
    with pytest.raises(ValueError):
        buds.save_custom_eq(None, "x", [(62, 9)])


def test_dynamic_audio_band(pair):
    buds, sim = pair()
    buds.initialise()
    buds.set_dynamic_band(1, -3)
    buds.set_dynamic_band(3, 5)
    assert sim.dynamic == {1: -3, 2: 0, 3: 5}
    with pytest.raises(ValueError):
        buds.set_dynamic_band(2, 6)
    with pytest.raises(ValueError):
        buds.set_dynamic_band(4, 0)


def test_fragmented_replies_work(pair):
    buds, sim = pair(fragment_replies=True)
    buds.initialise()
    buds.sync()
    assert len(buds.state.gestures) == 8 and buds.state.firmware == "138.138.105"


def test_scan_features_returns_only_ids_the_buds_have(pair):
    buds, sim = pair()
    buds.initialise()
    found = buds.scan_features()
    assert set(found) == set(sim.features)


def test_absent_optional_features_do_not_break_sync(pair):
    buds, sim = pair(ignore_commands={protocol.CMD_CUSTOM_EQ_LIST, protocol.CMD_DYNAMIC_AUDIO_BANDS,
                                      protocol.CMD_DYNAMIC_AUDIO_SINGLE})
    buds.request_timeout = 0.3
    buds.initialise()
    errors = buds.sync()
    assert "custom-eq" in errors and "anc" not in errors


def test_requests_time_out_cleanly(pair):
    buds, sim = pair(ignore_commands={protocol.CMD_BATTERY})
    buds.initialise()
    with pytest.raises(TimeoutError):
        buds.request(protocol.CMD_BATTERY, timeout=0.2)


def test_link_drop_fails_waiters_and_notifies(pair):
    buds, sim = pair()
    buds.initialise()
    events = []
    buds.add_listener(lambda ev, st: events.append(ev))
    sim.drop_link()
    assert wait_for(lambda: not buds.state.connected)
    assert "disconnected" in events
    with pytest.raises(LinkClosed):
        buds.read_battery()


def test_concurrent_requests_get_their_own_replies(pair):
    buds, sim = pair()
    buds.initialise()
    results = {}

    def run(name, fn):
        results[name] = fn()

    threads = [
        threading.Thread(target=run, args=("bat", buds.read_battery)),
        threading.Thread(target=run, args=("anc", buds.read_anc)),
        threading.Thread(target=run, args=("eq", buds.read_eq)),
        threading.Thread(target=run, args=("gest", buds.read_gestures)),
    ]
    [t.start() for t in threads]
    [t.join(3) for t in threads]
    assert results["anc"] == model.ANC_TRANSPARENCY
    assert results["eq"] == 1 and len(results["gest"]) == 8 and results["bat"][1] == (70, False)


def test_commands_the_buds_do_not_advertise_are_never_sent(pair):
    # Bitmap with only bits 0-8 set: no custom EQ (34), no bass engine (38/54), no spatial source (43).
    buds, sim = pair(capability_reply=bytes.fromhex("00 FF 01"))
    buds.initialise()
    errors = buds.sync()
    sim.features[0x1B] = False
    buds.set_spatial(True)
    sent = {p.cmd for p in sim.received}
    assert not sent & {0x0122, 0x0124, 0x012C, 0x041E}
    assert errors == {} and buds.state.custom_eqs == [] and buds.state.dynamic_audio == []
    assert buds.supports(0x0108) and not buds.supports(0x0418) and buds.supports(0x9999)


def test_write_the_buds_apply_without_acknowledging_still_succeeds(pair):
    buds, sim = pair(silent_commands={protocol.CMD_DYNAMIC_AUDIO_WRITE})
    buds.request_timeout = 0.5
    buds.initialise()
    bands = buds.set_dynamic_band(2, 4)
    assert sim.dynamic[2] == 4 and [b.value for b in bands if b.band == 2] == [4]
    assert buds.last_write_acked is False


def test_write_that_is_neither_acknowledged_nor_applied_says_so(pair):
    buds, sim = pair(ignore_commands={protocol.CMD_DYNAMIC_AUDIO_WRITE})
    buds.initialise()
    with pytest.raises(VerifyFailed, match="did not acknowledge"):
        buds.set_dynamic_band(1, 3)
    assert sim.dynamic[1] == 0


def test_a_refusal_is_still_an_error_not_a_silent_pass(pair):
    buds, sim = pair()
    buds.initialise()
    with pytest.raises(Nack):
        buds.set_feature(0x30, True)
