"""Tickets as intake: the Jira issues the user chose to consume become cases.

The onboarding stores what to consume (the projects, whether tickets assigned to the user and tickets that mention
them count, any extra JQL) and the user's own Jira API token. This module asks Jira for exactly that, over REST, and
opens one case per ticket that is new to the install. It only reads: nothing here comments on, moves or closes a
ticket.

A ticket is one piece of work however often it changes: its case is keyed by the ticket ('jira:<KEY>'), and a ticket
that already has an open case, or whose case ended while the ticket read the same, is left alone. The first sync is a
baseline, not a backlog run: only what moved in the last week becomes a case (a few at a time), and everything older
is remembered as seen, so connecting Jira never starts forty investigations.
"""
from __future__ import annotations

import time

from . import db, site
from .core import CFG, log, now

FIRST_SYNC_DAYS = 7
PER_POLL = 5          # new cases per pass: a reassigned backlog trickles in instead of flooding the queue
SEEN = "tickets_seen"  # kv: key -> the 'updated' stamp of the ticket as last seen


def enabled() -> bool:
    j = CFG.get("jira") or {}
    return bool(j.get("enabled") and site.jira_base() and j.get("email"))


def _text(node) -> str:
    """Plain text of an Atlassian Document Format value (what the REST API returns for a description)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text") or ""
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return (node.get("attrs") or {}).get("text") or ""
    body = _text(node.get("content") or [])
    return body + ("\n" if t in ("paragraph", "heading", "listItem", "codeBlock", "blockquote", "rule") else "")


def fetch(limit: int = 100) -> list[dict]:
    """The user's tickets as the install consumes them: [{key, summary, status, priority, updated, description}],
    most recently updated first."""
    from . import setup
    j = CFG.get("jira") or {}
    jql = setup.jira_jql(j.get("projects") or [], j.get("assigned", True), j.get("mentions", False), j.get("jql") or "")
    out = []
    for i in setup.jira_search(jql + " ORDER BY updated DESC", limit=limit):
        f = i.get("fields") or {}
        out.append({"key": i["key"], "summary": f.get("summary") or "", "status": (f.get("status") or {}).get("name") or "",
                    "priority": (f.get("priority") or {}).get("name") or "", "updated": f.get("updated") or "",
                    "description": _text(f.get("description") or "").strip()[:4000]})
    return out


def _age_days(updated: str) -> float:
    try:
        return (time.time() - time.mktime(time.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (ValueError, TypeError):
        return 0.0


def poll() -> int:
    """One pass: open a case for each consumed ticket that is new to the install. Returns how many were opened."""
    if not enabled():
        return 0
    issues = fetch()
    seen = db.kv_get(SEEN)
    first = seen is None
    seen = seen or {}
    made = 0
    for i in issues:
        key, tk = i["key"], f"jira:{i['key']}"
        text = f"{key} {i['summary']}\n{i['description']}"
        if key in seen:
            continue
        if first and (_age_days(i["updated"]) > FIRST_SYNC_DAYS or site.backlog(i["updated"])):
            seen[key] = i["updated"]  # the baseline: older backlog is known, not work the install was asked to start
            continue
        if made >= PER_POLL:
            continue  # not marked seen: the next pass takes it
        seen[key] = i["updated"]
        if db.open_item_for_thread(tk) or db.q1("SELECT 1 FROM items WHERE thread_key=? AND status IN ('dropped','done') AND "
                                                 "json_extract(data,'$.text')=?", tk, text):
            continue  # already being worked, or judged while it read exactly like this
        urgency = 3 if i["priority"] in ("Highest", "High", "Critical", "Blocker") else 2 if i["priority"] == "Medium" else 1
        iid = db.create_item("TASK", "investigate", f"{key}: {i['summary'][:80]}", tk, urgency=urgency,
                             data={"summary": i["summary"], "text": text, "jira": key, "from": "tickets", "ticket_status": i["status"]})
        if iid:
            made += 1
            log("tickets: case opened", item=iid, key=key, status=i["status"])
    db.kv_set(SEEN, {k: v for k, v in list(seen.items())[-2000:]})
    db.kv_set("tickets_polled_at", now())
    if first:
        log("tickets: first sync", consumed=len(issues), opened=made)
    return made
