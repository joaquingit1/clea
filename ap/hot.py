"""No restarts.

Two mechanisms, both driven from the scheduler tick:
1. hot reload: when the committed harness code changes, every `ap.*` module except the worker is reloaded in
   place (same module objects, so `module.func` lookups pick up the new code). Stages resolve their function
   by name at call time; jobs are looked up per tick. A file that does not compile is never loaded. The worker's
   own functions are rebound from a fresh copy of worker.py (load_worker).
2. reaper: a stage, a job or a pool thread that exceeds its wall-clock budget is killed (its subprocess group)
   and the slot re-armed; a thread hung in pure Python is replaced. Nothing waits for a human.
"""
from __future__ import annotations

import importlib
import os
import py_compile
import signal
import sys
import threading
import time

from . import db, site
from .core import HOME, VAR, log, now

# dependency order: base first. worker.py is not in it: it runs the loops, see load_worker.
ORDER = ["core", "site", "db", "slack", "voice_lint", "sentlearn", "gh", "worktree", "claude", "muse", "cases", "precedents", "gates",
         "finders", "trace", "bridge", "learn", "context", "alerts", "merge_order", "outbound", "report", "approvals", "replies", "tasks",
         "babysit", "precheck", "reviews", "epics", "closing", "digest", "board", "dm", "patrol", "backfill", "outcomes", "ingest",
         "evals", "triage", "dash", "console", "stale", "incidents", "observer", "guardcall", "impact", "dedupe", "escalate", "repro", "chat", "hot"]
LOCK = threading.Lock()

# wall-clock budgets (seconds) per stage; anything else 1800
# the user: "if one is stalled for over 45 mins we kill it". A killed session is resumed with "continue"
# (claude.resume_for), so killing costs almost nothing.
STAGE_MAX = {"build": 3600, "fix": 3600, "plan": 5400}  # plan reproduces the bug first (ap/repro.py): a session plus a container test run
STALL = 2700
JOB_MAX = {"context": 3000, "patrol": 2400, "backfill": 3000, "triage": 1500, "babysit": 1500, "closing": 1800,
           "team_messages": 1500, "watchdog": 900, "board": 600, "approvals": 600, "ingest-slack": 600,
           # the night batches replay cases through the lanes: learn's rule gate runs about 55 min awake, the weekly eval
           # about 3 h. On the 1800 s default the reaper cut learn on 11 nights from 09-29 (10-17 00:00: the judge call
           # in flight, one gate sample lost) while the thread ran on to 00:24, so the kill bought nothing
           "learn": 7200, "evals": 14400}


def _git(*a) -> str:
    import subprocess
    return subprocess.run(["git", "-C", str(HOME), *a], capture_output=True, text=True).stdout.strip()


def code_rev() -> str:
    """The committed revision. A half-finished edit (dirty tree under ap/ or prompts/) never goes live: the reload
    waits until it is committed (design review 2026-09-30)."""
    return _git("rev-parse", "--short=12", "HEAD")


def _dirty() -> bool:
    return bool(_git("status", "--porcelain", "--", "ap", "prompts"))


def _compiles() -> bool:
    for f in (HOME / "ap").rglob("*.py"):
        try:
            py_compile.compile(str(f), doraise=True)
        except Exception as e:
            log("hot reload refused: file does not compile", file=f.name, err=str(e)[:200])
            return False
    return True


def _file_hashes() -> dict:
    import hashlib
    return {".".join(f.relative_to(HOME / "ap").with_suffix("").parts): hashlib.sha1(f.read_bytes()).hexdigest()[:12]
            for f in (HOME / "ap").rglob("*.py") if "__pycache__" not in f.parts}


def _dirty_cleared(rev: str) -> None:
    """The tree is clean again (committed or reverted): a filed reload_blocked incident closes itself. 2026-10-20: his
    own console edit blocked reloads 27 min and was committed 7 min after the filing, yet the incident stayed open at
    impact 8 and topped the next supervisor run."""
    db.kv_set("hot_dirty_since", 0)
    sig = db.kv_get("hot_dirty_sig")
    if not sig:
        return
    db.kv_set("hot_dirty_sig", "")
    try:
        from . import incidents
        incidents.mark(sig, "wontfix", rev, f"cleared on its own: the tree was clean again at {rev}")
        log("hot reload: block cleared", sig=sig, rev=rev)
    except Exception as e:
        log("hot reload: incident clear failed", err=str(e)[:120])


