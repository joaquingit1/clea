"""REPLY and OPEN_LOOP lanes.

REPLY: someone is waiting on the user. Muse (with read-only repo tools) works out the
facts and a draft; Opus checks facts and voice; the text is left as a composer draft in
the thread and shown on the board. What he actually sends becomes a style-learning pair.
OPEN_LOOP: an intention the user stated; if nothing executes it before it goes stale,
the harness drafts the exact message that does (v1 rule: "you should be automatically
raising these sorts of things").
"""
from __future__ import annotations

import json
import re


from . import approvals, claude, context, db, muse, outbound, site, slack
from .core import CFG, item_dir, log, now
from .tasks import READ_ROOT, _context

REPLY_SCHEMA = {"type": "object", "required": ["needs_reply", "why", "facts", "draft", "needs_facts", "checks",
                                               "prod_problem"], "properties": {
    "needs_reply": {"type": "boolean"}, "why": {"type": "string"},
    "facts": {"type": "array", "items": {"type": "string"}}, "draft": {"type": "string"},
    "needs_facts": {"type": "boolean"}, "checks": {"type": "array", "items": {"type": "string"}},
    "prod_problem": {"type": "boolean"}}}
TEXT = {"type": "object", "required": ["text", "send"], "properties": {
    "text": {"type": "string"}, "send": {"type": "boolean"}, "note": {"type": "string"}}}
SELF = CFG["self_user_id"]


REPLY_OPUS = {"type": "object", "required": ["needs_reply", "why", "draft", "needs_facts", "checks", "prod_problem",
                                            "someone_else_owns", "target", "report"], "properties": {
    "needs_reply": {"type": "boolean"}, "why": {"type": "string"}, "draft": {"type": "string"},
    "needs_facts": {"type": "boolean"}, "checks": {"type": "array", "items": {"type": "string"}},
    "prod_problem": {"type": "boolean"}, "someone_else_owns": {"type": "boolean"},
    "target": {"type": "string", "enum": ["thread", "team_channel"]}, "report": {"type": "string"},
    "ops_request": {"type": "boolean", "description": site.own(
        "a customer/ops request about a school's data or setup (bulk delete/move, data fix, per-school config, 'hay forma de...')",
        "a customer/ops request about a customer's data or setup (bulk delete/move, data fix, per-customer config, 'is there a way to...')")},
    "school": {"type": "string", "description": site.own("the school named, or empty", "the customer named, or empty")},
    "open_problem": {"type": "string", "description": f"ONLY when a participant of the thread raised a problem or asked something in the {site.of_team('fence')} and the thread does NOT resolve it (nobody answered, or the answer is " + site.own("'no sé' / 'qué camino ven viable'", "'not sure' / 'which way do you see working'") + "). Quote who raised it. Empty when the question was answered, and never for a concern YOU derive (a caveat, a risk nobody mentioned)."},
    "precedent_terms": {"type": "array", "items": {"type": "string"}, "description": site.own(
        "2-4 short Spanish search terms to find how the squad handled similar requests before, e.g. 'eliminar clases masivo', 'clases sin alumnos'",
        "2-4 short search terms, in the language the team writes in, to find how the team handled similar requests before, e.g. 'bulk delete orders', 'orders without items'")},
    "board_line": {"type": "string", "description": site.own(
        "one line for his pending board, max 15 words: 'Respuesta a <quién> sobre <qué>'",
        f"one line for the user's pending board in {site.language_name()}, max 15 words: 'Reply to <who> about <what>'")}}}


FOLLOWUP = {"type": "object", "required": ["needs_him", "why"], "properties": {
    "needs_him": {"type": "boolean"}, "why": {"type": "string"}}}


def _conversation(d: dict, since: float) -> list[dict]:
    """The conversation's messages after `since` (DM: channel history; else the thread)."""
    if d.get("dm") or not d.get("thread_ts"):
        return slack.history(d["channel"], f"{since:.6f}")
    return [m for m in slack.replies(d["channel"], d["thread_ts"]) if float(m["ts"]) > since]


