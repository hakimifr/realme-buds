"""Finding the buds and opening the control link, over RFCOMM or Bluetooth LE (BlueZ on Linux)."""

from __future__ import annotations

import re
import socket
import subprocess
from dataclasses import dataclass

from .protocol import SERVICE_UUIDS
from .sdp import SdpError, find_rfcomm_channel

ADDR_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class TransportError(Exception):
    pass


@dataclass
class Device:
    addr: str
    name: str
    paired: bool = False
    connected: bool = False
    has_control_service: bool = False

    def looks_like_realme_buds(self) -> bool:
        return self.has_control_service or "buds" in self.name.lower()


def _bluetoothctl(*args: str, timeout: float = 5.0) -> str:
    try:
        res = subprocess.run(
            ["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as e:
        raise TransportError("bluetoothctl not found (install the bluez package)") from e
    except subprocess.TimeoutExpired as e:
        raise TransportError(f"bluetoothctl {' '.join(args)} timed out") from e
    return res.stdout


def parse_device_list(text: str) -> list[tuple[str, str]]:
    """Parse ``bluetoothctl devices`` output into (address, name) pairs."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*Device\s+(\S+)\s+(.*)$", line)
        if m and ADDR_RE.match(m.group(1)):
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


def parse_device_info(text: str, addr: str, fallback_name: str = "") -> Device:
    """Parse ``bluetoothctl info <addr>`` output."""
    dev = Device(addr=addr.upper(), name=fallback_name)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("Alias:", "Name:")) and not dev.name:
            dev.name = line.split(":", 1)[1].strip()
        elif line.startswith("Paired:"):
            dev.paired = line.endswith("yes")
        elif line.startswith("Connected:"):
            dev.connected = line.endswith("yes")
        elif line.startswith("UUID:") and any(u in line.lower() for u in SERVICE_UUIDS):
            dev.has_control_service = True
    return dev


def list_devices() -> list[Device]:
    devices = []
    for addr, name in parse_device_list(_bluetoothctl("devices")):
        devices.append(parse_device_info(_bluetoothctl("info", addr), addr, name))
    return devices


def pick_device(addr: str | None = None) -> Device:
    """The buds to talk to: the one given, else the connected realme buds."""
    if addr:
        if not ADDR_RE.match(addr):
            raise TransportError(f"{addr!r} is not a Bluetooth address (AA:BB:CC:DD:EE:FF)")
        return Device(addr=addr.upper(), name=addr, paired=True, connected=True)
    devices = [d for d in list_devices() if d.looks_like_realme_buds()]
    connected = [d for d in devices if d.connected]
    if len(connected) == 1:
        return connected[0]
    if len(connected) > 1:
        names = ", ".join(f"{d.name} ({d.addr})" for d in connected)
        raise TransportError(f"several earbuds are connected ({names}); pass --addr")
    if devices:
        names = ", ".join(f"{d.name} ({d.addr})" for d in devices)
        raise TransportError(
            f"no earbuds are connected to this computer right now (known: {names}). "
            "The control channel only exists while they are connected here."
        )
    raise TransportError("no paired realme earbuds found; pair them with bluetoothctl first")


def connect_rfcomm(addr: str, channel: int | None = None, timeout: float = 8.0) -> socket.socket:
    """Open the control channel. ``channel`` skips the SDP lookup."""
    last_error: Exception | None = None
    channels: list[int] = []
    if channel is not None:
        channels = [channel]
    else:
        for uuid in SERVICE_UUIDS:
            try:
                channels.append(find_rfcomm_channel(addr, uuid, timeout=timeout))
                break
            except (SdpError, OSError) as e:
                last_error = e
        if not channels:
            raise TransportError(f"could not find the control service on {addr}: {last_error}") from last_error
    ch = channels[0]
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    try:
        sock.settimeout(timeout)
        sock.connect((addr, ch))
    except OSError as e:
        sock.close()
        raise TransportError(f"RFCOMM connect to {addr} channel {ch} failed: {e}") from e
    return sock


TRANSPORTS = ("auto", "rfcomm", "ble")


def connect(
    addr: str,
    transport: str = "auto",
    channel: int | None = None,
    service_uuid: str | None = None,
    timeout: float = 8.0,
):
    """Open the control link and return a socket-like object (``Buds`` only needs a few methods).

    ``rfcomm`` and ``ble`` force one transport. ``auto`` tries RFCOMM first (the SDP lookup says
    whether the buds have the service) and falls back to Bluetooth LE GATT if that cannot be done,
    reporting both reasons when neither works. ``channel`` only matters for RFCOMM and
    ``service_uuid`` only for BLE.
    """
    if transport not in TRANSPORTS:
        raise TransportError(f"unknown transport {transport!r}; use one of: {', '.join(TRANSPORTS)}")
    if transport == "rfcomm":
        return connect_rfcomm(addr, channel, timeout)
    if transport == "ble":
        return _connect_ble(addr, service_uuid)
    try:
        return connect_rfcomm(addr, channel, timeout)
    except TransportError as rfcomm_error:
        try:
            return _connect_ble(addr, service_uuid)
        except TransportError as ble_error:
            raise TransportError(
                f"could not open the control link to {addr} either way.\n"
                f"  RFCOMM (Bluetooth Classic): {rfcomm_error}\n"
                f"  Bluetooth LE GATT (experimental): {ble_error}"
            ) from ble_error


def _connect_ble(addr: str, service_uuid: str | None):
    from .ble import connect_ble  # imported late: it needs PyGObject, RFCOMM does not

    return connect_ble(addr, service_uuid)
