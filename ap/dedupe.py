"""Duplicate work never starts. In the week before, about 230 USD went to cases later dropped as duplicates: the same
hourly alert as 12 cases with two parallel builds, the same request posted in two channels, the same thread opened
twice under different keys, the same ticket or PR picked up by two jobs.

Two layers, cheapest first:
1. same_work(): deterministic, zero tokens, inside db.create_item. A new work case that shares a thread, a CUS ticket,
   a PR, an alert monitor or its exact title with an open case is folded into that case (its source is appended, the
   text becomes an update) and the open case's id is returned, so the caller links to it.
2. semantic(): one Sonnet call with no tools (cents), before the first expensive stage (investigate / telemetry). It
   compares the case against the open cases' titles and problems and folds it when it is the same problem; the
   same problem as a case closed on its merits in the last hours (RULED_S: its investigation's drop or info, news in its thread that
   made it moot, or done) gets that verdict back instead.
"""
from __future__ import annotations

import re

from . import db, site
from .core import log, now

WORK = ("TASK", "INCIDENT", "IDEA")
_KEY = site.ticket_key("dedupe", site.ticket_digits("dedupe"))  # the tickets a case can BE (tickets.dedupe_prefixes), not every key it cites
TICKET = re.compile(rf"\b({_KEY})\b", site.ticket_flags("dedupe"))
PR = site.pr_link_re()
MONITOR = re.compile(r"\[(\w[\w-]*)\]\[(\w+)\]\s*([^\n/:]{6,80})")
STATE = re.compile(r"^\W*(?:re-?)?(?:triggered|warn(?:ing)?|recovered|alert)\b[^:]*:\s*", re.I)


# the prefixes the engine puts on titles; an English install also strips the Spanish ones it may have stored
_TITLE_PREFIX = site.t(r"^\((?:slack|audit|daily|sin respuesta|unanswered)\)\s*|^(?:patrol|alerta|alert|pedido repetido|repeated request):\s*",
                       r"^\((?:slack|audit|daily|sin respuesta)\)\s*|^(?:patrol|alerta|pedido repetido):\s*")


def _norm_title(t: str) -> str:
    t = re.sub(_TITLE_PREFIX, "", t.lower())
    t = re.sub(r"[^\w\s]", " ", t)  # numbers stay: TICKET-1355 and TICKET-1366 with the same words are different work
    return " ".join(w for w in t.split() if len(w) > 2 or w.isdigit())


def keys(title: str, thread_key: str, data: dict) -> dict[str, set[str]]:
    d = data or {}
    blob = " ".join(str(x) for x in (title, d.get("text") or "", d.get("summary") or "", d.get("jira") or ""))
    out: dict[str, set[str]] = {"thread": set(), "ticket": set(), "pr": set(), "monitor": set(), "title": set()}
    ch, ts = d.get("channel"), d.get("thread_ts") or d.get("ts")
    if ch and ts:
        out["thread"].add(f"{ch}:{ts}")
    m = re.match(r"slack:([A-Z0-9]+):(\d+\.\d+)$", thread_key or "")
    if m:
        out["thread"].add(f"{m.group(1)}:{m.group(2)}")
    for a in d.get("also_sources", []) or []:
        if a.get("channel") and a.get("ts"):
            out["thread"].add(f"{a['channel']}:{a['ts']}")
    # a case's ticket is its own key, not every ticket its text mentions (2026-10-06: TICKET-1385's description cites
    # TICKET-1411, and its new case was folded into TICKET-1411's): the Jira key, the title, or the only key in the text
    if d.get("jira"):
        out["ticket"] = {str(d["jira"]).upper()}
    else:
        in_title = {t.upper() for t in TICKET.findall(title)}
        in_text = {t.upper() for t in TICKET.findall(blob)}
        out["ticket"] = in_title or (in_text if len(in_text) == 1 else set())
    out["pr"] = {f"{r}#{n}" for r, n in PR.findall(blob)}
    for svc, env, name in MONITOR.findall(STATE.sub("", blob)):
        out["monitor"].add(f"{svc}|{env}|{_norm_title(name)}")
    nt = _norm_title(title)
    if len(nt.split()) >= 3:
        out["title"].add(nt)
    return out


