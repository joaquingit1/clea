"""The long-running worker: one process, a scheduler loop plus stage pools.

No LLM decides when to look (v1's per-tick prefix reload was the cost). Code polls,
triages in batches, and dispatches exactly one stage per claimed item. Pools:
  fast   (3 threads): local + Muse stages (free)
  claude (1 thread):  Claude stages, budget-gated, work-hours only unless urgent
Everything is resumable: state lives in SQLite, a crash loses at most one stage.
"""
from __future__ import annotations

import datetime as dt
import faulthandler
import fcntl
import signal
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

from . import (approvals, backfill, closing, context, babysit, board, claude, db, evals, gh, hot, ingest, learn, muse, outbound, patrol, replies, reviews,
               site, tasks, triage, worktree)
from .core import CFG, RUNS, VAR, WORKTREES, log, now, reload_config, run, work_hours

STAGES = {
    "telemetry": ("quick", "tasks.telemetry"), "vet": ("quick", "tasks.vet"),
    "investigate": ("fast", "tasks.investigate"), "plan": ("fast", "tasks.plan"),
    "critique": ("quick", "tasks.critique"), "propose": ("fast", "tasks.propose"),
    "propose_check": ("quick", "tasks.propose_check"), "self_find": ("fast", "tasks.self_find"),
    "build": ("claude", "tasks.build"), "gate": ("fast", "tasks.gate"),
    "self_review": ("quick", "tasks.self_review"), "fix": ("claude", "tasks.fix"),
    "publish": ("fast", "tasks.publish"), "ci_wait": ("fast", "tasks.ci_wait"), "ci_fix": ("fast", "tasks.ci_fix"),
    "verify_done": ("quick", "tasks.verify_done"), "pr_body_refresh": ("quick", "tasks.pr_body_refresh"),
    "announce": ("fast", "tasks.announce"),
    "announce_check": ("quick", "tasks.announce_check"), "waiting": ("fast", "tasks.waiting"),
    "review_prepare": ("fast", "reviews.prepare"), "review_find": ("fast", "reviews.find"),
    "review_verify": ("quick", "reviews.verify"), "review_post": ("fast", "reviews.post"),
    "reply_draft": ("quick", "replies.reply_draft"), "reply_check": ("quick", "replies.reply_draft"), "loop_track": ("fast", "replies.loop_track"),
    "comments_triage": ("fast", "babysit.comments_triage"),
    "comments_verify": ("quick", "babysit.comments_verify"), "comments_reply": ("fast", "babysit.comments_reply"),
}
FAST = [s for s, (p, _) in STAGES.items() if p == "fast"]
SLOW = [s for s, (p, _) in STAGES.items() if p == "claude"]   # long Fable builds/fixes, telemetry dives
QUICK = [s for s, (p, _) in STAGES.items() if p == "quick"]   # short Opus judgments; replies must never wait on a build
STOP = threading.Event()
PAUSE_FILE = VAR / "PAUSED"
RUNNING = False  # the pools and the scheduler are up (False while a new install is still being set up)
# how a case dropped as already covered starts its note (cover, pregate, dedupe), in either language: read, not written
COVERED = ("lo cubre", "covered by")
# the comment outbound.close_duplicate_pr leaves on a closed duplicate, before the keeper's link, in either language
DUP_COMMENT = ("duplica el fix de", "duplicates the fix in", "duplicates the fix of")


def paused() -> bool:
    return PAUSE_FILE.exists()


def muse_paused() -> bool:
    return time.time() < db.kv_get("muse_paused_until", 0)


