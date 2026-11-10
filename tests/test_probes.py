"""Probes: the answers the setup screen is built from are served from memory, and refreshed behind the reader."""
from __future__ import annotations

import threading
import time

from ap.core import Probes


def test_the_first_read_waits_and_later_reads_never_do():
    p, calls, gate = Probes(), [], threading.Event()

    def probe():
        calls.append(time.time())
        if len(calls) > 1:
            gate.wait(5)  # a slow refresh: a reader must not wait for it
        return len(calls)

    assert p.get("gh", 60, probe) == 1
    assert p.get("gh", 60, probe) == 1 and len(calls) == 1  # fresh: no probe at all
    p.vals["gh"] = (0, 1)  # now older than its age
    t = time.time()
    assert p.get("gh", 60, probe) == 1  # the last answer, at once
    assert p.get("gh", 60, probe) == 1  # and one refresh in flight, not two
    assert time.time() - t < 1
    gate.set()
    for _ in range(100):
        if p.get("gh", 60, probe) == 2:
            break
        time.sleep(0.02)
    assert p.get("gh", 60, probe) == 2 and len(calls) == 2


def test_a_clear_discards_a_refresh_that_was_already_running():
    p, gate = Probes(), threading.Event()
    p.vals["gh"] = (0, "signed out")

    def probe():
        gate.wait(5)
        return "signed out"  # read before the user signed in

    assert p.get("gh", 60, probe) == "signed out"
    p.clear()  # the user signed in: what is known is about the old state
    gate.set()
    time.sleep(0.1)
    assert p.get("gh", 60, lambda: "signed in") == "signed in"


def test_a_failed_refresh_keeps_the_last_answer():
    p = Probes()
    p.vals["gh"] = (0, "ok")

    def boom():
        raise OSError("gh is gone")

    assert p.get("gh", 60, boom) == "ok"
    time.sleep(0.1)
    assert p.get("gh", 60, lambda: "back") == "ok"  # still served, and this read starts the next refresh


def test_two_first_readers_share_one_probe():
    p, calls, gate, got = Probes(), [], threading.Event(), []

    def probe():
        calls.append(1)
        gate.wait(5)
        return "v"

    ts = [threading.Thread(target=lambda: got.append(p.get("gh", 60, probe))) for _ in range(3)]
    for t in ts:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in ts:
        t.join(5)
    assert got == ["v", "v", "v"] and len(calls) == 1
