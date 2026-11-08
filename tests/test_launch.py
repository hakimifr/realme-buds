"""realme-buds-gui on a Python without GTK (uv / pipx environments)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from realmebuds import launch

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_HAS_GTK = any(launch.python_has_gtk(c) for c in launch.candidates())
needs_gtk = pytest.mark.skipif(not SYSTEM_HAS_GTK, reason="no Python with GTK 4 + libadwaita on this machine")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in (launch.OVERRIDE_ENV, launch.REEXEC_ENV):
        monkeypatch.delenv(k, raising=False)


def test_with_gtk_available_it_just_runs_the_app(monkeypatch):
    ran = []
    monkeypatch.setattr(launch, "have_gtk", lambda: True)
    monkeypatch.setattr("realmebuds.gui.main", lambda argv: ran.append(argv) or 0, raising=False)
    assert launch.main(["--sim"]) == 0 and ran == [["--sim"]]


def test_without_gtk_it_runs_itself_on_the_python_that_has_it(monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "have_gtk", lambda: False)
    monkeypatch.setattr(launch, "find_python", lambda: ("/usr/bin/python3", None))
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append((path, argv, env)))
    launch.main(["--sim", "--profile", "x"])
    (path, argv, env), = calls
    assert path == "/usr/bin/python3"
    assert argv == ["/usr/bin/python3", "-m", "realmebuds.gui", "--sim", "--profile", "x"]
    assert env[launch.REEXEC_ENV] == "1"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == launch.package_root()  # so that Python can import this package


def test_the_package_path_goes_in_front_of_an_existing_pythonpath():
    env = launch.child_environment("/pkg", {"PYTHONPATH": "/a:/b", "X": "1"})
    assert env["PYTHONPATH"] == "/pkg" + os.pathsep + "/a:/b" and env["X"] == "1"
    assert launch.child_environment("/pkg", {})["PYTHONPATH"] == "/pkg"


def test_no_python_with_gtk_says_what_to_install(monkeypatch, capsys):
    monkeypatch.setattr(launch, "have_gtk", lambda: False)
    monkeypatch.setattr(launch, "find_python", lambda: (None, "no Python with GTK 4 and libadwaita (PyGObject) was found."))
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("must not exec"))
    assert launch.main([]) == 1
    err = capsys.readouterr().err
    assert "python3-gobject" in err and "dnf install" in err and launch.OVERRIDE_ENV in err and sys.executable in err


def test_a_child_without_gtk_does_not_loop(monkeypatch, capsys):
    monkeypatch.setenv(launch.REEXEC_ENV, "1")
    monkeypatch.setattr(launch, "have_gtk", lambda: False)
    monkeypatch.setattr(launch, "find_python", lambda: pytest.fail("must not search again"))
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("must not exec again"))
    assert launch.main([]) == 1
    assert "has no GTK 4" in capsys.readouterr().err


def test_an_override_is_used_when_it_works_and_never_silently_skipped(monkeypatch):
    monkeypatch.setattr(launch, "python_has_gtk", lambda p: p == "/opt/py/bin/python")
    monkeypatch.setenv(launch.OVERRIDE_ENV, "/opt/py/bin/python")
    assert launch.find_python() == ("/opt/py/bin/python", None)
    monkeypatch.setenv(launch.OVERRIDE_ENV, "/broken/python")
    python, problem = launch.find_python()
    assert python is None and "REALME_BUDS_PYTHON=/broken/python" in problem  # does not fall back to another one


def test_candidates_start_with_the_system_python_and_are_unique():
    c = launch.candidates()
    assert len(c) == len({os.path.realpath(x) for x in c})
    if os.path.exists("/usr/bin/python3"):
        assert c[0] == "/usr/bin/python3"


def test_a_missing_interpreter_has_no_gtk():
    assert launch.python_has_gtk("/definitely/not/a/python") is False


# -- for real, in a subprocess -----------------------------------------------------------------


def run_launcher(*args, env_extra=None):
    """Start the launcher on this interpreter with ``-S``: no site-packages, so no GTK, like a uv environment."""
    env = {k: v for k, v in os.environ.items() if k not in (launch.OVERRIDE_ENV, launch.REEXEC_ENV)}
    env["PYTHONPATH"] = str(ROOT)
    env.update(env_extra or {})
    code = "import sys; from realmebuds.launch import main; sys.exit(main())"
    return subprocess.run([sys.executable, "-S", "-c", code, *args], capture_output=True, text=True, env=env, timeout=120)


def test_the_subprocess_really_has_no_gtk():
    res = subprocess.run([sys.executable, "-S", "-c", "import gi"], capture_output=True, text=True)
    assert res.returncode != 0 or not SYSTEM_HAS_GTK


@needs_gtk
def test_launcher_finds_the_system_python_and_starts_the_app():
    res = run_launcher("--help")
    assert res.returncode == 0, res.stderr
    assert "usage: realme-buds-gui" in res.stdout and "--transport" in res.stdout


@needs_gtk
def test_launcher_passes_arguments_through_to_the_app():
    res = run_launcher("--transport", "bogus")
    assert res.returncode == 2 and "invalid choice" in res.stderr  # argparse in the app, so the arguments arrived


def test_launcher_reports_a_broken_override_in_a_subprocess():
    res = run_launcher("--help", env_extra={launch.OVERRIDE_ENV: "/nonexistent/python"})
    assert res.returncode == 1 and "REALME_BUDS_PYTHON=/nonexistent/python" in res.stderr and "python3-gobject" in res.stderr


def test_ble_without_gtk_names_the_command_that_works(monkeypatch):
    from realmebuds import ble

    monkeypatch.setattr(launch, "find_python", lambda: ("/usr/bin/python3", None))
    msg = ble._no_gi_message()
    assert "python3-gobject" in msg and "/usr/bin/python3 -m realmebuds" in msg and launch.package_root() in msg
    monkeypatch.setattr(launch, "find_python", lambda: (None, "none"))
    assert "dnf install" in ble._no_gi_message()
