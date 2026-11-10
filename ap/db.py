"""SQLite state: the single source of truth that v1 kept in prose and loose JSON files.

Every rule v1 re-derived per tick (claims, escalations, expiry, cursors, dedupe)
is a column or a transition here, enforced by code.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from contextlib import contextmanager

from .core import VAR, log, now

DB_PATH = VAR / "autopilot.db"
LIVE_DB = DB_PATH
_local = threading.local()


def is_live() -> bool:
    """False in a process that swapped in a scratch DB (the selftest). Anything that faces the user, like the app's
    ports, opens only on the live DB: on 10-22 the worker's startup selftest ran while port 4848 was free (the
    healthcheck had just killed the old worker), its hot.tick test opened the console there, and Clea.app showed the
    selftest's fake cases as his."""
    return DB_PATH == LIVE_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, source TEXT, channel TEXT, ts TEXT,
  thread_ts TEXT, user TEXT, text TEXT, payload TEXT, created_at INTEGER,
  triaged INTEGER DEFAULT 0, item_id INTEGER);
CREATE INDEX IF NOT EXISTS ev_untriaged ON events(triaged, created_at);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, kind TEXT, status TEXT, title TEXT, thread_key TEXT,
  repo TEXT, pr INTEGER, urgency INTEGER DEFAULT 1, data TEXT DEFAULT '{}',
  created_at INTEGER, updated_at INTEGER, next_run_at INTEGER DEFAULT 0,
  claimed_by TEXT, claimed_at INTEGER, attempts INTEGER DEFAULT 0, last_error TEXT);
CREATE INDEX IF NOT EXISTS it_run ON items(status, next_run_at);
CREATE UNIQUE INDEX IF NOT EXISTS it_thread_open ON items(thread_key)
  WHERE status NOT IN ('done','dropped','expired','failed');

CREATE TABLE IF NOT EXISTS transitions (
  item_id INTEGER, src TEXT, dst TEXT, at INTEGER, note TEXT);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY, item_id INTEGER, kind TEXT, ref TEXT, content TEXT,
  created_at INTEGER, outcome TEXT, outcome_at INTEGER, outcome_data TEXT);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, kind TEXT, item_id INTEGER, artifact_id INTEGER,
  data TEXT, at INTEGER, mined INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY, lane TEXT, text TEXT, trigger TEXT, status TEXT,
  evidence TEXT DEFAULT '[]', created_at INTEGER, activated_at INTEGER,
  last_used_at INTEGER, hits INTEGER DEFAULT 0, contradictions INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS budget (
  day TEXT, tier TEXT, calls INTEGER DEFAULT 0, usd REAL DEFAULT 0,
  in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0, PRIMARY KEY(day, tier));

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, at INTEGER, tier TEXT, lane TEXT, item_id INTEGER,
  ok INTEGER, dur_s REAL, usd REAL, in_tok INTEGER, out_tok INTEGER, note TEXT);

CREATE TABLE IF NOT EXISTS prs (
  repo TEXT, number INTEGER, data TEXT, head_sha TEXT, updated_at INTEGER,
  own INTEGER, PRIMARY KEY(repo, number));

CREATE TABLE IF NOT EXISTS merge_deps (
  repo TEXT, number INTEGER, dep_repo TEXT, dep_number INTEGER, need TEXT,
  reason TEXT, source TEXT, PRIMARY KEY(repo, number, dep_repo, dep_number));

CREATE TABLE IF NOT EXISTS branches (
  repo TEXT, branch TEXT, item_id INTEGER, created_at INTEGER, PRIMARY KEY(repo, branch));

CREATE TABLE IF NOT EXISTS console_msgs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT, role TEXT, text TEXT, at INTEGER, item_id INTEGER, meta TEXT);
CREATE INDEX IF NOT EXISTS console_msgs_conv ON console_msgs(conv, id);

CREATE TABLE IF NOT EXISTS scan_files (
  repo TEXT, path TEXT, blob TEXT, scanned_at INTEGER, lanes TEXT, findings INTEGER, PRIMARY KEY(repo, path));
