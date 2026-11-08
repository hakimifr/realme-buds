"""The bundled profiles, used for real: the simulator pretends to be each distinct kind of model."""

import pytest

from realmebuds import cli, model, profiles
from realmebuds.session import connect_buds

SHIPPED = profiles.load_all()


def shape(p):
    """What makes two models behave differently for this code."""
    return (
        tuple((m.set != m.reports[0], len(m.reports) > 1, m.group is not None) for m in p.anc_modes),
        tuple(bool(g.generic) for g in p.anc_groups),
        len(p.hold_options), p.hold_both, p.gesture_sides, p.gesture_actions,
        p.custom_eq is not None, bool(p.eq_presets), p.golden_sound,
    )


def representatives():
    seen: dict = {}
    for key in sorted(SHIPPED):
        seen.setdefault(shape(SHIPPED[key]), key)
    return sorted(seen.values())


REPS = representatives()


def test_there_are_many_models_and_every_vendor_is_covered():
    assert len(SHIPPED) >= 130
    vendors = {p.vendor for p in SHIPPED.values()}
    assert {"realme", "oppo", "oneplus"} <= vendors
    assert len(REPS) >= 15  # distinct behaviours, not just names


@pytest.mark.parametrize("key", REPS)
def test_model_works_end_to_end_in_the_simulator(key):
    prof = SHIPPED[key]
    buds = connect_buds(sim=True, sim_profile=key)
    try:
        # recognised from the product id alone, no override
        assert buds.profile.key == key and buds.state.product_id == prof.product_ids[0]
        errors = buds.sync()
        assert "anc" not in errors
        sim = buds.sim
        for mode in prof.anc_modes:
            buds.set_anc_mode(mode)
            assert buds.state.anc in prof.expected_reports(mode) and "unknown" not in prof.anc_label(buds.state.anc)
            assert prof.mode_by_report(buds.state.anc).key == mode.key or mode.group  # a level reads back as its group's
            assert sim.anc == buds.state.anc
        if prof.hold_options:
            want = sum(o.bit for o in prof.hold_options[: max(prof.hold_min, 1)])
            buds.set_hold_cycle(want)
            assert sim.hold_cycle == want
        for fid, _ in list(buds.state.features.items()):
            feat = prof.feature_for_id(fid)
            if feat.drops_link:
                continue
            buds.set_feature(fid, True)
            assert sim.features[fid] is True
        for act in (2, 3, 4):
            options = prof.options_for(act)
            if not options or act not in prof.gesture_actions or prof.hold_both and act == 4:
                continue
            for side in (1, 2):
                if side not in prof.gesture_sides:
                    continue
                target = next((o for o in options if o != next(e.fn for e in sim.gestures if (e.dev, e.btn, e.act) == (side, 1, act))), None)
                if target is not None:
                    buds.set_gesture(side, act, target)
                    assert next(e.fn for e in sim.gestures if (e.dev, e.btn, e.act) == (side, 1, act)) == target
        if prof.custom_eq:
            bands = [(hz, 0) for hz in prof.custom_eq.freqs]
            buds.save_custom_eq(None, "Mine", bands)
            assert [e.name for e in buds.state.custom_eqs] == ["Mine"]
    finally:
        buds.close()


@pytest.mark.parametrize("key", ["oneplus-buds-4", "oppo-enco-air3-pro", "realme-buds-t310"])
def test_cli_status_and_anc_for_a_few_real_models(key, capsys):
    prof = SHIPPED[key]
    assert cli.main(["--sim", "--sim-profile", key, "status"]) == 0
    out = capsys.readouterr().out
    assert prof.name in out and "unknown" not in out.lower().split("noise control")[1].splitlines()[0]
    first = prof.anc_buttons[0]
    assert cli.main(["--sim", "--sim-profile", key, "anc", first]) == 0
    assert cli.main(["--sim", "--sim-profile", key, "anc", "no-such-mode"]) == 1
    err = capsys.readouterr().err
    assert "choose from" in err and prof.anc_modes[0].key in err


def test_buds4_reports_off_as_either_number_and_is_still_verified(tmp_path):
    p = SHIPPED["oneplus-buds-4"]
    buds = connect_buds(sim=True, sim_profile="oneplus-buds-4")
    try:
        buds.set_anc_mode("off")
        assert buds.sim.anc == 8  # written as 1, the simulated buds report the alternate number
        assert p.anc_label(buds.state.anc) == "Off"
        buds.set_anc_mode("max")
        assert p.anc_label(buds.state.anc) == "Noise cancellation: Max"
    finally:
        buds.close()


def test_an_unlisted_report_value_still_fails_verification():
    from realmebuds.client import VerifyFailed

    buds = connect_buds(sim=True, sim_profile="oneplus-buds-4")
    try:
        with pytest.raises(VerifyFailed, match="expected"):
            buds.set_anc(1, expect=(0x99,))  # the buds would report 8, which is not accepted here
    finally:
        buds.close()


def test_both_buds_hold_and_single_button_models_are_described_correctly():
    both = [p for p in SHIPPED.values() if p.hold_both]
    one = [p for p in SHIPPED.values() if p.gesture_sides == (1,)]
    assert both and one
    assert all(4 in p.gesture_actions for p in both)
    assert all(p.vendor in ("realme", "oppo", "oneplus", "dizo") for p in both + one)
