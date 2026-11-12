"""Tier 1: Muse Spark 1.3 (free contributor tier, max effort) through a persistent opencode server.

All scanning, triage, investigation, review-finding, planning, drafting, eval runs and
learning go through here. Cost is $0, so the design spends Muse calls freely (parallel
finder passes, repeated eval runs, self-consistency) and saves Claude for verification.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
import urllib.request
from base64 import b64encode

from . import db, site
from .core import CFG, HOME, LOG_DIR, OPENCODE_DIR, RULES, VAR, check_schema, env, extract_json, log, run

PW_FILE = VAR / "opencode.pw"
RULES_DIR = RULES  # the install's learned packs (the first install tracks them in git: every change is a commit)
RULES_DIR.mkdir(parents=True, exist_ok=True)

DATA_HOME = VAR / "opencode-data"  # own data dir: the shared ~/.local/share/opencode db had grown to 250MB
SERVER_ENV = {
    "OPENCODE_CONFIG_DIR": str(OPENCODE_DIR),
    "XDG_DATA_HOME": str(DATA_HOME),
    # The 54 Claude skills cost 15K tokens of prefix per call and are Claude-shaped.
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_SHARE": "1",
}

_sem = threading.BoundedSemaphore(1)      # low: squad-review finders, patrol, digest labels
_sem_mid = threading.BoundedSemaphore(2)  # intake: triage, backfill, context, investigate/plan
_sem_hi = threading.BoundedSemaphore(2)   # delivering the user's own PR work
_sem_triage = threading.BoundedSemaphore(1)  # intake classification never queues behind anything
_inflight = 0
_inflight_lock = threading.Lock()


def inflight() -> int:
    return _inflight
LOW_LANES = ("review-", "patrol", "digest", "learn", "judge", "eval-")
_prio = threading.local()
_server_lock = threading.Lock()


class MuseRateLimited(Exception):
    pass


class MuseError(Exception):
    pass


def _password() -> str:
    if not PW_FILE.exists():
        PW_FILE.write_text(secrets.token_urlsafe(24))
        PW_FILE.chmod(0o600)
    return PW_FILE.read_text().strip()


def _url() -> str:
    return f"http://127.0.0.1:{CFG['muse']['port']}"


def server_up() -> bool:
    req = urllib.request.Request(_url() + "/doc")
    req.add_header("Authorization", "Basic " + b64encode(f"opencode:{_password()}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def _prepare_data_home() -> None:
    import shutil
    from pathlib import Path
    src = Path.home() / ".local/share/opencode/auth.json"
    dst = DATA_HOME / "opencode" / "auth.json"
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(0o600)


def kill_server() -> None:
    """Stop our opencode serve and any `opencode run --attach` clients left on its port."""
    port = str(CFG["muse"]["port"])
    subprocess.run(["pkill", "-f", f"opencode run --attach {_url()}"], capture_output=True)
    subprocess.run(["pkill", "-f", f"opencode serve --port {port}"], capture_output=True)
    time.sleep(2)


def healthy(timeout: int = 90) -> bool:
    """A real round trip (tiny prompt), not just /doc: on 2026-09-23 the server answered /doc
    while every session hung for hours (event-listener leak after ~4h uptime)."""
    m = pick_model()
    if m is None:
        return True  # every model is in rate-limit backoff: the server is fine, the quotas are not
    if db.q1("SELECT 1 FROM calls WHERE tier='muse' AND ok=1 AND at>?", int(time.time()) - 300):
        return True  # real calls are completing: a busy server is not a dead one (a restart kills them all)
    if inflight() > 0:
        # calls in progress ARE the health signal; the client-side timeout kills a truly stuck one.
        #
        log("muse health: skipped, calls in flight", n=inflight())
        return True
    try:
        for attempt in range(2):
            p = _run_watched("ap-triage", m, VAR, "health", None, "Reply with exactly: ok", timeout)
            if '"type":"text"' in p.stdout.replace(" ", ""):
                return True
            sid = next((json.loads(l).get("sessionID") for l in p.stdout.splitlines() if l.startswith("{")), None)
            if sid and _session_texts(sid)[0]:
                return True
            log("muse health: no answer", attempt=attempt, rc=p.returncode, sid=sid, out=p.stdout[-200:], err=p.stderr[-200:])
            time.sleep(5)
        return False
    except _RateLimitedNow:
        pause_model(m)
        return True
    except subprocess.TimeoutExpired:
        log("muse health: tiny prompt timed out", timeout=timeout)
        return False


def restart_server(reason: str) -> None:
    with _server_lock:
        log("muse: restarting opencode serve", reason=reason)
        kill_server()
    ensure_server()
    db.kv_set("muse_restarts", (db.kv_get("muse_restarts", []) + [{"at": int(time.time()), "why": reason}])[-20:])


def ensure_server() -> None:
    with _server_lock:
        if server_up():
            return
        _prepare_data_home()
        log("muse: starting opencode serve")
        logf = open(LOG_DIR / "opencode-serve.log", "a")
        subprocess.Popen(
            ["opencode", "serve", "--port", str(CFG["muse"]["port"]), "--hostname", "127.0.0.1",
             "--print-logs", "--log-level", "WARN"],
            env=env({**SERVER_ENV, "OPENCODE_SERVER_PASSWORD": _password()}),
            stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True,
        )
        for _ in range(90):
            time.sleep(1)
            if server_up():
                return
        raise MuseError("opencode serve did not come up in 90s")


def rules_pack(lane: str) -> str:
    """Rules for the exact lane plus its family (review-correctness also gets review.md)."""
    out = []
    names = list(dict.fromkeys([lane.split("-")[0], lane]))
    db.touch_rules(names)
    for name in names:
        f = RULES_DIR / f"{name}.md"
        if f.exists() and f.read_text().strip():
            out.append(f.read_text().strip())
    return "\n".join(out)


class _RateLimitedNow(Exception):
    pass


def chain() -> list[str]:
    return CFG["muse"].get("chain") or [CFG["muse"]["model"]]


def pick_model() -> str | None:
    """First model of the free chain that is not in a rate-limit backoff."""
    for m in chain():
        if time.time() >= db.kv_get(f"model_paused:{m}", {}).get("until", 0):
            return m
    return None


def pause_model(m: str) -> None:
    """Exponential backoff per model: 10 min, doubling, capped at 2 h; reset after a success."""
    st = db.kv_get(f"model_paused:{m}", {})
    if st.get("until", 0) > time.time():
        return  # already paused by a concurrent call in the same burst: don't double the backoff
    wait = min(7200, max(600, st.get("wait", 300) * 2))
    db.kv_set(f"model_paused:{m}", {"until": time.time() + wait, "wait": wait})
    log("muse: model rate-limited, switching", model=m, backoff_s=wait)


def _run_watched(agent, model, cwd, lane, item_id, prompt, timeout):
    """opencode retries 429s silently inside the server, so the client just hangs. Watch the
    server's own log while the call runs and abort within seconds on 'Rate limit exceeded'."""
    logf = LOG_DIR / "opencode-serve.log"
    start = logf.stat().st_size if logf.exists() else 0
    args = ["opencode", "run", "--attach", _url(), "-p", _password(), "--agent", agent, "-m", model,
            "--format", "json", "--dir", str(cwd), "--title", f"ap:{lane}:{item_id or ''}"]
    if "muse-spark" in model:
        args += ["--variant", CFG["muse"]["variant"]]
    proc = subprocess.Popen(args + [prompt], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env())
    t0 = time.time()
    while True:
        try:
            out, err = proc.communicate(timeout=3)
            break
        except subprocess.TimeoutExpired:
            if logf.exists() and logf.stat().st_size > start:
                with logf.open() as fh:
                    fh.seek(start)
                    chunk = fh.read()
                prov, mid = model.split("/", 1)
                if any("Rate limit exceeded" in l and f"providerID={prov} " in l and f"modelID={mid}" in l
                       for l in chunk.splitlines()):
                    proc.kill()
                    proc.communicate()
                    raise _RateLimitedNow(model)
            if time.time() - t0 > timeout:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(args, timeout)
    if proc.returncode == 0:
        db.kv_set(f"model_paused:{model}", {})
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _session_texts(session_id: str) -> tuple[list[str], int, int, list[str]]:
    """Read a finished session from the server. `opencode run --format json` sometimes exits
    right after step_start without printing the answer, so the server's copy is the source of truth."""
    req = urllib.request.Request(f"{_url()}/session/{session_id}/message")
    req.add_header("Authorization", "Basic " + b64encode(f"opencode:{_password()}".encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        msgs = json.load(r)
    texts, in_tok, out_tok, errors = [], 0, 0, []
    for m in msgs:
        info = m.get("info") or {}
        if info.get("role") != "assistant":
            continue
        tok = info.get("tokens") or {}
        in_tok += tok.get("input", 0) + (tok.get("cache") or {}).get("read", 0)
        out_tok += tok.get("output", 0) + tok.get("reasoning", 0)
        if info.get("error"):
            errors.append(json.dumps(info["error"])[:500])
        texts += [p["text"] for p in m.get("parts") or [] if p.get("type") == "text" and p.get("text")]
    return texts, in_tok, out_tok, errors


RATE_MARKERS = ("429", "rate limit", "free usage", "usage exceeded", "insufficient", "quota")


# v1's model policy, restored 2026-09-23 after a day of low-quality detections: every JUDGMENT about
# human messages or code (classify, investigate, plan, decide, draft) runs on Claude; the free model
# keeps the bulk read-only work whose output Opus verifies anyway (review finders, patrol, evals).
CLAUDE_ROUTES = [
    (("triage", "prioritize", "context", "backfill", "loop"), "sonnet", ()),
    (("deps", "ci-diagnose"), "sonnet", ("Read", "Grep", "Glob")),  # both read files (logs, code)
    # "draft", not "draft-", and "claude-": the rule gate (evals.run_signal) replays the bare `draft` lane and the Claude
    # roles' lanes (claude-fix...), which went to the free model and hung 20-30 min a call (2026-10-06/07: 11 timeouts,
    # every one swallowed, the gate read 0.0 vs 0.0 and accepted)
    # sorting a pull request's review comments is a first pass: comments_verify (Opus) confirms every fix and writes
    # every defense after it, and was written for "a cheaper model's triage"
    (("comments",), "sonnet", ("Read", "Grep", "Glob")),
    (("investigate", "plan", "draft", "reply", "claude-"), "opus", ("Read", "Grep", "Glob")),
    (("review-own-",), "sonnet", ("Read", "Grep", "Glob")),  # 2026-09-29: finders on our own PRs are bulk reading; Opus verifies
    (("review-",), "opus", ("Read", "Grep", "Glob")),  # 2026-09-23: "we need a good model for reviews"
    (("learn", "judge"), "opus", ()),  # rules that shape every prompt are not a free-model job
    (("patrol",), "sonnet", ("Read", "Grep", "Glob")),  # 2026-09-27: free model returned no usable JSON 6 runs in a row
]
_claude_sem = threading.BoundedSemaphore(8)
# mid-tier lanes whose failed call is asked again of Opus instead of failing the stage ("escalating if it fails")
STEP_UP = ("comments", "ci-diagnose")


def _claude_route(lane: str):
    if not CFG["muse"].get("judgment_on_claude", True):
        return None
    for prefixes, model, tools in CLAUDE_ROUTES:
        if any(lane == p or lane.startswith(p) for p in prefixes):
            return model, tools
    return None


def _via_claude(agent, prompt, schema, cwd, lane, item_id, timeout, model, tools):
    """Same agent instructions (the opencode agent file's body becomes the system prompt), same
    prompt and schema, answered by Claude. Recorded as tier=claude under role agent-<name>."""
    from . import claude
    role = f"agent-{agent}"
    # the lane's learned rules, as on the free model: learn compiles them per lane (rules/<lane>.md) and the eval gate
    # measures them there, but since the 09-23 Claude routing no call read them (13 triage, 10 draft rules active, dead)
    pack = rules_pack(lane)
    if pack:
        prompt = f"## Learned rules for this lane (apply them)\n{pack}\n\n{prompt}"
    # the system prompt is built from opencode/agents/<agent>.md by claude.system_file (never copied into prompts/)
    delivery_lanes = ("plan", "ci-diagnose", "deps", "draft-announce", "draft-comments", "comments")
    own_pr = bool(getattr(_prio, "hi", False))  # finders on OUR OWN PR are delivery, not squad reviews
    pool = claude.pool_ctx("delivery") if own_pr or any(lane == l or lane.startswith(l) for l in delivery_lanes) else claude.pool_ctx(None)
    # a file of ours the prompt names by path must be readable: since 38a0696 a lane with shell rules runs in
    # 'default' permission mode, where reads outside cwd are denied, and investigate and plan name slack-context.md
    # (10 dossiers from #909 on wrote 'reading slack-context.md was denied'; critique always passed the dir)
    add = (str(VAR),) if str(VAR) in prompt else ()
    with _claude_sem, pool, claude.caller_ctx(lane if lane != agent else None):
        kw = dict(cwd=cwd, schema=schema, tools=tools, item_id=item_id, add_dirs=add,
                  timeout=min(timeout or 900, 1500 if lane.startswith("review") else 900), urgent=True)
        try:
            out = claude.lean(role, prompt, model=model, **kw)
        except claude.Interrupted:
            raise
        except claude.ClaudeError as e:
            if model != "sonnet" or lane not in STEP_UP or "credit balance" in str(e).lower():
                raise
            log("muse: mid-tier call failed, asking Opus", lane=lane, item=item_id, err=str(e)[:200])
            out = claude.lean(role, prompt, model="opus", **kw)
    if schema:
        return out
    return out.get("text", "") if isinstance(out, dict) else out


def call(agent: str, prompt: str, *, cwd: str, schema: dict | None = None, lane: str | None = None,
         item_id: int | None = None, timeout: int | None = None, retries: int = 2,
         model: str | None = None, claude_model: str | None = None) -> dict | list | str:
    """Run one Muse turn-loop headless. Returns parsed JSON when a schema is given. A pinned `model` skips the Claude
    routes and the free chain: a rate limit on it raises MuseRateLimited at once (the code scanner runs it beside Haiku
    and keeps going without it). `claude_model` asks the lane's question of that Claude tier instead of the lane's own
    (a mid-tier answer that settled nothing, asked again of Opus)."""
    lane = lane or agent
    route = None if model else _claude_route(lane)
    if claude_model and not model:
        route = (claude_model, (route or (None, ("Read", "Grep", "Glob")))[1])
    if route is None and not site.feature("free_models"):
        # an install without the free models has one place to think, the provider its user chose. The lanes the first
        # install leaves to a free model (and a call that pins one) run there on the mid tier, read-only: left to the
        # free path they waited on a server this install never starts
        route = ("sonnet", ("Read", "Grep", "Glob"))
    if route:
        return _via_claude(agent, prompt, schema, cwd, lane, item_id, timeout, *route)
    pack = rules_pack(lane)
    if pack:
        prompt = f"## Learned rules for this lane (apply them)\n{pack}\n\n{prompt}"
    if schema:
        prompt += ("\n\n## Output\nReturn ONLY one JSON value matching this JSON schema, no prose:\n"
                   + json.dumps(schema, ensure_ascii=False))
    timeout = timeout or CFG["muse"]["call_timeout_s"]
    pinned = model
    last_err = ""
    attempts = 0
    for _hop in range(retries + 1 + len(CFG["muse"].get("chain", [])) * 2):
        if attempts > retries:
            break
        attempts += 1
        ensure_server()
        t0 = time.time()
        model = pinned or pick_model()
        if model is None:
            raise MuseRateLimited("every free model in the chain is rate-limited; backing off")
        if pinned and time.time() < db.kv_get(f"model_paused:{pinned}", {}).get("until", 0):
            raise MuseRateLimited(f"{pinned} is rate-limited; backing off")
        sem = (_sem_triage if lane == "triage" else _sem_hi if getattr(_prio, "hi", False)
               else _sem if lane.startswith(LOW_LANES) else _sem_mid)
        with sem:
            global _inflight
            with _inflight_lock:
                _inflight += 1
            try:
                p = _run_watched(agent, model, cwd, lane, item_id, prompt, timeout)
            except _RateLimitedNow:
                pause_model(model)
                db.record_call("muse", lane, item_id, False, time.time() - t0, 0, 0, 0, f"rate-limited {model}")
                continue  # next model in the chain; does not burn a real attempt
            except subprocess.TimeoutExpired:
                db.record_call("muse", lane, item_id, False, time.time() - t0, 0, 0, 0, f"timeout {model}")
                last_err = f"timeout after {timeout}s ({model})"
                db.kv_set("muse_suspect", int(time.time()))  # worker's health job verifies and restarts
                continue
            finally:
                with _inflight_lock:
                    _inflight -= 1
        texts, in_tok, out_tok, errors, sid = [], 0, 0, [], None
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            sid = sid or ev.get("sessionID")
            part = ev.get("part") or {}
            if ev.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
            elif ev.get("type") == "step_finish":
                tok = part.get("tokens") or {}
                in_tok += tok.get("input", 0)
                out_tok += tok.get("output", 0) + tok.get("reasoning", 0)
            elif ev.get("type") == "error":
                errors.append(json.dumps(ev.get("error") or ev)[:500])
        if not texts and sid:
            try:
                texts, in_tok, out_tok, errors = _session_texts(sid)
                if texts:
                    log("muse: answer recovered from the server session", lane=lane, session=sid)
            except Exception as e:
                log("muse: session read failed", session=sid, err=str(e)[:200])
        blob = " ".join(errors).lower() + " " + p.stderr[-2000:].lower()
        dur = time.time() - t0
        if any(m in blob for m in RATE_MARKERS) and not texts:
            pause_model(model)
            db.record_call("muse", lane, item_id, False, dur, 0, in_tok, out_tok, f"rate-limited {model}")
            continue
        if not texts:
            db.record_call("muse", lane, item_id, False, dur, 0, in_tok, out_tok, f"rc={p.returncode} no text")
            last_err = f"no text (rc={p.returncode}) {' '.join(errors)[:300]} {p.stderr[-300:]}"
            continue
        final = texts[-1]
        if not schema:
            db.record_call("muse", lane, item_id, True, dur, 0, in_tok, out_tok)
            return final
        try:
            obj = extract_json(final)
        except ValueError:
            try:
                obj = extract_json("\n".join(texts))
            except ValueError as e:
                obj, last_err = None, str(e)
                # draft lanes: a schema that is just {"text": string} is answered in prose; the prose IS the text
                req = schema.get("required", [])
                if (schema.get("type") == "object" and len(req) == 1 and final.strip()
                        and (schema.get("properties", {}).get(req[0], {}).get("type") == "string")):
                    obj = {req[0]: final.strip()}
        if obj is not None and isinstance(obj, list) and schema.get("type") == "object":
            arr = [k for k in schema.get("required", []) if schema.get("properties", {}).get(k, {}).get("type") == "array"]
            if len(schema.get("required", [])) == 1 and len(arr) == 1:
                obj = {arr[0]: obj}  # bare list for a one-list schema (Space a customer does this)
        if obj is not None:
            errs = check_schema(obj, schema)
            if errs and isinstance(obj, dict) and len(obj) == 1:
                inner = next(iter(obj.values()))  # {"plan": {...}} style wrapper: a formatting slip
                if not check_schema(inner, schema):
                    obj, errs = inner, []
            if not errs:
                db.record_call("muse", lane, item_id, True, dur, 0, in_tok, out_tok, model)
                return obj
            last_err = "; ".join(errs[:8])
        db.record_call("muse", lane, item_id, False, dur, 0, in_tok, out_tok, f"schema: {last_err}")
        prompt += f"\n\nYour previous answer was invalid ({last_err}). Return ONLY valid JSON for the schema."
    raise MuseError(f"{agent}: {last_err}")