CREATE TABLE IF NOT EXISTS scan_findings (
  id INTEGER PRIMARY KEY, repo TEXT, path TEXT, blob TEXT, line INTEGER, title TEXT, severity TEXT, lanes TEXT,
  data TEXT, status TEXT DEFAULT 'new', verdict TEXT, item_id INTEGER, created_at INTEGER, updated_at INTEGER);
CREATE INDEX IF NOT EXISTS scan_findings_status ON scan_findings(status, severity);
"""


_conns: dict = {}  # thread -> (path, its connection)
_conns_lock = threading.Lock()


def conn() -> sqlite3.Connection:
    """One connection per thread, and a thread that ended hands its connection to the next new one. A connection
    dropped without close() keeps its file open for the life of the process, and every job runs in a fresh thread:
    at 23:30:40 on 10-02 the worker hit the 256 open-file limit and its scheduler died."""
    c = getattr(_local, "c", None)
    if c is None:
        me = threading.current_thread()
        with _conns_lock:
            stale = [t for t in _conns if not t.is_alive() or t is me]
            for t in stale:
                path, old = _conns.pop(t)
                if c is None and path == DB_PATH and t is not me:
                    c = old
                else:
                    old.close()
        if c is None:
            c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.executescript(SCHEMA)
        elif c.in_transaction:
            c.execute("ROLLBACK")  # its thread ended inside tx()
        with _conns_lock:
            _conns[me] = (DB_PATH, c)
        _local.c = c
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def q(sql: str, *args) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def q1(sql: str, *args):
    r = conn().execute(sql, args).fetchone()
    return r


def x(sql: str, *args) -> int:
    cur = conn().execute(sql, args)
    return cur.lastrowid


# ---- kv -------------------------------------------------------------------
def kv_get(k: str, default=None):
    r = q1("SELECT v FROM kv WHERE k=?", k)
    return json.loads(r["v"]) if r else default


def kv_set(k: str, v) -> None:
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, json.dumps(v))


# ---- events ---------------------------------------------------------------
def fmt_time(t: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(float(t)).strftime("%d/%m %H:%M")


def add_event(key: str, source: str, *, channel="", ts="", thread_ts="", user="", text="",
              payload: dict | None = None) -> bool:
    """Insert once; returns True if new. Dedupe is the UNIQUE key."""
    try:
        x("INSERT INTO events(key,source,channel,ts,thread_ts,user,text,payload,created_at)"
          " VALUES(?,?,?,?,?,?,?,?,?)", key, source, channel, ts, thread_ts or "", user, text,
          json.dumps(payload or {}, ensure_ascii=False), now())
        return True
    except sqlite3.IntegrityError:
        return False


# ---- items ----------------------------------------------------------------
TERMINAL = ("done", "dropped", "expired", "failed")


def item(item_id: int) -> dict | None:
    r = q1("SELECT * FROM items WHERE id=?", item_id)
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"] or "{}")
    return d


def _words(s: str) -> set[str]:
    import re as _re
    stop = {"para", "con", "los", "las", "del", "que", "una", "por", "sobre", "the", "and", "for", "con", "sin", "este",
            "esta", "sus", "como", "más", "mas", "hacia", "desde", "entre", "revisar", "definir", "completar"}
    return {w for w in _re.split(r"[^a-záéíóúñ0-9#]+", s.lower()) if len(w) >= 4 and w not in stop}


def similar_recent_item(title: str, days: int = 7, threshold: float = 0.5, exclude_id: int | None = None,
                        kinds: tuple = ("TASK", "INCIDENT", "IDEA", "OPEN_LOOP", "REPLY")) -> dict | None:
    """An item (any status) from the last `days` whose title shares most content words with `title`."""
    w = _words(title)
    if len(w) < 2:
        return None
    for r in q(f"SELECT id, title, status FROM items WHERE created_at>? AND kind IN ({','.join('?' * len(kinds))})"
               " ORDER BY id", now() - days * 86400, *kinds):
        if r["id"] == exclude_id:
            continue
        ow = _words(r["title"])
        if ow and len(w & ow) / len(w | ow) >= threshold:
            return dict(r)
    return None


def open_item_for_thread(thread_key: str) -> dict | None:
    r = q1(f"SELECT id FROM items WHERE thread_key=? AND status NOT IN {TERMINAL}", thread_key)
    return item(r["id"]) if r else None


def create_item(kind: str, status: str, title: str, thread_key: str, *, repo=None, pr=None,
                urgency=1, data: dict | None = None) -> int:
    t = now()
    from . import site
    blob = f"{title} {thread_key} {str((data or {}).get('text') or '')[:600]}"
    note = "created"
    drop, author = site.drop_re(), str((data or {}).get("author_name") or "").lower()
    if (drop and drop.search(blob)) or any(a in author for a in site.drop_authors()):
        # feature requests (Alfred's FR tickets) are product's call, not the squad's: never a case (the user, 2026-09-29:
        # "don't handle feature requests, or FR by alfred, you're just wasting tokens"), unless a human pulled the
        # squad or him into that thread asking for help
        if not human_asked_squad(data or {}):
            # the drop says why: a bare 'created' read as an urgent key-account case dropped with no reason (#720, #722)
            status, note = "dropped", f"created dropped: {site.drop_note()}"
    if status not in TERMINAL:
        # the same work already open: fold this into it, zero tokens (ap/dedupe.py; 2026-10-01: ~230 USD a week went to
        # cases later dropped as duplicates)
        from . import dedupe
        dup = dedupe.same_work(kind, title, thread_key, data or {})
        if dup:
            dedupe.fold(dup[0], title, thread_key, data or {}, dup[1])
            return dup[0]["id"]
    iid = x("INSERT INTO items(kind,status,title,thread_key,repo,pr,urgency,data,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", kind, status, title, thread_key, repo, pr, urgency,
            json.dumps(data or {}, ensure_ascii=False), t, t)
    x("INSERT INTO transitions VALUES(?,?,?,?,?)", iid, "", status, t, note)
    return iid


def human_asked_squad(data: dict) -> bool:
    """Did a human (not a bot such as Alfred) tag the the team subteam or the user in this case's thread with a real
    message (not a bare ack)? Reads the thread when the case has one; else only the case text."""
    from .core import CFG
    from . import prefilter, site, slack
    me, subs = CFG.get("self_user_id", ""), CFG.get("team_subteam_tags", [])
    tags = [f"<@{me}>"] + [f"<!subteam^{s}" for s in subs] + [f"<@{s}>" for s in subs]
    msgs = [{"user": data.get("author") or "", "text": data.get("text") or ""}]
    ch, tts = data.get("channel"), data.get("thread_ts") or data.get("ts")
    if ch and tts:
        try:
            msgs += slack.replies(ch, tts)
        except Exception:
            pass
    for m in msgs:
        text, user = m.get("text") or "", m.get("user") or ""
        tagged = any(t in text for t in tags) or site.team_tag_note() in text  # rendered case texts carry the label
        if not tagged or m.get("bot_id") or not user.startswith("U") or slack.user_is_bot(user):
            continue
        if prefilter.noise_reason(text, user=user) is None:
            return True
    return False
def _own_stage(item_id: int) -> dict:
    """The stage of this item running in this thread (worker.run_stage marks it), or {}."""
    st = kv_get(f"stage_started:{threading.current_thread().name}") or {}
    return st if st.get("item") == item_id and st.get("at") else {}


def _ended_elsewhere(item_id: int) -> bool:
    """A stage never moves its item out of a terminal status another thread set while it ran. Reopens from outside a
    stage (rejudge, revert, his chat) are untouched."""
    st = _own_stage(item_id)
    return bool(st) and getattr(_local, "ended", None) != (item_id, st["at"])


def update_item(item_id: int, *, status: str | None = None, note: str = "", data: dict | None = None,
                next_run_in: int | None = None, **cols) -> None:
    it = item(item_id)
    if it is None:
        return
    if status and status != it["status"] and it["status"] in TERMINAL:
        # any change, even to another terminal status: a stage finishing late turned a linked-to-PR "done" into "dropped",
        # and closing.py never closes the ticket of a dropped case
        if _ended_elsewhere(item_id):
            log("stage result ignored: the item was ended meanwhile", item=item_id, was=it["status"], wanted=status)
            status = None
    if status in TERMINAL and _own_stage(item_id):
        _local.ended = (item_id, _own_stage(item_id)["at"])
    sets, args = ["updated_at=?"], [now()]
    if status == "blocked" and status != it["status"]:
        # each block states its own cause: a failure's keys left from an earlier block (#492 keeps blocked_stage 'plan'
        # and a harness_bug_rev while awaiting approval) would make a later verdict block read as that failure
        data = {"blocked_stage": None, "blocked_by": None, "harness_bug_rev": None, **(data or {})}
    if data:
        merged = {**it["data"], **data}
        sets.append("data=?")
        args.append(json.dumps(merged, ensure_ascii=False, default=str))
    moved = bool(status and status != it["status"])
    if moved:
        sets.append("status=?")
        args.append(status)
    if next_run_in is not None:
        sets.append("next_run_at=?")
        args.append(now() + next_run_in)
    for k, v in cols.items():
        sets.append(f"{k}=?")
        args.append(v)
    args.append(item_id)
    x(f"UPDATE items SET {', '.join(sets)} WHERE id=?", *args)
    if moved:  # after the update: a reopen refused by it_thread_open left a transition that never happened (2026-10-02
        # 15:32: #414 and #423 read 'dropped > reply_draft, rejudge under new rules' and stayed dropped)
        x("INSERT INTO transitions VALUES(?,?,?,?,?)", item_id, it["status"], status, now(), note[:500])


DELIVERY_ORDER = ["publish", "verify_done", "announce", "announce_check", "comments_reply", "reply_check", "reply_draft",
                  "self_review", "self_find", "fix", "gate", "ci_fix", "comments_verify", "propose_check",
                  "ci_wait", "build", "review_post", "review_verify", "critique", "telemetry", "reply_draft",
                  "comments_triage", "propose", "plan", "review_find", "investigate", "review_prepare", "loop_track"]

# the stages the worker runs outside work hours at any urgency (worker.pool); the rest wait for work hours unless the
# case is urgent. An on-call dev ships fixes on weekends too (2026-09-27: TICKET-1355 sat 'construyendo' for 4 days because
# Sat/Sun were off); babysitting HIS PRs (threads, waits) and vetting proposals into cases are delivery too (2026-09-27
# Sunday night: cometa#1475's threads sat 50 min in comments_verify)
OFF_HOURS_STAGES = {"build", "fix", "gate", "self_find", "self_review", "publish", "ci_wait", "ci_fix", "verify_done",
                    "pr_body_refresh", "critique", "plan", "telemetry", "announce", "announce_check", "comments_triage",
                    "comments_verify", "comments_reply", "waiting", "vet"}


def running_here() -> list[int]:
    """Items a stage of this process is running now: worker.run_stage keeps a `beat-<id>` thread alive exactly while
    the stage runs. A claim's age is wall-clock and the Mac sleeps: on each dark wake (no tick, so no heartbeat yet)
    a free thread found the claim 'stale' and ran the same stage again. 2026-10-01: #367's verify_done ran twice and
    #369's loop_track three times concurrently between 18:50 and 21:42."""
    return [int(th.name[5:]) for th in threading.enumerate() if th.name.startswith("beat-") and th.name[5:].isdigit()
            and th.is_alive()]


