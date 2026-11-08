"""LHDC refused while Spatial audio is on (seen on an OPPO Enco Air5 Pro, status 2)."""

import copy
import json

import pytest

from realmebuds import cli, profiles
from realmebuds.client import FeatureConflict, FeatureRefused, Nack
from realmebuds.profiles import ProfileError, profile_from_dict
from realmebuds.session import connect_buds

from tests.test_profiles import FICTIONAL

LHDC, SPATIAL = 0x18, 0x1B


@pytest.fixture
def enco():
    """The Enco Air5 Pro as the simulator, with the buds' own rule: LHDC and Spatial cannot both be on."""
    buds = connect_buds(sim=True, sim_profile="oppo-enco-air5-pro")
    buds.sim.feature_conflicts = {LHDC: (SPATIAL,), SPATIAL: (LHDC,)}
    buds.sim.features[SPATIAL] = True
    buds.sim.features[LHDC] = False
    buds.read_features()
    yield buds
    buds.close()


def feature_writes(buds):
    return [(p.payload[0], p.payload[1]) for p in buds.sim.received if p.cmd == 0x0403]


def test_the_profile_says_these_two_conflict_and_realme_does_not():
    enco = profiles.get("oppo-enco-air5-pro")
    lhdc = enco.feature("lhdc")
    assert [f.key for f in enco.conflicting(lhdc)] == ["spatial"]
    assert [f.key for f in enco.conflicting(enco.feature("spatial"))] == ["lhdc"]
    assert not any(f.conflicts for f in profiles.get("realme-buds-air7-pro").features)  # both can be on there


def test_refused_with_spatial_on_raises_a_conflict_that_names_both(enco):
    with pytest.raises(FeatureConflict) as e:
        enco.set_feature(LHDC, True)
    err = e.value
    assert isinstance(err, FeatureRefused) and isinstance(err, Nack) and err.status == 2
    assert [c.label for c in err.conflicts] == ["Spatial audio"]
    assert "High-definition sound (LHDC)" in str(err) and "Spatial audio" in str(err) and "cannot be on together" in str(err)
    assert enco.sim.features[LHDC] is False and enco.sim.features[SPATIAL] is True  # nothing was changed
    assert feature_writes(enco) == [(LHDC, 1)]


def test_resolving_turns_the_other_one_off_first_then_writes_once_more(enco):
    enco.set_feature(LHDC, True, resolve=True)
    assert feature_writes(enco) == [(LHDC, 1), (SPATIAL, 0), (LHDC, 1)]  # refused, spatial off, then on
    assert enco.sim.features == {**enco.sim.features, LHDC: True, SPATIAL: False}
    assert [f.key for f in enco.last_resolved] == ["spatial"]
    assert enco.state.features[LHDC] is True and enco.state.features[SPATIAL] is False


def test_no_extra_writes_when_nothing_conflicts(enco):
    enco.sim.features[SPATIAL] = False
    enco.read_features()
    enco.set_feature(LHDC, True, resolve=True)
    assert feature_writes(enco) == [(LHDC, 1)] and enco.last_resolved == []


def test_turning_a_switch_off_is_never_treated_as_a_conflict(enco):
    enco.sim.features[LHDC] = True
    enco.sim.features[SPATIAL] = False
    enco.read_features()
    enco.set_feature(LHDC, False)
    assert enco.sim.features[LHDC] is False


def test_spatial_has_the_same_handling(enco):
    enco.sim.features[LHDC], enco.sim.features[SPATIAL] = True, False
    enco.read_features()
    with pytest.raises(FeatureConflict, match="High-definition sound"):
        enco.set_spatial(True)
    enco.set_spatial(True, resolve=True)
    assert enco.sim.features[SPATIAL] is True and enco.sim.features[LHDC] is False


def test_a_refusal_with_no_known_conflict_explains_itself_and_mentions_a_bud_in_the_case(enco):
    enco.sim.features[SPATIAL] = False
    enco.read_features()
    enco.sim.feature_conflicts = {LHDC: (0x04,)}  # refused for a reason the profile does not know
    enco.sim.features[0x04] = True
    enco.sim.wearing = {1: 0, 2: 3, 3: 0}
    enco.read_wearing()
    with pytest.raises(FeatureRefused) as e:
        enco.set_feature(LHDC, True, resolve=True)
    assert not isinstance(e.value, FeatureConflict)
    msg = str(e.value)
    assert "High-definition sound (LHDC)" in msg and "status 2" in msg and "a bud is in the case" in msg and "--trace" in msg