def joaquin_said_after(d: dict) -> list[dict]:
    """His own messages in the conversation after the trigger (DM: channel history; else thread)."""
    last = float(d.get("last_ts") or d.get("ts", 0))
    try:
        msgs = _conversation(d, last)
    except Exception:
        return []
    return [m for m in msgs if m.get("user") == SELF and float(m["ts"]) > last]


def _joaquin_already_answered(d: dict) -> bool:
    """He answered after the trigger and after the verified facts reached the case: an answer written before the
    verification's verdict did not have it, so the redraft decides with both."""
    return any(float(m["ts"]) > (d.get("verified_at") or 0) for m in joaquin_said_after(d))


def _settled_by_him(it: dict) -> str | None:
    """He already answered in this case's conversation and only others spoke since: do their messages need him? One
    small Sonnet look, Opus drafts only when they do. Every message after his answer went straight to an Opus redraft,
    and all 6 such reopens (#93, #256, #355 twice, #368, #415, 09-24 to 10-02) ended 'no reply needed' over a thank-you,
    'eso! felizmente!' or a greeting. Returns why no reply is needed, or None to draft."""
    d = it["data"]
    if d.get("revision") or d.get("child") or d.get("dossier") or d.get("opener"):
        return None
    start = float(d.get("ts", 0))
    try:
        msgs = [m for m in _conversation(d, start - 1) if (m.get("text") or "").strip()]
    except Exception:
        return None
    his = [i for i, m in enumerate(msgs) if m.get("user") == SELF and float(m["ts"]) > start]
    if not his:
        return None  # he has not answered in this case: the question is still open
    convo = "\n".join(f"[{db.fmt_time(m['ts'])}] {site.first_name() if m.get('user') == SELF else slack.user_name(m.get('user'))}: "
                      f"{slack.render(m.get('text', ''))[:600]}" for m in msgs[max(0, his[-1] - 8):])
    try:
        r = claude.lean("followup", convo, model="sonnet", cwd=READ_ROOT, schema=FOLLOWUP, tools=(), item_id=it["id"],
                        timeout=180)
    except Exception as e:
        log("reply: follow-up gate failed, drafting", item=it["id"], err=f"{type(e).__name__}: {e}"[:160])
        return None
    return None if r["needs_him"] else (r.get("why") or site.t(f"nothing new for {site.first_name()}", "nada nuevo para él"))


def _moved_while_drafting(d: dict, since: float) -> str | None:
    """Someone wrote in the conversation after the draft read it: the Opus pass takes minutes, so the draft may answer a
    conversation that moved on. Returns who and when, or None."""
    try:
        msgs = _conversation(d, since)
    except Exception:
        return None
    new = [m for m in msgs if float(m["ts"]) > since and m.get("user") and slack.is_human(m)
           and not slack.user_is_bot(m.get("user"))]
    if not new:
        return None
    who = site.first_name() if new[-1].get("user") == SELF else slack.user_name(new[-1].get("user"))
    at = db.fmt_time(new[-1]['ts'])[-5:]
    return site.t(f"{who} wrote at {at} while I was drafting", f"{who} escribió a las {at} mientras redactaba")


