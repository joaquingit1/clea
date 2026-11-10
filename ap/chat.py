"""Orchestrator chat.
Anything he writes at TOP LEVEL in his self-DM, or in a thread under one of those messages, is a conversation with the
orchestrator: an agent with the harness's whole state in view. It answers in the thread within about a minute and
turns instructions into deterministic actions (create a case, drop, prioritize, requeue, pause, file an incident for
the supervisor, run the supervisor now). Replies inside a CASE thread keep their existing meaning (about that case)."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time

from . import db, site, slack
from .core import CFG, DATA, V1, VAR, log, now

import sys as _sys
import types as _types
# state that must survive hot reloads of this module (one loop, one lock, forever)
_S = _sys.modules.setdefault("ap._chat_state", _types.SimpleNamespace(lock=threading.Lock(), started=False))
_LOCK = _S.lock
_GUARD = site.path("guard", str(V1 / "guard"))  # the outbound guard's folder: only where one is installed
SUPERVISOR = _GUARD / "run_supervisor.sh" if _GUARD else None
SCHEMA = {"type": "object", "required": ["reply", "actions"], "properties": {
    "reply": {"type": "string", "description": "What he needs to know, in plain words for someone who was not watching: what happened, whether he has to do anything, and what happens next. 1-3 short sentences. Name things the way he would (the school, the PR as a link, the teammate), never internal case numbers, stage names or harness jargon (item, #319, auto-closed, awaiting_approval). When he asks for analysis, an investigation or an explanation, give it in full like an engineer's session answer: what you checked, what you found (numbers, times, links), the cause or the open unknowns, and what you recommend; bullets, as long as the findings need. Empty string if nothing needs saying."},
    "actions": {"type": "array", "items": {"type": "object", "required": ["type"], "properties": {
        "type": {"type": "string", "enum": ["create_case", "drop_case", "prioritize", "requeue", "go_ahead", "retry",
                                            "pause", "resume", "incident", "run_supervisor", "note"],
                 "description": "go_ahead: he gives the ok a case waiting on him asked for" + site.own(" ('dale', 'sí, hacelo')", "")
                                + ", with any extra direction in instructions; retry: a blocked case runs again; requeue only resets retries"},
        "item": {"type": "integer"}, "url": {"type": "string"}, "title": {"type": "string"},
        "instructions": {"type": "string", "description": "his directive for the case, verbatim plus what you inferred"},
        "text": {"type": "string"}, "impact": {"type": "integer"}}}}}}


def _state_summary() -> str:
    t = now()
    rows = db.q("SELECT id, kind, status, title, urgency, created_at, updated_at, data FROM items WHERE status NOT IN "
                "('done','dropped','expired','failed') ORDER BY updated_at DESC LIMIT 60")
    lines = []
    from . import stale
    cache: dict = {}
    for r in rows:
        d = json.loads(r["data"] or "{}")
        prs = ",".join(p["pr_url"].rsplit("/", 3)[-3] + "#" + p["pr_url"].rsplit("/", 1)[-1] for p in d.get("parts", []) if p.get("pr_url"))
        bucket, why = stale.what_it_needs(db.item(r["id"]), cache)  # verified against GitHub now, not the stored status
        if bucket == "done":
            continue
        lines.append(f"#{r['id']} {r['kind']} NEEDS={bucket.upper()} ({why}) u{r['urgency']} {r['title'][:80]} | age "
                     f"{round((t - r['created_at'])/3600, 1)}h | PRs {prs or '-'}")
    closed = db.q("SELECT id, kind, status, title FROM items WHERE status IN ('done','dropped') AND updated_at>? ORDER BY updated_at DESC LIMIT 15", t - 86400)
    pu = db.kv_get("claude_paused_until", 0) or 0
    inc = []
    try:
        inc = [f"{i['sig']} {i['kind']} {i['title'][:70]}" for i in json.loads((VAR / "incidents.json").read_text())["incidents"]
               if i.get("status") in ("open", "recurred")][:10]
    except Exception:
        pass
    return ("OPEN CASES:\n" + "\n".join(lines) + "\n\nCLOSED LAST 24H:\n" + "\n".join(f"#{c['id']} {c['kind']} {c['status']} {c['title'][:70]}" for c in closed)
            + f"\n\nCLAUDE: {'paused until ' + time.strftime('%H:%M', time.localtime(pu)) if pu > t else 'running'} | worker paused: {(VAR / 'PAUSED').exists()}"
            + "\nOPEN INCIDENTS:\n" + "\n".join(inc))


def _conversation(thread_ts: str) -> str:
    try:
        msgs = slack.replies(CFG["self_dm_channel"], thread_ts)
    except Exception:
        return ""
    me = CFG["self_user_id"]
    return "\n".join(f"{site.marker() if (m.get('client_msg_id') or (m.get('text') or '').startswith('[e2e]')) else 'ORCHESTRATOR'}: {slack.render(m.get('text') or '')[:1500]}" for m in msgs[-20:])


ASKED_IN_CHAT = site.t(f"{site.first_name()} asked for it in chat", f"{site.first_name()} lo pidió por chat")


def _act(a: dict) -> str:
    t = a["type"]
    if t == "create_case":
        m = re.search(r"archives/([A-Z0-9]+)/p(\d{10})(\d{6})", a.get("url") or "")
        data = {"instructions": a.get("instructions", ""), "from": "orchestrator", "taken_by_joaquin": True}
        key = f"orch:{now()}"
        if m:
            ch, ts = m.group(1), f"{m.group(2)}.{m.group(3)}"
            root = slack.resolve_root(ch, ts)
            msg = slack.fetch_message(ch, root) or {}
            data.update({"channel": ch, "ts": root, "thread_ts": root, "text": slack.render(msg.get("text") or "")[:4000], "author": msg.get("user")})
            key = f"slack:{ch}:{root}"
            existing = db.q1("SELECT id FROM items WHERE thread_key LIKE ? AND status NOT IN ('done','dropped','expired','failed')", key + "%")
            if existing:
                db.update_item(existing["id"], status="plan" if db.item(existing["id"])["kind"] != "REPLY" else "reply_draft",
                               attempts=0, next_run_at=0, urgency=3, note=ASKED_IN_CHAT, data={"instructions": a.get("instructions", "")})
                return f"case #{existing['id']} requeued with his instructions"
        iid = db.create_item("TASK", "investigate", a.get("title") or (a.get("instructions") or site.t(f"request from {site.first_name()}", f"pedido de {site.first_name()}"))[:90],
                             key, urgency=3, data=data)
        return f"case #{iid} created"
    if t in ("go_ahead", "retry") and a.get("item"):
        # 'dale' had nothing to consume it: requeue reset the retry counter and the case stayed in awaiting_human
        #. Same code path as the app's buttons
        from . import actions
        try:
            fn = actions.go_ahead if t == "go_ahead" else actions.retry
            return fn(int(a["item"]), a.get("instructions") or "", by=site.t(f"{site.first_name()} in chat", f"{site.first_name()} por chat"))
        except ValueError as e:
            return f"#{a['item']}: {e}"
    if t in ("drop_case", "prioritize", "requeue") and a.get("item"):
        it = db.item(int(a["item"]))
        if not it:
            return f"no case #{a['item']}"
        if t == "drop_case":
            db.update_item(it["id"], status="dropped", note=ASKED_IN_CHAT)
        elif t == "prioritize":
            db.update_item(it["id"], urgency=3, next_run_at=0, note=site.t("prioritized in chat", "prioridad por chat"))
        else:
            db.update_item(it["id"], attempts=0, next_run_at=0, note=site.t("requeued in chat", "reencolado por chat"),
                           data={"instructions": (it["data"].get("instructions") or "") + ("\n" + a["instructions"] if a.get("instructions") else "")})
        return f"#{it['id']} {t}"
    if t == "pause":
        (VAR / "PAUSED").write_text(f"paused by {site.first_name()} via chat")
        return "worker paused"
    if t == "resume":
        from . import hot
        hot.resume()
        return "worker resumed"
    if t == "incident":
        from . import incidents
        with incidents._Lock():
            p = incidents.OUT
            data = json.loads(p.read_text()) if p.exists() else {"incidents": [], "state": {}}
            sig = incidents._sig("joaquin_chat", a.get("text", "")[:200])
            data["incidents"].insert(0, {"sig": sig, "kind": "joaquin_correction",
                                         "title": f"{site.first_name()} (chat): {a.get('text', '')[:120]}",
                                         "impact": float(a.get("impact") or 15), "count": 1, "first": now(), "last": now(),
                                         "status": "open", "attempts": [], "evidence": [a.get("text", "")[:1500]]})
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        return f"incident {sig} filed for the supervisor"
    if t == "run_supervisor":
        if SUPERVISOR and SUPERVISOR.exists():
            subprocess.Popen(["/bin/zsh", str(SUPERVISOR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return "supervisor pass started"
        return "supervisor launcher not found"
    return "noted"


FAST = {"type": "object", "required": ["reply", "needs_deep"], "properties": {
    "reply": {"type": "string", "description": "a brief, professional reply to him: 1-2 short lines, complete sentences, no preamble"},
    "needs_deep": {"type": "boolean", "description": "true unless the reply above fully answers him"}}}
ACKS = site.t(("Understood. I'm looking into it.", "On it. Checking now.", "I'll check and report back shortly.", "Checking. I'll get back to you."),
              ("Entendido, lo reviso.", "Lo estoy revisando.", "Lo verifico y te aviso.", "Lo reviso y te confirmo."))
VOICE = (f"You answer {site.first_name()} on Slack as his engineering assistant: short, direct and professional (complete sentences, "
         "no slang, no jokes), in " + site.own("Rioplatense Spanish if he writes in Spanish", f"{site.language_name()}") + ", "
         "no preamble, no bullet lists unless asked, no em dashes, 1-3 lines. Facts only from what you are given.")


def _brief_state() -> str:
    """What the voice may know for an instant answer: a compact view of open cases (cheap, deterministic)."""
    rows = db.q("SELECT id, kind, status, title FROM items WHERE status NOT IN ('done','dropped','expired','failed') "
                "ORDER BY urgency DESC, updated_at DESC LIMIT 60")
    pu = db.kv_get("claude_paused_until", 0) or 0
    return ("\n".join(f"#{r['id']} {r['kind']} [{r['status']}] {r['title'][:70]}" for r in rows)
            + (f"\n(backend paused by usage limit until {time.strftime('%H:%M', time.localtime(pu))})" if pu > now() else ""))


def _fast(text: str, thread_ts: str) -> dict:
    """The voice only talks: an instant, natural
    acknowledgement that fits what he wrote. It never answers or decides; every message goes to the thinker."""
    from . import voice
    reply = voice.ack(f"{site.first_name()} just wrote: {text}\n\nWrite ONLY a short acknowledgement (one brief, professional sentence) "
                      f"that you got it and are checking. NEVER answer the question (no yes/no{site.own(", no 'nada', no 'listo'", "")}), never state "
                      f"facts, never promise specifics. "
                      f"If he is only thanking or closing the conversation, reply with a matching one-liner.")
    reply = reply.replace("[DEEP]", "").strip()
    if re.search(r"\b(no|nope|nada|s[ií]|listo|ya est[aá]|todo bien|tranqui)\b", reply.lower()) and not re.search(r"\bgracias|de nada|por nada", text.lower() + reply.lower()):
        reply = ACKS[int(time.time()) % len(ACKS)]  # an ack that sounds like an answer is a false answer
    return {"reply": reply, "needs_deep": True}


def _speak(deep_reply: str, done: list[str], text: str) -> str:
    """The voice delivers the thinker's answer: plain, short, professional, nothing he would have to decode."""
    from . import voice
    try:
        out = voice.say(f"{site.first_name()} asked: {text}\n\nThe answer to give him:\n{deep_reply[:6000]}\n\nRewrite it as a clear, "
                        "professional "
                        f"message for Slack: plain words, complete sentences, no slang. Keep EVERY finding, number, time, link and recommendation: you change the wording, never the content or its depth "
                        f"(an analysis stays an analysis). If it lists two or more things, use a one-line intro and short bullets "
                        f"(\"• \"), one item per bullet, most urgent first, each saying what it is and what he has to do"
                        + site.own(" (or 'nada')", " (or nothing)") + ". A plain answer stays 1-3 short lines. Keep links as Slack links with short labels "
                        f"(<url|{site.primary_repo() or 'repo'}#2383>) "
                        f"and names; drop internal numbers and jargon. No apologies, no filler openers{site.own(" ('perdón', 'bueno')", '')}. Output only the message.")
        return out.replace("[DEEP]", "").strip() or deep_reply
    except Exception:
        return deep_reply


