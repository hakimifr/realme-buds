"""The Advanced dialog: every bypass and tool the command line has.

    CLI                              here
    gesture/hold-cycle --force       force_gestures_row / force_hold_row
    fit-test/golden-sound --force    force_caps_row
    golden-sound test --skip-scan    skip_scan_row
    fit-test --timeout               fit_timeout_row
    --profile / --profile-file       profile_row / profile_file_button
    --addr / --channel               address_row / channel_row
    devices                          devices_row
    features --scan                  scan_button
    raw                              raw_send_button
    status --json                    json_button
    profile detect                   draft_button
    --trace                          trace_view

The mapping is also written down in ``parity.py`` and checked by a test, so the two front ends
cannot drift apart without a test failing.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import profiles  # noqa: E402
from .cli import state_to_dict  # noqa: E402
from .profiles.draft import build_draft  # noqa: E402
from .transport import list_devices  # noqa: E402


class AdvancedDialog:
    def __init__(self, window) -> None:
        self.window = window
        self.ov = window.overrides
        self.dialog = Adw.Dialog(title="Advanced", content_width=560, content_height=720)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        toolbar.set_content(self.page)
        self.dialog.set_child(toolbar)
        self._build_overrides()
        self._build_device()
        self._build_tools()
        self._build_trace()
        self.dialog.connect("closed", lambda *_: setattr(window, "_advanced", None))

    def present(self) -> None:
        self.window._advanced = self
        self.refresh_trace()
        self.dialog.present(self.window)

    # -- overrides (take effect immediately, kept until the app closes) ----------------

    def _switch(self, group, title, subtitle, attr) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(getattr(self.ov, attr)))
        row.connect("notify::active", lambda r, _p: self._set_override(attr, r.get_active()))
        group.add(row)
        return row

    def _set_override(self, attr: str, value) -> None:
        setattr(self.ov, attr, value)
        self.window.overrides_changed()

    def _build_overrides(self) -> None:
        g = Adw.PreferencesGroup(
            title="Overrides",
            description="Do what the model's profile does not list. These stay until you close the app. The buds may refuse; every write is checked.",
        )
        self.page.add(g)
        self.force_gestures_row = self._switch(g, "Offer every touch gesture and function", "Not only what the model's profile lists (command line: gesture --force)", "force_gestures")
        self.force_hold_row = self._switch(g, "Allow fewer touch-and-hold modes than the minimum", "(command line: hold-cycle --force)", "force_hold_cycle")
        self.force_caps_row = self._switch(g, "Show Fit test and Golden Sound even if the profile does not list them", "(command line: fit-test --force, golden-sound --force)", "force_capabilities")
        self.skip_scan_row = self._switch(g, "Skip the ear scan in the Golden Sound test", "Not what the official app does (command line: golden-sound test --skip-scan)", "skip_ear_scan")
        self.fit_timeout_row = Adw.SpinRow(title="Fit test timeout (seconds)", subtitle="(command line: fit-test --timeout)", adjustment=Gtk.Adjustment(value=self.ov.fit_timeout, lower=5, upper=120, step_increment=5))
        self.fit_timeout_row.connect("notify::value", lambda r, _p: self._set_override("fit_timeout", float(r.get_value())))
        g.add(self.fit_timeout_row)

    # -- which buds and which profile --------------------------------------------------

    def _build_device(self) -> None:
        g = Adw.PreferencesGroup(title="Device", description="Applied when you press Apply and reconnect.")
        self.page.add(g)
        self.model_row = Adw.ActionRow(title="Recognised as", subtitle=self.window.current_profile.describe())
        g.add(self.model_row)

        self._profile_keys = [None, *sorted(profiles.load_all())]
        labels = ["Recognise the buds automatically"] + [f"Use profile: {k}" for k in self._profile_keys[1:]]
        self.profile_row = Adw.ComboRow(title="Device profile", subtitle="(command line: --profile)", model=Gtk.StringList.new(labels))
        if self.ov.profile in self._profile_keys:
            self.profile_row.set_selected(self._profile_keys.index(self.ov.profile))
        g.add(self.profile_row)

        self.profile_file_button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        self.profile_file_button.connect("clicked", lambda *_: self._choose_profile_file())
        self.profile_file_row = Adw.ActionRow(title="Profile from a file", subtitle=self.ov.profile_file or "(command line: --profile-file)")
        self.profile_file_row.add_suffix(self.profile_file_button)
        g.add(self.profile_file_row)

        self.address_row = Adw.EntryRow(title="Bluetooth address (blank: the buds connected here)", text=self.ov.address or "")
        g.add(self.address_row)
        self.channel_row = Adw.SpinRow(title="RFCOMM channel (0: look it up automatically)", adjustment=Gtk.Adjustment(value=self.ov.channel or 0, lower=0, upper=30, step_increment=1))
        g.add(self.channel_row)

        self.apply_button = Gtk.Button(label="Apply and reconnect", css_classes=["suggested-action"], halign=Gtk.Align.END)
        self.apply_button.connect("clicked", lambda *_: self.apply_device())
        g.add(self.apply_button)

        self.devices_row = Adw.ExpanderRow(title="Bluetooth devices", subtitle="(command line: devices)")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Refresh the list")
        refresh.connect("clicked", lambda *_: self.refresh_devices_async())
        self.devices_row.add_suffix(refresh)
        g.add(self.devices_row)
        self._device_rows: list[Gtk.Widget] = []

    def apply_device(self) -> None:
        idx = self.profile_row.get_selected()
        self.ov.profile = self._profile_keys[idx] if 0 <= idx < len(self._profile_keys) else None
        self.ov.address = self.address_row.get_text().strip() or None
        ch = int(self.channel_row.get_value())
        self.ov.channel = ch or None
        self.window.apply_connection_overrides()

    def _choose_profile_file(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a profile (JSON)")
        flt = Gtk.FileFilter()
        flt.add_pattern("*.json")
        flt.set_name("JSON profiles")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)

        def done(d, result):
            try:
                f = d.open_finish(result)
            except GLib.Error:
                return
            self.set_profile_file(f.get_path())

        dialog.open(self.window, None, done)

    def set_profile_file(self, path: str | None) -> None:
        self.ov.profile_file = path or None
        self.profile_file_row.set_subtitle(path or "(command line: --profile-file)")

    def refresh_devices(self, devices) -> None:
        for w in self._device_rows:
            self.devices_row.remove(w)
        self._device_rows = []
        for d in devices:
            row = Adw.ActionRow(title=d.name or d.addr, subtitle=f"{d.addr}  ·  {'connected' if d.connected else 'not connected'}" + ("  ·  has the control service" if d.has_control_service else ""))
            use = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
            use.connect("clicked", lambda _b, a=d.addr: self.address_row.set_text(a))
            row.add_suffix(use)
            self.devices_row.add_row(row)
            self._device_rows.append(row)

    def refresh_devices_async(self) -> None:
        def work() -> None:
            try:
                devices = list_devices()
            except Exception as e:  # bluetoothctl missing, no adapter...
                GLib.idle_add(self.window.toast, f"Could not list devices: {e}")
                return
            GLib.idle_add(self.refresh_devices, devices)

        threading.Thread(target=work, daemon=True).start()

    # -- tools ---------------------------------------------------------------------------

    def _build_tools(self) -> None:
        g = Adw.PreferencesGroup(title="Tools")
        self.page.add(g)

        self.scan_button = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        self.scan_button.connect("clicked", lambda *_: self.scan_features())
        scan_row = Adw.ActionRow(title="Scan feature ids", subtitle="Ask for every id worth knowing (command line: features --scan). Toggle a setting in the vendor app and scan again to find which id it is.")
        scan_row.add_suffix(self.scan_button)
        g.add(scan_row)
        self.scan_result = Gtk.Label(xalign=0, selectable=True, wrap=True, css_classes=["monospace"])
        g.add(self.scan_result)

        self.raw_cmd_row = Adw.EntryRow(title="Raw command (hex, e.g. 0106)")
        self.raw_payload_row = Adw.EntryRow(title="Payload (hex, optional, e.g. 0101)")
        self.raw_send_button = Gtk.Button(label="Send", valign=Gtk.Align.CENTER)
        self.raw_send_button.connect("clicked", lambda *_: self.send_raw())
        self.raw_cmd_row.add_suffix(self.raw_send_button)
        g.add(self.raw_cmd_row)
        g.add(self.raw_payload_row)
        self.raw_result = Gtk.Label(xalign=0, selectable=True, wrap=True, css_classes=["monospace"])
        g.add(self.raw_result)

        self.json_button = Gtk.Button(label="Copy", valign=Gtk.Align.CENTER)
        self.json_button.connect("clicked", lambda *_: self.copy(self.status_json(), "Status copied as JSON"))
        json_row = Adw.ActionRow(title="Copy status as JSON", subtitle="(command line: status --json)")
        json_row.add_suffix(self.json_button)
        g.add(json_row)

        self.draft_button = Gtk.Button(label="Save…", valign=Gtk.Align.CENTER)
        self.draft_button.connect("clicked", lambda *_: self.save_draft())
        draft_row = Adw.ActionRow(title="Make a draft profile from these buds", subtitle="A starting point for supporting another model (command line: profile detect). See docs/ADDING_A_DEVICE.md.")
        draft_row.add_suffix(self.draft_button)
        g.add(draft_row)

    def scan_features(self) -> None:
        self.scan_result.set_label("Scanning…")

        def done(found, err) -> None:
            def show() -> bool:
                if err is not None:
                    self.scan_result.set_label(str(err))
                    return False
                prof = self.window.current_profile
                lines = [f"0x{fid:02X}  {'on ' if val else 'off'}  {(prof.feature(fid).label if prof.feature(fid) else '(not identified)')}" for fid, val in sorted(found.items())]
                self.scan_result.set_label("\n".join(lines) or "The buds reported no features")
                return False

            GLib.idle_add(show)

        self.window.session.submit(lambda b: b.scan_features(), done)

    def send_raw(self) -> None:
        try:
            cmd = int(self.raw_cmd_row.get_text().strip(), 16)
            payload = bytes.fromhex(self.raw_payload_row.get_text().strip().replace(" ", ""))
        except ValueError:
            self.raw_result.set_label("The command and the payload must be hex, e.g. 0106 or 010c / 0101")
            return
        self.raw_result.set_label("Sending…")

        def work(b):
            try:
                pkt = b.request(cmd, payload, ok=False, timeout=3.0)
            except TimeoutError:
                return "no reply within 3 s (some writes are applied without an acknowledgement; read the value back to check)"
            return f"reply 0x{pkt.cmd:04X} status={pkt.status}: {pkt.payload.hex(' ')}"

        self.window.session.submit(work, lambda res, err: GLib.idle_add(self.raw_result.set_label, str(err) if err else res))

    def status_json(self) -> str:
        buds = self.window.session.buds
        if buds is None:
            return "{}"
        return json.dumps(state_to_dict(buds.state, buds.active_profile), indent=2)

    def draft_text(self) -> str:
        buds = self.window.session.buds
        if buds is None:
            raise RuntimeError("not connected")
        return json.dumps(build_draft(buds), indent=2) + "\n"

    def save_draft(self) -> None:
        try:
            text = self.draft_text()
        except Exception as e:
            self.window.toast(f"Could not make a draft: {e}")
            return
        dialog = Gtk.FileDialog(title="Save draft profile", initial_name="draft-profile.json")

        def done(d, result):
            try:
                f = d.save_finish(result)
            except GLib.Error:
                return
            Path(f.get_path()).write_text(text, encoding="utf-8")
            self.window.toast(f"Draft saved. Edit it, then put it in {profiles.user_profile_dirs()[0]}")

        dialog.save(self.window, None, done)

    def copy(self, text: str, message: str) -> None:
        Gdk.Display.get_default().get_clipboard().set(text)
        self.window.toast(message)

    # -- trace ---------------------------------------------------------------------------

    def _build_trace(self) -> None:
        g = Adw.PreferencesGroup(title="Trace", description="Every frame sent to and received from the buds (command line: --trace). Useful when something does not work.")
        self.page.add(g)
        self.trace_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR, height_request=220)
        scroll = Gtk.ScrolledWindow(child=self.trace_view, min_content_height=220, css_classes=["card"])
        g.add(scroll)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6)
        self.trace_copy = Gtk.Button(label="Copy")
        self.trace_copy.connect("clicked", lambda *_: self.copy(self.trace_text(), "Trace copied"))
        self.trace_clear = Gtk.Button(label="Clear")
        self.trace_clear.connect("clicked", lambda *_: (self.window.trace.clear(), self.refresh_trace()))
        self.trace_save = Gtk.Button(label="Save…")
        self.trace_save.connect("clicked", lambda *_: self.save_trace())
        for b in (self.trace_copy, self.trace_clear, self.trace_save):
            row.append(b)
        g.add(row)

    def trace_text(self) -> str:
        return "\n".join(self.window.trace)

    def refresh_trace(self) -> None:
        self.trace_view.get_buffer().set_text(self.trace_text())

    def append_trace(self, line: str) -> bool:
        buf = self.trace_view.get_buffer()
        buf.insert(buf.get_end_iter(), ("\n" if buf.get_char_count() else "") + line)
        return False

    def save_trace(self) -> None:
        text = self.trace_text()
        dialog = Gtk.FileDialog(title="Save trace", initial_name="realme-buds-trace.txt")

        def done(d, result):
            try:
                f = d.save_finish(result)
            except GLib.Error:
                return
            Path(f.get_path()).write_text(text + "\n", encoding="utf-8")
            self.window.toast("Trace saved")

        dialog.save(self.window, None, done)
