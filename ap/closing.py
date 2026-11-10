"""Epic closing: when every PR that solves a case is merged and live, tell the Slack
thread it came from (short, non-technical when it is ops/sales) and close its Jira ticket the way the
CUS workflow accepts it. A thread waits for his ✅ (or his own message there); a ticket with no thread
closes without asking. Ticket procedure: ~/.claude/skills/close-cus-ticket (verified 2026-09-04)."""
from __future__ import annotations

import json
import time

from . import approvals, bridge, cases, claude, db, gh, merge_order, site, slack
from . import comms as _comms
from .core import CFG, log, now
from .worktree import MIRROR

CLOSE_MSG = {"type": "object", "required": ["text"], "properties": {
    "text": {"type": "string", "description": "only the message the thread reads, in his voice"},
    "hold": approvals.HOLD}}


def _live(repo: str, n: int) -> tuple[bool, str]:
    """Merged and live where it matters: prod for backends, merge for the frontend (merge_order's rule)."""
    rc = CFG["repos"].get(repo) or {}
    need = "merged" if rc.get("frontend_live_on_merge") else "prod"
    return merge_order.dep_status(repo, n, need)


def _sources(it: dict) -> tuple[list[dict], list[str]]:
    d = it["data"]
    threads = []
    if d.get("channel") and (d.get("thread_ts") or d.get("ts")):
        threads.append({"channel": d["channel"], "ts": d.get("thread_ts") or d["ts"]})
    for a in d.get("also_sources", []):
        if a.get("channel") and a.get("ts") and not a.get("pattern"):
            threads.append({"channel": a["channel"], "ts": a["ts"]})
    tickets = set(cases.tickets_in(f"{it['title']} {d.get('text', '')} {d.get('summary', '')} {d.get('jira', '')}"))
    for a in d.get("also_sources", []):
        tickets |= set(a.get("tickets") or [])
    closable = tuple(f"{p}-" for p in site.prefixes("close"))  # none configured: the harness closes no ticket
    return threads, sorted(t for t in tickets if t.startswith(closable))


def _prs(it: dict) -> list[dict]:
    """The PRs that solve the case: its own (parts) and a teammate's that provably covers it (ap/cover.py)."""
    return ([p for p in it["data"].get("parts", []) if p.get("pr_url") and p.get("pr")]
            + [p for p in it["data"].get("linked_prs") or [] if p.get("pr_url") and p.get("pr")])


def _reopen_if_linked_pr_closed(it: dict) -> bool:
    """A covering PR closed without merge no longer covers anything: the case goes back to work."""
    linked = [p for p in it["data"].get("linked_prs") or [] if p.get("pr")]
    if not linked or it["data"].get("parts") and any(p.get("pr_url") for p in it["data"]["parts"]):
        return False
    try:
        states = [(gh.pr_view(p["repo"], p["pr"]) or {}).get("state") for p in linked]
    except Exception:
        return False
    if not all(s == "CLOSED" for s in states):
        return False
    db.update_item(it["id"], status="plan", attempts=0, next_run_at=0,
                   note=site.t(f"the PR that covered it closed without merging ({', '.join(p['pr_url'] for p in linked)}): back to work",
                               f"el PR que lo cubría se cerró sin mergear ({', '.join(p['pr_url'] for p in linked)}): vuelve a trabajo"),
                   data={"linked_prs": [], "covered_by": None, "pregate": None, "related_checked_at": None, "related_prs": []})
    return True


def sweep() -> int:
    n = 0
    for r in db.q("SELECT id, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('dropped','failed') "
                  "AND (json_extract(data,'$.parts') LIKE '%pr_url%' OR json_extract(data,'$.linked_prs') LIKE '%pr_url%') "
                  "AND json_extract(data,'$.closing.state') IS NULL"):
        it = db.item(r["id"])
        if _reopen_if_linked_pr_closed(it):
            continue
        parts = _prs(it)
        if not parts:
            continue
        states = [_live(p["repo"], p["pr"]) for p in parts]
        if not all(ok for ok, _ in states):
            continue
        threads, tickets = _sources(it)
        if not threads and not tickets:
            db.update_item(it["id"], data={"closing": {"state": "nothing_to_close", "at": now()}})
            continue
        log("closing: epic complete", item=it["id"], threads=len(threads), tickets=tickets)
        if threads:
            _ask_thread_message(it, threads, tickets, [w for _, w in states])
        else:
            db.update_item(it["id"], data={"closing": {"state": "closing_tickets", "tickets": tickets, "at": now()}})
            _close_tickets(db.item(it["id"]), tickets)
        n += 1
    # threads where he already answered himself after the merge: close the tickets, retire the ask
    for r in db.q("SELECT id, data FROM items WHERE json_extract(data,'$.closing.state')='ask'"):
        it = db.item(r["id"]); c = it["data"]["closing"]
        since = _merged_at(it) or c["at"]
        for th in c.get("threads", []):
            try:
                mine = [m for m in slack.replies(th["channel"], th["ts"]) if m.get("user") == CFG["self_user_id"] and float(m["ts"]) > since]
            except Exception:
                mine = []
            if mine:
                for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"]):
                    meta = json.loads(a["content"] or "{}")
                    if (meta.get("payload") or {}).get("which") == "closing":
                        approvals.supersede(a, meta, site.t("you already told the thread yourself", "ya avisaste vos en el hilo"))
                after_notified(db.item(it["id"]))
                break
    return n