def _handle(m: dict, thread_ts: str) -> None:
    from . import claude
    text = slack.render(m.get("text") or "").removeprefix("[e2e]").strip()
    try:
        t0 = time.time()
        box: dict = {}
        th = threading.Thread(target=lambda: box.update(q=_fast(text, thread_ts)), daemon=True)
        th.start()
        th.join(3.5)  # the promise: something back in under 4 s, always
        quick = box.get("q") or {"reply": ACKS[int(t0) % len(ACKS)], "needs_deep": True}
        slack.send_self(quick["reply"].strip(), thread_ts=thread_ts, topic="chat", guard=False)
        log("chat fast reply", secs=round(time.time() - t0, 1), deep=quick.get("needs_deep"))
        if not quick.get("needs_deep"):
            return
    except Exception as e:
        log("chat fast failed", err=str(e)[:120])
    gen = _S.__dict__.setdefault("gen", {})
    gen[thread_ts] = gen.get(thread_ts, 0) + 1
    my = gen[thread_ts]
    threading.Thread(target=_deep, args=(m, thread_ts, text, my), name="chat-deep", daemon=True).start()


def _latest(thread_ts: str, my: int) -> bool:
    return _S.__dict__.get("gen", {}).get(thread_ts, 0) == my


def _deep(m: dict, thread_ts: str, text: str, my: int = 0) -> None:
    """One answer per burst: if he writes again before this answer is ready, this one is dropped and the newer
    thinker answers the whole conversation."""
    locks = _S.__dict__.setdefault("thread_locks", {})
    lk = locks.setdefault(thread_ts, threading.Lock())
    with lk:  # one thinker at a time per conversation
        if my and not _latest(thread_ts, my):
            return
        try:
            _deep_inner(m, thread_ts, text, my)
        except Exception as e:
            from . import claude as _cl
            if isinstance(e, (_cl.ClaudeLimited, getattr(_cl, "BudgetExhausted", _cl.ClaudeLimited))):
                # a usage limit is not an error: say it once per conversation, answer when it lifts
                pend = db.kv_get("chat_pending") or {}
                first = thread_ts not in pend
                pend[thread_ts] = {"ts": m["ts"], "text": text}
                db.kv_set("chat_pending", pend)
                if first:
                    pu = db.kv_get("claude_paused_until", 0) or 0
                    back = time.strftime('%H:%M', time.localtime(pu))
                    when = site.t(f" (back at {back})", f" (vuelve {back})") if pu > now() else ""
                    try:
                        slack.send_self(site.t(f"I am out of quota to think it through{when}, I will answer as soon as it is back",
                                               f"ahora estoy sin cuota para pensarlo{when}, te contesto apenas vuelva"),
                                        thread_ts=thread_ts, topic="chat", guard=False)
                    except Exception:
                        pass
                log("chat: deferred until the limit lifts", ts=m["ts"])
                return
            log("chat deep failed", err=f"{type(e).__name__}: {e}"[:200])
            try:
                slack.send_self(site.t(f"I got stuck processing it ({type(e).__name__}); I will look into it and write you again",
                                       f"se me trabó al procesarlo ({type(e).__name__}); lo reviso y te vuelvo a escribir"),
                                thread_ts=thread_ts, topic="chat", guard=False)
            except Exception:
                pass


