"""Self-DM hygiene. The DM holds exactly three kinds of messages:
  1. the board: one standing catch-up note, always the newest top-level message;
  2. one thread per LIVE case (report at the root, updates and asks as replies);
  3. live asks that are not part of a case (squad reviews, nudges).
Everything else the harness ever posted is deleted: superseded or expired asks, old boards,
PR digests, pauses, health pings, and the threads of cases that ended without a deliverable.
Only messages posted by the harness (his token, no client_msg_id) are ever touched."""
from __future__ import annotations

import json
import subprocess
import time

from . import db, site, slack
from .core import CFG, V1, log, now

CH = CFG["self_dm_channel"]
NOISE_PREFIXES = (":warning:", "⚠️", ":red_circle:", "🔴", ":large_green_circle:", "🟢", ":double_vertical_bar:", "⏸",
                  ":rocket: *Tus PRs*", ":rocket: *Your PRs*", ":negative_squared_cross_mark:", "❎", "~",
                  ":large_yellow_circle: IDLE")
# markers of the harness's own messages; an English install also knows the Spanish ones, since its DM can hold
# messages from before a language change
BOARD_MARKS = ("Pendientes tuyos",) + site.t(("*Waiting on you (",), ())
CASE_MARKS = ("Te dejé", "Qué encontré", "lo mando", "Cierro:") + site.t(("I left you this draft", "What I found", "send it ·", "Closing:"), ())


def _delete(ts: str) -> bool:
    try:
        return slack.delete_self(ts)
    except Exception as e:
        log("dm: delete failed", ts=ts, err=str(e)[:120])
        return False


def _harness(m: dict) -> bool:
    return m.get("user") == CFG["self_user_id"] and not m.get("client_msg_id")


def _holds_live_ask(root_ts: str) -> bool:
    """A thread with a live ask inside it is kept: deleting it takes the ask's message along, and the ask is then never
    answerable."""
    return bool(db.q1("SELECT 1 FROM artifacts WHERE kind='approval' AND outcome IS NULL AND ref!=? "
                      "AND json_extract(content,'$.thread_ts')=?", root_ts, root_ts))


def _delete_thread(root_ts: str) -> int:
    n = 0
    if _holds_live_ask(root_ts):
        return n
    try:
        for r in slack.replies(CH, root_ts):
            if r["ts"] != root_ts and _harness(r) and _delete(r["ts"]):
                n += 1
    except Exception as e:
        log("dm: replies failed", ts=root_ts, err=str(e)[:100])
    if _delete(root_ts):
        n += 1
    return n
# note: a root whose replies include HIS typed messages cannot be fully removed (his messages stay)


def tidy(days: int = 7) -> dict:
    stats = {"asks": 0, "noise": 0, "boards": 0, "cases": 0}
    board_ts = (db.kv_get("board") or {}).get("ts")
    # 1. asks that are no longer live (superseded, expired, rejected, failed): their messages go
    for a in db.q("SELECT id, ref, content FROM artifacts WHERE kind='approval' AND outcome IS NOT NULL AND ref!='' "
                  "AND created_at>?", now() - days * 86400):
        meta = json.loads(a["content"] or "{}")
        if meta.get("deleted") or _holds_live_ask(a["ref"]):
            continue  # a thread kept for its live asks goes in a later pass, once they end
        if _delete_thread(a["ref"]):
            stats["asks"] += 1
        meta["deleted"] = True
        db.x("UPDATE artifacts SET content=? WHERE id=?", json.dumps(meta, ensure_ascii=False), a["id"])
    # 2. top-level noise and old boards
    live_reports = {it["data"].get("report_ts") for it in (db.item(r["id"]) for r in db.q(
        "SELECT id FROM items WHERE status NOT IN ('done','dropped','expired','failed')")) if it["data"].get("report_ts")}
    deliverable_reports = {it["data"].get("report_ts") for it in (db.item(r["id"]) for r in db.q(
        "SELECT id FROM items WHERE json_extract(data,'$.parts') LIKE '%pr_url%'"))
        if it["data"].get("report_ts") and any(p.get("pr_url") for p in it["data"].get("parts", []))}
    live_asks = {a["ref"] for a in db.q("SELECT ref FROM artifacts WHERE kind='approval' AND outcome IS NULL AND ref!=''")}
    try:
        msgs = slack.history(CH, f"{now() - days * 86400:.6f}", pages=10)
    except Exception as e:
        log("dm: history failed", err=str(e)[:100])
        return stats
    for m in msgs:
        if not _harness(m) or (m.get("thread_ts") and m["thread_ts"] != m["ts"]):
            continue
        ts, text = m["ts"], (m.get("text") or "")
        if ts == board_ts or ts in live_asks:
            continue
        if m.get("subtype") == "tombstone" or text == "This message was deleted.":
            stats["noise"] += _delete_thread(ts)  # clear the replies and the tombstone goes with them
            continue
        if any(k in text for k in BOARD_MARKS) or text.startswith((":pushpin:", "📌")):
            if _delete_thread(ts):
                stats["boards"] += 1
            continue
        if text.startswith(NOISE_PREFIXES) or "autopilot recuperado" in text or "autopilot DOWN" in text:
            if _delete_thread(ts):
                stats["noise"] += 1
            continue
        if ts in live_reports or ts in deliverable_reports:
            continue
        # a case thread whose case ended without a deliverable, older than 1 h: gone
        is_case = (any(k in text for k in CASE_MARKS)
                   or db.q1("SELECT 1 FROM items WHERE json_extract(data,'$.report_ts')=?", ts))
        if is_case and now() - float(ts) > 3600:
            stats["cases"] += _delete_thread(ts)
            continue
        # an old-format standalone ask whose item is no longer waiting
        if text.startswith((":large_yellow_circle:", "🟡")) and ts not in live_asks:
            if _delete_thread(ts):
                stats["asks"] += 1
    log("dm tidy", **stats)
    return stats


