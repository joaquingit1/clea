"""Ingest: turn Slack and GitHub activity into events/items. Zero model tokens.

Slack coverage is exactly v1's: team channel, PR channel (the team tag or squad-member PR links), watched
channels (#squad-sis, #sis-ops), the alerts channel, his DMs, and mentions of him /
the the team subteam / "the team" anywhere (Slack search, which also covers group
DMs and thread replies). Added: replies inside threads he or an open item is part of
(conversations.history only returns roots, which is how v1 missed replies).
"""
from __future__ import annotations

import json
import re
import time

from . import cases, db, gh, merge_order, site, slack
from .core import CFG, log, now

SELF = CFG["self_user_id"]
TEAM = set(CFG.get("team_members", {}).keys()) - {"_note"}
THREAD_TTL_S = 5 * 86400
DM_SESSION_S = 6 * 3600


def _cursor(name: str) -> str:
    c = db.kv_get(f"cursor:{name}")
    if c is None:
        c = f"{time.time() - 4 * 3600:.6f}"
        db.kv_set(f"cursor:{name}", c)
    return c


def _aimed(ch: str, m: dict, extra: dict) -> bool:
    """Is this message for the user? A DM, a mention of him or the the team group, or a reply in
    a thread he started or is part of. These never wait for the free-model triage queue."""
    text = m.get("text") or ""
    if extra.get("channel_kind") == "dm" or f"<@{SELF}>" in text:
        return True
    if any(f"<!subteam^{s}" in text or f"<@{s}>" in text for s in CFG.get("team_subteam_tags", [])):
        return True  # search.messages renders group tags as <@S...>
    tts = m.get("thread_ts")
    if tts and tts != m["ts"]:
        th = db.kv_get("threads", {}).get(f"{ch}:{tts}", {})
        return bool(th.get("self_root") or th.get("self_in"))
    return False


def _fast_reply(ev_key: str, ch: str, m: dict, extra: dict) -> None:
    """v1's daemon routed anything aimed at him straight to the reply lane (~2 min). v2 lost
    that."""
    dm = extra.get("channel_kind") == "dm"
    if not dm and not m.get("thread_ts"):
        # search results and re-judged events often lack thread_ts: resolve the real root so every
        # reply of one conversation lands in ONE case
        root = slack.resolve_root(ch, m["ts"])
        if root != m["ts"]:
            m = {**m, "thread_ts": root}
    # a DM has no threads: the conversation IS the unit (2026-09-23: a teammate's 4 messages became
    # 4 cases, the report saw one line and drafted "acá estoy, decime"). Channel threads stay
    # keyed by thread. A short debounce lets a burst of messages land before drafting.
    tk = f"slack:{ch}:dm" if dm else f"slack:{ch}:{m.get('thread_ts') or m['ts']}"
    ev = db.q1("SELECT id FROM events WHERE key=?", ev_key)
    text = slack.render(m.get("text") or "")
    existing = reply_case(tk, dm)
    if existing:
        if existing["kind"] == "REPLY":
            continue_case(existing, ev["id"], m["ts"], text, dm)
        else:
            _note_update(existing, ev["id"], m["ts"], text)
        db.x("UPDATE events SET item_id=? WHERE id=?", existing["id"], ev["id"])
        if existing["kind"] != "REPLY" and not dm:
            # a question to him in the thread of a build/incident case is still a question: it gets its own reply case
            # (linked to the case) instead of sitting as a note (2026-10-19: a teammate asked him about boletas permissions in
            # a teammate's thread, it was filed on #890's updates and nothing answered it). One per thread, like any
            # conversation: keyed by the message, every later question in that thread opened another
            qk = f"{tk}:q"
            asked = _question_case(qk)
            if asked:
                continue_case(asked, ev["id"], m["ts"], text, False)
            else:
                iid = db.create_item("REPLY", "reply_draft", f"(slack) {text[:80]}", qk, urgency=2,
                                     data={"channel": ch, "ts": m["ts"], "thread_ts": m.get("thread_ts") or m["ts"],
                                           "text": text[:4000], "author": m.get("user"), "summary": text[:300], "fast": True,
                                           "dm": False, "last_ts": m["ts"], "parent": existing["id"]})
                db.update_item(iid, next_run_in=45)
                log("ingest: question in a case thread, own reply case", item=iid, case=existing["id"])
    else:
        iid = db.create_item("REPLY", "reply_draft", f"(slack) {text[:80]}", tk, urgency=3 if dm else 2,
                             data={"channel": ch, "ts": m["ts"], "thread_ts": None if dm else (m.get("thread_ts") or m["ts"]),
                                   "text": text[:4000], "author": m.get("user"), "summary": text[:300], "fast": True,
                                   "dm": dm, "last_ts": m["ts"]})
        db.update_item(iid, next_run_in=90 if dm else 45)
        db.x("UPDATE events SET item_id=? WHERE id=?", iid, ev["id"])
        if not dm:  # follow the thread from now on: a teammate's answer must reach this case
            watch_thread(ch, m.get("thread_ts") or m["ts"], m["ts"])
        log("ingest: aimed at the user, reply lane now", item=iid, ch=ch)
    db.x("UPDATE events SET triaged=1 WHERE id=?", ev["id"])


def reply_case(tk: str, dm: bool = False) -> dict | None:
    """The case of a conversation: the open one, else its last reply case closed while the conversation is still alive
    (a DM: 6 h since it last moved; a thread: as long as it is watched). One conversation is one case. A thread case
    used to be reusable only while open, so in a thread he takes part in (where every message counts as aimed at him)
    each message after the case closed opened a new one that reread the whole thread: 13 cases for one #sis-ops thread
    on 10-21 (#1066 to #1086, eleven of them ending 'ya respondió él' or 'no reply needed'), 20 for another, and 83 of
    the 221 reply cases opened from 10-02 to 10-23 were such repeats, each a separate thread in the app."""
    it = db.open_item_for_thread(tk)
    if it:
        return it
    r = db.q1("SELECT id FROM items WHERE thread_key=? AND kind='REPLY' AND updated_at>? ORDER BY id DESC",
              tk, now() - (DM_SESSION_S if dm else THREAD_TTL_S))
    return db.item(r["id"]) if r else None


