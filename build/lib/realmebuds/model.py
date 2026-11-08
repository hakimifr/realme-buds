"""Vocabulary of the "TL" earbud protocol family, shared by every model that speaks it.

Per-model data (which feature ids exist, what the noise-control values mean, which gestures
accept which functions, EQ presets) is NOT here. It lives in device profiles, see
``realmebuds/profiles`` and docs/ADDING_A_DEVICE.md. What stays here is what a function code
or a status byte means in the family, and the Golden Sound / fit-test tables, which are the same
for every model of the family that has those features.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Noise-control values as realme models use them (the same number is written and reported).
# These are named only so the built-in Air7 Pro profile, the simulator and the tests can refer
# to them; code that talks to buds must go through the profile in use.

ANC_OFF = 0x01
ANC_TRANSPARENCY = 0x02
ANC_MILD = 0x04
ANC_MAX = 0x08
ANC_MODERATE = 0x10
ANC_SMART = 0x20

# Feature ids worth asking about when the model is unknown or when exploring
# (`realme-buds features --scan`). Reading an id the buds do not have is harmless.
DEFAULT_SCAN_IDS = (0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0B, 0x0C, 0x0F, 0x11, 0x13, 0x15, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1D, 0x26, 0x36)

# ---------------------------------------------------------------------------
# Touch controls. The gesture table is a list of [side, button, action, function].

SIDE_LEFT = 1
SIDE_RIGHT = 2
SIDE_BOTH = 4
BTN_MAIN = 1
BTN_CALL = 6

SIDES = {"left": SIDE_LEFT, "right": SIDE_RIGHT, "both": SIDE_BOTH}  # "both": models with one hold gesture for both buds
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


# Function codes of the family. Measured on OnePlus Buds 4 (none, play/pause, assistant, previous, next,
# noise cycle, the two slide functions, volume, game mode, call handling) and used unchanged by realme buds.
# Favourite/collect music and zen mode are named in HeyMelody's data but were not measured. Which of
# these a gesture accepts is the model's profile; the buds reject what they do not like and every
# write is read back.
GESTURE_FNS = (
    GestureFn("none", 0x00, "No function", "family"),
    GestureFn("play-pause", 0x01, "Play / pause", "family"),
    GestureFn("assistant", 0x03, "Voice assistant", "family"),
    GestureFn("previous", 0x05, "Previous track", "family"),
    GestureFn("next", 0x06, "Next track", "family"),
    GestureFn("slide-volume", 0x07, "Volume (slide)", "family"),
    GestureFn("anc-cycle", 0x08, "Switch noise control", "reported"),
    GestureFn("favourite-music", 0x09, "Favourite music", "guess"),
    GestureFn("slide-track", 0x0A, "Switch track (slide)", "family"),
    GestureFn("volume-up", 0x0B, "Volume up", "family"),
    GestureFn("volume-down", 0x0C, "Volume down", "family"),
    GestureFn("switch-device", 0x0D, "Switch device", "guess"),
    GestureFn("zen-mode", 0x12, "Zen mode", "guess"),
    GestureFn("game-mode", 0x11, "Game mode", "family"),
    GestureFn("collect-music", 0x16, "Collect music", "guess"),
    GestureFn("decline", 0x1C, "Decline call", "family"),
    GestureFn("answer", 0x1D, "Answer / end call", "family"),
)
GESTURE_FN_BY_KEY = {g.key: g for g in GESTURE_FNS}
GESTURE_FN_BY_CODE = {g.code: g for g in GESTURE_FNS}

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


# ---------------------------------------------------------------------------
# Earbud fit test. Result push: `04 <side> <status> <side> <status>`.

FIT_GOOD = 1
FIT_AVERAGE = 0
FIT_RETRY = (2, 3)
FIT_REMOVED = 4
FIT_INTERRUPTED = 5


def fit_label(status: int) -> str:
    """Wording follows realme Link: 1 good, 0 average, anything else not listed is a poor seal."""
    if status == FIT_GOOD:
        return "good seal"
    if status == FIT_AVERAGE:
        return "average seal, try another ear tip or adjust"
    return "poor seal, adjust the bud or try another ear tip"


# ---------------------------------------------------------------------------
# Golden Sound (the app calls it HearId): ear scan, then a hearing test, then apply.
# All numbers below are the app's own (GoldenHearIdDetectingViewModel).

HEARING_SIDES = (SIDE_LEFT, SIDE_RIGHT)
HEARING_BANDS = (1, 2, 3, 4, 5, 6)
# The test visits the left ear's six bands, then the right ear's.
HEARING_NODES = tuple((side, band) for side in HEARING_SIDES for band in HEARING_BANDS)
# Slider stops, quietest to loudest (dB). The tone plays at the chosen stop.
HEARING_STOPS = (-120, -88, -55, -52, -49, -45, -41, -38, -35, -30, -25, -22, -19, -15, -11, -8, -5, -1, 3, 5, 7, 10, 13, 15, 17)
# The values a profile can actually store; a stop is snapped to the nearest of these.
HEARING_RESULT_VALUES = (-55, -49, -41, -35, -25, -19, -11, -5, 3, 7, 13, 17)
HEARING_START_DB = -25
HEARING_LOUD_WARNING_DB = 10
# The id of the "restore data" block the app sends before applying a profile.
HEARING_RESTORE_TYPE_ID = 0x0B
EAR_SCAN_BYTES = 168


def snap_hearing_value(db: int) -> int:
    return min(HEARING_RESULT_VALUES, key=lambda v: (abs(v - db), v))
