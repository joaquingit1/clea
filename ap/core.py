"""Shared plumbing: paths, config, logging, subprocess, time windows."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Two places, one per question. CODE is the engine: this checkout, its prompts and its built UI. DATA is one install:
# its config, its state (var/), what it learned. The first install kept both in one directory and that still works
# (no CLEA_HOME: DATA is the checkout). A packaged install sets CLEA_HOME, so the engine can be replaced without
# touching anyone's state and two installs on one machine never share a database.
CODE = Path(os.environ.get("CLEA_CODE") or Path(__file__).resolve().parent.parent)
DATA = Path(os.environ.get("CLEA_HOME") or CODE).expanduser()
V1 = Path.home() / ".claude/autopilot"  # the first install's older tree (its Slack ids, slack.sh); absent anywhere else
HOME = CODE  # the name the modules import for the engine tree
VAR = DATA / "var"
PROMPTS = CODE / "prompts"
OPENCODE_DIR = CODE / "opencode"
ITEMS_DIR = VAR / "items"
LOG_DIR = VAR / "log"
RUNS = VAR / "runs"  # one file per live run() process group: who started it, so a dead process's groups can be reaped
# what an install learns or is told about its user: rule packs, knowledge, the style guides
RULES = DATA / "rules"
KNOWLEDGE = DATA / "knowledge"
STYLE = (DATA / "prompts" / "style") if DATA == CODE else (DATA / "style")

PATH_PREFIX = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def _deep(base: dict, over: dict) -> dict:
    """`over` on top of `base`: dicts merge key by key, anything else is replaced."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def _load_config() -> dict:
    """Engine defaults (ap/defaults.json), then the install's own config.json. The first install is 'internal': its
    config is the v1 file with the v2 one on top, exactly as before (v2 keys win; v1 supplies the Slack and squad
    identity it already had right), and the defaults only fill what neither names. Every other install is 'release':
    its config.json is written by the onboarding."""
    defaults = _read(CODE / "ap" / "defaults.json")
    own = _read(DATA / "config.json")
    legacy = _read(V1 / "config.json") if DATA == CODE else {}
    edition = own.get("edition") or ("internal" if legacy and own else "release")
    cfg = _deep(defaults, {**legacy, **own} if edition == "internal" else own)
    cfg["edition"] = edition
    return cfg


CFG = _load_config()


def reload_config() -> None:
    new = _load_config()  # never clear first: readers in other threads would see a half-empty dict
    CFG.update(new)
    for k in [k for k in CFG if k not in new]:
        CFG.pop(k, None)


def _path(key: str, default: Path) -> Path:
    v = (CFG.get("paths") or {}).get(key)
    return Path(v).expanduser() if v else default


# where the harness builds: one git worktree per case, beside a read-only mirror of each default branch
WORKTREES = _path("worktrees", DATA / "worktrees")

for d in (VAR, ITEMS_DIR, LOG_DIR, WORKTREES, RUNS):
    d.mkdir(parents=True, exist_ok=True)