def _spawn_open_problem(it: dict, r: dict, root_key: str) -> None:
    """A reply may be unnecessary (someone owns the thread) but the problem is still open: the work is to find the cause
    and a solution. Only a ROOT case spawns, once
    per thread, and
    never next to its verification: both are the same thread, so the dedupe folded the verification into the idea and
    its verdict lost the way back to the reply (#380, #398 and #411 on 10-01 and 10-02). A verification that already
    delivered is that investigation too: delivery clears `child` and sets `verified`, so the redraft spawned #472 next
    to #471's verdict on 10-05 (and #422 next to #420 on 10-02). So is a case's own dossier (a TASK whose
    investigation said 'ask')."""
    d = it["data"]
    if (not (r.get("open_problem") or "").strip() or d.get("idea_spawned") or root_key != it["thread_key"]
            or d.get("child") or d.get("verified") or d.get("dossier")
            or db.q1("SELECT 1 FROM items WHERE thread_key IN (?, ?)", f"{root_key}:idea", f"{root_key}:verify")):
        return
    iid = db.create_item("IDEA", "vet", r["open_problem"][:90], f"{it['thread_key']}:idea", urgency=2,
                         data={k: d[k] for k in ("channel", "ts", "thread_ts", "text", "author") if k in d}
                         | {"summary": r["open_problem"], "proposal": False, "from_reply": it["id"]})
    db.update_item(it["id"], data={"idea_spawned": iid})
    log("reply lane: open problem handed to an investigation", item=it["id"], idea=iid)


def _decide(it: dict, lead: str = "") -> dict:
    """The Opus pass that decides needs_reply and drafts. Only this, its schema and its prompt files make a 'no reply
    needed' verdict, so only they are ingest.verdict_rev('reply'): hashing all of reply_draft re-judged every drop on
    edits to what happens after the verdict."""
    d = it["data"]
    ctx = _context(it)
    (item_dir(it["id"]) / "context.md").write_text(ctx)
    dossier = d.get("dossier")
    prompt = ((f"{site.marker()} ASKED FOR CHANGES to the previous draft: {d['revision']}\nPrevious draft: "
               f"{json.dumps(d.get('revision_of'), ensure_ascii=False)[:3000]}\n\n" if d.get("revision") else "")
              + (f"VERIFIED INVESTIGATION (by Opus, with repo + org research): {json.dumps(dossier, ensure_ascii=False)[:6000]}\n"
                 f"The draft must reflect these facts; where the investigation says only a human knows something, ask for "
                 f"exactly that: {json.dumps(dossier.get('questions', []), ensure_ascii=False)}\n\n" if dossier else "")
              + f"{context.render(limit=4000)}\n\n{ctx}")
    if d.get("report_ts"):
        # the update replaces the report's Spanish first-line shape, so it says its language itself: as 'what changed
        # since', #792, #837 (twice) and #734 reached his DM as 'Since the first report:...'
        prompt = (f"REDRAFT: this case already has a report in {site.first_name()}'s DM. Write `report` as a short update only, in "
                  + site.own("rioplatense Spanish like the original (\"Novedad: a teammate respondió a las 10.12 que ...\")",
                             f"{site.language_name()} like the original (\"Update: a teammate answered at 10:12 that ...\")")
                  + ": who answered "
                  "what, what the verification found, why the draft changes or is dropped; max 80 words. Then the new "
                  "draft. Do not repeat the original report.\n\n" + prompt)
    from . import learn, precedents, voice_lint
    try:  # who reads the answer decides its shape: the audience template and his real messages to that audience
        from .tasks import _own_announcements
        aud = voice_lint.audience(d.get("channel"))
        examples = _own_announcements(6, aud) if aud in ("ops", "team", "pr") else []
        block = voice_lint.prompt_block(d.get("channel"), examples)
        if aud != "team":  # the draft may be retargeted to the team channel (someone_else_owns)
            block += f"\n<audience_if_team_channel>\n{voice_lint.template('team')}\n</audience_if_team_channel>"
        prompt = block + "\n\n" + prompt
    except Exception as e:
        log("reply: audience block failed", item=it["id"], err=str(e)[:160])
    kb = learn.knowledge_text()
    if kb:
        prompt = kb + "\n\n" + prompt
    pre = precedents.text_for(f"{d.get('text', '')} {d.get('summary', '')}")
    if pre:
        prompt = pre + "\n\n" + prompt
    if d.get("opener"):
        prompt = (f"THIS CASE WAS OPENED BY THE UNANSWERED-QUESTION SWEEP. The report's first line must be exactly: "
                  f"\"{d['opener']}\" Then: what they asked, what you found ({site.research_sources()}), what he should "
                  f"answer, with the draft.\n\n") + prompt
    return claude.lean("reply", lead + prompt, cwd=READ_ROOT, schema=REPLY_OPUS, tools=("Read", "Grep", "Glob"),
                       add_dirs=(str(item_dir(it["id"])),), item_id=it["id"], urgent=it["urgency"] >= 3)


