import copy
import json

import pytest

from realmebuds import model, profiles
from realmebuds.client import BudsError
from realmebuds.profiles import ProfileError, profile_from_dict
from realmebuds.session import connect_buds

# A made-up model. Its noise-control numbers differ from the Air7 Pro and, like some real OPPO/OnePlus
# models, what is written is not what is reported. It exists to prove nothing is hard-wired.
FICTIONAL = {
    "schema": 1,
    "key": "test-buds-x",
    "name": "Test Buds X",
    "vendor": "oppo",
    "family": "tl",
    "match": {"product_ids": ["0A0B0C"], "names": ["Test Buds X"]},
    "anc": {
        "modes": [
            {"key": "off", "label": "Off", "set": 1, "report": 8},
            {"key": "transparency", "label": "Transparency", "set": 4, "report": 256},
            {"key": "anc", "label": "Noise cancellation", "set": 2, "report": 2},
        ],
        "hold_cycle": {"options": [{"key": "off", "label": "Off", "bit": 1}, {"key": "anc", "label": "ANC", "bit": 2}], "min": 1},
    },
    "features": [
        {"key": "game-mode", "id": 6, "label": "Game mode", "group": "mode", "evidence": "family"},
        {"key": "spatial", "id": 27, "label": "Spatial audio", "group": "sound"},
    ],
    "gestures": {"sides": [1, 2], "actions": [2, 4], "options": {"2": [0, 1], "4": [0, 8]}},
    "equalizer": {"presets": [{"id": 0, "label": "Balanced"}, {"id": 1, "label": "Bass"}]},
}


