import json

import pytest

from realmebuds import cli, model
from realmebuds.client import Buds
from realmebuds.sim import connected_pair


@pytest.fixture
def sim(monkeypatch):
    """Every CLI invocation connects to the same fake buds, like real life."""
    import contextlib

    sock_holder = {}

    @contextlib.contextmanager
    def fake_open(args):
        sock, fake = connected_pair(**sock_holder.get("kwargs", {}))
        # Reuse state between invocations by copying it onto each fresh fake.
        if "state" in sock_holder:
            fake.__dict__.update(sock_holder["state"])
        buds = Buds(sock, init_delay=0, request_timeout=1.5)
        buds.start()
        try:
            buds.initialise()
            yield buds
        finally:
            buds.close()
            sock_holder["state"] = {
                k: getattr(fake, k)
                for k in ("anc", "features", "eq", "custom_eqs", "dynamic", "gestures", "hold_cycle")
            }
            fake.close()

    monkeypatch.setattr(cli, "open_buds", fake_open)
    return sock_holder


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_status_text_and_json(sim, capsys):
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "138.138.105" in out and "Transparency" in out and "left 70%" in out
    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert data["noise_control"] == "Transparency" and data["features"]["spatial"] is False
    assert data["battery"]["left"] == {"percent": 70, "charging": False}


def test_state_persists_across_invocations(sim, capsys):
    assert run(capsys, "anc", "max")[0] == 0
    assert run(capsys, "spatial", "on")[0] == 0
    assert run(capsys, "game-mode", "on")[0] == 0
    _, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert data["noise_control"] == "Noise cancellation: Max"
    assert data["features"]["spatial"] and data["features"]["game-mode"]


def test_feature_the_buds_lack_is_refused_without_writing(sim, capsys):
    sim["state"] = {"features": {0x04: True}}
    code, _, err = run(capsys, "wind", "on")
    assert code == 1 and "do not report" in err


def test_gesture_and_hold_cycle(sim, capsys):
    assert run(capsys, "gesture", "right", "triple", "game-mode")[0] == 0
    assert run(capsys, "hold-cycle", "off,transparency,anc")[0] == 0
    _, out, _ = run(capsys, "gestures")
    assert "Game mode" in out
    assert "Off" in out and "Transparency" in out


def test_hold_cycle_rejects_unknown_mode(sim, capsys):
    code, _, err = run(capsys, "hold-cycle", "off,smart")
    assert code == 1 and "unknown mode" in err


def test_eq_flow(sim, capsys):
    assert run(capsys, "eq", "set", "pure-bass")[0] == 0
    _, out, _ = run(capsys, "eq")
    assert "current: Pure Bass" in out
    code, out, _ = run(capsys, "eq", "save", "Mine", "62:2", "250:0", "1000:0", "4000:1", "8000:0", "16000:-3")
    assert code == 0
    _, out, _ = run(capsys, "eq")
    assert "Mine (custom" in out
    code, _, err = run(capsys, "eq", "set", "nonsense")
    assert code == 1 and "unknown EQ preset" in err
    code, _, err = run(capsys, "eq", "save", "x", "62-2")
    assert code == 1 and "Hz:dB" in err


def test_dynamic_audio(sim, capsys):
    code, out, _ = run(capsys, "dynamic-audio", "2", "0", "-3")
    assert code == 0 and "Low freq +2" in out and "High freq -3" in out
    code, _, err = run(capsys, "dynamic-audio", "9", "0", "0")
    assert code == 1 and "-5..5" in err
    assert run(capsys, "dynamic-audio", "off")[0] == 0
    _, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["features"]["dynamic-audio"] is False


def test_raw_command(sim, capsys):
    code, out, _ = run(capsys, "raw", "010c", "0101")
    assert code == 0 and "status=0" in out
    assert run(capsys, "raw", "zz")[0] == 1


def test_every_anc_mode_and_feature_name_parses():
    ap = cli.build_parser()
    for m in model.ANC_MODES:
        ap.parse_args(["anc", m.key])
    for f in model.FEATURES:
        if f.key not in ("dynamic-audio", "golden-sound"):
            assert ap.parse_args([f.key, "on"]).state is True
    assert ap.parse_args(["golden-sound", "on"]).action == "on"


def test_band_argument_forms():
    f = cli.parse_band_args
    assert f(["2", "0", "-1"]) == {1: 2, 2: 0, 3: -1}
    assert f(["low", "+2"]) == {1: 2}
    assert f(["low", "+2", "high", "-1"]) == {1: 2, 3: -1}
    assert f(["bass=3", "treble:-2"]) == {1: 3, 3: -2}
    for bad in (["1", "2"], ["low"], ["loud", "2"], ["low", "x"]):
        with pytest.raises(cli.CliError):
            f(bad)


def test_dynamic_audio_by_band_name(sim, capsys):
    code, out, _ = run(capsys, "dynamic-audio", "low", "+2")
    assert code == 0 and "Low freq +2" in out and "Mid freq +0" in out
    code, out, _ = run(capsys, "dynamic-audio", "high", "-1", "mid", "3")
    assert "Mid freq +3" in out and "High freq -1" in out and "Low freq +2" in out


def test_raw_reports_missing_reply_without_failing(sim, capsys):
    sim["kwargs"] = {"ignore_commands": {0x041B}}
    code, out, _ = run(capsys, "raw", "041b", "fb050201")
    assert code == 0 and "no reply" in out


def test_gesture_functions_outside_the_apps_list_are_refused_unless_forced(sim, capsys):
    code, _, err = run(capsys, "gesture", "left", "double", "volume-up")
    assert code == 1 and "only offers" in err and "play-pause" in err
    assert run(capsys, "gesture", "left", "double", "volume-up", "--force")[0] == 0
    _, out, _ = run(capsys, "gestures")
    assert "Volume up" in out


def test_hold_cycle_needs_two_modes_unless_forced(sim, capsys):
    code, _, err = run(capsys, "hold-cycle", "off")
    assert code == 1 and "at least 2" in err
    assert run(capsys, "hold-cycle", "off", "--force")[0] == 0