def reply_draft(it: dict) -> None:
    """One Opus pass: decide, gather, draft, report. Runs in the quick pool so it never waits
    behind a build or the free-model queue (v1's reply lane ran on Opus for the same reason)."""
    d = it["data"]
    child = db.item(d["child"]) if d.get("child") else None
    if _not_ours(it, child):
        return _verified_by(it, child)
    if child and child["status"] not in db.TERMINAL and not d.get("verified") and now() - child["created_at"] < 3 * 3600:
        # the verification is still running: one ask with the facts beats a placeholder now and the facts later
        #
        return db.update_item(it["id"], next_run_in=120, note="waiting for the verification verdict")
    if _joaquin_already_answered(d):
        db.add_signal("reply_skipped", {"why": f"{site.first_name()} already answered"}, it["id"])
        return db.update_item(it["id"], status="done", note=site.t(f"{site.first_name()} already answered", "ya respondió él"))
    settled = _settled_by_him(it)
    if settled:
        db.add_signal("reply_skipped", {"why": settled, "after_his_answer": True}, it["id"])
        db.update_item(it["id"], status="done", note=site.t(
            f"{site.first_name()} already answered and nothing new asks anything of them: {settled[:180]}",
            f"ya respondió él, lo nuevo no le pide nada: {settled[:180]}"))
        from . import dm
        dm.close_case_thread(db.item(it["id"]), settled)
        return
    read_at = now()  # _decide reads the conversation now; what is posted after this, the draft has not seen
    r = _decide(it)
    from . import comms
    if site.lang() == "es" and comms.is_english(r.get("report") or ""):
        # his DM is Spanish: comms.check only logged it after it was posted. An install in another language has no
        # such check (in an English one every report is English, and each was drafted twice)
        log("reply: report in English, drafted once more", item=it["id"])
        r = _decide(it, "Your previous `report` came out in English. Write `report` in "
                    + site.own("rioplatense Spanish", site.language_name()) + ".\n\n")
    redraft = bool(d.get("report_ts"))
    root_key = re.sub(r"(:idea|:verify)+$", "", it["thread_key"])
    is_root = root_key == it["thread_key"]
    if not r["needs_reply"]:
        moved = _moved_while_drafting(d, read_at)
        if moved and d.get("moved_redrafts", 0) < 2:
            # the verdict is about a conversation that went on while it was being read (a "hi" and, a minute into the
            # pass, the question): closed here, the message that arrived was already on the case and nothing reread it
            db.update_item(it["id"], next_run_in=60, data={"moved_redrafts": d.get("moved_redrafts", 0) + 1},
                           note=f"{moved}: " + site.t("rereading with that", "lo releo con eso"))
            return
        _spawn_open_problem(it, r, root_key)
        db.add_signal("reply_skipped", {"why": r["why"]}, it["id"])
        _close_learning(it, r["why"])
        db.update_item(it["id"], status="dropped", note=f"no reply needed: {r['why'][:200]}")
        from . import dm
        dm.close_case_thread(db.item(it["id"]), r["why"])
        return
    moved = _moved_while_drafting(d, read_at)
    if moved and d.get("moved_redrafts", 0) < 2:
        # no ask on a conversation the draft never read: redraft once it settles. drafted_at below is read_at for the
        # same reason, so approvals._thread_moved sees what arrived during the pass (2026-10-07: #607 drafted 12:27-12:33
        # while a teammate answered a teammate, the 12:36 ask stood 36 min and the poll counted those messages as seen)
        db.update_item(it["id"], next_run_in=600, data={"moved_redrafts": d.get("moved_redrafts", 0) + 1},
                       note=f"{moved}: " + site.t("drafting it again with that", "lo rehago con eso"))
        return
    db.update_item(it["id"], data={"reply_text": r["draft"], "reply_opus": r, "drafted_at": read_at, "after_approval": "done",
                                   "gist": (r.get("board_line") or "")[:120] or None})
    from . import precedents
    ops = bool(r.get("ops_request"))
    if ops and not d.get("verified") and not precedents.search(f"{d.get('text', '')} {r.get('school', '')}"):
        # no stored precedent for this kind of request: the child hunts for one (Slack history, Jira,
        # our own store) before the answer claims "nunca lo hicimos" or invents a procedure
        r["needs_facts"] = True
        r["checks"] = (r.get("checks") or []) + [site.t("internal precedent: how the team handled similar requests before (Slack, tickets, scripts)",
                                                        "precedente interno: cómo resolvimos pedidos parecidos antes (Slack, tickets, scripts)")]
    db.update_item(it["id"], data={"ops_request": ops, "school": r.get("school") or d.get("school"),
                                   "precedent_terms": r.get("precedent_terms") or d.get("precedent_terms")})
    if (r.get("needs_facts") and r.get("checks") and not d.get("child") and not d.get("dossier") and is_root
            and not db.q1("SELECT 1 FROM items WHERE thread_key=?", f"{root_key}:verify")):
        # the draft only takes the case; the facts come from a telemetry-first investigation whose
        # verdict lands in this same case thread (v1: "un draft no resuelve nada, la verificación sí").
        # A case with a dossier was investigated: a TASK or INCIDENT whose own investigation said 'ask' spawned a
        # second one, which the dedupe folded into the case itself (#337, #421, #503, #589, #620 became their own
        # child, a redraft then waits on itself) and which before the dedupe ran twice (#331 to #336 on 10-01)
        child = db.create_item("INCIDENT" if r.get("prod_problem") else "TASK",
                               "telemetry" if r.get("prod_problem") else "investigate",
                               it["title"], f"{it['thread_key']}:verify", urgency=max(it["urgency"], 2),
                               data={k: d[k] for k in ("channel", "ts", "thread_ts", "text", "author", "summary") if k in d}
                               | {"parent": it["id"], "checks": r["checks"], "precedent_terms": r.get("precedent_terms") or [],
                                  "school": r.get("school") or ""})
        if _not_ours(it, db.item(child)):
            return _verified_by(db.item(it["id"]), db.item(child))
        db.update_item(it["id"], data={"child": child})
    else:  # only when no verification runs or ran on this thread (it is the investigation)
        _spawn_open_problem(it, r, root_key)
    if ((r.get("target") == "team_channel" or r.get("someone_else_owns") or not d.get("channel"))
            and d.get("channel") != CFG["team_channel"]):
        # the open take goes where the squad reads; a thread already in the team channel is that place, and a top-level
        # post would split it. A case
        # with no Slack conversation (a daily's action item, a patrol finding whose dossier said 'ask') has no thread
        # to answer in, so its question goes there too
        payload = {"channel": CFG["team_channel"], "thread_ts": None, "text": r["draft"]}
    else:
        payload = {"channel": d["channel"], "thread_ts": None if d.get("dm") else d.get("thread_ts"), "text": r["draft"]}
    from . import report
    child = db.item(db.item(it["id"])["data"].get("child") or 0)
    if (child and child["status"] not in db.TERMINAL and not d.get("verified") and not r.get("prod_problem")
            and now() - child["created_at"] < 3 * 3600):
        # a verification is running: no "lo tomo y mañana te paso" placeholder. Its verdict always redrafts this case
        # (tasks.investigate), so an ask now is superseded minutes later: 14 of 14 'redraft with the verified facts'
        # since 09-28 came from awaiting_approval, because checks naming Metabase, Datadog or prod were held to be
        # human-only while the child reads them too.
        # The report says what is being verified; the ask comes with the verified answer. A prod problem still asks
        # at once: the acknowledgement is worth sending before the diagnosis
        db.update_item(it["id"], data={"reply_text": None, "needs_you": False, "held_for_facts": True})
        report.post(db.item(it["id"]), "update" if redraft else "surfaced",
                    body=r["report"] + site.t("\n\nChecking: ", "\n\nVerificando: ") + "; ".join(c[:120] for c in r["checks"][:5])
                    + site.t(". The answer with what was checked comes in this thread.",
                             ". La respuesta con lo verificado llega en este hilo."))
        db.update_item(child["id"], urgency=3, data={"parent_waiting": True})
        return db.update_item(it["id"], status="reply_draft", next_run_in=120, note=site.t(
            "waiting for the verification: no placeholder draft", "esperando la verificación: sin borrador placeholder"))
    report.post(db.item(it["id"]), "update" if redraft else "surfaced", body=r["report"], sendable=payload,
                ask=("slack_send", payload, "reply_draft"))


