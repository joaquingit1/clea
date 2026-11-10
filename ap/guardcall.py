"""The harness's only way to write outside this machine: a request to the outbound guard (~/.claude/autopilot/guard,
installed root-owned at /usr/local/ap-guard). The guard enforces the user's policy (what needs his ✅, what is
pre-approved) and holds the credentials; this module has none. The harness may rewrite itself freely, but not this
boundary: the guard's tripwire stops the worker if harness code grows a direct write path."""
from __future__ import annotations

import json
import os
import socket

LEGACY_SOCKS = ("/var/run/ap-guard.sock", "/tmp/ap-guard.sock")


def socks() -> tuple:
    """Where this install's guard listens. Each install has its own guard, holding its own user's credentials: an
    install must never reach another's (a second install on the same machine would post as the first one's user).
    The first install's guard predates this and keeps its two system-wide paths."""
    from . import site
    from .core import VAR
    if os.environ.get("CLEA_GUARD_SOCK"):
        return (os.environ["CLEA_GUARD_SOCK"],)
    return LEGACY_SOCKS if site.internal() else (str(VAR / "guard.sock"),)


class GuardDenied(RuntimeError):
    pass


def act(action: str, **payload) -> dict:
    last = None
    for path in socks():
        if not os.path.exists(path):
            continue
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(180)
            s.connect(path)
            s.sendall((json.dumps({"action": action, "payload": payload}, ensure_ascii=False) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            s.close()
            resp = json.loads(buf.decode())
            if not resp.get("ok"):
                err = resp.get("error", "")
                raise (GuardDenied if err.startswith("guard:") else RuntimeError)(err)
            return resp.get("result") or {}
        except (ConnectionRefusedError, FileNotFoundError) as e:
            last = e
            continue
    raise RuntimeError(f"outbound guard not running ({last})")
