"""Bluetooth LE (GATT) link to the buds through BlueZ's D-Bus API. Experimental, untested on real buds.

Models that only speak BLE have no RFCOMM channel. The protocol frames are the same, only the
pipe differs, so :class:`GattSocket` looks like the small part of a socket that
:class:`realmebuds.client.Buds` uses (settimeout, recv, sendall, shutdown, close) and carries the
frames in GATT writes and notifications. Which characteristic does what is derived from the
vendor's Android app, see "BLE GATT transport" in docs/PROTOCOL.md.

Needs PyGObject (Gio/GLib) and a running bluetoothd. ``REALME_BUDS_DBUS_ADDRESS`` points the code
at another bus (the tests serve a fake BlueZ on a private one).
"""

from __future__ import annotations

import os
import queue
import re
import socket
import threading
import time
from typing import Any, Callable

from . import protocol
from .transport import TransportError

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):  # PyGObject missing: only fails when BLE is actually used
    Gio = GLib = None  # type: ignore[assignment]

DBUS_ADDRESS_ENV = "REALME_BUDS_DBUS_ADDRESS"
BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
SERVICE_IFACE = "org.bluez.GattService1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OBJMGR_IFACE = "org.freedesktop.DBus.ObjectManager"

# From the vendor app (class GattDevice): writes go to WRITE_CHAR inside the model's service
# (default DEFAULT_SERVICE), notifications are enabled on NOTIFY_CHAR inside DEFAULT_SERVICE.
DEFAULT_SERVICE = "0000079a-d102-11e1-9b23-00025b00a5a5"
WRITE_CHAR = "0100079a-d102-11e1-9b23-00025b00a5a5"
NOTIFY_CHAR = "0200079a-d102-11e1-9b23-00025b00a5a5"

DEFAULT_ATT_PAYLOAD = 20  # ATT_MTU 23 minus the 3 bytes of ATT header; what the vendor app works with
CONNECT_TIMEOUT = 20.0
CALL_TIMEOUT = 5.0

_EOF = object()
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_REMOTE_ERROR = re.compile(r"GDBus\.Error:([^:]+): ?(.*)", re.S)

_BLUEZ_HINTS = {
    "org.bluez.Error.NotReady": "the Bluetooth adapter is not ready (is it powered on? try: bluetoothctl power on)",
    "org.bluez.Error.InProgress": "BlueZ is busy with another operation on this device, try again in a moment",
    "org.bluez.Error.NotConnected": "the buds are not connected over Bluetooth LE",
    "org.bluez.Error.AlreadyConnected": "the buds are already connected",
    "org.bluez.Error.NotAvailable": "the buds are not reachable (out of range, or in the case with the lid shut)",
    "org.bluez.Error.NotSupported": "the buds do not support this operation",
    "org.bluez.Error.NotPermitted": "BlueZ refused the operation (the buds may need to be paired first)",
    "org.bluez.Error.NotAuthorized": "BlueZ refused the operation (the buds may need to be paired first)",
    "org.bluez.Error.AuthenticationFailed": "pairing/authentication with the buds failed (remove the device and pair again)",
    "org.bluez.Error.InvalidArguments": "BlueZ rejected the request as invalid",
    "org.bluez.Error.Failed": "BlueZ reported a failure",
    "org.freedesktop.DBus.Error.ServiceUnknown": "BlueZ (bluetoothd) is not running, so there is no Bluetooth to talk to",
    "org.freedesktop.DBus.Error.UnknownObject": "BlueZ no longer knows the buds (they were removed or went away)",
    "org.freedesktop.DBus.Error.NoReply": "BlueZ did not answer in time",
}


