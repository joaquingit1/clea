"""Triage: Muse classifies new events in batches; code turns verdicts into items.

Recall safety: an IGNORE/INFO verdict on a message from the squad or aimed at the user
gets a second, independent Muse opinion (free). Disagreement escalates to REPLY at low
urgency; the daily Opus noop-audit reviews what remains ignored.
"""
from __future__ import annotations

import json
import re

from . import context, db, muse, site, slack
from .core import CFG, HOME, log, now

CLASSES = ["TASK", "REVIEW", "REPLY", "INCIDENT", "OPEN_LOOP", "IDEA", "INFO", "IGNORE"]
SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object",
    "required": ["event_id", "class", "confidence", "needs_joaquin", "urgency", "title", "summary", "reason"],
    "properties": {
        "event_id": {"type": "integer"}, "class": {"type": "string", "enum": CLASSES},
        "confidence": {"type": "number"}, "needs_joaquin": {"type": "boolean"},
        "urgency": {"type": "integer"}, "title": {"type": "string"}, "summary": {"type": "string"},
        "repo_hint": {"type": ["string", "null"]}, "pr_refs": {"type": "array", "items": {"type": "string"}},
        "duplicate_of": {"type": ["integer", "null"]}, "duplicate_of_event": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
        "demand_key": {"type": ["string", "null"]}}}}}}
START_STATUS = {"TASK": "investigate", "INCIDENT": "investigate", "IDEA": "investigate",
                "REVIEW": "review_prepare", "REPLY": "reply_draft", "OPEN_LOOP": "loop_track"}
PR_REF = re.compile(rf"({site.repo_names_re()})#(\d+)")
SELF = CFG["self_user_id"]
TEAM = set(CFG.get("team_members", {}).keys()) - {"_note"}


def _thread_key(ev) -> str:
    return f"slack:{ev['channel']}:{ev['thread_ts'] or ev['ts']}"


def _attachments(p: dict) -> str:
    meta = p.get("files_meta") or [{"name": n} for n in p.get("files") or [] if n]
    bits = [f"{f.get('name') or 'archivo'} ({f.get('filetype') or f.get('mimetype') or '?'})" for f in meta]
    bits += [f"imagen inline ({'gif' if str(u).lower().split('?')[0].endswith('.gif') else 'imagen'})" for u in p.get("media") or []]
    return f"attachments: {', '.join(bits)}\n" if bits else ""


RELEVANCE_SCHEMA = {"type": "object", "properties": {"relevant": {"type": "boolean"}, "reason": {"type": "string"}},
                    "required": ["relevant", "reason"]}


def _academic_channel(ch: str) -> bool:
    name, by_name = (CFG.get("channel_names") or {}).get(ch, ""), site.channel_name_re()
    return (ch in (CFG.get("team_channel"), CFG.get("pr_channel")) or ch in (CFG.get("watched_channels") or [])
            or bool(by_name and by_name.search(name)))


def team_review(ev, repo: str, n: int) -> tuple[bool, str]:
    """A review ask found only by the 'the team' word search is checked before it becomes a case. Squads review their members' PRs and what
    directly and specifically changes their domain: a roster author or a request to him by name passes at zero
    tokens; anything else asks Haiku 5.5. A failed check does not review (the ask was never aimed at him)."""
    text = ev["text"] or ""
    tags = [f"<@{SELF}>"] + [f"<!subteam^{s}" for s in CFG.get("team_subteam_tags", [])]
    if ev["source"] != "slack-mention" or any(t in text for t in tags) or ev["user"] in TEAM or _academic_channel(ev["channel"] or ""):
        return True, "aimed at him or the squad"
    from . import claude, gh
    try:
        pr = gh.pr_view(repo, n)
    except Exception as e:
        return False, f"could not read the PR: {str(e)[:120]}"
    author = (pr.get("author") or {}).get("login", "")
    if author in CFG.get("github_team_handles", []):
        return True, f"{author} is on the squad"
    if any((r.get("login") or "") == CFG["self_github"] for r in pr.get("reviewRequests") or []):
        return True, "his review was requested on GitHub"
    files = "\n".join(f["path"] for f in (pr.get("files") or [])[:80])
    name = (CFG.get("channel_names") or {}).get(ev["channel"], ev["channel"])
    prompt = (f"Squad roster (GitHub): {', '.join(CFG.get('github_team_handles', []))}\n\nSlack message in {name}:\n{text[:2000]}\n\n"
              f"PR {repo}#{n} by {author}: {pr.get('title')}\n\nDescription:\n{(pr.get('body') or '')[:2500]}\n\n"
              f"Changed files ({len(pr.get('files') or [])}):\n{files}")
    try:
        out = claude.lean("review_relevance", prompt, model="haiku5", cwd=str(HOME), schema=RELEVANCE_SCHEMA, tools=(), timeout=120)
    except Exception as e:
        return False, f"relevance check failed: {str(e)[:120]}"
    return bool(out.get("relevant")), out.get("reason") or ""