def keep_board_last() -> None:
    """The status note is always the newest top-level message: if the harness posted case reports
    after it, move it to the bottom (delete + repost), at most every 30 min."""
    from . import board
    st = db.kv_get("board") or {}
    if st.get("digest_due"):
        try:  # an ask nobody answered in 2 days is asked once more with the digest, never dropped
            from . import approvals
            approvals.reask_expired()
        except Exception as e:
            log("re-ask expired failed", err=str(e)[:120])
    if not st.get("ts") or not st.get("digest_due"):  # a repost re-notifies: only when a full digest is due
        return
    db.kv_set("board", {**st, "digest_due": False})
    try:
        newer = [m for m in slack.history(CH, st["ts"], pages=1) if _harness(m) and m["ts"] != st["ts"]
                 and (not m.get("thread_ts") or m["thread_ts"] == m["ts"])]
    except Exception:
        return
    if not newer:
        return
    try:
        slack.pin_self(st["ts"], pin=False)
    except Exception:
        pass
    _delete_thread(st["ts"])  # the board and its detail reply move together
    db.kv_set("board", {**st, "ts": None, "hash": None})
    db.kv_set("board_detail", {})
    board.refresh(force=True)
    db.kv_set("board", {**(db.kv_get("board") or {}), "moved_at": now()})


def delete_ask(ts: str, thread_ts: str | None) -> bool:
    """Remove a dead ask. If the ask is the root of a case thread, only its own replies go
    (confirmations); the case root stays with the case. Returns True when handled."""
    if thread_ts and thread_ts != ts:
        return _delete(ts)  # a reply inside a case thread
    try:
        replies = slack.replies(CH, ts)
    except Exception:
        replies = []
    if any(r.get("client_msg_id") for r in replies):
        return False  # he wrote in this thread: keep it, mark it instead
    return _delete_thread(ts) > 0


def close_case_thread(it: dict, why: str) -> None:
    """Called when a case ends with nothing for the user (dropped, answered by a teammate, moot).
    The report thread is deleted unless he typed in it; a case that produced a deliverable keeps it."""
    d = it["data"]
    ts = d.get("report_ts")
    if not ts or any(p.get("pr_url") for p in d.get("parts", [])):
        return
    if d.get("parent") and (db.item(d["parent"]) or {}).get("data", {}).get("report_ts") == ts:
        return  # the message belongs to the parent case; a child closing never takes it down
    try:
        replies = slack.replies(CH, ts)
    except Exception:
        return
    if any(r.get("client_msg_id") for r in replies):
        slack.send_self(site.t(f"Closing: {why[:200]}", f"Cierro: {why[:200]}"), thread_ts=ts, topic=f"close-{it['id']}")
        return
    if now() - float(ts) < 24 * 3600:
        # a message he may be reading right now is never yanked:
        # it is rewritten to one closed line and its replies go; the tidy removes it after a day
        for r in replies:
            if r["ts"] != ts and _harness(r):
                _delete(r["ts"])
        try:
            slack.edit_self(ts, f"~{it['title'][:90]}~ · {site.t('closed', 'cerrado')}: {why[:160]}", topic=f"close-{it['id']}")
        except Exception as e:
            log("dm: close edit failed", item=it["id"], err=str(e)[:100])
        db.update_item(it["id"], data={"report_closed": True})
        log("dm: case thread closed in place", item=it["id"], why=why[:80])
        return
    n = _delete_thread(ts)
    db.update_item(it["id"], data={"report_deleted": True, "report_ts": None})
    log("dm: case thread removed", item=it["id"], msgs=n, why=why[:80])


def reconcile() -> int:
    """Lifecycle catch-up, zero tokens: a harness case message whose case is closed with no PR should
    not exist; a live case message must be the one its item points at. Runs with the watchdog."""
    n = 0
    try:
        msgs = slack.history(CH, f"{now() - 3 * 86400:.6f}", pages=6)
    except Exception:
        return 0
    board_ts = (db.kv_get("board") or {}).get("ts")
    live_asks = {a["ref"] for a in db.q("SELECT ref FROM artifacts WHERE kind='approval' AND outcome IS NULL AND ref!=''")}
    for m in msgs:
        if not _harness(m) or (m.get("thread_ts") and m["thread_ts"] != m["ts"]) or m["ts"] in (board_ts,) or m["ts"] in live_asks:
            continue
        text = m.get("text") or ""
        if text == "This message was deleted." or m.get("subtype") == "tombstone":
            n += _delete_thread(m["ts"])
            continue
        owners = db.q("SELECT id, status, data FROM items WHERE json_extract(data,'$.report_ts')=?", m["ts"])
        if not owners:
            if text.startswith(":pushpin:") or now() - float(m["ts"]) > 24 * 3600:  # an unowned case message older than a day
                n += _delete_thread(m["ts"])
            continue
        if all(o["status"] in ("done", "dropped", "expired", "failed") and not any(p.get("pr_url") for p in json.loads(o["data"]).get("parts", []))
               for o in owners):
            if now() - float(m["ts"]) < 24 * 3600:
                continue  # closed in place by close_case_thread; removed only after a day
            n += _delete_thread(m["ts"])
    if n:
        log("dm reconcile", removed=n)
    return n