def _case_context(thread_ts: str) -> str:
    """When he writes under a case report, the thinker starts from that case, like his own sessions would."""
    r = db.q1("SELECT id, kind, status, title FROM items WHERE json_extract(data,'$.report_ts')=?", thread_ts)
    if not r:
        return ""
    return (f"THIS THREAD IS THE REPORT OF CASE #{r['id']} ({r['kind']}, {r['status']}): {r['title']}. Its folder is "
            f"var/items/{r['id']}/ and its row in items/transitions. If he asks for deeper analysis, do the analysis now "
            f"with your tools (logs, Datadog, code) and answer with the findings, the way an engineer would in a session; "
            f"if something blocks you (access, a tool), name exactly what is missing.\n\n")


def _deep_inner(m: dict, thread_ts: str, text: str, my: int = 0) -> None:
    from . import claude
    prompt = (f"{site.first_name()} wrote to you (the orchestrator of his autonomous engineering harness) in his Slack "
              f"self-DM:\n\n{text}\n\n"
              f"CONVERSATION SO FAR:\n{_conversation(thread_ts)}\n\n{_case_context(thread_ts)}HARNESS STATE NOW:\n{_state_summary()}\n\n"
              f"Verify with your tools before answering (DB, logs, gh, Slack): the summary above is a starting point, not the truth. Answer his question or confirm what you will do; turn instructions into "
              f"actions. Only cases marked NEEDS=YOU need him; NEEDS=REVIEW waits on teammates, NEEDS=PROGRESS is being worked: "
              f"never call those 'decisions'. Complaints or corrections about the harness itself become an 'incident' (the supervisor fixes the mechanism; "
              f"add 'run_supervisor' if it is urgent). Changing what needs his ✅ is not yours to do: tell him the one command. "
              f"Never promise what no action does.")
    sid = db.kv_get(f"chat_sid:{thread_ts}")
    try:
        out = claude.orchestrator(prompt, cwd=str(DATA), schema=SCHEMA, session=sid)
    except claude.ClaudeError:
        if not sid:
            raise
        out = claude.orchestrator(prompt, cwd=str(DATA), schema=SCHEMA)  # an expired session starts fresh
    if out.get("_session"):
        db.kv_set(f"chat_sid:{thread_ts}", out["_session"])
    done = []
    # a test conversation is read-only: its actions are reported, never executed (2026-10-01: a burst test message
    # "no hay nada pendiente?" made the thinker drop five real reminder asks)
    test = "[e2e]" in _conversation(thread_ts)
    for a in out.get("actions", [])[:6]:
        if test:
            log("chat e2e: action not executed", action=a.get("type"), item=a.get("item"))
            continue
        try:
            done.append(_act(a))
        except Exception as e:
            done.append(f"{a.get('type')} failed: {str(e)[:120]}")
    if my and not _latest(thread_ts, my):
        log("chat: newer message arrived, answer dropped", ts=m["ts"])
        return
    if not out["reply"].strip() and not [d for d in done if d != "noted"]:
        log("chat: thinker had nothing to add", ts=m["ts"])
        return
    spoken = _speak(out["reply"].strip(), [d for d in done if d != "noted"], text)
    if my and not _latest(thread_ts, my):  # he wrote again while the voice was phrasing it: the newer thinker answers
        log("chat: newer message arrived, answer dropped", ts=m["ts"])
        return
    slack.send_self(spoken, thread_ts=thread_ts, topic="chat", guard=False)
    log("chat answered", ts=m["ts"], thread=thread_ts, actions=done)


