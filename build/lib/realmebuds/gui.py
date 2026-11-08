"""GTK4 + libadwaita front end: ``realme-buds-gui``.

Which sections and rows appear is decided by the connected model's device profile (and by the
overrides in the Advanced dialog), so the same window serves any model that has a profile.
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import model, profiles  # noqa: E402
from .client import Buds, State  # noqa: E402
from .gui_advanced import AdvancedDialog  # noqa: E402
from .gui_dialogs import FitTestDialog, HearingTestDialog  # noqa: E402
from .gui_eq import CustomEqDialog, band_label  # noqa: E402
from .session import Session, connect_buds  # noqa: E402

APP_ID = "dev.realmebuds.Controller"
GOLDEN_SOUND_DEFAULT_ID = 0x0B
SECTION_FOR_GROUP = {"noise": "noise_group", "sound": "sound_group", "mode": "mode_group", "personal": "personal_group"}


@dataclass
class Overrides:
    """Everything the command line can bypass or choose, kept in one place (see parity.py)."""

    force_gestures: bool = False  # gesture --force
    force_hold_cycle: bool = False  # hold-cycle --force
    force_capabilities: bool = False  # fit-test --force, golden-sound --force
    skip_ear_scan: bool = False  # golden-sound test --skip-scan
    fit_timeout: float = 30.0  # fit-test --timeout
    profile: str | None = None  # --profile
    profile_file: str | None = None  # --profile-file
    address: str | None = None  # --addr
    channel: int | None = None  # --channel


class BudsWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, connector=None, overrides: Overrides | None = None, sim: bool = False) -> None:
        super().__init__(application=app, title="realme Buds", default_width=520, default_height=820)
        self.overrides = overrides or Overrides()
        self.trace: collections.deque[str] = collections.deque(maxlen=3000)
        self._advanced = None
        self._sim = sim
        self.sim_profile: str | None = None  # development hook: make the pretend buds another model
        self._updating = False
        self._applied: tuple | None = None
        self._owned: list[tuple[Adw.PreferencesGroup, Gtk.Widget]] = []
        self._custom_rows: list[Gtk.Widget] = []
        self._pending_dynamic: dict[int, int] = {}
        self._dynamic_timers: dict[int, int] = {}
        self._gesture_fns: dict[tuple[int, int], list[int]] = {}
        self._eq_ids: list[int] = []
        self._profile: profiles.DeviceProfile = profiles.generic_profile()
        self.anc_buttons: dict[str, Gtk.ToggleButton] = {}
        self.level_rows: dict[str, Adw.ComboRow] = {}
        self.level_row: Adw.ComboRow | None = None
        self.feature_rows: dict[str, Adw.SwitchRow] = {}
        self._feature_defs: dict[str, profiles.Feature] = {}
        self.gesture_rows: dict[tuple[int, int], Adw.ComboRow] = {}
        self.cycle_switches: dict[int, Adw.SwitchRow] = {}
        self.dynamic_scales: dict[int, Gtk.Scale] = {}
        self.cycle_row: Adw.ExpanderRow | None = None
        self.eq_row: Adw.ComboRow | None = None

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Read everything again")
        self.refresh_button.connect("clicked", lambda *_: self.refresh_from_buds())
        header.pack_end(self.refresh_button)
        self.advanced_button = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Advanced: overrides, tools, trace")
        self.advanced_button.connect("clicked", lambda *_: self.open_advanced())
        header.pack_end(self.advanced_button)
        toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.status_page = Adw.StatusPage(icon_name="bluetooth-symbolic", title="Connecting…")
        self.stack.add_named(self.status_page, "status")
        self.page = Adw.PreferencesPage()
        self.stack.add_named(self.page, "main")
        self._build_static()
        self.stack.set_visible_child_name("status")

        self._connector = connector or self._default_connector
        self.session = Session(
            self._connector,
            lambda st, msg: GLib.idle_add(self._on_status, st, msg),
            lambda ev, state: GLib.idle_add(self._on_event, ev),
        )
        self.session.start()
        self.connect("close-request", self._on_close)

    # -- connection -------------------------------------------------------------

    def _default_connector(self) -> Buds:
        ov = self.overrides
        return connect_buds(ov.address, ov.channel, self._sim, self._record_trace, profile=ov.profile, profile_file=ov.profile_file, sim_profile=self.sim_profile)

    def _record_trace(self, direction: str, data: bytes) -> None:
        line = f"{time.strftime('%H:%M:%S')} {direction} {data.hex(' ').upper()}"
        self.trace.append(line)
        adv = self._advanced
        if adv is not None:
            GLib.idle_add(adv.append_trace, line)

    def apply_connection_overrides(self) -> None:
        """Profile, address and channel overrides are used when connecting, so connect again."""
        self.status_page.set_title("Reconnecting…")
        self.session.reconnect()

    def overrides_changed(self) -> None:
        """A force-style override changed: show or hide rows without reconnecting."""
        self._applied = None
        if self.session.buds is not None:
            self._apply_profile(self.session.buds.active_profile)
            self.refresh_ui()

    def open_advanced(self) -> None:
        AdvancedDialog(self).present()

    @property
    def current_profile(self) -> profiles.DeviceProfile:
        return self._profile

    # -- construction -----------------------------------------------------------

    def _group(self, title: str, description: str = "") -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=title, description=description)
        self.page.add(g)
        return g

    def _build_static(self) -> None:
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

        # Groups whose content depends on the model; filled by _apply_profile.
        self.noise_group = self._group("Noise control")
        self.sound_group = self._group("Sound effects")
        self.dynamic_group = self._group("Dynamic audio", "Shape the sound by band")
        self.golden_group = self._group("Golden Sound", "A sound profile made from a short hearing test")
        self.mode_group = self._group("Mode")
        self.personal_group = self._group("Personalisation")
        self.touch_group = self._group("Touch controls", "Choose what each gesture does on each bud")

        find_group = self._group("Find my earbuds")
        find_row = Adw.ActionRow(title="Make the buds ring", subtitle="Take them out of your ears first")
        self.find_button = Gtk.Button(label="Ring", valign=Gtk.Align.CENTER)
        self.find_button.connect("clicked", self._on_find_clicked)
        find_row.add_suffix(self.find_button)
        find_group.add(find_row)

        about = self._group("About")
        self.model_label = Adw.ActionRow(title="Model", subtitle="–")
        self.firmware_label = Adw.ActionRow(title="Firmware", subtitle="–")
        about.add(self.model_label)
        about.add(self.firmware_label)

    def _own(self, group: Adw.PreferencesGroup, row: Gtk.Widget) -> Gtk.Widget:
        group.add(row)
        self._owned.append((group, row))
        return row

    def _clear(self) -> None:
        for group, row in self._owned:
            group.remove(row)
        self._owned.clear()
        self._custom_rows.clear()
        self.anc_buttons = {}
        self.level_rows = {}
        self.level_row = None
        self.feature_rows = {}
        self._feature_defs = {}
        self.gesture_rows = {}
        self.cycle_switches = {}
        self.dynamic_scales = {}
        self.cycle_row = None
        self.eq_row = None
        self._gesture_fns = {}

    def _apply_profile(self, prof: profiles.DeviceProfile) -> None:
        """(Re)build every model-dependent row from the profile and the overrides."""
        ov = self.overrides
        signature = (prof.key, prof.source, ov.force_gestures, ov.force_hold_cycle, ov.force_capabilities)
        if signature == self._applied:
            return
        self._applied = signature
        self._profile = prof
        self._updating = True
        try:
            self._clear()
            self._build_noise(prof)
            self._build_sound(prof)
            self._build_dynamic(prof)
            self._build_golden(prof)
            self._build_feature_rows(prof)
            self._build_personal_extras(prof)
            self._build_touch(prof)
            self.model_label.set_subtitle(prof.describe())
            self.set_title(prof.name if not prof.generic else "Earbuds")
        finally:
            self._updating = False

    # -- noise control ------------------------------------------------------------

    def _build_noise(self, prof: profiles.DeviceProfile) -> None:
        if not prof.has_anc:
            return
        mode_row = Adw.ActionRow(title="Mode")
        box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        first = None
        for key in prof.anc_buttons:
            grp = prof.group(key)
            label = grp.label if grp else prof.mode(key).label
            btn = Gtk.ToggleButton(label=label)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_anc_toggled, key)
            box.append(btn)
            self.anc_buttons[key] = btn
        mode_row.add_suffix(box)
        self._own(self.noise_group, mode_row)
        for grp in prof.anc_groups:
            modes = prof.group_modes(grp.key)
            combo = Adw.ComboRow(title=f"{grp.label} level", model=Gtk.StringList.new([m.label for m in modes]))
            default_idx = next((i for i, m in enumerate(modes) if m.key == grp.default), 0)
            combo.set_selected(default_idx)
            combo.connect("notify::selected", self._on_level_selected, grp.key)
            self._own(self.noise_group, combo)
            self.level_rows[grp.key] = combo
            if self.level_row is None:
                self.level_row = combo

    def _refresh_anc(self, st: State) -> None:
        prof = self._profile
        if not prof.has_anc:
            return
        mode = prof.mode_by_report(st.anc) if st.anc is not None else None
        key = None
        if mode is not None:
            key = mode.group or mode.key
        for k, btn in self.anc_buttons.items():
            btn.set_active(k == key)
        if key is None:
            for btn in self.anc_buttons.values():
                btn.set_active(False)
        if mode is not None and mode.group in self.level_rows:
            modes = prof.group_modes(mode.group)
            self.level_rows[mode.group].set_selected(next((i for i, m in enumerate(modes) if m.key == mode.key), 0))
        for gkey, combo in self.level_rows.items():
            if mode is not None and mode.group == gkey and mode.key == "smart" and st.smart_level:
                level = prof.mode_by_report(st.smart_level)
                combo.set_subtitle(f"Smart is currently using {level.label.lower()}" if level else "")
            else:
                combo.set_subtitle("")

    def _on_anc_toggled(self, button: Gtk.ToggleButton, key: str) -> None:
        if self._updating or not button.get_active():
            return
        prof = self._profile
        grp = prof.group(key)
        if grp is not None:
            combo = self.level_rows.get(key)
            modes = prof.group_modes(key)
            mode = modes[combo.get_selected()] if combo is not None and combo.get_selected() < len(modes) else prof.mode(grp.default)
        else:
            mode = prof.mode(key)
        self.act(lambda b: b.set_anc_mode(mode))

    def _on_level_selected(self, row: Adw.ComboRow, _pspec, gkey: str) -> None:
        btn = self.anc_buttons.get(gkey)
        if self._updating or btn is None or not btn.get_active():
            return
        modes = self._profile.group_modes(gkey)
        if row.get_selected() >= len(modes):
            return
        mode = modes[row.get_selected()]
        self.act(lambda b: b.set_anc_mode(mode))

    # -- sound effects: EQ and custom presets ------------------------------------------

    def _build_sound(self, prof: profiles.DeviceProfile) -> None:
        if prof.eq_presets or prof.custom_eq:
            self.eq_row = Adw.ComboRow(title="EQ mode", model=Gtk.StringList.new([]))
            self.eq_row.connect("notify::selected", self._on_eq_selected)
            self._own(self.sound_group, self.eq_row)
        if prof.custom_eq:
            new_row = Adw.ActionRow(title="Custom equaliser", subtitle="Make your own with sliders (command line: eq save)")
            new_button = Gtk.Button(label="New…", valign=Gtk.Align.CENTER)
            new_button.connect("clicked", lambda *_: CustomEqDialog(self).present())
            new_row.add_suffix(new_button)
            self._own(self.sound_group, new_row)
            self.new_eq_button = new_button

    def _refresh_eq(self, st: State) -> None:
        prof = self._profile
        if self.eq_row is None:
            return
        names = [p.label for p in prof.eq_presets]
        self._eq_ids = [p.id for p in prof.eq_presets]
        for c in st.custom_eqs:
            names.append(f"Custom: {c.name}")
            self._eq_ids.append(c.id)
        if st.eq is not None and st.eq not in self._eq_ids:
            names.append(f"Preset {st.eq}")
            self._eq_ids.append(st.eq)
        self.eq_row.set_model(Gtk.StringList.new(names))
        if st.eq in self._eq_ids:
            self.eq_row.set_selected(self._eq_ids.index(st.eq))
        self._refresh_custom_rows(st)

    def _refresh_custom_rows(self, st: State) -> None:
        for w in self._custom_rows:
            self.sound_group.remove(w)
            self._owned = [(g, r) for g, r in self._owned if r is not w]
        self._custom_rows = []
        if not self._profile.custom_eq:
            return
        for c in st.custom_eqs:
            summary = "  ".join(f"{band_label(hz)} {g:+d}" for hz, g in c.bands)
            row = Adw.ActionRow(title=c.name, subtitle=summary)
            edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Edit")
            edit.connect("clicked", lambda _b, eq=c: CustomEqDialog(self, eq).present())
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Delete")
            delete.connect("clicked", lambda _b, eq=c: self._confirm_delete_eq(eq))
            row.add_suffix(edit)
            row.add_suffix(delete)
            self._own(self.sound_group, row)
            self._custom_rows.append(row)

    def _confirm_delete_eq(self, eq) -> None:
        dialog = Adw.AlertDialog(heading=f"Delete “{eq.name}”?", body="This removes the preset from the buds.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "delete" and self.delete_custom_eq(eq))
        dialog.present(self)

    def delete_custom_eq(self, eq) -> None:
        self.act(lambda b: b.delete_custom_eq(eq.id), "Deleted")

    def _on_eq_selected(self, row: Adw.ComboRow, _pspec) -> None:
        if self._updating:
            return
        idx = row.get_selected()
        if idx >= len(self._eq_ids):
            return
        preset = self._eq_ids[idx]
        st = self.state
        if st is not None and st.eq == preset:
            return
        self.act(lambda b: b.select_eq(preset))

    # -- dynamic audio ------------------------------------------------------------

    def _build_dynamic(self, prof: profiles.DeviceProfile) -> None:
        feat = prof.feature("dynamic-audio")
        self.dynamic_switch = Adw.SwitchRow(title=feat.label if feat else "Dynamic audio")
        self.dynamic_switch.connect("notify::active", self._on_dynamic_switch)
        self._own(self.dynamic_group, self.dynamic_switch)
        lo, hi = prof.dynamic_range
        for band, name in prof.dynamic_bands.items():
            row = Adw.ActionRow(title=name)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
            scale.set_digits(0)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.set_hexpand(True)
            scale.set_size_request(240, -1)
            for v in (lo, 0, hi):
                scale.add_mark(v, Gtk.PositionType.BOTTOM, None)
            scale.connect("value-changed", self._on_dynamic_changed, band)
            row.add_suffix(scale)
            self._own(self.dynamic_group, row)
            self.dynamic_scales[band] = scale

    def _refresh_dynamic(self, st: State) -> None:
        prof = self._profile
        dyn_id = prof.feature("dynamic-audio").id if prof.feature("dynamic-audio") else None
        present = bool(st.dynamic_audio) and bool(self.dynamic_scales) or (dyn_id is not None and dyn_id in st.features)
        self.dynamic_group.set_visible(present)
        self.dynamic_switch.set_visible(dyn_id is not None and dyn_id in st.features)
        if dyn_id is not None and dyn_id in st.features:
            self.dynamic_switch.set_active(st.features[dyn_id])
        for b in st.dynamic_audio:
            scale = self.dynamic_scales.get(b.band)
            if scale and b.band not in self._dynamic_timers:
                scale.set_value(b.value)
            if scale:
                scale.set_sensitive(st.features.get(dyn_id, True) if dyn_id is not None else True)

    def _on_dynamic_switch(self, row: Adw.SwitchRow, _pspec) -> None:
        feat = self._profile.feature("dynamic-audio")
        if self._updating or feat is None:
            return
        on = row.get_active()
        self.act(lambda b: b.set_feature(feat.id, on))

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

    # -- Golden Sound and fit test ----------------------------------------------------

    def _show_capability(self, listed: bool) -> bool:
        return listed or self.overrides.force_capabilities

    def _build_golden(self, prof: profiles.DeviceProfile) -> None:
        self.golden_switch = Adw.SwitchRow(title="Golden Sound")
        self.golden_switch.connect("notify::active", self._on_golden_switch)
        self._own(self.golden_group, self.golden_switch)
        self.profile_row = Adw.ActionRow(title="Hearing profile", subtitle="None")
        button = Gtk.Button(label="Run hearing test…", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: HearingTestDialog(self).present())
        self.profile_row.add_suffix(button)
        self._own(self.golden_group, self.profile_row)

    def _refresh_golden(self, st: State) -> None:
        prof = self._profile
        feat = prof.feature("golden-sound")
        fid = feat.id if feat else GOLDEN_SOUND_DEFAULT_ID
        show = self._show_capability(prof.golden_sound) and (fid in st.features or bool(st.hearing_record and st.hearing_record.entries) or self.overrides.force_capabilities)
        self.golden_group.set_visible(show)
        self.golden_switch.set_visible(fid in st.features)
        if fid in st.features:
            self.golden_switch.set_active(st.features[fid])
        rec = st.hearing_record
        if rec and rec.entries:
            self.profile_row.set_subtitle(f"{rec.name or 'Unnamed'} (id {rec.id})")
        else:
            self.profile_row.set_subtitle("None yet. Run the hearing test to make one.")

    def _on_golden_switch(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._updating:
            return
        on = row.get_active()
        self.act(lambda b: b.set_golden_sound(on))

    def _build_personal_extras(self, prof: profiles.DeviceProfile) -> None:
        self.fit_row = None
        self.fit_button = None
        if self._show_capability(prof.fit_test):
            row = Adw.ActionRow(title="Earbud fit test", subtitle="Checks that the ear tips seal well")
            button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: FitTestDialog(self).present())
            row.add_suffix(button)
            self._own(self.personal_group, row)
            self.fit_row = row
            self.fit_button = button

    # -- feature switches ---------------------------------------------------------------

    def _build_feature_rows(self, prof: profiles.DeviceProfile) -> None:
        for feat in prof.features:
            if feat.key in profiles.RESERVED_FEATURE_KEYS:
                continue
            self._add_feature_row(prof, feat)

    def _add_feature_row(self, prof: profiles.DeviceProfile, feat: profiles.Feature) -> None:
        group = getattr(self, SECTION_FOR_GROUP[feat.group])
        subtitle = feat.note + ("" if feat.evidence in ("app", "reported") or not feat.note else "")
        if feat.evidence in ("family", "guess"):
            subtitle = (subtitle + "  ·  experimental").strip(" ·")
        row = Adw.SwitchRow(title=feat.label, subtitle=subtitle)
        row.connect("notify::active", self._on_feature_toggled, feat)
        row.set_visible(False)  # shown once the buds report the feature
        self._own(group, row)
        self.feature_rows[feat.key] = row
        self._feature_defs[feat.key] = feat

    def _refresh_features(self, st: State) -> None:
        prof = self._profile
        # a switch the buds report but the profile does not name still gets a row
        for fid in sorted(st.features):
            if prof.feature(fid) is None and prof.feature_for_id(fid).key not in self.feature_rows:
                self._updating, was = True, self._updating
                self._add_feature_row(prof, prof.feature_for_id(fid))
                self._updating = was
        for key, row in self.feature_rows.items():
            fid = self._feature_defs[key].id
            row.set_visible(fid in st.features)
            if fid in st.features:
                row.set_active(st.features[fid])

    def _on_feature_toggled(self, row: Adw.SwitchRow, _pspec, feat: profiles.Feature) -> None:
        if self._updating:
            return
        on = row.get_active()
        message = "The buds will reconnect in a few seconds" if feat.drops_link else None

        def do(b: Buds):
            if feat.key == "spatial":
                return b.set_spatial(on)
            return b.set_feature(feat.id, on, verify=not feat.drops_link)

        self.act(do, message)

    # -- touch controls -----------------------------------------------------------------

    def _build_touch(self, prof: profiles.DeviceProfile) -> None:
        ov = self.overrides
        actions = sorted(model.ACTION_LABELS) if ov.force_gestures else list(prof.gesture_actions)
        sides = [model.SIDE_LEFT, model.SIDE_RIGHT]
        side_names = {model.SIDE_LEFT: "Left", model.SIDE_RIGHT: "Right"}
        for side in sides:
            if side not in prof.gesture_sides and not ov.force_gestures:
                continue
            for act in actions:
                row = Adw.ComboRow(title=f"{side_names[side]} bud: {model.ACTION_LABELS[act].lower()}")
                row.connect("notify::selected", self._on_gesture_selected, side, act)
                self._own(self.touch_group, row)
                self.gesture_rows[(side, act)] = row
        if prof.hold_options:
            self.cycle_row = Adw.ExpanderRow(title="Touch and hold cycles through", subtitle="Used when a hold is set to “Switch noise control”")
            for opt in prof.hold_options:
                sw = Adw.SwitchRow(title=opt.label)
                sw.connect("notify::active", self._on_cycle_toggled, opt.bit)
                self.cycle_row.add_row(sw)
                self.cycle_switches[opt.bit] = sw
            self._own(self.touch_group, self.cycle_row)

    def _refresh_touch(self, st: State) -> None:
        prof = self._profile
        force = self.overrides.force_gestures
        all_codes = [g.code for g in model.GESTURE_FNS]
        for (side, act), row in self.gesture_rows.items():
            slots = [e for e in st.gestures if e.dev == side and e.act == act and e.btn != model.BTN_CALL]
            row.set_visible(bool(slots))
            if not slots:
                continue
            fn = slots[0].fn
            listed = prof.options_for(act)
            codes = list(all_codes if force or not listed else listed)
            if fn not in codes:
                codes.append(fn)  # set elsewhere, not offered here: show it rather than hide it
            labels = [model.GESTURE_FN_BY_CODE[c].label if c in model.GESTURE_FN_BY_CODE else f"Unknown (0x{c:02X})" for c in codes]
            self._gesture_fns[(side, act)] = codes
            row.set_model(Gtk.StringList.new(labels))
            row.set_selected(codes.index(fn))
        if self.cycle_row is not None:
            mask = st.hold_cycle or 0
            self.cycle_row.set_visible(any(e.fn == model.FN_ANC_CYCLE for e in st.gestures) or bool(mask))
            for bit, sw in self.cycle_switches.items():
                sw.set_active(bool(mask & bit))

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
        minimum = 1 if self.overrides.force_hold_cycle else self._profile.hold_min
        if bin(mask).count("1") < minimum:
            self.toast(f"Touch and hold needs at least {minimum} mode{'s' if minimum != 1 else ''} to cycle through (see Advanced to override)")
            self.refresh_ui()
            return
        self.act(lambda b: b.set_hold_cycle(mask))

    # -- find ------------------------------------------------------------------------

    def _on_find_clicked(self, _button) -> None:
        dialog = Adw.AlertDialog(heading="Make the buds ring?", body="They get loud. Take them out of your ears first.")
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

    # -- session callbacks (main thread) -----------------------------------------------

    def _on_status(self, status: str, message: str) -> bool:
        if status == "connected":
            if self.session.buds is not None:
                self._apply_profile(self.session.buds.active_profile)
            self.stack.set_visible_child_name("main")
            self.refresh_ui()
        else:
            titles = {"connecting": "Connecting…", "error": "Can’t reach the buds", "disconnected": "Disconnected"}
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

    # -- state -> widgets ----------------------------------------------------------------

    @property
    def state(self) -> State | None:
        buds = self.session.buds
        return buds.state if buds else None

    def refresh_from_buds(self) -> None:
        self.act(lambda b: b.sync(), None)

    def refresh_ui(self) -> None:
        st = self.state
        if st is None or self._applied is None:  # nothing to show until the model's rows exist
            return
        self._updating = True
        try:
            self._refresh_battery(st)
            self._refresh_anc(st)
            self._refresh_features(st)
            self._refresh_eq(st)
            self._refresh_dynamic(st)
            self._refresh_golden(st)
            self._refresh_touch(st)
            self.firmware_label.set_subtitle(f"{st.firmware or '–'}  ·  product id {st.product_id or '–'}")
            self.noise_group.set_visible(bool(self.anc_buttons) or any(f.group == "noise" for f in self._feature_defs.values()))
            self.sound_group.set_visible(self.eq_row is not None or any(f.group == "sound" for f in self._feature_defs.values()))
            self.touch_group.set_visible(bool(self.gesture_rows) or self.cycle_row is not None)
        finally:
            self._updating = False

    def _refresh_battery(self, st: State) -> None:
        for comp, (row, bar, label) in self.battery_rows.items():
            level = st.battery.get(comp)
            row.set_visible(level is not None or comp != model.COMPONENT_CASE)
            bar.set_value(level[0] if level else 0)
            label.set_label(f"{level[0]}%" + ("⚡" if level and level[1] else "") if level else "–")
            wear = st.wearing.get(comp)
            row.set_subtitle(model.wear_label(wear) if wear is not None else "")

    # -- actions ------------------------------------------------------------------------

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


class BudsApp(Adw.Application):
    def __init__(self, connector=None, overrides: Overrides | None = None, sim: bool = False) -> None:
        super().__init__(application_id=APP_ID)
        self.connector = connector
        self.overrides = overrides
        self.sim = sim
        self.window: BudsWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = BudsWindow(self, self.connector, self.overrides, self.sim)
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="realme-buds-gui", description="GTK front end for OPPO/realme-family earbuds")
    ap.add_argument("--addr", help="Bluetooth address of the buds")
    ap.add_argument("--channel", type=int, help="RFCOMM channel (default: look it up over SDP)")
    ap.add_argument("--profile", metavar="KEY", help="use this device profile instead of recognising the buds")
    ap.add_argument("--profile-file", metavar="FILE", help="use the profile in this JSON file")
    ap.add_argument("--sim", action="store_true", help="use pretend buds, to try the interface")
    args = ap.parse_args(argv)
    ov = Overrides(profile=args.profile, profile_file=args.profile_file, address=args.addr, channel=args.channel)
    return BudsApp(overrides=ov, sim=args.sim).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
