"""Dialogs for the GTK app: the earbud fit test and the Golden Sound hearing test."""

from __future__ import annotations

import random
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import model, parsing  # noqa: E402


def _dialog_shell(title: str, width: int = 440) -> tuple[Adw.Dialog, Gtk.Box]:
    dialog = Adw.Dialog(title=title, content_width=width)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
    toolbar.set_content(box)
    dialog.set_child(toolbar)
    return dialog, box


def _label(text: str, css: str | None = None, wrap: bool = True) -> Gtk.Label:
    lab = Gtk.Label(label=text, wrap=wrap, xalign=0, selectable=False)
    if css:
        lab.add_css_class(css)
    return lab


class FitTestDialog:
    """Runs the fit test and shows what each bud reports."""

    def __init__(self, window) -> None:
        self.window = window
        self.dialog, box = _dialog_shell("Earbud fit test")
        self.info = _label("Put both earbuds in your ears and keep still. The buds play a short sound while they measure.")
        self.result = _label("", "title-4")
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER)
        self.button = Gtk.Button(label="Start test", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.button.connect("clicked", lambda *_: self.start())
        for w in (self.info, self.spinner, self.result, self.button):
            box.append(w)

    def present(self) -> None:
        self.dialog.present(self.window)

    def start(self) -> None:
        self.button.set_sensitive(False)
        self.result.set_label("")
        self.info.set_label("Testing… keep still.")
        self.spinner.start()
        timeout = self.window.overrides.fit_timeout
        self.window.session.submit(lambda b: b.run_fit_test(timeout=timeout), lambda res, err: GLib.idle_add(self._done, res, err))

    def _done(self, result, err) -> bool:
        self.spinner.stop()
        self.button.set_sensitive(True)
        self.button.set_label("Test again")
        if err is not None:
            self.info.set_label(str(err))
            return False
        self.info.set_label("Result")
        lines = [f"{name} bud: {model.fit_label(result[side])}" for side, name in ((1, "Left"), (2, "Right")) if side in result]
        self.result.set_label("\n".join(lines))
        return False


class HearingTestDialog:
    """Golden Sound wizard: ear scan, twelve tones, name, save."""

    def __init__(self, window) -> None:
        self.window = window
        self.uid = random.randint(1, 2**31 - 1)
        self.scan: parsing.EarScan | None = None
        self.results: list[tuple[int, int, int]] = []
        self.node = 0
        self.test_started = False
        self.loud_confirmed = False
        self._tone_timer = 0
        self._last_index = model.HEARING_STOPS.index(model.HEARING_START_DB)
        self._closed = False
        self._setting = False

        self.dialog, box = _dialog_shell("Golden Sound hearing test", 480)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        box.append(self.stack)
        self._build_intro()
        self._build_scan()
        self._build_test()
        self._build_save()
        self._build_message()
        self.dialog.connect("closed", self._on_closed)

    # -- pages ---------------------------------------------------------------

    def _page(self, name: str) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.stack.add_named(page, name)
        return page

    def _build_intro(self) -> None:
        page = self._page("intro")
        page.append(_label("Sit somewhere quiet and keep the earbuds in both ears.", "title-4"))
        page.append(
            _label(
                "First the buds measure your ear canals. Then you will hear a tone in one ear at a time, "
                "twelve in total (six pitches per ear). Make each tone as quiet as you can while still just hearing it. "
                "It starts quiet, and loud steps ask first."
            )
        )
        page.append(_label("Experimental: not yet confirmed on real Air7 Pro hardware.", "dim-label"))
        self.start_button = Gtk.Button(label="Start", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.start_button.connect("clicked", lambda *_: self.start())
        page.append(self.start_button)

    def _build_scan(self) -> None:
        page = self._page("scan")
        self.scan_spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, width_request=32, height_request=32)
        page.append(self.scan_spinner)
        page.append(_label("Measuring your ear canals… this takes about ten seconds. Keep still.", "title-4"))

    def _build_test(self) -> None:
        page = self._page("test")
        self.progress = Gtk.ProgressBar(show_text=True)
        page.append(self.progress)
        self.node_label = _label("", "title-3")
        page.append(self.node_label)
        page.append(_label("Move the slider until the tone is barely audible, then press Next."))
        stops = model.HEARING_STOPS
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, len(stops) - 1, 1)
        self.scale.set_digits(0)
        self.scale.set_draw_value(True)
        self.scale.set_value_pos(Gtk.PositionType.TOP)
        self.scale.set_margin_start(16)
        self.scale.set_margin_end(16)
        self.scale.set_format_value_func(lambda _s, v: f"{stops[int(round(v))]:+d} dB")
        self.scale.add_mark(0, Gtk.PositionType.BOTTOM, "quiet")
        self.scale.add_mark(len(stops) - 1, Gtk.PositionType.BOTTOM, "loud")
        self.scale.connect("value-changed", self._on_scale)
        page.append(self.scale)
        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.next_button = Gtk.Button(label="Next", css_classes=["suggested-action", "pill"])
        self.next_button.connect("clicked", lambda *_: self.next_node())
        cancel = Gtk.Button(label="Cancel", css_classes=["pill"])
        cancel.connect("clicked", lambda *_: self.dialog.close())
        row.append(cancel)
        row.append(self.next_button)
        page.append(row)

    def _build_save(self) -> None:
        page = self._page("save")
        page.append(_label("Test finished", "title-4"))
        self.summary = _label("")
        self.summary.add_css_class("monospace")
        page.append(self.summary)
        self.name_entry = Gtk.Entry(text=time.strftime("%Y/%m/%d %H:%M"), max_length=24)
        page.append(_label("Name for this profile"))
        page.append(self.name_entry)
        self.save_button = Gtk.Button(label="Save and turn Golden Sound on", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.save_button.connect("clicked", lambda *_: self.save())
        page.append(self.save_button)

    def _build_message(self) -> None:
        page = self._page("message")
        self.message = _label("", "title-4")
        page.append(self.message)
        close = Gtk.Button(label="Close", css_classes=["pill"], halign=Gtk.Align.CENTER)
        close.connect("clicked", lambda *_: self.dialog.close())
        page.append(close)

    # -- flow ------------------------------------------------------------------

    def present(self) -> None:
        self.stack.set_visible_child_name("intro")
        self.dialog.present(self.window)

    def _show_message(self, text: str) -> None:
        self.message.set_label(text)
        self.stack.set_visible_child_name("message")

    def start(self) -> None:
        self.start_button.set_sensitive(False)
        if self.window.overrides.skip_ear_scan:
            self._scan_done(None, None)  # the override: no ear scan, so none is stored with the profile
            return
        self.stack.set_visible_child_name("scan")
        self.scan_spinner.start()
        self.window.session.submit(lambda b: b.ear_scan(self.uid), lambda res, err: GLib.idle_add(self._scan_done, res, err))

    def _scan_done(self, scan, err) -> bool:
        self.scan_spinner.stop()
        if self._closed:
            return False
        if err is not None:
            self._show_message(f"The ear scan did not work: {err}")
            return False
        self.scan = scan
        self.window.session.submit(lambda b: b.hearing_test_start(), lambda res, err: GLib.idle_add(self._test_started, err))
        return False

    def _test_started(self, err) -> bool:
        if self._closed:
            return False
        if err is not None:
            self._show_message(f"Could not start the hearing test: {err}")
            return False
        self.test_started = True
        self.node = 0
        self.results = []
        self._enter_node()
        self.stack.set_visible_child_name("test")
        return False

    def _enter_node(self) -> None:
        side, band = model.HEARING_NODES[self.node]
        ear = "Left" if side == model.SIDE_LEFT else "Right"
        self.node_label.set_label(f"{ear} ear · pitch {band} of 6")
        self.progress.set_fraction(self.node / len(model.HEARING_NODES))
        self.progress.set_text(f"{self.node + 1} of {len(model.HEARING_NODES)}")
        self.loud_confirmed = False
        self._last_index = model.HEARING_STOPS.index(model.HEARING_START_DB)
        self._setting = True
        self.scale.set_value(self._last_index)
        self._setting = False
        self._play(self._last_index)

    def _on_scale(self, scale: Gtk.Scale) -> None:
        if self._setting:
            return
        idx = int(round(scale.get_value()))
        if model.HEARING_STOPS[idx] >= model.HEARING_LOUD_WARNING_DB and not self.loud_confirmed:
            self._ask_loud(idx)
            return
        self._last_index = idx
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
        self._tone_timer = GLib.timeout_add(300, self._play_later, idx)

    def _ask_loud(self, idx: int) -> None:
        self._setting = True
        self.scale.set_value(self._last_index)  # back to the last safe level while asking
        self._setting = False
        dialog = Adw.AlertDialog(heading="That tone is loud", body="Play it anyway?")
        dialog.add_response("cancel", "Keep it quieter")
        dialog.add_response("play", "Play it")
        dialog.set_response_appearance("play", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def chosen(_d, response: str) -> None:
            if response == "play":
                self.loud_confirmed = True
                self.scale.set_value(idx)

        dialog.connect("response", chosen)
        dialog.present(self.dialog)

    def _play_later(self, idx: int) -> bool:
        self._tone_timer = 0
        self._play(idx)
        return False

    def _play(self, idx: int) -> None:
        side, band = model.HEARING_NODES[self.node]
        db = model.HEARING_STOPS[idx]
        self.window.session.submit(lambda b: b.play_tone(side, band, db), lambda res, err: err and GLib.idle_add(self._tone_failed, err))

    def _tone_failed(self, err) -> bool:
        self.window.toast(f"Tone failed: {err}")
        return False

    def next_node(self) -> None:
        side, band = model.HEARING_NODES[self.node]
        idx = int(round(self.scale.get_value()))
        self.results.append((side, band, model.snap_hearing_value(model.HEARING_STOPS[idx])))
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
            self._tone_timer = 0
        self.window.session.submit(lambda b: b.stop_tone())
        self.node += 1
        if self.node < len(model.HEARING_NODES):
            self._enter_node()
            return
        self.window.session.submit(lambda b: b.hearing_test_stop())
        self.test_started = False
        rec = parsing.HearingRecord(tuple(parsing.HearingEntry(*r) for r in self.results), self.uid, "")
        by = {(e.side, e.band): e.db for e in rec.entries}
        lines = ["         " + " ".join(f"{b:>5d}" for b in model.HEARING_BANDS)]
        for side, name in ((1, "left  "), (2, "right ")):
            lines.append(f"{name}  " + " ".join(f"{by[(side, b)]:>5d}" for b in model.HEARING_BANDS))
        self.summary.set_label("pitch   " + "\n".join(lines))
        self.stack.set_visible_child_name("save")

    def save(self) -> None:
        name = self.name_entry.get_text().strip() or time.strftime("%Y/%m/%d %H:%M")
        self.save_button.set_sensitive(False)
        entries, uid, data = list(self.results), self.uid, (self.scan.data if self.scan else None)
        self.window.session.submit(
            lambda b: b.apply_golden_sound(entries, name, uid, data),
            lambda res, err: GLib.idle_add(self._saved, name, err),
        )

    def _saved(self, name: str, err) -> bool:
        if err is not None:
            self._show_message(f"Could not save the profile: {err}")
        else:
            self._show_message(f"Saved “{name}” and turned Golden Sound on.")
            self.window.refresh_ui()
        return False

    def _on_closed(self, *_args) -> None:
        self._closed = True
        if self._tone_timer:
            GLib.source_remove(self._tone_timer)
            self._tone_timer = 0
        if self.test_started:
            self.window.session.submit(lambda b: b.hearing_test_stop())
            self.test_started = False