def claim_next(statuses: list[str], worker: str, stale_s: int = 900, kinds: list[str] | None = None,
               urgent_only: list[str] | None = None) -> dict | None:
    """Atomically claim the most urgent runnable item in the given statuses (optionally of the given kinds; in the
    urgent_only statuses only an urgent item). An item whose stage is still running here is never claimed, however old
    its claim looks."""
    marks = ",".join("?" * len(statuses))
    kind_sql = f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else ""
    if urgent_only:
        kind_sql += f" AND (urgency>=3 OR status NOT IN ({','.join('?' * len(urgent_only))}))"
    filters = [*(kinds or []), *(urgent_only or [])]
    live = running_here()
    live_sql = f" AND id NOT IN ({','.join('?' * len(live))})" if live else ""
    with tx() as c:
        # finish what's started: stages closer to delivery first, then urgency, with aging so
        # older items rise
        stage_rank = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(DELIVERY_ORDER))
        # at most one squad-review finder run at a time: 9 passes each used to hold every fast thread
        if "review_find" in statuses and c.execute(
                "SELECT 1 FROM items WHERE status='review_find' AND claimed_by IS NOT NULL AND claimed_at>=?",
                (now() - stale_s,)).fetchone():
            statuses = [s for s in statuses if s != "review_find"]
            if not statuses:
                return None
            marks = ",".join("?" * len(statuses))
        r = c.execute(
            f"SELECT id FROM items WHERE status IN ({marks}) AND next_run_at<=? AND "
            f"(claimed_by IS NULL OR claimed_at<?){kind_sql}{live_sql} ORDER BY "
            f"(CASE status {stage_rank} ELSE 99 END) - urgency * 3 - (? - created_at) / 14400 ASC, created_at ASC LIMIT 1",
            (*statuses, now(), now() - stale_s, *filters, *live, now())).fetchone()
        if not r:
            return None
        c.execute("UPDATE items SET claimed_by=?, claimed_at=? WHERE id=?", (worker, now(), r["id"]))
    return item(r["id"])


