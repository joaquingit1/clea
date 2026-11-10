"""Incidents: what went wrong, collected deterministically (zero tokens) from outcomes, so the supervisor session can
find root causes and fix the harness by itself. One incident per signature (deduplicated), with evidence pointers and an
impact score. Sources: stage failures, stuck or blocked cases, reaper kills, PRs superseded or closed, human findings on
our PRs, his rejections and revisions, expired asks, and his corrections in the Claude Code sessions about the harness."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import db, site
from .core import HOME, VAR, log, now

OUT = VAR / "incidents.json"
SESSIONS = Path.home() / ".claude" / "projects"
LIVE = HOME  # the checkout the worker runs: a fix counts only once its commit is in this HEAD
CORRECTIVE = re.compile(r"\b(why|still|stuck|stalled|broken|wrong|unacceptable|not (working|good|picked|showing)|didn'?t|doesn'?t|"
                        r"never|no (updates|messages|progress)|what('?s| is) going on|over an hour|hours|fix|should(n'?t)?|missing|lost|"
                        r"outdated|duplicat|waste|slow)\b", re.I)
HARNESS = re.compile(r"\b(harness|autopilot|digest|dm|dms|board|case|thread|pr|prs|draft|ticket|review|reply|message|build|tick|"
                     r"dashboard|restart|tokens?)\b", re.I)
# a home-dir session is about the harness only when its tool calls touched it (tool results and summaries name the
# autopilot worktrees of any session that merely listed ~/.claude/projects)
ABOUT = re.compile(r"\.claude/autopilot|\bapctl\b")


def _harness_share(lines: list[str]) -> float:
    """The share of a session's own tool calls that touch the harness (its subject, not a tool it borrowed)."""
    calls = []
    for l in lines:
        if '"type":"tool_use"' in l.replace(" ", "") and '"type":"assistant"' in l.replace(" ", ""):
            try:
                content = (json.loads(l).get("message") or {}).get("content") or []
            except ValueError:
                continue
            calls += [json.dumps(c.get("input")) for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
    return sum(1 for c in calls if ABOUT.search(c)) / len(calls) if calls else 0.0


def _sig(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _live(commit: str) -> bool:
    import subprocess
    return subprocess.run(["git", "-C", str(LIVE), "merge-base", "--is-ancestor", commit, "HEAD"],
                          capture_output=True).returncode == 0


def _norm_err(e: str) -> str:
    e = re.sub(r"\b[0-9a-f]{7,40}\b", "#", e or "")
    e = re.sub(r"\d+(\.\d+)?", "#", e)
    return re.sub(r"\s+", " ", e)[:140]


class _Lock:
    """fcntl lock shared by every writer of incidents.json (collector, observer, supervisor marks, ratchet)."""
    def __enter__(self):
        import fcntl
        self.f = open(str(OUT) + ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        import fcntl
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _runnable_s(status: str, urgency: int, start: float, end: float) -> float:
    """Seconds between start and end in which the worker could have run this stage: the Mac awake (hot.note_sleep's
    record) and, unless the stage runs off hours or the case is urgent, inside work hours (worker.pool). By wall clock,
    #644 (review_verify since 23:07) and #646 (reply_draft since 01:01) waiting for 08:00 by design were filed as
    'casos trabados' and then as 'stuck cases absent from the case list'."""
    import datetime as dt
    from . import hot
    from .core import work_hours
    sleeps = [(w - g, w) for w, g in (db.kv_get("sleeps") or []) + hot.pauses(end)]  # a pause holds stages too
    anytime = urgency >= 3 or status in db.OFF_HOURS_STAGES
    n, t = 0.0, max(start, end - 7 * 86400)  # past a week it is stuck anyway
    while t < end:
        if (anytime or work_hours(dt.datetime.fromtimestamp(t))) and not any(a <= t < w for a, w in sleeps):
            n += min(300, end - t)
        t += 300
    return n


def failed_asleep(call) -> bool:
    """A failed call (a calls row) whose timeout or connection error is the machine's sleep: the impact judges of 137,
    367, 595 and 624 ran 2100 s of wall time over the 10-09 05:04-05:37 sleep for a 180 s budget, were judged on the
    next wake, and listed the incident fixed by ef01435 as 'recurred', which the ratchet reverts. A dark wake has no
    keychain either, so the CLI answers 'Not logged in'."""
    from . import hot
    return bool(re.search(r"timeout|timed out|connection|network|not logged in", call["note"] or "", re.I)
                and hot.slept_in(call["at"] - (call["dur_s"] or 0), call["at"]))


def collect(hours: int = 48) -> list[dict]:
    with _Lock():
        return _collect(hours)


def _collect(hours: int = 48) -> list[dict]:
    since = now() - hours * 3600
    inc: dict[str, dict] = {}

    def add(kind: str, key: str, impact: float, title: str, evidence: str, at: float):
        s = _sig(kind, key)
        i = inc.setdefault(s, {"sig": s, "kind": kind, "title": title, "impact": 0.0, "count": 0, "first": at, "last": at, "evidence": []})
        i["impact"] += impact
        i["count"] += 1
        i["first"], i["last"] = min(i["first"], at), max(i["last"], at)
        if len(i["evidence"]) < 8:
            i["evidence"].append(evidence[:400])

    # 1. stage failures grouped by lane + normalized error, minus the sleep's (failed_asleep)
    for r in db.q("SELECT at, dur_s, lane, item_id, note FROM calls WHERE ok=0 AND at>? AND note NOT LIKE '%limit%' "
                  "AND note NOT LIKE 'interrupted for thread news%'", since):
        if failed_asleep(r):
            continue
        add("call_failure", f"{r['lane']}:{_norm_err(r['note'])}", 0.5, f"{r['lane']} falla: {_norm_err(r['note'])[:80]}",
            f"item {r['item_id']} {time.strftime('%d %H:%M', time.localtime(r['at']))}: {r['note'][:200]}", r["at"])
    for r in db.q("SELECT item_id, at, note FROM transitions WHERE at>? AND note LIKE '%failed 3x%'", since):
        add("stage_blocked", r["note"][:40], 3, f"stage bloqueado: {r['note'][:60]}", f"item {r['item_id']}: {r['note']}", r["at"])
    # 2. stuck: live cases idle for more than 6 h in an active stage, counting only the time the worker could run it
    for r in db.q("SELECT id, status, title, updated_at, urgency FROM items WHERE status NOT IN ('done','dropped','expired','failed','awaiting_approval',"
                  "'awaiting_human','waiting','ci_wait','loop_track') AND updated_at<?", now() - 6 * 3600):
        idle = _runnable_s(r["status"], r["urgency"] or 1, r["updated_at"], now())
        if idle >= 6 * 3600:
            add("stuck", f"{r['status']}", 4, f"casos trabados en {r['status']}", f"item {r['id']} ({r['title'][:60]}) idle {round(idle/3600)}h", r["updated_at"])
    # 3. outcome failures on delivered work
    for r in db.q("SELECT item_id, at, note FROM transitions WHERE at>? AND (note LIKE '%en favor de%' OR note LIKE ? "
                  "OR note LIKE 'revision requested%')", since, f"{site.first_name()} rejected%"):
        k = "superseded" if "en favor de" in r["note"] else "rejected" if "rejected" in r["note"] else "revised"
        add(k, f"{k}:{r['item_id']}", {"superseded": 8, "rejected": 4, "revised": 2}[k], f"{k}: item {r['item_id']}", r["note"][:200], r["at"])
    for r in db.q("SELECT item_id, at, data FROM signals WHERE at>? AND kind='review_lesson'", since):
        d = json.loads(r["data"] or "{}")
        if d.get("ours"):
            add("human_finding", f"lesson:{r['item_id']}", 6, "un compañero encontró un defecto nuestro", d.get("cause", "")[:300], r["at"])
    n_exp = db.q1("SELECT count(*) n FROM signals WHERE at>? AND kind='approval_expired'", since)["n"]
    if n_exp >= 5:
        add("expired_asks", "expired", n_exp * 0.3, f"{n_exp} pedidos expiraron sin respuesta", "asks nobody acted on in 2 days", now())
    # 4. harness mechanics from the log (reaper kills, hot-reload failures, tick guards)
    import datetime as dt
    from .core import LOG_DIR
    for day in (dt.date.today() - dt.timedelta(days=1), dt.date.today()):
        f = LOG_DIR / f"ap-{day:%F}.log"
        if not f.exists():
            continue
        for line in f.read_text(errors="ignore").splitlines():
            if "reaper: stage over budget" in line or "tick guard" in line or "hot reload refused" in line or "run_stage escaped" in line:
                # the line's own time, not now(): stamped now, every log-derived incident read as "recurred" 10 min after
                # its fix was marked (yesterday's lines are still in the window), and the ratchet reverts such fixes
                try:
                    at = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                if at < since:
                    continue
                kind = "reaper" if "reaper" in line else "tick_guard" if "tick guard" in line else "hot_reload" if "hot reload" in line else "stage_escape"
                add(kind, _norm_err(line[20:120]), 1, f"{kind}: {_norm_err(line[20:100])}", line[:300], at)
    # 5. his corrections about the harness in Claude Code sessions (the strongest signal: he had to say it)
    # only his INTERACTIVE sessions about the harness (headless harness runs have no permissionMode and live in other dirs)
    home = str(Path.home()).replace("/", "-")  # how Claude Code names a project folder after its path
    projs = [SESSIONS / home] + list(SESSIONS.glob(f"{home}--claude-autopilot*"))
    for proj in projs:
        for tf in proj.glob("*.jsonl"):
            if tf.stat().st_mtime < since:
                continue
            try:
                lines = tf.read_text(errors="ignore").splitlines()
                human_turns = sum(1 for l in lines if '"type":"user"' in l.replace(" ", "") and '"tool_use_id"' not in l
                                  and '"isMeta":true' not in l.replace(" ", ""))
                if human_turns < 6:
                    continue  # a headless run has one prompt (two when resumed): only real conversations count
                if proj.name == home and _harness_share(lines) < 0.2:
                    # the home dir holds all his work: 10-16 a session revamping a video skill filed "why didnt we say
                    # it" about its Slack draft (HARNESS matches draft, dashboard, message) as a harness correction, and
                    # 10-20 the same session filed "should i explain that the skill has a harness" once it had borrowed
                    # the harness's Slack client (3 of its 326 tool calls; a harness session has most of them there)
                    continue
                for line in lines:
                    if '"type":"user"' not in line.replace(" ", ""):
                        continue
                    e = json.loads(line)
                    msg = (e.get("message") or {}).get("content")
                    text = msg if isinstance(msg, str) else " ".join(c.get("text", "") for c in msg or [] if isinstance(c, dict) and c.get("type") == "text")
                    ts = time.mktime(time.strptime(e.get("timestamp", "")[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if e.get("timestamp") else 0
                    if not text or ts < since or text.startswith(("<", "[", "Caveat", "Base directory", "This session is being continued", "Another Claude session")) or "<system-reminder>" in text[:40]:
                        continue
                    if len(text) > 30 and CORRECTIVE.search(text) and HARNESS.search(text):
                        add("joaquin_correction", _sig(text[:200]), 10, f"{site.first_name()}: {text[:90]}", f"{tf.name}: {text[:380]}", ts)
            except Exception:
                continue
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    for o in prev.get("incidents", []):
        if (o.get("kind") in PERSISTENT or o.get("filed")) and o["sig"] not in inc and now() - o.get("last", 0) < 7 * 86400:
            inc[o["sig"]] = o  # the observer's findings persist until fixed or a week old
    out = sorted(inc.values(), key=lambda i: -i["impact"])
    state = prev.get("state", {})
    for i in out:
        st = state.get(i["sig"]) or {}
        i["status"] = st.get("status", "open")
        fix = next((a.get("commit") or "" for a in reversed(st.get("attempts", [])) if a.get("status") == "fixed"), "")
        if (st.get("status") == "fixed" and re.fullmatch(r"[0-9a-f]{7,40}", fix) and now() - st.get("fixed_at", 0) > 2 * 3600
                and not _live(fix)):
            # 2026-09-30: nine supervisor fixes were marked fixed but never reached main (the promotion's ff-only merge
            # failed whenever main moved during the run, and the next run reset the branch), so nobody looked again
            i["status"] = "unpromoted"
        elif st.get("status") == "fixed" and i["last"] > st.get("fixed_at", 0) + 600:
            i["status"] = "recurred"  # the ratchet reverts the fix and the supervisor sees its failed attempt
        i["attempts"] = st.get("attempts", [])
    OUT.write_text(json.dumps({"at": now(), "incidents": out, "state": state}, ensure_ascii=False, indent=1))
    log("incidents collected", n=len(out), open=sum(1 for i in out if i["status"] != "fixed"))
    return out


PERSISTENT = ("observed", "intake_gap")  # filed from outside the collector: kept until fixed or a week old


def file(kind: str, key: str, title: str, evidence: str, impact: float = 5) -> str:
    """File (or refresh) one incident from a deterministic check outside the collector. Deduped by kind+key. Marked
    `filed`, so the collector keeps it like an observed one."""
    sig = _sig(kind, key)
    with _Lock():
        data = json.loads(OUT.read_text()) if OUT.exists() else {"incidents": [], "state": {}}
        cur = next((i for i in data["incidents"] if i["sig"] == sig), None)
        if cur:
            cur["last"], cur["count"], cur["filed"] = now(), cur.get("count", 1) + 1, True
        else:
            data["incidents"].insert(0, {"sig": sig, "kind": kind, "title": title[:160], "impact": float(impact), "count": 1,
                                         "first": now(), "last": now(), "status": "open", "attempts": [], "filed": True,
                                         "evidence": [evidence[:600]]})
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return sig


def mark(sig: str, status: str, commit: str = "", note: str = "") -> None:
    """CLI helper for the supervisor: `bin/apctl incident <sig> <fixed|wontfix|open> [commit] [note]`."""
    with _Lock():
        _mark(sig, status, commit, note)


def _mark(sig: str, status: str, commit: str = "", note: str = "") -> None:
    data = json.loads(OUT.read_text()) if OUT.exists() else {"incidents": [], "state": {}}
    st = data.setdefault("state", {}).setdefault(sig, {"attempts": []})
    st["status"] = status
    if status == "fixed":
        st["fixed_at"] = now()
    st["attempts"].append({"at": now(), "status": status, "commit": commit, "note": note[:400]})
    for i in data.get("incidents", []):
        if i["sig"] == sig:
            # the listed status is what the ratchet reads: left 'recurred' until the next collect, it reverted the new
            # fix of a recurred incident
            i["status"], i["attempts"] = status, st["attempts"]
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
