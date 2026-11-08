#!/usr/bin/env python3
"""Turn QuickBuds' ``models.json`` into one device profile per model.

    python3 scripts/import_quickbuds.py /path/to/QuickBuds/app/src/main/assets/models.json

QuickBuds (https://github.com/spizganed/QuickBuds, GPL-3.0-or-later) extracted that file from
HeyMelody's and realme Link's per-model data. It is the only readable per-model dataset available:
HeyMelody ships its own list encrypted and realme Link fetches it from the cloud. The profiles written
here are therefore a derived work of QuickBuds' data, which is why this repository is GPL-3.0-or-later.

How the data is read follows QuickBuds' ``AncModes.kt`` and ``docs/PROTOCOL.md``:

* a mode's *write* value is ``1 << protocolIndex`` of the mode;
* a *reported* value is matched by bit position against every ``protocolIndex`` in the tree, parents
  first and then children, so one mode can be reported under several numbers (Off is written as bit 0
  but reported as bit 3 on some models);
* gesture ``support`` is a bit mask of the functions a gesture accepts.

Nothing here is verified on real buds. Evidence is ``family`` for everything except what QuickBuds lists
under the model's ``confirmed`` field, which is ``reported``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GENERATED_MARK = "Generated from QuickBuds"
# Entries in the source that are not earbuds (the app lists realme speakers too). Nothing here is built for them.
SKIP_NAME_PARTS = ("Speaker",)
ORIGIN = (
    "QuickBuds models.json (GPL-3.0-or-later, https://github.com/spizganed/QuickBuds), "
    "itself extracted from HeyMelody and realme Link"
)

# HeyMelody modeType -> (key, label). 5 is the parent of the noise-cancelling levels (or ANC alone).
MODE_TYPES = {1: ("off", "Off"), 2: ("transparency", "Transparency"), 3: ("mild", "Mild"), 8: ("moderate", "Moderate"),
              4: ("max", "Max"), 5: ("anc", "Noise cancellation"), 10: ("adaptive", "Adaptive"), 7: ("smart", "Smart"),
              6: ("voice", "Transparency (voice)")}
LEVEL_ORDER = ("mild", "moderate", "max", "smart")

# gesture "support" bit -> function code (QuickBuds docs/PROTOCOL.md section 6). AI options are left out on purpose.
SUPPORT_BITS = {1: 0x03, 4: 0x01, 8: 0x0B, 16: 0x0C, 32: 0x05, 64: 0x06, 128: 0x08, 256: 0x09, 512: 0x00, 1024: 0x07,
                2048: 0x0A, 4096: 0x0D, 8192: 0x11, 32768: 0x16, 65536: 0x12}
# control "action" -> gesture action (1 single, 2 double, 3 triple, 4 hold, 5 slide, 6 long hold)
ACTION_MAP = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 16: 1, 17: 2, 18: 3}
NOISE_CYCLE_ACTIONS = (11, 20, 27)

# feature id -> (key, label, group, note, drops_link)
FEATURES = {
    0x04: ("wear-detect", "Auto play/pause", "personal", "Pause when a bud is taken out, resume when it is back", False),
    0x06: ("game-mode", "Game mode", "mode", "Low-latency audio", False),
    0x08: ("auto-answer", "Auto-answer", "personal", "Answer a call when a bud is taken out of the case", False),
    0x09: ("enhance-voices", "Enhance voices", "noise", "", False),
    0x0B: ("golden-sound", "Golden Sound", "sound", "Hearing profile made by the hearing test", False),
    0x11: ("dual", "Dual-device connection", "personal", "Stay connected to two devices", False),
    0x17: ("power-saving", "Power saving", "mode", "Restarts the buds when changed", True),
    0x18: ("lhdc", "High-definition sound (LHDC)", "sound", "Buds reconnect after changing this", True),
    0x1A: ("wind", "Wind noise reduction", "noise", "", False),
    0x1B: ("spatial", "Spatial audio", "sound", "", False),
    0x1C: ("smart-volume", "Smart volume", "sound", "", False),
    0x1D: ("dynamic-audio", "Dynamic audio", "sound", "", False),
    0x27: ("game-sound", "Game sound effects", "mode", "", False),
    0x30: ("adaptive-volume", "Adaptive volume", "sound", "", False),
    0x31: ("adaptive-ear", "Adaptive left/right ear", "sound", "", False),
    0x32: ("conversation-awareness", "Conversation awareness", "noise", "", False),
    0x35: ("touch-hold-volume", "Touch-and-hold volume", "personal", "", False),
    0x37: ("swift-pair", "Windows Swift Pair", "personal", "", False),
    0x38: ("adaptive-sound", "Adaptive sound", "sound", "", False),
    0x3A: ("sleep-pause", "Pause when asleep", "personal", "", False),
    0x3B: ("head-gestures", "Head gestures", "personal", "", False),
}
# Asked for when a model has no statusQuery of its own: realme Link's list for realme models, a short one otherwise.
DEFAULT_REALME_IDS = (0x04, 0x06, 0x08, 0x09, 0x11, 0x18, 0x1B, 0x1D)
DEFAULT_OTHER_IDS = (0x04, 0x06, 0x09, 0x11, 0x18, 0x1B)
CONFIRMED_FEATURE = {"wind": 0x1A, "game": 0x06, "hires": 0x18, "spatial": 0x1B, "golden": 0x0B, "dual": 0x11, "wear": 0x04}

# HeyMelody EQ modeType -> name (QuickBuds ui/EqActivity.kt and strings.xml). Types 1-4 have other names on some models.
EQ_NAMES = {1: "Classic", 2: "Dynamic bass", 3: "Clear Vocals", 4: "Clear", 5: "Default", 6: "Dynaudio Simple & Clear",
            7: "Dynaudio Warm & Soft", 8: "Dynaudio Punchy", 9: "Dynaudio Real", 10: "Tuned by Joe Hisaishi", 11: "Balanced",
            12: "Bass", 13: "Bold", 14: "Clear Vocals", 15: "Gentle", 16: "Enco X Classic", 17: "Balanced",
            18: "Reno Featured (Dawn)", 19: "Hans Zimmer Soundscape Tuning", 20: "Natural Inspiration",
            21: "Reno Featured (Sunrise Glow)", 22: "Nature Balance", 23: "Punchy", 24: "Spacious",
            25: "Reno Featured (Purple Galaxy)", 26: "Ultimate sound", 27: "HD clarity", 28: "Pure vocals",
            29: "Thundering bass", 30: "Dynaudio featured", 31: "Bass boost", 32: "Clear Vocals", 33: "Galactic Monologue",
            34: "Vibrant", 35: "Default", 36: "Dynaudio Simple & Clear", 37: "Dynaudio Warm & Soft", 38: "Dynaudio Punchy",
            39: "Dynaudio Real", 40: "Dynaudio vocal", 101: "Pure Raw", 102: "Deep Bass", 103: "Pure Voice", 104: "Pure Bass"}
# product ids that HeyMelody folds into one (QuickBuds ModelCatalog.normalise)
ID_ALIASES = {"060414": ["100100", "100101", "100102"], "060814": ["100200", "100201", "100202"],
              "068414": ["108100", "108101", "108102"], "068814": ["108200", "108201", "108202"]}
DEFAULT_EQ_FREQS = [62, 250, 1000, 4000, 8000, 16000]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def vendor_of(name: str) -> str:
    for prefix in ("realme", "OPPO", "OnePlus", "DIZO"):
        if name.startswith(prefix):
            return prefix.lower()
    return "unknown"


# -- noise control -----------------------------------------------------------------------------


def convert_anc(tree: list[dict], evidence: str) -> tuple[dict, list[dict], int] | None:
    """Return (anc section, hold options, top-level mode count) or None if the model has no table."""
    set_idx: dict[str, int] = {}
    report: dict[int, str] = {}
    top_level: list[tuple[str, int]] = []
    for top in tree:
        key = MODE_TYPES.get(top["modeType"], (None,))[0]
        if key is None:
            continue
        shown = "transparency" if key == "voice" else key
        if not top.get("decideByEarDevice"):
            set_idx.setdefault(key, top["protocolIndex"])
            if key != "voice":
                top_level.append((key, top["protocolIndex"]))
        report[top["protocolIndex"]] = shown
        for child in top.get("childrenMode", []):
            ckey = MODE_TYPES.get(child["modeType"], (None,))[0]
            if ckey is None:
                continue
            if ckey != key:  # a child that repeats its parent (Off's bit 3) is a report value only
                set_idx.setdefault(ckey, child["protocolIndex"])
            report[child["protocolIndex"]] = "transparency" if ckey == "voice" else ckey
    set_idx.pop("voice", None)
    has_levels = any(k in set_idx for k in LEVEL_ORDER)
    nc_generic = [1 << i for i, n in report.items() if n == "anc"]
    plain_anc = "anc" in set_idx and not has_levels
    if "anc" in set_idx and has_levels:
        del set_idx["anc"]
    if not set_idx:
        return None

    def reports_of(key: str) -> list[int]:
        vals = sorted(1 << i for i, n in report.items() if n == key)
        if key == "anc" and plain_anc:
            vals = sorted(set(vals) | {1 << set_idx["anc"]})
        return vals or [1 << set_idx[key]]

    modes: list[dict] = []
    order = [k for k in ("off", "transparency", "adaptive", "anc") if k in set_idx] + [k for k in LEVEL_ORDER if k in set_idx]
    for key in order:
        label = next(v[1] for v in MODE_TYPES.values() if v[0] == key)
        m: dict = {"key": key, "label": label, "set": 1 << set_idx[key], "evidence": evidence}
        reps = reports_of(key)
        m["report"] = reps if len(reps) > 1 else reps[0]
        if key in LEVEL_ORDER:
            m["group"] = "nc"
        modes.append(m)
    groups = []
    buttons: list[str] = []
    if has_levels:
        default = "smart" if "smart" in set_idx else "moderate" if "moderate" in set_idx else next(k for k in LEVEL_ORDER if k in set_idx)
        groups.append({"key": "nc", "label": "Noise cancellation", "default": default, "generic": sorted(nc_generic)})
    for key, _idx in top_level:
        if key == "anc":
            btn = "nc" if has_levels else "anc"
        else:
            btn = key
        if btn in set_idx or btn == "nc":
            if btn not in buttons:
                buttons.append(btn)
    for key in order:
        if key not in LEVEL_ORDER and key not in buttons:
            buttons.append(key)
    hold = []
    for key, idx in top_level:
        if key in LEVEL_ORDER or key == "voice":
            continue
        label = "Noise cancellation" if key == "anc" else next(v[1] for v in MODE_TYPES.values() if v[0] == key)
        hold.append({"key": key, "label": label, "bit": 1 << idx})
    anc = {"modes": modes, "groups": groups, "buttons": buttons}
    return anc, hold, len(top_level)


# -- gestures ----------------------------------------------------------------------------------


def functions_from_support(support: int) -> list[int]:
    out = [fn for bit, fn in SUPPORT_BITS.items() if support & bit]
    return sorted(out, key=lambda fn: fn != 0x00)  # "none" first, like the apps list it


def convert_gestures(model: dict, hold_options: list[dict]) -> tuple[dict, bool, int]:
    """Return (gestures section, per-bud-hold-without-shared-cycle, hold_min)."""
    options: dict[int, list[int]] = {}
    cycle_min = None
    for c in model.get("control", []):
        action = c["action"]
        if action in NOISE_CYCLE_ACTIONS:
            options.setdefault(4, [])
            if 0x08 not in options[4]:
                options[4].append(0x08)
            cycle_min = c.get("minSelectCount")
            continue
        act = ACTION_MAP.get(action)
        if act is None:
            continue
        fns = functions_from_support(c.get("support", 0))
        if fns:
            options[act] = sorted(set(options.get(act, [])) | set(fns), key=lambda fn: fn != 0x00)
    per_bud_hold = bool(model.get("longPressType"))
    if per_bud_hold:
        options[4] = functions_from_support(model["longPressType"])
    both = bool(model.get("bothHold"))
    one = bool(model.get("oneButton"))
    sides = [1] if one else [1, 2]
    g = {"sides": sides, "actions": sorted(options), "options": {str(a): v for a, v in sorted(options.items())}}
    if both and 4 in options:
        g["hold_both"] = True
    brand_oneplus = model["name"].startswith("OnePlus")
    hold_min = cycle_min or (2 if brand_oneplus or per_bud_hold else 1)
    return g, per_bud_hold and not model.get("sharedHoldMask"), hold_min


# -- everything else ----------------------------------------------------------------------------


def convert_model(model: dict, key: str) -> dict:
    name = model["name"]
    pid = model["id"].upper()
    confirmed = set(model.get("confirmed", []))
    anc_evidence = "reported" if "anc" in confirmed else "family"
    notes_extra: list[str] = []

    d: dict = {
        "schema": 1, "key": key, "name": name, "vendor": vendor_of(name), "family": "tl",
        "match": {"product_ids": [pid, *ID_ALIASES.get(pid, [])], "names": [name]},
        "origin": ORIGIN,
    }
    gestures, no_cycle, hold_min = convert_gestures(model, [])
    anc_result = convert_anc(model["noiseReductionMode"], anc_evidence) if model.get("noiseReductionMode") else None
    if anc_result:
        anc, hold, _n = anc_result
        if no_cycle:
            hold = []
            notes_extra.append("Touch-and-hold noise cycle is per bud on this model, which is not supported yet, so it is not offered.")
        if hold:
            anc["hold_cycle"] = {"options": hold, "min": min(hold_min, len(hold))}
        d["anc"] = anc
    else:
        notes_extra.append("HeyMelody lists this model without noise-control modes, so none are offered.")

    ids = [int(x, 16) for x in model["statusQuery"].split()] if model.get("statusQuery") else list(DEFAULT_REALME_IDS if name.startswith(("realme", "DIZO")) else DEFAULT_OTHER_IDS)
    if model.get("windNoise") and 0x1A not in ids:
        ids.append(0x1A)
    if model.get("spatialSwitch") and 0x1B not in ids:
        ids.append(0x1B)
    features = []
    confirmed_ids = {CONFIRMED_FEATURE[c] for c in confirmed if c in CONFIRMED_FEATURE}
    for fid in ids:
        if fid not in FEATURES:
            continue
        fkey, label, group, note, drops = FEATURES[fid]
        if fid == 0x1D and model.get("bassEngineSupport"):
            label, note = "BassWave", "Bass boost engine"
        features.append({"key": fkey, "id": fid, "label": label, "note": note, "group": group, "drops_link": drops,
                         "evidence": "reported" if fid in confirmed_ids else "family"})
    d["features"] = features
    if model.get("bassEngineSupport"):
        notes_extra.append("BassWave has one level on the buds' side and is not modelled as bands here, only its switch is.")

    if gestures["actions"]:
        d["gestures"] = gestures
    if gestures.get("hold_both"):
        notes_extra.append("Holding is one gesture for both buds on this model.")

    presets, seen = [], set()
    for list_key in ("equalizerModeCompat", "equalizerModeByVersion", "equalizerMode"):
        for e in model.get(list_key, []):
            if e.get("minFirmVersion", 0) > 0 or e["protocolIndex"] in seen:
                continue  # firmware-gated entries are left out: the firmware may not have them
            alt = model["name"] in ("OPPO Enco R", "OPPO Enco Air2") or model.get("equalizer") == 2
            mt = e["modeType"]
            label = {1: "Nature Balance", 2: "Bass boost"}.get(mt) if alt and mt in (1, 2) else None
            label = label or ("Gentle" if mt == 4 and name == "OPPO Enco R" else EQ_NAMES.get(mt))
            if label is None:
                continue
            seen.add(e["protocolIndex"])
            presets.append({"id": e["protocolIndex"], "label": label, "evidence": "reported" if "eq" in confirmed else "family"})
    eq: dict = {"presets": sorted(presets, key=lambda p: p["id"])}
    if model.get("customEqualizer"):
        freqs = model.get("customEqFrequency") or DEFAULT_EQ_FREQS
        eq["custom"] = {"freqs": freqs, "gain": [-6, 6], "max": model.get("customEqMax") or 3}
    d["equalizer"] = eq

    d["capabilities"] = {"fit_test": True, "golden_sound": 0x0B in ids}
    notes = f"{GENERATED_MARK} models.json. Not tried on real buds: everything is evidence 'family'" + (
        f", except what QuickBuds confirmed on its owners' buds ({', '.join(sorted(confirmed))})" if confirmed else "") + "."
    d["notes"] = " ".join([notes, *notes_extra])
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models_json", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "realmebuds" / "profiles" / "builtin")
    args = ap.parse_args()
    models = json.loads(args.models_json.read_text(encoding="utf-8"))["whiteList"]
    args.out.mkdir(parents=True, exist_ok=True)

    # a rerun replaces what an earlier run generated and never touches hand-written profiles
    hand_ids: set[str] = set()
    for f in args.out.glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        if GENERATED_MARK in data.get("notes", ""):
            f.unlink()
        else:
            hand_ids.update(data.get("match", {}).get("product_ids", []))

    base_counts: dict[str, int] = {}
    for m in models:
        base_counts[slug(m["name"])] = base_counts.get(slug(m["name"]), 0) + 1
    taken = {json.loads(f.read_text(encoding="utf-8")).get("key") for f in args.out.glob("*.json")}
    written, skipped, not_buds = 0, [], []
    for m in models:
        if m["id"].upper() in hand_ids:
            skipped.append(m["name"])
            continue
        if any(part in m["name"] for part in SKIP_NAME_PARTS):
            not_buds.append(m["name"])
            continue
        # names are not unique in the source (the same name appears under several product ids)
        key = slug(m["name"]) + (f"-{m['id'].lower()}" if base_counts[slug(m["name"])] > 1 else "")
        if key in taken:
            key = f"{key}-{m['id'].lower()}"
        taken.add(key)
        (args.out / f"{key}.json").write_text(json.dumps(convert_model(m, key), indent=1) + "\n", encoding="utf-8")
        written += 1
    print(f"wrote {written} profiles to {args.out}; kept hand-written: {', '.join(skipped) or 'none'}; "
          f"skipped, not earbuds: {', '.join(not_buds) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