def same_work(kind: str, title: str, thread_key: str, data: dict) -> tuple[dict, str] | None:
    """The open case doing this work already, and why, or None."""
    if kind not in WORK or (thread_key or "").startswith("selftest:"):  # selftest builds parallel cases on purpose
        return None
    mine = keys(title, thread_key, data)
    if not any(mine.values()):
        return None
    for r in db.q(f"SELECT id FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN {db.TERMINAL} "
                  "AND created_at > ? ORDER BY id DESC LIMIT 300", now() - 21 * 86400):
        if r["id"] == data.get("parent"):
            # a verification child carries its parent's thread and title by design: folded, the case became its own
            # child and waited on itself (#337, #421, #503, #589, #620; 'its answer is verified by #589' on 10-11)
            continue
        it = db.item(r["id"])
        theirs = keys(it["title"], it["thread_key"], it["data"])
        for kind_ in ("thread", "ticket", "monitor", "pr", "title"):
            hit = mine[kind_] & theirs[kind_]
            if hit and not (kind_ == "title" and now() - it["created_at"] > 3 * 86400):
                return it, site.t(f"same {kind_}: {sorted(hit)[0]}", f"mismo {kind_}: {sorted(hit)[0]}")
    return None


def fold(existing: dict, title: str, thread_key: str, data: dict, why: str) -> None:
    d = data or {}
    if d.get("from_escalation") == existing["id"]:
        # a finding the escalation filed that is its own case restated: the escalation's verdict already acted on that
        # case. Folded as news it was judged 'changes_task' and reran the whole pipeline (2026-10-14: #774 from #636's
        # escalation, 'noticia en el hilo (alguien): ajustar', a second fix cycle and a second ask)
        log("dedupe: escalation finding is its own case, not news", item=existing["id"], title=title[:80])
        return
    also = existing["data"].get("also_sources", []) or []
    new_src = False
    if d.get("channel") and (d.get("thread_ts") or d.get("ts")):
        src = {"channel": d["channel"], "ts": d.get("thread_ts") or d["ts"], "why": why}
        if not any(a.get("channel") == src["channel"] and a.get("ts") == src["ts"] for a in also):
            also.append(src)
            new_src = True
    new_tickets = keys(title, thread_key, d)["ticket"] - keys(existing["title"], existing["thread_key"], existing["data"])["ticket"]
    unowned = new_tickets - {t for a in also for t in a.get("tickets") or []}
    if unowned:  # the open case owns the folded ticket: closing.py closes it with that case's PR (2026-10-06:
        # TICKET-1368's Jira case folded into #228, a Slack case, and left nothing that would ever close the ticket)
        also.append({"tickets": sorted(unowned), "why": why})
    text =(d.get("text") or d.get("summary") or title or "")[:1200]
    ups = existing["data"].get("updates", []) + ([{"text": site.t(f"(same work, {why}) {text}", f"(mismo trabajo, {why}) {text}")}] if text else [])
    extra = {}
    if d.get("instructions"):  # his directive on the folded case binds the open one
        extra["instructions"] = ((existing["data"].get("instructions") or "") + "\n" + d["instructions"]).strip()
    db.update_item(existing["id"], data={"also_sources": also[-30:], "updates": ups[-20:], **extra},
                   note=site.t(f"takes in a new case: {why}", f"absorbe un caso nuevo: {why}")[:200])
    log("dedupe: folded into an open case", item=existing["id"], why=why, title=title[:80], key=thread_key)
    # what was folded can change the open case ("ya estoy trabajando en esa alerta"): its worker hears it
    #
    # a case parked on someone never reads its updates, so a new report must reach the user even when the judge calls it
    # only info (2026-10-01: TICKET-1385, a second school hit by #24's week-old prod 500, folded silently while
    # schools#945 sat green without approval). Alert re-triggers and same-thread folds are not new reports.
    report = (existing["status"] in PARKED and bool(new_tickets or new_src)
              and not why.startswith(("mismo monitor", "mismo thread", "same monitor", "same thread")))
    if text and existing["status"] not in db.TERMINAL:
        try:
            from . import impact
            impact.news(existing["id"], d.get("author") or "", text, d.get("channel") or "", report=report)
        except Exception as e:
            log("dedupe: impact news failed", item=existing["id"], err=str(e)[:120])


