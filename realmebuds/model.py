"""Static knowledge about the realme Buds Air7 Pro: what each number on the wire means.

Every table here has an evidence level, because a wrong *write* fails silently on
these buds. See docs/PROTOCOL.md for where each fact comes from.

    "app"        confirmed in the decompiled realme Link 5.8.811.2814 (the id and payload
                 the app itself sends)
    "reported"   seen in a capture of an Air7 Pro by someone who owns one
    "family"     seen on other realme/OPPO buds of the same protocol family
    "guess"      plausible, never confirmed; the CLI/UI mark these as experimental
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Noise control. realme models use the same bit for writing and for reporting.

ANC_OFF = 0x01
ANC_TRANSPARENCY = 0x02
ANC_MILD = 0x04
ANC_MAX = 0x08
ANC_MODERATE = 0x10
ANC_SMART = 0x20


@dataclass(frozen=True)
class AncMode:
    key: str
    label: str
    bit: int
    evidence: str


ANC_MODES = (
    AncMode("off", "Off", ANC_OFF, "reported"),
    AncMode("transparency", "Transparency", ANC_TRANSPARENCY, "reported"),
    AncMode("smart", "Noise cancellation: Smart", ANC_SMART, "reported"),
    AncMode("max", "Noise cancellation: Max", ANC_MAX, "reported"),
    AncMode("moderate", "Noise cancellation: Moderate", ANC_MODERATE, "reported"),
    AncMode("mild", "Noise cancellation: Mild", ANC_MILD, "reported"),
)
ANC_BY_KEY = {m.key: m for m in ANC_MODES}
ANC_BY_BIT = {m.bit: m for m in ANC_MODES}
# The level choices that live under "noise cancellation" in the app.
ANC_LEVELS = tuple(m for m in ANC_MODES if m.key in ("smart", "max", "moderate", "mild"))


def anc_label(mask: int) -> str:
    """Human name for a reported ANC value (a bitmask, normally with a single bit)."""
    mode = ANC_BY_BIT.get(mask)
    if mode:
        return mode.label
    return f"unknown (0x{mask:02X})"


# ---------------------------------------------------------------------------
# Feature switches: 0x0403 <id> <0/1>, read with 0x010D.


@dataclass(frozen=True)
class Feature:
    key: str
    id: int
    label: str
    note: str
    evidence: str
    drops_link: bool = False


FEATURES = (
    Feature("wear-detect", 0x04, "Auto play/pause", "Pause when a bud is taken out, resume when it is back", "app"),
    Feature("game-mode", 0x06, "Game mode", "Low-latency audio", "app"),
    Feature("auto-answer", 0x08, "Auto-answer", "Answer a call when a bud is taken out of the case", "app"),
    Feature("enhance-voices", 0x09, "Enhance voices", "Diminish ambient sound, lift voices (transparency)", "app"),
    Feature("dual", 0x11, "Dual-device connection", "Stay connected to two devices", "app"),
    Feature("lhdc", 0x18, "High-definition sound (LHDC)", "Buds reconnect after changing this", "app", drops_link=True),
    Feature("wind", 0x1A, "Wind noise reduction", "", "app"),
    Feature("spatial", 0x1B, "Spatial audio", "", "app"),
    Feature("dynamic-audio", 0x1D, "Dynamic audio", "Three-band bass/mid/treble engine", "app"),
)
FEATURE_BY_KEY = {f.key: f for f in FEATURES}
FEATURE_BY_ID = {f.id: f for f in FEATURES}

# Ids worth probing when exploring what a pair of buds has (`realme-buds features --scan`).
# Includes ids whose meaning for the Air7 Pro is unknown, so they can be matched against
# what the official app shows by toggling a setting and scanning again.
SCAN_FEATURE_IDS = tuple(sorted({f.id for f in FEATURES} | {0x05, 0x07, 0x0B, 0x0C, 0x0F, 0x13, 0x15, 0x17, 0x19, 0x26, 0x36}))

# Dynamic audio: three bands, -5..+5, 1 low, 2 mid, 3 high.
DYNAMIC_BANDS = {1: "Low freq", 2: "Mid freq", 3: "High freq"}
DYNAMIC_RANGE = (-5, 5)

# ---------------------------------------------------------------------------
# Touch controls. The gesture table is a list of [side, button, action, function].

SIDE_LEFT = 1
SIDE_RIGHT = 2
SIDE_BOTH = 4
BTN_MAIN = 1
BTN_CALL = 6

SIDES = {"left": SIDE_LEFT, "right": SIDE_RIGHT}
ACTIONS = {
    "single": 1,
    "double": 2,
    "triple": 3,
    "hold": 4,
    "slide": 5,
    "long-hold": 6,
}
ACTION_LABELS = {1: "Single tap", 2: "Double tap", 3: "Triple tap", 4: "Touch and hold", 5: "Slide", 6: "Long hold"}

FN_NONE = 0x00
FN_ANC_CYCLE = 0x08


@dataclass(frozen=True)
class GestureFn:
    key: str
    code: int
    label: str
    evidence: str


# Codes measured on OnePlus Buds 4 and confirmed to be shared by realme buds in
# the same family. Which of these a given gesture accepts is decided by realme
# Link's per-model data, which we have not read; the buds themselves will reject
# a write they do not like, and every write is read back.
GESTURE_FNS = (
    GestureFn("none", 0x00, "No function", "family"),
    GestureFn("play-pause", 0x01, "Play / pause", "family"),
    GestureFn("assistant", 0x03, "Voice assistant", "family"),
    GestureFn("previous", 0x05, "Previous track", "family"),
    GestureFn("next", 0x06, "Next track", "family"),
    GestureFn("anc-cycle", 0x08, "Switch noise control", "reported"),
    GestureFn("volume-up", 0x0B, "Volume up", "family"),
    GestureFn("volume-down", 0x0C, "Volume down", "family"),
    GestureFn("switch-device", 0x0D, "Switch device", "guess"),
    GestureFn("game-mode", 0x11, "Game mode", "family"),
    GestureFn("decline", 0x1C, "Decline call", "family"),
    GestureFn("answer", 0x1D, "Answer / end call", "family"),
)
GESTURE_FN_BY_KEY = {g.key: g for g in GESTURE_FNS}
GESTURE_FN_BY_CODE = {g.code: g for g in GESTURE_FNS}

# Actions the Air7 Pro lets you remap, per realme Link (per-bud double, triple, hold).
REMAPPABLE_ACTIONS = (2, 3, 4)

# Which modes the hold gesture can cycle through (a bitmask on the wire).
HOLD_CYCLE_MODES = ("off", "transparency", "max")

# ---------------------------------------------------------------------------
# Equaliser. 0x0406 <id> selects a built-in preset.


@dataclass(frozen=True)
class EqPreset:
    id: int
    label: str
    evidence: str


EQ_PRESETS = (
    EqPreset(0, "Pure Raw", "family"),
    EqPreset(1, "Deep Bass", "family"),
    EqPreset(2, "Pure Voice", "family"),
    EqPreset(3, "Pure Bass", "family"),
)
EQ_BY_ID = {p.id: p for p in EQ_PRESETS}
EQ_BY_KEY = {p.label.lower().replace(" ", "-"): p for p in EQ_PRESETS}

# Custom presets: 6 bands, gain limited to +-6 dB by the firmware.
CUSTOM_EQ_FREQS = (62, 250, 1000, 4000, 8000, 16000)
CUSTOM_EQ_GAIN_RANGE = (-6, 6)

# ---------------------------------------------------------------------------
# Battery / wearing components.

COMPONENT_LEFT = 1
COMPONENT_RIGHT = 2
COMPONENT_CASE = 3
COMPONENT_NAMES = {COMPONENT_LEFT: "left", COMPONENT_RIGHT: "right", COMPONENT_CASE: "case"}

# Wearing status values meaning "in ear".
IN_EAR_STATUSES = {3, 7}
# Wearing status values meaning "in the case".
IN_CASE_STATUSES = {0, 4}


def wear_label(status: int) -> str:
    if status in IN_EAR_STATUSES:
        return "in ear"
    if status in IN_CASE_STATUSES:
        return "in case"
    if status in (1, 5):
        return "out of ear"
    return f"unknown ({status})"
