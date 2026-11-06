"""Command line interface: ``realme-buds <command>``."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from typing import Iterator

from . import model, protocol
from .client import Buds, BudsError, State
from .sim import connected_pair
from .transport import TransportError, connect_rfcomm, list_devices, pick_device

EXPERIMENTAL = "(experimental: not yet confirmed on real Air7 Pro hardware)"


class CliError(Exception):
    pass


@contextlib.contextmanager
def open_buds(args: argparse.Namespace) -> Iterator[Buds]:
    """Connect, run the init sequence, and always close the link."""
    trace = None
    if args.trace:
        def trace(direction: str, data: bytes) -> None:
            print(f"{direction} {data.hex(' ').upper()}", file=sys.stderr)

    if args.sim:
        sock, _sim = connected_pair()
    else:
        try:
            dev = pick_device(args.addr)
            sock = connect_rfcomm(dev.addr, args.channel)
        except TransportError as e:
            raise CliError(str(e)) from e
    buds = Buds(sock, trace=trace)
    buds.start()
    try:
        buds.initialise()
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


def mask_names(mask: int | None) -> str:
    if mask is None:
        return "unknown"
    names = [m.label for m in model.ANC_MODES if mask & m.bit]
    return ", ".join(names) if names else f"0x{mask:02X}"


def pct(entry: tuple[int, bool] | None) -> str:
    if not entry:
        return "-"
    return f"{entry[0]}%" + (" (charging)" if entry[1] else "")


def row(label: str, value: str) -> str:
    return f"{label + ':':<32}{value}"


def format_status(st: State) -> str:
    lines = [row("Firmware", f"{st.firmware or '?'}    product id {st.product_id or '?'}")]
    b = st.battery
    lines.append(row("Battery", f"left {pct(b.get(1))}   right {pct(b.get(2))}   case {pct(b.get(3))}"))
    w = st.wearing
    if w:
        lines.append(
            row("Wearing", "   ".join(f"{model.COMPONENT_NAMES[c]} {model.wear_label(v)}" for c, v in sorted(w.items()) if c in model.COMPONENT_NAMES))
        )
    anc = model.anc_label(st.anc) if st.anc is not None else "unknown"
    if st.anc == model.ANC_SMART and st.smart_level:
        anc += f" (currently {model.anc_label(st.smart_level).split(': ')[-1].lower()})"
    lines.append(row("Noise control", anc))
    lines.append(row("Touch-and-hold cycles", mask_names(st.hold_cycle)))
    for f in model.FEATURES:
        if f.id in st.features:
            lines.append(row(f.label, "on" if st.features[f.id] else "off"))
    if st.eq is not None:
        lines.append(row("Equaliser", eq_name(st)))
    if st.dynamic_audio:
        lines.append(
            row("Dynamic audio bands", "  ".join(f"{model.DYNAMIC_BANDS.get(x.band, x.band)} {x.value:+d}" for x in st.dynamic_audio))
        )
    return "\n".join(lines)


def eq_name(st: State) -> str:
    if st.eq in model.EQ_BY_ID:
        return model.EQ_BY_ID[st.eq].label
    for e in st.custom_eqs:
        if e.id == st.eq:
            return f"{e.name} (custom #{e.id})"
    return f"preset {st.eq}"


def format_gestures(st: State) -> str:
    inv_side = {v: k for k, v in model.SIDES.items()}
    rows = []
    for e in sorted(st.gestures, key=lambda e: (e.btn == model.BTN_CALL, e.dev, e.act)):
        side = inv_side.get(e.dev, "both" if e.dev == model.SIDE_BOTH else str(e.dev))
        fn = model.GESTURE_FN_BY_CODE.get(e.fn)
        label = fn.label if fn else f"unknown (0x{e.fn:02X})"
        if e.fn == model.FN_ANC_CYCLE:
            label += f" [{mask_names(st.hold_cycle)}]"
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
            print(json.dumps(state_to_dict(st), indent=2))
        else:
            print(format_status(st))
            for what, err in errors.items():
                print(f"(could not read {what}: {err})", file=sys.stderr)
    return 0


def state_to_dict(st: State) -> dict:
    return {
        "firmware": st.firmware,
        "product_id": st.product_id,
        "battery": {model.COMPONENT_NAMES[k]: {"percent": v[0], "charging": v[1]} for k, v in st.battery.items() if k in model.COMPONENT_NAMES},
        "wearing": {model.COMPONENT_NAMES[k]: model.wear_label(v) for k, v in st.wearing.items() if k in model.COMPONENT_NAMES},
        "noise_control": model.anc_label(st.anc) if st.anc is not None else None,
        "hold_cycle": [m.key for m in model.ANC_MODES if st.hold_cycle and st.hold_cycle & m.bit],
        "features": {(model.FEATURE_BY_ID[i].key if i in model.FEATURE_BY_ID else f"0x{i:02x}"): v for i, v in st.features.items()},
        "eq": eq_name(st) if st.eq is not None else None,
        "dynamic_audio": {model.DYNAMIC_BANDS.get(b.band, str(b.band)): b.value for b in st.dynamic_audio},
        "gestures": [
            {"side": e.dev, "button": e.btn, "action": e.act, "function": e.fn} for e in st.gestures
        ],
    }


def cmd_anc(args) -> int:
    mode = model.ANC_BY_KEY[args.mode]
    with open_buds(args) as buds:
        buds.set_anc(mode.bit)
        print(f"noise control: {mode.label}")
    return 0


def cmd_feature(args) -> int:
    feat = model.FEATURE_BY_KEY[args.feature]
    with open_buds(args) as buds:
        buds.read_features([feat.id])
        if feat.id not in buds.state.features:
            raise CliError(f"these buds do not report '{feat.label}' (feature 0x{feat.id:02X}); nothing was changed")
        if feat.evidence == "family":
            print(EXPERIMENTAL, file=sys.stderr)
        if feat.key == "spatial":
            buds.set_spatial(args.state)
        else:
            buds.set_feature(feat.id, args.state, verify=not feat.drops_link)
        if feat.drops_link:
            print("the buds will reconnect in a few seconds")
        print(f"{feat.label}: {'on' if args.state else 'off'}")
        if args.state and feat.key == "spatial" and buds.state.features.get(0x18):
            print("note: high-definition (LHDC) sound is also on; the official app treats the two as exclusive")
    return 0


def cmd_features(args) -> int:
    with open_buds(args) as buds:
        found = buds.scan_features() if args.scan else buds.read_features()
        for fid, val in sorted(found.items()):
            known = model.FEATURE_BY_ID.get(fid)
            name = known.label if known else "(not identified)"
            print(f"0x{fid:02X}  {'on ' if val else 'off'}  {name}")
        if not found:
            print("the buds reported no features")
    return 0


def parse_eq_choice(text: str) -> int:
    if text.isdigit():
        return int(text)
    key = text.lower().replace(" ", "-").replace("_", "-")
    if key in model.EQ_BY_KEY:
        return model.EQ_BY_KEY[key].id
    raise CliError(f"unknown EQ preset {text!r}; choose from {', '.join(model.EQ_BY_KEY)} or an id")


def parse_band(text: str) -> tuple[int, int]:
    try:
        hz, gain = text.split(":")
        return int(hz), int(gain)
    except ValueError as e:
        raise CliError(f"band {text!r} should look like 1000:3 (Hz:dB)") from e


def cmd_eq(args) -> int:
    with open_buds(args) as buds:
        if args.action in (None, "show"):
            buds.read_eq()
            with contextlib.suppress(BudsError, TimeoutError):
                buds.read_custom_eqs()
            print(f"current: {eq_name(buds.state)}")
            print("built-in presets (names from realme Link; ids unconfirmed on Air7 Pro):")
            for pr in model.EQ_PRESETS:
                print(f"  {pr.id}  {pr.label}")
            for e in buds.state.custom_eqs:
                print(f"  {e.id}  {e.name} (custom): " + " ".join(f"{hz}Hz:{g:+d}" for hz, g in e.bands))
        elif args.action == "set":
            pid = parse_eq_choice(args.args[0]) if args.args else _need("eq set <preset>")
            buds.set_eq_preset(pid)
            print(f"equaliser: {eq_name(buds.state)}")
        elif args.action == "save":
            print(EXPERIMENTAL, file=sys.stderr)
            if len(args.args) < 2:
                _need("eq save <name> <hz:db> ... (6 bands, gain -6..6)")
            name, bands = args.args[0], [parse_band(b) for b in args.args[1:]]
            existing = next((e for e in buds.read_custom_eqs() if e.name == name), None)
            buds.save_custom_eq(existing.id if existing else None, name, bands)
            print(f"saved custom equaliser {name!r} and selected it")
        elif args.action == "delete":
            print(EXPERIMENTAL, file=sys.stderr)
            if not args.args or not args.args[0].isdigit():
                _need("eq delete <id>")
            buds.delete_custom_eq(int(args.args[0]))
            print("deleted")
    return 0


def _need(usage: str):
    raise CliError(f"usage: realme-buds {usage}")


def cmd_dynamic_audio(args) -> int:
    values = args.values
    with open_buds(args) as buds:
        if len(values) == 1 and values[0].lower() in ("on", "off"):
            print(EXPERIMENTAL, file=sys.stderr)
            buds.read_features([0x1D])
            if 0x1D not in buds.state.features:
                raise CliError("these buds do not report the dynamic audio switch (0x1D); nothing was changed")
            buds.set_feature(0x1D, values[0].lower() == "on")
            print(f"Dynamic audio: {values[0].lower()}")
            return 0
        bands = buds.read_dynamic_audio()
        if not bands:
            raise CliError("these buds do not report dynamic audio bands")
        if values:
            print(EXPERIMENTAL, file=sys.stderr)
            try:
                wanted = [int(v) for v in values]
            except ValueError as e:
                raise CliError("use 'on', 'off' or three numbers: low mid high (each -5..5)") from e
            if len(wanted) != 3:
                raise CliError("give three values: low mid high (each -5..5)")
            for band, value in zip((1, 2, 3), wanted):
                if next((b.value for b in bands if b.band == band), None) != value:
                    try:
                        bands = buds.set_dynamic_band(band, value)
                    except ValueError as e:
                        raise CliError(str(e)) from e
        print("  ".join(f"{model.DYNAMIC_BANDS.get(b.band, b.band)} {b.value:+d}" for b in bands))
    return 0


def cmd_gestures(args) -> int:
    with open_buds(args) as buds:
        buds.read_gestures()
        buds.read_hold_cycle()
        print(format_gestures(buds.state))
    return 0


def cmd_gesture(args) -> int:
    side = model.SIDES[args.side]
    action = model.ACTIONS[args.action]
    fn = model.GESTURE_FN_BY_KEY[args.function]
    with open_buds(args) as buds:
        if fn.evidence != "reported":
            print(f"note: '{fn.label}' is {fn.evidence}-level knowledge; the buds will refuse it if unsupported", file=sys.stderr)
        buds.set_gesture(side, action, fn.code)
        print(f"{args.side} {model.ACTION_LABELS[action].lower()}: {fn.label}")
        if fn.code == model.FN_ANC_CYCLE:
            print(f"cycles through: {mask_names(buds.read_hold_cycle())} (change with 'hold-cycle')")
    return 0


HOLD_CYCLE_CHOICES = {"off": model.ANC_OFF, "transparency": model.ANC_TRANSPARENCY, "anc": model.ANC_MAX, "max": model.ANC_MAX}


def cmd_hold_cycle(args) -> int:
    mask = 0
    for part in args.modes.split(","):
        key = part.strip().lower()
        if key not in HOLD_CYCLE_CHOICES:
            raise CliError(f"unknown mode {part!r}; use a comma list of off, transparency, anc")
        mask |= HOLD_CYCLE_CHOICES[key]
    with open_buds(args) as buds:
        buds.set_hold_cycle(mask)
        print(f"touch-and-hold cycles: {mask_names(mask)}")
    return 0


def cmd_find(args) -> int:
    with open_buds(args) as buds:
        buds.find_buds(args.state)
        print("the buds are ringing (take them out of your ears!)" if args.state else "stopped")
    return 0


def cmd_watch(args) -> int:
    with open_buds(args) as buds:
        buds.sync()
        print(format_status(buds.state))
        print("\nwatching for changes (Ctrl-C to stop)...")

        def on_event(event: str, st: State) -> None:
            if event == "anc":
                print(f"noise control -> {model.anc_label(st.anc) if st.anc is not None else '?'}")
            elif event == "battery":
                print(f"battery: left {pct(st.battery.get(1))} right {pct(st.battery.get(2))} case {pct(st.battery.get(3))}")
            elif event == "wearing":
                print("wearing: " + ", ".join(f"{model.COMPONENT_NAMES[c]} {model.wear_label(v)}" for c, v in sorted(st.wearing.items()) if c in model.COMPONENT_NAMES))
            elif event == "features":
                print("features: " + ", ".join(f"0x{i:02X}={'on' if v else 'off'}" for i, v in sorted(st.features.items())))
            elif event == "hold-cycle":
                print(f"hold cycle -> {mask_names(st.hold_cycle)}")
            elif event == "gesture-fired" and st.last_gesture_event is not None:
                print(f"gesture: {st.last_gesture_event.hex(' ')}")
            elif event == "eq":
                print(f"equaliser -> {eq_name(st)}")
            elif event == "disconnected":
                print("link closed (case lid closed, or the buds went to another device)")

        buds.add_listener(on_event)
        try:
            while buds.state.connected:
                time.sleep(0.5)
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
        pkt = buds.request(cmd, payload, ok=False)
        print(f"reply 0x{pkt.cmd:04X} status={pkt.status}: {pkt.payload.hex(' ')}")
    return 0


# -- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="realme-buds", description="Control realme Buds Air7 Pro from this computer.")
    ap.add_argument("--addr", help="Bluetooth address of the buds (default: the one connected here)")
    ap.add_argument("--channel", type=int, help="RFCOMM channel (default: look it up over SDP)")
    ap.add_argument("--trace", action="store_true", help="print every frame sent and received to stderr")
    ap.add_argument("--sim", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list Bluetooth devices BlueZ knows about").set_defaults(func=cmd_devices)
    p = sub.add_parser("status", help="show everything the buds report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("anc", help="noise control mode")
    p.add_argument("mode", choices=[m.key for m in model.ANC_MODES])
    p.set_defaults(func=cmd_anc)

    for feat in model.FEATURES:
        if feat.key == "dynamic-audio":
            continue  # shares its name with the band command below
        p = sub.add_parser(feat.key, help=f"{feat.label} on/off" + ("" if feat.evidence == "reported" else " (experimental)"))
        p.add_argument("state", type=on_off)
        p.set_defaults(func=cmd_feature, feature=feat.key)

    p = sub.add_parser("features", help="list feature switches the buds have")
    p.add_argument("--scan", action="store_true", help="also probe ids with unknown meaning (toggle something in the app, scan again, compare)")
    p.set_defaults(func=cmd_features)

    p = sub.add_parser("eq", help="equaliser: show | set <preset> | save <name> <hz:db>... | delete <id>")
    p.add_argument("action", nargs="?", choices=["show", "set", "save", "delete"])
    p.add_argument("args", nargs="*")
    p.set_defaults(func=cmd_eq)

    p = sub.add_parser("dynamic-audio", help="dynamic-audio [on|off | LOW MID HIGH (-5..5)]; no argument shows the bands")
    p.add_argument("values", nargs="*")
    p.set_defaults(func=cmd_dynamic_audio)

    sub.add_parser("gestures", help="show touch controls").set_defaults(func=cmd_gestures)
    p = sub.add_parser("gesture", help="remap one touch control")
    p.add_argument("side", choices=list(model.SIDES))
    p.add_argument("action", choices=[a for a in model.ACTIONS if model.ACTIONS[a] in model.REMAPPABLE_ACTIONS])
    p.add_argument("function", choices=[g.key for g in model.GESTURE_FNS])
    p.set_defaults(func=cmd_gesture)

    p = sub.add_parser("hold-cycle", help="which noise modes touch-and-hold cycles, e.g. off,transparency,anc")
    p.add_argument("modes")
    p.set_defaults(func=cmd_hold_cycle)

    p = sub.add_parser("find", help="make the buds ring")
    p.add_argument("state", type=on_off)
    p.set_defaults(func=cmd_find)

    sub.add_parser("watch", help="print changes as they happen (also from the buds' own touch controls)").set_defaults(func=cmd_watch)

    p = sub.add_parser("raw", help="send a raw command and print the reply (for exploring)")
    p.add_argument("cmd", help="command id in hex, e.g. 0106")
    p.add_argument("payload", nargs="?", help="payload in hex")
    p.set_defaults(func=cmd_raw)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
