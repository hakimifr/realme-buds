"""Editor for custom equaliser presets (CLI: ``realme-buds eq save`` / ``eq delete``)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from . import parsing  # noqa: E402
from .gui_util import pass_scroll_to_parent  # noqa: E402


def band_label(hz: int) -> str:
    return f"{hz / 1000:g} kHz" if hz >= 1000 else f"{hz} Hz"


class CustomEqDialog:
    """Name plus one gain slider per band. Bands and gain range come from the model's profile."""

    def __init__(self, window, existing: parsing.CustomEq | None = None) -> None:
        spec = window.current_profile.custom_eq
        if spec is None:
            raise ValueError("this model's profile does not describe a custom equaliser")
        self.window = window
        self.existing = existing
        self.spec = spec
        freqs = [hz for hz, _ in existing.bands] if existing and existing.bands else list(spec.freqs)
        gains = {hz: g for hz, g in existing.bands} if existing else {}

        self.dialog = Adw.Dialog(title="Edit equaliser" if existing else "New equaliser", content_width=420)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        toolbar.set_content(box)
        self.dialog.set_child(toolbar)

        self.name_entry = Gtk.Entry(text=existing.name if existing else "My EQ", max_length=24, placeholder_text="Name")
        box.append(self.name_entry)
        self.scales: dict[int, Gtk.Scale] = {}
        for hz in freqs:
            row = Gtk.Box(spacing=12)
            row.append(Gtk.Label(label=band_label(hz), width_chars=8, xalign=0))
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, spec.gain_min, spec.gain_max, 1)
            scale.set_digits(0)
            scale.set_hexpand(True)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
            scale.set_value(max(spec.gain_min, min(spec.gain_max, gains.get(hz, 0))))
            pass_scroll_to_parent(scale)
            row.append(scale)
            box.append(row)
            self.scales[hz] = scale
        self.save_button = Gtk.Button(label="Save and use", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.save_button.connect("clicked", lambda *_: self.save())
        box.append(self.save_button)

    def present(self) -> None:
        self.dialog.present(self.window)

    def bands(self) -> list[tuple[int, int]]:
        return [(hz, int(round(s.get_value()))) for hz, s in self.scales.items()]

    def save(self) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            self.window.toast("Give the equaliser a name")
            return
        bands, eq_id = self.bands(), (self.existing.id if self.existing else None)
        self.window.act(lambda b: b.save_custom_eq(eq_id, name, bands), f"Saved “{name}”")
        self.dialog.close()
