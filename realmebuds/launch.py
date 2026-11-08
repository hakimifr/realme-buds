"""Entry point of ``realme-buds-gui``: run the GTK app on a Python that has GTK.

PyGObject comes from the distribution (``python3-gobject``) and exists only for the distribution's
own Python. Tools such as ``uv tool install`` and ``pipx`` build an isolated environment on some other
interpreter (even a free-threaded one) where ``import gi`` fails. This package is pure Python with no
dependencies, so any Python can import it: when GTK is missing here, find one that has it and run the
app there. The command-line tool never needs this, it uses no GTK.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

OVERRIDE_ENV = "REALME_BUDS_PYTHON"
REEXEC_ENV = "REALME_BUDS_REEXEC"  # set in the child so a broken interpreter can never loop
GTK_CHECK = "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw"

INSTALL_HELP = (
    "Install GTK 4 and libadwaita for your system Python:\n"
    "  Fedora:        sudo dnf install python3-gobject gtk4 libadwaita\n"
    "  Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
    "  Arch:          sudo pacman -S python-gobject gtk4 libadwaita\n"
    f"Or point {OVERRIDE_ENV} at a Python that already has them."
)


def have_gtk() -> bool:
    """Whether the running interpreter can use GTK 4 and libadwaita."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def python_has_gtk(python: str) -> bool:
    try:
        res = subprocess.run([python, "-c", GTK_CHECK], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def candidates() -> list[str]:
    """Interpreters worth trying, most likely first: the distribution's own Python."""
    found: list[str] = ["/usr/bin/python3"]
    found += sorted(glob.glob("/usr/bin/python3.*"), reverse=True)
    for name in ("python3", "python"):
        path = shutil.which(name)
        if path:
            found.append(path)
    seen, out = set(), []
    for c in found:
        real = os.path.realpath(c)
        if real not in seen and os.path.isfile(c) and os.access(c, os.X_OK) and not c.endswith(("-config", "m")):
            seen.add(real)
            out.append(c)
    return out


def find_python() -> tuple[str | None, str | None]:
    """(interpreter, problem). An interpreter named in REALME_BUDS_PYTHON is used or reported, never skipped."""
    override = os.environ.get(OVERRIDE_ENV)
    if override:
        if python_has_gtk(override):
            return override, None
        return None, f"{OVERRIDE_ENV}={override} cannot import GTK 4 and libadwaita (PyGObject)."
    for c in candidates():
        if python_has_gtk(c):
            return c, None
    return None, "no Python with GTK 4 and libadwaita (PyGObject) was found."


def package_root() -> str:
    """The folder that contains the ``realmebuds`` package, so another Python can import it."""
    return str(Path(__file__).resolve().parent.parent)


def child_environment(root: str | None = None, env: dict | None = None) -> dict:
    out = dict(os.environ if env is None else env)
    root = root or package_root()
    out["PYTHONPATH"] = root + (os.pathsep + out["PYTHONPATH"] if out.get("PYTHONPATH") else "")
    out[REEXEC_ENV] = "1"
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if have_gtk():
        from .gui import main as gui_main

        return gui_main(args)
    if os.environ.get(REEXEC_ENV):
        print(f"realme-buds-gui: this Python ({sys.executable}) has no GTK 4 / libadwaita (PyGObject).\n{INSTALL_HELP}", file=sys.stderr)
        return 1
    python, problem = find_python()
    if python is None:
        print(
            f"realme-buds-gui: the GTK app needs PyGObject, and this Python ({sys.executable}) does not have it,\n"
            f"and {problem}\n{INSTALL_HELP}",
            file=sys.stderr,
        )
        return 1
    os.execve(python, [python, "-m", "realmebuds.gui", *args], child_environment())
    return 0  # unreachable, execve replaces the process