RULED_S = 12 * 3600
# drop notes that close a case on its merits; impact.py writes the last one in the install's language
RULINGS = ("dossier: drop", "dossier: info", "frenado por noticia", "stopped by news")
PARKED = ("awaiting_human", "awaiting_approval", "waiting", "blocked", "ci_wait", "announce_check", "propose_check")


SEMANTIC = {"type": "object", "required": ["same_as", "why"], "properties": {
    "same_as": {"type": "integer", "description": "id of the case that is the SAME problem (same root cause or the same request), or 0"},
    "why": {"type": "string"}}}


def semantic(it: dict) -> int | None:
    """Before the first expensive stage: is this the same problem as an open case? Folds it and returns that id."""
    if it["kind"] not in WORK or it["data"].get("dedupe_checked") or it["data"].get("instructions") \
            or (it["thread_key"] or "").startswith("selftest:"):  # his explicit ask runs; selftest cases are parallel on purpose
        return None
    db.update_item(it["id"], data={"dedupe_checked": True})
    rows = db.q(f"SELECT id FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN {db.TERMINAL} "
                "AND id != ? AND created_at > ? ORDER BY id DESC LIMIT 40", it["id"], now() - 14 * 86400)
    # a case closed on its merits is a verdict, not a closed door: the same problem reported again within hours gets
    # that verdict back, not a second investigation. Its merits are the investigation's drop or info, or the news in its
    # thread that made it moot (2026-10-01: #403 'Feature "new_finance" is not in datafile' ran a 33 min investigation
    # to the same drop #394 reached 31 s before; 2026-10-02: a teammate's cross-post became #427 20 s after his own
    # thread had answered #426, and ran investigate, propose and an ask, $1.6, to the same stop). A verification child
    # is not a report but its parent's open questions, which no earlier verdict answered: ruled, its reply drafts blind
    #
    # A case done (shipped, answered) is closed on its merits too: open cases only, a teammate's message on the same
    # ticket 4 min after its PR merged was a fresh investigation
    ruled = [] if it["data"].get("parent") else db.q("SELECT t.item_id AS id, MAX(t.at), t.note, i.status FROM transitions t JOIN items i ON i.id=t.item_id WHERE "
                 "((t.dst='dropped' AND (" + " OR ".join("t.note LIKE ?" for _ in RULINGS) + ") AND i.status='dropped') "
                 "OR (t.dst='done' AND i.status='done')) "
                 "AND i.kind IN ('TASK','INCIDENT','IDEA') AND t.at > ? AND t.item_id != ? GROUP BY t.item_id "
                 "ORDER BY t.item_id DESC LIMIT 10", *(f"{p}%" for p in RULINGS), now() - RULED_S, it["id"])
    ruled_ids = {r["id"]: f"{r['status']}: {r['note'] or ''}" for r in ruled}
    if not rows and not ruled:
        return None
    cands = []
    for r in list(rows) + list(ruled):
        o = db.item(r["id"])
        state = f"closed: {ruled_ids[o['id']][:160]}" if o["id"] in ruled_ids else o["status"]
        cands.append(f"#{o['id']} [{o['kind']}, {state}, {_where(o)}] {o['title'][:100]} :: {_about(o)}")
    from . import claude
    from .core import VAR
    d = it["data"]
    # where and when: a request with no subject of its own is about what its channel was discussing minutes before
    # (2026-10-21: #1069 'Joaco puedes ir corriendo un primer diagnóstico?' 3 min after #1068's boletas post in the same
    # channel was read as 'no topic' and ran its own $3.5 investigation to the same incident)
    out = claude.lean("dupcase", f"NEW CASE [{_where(it)}]: {it['title']}\n{(d.get('text') or d.get('summary') or '')[:1500]}\n\n"
                                  "CASES:\n" + "\n".join(cands) + "\n\nIs the new case the same problem or the same request as one "
                                  "of these cases (same root cause, same school and symptom, or the same ask repeated)? A related "
                                  "but different bug is NOT the same, nor is a report that a done case's fix did not work or "
                                  "that the problem came back after it. A message that names no subject of its own (a bare 'can "
                                  "you take a look?') opened in the same channel minutes after a case is that case's request. "
                                  "Answer 0 when unsure.",
                      model="sonnet", cwd=str(VAR), schema=SEMANTIC, tools=(), item_id=it["id"], timeout=180)
    sid = int(out.get("same_as") or 0)
    if sid in ruled_ids:
        db.update_item(it["id"], status="dropped", note=site.t(f"already closed in #{sid}: {out['why'][:160]}", f"ya cerrado en #{sid}:{out['why'][:160]}"))
        return sid
    if not sid or sid == it["id"] or not db.item(sid) or db.item(sid)["status"] in db.TERMINAL:
        return None
    fold(db.item(sid), it["title"], it["thread_key"], d, site.t(f"same problem: {out['why'][:160]}", f"mismo problema: {out['why'][:160]}"))
    db.update_item(it["id"], status="dropped", note=site.t(f"duplicate of #{sid}: {out['why'][:160]}", f"duplicado de #{sid}: {out['why'][:160]}"))
    if d.get("parent"):
        _verified_by_case(db.item(d["parent"]), db.item(sid))
    return sid


