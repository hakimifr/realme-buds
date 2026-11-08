"""A mock of the parts of BlueZ's D-Bus API that realmebuds.ble uses, served on a private bus.

``PrivateBus`` runs a throwaway ``dbus-daemon``. ``FakeBluez`` owns ``org.bluez`` on it and exports
one device with GATT services and characteristics, an ObjectManager at "/" and the Properties
interface, like bluetoothd does. Writes to the write characteristic are recorded and handed to a
``realmebuds.sim.FakeBuds``; what the fake buds say comes back as PropertiesChanged(Value) signals
on the notifying characteristic, cut into chunks of ``notify_chunk`` bytes.

What this proves: our code uses the BlueZ API in a way that is consistent with its documented
behaviour (object paths, signatures, StartNotify before notifications, write types and the ATT
payload limit for write-without-response). What it cannot prove: that a real bluetoothd or real
buds behave like this mock. See docs/PROTOCOL.md.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from realmebuds.sim import FakeBuds  # noqa: E402

DEFAULT_SERVICE = "0000079a-d102-11e1-9b23-00025b00a5a5"
WRITE_CHAR = "0100079a-d102-11e1-9b23-00025b00a5a5"
NOTIFY_CHAR = "0200079a-d102-11e1-9b23-00025b00a5a5"
DEFAULT_LAYOUT = [(DEFAULT_SERVICE, [(WRITE_CHAR, ["write", "write-without-response"]), (NOTIFY_CHAR, ["notify"])])]

ADAPTER = "/org/bluez/hci0"


def have_dbus_daemon() -> bool:
    return shutil.which("dbus-daemon") is not None


class PrivateBus:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        assert self.proc.stdout is not None
        self.address = self.proc.stdout.readline().strip()
        if not self.address:
            self.stop()
            raise RuntimeError("dbus-daemon printed no address")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.proc.stdout:
            self.proc.stdout.close()


def _device_xml() -> str:
    return """<node><interface name="org.bluez.Device1">
      <method name="Connect"/><method name="Disconnect"/>
      <property name="Address" type="s" access="read"/>
      <property name="Name" type="s" access="read"/>
      <property name="Paired" type="b" access="read"/>
      <property name="Connected" type="b" access="read"/>
      <property name="ServicesResolved" type="b" access="read"/>
    </interface></node>"""


def _service_xml() -> str:
    return """<node><interface name="org.bluez.GattService1">
      <property name="UUID" type="s" access="read"/>
      <property name="Primary" type="b" access="read"/>
      <property name="Device" type="o" access="read"/>
    </interface></node>"""


def _char_xml(with_mtu: bool) -> str:
    mtu = '<property name="MTU" type="q" access="read"/>' if with_mtu else ""
    return f"""<node><interface name="org.bluez.GattCharacteristic1">
      <method name="ReadValue"><arg type="a{{sv}}" direction="in"/><arg type="ay" direction="out"/></method>
      <method name="WriteValue"><arg type="ay" direction="in"/><arg type="a{{sv}}" direction="in"/></method>
      <method name="StartNotify"/><method name="StopNotify"/>
      <property name="UUID" type="s" access="read"/>
      <property name="Service" type="o" access="read"/>
      <property name="Value" type="ay" access="read"/>
      <property name="Notifying" type="b" access="read"/>
      <property name="Flags" type="as" access="read"/>
      {mtu}
    </interface></node>"""


_OBJMGR_XML = """<node><interface name="org.freedesktop.DBus.ObjectManager">
  <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
