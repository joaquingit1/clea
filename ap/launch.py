"""`clea run`: one install, kept running. Starts the install's guard and its worker as two processes and restarts
either when it stops, so the app (or a login item) has one thing to start and nothing to babysit.

Two processes because they must not be one: the guard holds the credentials that write in the user's name and checks
every request against the policy; the worker thinks, and asks. The worker replaces itself when setup finishes or its
code asks for a graceful restart (it exits 0), and that is the normal way for it to stop: it comes straight back.
The guard only starts once the onboarding wrote its policy, and is restarted when the user changes who they are
signed in as (it refuses a policy that names another user)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from .core import CODE, DATA, VAR, log

GUARD = CODE / "guard" / "ap_guard.py"
POLICY = DATA / "guard" / "policy.json"
BACKOFF = (1, 2, 5, 15, 30)


class Child:
    def __init__(self, name: str, cmd: list[str], ready=lambda: True):
        self.name, self.cmd, self.ready = name, cmd, ready
        self.p: subprocess.Popen | None = None
        self.fails, self.next_at, self.started = 0, 0.0, 0.0

    def tick(self) -> None:
        if self.p is not None:
            rc = self.p.poll()
            if rc is None:
                return
            ran = time.time() - self.started
            # an exit after a real run starts the count again; quick repeated exits back off instead of spinning
            self.fails = 0 if (rc == 0 or ran > 120) else self.fails + 1
            self.next_at = time.time() + (0.5 if rc == 0 else BACKOFF[min(self.fails, len(BACKOFF)) - 1])
            log(f"launch: {self.name} stopped", rc=rc, ran_s=round(ran), again_in=round(self.next_at - time.time(), 1))
            self.p = None
        if time.time() < self.next_at or not self.ready():
            return
        out = open(VAR / "log" / f"{self.name}.out", "a")
        self.p = subprocess.Popen(self.cmd, cwd=str(CODE), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                  env={**os.environ, "CLEA_HOME": str(DATA), "PYTHONUNBUFFERED": "1"})
        self.started = time.time()
        log(f"launch: {self.name} started", pid=self.p.pid)

    def stop(self) -> None:
        if self.p is not None and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.p.kill()


def main() -> None:
    (VAR / "log").mkdir(parents=True, exist_ok=True)
    kids = [Child("guard", [sys.executable, str(GUARD)], ready=POLICY.exists),
            Child("worker", [sys.executable, "-m", "ap.cli", "worker"])]
    stop = {"now": False}

    def on_signal(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log("launch: running", home=str(DATA), code=str(CODE))
    try:
        while not stop["now"]:
            for k in kids:
                k.tick()
            time.sleep(1)
    finally:
        for k in reversed(kids):
            k.stop()
        log("launch: stopped")
