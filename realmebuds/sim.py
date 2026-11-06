"""A pretend pair of buds speaking the same protocol, for tests and for trying the UI.

It is built from the documented behaviour of the real firmware (acks, pushes after a
change, a time request on connect, "not worn" refusals), not from a recording, so it
proves our code is self-consistent, not that the real buds agree. Real-hardware checks
are listed in the README.
"""

from __future__ import annotations

import socket
import struct
import threading

from . import model, protocol
from .parsing import GestureEntry
from .protocol import Packet


class FakeBuds:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.framer = protocol.Framer()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop = False
        self.received: list[Packet] = []
        self.time_answered = threading.Event()
        self.answers: list[Packet] = []

        # Behaviour switches for tests.
        self.worn = True  # False: ANC writes are refused with status 0x0E
        self.ignore_gesture_writes = False
        self.send_time_request = True
        self.fragment_replies = False  # split large replies like the real buds can
        self.ignore_commands: set[int] = set()  # never answer these (simulates absent features)

        # Device state.
        self.battery = {1: 70, 2: 68, 3: 0}
        self.wearing = {1: 3, 2: 3, 3: 0}
        self.anc = model.ANC_TRANSPARENCY
        self.smart_level = model.ANC_MILD
        self.hold_cycle = model.ANC_MAX | model.ANC_TRANSPARENCY
        self.features = {0x04: True, 0x06: False, 0x08: False, 0x09: False, 0x11: True,
                         0x18: True, 0x1A: False, 0x1B: False, 0x1D: True}
        self.eq = 1
        self.custom_eqs: list[tuple[int, str, tuple[tuple[int, int], ...]]] = []
        self.dynamic = {1: 0, 2: 0, 3: 0}
        self.gestures = [
            GestureEntry(1, 1, 2, 0x01), GestureEntry(1, 1, 3, 0x06), GestureEntry(1, 1, 4, 0x08),
            GestureEntry(2, 1, 2, 0x01), GestureEntry(2, 1, 3, 0x05), GestureEntry(2, 1, 4, 0x08),
            GestureEntry(1, 6, 2, 0x00), GestureEntry(2, 6, 2, 0x00),
        ]
        self.find_ringing = False

    # -- plumbing ---------------------------------------------------------

    def start(self) -> "FakeBuds":
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._loop, name="fake-buds", daemon=True)
        self.thread.start()
        return self

    def close(self) -> None:
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def drop_link(self) -> None:
        """Like closing the case lid: the socket just dies."""
        self.close()

    def _send(self, cmd: int, seq: int, payload: bytes = b"") -> None:
        frame = protocol.build_frame(cmd, seq, payload)
        with self.lock:
            try:
                if self.fragment_replies and len(payload) > 8:
                    self.sock.sendall(self._fragment(cmd, seq, payload))
                else:
                    self.sock.sendall(frame)
            except OSError:
                pass

    @staticmethod
    def _fragment(cmd: int, seq: int, payload: bytes) -> bytes:
        packet = struct.pack("<HBH", cmd, seq, len(payload)) + payload
        chunks = [packet[i : i + 6] for i in range(0, len(packet), 6)]
        out = b""
        for i, chunk in enumerate(chunks):
            kind = 1 if i == 0 else 3 if i == len(chunks) - 1 else 2
            body = bytes([kind, 0, i & 0xFF]) + chunk
            out += bytes([protocol.HEADER]) + protocol.encode_leb128(len(body)) + body
        return out

    def push(self, payload: bytes) -> None:
        self._send(protocol.CMD_PUSH, protocol.PUSH_SEQ, payload)

    def _loop(self) -> None:
        if self.send_time_request:
            self._send(protocol.CMD_TIME_REQUEST, 5, b"\x00\x00\x00\x00")
        while not self.stop:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            for pkt in self.framer.feed(data):
                self.received.append(pkt)
                try:
                    self._handle(pkt)
                except OSError:
                    return

    # -- command handling -------------------------------------------------

    def _reply(self, pkt: Packet, payload: bytes) -> None:
        self._send(pkt.cmd | protocol.RESPONSE_BIT, pkt.seq, payload)

    def _handle(self, pkt: Packet) -> None:
        if pkt.is_response:
            self.answers.append(pkt)
            if pkt.base_cmd == protocol.CMD_TIME_REQUEST and pkt.payload[:1] == b"\x00" and len(pkt.payload) == 5:
                self.time_answered.set()
            return
        if pkt.cmd in self.ignore_commands:
            return
        c, p = pkt.cmd, pkt.payload
        ok = b"\x00"
        if c == protocol.CMD_HANDSHAKE:
            self._reply(pkt, b"\x00\xff\x77\x5a\xea\x67\x0e\x20\x07")
        elif c == protocol.CMD_PRODUCT_ID:
            self._reply(pkt, b"\x00\x14\x54\x06")
        elif c == protocol.CMD_FIRMWARE:
            self._reply(pkt, b"\x00\x04" + b"1,2,138,2,2,138,3,1,01,3,2,105")
        elif c == protocol.CMD_BATTERY:
            known = {k: v for k, v in self.battery.items() if v}
            self._reply(pkt, b"\x00" + bytes([len(known)]) + b"".join(bytes([k, v]) for k, v in known.items()))
        elif c == protocol.CMD_WEARING:
            self._reply(pkt, b"\x00" + bytes([len(self.wearing)]) + b"".join(bytes([k, v]) for k, v in self.wearing.items()))
        elif c == protocol.CMD_BROADCAST_CODES:
            self._reply(pkt, b"\x00\x05\x01\x02\x03\x04\x05")
        elif c == protocol.CMD_SUBSCRIBE:
            self._reply(pkt, ok)
        elif c == protocol.CMD_ANC_QUERY:
            if p == b"\x01\x01":
                self._reply(pkt, b"\x00\x01\x01" + protocol.mask_to_bytes(self.anc))
            elif p == b"\x02\x01":
                self._reply(pkt, b"\x00\x02\x01" + protocol.mask_to_bytes(self.hold_cycle))
            else:
                self._reply(pkt, b"\x01")
        elif c == protocol.CMD_FEATURE_QUERY:
            ids = list(p[1 : 1 + p[0]])
            have = [(i, self.features[i]) for i in ids if i in self.features]
            self._reply(pkt, b"\x00" + bytes([len(have)]) + b"".join(bytes([i, int(v)]) for i, v in have))
        elif c == protocol.CMD_GESTURE_TABLE:
            body = b"".join(bytes(e) for e in self.gestures)
            self._reply(pkt, b"\x00" + bytes([len(self.gestures)]) + body)
        elif c == protocol.CMD_EQ_QUERY:
            self._reply(pkt, b"\x00" + bytes([self.eq]))
        elif c == protocol.CMD_CUSTOM_EQ_LIST:
            out = bytes([len(self.custom_eqs)])
            for eq_id, name, bands in self.custom_eqs:
                raw = name.encode()
                out += bytes([int(self.eq == eq_id), 0xFA, 0x06, eq_id, len(raw)]) + raw + bytes([len(bands)])
                for hz, g in bands:
                    out += hz.to_bytes(2, "little") + bytes([g & 0xFF])
            self._reply(pkt, b"\x00" + out)
        elif c in (protocol.CMD_DYNAMIC_AUDIO_BANDS,):
            out = b"".join(bytes([0xFB, 5, v & 0xFF, b]) for b, v in self.dynamic.items())
            self._reply(pkt, b"\x00" + bytes([len(self.dynamic)]) + out)
        elif c == protocol.CMD_ANC_WRITE:
            self._write_anc(pkt)
        elif c == protocol.CMD_FEATURE_WRITE and len(p) == 2:
            if p[0] not in self.features:
                self._reply(pkt, b"\x01")
                return
            self.features[p[0]] = bool(p[1])
            self._reply(pkt, ok)
            if p[0] == 0x06:
                self.push(bytes([protocol.EV_GAME_MODE, p[1]]))
        elif c == protocol.CMD_EQ_SELECT and len(p) == 1:
            self.eq = p[0]
            self._reply(pkt, ok)
            self._send(0x0504, protocol.PUSH_SEQ, bytes([self.eq]))
        elif c == protocol.CMD_GESTURE_WRITE:
            self._write_gestures(pkt)
        elif c == protocol.CMD_DYNAMIC_AUDIO_WRITE and len(p) == 4:
            lo, hi, value, band = p
            if band not in self.dynamic:
                self._reply(pkt, b"\x01")
                return
            self.dynamic[band] = value - 256 if value > 127 else value
            self._reply(pkt, ok)
        elif c == protocol.CMD_CUSTOM_EQ_WRITE:
            self._write_custom_eq(pkt)
        elif c == protocol.CMD_SPATIAL_SOURCE:
            self._reply(pkt, ok)
        elif c == protocol.CMD_FIND:
            self.find_ringing = p == b"\x01"
            self._reply(pkt, ok)
        else:
            self._reply(pkt, b"\x01")

    def _write_anc(self, pkt: Packet) -> None:
        p = pkt.payload
        if p[:2] == b"\x01\x01":
            if not self.worn:
                self._reply(pkt, bytes([protocol.STATUS_NOT_WORN]))
                return
            mask = int.from_bytes(p[2:], "little")
            if mask not in model.ANC_BY_BIT:
                self._reply(pkt, b"\x01")
                return
            self.anc = mask
            self._reply(pkt, b"\x00")
            self.push(b"\x03\x01\x01" + protocol.mask_to_bytes(mask))
        elif p[:2] == b"\x02\x01":
            self.hold_cycle = int.from_bytes(p[2:], "little")
            self._reply(pkt, b"\x00")
            self.push(b"\x03\x02\x01" + protocol.mask_to_bytes(self.hold_cycle))
        else:
            self._reply(pkt, b"\x01")

    def _write_gestures(self, pkt: Packet) -> None:
        p = pkt.payload
        count = p[0]
        entries = [GestureEntry(*p[1 + 4 * i : 5 + 4 * i]) for i in range(count)]
        self._reply(pkt, b"\x00")
        if self.ignore_gesture_writes:
            return
        for e in entries:
            for i, cur in enumerate(self.gestures):
                if (cur.dev, cur.btn, cur.act) == (e.dev, e.btn, e.act):
                    self.gestures[i] = e

    def _write_custom_eq(self, pkt: Packet) -> None:
        p = pkt.payload
        action, eq_id, name_len = p[0], p[3], p[4]
        name = p[5 : 5 + name_len].decode()
        n = p[5 + name_len]
        base = 6 + name_len
        bands = tuple(
            (int.from_bytes(p[base + 3 * i : base + 3 * i + 2], "little"), (lambda b: b - 256 if b > 127 else b)(p[base + 3 * i + 2]))
            for i in range(n)
        )
        if action == 1:
            eq_id = max([e[0] for e in self.custom_eqs] + [3]) + 1
            self.custom_eqs.append((eq_id, name, bands))
            self.eq = eq_id
        elif action == 2:
            self.custom_eqs = [(i, name, bands) if i == eq_id else (i, nm, b) for i, nm, b in self.custom_eqs]
            self.eq = eq_id
        elif action == 3:
            self.custom_eqs = [e for e in self.custom_eqs if e[0] != eq_id]
            self.eq = 0
        self._reply(pkt, b"\x00" + bytes([eq_id]))

    # -- things that happen on the buds themselves --------------------------

    def press_anc_button(self, mask: int) -> None:
        self.anc = mask
        self.push(b"\x03\x01\x01" + protocol.mask_to_bytes(mask))

    def set_battery(self, left: int, right: int, case: int = 0) -> None:
        self.battery = {1: left, 2: right, 3: case}
        known = [(k, v) for k, v in self.battery.items() if v]
        self.push(b"\x01" + bytes([len(known)]) + b"".join(bytes([k, v]) for k, v in known))


def connected_pair(**kwargs) -> tuple[socket.socket, FakeBuds]:
    """A socket for the client plus the running fake buds on its other end."""
    ours, theirs = socket.socketpair()
    sim = FakeBuds(theirs)
    for k, v in kwargs.items():
        setattr(sim, k, v)
    return ours, sim.start()
