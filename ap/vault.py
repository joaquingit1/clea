"""Where an install keeps what must never sit in a config file, a log or a model's context: API keys and OAuth tokens.
The macOS Keychain when there is one (service 'clea-<name>', the login keychain of the user running Clea), else a
file only that user can read in the install's home. Values are stored base64-encoded, so a token set (JSON) or a key
with any character survives the `security` tool's quoting, and nothing here ever puts a secret on a command line,
where every process on the machine could read it."""
from __future__ import annotations

import base64
import getpass
import json
import os
import shutil
import subprocess
import threading

from .core import DATA

PREFIX = "b64:"
_lock = threading.Lock()


def _file() -> "os.PathLike":
    return DATA / "secrets.json"


def _use_file() -> bool:
    """A test install or a machine with no Keychain keeps its secrets in the home's own file."""
    return os.environ.get("CLEA_SECRETS") == "file" or not shutil.which("security")


def _service(name: str) -> str:
    return f"clea-{name}"


def _read_file() -> dict:
    try:
        return json.loads(_file().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _write_file(d: dict) -> None:
    path = _file()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _decode(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith(PREFIX):
        try:
            return base64.b64decode(raw[len(PREFIX):]).decode()
        except Exception:
            return ""
    return raw  # stored by hand with `security add-generic-password`


def get(name: str) -> str:
    """The secret, or '' when there is none. CLEA_SECRET_<NAME> in the environment wins (a server with no Keychain)."""
    env = os.environ.get("CLEA_SECRET_" + name.upper().replace("-", "_"))
    if env:
        return env
    if _use_file():
        return _decode(_read_file().get(name, ""))
    p = subprocess.run(["security", "find-generic-password", "-a", getpass.getuser(), "-s", _service(name), "-w"],
                       capture_output=True, text=True, timeout=30)
    return _decode(p.stdout) if p.returncode == 0 else ""


def put(name: str, value: str) -> None:
    enc = PREFIX + base64.b64encode(value.encode()).decode()
    with _lock:
        if _use_file():
            d = _read_file()
            d[name] = enc
            return _write_file(d)
        # through `security -i`, which reads the command from stdin: as an argument the value would be in the process list
        cmd = f'add-generic-password -a "{getpass.getuser()}" -s "{_service(name)}" -w "{enc}" -U\n'
        p = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=30)
        if p.returncode != 0 or get(name) != value:
            raise RuntimeError(f"could not store {name} in the Keychain: {(p.stderr or p.stdout).strip()[:200]}")


def delete(name: str) -> None:
    with _lock:
        if _use_file():
            d = _read_file()
            if d.pop(name, None) is not None:
                _write_file(d)
            return
        subprocess.run(["security", "delete-generic-password", "-a", getpass.getuser(), "-s", _service(name)],
                       capture_output=True, text=True, timeout=30)


def hint(value: str) -> str:
    """What a screen may show of a key: enough to recognise it, nothing a reader could use."""
    v = (value or "").strip()
    return f"{v[:7]}...{v[-4:]}" if len(v) > 16 else ("saved" if v else "")