def ensure_loop() -> None:
    """A dedicated poller every 2 s (called from hot.tick; idempotent across hot reloads). Each iteration calls the
    NEWEST poll(), so reloaded code takes effect without a second loop."""
    if _S.started:
        return
    _S.started = True

    def warm():
        from . import voice
        while True:
            try:
                voice.keep_warm()
            except Exception as e:
                log("voice warm failed", err=str(e)[:120])
            time.sleep(300)
    threading.Thread(target=warm, name="voice-warm", daemon=True).start()

    def loop():
        while True:
            try:
                _sys.modules["ap.chat"].poll()
            except Exception as e:
                log("chat loop error", err=str(e)[:120])
            time.sleep(1)  # plus the ~1 s the calls take: about one check every 3 s
    threading.Thread(target=loop, name="chat-loop", daemon=True).start()


def poll() -> int:
    """One check (the running loop sleeps between iterations; see ensure_loop)."""
    return _poll_once()


def _poll_once() -> int:
    """Top-level messages by him are handled once each (his replies in older threads: _scan_threads)."""
    if not _S.lock.acquire(blocking=False):
        if now() - getattr(_S, "lock_at", now()) > 20:
            log("chat: poll hung over 20 s, replacing its lock", held=round(now() - _S.lock_at))
            _S.lock = threading.Lock()
        return 0
    _S.lock_at = now()
    lock = _S.lock
    try:
        if now() - getattr(_S, "warmed_at", 0) > 300:  # keep both voice sessions warm (works in an already-running loop)
            _S.warmed_at = now()
            def _w():
                try:
                    from . import voice
                    voice.keep_warm()
                except Exception as e:
                    log("voice warm failed", err=str(e)[:120])
            threading.Thread(target=_w, name="voice-warm", daemon=True).start()
        ch, me = CFG["self_dm_channel"], CFG["self_user_id"]
        cursor = db.kv_get("chat_cursor") or f"{now() - 600:.6f}"
        n = 0
        # one call per cycle: the active conversation's root rides along (its latest_reply says whether he answered
        # there), so replies are fetched only when something changed
        hot = _hot_root()
        oldest = min(cursor, f"{float(hot) - 0.000001:.6f}", key=float) if hot else cursor
        msgs = slack.history(ch, oldest, pages=1)
        newest = cursor
        hot_changed = False
        for m in msgs:
            if hot and m["ts"] == hot:
                hot_changed = float(m.get("latest_reply") or 0) > float(db.kv_get(f"chat_seen:{hot}") or hot)
            if float(m["ts"]) <= float(cursor):
                continue
            newest = max(newest, m["ts"], key=float)
            if m.get("user") != me or not (m.get("client_msg_id") or (m.get("text") or "").startswith("[e2e]")):
                continue
            if not m.get("thread_ts") or m["thread_ts"] == m["ts"]:
                log("chat detected", lag=round(time.time() - float(m["ts"]), 1))
                threading.Thread(target=_run, args=(m, m["ts"]), name="chat-msg", daemon=True).start()
                n += 1
        db.kv_set("chat_cursor", newest)
        pend = db.kv_get("chat_pending") or {}
        if pend and (db.kv_get("claude_paused_until", 0) or 0) <= now():
            db.kv_set("chat_pending", {})
            for th, p in pend.items():
                gen = _S.__dict__.setdefault("gen", {})
                gen[th] = gen.get(th, 0) + 1
                threading.Thread(target=_deep, args=({"ts": p["ts"]}, th, p["text"], gen[th]), name="chat-deep", daemon=True).start()
        if hot_changed or (hot and not any(m["ts"] == hot for m in msgs)):
            _hot_threads()
        if now() - getattr(_S, "thread_scan_at", 0) >= 15 and not getattr(_S, "scanning", False):
            _S.thread_scan_at, _S.scanning = now(), True
            threading.Thread(target=_scan_threads, name="chat-scan", daemon=True).start()
        return n
    finally:
        lock.release()


