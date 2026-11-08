"""High-level, thread-safe client for one pair of buds over an open RFCOMM socket."""

from __future__ import annotations

import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import model, parsing, profiles, protocol
from .parsing import GestureEntry
from .protocol import Packet


class BudsError(Exception):
    pass


class LinkClosed(BudsError):
    pass


class Nack(BudsError):
    def __init__(self, cmd: int, status: int):
        self.cmd = cmd
        self.status = status
        hint = " (put a bud in your ear first)" if status == protocol.STATUS_NOT_WORN else ""
        super().__init__(f"buds refused command 0x{cmd:04X} with status {status}{hint}")


class FeatureRefused(Nack):
    """The buds refused a feature switch. The message names the switch and the likely reasons."""

    def __init__(self, cmd: int, status: int, feature: "profiles.Feature", on: bool, reasons: list[str]):
        self.cmd, self.status, self.feature, self.on, self.reasons = cmd, status, feature, on, reasons
        text = f"The buds refused to turn {feature.label} {'on' if on else 'off'} (status {status})."
        if reasons:
            text += " Possible reasons: " + "; ".join(reasons) + "."
        Exception.__init__(self, text)


class FeatureConflict(FeatureRefused):
    """A switch was refused while another one it cannot be combined with is on."""

    def __init__(self, cmd: int, status: int, feature: "profiles.Feature", on: bool, conflicts: "list[profiles.Feature]", reasons: list[str]):
        super().__init__(cmd, status, feature, on, reasons)
        self.conflicts = conflicts
        names = " and ".join(c.label for c in conflicts)
        Exception.__init__(
            self,
            f"The buds refused to turn {feature.label} on while {names} {'is' if len(conflicts) == 1 else 'are'} on: "
            "on this model they cannot be on together. Turn "
            f"{'it' if len(conflicts) == 1 else 'them'} off first (the vendor app asks and then does exactly that).",
        )


class VerifyFailed(BudsError):
    pass


@dataclass
class State:
    connected: bool = False
    product_id: str | None = None
    firmware: str | None = None
    capabilities: set[int] = field(default_factory=set)
    broadcast_codes: list[int] = field(default_factory=list)
    battery: dict[int, tuple[int, bool]] = field(default_factory=dict)
    wearing: dict[int, int] = field(default_factory=dict)
    anc: int | None = None
    smart_level: int | None = None
    hold_cycle: int | None = None
    features: dict[int, bool] = field(default_factory=dict)
    gestures: list[GestureEntry] = field(default_factory=list)
    eq: int | None = None
    custom_eqs: list[parsing.CustomEq] = field(default_factory=list)
    dynamic_audio: list[parsing.DynamicBand] = field(default_factory=list)
    last_gesture_event: bytes | None = None
    fit_test: dict[int, int] | None = None  # side -> raw status of the latest result push
    fit_test_seq: int = 0
    hearing_record: parsing.HearingRecord | None = None
    ear_scan: parsing.EarScan | None = None
    ear_scan_seq: int = 0
    hearing_status: tuple[int, int] | None = None  # (kind, status) of the last 0x08 push


Listener = Callable[[str, State], None]
Trace = Callable[[str, bytes], None]


