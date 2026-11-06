"""GTK4 + libadwaita front end: ``realme-buds-gui``."""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import model  # noqa: E402
from .client import Buds, State  # noqa: E402
from .session import Session, connect_buds  # noqa: E402

APP_ID = "dev.realmebuds.Controller"

# Which group each feature switch lives in. Rows stay hidden until the buds report the feature.
FEATURE_GROUP = {
    "wind": "noise",
    "enhance-voices": "noise",
    "spatial": "sound",
    "lhdc": "sound",
    "game-mode": "mode",
    "wear-detect": "personal",
    "auto-answer": "personal",
    "dual": "personal",
}
FEATURE_ORDER = ["wind", "enhance-voices", "spatial", "lhdc", "game-mode", "wear-detect", "auto-answer", "dual"]
ANC_LEVEL_LABELS = [m.label.split(": ")[-1] for m in model.ANC_LEVELS]
HOLD_CYCLE_ROWS = (
    ("Off", model.ANC_OFF),
    ("Transparency", model.ANC_TRANSPARENCY),
    ("Noise cancellation", model.ANC_MAX),
)
GESTURE_ROWS = [(side, act) for side in (model.SIDE_LEFT, model.SIDE_RIGHT) for act in model.REMAPPABLE_ACTIONS]


class BudsWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, connector) -> None:
        super().__init__(application=app, title="realme Buds Air7 Pro", default_width=520, default_height=820)
        self._updating = False
        self._pending_dynamic: dict[int, int] = {}
        self._dynamic_timers: dict[int, int] = {}
        self._gesture_fns: dict[tuple[int, int], list[int]] = {}
        self._eq_ids: list[int] = []

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Read everything again")
        self.refresh_button.connect("clicked", lambda *_: self.refresh_from_buds())
        header.pack_end(self.refresh_button)
        toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.status_page = Adw.StatusPage(icon_name="bluetooth-symbolic", title="Connecting…")
        self.stack.add_named(self.status_page, "status")
        self.page = Adw.PreferencesPage()
        self.stack.add_named(self.page, "main")
        self._build_main()
        self.stack.set_visible_child_name("status")

        self.session = Session(
            connector,
            lambda st, msg: GLib.idle_add(self._on_status, st, msg),
            lambda ev, state: GLib.idle_add(self._on_event, ev),
        )
        self.session.start()
        self.connect("close-request", self._on_close)

    # -- construction -----------------------------------------------------

    def _group(self, title: str, description: str = "") -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=title, description=description)
        self.page.add(g)
        return g

    def _switch_row(self, title: str, subtitle: str = "") -> Adw.SwitchRow:
        return Adw.SwitchRow(title=title, subtitle=subtitle)

    def _build_main(self) -> None:
        # Battery
        self.battery_group = self._group("Battery")
        self.battery_rows: dict[int, tuple[Adw.ActionRow, Gtk.LevelBar, Gtk.Label]] = {}
        for comp, name in ((1, "Left bud"), (2, "Right bud"), (3, "Case")):
            row = Adw.ActionRow(title=name)
            label = Gtk.Label(label="–", width_chars=5, xalign=1)
            bar = Gtk.LevelBar(min_value=0, max_value=100, value=0, valign=Gtk.Align.CENTER, width_request=110)
            row.add_suffix(bar)
            row.add_suffix(label)
            self.battery_group.add(row)
            self.battery_rows[comp] = (row, bar, label)

        # Noise control
        self.noise_group = self._group("Noise control")
        mode_row = Adw.ActionRow(title="Mode")
        box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self.anc_buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for key, label in (("nc", "Noise cancellation"), ("off", "Off"), ("transparency", "Transparency")):
            btn = Gtk.ToggleButton(label=label)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_anc_toggled, key)
            box.append(btn)
            self.anc_buttons[key] = btn
        mode_row.add_suffix(box)
        self.noise_group.add(mode_row)
        self.level_row = Adw.ComboRow(title="Noise cancellation level", model=Gtk.StringList.new(ANC_LEVEL_LABELS))
        self.level_row.connect("notify::selected", self._on_level_selected)
        self.noise_group.add(self.level_row)

        # Sound effects
        self.sound_group = self._group("Sound effects")
        self.eq_row = Adw.ComboRow(title="EQ mode", model=Gtk.StringList.new([]))
        self.eq_row.connect("notify::selected", self._on_eq_selected)
        self.sound_group.add(self.eq_row)

        self.dynamic_group = self._group("Dynamic audio", "Bass, mid and treble shaping, −5 to +5")
        self.dynamic_switch = self._switch_row("Dynamic audio")
        self.dynamic_switch.connect("notify::active", self._on_dynamic_switch)
        self.dynamic_group.add(self.dynamic_switch)
        self.dynamic_scales: dict[int, Gtk.Scale] = {}
        for band, name in model.DYNAMIC_BANDS.items():
            row = Adw.ActionRow(title=name)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, *model.DYNAMIC_RANGE, 1)
            scale.set_digits(0)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.set_hexpand(True)
            scale.set_size_request(240, -1)
            for v in (-5, 0, 5):
                scale.add_mark(v, Gtk.PositionType.BOTTOM, None)
            scale.connect("value-changed", self._on_dynamic_changed, band)
            row.add_suffix(scale)
            self.dynamic_group.add(row)
            self.dynamic_scales[band] = scale

        self.mode_group = self._group("Mode")
        self.personal_group = self._group("Personalisation")

        # Touch controls
        self.touch_group = self._group("Touch controls", "Choose what each gesture does on each bud")
        self.gesture_rows: dict[tuple[int, int], Adw.ComboRow] = {}
        side_names = {model.SIDE_LEFT: "Left", model.SIDE_RIGHT: "Right"}
        for side, act in GESTURE_ROWS:
            row = Adw.ComboRow(title=f"{side_names[side]} bud: {model.ACTION_LABELS[act].lower()}")
            row.set_model(Gtk.StringList.new([g.label for g in model.GESTURE_FNS]))
            row.connect("notify::selected", self._on_gesture_selected, side, act)
            self.touch_group.add(row)
            self.gesture_rows[(side, act)] = row
        self.cycle_row = Adw.ExpanderRow(title="Touch and hold cycles through", subtitle="Used when a hold is set to “Switch noise control”")
        self.cycle_switches: dict[int, Adw.SwitchRow] = {}
        for label, bit in HOLD_CYCLE_ROWS:
            sw = self._switch_row(label)
            sw.connect("notify::active", self._on_cycle_toggled, bit)
            self.cycle_row.add_row(sw)
            self.cycle_switches[bit] = sw
        self.touch_group.add(self.cycle_row)

        # Feature switches
        self.feature_rows: dict[str, Adw.SwitchRow] = {}
        groups = {"noise": self.noise_group, "sound": self.sound_group, "mode": self.mode_group, "personal": self.personal_group}
        for key in FEATURE_ORDER:
            feat = model.FEATURE_BY_KEY[key]
            row = self._switch_row(feat.label, feat.note)
            row.connect("notify::active", self._on_feature_toggled, feat)
            groups[FEATURE_GROUP[key]].add(row)
            self.feature_rows[key] = row

        find_group = self._group("Find my earbuds")
        find_row = Adw.ActionRow(title="Make the buds ring", subtitle="Take them out of your ears first")
        button = Gtk.Button(label="Ring", valign=Gtk.Align.CENTER)
        button.connect("clicked", self._on_find_clicked)
        find_row.add_suffix(button)
        find_group.add(find_row)

        self.firmware_label = Adw.ActionRow(title="Firmware", subtitle="–")
        about = self._group("About")
        about.add(self.firmware_label)

    # -- session callbacks (main thread) ------------------------------------

    def _on_status(self, status: str, message: str) -> bool:
        if status == "connected":
            self.stack.set_visible_child_name("main")
            self.refresh_ui()
        else:
            titles = {
                "connecting": "Connecting…",
                "error": "Can’t reach the buds",
                "disconnected": "Disconnected",
            }
            self.status_page.set_title(titles.get(status, status))
            self.status_page.set_description(message or "Make sure the earbuds are connected to this computer.")
            self.stack.set_visible_child_name("status")
        return False

    def _on_event(self, event: str) -> bool:
        if event != "disconnected":
            self.refresh_ui()
        return False

    def _on_close(self, *_args) -> bool:
        self.session.stop()
        return False

    # -- state -> widgets ---------------------------------------------------

    @property
    def state(self) -> State | None:
        buds = self.session.buds
        return buds.state if buds else None

    def refresh_from_buds(self) -> None:
        self.act(lambda b: b.sync(), None)

    def refresh_ui(self) -> None:
        st = self.state
        if st is None:
            return
        self._updating = True
        try:
            self._refresh_battery(st)
            self._refresh_anc(st)
            self._refresh_features(st)
            self._refresh_eq(st)
            self._refresh_dynamic(st)
            self._refresh_touch(st)
            self.firmware_label.set_subtitle(st.firmware or "–")
        finally:
            self._updating = False

    def _refresh_battery(self, st: State) -> None:
        for comp, (row, bar, label) in self.battery_rows.items():
            level = st.battery.get(comp)
            known = level is not None
            row.set_visible(known or comp != model.COMPONENT_CASE)
            bar.set_value(level[0] if level else 0)
            label.set_label(f"{level[0]}%" + ("⚡" if level and level[1] else "") if level else "–")
            wear = st.wearing.get(comp)
            row.set_subtitle(model.wear_label(wear) if wear is not None else "")

    def _refresh_anc(self, st: State) -> None:
        mask = st.anc
        key = "off" if mask == model.ANC_OFF else "transparency" if mask == model.ANC_TRANSPARENCY else "nc" if mask else None
        for k, btn in self.anc_buttons.items():
            btn.set_active(k == key)
        if key is None:
            for btn in self.anc_buttons.values():
                btn.set_active(False)
        for i, mode in enumerate(model.ANC_LEVELS):
            if mode.bit == mask:
                self.level_row.set_selected(i)
        if mask == model.ANC_SMART and st.smart_level:
            self.level_row.set_subtitle(f"Smart is currently using {model.anc_label(st.smart_level).split(': ')[-1].lower()}")
        else:
            self.level_row.set_subtitle("")

    def _refresh_features(self, st: State) -> None:
        for key, row in self.feature_rows.items():
            fid = model.FEATURE_BY_KEY[key].id
            row.set_visible(fid in st.features)
            if fid in st.features:
                row.set_active(st.features[fid])

    def _refresh_eq(self, st: State) -> None:
        names = [p.label for p in model.EQ_PRESETS]
        self._eq_ids = [p.id for p in model.EQ_PRESETS]
        if st.eq is not None and st.eq not in model.EQ_BY_ID:
            custom = next((e for e in st.custom_eqs if e.id == st.eq), None)
            names.append(f"Custom: {custom.name}" if custom else f"Preset {st.eq}")
            self._eq_ids.append(st.eq)
        self.eq_row.set_model(Gtk.StringList.new(names))
        if st.eq in self._eq_ids:
            self.eq_row.set_selected(self._eq_ids.index(st.eq))

    def _refresh_dynamic(self, st: State) -> None:
        present = bool(st.dynamic_audio)
        self.dynamic_group.set_visible(present or 0x1D in st.features)
        self.dynamic_switch.set_visible(0x1D in st.features)
        if 0x1D in st.features:
            self.dynamic_switch.set_active(st.features[0x1D])
        for b in st.dynamic_audio:
            scale = self.dynamic_scales.get(b.band)
            if scale and b.band not in self._dynamic_timers:
                scale.set_value(b.value)
            if scale:
                scale.set_sensitive(st.features.get(0x1D, True))

    def _refresh_touch(self, st: State) -> None:
        for (side, act), row in self.gesture_rows.items():
            slots = [e for e in st.gestures if e.dev == side and e.act == act and e.btn != model.BTN_CALL]
            row.set_visible(bool(slots))
            if not slots:
                continue
            codes = [g.code for g in model.GESTURE_FNS]
            fn = slots[0].fn
            if fn not in codes:
                codes = codes + [fn]
                row.set_model(Gtk.StringList.new([g.label for g in model.GESTURE_FNS] + [f"Unknown (0x{fn:02X})"]))
            self._gesture_fns[(side, act)] = codes
            row.set_selected(codes.index(fn))
        mask = st.hold_cycle or 0
        self.cycle_row.set_visible(any(e.fn == model.FN_ANC_CYCLE for e in st.gestures) or bool(mask))
        for bit, sw in self.cycle_switches.items():
            sw.set_active(bool(mask & bit))

    # -- actions ------------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def act(self, fn, ok_message: str | None = None) -> None:
        """Run ``fn(buds)`` on the session thread; show errors and re-sync the widgets after."""

        def done(_result, err) -> None:
            GLib.idle_add(self._after_action, err, ok_message)

        self.session.submit(fn, done)

    def _after_action(self, err: Exception | None, ok_message: str | None) -> bool:
        if err is not None:
            self.toast(str(err))
        elif ok_message:
            self.toast(ok_message)
        self.refresh_ui()  # on failure this puts the widgets back to what the buds really have
        return False

    def _on_anc_toggled(self, button: Gtk.ToggleButton, key: str) -> None:
        if self._updating or not button.get_active():
            return
        if key == "off":
            mask = model.ANC_OFF
        elif key == "transparency":
            mask = model.ANC_TRANSPARENCY
        else:
            mask = model.ANC_LEVELS[self.level_row.get_selected()].bit
        self.act(lambda b: b.set_anc(mask))

    def _on_level_selected(self, row: Adw.ComboRow, _pspec) -> None:
        if self._updating or not self.anc_buttons["nc"].get_active():
            return
        mask = model.ANC_LEVELS[row.get_selected()].bit
        self.act(lambda b: b.set_anc(mask))

    def _on_feature_toggled(self, row: Adw.SwitchRow, _pspec, feat: model.Feature) -> None:
        if self._updating:
            return
        on = row.get_active()
        message = "The buds will reconnect in a few seconds" if feat.drops_link else None

        def do(b: Buds):
            if feat.key == "spatial":
                return b.set_spatial(on)
            return b.set_feature(feat.id, on, verify=not feat.drops_link)

        self.act(do, message)

    def _on_dynamic_switch(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._updating:
            return
        on = row.get_active()
        self.act(lambda b: b.set_feature(0x1D, on))

    def _on_dynamic_changed(self, scale: Gtk.Scale, band: int) -> None:
        if self._updating:
            return
        self._pending_dynamic[band] = int(round(scale.get_value()))
        if band in self._dynamic_timers:
            GLib.source_remove(self._dynamic_timers[band])
        self._dynamic_timers[band] = GLib.timeout_add(400, self._flush_dynamic, band)

    def _flush_dynamic(self, band: int) -> bool:
        self._dynamic_timers.pop(band, None)
        value = self._pending_dynamic.pop(band, None)
        if value is not None:
            self.act(lambda b: b.set_dynamic_band(band, value))
        return False

    def _on_eq_selected(self, row: Adw.ComboRow, _pspec) -> None:
        if self._updating:
            return
        idx = row.get_selected()
        if idx >= len(self._eq_ids):
            return
        preset = self._eq_ids[idx]
        if preset not in model.EQ_BY_ID:
            return  # the entry that just shows a custom preset
        self.act(lambda b: b.set_eq_preset(preset))

    def _on_gesture_selected(self, row: Adw.ComboRow, _pspec, side: int, act: int) -> None:
        if self._updating:
            return
        codes = self._gesture_fns.get((side, act))
        idx = row.get_selected()
        if not codes or idx >= len(codes):
            return
        code = codes[idx]
        self.act(lambda b: b.set_gesture(side, act, code))

    def _on_cycle_toggled(self, row: Adw.SwitchRow, _pspec, bit: int) -> None:
        if self._updating:
            return
        mask = 0
        for b, sw in self.cycle_switches.items():
            if sw.get_active():
                mask |= b
        if mask == 0:
            self.toast("Touch and hold needs at least one mode to cycle through")
            self.refresh_ui()
            return
        self.act(lambda b: b.set_hold_cycle(mask))

    def _on_find_clicked(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading="Make the buds ring?",
            body="They get loud. Take them out of your ears first.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ring", "Ring")
        dialog.set_response_appearance("ring", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def chosen(_d, response: str) -> None:
            if response == "ring":
                self.act(lambda b: b.find_buds(True), "Ringing… use the button again to stop")
                GLib.timeout_add_seconds(10, self._stop_ringing)

        dialog.connect("response", chosen)
        dialog.present(self)

    def _stop_ringing(self) -> bool:
        self.session.submit(lambda b: b.find_buds(False))
        return False


class BudsApp(Adw.Application):
    def __init__(self, connector) -> None:
        super().__init__(application_id=APP_ID)
        self.connector = connector
        self.window: BudsWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = BudsWindow(self, self.connector)
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="realme-buds-gui", description="GTK front end for realme Buds Air7 Pro")
    ap.add_argument("--addr", help="Bluetooth address of the buds")
    ap.add_argument("--channel", type=int, help="RFCOMM channel (default: look it up over SDP)")
    ap.add_argument("--sim", action="store_true", help="use pretend buds, to try the interface")
    args = ap.parse_args(argv)
    app = BudsApp(lambda: connect_buds(args.addr, args.channel, args.sim))
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