def reload_if_changed() -> bool:
    """Called every tick. Reloads only the modules whose file changed (plus everything when a base module,
    core/db, changed, since others bind its names). Prompts (.md) are read per call and need no reload.
    A reload is milliseconds and no tokens, but touching module-level state without need is pointless."""
    rev = code_rev()
    loaded = db.kv_get("hot_loaded_rev")
    hashes = _file_hashes()
    if loaded is None:
        db.kv_set("hot_loaded_rev", rev)
        db.kv_set("hot_file_hashes", hashes)
        return False
    if rev == loaded:
        _dirty_cleared(rev)
        return worker_catch_up()
    if _dirty():
        # a half-applied edit must not freeze every later commit silently (2026-10-06: a prompt left modified at 22:10
        # by an aborted revert blocked all reloads for 5.5 h, the #the team-alerts stop included)
        since = db.kv_get("hot_dirty_since", 0) or now()
        db.kv_set("hot_dirty_since", since)
        if now() - since > 1200 and db.kv_get("hot_dirty_filed") != since:
            db.kv_set("hot_dirty_filed", since)
            files = _git("status", "--porcelain", "--", "ap", "prompts")
            log("hot reload blocked by uncommitted changes", minutes=round((now() - since) / 60), files=files[:300])
            try:
                from . import incidents
                db.kv_set("hot_dirty_sig", incidents.file(
                    "reload_blocked", files[:120], site.t("Hot reload blocked: uncommitted changes in ap/ or prompts/",
                                                          "Hot reload bloqueado: hay cambios sin commitear en ap/ o prompts/"),
                    site.t(f"HEAD {rev} has not loaded for {round((now() - since) / 60)} min. Changes:\n{files}",
                           f"HEAD {rev} no se carga desde hace {round((now() - since) / 60)} min. Cambios:\n{files}"), impact=8))
            except Exception as e:
                log("hot reload: incident failed", err=str(e)[:120])
        return False
    _dirty_cleared(rev)
    prev = db.kv_get("hot_file_hashes") or {}
    changed = [n for n, h in hashes.items() if prev.get(n) != h and n != "worker"]
    with LOCK:
        if not _compiles():
            return False
        # every changed module reloads, listed or not (2026-09-30: voice.py was missing from ORDER, so the live worker
        # ran stale voice code and every chat ack failed); ORDER only fixes the sequence for known dependencies
        extra = [n for n in changed if n not in ORDER and n != "worker"]
        names = (ORDER + extra) if any(n in ("core", "site", "db") for n in changed) else [n for n in ORDER if n in changed] + extra
        failed = []
        for name in names:
            mod = sys.modules.get(f"ap.{name}")
            if mod is None:
                continue
            try:
                importlib.reload(mod)
            except Exception as e:
                failed.append(f"{name}: {str(e)[:120]}")
        db.kv_set("hot_file_hashes", hashes)
        db.kv_set("hot_loaded_rev", rev)
        db.kv_set("hot_reload", {"at": now(), "rev": rev, "failed": failed})
        log("hot reload", rev=rev, modules=names, failed=failed)
        try:
            from . import core
            core.reload_config()
        except Exception:
            pass
    return worker_catch_up() and not failed


# objects of the running process (its loop flag, its pool table): a function that uses them keeps its running copy
_PROCESS_STATE = {"STOP", "POOL_SPEC"}


