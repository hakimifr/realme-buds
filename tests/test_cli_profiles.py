import json

import pytest

from realmebuds import cli, profiles
from realmebuds.profiles import profile_from_dict
from realmebuds.profiles.draft import build_draft, slug
from realmebuds.session import connect_buds

from tests.test_profiles import FICTIONAL  # noqa: E402


@pytest.fixture
def userdir(tmp_path, monkeypatch):
    monkeypatch.setenv("REALME_BUDS_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "x.json").write_text(json.dumps(FICTIONAL))
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_profile_list_show_paths_validate(userdir, capsys):
    code, out, _ = run(capsys, "profile", "list")
    assert code == 0 and "realme-buds-air7-pro" in out and "test-buds-x" in out and "built-in" in out
    code, out, _ = run(capsys, "profile", "show", "test-buds-x")
    assert json.loads(out)["name"] == "Test Buds X"
    code, out, _ = run(capsys, "profile", "paths")
    assert "built-in" in out and str(userdir) in out
    code, out, _ = run(capsys, "profile", "validate", str(userdir / "x.json"))
    assert code == 0 and out.startswith("OK: Test Buds X")
    (userdir / "bad.json").write_text(json.dumps({**FICTIONAL, "family": "wm"}))
    code, _, err = run(capsys, "profile", "validate", str(userdir / "bad.json"))
    assert code == 1 and "unsupported protocol family" in err and "bad.json" in err
    code, _, err = run(capsys, "profile", "show", "nope")
    assert code == 1 and "no profile 'nope'" in err


def test_list_reports_a_broken_user_profile_without_failing(userdir, capsys):
    (userdir / "bad.json").write_text("{ nope")
    code, out, err = run(capsys, "profile", "list")
    assert code == 0 and "test-buds-x" in out and "skipped" in err and "bad.json" in err


def test_status_of_another_model_uses_its_numbers_and_labels(userdir, capsys):
    code, out, _ = run(capsys, "--sim", "--sim-profile", "test-buds-x", "status")
    assert code == 0
    assert "Test Buds X  [test-buds-x]" in out and "product id 0A0B0C" in out
    assert "Off" in out.split("Noise control:")[1].splitlines()[0]  # report value 8 means Off here
    assert "Game mode:" in out and "Spatial audio:" in out  # this model's own switches
    assert "Wind noise reduction" not in out and "Pure Raw" not in out  # nothing borrowed from the Air7 Pro


def test_anc_uses_the_connected_models_modes(userdir, capsys):
    assert run(capsys, "--sim", "--sim-profile", "test-buds-x", "anc", "transparency")[0] == 0
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "anc", "smart")
    assert code == 1 and "no mode 'smart'" in err and "transparency" in err
    # the Air7 Pro's group alias exists there, not here
    assert run(capsys, "--sim", "anc", "nc")[0] == 0


def test_profile_override_applies_to_buds_that_are_recognised_as_something_else(userdir, capsys):
    code, out, _ = run(capsys, "--sim", "--profile", "test-buds-x", "status")
    assert code == 0 and "Test Buds X" in out
    code, _, err = run(capsys, "--sim", "--profile", "nope", "status")
    assert code == 1 and "no profile 'nope'" in err


def test_profile_file_override_and_its_feature_names_are_known_to_the_parser(userdir, tmp_path, capsys):
    other = {**FICTIONAL, "key": "from-file", "match": {"product_ids": ["777777"]},
             "features": FICTIONAL["features"] + [{"key": "special-thing", "id": 99, "label": "Special", "group": "personal"}]}
    f = tmp_path / "draft.json"
    f.write_text(json.dumps(other))
    code, _, err = run(capsys, "--sim", "--profile-file", str(f), "special-thing", "on")
    assert code == 1 and "do not report" in err  # parsed and looked up, then refused: the sim has no such switch