def _not_ours(it: dict, child: dict | None) -> bool:
    """The verification create_item returned is an open case already on this thread (dedupe.same_work folded it), not a
    child of this reply: it never redrafts the reply, so holding for its verdict waits out the 3 h."""
    return bool(child and child["status"] not in db.TERMINAL and child["data"].get("parent") != it["id"]
                and not it["data"].get("verified"))


def _verified_by(it: dict, case: dict) -> None:
    from .dedupe import _verified_by_case
    log("reply: the verification is an open case of the thread", item=it["id"], case=case["id"])
    _verified_by_case(it, case)  # redraft with that case's dossier, as a fold by dedupe.semantic does


def _close_learning(it: dict, why: str) -> None:
    """A teammate (or the user) answered where we had a draft: post-mortem, knowledge, one closing line."""
    d = it["data"]
    if not d.get("reply_text") or not (d.get("channel") and d.get("thread_ts")) or d.get("dm"):
        return
    try:
        msgs = slack.replies(d["channel"], d["thread_ts"])
        theirs = [m for m in msgs if float(m["ts"]) > float(d.get("ts", 0)) and m.get("user") != d.get("author")
                  and slack.is_human(m) and (m.get("text") or "").strip()]
        if not theirs:
            return
        from . import learn, report
        thread_text = "\n".join(f"[{db.fmt_time(m['ts'])}] {slack.user_name(m.get('user'))}: {slack.render(m.get('text', ''))[:1200]}" for m in msgs)
        pm = learn.compare_answer(it, d["reply_text"], theirs, thread_text)
        if d.get("ops_request"):
            from . import precedents
            p = (pm or {}).get("precedent") or {}
            precedents.add(request=p.get("request") or d.get("gist") or d.get("summary") or d.get("text", "")[:300],
                           resolution=p.get("resolution") or slack.render(theirs[-1].get("text", ""))[:800],
                           kind=p.get("kind") or " ".join(d.get("precedent_terms") or [])[:80], school=d.get("school") or "",
                           who=slack.user_name(theirs[-1].get("user")), links=[slack.permalink(d["channel"], d["thread_ts"])],
                           source_item=it["id"])
        if pm and d.get("report_ts"):
            who = slack.user_name(theirs[-1].get("user"))
            verdict = {"theirs_better": site.t("their answer was better than my draft", "su respuesta fue mejor que mi borrador"),
                       "equal": site.t("same as my draft", "quedó igual que mi borrador"),
                       "ours_better": site.t("my draft had more, but it is answered", "mi borrador tenía más, pero ya está respondido")}[pm["verdict"]]
            # the closing line lives in the thread only while the thread lives; close_case_thread decides
            log("case closed by a teammate", item=it["id"], who=who, verdict=pm["verdict"], learned=len(pm.get("knowledge") or []))
    except Exception as e:
        log("close learning failed", item=it["id"], err=str(e)[:200])