class Buds:
    """Talks the protocol over ``sock``. Call :meth:`start` once, then use the methods.

    A reader thread decodes everything the buds send. Replies are matched to their
    requests by seq, pushes update :attr:`state` and notify listeners, and requests
    the buds make of us (they ask for the time on connect) are answered right there.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        trace: Trace | None = None,
        request_timeout: float = 3.0,
        init_delay: float = 0.2,
        profile: profiles.DeviceProfile | None = None,
        device_name: str | None = None,
    ) -> None:
        self.sock = sock
        self.state = State()
        # The model-specific data. Given explicitly (an override), else chosen after the
        # handshake from the product id and Bluetooth name. See realmebuds/profiles.
        self.profile: profiles.DeviceProfile | None = profile
        self.profile_forced = profile is not None
        self.device_name = device_name
        self.request_timeout = request_timeout
        self.init_delay = init_delay
        self._trace = trace
        self._listeners: list[Listener] = []
        self._framer = protocol.Framer()
        self._pending: dict[int, queue.Queue[Packet | None]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._seq = 0
        self._closing = False
        self._reader: threading.Thread | None = None
        self.last_write_acked = True
        self._cond = threading.Condition()

    # -- lifecycle --------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def start(self) -> None:
        self.sock.settimeout(0.5)
        self.state.connected = True
        self._reader = threading.Thread(target=self._read_loop, name="buds-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closing = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)

    def __enter__(self) -> "Buds":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- plumbing ---------------------------------------------------------

    def _emit(self, event: str) -> None:
        with self._cond:
            self._cond.notify_all()
        for fn in list(self._listeners):
            try:
                fn(event, self.state)
            except Exception:  # a broken listener must not kill the reader
                pass

    def _send_raw(self, cmd: int, seq: int, payload: bytes) -> None:
        frame = protocol.build_frame(cmd, seq, payload)
        if self._trace:
            self._trace("TX", frame)
        with self._send_lock:
            try:
                self.sock.sendall(frame)
            except OSError as e:
                raise LinkClosed(f"send failed: {e}") from e

    def _next_seq(self) -> int:
        with self._pending_lock:
            for _ in range(0xFE):
                self._seq = self._seq % 0xFE + 1
                if self._seq not in self._pending:
                    return self._seq
        raise BudsError("too many requests in flight")

    def request(self, cmd: int, payload: bytes = b"", *, timeout: float | None = None, ok: bool = True) -> Packet:
        """Send a command and wait for its reply. Raises Nack if the status is non-zero."""
        if not self.state.connected:
            raise LinkClosed("not connected")
        seq = self._next_seq()
        box: queue.Queue[Packet | None] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[seq] = box
        try:
            self._send_raw(cmd, seq, payload)
            try:
                pkt = box.get(timeout=timeout if timeout is not None else self.request_timeout)
            except queue.Empty:
                raise TimeoutError(f"no reply to 0x{cmd:04X}") from None
            if pkt is None:
                raise LinkClosed("link closed while waiting for a reply")
            if ok and pkt.status not in (None, protocol.STATUS_OK):
                raise Nack(cmd, pkt.status)
            return pkt
        finally:
            with self._pending_lock:
                self._pending.pop(seq, None)

    def _write(self, cmd: int, payload: bytes = b"", *, timeout: float = 1.5) -> bool:
        """Send a write and wait briefly for its ack. Returns whether one arrived.

        Some firmware applies a write without acknowledging it (the Air7 Pro does this for
        the subscription and, it seems, Dynamic audio), so a missing ack is not an error.
        A refusal (non-zero status) still raises. Callers confirm by reading the value back.
        """
        try:
            self.request(cmd, payload, timeout=timeout)
        except TimeoutError:
            self.last_write_acked = False
            return False
        self.last_write_acked = True
        return True

    def _verify_failed(self, message: str) -> VerifyFailed:
        hint = "" if self.last_write_acked else " The buds did not acknowledge the write either, so they may expect a different payload."
        return VerifyFailed(message + hint)

    def _read_loop(self) -> None:
        while not self._closing:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            if self._trace:
                self._trace("RX", data)
            for pkt in self._framer.feed(data):
                try:
                    self._dispatch(pkt)
                except LinkClosed:
                    break
        self._on_closed()

    def _on_closed(self) -> None:
        was = self.state.connected
        self.state.connected = False
        with self._pending_lock:
            for box in self._pending.values():
                try:
                    box.put_nowait(None)
                except queue.Full:
                    pass
        if was:
            self._emit("disconnected")

    def _dispatch(self, pkt: Packet) -> None:
        if pkt.is_response:
            with self._pending_lock:
                box = self._pending.get(pkt.seq)
            if box is not None:
                try:
                    box.put_nowait(pkt)
                except queue.Full:
                    pass
            return
        answer = protocol.answer_for(pkt)
        if answer is not None:
            self._send_raw(answer[0], pkt.seq, answer[1])
        if pkt.cmd == protocol.CMD_PUSH:
            self._handle_push(pkt.payload)
        elif pkt.cmd == 0x0504 and pkt.payload:
            with self._state_lock:
                self.state.eq = pkt.payload[0]
            self._emit("eq")

    def _handle_push(self, p: bytes) -> None:
        if not p:
            return
        code, body = p[0], p[1:]
        st = self.state
        with self._state_lock:
            if code == protocol.EV_BATTERY:
                for comp, val in parsing.parse_battery(body).items():
                    st.battery[comp] = val
                event = "battery"
            elif code == protocol.EV_WEARING:
                st.wearing.update(parsing.parse_wearing(body))
                event = "wearing"
            elif code == protocol.EV_ANC:
                event = self._handle_anc_push(body)
            elif code == protocol.EV_GAME_MODE:
                # `05 <state>`, or `05 xx xx <state>` on some models: the state is the last byte.
                if body:
                    st.features[0x06] = bool(body[-1])
                event = "features"
            elif code == protocol.EV_GESTURE:
                st.last_gesture_event = bytes(body)
                event = "gesture-fired"
            elif code == protocol.EV_FIT_TEST:
                st.fit_test = parsing.parse_fit_test(body)
                st.fit_test_seq += 1
                event = "fit-test"
            elif code == protocol.EV_EAR_SCAN:
                scan = parsing.parse_ear_scan_event(body)
                if scan is None:
                    return
                st.ear_scan = scan
                st.ear_scan_seq += 1
                event = "ear-scan"
            elif code == protocol.EV_HEARING_STATUS and len(body) >= 2:
                st.hearing_status = (body[0], body[1])
                event = "hearing-status"
            else:
                return
        self._emit(event)

    def _handle_anc_push(self, body: bytes) -> str:
        st = self.state
        if body[:2] == b"\x01\x01":
            st.anc = parsing.parse_value_le(body[2:])
            return "anc"
        if body[:2] == b"\x02\x01":
            st.hold_cycle = parsing.parse_value_le(body[2:])
            return "hold-cycle"
        if body[:2] == b"\x04\x01":
            st.smart_level = parsing.parse_value_le(body[2:])
            return "anc"
        return "anc"

    # -- connect-time sequence -------------------------------------------

    def initialise(self) -> None:
        """Handshake, identify, and subscribe to pushes. Call once after start()."""
        time.sleep(self.init_delay * 1.5)
        pkt = self.request(protocol.CMD_HANDSHAKE)
        self.state.capabilities = parsing.parse_bitmap(pkt.payload)
        time.sleep(self.init_delay)
        pkt = self.request(protocol.CMD_PRODUCT_ID, ok=False)
        self.state.product_id = parsing.parse_product_id(pkt.payload)
        if self.profile is None:
            self.profile = profiles.resolve(self.state.product_id, self.device_name)
        self._emit("profile")
        time.sleep(self.init_delay)
        codes: list[int] = []
        try:
            codes = parsing.parse_broadcast_codes(self.request(protocol.CMD_BROADCAST_CODES).payload)
        except (TimeoutError, Nack):
            pass
        self.state.broadcast_codes = codes
        wanted = [c for c in (protocol.EV_BATTERY, protocol.EV_WEARING, protocol.EV_ANC, protocol.EV_GAME_MODE) if c in codes]
        if wanted:
            time.sleep(self.init_delay)
            # Payload is `<count> <ids>`. Never "01 <id> ...": that subscribes to one id only.
            # The Air7 Pro never acknowledges this command, so a missing reply is normal.
            try:
                self.request(protocol.CMD_SUBSCRIBE, bytes([len(wanted), *wanted]), ok=False, timeout=0.7)
            except TimeoutError:
                pass

    @property
    def active_profile(self) -> profiles.DeviceProfile:
        """The profile in use; the generic one until the product id has been read."""
        return self.profile or profiles.generic_profile(self.state.product_id, self.device_name)

    def supports(self, cmd: int) -> bool:
        """Whether the buds' capability bitmap enables ``cmd``. Unknown commands and a
        missing bitmap count as supported, so this can only ever skip a request."""
        bit = protocol.CAPABILITY_BIT.get(cmd)
        return bit is None or not self.state.capabilities or bit in self.state.capabilities

    @property
    def needs_polling(self) -> bool:
        """True when the buds do not push noise-control changes, so a front end must ask."""
        return protocol.EV_ANC not in self.state.broadcast_codes

    def poll(self) -> None:
        """Re-read what the buds would have pushed had they offered to. Cheap, two requests."""
        self.read_anc()
        self.read_features(sorted(self.state.features) or None)

    def sync(self) -> dict[str, str]:
        """Read everything the UI shows. Returns {what: error} for reads that failed."""
        errors: dict[str, str] = {}
        steps: list[tuple[str, Callable[[], object]]] = [
            ("firmware", self.read_firmware),
            ("battery", self.read_battery),
            ("wearing", self.read_wearing),
            ("anc", self.read_anc),
            ("features", self.read_features),
            ("gestures", self.read_gestures),
            ("hold-cycle", self.read_hold_cycle),
            ("eq", self.read_eq),
            ("custom-eq", self.read_custom_eqs),
            ("dynamic-audio", self.read_dynamic_audio),
            ("hearing-record", self.read_hearing_record),
        ]
        optional = {
            "custom-eq": self.supports(protocol.CMD_CUSTOM_EQ_LIST),
            "dynamic-audio": self.supports(protocol.CMD_DYNAMIC_AUDIO_BANDS) or self.supports(protocol.CMD_DYNAMIC_AUDIO_SINGLE),
            "hearing-record": self.supports(protocol.CMD_HEARING_RECORD),
        }
        for name, fn in steps:
            if not optional.get(name, True):
                continue
            time.sleep(self.init_delay)
            try:
                fn()
            except LinkClosed:
                raise
            except (BudsError, TimeoutError) as e:
                errors[name] = str(e)
        self._emit("synced")
        return errors

    # -- reads ------------------------------------------------------------

    def read_firmware(self) -> str:
        v = parsing.parse_firmware(self.request(protocol.CMD_FIRMWARE).payload)
        self.state.firmware = v
        return v

    def read_battery(self) -> dict[int, tuple[int, bool]]:
        bat = parsing.parse_battery(self.request(protocol.CMD_BATTERY).payload)
        with self._state_lock:
            self.state.battery.update(bat)
        self._emit("battery")
        return dict(self.state.battery)

    def read_wearing(self) -> dict[int, int]:
        w = parsing.parse_wearing(self.request(protocol.CMD_WEARING).payload)
        with self._state_lock:
            self.state.wearing.update(w)
        self._emit("wearing")
        return dict(self.state.wearing)

    def read_anc(self) -> int | None:
        value = parsing.parse_anc_reply(self.request(protocol.CMD_ANC_QUERY, b"\x01\x01").payload)
        if value is not None:
            self.state.anc = value
            self._emit("anc")
        return value

    def read_hold_cycle(self) -> int | None:
        value = parsing.parse_hold_cycle_reply(self.request(protocol.CMD_ANC_QUERY, b"\x02\x01").payload)
        if value is not None:
            self.state.hold_cycle = value
            self._emit("hold-cycle")
        return value

    def _default_feature_ids(self) -> list[int]:
        """The profile's feature ids; for unknown buds, the family's usual suspects."""
        prof = self.active_profile
        return prof.feature_ids() if prof.features else list(model.DEFAULT_SCAN_IDS)

    def read_features(self, ids: list[int] | None = None) -> dict[int, bool]:
        """Ask for feature switches. Only the ones the firmware has come back."""
        wanted = ids if ids is not None else self._default_feature_ids()
        found: dict[int, bool] = {}
        # Long id lists have made some buds drop the link, so ask in small chunks.
        for i in range(0, len(wanted), 4):
            chunk = wanted[i : i + 4]
            pkt = self.request(protocol.CMD_FEATURE_QUERY, bytes([len(chunk), *chunk]))
            found.update(parsing.parse_features(pkt.payload))
        with self._state_lock:
            self.state.features.update(found)
        self._emit("features")
        return found

    def read_gestures(self) -> list[GestureEntry]:
        table = parsing.parse_gesture_table(self.request(protocol.CMD_GESTURE_TABLE).payload)
        with self._state_lock:
            self.state.gestures = table
        self._emit("gestures")
        return table

    def read_eq(self) -> int | None:
        pkt = self.request(protocol.CMD_EQ_QUERY)
        if len(pkt.payload) >= 2:
            self.state.eq = pkt.payload[1]
            self._emit("eq")
            return self.state.eq
        return None

    def read_custom_eqs(self) -> list[parsing.CustomEq]:
        pkt = self.request(protocol.CMD_CUSTOM_EQ_LIST, timeout=1.5)
        eqs = parsing.parse_custom_eq_list(pkt.payload)
        with self._state_lock:
            self.state.custom_eqs = eqs
        self._emit("eq")
        return eqs

    def read_dynamic_audio(self) -> list[parsing.DynamicBand]:
        """Three-band engine (0x012C); older firmware answers on 0x0124 instead."""
        bands: list[parsing.DynamicBand] = []
        for cmd in (protocol.CMD_DYNAMIC_AUDIO_BANDS, protocol.CMD_DYNAMIC_AUDIO_SINGLE):
            if not self.supports(cmd):
                continue
            try:
                bands = parsing.parse_dynamic_audio(self.request(cmd, timeout=1.5).payload)
            except (Nack, TimeoutError):
                continue
            if bands:
                break
        with self._state_lock:
            self.state.dynamic_audio = bands
        self._emit("dynamic-audio")
        return bands

    def scan_features(self, ids: list[int] | None = None) -> dict[int, bool]:
        """Read every feature id worth knowing about, to see what these buds have.

        Toggle a setting in the official app and scan again to learn which id it is.
        """
        wanted = ids if ids is not None else sorted(set(self.active_profile.feature_ids()) | set(model.DEFAULT_SCAN_IDS))
        return self.read_features(list(wanted))

    # -- writes (each one is read back; an ack of 00 does not prove the write took) ----

    def set_dynamic_band(self, band: int, value: int) -> list[parsing.DynamicBand]:
        known = next((b for b in self.state.dynamic_audio if b.band == band), None)
        prof = self.active_profile
        if known is not None:
            lo, hi = known.lo, known.hi  # what the buds themselves report for this band
        else:
            lo, hi = prof.dynamic_range
        bands = {b.band for b in self.state.dynamic_audio} or set(prof.dynamic_bands)
        if band not in bands or not lo <= value <= hi:
            raise ValueError(f"band must be one of {sorted(bands)} and value {lo}..{hi}")
        self._write(protocol.CMD_DYNAMIC_AUDIO_WRITE, parsing.encode_dynamic_band(band, value, lo, hi))
        bands = self.read_dynamic_audio()
        got = next((b.value for b in bands if b.band == band), None)
        if got != value:
            raise self._verify_failed(f"dynamic audio band {band}: wanted {value}, buds report {got!r}")
        return bands

    def save_custom_eq(self, eq_id: int, name: str, bands: list[tuple[int, int]]) -> list[parsing.CustomEq]:
        """Create (eq_id None/0 -> action 1) or overwrite a custom preset; the buds select it."""
        spec = self.active_profile.custom_eq
        if spec is None:
            raise ValueError("this profile does not describe a custom equaliser (pick a profile that does, or add it to yours)")
        lo, hi = spec.gain_min, spec.gain_max
        if any(not lo <= g <= hi for _, g in bands):
            raise ValueError(f"custom EQ gain must be within {lo}..{hi} dB")
        if not 1 <= len(name.encode()) <= 32:
            raise ValueError("custom EQ name must be 1-32 bytes")
        action = 2 if eq_id else 1
        self._write(
            protocol.CMD_CUSTOM_EQ_WRITE,
            parsing.encode_custom_eq(action, eq_id or 0, name, bands, (lo, hi)),
            timeout=4.0,
        )
        eqs = self.read_custom_eqs()
        self.read_eq()
        if not any(e.name == name and list(e.bands) == [tuple(b) for b in bands] for e in eqs):
            raise self._verify_failed(f"custom EQ {name!r} is not on the buds after the write")
        return eqs

    def select_eq(self, eq_id: int) -> int | None:
        """Switch to a built-in preset or one of the custom presets (a custom one is selected
        by saving it again, which is how the buds make a custom preset active)."""
        customs = self.state.custom_eqs
        if not any(e.id == eq_id for e in customs) and self.supports(protocol.CMD_CUSTOM_EQ_LIST):
            customs = self.read_custom_eqs()
        custom = next((e for e in customs if e.id == eq_id), None)
        if custom is None:
            return self.set_eq_preset(eq_id)
        self.save_custom_eq(custom.id, custom.name, [tuple(b) for b in custom.bands])
        if self.state.eq != eq_id:
            raise self._verify_failed(f"asked for custom EQ {eq_id}, buds report {self.state.eq!r}")
        return self.state.eq

    def delete_custom_eq(self, eq_id: int) -> list[parsing.CustomEq]:
        self._write(protocol.CMD_CUSTOM_EQ_WRITE, parsing.encode_custom_eq(3, eq_id, "", []), timeout=4.0)
        eqs = self.read_custom_eqs()
        self.read_eq()
        if any(e.id == eq_id for e in eqs):
            raise self._verify_failed(f"custom EQ {eq_id} is still on the buds after deleting it")
        return eqs

    def set_anc(self, value: int, expect: "int | tuple[int, ...] | None" = None) -> int | None:
        """Write a raw noise-control value and check the buds report ``expect`` (default: the
        same number). Some models report a different number than they are written, or one of
        several, so ``expect`` may be a tuple of accepted values."""
        want = (value,) if expect is None else (expect if isinstance(expect, tuple) else (expect,))
        self._write(protocol.CMD_ANC_WRITE, b"\x01\x01" + protocol.mask_to_bytes(value))
        got = self.read_anc()
        if got not in want:
            shown = " or ".join(f"0x{w:02X}" for w in want)
            raise self._verify_failed(f"asked for noise mode 0x{value:02X}, buds report {got!r} (expected {shown})")
        return got

    def set_anc_mode(self, mode: "profiles.AncMode | str") -> int | None:
        """Select a mode of the profile in use, by object or key."""
        if isinstance(mode, str):
            try:
                mode = self.active_profile.mode(mode)
            except KeyError:
                raise BudsError(f"the profile {self.active_profile.key!r} has no noise-control mode {mode!r}") from None
        return self.set_anc(mode.set, expect=self.active_profile.expected_reports(mode))

    def _active_conflicts(self, feat: "profiles.Feature") -> "list[profiles.Feature]":
        """Switches that conflict with ``feat`` and are on right now (asked from the buds, not remembered)."""
        others = list(self.active_profile.conflicting(feat))
        if not others:
            return []
        try:
            self.read_features([o.id for o in others])
        except (BudsError, TimeoutError):
            pass
        return [o for o in others if self.state.features.get(o.id)]

    def _refusal(self, err: Nack, feat: "profiles.Feature", on: bool, active: "list[profiles.Feature]") -> FeatureRefused:
        reasons = []
        if active:
            return FeatureConflict(err.cmd, err.status, feat, on, active, reasons)
        if any(self.state.wearing.get(c) in model.IN_CASE_STATUSES for c in (1, 2)):
            reasons.append("a bud is in the case")
        reasons.append("the model may not support it, or may only allow it in some states (this tool does not know status "
                       f"{err.status}; run with --trace and send the output if it should work)")
        return FeatureRefused(err.cmd, err.status, feat, on, reasons)

    def set_feature(self, feature_id: int, on: bool, *, verify: bool = True, resolve: bool = False) -> bool | None:
        """Switch a feature on or off.

        If the buds refuse while a conflicting switch is on, :class:`FeatureConflict` is raised, or with
        ``resolve`` the conflicting switches are turned off first and the write is tried once more, which
        is what the vendor app does after the user confirms.
        """
        feat = self.active_profile.feature_for_id(feature_id)
        payload = bytes([feature_id, 1 if on else 0])
        self.last_resolved: list[profiles.Feature] = []  # what had to be turned off first, for the caller to report
        try:
            self._write(protocol.CMD_FEATURE_WRITE, payload)
        except Nack as e:
            if e.status == protocol.STATUS_NOT_WORN:
                raise
            active = self._active_conflicts(feat) if on else []
            if not (active and resolve):
                raise self._refusal(e, feat, on, active) from None
            # switches that drop the link go last, since nothing can be written until the buds are back
            for other in sorted(active, key=lambda o: o.drops_link):
                self.set_feature(other.id, False, verify=not other.drops_link)
            self.last_resolved = list(active)  # set after the nested calls, which reset it
            try:
                self._write(protocol.CMD_FEATURE_WRITE, payload)
            except Nack as again:
                raise self._refusal(again, feat, on, []) from None
        with self._state_lock:
            self.state.features[feature_id] = on
        if not verify:
            self._emit("features")
            return on
        got = self.read_features([feature_id]).get(feature_id)
        if got is not None and got != on:
            raise self._verify_failed(f"feature 0x{feature_id:02X}: wanted {on}, buds report {got}")
        return got

    def set_spatial(self, on: bool, *, resolve: bool = False) -> bool | None:
        if on and self.supports(protocol.CMD_SPATIAL_SOURCE):
            try:
                # realme Link tells the buds to render 3D audio themselves (00) rather than the phone (01).
                self.request(protocol.CMD_SPATIAL_SOURCE, b"\x00")
            except (Nack, TimeoutError):
                pass
        return self.set_feature(0x1B, on, resolve=resolve)

    def set_eq_preset(self, preset_id: int) -> int | None:
        self._write(protocol.CMD_EQ_SELECT, bytes([preset_id]))
        got = self.read_eq()
        if got != preset_id:
            raise self._verify_failed(f"asked for EQ preset {preset_id}, buds report {got!r}")
        return got

    def set_hold_cycle(self, mask: int) -> int | None:
        if mask == 0:
            raise ValueError("the hold gesture needs at least one mode to cycle through")
        self._write(protocol.CMD_ANC_WRITE, b"\x02\x01" + protocol.mask_to_bytes(mask))
        got = self.read_hold_cycle()
        if got != mask:
            raise self._verify_failed(f"asked for hold cycle 0x{mask:02X}, buds report {got!r}")
        return got

    def set_gesture(self, side: int, action: int, fn: int) -> list[GestureEntry]:
        """Bind ``fn`` to one gesture on one bud (side 1 left, 2 right).

        Writes every slot of the table that matches (side, action), because the table's
        shape differs between buds and a gesture can span several slots. The on-call
        group (button 6) is never touched.
        """
        table = self.read_gestures()
        slots = [e for e in table if e.dev == side and e.act == action and e.btn != model.BTN_CALL]
        if not slots:
            raise BudsError(
                f"this bud has no {model.ACTION_LABELS.get(action, action)} gesture to remap "
                f"(table has: {sorted({(e.dev, e.act) for e in table})})"
            )
        payload = bytes([len(slots)]) + b"".join(bytes([e.dev, e.btn, e.act, fn]) for e in slots)
        self._write(protocol.CMD_GESTURE_WRITE, payload)
        after = self.read_gestures()
        for e in slots:
            now = [x for x in after if (x.dev, x.btn, x.act) == (e.dev, e.btn, e.act)]
            if not now or any(x.fn != fn and not (x.fn == model.FN_ANC_CYCLE and fn == model.FN_NONE) for x in now):
                raise self._verify_failed(f"gesture {e.dev}/{e.btn}/{e.act} did not take function 0x{fn:02X}")
        return after

    def find_buds(self, on: bool) -> None:
        self._write(protocol.CMD_FIND, b"\x01" if on else b"\x00")


    # -- waiting for pushes -------------------------------------------------

    def _wait_for(self, predicate, timeout: float) -> bool:
        """Block until ``predicate()`` is true, the link drops, or ``timeout`` passes."""
        with self._cond:
            return self._cond.wait_for(lambda: predicate() or not self.state.connected, timeout) and self.state.connected

    # -- earbud fit test ----------------------------------------------------

    def run_fit_test(self, timeout: float = 30.0) -> dict[int, int]:
        """Run the fit test and return ``{side: raw status}`` (1 left, 2 right).

        Both buds must be in the ears. The result arrives as a push (event 4), which the
        app handles even for events it did not register for, so this does the same.
        """
        if not self.supports(protocol.CMD_FIT_TEST):
            raise BudsError("these buds do not list the fit test in their capabilities")
        self.read_wearing()
        out_of_ear = [c for c in (1, 2) if self.state.wearing.get(c) not in model.IN_EAR_STATUSES]
        if out_of_ear:
            raise BudsError("put both earbuds in your ears first, the fit test needs both")
        if protocol.EV_FIT_TEST not in self.state.broadcast_codes:
            # Not on the buds' list of offered events. Asking is harmless if they ignore it.
            self._write(protocol.CMD_REGISTER_EVENT, bytes([protocol.EV_FIT_TEST]), timeout=0.7)
        start_seq = self.state.fit_test_seq
        self._write(protocol.CMD_FIT_TEST, b"\x01", timeout=2.0)
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                seen = start_seq
                if remaining <= 0 or not self._wait_for(lambda: self.state.fit_test_seq != seen, remaining):
                    if not self.state.connected:
                        raise LinkClosed("link closed during the fit test")
                    raise BudsError(
                        "no fit-test result arrived. These buds may not push it unprompted; "
                        "rerun with --trace and send the output"
                    )
                start_seq = self.state.fit_test_seq
                result = dict(self.state.fit_test or {})
                statuses = set(result.values())
                if model.FIT_INTERRUPTED in statuses:
                    continue  # the app ignores this one and keeps waiting
                if statuses & set(model.FIT_RETRY):
                    raise BudsError("the fit test failed, try again (keep still and keep both buds in)")
                if model.FIT_REMOVED in statuses:
                    raise BudsError("a bud was taken out during the fit test")
                return result
        finally:
            try:
                self._write(protocol.CMD_FIT_TEST, b"\x00", timeout=1.0)
            except (BudsError, TimeoutError):
                pass

    # -- Golden Sound (hearing profile) -------------------------------------

    def read_hearing_record(self) -> parsing.HearingRecord | None:
        """The profile stored on the buds (they hold only the active one)."""
        try:
            pkt = self.request(protocol.CMD_HEARING_RECORD, timeout=2.0)
        except (Nack, TimeoutError):
            rec = None
        else:
            rec = parsing.parse_hearing_record(pkt.payload)
        with self._state_lock:
            self.state.hearing_record = rec
        self._emit("hearing-record")
        return rec

    def set_golden_sound(self, on: bool) -> bool | None:
        return self.set_feature(0x0B, on)

    def hearing_test_start(self) -> None:
        self._write(protocol.CMD_HEARING_DETECT, bytes([2, 1]))

    def hearing_test_stop(self) -> None:
        self.stop_tone()
        self._write(protocol.CMD_HEARING_DETECT, bytes([2, 0]))

    def play_tone(self, side: int, band: int, db: int) -> None:
        """Play one test tone in one ear. Stops the previous one first, like the app."""
        if side not in model.HEARING_SIDES or band not in model.HEARING_BANDS:
            raise ValueError("side must be 1 or 2 and band 1-6")
        if db not in model.HEARING_STOPS:
            raise ValueError(f"tone level must be one of {model.HEARING_STOPS}")
        self.stop_tone()
        self._write(protocol.CMD_HEARING_PARAMS, parsing.encode_tone(side, band, db))

    def stop_tone(self) -> None:
        self._write(protocol.CMD_HEARING_PARAMS, bytes([4]), timeout=0.8)

    def ear_scan(self, uid: int, timeout: float = 30.0) -> parsing.EarScan:
        """Measure the ear canals (both buds in). Returns the scan data to store with a profile."""
        self.read_wearing()
        if any(self.state.wearing.get(c) not in model.IN_EAR_STATUSES for c in (1, 2)):
            raise BudsError("put both earbuds in your ears first, the ear scan needs both")
        seq = self.state.ear_scan_seq
        self._write(protocol.CMD_HEARING_DETECT, parsing.encode_ear_scan_start(1, uid), timeout=2.0)
        try:
            ok = self._wait_for(lambda: self.state.ear_scan_seq != seq and self.state.ear_scan is not None, timeout)
            if not self.state.connected:
                raise LinkClosed("link closed during the ear scan")
            scan = self.state.ear_scan
            if not ok or scan is None:
                raise BudsError("the ear scan produced no result; check both buds are in and the room is quiet")
            return scan
        finally:
            try:
                self._write(protocol.CMD_HEARING_DETECT, parsing.encode_ear_scan_start(0, uid), timeout=1.0)
            except (BudsError, TimeoutError):
                pass

    def apply_golden_sound(self, entries, name: str, uid: int, scan_data: bytes | None) -> parsing.HearingRecord:
        """Store a hearing profile on the buds and switch Golden Sound on.

        Same order as realme Link: restore data, profile, ear-scan data, feature switch.
        """
        entries = [(int(a), int(b), int(c)) for a, b, c in entries]
        if len(entries) != len(model.HEARING_NODES) or {(a, b) for a, b, _ in entries} != set(model.HEARING_NODES):
            raise ValueError("a profile needs one value for each of the six bands of each ear")
        if any(db not in model.HEARING_RESULT_VALUES for _, _, db in entries):
            raise ValueError(f"profile values must be one of {model.HEARING_RESULT_VALUES}")
        if not 0 < uid < 2**31:
            raise ValueError("profile id must be a positive 31-bit number")
        self._write(protocol.CMD_RESTORE_DATA, bytes([1, 1, 1, 0, model.HEARING_RESTORE_TYPE_ID]))
        self._write(protocol.CMD_HEARING_PARAMS, parsing.encode_hearing_profile(3, entries, uid, name), timeout=3.0)
        if scan_data:
            self._write(protocol.CMD_EAR_SCAN_WRITE, parsing.encode_ear_scan_data(3, uid, scan_data), timeout=3.0)
        self.set_golden_sound(True)
        rec = self.read_hearing_record()
        if rec is None or rec.id != uid or sorted(rec.entries) != sorted(parsing.HearingEntry(*e) for e in entries):
            raise self._verify_failed("the profile on the buds does not match what was applied")
        return rec
