"""Small GTK helpers shared by the windows and dialogs."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


def pass_scroll_to_parent(widget: Gtk.Widget) -> Gtk.EventControllerScroll:
    """Stop a slider reacting to the mouse wheel and scroll the page that contains it instead.

    A Gtk.Scale claims wheel events, so scrolling a settings page with the pointer over a slider moves
    the slider (one notch can push it to the end) and the page does not scroll. A capture-phase controller
    gets the event first; it hands the scroll to the nearest scrolled window, the same distance that
    window would have scrolled, and marks the event handled so the slider never sees it. Sliders can
    still be changed by dragging, clicking or the keyboard.
    """
    controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
    controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def on_scroll(_controller, _dx: float, dy: float) -> bool:
        window = widget.get_ancestor(Gtk.ScrolledWindow)
        if window is not None and dy:
            adj = window.get_vadjustment()
            top = adj.get_upper() - adj.get_page_size()
            step = dy * adj.get_page_size() ** (2.0 / 3.0)  # what a scrolled window itself uses per wheel unit
            adj.set_value(max(adj.get_lower(), min(top, adj.get_value() + step)))
        return True

    controller.connect("scroll", on_scroll)
    widget.add_controller(controller)
    return controller
