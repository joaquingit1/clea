"""Precedents: how the squad handled peculiar customer/ops requests before (bulk deletes,
data moves, per-school config, "hay forma de..."). the user, 2026-09-24: check for an internal
precedent first, record how each request was resolved, and grow one master memory so nobody
re-researches the same kind of request.

Two views of the same data: kv `precedents` (structured, searched by keyword overlap and
injected into prompts) and knowledge/precedents.md (tracked in git, human-readable)."""
from __future__ import annotations

import json
import re
import time

from . import db, site, slack
from .core import CFG, KNOWLEDGE, log, now, run

MD = KNOWLEDGE / "precedents.md"
STOP = {"para", "como", "esta", "este", "esto", "pero", "porque", "tiene", "tienen", "hay", "forma",
        "favor", "hola", "buenas", "consulta", "quiero", "quieren",
        "they", "that", "with", "from", "this", "squad", "group", "espero", "gracias"}


def _words(s: str) -> set[str]:
    stop = STOP | site.stop_words("precedents")  # plus the words every request of this install carries (its customers, its area)
    return {w for w in re.split(r"[^a-záéíóúñ0-9]+", (s or "").lower()) if len(w) >= 4 and w not in stop}


def all_entries() -> list[dict]:
    return db.kv_get("precedents", [])


def search(text: str, limit: int = 8) -> list[dict]:
    """Entries whose request/resolution share content words with `text`; school name matches count double."""
    w = _words(text)
    scored = []
    for e in all_entries():
        ew = _words(f"{e.get('request', '')} {e.get('kind', '')} {e.get('resolution', '')}")
        if not ew:
            continue
        s = len(w & ew) / max(1, len(w | ew))
        if e.get("school") and e["school"].lower() in (text or "").lower():
            s += 0.3
        if s >= 0.08:
            scored.append((s, e))
    return [e for _, e in sorted(scored, key=lambda x: -x[0])[:limit]]


def text_for(query: str) -> str:
    hits = search(query)
    if not hits:
        return ""
    lines = ["<precedents> (how the squad handled similar requests before; cite them with date and link)"]
    for e in hits:
        lines.append(f"- {e['date']} · {e.get('school') or site.t('several', 'varios')} · {e['request'][:160]} → {e['resolution'][:260]}"
                     + (site.t(f" (by {e['who']})", f" (por {e['who']})") if e.get("who") else "")
                     + (f" {e['links'][0]}" if e.get("links") else ""))
    lines.append("</precedents>")
    return "\n".join(lines)


def add(*, request: str, resolution: str, kind: str = "", school: str = "", who: str = "", links: list[str] | None = None,
        source_item: int | None = None, date: str | None = None) -> dict:
    entries = all_entries()
    key = re.sub(r"\s+", " ", f"{school} {request}".strip().lower())[:160]
    for e in entries:
        if e.get("key") == key:
            e.update(resolution=resolution[:1200] or e["resolution"], who=who or e.get("who"), links=links or e.get("links"),
                     updated=time.strftime("%Y-%m-%d"))
            db.kv_set("precedents", entries)
            _render()
            return e
    e = {"id": (max([x["id"] for x in entries], default=0) + 1), "key": key, "date": date or time.strftime("%Y-%m-%d"),
         "school": school[:80], "kind": kind[:80], "request": request[:400], "resolution": resolution[:1200], "who": who[:80],
         "links": (links or [])[:4], "item": source_item, "at": now()}
    entries.append(e)
    db.kv_set("precedents", entries)
    _render()
    log("precedent recorded", id=e["id"], kind=kind, school=school)
    return e


