"""Drives the real GTK widgets against the simulator. Needs a display: run with xvfb-run."""

import json
import os
import time

import pytest

os.environ.setdefault("GSK_RENDERER", "cairo")

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, GLib, Graphene, Gtk
except (ValueError, ImportError):
    pytest.skip("GTK4/libadwaita not installed", allow_module_level=True)

if not Gtk.init_check():
    pytest.skip("no display (run with xvfb-run)", allow_module_level=True)

from realmebuds import model  # noqa: E402
from realmebuds.gui import BudsApp  # noqa: E402
from realmebuds.gui_advanced import AdvancedDialog  # noqa: E402
from realmebuds.gui_dialogs import FitTestDialog, HearingTestDialog  # noqa: E402
from realmebuds.gui_eq import CustomEqDialog  # noqa: E402
from realmebuds import profiles  # noqa: E402
from realmebuds.transport import Device  # noqa: E402
from realmebuds.session import connect_buds  # noqa: E402


def pump(cond, timeout=6.0):
    ctx = GLib.MainContext.default()
    end = time.time() + timeout
    while time.time() < end:
        while ctx.iteration(False):
            pass
        if cond():
            return True
        time.sleep(0.01)
    while ctx.iteration(False):
        pass
    return cond()


@pytest.fixture(scope="module")
def win(tmp_path_factory):
    import json

    from tests.test_profiles import FICTIONAL

    profile_dir = tmp_path_factory.mktemp("profiles")
    (profile_dir / "x.json").write_text(json.dumps(FICTIONAL))
    os.environ["REALME_BUDS_PROFILE_DIR"] = str(profile_dir)
    app = BudsApp(sim=True)
    app.register(None)
    app.activate()
    window = app.window
    assert pump(lambda: window.stack.get_visible_child_name() == "main"), "never connected"
    window.set_default_size(520, 1500)
    yield window
    window.session.stop()
    os.environ.pop("REALME_BUDS_PROFILE_DIR", None)


def sim(win):
    return win.session.buds.sim


def screenshot(win, name):
    path = os.environ.get("GUI_SHOTS")
    if not path:
        return
    os.makedirs(path, exist_ok=True)
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    clip = Graphene.Rect().init(0, 0, w, h)
    texture = win.get_native().get_renderer().render_texture(node, clip)
    texture.save_to_png(os.path.join(path, name))


def test_initial_state_is_shown(win):
    assert win.battery_rows[1][2].get_label() == "70%"
    assert win.battery_rows[2][2].get_label() == "68%"
    assert not win.battery_rows[3][0].get_visible()  # case level unknown while the lid is closed
    assert win.anc_buttons["transparency"].get_active()
    assert win.feature_rows["game-mode"].get_visible() and not win.feature_rows["game-mode"].get_active()
    assert win.firmware_label.get_subtitle().startswith("138.138.105") and "064C12" in win.firmware_label.get_subtitle()
    assert "realme Buds Air7 Pro" in win.model_label.get_subtitle()
    assert win.eq_row.get_selected() == 1  # Deep Bass
    pump(lambda: False, 0.3)
    screenshot(win, "main.png")


def test_noise_control_buttons_and_level(win):
    win.anc_buttons["off"].set_active(True)
    assert pump(lambda: sim(win).anc == model.ANC_OFF)
    win.anc_buttons["nc"].set_active(True)
    assert pump(lambda: sim(win).anc == model.ANC_SMART)  # first level in the list
    win.level_row.set_selected(3)
    assert pump(lambda: sim(win).anc == model.ANC_MILD)
    assert win.anc_buttons["nc"].get_active()
    win.anc_buttons["transparency"].set_active(True)
    assert pump(lambda: sim(win).anc == model.ANC_TRANSPARENCY)


def test_changes_made_on_the_buds_show_up(win):
    sim(win).press_anc_button(model.ANC_MAX)
    assert pump(lambda: win.anc_buttons["nc"].get_active())
    assert ANC_LEVEL(win) == "Max"
    sim(win).set_battery(33, 34, 90)
    assert pump(lambda: win.battery_rows[3][2].get_label() == "90%")
    assert win.battery_rows[3][0].get_visible()


def ANC_LEVEL(win):
    return win.level_row.get_selected_item().get_string()


