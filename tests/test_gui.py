"""Drives the real GTK widgets against the simulator. Needs a display: run with xvfb-run."""

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
from realmebuds.gui_dialogs import FitTestDialog, HearingTestDialog  # noqa: E402
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
def win():
    app = BudsApp(lambda: connect_buds(sim=True))
    app.register(None)
    app.activate()
    window = app.window
    assert pump(lambda: window.stack.get_visible_child_name() == "main"), "never connected"
    window.set_default_size(520, 1500)
    yield window
    window.session.stop()


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
    assert win.firmware_label.get_subtitle() == "138.138.105"
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
        assert set(model.GESTURE_OPTIONS[act]) <= set(codes)
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
    assert sim(win).tones[-1] == (1, 1, model.HEARING_START_DB)  # starts quiet

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


def test_refused_write_puts_the_widgets_back(win):
    sim(win).worn = False
    current = sim(win).anc
    win.anc_buttons["off"].set_active(True)
    key = {model.ANC_TRANSPARENCY: "transparency", model.ANC_OFF: "off"}.get(current, "nc")
    assert pump(lambda: win.anc_buttons[key].get_active() and not win.anc_buttons["off"].get_active())
    assert sim(win).anc == current
    sim(win).worn = True


def test_disconnect_shows_status_and_reconnects(win):
    first = win.session.buds
    first.sim.drop_link()
    assert pump(lambda: win.stack.get_visible_child_name() == "status")
    assert pump(lambda: win.stack.get_visible_child_name() == "main" and win.session.buds is not first, timeout=10)
    screenshot(win, "after-reconnect.png")