def _render() -> None:
    MD.parent.mkdir(parents=True, exist_ok=True)
    out = site.own(["# Precedentes: pedidos peculiares de colegios y cómo los resolvimos", "",
                    "Una entrada por pedido. Lo escribe el harness cuando un caso se cierra (respuesta enviada, respuesta de un",
                    "compañero, PR o script). Fuente para responder pedidos parecidos sin volver a investigar.", ""],
                   site.t(["# Precedents: unusual requests and how they were resolved", "",
                           "One entry per request, written when a case closes (an answer sent, a teammate's answer, a PR or a",
                           "script). The source for answering similar requests without researching them again.", ""],
                          ["# Precedentes: pedidos peculiares y cómo los resolvimos", "",
                           "Una entrada por pedido. Se escribe cuando un caso se cierra (respuesta enviada, respuesta de un",
                           "compañero, PR o script). Fuente para responder pedidos parecidos sin volver a investigar.", ""]))
    by_kind: dict[str, list[dict]] = {}
    for e in sorted(all_entries(), key=lambda x: x["date"]):
        by_kind.setdefault(e.get("kind") or site.t("other", "otros"), []).append(e)
    for kind, es in sorted(by_kind.items()):
        out.append(f"## {kind}")
        for e in es:
            out.append(f"- **{e['date']} · {e.get('school') or site.t('several', 'varios')}**: {e['request']}")
            out.append(site.t(f"  - Resolution: {e['resolution']}", f"  - Resolución: {e['resolution']}")
                       + (site.t(f" (by {e['who']})", f" (por {e['who']})") if e.get("who") else ""))
            for l in e.get("links") or []:
                out.append(f"  - {l}")
        out.append("")
    MD.write_text("\n".join(out))  # committed by the hourly knowledge_commit job, never from here


def hunt(terms: list[str], school: str = "", days: int = 240) -> dict:
    """Look for precedent where it would be: our own store, Slack (ops channels, last months) and
    Jira/CUS tickets through the bridge. Returns raw material for Opus to judge, with links."""
    from . import bridge
    out = {"stored": search(" ".join(terms + [school]), limit=6), "slack": [], "jira": []}
    after = time.strftime("%Y-%m-%d", time.localtime(now() - days * 86400))
    seen = set()
    chans = [CFG["team_channel"], *CFG.get("watched_channels", [])]
    for term in terms[:4]:
        for q in (f"{term} after:{after}",) + tuple(f"{term} in:<#{c}> after:{after}" for c in chans[:2]):
            try:
                for m in slack.search(q, count=20):
                    ch = (m.get("channel") or {}).get("id")
                    if not ch or ch == CFG["self_dm_channel"] or m["ts"] in seen:
                        continue
                    seen.add(m["ts"])
                    out["slack"].append({"when": db.fmt_time(m["ts"]), "channel": (m.get("channel") or {}).get("name"),
                                         "who": slack.user_name(m.get("user")) if m.get("user") else m.get("username"),
                                         "text": slack.render(m.get("text") or "")[:500], "link": m.get("permalink")})
            except Exception as e:
                log("precedent hunt: slack search failed", q=q, err=str(e)[:120])
        if len(out["slack"]) >= 25:
            break
    # the resolution is in the thread, not in the root message: read the threads of the best hits
    tw = _words(" ".join(terms) + " " + school)
    ranked = sorted(out["slack"], key=lambda s: -len(tw & _words(s["text"])))
    for s in ranked[:5]:
        try:
            ch, ts = slack.thread_ts_from_permalink(s.get("link")), None
            m = __import__("re").search(r"archives/([A-Z0-9]+)/p(\d{10})(\d{6})", s.get("link") or "")
            if m:
                root = ch or f"{m.group(2)}.{m.group(3)}"
                msgs = slack.replies(m.group(1), root)[:10]
                s["thread"] = [f"[{db.fmt_time(x['ts'])}] {slack.user_name(x.get('user'))}: {slack.render(x.get('text') or '')[:400]}" for x in msgs]
        except Exception as e:
            s["thread_error"] = str(e)[:100]
    try:
        out["jira"] = bridge.fetch("precedent-jira",
                                   f'Run searchJiraIssuesUsingJql with cloudId "{bridge.JIRA_CLOUD}" for each of these: '
                                   + "; ".join(f'text ~ "{t}" ORDER BY updated DESC' for t in terms[:3])
                                   + f". maxResults 15 each, fields [summary,status,resolution,updated,description,project]. "
                                   f"Return every distinct issue: key, summary, status, project, updated, and the first 400 chars of "
                                   f"the description or the resolution comment.",
                                   ["mcp__claude_ai_Atlassian__searchJiraIssuesUsingJql", "mcp__claude_ai_Atlassian__getJiraIssue"],
                                   {"type": "object", "required": ["key", "summary"], "properties": {
                                       "key": {"type": "string"}, "summary": {"type": "string"}, "status": {"type": "string"},
                                       "project": {"type": "string"}, "updated": {"type": "string"}, "description": {"type": "string"}}})
    except Exception as e:
        out["jira_error"] = str(e)[:200]
    log("precedent hunt", terms=terms[:4], stored=len(out["stored"]), slack=len(out["slack"]), jira=len(out["jira"]))
    return out