def test_switches_write_to_the_buds(win):
    win.feature_rows["spatial"].set_active(True)
    assert pump(lambda: sim(win).features[0x1B] is True)
    win.feature_rows["game-mode"].set_active(True)
    assert pump(lambda: sim(win).features[0x06] is True)
    win.feature_rows["wear-detect"].set_active(False)
    assert pump(lambda: sim(win).features[0x04] is False)
    win.dynamic_switch.set_active(False)
    assert pump(lambda: sim(win).features[0x1D] is False)
    assert pump(lambda: not win.dynamic_scales[1].get_sensitive())


def test_eq_preset(win):
    win.eq_row.set_selected(3)
    assert pump(lambda: sim(win).eq == 3)


def test_dynamic_audio_sliders_are_debounced_and_written(win):
    win.dynamic_switch.set_active(True)
    assert pump(lambda: sim(win).features[0x1D] is True)
    before = len([p for p in sim(win).received if p.cmd == 0x041B])
    for v in (1, 2, 3):
        win.dynamic_scales[1].set_value(v)
    assert pump(lambda: sim(win).dynamic[1] == 3)
    assert len([p for p in sim(win).received if p.cmd == 0x041B]) - before == 1  # one write, not three


def test_touch_controls(win):
    row = win.gesture_rows[(model.SIDE_LEFT, 3)]
    codes = win._gesture_fns[(model.SIDE_LEFT, 3)]
    assert 0x0B not in codes  # volume is not offered for a triple tap, matching realme Link
    row.set_selected(codes.index(0x11))  # game mode
    assert pump(lambda: next(e for e in sim(win).gestures if (e.dev, e.btn, e.act) == (1, 1, 3)).fn == 0x11)
    # the right bud is untouched
    assert next(e for e in sim(win).gestures if (e.dev, e.btn, e.act) == (2, 1, 3)).fn == 0x05


def test_hold_cycle_switches(win):
    win.cycle_switches[model.ANC_OFF].set_active(True)
    assert pump(lambda: sim(win).hold_cycle == model.ANC_OFF | model.ANC_MAX | model.ANC_TRANSPARENCY)
    win.cycle_switches[model.ANC_TRANSPARENCY].set_active(False)
    assert pump(lambda: sim(win).hold_cycle == model.ANC_OFF | model.ANC_MAX)
    # realme Link needs two modes: dropping to one is refused and the switch goes back
    win.cycle_switches[model.ANC_MAX].set_active(False)
    assert pump(lambda: win.cycle_switches[model.ANC_MAX].get_active())
    assert sim(win).hold_cycle == model.ANC_OFF | model.ANC_MAX


def test_gesture_rows_offer_only_what_the_app_offers(win):
    for (side, act), row in win.gesture_rows.items():
        codes = win._gesture_fns[(side, act)]
        assert set(win.current_profile.options_for(act)) <= set(codes)
        assert row.get_model().get_n_items() == len(codes)


def test_fit_test_dialog(win):
    dlg = FitTestDialog(win)
    dlg.start()
    assert pump(lambda: dlg.result.get_label() != "")
    assert dlg.result.get_label() == "Left bud: good seal\nRight bud: good seal"
    assert sim(win).fit_started >= 1
    sim(win).fit_result = (3, 3)
    dlg.start()
    assert pump(lambda: "try again" in dlg.info.get_label())


def test_golden_sound_section_and_switch(win):
    assert win.golden_group.get_visible() and win.golden_switch.get_visible()
    win.golden_switch.set_active(True)
    assert pump(lambda: sim(win).features[0x0B] is True)
    win.golden_switch.set_active(False)
    assert pump(lambda: sim(win).features[0x0B] is False)


