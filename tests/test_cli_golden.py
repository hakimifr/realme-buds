import contextlib

import pytest

from realmebuds import cli, model
from realmebuds.client import Buds
from realmebuds.sim import connected_pair


@pytest.fixture
def env(monkeypatch):
    holder = {"prompts": []}

    @contextlib.contextmanager
    def fake_open(args):
        sock, fake = connected_pair()
        holder["sim"] = fake
        buds = Buds(sock, init_delay=0, request_timeout=1.5)
        buds.start()
        buds.initialise()
        try:
            yield buds
        finally:
            buds.close()
            fake.close()

    def script(*answers):
        it = iter(answers)

        def ask(prompt):
            holder["prompts"].append(prompt)
            return next(it)

        monkeypatch.setattr(cli, "_ask", ask)

    monkeypatch.setattr(cli, "open_buds", fake_open)
    holder["script"] = script
    return holder


def run(*argv):
    return cli.main(list(argv))


def test_fit_test_command(env, capsys):
    assert run("fit-test") == 0
    out = capsys.readouterr().out
    assert "left bud: good seal" in out and "right bud: good seal" in out


def test_status_without_a_profile_points_to_the_test(env, capsys):
    assert run("golden-sound") == 0
    assert "No hearing profile" in capsys.readouterr().out


def test_wizard_end_to_end(env, capsys):
    # begin, node 1: louder once then accept, nodes 2-12 accept, then save.
    env["script"]("", "u", "", *[""] * 11, "y")
    assert run("golden-sound", "test", "--name", "Mine") == 0
    sim = env["sim"]
    levels = [t for t in sim.tones]
    assert levels[0] == (1, 1, -25) and levels[1] == (1, 1, -22)  # starts quiet, then one step louder
    assert [(s, b) for s, b, _ in levels[2:]] == [n for n in model.HEARING_NODES[1:]]
    entries, uid, name = sim.hearing_record
    assert name == "Mine" and len(entries) == 12
    assert entries[0] == (1, 1, -25)  # -22 snaps to the nearest storable value
    assert all(e[2] in model.HEARING_RESULT_VALUES for e in entries)
    assert sim.features[0x0B] is True and sim.scan_data == sim.ear_scan_bytes
    assert not sim.test_active  # the test was closed
    out = capsys.readouterr().out
    assert "Saved 'Mine'" in out and "left ear" in out


def test_loud_levels_need_confirmation_and_can_be_refused(env, capsys):
    env["script"]("", *["u"] * 11, "n", "q")
    assert run("golden-sound", "test", "--skip-scan") == 1
    sim = env["sim"]
    assert all(db < model.HEARING_LOUD_WARNING_DB for _, _, db in sim.tones)  # the loud tone never played
    assert any("That is loud" in p for p in env["prompts"])
    assert sim.hearing_record is None and not sim.test_active and sim.tones_stopped >= 1
    assert "Cancelled" in capsys.readouterr().out


def test_loud_levels_play_after_confirming(env):
    env["script"]("", *["u"] * 11, "y", "q")
    run("golden-sound", "test", "--skip-scan")
    assert env["sim"].tones[-1][2] == 10


def test_cancel_at_the_start_does_nothing(env):
    env["script"]("q")
    assert run("golden-sound", "test") == 1
    assert env["sim"].tones == [] and not env["sim"].test_active


def test_declining_to_save_leaves_the_buds_alone(env, capsys):
    env["script"]("", *[""] * 12, "n")
    assert run("golden-sound", "test", "--skip-scan") == 1
    assert env["sim"].hearing_record is None and env["sim"].features[0x0B] is False
    assert "Not saved" in capsys.readouterr().out


def test_yes_flag_and_skip_scan(env):
    env["script"](*[""] * 12)
    assert run("golden-sound", "test", "--skip-scan", "--yes", "--name", "Auto") == 0
    sim = env["sim"]
    assert sim.hearing_record[2] == "Auto" and sim.scan_data is None


def test_on_off_and_status_after_a_profile(env, capsys):
    env["script"](*[""] * 12)
    run("golden-sound", "test", "--skip-scan", "--yes", "--name", "Mine")
    capsys.readouterr()
    assert run("golden-sound", "off") == 0
    assert env["sim"].features[0x0B] is False
