"""Slack as the primary source of truth.

A rolling situational digest of the last days of his channels, DMs and HIS OWN messages,
maintained by Muse (free) and refreshed hourly or when enough new messages arrive. It feeds:
  - triage (ownership, "already handled", what matters now)
  - prioritize(): re-ranks every open item's urgency against current priorities
  - patrol (areas people are complaining about first)
  - investigate / plan / critique / drafts (decisions and constraints stated in Slack)
Precedence: a recent Slack decision beats a PRD, a code comment or an older message; when
sources conflict the digest records the conflict instead of picking silently.
"""
from __future__ import annotations

import json
import re
import time

from . import db, muse, site, slack
from .core import CFG, VAR, log, now

CTX_FILE = VAR / "slack-context.md"
DAYS = 5
SCHEMA = {"type": "object", "required": ["summary", "priorities", "active_work", "decisions", "joaquin_focus",
                                          "ownership", "incidents", "conflicts"], "properties": {
    "summary": {"type": "string"},
    "priorities": {"type": "array", "items": {"type": "string"}},
    "active_work": {"type": "array", "items": {"type": "object", "properties": {
        "who": {"type": "string"}, "what": {"type": "string"}, "refs": {"type": "array", "items": {"type": "string"}}}}},
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "decision": {"type": "string"}, "by": {"type": "string"}, "when": {"type": "string"}, "ref": {"type": "string"}}}},
    "joaquin_focus": {"type": "array", "items": {"type": "string"}},
    "ownership": {"type": "array", "items": {"type": "object", "properties": {
        "topic": {"type": "string"}, "owner": {"type": "string"}}}},
    "incidents": {"type": "array", "items": {"type": "string"}},
    "deadlines": {"type": "array", "items": {"type": "string"}},
    "conflicts": {"type": "array", "items": {"type": "string"}},
    "patrol_hints": {"type": "array", "items": {"type": "string"}}}}


def _corpus() -> str:
    oldest = f"{max(time.time() - DAYS * 86400, site.intake_since()):.6f}"
    alerts, mine = CFG.get("alerts_channel"), f" ({site.marker()})"
    chans = {c: slack.channel_name(c).lstrip("#")
             for c in (CFG.get("team_channel"), CFG.get("pr_channel"), alerts, *CFG.get("watched_channels", [])) if c}
    out = []
    for ch, name in chans.items():
        try:
            msgs = slack.history(ch, oldest, pages=4)
        except Exception as e:
            log("context: history failed", ch=ch, err=str(e)[:120])
            continue
        lines = []
        for m in msgs[-250:]:
            if ch == alerts and m.get("bot_id"):
                lines.append(f"[{time.strftime('%m-%d %H:%M', time.localtime(float(m['ts'])))}] ALERT {(m.get('text') or '')[:160]}")
                continue
            if not slack.is_human(m):
                continue
            who = slack.user_name(m.get("user"))
            tag = mine if m.get("user") == CFG["self_user_id"] else ""
            lines.append(f"[{time.strftime('%m-%d %H:%M', time.localtime(float(m['ts'])))}] {who}{tag}: "
                         f"{slack.render(m.get('text'))[:700]}" + (f"  (+{m['reply_count']} replies)" if m.get("reply_count") else ""))
        if lines:
            out.append(f"## #{name}\n" + "\n".join(lines[-200:]))
    # thread replies and DMs already ingested (they carry the conversations aimed at him)
    rows = db.q("SELECT source, channel, user, text, ts FROM events WHERE created_at>? AND source IN "
                "('slack-dm','slack-thread','slack-mention') ORDER BY ts", now() - DAYS * 86400)
    if rows:
        out.append("## DMs, threads and mentions\n" + "\n".join(
            f"[{time.strftime('%m-%d %H:%M', time.localtime(float(r['ts'])))}] {slack.user_name(r['user'])}"
            f"{mine if r['user'] == CFG['self_user_id'] else ''}: {slack.render(r['text'])[:500]}" for r in rows[-300:]))
    return "\n\n".join(out)[-80000:]  # 2026-09-25: 581k-token prompts ran for hours and returned nothing


