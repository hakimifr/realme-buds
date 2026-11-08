"""Build a starting-point profile from buds that are connected right now.

``realme-buds profile detect`` (and the GUI's Advanced tools) use this so adding another model
starts from what the buds actually report instead of a blank file. Everything it cannot know
(the noise-control numbers, gesture options, EQ preset names) is left out or marked ``guess``,
and the notes say what to fill in. See docs/ADDING_A_DEVICE.md.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from .. import protocol
from .schema import SCHEMA_VERSION, profile_from_dict

if TYPE_CHECKING:
    from ..client import Buds


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "unknown-buds"


def build_draft(buds: "Buds") -> dict:
    st = buds.state
    known = buds.active_profile
    name = buds.device_name or (known.name if not known.generic else None) or f"Earbuds {st.product_id or ''}".strip()

    features = []
    for fid in sorted(st.features):
        f = known.feature(fid)
        if f:  # the profile in use already names it: keep what it knows
            features.append({"key": f.key, "id": f.id, "label": f.label, "note": f.note, "evidence": f.evidence,
                             "drops_link": f.drops_link, "group": f.group})
        else:
            features.append({"key": f"feature-{fid:02x}", "id": fid, "label": f"Feature 0x{fid:02X}",
                             "note": "id not identified yet: toggle it in the vendor app and scan again", "evidence": "guess", "group": "personal"})

    table = [e for e in st.gestures if e.btn != 6]
    actions = sorted({e.act for e in table})
    sides = sorted({e.dev for e in table if e.dev in (1, 2)}) or [1, 2]

    caps = sorted(st.capabilities)
    todo = [
        "Fill in 'anc' (what each noise-control value means). The current value is "
        + (f"0x{st.anc:02X}" if st.anc is not None else "unknown")
        + ". Change the mode in the vendor app and read it again with: realme-buds raw 010c 0101",
        "Fill in gestures.options (which functions each gesture accepts) and equalizer.presets (names for ids).",
        "Raise 'evidence' to 'reported' or 'app' only for what you have confirmed.",
    ]
    if st.eq is not None:
        todo.append(f"The buds currently report EQ preset id {st.eq}.")
    notes = (
        f"Draft made by 'realme-buds profile detect' on {time.strftime('%Y-%m-%d')}. "
        f"Capability bits set: {caps}. " + " ".join(todo)
    )

    draft: dict = {
        "schema": SCHEMA_VERSION,
        "key": slug(name),
        "name": name,
        "vendor": "unknown",
        "family": "tl",
        "match": {"product_ids": [st.product_id] if st.product_id else [], "names": [buds.device_name] if buds.device_name else []},
        "notes": notes,
        "features": features,
        "gestures": {"sides": sides, "actions": actions, "options": {}},
        "equalizer": {"presets": []},
        "capabilities": {
            "fit_test": buds.supports(protocol.CMD_FIT_TEST) and protocol.CAPABILITY_BIT[protocol.CMD_FIT_TEST] in st.capabilities,
            "golden_sound": 0x0B in st.features and buds.supports(protocol.CMD_HEARING_RECORD),
        },
    }
    if st.dynamic_audio:
        draft["dynamic_audio"] = {
            "bands": {str(b.band): f"Band {b.band}" for b in st.dynamic_audio},
            "range": [st.dynamic_audio[0].lo, st.dynamic_audio[0].hi],
        }
    profile_from_dict(draft, "draft")  # a draft must at least load
    return draft