def _question_case(qk: str) -> dict | None:
    """The reply case for questions asked in a work case's thread (key '<thread>:q'; ones opened per message before
    are '<thread>:q:<ts>'): the open one, else the last closed while the thread is watched."""
    r = db.q1(f"SELECT id FROM items WHERE kind='REPLY' AND (thread_key=? OR thread_key LIKE ?) AND "
              f"(status NOT IN {db.TERMINAL} OR updated_at>?) ORDER BY (status NOT IN {db.TERMINAL}) DESC, id DESC",
              qk, qk + ":%", now() - THREAD_TTL_S)
    return db.item(r["id"]) if r else None


def _note_update(case: dict, ev_id: int, ts: str, text: str) -> dict:
    """The message on the case's updates, once: impact.link puts a thread message there before the reply lane sees it,
    and a second entry made every later stage read it twice."""
    ups = case["data"].get("updates", [])
    if not any(u.get("event_id") == ev_id for u in ups):
        ups = ups + [{"event_id": ev_id, "text": text[:1500], "ts": ts}]
    patch = {"updates": ups[-20:], "last_ts": ts}
    db.update_item(case["id"], data=patch)
    return patch


# what one round of a reply case leaves behind that the next round must not read as its own: the draft, his change
# request, the wait for facts, and the report message once the round closed it
ROUND = {"reply_text": None, "revision": None, "revision_of": None, "moved_redrafts": 0, "held_for_facts": False,
         "needs_you": False}


def continue_case(case: dict, ev_id: int, ts: str, text: str, dm: bool) -> None:
    """A new message in a reply case's conversation: the case rereads it (after his own answer a small model first
    asks whether the news needs him, replies._settled_by_him). A closed case starts a new round instead of a new case."""
    d = case["data"]
    _note_update(case, ev_id, ts, text)
    if case["status"] in db.TERMINAL:
        child = db.item(d["child"]) if d.get("child") else None
        running = bool(child and child["status"] not in db.TERMINAL)
        fresh = dict(ROUND, round=d.get("round", 1) + 1)
        if not running and (d.get("dossier") or d.get("verified")):
            # the last round's investigation answered the last round's question
            fresh.update(prior_dossier=d.get("dossier"), dossier=None, verified=None, verified_at=None, child=None)
        if d.get("report_closed") or d.get("report_deleted"):
            # its message in the DM was closed with the round (struck through, or deleted): the new round reports anew
            fresh.update(report_ts=None, report_closed=None, report_deleted=None, detail_ts=None, ping_sig=None)
        db.update_item(case["id"], status="reply_draft", next_run_in=90, attempts=0, data=fresh,
                       note=f"new message in the same conversation: round {fresh['round']} of this case")
        log("ingest: closed reply case continued", item=case["id"], round=fresh["round"])
        return
    # a thread case whose ask stands is the approval poll's: it first sees whether he answered himself, then redrafts
    # once the thread settles (_thread_moved). Reset here, the ask went out as moot over his own answer (2026-10-02:
    # #414, he answered at 11:49, the messages after it came in at 12:14 after an outage and the case redrafted on Opus)
    held = case["status"] == "awaiting_approval" and not dm and db.q1(
        "SELECT 1 FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL AND ref IS NOT NULL "
        "AND json_extract(content,'$.keep_status') IS NOT 1", case["id"])
    if not held and case["status"] in ("awaiting_human", "awaiting_approval", "reply_draft"):
        # the standing draft no longer answers the conversation: redo it over everything said
        db.update_item(case["id"], status="reply_draft", next_run_in=90, attempts=0,
                       note="new message, rereading the whole conversation (a redraft only if it still needs him)")


def _ignored(ch: str) -> bool:
    """Channels the harness never reads."""
    return ch in CFG.get("ignored_channels", [])


def _event_from_msg(ch: str, m: dict, source: str, extra: dict | None = None) -> bool:
    if _ignored(ch):
        return False
    key = f"slack:{ch}:{m['ts']}"
    extra = extra or {}
    # an alerts-channel root message is an alert whichever path saw it first: the event key is channel+ts, so when the
    # mention search ("the team" matches the channel) inserted it first as a mention, the alert ledger never saw it and
    # triage opened one INCIDENT per hourly repeat
    if _channels().get(ch) == "alerts" and (not m.get("thread_ts") or m.get("thread_ts") == m["ts"]) \
            and (m.get("bot_id") or m.get("subtype") == "bot_message"):  # a person writing there is not an alert
        source = "slack-alerts"
    from . import prefilter
    payload = {"channel_name": extra.get("channel_name"),
               "reply_count": m.get("reply_count", 0), "files": [f.get("name") for f in m.get("files", [])],
               # attachment metadata and message shape for the deterministic prefilter
               "files_meta": prefilter.file_meta(m), "media": prefilter.media_urls(m), "subtype": m.get("subtype"),
               "bot": bool(m.get("bot_id")) and not m.get("user"),
               "self": m.get("user") == SELF, "squad": m.get("user") in TEAM, **extra}
    new = db.add_event(key, source, channel=ch, ts=m["ts"], thread_ts=m.get("thread_ts") or "",
                       user=m.get("user") or m.get("bot_id") or "", text=slack.message_text(m), payload=payload)
    if new and (m.get("reply_count") or m.get("user") == SELF):
        watch_thread(ch, m.get("thread_ts") or m["ts"], m["ts"], self_msg=m.get("user") == SELF,
                     root=not m.get("thread_ts") or m.get("thread_ts") == m["ts"])
    if new and m.get("user") == SELF and ch != CFG["self_dm_channel"]:
        try:
            _self_loop(ch, m, key)
        except Exception as e:
            log("ingest: promise loop failed", key=key, err=f"{type(e).__name__}: {e}"[:200])
    if new and m.get("user") == cases.STATUS_BOT and m.get("thread_ts") and m["thread_ts"] != m["ts"]:
        # Status created a ticket in a thread we work: the case is that ticket (ap/cases.bind_ticket)
        key = cases.created_ticket(slack.render(m.get("text") or ""))
        if key:
            try:
                from . import impact
                owner = impact._case_for(ch, m["thread_ts"])
                if owner and cases.bind_ticket(owner["id"], key, f"{ch}:{m['ts']}"):
                    log("ingest: case bound to its ticket", item=owner["id"], ticket=key)
            except Exception as e:
                log("ingest: ticket bind failed", key=key, err=str(e)[:160])
    if new and m.get("user") and m.get("user") != SELF and slack.is_human(m):
        aimed = _aimed(ch, m, extra)
        try:  # news in the thread of an open case goes to that case first, deterministically (ap/impact.py)
            from . import impact
            linked = impact.link(key, ch, m, aimed=aimed)
        except Exception as e:
            linked = False
            log("ingest: impact link failed", key=key, err=f"{type(e).__name__}: {e}"[:200])
        if linked and not aimed:
            return new  # the case owns it; a question aimed at him still gets the reply lane below
        noise = prefilter.from_message(m, SELF) if aimed else None
        log("ingest: new event", key=key, source=source, aimed=aimed, **({"prefiltered": noise} if noise else {}))
        if noise:
            # an ack, emoji or gif aimed at him is not an ask: no reply case, no model (the case thread, if any,
            # still shows it when its context is rebuilt from Slack)
            ev = db.q1("SELECT id FROM events WHERE key=?", key)
            if ev:
                prefilter.drop_event(ev["id"], noise)
            aimed = False
        elif aimed:
            prefilter.count("fast_lane")
        if aimed:
            try:
                _fast_reply(key, ch, m, extra)
            except Exception as e:
                log("ingest: fast reply failed", key=key, err=f"{type(e).__name__}: {e}"[:200])
    return new


