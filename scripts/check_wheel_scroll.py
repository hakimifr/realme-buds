#!/usr/bin/env python3
"""Manual check with REAL wheel events: scroll over a Dynamic audio slider and see what moves.

    xvfb-run -a -s "-screen 0 1280x1400x24" python3 scripts/check_wheel_scroll.py

Needs xdotool. Expected: the slider value does not change and the page scrolls. (tests/test_gui.py checks the
same thing without synthesized X events; this script exists because the bug is about event routing.)
"""
"""Scroll the mouse wheel over a Dynamic audio slider with real X events and report what moved."""
import os, subprocess, sys, time
os.environ["GSK_RENDERER"] = "cairo"
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Graphene
sys.path.insert(0, os.path.expanduser("~/general-stuff/realme-buds"))
from realmebuds.gui import BudsApp

app = BudsApp(sim=True); app.register(None); app.activate()
w = app.window
ctx = GLib.MainContext.default()
def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        while ctx.iteration(False): pass
        time.sleep(0.01)
w.set_default_size(560, 700)
pump(3.5)

def find_scrolled(widget):
    if isinstance(widget, Gtk.ScrolledWindow): return widget
    c = widget.get_first_child()
    while c:
        r = find_scrolled(c)
        if r: return r
        c = c.get_next_sibling()
sw = find_scrolled(w.page)
adj = sw.get_vadjustment()
scale = w.dynamic_scales[1]
# bring the slider into view, then find where it is on screen
ok, point = scale.compute_point(w, Graphene.Point().init(scale.get_width() / 2, scale.get_height() / 2))
adj.set_value(0); pump(0.3)
target = w.dynamic_group
res = target.compute_bounds(sw)
adj.set_value(max(0, res[1].get_y() - 100)); pump(0.5)
ok, point = scale.compute_point(w, Graphene.Point().init(scale.get_width() / 2, scale.get_height() / 2))
# window origin on the X display: the largest visible window (there is no window manager here)
best = None
for wid in subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ""], capture_output=True, text=True).stdout.split():
    geo = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid], capture_output=True, text=True).stdout
    try:
        g = dict(l.split("=") for l in geo.split())
        area = int(g["WIDTH"]) * int(g["HEIGHT"])
    except (ValueError, KeyError):
        continue
    if best is None or area > best[0]:
        best = (area, g)
g = best[1]
x, y = int(g["X"]) + int(point.x), int(g["Y"]) + int(point.y)
before_scale, before_page = scale.get_value(), adj.get_value()
subprocess.run(["xdotool", "mousemove", str(x), str(y)]); pump(0.3)
for _ in range(3):
    subprocess.run(["xdotool", "click", "5"]); pump(0.25)   # wheel down
pump(0.8)
print(f"slider value: {before_scale:+.0f} -> {scale.get_value():+.0f}")
print(f"page scroll:  {before_page:.0f} -> {adj.get_value():.0f}")
w.session.stop()