def _merged_at(it: dict) -> float:
    """Epoch of the earliest merge among the case's PRs (his notice in the thread after that counts as done)."""
    ts = []
    for p in _prs(it):
        if p.get("pr") and p.get("repo"):
            try:
                m = (gh.api(f"repos/{site.slug(p['repo'])}/pulls/{p['pr']}") or {}).get("merged_at")
                if m:
                    ts.append(time.mktime(time.strptime(m, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone)
            except Exception:
                pass
    return min(ts) if ts else 0.0


def _ask_thread_message(it: dict, threads: list[dict], tickets: list[str], live_notes: list[str]) -> None:
    d = it["data"]
    ds = d.get("dossier") or {}
    th = threads[0]
    try:
        raw = "\n".join(f"{slack.user_name(m.get('user'))}: {slack.render(m.get('text') or '')[:400]}" for m in slack.replies(th["channel"], th["ts"])[:12])
    except Exception:
        raw = d.get("text", "")[:1500]
    names = (CFG.get("closing") or {}).get("ops_channel_names") or []
    ops = (slack.channel_name(th["channel"]) in names if names
           else th["channel"] in ((CFG.get("audiences") or {}).get("ops_channels") or []))
    problem, done = ds.get('problem', d.get('summary', ''))[:800], d.get('done_summary', '')[:500]
    facts = (f"PRs: {', '.join(p['pr_url'] for p in _prs(it))} ({'; '.join(live_notes)})\n"
             f"Tickets: {', '.join(tickets)}\n\n")
    prompt = site.own(
        (f"Escribí el mensaje que {site.first_name()} manda al hilo de Slack avisando que el problema ya quedó resuelto. "
         + ("El hilo es de ops/ventas: nada técnico, corto (máximo 40 palabras): qué se arregló en una frase, que ya está en producción, "
            "y que avisen si vuelve a pasar. " if ops else
            "El hilo es del equipo: una o dos frases, podés nombrar el PR. ")
         + "En su voz (voseo, minúsculas, natural, sin formalidad, sin listas). Solo el texto.\n\n"
         f"Problema: {problem}\nQué se hizo: {done}\n" + facts + f"HILO:\n{raw}"),
        (f"Write the message {site.first_name()} posts in the Slack thread to say the problem is solved, in "
         f"{site.language_name()}. "
         + ("The thread belongs to operations or sales: nothing technical, short (at most 40 words): what was fixed in one "
            "sentence, that it is already in production, and to say so if it happens again. " if ops else
            "The thread belongs to the team: one or two sentences, you can name the PR. ")
         + f"In {site.first_name()}'s own voice (natural, informal, no lists). Only the text.\n\n"
         f"Problem: {problem}\nWhat was done: {done}\n" + facts + f"THREAD:\n{raw}"))
    since = _merged_at(it)
    try:  # he may already have told the thread himself after the merge: then no notice, straight to the tickets
        mine = [m for m in slack.replies(th["channel"], th["ts"]) if m.get("user") == CFG["self_user_id"] and since and float(m["ts"]) > since]
    except Exception:
        mine = []
    if mine:
        db.update_item(it["id"], data={"closing": {"state": "ask", "threads": threads, "tickets": tickets, "at": now(), "self_notified": True}})
        return after_notified(db.item(it["id"]))
    # a writer, not a verifier: merged/live is _live's verdict, given above. With tools in his working checkout it
    # re-checked production on a stale tree and, having one field, wrote "Hold this, don't send it yet..." into the
    # message for #sis-ops; an objection goes to `hold`, which only he reads
    out = claude.lean("draft", prompt, cwd=str(MIRROR), schema=CLOSE_MSG, tools=(), item_id=it["id"])
    text, hold = out["text"].strip(), (out.get("hold") or "").strip()
    if text.upper().startswith("NO MESSAGE"):  # the drafter says the thread already has it
        db.update_item(it["id"], data={"closing": {"state": "ask", "threads": threads, "tickets": tickets, "at": now(), "drafter_declined": text[:400]}})
        return after_notified(db.item(it["id"]))
    for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"]):
        meta = json.loads(a["content"] or "{}")
        if (meta.get("payload") or {}).get("which") == "closing":
            approvals.supersede(a, meta, site.t("new version of the notice", "nueva versión del aviso"))  # one closing ask per case, ever
    payload = {"channel": th["channel"], "thread_ts": th["ts"], "text": text, "which": "closing"}
    case = f"\n\n<{site.slack_link(CFG['self_dm_channel'], d['report_ts'])}|{site.t('case', 'caso')}>" if d.get("report_ts") else ""
    # a closing notice lands as a NEW message in the DM, not a reply buried in a days-old case thread
    #
    ask = approvals.request(it["id"], "slack_send", payload, it["status"],
                            text=site.t(f"🏁 *Resolved: {_comms.name(it)}*\nNotice for the thread in {slack.channel_name(th['channel'])}, ✅ and I send it",
                                        f"🏁 *Resuelto: {_comms.name(it)}*\nAviso al hilo de {slack.channel_name(th['channel'])}, ✅ y lo mando")
                                 + (site.t(f" and close {', '.join(tickets)}", f" y cierro {', '.join(tickets)}") if tickets else "")
                                 + f":\n\n> {text.replace(chr(10), chr(10) + '> ')}"
                                 + (site.t(f"\n\n⚠️ before you send it: {hold}", f"\n\n⚠️ antes de mandarlo: {hold}") if hold else "") + case,
                            thread_ts=None, keep_status=True)
    db.update_item(it["id"], data={"closing": {"state": "ask", "threads": threads, "tickets": tickets, "text": text,
                                               "ask_ts": ask.get("ts"), "at": now()}})


def after_notified(it: dict) -> None:
    """He told the thread (✅ or by hand): now the tickets."""
    c = it["data"].get("closing") or {}
    tickets = c.get("tickets") or []
    db.update_item(it["id"], data={"closing": {**c, "state": "closing_tickets" if tickets else "done", "notified_at": now()}})
    if tickets:
        _close_tickets(db.item(it["id"]), tickets)


TICKET_TEXTS = {"type": "object", "required": ["technical_comment", "root_cause", "solution", "customer_reply"], "properties": {
    "technical_comment": {"type": "string", "description": site.own(
        "internal, Spanish, no em dashes: first line 'Resuelto.', then 'Causa:', 'Fix:' (PRs, merged and deployed), 'Para el colegio:', 'Para soporte:'",
        f"internal, in {site.language_name()}, no em dashes: a first line saying it is resolved, then the cause, the fix (PRs, "
        "merged and deployed), what the customer should know and what support should know")},
    "root_cause": {"type": "string", "description": "one paragraph: the cause"},
    "solution": {"type": "string", "description": "one paragraph: the fix, PR links, what the school does now"},
    "customer_reply": {"type": "string", "description": site.own(
        "customer-facing, plain Spanish: what was wrong in one sentence, 'el fix ya está en producción', what to do now, 'si vuelve a fallar avísennos con la hora'",
        f"customer-facing, plain {site.language_name()}: what was wrong in one sentence, that the fix is already in production, "
        "what to do now, and to tell us the time if it fails again")}}}


def _close_tickets(it: dict, tickets: list[str]) -> None:
    """CUS closing procedure, executed by the outbound guard (~/.claude/skills/close-cus-ticket semantics)."""
    from .guardcall import act
    d = it["data"]
    ds = d.get("dossier") or {}
    prs = ", ".join(p["pr_url"] for p in _prs(it))
    results = {}
    for key in tickets:
        try:
            texts = claude.lean("draft", f"Ticket {key} is solved. Problem: {(ds.get('problem') or d.get('summary') or '')[:1200]}\n"
                                f"Fix: {prs} (merged and deployed to production).\nWrite the four closing texts.",
                                cwd=site.primary_path(), schema=TICKET_TEXTS, item_id=it["id"], model="sonnet")
            res = act("jira_close", key=key, assignee=site.jira().get("account_id") or "", **texts)
            results[key] = res
            log("closing: ticket", key=key, status=res.get("status"), item=it["id"])
        except Exception as e:
            results[key] = {"error": str(e)[:200]}
            log("closing: ticket failed", key=key, err=str(e)[:200], item=it["id"])
    ok = all((r.get("status") or "").lower().startswith("cerrad") for r in results.values())
    c = it["data"].get("closing") or {}
    db.update_item(it["id"], data={"closing": {**c, "state": "done" if ok else "ticket_failed", "tickets_result": results, "closed_at": now()}})
    if it["data"].get("report_ts"):
        try:
            line = " · ".join(f"{k}: {(r.get('status') or r.get('error') or '?')}" for k, r in results.items())
            slack.send_self(f"🎫 {line}", thread_ts=it["data"]["report_ts"], topic=f"closing-{it['id']}")
        except Exception:
            pass