def heartbeat(item_id: int, worker: str) -> None:
    x("UPDATE items SET claimed_at=? WHERE id=? AND claimed_by=?", now(), item_id, worker)


def release(item_id: int) -> None:
    x("UPDATE items SET claimed_by=NULL, claimed_at=NULL WHERE id=?", item_id)


# ---- artifacts / signals --------------------------------------------------
def add_artifact(item_id: int, kind: str, ref: str, content: str = "") -> int:
    return x("INSERT INTO artifacts(item_id,kind,ref,content,created_at) VALUES(?,?,?,?,?)",
             item_id, kind, ref, content, now())


def set_artifact_outcome(art_id: int, outcome: str, data: dict | None = None) -> None:
    x("UPDATE artifacts SET outcome=?, outcome_at=?, outcome_data=? WHERE id=?",
      outcome, now(), json.dumps(data or {}, ensure_ascii=False), art_id)


def add_signal(kind: str, data: dict, item_id: int | None = None, artifact_id: int | None = None) -> None:
    x("INSERT INTO signals(kind,item_id,artifact_id,data,at) VALUES(?,?,?,?,?)",
      kind, item_id, artifact_id, json.dumps(data, ensure_ascii=False, default=str), now())


# ---- budget ---------------------------------------------------------------
def today() -> str:
    return dt.date.today().isoformat()


