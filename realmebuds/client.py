"""High-level, thread-safe client for one pair of buds over an open RFCOMM socket."""

from __future__ import annotations

import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import model, parsing, protocol
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
    ) -> None:
        self.sock = sock
        self.state = State()
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
            self.request(protocol.CMD_SUBSCRIBE, bytes([len(wanted), *wanted]), ok=False)

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
        ]
        for name, fn in steps:
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

    def read_features(self, ids: list[int] | None = None) -> dict[int, bool]:
        """Ask for feature switches. Only the ones the firmware has come back."""
        wanted = ids if ids is not None else [f.id for f in model.FEATURES]
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
        return self.read_features(list(ids if ids is not None else model.SCAN_FEATURE_IDS))

    # -- writes (each one is read back; an ack of 00 does not prove the write took) ----

    def set_dynamic_band(self, band: int, value: int) -> list[parsing.DynamicBand]:
        lo, hi = model.DYNAMIC_RANGE
        if band not in model.DYNAMIC_BANDS or not lo <= value <= hi:
            raise ValueError(f"band must be 1-3 and value {lo}..{hi}")
        self.request(protocol.CMD_DYNAMIC_AUDIO_WRITE, parsing.encode_dynamic_band(band, value, lo, hi))
        bands = self.read_dynamic_audio()
        got = next((b.value for b in bands if b.band == band), None)
        if got != value:
            raise VerifyFailed(f"dynamic audio band {band}: wanted {value}, buds report {got!r}")
        return bands

    def save_custom_eq(self, eq_id: int, name: str, bands: list[tuple[int, int]]) -> list[parsing.CustomEq]:
        """Create (eq_id None/0 -> action 1) or overwrite a custom preset; the buds select it."""
        lo, hi = model.CUSTOM_EQ_GAIN_RANGE
        if any(not lo <= g <= hi for _, g in bands):
            raise ValueError(f"custom EQ gain must be within {lo}..{hi} dB")
        if not 1 <= len(name.encode()) <= 32:
            raise ValueError("custom EQ name must be 1-32 bytes")
        action = 2 if eq_id else 1
        self.request(
            protocol.CMD_CUSTOM_EQ_WRITE,
            parsing.encode_custom_eq(action, eq_id or 0, name, bands, (lo, hi)),
            timeout=4.0,
        )
        eqs = self.read_custom_eqs()
        self.read_eq()
        return eqs

    def delete_custom_eq(self, eq_id: int) -> list[parsing.CustomEq]:
        self.request(protocol.CMD_CUSTOM_EQ_WRITE, parsing.encode_custom_eq(3, eq_id, "", []), timeout=4.0)
        eqs = self.read_custom_eqs()
        self.read_eq()
        return eqs


    def set_anc(self, mask: int) -> int | None:
        self.request(protocol.CMD_ANC_WRITE, b"\x01\x01" + protocol.mask_to_bytes(mask))
        got = self.read_anc()
        if got != mask:
            raise VerifyFailed(f"asked for noise mode 0x{mask:02X}, buds report {got!r}")
        return got

    def set_feature(self, feature_id: int, on: bool, *, verify: bool = True) -> bool | None:
        self.request(protocol.CMD_FEATURE_WRITE, bytes([feature_id, 1 if on else 0]))
        with self._state_lock:
            self.state.features[feature_id] = on
        if not verify:
            self._emit("features")
            return on
        got = self.read_features([feature_id]).get(feature_id)
        if got is not None and got != on:
            raise VerifyFailed(f"feature 0x{feature_id:02X}: wanted {on}, buds report {got}")
        return got

    def set_spatial(self, on: bool) -> bool | None:
        if on:
            try:
                # realme Link tells the buds to render 3D audio themselves (00) rather than the phone (01).
                self.request(protocol.CMD_SPATIAL_SOURCE, b"\x00")
            except (Nack, TimeoutError):
                pass
        return self.set_feature(0x1B, on)

    def set_eq_preset(self, preset_id: int) -> int | None:
        self.request(protocol.CMD_EQ_SELECT, bytes([preset_id]))
        got = self.read_eq()
        if got != preset_id:
            raise VerifyFailed(f"asked for EQ preset {preset_id}, buds report {got!r}")
        return got

    def set_hold_cycle(self, mask: int) -> int | None:
        if mask == 0:
            raise ValueError("the hold gesture needs at least one mode to cycle through")
        self.request(protocol.CMD_ANC_WRITE, b"\x02\x01" + protocol.mask_to_bytes(mask))
        got = self.read_hold_cycle()
        if got != mask:
            raise VerifyFailed(f"asked for hold cycle 0x{mask:02X}, buds report {got!r}")
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
        self.request(protocol.CMD_GESTURE_WRITE, payload)
        after = self.read_gestures()
        for e in slots:
            now = [x for x in after if (x.dev, x.btn, x.act) == (e.dev, e.btn, e.act)]
            if not now or any(x.fn != fn and not (x.fn == model.FN_ANC_CYCLE and fn == model.FN_NONE) for x in now):
                raise VerifyFailed(f"gesture {e.dev}/{e.btn}/{e.act} did not take function 0x{fn:02X}")
        return after

    def find_buds(self, on: bool) -> None:
        self.request(protocol.CMD_FIND, b"\x01" if on else b"\x00")