LOOP_FOLLOWUP_S = 300  # a message of his this long after the promise, in the same thread, closes the loop


def _self_loop(ch: str, m: dict, ev_key: str) -> None:
    """His promise in a thread ("ahora lo cierro", "lo veo y te aviso") opens an OPEN_LOOP case for that thread,
    deterministically (the triage model used to see only some of his messages and could call them noise); his next
    message in the thread at least 5 min later closes it. A DM has no threads: the whole DM is the thread,
    as in the reply lane."""
    from . import prefilter
    text = slack.message_text(m)
    dm = ch.startswith("D") and not m.get("thread_ts")
    root = m.get("thread_ts") or m["ts"]
    tk = f"loop:{ch}:{'dm' if dm else root}"
    loop = db.open_item_for_thread(tk)
    if prefilter.promise(text):
        ev = db.q1("SELECT id FROM events WHERE key=?", ev_key)
        if loop:
            db.update_item(loop["id"], data={"text": slack.render(text)[:4000], "ts": m["ts"], "promise_ts": m["ts"]})
        else:
            later = db.q1("SELECT 1 FROM events WHERE channel=? AND user=? AND (? OR thread_ts=? OR ts=?) AND CAST(ts AS REAL)>=?",
                          ch, SELF, dm, root, root, float(m["ts"]) + LOOP_FOLLOWUP_S)
            if later:
                return  # read out of order (search returns newest first): he already followed up
            iid = db.create_item("OPEN_LOOP", "loop_track", site.t("(promise) ", "(promesa) ") + slack.render(text)[:80], tk, urgency=1,
                                 data={"channel": ch, "ts": m["ts"], "thread_ts": root, "text": slack.render(text)[:4000],
                                       "author": SELF, "from": "promise", "promise_ts": m["ts"]})
            log("ingest: his promise opened a loop", item=iid, ch=ch, thread=root)
            if ev:
                db.x("UPDATE events SET item_id=?, triaged=1 WHERE id=?", iid, ev["id"])  # no second loop from the triage model
        return
    # every loop on this conversation, whoever opened it: the triage model and the backfill key theirs by the Slack
    # thread or message and keep the conversation in data (2026-10-01: #347 kept asking him to answer a teammate after his
    # 17:48 'ahi contesté tu comment' in its thread, and #370 after he sent the codes in the DM, since only the
    # 'loop:' key of his own promises was looked up)
    for r in db.q("SELECT id FROM items WHERE kind='OPEN_LOOP' AND status NOT IN ('done','dropped','expired','failed') AND "
                  "(thread_key=? OR (json_extract(data,'$.channel')=? AND (? OR json_extract(data,'$.thread_ts')=?)))",
                  tk, ch, dm, root):
        loop = db.item(r["id"])
        if float(m["ts"]) - float(loop["data"].get("promise_ts") or loop["data"].get("ts") or 0) >= LOOP_FOLLOWUP_S:
            db.update_item(loop["id"], status="done", note=site.t("you followed up in the thread", "él siguió en el hilo"), data={"closed_by_ts": m["ts"]})
            log("ingest: his follow-up closed a loop", item=loop["id"], ch=ch, thread=root)


import threading as _threading

_threads_lock = _threading.RLock()  # the watch list is one kv value rewritten whole by several jobs


def watch_thread(ch: str, thread_ts: str, seen_ts: str, self_msg: bool = False, root: bool = False) -> None:
    if _ignored(ch):
        return
    with _threads_lock:
        threads = db.kv_get("threads", {})
        k = f"{ch}:{thread_ts}"
        prev = threads.get(k, {})
        threads[k] = {**prev, "seen": max(prev.get("seen", "0"), seen_ts, key=float), "touched": now(),
                      "self_root": prev.get("self_root") or (self_msg and root), "self_in": prev.get("self_in") or self_msg}
        db.kv_set("threads", threads)


def _pr_channel_relevant(m: dict) -> bool:
    text = m.get("text") or ""
    kw, tags = site.keyword_re(), CFG.get("team_subteam_tags", [])
    if (kw and kw.search(text)) or any(f"<!subteam^{s}" in text for s in tags):
        return True
    if not CFG.get("pr_channel_requires_keywords") or not (kw or tags):
        return True  # a PR channel with no tag to look for is the user's own: every post in it is theirs to see
    return CFG.get("pr_channel_also_team_member_prs") and m.get("user") in TEAM and bool(site.pr_link_re().search(text))


def _channels() -> dict[str, str]:
    """The channels the install reads, each with its kind. An install may have no PR or alerts channel: an empty id is
    no channel (it was polled as one, and Slack answered channel_not_found on every pass)."""
    chans = {CFG["team_channel"]: "team", CFG["pr_channel"]: "pr", CFG["alerts_channel"]: "alerts"}
    for c in CFG.get("extra_alerts_channels", []):  # #the team-alerts
        chans[c] = "alerts"
    for c in CFG.get("watched_channels", []):
        chans.setdefault(c, "watched")  # a channel that is also the team's or the PR one keeps that kind
    return {c: k for c, k in chans.items() if c}