def reply_check(it: dict) -> None:  # legacy stage name; same lane
    reply_draft(it)


def track_reply(it: dict) -> None:
    """Did the user answer? Pair what we drafted with what he sent (style signal)."""
    d = it["data"]
    try:
        msgs = slack.replies(d["channel"], d["thread_ts"], oldest=str(d.get("drafted_at", 0)))
    except Exception:
        return
    mine = [m for m in msgs if m.get("user") == SELF and float(m["ts"]) > d.get("drafted_at", 0)]
    if mine:
        sent = "\n".join(m.get("text", "") for m in mine)
        from . import sentlearn
        sentlearn.record_pair(it["id"], d.get("reply_text") or "", sent, d["channel"], sent_ts=mine[0]["ts"],
                              match=sentlearn.similarity(d.get("reply_text") or "", sent), via="track_reply")
        art = db.q1("SELECT id FROM artifacts WHERE item_id=? AND kind='slack_draft' ORDER BY id DESC", it["id"])
        if art:
            db.set_artifact_outcome(art["id"], "sent_as_is" if sent.strip() == (d.get("reply_text") or "").strip()
                                    else "edited", {"sent": sent})
        return db.update_item(it["id"], status="done", note=f"{site.first_name()} replied")
    if now() - d.get("drafted_at", now()) > 30 * 3600:
        db.add_signal("reply_ignored", {"draft": d.get("reply_text")}, it["id"])
        db.update_item(it["id"], status="expired", note=f"no reply from {site.first_name()} in 30h")