def refresh(force: bool = False) -> dict | None:
    new_events = db.q1("SELECT count(*) c FROM events WHERE created_at>?", db.kv_get("context_at", 0))["c"]
    if not force and now() - db.kv_get("context_at", 0) < 7200 and new_events < 60:
        return None
    if not force and now() < db.kv_get("context_backoff_until", 0):
        return None
    corpus = _corpus()
    prev = db.kv_get("slack_context")
    if prev:  # carry the previous picture forward: the corpus is only the recent days
        corpus = "## Previous picture (built " + time.strftime("%m-%d %H:%M", time.localtime(prev.get("built_at", 0))) + \
                 "; update it, drop what is stale)\n" + json.dumps({k: prev.get(k) for k in ("summary", "priorities", "joaquin_focus", "incidents", "deadlines")}, ensure_ascii=False)[:12000] + "\n\n" + corpus
    me = site.first_name()
    prompt = (f"Build {me}'s situational picture from the last days of his Slack (he is on the {site.of_team('squad')}; "
              f"his own messages are marked {site.marker()}). Slack is the primary source of truth: extract what the squad "
              "is prioritizing now, who is working on what (with PR/ticket refs), decisions made (who, when), what "
              f"{me} himself is focused on or promised, ownership of topics, live incidents, deadlines, and any "
              "conflict between sources (a later message contradicting an earlier one, Slack vs a PRD). "
              f"patrol_hints: {site.of_team('code areas')} people are complaining about or reporting bugs in. Be concrete and "
              "cite refs; no filler.\n\n" + corpus)
    try:
        ctx = muse.call("ap-triage", prompt, cwd=site.primary_path(), schema=SCHEMA, lane="context", timeout=600)
    except Exception as e:
        # the picture is orientation, never a deliverable: back off quietly, keep the previous one
        db.kv_set("context_backoff_until", now() + 3 * 3600)
        log("context refresh failed, backing off 3h", err=str(e)[:200])
        return None
    ctx["built_at"] = now()
    db.kv_set("slack_context", ctx)
    db.kv_set("context_at", now())
    CTX_FILE.write_text(render(ctx))
    log("context refreshed", priorities=len(ctx["priorities"]), work=len(ctx["active_work"]))
    return ctx


def render(ctx: dict | None = None, limit: int = 6000) -> str:
    ctx = ctx or db.kv_get("slack_context")
    if not ctx:
        return ""
    parts = [f"# Current Slack context (built {time.strftime('%m-%d %H:%M', time.localtime(ctx.get('built_at', 0)))}; "
             "orientation on priorities and ownership only; facts come from the raw thread, the code mirrors and telemetry)", ctx["summary"]]
    for key, title in (("priorities", "Priorities now"), ("joaquin_focus", f"{site.first_name()}'s focus / promises"),
                       ("incidents", "Live incidents"), ("deadlines", "Deadlines"), ("conflicts", "Conflicts")):
        if ctx.get(key):
            parts.append(f"## {title}\n" + "\n".join(f"- {x}" for x in ctx[key]))
    if ctx.get("active_work"):
        parts.append("## Who is on what\n" + "\n".join(
            f"- {w.get('who')}: {w.get('what')} {' '.join(w.get('refs') or [])}" for w in ctx["active_work"]))
    if ctx.get("decisions"):
        parts.append("## Decisions\n" + "\n".join(
            f"- {d.get('decision')} ({d.get('by')}, {d.get('when')})" for d in ctx["decisions"]))
    if ctx.get("ownership"):
        parts.append("## Ownership\n" + "\n".join(f"- {o.get('topic')}: {o.get('owner')}" for o in ctx["ownership"]))
    return "\n\n".join(parts)[:limit]


