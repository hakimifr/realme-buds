"""Keeps a connection to the buds alive and runs commands on it, for long-lived front ends."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import profiles
from .client import Buds, State, Trace
from .sim import FakeBuds, connected_pair
from .transport import connect_rfcomm, pick_device


def connect_buds(
    addr: str | None = None,
    channel: int | None = None,
    sim: bool = False,
    trace: Trace | None = None,
    profile: str | None = None,
    profile_file: str | None = None,
    sim_profile: str | None = None,
) -> Buds:
    """Open the link, start the reader and run the init sequence.

    ``profile`` forces a profile by key (or ``profile_file`` a JSON file) instead of recognising
    the buds. With ``sim`` the buds are pretend; the simulator is reachable as ``buds.sim``
    and ``sim_profile`` makes it a different model.
    """
    fake: FakeBuds | None = None
    name: str | None = None
    if sim:
        sock, fake = connected_pair()
        if sim_profile:
            fake.profile = profiles.get(sim_profile)
        name = fake.profile.name
    else:
        dev = pick_device(addr)
        name = dev.name if dev.name != dev.addr else None
        sock = connect_rfcomm(dev.addr, channel)
    chosen = None
    if profile_file:
        chosen = profiles.load_file(Path(profile_file))
    elif profile:
        chosen = profiles.get(profile)
    buds = Buds(sock, trace=trace, init_delay=0.05 if sim else 0.2, profile=chosen, device_name=name)
    buds.sim = fake  # type: ignore[attr-defined]
    buds.start()
    try:
        buds.initialise()
    except BaseException:
        buds.close()
        raise
    return buds


StatusCallback = Callable[[str, str], None]
EventCallback = Callable[[str, State], None]
DoneCallback = Callable[[Any, "Exception | None"], None]


class Session:
    """Background connection manager.

    Callbacks run on the session's own thread; a GUI must hop to its main loop.
    Jobs submitted with :meth:`submit` run one at a time, in order, on that thread,
    so UI actions never interleave on the wire.
    """

    def __init__(
        self,
        connector: Callable[[], Buds],
        on_status: StatusCallback,
        on_event: EventCallback,
        retry_delay: float = 3.0,
        poll_interval: float = 4.0,
    ) -> None:
        self._connector = connector
        self._poll_interval = poll_interval
        self._on_status = on_status
        self._on_event = on_event
        self._retry_delay = retry_delay
        self._jobs: queue.Queue[tuple[Callable[[Buds], Any], DoneCallback | None]] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="buds-session", daemon=True)
        self.buds: Buds | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def reconnect(self) -> None:
        """Drop the current link; the loop connects again (used after changing the profile or address)."""
        buds = self.buds
        if buds is not None:
            buds.close()

    def submit(self, fn: Callable[[Buds], Any], on_done: DoneCallback | None = None) -> None:
        self._jobs.put((fn, on_done))

    def _fail_pending(self, why: str) -> None:
        while True:
            try:
                _, done = self._jobs.get_nowait()
            except queue.Empty:
                return
            if done:
                done(None, RuntimeError(why))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._on_status("connecting", "")
            self._fail_pending("not connected")
            buds = None
            try:
                buds = self._connector()
                buds.add_listener(self._on_event)
                buds.sync()
            except Exception as e:  # any failure here means "try again shortly"
                if buds is not None:
                    buds.close()
                self._on_status("error", str(e) or e.__class__.__name__)
                if self._stop.wait(self._retry_delay):
                    return
                continue
            self.buds = buds
            self._on_status("connected", "")
            self._on_event("synced", buds.state)
            last_poll = time.monotonic()
            while buds.state.connected and not self._stop.is_set():
                try:
                    fn, done = self._jobs.get(timeout=0.2)
                except queue.Empty:
                    # Some buds never push noise-control changes, so ask now and then.
                    if buds.needs_polling and time.monotonic() - last_poll >= self._poll_interval:
                        last_poll = time.monotonic()
                        try:
                            buds.poll()
                        except Exception:
                            pass  # a failed poll must not end the session; a dead link ends the loop
                    continue
                try:
                    result, error = fn(buds), None
                except Exception as e:
                    result, error = None, e
                if done:
                    done(result, error)
            self.buds = None
            buds.close()
            if self._stop.is_set():
                return
            self._on_status("disconnected", "The buds closed the connection (case lid, or they moved to another device).")
            if self._stop.wait(self._retry_delay):
                return
