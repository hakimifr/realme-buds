"""Device profiles: everything that differs between one model of earbuds and another.

The wire protocol of a family (framing, command ids, how a write is confirmed) lives in
the library. A profile is the per-model data on top of it: which product id it is, what
the noise-control bits mean, which feature switches exist, what each gesture may do, which
EQ presets there are. Adding a model is adding a JSON file, see docs/ADDING_A_DEVICE.md.

Evidence levels, used for every table entry:

    app       the id/payload is what the vendor's own app sends (decompiled)
    reported  seen on real buds of this model
    family    seen on other models of the same protocol family
    guess     plausible only; the CLI/GUI label these experimental
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
EVIDENCE_LEVELS = ("app", "reported", "family", "guess")
FEATURE_GROUPS = ("noise", "sound", "mode", "personal")
TRANSPORT_CHOICES = ("auto", "rfcomm", "ble")
KNOWN_FAMILIES = ("tl",)  # protocol families this library can speak
RESERVED_FEATURE_KEYS = ("golden-sound", "dynamic-audio")  # have dedicated commands and screens


_UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class AncMode:
    key: str
    label: str
    set: int  # the value written to select the mode
    reports: tuple[int, ...]  # every value the buds may report while it is active (differs from `set` on some models)
    evidence: str = "family"
    group: str | None = None  # a level inside a group such as "noise cancellation"

    @property
    def report(self) -> int:
        """The first reported value; the one a simulator should send."""
        return self.reports[0]


@dataclass(frozen=True)
class AncGroup:
    key: str
    label: str
    default: str  # the mode chosen when the group's button is pressed
    generic: tuple[int, ...] = ()  # reported values that mean "noise cancelling" without naming a level


@dataclass(frozen=True)
class HoldOption:
    key: str
    label: str
    bit: int  # bit of the touch-and-hold cycle mask


@dataclass(frozen=True)
class Feature:
    key: str
    id: int
    label: str
    note: str = ""
    evidence: str = "family"
    drops_link: bool = False
    group: str = "personal"


@dataclass(frozen=True)
class EqPreset:
    id: int
    label: str
    evidence: str = "family"

    @property
    def key(self) -> str:
        return self.label.lower().replace(" ", "-")


@dataclass(frozen=True)
class CustomEq:
    freqs: tuple[int, ...]
    gain_min: int
    gain_max: int
    max_presets: int = 3


@dataclass(frozen=True)
class DeviceProfile:
    key: str
    name: str
    vendor: str
    family: str = "tl"
    product_ids: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    notes: str = ""
    anc_modes: tuple[AncMode, ...] = ()
    anc_groups: tuple[AncGroup, ...] = ()
    anc_buttons: tuple[str, ...] = ()  # order of the top-level buttons (mode keys and group keys)
    hold_options: tuple[HoldOption, ...] = ()
    hold_min: int = 1
    hold_both: bool = False  # touch-and-hold is one gesture for both buds, not one per bud
    features: tuple[Feature, ...] = ()
    gesture_sides: tuple[int, ...] = (1, 2)
    gesture_actions: tuple[int, ...] = ()
    gesture_options: dict[int, tuple[int, ...]] = field(default_factory=dict)
    eq_presets: tuple[EqPreset, ...] = ()
    custom_eq: CustomEq | None = None
    dynamic_bands: dict[int, str] = field(default_factory=dict)
    dynamic_range: tuple[int, int] = (-5, 5)
    fit_test: bool = False
    golden_sound: bool = False
    transport_preferred: str = "auto"  # which link to open: auto, rfcomm or ble
    transport_service_uuid: str | None = None  # BLE GATT service of the control channel (None: the family default)
    source: str = "builtin"
    origin: str = ""  # where the data came from, when it was not written by hand
    generic: bool = False

    # -- noise control ---------------------------------------------------------

    def mode(self, key: str) -> AncMode:
        for m in self.anc_modes:
            if m.key == key:
                return m
        raise KeyError(key)

    def mode_by_set(self, value: int) -> AncMode | None:
        return next((m for m in self.anc_modes if m.set == value), None)

    def mode_by_report(self, value: int) -> AncMode | None:
        """The mode a reported value means. A value that only says "noise cancelling" means the
        group's default level, because the level in use is then not known."""
        exact = next((m for m in self.anc_modes if value in m.reports), None)
        if exact is not None:
            return exact
        for g in self.anc_groups:
            if value in g.generic:
                return self.mode(g.default)
        return None

    def expected_reports(self, mode: AncMode) -> tuple[int, ...]:
        """Values the buds may report after ``mode`` was written: its own, plus the generic
        "noise cancelling" value of its group."""
        grp = self.group(mode.group) if mode.group else None
        return mode.reports + (grp.generic if grp else ())

    def group(self, key: str) -> AncGroup | None:
        return next((g for g in self.anc_groups if g.key == key), None)

    def group_modes(self, key: str) -> tuple[AncMode, ...]:
        return tuple(m for m in self.anc_modes if m.group == key)

    def anc_label(self, report_value: int | None) -> str:
        if report_value is None:
            return "unknown"
        m = self.mode_by_report(report_value)
        if m is None:
            return f"unknown (0x{report_value:02X})"
        grp = self.group(m.group) if m.group else None
        return f"{grp.label}: {m.label}" if grp else m.label

    @property
    def has_anc(self) -> bool:
        return bool(self.anc_modes)

    # -- features ---------------------------------------------------------------

    def feature(self, key_or_id: str | int) -> Feature | None:
        for f in self.features:
            if f.key == key_or_id or f.id == key_or_id:
                return f
        return None

    def feature_for_id(self, fid: int) -> Feature:
        """The known feature for ``fid``, or a placeholder so unknown switches can still be shown."""
        return self.feature(fid) or Feature(f"feature-{fid:02x}", fid, f"Feature 0x{fid:02X}", "id not identified for this model", "guess")

    def feature_ids(self) -> list[int]:
        return [f.id for f in self.features]

    # -- gestures / EQ ----------------------------------------------------------

    def options_for(self, action: int) -> tuple[int, ...] | None:
        return self.gesture_options.get(action)

    def eq(self, preset_id: int) -> EqPreset | None:
        return next((p for p in self.eq_presets if p.id == preset_id), None)

    def eq_by_key(self, key: str) -> EqPreset | None:
        return next((p for p in self.eq_presets if p.key == key), None)

    def describe(self) -> str:
        return f"{self.name} [{self.key}, {self.vendor}, family {self.family}, from {self.source}]"

    # -- (de)serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "key": self.key,
            "name": self.name,
            "vendor": self.vendor,
            "family": self.family,
            "match": {"product_ids": list(self.product_ids), "names": list(self.names)},
            "notes": self.notes,
            **({"origin": self.origin} if self.origin else {}),
        }
        if self.anc_modes:
            d["anc"] = {
                "modes": [
                    {k: v for k, v in {"key": m.key, "label": m.label, "set": m.set,
                                       "report": m.reports[0] if len(m.reports) == 1 else list(m.reports),
                                       "evidence": m.evidence, "group": m.group}.items() if v is not None}
                    for m in self.anc_modes
                ],
                "groups": [{"key": g.key, "label": g.label, "default": g.default, **({"generic": list(g.generic)} if g.generic else {})}
                           for g in self.anc_groups],
                "buttons": list(self.anc_buttons),
                "hold_cycle": {
                    "options": [{"key": o.key, "label": o.label, "bit": o.bit} for o in self.hold_options],
                    "min": self.hold_min,
                },
            }
        d["features"] = [
            {"key": f.key, "id": f.id, "label": f.label, "note": f.note, "evidence": f.evidence,
             "drops_link": f.drops_link, "group": f.group}
            for f in self.features
        ]
        d["gestures"] = {
            "sides": list(self.gesture_sides),
            "actions": list(self.gesture_actions),
            "options": {str(a): list(v) for a, v in self.gesture_options.items()},
            **({"hold_both": True} if self.hold_both else {}),
        }
        eq: dict[str, Any] = {"presets": [{"id": p.id, "label": p.label, "evidence": p.evidence} for p in self.eq_presets]}
        if self.custom_eq:
            eq["custom"] = {"freqs": list(self.custom_eq.freqs), "gain": [self.custom_eq.gain_min, self.custom_eq.gain_max],
                            "max": self.custom_eq.max_presets}
        d["equalizer"] = eq
        if self.dynamic_bands:
            d["dynamic_audio"] = {"bands": {str(k): v for k, v in self.dynamic_bands.items()}, "range": list(self.dynamic_range)}
        d["capabilities"] = {"fit_test": self.fit_test, "golden_sound": self.golden_sound}
        d["transport"] = {"preferred": self.transport_preferred, "service_uuid": self.transport_service_uuid}
        return d