def channel_health(days: int = 7) -> int:
    """A polled channel with no messages for `days` while a channel with the same name is active is a rotted id
. Logs and files an
    intake_gap incident per channel. Cheap: one history call per polled channel, one search per quiet one."""
    from . import incidents
    since = time.time() - days * 86400
    gaps = 0
    for ch, kind in _channels().items():
        try:
            last = slack.latest_ts(ch)
        except Exception as e:
            log("channel health: history failed", ch=ch, err=str(e)[:120])
            continue
        if last and float(last) > since:
            continue
        name = slack.channel_name(ch)
        if not name.startswith("#"):
            continue
        try:
            ms = slack.search(f"in:{name} after:{time.strftime('%Y-%m-%d', time.localtime(since))}", count=5)
        except Exception as e:
            log("channel health: search failed", ch=ch, err=str(e)[:120])
            continue
        live = next(((m.get("channel") or {}).get("id") for m in ms if (m.get("channel") or {}).get("id") not in (None, ch)), None)
        if not live:
            continue
        when = time.strftime("%d/%m", time.localtime(float(last))) if last else site.t("never", "nunca")
        log("channel health: configured channel is dead, a same-named one is active", ch=ch, kind=kind, name=name, live=live, last=when)
        incidents.file("intake_gap", f"dead_channel:{ch}",
                       site.t(f"channel {name} ({kind}) is set up as {ch}, with no messages since {when}; the active one is {live}",
                              f"canal {name} ({kind}) configurado como {ch} sin mensajes desde {when}; "
                              f"el activo es {live}"), f"{ch} last message {when}; search in:{name} finds recent messages in {live}. "
                       f"Fix the channel id in v2 config.json.", impact=12)
        gaps += 1
    return gaps


def poll_channel(ch: str, kind: str) -> int:
    cur = _cursor(ch)
    try:
        msgs = slack.history(ch, cur)
    except Exception as e:
        log("ingest: history failed", ch=ch, err=str(e)[:200])
        return 0
    newest, n = cur, 0
    for m in msgs:
        newest = max(newest, m["ts"], key=float)
        if m.get("subtype") in ("channel_join", "channel_leave", "bot_add"):
            continue
        if kind == "pr" and not _pr_channel_relevant(m):
            continue
        if kind != "alerts" and not slack.is_human(m):
            continue
        n += _event_from_msg(ch, m, f"slack-{kind}", {"channel_kind": kind})
    db.kv_set(f"cursor:{ch}", newest)
    return n


def poll_dm(ch: str) -> int:
    cur = _cursor(f"dm:{ch}")
    msgs = slack.history(ch, cur, pages=2)
    newest, n = cur, 0
    for m in msgs:
        newest = max(newest, m["ts"], key=float)
        if slack.is_human(m):
            n += _event_from_msg(ch, m, "slack-dm", {"channel_kind": "dm"})
    db.kv_set(f"cursor:dm:{ch}", newest)
    return n


def _guard_socket_eof() -> None:
    """slack_sdk's builtin Socket Mode loop (3.43) treats an empty read as 'no messages yet' and reads again at once.
    Once Slack closes the connection every read is empty, so the loop spun at 97% of a core holding the GIL: the
    worker's other threads waited seconds for it, case pages took 10-60 s, and the scheduler's heartbeat went stale
    until the healthcheck killed the worker. An empty read now raises ECONNRESET, which the SDK already handles: it disconnects
    and its monitor reconnects to a new endpoint. Installed on import, so a hot reload fixes a loop already running
    (the SDK looks the function up on every pass)."""
    try:
        from slack_sdk.socket_mode.builtin import connection as _sc
    except Exception:
        return
    orig = getattr(_sc, "_ap_orig_receive", None) or _sc._receive_messages
    _sc._ap_orig_receive = orig

    def receive(*a, **k):
        msgs = orig(*a, **k)
        if not msgs:
            import errno
            time.sleep(0.5)  # never a hot loop, even if the SDK swallowed the error below
            raise ConnectionResetError(errno.ECONNRESET, "Slack closed the Socket Mode connection (empty read)")
        return msgs
    _sc._receive_messages = receive


_guard_socket_eof()


def socket_listener(stop) -> None:
    """v1's interrupt daemon, in-process: Slack Socket Mode pushes every message in the
    conversations the user is in; the matching channel/DM/thread is polled at once (the 60 s
    poll stays as the fallback). Needs the app token in the keychain; silently absent otherwise."""
    sl = CFG.get("slack") or {}
    if not sl.get("socket_mode", True):
        tok = ""
    elif site.internal():
        tok = slack.keychain(sl.get("app_keychain") or "clea-slack-app-token")
    else:
        from . import vault
        tok = vault.get("slack-app-token") or ""
    if not tok:
        return log("ingest: no Slack app token, socket mode off (60 s polling only)")
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
    except Exception as e:
        return log("ingest: socket mode unavailable", err=str(e)[:120])
    import threading
    pending: set[tuple[str, str]] = set()
    wake = threading.Event()

    def handle(client, req):
        if req.type != "events_api":
            return
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        ev = (req.payload or {}).get("event") or {}
        log("socket event", type=ev.get("type"), ch=ev.get("channel"), ct=ev.get("channel_type"), sub=ev.get("subtype"))
        if ev.get("type") == "message" and ev.get("channel"):
            pending.add((ev["channel"], ev.get("thread_ts") or ""))
            wake.set()

    client = SocketModeClient(app_token=tok, web_client=slack.client())
    client.socket_mode_request_listeners.append(handle)
    try:
        client.connect()
    except Exception as e:
        return log("ingest: socket mode connect failed", err=str(e)[:160])
    log("ingest: socket mode connected (instant wake)")
    chans = _channels()
    while not stop.is_set():
        wake.wait(30)
        wake.clear()
        if not pending:
            if not client.is_connected():
                try:
                    client.connect()
                except Exception:
                    pass
            continue
        time.sleep(2)  # let a burst land
        batch, pending_now = set(pending), None
        pending.clear()
        for ch, tts in batch:
            try:
                if ch in chans:
                    poll_channel(ch, chans[ch])
                elif ch.startswith("D") and ch != CFG["self_dm_channel"] and sl.get("scan_dms", True):
                    poll_dm(ch)
                if tts:
                    watch_thread(ch, tts, "0")
                    poll_threads()
            except Exception as e:
                log("ingest: socket poll failed", ch=ch, err=str(e)[:160])


_Q = re.compile(r"\?|¿|\b(pueden|podr[ií]an|hay forma|c[oó]mo|alguien|me ayudan|nos ayudan|sigue|saben|se puede|es posible)\b", re.I)