def _render_batch(evs) -> str:
    """Each event with its WHOLE thread (root included, also for a root that already has replies) and the
    attachment metadata, fetched before the model sees anything."""
    out, contexts, shown = [], {}, set()
    for ev in evs:
        tk = _thread_key(ev)
        p = json.loads(ev["payload"] or "{}")
        root = ev["thread_ts"] or (ev["ts"] if p.get("reply_count") else "")
        if root and tk not in contexts:
            try:
                contexts[tk] = "\n".join("  " + l for l in slack.thread_text(ev["channel"], root, max_chars=12000).splitlines())
            except Exception:
                contexts[tk] = "  (thread unavailable)"
        who = slack.user_name(ev["user"]) if ev["user"].startswith("U") else f"bot:{ev['user']}"
        tags = [t for t, on in (("SQUAD", ev["user"] in TEAM), (f"{site.marker()}-HIMSELF", ev["user"] == SELF),
                                (f"MENTIONS-{site.marker()}", f"<@{SELF}>" in (ev["text"] or ""))) if on]
        out.append(f"### event_id={ev['id']} source={ev['source']} channel={p.get('channel_name') or ev['channel']} "
                   f"kind={p.get('channel_kind')} author={who} {' '.join(tags)}\n"
                   f"thread={'yes' if root else 'no'} ts={ev['ts']}\n" + _attachments(p) +
                   f"<message>\n{slack.render(ev['text'])[:3000]}\n</message>")
        if root and tk in shown:
            out.append("<thread_context>(same thread as an event above)</thread_context>")
        elif root:
            shown.add(tk)
            out.append(f"<thread_context>\n{contexts[tk]}\n</thread_context>")
    return "\n\n".join(out)


def _second_opinion(ev, first: dict) -> bool | None:
    """True if an independent pass thinks the ignored event does need the user; None when the
    check could not run (the event stays untriaged and is retried, instead of a false REPLY)."""
    schema = {"type": "object", "required": ["needs_joaquin", "reason"],
              "properties": {"needs_joaquin": {"type": "boolean"}, "reason": {"type": "string"}}}
    prompt = (f"Independent check. Does this Slack message need {site.first_name()} to act or answer (a question to him, a "
              "request to his squad, a bug in his domain, someone waiting on him)? Social acks, thanks and "
              "out-of-domain chatter do not.\n\n" + _render_batch([ev]))
    try:
        out = muse.call("ap-triage", prompt, cwd=site.primary_path(), schema=schema, lane="triage2")
        return bool(out["needs_joaquin"])
    except Exception as e:
        log("triage second opinion failed", ev=ev["id"], err=str(e)[:200])
        return None


_PROBLEM_KEY = site.ticket_key("problem", r"\d+")
PROBLEM = re.compile(r"no (se |me |les |nos )?(pued|deja|funciona|carga|aparec|muestra|guarda)|error|falla|fall[oó]|bug|"
                     rf"roto|se cae|500|{_PROBLEM_KEY}", re.I)
FENCE = site.fence_re("text_re")  # None: the install has no fence, every message it watches is in scope


def _demand(ev, key: str, r: dict) -> None:
    """v1 demand ledger: the second ask
    for the same capability within 7 days becomes a proposal to the team channel."""
    key = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:60]
    if not key:
        return
    led = db.kv_get(f"demand:{key}", {"asks": []})
    if any(a["event"] == ev["id"] for a in led["asks"]):
        return
    led["asks"] = [a for a in led["asks"] if a["at"] > now() - 7 * 86400] + [
        {"event": ev["id"], "at": now(), "channel": ev["channel"], "ts": ev["ts"], "who": ev["user"],
         "text": slack.render(ev["text"])[:300], "summary": r.get("summary", "")[:200]}]
    if len(led["asks"]) >= 2 and not led.get("surfaced_at") and not db.open_item_for_thread(f"demand:{key}"):
        db.create_item("IDEA", "investigate", site.t(f"repeated request: {key}", f"pedido repetido: {key}"), f"demand:{key}", urgency=2,
                       data={"channel": ev["channel"], "ts": ev["ts"], "thread_ts": ev["thread_ts"] or ev["ts"],
                             "text": slack.render(ev["text"])[:4000], "author": ev["user"], "proposal": True,
                             "summary": site.t(f"Second time in 7 days this request comes in: {key}",
                                               f"Segunda vez en 7 días que llega este pedido: {key}"),
                             "demand": {"key": key, "asks": led["asks"]}})
        led["surfaced_at"] = now()
        log("triage: demand pattern surfaced", key=key, n=len(led["asks"]))
    db.kv_set(f"demand:{key}", led)