def _no_gi_message() -> str:
    """Bluetooth LE needs PyGObject, which only the distribution's Python has. Say how to get it."""
    import sys

    from . import launch

    python, _ = launch.find_python()
    msg = f"Bluetooth LE needs PyGObject (python3-gobject), and this Python ({sys.executable}) does not have it."
    if python:
        msg += f" Run the tool with one that does: PYTHONPATH={launch.package_root()} {python} -m realmebuds ..."
    else:
        msg += " " + launch.INSTALL_HELP.replace("\n", " ")
    return msg


def describe_dbus_error(err: Any) -> str:
    """A GLib.Error from a D-Bus call as one sentence a user can act on."""
    message = getattr(err, "message", str(err))
    m = _REMOTE_ERROR.search(message)
    if m:
        name, detail = m.group(1), m.group(2).strip()
        hint = _BLUEZ_HINTS.get(name)
        if hint is None:
            return f"{name}: {detail}" if detail else name
        return f"{hint} ({detail})" if detail and detail.lower() not in hint.lower() else hint
    if Gio is not None and err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
        return "BlueZ did not answer in time"
    return message


def normalise_uuid(text: str) -> str:
    u = text.strip().lower()
    if len(u) == 4 and all(c in "0123456789abcdef" for c in u):
        u = f"0000{u}-0000-1000-8000-00805f9b34fb"
    if not _UUID_RE.match(u):
        raise TransportError(f"{text!r} is not a UUID (like {DEFAULT_SERVICE})")
    return u