def test_a_refusal_after_resolving_is_reported_not_retried_forever(enco):
    enco.sim.feature_conflicts = {LHDC: (SPATIAL, 0x04)}
    enco.sim.features[0x04] = True  # a second, unlisted blocker
    with pytest.raises(FeatureRefused):
        enco.set_feature(LHDC, True, resolve=True)
    assert feature_writes(enco).count((LHDC, 1)) == 2  # one retry only


def test_switches_that_drop_the_link_are_turned_off_last():
    data = copy.deepcopy(FICTIONAL)
    data["features"] = [
        {"key": "a", "id": 6, "label": "A", "conflicts": ["b", "c"]},
        {"key": "b", "id": 0x18, "label": "B (drops the link)", "drops_link": True},
        {"key": "c", "id": 0x1B, "label": "C"},
    ]
    prof = profile_from_dict(data)
    buds = connect_buds(sim=True)
    try:
        buds.profile = prof
        buds.sim.feature_conflicts = {6: (0x18, 0x1B)}
        buds.sim.features.update({6: False, 0x18: True, 0x1B: True})
        buds.read_features()
        buds.set_feature(6, True, resolve=True, verify=False)
        order = feature_writes(buds)
        assert order == [(6, 1), (0x1B, 0), (0x18, 0), (6, 1)]  # C first, B (which drops the link) last
    finally:
        buds.close()


# -- profiles ---------------------------------------------------------------------------------


def test_conflicts_round_trip_and_are_validated():
    data = copy.deepcopy(FICTIONAL)
    data["features"][0]["conflicts"] = ["spatial"]
    prof = profile_from_dict(data)
    assert prof.feature("game-mode").conflicts == ("spatial",)
    assert [f.key for f in prof.conflicting(prof.feature("spatial"))] == ["game-mode"]  # declared on one side only
    assert profile_from_dict(prof.to_dict()) == prof
    for bad, message in (("nope", "not another feature"), ("game-mode", "not another feature")):
        broken = copy.deepcopy(data)
        broken["features"][0]["conflicts"] = [bad]
        with pytest.raises(ProfileError, match=message):
            profile_from_dict(broken, "x.json")
    broken = copy.deepcopy(data)
    broken["features"][0]["conflicts"] = "spatial"
    with pytest.raises(ProfileError, match="expected a list"):
        profile_from_dict(broken, "x.json")


# -- command line -----------------------------------------------------------------------------


@pytest.fixture
def cli_enco(monkeypatch):
    real = connect_buds

    def patched(*a, **kw):
        buds = real(*a, **kw)
        buds.sim.feature_conflicts = {LHDC: (SPATIAL,), SPATIAL: (LHDC,)}
        buds.sim.features[SPATIAL], buds.sim.features[LHDC] = True, False
        return buds

    monkeypatch.setattr(cli, "connect_buds", patched)


def test_cli_explains_the_conflict_and_the_flag_fixes_it(cli_enco, capsys):
    base = ["--sim", "--sim-profile", "oppo-enco-air5-pro"]
    assert cli.main([*base, "lhdc", "on"]) == 1
    err = capsys.readouterr().err
    assert "Spatial audio" in err and "cannot be on together" in err and "--resolve-conflicts" in err
    assert cli.main(["--resolve-conflicts", *base, "lhdc", "on"]) == 0
    out = capsys.readouterr()
    assert "turned off first: Spatial audio" in out.err and "High-definition sound (LHDC): on" in out.out


def test_cli_reports_an_unexplained_refusal_with_the_switch_name(monkeypatch, capsys):
    real = connect_buds

    def patched(*a, **kw):
        buds = real(*a, **kw)
        buds.sim.feature_conflicts = {LHDC: (0x04,)}  # a blocker the profile does not list
        buds.sim.features[0x04] = True
        return buds

    monkeypatch.setattr(cli, "connect_buds", patched)
    assert cli.main(["--sim", "--sim-profile", "oppo-enco-air5-pro", "lhdc", "on"]) == 1
    err = capsys.readouterr().err
    assert "refused to turn High-definition sound (LHDC) on (status 2)" in err