def test_hearing_test_wizard_end_to_end(win):
    stops = model.HEARING_STOPS
    dlg = HearingTestDialog(win)
    dlg.present()
    assert dlg.stack.get_visible_child_name() == "intro"
    dlg.start()
    assert pump(lambda: dlg.stack.get_visible_child_name() == "test")
    assert sim(win).test_active and dlg.scan is not None and dlg.scan.data == sim(win).ear_scan_bytes
    assert pump(lambda: sim(win).tones and sim(win).tones[-1] == (1, 1, model.HEARING_START_DB))  # starts quiet

    # moving the slider plays the new level after a short pause
    dlg.scale.set_value(stops.index(-35))
    assert pump(lambda: (1, 1, -35) in sim(win).tones)

    # a loud level is held back until it is confirmed
    before = len(sim(win).tones)
    dlg.scale.set_value(stops.index(10))
    assert pump(lambda: dlg.scale.get_value() == stops.index(-35)) or dlg.scale.get_value() == stops.index(-35)
    pump(lambda: False, 0.6)
    assert all(t[2] < model.HEARING_LOUD_WARNING_DB for t in sim(win).tones[before:])

    for _ in model.HEARING_NODES:
        dlg.next_node()
    assert dlg.stack.get_visible_child_name() == "save"
    assert pump(lambda: not sim(win).test_active)
    assert len(dlg.results) == 12 and dlg.results[0] == (1, 1, -35)

    dlg.name_entry.set_text("GUI test")
    dlg.save()
    assert pump(lambda: sim(win).hearing_record is not None)
    entries, uid, name = sim(win).hearing_record
    assert name == "GUI test" and len(entries) == 12 and uid == dlg.uid
    assert sim(win).scan_data == sim(win).ear_scan_bytes and sim(win).features[0x0B] is True
    assert pump(lambda: dlg.stack.get_visible_child_name() == "message")
    assert pump(lambda: "GUI test" in win.profile_row.get_subtitle())
    assert win.golden_switch.get_active()
    dlg.dialog.close()


def test_closing_the_wizard_stops_the_test(win):
    dlg = HearingTestDialog(win)
    dlg.present()
    dlg.start()
    assert pump(lambda: dlg.stack.get_visible_child_name() == "test")
    assert sim(win).test_active
    dlg.dialog.close()
    assert pump(lambda: not sim(win).test_active)


def reconnect(win, *, profile=None, sim_profile=None):
    """Connect again as a different model and wait for the window to be rebuilt for it."""
    old = win.session.buds
    win.overrides.profile = profile
    win.sim_profile = sim_profile
    win.apply_connection_overrides()
    assert pump(lambda: win.session.buds is not None and win.session.buds is not old and win.stack.get_visible_child_name() == "main" and win._applied is not None, timeout=12)
    assert pump(lambda: win.state is not None and win.state.firmware is not None)


def reset(win):
    win.overrides.force_gestures = win.overrides.force_hold_cycle = win.overrides.force_capabilities = False
    win.overrides.skip_ear_scan = False
    win.overrides.fit_timeout = 30.0
    win.overrides_changed()


# -- another model, chosen through the Advanced dialog ----------------------------------


def test_choosing_another_profile_in_advanced_rebuilds_the_window(win):
    adv = AdvancedDialog(win)
    adv.present()
    assert "realme Buds Air7 Pro" in adv.model_row.get_subtitle()
    keys = adv._profile_keys
    old = win.session.buds
    win.sim_profile = "test-buds-x"  # make the pretend buds that model too
    adv.profile_row.set_selected(keys.index("test-buds-x"))
    adv.apply_device()
    assert win.overrides.profile == "test-buds-x"
    assert pump(lambda: win.session.buds is not None and win.session.buds is not old and win.stack.get_visible_child_name() == "main" and win._applied is not None, timeout=12)
    assert pump(lambda: win.state is not None and win.state.firmware is not None)

    p = win.current_profile
    assert p.key == "test-buds-x"
    assert set(win.anc_buttons) == {"off", "transparency", "anc"} and win.level_rows == {}
    assert set(win.feature_rows) == {"game-mode", "spatial"}
    assert {act for _, act in win.gesture_rows} == {2, 4}
    assert win.dynamic_scales == {} and win.fit_row is None
    assert win.eq_row is not None and win.eq_row.get_model().get_string(0) == "Balanced"
    assert not win.golden_group.get_visible()
    # writes use this model's numbers: Off is written as 1 and reported as 8
    win.anc_buttons["off"].set_active(True)
    assert pump(lambda: sim(win).anc == 8)
    win.anc_buttons["transparency"].set_active(True)
    assert pump(lambda: sim(win).anc == 256)
    assert pump(lambda: win.anc_buttons["transparency"].get_active())
    adv.dialog.close()
    reconnect(win)  # back to the Air7 Pro
    assert win.current_profile.key == "realme-buds-air7-pro" and "nc" in win.anc_buttons


