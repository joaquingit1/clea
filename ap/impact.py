"""Thread news reaches the case worker within seconds.

The miss that motivated it: Renacimiento CDMX. At 14:59 a teammate wrote in the case's own thread that the code had been
fixed weeks ago, at 17:38 that his fix was going up, at 20:42 that it was live. Every message was ingested within a
minute, but triage never attached them to the open case, so the case sat on the user's ✅ for a redundant PR and the
next morning asked to nudge reviewers on it.

1. link(): deterministic, at ingest. A human message in the thread of an open case is that case's news: the event is
   attached, the case's `updates` (read by every stage through tasks._later_messages) get it, triage skips it.
2. judge(): Sonnet, no tools, a few cents. Does it make the work redundant, change it, block it, or is it just info?
3. worker(): only when it matters. A running stage is interrupted (its session resumes with the news, see
   claude.news_for); a parked case gets its worker (Opus, read-only tools) to decide continue / adjust / stop.
   Stop closes our own open PRs with a comment naming why, retires the pending asks and drops the case.
the user hears about it once, in the case thread, with the decision.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types

from . import db, site, slack
from . import comms as _comms
from .core import CFG, log, now

PARKED_STOP_OK = ("done", "blocked", "awaiting_approval", "awaiting_human", "waiting", "ci_wait", "announce", "announce_check",
                  "verify_done", "propose", "propose_check", "review_find", "comments_triage", "plan", "critique",
                  "pregate", "investigate")

JUDGE = {"type": "object", "required": ["impact", "why"], "properties": {
    "impact": {"type": "string", "enum": ["none", "info", "changes_task", "redundant", "blocker"],
               "description": "redundant: someone else fixed/merged/shipped it, or it turns out not to be a bug; "
                              "changes_task: new scope, a different cause, an objection to our approach, a new repro; "
                              "blocker: we must wait or must not proceed; info: useful context only; none: ack/thanks/chatter"},
    "why": {"type": "string", "description": f"one sentence in {site.own('Spanish', site.language_name())}, with the concrete fact (who, what PR, what changed)"}}}

DECIDE = {"type": "object", "required": ["decision", "reason"], "properties": {
    "decision": {"type": "string", "enum": ["continue", "adjust", "stop"]},
    "reason": {"type": "string", "description": f"{site.own('Spanish', f'In {site.language_name()}')}, 1-2 sentences, with the evidence you checked (PR state, diff)"},
    "instructions": {"type": "string", "description": "for adjust: what the work must change"},
    "pr_comment": {"type": "string", "description": f"for stop: the short comment to leave when closing our PR ({site.own('Spanish', f'in {site.language_name()}')}, names the PR that solves it)"}}}


def _case_for(ch: str, thread_ts: str) -> dict | None:
    it = db.open_item_for_thread(f"slack:{ch}:{thread_ts}")
    if it:
        return it
    # cases that absorbed this thread as an extra source
    for r in db.q(f"SELECT id FROM items WHERE status NOT IN {db.TERMINAL} AND data LIKE ?", f"%{thread_ts}%"):
        cand = db.item(r["id"])
        d = cand["data"]
        if (d.get("channel") == ch and (d.get("thread_ts") or d.get("ts")) == thread_ts) or any(
                a.get("channel") == ch and a.get("ts") == thread_ts for a in d.get("also_sources", [])):
            return cand
    return None


def link(ev_key: str, ch: str, m: dict, aimed: bool = False) -> bool:
    """Called by ingest for every new human message. True when it belonged to an open case (triage then skips it).
    aimed: the message is for the user, so the reply lane takes it next. When the case is that conversation's reply
    case the lane rereads the whole thread for it anyway, and the judge here was a second model pass over the same
    message (#1066, 10-21: two 'noticia en el hilo' judgments beside the redrafts of the same messages)."""
    tts = m.get("thread_ts")
    if not tts or tts == m["ts"] or m.get("user") == CFG["self_user_id"]:
        return False
    it = _case_for(ch, tts)
    if not it or it["kind"] in ("NUDGE", "CI"):
        return False
    ev = db.q1("SELECT id FROM events WHERE key=?", ev_key)
    if not ev:
        return False
    text = slack.render(slack.message_text(m))[:1500]
    ups = it["data"].get("updates", []) + [{"event_id": ev["id"], "text": text, "user": m.get("user"), "at": now()}]
    db.update_item(it["id"], data={"updates": ups[-20:]})
    db.x("UPDATE events SET item_id=?, triaged=1 WHERE id=?", it["id"], ev["id"])
    log("impact: thread news linked to its case", item=it["id"], ev=ev["id"])
    from . import prefilter
    if prefilter.from_message(m, CFG["self_user_id"]):
        return True  # an ack, emoji or gif: on the case, no judge
    if aimed and it["kind"] == "REPLY":
        return True  # the reply lane redrafts over it: one pass per message
    _enqueue(it["id"], (ev["id"], m.get("user"), text, ch, tts, m["ts"]))
    return True


# One judge per case at a time. Each message used to get its own parallel judge of the same case, though every judge
# reads the whole recent thread: 3 to 5 Sonnet calls per burst (#376 x5, #383 x3, #414 x3 twice on 10-01/02), all
# slow together, and a judge that timed out lost its news for good (9 'impact: timeout' since 10-01).
_Q = sys.modules.setdefault("ap._impact_queue", types.SimpleNamespace(lock=threading.Lock(), pending={}))  # survives hot reloads
_pending: dict[int, list[tuple]] = _Q.pending
_lock = _Q.lock
SETTLE_S = 2  # one ingest pass links a burst within the same second
THREAD_SETTLE_S = 600  # quiet in the thread before a draft to send is redone over it


def _enqueue(item_id: int, msg: tuple) -> None:
    with _lock:
        q = _pending.setdefault(item_id, [])
        q.append(msg)
        if len(q) > 1:
            return  # this case's judge is running: it takes the message in its next call
    threading.Thread(target=_drain, args=(item_id,), name="impact", daemon=True).start()


def _drain(item_id: int) -> None:
    time.sleep(SETTLE_S)
    failed = 0
    while True:
        with _lock:
            batch = list(_pending.get(item_id) or [])
            if not batch:
                _pending.pop(item_id, None)
                return
        try:
            ev_id, user, text, ch, tts = batch[-1][:5]
            before = batch[0][5] if len(batch[0]) > 5 else None
            if len(batch) > 1:
                user = ", ".join(dict.fromkeys(_who(b[1]) for b in batch))
                text = "\n".join(f"{_who(b[1])}: {b[2]}" for b in batch)
            judge(item_id, ev_id, user, text, ch, tts, before=before)
        except Exception as e:
            failed += 1
            log("impact failed", item=item_id, msgs=len(batch), retry=failed < 2, err=f"{type(e).__name__}: {e}"[:200])
            if failed < 2:
                continue  # once more, with whatever arrived meanwhile; the news is on the case's updates either way
            _unjudged(item_id, user, text, ch, tts, e, before=before)
        failed = 0
        with _lock:
            del _pending[item_id][:len(batch)]


def _who(user: str | None) -> str:
    return (slack.user_name(user) if user and user.startswith("U") else user) or site.t("someone", "alguien")


def _safe(item_id, ev_id, user, text, ch, tts, report=False, tries=0, before=None) -> None:
    try:
        judge(item_id, ev_id, user, text, ch, tts, report, before=before)
    except Exception as e:
        log("impact failed", item=item_id, err=f"{type(e).__name__}: {e}"[:200])
        _unjudged(item_id, user, text, ch, tts, e, report, tries + 1, before=before)


UNJUDGED_TRIES = 3


def _unjudged(item_id, user, text, ch, tts, err, report=False, tries=1, before=None) -> None:
    """News whose judge could not run (usage limit, budget, timeout) stays on the case as 'unjudged': an interrupted
    session reads it when it resumes (claude.news_for), fresh stages read news from outside the thread
    (tasks._later_messages) and retry_unjudged judges it once the model is back. It used to be logged and lost:
    on 10-06 at 10:38 pr_watch found a teammate's schools#995 (TICKET-1415) for #539 inside the 09:04-13:00 usage limit, the
    judge raised BudgetExhausted, and the build resumed at 13:03 spent 3 more hours and about $10 on the same fix."""
    it = db.item(item_id)
    if not it or it["status"] in db.TERMINAL:
        return
    inbox = it["data"].get("inbox", []) + [{
        "at": now(), "who": _who(user), "user": user, "text": text[:800], "impact": "unjudged", "thread": bool(tts),
        "why": site.t(f"not judged ({type(err).__name__})", f"sin juzgar ({type(err).__name__})"), "ch": ch, "tts": tts, "report": report, "tries": tries, "before": before}]
    db.update_item(item_id, data={"inbox": inbox[-10:]})


def retry_unjudged() -> int:
    """Judge the news a failed judge left on open cases, once the model can be called again (a few tries each)."""
    from . import claude
    if not claude.can_spend("impact", True):
        return 0
    n = 0
    for r in db.q(f"SELECT id FROM items WHERE status NOT IN {db.TERMINAL} AND data LIKE '%\"unjudged\"%'"):
        inbox = db.item(r["id"])["data"].get("inbox", [])
        todo = [x for x in inbox if x.get("impact") == "unjudged" and x.get("tries", 0) < UNJUDGED_TRIES]
        if not todo:
            continue
        db.update_item(r["id"], data={"inbox": [x for x in inbox if x not in todo]})
        for x in todo:
            _safe(r["id"], 0, x.get("user") or x.get("who"), x["text"], x.get("ch"), x.get("tts"), x.get("report", False),
                  x.get("tries", 0), before=x.get("before"))
            n += 1
    return n


def _prs(it: dict) -> list[dict]:
    return [p for p in it["data"].get("parts", []) if p.get("pr") and p.get("repo")]


def judge(item_id: int, ev_id: int, user: str, text: str, ch: str, tts: str, report: bool = False,
          before: str | None = None) -> str:
    """before: the ts of the first message being judged. The thread read after it arrived already holds it, so the
    context stops there: with the news inside RECENT THREAD the judge read it as a repeat of itself and called it 'none'
    (5 of 46 verdicts on 10-13/14, 3 of them #789's: a teammate's 'no nos afecta' answer to the question the case was
    drafting, 'idéntico palabra por palabra a los últimos 4 mensajes'), so the answer never reached the case."""
    from . import claude
    it = db.item(item_id)
    d = it["data"]
    who = _who(user)
    recent = ""
    if ch and tts:
        try:
            prior = [x for x in slack.replies(ch, tts) if not before or float(x["ts"]) < float(before)]
            recent = "\n".join(f"{slack.user_name(x.get('user'))}: {slack.render(x.get('text') or '')[:300]}"
                               for x in prior[-8:])
        except Exception:
            pass
    prompt = (f"A case my engineering harness is working on:\nTITLE: {it['title']}\nSTATUS: {it['status']}\n"
              f"PROBLEM: {((d.get('dossier') or {}).get('problem') or d.get('summary') or d.get('text') or '')[:1200]}\n"
              f"OUR PRs: {', '.join(p.get('pr_url') or '' for p in _prs(it)) or 'none yet'}\n\n"
              f"RECENT THREAD (before the new message):\n{recent}\n\nNEW MESSAGE from {who}:\n{text}\n\n"
              "How does the NEW message affect our work on this case?")
    out = claude.lean("impact", prompt, model="sonnet", cwd=site.primary_path(), schema=JUDGE,
                      tools=(), item_id=item_id, urgent=True, timeout=180)
    imp = out["impact"]
    log("impact judged", item=item_id, impact=imp, why=out["why"][:160])
    if imp in ("none", "info"):
        if report:  # the worker of a parked case would not act on it: he hears it once, with what the case waits for
            prs = ", ".join(p.get("pr_url") or "" for p in _prs(it))
            waits = d.get("blocked_reason") or d.get("waiting_for") or ""
            _tell(it, site.t(f"📌 *{_comms.name(it)}*: another report of the same problem came in: {text[:200]} ({out['why'][:160]}). "
                             f"The case is still at {it['status']}",
                             f"📌 *{_comms.name(it)}*: llegó otro reporte del mismo problema: {text[:200]} ({out['why'][:160]}). "
                             f"El caso sigue en {it['status']}") + (f", PR {prs}" if prs else "") + (f": {waits[:200]}" if waits else "") + ".")
        return imp
    inbox = d.get("inbox", []) + [{"at": now(), "who": who, "text": text[:800], "impact": imp, "why": out["why"], "thread": bool(tts)}]
    db.update_item(item_id, data={"inbox": inbox[-10:]})
    worker(item_id, who, text, imp, out["why"], ch, tts)
    return imp


def _running_thread(item_id: int) -> str | None:
    for r in db.q("SELECT k FROM kv WHERE k LIKE 'stage_started:%'"):
        st = db.kv_get(r["k"]) or {}
        if st.get("item") == item_id and st.get("at"):
            return r["k"].split(":", 1)[1]
    return None


def _tell(it: dict, text: str) -> None:
    try:
        rt = it["data"].get("report_ts")
        slack.send_self(text, thread_ts=rt, topic=f"impact-{it['id']}")
    except Exception as e:
        log("impact: DM failed", item=it["id"], err=str(e)[:120])


def worker(item_id: int, who: str, text: str, imp: str, why: str, ch: str, tts: str) -> None:
    it = db.item(item_id)
    where = (site.t(f"in the <{site.slack_link(ch, tts)}|thread>", f"en el <{site.slack_link(ch, tts)}|hilo>") if ch and tts
             else (f"({ch})" if ch else ""))
    head = f"📌 *{_comms.name(it)}*: {who} {where}: {why}"
    th = _running_thread(item_id)
    if th:
        from . import hot
        # marked first, so the stage reads the kill as this hand-off, not a failure (2026-10-05: #487's fix interrupt was
        # logged 'stage failed' with a traceback, spent an attempt and was one of the 6 errors behind a ratchet revert)
        db.kv_set(f"news_interrupt:{th}", {"item": item_id, "at": now()})
        n = hot._kill_thread_procs(th)  # the stage retries and resumes its session with the news (claude.news_for)
        log("impact: running stage interrupted with the news", item=item_id, thread=th, procs=n)
        return _tell(it, f"{head}\n" + site.t("I passed it to the worker on the case; it decides whether to go on, adjust or stop.",
                                              "Se lo pasé al worker que está trabajando el caso; decide él si sigue, ajusta o frena."))
    if it["status"] not in PARKED_STOP_OK:
        return _tell(it, head)
    dec = decide(it, who, text, imp, why)
    if dec["decision"] == "stop":
        closed = stop(it, dec)
        return _tell(it, f"{head}\n" + site.t(f"I stopped it: {dec['reason']}", f"Lo frené: {dec['reason']}")
                     + (site.t(f" Closed {', '.join(closed)}.", f" Cerré {', '.join(closed)}.") if closed else ""))
    if dec["decision"] == "adjust":
        # the news reshapes what the case delivers, so the stage that made it redoes it: its PR, its squad proposal, else
        # the plan. The adjustment is the thread's, not the user's `instructions`, which make the pregate build anyway
        # (2026-10-05: #496, a proposal his teammate then took on, went to plan, the gate's drop was overridden "by his
        # instructions" and plan failed twice on "unknown repo none")
        # A reply is redrafted by its own stage, which reads the thread again (2026-10-16: #866, sent to plan after a teammate
        # answered a teammate, became a squad post saying nobody had answered her)
        back = ("fix" if _prs(it) else "reply_draft" if it["kind"] == "REPLY"
                else "propose" if it["data"].get("proposal_draft") else "plan")
        # a draft to send is redone once the thread settles, as a reply's moved thread is (approvals._thread_moved): each
        # message of a live conversation redrafted and re-asked at once (2026-10-20: #889, four drafts and asks between
        # 12:54 and 13:19, each superseded before he could read it). News in the wait is judged again and moves it on
        settle = THREAD_SETTLE_S if back in ("propose", "reply_draft") else 0
        db.update_item(item_id, status=back, next_run_in=settle, note=site.t(f"news in the thread ({who}): adjust", f"noticia en el hilo ({who}): ajustar"),
                       data={"news_instructions": ((it["data"].get("news_instructions") or "")
                                                   + f"\n[{who}, {site.t('thread', 'hilo')}] {dec.get('instructions') or why}").strip()})
        return _tell(it, f"{head}\n" + site.t("Adjusting the work: ", "Ajusto el trabajo: ") + (dec.get('instructions') or dec['reason']))
    _tell(it, f"{head}\n" + site.t("Going on: ", "Sigo: ") + dec['reason'])


def decide(it: dict, who: str, text: str, imp: str, why: str) -> dict:
    """The case's worker judges with evidence (it can open the PRs and the diff), not the judge's one-liner."""
    from . import claude
    d = it["data"]
    prompt = (f"You own this case in {site.first_name()}'s engineering harness.\nTITLE: {it['title']}\nSTATUS: {it['status']}\n"
              f"PROBLEM: {((d.get('dossier') or {}).get('problem') or d.get('summary') or '')[:1500]}\n"
              f"OUR PRs: {json.dumps([{k: p.get(k) for k in ('repo', 'pr', 'pr_url', 'branch')} for p in _prs(it)])}\n"
              f"Case folder: var/items/{it['id']}/\n\n"
              f"NEW in the case thread, from {who} (judged '{imp}': {why}):\n{text}\n\n"
              "Verify before deciding: open the PR they mention (gh pr view / gh pr diff), compare with ours and with the problem. "
              "stop = our work is redundant or not needed (their merged/open PR solves the same problem, or it is not a bug); "
              "adjust = keep going but the work must change; continue = it does not change our work. Never stop on a claim you could not verify.")
    from .core import DATA
    return claude.orchestrator(prompt, cwd=str(DATA), schema=DECIDE, item_id=it["id"], caller="news_decide")


def stop(it: dict, dec: dict) -> list[str]:
    from . import approvals
    from .guardcall import act
    closed = []
    for p in _prs(it):
        try:
            from . import gh
            if (gh.pr_view(p["repo"], p["pr"]) or {}).get("state") != "OPEN":
                continue
            act("gh_pr_op", repo=p["repo"], pr=p["pr"], op="close", comment=dec.get("pr_comment") or dec["reason"])
            closed.append(f"{p['repo']}#{p['pr']}")
        except Exception as e:
            log("impact: close failed", item=it["id"], pr=p.get("pr"), err=str(e)[:160])
    for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"]):
        try:
            approvals.supersede(a, json.loads(a["content"] or "{}"), site.t("the case is no longer needed", "el caso ya no hace falta"))
        except Exception:
            pass
    # its verification works for it on the same thread but never hears the thread's news, which reaches the case the
    # thread is keyed to: still queued, it would investigate for a reply nobody needs (2026-10-15: #824 started its
    # 0.9 USD investigate 9 s after #821 was stopped because the user had answered TICKET-1502 himself)
    kid = db.item(it["data"]["child"]) if it["data"].get("child") else None
    if kid and kid["status"] not in db.TERMINAL and not _running_thread(kid["id"]):
        db.update_item(kid["id"], status="dropped", note=site.t(f"stopped with case #{it['id']}: {dec['reason'][:180]}",
                                                               f"frenado con el caso #{it['id']}: {dec['reason'][:180]}"))
    # nudges and CI watchers of the closed PRs go too
    for p in _prs(it):
        for r in db.q(f"SELECT id FROM items WHERE kind IN ('NUDGE','CI') AND status NOT IN {db.TERMINAL} AND title LIKE ?",
                      f"%{p['repo']}#{p['pr']}%"):
            db.update_item(r["id"], status="dropped", note=site.t("the PR closed: the case was redundant", "el PR se cerró: el caso era redundante"))
    # the closed PRs lead the note: #487's 'frenado por noticia' never said cometa#2525 was closed, so on 10-05 the
    # observer filed its PR as left open on a dropped case
    shut = site.t(f" (closed {', '.join(closed)})", f" (cerré {', '.join(closed)})") if closed else ""
    # dedupe.RULINGS reads this note's opening ("frenado por noticia" / "stopped by news")
    db.update_item(it["id"], status="dropped", note=site.t(f"stopped by news in the thread{shut}: {dec['reason'][:200]}",
                                                         f"frenado por noticia en el hilo{shut}: {dec['reason'][:200]}"))
    log("impact: case stopped", item=it["id"], closed=closed)
    return closed


def news(item_id: int, who: str, text: str, where: str = "", report: bool = False) -> None:
    """News for a case that did not come from its own Slack thread (a folded duplicate, a teammate's PR): judged and
    routed to the worker like thread news, in its own thread. report: a new report of the same problem folded into a
    parked case, which the user hears about even when the judge calls it info."""
    threading.Thread(target=_safe, args=(item_id, 0, who, text, where or None, None, report), name="impact", daemon=True).start()


WATCH_KINDS = ("TASK", "INCIDENT", "IDEA")


def _files_of(repo: str, n: int) -> list[str]:
    key = f"prfiles:{repo}#{n}"
    hit = db.kv_get(key)
    if hit is not None:
        return hit
    from . import gh
    try:
        files = [f["filename"] for f in (gh.api(f"repos/{site.slug(repo)}/pulls/{n}/files?per_page=100") or [])]
    except Exception:
        return []
    db.kv_set(key, files)
    return files


def _our_files(it: dict) -> dict[str, set[str]]:
    """repo -> files our work touches or plans to touch (scope entries start with the path)."""
    out: dict[str, set[str]] = {}
    for p in it["data"].get("parts", []) or (it["data"].get("plan") or {}).get("parts", []):
        fs = out.setdefault(p.get("repo") or "", set())
        for s in p.get("scope") or []:
            path = str(s).split(" ", 1)[0].strip("`,:")
            if "/" in path or path.endswith((".py", ".ts", ".tsx")):
                fs.add(path.split("/", 1)[-1] if path.startswith(f"{p.get('repo')}/") else path)
    return {r: f for r, f in out.items() if r and f}


def pr_watch() -> int:
    """Teammates' PRs that touch what an active case touches.
    One list call per repo every few minutes; files are cached per PR; a match is news for the case's worker."""
    from . import gh
    me = CFG.get("self_github", "")
    # active cases, plus delivered ones whose PR is still open: until it merges, someone else's equivalent PR makes it
    # redundant
    active = [db.item(r["id"]) for r in db.q(
        f"SELECT id FROM items WHERE kind IN {WATCH_KINDS} AND created_at > ? AND (status NOT IN {db.TERMINAL} "
        "OR (status='done' AND json_extract(data,'$.parts') LIKE '%pr_url%' AND updated_at > ?))", now() - 21 * 86400, now() - 14 * 86400)]
    # a done case's PR closed or merged (ingest marks his PRs own=2 when they leave his open list): its work is shipped,
    # nothing can make it redundant. Without this #137, done on 09-29 with both PRs merged, got a judge for every teammate
    # PR on its files (5 on 10-06, 10-08, 10-09), and each match's pr_watch_seen write kept it inside the 14-day window
    active = [it for it in active if it["status"] != "done" or any(
        not db.q1("SELECT 1 FROM prs WHERE repo=? AND number=? AND own=2", p.get("repo"), p.get("pr"))
        for p in it["data"].get("parts", []) if p.get("pr"))]
    ours = {f"{p.get('repo')}#{p.get('pr')}" for r in db.q(f"SELECT data FROM items WHERE kind IN {WATCH_KINDS} "
                                                          "AND json_extract(data,'$.parts') LIKE '%pr_url%'")  # CI/COMMENTS wrap his own PRs
            for p in json.loads(r["data"] or "{}").get("parts", []) if p.get("pr")}
    wants: dict[str, list[tuple[dict, set[str]]]] = {}
    for it in active:
        for repo, files in _our_files(it).items():
            wants.setdefault(repo, []).append((it, files))
    n = 0
    for repo, cases in wants.items():
        try:
            prs = gh.api(f"repos/{site.slug(repo)}/pulls?state=all&sort=updated&direction=desc&per_page=30") or []
        except Exception as e:
            log("pr watch: list failed", repo=repo, err=str(e)[:120])
            continue
        for pr in prs:
            login = (pr.get("user") or {}).get("login")
            if login is None or "[bot]" in login or f"{repo}#{pr['number']}" in ours:
                continue  # the harness's own PRs are the work itself; his manual PRs count like a teammate's
            if pr.get("state") == "closed" and not pr.get("merged_at"):
                continue
            pf = set(_files_of(repo, pr["number"]))
            for it, files in cases:
                seen = it["data"].get("pr_watch_seen", [])
                ref = f"{repo}#{pr['number']}"
                created = it["created_at"]
                upd = pr.get("updated_at") or ""
                if ref in seen or not (pf & files):
                    continue
                if pr["html_url"] in json.dumps(it["data"], ensure_ascii=False):
                    continue  # the case already knows this PR (its own, linked or related)
                if upd and upd < time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)):
                    continue  # older than the case: the related-PR check at plan time already saw it
                db.update_item(it["id"], data={"pr_watch_seen": (seen + [ref])[-50:]})
                state = site.t("merged", "mergeado") if pr.get("merged_at") else site.t("open", "abierto")
                same = ', '.join(sorted(pf & files))[:300]
                text = (site.t(f"{pr['user']['login']} has {ref} ({state}): \"{pr['title']}\" {pr['html_url']}\n"
                               f"it touches the same files as this case: {same}\n",
                               f"{pr['user']['login']} tiene {ref} ({state}): \"{pr['title']}\" {pr['html_url']}\n"
                               f"toca los mismos archivos que este caso: {same}\n")
                        + f"{(pr.get('body') or '')[:1200]}")
                news(it["id"], pr["user"]["login"], text, pr["html_url"])
                log("pr watch: teammate PR on our files", item=it["id"], pr=ref, state=state)
                n += 1
    return n
