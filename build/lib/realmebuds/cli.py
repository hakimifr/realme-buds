"""Command line interface: ``realme-buds <command>``."""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import sys
import time
from typing import Iterator

from pathlib import Path

from . import model, parsing, profiles
from .client import Buds, BudsError, State
from .profiles import DeviceProfile, ProfileError
from .session import connect_buds
from .transport import TRANSPORTS, TransportError, list_devices

EXPERIMENTAL = "(experimental: not confirmed on real buds of this model)"
UNTESTED = "(untested on real hardware, but the payload matches what realme Link sends)"
GOLDEN_SOUND_DEFAULT_ID = 0x0B  # the family's id, used when a profile does not name the feature


class CliError(Exception):
    pass


@contextlib.contextmanager
def open_buds(args: argparse.Namespace) -> Iterator[Buds]:
    """Connect, run the init sequence, and always close the link."""
    trace = None
    if args.trace:
        def trace(direction: str, data: bytes) -> None:
            print(f"{direction} {data.hex(' ').upper()}", file=sys.stderr)

    try:
        buds = connect_buds(
            args.addr, args.channel, args.sim, trace,
            profile=getattr(args, "profile", None),
            profile_file=getattr(args, "profile_file", None),
            sim_profile=getattr(args, "sim_profile", None),
            transport=getattr(args, "transport", "auto"),
            service_uuid=getattr(args, "service_uuid", None),
        )
    except (TransportError, ProfileError) as e:
        raise CliError(str(e)) from e
    except TimeoutError as e:
        raise CliError(f"the buds did not answer ({e}). Are they in the case or connected elsewhere?") from e
    try:
        yield buds
    except TimeoutError as e:
        raise CliError(f"the buds did not answer ({e}). Are they in the case or connected elsewhere?") from e
    finally:
        buds.close()