def test_capability_override_shows_fit_test_and_golden_sound_for_a_model_that_lacks_them(win):
    reconnect(win, profile="test-buds-x", sim_profile="test-buds-x")
    assert win.fit_row is None and not win.golden_group.get_visible()
    win.overrides.force_capabilities = True
    win.overrides_changed()
    assert win.fit_row is not None and win.golden_group.get_visible()
    dlg = FitTestDialog(win)
    dlg.start()
    assert pump(lambda: dlg.result.get_label() != "")  # and it runs
    reset(win)
    assert win.fit_row is None
    reconnect(win)


def test_unrecognised_buds_get_a_cautious_window_and_the_gesture_override_opens_it_up(win, monkeypatch):
    monkeypatch.setattr(profiles, "find", lambda *a, **k: None)
    reconnect(win)
    assert win.current_profile.generic
    assert win.anc_buttons == {} and win.gesture_rows == {} and win.cycle_row is None and win.fit_row is None
    assert not win.noise_group.get_visible() and not win.touch_group.get_visible()
    # the switches the buds report are still there, with placeholder names
    assert "feature-06" in win.feature_rows and "Feature 0x06" in win.feature_rows["feature-06"].get_title()
    win.feature_rows["feature-06"].set_active(True)
    assert pump(lambda: sim(win).features[0x06] is True)

    win.overrides.force_gestures = True
    win.overrides_changed()
    assert win.touch_group.get_visible() and (1, 3) in win.gesture_rows and (2, 5) in win.gesture_rows
    codes = win._gesture_fns[(model.SIDE_LEFT, 3)]
    assert 0x0B in codes  # every function is offered when forced
    win.gesture_rows[(model.SIDE_LEFT, 3)].set_selected(codes.index(0x0B))
    assert pump(lambda: next(e for e in sim(win).gestures if (e.dev, e.btn, e.act) == (1, 1, 3)).fn == 0x0B)
    reset(win)
    monkeypatch.undo()
    reconnect(win)
    assert win.current_profile.key == "realme-buds-air7-pro"


def test_forcing_gestures_on_a_known_model_offers_everything(win):
    row = win.gesture_rows[(model.SIDE_RIGHT, 3)]
    assert 0x0B not in win._gesture_fns[(model.SIDE_RIGHT, 3)]
    win.overrides.force_gestures = True
    win.overrides_changed()
    row = win.gesture_rows[(model.SIDE_RIGHT, 3)]
    codes = win._gesture_fns[(model.SIDE_RIGHT, 3)]
    assert 0x0B in codes and 0x0D in codes
    row.set_selected(codes.index(0x0C))  # volume down: not in realme Link's list for a triple tap
    assert pump(lambda: next(e for e in sim(win).gestures if (e.dev, e.btn, e.act) == (2, 1, 3)).fn == 0x0C)
    win.gesture_rows[(model.SIDE_RIGHT, 3)].set_selected(win._gesture_fns[(model.SIDE_RIGHT, 3)].index(0x05))
    assert pump(lambda: next(e for e in sim(win).gestures if (e.dev, e.btn, e.act) == (2, 1, 3)).fn == 0x05)
    reset(win)
    assert 0x0B not in win._gesture_fns[(model.SIDE_RIGHT, 3)]


def test_forcing_the_hold_cycle_allows_a_single_mode(win):
    sim(win).hold_cycle = model.ANC_OFF | model.ANC_TRANSPARENCY | model.ANC_MAX
    win.session.submit(lambda b: b.read_hold_cycle())
    assert pump(lambda: all(win.cycle_switches[b].get_active() for b in win.cycle_switches))
    win.overrides.force_hold_cycle = True
    win.cycle_switches[model.ANC_TRANSPARENCY].set_active(False)
    assert pump(lambda: sim(win).hold_cycle == model.ANC_OFF | model.ANC_MAX)
    win.cycle_switches[model.ANC_MAX].set_active(False)
    assert pump(lambda: sim(win).hold_cycle == model.ANC_OFF)  # one mode: allowed only because it is forced
    win.cycle_switches[model.ANC_TRANSPARENCY].set_active(True)
    win.cycle_switches[model.ANC_MAX].set_active(True)
    assert pump(lambda: sim(win).hold_cycle == model.ANC_OFF | model.ANC_TRANSPARENCY | model.ANC_MAX)
    reset(win)