def save_config(patch: dict, replace: tuple = ()) -> dict:
    """Merge `patch` into the install's config.json (atomically) and into the live CFG. The onboarding and the settings
    screen write through here; nothing else edits the file while the worker runs. Keys in `replace` are a selection
    (the repos, the teammates): the saved value takes the old one's place, so what was deselected is gone."""
    path = DATA / "config.json"
    own = _read(path)
    for k in replace:
        own.pop(k, None)
    own = _deep(own, patch)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(own, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    reload_config()
    return own


_log_lock = threading.Lock()


def log(msg: str, **kv) -> None:
    line = f"{dt.datetime.now():%F %T} [{threading.current_thread().name}] {msg}"
    if kv:
        line += " " + json.dumps(kv, ensure_ascii=False, default=str)
    with _log_lock:
        try:
            with (LOG_DIR / f"ap-{dt.date.today():%F}.log").open("a") as f:
                f.write(line + "\n")
        except OSError:  # out of file descriptors: a log line must never be what kills its caller
            print(line, file=sys.stderr, flush=True)


def _docker_env() -> dict:
    """Docker without Docker Desktop's app container: our own DOCKER_CONFIG (no `credsStore:
    desktop`, whose helper reads Group Containers/group.com.docker on every pull and, under
    launchd, makes macOS ask "Python would like to access data from other apps" each time)
    and the engine socket addressed directly (no context lookup)."""
    cfg_dir = VAR / "docker-config"
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = cfg_dir / "config.json"
        want = {"auths": {}, "cliPluginsExtraDirs": [str(Path.home() / ".docker/cli-plugins"), "/Applications/Docker.app/Contents/Resources/cli-plugins"]}  # compose/desktop plugins (see dtest.isolated_docker_env)
        try:
            have = json.loads(cfg.read_text()) if cfg.exists() else {}
        except Exception:
            have = {}
        if have != {**have, **want}:
            cfg.write_text(json.dumps({**have, **want}, indent=1))
    except Exception:
        return {}
    out = {"DOCKER_CONFIG": str(cfg_dir)}
    sock = Path.home() / ".docker/run/docker.sock"
    if sock.exists():
        out["DOCKER_HOST"] = f"unix://{sock}"
    return out


def env(extra: dict | None = None) -> dict:
    """The environment of a child process. A None value removes the variable: a provider that bills the user's plan
    must not inherit an API key that would bill something else, and the other way round."""
    e = {**os.environ, "PATH": PATH_PREFIX + ":" + os.environ.get("PATH", ""), **_docker_env()}
    for k, v in (extra or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


ACTIVE: dict = {}  # thread name -> (pid, started, cmd): what the reaper may kill (ap/hot.py)
ENGINE_CMDS = ("claude ", "codex ")  # a model session, whichever engine runs it: parked when orphaned, never reaped as a tool


def run(cmd: list[str], cwd: str | Path | None = None, timeout: int = 600,
        input: str | None = None, extra_env: dict | None = None, check: bool = False,
        tag: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command with stdin closed unless input is given (opencode run blocks
    forever reading an open stdin; verified 2026-09-23). `tag` goes into the run registry (a claude session's item)."""
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, text=True,
                            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env(extra_env), start_new_session=True)
    ACTIVE[threading.current_thread().name] = (proc.pid, time.time(), " ".join(cmd)[:120])
    mark = RUNS / str(proc.pid)
    try:
        mark.write_text(json.dumps({"by": os.getpid(), "cmd": " ".join(cmd)[:200], "cwd": str(cwd or ""),
                                    "at": time.time(), "timeout": timeout, **(tag or {})}))
    except OSError:
        pass
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:  # kill the whole group: a timed-out git or curl must not leave children behind
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
        proc.communicate()
        raise
    finally:
        mark.unlink(missing_ok=True)
    ACTIVE.pop(threading.current_thread().name, None)
    p = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed rc={p.returncode}: {(p.stderr or p.stdout)[-2000:]}")
    return p


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def reap_orphans(rows: list) -> list[str]:
    """Kill the process groups run() started for a process that is gone. Each child gets its own session, so a worker
    restart does not take them along: on 10-07 at 14:00 four dashboard `tsc --noEmit` trees left by the 09:03, 09:21,
    13:23 and 13:30 workers still ran under launchd, each thrashing (15M page faults, 0.05 cores) next to the live
    gates, and from 12:25 to 13:11 every Claude call, gate and git command on the machine timed out. `rows` are ps
    (pid, ppid, etime, command): a group is killed only while its leader is orphaned and still runs that command, so a
    reused pid is never hit."""
    leaders = {r[0]: r for r in rows}
    notes = []
    for f in RUNS.glob("*"):
        try:
            meta = json.loads(f.read_text())
            pgid, owner = int(f.name), int(meta["by"])
        except (OSError, ValueError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        if owner == os.getpid() or _alive(owner) or meta["cmd"].startswith(ENGINE_CMDS):
            continue  # orphaned sessions are worker.park_orphan_sessions': a code one is parked, not killed
        row = leaders.get(str(pgid))
        if row and row[1] == "1" and meta["cmd"].split(" ", 1)[-1][:60] in row[3]:
            try:
                os.killpg(pgid, signal.SIGKILL)
                notes.append((f"maté herramienta huérfana {pgid}: " if (CFG.get("language") or "en")[:2] == "es"
                              else f"killed an orphaned tool {pgid}: ") + meta['cmd'][:60])
            except OSError:
                pass
        f.unlink(missing_ok=True)
    return notes


def now() -> int:
    return int(time.time())


def work_hours(t: dt.datetime | None = None) -> bool:
    t = t or dt.datetime.now()
    wh = CFG["schedule"]["work_hours"]
    return t.isoweekday() in wh["days"] and wh["start"] <= t.hour < wh["end"]


def slug(text: str, maxlen: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].rstrip("-") or "item"


def item_dir(item_id: int) -> Path:
    d = ITEMS_DIR / str(item_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def extract_json(text: str):
    """Last JSON object/array in a model reply (fenced or bare)."""
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for cand in reversed(fences):
        try:
            return json.loads(cand)
        except Exception:
            pass
    # scan for the last balanced {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        end = text.rfind(closer)
        while end != -1:
            depth = 0
            for i in range(end, -1, -1):
                if text[i] == closer:
                    depth += 1
                elif text[i] == opener:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:end + 1])
                        except Exception:
                            break
            end = text.rfind(closer, 0, end)
    raise ValueError("no JSON found in model output")


def check_schema(obj, schema: dict, path: str = "$") -> list[str]:
    """Small JSON-schema subset validator (type/required/properties/items/enum)."""
    errs: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool}
    if t:
        allowed = t if isinstance(t, list) else [t]
        if not any((x == "null" and obj is None) or (x in types and isinstance(obj, types[x])
                   and not (x in ("integer", "number") and isinstance(obj, bool))) for x in allowed):
            return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, dict):
        for k in schema.get("required", []):
            if k not in obj:
                errs.append(f"{path}: missing '{k}'")
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                errs += check_schema(obj[k], sub, f"{path}.{k}")
    if isinstance(obj, list) and "items" in schema:
        for i, el in enumerate(obj):
            errs += check_schema(el, schema["items"], f"{path}[{i}]")
    return errs


def read_prompt(rel: str, **subs) -> str:
    text = (PROMPTS / rel).read_text()
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text