LOOP = {"type": "object", "required": ["action", "who", "stale_hours", "still_relevant"], "properties": {
    "action": {"type": "string"}, "who": {"type": "string"}, "stale_hours": {"type": "number"},
    "still_relevant": {"type": "boolean"}}}


def loop_track(it: dict) -> None:
    d = it["data"]
    if "loop" not in d:
        lp = muse.call("ap-triage", f"{site.first_name()} stated an intention in this message. Extract the concrete next action, "
                       "who it involves, and after how many working hours it is stale if nobody executed it.\n\n"
                       + _context(it), cwd=READ_ROOT, schema=LOOP, lane="loop", item_id=it["id"])
        return db.update_item(it["id"], data={"loop": lp, "decided_at": now()},
                              next_run_in=int(lp["stale_hours"] * 3600))
    lp = d["loop"]
    done = muse.call("ap-triage", f"Has this intention been executed or become moot? Intention: {lp['action']} "
                     f"(involves {lp['who']}). Judge from the conversation.\n\n" + _context(it),
                     cwd=READ_ROOT, schema={"type": "object", "required": ["executed", "moot"], "properties": {
                         "executed": {"type": "boolean"}, "moot": {"type": "boolean"}}}, lane="loop", item_id=it["id"])
    if done["executed"] or done["moot"]:
        return db.update_item(it["id"], status="done", note="loop closed")
    draft = muse.call("ap-draft", f"Draft the exact message {site.first_name()} would send to execute this pending intention: "
                      f"{lp['action']} (to {lp['who']}). Context:\n{_context(it)[:6000]}",
                      cwd=READ_ROOT, schema={"type": "object", "required": ["text", "where"], "properties": {
                          "text": {"type": "string"}, "where": {"type": "string"}}}, lane="draft-loop", item_id=it["id"])
    db.update_item(it["id"], status="awaiting_human", data={"loop_draft": draft, "needs_you": True, "drafted_at": now()})
    # the intended recipient is not always a channel we can address; the draft stays on the board to copy
