"""Case files (v1, 2026-08-18): shared memory per incident/ticket. `verified_facts` carry their
evidence, `refuted` hypotheses are never re-asserted, `remaining` lists what is still open.
Keyed by ticket (TICKET-1234, DA-5678) when the conversation names one, else by the Slack thread.
Every investigation, telemetry dive, reply and report reads the file first and writes back."""
from __future__ import annotations

import json
import re

from . import db, site
from .core import now

_KEY = site.ticket_key(digits=site.ticket_digits())
TICKET = re.compile(rf"\b({_KEY})\b")
_SUGGESTED = re.compile(r"esto puede ya existir.*?(?=Voy a crear|Si es un caso distinto|$)", re.S | re.I)


def tickets_in(text: str) -> list[str]:
    """Ticket keys that are SOURCES of a case. The Status bot lists 'esto puede ya existir' candidates
    (unrelated tickets) before creating one: those are never evidence."""
    cleaned = _SUGGESTED.sub(" ", text or "")
    return sorted(set(TICKET.findall(cleaned)))


def key_for(it: dict) -> str:
    d = it["data"]
    blob = f"{it['title']} {d.get('text', '')} {d.get('summary', '')}"
    m = TICKET.search(blob)
    if m:
        return m.group(1)
    if d.get("channel") and d.get("thread_ts"):
        return f"{d['channel']}:{d['thread_ts']}"
    return f"item:{d.get('parent') or it['id']}"


def read(key: str) -> dict:
    return db.kv_get(f"case:{key}", {"verified_facts": [], "refuted": [], "remaining": [], "reports": []})


def add(key: str, *, facts: list[dict] | None = None, refuted: list[str] | None = None,
        remaining: list[str] | None = None, report_ts: str | None = None, source: str = "") -> None:
    c = read(key)
    for f in facts or []:
        fact = f if isinstance(f, dict) else {"fact": str(f), "evidence": ""}
        if fact.get("fact") and fact["fact"] not in [x["fact"] for x in c["verified_facts"]]:
            c["verified_facts"].append({**fact, "at": now(), "source": source})
    for r in refuted or []:
        if r and r not in c["refuted"]:
            c["refuted"].append(r)
    if remaining is not None:
        c["remaining"] = remaining
    if report_ts:
        c["reports"].append(report_ts)
    c["updated_at"] = now()
    db.kv_set(f"case:{key}", c)


def text_for(it: dict) -> str:
    key = key_for(it)
    c = read(key)
    if not (c["verified_facts"] or c["refuted"]):
        return ""
    out = [f"<case_file key={key}> (shared memory; refuted hypotheses must never be re-asserted)"]
    for f in c["verified_facts"][-25:]:
        out.append(f"- VERIFIED: {f['fact']}" + (f"  [evidence: {f['evidence'][:200]}]" if f.get("evidence") else ""))
    for r in c["refuted"][-15:]:
        out.append(f"- REFUTED: {r}")
    for r in c.get("remaining", [])[-10:]:
        out.append(f"- OPEN: {r}")
    out.append("</case_file>")
    return "\n".join(out)


# ---- a case that came from a ticket is that ticket ---------------------------------------------------------------
# 2026-10-06: a customer's case started as a #sis-ops thread where the Status bot created TICKET-1368 ("Ticket creado:
# <...|TICKET-1368>"). The case kept "Instituto a customer:..." as its title and no ticket, so the digest showed it as a
# plain Slack request and the user could not tell it was a ticket.
STATUS_BOT = site.ticket_bot()  # '' when the workspace has no such bot: no message is ever its
_BOT_KEY = site.ticket_key("bot", r"\d+")
_CREATED = re.compile(rf"Ticket creado:?\s*<[^|>]*\|({_BOT_KEY})>|Ticket creado:?\s*({_BOT_KEY})")


def created_ticket(text: str) -> str | None:
    m = _CREATED.search(text or "")
    return (m.group(1) or m.group(2)) if m else None


def bind_ticket(item_id: int, key: str, source: str = "") -> bool:
    """The ticket becomes the case's own: data.jira (closing closes it, dedupe keys on it) and the title leads with it."""
    from . import db
    it = db.item(item_id)
    if not it or it["data"].get("jira") == key:
        return False
    bare = re.sub(r"^\(slack\)\s*", "", it["title"])
    title = it["title"] if key in it["title"] else f"{key}: {bare}"[:200]
    db.update_item(item_id, title=title, data={"jira": key, "ticket_source": source or "status-bot"},
                   note=site.t(f"it is ticket {key} (the Status bot created it in the thread)", f"es el ticket {key} (lo creó Status en el hilo)"))
    return True


def ticket_in_thread(channel: str, ts: str) -> str | None:
    from . import slack
    if not STATUS_BOT:
        return None
    try:
        for m in slack.replies(channel, ts):
            if m.get("user") == STATUS_BOT:
                key = created_ticket(slack.render(m.get("text") or ""))
                if key:
                    return key
    except Exception:
        return None
    return None