def record_call(tier: str, lane: str, item_id: int | None, ok: bool, dur_s: float, usd: float,
                in_tok: int, out_tok: int, note: str = "") -> int:
    row = x("INSERT INTO calls(at,tier,lane,item_id,ok,dur_s,usd,in_tok,out_tok,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            now(), tier, lane, item_id, int(ok), dur_s, usd, in_tok, out_tok, note[:300])
    x("INSERT INTO budget(day,tier,calls,usd,in_tok,out_tok) VALUES(?,?,1,?,?,?) "
      "ON CONFLICT(day,tier) DO UPDATE SET calls=calls+1, usd=usd+excluded.usd, "
      "in_tok=in_tok+excluded.in_tok, out_tok=out_tok+excluded.out_tok",
      today(), tier, usd, in_tok, out_tok)
    try:  # Langfuse mirror (v1 parity); never blocks or fails the call
        from . import trace
        trace.record(tier, lane, item_id=item_id, ok=ok, dur_s=dur_s, in_tok=in_tok, out_tok=out_tok, usd=usd, note=note)
    except Exception:
        pass
    return row


def spent_today(tier: str = "claude") -> float:
    r = q1("SELECT usd FROM budget WHERE day=? AND tier=?", today(), tier)
    return float(r["usd"]) if r else 0.0


def touch_rules(lanes: list[str]) -> None:
    """A rule counts as used whenever its lane runs with it in the pack (Copilot-memory
    style expiry): rules that silently prevent mistakes must not age out."""
    marks = ",".join("?" * len(lanes))
    x(f"UPDATE rules SET last_used_at=?, hits=hits+1 WHERE status='active' AND lane IN ({marks})", now(), *lanes)