def test_skip_ear_scan_and_fit_timeout_overrides(win):
    win.overrides.skip_ear_scan = True
    dlg = HearingTestDialog(win)
    dlg.present()
    before = len([p for p in sim(win).received if p.cmd == 0x040D and p.payload[:1] == b"\x04"])
    dlg.start()
    assert pump(lambda: dlg.stack.get_visible_child_name() == "test")
    assert dlg.scan is None
    for _ in model.HEARING_NODES:
        dlg.next_node()
    dlg.save()
    assert pump(lambda: sim(win).scan_data is None and sim(win).hearing_record is not None and dlg.stack.get_visible_child_name() == "message")
    assert len([p for p in sim(win).received if p.cmd == 0x040D and p.payload[:1] == b"\x04"]) == before  # no scan was started
    dlg.dialog.close()
    win.overrides.skip_ear_scan = False

    win.overrides.fit_timeout = 0.5
    sim(win).fit_result = None
    fit = FitTestDialog(win)
    fit.start()
    assert pump(lambda: "no fit-test result" in fit.info.get_label(), timeout=5)
    sim(win).fit_result = (1, 1)
    reset(win)


# -- the Advanced dialog's tools ---------------------------------------------------------------


def test_advanced_overrides_drive_the_window(win):
    adv = AdvancedDialog(win)
    adv.present()
    rows_before = len(win.gesture_rows)
    adv.force_gestures_row.set_active(True)
    assert win.overrides.force_gestures and len(win.gesture_rows) > rows_before
    adv.force_gestures_row.set_active(False)
    assert not win.overrides.force_gestures and len(win.gesture_rows) == rows_before
    adv.force_hold_row.set_active(True)
    adv.force_caps_row.set_active(True)
    adv.skip_scan_row.set_active(True)
    adv.fit_timeout_row.set_value(45)
    assert (win.overrides.force_hold_cycle, win.overrides.force_capabilities, win.overrides.skip_ear_scan, win.overrides.fit_timeout) == (True, True, True, 45.0)
    reset(win)
    adv.dialog.close()


def test_advanced_tools_scan_raw_json_draft_trace(win):
    adv = AdvancedDialog(win)
    adv.present()
    adv.scan_features()
    assert pump(lambda: "Auto play/pause" in adv.scan_result.get_label())
    assert "Golden Sound" in adv.scan_result.get_label()

    adv.raw_cmd_row.set_text("0106")
    adv.send_raw()
    assert pump(lambda: adv.raw_result.get_label().startswith("reply 0x8106 status=0"))
    adv.raw_cmd_row.set_text("zz")
    adv.send_raw()
    assert "must be hex" in adv.raw_result.get_label()

    data = json.loads(adv.status_json())
    assert data["profile"] == "realme-buds-air7-pro" and data["battery"]["left"]["percent"] == 70
    draft = profiles.profile_from_dict(json.loads(adv.draft_text()), "draft")
    assert draft.product_ids == ("064C12",) and draft.feature(0x1A) is not None

    assert win.trace and any(" TX " in line for line in win.trace) and any(" RX " in line for line in win.trace)
    adv.refresh_trace()
    assert "TX" in adv.trace_text()
    adv.trace_clear.emit("clicked")
    assert not win.trace and adv.trace_text() == ""
    # frames sent from now on show up live
    win.session.submit(lambda b: b.read_battery())
    assert pump(lambda: adv.trace_view.get_buffer().get_char_count() > 0)
    adv.dialog.close()


def test_advanced_device_list_and_address(win):
    adv = AdvancedDialog(win)
    adv.present()
    adv.refresh_devices([Device("AA:BB:CC:DD:EE:FF", "realme Buds Air7 Pro", True, True, True), Device("11:22:33:44:55:66", "Mouse")])
    assert len(adv._device_rows) == 2
    assert "AA:BB:CC:DD:EE:FF" in adv._device_rows[0].get_subtitle() and "connected" in adv._device_rows[0].get_subtitle()
    adv.refresh_devices([])
    assert adv._device_rows == []
    adv.set_profile_file("/tmp/x.json")
    assert win.overrides.profile_file == "/tmp/x.json"
    adv.set_profile_file(None)
    adv.dialog.close()