def _about(o: dict) -> str:
    """What a case is about, as the duplicate check sees it. Before its investigation a case has only its trigger message,
    and a verification child's is its reply's ping: '@the team:eyes::rotating_light:' was all the check saw of #1067,
    so the same boletas outage reported in the squad channel 6 min later ran a second investigation that contradicted
    the first. The school and the checks it was spawned to verify say what it is."""
    d = o["data"]
    prob = (d.get("dossier") or {}).get("problem")
    if prob:
        return prob[:300]
    parts = [(d.get("summary") or d.get("text") or "")[:300]]
    if d.get("school"):
        parts.append(f"school: {d['school']}")
    if d.get("checks"):
        parts.append("verifies: " + "; ".join(c[:150] for c in d["checks"][:3]))
    return " | ".join(p for p in parts if p)


def _where(o: dict) -> str:
    mins = max(0, int((now() - (o["created_at"] or now())) / 60))
    return f"channel {o['data'].get('channel') or '-'}, opened {mins} min ago"


def _verified_by_case(parent: dict | None, case: dict) -> None:
    """A verification child folded into an open case: that case is the parent reply's verification. Its parent used to
    hear nothing, saw its child ended and drafted blind."""
    if not parent or parent["kind"] != "REPLY" or parent["status"] not in ("awaiting_approval", "awaiting_human", "reply_draft"):
        return
    same = (f"The same problem is already case #{case['id']} ({case['kind']}, {case['status']}): {case['title'][:160]}. "
            "One problem gets one answer: the reply must agree with that case's findings and direction, never call the "
            "behavior expected or promise a separate fix.")
    dossier = dict(case["data"].get("dossier") or {}) or {
        "problem": case["title"], "root_cause_status": "unknown", "recommendation": "ask", "questions": [],
        "evidence": "That case is still being investigated: the reply acknowledges and says it is being looked at, it states no cause."}
    dossier["same_case"] = same
    db.update_item(parent["id"], status="reply_draft", attempts=0, next_run_at=0, note=site.t(f"redraft: case #{case['id']} verifies it", f"redraft: lo verifica el caso #{case['id']}"),
                   data={"dossier": dossier, "verified": True, "verified_at": now(), "child": None})