class _Loop:
    """A GLib main loop in a private thread. D-Bus signal callbacks arrive on it."""

    def __init__(self) -> None:
        self.context = GLib.MainContext.new()
        self._loop = GLib.MainLoop.new(self.context, False)
        started = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(started,), name="ble-glib", daemon=True)
        self._thread.start()
        started.wait(2)

    def _run(self, started: threading.Event) -> None:
        self.context.push_thread_default()
        try:
            # Attach to our own context: GLib.idle_add would use the global default one, which nobody runs.
            ready = GLib.idle_source_new()
            ready.set_callback(lambda *_: started.set() or False)
            ready.attach(self.context)
            self._loop.run()
        finally:
            self.context.pop_thread_default()

    def call(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the loop thread and return its result (or raise its exception)."""
        if threading.current_thread() is self._thread:
            return fn()
        done = threading.Event()
        box: list[Any] = [None, None]

        def run() -> bool:
            try:
                box[0] = fn()
            except BaseException as e:  # handed back to the caller
                box[1] = e
            done.set()
            return False

        self.context.invoke_full(GLib.PRIORITY_DEFAULT, run)
        done.wait()
        if box[1] is not None:
            raise box[1]
        return box[0]

    def stop(self) -> None:
        self._loop.quit()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)


class GattSocket:
    """Carries the buds' frames over GATT, with the interface of the socket parts ``Buds`` uses.

    Build one with :func:`connect_ble`. ``recv`` returns the bytes of notifications in arrival
    order, raises ``socket.timeout`` when nothing came within the timeout and returns ``b""`` once
    the link is gone. ``sendall`` splits a frame into consecutive writes that fit the ATT payload.
    """

    transport = "ble"

    def __init__(self, addr: str, service_uuid: str | None = None, *, timeout: float = CONNECT_TIMEOUT) -> None:
        if Gio is None:
            raise TransportError(_no_gi_message())
        self.addr = addr.upper()
        self.service_uuid = normalise_uuid(service_uuid) if service_uuid else DEFAULT_SERVICE
        self.mtu_payload = DEFAULT_ATT_PAYLOAD
        self.write_type = "command"
        self._timeout: float | None = None
        self._rx: queue.Queue[Any] = queue.Queue()
        self._left = b""
        self._eof = False
        self._shut = False
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._conn: Any = None
        self._own_conn = False
        self._loop: _Loop | None = None
        self._subs: list[int] = []
        self._device_path = ""
        self._write_path = ""
        self._notify_paths: list[str] = []
        self._framers: dict[str, protocol.Framer] | None = None
        self._we_connected = False
        self._released = False
        try:
            self._open(timeout)
        except BaseException:
            self.close()
            raise

    # -- D-Bus helpers -----------------------------------------------------------

    def _call(self, path: str, iface: str, method: str, args: Any = None, reply: str | None = None, *, timeout: float = CALL_TIMEOUT, dest: str = BLUEZ) -> Any:
        try:
            result = self._conn.call_sync(
                dest, path, iface, method, args, GLib.VariantType.new(reply) if reply else None,
                Gio.DBusCallFlags.NONE, int(timeout * 1000), None,
            )
        except GLib.Error as e:
            raise _DBusFailure(describe_dbus_error(e), e) from e
        return result.unpack() if result is not None else None

    def _managed_objects(self) -> dict[str, dict[str, dict[str, Any]]]:
        (objects,) = self._call("/", OBJMGR_IFACE, "GetManagedObjects", None, "(a{oa{sa{sv}}})")
        return objects

    def _properties(self, path: str, iface: str) -> dict[str, Any]:
        (props,) = self._call(path, PROPS_IFACE, "GetAll", GLib.Variant("(s)", (iface,)), "(a{sv})")
        return props

    # -- opening -----------------------------------------------------------------

    def _open(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        self._connect_bus()
        self._loop = _Loop()
        self._device_path = self._find_device()
        self._ensure_connected(deadline)
        self._subscribe(f"{self._device_path}", PROPS_IFACE, "PropertiesChanged", self._on_device_props)
        self._subscribe("/", OBJMGR_IFACE, "InterfacesRemoved", self._on_removed)
        self._find_characteristics()
        for path in self._notify_paths:
            self._subscribe(path, PROPS_IFACE, "PropertiesChanged", self._on_char_props)
        if len(self._notify_paths) > 1:
            self._framers = {p: protocol.Framer() for p in self._notify_paths}
        for path in self._notify_paths:
            try:
                self._call(path, CHAR_IFACE, "StartNotify")
            except _DBusFailure as e:
                raise TransportError(f"could not subscribe to notifications from the buds ({path}): {e}") from e
        # A device that dropped while we were setting up must not look like a working link.
        if not self._properties(self._device_path, DEVICE_IFACE).get("Connected"):
            raise TransportError(f"the buds ({self.addr}) disconnected while the Bluetooth LE link was being set up")

    def _connect_bus(self) -> None:
        address = os.environ.get(DBUS_ADDRESS_ENV)
        try:
            if address:
                self._conn = Gio.DBusConnection.new_for_address_sync(
                    address,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None, None,
                )
                self._own_conn = True
            else:
                self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            raise TransportError(f"cannot reach the system D-Bus, so BlueZ cannot be used: {describe_dbus_error(e)}") from e

    def _find_device(self) -> str:
        try:
            objects = self._managed_objects()
        except _DBusFailure as e:
            raise TransportError(f"cannot list Bluetooth devices from BlueZ: {e}") from e
        for path, ifaces in objects.items():
            dev = ifaces.get(DEVICE_IFACE)
            if dev and str(dev.get("Address", "")).upper() == self.addr:
                return path
        raise TransportError(
            f"BlueZ does not know a device {self.addr}. Pair the buds first (bluetoothctl scan on, then pair {self.addr}), "
            "and check the address with: realme-buds devices"
        )

    def _ensure_connected(self, deadline: float) -> None:
        props = self._properties(self._device_path, DEVICE_IFACE)
        if not props.get("Connected"):
            remaining = max(deadline - time.monotonic(), 1.0)
            try:
                self._call(self._device_path, DEVICE_IFACE, "Connect", timeout=remaining)
            except _DBusFailure as e:
                raise TransportError(f"could not connect to the buds {self.addr} over Bluetooth LE: {e}") from e
            self._we_connected = True
        while True:
            props = self._properties(self._device_path, DEVICE_IFACE)
            if not props.get("Connected"):
                raise TransportError(f"the buds {self.addr} disconnected while their services were being read")
            if props.get("ServicesResolved"):
                return
            if time.monotonic() >= deadline:
                raise TransportError(
                    f"timed out waiting for BlueZ to read the GATT services of {self.addr}. "
                    "Are the buds awake, in range and not connected to another phone?"
                )
            time.sleep(0.05)

    def _find_characteristics(self) -> None:
        objects = self._managed_objects()
        services = {
            p: i[SERVICE_IFACE]
            for p, i in objects.items()
            if SERVICE_IFACE in i and i[SERVICE_IFACE].get("Device") == self._device_path
        }
        chars = {
            p: i[CHAR_IFACE]
            for p, i in objects.items()
            if CHAR_IFACE in i and i[CHAR_IFACE].get("Service") in services
        }

        def find(service_uuid: str, char_uuid: str) -> str | None:
            for p, c in chars.items():
                if str(c.get("UUID", "")).lower() == char_uuid and str(services[c["Service"]].get("UUID", "")).lower() == service_uuid:
                    return p
            return None

        offered = sorted({str(s.get("UUID", "")).lower() for s in services.values()})

        def in_service(p: str) -> bool:
            return str(services[chars[p]["Service"]].get("UUID", "")).lower() == self.service_uuid

        if self.service_uuid not in offered:
            raise TransportError(
                f"the buds {self.addr} do not offer the Bluetooth LE control service {self.service_uuid}. "
                "They may be Classic-only buds (use the RFCOMM transport), or this model uses another service UUID "
                f"(pass --service-uuid). Services seen: {', '.join(offered) or 'none'}"
            )
        write = find(self.service_uuid, WRITE_CHAR)
        if write is None:
            seen = ", ".join(sorted(str(c.get("UUID", "")).lower() for p, c in chars.items() if in_service(p))) or "none"
            raise TransportError(
                f"the control service {self.service_uuid} on {self.addr} has no write characteristic {WRITE_CHAR} "
                f"(characteristics seen: {seen}). This model may use a different layout; see docs/PROTOCOL.md."
            )
        self._write_path = write
        flags = set(chars[write].get("Flags", []))
        # The vendor app writes with ATT write requests (Android's default write type).
        if "write" in flags:
            self.write_type = "request"
        elif "write-without-response" in flags:
            self.write_type = "command"
        else:
            raise TransportError(f"the characteristic {WRITE_CHAR} of {self.addr} cannot be written (flags: {', '.join(sorted(flags)) or 'none'})")

        notify = []
        for service in dict.fromkeys((DEFAULT_SERVICE, self.service_uuid)):
            p = find(service, NOTIFY_CHAR)
            if p is not None:
                notify.append(p)
        if not notify:
            notify = [
                p for p, c in chars.items()
                if in_service(p) and p != write and set(c.get("Flags", [])) & {"notify", "indicate"}
            ]
        if not notify:
            raise TransportError(
                f"the control service {self.service_uuid} on {self.addr} has no characteristic to receive replies on "
                f"({NOTIFY_CHAR} or any other that notifies); see docs/PROTOCOL.md."
            )
        self._notify_paths = notify

        mtu = chars[write].get("MTU")
        if isinstance(mtu, int) and mtu > 3 + DEFAULT_ATT_PAYLOAD:
            self.mtu_payload = mtu - 3

    def _subscribe(self, path: str, iface: str, member: str, callback: Callable[..., None]) -> None:
        def subscribe() -> int:
            return self._conn.signal_subscribe(BLUEZ, iface, member, path, None, Gio.DBusSignalFlags.NONE, callback)

        self._subs.append(self._loop.call(subscribe))

    # -- signals (run on the GLib thread; they must not block) --------------------

    def _on_char_props(self, _conn, _sender, path, _iface, _signal, params, *_rest) -> None:
        interface, changed, _invalidated = params.unpack()
        if interface != CHAR_IFACE or "Value" not in changed:
            return
        data = bytes(changed["Value"])
        if not data:
            return
        if self._framers is None:
            self._rx.put(data)
            return
        # Several notifying characteristics: a frame split across notifications must not be
        # glued to another characteristic's bytes, so each gets its own reassembly.
        for pkt in self._framers[path].feed(data):
            self._rx.put(protocol.build_frame(pkt.cmd, pkt.seq, pkt.payload))

    def _on_device_props(self, _conn, _sender, _path, _iface, _signal, params, *_rest) -> None:
        interface, changed, _invalidated = params.unpack()
        if interface == DEVICE_IFACE and changed.get("Connected") is False:
            self._mark_closed()

    def _on_removed(self, _conn, _sender, _path, _iface, _signal, params, *_rest) -> None:
        removed, _ifaces = params.unpack()
        if removed == self._device_path or removed.startswith(self._device_path + "/"):
            self._mark_closed()

    def _mark_closed(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self._rx.put(_EOF)

    # -- the socket interface Buds uses ------------------------------------------

    def settimeout(self, value: float | None) -> None:
        self._timeout = value

    def gettimeout(self) -> float | None:
        return self._timeout

    def recv(self, bufsize: int) -> bytes:
        if self._shut:
            return b""
        if self._left:
            out, self._left = self._left[:bufsize], self._left[bufsize:]
            return out
        if self._eof:
            return b""
        try:
            if self._timeout == 0:
                item = self._rx.get_nowait()
            else:
                item = self._rx.get(timeout=self._timeout)
        except queue.Empty:
            raise socket.timeout("timed out") from None
        if item is _EOF:
            self._eof = True
            return b""
        out, self._left = item[:bufsize], item[bufsize:]
        return out

    def sendall(self, data: bytes) -> None:
        if self._closed.is_set():
            raise BrokenPipeError("the Bluetooth LE link is closed")
        step = max(self.mtu_payload, 1)
        with self._send_lock:
            for i in range(0, len(data), step):
                chunk = data[i : i + step]
                args = GLib.Variant("(aya{sv})", (chunk, {"type": GLib.Variant("s", self.write_type)}))
                try:
                    self._call(self._write_path, CHAR_IFACE, "WriteValue", args)
                except _DBusFailure as e:
                    raise ConnectionError(f"Bluetooth LE write failed: {e}") from e

    def shutdown(self, how: int = socket.SHUT_RDWR) -> None:
        self._shut = True
        self._mark_closed()

    def close(self) -> None:
        with self._close_lock:
            already = self._released
            self._released = True
        self._shut = True
        self._mark_closed()
        if already:
            return
        if self._conn is not None and self._loop is not None:
            for path in self._notify_paths:
                try:
                    self._call(path, CHAR_IFACE, "StopNotify", timeout=1.0)
                except _DBusFailure:
                    pass  # the link may already be gone
            if self._we_connected and self._device_path:
                try:
                    self._call(self._device_path, DEVICE_IFACE, "Disconnect", timeout=2.0)
                except _DBusFailure:
                    pass
        if self._loop is not None:
            conn, subs = self._conn, self._subs
            self._loop.call(lambda: [conn.signal_unsubscribe(s) for s in subs])
            self._loop.stop()
            self._loop = None
        if self._conn is not None and self._own_conn:
            try:
                self._conn.close_sync(None)
            except GLib.Error:
                pass
        self._conn = None

    def __enter__(self) -> "GattSocket":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _DBusFailure(Exception):
    def __init__(self, message: str, cause: Any = None) -> None:
        super().__init__(message)
        self.cause = cause


def connect_ble(addr: str, service_uuid: str | None = None, timeout: float = CONNECT_TIMEOUT) -> GattSocket:
    """Open the control link over Bluetooth LE. Raises TransportError with a reason the user can act on."""
    return GattSocket(addr, service_uuid, timeout=timeout)
