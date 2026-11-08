"""The QuickBuds importer, on a synthetic models.json (the real one is not in the repository)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from realmebuds.profiles import profile_from_dict

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_quickbuds.py"
spec = importlib.util.spec_from_file_location("import_quickbuds", SCRIPT)
imp = importlib.util.module_from_spec(spec)
sys.modules["import_quickbuds"] = imp
spec.loader.exec_module(imp)

# OnePlus Buds 4 style: Off is written as bit 0 and reported as bit 0 or 3, and the NC parent has levels.
BUDS4_TREE = [
    {"modeType": 5, "protocolIndex": 1, "childrenMode": [{"modeType": 4, "protocolIndex": 4}, {"modeType": 8, "protocolIndex": 5},
                                                         {"modeType": 3, "protocolIndex": 6}, {"modeType": 7, "protocolIndex": 7}]},
    {"modeType": 10, "protocolIndex": 11},
    {"modeType": 2, "protocolIndex": 2, "childrenMode": [{"modeType": 2, "protocolIndex": 8}]},
    {"modeType": 1, "protocolIndex": 0, "childrenMode": [{"modeType": 1, "protocolIndex": 3}]},
]
# realme style: the parent and one level share an index, so the level wins the report.
REALME_TREE = [
    {"modeType": 5, "protocolIndex": 3, "childrenMode": [{"modeType": 7, "protocolIndex": 5}, {"modeType": 4, "protocolIndex": 3},
                                                         {"modeType": 8, "protocolIndex": 4}, {"modeType": 3, "protocolIndex": 2}]},
    {"modeType": 1, "protocolIndex": 0},
    {"modeType": 2, "protocolIndex": 1},
]
# the T310 has the same layout without Smart
T310_TREE = [
    {"modeType": 5, "protocolIndex": 3, "childrenMode": [{"modeType": 4, "protocolIndex": 3}, {"modeType": 8, "protocolIndex": 4},
                                                         {"modeType": 3, "protocolIndex": 2}]},
    {"modeType": 1, "protocolIndex": 0},
    {"modeType": 2, "protocolIndex": 1},
]
PLAIN_ANC_TREE = [{"modeType": 5, "protocolIndex": 1}, {"modeType": 1, "protocolIndex": 0}, {"modeType": 2, "protocolIndex": 2}]
VOICE_TREE = [{"modeType": 1, "protocolIndex": 0}, {"modeType": 2, "protocolIndex": 1},
              {"modeType": 6, "protocolIndex": 6, "decideByEarDevice": True}]

MODELS = [
    {"id": "065414", "name": "OnePlus Buds 4", "confirmed": ["anc", "wind"], "statusQuery": "05 04 0B 11 18 06 1B 1D 1C",
     "noiseReductionMode": BUDS4_TREE, "equalizerMode": [{"modeType": 11, "protocolIndex": 0}, {"modeType": 14, "protocolIndex": 1}],
     "control": [{"action": 1, "support": 516}, {"action": 2, "support": 8807}, {"action": 27, "minSelectCount": 1}, {"action": 5, "support": 3584}],
     "customEqualizer": 1, "bassEngineSupport": 1},
    {"id": "060414", "name": "OnePlus Buds Alias", "noiseReductionMode": PLAIN_ANC_TREE},
    {"id": "063C12", "name": "realme Buds T310", "noiseReductionMode": T310_TREE, "longPressType": 8833, "sharedHoldMask": 1,
     "control": [{"action": 2, "support": 581}, {"action": 3, "support": 8825}, {"action": 11, "minSelectCount": 2}],
     "equalizerMode": [{"modeType": 103, "protocolIndex": 2}, {"modeType": 101, "protocolIndex": 0}], "windNoise": 1},
    {"id": "064C12", "name": "realme Buds Air7 Pro", "noiseReductionMode": REALME_TREE},
    {"id": "06AAAA", "name": "OPPO Voice Buds", "noiseReductionMode": VOICE_TREE},
    {"id": "06BBBB", "name": "OPPO Neckband", "oneButton": 1, "control": [{"action": 1, "support": 4}, {"action": 4, "support": 513}]},
    {"id": "06CCCC", "name": "realme Both Hold", "bothHold": 1, "control": [{"action": 4, "support": 8704}, {"action": 2, "support": 4}]},
    {"id": "06DDDD", "name": "OPPO Per Bud", "noiseReductionMode": PLAIN_ANC_TREE, "longPressType": 8833, "control": [{"action": 11}]},
    {"id": "06EEEE", "name": "OnePlus Buds", "equalizerMode": [{"modeType": 1, "protocolIndex": 0, "minFirmVersion": 5}]},
    {"id": "06FFFF", "name": "OnePlus Buds", "noiseReductionMode": []},
    {"id": "06SPKR", "name": "realme Pocket Speaker"},
]


@pytest.fixture
def out(tmp_path):
    f = tmp_path / "models.json"
    f.write_text(json.dumps({"whiteList": MODELS}))
    target = tmp_path / "builtin"
    target.mkdir()
    (target / "hand.json").write_text(json.dumps({"key": "realme-buds-air7-pro", "notes": "hand written", "match": {"product_ids": ["064C12"]}}))
    old = sys.argv
    sys.argv = ["import", str(f), "--out", str(target)]
    try:
        assert imp.main() == 0
    finally:
        sys.argv = old
    return target


def load(out, key):
    return json.loads((out / f"{key}.json").read_text())


def test_hand_written_profiles_are_kept_and_everything_else_is_written(out):
    names = {p.name for p in out.glob("*.json")}
    assert "hand.json" in names and "realme-buds-air7-pro.json" not in names
    assert len(names) == 1 + len(MODELS) - 2  # the Air7 Pro (hand-written) and the speaker are skipped
    assert not any("speaker" in n for n in names)
    for p in out.glob("*.json"):
        if p.name != "hand.json":
            profile_from_dict(json.loads(p.read_text()), p.name)  # every generated profile is valid


def test_duplicate_names_get_unique_keys(out):
    keys = {json.loads(p.read_text()).get("key") for p in out.glob("*.json")}
    assert "oneplus-buds-06eeee" in keys and "oneplus-buds-06ffff" in keys and "oneplus-buds" not in keys
    assert len(keys) == len(list(out.glob("*.json")))


def test_buds4_style_tree_write_and_report_values(out):
    p = profile_from_dict(load(out, "oneplus-buds-4"), "x")
    assert p.mode("off").set == 1 and p.mode("off").reports == (1, 8)  # written as bit 0, reported as bit 0 or 3
    assert p.mode("transparency").set == 4 and p.mode("transparency").reports == (4, 256)
    assert p.mode("adaptive").set == 0x800
    assert [p.mode(k).set for k in ("max", "moderate", "mild", "smart")] == [0x10, 0x20, 0x40, 0x80]
    assert p.group("nc").generic == (2,) and p.group("nc").default == "smart"
    assert p.mode_by_report(8).key == "off" and p.mode_by_report(256).key == "transparency"
    assert p.mode_by_report(2).key == "smart"  # a generic "noise cancelling" report maps to the group default
    assert p.anc_buttons == ("nc", "adaptive", "transparency", "off")
    assert [(o.key, o.bit) for o in p.hold_options] == [("anc", 2), ("adaptive", 0x800), ("transparency", 4), ("off", 1)]
    assert p.hold_min == 1  # the model's own minSelectCount
    assert p.mode("off").evidence == "reported"  # QuickBuds confirmed noise control on this model


def test_realme_style_tree_matches_the_air7_pro_numbers(out):
    p = profile_from_dict(load(out, "realme-buds-t310"), "x")
    assert [(m.key, m.set, m.reports) for m in p.anc_modes] == [
        ("off", 1, (1,)), ("transparency", 2, (2,)), ("mild", 4, (4,)), ("moderate", 0x10, (0x10,)), ("max", 8, (8,))]
    assert p.group("nc").generic == ()  # the level took over the parent's number
    assert p.hold_min == 2 and {o.key: o.bit for o in p.hold_options} == {"anc": 8, "off": 1, "transparency": 2}
    assert p.mode("off").evidence == "family"


def test_plain_anc_without_levels_is_one_mode(out):
    p = profile_from_dict(load(out, "oneplus-buds-alias"), "x")
    assert [m.key for m in p.anc_modes] == ["off", "transparency", "anc"] and p.anc_groups == ()
    assert p.mode("anc").set == 2 and p.mode("anc").reports == (2,)
    assert p.product_ids == ("060414", "100100", "100101", "100102")  # the id range HeyMelody folds into one model


def test_voice_transparency_is_reported_as_transparency_and_never_offered(out):
    p = profile_from_dict(load(out, "oppo-voice-buds"), "x")
    assert [m.key for m in p.anc_modes] == ["off", "transparency"]
    assert 1 << 6 in p.mode("transparency").reports and p.mode_by_report(1 << 6).key == "transparency"
    assert all(o.key != "voice" for o in p.hold_options)


def test_gesture_options_come_from_the_support_bits(out):
    p = profile_from_dict(load(out, "oneplus-buds-4"), "x")
    assert p.options_for(1) == (0x00, 0x01)  # support 516 = none + play/pause
    assert p.options_for(2) == (0x00, 0x01, 0x11, 0x03, 0x05, 0x06)  # 8807, bit 2 is unknown and left out
    assert p.options_for(4) == (0x08,)  # action 27: the hold cycles noise control
    assert p.options_for(5) == (0x00, 0x0A, 0x07)  # slide: switch track, volume
    t = profile_from_dict(load(out, "realme-buds-t310"), "x")
    assert t.options_for(4) == (0x00, 0x03, 0x08, 0x11) and t.options_for(3)[0] == 0x00


def test_one_button_and_both_hold_models(out):
    n = profile_from_dict(load(out, "oppo-neckband"), "x")
    assert n.gesture_sides == (1,) and not n.hold_both
    b = profile_from_dict(load(out, "realme-both-hold"), "x")
    assert b.hold_both and b.options_for(4) is not None


def test_per_bud_cycle_without_a_shared_mask_is_not_offered(out):
    p = profile_from_dict(load(out, "oppo-per-bud"), "x")
    assert p.hold_options == () and "per bud" in p.notes


def test_features_eq_and_capabilities(out):
    p = profile_from_dict(load(out, "oneplus-buds-4"), "x")
    assert [f.id for f in p.features] == [0x04, 0x0B, 0x11, 0x18, 0x06, 0x1B, 0x1D, 0x1C]  # statusQuery order, 05 unknown
    assert p.feature("dynamic-audio").label == "BassWave" and p.feature(0x18).drops_link
    assert p.custom_eq is not None and p.custom_eq.freqs == (62, 250, 1000, 4000, 8000, 16000)
    assert [(e.id, e.label) for e in p.eq_presets] == [(0, "Balanced"), (1, "Clear Vocals")]
    assert p.golden_sound and p.fit_test
    t = profile_from_dict(load(out, "realme-buds-t310"), "x")
    assert 0x1A in [f.id for f in t.features] and 0x08 in [f.id for f in t.features]  # windNoise flag, realme default list
    assert [e.label for e in t.eq_presets] == ["Pure Raw", "Pure Voice"] and not t.golden_sound
    gated = profile_from_dict(load(out, "oneplus-buds-06eeee"), "x")
    assert gated.eq_presets == ()  # firmware-gated presets are left out


def test_origin_and_notes_say_where_the_data_came_from(out):
    d = load(out, "oneplus-buds-4")
    assert "QuickBuds" in d["origin"] and "GPL" in d["origin"] and imp.GENERATED_MARK in d["notes"] and "confirmed" in d["notes"]


def test_a_rerun_replaces_generated_files_and_keeps_hand_written_ones(out, tmp_path):
    (out / "stale-model.json").write_text(json.dumps({"key": "stale", "notes": imp.GENERATED_MARK + " old"}))
    f = tmp_path / "models.json"
    old = sys.argv
    sys.argv = ["import", str(f), "--out", str(out)]
    try:
        assert imp.main() == 0
    finally:
        sys.argv = old
    assert not (out / "stale-model.json").exists() and (out / "hand.json").exists()
    assert len(list(out.glob("*.json"))) == len(MODELS) - 1 + 0  # all but the speaker (the hand-written file counts as the Air7 Pro)


# -- invariants over the profiles that ship ---------------------------------------------------


def test_bundled_profiles_are_valid_and_unambiguous():
    from realmebuds import profiles

    shipped = profiles.load_all()
    assert len(shipped) >= 100, "the bundled profiles are missing"
    seen_ids: dict[str, str] = {}
    for key, p in shipped.items():
        for pid in p.product_ids:
            assert pid not in seen_ids or seen_ids[pid] == key, f"product id {pid} is claimed by {key} and {seen_ids.get(pid)}"
            seen_ids[pid] = key
        if p.has_anc:
            sets = [m.set for m in p.anc_modes]
            assert len(sets) == len(set(sets)), f"{key}: two modes are written with the same value"
            reports = [r for m in p.anc_modes for r in m.reports] + [g for grp in p.anc_groups for g in grp.generic]
            assert len(reports) == len(set(reports)), f"{key}: a reported value would be ambiguous"
            assert all(m.reports for m in p.anc_modes) and all(m.set for m in p.anc_modes)
            for o in p.hold_options:
                assert o.bit and o.bit & (o.bit - 1) == 0, f"{key}: a hold-cycle option must be a single bit"
            assert p.hold_min <= max(len(p.hold_options), 1)
        for act, fns in p.gesture_options.items():
            assert fns and len(set(fns)) == len(fns), f"{key}: bad function list for action {act}"
        fids = [f.id for f in p.features]
        assert len(fids) == len(set(fids))
    assert "realme-buds-air7-pro" in shipped and shipped["realme-buds-air7-pro"].source == "builtin"