def test_advanced_transport_rows_are_passed_to_the_connector(win, monkeypatch):
    import realmebuds.gui as gui
    from realmebuds.transport import TRANSPORTS

    calls = []
    real = gui.connect_buds
    monkeypatch.setattr(gui, "connect_buds", lambda *a, **kw: calls.append(kw) or real(*a, **kw))
    adv = AdvancedDialog(win)
    adv.present()
    assert TRANSPORTS[adv.transport_row.get_selected()] == "auto" and adv.service_uuid_row.get_text() == ""
    assert win.overrides.transport == "auto" and win.overrides.service_uuid is None

    old = win.session.buds
    uuid = "0000079c-d102-11e1-9b23-00025b00a5a5"
    adv.transport_row.set_selected(TRANSPORTS.index("ble"))
    adv.service_uuid_row.set_text(f"  {uuid} ")
    assert win.overrides.transport == "auto"  # applied on "Apply and reconnect", like the address
    adv.apply_device()
    assert (win.overrides.transport, win.overrides.service_uuid) == ("ble", uuid)
    assert pump(lambda: win.session.buds is not None and win.session.buds is not old and win.stack.get_visible_child_name() == "main", timeout=12)
    assert calls and calls[-1]["transport"] == "ble" and calls[-1]["service_uuid"] == uuid

    adv.transport_row.set_selected(TRANSPORTS.index("auto"))
    adv.service_uuid_row.set_text("")
    old = win.session.buds
    adv.apply_device()
    assert (win.overrides.transport, win.overrides.service_uuid) == ("auto", None)
    assert pump(lambda: win.session.buds is not None and win.session.buds is not old and win.stack.get_visible_child_name() == "main", timeout=12)
    assert calls[-1]["transport"] == "auto" and calls[-1]["service_uuid"] is None
    adv.dialog.close()


# -- custom equaliser ---------------------------------------------------------------------------


def test_custom_equaliser_create_select_edit_delete(win):
    assert win.new_eq_button is not None
    dlg = CustomEqDialog(win)
    dlg.present()
    assert list(dlg.scales) == [62, 250, 1000, 4000, 8000, 16000]
    dlg.name_entry.set_text("Mine")
    dlg.scales[62].set_value(4)
    dlg.scales[16000].set_value(-3)
    dlg.save()
    assert pump(lambda: len(sim(win).custom_eqs) == 1)
    eq_id, name, bands = sim(win).custom_eqs[0]
    assert name == "Mine" and bands[0] == (62, 4) and bands[-1] == (16000, -3)
    assert pump(lambda: len(win._custom_rows) == 1 and win.eq_row.get_selected() == win._eq_ids.index(eq_id))
    assert sim(win).eq == eq_id

    win.eq_row.set_selected(0)  # a built-in preset
    assert pump(lambda: sim(win).eq == 0)
    win.eq_row.set_selected(win._eq_ids.index(eq_id))  # and back to the custom one
    assert pump(lambda: sim(win).eq == eq_id)

    edit = CustomEqDialog(win, win.state.custom_eqs[0])
    edit.scales[1000].set_value(2)
    edit.save()
    assert pump(lambda: dict(sim(win).custom_eqs[0][2])[1000] == 2)

    win.delete_custom_eq(win.state.custom_eqs[0])
    assert pump(lambda: sim(win).custom_eqs == [] and win._custom_rows == [])
    dlg.dialog.close()


def test_empty_custom_eq_name_is_refused(win):
    dlg = CustomEqDialog(win)
    dlg.name_entry.set_text("   ")
    before = len(sim(win).custom_eqs)
    dlg.save()
    pump(lambda: False, 0.4)
    assert len(sim(win).custom_eqs) == before


# -- bundled models in the window ---------------------------------------------------------------


def test_a_both_buds_hold_model_gets_one_shared_hold_row(win):
    from realmebuds.sim import GestureEntry

    reconnect(win, sim_profile="realme-buds-air-3s")
    assert win.current_profile.hold_both and win.current_profile.key == "realme-buds-air-3s"
    sim(win).gestures = [GestureEntry(1, 1, 2, 0x01), GestureEntry(2, 1, 2, 0x01), GestureEntry(4, 1, 4, 0x11)]
    win.session.submit(lambda b: b.read_gestures())
    assert pump(lambda: (4, 4) in win.gesture_rows and win.gesture_rows[(4, 4)].get_visible())
    assert (1, 4) not in win.gesture_rows and (2, 4) not in win.gesture_rows  # no per-bud hold rows
    assert win.gesture_rows[(4, 4)].get_title() == "Both buds: touch and hold"
    codes = win._gesture_fns[(4, 4)]
    win.gesture_rows[(4, 4)].set_selected(codes.index(0x00))
    assert pump(lambda: next(e for e in sim(win).gestures if e.dev == 4).fn == 0x00)
    reconnect(win)