</interface></node>"""


class _Char:
    def __init__(self, path: str, service_path: str, uuid: str, flags: list[str]) -> None:
        self.path, self.service_path, self.uuid, self.flags = path, service_path, uuid, flags
        self.notifying = False


class FakeBluez:
    """One earbud as BlueZ shows it.

    Knobs (set them before or while a test runs): ``connect_error`` makes Connect() fail with that
    (name, message), ``resolve_delay`` delays ServicesResolved after Connect, ``never_resolve``
    keeps it false, ``mtu`` exposes the characteristic MTU property, ``att_payload`` is the longest
    write-without-response BlueZ accepts, ``notify_chunk`` how a notification is cut.
    """

    def __init__(
        self,
        bus_address: str,
        addr: str = "AA:BB:CC:DD:EE:FF",
        *,
        layout: list[tuple[str, list[tuple[str, list[str]]]]] | None = None,
        connected: bool = False,
        attach_buds: bool = True,
        mtu: int | None = None,
        att_payload: int = 20,
        notify_chunk: int = 20,
        resolve_delay: float = 0.0,
        connect_error: tuple[str, str] | None = None,
    ) -> None:
        self.bus_address = bus_address
        self.addr = addr
        self.layout = DEFAULT_LAYOUT if layout is None else layout
        self.mtu = mtu
        self.att_payload = att_payload
        self.notify_chunk = notify_chunk
        self.resolve_delay = resolve_delay
        self.connect_error = connect_error
        self.never_resolve = False
        self.fail_writes: tuple[str, str] | None = None
        self.dev_path = f"{ADAPTER}/dev_{addr.replace(':', '_')}"
        self.lock = threading.RLock()
        self.connected = connected
        self.resolved = connected
        self.chars: dict[str, _Char] = {}
        self.services: dict[str, str] = {}  # path -> uuid
        n = 1
        for service_uuid, chars in self.layout:
            spath = f"{self.dev_path}/service{n:04x}"
            self.services[spath] = service_uuid
            n += 1
            for char_uuid, flags in chars:
                cpath = f"{spath}/char{n:04x}"
                self.chars[cpath] = _Char(cpath, spath, char_uuid, flags)
                n += 1
        # What happened, for assertions.
        self.writes: list[tuple[str, bytes, str]] = []  # (characteristic uuid, value, write type)
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.start_notify_calls: list[str] = []
        self.stop_notify_calls: list[str] = []
        self.managed_calls = 0

        self.buds: FakeBuds | None = None
        self._bridge: socket.socket | None = None
        if attach_buds:
            self._bridge, theirs = socket.socketpair()
            self.buds = FakeBuds(theirs)
        self._buds_started = False
        self._conn: Any = None
        self._loop: Any = None
        self._ctx: Any = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._ids: list[int] = []

    # -- life cycle -----------------------------------------------------------------

    def start(self) -> "FakeBluez":
        ready = threading.Event()
        errors: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, args=(ready, errors), name="fake-bluez", daemon=True)
        self._thread.start()
        if not ready.wait(5):
            raise RuntimeError("fake BlueZ did not start")
        if errors:
            raise errors[0]
        return self

    def stop(self) -> None:
        self._stopping = True
        if self.buds is not None:
            self.buds.close()
        if self._bridge is not None:
            try:
                self._bridge.close()
            except OSError:
                pass
        if self._loop is not None:
            self._loop.quit()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._conn is not None:
            try:
                self._conn.close_sync(None)
            except GLib.Error:
                pass

    def __enter__(self) -> "FakeBluez":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self, ready: threading.Event, errors: list[BaseException]) -> None:
        ctx = self._ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            self._conn = Gio.DBusConnection.new_for_address_sync(
                self.bus_address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None,
            )
            self._register()
            self._conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                GLib.Variant("(su)", ("org.bluez", 0)), GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 2000, None,
            )
            self._loop = GLib.MainLoop.new(ctx, False)
        except BaseException as e:
            errors.append(e)
            ready.set()
            return
        ready.set()
        self._loop.run()
        ctx.pop_thread_default()

    def _register(self) -> None:
        conn = self._conn
        reg = lambda path, xml, name: conn.register_object(  # noqa: E731
            path, Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(name), self._on_method, self._on_get, None
        )
        reg("/", _OBJMGR_XML, "org.freedesktop.DBus.ObjectManager")
        reg(self.dev_path, _device_xml(), "org.bluez.Device1")
        for path in self.services:
            reg(path, _service_xml(), "org.bluez.GattService1")
        for path in self.chars:
            reg(path, _char_xml(self.mtu is not None), "org.bluez.GattCharacteristic1")

    # -- state as D-Bus properties --------------------------------------------------------

    def _props(self, path: str, iface: str) -> dict[str, Any]:
        V = GLib.Variant
        if iface == "org.bluez.Device1":
            return {
                "Address": V("s", self.addr), "Name": V("s", "realme Buds Fake"), "Paired": V("b", True),
                "Connected": V("b", self.connected), "ServicesResolved": V("b", self.resolved),
            }
        if iface == "org.bluez.GattService1":
            return {"UUID": V("s", self.services[path]), "Primary": V("b", True), "Device": V("o", self.dev_path)}
        c = self.chars[path]
        props = {
            "UUID": V("s", c.uuid), "Service": V("o", c.service_path), "Value": V("ay", b""),
            "Notifying": V("b", c.notifying), "Flags": V("as", c.flags),
        }
        if self.mtu is not None:
            props["MTU"] = V("q", self.mtu)
        return props

    def _on_get(self, _conn, _sender, path, iface, prop) -> Any:
        with self.lock:
            return self._props(path, iface)[prop]

    def _managed(self) -> dict[str, dict[str, dict[str, Any]]]:
        objects: dict[str, dict[str, dict[str, Any]]] = {
            self.dev_path: {"org.bluez.Device1": self._props(self.dev_path, "org.bluez.Device1")}
        }
        if self.resolved:
            for path in self.services:
                objects[path] = {"org.bluez.GattService1": self._props(path, "org.bluez.GattService1")}
            for path in self.chars:
                objects[path] = {"org.bluez.GattCharacteristic1": self._props(path, "org.bluez.GattCharacteristic1")}
        return objects

    def _emit_changed(self, path: str, iface: str, changed: dict[str, Any]) -> None:
        if self._conn is None or self._stopping:
            return
        try:
            self._conn.emit_signal(
                None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (iface, changed, [])),
            )
        except GLib.Error:
            pass

    # -- method calls ---------------------------------------------------------------------

    def _on_method(self, _conn, _sender, path, iface, method, params, invocation) -> None:
        try:
            self._dispatch(path, iface, method, params, invocation)
        except Exception as e:  # a bug in the mock must show up as a failing call, not a hang
            invocation.return_dbus_error("org.bluez.Error.Failed", f"fake bluez bug: {e!r}")

    def _dispatch(self, path: str, iface: str, method: str, params: Any, invocation: Any) -> None:
        err = invocation.return_dbus_error
        if iface == "org.freedesktop.DBus.ObjectManager":
            self.managed_calls += 1
            with self.lock:
                managed = self._managed()
            invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", (managed,)))
        elif iface == "org.bluez.Device1" and method == "Connect":
            self.connect_calls += 1
            if self.connect_error:
                err(*self.connect_error)
                return
            self._set_connected(True)
            invocation.return_value(None)
        elif iface == "org.bluez.Device1" and method == "Disconnect":
            self.disconnect_calls += 1
            self._set_connected(False)
            invocation.return_value(None)
        elif iface == "org.bluez.GattCharacteristic1":
            self._characteristic_call(self.chars[path], method, params, invocation)
        else:
            err("org.bluez.Error.NotSupported", f"{iface}.{method}")

    def _set_connected(self, on: bool) -> None:
        with self.lock:
            self.connected = on
            if not on:
                self.resolved = False
                for c in self.chars.values():
                    c.notifying = False
        self._emit_changed(self.dev_path, "org.bluez.Device1", {"Connected": GLib.Variant("b", on)})
        if on and not self.never_resolve:
            if self.resolve_delay:
                source = GLib.timeout_source_new(int(self.resolve_delay * 1000))
                source.set_callback(lambda *_: self._resolve())
                source.attach(self._ctx)
            else:
                self._resolve()
        elif not on:
            self._emit_changed(self.dev_path, "org.bluez.Device1", {"ServicesResolved": GLib.Variant("b", False)})

    def _resolve(self) -> bool:
        with self.lock:
            if not self.connected:
                return False
            self.resolved = True
        self._emit_changed(self.dev_path, "org.bluez.Device1", {"ServicesResolved": GLib.Variant("b", True)})
        return False

    def _characteristic_call(self, c: _Char, method: str, params: Any, invocation: Any) -> None:
        err = invocation.return_dbus_error
        if not self.connected:
            err("org.bluez.Error.NotConnected", "Not connected")
            return
        if method == "StartNotify":
            self.start_notify_calls.append(c.uuid)
            if not set(c.flags) & {"notify", "indicate"}:
                err("org.bluez.Error.NotSupported", "Operation is not supported")
                return
            c.notifying = True
            invocation.return_value(None)
            self._start_buds()
        elif method == "StopNotify":
            self.stop_notify_calls.append(c.uuid)
            c.notifying = False
            invocation.return_value(None)
        elif method == "WriteValue":
            value_list, options = params.unpack()
            value, kind = bytes(value_list), options.get("type", "command")
            # bluetoothd's default type is a request when the characteristic supports it.
            wanted = "write-without-response" if kind == "command" else "write"
            if wanted not in c.flags:
                err("org.bluez.Error.NotSupported", "Operation is not supported")
                return
            if kind == "command" and len(value) > self.att_payload:
                err("org.bluez.Error.Failed", f"Invalid Length: a write without response carries at most {self.att_payload} bytes")
                return
            if self.fail_writes:
                err(*self.fail_writes)
                return
            self.writes.append((c.uuid, value, kind))
            if self._bridge is not None:
                try:
                    self._bridge.sendall(value)
                except OSError:
                    pass
            invocation.return_value(None)
        else:
            err("org.bluez.Error.NotSupported", method)

    # -- the fake buds on the other side of the characteristics ------------------------------

    def _start_buds(self) -> None:
        """The buds only talk once the host subscribed, like a real peripheral (no CCCD, no notifications)."""
        if self.buds is None or self._buds_started:
            return
        self._buds_started = True
        self.buds.start()
        threading.Thread(target=self._pump_buds, name="fake-bluez-bridge", daemon=True).start()

    def _pump_buds(self) -> None:
        assert self._bridge is not None
        while not self._stopping:
            try:
                data = self._bridge.recv(4096)
            except OSError:
                return
            if not data:
                return
            for i in range(0, len(data), self.notify_chunk):
                self.notify(data[i : i + self.notify_chunk])

    def notify(self, data: bytes, uuid: str = NOTIFY_CHAR, nth: int = 0) -> None:
        """Send one notification from the ``nth`` characteristic with this uuid (if it is subscribed)."""
        matches = [c for c in self.chars.values() if c.uuid == uuid and c.notifying]
        if len(matches) <= nth:
            return
        self.notify_on(matches[nth].path, data)

    def notify_on(self, path: str, data: bytes) -> None:
        self._emit_changed(path, "org.bluez.GattCharacteristic1", {"Value": GLib.Variant("ay", data)})

    # -- things that happen to the link ------------------------------------------------------

    def drop_link(self) -> None:
        """The buds go away (case lid closed): Connected turns false."""
        self._set_connected(False)

    def remove_device(self) -> None:
        """BlueZ forgets the device (InterfacesRemoved)."""
        self._conn.emit_signal(
            None, "/", "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved",
            GLib.Variant("(oas)", (self.dev_path, ["org.bluez.Device1"])),
        )

    def wait_for(self, cond, timeout: float = 3.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    def written(self) -> bytes:
        return b"".join(v for _, v, _ in self.writes)