MISSING = {"type": "array", "items": {"type": "object", "required": ["kind", "title", "why", "urgency", "in_fence"],
    "properties": {"kind": {"type": "string", "enum": ["TASK", "INCIDENT", "OPEN_LOOP", "IDEA", "REPLY", "REVIEW"],
                            "description": f"REVIEW: someone asks {site.first_name()} or the squad to review a PR "
                                           "(name it as repo#number)"},
                   "title": {"type": "string"}, "why": {"type": "string"}, "ref": {"type": "string"},
                   "urgency": {"type": "integer"}, "in_fence": {"type": "boolean"},
                   "duplicate_of": {"type": ["integer", "null"]}}}}


def _slack_ref(ref: str):
    """(channel, message ts, thread root ts) from a permalink; the root is the message itself
    when it is not a reply."""
    import re
    m = re.search(r"archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?thread_ts=(\d+\.\d+))?", ref or "")
    if not m:
        return (None, None, None)
    ts = f"{m.group(2)}.{m.group(3)}"
    return (m.group(1), ts, m.group(4) or ts)


PR_REF = re.compile(rf"\b({site.repo_names_re()})(?:#|/pull/)(\d+)")
REVIEW_ASK = re.compile(r"^\W*(revis\w*|review)\b", re.I)


def create_missing(missing: list[dict], source: str) -> int:
    """Evidenced gaps -> items, deduped by title and by recent history."""
    import hashlib
    made = 0
    for m in missing:
        if not m.get("in_fence") or m.get("duplicate_of"):
            continue
        ch, ts, root = _slack_ref(m.get("ref", ""))
        # no Slack evidence, no work item; the self-DM
        # holds the harness's own reports, and what they report on is already a case
        if not ch or ch == CFG["self_dm_channel"]:
            continue
        # the ref is the model's copy of a [channel:ts]: read the message it names before it becomes a case. A garbled
        # ts made a case nobody could read (2026-10-14: backfill cited a teammate's ask as...137.676179, the thread is
        #...137.070939; #767 was vetted, drafted and given a verify child, 0.9 USD, before the child's precedent hunt
        # found it answered on 05/10). The only message in that second is the one meant; none or several, no case
        msg = slack.fetch_message(ch, ts)
        if not msg:
            near = slack.in_second(ch, ts, root)
            msg = near[0] if len(near) == 1 else None
            log("missing: ref names no message" if not msg else "missing: ref repaired", ref=m.get("ref"),
                found=msg and msg["ts"], title=m["title"][:80])
            if not msg:
                continue
        ts, root = msg["ts"], msg.get("thread_ts") or msg["ts"]
        if site.backlog(ts):
            continue  # asked before this install started: not work it was asked to take on (site.intake_since)
        key = f"slack:{ch}:{root}"  # same key ingest/triage use for that conversation: one item per thread, any source
        if db.open_item_for_thread(key) or db.q1("SELECT 1 FROM items WHERE thread_key=? AND created_at>?", key, now() - 7 * 86400):
            continue
        if db.similar_recent_item(m["title"]):
            continue
        pr = PR_REF.search(f"{m['title']} {m['why']}")
        if pr and (m["kind"] == "REVIEW" or REVIEW_ASK.match(m["title"])):
            # a review ask goes where triage sends it: the review lane reads the PR first, and a merged or closed PR ends
            # there in seconds. As a TASK it paid vet, telemetry and an investigation to learn
            # the same
            rkey = f"review:{pr.group(1)}#{pr.group(2)}"
            if not (db.open_item_for_thread(rkey) or db.q1("SELECT 1 FROM items WHERE thread_key=? AND created_at>?",
                                                            rkey, now() - 7 * 86400)):
                db.create_item("REVIEW", "review_prepare", m["title"][:90], rkey, repo=pr.group(1), pr=int(pr.group(2)),
                               urgency=max(2, min(3, int(m["urgency"]))),
                               data={"summary": m["why"], "from": source, "channel": ch, "ts": ts, "thread_ts": root})
                made += 1
            continue
        if m["kind"] == "REVIEW":  # no findable PR: as in triage, answer or ask for the link
            m = {**m, "kind": "REPLY"}
        status = "vet"  # a frontier model reads the raw thread before this becomes work
        data = {"summary": m["why"], "text": f"{m['title']}\n{m['why']}\nref: {m.get('ref', '')}",
                "proposal": m["kind"] == "IDEA", "from": source}
        data.update(channel=ch, ts=ts, thread_ts=root)
        db.create_item(m["kind"], status, m["title"][:90], key,
                       urgency=max(0, min(3, int(m["urgency"]))), data=data)
        made += 1
    return made