def unanswered_sweep(hours: int = 12, quiet_s: int = 2 * 3600) -> int:
    """Questions from people outside the squad in the ops/team channels that nobody answered for two work hours
    become REPLY cases with a v1-style opener."""
    chans = list(dict.fromkeys(CFG.get("watched_channels", []) + [c for c in (CFG.get("team_channel"), CFG.get("ops_channel")) if c]))
    n = 0
    for ch in chans:
        try:
            msgs = slack.history(ch, f"{time.time() - hours * 3600:.6f}", pages=3)
        except Exception as e:
            log("unanswered sweep: history failed", channel=ch, err=str(e)[:100])
            continue
        for m in msgs:
            if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
                continue
            text = slack.render(m.get("text") or "")
            if not slack.is_human(m) or m.get("user") == SELF or not _Q.search(text) or len(text) < 40:
                continue
            if time.time() - float(m["ts"]) < quiet_s:
                continue
            tk = f"slack:{ch}:{m['ts']}"
            if db.q1("SELECT 1 FROM items WHERE thread_key=? OR thread_key LIKE ?", tk, tk + ":%"):
                continue
            try:
                replies = [r for r in slack.replies(ch, m["ts"]) if r["ts"] != m["ts"] and slack.is_human(r) and r.get("user") != m.get("user")]
            except Exception:
                replies = []
            if replies:
                continue
            seen_key = f"sweep_judged:{ch}:{m['ts']}"
            if db.kv_get(seen_key):
                continue  # judged once, never again
            if n >= 3:
                break  # at most three judgments per run
            db.kv_set(seen_key, 1)
            try:  # one cheap Sonnet call, low effort: is this an unanswered question for the the team squad?
                from . import claude
                v = claude.lean("sweep", f"Channel {slack.channel_name(ch)}, {db.fmt_time(m['ts'])}, {slack.user_name(m.get('user'))} wrote (no reply "
                                f"from anyone else so far):\n{text[:1500]}", model="sonnet", cwd=site.root(),
                                schema={"type": "object", "required": ["ours", "why"], "properties": {"ours": {"type": "boolean"}, "why": {"type": "string"}}})
            except Exception as e:
                log("unanswered sweep: judge failed", err=str(e)[:100])
                continue
            if not v.get("ours"):
                continue
            who = slack.user_name(m.get("user"))
            topic = re.sub(r"\s+", " ", text)[:40].rstrip(" ,.:") + "…"
            at = db.fmt_time(m['ts'])[-5:]
            opener = site.t(f"{who} in {slack.channel_name(ch)} (thread {topic}): their question from {at} is still unanswered.",
                            f"{who} en {slack.channel_name(ch)} (hilo {topic}): su pregunta de las {at} sigue sin respuesta.")
            _event_from_msg(ch, {**m, "thread_ts": m["ts"]}, "slack-unanswered", {"channel_kind": "channel"})
            iid = db.create_item("REPLY", "reply_draft", site.t(f"(unanswered) {who}: {text[:70]}", f"(sin respuesta) {who}: {text[:70]}"), tk, urgency=2,
                                 data={"channel": ch, "ts": m["ts"], "thread_ts": m["ts"], "author": m.get("user"), "text": text[:4000],
                                       "opener": opener, "from": "unanswered_sweep"})
            log("unanswered sweep: case", item=iid, channel=ch, who=who)
            n += 1
    return n


def poll_channels() -> int:
    n = 0
    # DMs first: they are the most likely to be aimed at him and the cheapest to poll
    #
    try:
        for im in (slack.ims() if (CFG.get("slack") or {}).get("scan_dms", True) else []):
            ch = im["id"]
            if ch == CFG["self_dm_channel"] or im.get("is_user_deleted"):
                continue
            cur = _cursor(f"dm:{ch}")
            msgs = slack.history(ch, cur, pages=2)
            newest = cur
            for m in msgs:
                newest = max(newest, m["ts"], key=float)
                if slack.is_human(m):
                    n += _event_from_msg(ch, m, "slack-dm", {"channel_kind": "dm"})
            db.kv_set(f"cursor:dm:{ch}", newest)
    except Exception as e:
        log("ingest: dm poll failed", err=str(e)[:200])
    for ch, kind in _channels().items():
        n += poll_channel(ch, kind)
    return n


def poll_threads() -> int:
    threads = db.kv_get("threads", {})
    n, keep = 0, {}
    # threads of open cases first: their news can make running work redundant
    hot = {r["thread_key"].removeprefix("slack:") for r in db.q(f"SELECT thread_key FROM items WHERE status NOT IN {db.TERMINAL} AND thread_key LIKE 'slack:%'")}
    for k, v in sorted(threads.items(), key=lambda kv: kv[0] not in hot):
        if now() - v["touched"] > THREAD_TTL_S:
            continue
        ch, tts = k.split(":", 1)
        if _ignored(ch):
            continue  # dropped from the watch list too
        keep[k] = v
        try:
            msgs = slack.replies(ch, tts, oldest=v["seen"])
        except Exception as e:
            if "thread_not_found" in str(e) or "channel_not_found" in str(e):
                keep.pop(k, None)  # a phantom root (deleted message or a bad ts): stop polling it
                log("ingest: watched thread gone", thread=k)
            else:
                log("ingest: replies failed", thread=k, err=str(e)[:160])
            continue
        for m in msgs:
            if float(m["ts"]) <= float(v["seen"]) or m["ts"] == tts:
                continue
            v["seen"] = max(v["seen"], m["ts"], key=float)
            v["touched"] = now()
            if slack.is_human(m):
                n += _event_from_msg(ch, m, "slack-thread", {"channel_kind": "thread"})
    # Written back merged with what the list became meanwhile. This pass read the list at its start; reading a thread,
    # it finds the user's own reply and watch_thread marks the thread as one he takes part in (self_in). Saving the
    # stale copy erased that mark, so the next replies in his own thread were not his, went to triage and opened new
    # cases there (the mark only survived when the mention search happened to see his reply before this pass did).
    with _threads_lock:
        latest = db.kv_get("threads", {})
        for k, v in keep.items():
            cur = latest.get(k, {})
            v["self_in"], v["self_root"] = v.get("self_in") or cur.get("self_in"), v.get("self_root") or cur.get("self_root")
            v["seen"] = max(v["seen"], cur.get("seen", "0"), key=float)
            v["touched"] = max(v["touched"], cur.get("touched", 0))
        for k, v in latest.items():
            if k not in threads:  # a thread first watched during this pass
                keep[k] = v
        db.kv_set("threads", keep)
    return n


def _self_thread_channels() -> set[str]:
    """Where a thread he starts or replies in is followed: every polled channel but the PR channel (team, alerts,
    #sis-ops and the other watched ones)."""
    return {c for c, k in _channels().items() if k != "pr"}