def run_batch(limit: int = 25) -> int:
    from . import alerts
    for ev in db.q("SELECT * FROM events WHERE triaged=0 AND source='slack-alerts' ORDER BY created_at LIMIT 50"):
        try:
            alerts.ingest(ev)  # deterministic ledger, no model
        except Exception as e:
            log("alerts ledger failed", event=ev["id"], err=str(e)[:200])
            db.x("UPDATE events SET triaged=1 WHERE id=?", ev["id"])
    # deterministic prefilter before any model: gifs, emoji, acks, greetings, bot noise and his own non-commitment
    # messages never reach the triage model
    from . import prefilter
    for ev in db.q("SELECT * FROM events WHERE triaged=0 AND source!='slack-alerts' ORDER BY created_at LIMIT 300"):
        reason = prefilter.from_event(ev, SELF)
        if reason:
            prefilter.drop_event(ev["id"], reason)
    # belt and braces for the ingest-time fast path: anything aimed at the user that is still untriaged
    # goes to the reply lane now, without a model
    from . import ingest
    for ev in db.q("SELECT * FROM events WHERE triaged=0 AND user!=? ORDER BY created_at LIMIT 50", SELF):
        payload = json.loads(ev["payload"] or "{}")
        extra = {"channel_kind": payload.get("channel_kind") or ("dm" if ev["channel"].startswith("D") else "")}
        msg = {"ts": ev["ts"], "user": ev["user"], "text": ev["text"], "thread_ts": ev["thread_ts"] or None}
        if ev["user"] and not ev["user"].startswith("B") and ingest._aimed(ev["channel"], msg, extra):
            try:
                ingest._fast_reply(ev["key"], ev["channel"], msg, extra)
                prefilter.count("fast_lane")
                log("triage: aimed event routed to the reply lane", event=ev["id"])
            except Exception as e:
                log("triage: fast reply failed", event=ev["id"], err=str(e)[:200])
    evs = db.q("SELECT * FROM events WHERE triaged=0 ORDER BY created_at LIMIT ?", limit)
    if not evs:
        return 0
    ctx = context.render(limit=5000)
    open_items = "\n".join(f"#{r['id']} {r['kind']} [{r['status']}] {r['title'][:100]}" for r in db.q(
        "SELECT id, kind, status, title FROM items WHERE status NOT IN ('done','dropped','expired','failed') "
        "AND kind IN ('TASK','INCIDENT','IDEA','REPLY','OPEN_LOOP') ORDER BY id DESC LIMIT 60"))
    prompt = ((ctx + "\n\n" if ctx else "") + "Classify each event below. One result per event_id. Use the Slack "
              "context for ownership, what is already handled and what matters now. If an event is about the SAME "
              "problem or request as an open item below (e.g. another alert of the same incident, a follow-up "
              "message), set duplicate_of to that item id instead of starting new work. If it is the same request or "
              "commitment as an EARLIER event in this batch (one message split in two, an add-on), set "
              "duplicate_of_event to that event_id.\n\nOpen items:\n"
              + (open_items or "(none)") + "\n\n" + _render_batch(evs))
    try:
        res = muse.call("ap-triage", prompt, cwd=site.primary_path(), schema=SCHEMA, lane="triage")
    except muse.MuseRateLimited:
        raise
    except Exception as e:
        log("triage failed", err=str(e)[:300])
        raise  # the job health check must see a triage that keeps failing
    prefilter.count("model_triaged", n=len(evs))
    by_id = {r["event_id"]: r for r in res["results"]}
    made, case_of = 0, {}  # event id -> the case it became or joined in this batch
    for ev in evs:
        r = by_id.get(ev["id"])
        if r is None:
            continue  # stays untriaged, retried next batch
        cls = r["class"]
        if cls == "REPLY" and ev["user"] == SELF:
            # his own message is never an ask to himself: what it can be is something he is waiting on (a question he
            # put to the channel), which is a loop to track. As a reply case, the draft answered him in his own name
            cls = "OPEN_LOOP"
        aimed = ev["user"] in TEAM or f"<@{SELF}>" in (ev["text"] or "")
        db.x("UPDATE events SET triaged=1 WHERE id=?", ev["id"])
        db.add_signal("triage", {"event_id": ev["id"], "class": cls, "first": r["class"], "conf": r["confidence"],
                                 "aimed": aimed, "reason": r["reason"][:200]})
        if r.get("demand_key") and ev["user"] != SELF:
            try:
                _demand(ev, r["demand_key"], r)
            except Exception as e:
                log("demand ledger failed", err=str(e)[:200])
        if cls not in START_STATUS:
            continue
        # an earlier event of this batch has no item id the model could name in duplicate_of yet (2026-10-15: his
        # promise split in two DM messages opened #828 and #829, the model's reason said "same commitment, merged")
        dup_id = r.get("duplicate_of") or case_of.get(r.get("duplicate_of_event"))
        dup = db.item(dup_id) if dup_id else None
        if dup and dup["status"] not in db.TERMINAL:
            ups = dup["data"].get("updates", []) + [{"event_id": ev["id"], "text": slack.render(ev["text"])[:1500]}]
            db.update_item(dup["id"], data={"updates": ups[-20:]})
            db.x("UPDATE events SET item_id=? WHERE id=?", dup["id"], ev["id"])
            case_of[ev["id"]] = dup["id"]
            log("triage: attached to existing item", item=dup["id"], event=ev["id"])
            continue
        tk = _thread_key(ev)
        if ev["channel"] and not ev["channel"].startswith("D"):
            from . import ingest
            ingest.watch_thread(ev["channel"], ev["thread_ts"] or ev["ts"], ev["ts"])  # follow replies to any case thread
        if cls == "REVIEW":
            refs = [PR_REF.search(x) for x in r.get("pr_refs") or []]
            refs = [m for m in refs if m]
            if not refs:
                blob = ev["text"] or ""
                if ev["thread_ts"]:
                    try:
                        blob += " ".join(m.get("text", "") for m in slack.replies(ev["channel"], ev["thread_ts"]))
                    except Exception:
                        pass
                m = site.pr_link_re().search(blob)
                refs = [m] if m else []
            if not refs:
                cls = "REPLY"  # a review ask without a findable PR: answer/ask for the link, never drop it
        if cls == "REVIEW":
            repo, n = refs[0].group(1), int(refs[0].group(2))
            tk = f"review:{repo}#{n}"
            if db.open_item_for_thread(tk):
                continue
            keep, why = team_review(ev, repo, n)
            if not keep:
                log("triage: review left to its squad", event=ev["id"], pr=f"{repo}#{n}", why=why[:200])
                db.add_signal("review_relevance", {"event_id": ev["id"], "pr": f"{repo}#{n}", "relevant": False, "why": why[:300]})
                continue
            iid = db.create_item("REVIEW", "review_prepare", r["title"], tk, repo=repo, pr=n, urgency=max(r["urgency"], 2),
                                 data={"source_event": ev["id"], "summary": r["summary"]})
        else:
            existing = db.open_item_for_thread(tk)
            if existing:
                ups = existing["data"].get("updates", []) + [{"event_id": ev["id"], "text": slack.render(ev["text"])[:1500]}]
                db.update_item(existing["id"], data={"updates": ups[-20:]}, next_run_in=0)
                if existing["status"] == "awaiting_human" and cls == "REPLY":
                    db.update_item(existing["id"], status="reply_draft", note="new message in thread")
                db.x("UPDATE events SET item_id=? WHERE id=?", existing["id"], ev["id"])
                case_of[ev["id"]] = existing["id"]
                continue
            if cls == "REPLY":
                # one conversation is one case here too: the thread's reply case that closed while the thread is still
                # alive starts a new round (ingest.continue_case) instead of a second case that rereads the thread
                from . import ingest
                prev = ingest.reply_case(tk)
                if prev and prev["kind"] == "REPLY":
                    ingest.continue_case(prev, ev["id"], ev["ts"], slack.render(ev["text"])[:1500], False)
                    db.x("UPDATE events SET item_id=? WHERE id=?", prev["id"], ev["id"])
                    case_of[ev["id"]] = prev["id"]
                    log("triage: the thread's closed reply case continues", item=prev["id"], event=ev["id"])
                    continue
            hint = r.get("repo_hint") if r.get("repo_hint") in CFG["repos"] else None
            iid = db.create_item(cls, START_STATUS[cls], r["title"], tk, repo=hint,
                                 urgency=r["urgency"],
                                 data={"source_event": ev["id"], "channel": ev["channel"], "ts": ev["ts"],
                                       "thread_ts": ev["thread_ts"] or ev["ts"], "summary": r["summary"],
                                       "text": slack.render(ev["text"])[:4000], "author": ev["user"],
                                       "proposal": cls == "IDEA"})
        db.x("UPDATE events SET item_id=? WHERE id=?", iid, ev["id"])
        case_of[ev["id"]] = iid
        made += 1
        log("triage: item", item=iid, cls=cls, title=r["title"][:80])
    return made