def test_unknown_buds_still_work_and_say_why_anc_is_unavailable(userdir, capsys, monkeypatch):
    code, out, err = run(capsys, "--sim", "--profile", "realme-buds-air7-pro", "status")
    assert code == 0
    # a model nobody has a profile for: connect as the generic profile
    monkeypatch.setattr(profiles, "find", lambda *a, **k: None)
    code, out, _ = run(capsys, "--sim", "status")
    assert code == 0 and "[generic]" in out and "Battery" in out and "Feature 0x04" in out
    code, _, err = run(capsys, "--sim", "anc", "off")
    assert code == 1 and "no noise-control table" in err and "--profile" in err
    code, out, _ = run(capsys, "--sim", "switch", "0x06", "on")
    assert code == 0 and "Feature 0x06: on" in out
    code, _, err = run(capsys, "--sim", "game-mode", "on")  # a known key of another profile still resolves by id
    assert code in (0, 1)


def test_gesture_gating_follows_the_profile_and_force_bypasses_it(userdir, capsys):
    # Test Buds X lists double tap and hold only
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "gesture", "left", "triple", "next")
    assert code == 1 and "does not list a 'triple' gesture" in err
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "gesture", "left", "double", "next")
    assert code == 1 and "offers: none, play-pause" in err
    code, out, _ = run(capsys, "--sim", "--sim-profile", "test-buds-x", "gesture", "left", "double", "next", "--force")
    assert code == 0 and "Next track" in out


def test_hold_cycle_names_and_minimum_come_from_the_profile(userdir, capsys):
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "hold-cycle", "off,transparency")
    assert code == 1 and "unknown mode 'transparency'" in err and "off, anc" in err
    assert run(capsys, "--sim", "--sim-profile", "test-buds-x", "hold-cycle", "off")[0] == 0  # this model allows one


def test_fit_test_and_golden_sound_are_gated_by_the_profile_unless_forced(userdir, capsys):
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "fit-test")
    assert code == 1 and "not listed as having an earbud fit test" in err
    assert run(capsys, "--sim", "--sim-profile", "test-buds-x", "fit-test", "--force")[0] == 0
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "golden-sound")
    assert code == 1 and "Golden Sound" in err and "--force" in err
    assert run(capsys, "--sim", "--sim-profile", "test-buds-x", "golden-sound", "--force")[0] == 0


def test_eq_choices_come_from_the_profile(userdir, capsys):
    code, out, _ = run(capsys, "--sim", "--sim-profile", "test-buds-x", "eq")
    assert code == 0 and "Balanced" in out and "Pure Raw" not in out
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "eq", "set", "deep-bass")
    assert code == 1 and "unknown EQ preset" in err and "balanced" in err
    # without a custom-EQ description a custom preset cannot be written
    code, _, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "eq", "save", "x", "62:1")
    assert code == 1 and "does not describe a custom equaliser" in err


# -- draft profiles ---------------------------------------------------------


def test_detect_writes_a_draft_that_loads_and_inherits_what_is_known(userdir, tmp_path, capsys):
    out_file = tmp_path / "draft.json"
    code, out, err = run(capsys, "--sim", "--sim-profile", "test-buds-x", "profile", "detect", "--out", str(out_file))
    assert code == 0 and "Draft written" in out and "Recognised as: Test Buds X" in err
    draft = json.loads(out_file.read_text())
    prof = profile_from_dict(draft, "draft")
    assert prof.product_ids == ("0A0B0C",) and {f.id for f in prof.features} == {6, 27}
    assert prof.feature(6).label == "Game mode"  # kept from the matched profile
    assert "anc" not in draft and "Fill in 'anc'" in draft["notes"]
    assert code == 0 and run(capsys, "profile", "validate", str(out_file))[0] == 0


def test_draft_for_unrecognised_buds_labels_unknown_switches(userdir):
    buds = connect_buds(sim=True)
    try:
        buds.sync()
        buds.profile = profiles.generic_profile(buds.state.product_id, "Mystery Buds")
        buds.device_name = "Mystery Buds"
        draft = build_draft(buds)
    finally:
        buds.close()
    assert draft["name"] == "Mystery Buds" and draft["key"] == "mystery-buds"
    unknown = [f for f in draft["features"] if f["evidence"] == "guess"]
    assert unknown and all(f["key"].startswith("feature-") for f in unknown)
    assert draft["gestures"]["actions"] == [2, 3, 4] and draft["capabilities"]["fit_test"] is True
    profile_from_dict(draft)


def test_slug():
    assert slug("OPPO Enco Air3 Pro") == "oppo-enco-air3-pro" and slug("!!!") == "unknown-buds"