def _is_his(m: dict, me: str) -> bool:
    return m.get("user") == me and bool(m.get("client_msg_id") or (m.get("text") or "").startswith("[e2e]"))


def _hot_root() -> str | None:
    """The conversation active in the last 5 min, if any."""
    roots = [r for r in (db.kv_get("chat_threads") or [])[-3:]
             if now() - float(db.kv_get(f"chat_seen:{r}") or r) < 300 or now() - float(r) < 300]
    return roots[-1] if roots else None


def _hot_threads() -> None:
    """His new replies in the active conversation (called when its root says it has a newer reply)."""
    ch, me = CFG["self_dm_channel"], CFG["self_user_id"]
    for root in [r for r in [_hot_root()] if r]:
        seen = db.kv_get(f"chat_seen:{root}") or root
        try:
            for r in slack.replies(ch, root, oldest=seen):
                if r["ts"] != root and _is_his(r, me) and float(r["ts"]) > float(seen):
                    db.kv_set(f"chat_seen:{root}", r["ts"])
                    threading.Thread(target=_run, args=(r, root), name="chat-msg", daemon=True).start()
        except Exception:
            continue


def _scan_threads() -> None:
    """His replies in older conversations, scanned off the hot path so a new message is never delayed by it."""
    ch, me = CFG["self_dm_channel"], CFG["self_user_id"]
    try:
        # replies in orchestrator conversations (threads rooted at his own top-level messages, active in the last 2 h)
        # one history call tells which conversations have new replies (latest_reply); only those are read
        roots = set((db.kv_get("chat_threads") or [])[-50:])
        # and every other thread in his DM where he writes and no ask is waiting: a case report, a digest, a notice.
        # Before, those replies reached nobody (2026-10-01: "esto potencialmente puede ser un problema... te falta
        # acceso?" under a Datadog case report got no answer, twice). Threads with a pending ask stay with approvals.
        asks = {x for r in db.q("SELECT ref, json_extract(content,'$.thread_ts') AS th FROM artifacts "
                                "WHERE kind='approval' AND outcome IS NULL") for x in (r["ref"], r["th"]) if x}
        floor = db.kv_get("chat_any_thread_floor")
        if floor is None:  # first run: only replies from the last hour, never a backlog of old threads
            floor = now() - 3600
            db.kv_set("chat_any_thread_floor", floor)
        active = []
        for p in slack.history(ch, f"{now() - 3 * 86400:.6f}", pages=2):
            if not p.get("latest_reply"):
                continue
            mine = p["ts"] in roots
            if not mine and (p["ts"] in asks or float(p["latest_reply"]) < floor):
                continue
            if float(p["latest_reply"]) > float(db.kv_get(f"chat_seen:{p['ts']}") or (p["ts"] if mine else floor)):
                if slack.is_own_post(p["latest_reply"]):  # the newest reply is the harness's own post: nothing to read
                    db.kv_set(f"chat_seen:{p['ts']}", p["latest_reply"])
                    continue
                active.append(p["ts"])
        for root in active:
            try:
                base = db.kv_get(f"chat_seen:{root}") or (root if root in roots else f"{floor:.6f}")
                for r in slack.replies(ch, root, oldest=base):
                    if r["ts"] != root and _is_his(r, me) and float(r["ts"]) > float(db.kv_get(f"chat_seen:{root}") or base):
                        db.kv_set(f"chat_seen:{root}", r["ts"])
                        threading.Thread(target=_run, args=(r, root), name="chat-msg", daemon=True).start()
            except Exception:
                continue
    finally:
        _S.scanning = False


def _run(m: dict, thread_ts: str) -> None:
    # atomic claim: a message is answered exactly once even if two pollers race (e.g. across a hot reload)
    cur = db.conn().execute("INSERT OR IGNORE INTO kv(k, v) VALUES(?, ?)", (f"chat_done:{m['ts']}", "1"))
    if cur.rowcount == 0:
        return
    threads = db.kv_get("chat_threads") or []
    if thread_ts not in threads:
        db.kv_set("chat_threads", (threads + [thread_ts])[-50:])
    db.kv_set(f"chat_seen:{thread_ts}", max(m["ts"], db.kv_get(f"chat_seen:{thread_ts}") or "0", key=float))
    try:
        _handle(m, thread_ts)
    except Exception as e:
        log("chat failed", err=str(e)[:200])
        try:
            slack.send_self(site.t(f"I could not process this ({str(e)[:120]}). I will try again in a minute.",
                                   f"No pude procesar esto ({str(e)[:120]}). Lo reintento en un minuto."), thread_ts=thread_ts, topic="chat", guard=False)
        except Exception:
            pass
        db.x("DELETE FROM kv WHERE k=?", f"chat_done:{m['ts']}")