def on_off(text: str) -> bool:
    t = text.lower()
    if t in ("on", "1", "true", "yes"):
        return True
    if t in ("off", "0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("use on or off")


def mask_names(profile: DeviceProfile, mask: int | None) -> str:
    """Names of the modes in a touch-and-hold cycle mask, from the profile's options."""
    if mask is None:
        return "unknown"
    names = [o.label for o in profile.hold_options if mask & o.bit]
    return ", ".join(names) if names else f"0x{mask:02X}"


def pct(entry: tuple[int, bool] | None) -> str:
    if not entry:
        return "-"
    return f"{entry[0]}%" + (" (charging)" if entry[1] else "")


def row(label: str, value: str) -> str:
    return f"{label + ':':<32}{value}"


def format_status(st: State, profile: DeviceProfile) -> str:
    lines = [row("Model", f"{profile.name}  [{profile.key}]")]
    lines.append(row("Firmware", f"{st.firmware or '?'}    product id {st.product_id or '?'}"))
    b = st.battery
    lines.append(row("Battery", f"left {pct(b.get(1))}   right {pct(b.get(2))}   case {pct(b.get(3))}"))
    w = st.wearing
    if w:
        lines.append(
            row("Wearing", "   ".join(f"{model.COMPONENT_NAMES[c]} {model.wear_label(v)}" for c, v in sorted(w.items()) if c in model.COMPONENT_NAMES))
        )
    if st.anc is not None or profile.has_anc:
        anc = profile.anc_label(st.anc)
        cur = profile.mode_by_report(st.anc) if st.anc is not None else None
        if cur is not None and cur.key == "smart" and st.smart_level:
            level = profile.mode_by_report(st.smart_level)
            if level is not None:
                anc += f" (currently {level.label.lower()})"
        lines.append(row("Noise control", anc))
    if profile.hold_options:
        lines.append(row("Touch-and-hold cycles", mask_names(profile, st.hold_cycle)))
    ordered = [f.id for f in profile.features if f.id in st.features] + sorted(i for i in st.features if not profile.feature(i))
    for fid in ordered:
        lines.append(row(profile.feature_for_id(fid).label, "on" if st.features[fid] else "off"))
    if st.eq is not None:
        lines.append(row("Equaliser", eq_name(st, profile)))
    if st.dynamic_audio:
        lines.append(row("Dynamic audio bands", "  ".join(f"{profile.dynamic_bands.get(x.band, f'band {x.band}')} {x.value:+d}" for x in st.dynamic_audio)))
    if st.hearing_record and st.hearing_record.entries:
        lines.append(row("Golden Sound profile", f"{st.hearing_record.name or 'unnamed'} (id {st.hearing_record.id})"))
    return "\n".join(lines)


def eq_name(st: State, profile: DeviceProfile) -> str:
    preset = profile.eq(st.eq) if st.eq is not None else None
    if preset:
        return preset.label
    for e in st.custom_eqs:
        if e.id == st.eq:
            return f"{e.name} (custom #{e.id})"
    return f"preset {st.eq}"


def format_gestures(st: State, profile: DeviceProfile) -> str:
    inv_side = {v: k for k, v in model.SIDES.items()}
    rows = []
    for e in sorted(st.gestures, key=lambda e: (e.btn == model.BTN_CALL, e.dev, e.act)):
        side = inv_side.get(e.dev, "both" if e.dev == model.SIDE_BOTH else str(e.dev))
        fn = model.GESTURE_FN_BY_CODE.get(e.fn)
        label = fn.label if fn else f"unknown (0x{e.fn:02X})"
        if e.fn == model.FN_ANC_CYCLE:
            label += f" [{mask_names(profile, st.hold_cycle)}]"
        where = " (on call)" if e.btn == model.BTN_CALL else ""
        rows.append(f"{side:<6} {model.ACTION_LABELS.get(e.act, str(e.act)):<15}{where:<11} {label}")
    return "\n".join(rows)


# -- commands ---------------------------------------------------------------


def cmd_devices(args) -> int:
    try:
        devices = list_devices()
    except TransportError as e:
        raise CliError(str(e)) from e
    if not devices:
        print("no known Bluetooth devices")
    for d in devices:
        flags = ("connected" if d.connected else "not connected") + (", realme control service" if d.has_control_service else "")
        print(f"{d.addr}  {d.name}  [{flags}]")
    return 0


def cmd_status(args) -> int:
    with open_buds(args) as buds:
        errors = buds.sync()
        st = buds.state
        if args.json:
            print(json.dumps(state_to_dict(st, buds.active_profile), indent=2))
        else:
            print(format_status(st, buds.active_profile))
            for what, err in errors.items():
                print(f"(could not read {what}: {err})", file=sys.stderr)
    return 0


def state_to_dict(st: State, profile: DeviceProfile) -> dict:
    return {
        "model": profile.name,
        "profile": profile.key,
        "firmware": st.firmware,
        "product_id": st.product_id,
        "battery": {model.COMPONENT_NAMES[k]: {"percent": v[0], "charging": v[1]} for k, v in st.battery.items() if k in model.COMPONENT_NAMES},
        "wearing": {model.COMPONENT_NAMES[k]: model.wear_label(v) for k, v in st.wearing.items() if k in model.COMPONENT_NAMES},
        "noise_control": profile.anc_label(st.anc) if st.anc is not None else None,
        "hold_cycle": [o.key for o in profile.hold_options if st.hold_cycle and st.hold_cycle & o.bit],
        "features": {profile.feature_for_id(i).key: v for i, v in st.features.items()},
        "eq": eq_name(st, profile) if st.eq is not None else None,
        "dynamic_audio": {profile.dynamic_bands.get(b.band, f"band-{b.band}"): b.value for b in st.dynamic_audio},
        "gestures": [{"side": e.dev, "button": e.btn, "action": e.act, "function": e.fn} for e in st.gestures],
    }


def resolve_anc(profile: DeviceProfile, text: str):
    """A mode of this model by key; a group key (e.g. noise cancellation) means its default level."""
    if not profile.has_anc:
        raise CliError(
            f"there is no noise-control table for {profile.name}. Pick a similar model with --profile, "
            "add a profile (docs/ADDING_A_DEVICE.md), or send raw values with: realme-buds raw 0404 <hex>"
        )
    key = text.lower()
    for m in profile.anc_modes:
        if m.key == key:
            return m
    grp = profile.group(key)
    if grp is not None:
        return profile.mode(grp.default)
    names = [m.key for m in profile.anc_modes] + [g.key for g in profile.anc_groups]
    raise CliError(f"{profile.name} has no mode {text!r}; choose from: {', '.join(names)}")


def cmd_anc(args) -> int:
    with open_buds(args) as buds:
        mode = resolve_anc(buds.active_profile, args.mode)
        buds.set_anc_mode(mode)
        print(f"noise control: {buds.active_profile.anc_label(buds.state.anc)}")
    return 0


def parse_feature_ref(profile: DeviceProfile, text: str):
    """A feature by key, or by id (27 or 0x1B) when the model's profile does not name it."""
    f = profile.feature(text)
    if f:
        return f
    try:
        return profile.feature_for_id(int(text, 0))
    except ValueError:
        keys = ", ".join(x.key for x in profile.features) or "none known for this model"
        raise CliError(f"{profile.name} has no feature {text!r} (known: {keys}); an id such as 0x1B also works") from None


def set_switch(args, buds: Buds, feat, state: bool) -> int:
    buds.read_features([feat.id])
    if feat.id not in buds.state.features:
        raise CliError(f"these buds do not report '{feat.label}' (feature 0x{feat.id:02X}); nothing was changed")
    if feat.evidence in ("family", "guess"):
        print(EXPERIMENTAL, file=sys.stderr)
    if feat.key == "spatial":
        buds.set_spatial(state)
    else:
        buds.set_feature(feat.id, state, verify=not feat.drops_link)
    if feat.drops_link:
        print("the buds will reconnect in a few seconds")
    print(f"{feat.label}: {'on' if state else 'off'}")
    return 0


def cmd_feature(args) -> int:
    with open_buds(args) as buds:
        feat = parse_feature_ref(buds.active_profile, args.feature)
        return set_switch(args, buds, feat, args.state)


def cmd_switch(args) -> int:
    with open_buds(args) as buds:
        feat = parse_feature_ref(buds.active_profile, args.feature)
        return set_switch(args, buds, feat, args.state)


def cmd_features(args) -> int:
    with open_buds(args) as buds:
        prof = buds.active_profile
        found = buds.scan_features() if args.scan else buds.read_features()
        for fid, val in sorted(found.items()):
            known = prof.feature(fid)
            name = known.label if known else "(not identified)"
            print(f"0x{fid:02X}  {'on ' if val else 'off'}  {name}")
        if not found:
            print("the buds reported no features")
    return 0


def parse_eq_choice(profile: DeviceProfile, customs, text: str) -> int:
    """A built-in preset by key or label, a custom preset by name, or any number as an id."""
    if text.isdigit():
        return int(text)
    key = text.lower().replace(" ", "-").replace("_", "-")
    preset = profile.eq_by_key(key)
    if preset:
        return preset.id
    for c in customs:
        if c.name.lower() == text.lower():
            return c.id
    known = [p.key for p in profile.eq_presets] + [c.name for c in customs]
    raise CliError(f"unknown EQ preset {text!r}; choose from {', '.join(known) or 'none known'} or an id")


def parse_band(text: str) -> tuple[int, int]:
    try:
        hz, gain = text.split(":")
        return int(hz), int(gain)
    except ValueError as e:
        raise CliError(f"band {text!r} should look like 1000:3 (Hz:dB)") from e


def cmd_eq(args) -> int:
    with open_buds(args) as buds:
        prof = buds.active_profile
        if args.action in (None, "show"):
            buds.read_eq()
            with contextlib.suppress(BudsError, TimeoutError):
                buds.read_custom_eqs()
            print(f"current: {eq_name(buds.state, prof)}")
            if prof.eq_presets:
                print(f"built-in presets for {prof.name}:")
            for pr in prof.eq_presets:
                print(f"  {pr.id}  {pr.label}")
            for e in buds.state.custom_eqs:
                print(f"  {e.id}  {e.name} (custom): " + " ".join(f"{hz}Hz:{g:+d}" for hz, g in e.bands))
        elif args.action == "set":
            if not args.args:
                _need("eq set <preset>")
            customs = []
            with contextlib.suppress(BudsError, TimeoutError):
                customs = buds.read_custom_eqs()
            buds.select_eq(parse_eq_choice(prof, customs, args.args[0]))
            print(f"equaliser: {eq_name(buds.state, prof)}")
        elif args.action == "save":
            print(UNTESTED, file=sys.stderr)
            if len(args.args) < 2:
                _need("eq save <name> <hz:db> ... (gain range from the profile)")
            name, bands = args.args[0], [parse_band(b) for b in args.args[1:]]
            existing = next((e for e in buds.read_custom_eqs() if e.name == name), None)
            try:
                buds.save_custom_eq(existing.id if existing else None, name, bands)
            except ValueError as e:
                raise CliError(str(e)) from e
            print(f"saved custom equaliser {name!r} and selected it")
        elif args.action == "delete":
            print(UNTESTED, file=sys.stderr)
            if not args.args or not args.args[0].isdigit():
                _need("eq delete <id>")
            buds.delete_custom_eq(int(args.args[0]))
            print("deleted")
    return 0


def _need(usage: str):
    raise CliError(f"usage: realme-buds {usage}")


BAND_NAMES = {"low": 1, "bass": 1, "mid": 2, "high": 3, "treble": 3}
BAND_USAGE = "use 'on', 'off', three numbers (low mid high) or band/value pairs like: low +2 high -1 (each -5..5)"


def parse_band_args(values: list[str]) -> dict[int, int]:
    """``2 0 -1`` (low mid high) or ``low +2 high=-1`` -> {band: value}."""
    tokens: list[str] = []
    for v in values:
        for sep in ("=", ":"):
            if sep in v:
                tokens.extend(v.split(sep, 1))
                break
        else:
            tokens.append(v)
    try:
        if all(t.lstrip("+-").isdigit() for t in tokens):
            if len(tokens) != 3:
                raise CliError("give three values (low mid high), or name the bands: low +2 high -1")
            return {band: int(t) for band, t in zip((1, 2, 3), tokens)}
        if len(tokens) % 2:
            raise CliError(BAND_USAGE)
        out: dict[int, int] = {}
        for name, num in zip(tokens[0::2], tokens[1::2]):
            if name.lower() not in BAND_NAMES:
                raise CliError(f"unknown band {name!r}; use low, mid or high")
            out[BAND_NAMES[name.lower()]] = int(num)
        return out
    except ValueError as e:
        raise CliError(BAND_USAGE) from e


def cmd_dynamic_audio(args) -> int:
    values = args.values
    with open_buds(args) as buds:
        prof = buds.active_profile
        label = lambda b: prof.dynamic_bands.get(b.band, f"band {b.band}")  # noqa: E731
        if len(values) == 1 and values[0].lower() in ("on", "off"):
            feat = prof.feature("dynamic-audio")
            if feat is None:
                raise CliError(f"{prof.name} has no 'dynamic-audio' switch in its profile; use: realme-buds switch <id> {values[0].lower()}")
            return set_switch(args, buds, feat, values[0].lower() == "on")
        bands = buds.read_dynamic_audio()
        if not bands:
            raise CliError("these buds do not report dynamic audio bands")
        if values:
            wanted = parse_band_args(values)
            for band, value in wanted.items():
                if next((b.value for b in bands if b.band == band), None) != value:
                    try:
                        bands = buds.set_dynamic_band(band, value)
                    except ValueError as e:
                        raise CliError(str(e)) from e
        print("  ".join(f"{label(b)} {b.value:+d}" for b in bands))
    return 0


def cmd_gestures(args) -> int:
    with open_buds(args) as buds:
        buds.read_gestures()
        buds.read_hold_cycle()
        print(format_gestures(buds.state, buds.active_profile))
    return 0


def cmd_gesture(args) -> int:
    side = model.SIDES[args.side]
    action = model.ACTIONS[args.action]
    fn = model.GESTURE_FN_BY_KEY[args.function]
    with open_buds(args) as buds:
        prof = buds.active_profile
        if not args.force:
            if side == model.SIDE_BOTH and not (prof.hold_both and action == 4):
                raise CliError(f"{prof.name} has no gesture shared by both buds. Use --force to try anyway")
            if prof.hold_both and action == 4 and side != model.SIDE_BOTH:
                raise CliError(f"on {prof.name} touch-and-hold is one gesture for both buds: use side 'both'. Use --force to try anyway")
            if side in (model.SIDE_LEFT, model.SIDE_RIGHT) and side not in prof.gesture_sides:
                raise CliError(f"{prof.name} has no {args.side} bud gesture (it is a single-button model). Use --force to try anyway")
            if action not in prof.gesture_actions:
                have = ", ".join(a for a, n in model.ACTIONS.items() if n in prof.gesture_actions) or "none listed"
                raise CliError(f"{prof.name} does not list a '{args.action}' gesture to remap (it lists: {have}). Use --force to try anyway")
            allowed = prof.options_for(action)
            if allowed and fn.code not in allowed:
                names = ", ".join(model.GESTURE_FN_BY_CODE[c].key for c in allowed if c in model.GESTURE_FN_BY_CODE)
                raise CliError(f"for a {args.action} {prof.name} offers: {names}. Use --force to try '{fn.key}' anyway")
        buds.set_gesture(side, action, fn.code)
        print(f"{args.side} {model.ACTION_LABELS[action].lower()}: {fn.label}")
        if fn.code == model.FN_ANC_CYCLE:
            print(f"cycles through: {mask_names(prof, buds.read_hold_cycle())} (change with 'hold-cycle')")
    return 0


def cmd_hold_cycle(args) -> int:
    with open_buds(args) as buds:
        prof = buds.active_profile
        if not prof.hold_options:
            raise CliError(f"{prof.name} has no touch-and-hold cycle in its profile; pick a profile with --profile")
        choices = {o.key: o.bit for o in prof.hold_options}
        mask = 0
        for part in args.modes.split(","):
            key = part.strip().lower()
            if key not in choices:
                raise CliError(f"unknown mode {part!r}; use a comma list of: {', '.join(choices)}")
            mask |= choices[key]
        if bin(mask).count("1") < prof.hold_min and not args.force:
            raise CliError(f"realme Link needs at least {prof.hold_min} modes in the cycle for {prof.name}. Use --force to try fewer")
        buds.set_hold_cycle(mask)
        print(f"touch-and-hold cycles: {mask_names(prof, mask)}")
    return 0


def cmd_find(args) -> int:
    with open_buds(args) as buds:
        buds.find_buds(args.state)
        print("the buds are ringing (take them out of your ears!)" if args.state else "stopped")
    return 0


def need_capability(args, profile: DeviceProfile, what: str, have: bool) -> None:
    """Refuse a feature the profile does not list, unless the user forces it."""
    if not have and not getattr(args, "force", False):
        raise CliError(f"{profile.name} is not listed as having {what} in its profile. Use --force to try it anyway")


def cmd_fit_test(args) -> int:
    with open_buds(args) as buds:
        need_capability(args, buds.active_profile, "an earbud fit test", buds.active_profile.fit_test)
        print("Put both earbuds in your ears and keep still. The buds play a short sound while they measure.")
        result = buds.run_fit_test(timeout=args.timeout)
        for side, name in ((1, "left"), (2, "right")):
            if side in result:
                print(f"{name} bud: {model.fit_label(result[side])}")
    return 0


def _ask(prompt: str) -> str:
    """The only place the interactive commands read the keyboard (tests replace it)."""
    return input(prompt)


def format_hearing_record(rec) -> str:
    lines = [f"Profile: {rec.name or '(unnamed)'}  (id {rec.id})"]
    by = {(e.side, e.band): e.db for e in rec.entries}
    lines.append("            " + "  ".join(f"band {b}" for b in model.HEARING_BANDS))
    for side, name in ((1, "left ear "), (2, "right ear")):
        lines.append(f"{name}   " + "  ".join(f"{by.get((side, b), 0):>6d}" for b in model.HEARING_BANDS))
    return "\n".join(lines)


def cmd_golden_sound(args) -> int:
    action = args.action or "status"
    with open_buds(args) as buds:
        prof = buds.active_profile
        need_capability(args, prof, "Golden Sound", prof.golden_sound)
        feat = prof.feature("golden-sound")
        fid = feat.id if feat else GOLDEN_SOUND_DEFAULT_ID
        if action in ("on", "off"):
            buds.read_features([fid])
            if fid not in buds.state.features:
                raise CliError(f"these buds do not report Golden Sound (feature 0x{fid:02X}); nothing was changed")
            buds.set_golden_sound(action == "on")
            print(f"Golden Sound: {action}")
        elif action == "status":
            buds.read_features([fid])
            state = buds.state.features.get(fid)
            print("Golden Sound: " + ("not reported by these buds" if state is None else "on" if state else "off"))
            rec = buds.read_hearing_record()
            if rec and rec.entries:
                print(format_hearing_record(rec))
            else:
                print("No hearing profile is stored on the buds. Run: realme-buds golden-sound test")
        else:
            return run_hearing_wizard(buds, args)
    return 0


def run_hearing_wizard(buds, args) -> int:
    print(UNTESTED, file=sys.stderr)
    print(
        "\nGolden Sound hearing test\n"
        "  - Sit somewhere quiet and keep the earbuds in both ears.\n"
        "  - You will hear a tone in one ear at a time, 12 in total (six pitches per ear).\n"
        "  - Make each tone as quiet as you can while still just hearing it.\n"
        "  - It starts quiet. Loud steps ask for confirmation first.\n"
    )
    if not args.yes and _ask("Press Enter to begin, or q to cancel: ").strip().lower() == "q":
        return 1
    uid = random.randint(1, 2**31 - 1)
    scan = None
    if not args.skip_scan:
        print("Measuring your ear canals (about 10 seconds)...")
        scan = buds.ear_scan(uid)
        print("Ear scan done.")
    stops = model.HEARING_STOPS
    results: list[tuple[int, int, int]] = []
    buds.hearing_test_start()
    try:
        for n, (side, band) in enumerate(model.HEARING_NODES, 1):
            idx = stops.index(model.HEARING_START_DB)
            confirmed_loud = False
            buds.play_tone(side, band, stops[idx])
            ear = "left" if side == 1 else "right"
            while True:
                answer = _ask(f"[{n}/12 {ear} ear, pitch {band}/6, {stops[idx]:+d} dB]  u = louder, d = quieter, Enter = barely audible, q = quit: ").strip().lower()
                if answer == "q":
                    print("Cancelled, nothing was saved.")
                    return 1
                if answer == "":
                    results.append((side, band, model.snap_hearing_value(stops[idx])))
                    break
                if answer in ("u", "d"):
                    new = min(idx + 1, len(stops) - 1) if answer == "u" else max(idx - 1, 0)
                    if stops[new] >= model.HEARING_LOUD_WARNING_DB and not confirmed_loud:
                        if _ask("That is loud. Play it anyway? [y/N] ").strip().lower() != "y":
                            continue
                        confirmed_loud = True
                    idx = new
                    buds.play_tone(side, band, stops[idx])
            buds.stop_tone()
    finally:
        try:
            buds.hearing_test_stop()
        except (BudsError, TimeoutError):
            pass
    rec = parsing.HearingRecord(tuple(parsing.HearingEntry(*r) for r in results), uid, "")
    print("\n" + format_hearing_record(rec))
    name = args.name or time.strftime("%Y/%m/%d %H:%M")
    if not args.yes and _ask(f"\nSave this as '{name}' and switch Golden Sound on? [y/N] ").strip().lower() != "y":
        print("Not saved.")
        return 1
    buds.apply_golden_sound(results, name, uid, scan.data if scan else None)
    print(f"Saved '{name}' on the buds and switched Golden Sound on.")
    return 0


def cmd_watch(args) -> int:
    with open_buds(args) as buds:
        buds.sync()
        prof = buds.active_profile
        print(format_status(buds.state, prof))
        print("\nwatching for changes (Ctrl-C to stop)...")

        def on_event(event: str, st: State) -> None:
            if event == "anc":
                print(f"noise control -> {prof.anc_label(st.anc)}")
            elif event == "battery":
                print(f"battery: left {pct(st.battery.get(1))} right {pct(st.battery.get(2))} case {pct(st.battery.get(3))}")
            elif event == "wearing":
                print("wearing: " + ", ".join(f"{model.COMPONENT_NAMES[c]} {model.wear_label(v)}" for c, v in sorted(st.wearing.items()) if c in model.COMPONENT_NAMES))
            elif event == "features":
                print("features: " + ", ".join(f"{prof.feature_for_id(i).label}={'on' if v else 'off'}" for i, v in sorted(st.features.items())))
            elif event == "hold-cycle":
                print(f"hold cycle -> {mask_names(prof, st.hold_cycle)}")
            elif event == "gesture-fired" and st.last_gesture_event is not None:
                print(f"gesture: {st.last_gesture_event.hex(' ')}")
            elif event == "eq":
                print(f"equaliser -> {eq_name(st, prof)}")
            elif event == "disconnected":
                print("link closed (case lid closed, or the buds went to another device)")

        buds.add_listener(on_event)
        try:
            ticks = 0
            while buds.state.connected:
                time.sleep(0.5)
                ticks += 1
                if buds.needs_polling and ticks % 6 == 0:  # these buds do not push noise-control changes
                    buds.poll()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_raw(args) -> int:
    try:
        cmd = int(args.cmd, 16)
        payload = bytes.fromhex(args.payload or "")
    except ValueError as e:
        raise CliError("cmd and payload must be hex, e.g. raw 0106 or raw 010c 0101") from e
    with open_buds(args) as buds:
        try:
            pkt = buds.request(cmd, payload, ok=False, timeout=3.0)
        except TimeoutError:
            print("no reply within 3 s (some writes are applied without an acknowledgement; read the value back to check)")
            return 0
        print(f"reply 0x{pkt.cmd:04X} status={pkt.status}: {pkt.payload.hex(' ')}")
    return 0


def cmd_profile(args) -> int:
    action = args.action or "list"
    if action == "paths":
        print(f"built-in: {profiles.BUILTIN_DIR}")
        for d in profiles.user_profile_dirs():
            print(f"yours:    {d}{'' if d.is_dir() else '  (does not exist yet)'}")
        return 0
    if action == "list":
        errors: list[str] = []
        known = profiles.load_all(errors=errors)
        for p in sorted(known.values(), key=lambda p: p.key):
            ids = ",".join(p.product_ids) or "-"
            print(f"{p.key:<28} {p.name:<28} {p.vendor:<8} family {p.family}  ids {ids}  [{Path(p.source).name if p.source != 'builtin' else 'built-in'}]")
        for e in errors:
            print(f"skipped: {e}", file=sys.stderr)
        return 0
    if action == "show":
        if not args.target:
            _need("profile show <key>")
        try:
            print(json.dumps(profiles.get(args.target).to_dict(), indent=2))
        except ProfileError as e:
            raise CliError(str(e)) from e
        return 0
    if action == "validate":
        if not args.target:
            _need("profile validate <file.json>")
        try:
            p = profiles.load_file(Path(args.target))
        except (ProfileError, OSError) as e:
            raise CliError(str(e)) from e
        print(f"OK: {p.describe()}")
        return 0
    # detect
    from .profiles.draft import build_draft

    with open_buds(args) as buds:
        buds.sync()
        draft = build_draft(buds)
        matched = buds.active_profile
    text = json.dumps(draft, indent=2) + "\n"
    print(f"Recognised as: {matched.describe()}", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Draft written to {args.out}. Edit it, check it with 'realme-buds profile validate {args.out}',")
        print("then copy it into one of the folders shown by 'realme-buds profile paths'.")
    else:
        print(text, end="")
    return 0


# -- parser -----------------------------------------------------------------


def build_parser(known: dict[str, DeviceProfile] | None = None) -> argparse.ArgumentParser:
    """The parser. Names that depend on the model (noise-control modes, gesture functions) are not
    fixed here: they are checked against the connected buds' profile, and errors list what is valid."""
    known = known if known is not None else profiles.load_all()
    ap = argparse.ArgumentParser(
        prog="realme-buds",
        description="Control OPPO/OnePlus/realme-family earbuds from this computer. "
        "Built in: realme Buds Air7 Pro. Other models: see 'realme-buds profile' and docs/ADDING_A_DEVICE.md.",
    )
    ap.add_argument("--addr", help="Bluetooth address of the buds (default: the one connected here)")
    ap.add_argument("--channel", type=int, help="RFCOMM channel (default: look it up over SDP)")
    ap.add_argument("--transport", choices=TRANSPORTS, default="auto",
                    help="how to reach the buds: auto (RFCOMM, then Bluetooth LE), rfcomm, or ble (experimental, untested on real buds)")
    ap.add_argument("--service-uuid", metavar="UUID", help="GATT service of the control channel for --transport ble (default: 0000079a-d102-11e1-9b23-00025b00a5a5)")
    ap.add_argument("--trace", action="store_true", help="print every frame sent and received to stderr")
    ap.add_argument("--profile", metavar="KEY", help="use this device profile instead of recognising the buds (see: profile list)")
    ap.add_argument("--profile-file", metavar="FILE", help="use the profile in this JSON file (to try a draft)")
    ap.add_argument("--sim", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--sim-profile", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list Bluetooth devices BlueZ knows about").set_defaults(func=cmd_devices)
    p = sub.add_parser("status", help="show everything the buds report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("anc", help="noise control mode (names depend on the model, e.g. off, transparency, smart, max)")
    p.add_argument("mode")
    p.set_defaults(func=cmd_anc)

    shortcuts = {}
    for prof in known.values():
        for feat in prof.features:
            shortcuts.setdefault(feat.key, feat)
    for key, feat in shortcuts.items():
        if key in profiles.RESERVED_FEATURE_KEYS:
            continue  # these have richer commands of their own below
        p = sub.add_parser(key, help=f"{feat.label} on/off (if the model has it)")
        p.add_argument("state", type=on_off)
        p.set_defaults(func=cmd_feature, feature=key)

    p = sub.add_parser("switch", help="turn any feature switch on/off by key or id, e.g. 'switch 0x1B on'")
    p.add_argument("feature")
    p.add_argument("state", type=on_off)
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("features", help="list feature switches the buds have")
    p.add_argument("--scan", action="store_true", help="also probe ids with unknown meaning (toggle something in the app, scan again, compare)")
    p.set_defaults(func=cmd_features)

    p = sub.add_parser("eq", help="equaliser: show | set <preset> | save <name> <hz:db>... | delete <id>")
    p.add_argument("action", nargs="?", choices=["show", "set", "save", "delete"])
    p.add_argument("args", nargs="*")
    p.set_defaults(func=cmd_eq)

    p = sub.add_parser("dynamic-audio", help="no argument: show the bands. on|off: the engine. Numbers: 'dynamic-audio 2 0 -1' sets low, mid, high; 'dynamic-audio low +2' sets only the bands named")
    p.description = "Show or change the Dynamic audio bands."
    p.epilog = (
        "examples:\n"
        "  realme-buds dynamic-audio                  show the three bands\n"
        "  realme-buds dynamic-audio 2 0 -1           low=+2 mid=0 high=-1\n"
        "  realme-buds dynamic-audio low +2           only change low\n"
        "  realme-buds dynamic-audio high -1 mid 3    several named bands\n"
        "  realme-buds dynamic-audio off              switch the engine off (or: on)"
    )
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    p.add_argument("values", nargs="*", metavar="value", help="on | off | three numbers | band/number pairs (bands: low, mid, high)")
    p.set_defaults(func=cmd_dynamic_audio)

    sub.add_parser("gestures", help="show touch controls").set_defaults(func=cmd_gestures)
    p = sub.add_parser("gesture", help="remap one touch control; which gestures and functions are allowed depends on the model")
    p.add_argument("side", choices=list(model.SIDES))
    p.add_argument("action", choices=list(model.ACTIONS))
    p.add_argument("function", choices=[g.key for g in model.GESTURE_FNS])
    p.add_argument("--force", action="store_true", help="send a gesture or function the model's profile does not offer")
    p.set_defaults(func=cmd_gesture)

    p = sub.add_parser("hold-cycle", help="which noise modes touch-and-hold cycles, e.g. off,transparency,anc (names from the model's profile)")
    p.add_argument("modes")
    p.add_argument("--force", action="store_true", help="allow fewer modes than the profile's minimum")
    p.set_defaults(func=cmd_hold_cycle)

    p = sub.add_parser("find", help="make the buds ring")
    p.add_argument("state", type=on_off)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("fit-test", help="check how well the earbuds seal in your ears (both in, keep still)")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the result")
    p.add_argument("--force", action="store_true", help="run it even if the model's profile does not list a fit test")
    p.set_defaults(func=cmd_fit_test)

    p = sub.add_parser("golden-sound", help="hearing profile: status (default) | on | off | test")
    p.add_argument("action", nargs="?", choices=["status", "on", "off", "test"])
    p.add_argument("--name", help="name for the profile made by 'test' (default: the date)")
    p.add_argument("--skip-scan", action="store_true", help="test without the ear-canal scan (not what the app does)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation before starting and saving")
    p.add_argument("--force", action="store_true", help="run it even if the model's profile does not list Golden Sound")
    p.set_defaults(func=cmd_golden_sound)

    sub.add_parser("watch", help="print changes as they happen (also from the buds' own touch controls)").set_defaults(func=cmd_watch)

    p = sub.add_parser("raw", help="send a raw command and print the reply (for exploring)")
    p.add_argument("cmd", help="command id in hex, e.g. 0106")
    p.add_argument("payload", nargs="?", help="payload in hex")
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("profile", help="device profiles: list | show <key> | validate <file> | detect [--out FILE] | paths")
    p.add_argument("action", nargs="?", choices=["list", "show", "validate", "detect", "paths"])
    p.add_argument("target", nargs="?", help="a profile key (show) or a JSON file (validate)")
    p.add_argument("--out", help="with detect: write the draft profile to this file")
    p.set_defaults(func=cmd_profile)
    return ap


def _preload_profile_file(argv: list[str] | None) -> dict[str, DeviceProfile]:
    """``--profile-file`` can add feature names the parser should know, so read it before parsing."""
    known = profiles.load_all()
    args = list(argv if argv is not None else sys.argv[1:])
    for i, a in enumerate(args):
        path = args[i + 1] if a == "--profile-file" and i + 1 < len(args) else a.split("=", 1)[1] if a.startswith("--profile-file=") else None
        if path:
            try:
                extra = profiles.load_file(Path(path))
                known[extra.key] = extra
            except (ProfileError, OSError):
                pass  # reported properly when the connection is opened
    return known


def main(argv: list[str] | None = None) -> int:
    args = build_parser(_preload_profile_file(argv)).parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except BudsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