def search_mentions() -> int:
    """Mentions of him / the squad / the team anywhere, plus his OWN messages in the followed channels: a thread
    he replies in is invisible to conversations.history (roots only), so search is how it gets followed
."""
    after = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
    sl = CFG.get("slack") or {}
    queries = ([f"<@{SELF}>"] if sl.get("mentions", True) else []) + site.keywords() + [
        f"<!subteam^{s}>" for s in CFG.get("team_subteam_tags", [])]
    # the self-DM is excluded in the query: the harness posts there as him and would crowd out the real messages
    queries.append(f"from:<@{SELF}> -in:<#{CFG['self_dm_channel']}>")
    follow = _self_thread_channels()
    cur = float(_cursor("search"))
    floor = cur - 300  # the search index lags: re-read 5 min behind the cursor; the event key dedupes
    newest, n, dms = cur, 0, set()
    for qy in queries:
        try:
            matches = slack.search(f"{qy} after:{after}", count=100 if qy.startswith("from:") else 50)
        except Exception as e:
            log("ingest: search failed", q=qy, err=str(e)[:160])
            continue
        for m in sorted(matches, key=lambda x: float(x["ts"])):  # oldest first: a promise before its follow-up
            ts = float(m["ts"])
            if ts <= floor:
                continue
            newest = max(newest, ts)
            ch = (m.get("channel") or {}).get("id")
            if not ch or ch == CFG["self_dm_channel"]:
                continue
            msg = {"ts": m["ts"], "user": m.get("user"), "text": m.get("text"), "attachments": m.get("attachments"),
                   "blocks": m.get("blocks"), "thread_ts": slack.thread_ts_from_permalink(m.get("permalink"))}
            extra = {"channel_kind": "mention", "channel_name": (m.get("channel") or {}).get("name")}
            if msg["user"] == SELF:
                if ch not in follow:
                    continue
                n += _event_from_msg(ch, msg, "slack-self", {**extra, "channel_kind": "self"})
                continue
            if ch.startswith("D") and not sl.get("scan_dms", True):
                continue  # DMs are not this install's to read: a keyword or a mention inside one does not change that
            if ch.startswith("D"):
                # a 1:1 DM is read by the DM poll, with its bot fields and keyed by the DM: the event key is channel+ts,
                # so a hit stored here first as a 'mention' (no tag, so not aimed) went to triage, which opened a case
                # keyed by the message beside the DM's own case (2026-10-06: a teammate's question became #583 at 18:02, 45
                # min after he answered, next to #577, and both asked him to send the same reply; a teammate's #437 by #435)
                if ch not in dms:
                    dms.add(ch)
                    try:
                        n += poll_dm(ch)
                    except Exception as e:
                        log("ingest: dm poll from search failed", ch=ch, err=str(e)[:160])
                continue
            n += _event_from_msg(ch, msg, "slack-mention", extra)
    db.kv_set("cursor:search", f"{newest:.6f}")
    return n


# ---- GitHub -----------------------------------------------------------------
def poll_github() -> dict:
    """Snapshot own open PRs (babysit input) and open REVIEW items for squad PRs."""
    stats = {"own": 0, "reviews": 0}
    me = CFG["self_github"]
    for repo in CFG["repos"]:
        try:
            own = gh.pr_list(repo, f"is:open author:{me}")
        except Exception as e:
            log("ingest: gh own failed", repo=repo, err=str(e)[:200])
            continue
        open_nums = set()
        for p in own:
            open_nums.add(p["number"])
            db.x("INSERT INTO prs(repo,number,data,head_sha,updated_at,own) VALUES(?,?,?,?,?,1) "
                 "ON CONFLICT(repo,number) DO UPDATE SET data=excluded.data, head_sha=excluded.head_sha, "
                 "updated_at=excluded.updated_at, own=1", repo, p["number"], json.dumps(p), p["headRefOid"], now())
            stats["own"] += 1
        for r in db.q("SELECT number FROM prs WHERE repo=? AND own=1", repo):
            if r["number"] not in open_nums:
                db.x("UPDATE prs SET own=2 WHERE repo=? AND number=?", repo, r["number"])  # closed/merged
        # A review request for the user is a REVIEW case whatever the author and even on a draft (the author
        # asked him by name, e.g. cometa#2351). Roster authors are covered too, but their drafts are not asks.
        handles = CFG.get("github_team_handles", [])
        searches = [(f"is:open review-requested:{me}", True)] + [(f"is:open author:{h}", False) for h in handles]
        seen = set()
        for s, requested in searches:
            try:
                found = gh.pr_list(repo, s, limit=30)
            except Exception as e:
                log("ingest: gh review search failed", repo=repo, search=s, err=str(e)[:200])
                continue
            for p in found:
                # a hold marker in the title is a draft too
                draft = p.get("isDraft") or merge_order.HOLD.search(p.get("title") or "")
                if p["number"] in seen or (draft and not requested):
                    continue
                seen.add(p["number"])
                stats["reviews"] += _maybe_review_item(repo, p, requested=requested)
    try:
        stats["reviews"] += pr_channel_reviews()
    except Exception as e:
        log("ingest: pr channel reviews failed", err=f"{type(e).__name__}: {e}"[:200])
    return stats


_PR_LINK = re.compile(r"github\.com/[\w.-]+/([\w.-]+)/pull/(\d+)")
PR_CHANNEL_EVERY = 600


def pr_channel_reviews(days: int = 14) -> int:
    """Every PR posted in the PR channel is a review case. Before, a review came only from a request to him by name or an
    author on the roster, so a teammate off the roster posting there (Nano's schools#1085, a teammate's cometa#2805) was
    never reviewed. A post in the channel asks the squad to review it, so it counts as a request. One channel read per
    10 min; a PR is looked up again at most every 30 min while open, never once merged or closed."""
    ch = CFG.get("pr_channel")
    if not ch or now() - float(db.kv_get("pr_channel_read_at") or 0) < PR_CHANNEL_EVERY:
        return 0
    db.kv_set("pr_channel_read_at", now())
    links = []
    for m in slack.history(ch, f"{now() - days * 86400:.6f}", pages=2):
        if m.get("user") == SELF or m.get("bot_id") or (m.get("thread_ts") and m.get("thread_ts") != m["ts"]):
            continue  # his own PRs are his to deliver; thread replies discuss a PR already posted
        for repo, n in _PR_LINK.findall(m.get("text") or ""):
            if repo in CFG["repos"] and (repo, int(n)) not in links:
                links.append((repo, int(n)))
    made = 0
    for repo, n in links:
        if db.open_item_for_thread(f"review:{repo}#{n}"):
            continue
        k = f"pr_channel_pr:{repo}#{n}"
        st = db.kv_get(k) or {}
        if st.get("state") in ("MERGED", "CLOSED") or now() - st.get("at", 0) < 1800:
            continue
        try:
            pr = gh.pr_view(repo, n)
        except Exception as e:
            log("ingest: pr channel lookup failed", pr=f"{repo}#{n}", err=str(e)[:160])
            continue
        db.kv_set(k, {"state": pr["state"], "at": now()})
        if pr["state"] == "OPEN":
            made += _maybe_review_item(repo, pr, requested=True)
    return made