PRIO = {"type": "object", "required": ["items", "missing"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["id", "urgency", "why"], "properties": {
        "id": {"type": "integer"}, "urgency": {"type": "integer"}, "why": {"type": "string"},
        "drop": {"type": "boolean"}}}},
    "missing": MISSING}}


def prioritize() -> int:
    """Re-rank open items against the Slack context. Urgency 0-3; drop only what Slack shows
    is moot (someone else took it, already solved, decided against)."""
    ctx = render()
    if not ctx:
        return 0
    rows = db.q("SELECT id, kind, status, title, repo, pr, urgency, json_extract(data, '$.source_event') AS ev FROM items "
                "WHERE status NOT IN ('done','dropped','expired','failed','awaiting_approval') "
                "AND kind IN ('REVIEW','TASK','INCIDENT','IDEA','REPLY')")
    # the same picture over the same cases ranks the same: the job ran every 15 min while refresh rebuilds the picture
    # every 2 h or 60 events. On the same picture only
    # a case it never ranked counts: a case closing changes nothing, and triage ranked the cases it opened against this
    # picture
    pic = (db.kv_get("slack_context") or {}).get("built_at")
    last = db.kv_get("prioritized_on") or [None, []]
    ranked = set(last[1]) if last[0] == pic else set()
    if not [r for r in rows if r["id"] not in ranked and not (ranked and r["ev"])]:
        return 0
    seen = [pic, sorted(ranked | {r["id"] for r in rows})]
    listing = "\n".join(f"#{r['id']} {r['kind']} [{r['status']}] u{r['urgency']} {r['title']}"
                        + (f" ({r['repo']}#{r['pr']})" if r["pr"] else "") for r in rows)
    me = site.first_name()
    out = muse.call("ap-triage", f"{ctx}\n\nOpen work items of {me}'s assistant:\n{listing}\n\nAssign each an "
                    f"urgency 0-3 from the Slack context (3 = someone is blocked on {me} / prod incident in the fence / "
                    "deadline today; 2 = squad priority or a teammate waiting; 1 = useful, nobody waiting; 0 = chores and "
                    "noise). drop=true only if Slack shows it is moot (someone else took it, solved, decided against), "
                    f"with the reason. Titles in plain {site.own('Spanish', site.language_name())} (they are shown to {me} as-is). If a gap is the same work as one of "
                    "the open items listed above (same PR, same thread, same promise, even worded differently), set duplicate_of to "
                    f"that item id instead of describing it again.\n\nmissing: work the Slack context shows {me} (or his assistant) should be doing "
                    f"that NO open item covers: an in-fence prod incident nobody owns, a promise {me} made that is still "
                    "open (OPEN_LOOP), a teammate's direct ask to him. Only concrete, evidenced gaps; empty is fine. Never "
                    "out-of-fence incidents.", cwd=site.primary_path(), schema=PRIO, lane="prioritize")
    ids = {r["id"]: r for r in rows}
    n = 0
    for x in out["items"]:
        r = ids.get(x["id"])
        if not r:
            continue
        if x.get("drop") and r["status"] in ("review_prepare", "investigate", "reply_draft"):
            db.update_item(r["id"], status="dropped", note=f"slack context: {x['why'][:200]}")
            db.add_signal("dropped_by_context", {"item": r["id"], "why": x["why"]}, r["id"])
        elif int(x["urgency"]) != r["urgency"]:
            db.update_item(r["id"], urgency=max(0, min(3, int(x["urgency"]))), note=f"priority: {x['why'][:200]}")
        n += 1
    made = create_missing(out.get("missing") or [], "ctx")
    db.kv_set("prioritized_on", seen)
    log("prioritized", items=n, created=made)
    return n + made