def load_worker() -> list[str]:
    """worker.py runs the loops, so it is never re-executed in place (that would give it a new STOP, POOL_SPEC and job
    locks). Its committed copy is loaded beside it and every function of it that does not touch that state is rebound
    on the running module: the loops look those names up on each call, so the watchdog, the jobs, run_stage and the
    schedule run the new code. 2026-10-01: worker.py changed at 18:15 and 21:45 after the 13:15 start and only logged
    'needs the graceful restart'; ad12152's watchdog step never ran and #31 (cometa#1931) stayed blocked by the 3-day
    wait cap it was written to lift."""
    import importlib.util
    import inspect
    live = sys.modules["ap.worker"]
    spec = importlib.util.spec_from_file_location("ap.worker_live", live.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    sys.modules["ap.worker_live"] = fresh
    names = [n for n, f in vars(fresh).items() if inspect.isfunction(f) and f.__module__ == "ap.worker_live"
             and not _PROCESS_STATE & set(f.__code__.co_names)]
    for n in names:
        setattr(live, n, getattr(fresh, n))
    return names


def worker_catch_up() -> bool:
    """Rebind the worker when worker.py on disk is newer than the code this process runs (its start, or the last
    load). Checked every tick, so a change committed together with this module's own change also goes live."""
    w = sys.modules.get("ap.worker")
    if w is None or not hasattr(w, "STARTED_AT"):
        return True
    if os.stat(w.__file__).st_mtime <= getattr(w, "_hot_loaded", w.STARTED_AT) or _dirty():
        return True
    with LOCK:
        w._hot_loaded = now()
        try:
            names = load_worker()
        except Exception as e:
            log("hot reload: worker.py failed to load, the running copy stays", err=str(e)[:200])
            return False
    log("hot reload: worker.py rebound", functions=len(names))
    return True


# ---- reaper ---------------------------------------------------------------------------------------------------
# Budgets are AWAKE seconds. The Mac sleeps; the wall
# clock runs on while every process is frozen, and core.run's subprocess timeouts (monotonic) do not. Measured on the
# wall clock, the first tick after each wake found every job, stage and pool thread "over budget" and killed them all
# (context killed 10 times after "3626s" of a 600 s call, then backed off 3 h). time.monotonic() stops during sleep
# on macOS, so the gap between the two clocks across ticks is the sleep.
SLEEP_MIN = 60
# as long as anyone looks back: incidents judge 48 h of calls and count a week of runnable time. Kept 24 h, the
# dark-wake night of 10-10 (timeouts of 3 to 5 h) was forgotten by the next morning and its context, patrol and deps
# timeouts were filed as call failures
SLEEP_KEEP_S = 8 * 86400


def note_sleep(t: float, mono: float | None = None) -> float:
    """Record a sleep that happened since the previous tick; returns its length (0 when none)."""
    mono = time.monotonic() if mono is None else mono
    last = db.kv_get("clock_last") or {}
    db.kv_set("clock_last", {"wall": t, "mono": mono})
    gap = (t - last["wall"]) - (mono - last["mono"]) if last and mono >= last.get("mono", 0) else 0
    if gap < SLEEP_MIN:
        return 0
    sleeps = [s for s in (db.kv_get("sleeps") or []) if s[0] > t - SLEEP_KEEP_S]
    db.kv_set("sleeps", sleeps + [[t, round(gap)]])
    log("reaper: machine slept, budgets exclude it", secs=round(gap))
    return gap


def slept_in(start: float, end: float, before: int = 300, extra: list | None = None) -> bool:
    """Whether a recorded sleep (or one in extra) fell between `before` s ahead of start and end: a dark wake has no
    network or keychain, so what fails then or just after waking failed on the host."""
    return any(woke > start - before and woke - g < end for woke, g in (db.kv_get("sleeps") or []) + (extra or []))


def slept_near(start: float, t: float, mono: float | None = None, before: int = 300) -> bool:
    """slept_in up to now, counting a sleep the reaper has not recorded yet (its tick can trail a dark wake: #680's
    07:20 stage failed 7 s before the 07:21 tick logged the 925 s sleep)."""
    mono = time.monotonic() if mono is None else mono
    last = db.kv_get("clock_last") or {}
    pending = (t - last["wall"]) - (mono - last["mono"]) if last and mono >= last.get("mono", 0) else 0
    unrecorded = [[last["wall"] + pending, pending]] if pending >= SLEEP_MIN else []  # woke no earlier than that
    return slept_in(start, t, before, unrecorded)


def awake_since(start: float, t: float) -> float:
    """Seconds awake between start and t: wall time minus the part of each recorded sleep that falls after start
    (a sleep is stored as [wake time, length])."""
    return (t - start) - sum(max(0, min(g, woke - start)) for woke, g in (db.kv_get("sleeps") or []))


# the user's pause (`apctl pause`, chat) holds every stage like a sleep does. Unrecorded, 10-23 to 10-29 paused read as
# 'casos trabados en gate/fix', '#636 frozen 22 days' and 'whole window produced zero case progress'
PAUSE_FILE = VAR / "PAUSED"


def pauses(t: float | None = None) -> list:
    """Spans the worker was paused, stored like sleeps as [end, length]; an open pause ends at t (its file's mtime is
    when it began)."""
    t = now() if t is None else t
    out = list(db.kv_get("pauses") or [])
    try:
        out.append([t, max(0, t - PAUSE_FILE.stat().st_mtime)])
    except FileNotFoundError:
        pass
    return out


def resume() -> None:
    """End a pause and keep its span for the stuck signal and the observer."""
    try:
        began = PAUSE_FILE.stat().st_mtime
    except FileNotFoundError:
        return
    t = now()
    db.kv_set("pauses", [p for p in (db.kv_get("pauses") or []) if p[0] > t - SLEEP_KEEP_S] + [[t, round(t - began)]])
    PAUSE_FILE.unlink(missing_ok=True)


def paused_since(start: float, t: float) -> float:
    """Seconds of pause between start and t."""
    return sum(max(0, min(end, t) - max(end - g, start)) for end, g in pauses(t))


def _kill_thread_procs(thread_name: str) -> int:
    """Kill every subprocess group registered by that thread (core.run registers them)."""
    from . import core
    n = 0
    for tname, (pid, started, cmd) in list(core.ACTIVE.items()):
        if tname == thread_name:
            try:
                os.killpg(pid, signal.SIGKILL)
                n += 1
            except Exception:
                pass
            core.ACTIVE.pop(tname, None)
    return n


def _kill_reason(thread_name: str, why: str, t: float) -> None:
    """Left for the call the kill ends, which records it (claude._invoke): a bare 'killed' became an unexplained
    call_failure incident and the observer guessed an OOM."""
    db.kv_set(f"kill_reason:{thread_name}", {"why": why, "at": int(t)})


def spawn_by_name(name: str) -> None:
    """Replacement pool thread by name prefix (worker.POOL_SPEC misses the quick threads)."""
    from . import worker
    if any(th.name == name and th.is_alive() for th in threading.enumerate()):
        return  # never a duplicate: an alive thread is left alone (its processes were already killed)
    spec = (("claude", worker.SLOW, ["CI", "COMMENTS"]) if name == "claude-own" else
            ("claude", worker.SLOW, None) if name.startswith("claude") else
            ("quick", worker.QUICK, None) if name.startswith("quick") else ("fast", worker.FAST, None))
    threading.Thread(target=worker.pool, args=spec, name=name, daemon=True).start()


STALL_S = 90

# The worker is ~40 threads doing many small SQLite calls; each call drops the GIL and waits up to a switch interval to
# get it back from the CPU-bound ones (board pass, intake, chat loop). At the default 5 ms a case page making a few
# hundred calls took seconds in bursts.
SWITCH_S = 0.001
if sys.getswitchinterval() != SWITCH_S:
    sys.setswitchinterval(SWITCH_S)


def _main_watch() -> None:
    """Logs where the scheduler's own thread is when its heartbeat goes stale. The healthcheck kills a worker whose
    health.json is 5 min old, and its faulthandler dump lists only the newest 100 threads, so the main thread, the
    oldest, was missing from both kills on 10-22 (12:38 and 12:58), each a restart the user saw as a blank Clea."""
    main = threading.main_thread().ident
    health = VAR / "health.json"
    told = 0.0
    while True:
        time.sleep(30)
        try:
            age = time.time() - health.stat().st_mtime
            if age > STALL_S and time.time() - told > 60:
                import traceback
                frame = sys._current_frames().get(main)
                stack = traceback.format_stack(frame)[-14:] if frame else []
                log("scheduler stalled", secs=round(age), stack="".join(stack)[-3000:])
                told = time.time()
        except Exception as e:
            log("main watch failed", err=f"{type(e).__name__}: {e}"[:160])


def tick(spawn_pool) -> list[str]:
    """Returns notes for the log. spawn_pool is ignored: spawn_by_name knows every pool."""
    spawn_pool = spawn_by_name
    notes = []
    t = now()
    if not any(th.name == "main-watch" and th.is_alive() for th in threading.enumerate()) and db.is_live():
        threading.Thread(target=_main_watch, name="main-watch", daemon=True).start()
    note_sleep(t)  # before any budget check: the first tick after a wake must not reap what the sleep froze
    # orchestrator chat: his top-level self-DM messages, answered in real time (every ~20 s, own thread)
    try:
        from . import chat
        chat.ensure_loop()
    except Exception as e:
        log("chat loop start failed", err=str(e)[:160])
    # periodic reconcilers
    # own thread: it runs a gh call per PR (120 s timeout each) and a Sonnet loop check, and inline it froze the heartbeat
    # until the healthcheck killed a working worker
    if (t - (db.kv_get("stale_at", 0) or 0) >= 600
            and not any(th.name == "stale-sweep" and th.is_alive() for th in threading.enumerate())):
        db.kv_set("stale_at", t)
        def _stale():
            try:
                from . import stale
                stale.sweep()
            except Exception as e:
                log("stale sweep failed", err=str(e)[:160])
        threading.Thread(target=_stale, name="stale-sweep", daemon=True).start()
    if t - (db.kv_get("pr_watch_at", 0) or 0) >= 300:  # teammates' PRs on files our active cases touch (ap/impact.py)
        db.kv_set("pr_watch_at", t)
        def _pw():
            try:
                from . import impact
                impact.retry_unjudged()  # news a failed judge left on its case (usage limit, budget, timeout)
                impact.pr_watch()
            except Exception as e:
                log("pr watch failed", err=str(e)[:160])
        threading.Thread(target=_pw, name="pr-watch", daemon=True).start()
    if t - (db.kv_get("escalate_at", 0) or 0) >= 120:  # blocked cases get an owner and a next step (ap/escalate.py)
        db.kv_set("escalate_at", t)
        try:
            from . import escalate
            escalate.sweep()
        except Exception as e:
            log("escalate sweep failed", err=str(e)[:160])
    try:  # drafts he sent by hand: (draft, sent) pairs for the style learning (own thread, every 10 min)
        from . import sentlearn, site
        if site.self_improving():
            sentlearn.tick()
    except Exception as e:
        log("sentlearn tick failed", err=str(e)[:160])
    try:  # the console app (127.0.0.1:4848): JSON API + chat with the orchestrator; idle when nobody has it open
        from . import console
        console.serve_once()
    except Exception as e:
        log("console failed", err=str(e)[:120])
    try:  # the operator's own dashboard (budgets, reaper, reload log): the first install's, the app is everyone's
        from . import dash, site
        if site.internal():
            dash.serve_once()
            if t - (db.kv_get("dash_at", 0) or 0) >= 30:
                dash.render()
                db.kv_set("dash_at", t)
    except Exception as e:
        log("dash failed", err=str(e)[:120])
    return notes + reap(t, spawn_pool)


def _held(st: dict) -> dict | None:
    """The item row while the stage entry's own claim still holds it. An entry is keyed by thread name, and a killed
    worker leaves its entries behind: on 10-13 worker 97718's quick1 was vetting #734 when it died at 11:42, and from
    12:34 the new worker's quick0 held #734. Any claim made the old entry look live, so the reaper reported 'vet over
    budget, procs 0' at 12:57, and quick1 borrowed #734's heartbeat as its own beat and was never respawned (the quick
    lane ran on two threads until the 13:10 restart). An orphan claim is a dead worker's session, not this thread's:
    the watchdog holds it to its timeout (worker.tend_orphan_claims)."""
    if not st.get("item"):
        return None
    row = db.q1("SELECT claimed_by, claimed_at FROM items WHERE id=?", st["item"])
    c = row["claimed_by"] if row else None
    if not c or c.startswith("orphan:") or c != st.get("claim", c):
        return None
    return row


def reap(t: float, spawn_pool) -> list[str]:
    notes = []
    # 1. stages over budget: kill their processes; the pool thread then records the failure and moves on
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'stage_started:%'"):
        st = db.kv_get(r["k"]) or {}
        if not st.get("at"):
            continue
        limit = STAGE_MAX.get(st.get("status"), STALL)
        if awake_since(st["at"], t) > limit:
            if not _held(st):
                # a running stage always holds its claim; none means the entry outlived its stage (the worker was
                # killed mid-stage and the new one cleared every claim). 2026-10-01: such ghosts were "reaped" with
                # 0 procs every 45 min all night, items 242 and 261 on two threads each.
                db.kv_set(r["k"], {})
                continue
            _kill_reason(st.get("thread", ""), f"stage {st.get('status')} over its {limit} s budget (reaper)", t)
            n = _kill_thread_procs(st.get("thread", ""))
            db.kv_set(r["k"], {**st, "at": t, "killed": st.get("killed", 0) + 1})
            notes.append(f"stage {st.get('status')} #{st.get('item')} over {limit}s: {n} procs killed")
            log("reaper: stage over budget", item=st.get("item"), status=st.get("status"), procs=n)
    # 2. jobs over budget: kill their processes and release the lock so the next tick can run the job again
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'job_started:%'"):
        name = r["k"].split(":", 1)[1]
        started = db.kv_get(r["k"], 0) or 0
        done = db.kv_get(f"job_done:{name}", 0) or 0
        # a run this reaper released still writes job_done when it finally returns, which made the run that replaced
        # it look finished: never reaped, its lock held, every tick skipped (2026-10-22: job-approvals released at
        # 11:14:48, rerun at 11:15:21, the old run's job_done at 11:19:07 hid it; the review backlog waited on it).
        # A live thread of the job that started after the last release is the truth
        live = done >= started and db.kv_get(f"job_force_release:{name}", 0) < started \
            and any(th.name == f"job-{name}" and th.is_alive() for th in threading.enumerate())
        if started and (started > done or live) and awake_since(started, t) > JOB_MAX.get(name, 1800):
            _kill_reason(f"job-{name}", f"job {name} over its {JOB_MAX.get(name, 1800)} s budget (reaper)", t)
            n = _kill_thread_procs(f"job-{name}")
            db.kv_set(f"job_done:{name}", t)
            db.kv_set(f"job_force_release:{name}", t)
            notes.append(f"job {name} over budget: {n} procs killed, lock released")
            log("reaper: job over budget", job=name, procs=n)
    # 3. pool threads that stopped beating (hung in Python): replace them
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'thread_beat:%'"):
        name = r["k"].split(":", 1)[1]
        beat = db.kv_get(r["k"], 0) or 0
        alive = any(th.name == name and th.is_alive() for th in threading.enumerate())
        st = db.kv_get(f"stage_started:{name}") or {}
        held = _held(st)
        if held:  # inside a stage the item heartbeat (claimed_at) is the beat
            beat = max(beat, held["claimed_at"] or 0)
        budget = STAGE_MAX.get(st.get("status"), STALL) + 900 if st.get("at") else 900
        if beat and awake_since(beat, t) > budget:
            _kill_thread_procs(name)
            db.kv_set(r["k"], t)
            db.kv_set(f"stage_started:{name}", {})
            if held:
                db.release(st["item"])
                db.update_item(st["item"], next_run_in=120, note=site.t("reaper: hung thread, retrying", "reaper: hilo colgado, reintento"))
            try:
                spawn_pool(name)
                notes.append(f"thread {name} {'hung' if alive else 'dead'}: replaced")
                log("reaper: pool thread replaced", thread=name, alive=alive)
            except Exception as e:
                log("reaper: spawn failed", thread=name, err=str(e)[:100])
    return notes
