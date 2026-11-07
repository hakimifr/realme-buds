import threading
import time

from realmebuds import model
from realmebuds.session import Session, connect_buds


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make_session(connector=None, retry=0.1, poll=4.0):
    statuses, events = [], []
    s = Session(connector or (lambda: connect_buds(sim=True)),
                lambda st, msg: statuses.append((st, msg)),
                lambda ev, state: events.append(ev), retry_delay=retry, poll_interval=poll)
    return s, statuses, events


def test_connects_syncs_and_runs_jobs_in_order():
    s, statuses, events = make_session()
    s.start()
    try:
        assert wait_for(lambda: ("connected", "") in statuses)
        assert "synced" in events
        results = []
        done = threading.Event()
        s.submit(lambda b: b.set_anc(model.ANC_MAX), lambda r, e: results.append(("anc", e)))
        s.submit(lambda b: b.set_feature(0x06, True), lambda r, e: results.append(("game", e)))
        s.submit(lambda b: b.state.anc, lambda r, e: (results.append(("read", r)), done.set()))
        assert done.wait(5)
        assert results == [("anc", None), ("game", None), ("read", model.ANC_MAX)]
    finally:
        s.stop()


def test_job_errors_are_delivered_not_raised():
    s, statuses, _ = make_session()
    s.start()
    try:
        assert wait_for(lambda: ("connected", "") in statuses)
        got = []
        s.submit(lambda b: b.set_feature(0x30, True), lambda r, e: got.append(e))
        assert wait_for(lambda: got)
        assert got[0] is not None and "refused" in str(got[0])
    finally:
        s.stop()


def test_reconnects_after_the_link_drops():
    s, statuses, _ = make_session()
    s.start()
    try:
        assert wait_for(lambda: s.buds is not None)
        first = s.buds
        first.sim.drop_link()
        assert wait_for(lambda: any(st == "disconnected" for st, _ in statuses))
        assert wait_for(lambda: s.buds is not None and s.buds is not first)
        assert statuses[-1][0] == "connected"
    finally:
        s.stop()


def test_connection_errors_are_reported_and_retried():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("no route to buds")
        return connect_buds(sim=True)

    s, statuses, _ = make_session(flaky)
    s.start()
    try:
        assert wait_for(lambda: ("connected", "") in statuses)
        errors = [m for st, m in statuses if st == "error"]
        assert errors == ["no route to buds", "no route to buds"]
    finally:
        s.stop()


def test_session_polls_when_the_buds_do_not_push_noise_control():
    s, statuses, _ = make_session(poll=0.2)
    s.start()
    try:
        assert wait_for(lambda: s.buds is not None)
        s.buds.sim.anc = model.ANC_MAX  # changed on the buds, no push
        assert wait_for(lambda: s.buds.state.anc == model.ANC_MAX)
    finally:
        s.stop()