@pytest.fixture
def userdir(tmp_path, monkeypatch):
    monkeypatch.setenv("REALME_BUDS_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def write(userdir, name, data):
    (userdir / name).write_text(json.dumps(data) if not isinstance(data, str) else data)


# -- loading and matching -----------------------------------------------------


def test_builtin_air7_pro_profile_loads_and_is_recognised(userdir):
    ps = profiles.load_all()
    p = ps["realme-buds-air7-pro"]
    assert p.source == "builtin" and p.family == "tl" and p.fit_test and p.golden_sound
    assert profiles.find("064C12", None, ps) is p  # by product id
    assert profiles.find("064c12", "anything", ps) is p  # case-insensitive
    assert profiles.find(None, "realme Buds Air7 Pro", ps) is p  # by name
    assert profiles.find("FFFFFF", "Some Other Buds", ps) is None


def test_product_id_beats_name(userdir):
    other = copy.deepcopy(FICTIONAL)
    other["match"] = {"product_ids": ["111111"], "names": ["realme Buds Air7 Pro"]}
    write(userdir, "x.json", other)
    ps = profiles.load_all()
    assert profiles.find("064C12", "realme Buds Air7 Pro", ps).key == "realme-buds-air7-pro"


def test_unknown_buds_get_a_generic_profile_that_claims_nothing(userdir):
    p = profiles.resolve("FFFFFF", "Mystery Buds")
    assert p.generic and p.key == "generic" and p.name == "Mystery Buds"
    assert not p.has_anc and not p.features and not p.eq_presets and not p.gesture_actions
    assert not p.fit_test and not p.golden_sound and p.custom_eq is None
    # unknown switches can still be shown, with a placeholder label
    f = p.feature_for_id(0x2A)
    assert f.id == 0x2A and f.evidence == "guess" and "0x2A" in f.label


def test_explicit_override_wins_and_unknown_key_is_an_error(userdir):
    assert profiles.resolve("FFFFFF", "x", override="realme-buds-air7-pro").key == "realme-buds-air7-pro"
    with pytest.raises(ProfileError, match="no profile 'nope'"):
        profiles.resolve("064C12", "x", override="nope")


def test_user_profiles_are_loaded_and_can_replace_a_builtin(userdir):
    write(userdir, "x.json", FICTIONAL)
    fixed = json.loads((profiles.BUILTIN_DIR / "realme-buds-air7-pro.json").read_text())
    fixed["name"] = "My corrected Air7 Pro"
    write(userdir, "air7.json", fixed)
    ps = profiles.load_all()
    assert ps["test-buds-x"].source.endswith("x.json")
    assert ps["realme-buds-air7-pro"].name == "My corrected Air7 Pro"


def test_a_broken_user_profile_is_skipped_and_reported(userdir):
    write(userdir, "good.json", FICTIONAL)
    write(userdir, "bad.json", "{ not json")
    errors: list[str] = []
    ps = profiles.load_all(errors=errors)
    assert "test-buds-x" in ps and "realme-buds-air7-pro" in ps
    assert len(errors) == 1 and "bad.json" in errors[0] and "not valid JSON" in errors[0]


def test_xdg_config_dir_is_searched(userdir):
    d = userdir / "xdg" / "realme-buds" / "profiles"
    d.mkdir(parents=True)
    (d / "x.json").write_text(json.dumps(FICTIONAL))
    assert "test-buds-x" in profiles.load_all()


# -- validation ---------------------------------------------------------------


def broken(**changes):
    d = copy.deepcopy(FICTIONAL)
    for path, value in changes.items():
        node = d
        parts = path.split("__")
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if value is None:
            del node[parts[-1]]
        else:
            node[parts[-1]] = value
    return d


@pytest.mark.parametrize(
    "data, message",
    [
        (broken(schema=2), "expected schema 1"),
        (broken(key=None), "missing 'key'"),
        (broken(key="Has Spaces"), "lowercase"),
        (broken(family="wm"), "unsupported protocol family"),
        (broken(match={"product_ids": ["12"], "names": []}), "six hex digits"),
        (broken(match={"product_ids": [], "names": []}), "at least one product id or name"),
        (broken(anc__modes__0__set="1"), "anc.modes[0].set"),
        (broken(anc__groups=[{"key": "g", "label": "G", "default": "nope"}]), "unknown mode 'nope'"),
        (broken(anc__buttons=["ghost"]), "unknown mode or group 'ghost'"),
        (broken(features__1__id=6), "duplicate feature"),
        (broken(features__0__group="elsewhere"), "group must be one of"),
        (broken(features__0__id=300), "features[0].id"),
        (broken(gestures__options={"3": [0]}), "listed in gestures.actions"),
        (broken(anc__modes__0__evidence="vibes"), "evidence must be one of"),
        (broken(transport="ble"), "transport: expected an object"),
        (broken(transport={"preferred": "wifi"}), "transport.preferred: must be one of auto, rfcomm, ble"),
        (broken(transport={"service_uuid": "079a"}), "transport.service_uuid: expected a UUID"),
        (broken(transport={"service_uuid": 5}), "transport.service_uuid: expected a UUID"),
    ],
)
def test_validation_errors_say_where_the_mistake_is(data, message):
    with pytest.raises(ProfileError, match="[\\s\\S]*" + message.replace("[", "\\[").replace("]", "\\]").replace(".", "\\.")):
        profile_from_dict(data, "mine.json")
    try:
        profile_from_dict(data, "mine.json")
    except ProfileError as e:
        assert str(e).startswith("mine.json: ")


def test_round_trip_through_json_is_lossless(userdir):
    for p in profiles.load_all().values():
        again = profile_from_dict(json.loads(json.dumps(p.to_dict())), p.source)
        assert again == p
    fic = profile_from_dict(FICTIONAL)
    assert profile_from_dict(fic.to_dict()) == fic


def test_transport_hint_defaults_to_auto_and_round_trips():
    p = profile_from_dict(FICTIONAL)
    assert (p.transport_preferred, p.transport_service_uuid) == ("auto", None)
    assert p.to_dict()["transport"] == {"preferred": "auto", "service_uuid": None}
    ble = profile_from_dict(broken(transport={"preferred": "ble", "service_uuid": "0000079C-D102-11E1-9B23-00025B00A5A5"}))
    assert (ble.transport_preferred, ble.transport_service_uuid) == ("ble", "0000079c-d102-11e1-9b23-00025b00a5a5")
    assert profile_from_dict(json.loads(json.dumps(ble.to_dict()))) == ble
    assert profile_from_dict(broken(transport={"preferred": "rfcomm", "service_uuid": None})).transport_preferred == "rfcomm"


def test_builtin_profile_has_no_transport_preference(userdir):
    p = profiles.get("realme-buds-air7-pro")
    assert (p.transport_preferred, p.transport_service_uuid) == ("auto", None)


# -- the profile object -------------------------------------------------------


def test_noise_control_tables_distinguish_write_from_report():
    p = profile_from_dict(FICTIONAL)
    assert p.mode("off").set == 1 and p.mode("off").report == 8
    assert p.mode_by_set(4).key == "transparency" and p.mode_by_report(256).key == "transparency"
    assert p.mode_by_report(1) is None  # 1 is what you write for Off, never what is reported
    assert p.anc_label(256) == "Transparency" and p.anc_label(0x55) == "unknown (0x55)"
    assert p.anc_buttons == ("off", "transparency", "anc")  # defaulted from the modes


def test_air7_pro_groups_and_labels(userdir):
    p = profiles.get("realme-buds-air7-pro")
    assert p.anc_buttons == ("nc", "off", "transparency")
    assert [m.key for m in p.group_modes("nc")] == ["smart", "max", "moderate", "mild"]
    assert p.anc_label(model.ANC_MAX) == "Noise cancellation: Max"
    assert p.group("nc").default == "smart"
    assert p.hold_min == 2 and [o.key for o in p.hold_options] == ["off", "transparency", "anc"]
    assert p.eq_by_key("deep-bass").id == 1 and p.feature("wind").id == 0x1A and p.feature(0x1A).key == "wind"
    assert p.options_for(3) == (0x00, 0x03, 0x05, 0x06, 0x11)


# -- the whole stack with a different model -----------------------------------


def test_another_model_works_without_any_code_change(userdir):
    write(userdir, "x.json", FICTIONAL)
    buds = connect_buds(sim=True, sim_profile="test-buds-x")
    try:
        assert buds.state.product_id == "0A0B0C"
        assert buds.profile.key == "test-buds-x"  # recognised from the product id alone
        sim = buds.sim
        buds.set_anc_mode("off")  # writes 1 ...
        assert sim.anc == 8 and buds.state.anc == 8  # ... and the buds report 8
        buds.set_anc_mode("transparency")
        assert sim.anc == 256 and buds.active_profile.anc_label(buds.state.anc) == "Transparency"
        # only this model's feature ids are asked for
        assert buds.read_features() == {6: False, 27: False}
        with pytest.raises(BudsError, match="no noise-control mode"):
            buds.set_anc_mode("smart")
    finally:
        buds.close()


def test_override_beats_recognition(userdir):
    write(userdir, "x.json", FICTIONAL)
    buds = connect_buds(sim=True, profile="test-buds-x")  # the sim is an Air7 Pro, we say it is not
    try:
        assert buds.profile.key == "test-buds-x" and buds.state.product_id == "064C12"
    finally:
        buds.close()


def test_profile_file_override(userdir, tmp_path):
    f = tmp_path / "draft.json"
    f.write_text(json.dumps(FICTIONAL))
    buds = connect_buds(sim=True, profile_file=str(f))
    try:
        assert buds.profile.key == "test-buds-x"
    finally:
        buds.close()


def test_unrecognised_buds_use_the_generic_profile_and_still_work(userdir):
    buds = connect_buds(sim=True)
    buds.close()
    # a sim that is a model nobody has a profile for
    unknown = profile_from_dict({**FICTIONAL, "key": "nobody-has-this", "match": {"product_ids": ["ABCDEF"]}})
    from realmebuds.client import Buds
    from realmebuds.sim import connected_pair

    sock, sim = connected_pair()
    sim.profile = unknown
    b = Buds(sock, init_delay=0, request_timeout=1.5, device_name="Mystery Buds")
    b.start()
    try:
        b.initialise()
        assert b.profile.generic and b.profile.name == "Mystery Buds" and b.state.product_id == "ABCDEF"
        b.sync()  # reads battery, wearing, firmware and the buds' own feature list
        assert b.state.battery and set(b.state.features) == {6, 27}
        found = b.scan_features()
        assert set(found) == {6, 27}
    finally:
        b.close()
        sim.close()