class Awake:
    """Hold off idle sleep while a stage runs (v1: ticks died to clamshell/idle sleep)."""
    def __enter__(self):
        try:
            self.p = subprocess.Popen(["caffeinate", "-i"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self.p = None

    def __exit__(self, *a):
        if self.p:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except Exception:
                self.p.kill()


def run_stage(it: dict) -> None:
    status = it["status"]
    _, ref = STAGES[status]
    fn = getattr(sys.modules[f"ap.{ref.split('.')[0]}"], ref.split('.')[1])  # resolved now: hot reload swaps it
    t0 = time.time()
    # own-PR delivery (fixes on his PRs, harness-built fixes) gets the reserved Muse slots
    muse._prio.hi = it["kind"] in ("CI", "COMMENTS") or status in ("self_find", "self_review", "fix", "gate", "publish",
                                                                   "ci_fix", "comments_triage", "comments_reply")
    worker = it.get("claimed_by") or ""
    db.kv_set(f"stage_started:{threading.current_thread().name}", {"item": it["id"], "status": status, "at": now(),
                                                                    "thread": threading.current_thread().name,
                                                                    "claim": worker})
    done = threading.Event()

    def beat():  # long builds must not look like dead claims to claim_next(stale_s) or the watchdog
        while not done.wait(60):
            db.heartbeat(it["id"], worker)
    threading.Thread(target=beat, name=f"beat-{it['id']}", daemon=True).start()
    try:
        someone_waits = it["kind"] in ("CI", "COMMENTS") or bool(it["data"].get("parent_waiting")) or it["kind"] == "REPLY"
        # cleared before the stage, not after it: a stage that holds itself (telemetry waiting for the work login) writes
        # why into last_error and returns, and the clear after it erased that, so #672 re-ran telemetry every 10
        # min for 31 min with no call and no reason, and its escalation filed the hold as a harness bug
        if it.get("last_error"):
            db.x("UPDATE items SET last_error=NULL WHERE id=?", it["id"])
        from . import scan
        cap = scan.over_budget(it)
        if cap:
            db.update_item(it["id"], status="blocked", note=cap, data={"blocked_reason": cap, "blocked_from": status,
                                                                         "blocked_by": "scan_budget"})
            return
        from . import precheck
        if precheck.settled(it):  # GitHub says there is nothing left to do: the stage's session is not paid for
            return
        with Awake(), (claude.exempt_ctx() if someone_waits else contextlib.nullcontext()):
            fn(it)
        db.update_item(it["id"], attempts=0)
    except muse.MuseRateLimited as e:
        db.kv_set("muse_paused_until", int(time.time()) + CFG["muse"]["rate_limit_backoff_s"])
        db.update_item(it["id"], next_run_in=CFG["muse"]["rate_limit_backoff_s"], last_error=f"muse rate limited: {e}"[:500])
        log("muse rate limited; pausing muse lanes", item=it["id"])
    except (claude.BudgetExhausted, claude.ClaudeLimited) as e:
        db.update_item(it["id"], next_run_in=max(300, int(claude.paused_until() - time.time()) + 60),
                       last_error=f"{type(e).__name__}: {e}"[:500])
    except claude.Interrupted as e:  # thread news handed to the session: rerun now, it resumes with the news
        db.update_item(it["id"], next_run_in=0, last_error=None)
        log("stage interrupted for thread news", item=it["id"], stage=status, err=str(e)[:200])
    except outbound.Blocked as e:
        db.update_item(it["id"], status="blocked", note=str(e)[:300], data={"blocked_reason": str(e)[:1000], "blocked_from": status,
                                                                                "blocked_by": "guard"})
    except Exception as e:
        n = (it.get("attempts") or 0) + 1
        err = f"{type(e).__name__}: {e}"[:1500]
        tb = traceback.format_exc()
        # a Python exception inside the harness (not a model/tool verdict) is a HARNESS bug: flag it
        # loudly and let it auto-retry once the harness code changes (see requeue_harness_bugs)
        harness_bug = isinstance(e, (AttributeError, FileNotFoundError, KeyError, TypeError, NameError,
                                     IndexError, ValueError, ImportError)) and "/ap/" in tb
        if harness_bug:
            db.add_signal("harness_bug", {"item": it["id"], "stage": status, "err": err[:500], "tb": tb[-1500:]}, it["id"])
        network = any(w in err.lower() for w in PAUSE_WORDS["network"])
        # a stage that ran across a machine sleep or right after one failed on the host, not on its work: a dark wake has
        # no network or keychain, and its errors match no network word (10-09: #680 review_prepare blocked twice overnight
        # on a TLS handshake timeout, a git fetch timeout and gh 401/404, every attempt next to a recorded sleep)
        slept = hot.slept_near(t0, time.time())
        deferred = (network or slept) and not harness_bug
        # a deferred stage spent no attempt, so it is not logged as a failure: the ratchet counts 'stage failed' and
        # tracebacks after each supervisor commit (10-16: 3134d95 reverted for 'errors 0->7', five #870 waiting retries on
        # "error connecting to api.github.com", one DNS failure and one gh timeout, none in the code it changed)
        if deferred:
            log("stage deferred", item=it["id"], stage=status, why="network" if network else "slept", err=err[:300])
        else:
            log("stage failed", item=it["id"], stage=status, attempt=n, err=err[:300], tb=tb[-1200:])
        if deferred:
            # the network went away, not the stage (2026-09-27: an outage blocked the Redondeo
            # comments item "failed 3x"): keep the attempt budget, retry in 5 min
            db.update_item(it["id"], attempts=it.get("attempts") or 0, last_error=err, next_run_in=300,
                           note=site.t("no network; retrying", "sin red; reintento") if network
                           else site.t("the Mac slept; retrying", "la Mac durmió; reintento"))
        elif n >= 3:
            db.update_item(it["id"], status="blocked", attempts=n, last_error=err,
                           note=f"{status} failed 3x", data={"blocked_reason": f"{status}: {err[:600]}",
                                                             "blocked_stage": status,
                                                             "harness_bug_rev": code_rev() if harness_bug else None})
        else:
            db.update_item(it["id"], attempts=n, last_error=err, next_run_in=120 * n)
    finally:
        done.set()
        db.release(it["id"])
        db.kv_set(f"stage_started:{threading.current_thread().name}", {})
        log("stage", item=it["id"], stage=status, secs=round(time.time() - t0))


def code_rev() -> str:
    """Hash of the harness code: changes whenever ap/ or prompts/ change."""
    import hashlib
    from .core import HOME
    h = hashlib.sha1()
    for f in sorted(list((HOME / "ap").glob("*.py")) + list((HOME / "prompts").rglob("*.md"))):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def republish_shadow_work() -> int:
    """Items built and verified while in shadow mode never got pushed; once in act mode they
    go back to publish."""
    if CFG.get("mode") != "act":
        return 0
    n = 0
    for r in db.q("SELECT id, kind, data FROM items WHERE status IN ('awaiting_human','comments_reply','awaiting_approval') "
                  "AND json_extract(data,'$.shadow_ready')"):
        d = json.loads(r["data"])
        parts = d.get("parts") or []
        if parts and parts[d.get("cur", 0)].get("state") == "ready_local":
            db.update_item(r["id"], status="publish", next_run_at=0, note="act mode: publishing work verified in shadow",
                           data={"shadow_ready": False})
            n += 1
    if n:
        log("republishing shadow-verified work", n=n)
    return n


RESTART_FLAG = VAR / "RESTART"


def running_stages() -> int:
    return db.q1("SELECT count(*) c FROM items WHERE claimed_by IS NOT NULL")["c"]


def requeue_harness_bugs() -> int:
    """Items blocked by a harness exception go back to their stage once the code changed. A self_review block gets the
    verdict once more when the code deciding it changed (no rev: blocked before it was recorded): 06b202a made spent
    rounds publish #595's kind of findings as his steps, and #595, blocked that afternoon, stayed blocked 45 h."""
    from .ingest import verdict_rev
    rev, n = code_rev(), 0
    for r in db.q("SELECT id, data FROM items WHERE status='blocked' AND json_extract(data,'$.harness_bug_rev') IS NOT NULL"):
        d = json.loads(r["data"])
        if d.get("harness_bug_rev") != rev and d.get("blocked_stage"):
            db.update_item(r["id"], status=d["blocked_stage"], attempts=0, last_error=None, next_run_at=0,
                           note="harness code changed: retrying", data={"harness_bug_rev": None, "blocked_reason": None})
            n += 1
    srev = verdict_rev("self_review")
    for r in db.q("SELECT id, data FROM items WHERE status='blocked' AND json_extract(data,'$.blocked_reason') LIKE 'self_review still failing%'"):
        if json.loads(r["data"]).get("blocked_rev") != srev:
            db.update_item(r["id"], status="self_review", attempts=0, last_error=None, next_run_at=0,
                           note="self_review changed: verdict again", data={"blocked_rev": srev, "blocked_reason": None})
            n += 1
    from . import repro
    rrev = repro.rev()  # 2026-10-13: #727's valid red test was blocked by a wrong diff base, fixed in repro.py
    for r in db.q("SELECT id, data FROM items WHERE status='blocked' AND json_extract(data,'$.blocked_reason') LIKE ?",
                  repro.BLOCKED + "%"):
        if json.loads(r["data"]).get("blocked_rev") != rrev:
            db.update_item(r["id"], status="plan", attempts=0, last_error=None, next_run_at=0, note="repro changed: one more round",
                           data={"blocked_rev": rrev, "blocked_reason": None, "repro_rounds": 1})
            n += 1
    # a gate that kept failing on the frozen reproduction, the same: one more fix round under the new rules (10-19: #916
    # blocked because its fix sessions were never told a rebase let them adapt the test's stale mock)
    for r in db.q("SELECT id, data FROM items WHERE status='blocked' AND json_extract(data,'$.blocked_reason') LIKE ?",
                  f"gate still failing%{repro.GATE}:%"):
        if json.loads(r["data"]).get("blocked_rev") != rrev:
            db.update_item(r["id"], status="gate", attempts=0, last_error=None, next_run_at=0,
                           note="repro changed: one more fix round", data={
                               "blocked_rev": rrev, "blocked_reason": None, "fix_rounds": CFG["quality"]["max_fix_rounds"] - 1})
            n += 1
    if n:
        log("requeued items blocked by harness bugs", n=n)
    return n


def pool(name: str, statuses: list[str], kinds: list[str] | None = None) -> None:
    """The loop only: each turn is pool_turn, looked up per turn, so hot.load_worker's rebind reaches running threads.
    The loop reads STOP and is never rebound; a running loop never sees new code."""
    worker = f"{name}-{threading.current_thread().name}"
    while not STOP.is_set():
        try:
            db.kv_set(f"thread_beat:{threading.current_thread().name}", now())
            wait = pool_turn(name, statuses, kinds, worker)
        except Exception as e:  # a bad turn (new code included) must not end the thread
            log("pool: turn failed", worker=worker, err=f"{type(e).__name__}: {e}"[:200])
            wait = 10
        STOP.wait(wait)


def pool_turn(name: str, statuses: list[str], kinds: list[str] | None, worker: str) -> float:
    """One claim and its stage; returns the seconds to wait before the next turn."""
    if paused():
        return 10
    allowed = list(statuses)
    if not kinds and name in ("fast", "quick", "claude"):
        # the live stage table, not the list this thread was born with: a stage added later is claimed without a
        # restart
        live = sys.modules["ap.worker"].STAGES
        allowed = [s for s, (p, _) in live.items() if p == name]
    if RESTART_FLAG.exists():
        # a graceful restart only waits for long code sessions; everything else keeps flowing
        #
        allowed = [s for s in allowed if s not in ("build", "fix")]
        if not allowed:
            return 10
    if name == "fast" and muse_paused():
        allowed = [s for s in allowed if s in ("gate", "publish", "ci_wait", "waiting", "review_prepare", "review_post")]
    off_hours = None
    if name in ("claude", "quick"):
        # gate per budget pool: only claim stages whose pool still has room (a single generic role
        # sat in the judgment pool and froze every build when judgment ran out, 2026-09-25)
        roles = {"build": "build", "fix": "fix", "self_review": "self_verify", "verify_done": "verify_done",
                 "critique": "critique", "pr_body_refresh": "publish_text", "comments_verify": "comments_verify",
                 "review_verify": "review_verify", "announce_check": "publish_text", "telemetry": "telemetry"}
        paced = [s for s in allowed if claude.can_spend(roles.get(s, "verify"), urgent=True)]
        if len(paced) < len(allowed) and not kinds:
            # his own PRs (reviewer comments, CI) are exempt from pacing: claim them even in a budget-blocked stage
            #
            try:
                own = db.claim_next([s for s in allowed if s not in paced], worker, kinds=["CI", "COMMENTS"])
            except Exception:
                own = None
            if own:
                try:
                    run_stage(own)
                except Exception as e:
                    log("pool: run_stage escaped", worker=worker, item=own["id"], err=f"{type(e).__name__}: {e}"[:200])
                    db.release(own["id"])
                return 0
        allowed = paced
        if not allowed:
            return 60
        if not work_hours():
            # off hours: delivery stages, and the other stages for an urgent case only. Narrowing the stages to
            # delivery before looking at urgency left no urgent case outside them claimable (10-16/17: schools#1091
            # and cometa#3098, urgency 3, sat in review_verify from 23:18 and 01:47 while this pool ran critiques)
            off_hours = [s for s in allowed if s not in db.OFF_HOURS_STAGES]
    try:
        it = db.claim_next(allowed, worker, kinds=kinds, urgent_only=off_hours)
    except Exception as e:  # a transient sqlite lock must never kill a pool thread
        log("pool: claim failed", worker=worker, err=str(e)[:160])
        return 5
    if not it:
        return 5 if off_hours is None else 60
    try:
        run_stage(it)
    except Exception as e:
        log("pool: run_stage escaped", worker=worker, item=it["id"], err=f"{type(e).__name__}: {e}"[:200])
        db.release(it["id"])
    return 0


# ---- periodic jobs --------------------------------------------------------------
def every(key: str, seconds: int) -> bool:
    last = db.kv_get(f"last:{key}", 0)
    if now() - last >= seconds:
        db.kv_set(f"last:{key}", now())
        return True
    return False


def daily_at(key: str, hhmm: str) -> bool:
    h, m = map(int, hhmm.split(":"))
    t = dt.datetime.now()
    if (t.hour, t.minute) < (h, m) or db.kv_get(f"day:{key}") == db.today():
        return False
    db.kv_set(f"day:{key}", db.today())
    return True


def track_awaiting() -> None:
    for r in db.q("SELECT id FROM items WHERE status='awaiting_human'"):
        it = db.item(r["id"])
        try:
            if it["kind"] == "REVIEW":
                reviews.track(it)
            elif it["kind"] == "REPLY":
                replies.track_reply(it)
            elif it["kind"] in ("TASK", "INCIDENT", "IDEA") and it["data"].get("pr_links"):
                states = [gh.pr_view(p["repo"], p["pr"])["state"]
                          for p in it["data"]["parts"] if p.get("pr")]
                if states and all(s != "OPEN" for s in states):
                    kind = "pr_merged" if all(s == "MERGED" for s in states) else "pr_closed"
                    db.add_signal(kind, {"prs": it["data"]["pr_links"]}, it["id"])
                    db.update_item(it["id"], status="done", note=kind)
            elif now() - it["updated_at"] > 4 * 86400:
                db.update_item(it["id"], status="expired", note="4 days without action")
        except Exception as e:
            log("track failed", item=it["id"], err=str(e)[:200])


def noop_audit() -> None:
    start = int(dt.datetime.combine(dt.date.today(), dt.time()).timestamp())
    rows = db.q("SELECT s.data, e.id eid, e.text, e.channel, e.user, e.thread_ts, e.ts FROM signals s JOIN events e "
                "ON e.id=json_extract(s.data,'$.event_id') WHERE s.kind='triage' AND s.at>=? AND "
                "json_extract(s.data,'$.class') IN ('IGNORE','INFO')", start)
    if not rows:
        return
    items = [{"event_id": r["eid"], "text": r["text"][:800], "why_ignored": json.loads(r["data"]).get("reason")} for r in rows[:80]]
    # the class is the lane the case starts in, so only lanes exist (the vet's): a free-text class (INFO, BUG,
    # SQUAD_REQUEST) used to start a reply draft, whose question is 'does anyone wait on his answer', and 7 of 8
    # audit cases were dropped on it
    schema = {"type": "object", "required": ["overturned"], "properties": {"overturned": {"type": "array", "items": {
        "type": "object", "required": ["event_id", "class", "reason"], "properties": {
            "event_id": {"type": "integer"}, "class": {"type": "string", "enum": list(tasks.VET_START)},
            "reason": {"type": "string"}}}}}}
    out = claude.lean("noop_audit", json.dumps(items, ensure_ascii=False), cwd=str(VAR), schema=schema, tools=())
    for o in out["overturned"]:
        ev = db.q1("SELECT * FROM events WHERE id=?", o["event_id"])
        if not ev or o["class"] not in tasks.VET_START:
            continue
        db.add_signal("triage_overturned", {"event_id": ev["id"], "to": o["class"], "reason": o["reason"]})
        tk = f"slack:{ev['channel']}:{ev['thread_ts'] or ev['ts']}"
        if not db.open_item_for_thread(tk):
            db.create_item(o["class"], tasks.VET_START[o["class"]], f"(audit) {o['reason'][:70]}", tk, urgency=2,
                           data={"channel": ev["channel"], "ts": ev["ts"], "thread_ts": ev["thread_ts"] or ev["ts"],
                                 "text": ev["text"][:4000], "author": ev["user"], "summary": o["reason"],
                                 "proposal": o["class"] == "IDEA"})


JOB_EVERY = {"triage": 60, "context": 900, "backfill": 3600, "patrol": 3600, "ingest-slack": 60,
             "ingest-mentions": 180, "ingest-github": 180, "babysit": 240, "approvals": 45, "board": 60}
STALL_S = {"build": 7200, "fix": 7200, "self_review": 3600, "self_find": 5400, "comments_verify": 3600,
           "reply_draft": 1200, "vet": 1200, "pr_body_refresh": 1800,
           "reply_check": 1200, "propose_check": 3600, "review_find": 1800, "telemetry": 3600,
           "investigate": 3600, "plan": 3600, "critique": 3600, "gate": 3600, "publish": 1800, "ci_fix": 3600,
           "comments_triage": 3600, "comments_reply": 3600, "review_verify": 3600,
           "review_prepare": 3600, "propose": 3600, "announce": 3600, "announce_check": 3600,
           "verify_done": 3600, "waiting": 7200, "ci_wait": 7200}
STARTED_AT = now()


_JOBS_SEEN: set[str] = set()


def _drop_nested_children() -> int:
    """Cases nested two or more levels (…:idea:idea, …:idea:verify) are products of the spawn loop fixed
    on 2026-09-28; the root case still owns the thread. Asks already in front of the user stay."""
    n = 0
    for r in db.q("SELECT id, kind, status, thread_key, data FROM items WHERE status NOT IN ('done','dropped','expired','failed')"):
        depth = len(re.findall(r":idea|:verify", r["thread_key"] or ""))
        if not depth or any(p.get("pr_url") for p in json.loads(r["data"] or "{}").get("parts", [])):
            continue
        root_key = re.sub(r"(:idea|:verify)+$", "", r["thread_key"])
        root_reply = db.q1("SELECT id FROM items WHERE kind='REPLY' AND thread_key=? AND status IN "
                           "('reply_draft','awaiting_approval','awaiting_human','done')", root_key)
        loop_product = depth >= 2 or (r["kind"] == "REPLY" and root_reply)  # a REPLY under an IDEA is the loop itself
        if not loop_product or (r["status"] in ("awaiting_approval", "awaiting_human") and not (r["kind"] == "REPLY" and root_reply)):
            continue
        for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", r["id"]):
            root = root_reply['id'] if root_reply else '?'
            approvals.supersede(a, json.loads(a["content"] or "{}"), site.t(f"duplicate: case #{root} handles the thread",
                                                                            f"duplicado: el hilo lo lleva el caso #{root}"))
        db.update_item(r["id"], status="dropped", note=site.t(
            f"product of the nested-case loop; {'#' + str(root_reply['id']) if root_reply else 'the root case'} handles it",
            f"producto del loop de casos anidados; lo lleva #{root_reply['id'] if root_reply else 'el caso raíz'}"))
        from . import dm
        dm.close_case_thread(db.item(r["id"]), site.t("duplicate", "duplicado"))
        n += 1
    return n


def dedupe_items() -> int:
    _drop_nested_children()
    """Self-healing dedupe: two open items about the same conversation or with near-identical
    titles collapse into the older one (the newer's text becomes an update). Zero tokens."""
    n = 0
    rows = db.q("SELECT id, kind, status, title, thread_key, data FROM items WHERE status NOT IN ('done','dropped','expired','failed') "
                "AND kind IN ('TASK','INCIDENT','IDEA','OPEN_LOOP','REPLY') ORDER BY id")
    seen: dict[str, dict] = {}
    # a CI item and a COMMENTS item on the same PR both end up fixing that PR's CI: the COMMENTS one
    # (which also answers threads) covers the CI one
    prs = db.q("SELECT id, kind, repo, pr FROM items WHERE status NOT IN ('done','dropped','expired','failed') "
               "AND kind IN ('CI','COMMENTS') AND pr IS NOT NULL ORDER BY id")
    by_pr: dict[tuple, list] = {}
    for r in prs:
        by_pr.setdefault((r["repo"], r["pr"]), []).append(r)
    for key, group in by_pr.items():
        if len(group) > 1 and any(g["kind"] == "COMMENTS" for g in group):
            keeper = [g for g in group if g["kind"] == "COMMENTS"][-1]
            for g in group:
                if g["id"] != keeper["id"]:
                    db.update_item(g["id"], status="dropped", note=site.t(f"covered by #{keeper['id']} (same PR)",
                                                                          f"lo cubre #{keeper['id']} (mismo PR)"))
                    n += 1
    # REPLY cases keyed by different message ts of ONE real thread collapse into the oldest
    roots: dict[tuple, dict] = {}
    for r in rows:
        if r["kind"] != "REPLY":
            continue
        d0 = json.loads(r["data"] or "{}")
        if not (d0.get("channel") and d0.get("ts")) or d0.get("dm"):
            continue
        real = (d0.get("raw") or {}).get("root") or d0.get("thread_ts") or d0["ts"]
        key = (d0["channel"], real)
        if key in roots:
            keeper = roots[key]
            db.update_item(r["id"], status="dropped", note=site.t(f"duplicate of #{keeper['id']} (same thread)",
                                                                  f"duplicado de #{keeper['id']} (mismo hilo)"))
            from . import dm
            dm.close_case_thread(db.item(r["id"]), site.t("duplicate", "duplicado"))
            n += 1
        else:
            roots[key] = dict(r)
    # verification children of cases about ONE real thread collapse into the one furthest along
    kids: dict[tuple, dict] = {}
    order = ["awaiting_approval", "propose_check", "propose", "announce_check", "announce", "verify_done", "publish", "build",
             "critique", "plan", "investigate", "telemetry", "vet"]
    for r in rows:
        d0 = json.loads(r["data"] or "{}")
        if not d0.get("parent") or any(p.get("pr_url") for p in d0.get("parts", [])):
            continue
        par = db.item(d0["parent"])
        pd = (par or {}).get("data") or {}
        if not pd.get("channel"):
            continue
        key = (pd["channel"], (pd.get("raw") or {}).get("root") or pd.get("thread_ts") or pd.get("ts"))
        cur = kids.get(key)
        if cur is None:
            kids[key] = dict(r)
            continue
        rank = lambda s: order.index(s) if s in order else len(order)
        loser, keeper = (cur, dict(r)) if rank(r["status"]) < rank(cur["status"]) else (dict(r), cur)
        kids[key] = keeper
        db.update_item(loser["id"], status="dropped", note=site.t(f"duplicate of #{keeper['id']} (same conversation)",
                                                                  f"duplicado de #{keeper['id']} (misma conversación)"))
        for a in db.q("SELECT id FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", loser["id"]):
            db.set_artifact_outcome(a["id"], "superseded", {"why": site.t("duplicate", "duplicado")})
        from . import dm
        dm.close_case_thread(db.item(loser["id"]), site.t("duplicate", "duplicado"))
        n += 1
    for r in rows:
        d0 = json.loads(r["data"] or "{}")
        if d0.get("parent") or r["thread_key"].endswith(":verify"):
            continue  # a verification child shares its parent's title on purpose
        if d0.get("from") == "ctx" and not d0.get("channel"):
            # a context-derived gap with no Slack reference is an unevidenced guess; those were
            # re-created in new wording every 15 min and never matched the real case
            db.update_item(r["id"], status="dropped", note=site.t("no Slack reference: not evidence", "sin referencia a Slack: no es evidencia"))
            n += 1
            continue
        dup = None
        if r["thread_key"] in seen and not r["thread_key"].endswith(":verify"):
            dup = seen[r["thread_key"]]
        elif r["kind"] != "REPLY":
            # a reply case is its conversation (the roots pass above), never its title: a title is the message's first
            # 80 chars, and two threads tagging @the team share most of them (2026-10-15: #869, a teammate asking him to
            # review two PR stacks, dropped as 'duplicado de #815' from another channel after its ask; #861 into #398)
            s = db.similar_recent_item(r["title"], days=14, threshold=0.6, exclude_id=r["id"],
                                       kinds=("TASK", "INCIDENT", "IDEA", "OPEN_LOOP"))
            if s and s["id"] < r["id"] and s["status"] not in db.TERMINAL:
                dup = s
        if dup:
            d = json.loads(r["data"] or "{}")
            old = db.item(dup["id"])
            ups = old["data"].get("updates", []) + [{"text": (d.get("text") or r["title"])[:1500], "from_item": r["id"]}]
            db.update_item(dup["id"], data={"updates": ups[-20:]})
            db.update_item(r["id"], status="dropped", note=site.t(f"duplicate of #{dup['id']}", f"duplicado de #{dup['id']}"))
            n += 1
        else:
            seen.setdefault(r["thread_key"], dict(r))
    if n:
        log("dedupe: collapsed duplicates", n=n)
    return n


PAUSE_WORDS = {"budget": ("budgetexhausted", "spend limit", "usage limit"), "limit": ("claudelimited", "session limit", "rate limit"),
               "network": ("enotfound", "could not resolve", "reach the api", "network", "unable to access", "connection",
                           "nodename nor servname")}


def block_retry(d: dict) -> str | None:
    """How the watchdog retries a block: 'network', 'retry' or None. Only a FAILURE is retried, a stage that raised
    (blocked_stage) or a write the guard refused (blocked_by): its reason is the error itself. A verdict block (fix rounds
    exhausted, nothing to push, CI still red) quotes the output it judged, and those words are evidence, not its cause;
    the escalation takes it. 2026-10-19: #776's 'fix: ninguna ronda dejó un commit' quoted a vitest 'Test timed out' and
    was reopened into the same block twice; #917/#920/#921 re-ran a gate with no fix round left, 6 times, same verdict."""
    if d.get("harness_bug_rev") or not (d.get("blocked_stage") or d.get("blocked_by") == "guard"):
        return None
    why = (d.get("blocked_reason") or "").lower()
    if any(w in why for w in PAUSE_WORDS["network"]):
        return "network"
    transient = any(w in why for w in ("timeout", "timed out", "rejected", "502", "503", "rate limit", "temporar",
                                       "push failed", "error connecting"))
    return "retry" if (d.get("blocked_stage") or transient) and d.get("auto_retries", 0) < 2 else None


def pr_digest_job() -> None:
    from . import digest
    text = digest.pr_digest()
    if text:
        outbound.ping(text)


def knowledge_commit() -> None:
    """Hourly: commit what the harness learned (knowledge/, rules/, style guide) with --no-verify.
    Runtime paths never commit on their own: the pre-commit hook runs the selftest."""
    from . import site
    from .core import DATA, run
    if not site.self_improving() or not (DATA / ".git").exists():
        return  # only an install that learns, and keeps what it learns in git, has anything to commit
    r = run(["git", "-C", str(DATA), "status", "--porcelain", "knowledge", "rules", "prompts/style"], timeout=60)
    if not r.stdout.strip():
        return
    run(["git", "-C", str(DATA), "add", "knowledge", "rules", "prompts/style"], timeout=60)
    # the repo's own git identity signs it: a hardcoded one overrode the repo-local config, so every 'learned:' commit
    # since 10-22 still carried the work address the repo had been moved off
    run(["git", "-C", str(DATA), "commit", "-q", "--no-verify", "-m", f"learned: knowledge, rules and style ({db.today()})"],
        timeout=120)


def _ps_rows() -> list:
    ps = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,etime=,command="], capture_output=True, text=True).stdout
    return [l.split(None, 3) for l in ps.splitlines() if l.strip()]


def park_orphan_sessions(rows: list) -> tuple[list[str], set[str]]:
    """Claude sessions a dead worker started keep running under launchd. The run registry (core.RUNS) says, from the
    spawn, which case and directory each is for: a code session (in a worktree) of an open case parks the case under
    it (2d releases it when the session ends, and the next stage resumes from the worktree), any other one has nobody
    to report to and dies. 10-07: the guard regex-parsed the prompt for the worktree (it caught
    'schools--skills-mixed-mode-409,', and a resumed session's prompt names no path) and looked it up in parts, which
    store the path only after the build, so after the 13:43 restart #595 was built again on the worktree orphan 54100
    was still writing. Returns notes and the pids handled here."""
    from . import core
    leaders = {r[0]: r for r in rows}
    notes, handled = [], set()
    for f in core.RUNS.glob("*"):
        try:
            meta = json.loads(f.read_text())
            pid, owner = f.name, int(meta["by"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not meta.get("cmd", "").startswith(core.ENGINE_CMDS) or owner == os.getpid() or core._alive(owner):
            continue
        row = leaders.get(pid)
        if not row or row[1] != "1" or not row[3].startswith(("claude", "codex")):
            f.unlink(missing_ok=True)  # the session ended (or the pid is someone else's now)
            continue
        handled.add(pid)
        if "cwd" not in meta:  # registered before sessions carried their case: left to finish
            continue
        it = db.item(meta["item"]) if meta.get("item") else None
        if it and it["status"] not in ("dropped", "failed", "done") and meta["cwd"].startswith(str(WORKTREES)):
            db.x("UPDATE items SET claimed_by=?, claimed_at=? WHERE id=? AND (claimed_by IS NULL OR claimed_by LIKE 'orphan:%')",
                 f"orphan:{pid}", now(), it["id"])
            continue
        try:  # a judgment session, or its case is over: nobody will read its answer
            os.kill(int(pid), signal.SIGKILL)
            notes.append(site.t(f"killed orphan session {pid} ({meta.get('role')}, #{meta.get('item')})",
                                f"maté sesión huérfana {pid} ({meta.get('role')}, #{meta.get('item')})"))
        except OSError:
            pass
        f.unlink(missing_ok=True)
    return notes, handled


def orphan_overdue(pid: int) -> bool:
    """Whether an orphaned session outlived the timeout its dead worker gave it, in awake seconds since the spawn. A
    mark written before marks carried both gets its file time and claude.build's default."""
    f = RUNS / str(pid)
    try:
        meta = json.loads(f.read_text())
        started = meta.get("at") or f.stat().st_mtime
    except (OSError, ValueError):
        return False
    return hot.awake_since(started, now()) > (meta.get("timeout") or 5400)


def tend_orphan_claims() -> list[str]:
    """Cases parked under an orphaned code session: heartbeat while it runs, release when it is gone. Its timeout died
    with the worker that started it, so it gets it back here (orphan_overdue): 10-13, #717's build and #727's reproduce,
    orphaned by the 11:42 kill, still ran at 14:16, and the reaper, killing by thread name, logged 'procs 0' every hour."""
    notes = []
    for r in db.q("SELECT id, claimed_by, attempts FROM items WHERE claimed_by LIKE 'orphan:%'"):
        pid = int(r["claimed_by"].split(":", 1)[1])
        try:
            os.kill(pid, 0)
            if orphan_overdue(pid):
                os.killpg(pid, signal.SIGKILL)
                (RUNS / str(pid)).unlink(missing_ok=True)
                db.update_item(r["id"], attempts=(r["attempts"] or 0) + 1, next_run_at=0,
                               note=site.t("the orphan session ran past its timeout: stopped it, resuming from the worktree",
                                           "la sesión huérfana pasó su timeout: la corté, retomo desde el worktree"))
                db.release(r["id"])
                notes.append(site.t(f"#{r['id']}: orphan session {pid} ran past its timeout, stopped",
                                    f"#{r['id']}: sesión huérfana {pid} pasó su timeout, cortada"))
                continue
            db.x("UPDATE items SET claimed_at=? WHERE id=?", now(), r["id"])
        except OSError:
            db.update_item(r["id"], attempts=0, next_run_at=0, note=site.t("the orphan session ended: resuming from the worktree",
                                                                           "la sesión huérfana terminó: retomo desde el worktree"))
            db.release(r["id"])
            notes.append(site.t(f"#{r['id']}: orphan session {pid} ended, resuming", f"#{r['id']}: sesión huérfana {pid} terminó, retomo"))
    return notes


def process_hygiene() -> list[str]:
    """Zero-token: the harness must never eat the machine. Count our descendants, kill known leak shapes, sweep our
    Docker test containers, and report anything abnormal."""
    import subprocess as sp
    rows = _ps_rows()
    notes, handled = park_orphan_sessions(rows)
    kids = {}
    for pid, ppid, et, cmd in rows:
        kids.setdefault(ppid, []).append((pid, et, cmd))
    mine, stack = [], [str(os.getpid())]
    while stack:
        p = stack.pop()
        for pid, et, cmd in kids.get(p, []):
            mine.append((pid, et, cmd))
            stack.append(pid)
    total = len(rows)
    from . import core
    notes += core.reap_orphans(rows)  # gate tools (tsc, eslint, vitest...) a dead worker left running
    if len(mine) > 120 or total > 1500:
        notes.append(site.t(f"{len(mine)} harness child processes, {total} in total", f"{len(mine)} procesos hijos del harness, {total} en total"))
    # known leak shapes anywhere in the session: stray caffeinate, selftests or hooks not started by us right now
    for pid, ppid, et, cmd in rows:
        # orphaned harness Claude sessions are park_orphan_sessions' (a code session keeps running to its end with
        # its case parked under it: 2026-09-28, killing them rebuilt three items from zero, ~$60); an unregistered
        # one in a worktree is left to finish
        if pid in handled or (ppid == "1" and cmd.startswith("claude -p") and str(WORKTREES) in cmd):
            continue
        if ppid == "1" and cmd.startswith("claude -p") and "/autopilot/" in cmd:
            try:  # judgment sessions (no worktree) orphaned by a restart: nobody will read their answer
                os.kill(int(pid), signal.SIGKILL)
                notes.append(site.t(f"killed orphan session {pid}: {cmd[:50]}", f"maté sesión huérfana {pid}: {cmd[:50]}"))
            except Exception:
                pass
            continue
        ours = cmd.strip() == "caffeinate -i" or "ap.selftest" in cmd or "hooks/pre-commit" in cmd
        old = ":" in et and (et.count(":") == 2 or "-" in et or int(et.split(":")[0]) >= 10)  # >= 10 min
        if ours and ppid == "1" and old:  # only OUR shapes, only orphans, only old ones (never the user's own caffeinate)
            try:
                os.kill(int(pid), signal.SIGKILL)
                notes.append(site.t(f"killed orphan process {pid}: {cmd[:50]}", f"maté proceso huérfano {pid}: {cmd[:50]}"))
            except Exception:
                pass
    try:  # our own docker test containers must not outlive their gate
        out = sp.run(["docker", "ps", "--filter", "name=ap-", "--format", "{{.Names}} {{.RunningFor}}"], capture_output=True, text=True,
                     timeout=30, env=__import__("ap.core", fromlist=["x"]).env()).stdout
        for line in out.splitlines():
            name, age = line.split(" ", 1)
            if "hour" in age or "day" in age:
                sp.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60, env=__import__("ap.core", fromlist=["x"]).env())
                notes.append(site.t(f"test container {name} was up {age}: removed", f"contenedor de test {name} llevaba {age}: eliminado"))
    except Exception:
        pass
    if notes:
        log("process hygiene", notes=notes[:6])
    return notes


def _pauses() -> dict[str, str]:
    """Reasons the system is legitimately waiting. These are announced once as a pause, never as
    'trabado'."""
    out = {}
    pu = claude.paused_until()
    if now() < pu:
        until = db.fmt_time(pu)[-5:]
        out["limit"] = site.t(f"Claude paused by its usage limit until {until}", f"Claude en pausa por límite de uso hasta las {until}")
    capped = [m for m in ("fable", "opus", "sonnet") if now() < db.kv_get(f"model_limited:{CFG['claude'][m]}", 0)]
    if capped:
        out["capped"] = site.t(f"{', '.join(capped)} reached the monthly spend cap; continuing with the other models",
                               f"{', '.join(capped)} llegó al tope mensual de gasto; sigo con los otros modelos")
    c = CFG["claude"]
    spent = db.spent_today("claude")
    if spent + 2 > c["daily_usd_cap"]:
        out["budget"] = site.t(
            f"out of the daily Claude budget (${spent:.0f} of ${c['daily_usd_cap']}); urgent work goes on with the "
            f"${c['urgent_overdraft_usd']} overdraft, the rest resumes at midnight",
            f"sin presupuesto diario de Claude (${spent:.0f} de ${c['daily_usd_cap']}); lo urgente sigue con el "
            f"sobregiro de ${c['urgent_overdraft_usd']}, el resto reanuda a medianoche")
    recent_err = [db.kv_get(f"job_err:{n}") or {} for n in JOB_EVERY]
    net = [e for e in recent_err if e.get("at", 0) > now() - 900 and any(w in e.get("err", "").lower() for w in PAUSE_WORDS["network"])]
    if len(net) >= 2:
        since = db.fmt_time(min(e['at'] for e in net))[-5:]
        out["network"] = site.t(f"no network since {since} (Slack, GitHub and the API do not resolve)",
                                f"sin red desde las {since} (Slack/GitHub/API no resuelven)")
    if muse.pick_model() is None:
        out["free"] = site.t("the free models are rate limited; reviews and patrols wait",
                             "los modelos gratis están con rate limit; las revisiones y patrullas esperan")
    return out


def blocked_caveats(d: dict) -> list:
    """A blocked case delivered with its PR keeps the caveats it had and adds the block."""
    return list(dict.fromkeys([*(d.get("caveats") or []), (d.get("blocked_reason") or "")[:300]]))


def deliver_blocked() -> list[str]:
    """A blocked case that already opened its PR is delivered with caveats, once its block's escalation has decided:
    the escalation may still retake it with instructions, and delivered first that verdict landed on a parked case that
    never reads them. An IDEA is never escalated, and a block held for
    the work account is delivered as before."""
    from . import escalate
    notes = []
    for r in db.q("SELECT id FROM items WHERE status='blocked' AND kind IN ('TASK','INCIDENT','IDEA')"):
        it = db.item(r["id"])
        d = it["data"]
        if not any(p.get("pr_url") for p in d.get("parts", [])):
            continue
        reason = (d.get("blocked_reason") or "")[:300]
        if it["kind"] != "IDEA" and not escalate._escalated(it) and d.get("escalation_held") != reason:
            continue
        rec = d.get("repro") or {}
        if rec.get("verdict") == "reproduced" and not rec.get("green_at"):
            # the reproduction is the definition of done: no PR is delivered without its final run, even a blocked one
            # (2026-10-20: TICKET-1520's schools#1104 and cometa#3057 went blocked > announce and never ran it, so their
            # PR screens said 'el fix todavía no pasó la prueba'; both passed when run)
            db.update_item(r["id"], status="verify_done", attempts=0, next_run_at=0,
                           note="PR exists: run the reproduction, then deliver with caveats",
                           data={"caveats": blocked_caveats(d), "blocked_reason": None, "deliver_blocked": True})
            notes.append(site.t(f"#{r['id']}: it had an open PR, running the reproduction and delivering it",
                                f"#{r['id']}: tenía PR abierto, corro la reproducción y lo entrego"))
            continue
        db.update_item(r["id"], status="announce", attempts=0, next_run_at=0, note=approvals.BLOCK_DELIVERED,
                       data={"caveats": blocked_caveats(d), "blocked_reason": None})
        notes.append(site.t(f"#{r['id']}: it had an open PR, delivering it", f"#{r['id']}: tenía PR abierto, lo entrego"))
    return notes


def watchdog() -> None:
    """Zero-token supervision: release dead claims, retry transient blocks, collapse duplicates,
    tell the difference between stuck, queued and paused, and alert once per real problem."""
    dedupe_items()
    try:
        claude.probe_limited()
    except Exception as e:
        log("probe_limited failed", err=str(e)[:100])
    try:
        if now() - db.kv_get("dedupe_prs_at", 0) > 1800:
            db.kv_set("dedupe_prs_at", now())
            dedupe_prs()
    except Exception as e:
        log("dedupe_prs failed", err=str(e)[:120])
    try:
        from . import dm
        dm.reconcile()
        dm.keep_board_last()
    except Exception as e:
        log("dm reconcile failed", err=str(e)[:120])
    problems, notes = [], []
    hyg = process_hygiene()
    # process_hygiene's own notes, as it (or core.reap_orphans) wrote them in either language
    notes += [n for n in hyg if any(w in n for w in ("maté", "eliminado", "killed", "removed"))]
    problems += [n for n in hyg if "procesos" in n or "processes" in n]
    pauses = _pauses()
    # 1. items past their stage's normal time. Claimed by a live worker for that long = stuck (release
    #    the claim so another thread retries). Unclaimed = queued behind capacity or a pause: only a
    #    problem after 4 h with no pause explaining it.
    for r in db.q("SELECT id, status, title, updated_at, claimed_by, claimed_at FROM items WHERE status IN (%s)"
                  % ",".join("?" * len(STALL_S)), *STALL_S):
        age = now() - r["updated_at"]
        if age < STALL_S[r["status"]]:
            db.kv_set(f"stuck:{r['id']}:{r['status']}", 0)
            continue
        key = f"stuck:{r['id']}:{r['status']}"
        if r["claimed_by"] and now() - (r["claimed_at"] or 0) > STALL_S[r["status"]]:
            db.update_item(r["id"], claimed_by=None, claimed_at=None, next_run_at=0, note="watchdog: stale claim released")
            notes.append(site.t(f"#{r['id']}: released a dead claim in {r['status']}", f"#{r['id']}: liberé un claim muerto en {r['status']}"))
            n = db.kv_get(key, 0) + 1
            db.kv_set(key, n)
            if n >= 3:
                problems.append(site.t(f"#{r['id']} keeps getting stuck in {r['status']} ({n} times, {age // 60} min): {r['title'][:60]}",
                                       f"#{r['id']} se traba en {r['status']} ({n} veces, {age // 60} min): {r['title'][:60]}"))
        elif not r["claimed_by"] and age > 4 * 3600 and not pauses and work_hours():
            problems.append(site.t(f"#{r['id']} waits its turn in {r['status']}: {r['title'][:60]}",
                                   f"#{r['id']} espera turno en {r['status']}: {r['title'][:60]}"))
    # 2. periodic jobs: a problem only when a job RAN and keeps failing (a job with nothing to do does
    #    not run and is fine); failures that are pauses are reported as pauses
    for name, every_s in JOB_EVERY.items():
        ok, err = db.kv_get(f"job_ok:{name}", 0), db.kv_get(f"job_err:{name}") or {}
        started = db.kv_get(f"job_started:{name}", 0)
        if err.get("at", 0) > ok and now() - ok > 3 * every_s:
            e = err.get("err", "").lower()
            if any(w in e for kind in PAUSE_WORDS.values() for w in kind):
                continue  # covered by _pauses()
            problems.append(site.t(f"the {name} job fails: {err.get('err', '')[:90]}", f"el job {name} falla: {err.get('err', '')[:90]}"))
        elif started and now() - started > max(3 * every_s, 3600) and ok < started:
            problems.append(site.t(f"the {name} job runs without finishing", f"el job {name} corre sin terminar"))
    # 2b. a blocked item that already opened its PR is delivered: announce it, caveats included
    notes += deliver_blocked()
    # 2b'. an expired ask never retires a case whose PR is still open: it waits for him on the board
    for r in db.q("SELECT id, data FROM items WHERE status='expired' AND kind IN ('TASK','INCIDENT','IDEA')"):
        d = json.loads(r["data"] or "{}")
        open_pr = False
        for p in d.get("parts", []):
            if p.get("pr_url") and p.get("pr"):
                try:
                    open_pr = open_pr or gh.pr_view(p["repo"], p["pr"]).get("state") == "OPEN"
                except Exception:
                    pass
        if open_pr:
            db.update_item(r["id"], status="awaiting_human", attempts=0, note=site.t("open PR: stays on the board", "PR abierto: sigue en el tablero"))
            notes.append(site.t(f"#{r['id']}: the ask expired but the PR is still open", f"#{r['id']}: expiró el pedido pero el PR sigue abierto"))
    # 2b''. dropped as 'covered' by a bot's PR (CusCopilot): bots never own a fix, we build ours
    for r in db.q("SELECT id, data FROM items WHERE status='dropped' AND kind IN ('TASK','INCIDENT','IDEA') AND updated_at>?", now() - 7 * 86400):
        d = json.loads(r["data"] or "{}")
        note = (db.q1("SELECT note FROM transitions WHERE item_id=? ORDER BY at DESC LIMIT 1", r["id"]) or {"note": ""})["note"] or ""
        dup = next((p for p in d.get("related_prs") or [] if p.get("relation") == "duplicate"), None)
        # the 'covered' note as cover, pregate and dedupe write it, in either language
        if note.startswith(COVERED) and dup and tasks._bot_author(dup.get("author", "")) and not d.get("bot_cover_retried"):
            db.update_item(r["id"], status="plan", attempts=0, next_run_at=0, note=site.t(
                "the PR that covered it is a bot's: building our own", "el PR que lo cubría es de un bot: construimos el nuestro"),
                           data={"related_checked_at": None, "related_prs": [], "bot_cover_retried": True})
            notes.append(site.t(f"#{r['id']}: back to plan, {dup.get('author')}'s PR is a bot's",
                                f"#{r['id']}: vuelve a plan, el PR de {dup.get('author')} es de un bot"))
    # 2d. items parked under an orphaned code session
    notes += tend_orphan_claims()
    # 2e. an open loop about unifying/closing PRs is over once the harness closed one of them as a duplicate
    for r in db.q("SELECT id, title FROM items WHERE kind='OPEN_LOOP' AND status IN ('awaiting_human','loop_track')"):
        names = "|".join(re.escape(n) for n in sorted(site.repos(), key=len, reverse=True))
        nums = re.findall(rf"({names})?#(\d{{2,5}})", r["title"]) if names else []
        closed = []
        for repo, n in nums:
            # a bare #123 is of the repo the title names, else of the primary one
            repo = repo or next((x for x in site.repos() if x != site.primary_repo() and x in r["title"]), site.primary_repo())
            # dedupe_prs's note, in either language
            if db.q1("SELECT 1 FROM transitions WHERE note LIKE ? OR note LIKE ?", "PR duplicado de #%", "duplicate PR of #%") and db.q1(
                    "SELECT 1 FROM items WHERE status='dropped' AND json_extract(data,'$.parts') LIKE ?", f"%/{repo}/pull/{n}%"):
                closed.append(f"{repo}#{n}")
        if closed:
            db.update_item(r["id"], status="done", note=site.t(f"resolved: {', '.join(closed)} closed as a duplicate",
                                                               f"resuelto: {', '.join(closed)} cerrado como duplicado"))
            from . import dm
            dm.close_case_thread(db.item(r["id"]), site.t("resolved", "resuelto"))
            notes.append(site.t(f"#{r['id']}: loop closed, {', '.join(closed)} no longer exists",
                                f"#{r['id']}: loop cerrado, {', '.join(closed)} ya no existe"))
    # 2f. harness PRs still in draft: ready as soon as CI is green and no thread is open (prs_ready_directly)
    if CFG.get("prs_ready_directly", True):
        for r in db.q("SELECT id, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('dropped','failed') AND json_extract(data,'$.parts') LIKE '%pr_url%'"):
            d = json.loads(r["data"] or "{}")
            for p in d.get("parts", []):
                if not (p.get("pr") and p.get("pr_url")) or p.get("readied"):
                    continue
                st = (db.kv_get(f"pr:{p['repo']}#{p['pr']}") or {})
                s = st.get("summary") or {}
                if s.get("draft") and s.get("green") and not st.get("unresolved_threads"):
                    if outbound.mark_ready(p["repo"], p["pr"]):
                        p["readied"] = now()
                        db.update_item(r["id"], data={"parts": d["parts"]})
                        notes.append(f"{p['repo']}#{p['pr']}: draft -> ready")
    # 2g. a case keyed by demand/ticket whose ticket a live Slack case is about is the same work
    notes += ticket_duplicates()
    # 2d. blocked by the old 3-day wait cap: waiting re-checks it and blocks again only when no dependency is on its way
    # (only an older version wrote that reason, and only in Spanish)
    for r in db.q("SELECT id FROM items WHERE status='blocked' AND json_extract(data,'$.blocked_reason') LIKE 'esperando hace 3 días: %'"):
        db.update_item(r["id"], status="waiting", next_run_at=0, note=site.t("the wait goes on while the dependency moves",
                                                                            "la espera sigue mientras la dependencia avance"),
                       data={"blocked_reason": None})
        notes.append(site.t(f"#{r['id']}: back to waiting for its dependency", f"#{r['id']}: vuelve a esperar su dependencia"))
    # 3. blocked by a stage failure or a transient error: retry twice (15 min for network, 2 h otherwise)
    for r in db.q("SELECT id, data, updated_at FROM items WHERE status='blocked' AND updated_at<?", now() - 900):
        d = json.loads(r["data"] or "{}")
        stage, why = d.get("blocked_stage"), (d.get("blocked_reason") or "").lower()
        retry = block_retry(d)
        if retry != "network" and r["updated_at"] > now() - 7200:
            continue
        if retry == "network":
            db.update_item(r["id"], status=stage or d.get("blocked_from") or "gate", attempts=0, next_run_at=0,
                           note=site.t("watchdog: network is back", "watchdog: red de vuelta"), data={"blocked_reason": None})
            notes.append(site.t(f"#{r['id']}: retrying, the block was the network", f"#{r['id']}: reintento, el bloqueo era de red"))
        elif retry == "retry":
            db.update_item(r["id"], status=stage or d.get("blocked_from") or "gate", attempts=0, next_run_at=0,
                           note="watchdog: retry after block", data={"auto_retries": d.get("auto_retries", 0) + 1})
            notes.append(site.t(f"#{r['id']}: retrying after a block ({why[:50]})", f"#{r['id']}: reintento tras bloqueo ({why[:50]})"))
    db.kv_set("watchdog", {"at": now(), "problems": problems, "pauses": pauses, "notes": notes})
    # the user's rule: nothing internal ever asks for his intervention. Problems and
    # pauses live on the board as one calm line; the only ping is a Claude budget/org limit,
    # once a day, because buying credits is genuinely his call.
    if "budget" in pauses and db.kv_get("pause_alerted:budget", 0) < now() - 86400:
        outbound.ping(f"⏸ {pauses['budget']}")
        db.kv_set("pause_alerted:budget", now())
    if problems:
        log("watchdog problems (board only)", problems=problems[:6], tried=notes[:4])


def ticket_duplicates() -> list[str]:
    """Drop a demand:/jira: case when a live Slack case owns its ticket. The ticket is the case's key (its own text can
    list 'esto puede ya existir' tickets), and the other case must be ABOUT it (cover.case_tickets), not just cite it:
    #463 was dropped mid-build for TICKET-1426 by #471, a board-review child whose dossier listed four tickets."""
    from . import cases, cover
    notes = []
    for r in db.q("SELECT id, title, thread_key, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('done','dropped','expired','failed') AND (thread_key LIKE 'demand:%' OR thread_key LIKE 'jira:%')"):
        d = json.loads(r["data"] or "{}")
        if any(p.get("pr_url") for p in d.get("parts", [])):
            continue
        for k in cases.tickets_in(cover._norm(f"{r['thread_key']} {d.get('jira') or ''}")):
            other = next((o for o in db.q("SELECT id FROM items WHERE id!=? AND thread_key NOT LIKE 'demand:%' AND kind IN ('TASK','INCIDENT','IDEA') "
                                          "AND status NOT IN ('done','dropped','expired','failed') AND (title LIKE ? OR data LIKE ?) ORDER BY id",
                                          r["id"], f"%{k}%", f"%{k}%")
                          if not (oi := db.item(o["id"]))["data"].get("parent") and k in cover.case_tickets(oi)), None)
            if other:
                db.update_item(r["id"], status="dropped", note=site.t(f"covered by #{other['id']} (same ticket {k})",
                                                                      f"lo cubre #{other['id']} (mismo ticket {k})"))
                notes.append(site.t(f"#{r['id']}: duplicate of #{other['id']} ({k})", f"#{r['id']}: duplicado de #{other['id']} ({k})"))
                break
    return notes


def _his_scheduled() -> list[dict]:
    """Messages the user scheduled in Slack: whatever they cover is off his to-do."""
    from . import slack
    try:
        return slack._call("chat_scheduledMessages_list", limit=100).get("scheduled_messages") or []
    except Exception as e:
        log("scheduled messages: list failed", err=str(e)[:100])
        return []


def reconcile_sent_messages() -> int:
    """A PR message the user already posted himself (any channel, any wording that carries the PR link) is marked
    sent: it leaves the board and its ask is retired."""
    from . import slack, approvals
    me = CFG["self_user_id"]
    n = 0
    for r in db.q("SELECT id, data FROM items WHERE json_extract(data,'$.team_message.team_text') IS NOT NULL AND status NOT IN ('dropped','failed')"):
        d = json.loads(r["data"] or "{}"); tm = d["team_message"]; sent = dict(tm.get("sent") or {})
        prs = [p for p in d.get("parts", []) if p.get("pr_url")]
        for p in prs:
            try:
                hits = slack.search(f"pull/{p['pr']} from:<@{me}>", count=20)
            except Exception as e:
                log("reconcile sent: search failed", err=str(e)[:100])
                hits = []
            hits += [{"channel": {"id": s.get("channel_id")}, "ts": str(s.get("post_at") or 0), "text": s.get("text")}
                     for s in _his_scheduled() if f"pull/{p['pr']}" in (s.get("text") or "")]
            for h in hits:
                ch = (h.get("channel") or {}).get("id")
                if not ch or ch == CFG["self_dm_channel"] or float(h.get("ts", 0)) < (tm.get("at") or 0) - 6 * 3600:
                    continue
                which = "team" if ch == CFG.get("team_channel") else "pr" if ch == CFG.get("pr_channel") else None
                for w in ([which] if which else ["team", "pr"]):
                    if not sent.get(w):
                        sent[w] = float(h["ts"])
                        n += 1
        if sent != (tm.get("sent") or {}):
            db.update_item(r["id"], data={"team_message": {**tm, "sent": sent}})
            for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", r["id"]):
                meta = json.loads(a["content"] or "{}")
                if meta.get("keep_status") and sent.get((meta.get("payload") or {}).get("which") or "team"):
                    approvals.supersede(a, meta, site.t("you already sent it", "ya lo mandaste vos"))
    return n


def team_messages() -> None:
    """Every open harness PR carries the message the user sends to the squad (up to three drafts per run)."""
    try:
        reconcile_sent_messages()  # Slack only, no tokens: runs even while Claude is paused
    except Exception as e:
        log("reconcile sent failed", err=str(e)[:120])
    if not claude.can_spend("draft", urgent=True):
        return
    made = 0
    # only for PRs past verification: announce drafts them when CI is green, this job catches what it missed. Before,
    # any open PR qualified, so #466 asked him at 14:34 (2 min into ci_wait) to send schools#985 to the squad and the PR
    # channel, through a CI fix and a flaky rerun, an hour before it was verified
    # blocked too: the watchdog sends a blocked case with a PR to announce, which drafts them with its caveats (#484 got
    # its squad asks at 21:17 in the 5 min between a verify block and that hand-off, 2026-10-05)
    building = ("build", "gate", "self_find", "self_review", "fix", "publish", "ci_wait", "ci_fix", "verify_done",
                "pr_body_refresh", "waiting", "announce", "blocked")
    for r in db.q(f"SELECT id, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('dropped','failed') "
                  f"AND status NOT IN ({','.join('?' * len(building))}) AND json_extract(data,'$.parts') LIKE '%pr_url%' "
                  "ORDER BY updated_at DESC", *building):
        d = json.loads(r["data"] or "{}")
        tm = d.get("team_message") or {}
        if tm.get("team_text") or tm.get("rejected") or d.get("team_message_tried", 0) >= 2:
            continue
        db.update_item(r["id"], data={"team_message_tried": d.get("team_message_tried", 0) + 1})
        if tasks.team_message(db.item(r["id"])):
            made += 1  # every open harness PR gets its message in one pass; cost is one short Opus draft each


POOL_SPEC: dict = {}  # thread name -> (pool, statuses, kinds)


def spawn_pool(name: str) -> None:
    spec = POOL_SPEC.get(name)
    if not spec:
        raise RuntimeError(f"unknown pool thread {name}")
    threading.Thread(target=pool, args=spec, name=name, daemon=True).start()


def safe(name: str, fn, *a) -> bool:
    try:
        fn(*a)
        return True
    except muse.MuseRateLimited:
        db.kv_set("muse_paused_until", int(time.time()) + CFG["muse"]["rate_limit_backoff_s"])
        return True  # quota, not a broken job
    except Exception as e:
        log(f"job {name} failed", err=f"{type(e).__name__}: {e}"[:400], tb=traceback.format_exc()[-800:])
        db.kv_set(f"job_err:{name}", {"at": now(), "err": f"{type(e).__name__}: {e}"[:300]})
        return False


def scheduler() -> None:
    """Only schedules and heartbeats; every job runs in its own thread."""
    locks: dict[str, threading.Lock] = {}

    def bg(name, fn, *a):
        """One lock PER job: a job only skips if its own previous run is still going.
"""
        lk = locks.setdefault(name, threading.Lock())

        def wrap():
            nonlocal lk
            if not lk.acquire(blocking=False):
                if db.kv_get(f"job_force_release:{name}", 0) > db.kv_get(f"job_started:{name}", 0):
                    # the reaper released a stuck run: new lock, run now (rebinding only the dict retried the held
                    # lock until RecursionError, 10-07 job-approvals and job-board)
                    lk = locks[name] = threading.Lock()
                    return wrap()
                return
            db.kv_set(f"job_started:{name}", now())
            try:
                if safe(name, fn, *a):
                    db.kv_set(f"job_ok:{name}", now())
            finally:
                db.kv_set(f"job_done:{name}", now())
                try:
                    lk.release()
                except RuntimeError:
                    pass
        threading.Thread(target=wrap, name=f"job-{name}", daemon=True).start()

    while not STOP.is_set():
        for fn_ in (lambda: hot.reload_if_changed(), lambda: hot.tick(spawn_pool)):
            try:
                fn_()
            except Exception as e:
                log("tick guard", err=f"{type(e).__name__}: {e}"[:200])
        (VAR / "health.json").write_text(json.dumps({"at": now(), "pid": os.getpid(), "mode": CFG["mode"],
                                                      "paused": paused(), "muse_paused": muse_paused(),
                                                      "claude_paused_until": claude.paused_until()}))
        if RESTART_FLAG.exists() and not db.q1("SELECT 1 FROM items WHERE claimed_by IS NOT NULL AND status IN ('build','fix')"):
            log("graceful restart: no code session in flight, exiting for launchd")
            db.x("UPDATE items SET claimed_by=NULL, claimed_at=NULL")  # short stages are re-claimed at once by the new worker
            os._exit(0)
        if paused():
            STOP.wait(15)
            continue
        try:
            schedule(bg)  # looked up each tick: hot.load_worker rebinds it, so a schedule change goes live without a restart
        except Exception as e:
            log("tick guard", err=f"{type(e).__name__}: {e}"[:200])
        STOP.wait(10)


def schedule(bg) -> None:
    """Start the jobs that are due, each through bg (its own thread and lock)."""
    S = CFG["schedule"]
    wh = work_hours()
    slack_every = S["slack_poll_s"] if wh else 600
    if every("slack", slack_every):
        bg("ingest-slack", lambda: (ingest.poll_channels(), ingest.poll_threads()))
    if every("mentions", S["mention_search_s"] if wh else 900):
        bg("ingest-mentions", ingest.search_mentions)
    if every("github", S["github_poll_s"]):
        bg("ingest-github", ingest.poll_github)
    claude_ok = now() >= claude.paused_until()
    if claude_ok and db.q1("SELECT 1 FROM events WHERE triaged=0 LIMIT 1") and every("triage", 30):
        bg("triage", triage.run_batch)
    if every("babysit", S["babysit_s"]):
        bg("babysit", babysit.run_once)
    if every("mirrors", 600):
        bg("mirrors", worktree.refresh_mirrors)
    if db.kv_get("rejudge_rev") != code_rev():
        db.kv_set("rejudge_rev", code_rev())  # right after every code change: new rules, re-check the misses
        bg("rejudge", ingest.rejudge)
        bg("requeue_blocks", requeue_harness_bugs)  # code goes live by hot reload: this ran only at worker start
    if every("rejudge_drops", 600):
        revs = ingest.drops_rev_due()
        if revs:
            bg("rejudge_drops", ingest.rejudge_drops, revs, 48, 5)
    if wh and site.feature("unanswered_sweep") and every("unanswered_sweep", 1800):
        bg("unanswered_sweep", ingest.unanswered_sweep)
    elif every("rejudge", 900):
        bg("rejudge", ingest.rejudge)
    if every("outcomes", 3600):
        from . import outcomes
        bg("outcomes", outcomes.audit)
    if every("knowledge_commit", 3600):
        bg("knowledge_commit", knowledge_commit)

    if every("merge_infer", 900):
        from . import merge_order
        bg("merge_infer", merge_order.infer_pending)
    if claude_ok and every("context", 900):
        bg("context", lambda: (context.refresh(), context.prioritize()))
    if site.feature("free_models") and (every("muse_health", 600) or (db.kv_get("muse_suspect", 0) > db.kv_get("muse_checked", 0)
                                                                      and every("muse_health_fast", 60))):
        db.kv_set("muse_checked", now())

        def check():
            if not muse.healthy():
                muse.restart_server("health check failed")
        bg("muse_health", check)
    if every("watchdog", 300):
        bg("watchdog", watchdog)  # never inline: a Slack ping on a dead network hung the scheduler 15 min
    if every("approvals", 45):
        bg("approvals", approvals.poll)
    if every("track", 600):
        bg("track", track_awaiting)
    if every("board", 60):
        bg("board", board.refresh)  # edited in place, silently
    if dt.datetime.now().isoweekday() <= 5 and any(daily_at(f"digest_{h}", h) for h in ("08:30", "11:00", "14:00", "17:30")):
        db.kv_set("board", {**(db.kv_get("board") or {}), "digest_due": True})  # the full digest, twice a day
    idle = not db.q1(f"SELECT 1 FROM items WHERE status IN ({','.join('?' * len(STAGES))}) LIMIT 1", *STAGES)
    # patrol is the only source of work nobody reported; it used to need an idle queue, which
    # never happens (0 runs in 3 days). Now: every patrol_idle_s during work hours, free model.
    code_busy = db.q1("SELECT 1 FROM items WHERE status IN ('build','fix') LIMIT 1")
    scan_on = (CFG.get("scan") or {}).get("enabled")
    if scan_on and not claude.paused_until() > time.time() and every("scan", CFG["scan"].get("every_s", 600)):
        from . import scan
        bg("scan", scan.run_job)  # every fence file on Haiku 5.5, the code as intake; replaces patrol (ap/scan.py)
    if (not scan_on and site.feature("patrol") and not claude.paused_until() > time.time() and not code_busy
            and every("patrol", S["patrol_idle_s"])):
        bg("patrol", patrol.run_once)  # filler: never competes with code for tokens
    if wh and dt.datetime.now().hour >= 18 and daily_at("noop_audit", "18:00"):
        bg("noop_audit", noop_audit)
    if site.feature("team_messages") and every("team_messages", 300):
        bg("team_messages", team_messages)
    if site.feature("closing") and every("closing", 1800):
        bg("closing", closing.sweep)
    if every("backfill", 3600):  # internally no-ops unless the last sweep is >20h old (covers downtime)
        bg("backfill", backfill.run)
    from . import tickets
    if tickets.enabled() and every("tickets", int((CFG.get("jira") or {}).get("poll_s") or 900)):
        bg("tickets", tickets.poll)  # the Jira tickets the user chose to consume (a release install; REST, read-only)
    # the layer that changes the harness from its own outcomes: rule and style learning, the weekly replay evals
    if site.self_improving() and daily_at("learn", S["learn_at"]):
        bg("learn", learn.nightly)
    if site.self_improving() and dt.date.today().isoweekday() == S["evals_weekday"] and daily_at("evals", "20:00"):
        bg("evals", evals.weekly)
    if every("gc", 3600):  # hourly: a cometa tree is ~3 GB and the disk has tens (daily at 04:00 kept 106 GB, 10-23)
        bg("gc", worktree.gc)
    if every("reload", 300):
        reload_config()


def setup_mode() -> None:
    """A new install: nobody to read for and nothing to think with yet. The worker serves the app, which shows the
    onboarding, and does nothing else. When the user finishes, it starts over as itself: modules read their config
    when they load, so the cleanest way into normal mode is a fresh process with the config the user just made."""
    from . import console
    log("worker start: setup mode", pid=os.getpid(), home=str(VAR.parent))
    while not STOP.is_set() and not site.setup_complete():
        (VAR / "health.json").write_text(json.dumps({"at": now(), "pid": os.getpid(), "mode": "setup", "paused": False}))
        try:
            console.serve_once()
        except Exception as e:
            log("setup mode: console failed", err=f"{type(e).__name__}: {e}"[:200])
        STOP.wait(1)
    if STOP.is_set():
        return
    log("setup complete: starting over in normal mode")
    time.sleep(1.5)  # the app's 'finish' request gets its answer before the process is replaced
    os.execv(sys.executable, [sys.executable, "-m", "ap.cli", "worker"])


def muse_start() -> None:
    """The free models' server at worker start, never fatal: the muse_health job and every muse call bring it up again.
"""
    def up():
        muse.ensure_server()
        if not muse.healthy():
            muse.restart_server("unhealthy at worker start")
    safe("muse_start", up)


def boot_background() -> threading.Thread:
    """Start-up work that waits on the network or the free models runs beside the scheduler, never before its first
    heartbeat. The mirrors job and every muse call redo both anyway."""
    def run():
        if not site.internal():
            from . import setup
            safe("clone", setup.clone_missing)  # repos the user chose that are not on this machine yet
        if site.feature("free_models"):
            muse_start()
        safe("mirrors", worktree.ensure_mirrors)
    th = threading.Thread(target=run, name="boot", daemon=True)
    th.start()
    return th


def main() -> None:
    lockf = open(VAR / "worker.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another worker is running")
        return
    lockf.write(str(os.getpid()))
    lockf.flush()
    if not site.setup_complete():
        return setup_mode()
    log("worker start", mode=CFG["mode"], pid=os.getpid(), edition=CFG.get("edition"))
    faulthandler.register(signal.SIGUSR1, file=open(VAR / "log" / "stacks.txt", "a"), all_threads=True)
    db.x("UPDATE items SET claimed_by=NULL, claimed_at=NULL WHERE claimed_by IS NOT NULL AND claimed_by NOT LIKE 'orphan:%'")
    # clean start: orphaned `opencode run` clients of the previous worker die; a healthy server stays
    subprocess.run(["pkill", "-f", "opencode run --attach"], capture_output=True)
    try:  # before any pool claims: a case whose session outlived the last worker is parked under it, not built twice
        park_orphan_sessions(_ps_rows())
    except Exception as e:
        log("orphan sessions: park failed", err=str(e)[:200])
    boot_background()
    # deploy gate: every item kind through every stage with fakes; a failing harness must not
    # touch real work
    rev = code_rev()
    cached = db.kv_get("selftest") or {}
    if not site.internal():
        # the gate guards an engine that changes under its own worker (the first install edits and reloads itself); a
        # released engine was gated when it was built, and its scenarios are written against the first install's config
        st = subprocess.CompletedProcess([], 0, "", "")
    elif cached.get("rev") == rev and cached.get("ok"):
        st = subprocess.CompletedProcess([], 0, cached.get("out", ""), "")  # same code already passed
    else:
        st = subprocess.run([sys.executable, "-m", "ap.selftest"], cwd=str(VAR.parent), capture_output=True, text=True,
                            timeout=600)
        db.kv_set("selftest", {"at": now(), "ok": st.returncode == 0, "out": st.stdout[-2000:], "rev": rev})
    if st.returncode != 0:
        log("SELFTEST FAILED: stage pools held until the harness is fixed", out=st.stdout[-1500:])
        try:
            from . import slack
            slack.send_self(site.t("🛠 Clea failed its selftest; no task moves until it is fixed:\n",
                                   "🛠 Clea no pasó su selftest, no avanzo tareas hasta que se corrija:\n")
                            + "\n".join(l for l in st.stdout.splitlines() if l.startswith(("BAD", "FAIL")))[:1500],
                            topic="selftest")
        except Exception:
            pass
        scheduler()  # ingest + board keep running; no stage pools
        return
    requeue_harness_bugs()
    republish_shadow_work()
    if CFG["mode"] == "act" and not db.kv_get("shadow_asks_reissued"):
        safe("reissue_asks", approvals.reissue_shadow_asks)
        db.kv_set("shadow_asks_reissued", now())
    RESTART_FLAG.unlink(missing_ok=True)
    threads = [threading.Thread(target=pool, args=("fast", FAST), name=f"fast{i}", daemon=True)
               for i in range(CFG["muse"]["concurrency"])]
    threads += [threading.Thread(target=pool, args=("claude", SLOW), name=f"claude{i}", daemon=True)
                for i in range(CFG["claude"].get("concurrency", 1))]
    # one code session reserved for HIS PRs (reviewer comments, CI): 2026-09-28 a teammate's fix on schools#913 waited
    # 4 h 48 behind two harness builds, and a reviewer waiting is the worst latency there is
    threads += [threading.Thread(target=pool, args=("claude", SLOW, ["CI", "COMMENTS"]), name="claude-own", daemon=True)]
    for th in threads:
        if th._target is pool:
            POOL_SPEC[th.name] = tuple(th._args) + ((None,) if len(th._args) == 2 else ())
    threads += [threading.Thread(target=pool, args=("quick", QUICK), name=f"quick{i}", daemon=True)
                for i in range(CFG["claude"].get("quick_concurrency", 2))]
    threads.append(threading.Thread(target=ingest.socket_listener, args=(STOP,), name="socket", daemon=True))
    for t in threads:
        t.start()
    global RUNNING
    RUNNING = True
    try:
        scheduler()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        log("worker stop", err=traceback.format_exc()[-800:] if sys.exc_info()[0] else None)
        # the process must not outlive its scheduler: Slack's socket threads are not daemons, so a returning main
        # left pid 14654 holding the lock, with no scheduler and no pools, from 23:30 on 10-02 to 10-04
        os._exit(1)


def _same_fix(repo: str, a: int, b: int) -> bool:
    key = f"dupcheck:{repo}:{a}:{b}"
    hit = db.kv_get(key)
    if hit is not None:
        return bool(hit["same"])
    try:
        da, dbb = gh.pr_diff(repo, a)[:20000], gh.pr_diff(repo, b)[:20000]
        with claude.pool_ctx("delivery"):
            out = claude.lean("dupcheck", f"PR #{a}:\n{da}\n\nPR #{b}:\n{dbb}", cwd=str(VAR),
                              schema={"type": "object", "required": ["same", "why"], "properties": {"same": {"type": "boolean"}, "why": {"type": "string"}}},
                              tools=(), timeout=300)
        db.kv_set(key, {"same": bool(out["same"]), "why": out["why"][:300]})
        log("dupcheck", repo=repo, a=a, b=b, same=out["same"], why=out["why"][:120])
        return bool(out["same"])
    except Exception as e:
        log("dupcheck failed, keeping both", err=str(e)[:120])
        return False


def _absorb_sources(survivor_id: int, loser_id: int) -> None:
    """The surviving case keeps every way the problem reached us."""
    from . import cases, gh
    s, l = db.item(survivor_id), db.item(loser_id)
    if not s or not l:
        return
    ld = l["data"]
    src = {"item": loser_id, "title": l["title"][:120], "channel": ld.get("channel"), "ts": ld.get("thread_ts") or ld.get("ts"),
           "author": ld.get("author"), "from": ld.get("from"), "pattern": bool(ld.get("pattern"))}
    tickets = cases.tickets_in(f"{l['title']} {ld.get('text', '')} {ld.get('summary', '')} {ld.get('jira', '')}")
    src["tickets"] = tickets
    also = [a for a in s["data"].get("also_sources", []) if a.get("item") != loser_id] + [src]
    db.update_item(survivor_id, data={"also_sources": also})
    for p in s["data"].get("parts", []):  # a ticket key is team-facing: it belongs in the PR body
        if p.get("pr") and tickets:
            try:
                body = gh.pr_view(p["repo"], p["pr"]).get("body") or ""
                missing = [t for t in tickets if t not in body]
                if missing:
                    outbound.edit_pr(p["repo"], p["pr"], gh.pr_view(p["repo"], p["pr"]).get("title") or "",
                                     body.rstrip() + "\n\nTickets: " + ", ".join(missing), survivor_id)
            except Exception as e:
                log("absorb: pr body ticket failed", item=survivor_id, err=str(e)[:120])


def dedupe_prs() -> int:
    """Harness PRs that change the same non-test files as an earlier harness PR are duplicates
. Zero tokens:
    compare real changed files, keep the older, close the newer and drop its case."""
    # duplicates closed before sources were absorbed (idempotent catch-up): the close comment names the keeper
    for r in db.q("SELECT id, data FROM items WHERE status IN ('dropped','expired') AND json_extract(data,'$.parts') LIKE '%pr_url%'"):
        d = json.loads(r["data"] or "{}")
        for p in d.get("parts", []):
            if not (p.get("pr_url") and p.get("pr")):
                continue
            try:
                cs = gh.issue_comments(p["repo"], p["pr"])
            except Exception:
                continue
            m = next((site.pr_link_re().search((c.get("body") or "").split(mk, 1)[1])
                      for c in cs for mk in DUP_COMMENT if mk in (c.get("body") or "")), None)
            if not m:
                continue
            surv = db.q1("SELECT id, data FROM items WHERE json_extract(data,'$.parts') LIKE ? AND status NOT IN ('dropped','failed')",
                         f"%/{m.group(1)}/pull/{m.group(2)}%")
            if surv and not any(a.get("item") == r["id"] for a in json.loads(surv["data"]).get("also_sources", [])):
                _absorb_sources(surv["id"], r["id"])
    owners = []
    for r in db.q("SELECT id, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('dropped','expired','failed')"):
        d = json.loads(r["data"] or "{}")
        for p in d.get("parts", []):
            if p.get("pr_url") and p.get("pr"):
                owners.append((r["id"], p["repo"], p["pr"], p["pr_url"]))
    seen: dict[str, tuple] = {}
    n = 0
    for iid, repo, pr, url in sorted(owners, key=lambda x: x[2]):
        try:
            v = gh.pr_view(repo, pr)
            if v.get("state") == "CLOSED":  # closed without merge (a duplicate, or by hand): its case is over
                it_ = db.item(iid)
                if it_ and it_["status"] not in db.TERMINAL:
                    db.update_item(iid, status="dropped", note=site.t(f"PR #{pr} closed without merging", f"PR #{pr} cerrado sin mergear"))
                    for a in db.q("SELECT id FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", iid):
                        db.set_artifact_outcome(a["id"], "superseded", {"why": site.t("PR closed", "PR cerrado")})
                    from . import dm
                    dm.close_case_thread(db.item(iid), site.t("PR closed", "PR cerrado"))
                continue
            if v.get("state") != "OPEN":
                continue
            files = {f["path"] for f in (v.get("files") or []) if not re.search(r"(^|/)(tests?|__tests__)/|test_[^/]+\.py$|\.(test|spec)\.", f["path"])}
        except Exception:
            continue
        dup = None
        for f in files:
            if f"{repo}:{f}" in seen and seen[f"{repo}:{f}"][2] != pr:
                dup = seen[f"{repo}:{f}"]
                break
        if dup and not _same_fix(repo, dup[2], pr):
            dup = None  # same file, different fix
        if dup:
            outbound.close_duplicate_pr(repo, pr, dup[3], iid)
            db.update_item(iid, status="dropped", note=site.t(f"duplicate PR of #{dup[0]} ({dup[3]})", f"PR duplicado de #{dup[0]} ({dup[3]})"))
            _absorb_sources(dup[0], iid)
            for a in db.q("SELECT id FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", iid):
                db.set_artifact_outcome(a["id"], "superseded", {"why": site.t("duplicate PR", "PR duplicado")})
            from . import dm
            dm.close_case_thread(db.item(iid), site.t("duplicate PR", "PR duplicado"))
            n += 1
            continue
        for f in files:
            seen.setdefault(f"{repo}:{f}", (iid, repo, pr, url))
    return n