# ---------------------------------------------------------------------------
# JSON -> DeviceProfile, with errors that say where the mistake is.


def _fail(source: str, path: str, message: str) -> ProfileError:
    return ProfileError(f"{source}: {path}: {message}")


def _need(d: dict, key: str, kind, source: str, path: str):
    if key not in d:
        raise _fail(source, path, f"missing '{key}'")
    if not isinstance(d[key], kind) or (isinstance(d[key], bool) and kind is int):
        raise _fail(source, f"{path}.{key}" if path else key, f"expected {kind.__name__ if isinstance(kind, type) else kind}")
    return d[key]


def _int_in(v, lo: int, hi: int, source: str, path: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise _fail(source, path, f"expected an integer {lo}..{hi}, got {v!r}")
    return v


def _evidence(v, source: str, path: str, default: str = "family") -> str:
    v = default if v is None else v
    if v not in EVIDENCE_LEVELS:
        raise _fail(source, path, f"evidence must be one of {', '.join(EVIDENCE_LEVELS)}")
    return v


def profile_from_dict(d: Any, source: str = "<dict>") -> DeviceProfile:
    if not isinstance(d, dict):
        raise _fail(source, "$", "a profile must be a JSON object")
    if d.get("schema") != SCHEMA_VERSION:
        raise _fail(source, "schema", f"expected schema {SCHEMA_VERSION}, got {d.get('schema')!r}")
    key = _need(d, "key", str, source, "")
    if not key or not all(c.islower() or c.isdigit() or c == "-" for c in key):
        raise _fail(source, "key", "use lowercase letters, digits and dashes")
    name = _need(d, "name", str, source, "")
    family = d.get("family", "tl")
    if family not in KNOWN_FAMILIES:
        raise _fail(source, "family", f"unsupported protocol family {family!r}; this build speaks: {', '.join(KNOWN_FAMILIES)}")
    match = d.get("match", {})
    pids = tuple(str(x).upper() for x in match.get("product_ids", []))
    for i, pid in enumerate(pids):
        if len(pid) != 6 or any(c not in "0123456789ABCDEF" for c in pid):
            raise _fail(source, f"match.product_ids[{i}]", "a product id is six hex digits, e.g. 064C12")
    names = tuple(str(x) for x in match.get("names", []))
    if not pids and not names:
        raise _fail(source, "match", "give at least one product id or name to recognise the buds by")

    modes: list[AncMode] = []
    groups: list[AncGroup] = []
    buttons: tuple[str, ...] = ()
    hold: list[HoldOption] = []
    hold_min = 1
    anc = d.get("anc")
    if anc is not None:
        seen = set()
        for i, m in enumerate(_need(anc, "modes", list, source, "anc")):
            p = f"anc.modes[{i}]"
            k = _need(m, "key", str, source, p)
            if k in seen:
                raise _fail(source, p, f"duplicate mode key {k!r}")
            seen.add(k)
            sv = _int_in(m.get("set"), 0, 0xFFFFFFFF, source, p + ".set")
            raw = m.get("report", sv)
            raw = raw if isinstance(raw, list) else [raw]
            if not raw:
                raise _fail(source, p + ".report", "give at least one reported value")
            rvs = tuple(_int_in(x, 0, 0xFFFFFFFF, source, p + ".report") for x in raw)
            modes.append(AncMode(k, _need(m, "label", str, source, p), sv, rvs, _evidence(m.get("evidence"), source, p + ".evidence"), m.get("group")))
        for i, g in enumerate(anc.get("groups", [])):
            p = f"anc.groups[{i}]"
            gk = _need(g, "key", str, source, p)
            default = _need(g, "default", str, source, p)
            if default not in seen:
                raise _fail(source, p + ".default", f"unknown mode {default!r}")
            generic = tuple(_int_in(x, 1, 0xFFFFFFFF, source, p + ".generic") for x in g.get("generic", []))
            groups.append(AncGroup(gk, _need(g, "label", str, source, p), default, generic))
        gkeys = {g.key for g in groups}
        for m in modes:
            if m.group is not None and m.group not in gkeys:
                raise _fail(source, "anc.modes", f"mode {m.key!r} is in unknown group {m.group!r}")
        buttons = tuple(anc.get("buttons") or [m.key for m in modes if m.group is None] + [g.key for g in groups])
        for b in buttons:
            if b not in seen and b not in gkeys:
                raise _fail(source, "anc.buttons", f"unknown mode or group {b!r}")
        hc = anc.get("hold_cycle", {})
        for i, o in enumerate(hc.get("options", [])):
            p = f"anc.hold_cycle.options[{i}]"
            hold.append(HoldOption(_need(o, "key", str, source, p), _need(o, "label", str, source, p), _int_in(o.get("bit"), 1, 0xFFFFFFFF, source, p + ".bit")))
        hold_min = _int_in(hc.get("min", 1), 1, 32, source, "anc.hold_cycle.min")

    feats: list[Feature] = []
    fkeys, fids = set(), set()
    for i, f in enumerate(d.get("features", [])):
        p = f"features[{i}]"
        fk = _need(f, "key", str, source, p)
        fid = _int_in(f.get("id"), 0, 255, source, p + ".id")
        if fk in fkeys or fid in fids:
            raise _fail(source, p, f"duplicate feature key {fk!r} or id 0x{fid:02X}")
        fkeys.add(fk)
        fids.add(fid)
        grp = f.get("group", "personal")
        if grp not in FEATURE_GROUPS:
            raise _fail(source, p + ".group", f"group must be one of {', '.join(FEATURE_GROUPS)}")
        feats.append(Feature(fk, fid, _need(f, "label", str, source, p), f.get("note", ""), _evidence(f.get("evidence"), source, p + ".evidence"), bool(f.get("drops_link", False)), grp))

    g = d.get("gestures", {})
    sides = tuple(_int_in(x, 1, 4, source, "gestures.sides") for x in g.get("sides", [1, 2]))
    actions = tuple(_int_in(x, 1, 6, source, "gestures.actions") for x in g.get("actions", []))
    hold_both = bool(g.get("hold_both", False))
    if hold_both and 4 not in actions:
        raise _fail(source, "gestures.hold_both", "needs the hold gesture (4) in gestures.actions")
    options: dict[int, tuple[int, ...]] = {}
    for a, v in g.get("options", {}).items():
        if not str(a).isdigit() or int(a) not in actions:
            raise _fail(source, f"gestures.options.{a}", "options must be for an action listed in gestures.actions")
        options[int(a)] = tuple(_int_in(x, 0, 255, source, f"gestures.options.{a}") for x in v)

    eqd = d.get("equalizer", {})
    presets = tuple(EqPreset(_int_in(p.get("id"), 0, 255, source, "equalizer.presets.id"), _need(p, "label", str, source, "equalizer.presets"), _evidence(p.get("evidence"), source, "equalizer.presets.evidence")) for p in eqd.get("presets", []))
    custom = None
    if "custom" in eqd:
        c = eqd["custom"]
        gain = c.get("gain", [-6, 6])
        custom = CustomEq(tuple(_int_in(x, 20, 20000, source, "equalizer.custom.freqs") for x in c.get("freqs", [])), _int_in(gain[0], -30, 0, source, "equalizer.custom.gain"), _int_in(gain[1], 0, 30, source, "equalizer.custom.gain"), _int_in(c.get("max", 3), 1, 32, source, "equalizer.custom.max"))
        if not custom.freqs:
            raise _fail(source, "equalizer.custom.freqs", "list the centre frequencies of the bands")

    dyn = d.get("dynamic_audio")
    bands: dict[int, str] = {}
    drange = (-5, 5)
    if dyn:
        bands = {int(k): str(v) for k, v in dyn.get("bands", {}).items()}
        r = dyn.get("range", [-5, 5])
        drange = (_int_in(r[0], -128, 0, source, "dynamic_audio.range"), _int_in(r[1], 0, 127, source, "dynamic_audio.range"))

    caps = d.get("capabilities", {})
    transport = d.get("transport", {})
    if not isinstance(transport, dict):
        raise _fail(source, "transport", "expected an object like {\"preferred\": \"auto\", \"service_uuid\": null}")
    preferred = transport.get("preferred", "auto")
    if preferred not in TRANSPORT_CHOICES:
        raise _fail(source, "transport.preferred", f"must be one of {', '.join(TRANSPORT_CHOICES)}, got {preferred!r}")
    service_uuid = transport.get("service_uuid")
    if service_uuid is not None:
        if not isinstance(service_uuid, str) or not _UUID_RE.match(service_uuid):
            raise _fail(source, "transport.service_uuid", f"expected a UUID like 0000079a-d102-11e1-9b23-00025b00a5a5 or null, got {service_uuid!r}")
        service_uuid = service_uuid.lower()
    return DeviceProfile(
        key=key, name=name, vendor=str(d.get("vendor", "unknown")), family=family, product_ids=pids, names=names,
        notes=str(d.get("notes", "")), anc_modes=tuple(modes), anc_groups=tuple(groups), anc_buttons=buttons,
        hold_options=tuple(hold), hold_min=hold_min, hold_both=hold_both, origin=str(d.get("origin", "")), features=tuple(feats), gesture_sides=sides, gesture_actions=actions,
        gesture_options=options, eq_presets=presets, custom_eq=custom, dynamic_bands=bands, dynamic_range=drange,
        fit_test=bool(caps.get("fit_test", False)), golden_sound=bool(caps.get("golden_sound", False)),
        transport_preferred=preferred, transport_service_uuid=service_uuid, source=source,
    )