def _maybe_review_item(repo: str, p: dict, requested: bool = False) -> int:
    key = f"review:{repo}#{p['number']}"
    if db.open_item_for_thread(key) or gh.bot_author(p.get("author")):
        return 0  # a bot PR is never reviewed: no case to drop again at each new head
    from .reviews import is_spec
    if is_spec(p.get("title") or ""):
        return 0  # a spec is never reviewed (reviews.SPEC_TITLE); one that says so only in its body drops at prepare
    last = db.q1("SELECT data FROM items WHERE thread_key=? ORDER BY id DESC LIMIT 1", key)
    if last and json.loads(last["data"]).get("head_sha") == p["headRefOid"]:
        return 0  # already reviewed this exact head
    age_days = (time.time() - time.mktime(time.strptime(p["updatedAt"][:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    if age_days > 14:
        return 0
    db.create_item("REVIEW", "review_prepare", f"review {repo}#{p['number']}: {p['title'][:80]}", key,
                   repo=repo, pr=p["number"], urgency=2,
                   data={"head_sha": p["headRefOid"], "author": (p.get("author") or {}).get("login"),
                         "url": p.get("url"), "review_requested": requested, "draft": bool(p.get("isDraft"))})
    log("ingest: review item", repo=repo, pr=p["number"])
    return 1


def rejudge(hours: int = 48) -> int:
    """Self-healing intake (v1's noop re-judge, automated). Events from the last `hours` that got no
    case are re-checked against the CURRENT rules: when detection improves, what was missed is recovered without anyone re-importing it.
    Only runs the deterministic aimed check here; everything aimed goes to the reply lane."""
    n = 0
    for ev in db.q("SELECT * FROM events WHERE triaged=1 AND item_id IS NULL AND created_at>? AND user!=? "
                   "AND source NOT IN ('slack-alerts')", now() - hours * 3600, SELF):
        payload = json.loads(ev["payload"] or "{}")
        extra = {"channel_kind": payload.get("channel_kind") or ("dm" if (ev["channel"] or "").startswith("D") else "")}
        msg = {"ts": ev["ts"], "user": ev["user"], "text": ev["text"], "thread_ts": ev["thread_ts"] or None}
        if not ev["user"] or ev["user"].startswith("B") or not _aimed(ev["channel"], msg, extra):
            continue
        from . import prefilter
        if prefilter.from_event(ev, SELF):
            continue  # prefiltered noise (ack, emoji, gif) stays dropped under every rule revision
        tk = f"slack:{ev['channel']}:dm" if extra["channel_kind"] == "dm" else f"slack:{ev['channel']}:{ev['thread_ts'] or ev['ts']}"
        if db.q1("SELECT 1 FROM items WHERE thread_key=? OR thread_key LIKE ?", tk, tk + ":%"):
            continue
        try:
            _fast_reply(ev["key"], ev["channel"], msg, extra)
            n += 1
            log("rejudge: recovered a missed aimed event", event=ev["id"], key=ev["key"])
        except Exception as e:
            log("rejudge failed", event=ev["id"], err=str(e)[:160])
    return n


REJUDGE_NOTE = "rejudge under new rules"
# what decides each kind of drop: its code, schema, prompts and learned rules. Any other commit asks the same question
# again and gets the same answer (2026-10-01 15:26: 9d680c5, a squad-review change, reopened 8 drops, $3.1, 7 re-dropped
# as before). One rev per verdict: 'reply' decides 'no reply needed', 'dossier' decides 'dossier: info' (2026-10-05:
# b297c66 changed only investigate's build rule and reopened four 'no reply needed' drops, all four re-dropped)
VERDICTS = {
    "reply": (("replies", "_decide"), ("replies", "REPLY_OPUS"),
              ("prompts/claude/reply.md", "prompts/claude/truth.md", "rules/claude-reply.md")),
    "dossier": (("tasks", "investigate"), ("tasks", "DOSSIER"),
                ("prompts/claude/truth.md", "opencode/agents/ap-investigate.md", "rules/investigate.md",
                 "rules/claude-agent-ap-investigate.md")),
    # decides a 'self_review still failing' block (worker.requeue_harness_bugs), not a drop
    "self_review": (("tasks", "self_review"), ("tasks", "SELF_VERIFY"),
                    ("prompts/claude/self_verify.md", "prompts/claude/truth.md", "rules/claude-self_verify.md")),
}


def verdict_rev(family: str) -> str:
    import hashlib, importlib, inspect
    from .core import HOME
    (mod, fn), (smod, schema), files = VERDICTS[family]
    h = hashlib.sha1()
    h.update(inspect.getsource(getattr(importlib.import_module(f".{mod}", __package__), fn)).encode())
    h.update(json.dumps(getattr(importlib.import_module(f".{smod}", __package__), schema), sort_keys=True).encode())
    for f in files:
        p = HOME / f
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()[:12]


def drops_rev_due() -> dict[str, str]:
    """The verdict revs to re-judge drops under, by family: only a family whose rev changed and whose last sweep is
    6 h old (re-running Opus replies on every deploy is waste). The first rev seen is the baseline, not a change."""
    due = {}
    for fam in VERDICTS:
        rev, seen = verdict_rev(fam), db.kv_get(f"rejudge_drops_rev:{fam}")
        if seen is None:
            db.kv_set(f"rejudge_drops_rev:{fam}", rev)
            continue
        if seen == rev:
            if _canary_overturned(fam, rev):
                due[fam] = rev  # a canary came out differently: the rest of the drops get their look
            continue
        if now() - db.kv_get(f"rejudge_drops_at:{fam}", 0) < 6 * 3600:
            continue
        db.kv_set(f"rejudge_drops_rev:{fam}", rev)
        db.kv_set(f"rejudge_drops_at:{fam}", now())
        due[fam] = rev
    return due


# drops asked again first under a new rev; the rest only when one of them comes out differently. Every rev change asked
# the whole recent backlog and none came out differently (10-06: 10 of 10 re-dropped, 10-07: 1 of 1, 10-15: 70f11d3,
# which only took Datadog out of reply.md and truth.md, reopened 7, all 7 re-dropped)
CANARY = 2


def _canary_cap(fam: str, rev: str, limit: int) -> int:
    c = db.kv_get(f"rejudge_canary:{fam}") or {}
    return limit if c.get("rev") == rev and c.get("full") else min(limit, CANARY)


def _canary_overturned(fam: str, rev: str) -> bool:
    """The canaries of `rev` are all decided again and one of them left 'dropped'. Decided once: the full sweep runs
    once per rev."""
    c = db.kv_get(f"rejudge_canary:{fam}") or {}
    if c.get("rev") != rev or c.get("decided") or not c.get("ids"):
        return False
    sts = [(db.item(i) or {}).get("status") or "dropped" for i in c["ids"]]
    if c["status"] in sts:
        return False  # still being re-decided
    over = any(s != "dropped" for s in sts)
    db.kv_set(f"rejudge_canary:{fam}", {**c, "decided": True, "full": over})
    log("rejudge: canaries decided", family=fam, statuses=sts, full_sweep=over)
    return over


def _canary_record(fam: str, rev: str | None, ids: list, status: str) -> None:
    c = db.kv_get(f"rejudge_canary:{fam}") or {}
    if rev and not (c.get("rev") == rev and c.get("full")):  # the full sweep keeps its decided canaries
        db.kv_set(f"rejudge_canary:{fam}", {"rev": rev, "ids": ids, "status": status})


def _rejudge_verdict(item_id: int) -> bool:
    """The case's current drop is the verdict of an earlier rejudge: the last time it left 'dropped' was a rejudge, so
    nothing new reached it since. Asking again under every later rev is the same question, and each verdict bumps
    updated_at, so the 48 h window never closed."""
    r = db.q1("SELECT note FROM transitions WHERE item_id=? AND src='dropped' ORDER BY at DESC, rowid DESC LIMIT 1", item_id)
    return bool(r and r["note"] == REJUDGE_NOTE)


def rejudge_drops(revs: dict[str, str], hours: int = 48, limit: int = 15) -> int:
    """After a code change, cases dropped under older rules get one more look with the new rules (e.g. open problems
    now become investigations), only for the verdict families in `revs` (drops_rev_due). Once per drop: a drop that
    came out of a rejudge is final until something new reopens the case. Under a new rev only CANARY drops per family
    reopen; drops_rev_due brings the family back for the rest when one of them came out differently."""
    n = 0
    rev = revs.get("reply")
    cap, taken = (_canary_cap("reply", rev, limit), []) if rev else (0, [])
    # one look per conversation: the newest case of a thread reads all of it, so an older drop of the same thread is
    # not asked again, and could not reopen next to a newer open case anyway (2026-10-02 15:32: #414, #423 and #425
    # were three drops of one thread, all queued for the rejudge)
    for r in db.q("SELECT id, data FROM items i WHERE kind='REPLY' AND status='dropped' AND updated_at>? "
                  "AND (thread_key IS NULL OR id=(SELECT MAX(id) FROM items j WHERE j.thread_key=i.thread_key)) "
                  "ORDER BY updated_at DESC",
                  now() - hours * 3600) if rev else ():
        d = json.loads(r["data"] or "{}")
        if d.get("rejudged_rev") == rev or d.get("idea_spawned") or not d.get("channel") or _rejudge_verdict(r["id"]):
            continue
        last = db.q1("SELECT note FROM transitions WHERE item_id=? ORDER BY at DESC, rowid DESC LIMIT 1", r["id"])
        if not last or not (last["note"] or "").startswith("no reply needed"):
            continue
        try:
            db.update_item(r["id"], status="reply_draft", attempts=0, next_run_at=0, note=REJUDGE_NOTE,
                           data={"rejudged_rev": rev})
        except Exception:  # another open case already owns this thread
            db.update_item(r["id"], data={"rejudged_rev": rev})
            continue
        n += 1
        taken.append(r["id"])
        if len(taken) >= cap:
            break
    _canary_record("reply", rev, taken, "reply_draft")
    # investigations dropped as out-of-fence under older rules: the dossier is kept, so re-deciding is cheap
    rev = revs.get("dossier")
    cap, taken = (_canary_cap("dossier", rev, limit), []) if rev else (0, [])
    for r in db.q("SELECT id, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status='dropped' AND updated_at>? "
                  "ORDER BY updated_at DESC", now() - hours * 3600) if rev else ():
        if len(taken) >= cap:
            break
        d = json.loads(r["data"] or "{}")
        last = db.q1("SELECT note FROM transitions WHERE item_id=? ORDER BY at DESC, rowid DESC LIMIT 1", r["id"])
        if (d.get("rejudged_rev") == rev or not d.get("dossier") or not last or not (last["note"] or "").startswith("dossier: info")
                or _rejudge_verdict(r["id"])):
            continue
        try:
            db.update_item(r["id"], status="investigate", attempts=0, next_run_at=0, note=REJUDGE_NOTE,
                           data={"rejudged_rev": rev, "reported_investigation": False})
            n += 1
            taken.append(r["id"])
        except Exception:
            db.update_item(r["id"], data={"rejudged_rev": rev})
    _canary_record("dossier", rev, taken, "investigate")
    # ideas spawned from a reply that skipped the vet under older rules: vet them now
    rev = revs.get("reply")
    for r in db.q("SELECT id, data FROM items WHERE kind='IDEA' AND status NOT IN ('done','dropped','expired','failed','vet') "
                  "AND json_extract(data,'$.from_reply') IS NOT NULL AND json_extract(data,'$.vetted') IS NULL") if rev else ():
        d = json.loads(r["data"] or "{}")
        if d.get("rejudged_rev") == rev or any(p.get("pr_url") for p in d.get("parts", [])):
            continue
        db.update_item(r["id"], status="vet", attempts=0, next_run_at=0, note="rejudge: vet under new rules",
                       data={"rejudged_rev": rev})
        n += 1
    if n:
        log("rejudge: reopened drops under new rules", n=n)
    return n