def test_a_single_button_model_has_no_right_bud_rows(win):
    reconnect(win, sim_profile="realme-buds-wireless-6")
    prof = win.current_profile
    assert prof.gesture_sides == (1,)
    assert all(side != model.SIDE_RIGHT for side, _ in win.gesture_rows)
    win.overrides.force_gestures = True
    win.overrides_changed()
    assert any(side == model.SIDE_RIGHT for side, _ in win.gesture_rows)  # the override opens it up
    reset(win)
    reconnect(win)


def test_a_model_without_noise_control_hides_that_section(win):
    reconnect(win, sim_profile="dizo-gopods")
    assert win.current_profile.key == "dizo-gopods" and not win.current_profile.has_anc
    assert win.anc_buttons == {} and win.level_rows == {} and win.cycle_row is None  # no mode buttons, no cycle
    # the group stays only if a switch of that group (such as Enhance voices) is reported
    assert win.noise_group.get_visible() == any(f.group == "noise" and f.id in win.state.features for f in win.current_profile.features)
    reconnect(win)


def test_a_model_with_ten_eq_bands_gets_ten_sliders(win):
    reconnect(win, sim_profile="oneplus-flow-buds")
    dlg = CustomEqDialog(win)
    assert len(dlg.scales) == 10 and list(dlg.scales)[0] == win.current_profile.custom_eq.freqs[0]
    reconnect(win)


def test_the_fit_test_row_follows_the_buds_capability_list(win):
    assert win.fit_row is not None and win.fit_row.get_visible()  # the Air7 Pro lists it
    # a buds whose capability list lacks the fit-test bit (9): the row goes away
    win.session.buds.state.capabilities = win.session.buds.state.capabilities - {9}
    win.refresh_ui()
    assert not win.fit_row.get_visible()
    win.overrides.force_capabilities = True
    win.overrides_changed()
    assert win.fit_row.get_visible()
    reset(win)
    reconnect(win)


def test_refused_write_puts_the_widgets_back(win):
    sim(win).worn = False
    current = sim(win).anc
    win.anc_buttons["off"].set_active(True)
    key = {model.ANC_TRANSPARENCY: "transparency", model.ANC_OFF: "off"}.get(current, "nc")
    assert pump(lambda: win.anc_buttons[key].get_active() and not win.anc_buttons["off"].get_active())
    assert sim(win).anc == current
    sim(win).worn = True


def test_every_parity_target_exists_in_the_gui(win):
    from realmebuds import parity

    adv = AdvancedDialog(win)
    objects = {"window": win, "advanced": adv, "hearing": HearingTestDialog(win), "eqdialog": CustomEqDialog(win)}
    for entry in parity.PARITY:
        owner, _, attr = entry.gui.partition(".")
        assert hasattr(objects[owner], attr), f"{entry.cli!r} says the GUI has {entry.gui}, but it does not"
    # every control on the Advanced dialog is either the counterpart of a CLI option or knowingly GUI-only
    targets = {e.gui.partition(".")[2] for e in parity.PARITY if e.gui.startswith("advanced.")}
    for name in dir(adv):
        if name.endswith(("_row", "_button", "_view", "_result", "_copy", "_clear", "_save")) and not name.startswith("_"):
            assert name in targets or name in parity.GUI_ONLY, f"Advanced has a control {name!r} with no CLI counterpart listed in parity.py"
    adv.dialog.close()
    objects["hearing"].dialog.close()


def test_disconnect_shows_status_and_reconnects(win):
    first = win.session.buds
    first.sim.drop_link()
    assert pump(lambda: win.stack.get_visible_child_name() == "status")
    assert pump(lambda: win.stack.get_visible_child_name() == "main" and win.session.buds is not first, timeout=10)
    screenshot(win, "after-reconnect.png")
