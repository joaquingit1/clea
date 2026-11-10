"""TASK / INCIDENT / IDEA pipeline: from a report to a verified draft PR.

investigate (Muse) -> plan (Muse) -> critique (Opus) -> build (Fable, worktree)
  -> gate (code) -> self_review (Muse finders + CodeRabbit CLI -> Opus) -> [fix (Fable) -> gate]*
  -> publish (push + draft PR, code) -> ci_wait -> [ci_fix]* -> verify_done (Opus) -> awaiting_human
Multi-repo work runs part by part in plan order; later parts record merge dependencies
on earlier ones so merge_order never lands them out of order.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import approvals, claude, context, cover, db, finders, gates, gh, merge_order, muse, outbound, site, slack, worktree
from .core import CFG, V1, item_dir, log, now, run, slug

PRECEDENTS_FOUND = {"type": "array", "items": {"type": "object", "required": ["request", "resolution"], "properties": {
    "request": {"type": "string"}, "resolution": {"type": "string"}, "who": {"type": "string"}, "date": {"type": "string"},
    "school": {"type": "string"}, "link": {"type": "string"}, "kind": {"type": "string"}}},
    "description": "earlier requests of the same kind found in the precedent hunt, with how each was actually resolved (only when the thread or ticket shows the resolution)"}
DOSSIER = {"type": "object", "required": ["problem", "hypotheses", "root_cause", "root_cause_status", "repos",
                                          "needs_migration", "new_ux", "in_fence", "recommendation", "evidence"],
           "properties": {
               "problem": {"type": "string"}, "root_cause": {"type": "string"},
               "root_cause_status": {"type": "string", "enum": ["proven", "probable", "unknown"]},
               "precedents_found": PRECEDENTS_FOUND,
               "hypotheses": {"type": "array", "items": {"type": "object", "required": ["h", "confidence", "evidence"],
                              "properties": {"h": {"type": "string"}, "confidence": {"type": "number"},
                                             "evidence": {"type": "string"}}}},
               "evidence": {"type": "array", "items": {"type": "string"}},
               "repos": {"type": "array", "items": {"type": "string"}},
               "needs_migration": {"type": "boolean"}, "new_ux": {"type": "boolean"}, "in_fence": {"type": "boolean"},
               "problem_kind": {"type": "string", "enum": ["bug", "incident", "feature", "ops", "question", "other"]},
               "recommendation": {"type": "string", "enum": ["build", "propose", "ask", "drop"]},
               "questions": {"type": "array", "items": {"type": "string"}},
               "reason": {"type": "string"}}}


def dossier_repos(ds: dict) -> list[str]:
    """The configured repos a dossier names, in its order (primary first). Investigate writes them as free text, often
    annotated: 'cometa (frontend/apps/dashboard: primary fix in the BFF)' is cometa. An exact-name filter dropped every
    annotated entry. Read here, not in DOSSIER: the schema is part of the drops' rejudge rev."""
    out = []
    for r in ds.get("repos") or []:
        m = re.match(r"[\w.-]+", str(r).strip())
        if m and m.group(0) in CFG["repos"] and m.group(0) not in out:
            out.append(m.group(0))
    return out


PLAN = {"type": "object", "required": ["title", "tier", "parts", "regression_ring"], "properties": {
    "title": {"type": "string"}, "tier": {"type": "integer"},
    "parts": {"type": "array", "items": {"type": "object", "required": ["repo", "branch", "scope", "steps", "tests_first"],
              "properties": {"repo": {"type": "string"}, "branch": {"type": "string"},
                             "scope": {"type": "array", "items": {"type": "string"}},
                             "steps": {"type": "array", "items": {"type": "string"}},
                             "tests_first": {"type": "array", "items": {"type": "string"}},
                             "contract_checks": {"type": "array", "items": {"type": "string"}}}}},
    "merge_order": {"type": "array", "items": {"type": "object", "properties": {
        "part": {"type": "integer"}, "after": {"type": "integer"}, "need": {"type": "string"}, "why": {"type": "string"}}}},
    "regression_ring": {"type": "array", "items": {"type": "string"}},
    "risks": {"type": "array", "items": {"type": "string"}},
    "outside": {"type": "array", "description": "work the case needs that is NOT a code change in an allowed repo, never a part: "
                "a Slack reply or squad proposal, a read-only production data check, an owner's decision",
                "items": {"type": "object", "required": ["kind", "what"], "properties": {
                    "kind": {"type": "string", "enum": ["reply", "proposal", "prod_check", "decision"]},
                    "what": {"type": "string"}, "before_build": {"type": "boolean"}}}}}}
CRITIQUE ={"type": "object", "required": ["verdict", "required_changes", "notes"], "properties": {
    "verdict": {"type": "string", "enum": ["approve", "revise", "propose", "drop"]},
    "required_changes": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}}}
NEEDS = {"type": "string", "enum": ["code", "human"],
         "description": "who can close it. code: a session in this worktree (a code, test or PR body change, a local "
                        "repro). human: ONLY when no change here can: it needs prod/cloud credentials or a deployed "
                        f"environment, a deploy, another PR merged first, a data repair, or {site.first_name()}'s decision"}
SELF_VERIFY = {"type": "object", "required": ["solves_end_to_end", "why", "gap_needs", "confirmed", "dismissed"], "properties": {
    "solves_end_to_end": {"type": "boolean", "description": "Does this PR solve the reported problem end to end, out of the box?"},
    "why": {"type": "string", "description": "the determination, concrete; if false, exactly what is missing or wrong"},
    "gap_needs": {**NEEDS, "description": "when solves_end_to_end is false, who can close what is missing: " + NEEDS["description"]},
    "confirmed": {"type": "array", "items": {"type": "object", "required": ["file", "problem", "fix", "severity", "needs"],
                  "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "problem": {"type": "string"},
                                 "fix": {"type": "string"}, "needs": NEEDS, "severity": {
                                     "type": "string", "enum": ["critical", "major", "minor"],
                                     "description": "critical: must not ship as is (wrong result, or a plan precondition "
                                                    "not met); major: a real defect to fix before publishing; minor: a nit"},
                                 "unverified": {"type": "boolean", "description": "true when the problem is that something "
                                                "the PR relies on was never actually run or checked (the reported flow not "
                                                "reproduced, a round trip or a persisted value never read back). It blocks "
                                                "whatever the severity. Numbers or wording that disagree between texts are not this"}}}},
    "dismissed": {"type": "array", "items": {"type": "object", "properties": {
        "what": {"type": "string"}, "why": {"type": "string"}}}}}}
DONE = {"type": "object", "required": ["verdict", "gaps", "summary"], "properties": {
    "verdict": {"type": "string", "enum": ["done", "not_done"]},
    "gaps": {"type": "array", "items": {"type": "string"}}, "summary": {"type": "string"}}}
READ_ROOT = str(worktree.MIRROR)  # read-only mirrors at origin/<default>; never the user's working checkouts
# what a release install is told about the text of a PR: its repo's own language, which only the repo's history says
PR_LANGUAGE = "in the language this repository's pull requests are written in (English when unsure)"


SLACK_LINK = re.compile(rf"{site.slack_host_re()}/archives/([A-Z0-9]+)/p(\d{{10}})(\d{{6}})(?:\?thread_ts=(\d+\.\d+))?")
_REPO = site.repo_or("[a-z-]+")
PR_LINK = re.compile(rf"github\.com/(?:{site.owners_re()})/({_REPO})/pull/(\d+)")
# a PR named in free text: 'repo#12', 'repo PR #12', 'owner/repo/pull/12'
PR_MENTION = re.compile(rf"(?:(?:{site.owners_re()})/)?({_REPO})(?:/pull/|\s+PR\s*#|#)(\d+)")
NAMED_CHANNEL = re.compile(r"<#(C[A-Z0-9]+)(?:\|[^>]*)?>")


def _thread_text(ch: str, tts: str, max_chars: int = 24000) -> str:
    # the whole thread with attachment metadata, never only its tail
    return slack.thread_text(ch, tts, max_chars=max_chars)


def _raw_slack(it: dict) -> dict:
    """Ground truth for a Slack-sourced item: the real trigger message text, the real thread
    root and the attached files, fetched from Slack (never from a model's paraphrase). Cached
    on the item the first time."""
    d = it["data"]
    if not (d.get("channel") and d.get("ts")):
        return {}
    raw = d.get("raw") or {}
    if raw.get("ts") == d["ts"] and raw.get("fetched_at", 0) > now() - 600:
        return raw
    m = slack.fetch_message(d["channel"], d["ts"]) or {}
    root = m.get("thread_ts") or d["ts"]
    thread_msgs = []
    if not (d.get("dm") or d["channel"].startswith("D")):
        try:
            thread_msgs = slack.replies(d["channel"], root)[-30:]
        except Exception:
            thread_msgs = [m] if m else []
    # a linked thread's attachments are the report too (#757: a teammate's screenshot sat in the thread a teammate linked)
    linked = []
    for lm in list(SLACK_LINK.finditer(f"{(m or {}).get('text') or ''} {d.get('text', '')}"))[:3]:
        ch, tts = lm.group(1), lm.group(4) or f"{lm.group(2)}.{lm.group(3)}"
        if (ch, tts) != (d["channel"], root):
            try:
                linked += slack.replies(ch, tts)
            except Exception:
                pass
    msgs = list({x["ts"]: x for x in ([m] if m else []) + thread_msgs + linked}.values())
    missed, read = [], {}
    files = slack.download_files(msgs, item_dir(it["id"]) / "files", missed=missed)
    if missed:
        try:  # the bot token has no files:read; the claude.ai Slack connector does
            from . import bridge
            read = bridge.read_files(missed)
        except Exception as e:
            log("slack files: connector read failed", item=it["id"], err=str(e)[:160])
    raw = {"ts": d["ts"], "root": root, "text": slack.render(m.get("text") or "") if m else None,
           "author": slack.user_name(m.get("user")) if m else None, "author_id": m.get("user"), "files": files,
           "files_read": [{"name": f["name"], "at": db.fmt_time(f["ts"]), "text": read[f["id"]]} for f in missed if f["id"] in read],
           "files_unreadable": [f["name"] for f in missed if f["id"] not in read], "n_thread": len(thread_msgs), "fetched_at": now()}
    try:
        db.update_item(it["id"], data={"raw": raw, "thread_ts": None if d.get("dm") else root})
        d["thread_ts"] = None if d.get("dm") else root
    except Exception:
        pass
    return raw


def _context(it: dict) -> str:
    d = it["data"]
    raw = _raw_slack(it)
    src = "raw Slack text" if raw.get("text") is not None else "as recorded"
    trigger = raw["text"] if raw.get("text") is not None else d.get("text", "")
    parts = [f"Title (proposed by the system, may be wrong): {it['title']}", f"Kind: {it['kind']}"]
    if d.get("from") in ("backfill", "ctx", "patrol", "granola") or d.get("summary"):
        parts.append(f"Summary proposed by a SMALL model (hypothesis, verify against the raw thread; it may invert who asked "
                     f"whom): {d.get('summary', '')}")
    parts.append(f"Trigger message ({src}; {db.fmt_time(d['ts']) if d.get('ts') else ''}"
                 + (f"; by {raw['author']}" if raw.get("author") else "") + f"):\n<message>\n{trigger}\n</message>")
    if raw.get("files"):
        parts.append("Attached files (open them with Read; a meme or reaction gif is not an ask):\n"
                     + "\n".join(raw["files"]))
    if raw.get("files_read"):
        parts.append("Attached files read through the Slack connector (verbatim transcripts; they are in the thread, never "
                     "ask anyone to share them):\n" + "\n".join(f"<file name=\"{f['name']}\" posted=\"{f['at']}\">\n{f['text']}\n</file>"
                                                                for f in raw["files_read"]))
    if raw.get("files_unreadable"):
        parts.append(f"Attachments nobody here can see (Slack would not hand them over; {site.first_name()} has been told): "
                     + ", ".join(raw["files_unreadable"]) + ". What they show is unknown: never conclude from their "
                     "absence, and when one may hold the answer (a screenshot reply), say so in your result.")
    # the permalink goes before the conversation: prompts read a prefix of context.md (propose: 6000 chars), and after a
    # long thread it was cut, so the squad proposals of #287, #504 and #638 opened with a '[link al hilo]' slot for him
    def _permalink(ts: str) -> None:
        try:
            parts.append(f"Permalink: {slack.permalink(d['channel'], ts)}")
        except Exception:
            pass
    if d.get("dm") or (d.get("channel") or "").startswith("D"):
        _permalink(d.get("last_ts") or d["ts"])
        try:  # a DM has no threads: the recent conversation is the context
            msgs = slack.history(d["channel"], f"{float(d['ts']) - 3 * 86400:.6f}", pages=2)
            msgs = sorted(msgs, key=lambda m: float(m["ts"]))[-40:]
            parts.append("DM conversation (oldest first; messages after the trigger are part of the same ask):\n<dm>\n"
                         + "\n".join(f"[{db.fmt_time(m['ts'])}] {slack.user_name(m.get('user'))}: {slack.render(m.get('text'))[:1200]}"
                                      for m in msgs) + "\n</dm>")
        except Exception as e:
            parts.append(f"(DM history unavailable: {str(e)[:80]})")
    elif d.get("channel") and (raw.get("root") or d.get("thread_ts")):
        root = raw.get("root") or d["thread_ts"]
        _permalink(d.get("ts") or root)
        try:
            parts.append(f"Thread (raw, oldest first; {site.marker()} is {site.user_name()}):\n<thread>\n" + _thread_text(d["channel"], root) + "\n</thread>")
        except Exception:
            pass
    # threads linked inside the message are part of the ask (2026-09-23: the reply lane said
    # "no abrí el hilo" about the one link a teammate's message consisted of)
    seen = set()
    for m in SLACK_LINK.finditer(f"{d.get('text', '')} " + " ".join(u["text"] for u in d.get("updates", []))):
        ch, tts = m.group(1), m.group(4) or f"{m.group(2)}.{m.group(3)}"
        if (ch, tts) in seen or (ch == d.get("channel") and tts == d.get("thread_ts")) or len(seen) >= 3:
            continue
        seen.add((ch, tts))
        try:
            parts.append(f"Linked thread {m.group(0)}:\n<linked_thread>\n{_thread_text(ch, tts, max_chars=8000)}\n</linked_thread>")
        except Exception as e:
            parts.append(f"Linked thread {m.group(0)}: could not be read ({str(e)[:80]})")
    # a channel the message names ("les mandé en #canal una propuesta") points at its author's latest post there, and
    # that thread is the same conversation (2026-10-07: #597 asked the user to answer a teammate's slide split, drafting
    # "las láminas están en otro canal y no las vi", while he had agreed to it with "Perfectoo" in that post's thread)
    for ch in dict.fromkeys(NAMED_CHANNEL.findall(f"{trigger} {d.get('text', '')}")):
        if ch == d.get("channel") or len(seen) >= 3 or not raw.get("author_id"):
            continue
        try:
            posts = [p for p in slack.history(ch, f"{float(d['ts']) - 6 * 3600:.6f}", pages=1)
                     if p.get("user") == raw["author_id"] and float(p["ts"]) <= float(d["ts"])
                     and p.get("thread_ts", p["ts"]) == p["ts"]]
        except Exception as e:
            parts.append(f"Channel <#{ch}> named in the message: could not be read ({str(e)[:80]})")
            continue
        if posts and (ch, posts[-1]["ts"]) not in seen:
            seen.add((ch, posts[-1]["ts"]))
            parts.append(f"Thread the message points at (the author's latest post in <#{ch}>; what was said there is part "
                         f"of this conversation):\n<linked_thread>\n{_thread_text(ch, posts[-1]['ts'], max_chars=8000)}\n</linked_thread>")
    if not d.get("channel") and d.get("search_terms"):
        found = _slack_found(it)
        if found:
            parts.append(found)
    for u in d.get("updates", [])[-8:]:
        parts.append(f"Later message: {u['text'][:800]}")
    if d.get("pattern_stats"):
        parts.append(f"Alert pattern stats: {json.dumps(d['pattern_stats'])}")
    if d.get("demand"):
        parts.append(f"Demand pattern (same capability asked more than once): {json.dumps(d['demand'], ensure_ascii=False)[:2000]}")
    if d.get("checks"):
        parts.append(f"Checks promised to {site.first_name()} for this case: " + "; ".join(d["checks"]))
    from . import cases
    cf = cases.text_for(it)
    if cf:
        parts.append(cf)
    return "\n\n".join(parts)


def _slack_found(it: dict, days: int = 21) -> str:
    """A case born outside Slack (a daily's action item) has no thread, so its context had no conversation at all: the
    conversations its search terms find, his DMs included, searched once per case. 2026-10-22: #1090 'confirm a customer's
    requirements' was investigated from squad knowledge alone and drafted an ask to a teammate with stale facts, while
    the user had already sized it and asked for the confirmations in his DM with a teammate on 10-20/21."""
    path = item_dir(it["id"]) / "slack-found.md"
    if path.exists():
        return path.read_text()
    after = time.strftime("%Y-%m-%d", time.localtime(now() - days * 86400))
    hits: dict[tuple, list] = {}
    for term in it["data"]["search_terms"][:3]:
        try:
            for m in slack.search(f'"{term}" after:{after}', count=20):
                ch = (m.get("channel") or {}).get("id")
                if ch and ch != CFG["self_dm_channel"]:  # the self-DM holds our own reports, not the conversation
                    root = slack.thread_ts_from_permalink(m.get("permalink")) or m["ts"]
                    hits.setdefault((ch, root), []).append(m)
        except Exception as e:
            log("slack found: search failed", item=it["id"], term=term, err=str(e)[:120])
            return ""  # not cached: the next stage searches again
    best = sorted(hits.items(), key=lambda kv: (-len(kv[1]), -max(float(m["ts"]) for m in kv[1])))[:4]
    out = []
    for (ch, root), ms in best:
        try:
            if ch.startswith("D"):  # a DM has no threads: the day around the hit is the conversation
                hit = max(float(m["ts"]) for m in ms)
                msgs = [m for m in slack.history(ch, f"{hit - 86400:.6f}", pages=1) if float(m["ts"]) <= hit + 86400][-30:]
                text = "\n".join(f"[{db.fmt_time(m['ts'])}] {slack.user_name(m.get('user'))}: {slack.render(m.get('text'))[:800]}"
                                 for m in msgs)
            else:
                text = _thread_text(ch, root, max_chars=5000)
        except Exception as e:
            text = f"(could not be read: {str(e)[:80]})"
        out.append(f"<conversation channel=\"{slack.channel_name(ch)}\" link=\"{slack.permalink(ch, root)}\">\n{text[:5000]}\n</conversation>")
    body = ("Slack conversations about this task, found by searching " + ", ".join(f'"{t}"' for t in it["data"]["search_terms"][:3])
            + f" over the last {days} days ({site.first_name()}'s DMs included; raw, oldest first in each; what he already said or did "
            "here is part of the task's state):\n" + "\n".join(out)) if out else \
        f"Slack searched for {', '.join(it['data']['search_terms'][:3])} over the last {days} days: no conversation found."
    path.write_text(body)
    return body


def _later_messages(it: dict) -> str:
    """Case-thread messages that arrived after investigate froze context.md. Every stage that can still change the work
    reads them."""
    ctx = item_dir(it["id"]) / "context.md"
    seen = " ".join((ctx.read_text() if ctx.exists() else "").split())
    new = [u["text"] for u in it["data"].get("updates", [])
           if u.get("text") and " ".join(u["text"].split())[:150] not in seen]
    adj = it["data"].get("news_instructions")  # impact.worker's read of that news (teammates, not the user's directive)
    # news from outside the thread (a teammate's PR on the same files, a folded duplicate) lives only in the inbox
    # (impact.news): a stage started after it must see it too, judged or not
    new += [f"{n.get('who')}: {n['text']}" for n in it["data"].get("inbox", []) if n.get("thread") is False and n.get("text")]
    if not new and not adj:
        return ""
    return ("\n\nNEW MESSAGES IN THE CASE THREAD OR ABOUT THE CASE SINCE THE INVESTIGATION (oldest first). They bind like "
            "the original report: a teammate's objection to the approach, a new fact, a wider scope or someone else taking "
            "it (their PR on the same files) must be handled in the work, or answered in AP_LEDGER.json with evidence of "
            "why it does not apply:\n"
            + "\n".join(f"- {t[:800]}" for t in new[-8:]) + "\n"
            + (f"What the case worker decided this news changes in the work:\n{adj[-2000:]}\n" if adj else ""))


def _repro_text(it: dict) -> str:
    rec = it["data"].get("repro") or {}
    if rec.get("verdict") == "reproduced":
        return ("\n\nTHE BUG IS ALREADY REPRODUCED (binding): the failing test "
                f"{rec.get('test_files')} is committed on branch {rec.get('branch')} of {rec.get('repo')} and fails with "
                f"\"{str(rec.get('expected_failure'))[:300]}\". Run it with: {rec.get('command')}\n"
                "The fix is built on that branch. The reproduction test must NOT be edited: the fix is done when that test "
                "passes unchanged (plus the other tests the plan adds). Plan the fix for exactly the reproduced cause.\n")
    if rec.get("verdict") == "observed":
        return f"\n\nREPRODUCED BY OBSERVATION (no test can express it): {str(rec.get('evidence'))[:800]}\n"
    return ""


def _cur(it: dict) -> dict:
    part = it["data"]["parts"][it["data"].get("cur", 0)]
    if part.get("pr") and part.get("wt") and not Path(part["wt"]).exists():
        try:  # 10-20: #367's fix failed 3 times on its collected tree and shipped with caveats instead of the adjustment
            worktree.restore(part["repo"], part["branch"], part["wt"])
        except Exception as e:
            log("worktree restore failed", item=it["id"], err=str(e)[:200])
    return part


ESCALATION_TAGS = ("[escalación]", "[escalation]")


def _instructions(it: dict, also: str = "") -> str:
    """The case's binding instructions, saying who wrote them: the escalation appends its own as '[escalación]' lines
."""
    instr = it["data"].get("instructions")
    if not instr:
        return ""
    tag = next((t for t in ESCALATION_TAGS if t in instr), "")  # the tag as the escalation wrote it, in either language
    who = (f"; lines tagged {tag} are the harness's own escalation review, the rest are {site.first_name()}'s"
           if tag else f"; written by {site.first_name()}")
    return f"Instructions for this case (binding{who}{'; ' + also if also else ''}):\n{instr}\n"


def _save_part(it: dict, part: dict, **data) -> None:
    parts = it["data"]["parts"]
    parts[it["data"].get("cur", 0)] = part
    db.update_item(it["id"], data={"parts": parts, **data})


TELEMETRY = {"type": "object", "required": ["summary", "facts", "mechanism", "mechanism_status", "links"], "properties": {
    "summary": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}},
    "mechanism": {"type": "string"}, "mechanism_status": {"type": "string", "enum": ["proven", "probable", "unknown"]},
    "code_refs": {"type": "array", "items": {"type": "string"}}, "links": {"type": "array", "items": {"type": "string"}},
    "new_since": {"type": "string"}, "in_fence": {"type": "boolean"},
    "refuted": {"type": "array", "items": {"type": "string"}}, "remaining": {"type": "array", "items": {"type": "string"}}}}
# the gathering run's contract is raw evidence, not a conclusion: given TELEMETRY it wrote the whole dump into `summary`
# and dropped or mangled the required arrays, so StructuredOutput was retried in 17 sessions and #366 failed for 2 USD
# after 5 tries on a missing `facts`
EVIDENCE = {"type": "object", "required": ["evidence"], "properties": {
    "evidence": {"type": "string", "description": "every query run and what it returned, verbatim: counts, UTC timestamps, "
                                                  "exact error texts, sample events, which schools, trace ids"},
    "sources": {"type": "array", "description": "one entry per source used or tried (Datadog logs, Datadog spans, Sentry, Metabase...)",
                "items": {"type": "object", "required": ["name", "newest_event_utc", "failed"], "properties": {
                    "name": {"type": "string"},
                    "newest_event_utc": {"type": "string", "description": "ISO UTC time of the newest event the source holds for ANY "
                                                                         "service or project (one extra query: everything, newest "
                                                                         "first, limit 1); empty for a database"},
                    "failed": {"type": "string", "description": "the exact error when the tool could not be used (scope, auth, "
                                                               "not connected); empty when it answered"}}}},
    "links": {"type": "array", "items": {"type": "string"}}}}
DARK_AFTER = 6 * 3600


def blind_sources(ev: dict, at: int) -> list[str]:
    """Sources the gathering could not see through: a tool error, or a source whose newest event of any kind is hours
    old. A dark source answers count=0 like a quiet one."""
    from datetime import datetime, timezone
    out = []
    for s in ev.get("sources") or []:
        name = str(s.get("name") or "?")[:60]
        if str(s.get("failed") or "").strip():
            out.append(f"{name}: {str(s['failed']).strip()[:200]}")
            continue
        try:
            newest = datetime.fromisoformat(str(s.get("newest_event_utc") or "").strip())
        except ValueError:
            continue
        newest = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
        if at - newest.timestamp() > DARK_AFTER:
            out.append(f"{name}: newest event {newest.astimezone(timezone.utc):%Y-%m-%d %H:%MZ}, nothing after it")
    return out


def _tell_blind(blind: list[str]) -> None:
    """Once a week per source while it lasts, like the files:read notice."""
    for b in blind:
        key = f"telemetry_blind_told:{b.split(':')[0]}"
        if now() - (db.kv_get(key) or 0) < 7 * 86400:
            continue
        db.kv_set(key, now())
        log("telemetry: source blind", source=b[:200])
        try:
            slack.send_self(site.t(
                f"⚠️ Telemetry cannot see {b}. Cases say so and do not read its zeros as no errors. If the source moved "
                f"to another tool, the new connector has to be in the Claude account this install uses and in "
                f"TELEMETRY_TOOLS (ap/claude.py).",
                f"⚠️ La telemetría no ve {b}. Los casos lo dicen y no toman sus ceros como ausencia de errores. "
                f"Si la fuente cambió (Datadog → Grafana), el conector nuevo tiene que estar en la cuenta de Claude "
                f"del autopilot y en TELEMETRY_TOOLS (ap/claude.py)."), topic="telemetry-blind")
        except Exception as e:
            log("telemetry blind notice failed", err=str(e)[:160])
ORG_HINT = re.compile(r"notion|roadmap|jira|ticket|\bFR\b|feature request|PRD|TDD|canvas|backlog|sprint|epic|okr", re.I)
TELEMETRY_HINT = re.compile(r"\b(500|502|503|5xx|error|exception|traceback|alert|datadog|sentry|timeout|crash|ca[ií]da|falla)\b", re.I)


def needs_telemetry(it: dict) -> bool:
    d = it["data"]
    return it["kind"] == "INCIDENT" or bool(TELEMETRY_HINT.search(f"{it['title']} {d.get('text', '')} {d.get('summary', '')}"))


def _folded(it: dict) -> bool:
    """Same problem as an open case: folded before any expensive work (ap/dedupe.py). Fails open."""
    try:
        from . import dedupe
        return dedupe.semantic(it) is not None
    except Exception as e:
        log("dedupe: semantic check failed, continuing", item=it["id"], err=str(e)[:160])
        return False


DARK_RECHECK = 6 * 3600


def telemetry(it: dict) -> None:
    if _folded(it):
        return
    dark = db.kv_get("telemetry_all_blind")
    if dark and now() - dark["at"] < dark.get("window", DARK_RECHECK):
        # the last gather saw through none of its sources: another one pays the same to see nothing (2026-10-14: Datadog
        # off since 10-13, Metabase without scope, Sentry without auth; #792 paid 2.4 USD to end in 'root cause
        # unknown', #806 1.75 more). The first gather after the window probes again, so a fixed connector is seen
        err = f"every telemetry source was blind at {db.fmt_time(dark['at'])}: " + "; ".join(dark["blind"])
        log("telemetry skipped: every source blind on the last gather", item=it["id"])
        return db.update_item(it["id"], status="investigate", note="telemetry skipped: every source blind",
                              data={"telemetry_done": True, "telemetry_error": err[:300], "telemetry_blind": dark["blind"]})
    ctx = _context(it)
    try:
        # 1) gathering is repetitive and token-heavy: Sonnet runs the Datadog/Sentry/Metabase queries and writes the raw
        #    evidence; 2) Opus reads only that evidence and concludes (the user, 2026-09-29: delegate the repetitive
        #    token-consuming parts, keep judgment on Opus)
        ev = claude.telemetry(f"GATHER ONLY. Run the queries this report needs (logs, error tracking, traces, Metabase) and return the raw "
                              f"evidence verbatim: counts, UTC timestamps, exact error texts, sample events, which schools. Draw no conclusion.\n\n"
                              f"Report ({site.of_team('squad')}; fence: {CFG['domain_fence']}):\n\n{ctx}", cwd=READ_ROOT, schema=EVIDENCE,
                              item_id=it["id"], urgent=it["urgency"] >= 3, model="sonnet")
        (item_dir(it["id"]) / "telemetry-evidence.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1))
        blind = blind_sources(ev, now())
        all_blind = bool(blind) and len(blind) >= len(ev.get("sources") or [])
        # a probe that is still blind doubles the window (up to a day): the sources stay dark until the user fixes access,
        # and each 6 h probe paid ~2.5 USD to learn it again
        window = min(2 * dark.get("window", DARK_RECHECK), 4 * DARK_RECHECK) if dark else DARK_RECHECK
        db.kv_set("telemetry_all_blind", {"at": now(), "blind": blind, "window": window} if all_blind else None)
        out = claude.lean("telemetry", f"Evidence gathered by a tool run (verbatim, trust the numbers):\n{json.dumps(ev, ensure_ascii=False)[:20000]}\n\n"
                          + (f"BLIND SOURCES (checked by the harness): {'; '.join(blind)}. A zero count, a missing error or a "
                             f"quiet window from these proves nothing about the report: name it as missing evidence, never as "
                             f"absence.\n\n" if blind else "") +
                          f"Report ({site.of_team('squad')}; fence: {CFG['domain_fence']}):\n\n{ctx[:12000]}\n\nRepos (read-only): "
                          f"{READ_ROOT}",
                          cwd=READ_ROOT, schema=TELEMETRY, tools=("Read", "Grep", "Glob"), item_id=it["id"], urgent=it["urgency"] >= 3)
    except claude.ClaudeError as e:
        held = it["data"].get("telemetry_held_since") or now()
        if isinstance(e, claude.ConnectorsMissing) and it["kind"] == "INCIDENT" and now() - held < 3600:
            # the CLI is on an account without Datadog/Sentry (claude.wrong_account; he was told) and nothing was
            # spent: an incident waits up to an hour for the login to come back instead of going out as a guess
            return db.update_item(it["id"], next_run_in=600, data={"telemetry_held_since": held}, last_error=str(e)[:300])
        if it["kind"] == "INCIDENT" and it["data"].get("telemetry_tries", 0) < 1 and not isinstance(e, claude.ConnectorsMissing):
            return db.update_item(it["id"], next_run_in=300, data={"telemetry_tries": 1}, last_error=str(e)[:300])
        # a TASK does not need telemetry to be investigated (2026-09-24: two announcements blocked on
        # "no MCP call in the transcript" because the tools had nothing to look up)
        log("telemetry unavailable, investigating without it", item=it["id"], err=str(e)[:160])
        return db.update_item(it["id"], status="investigate", note="telemetry skipped: " + str(e)[:120],
                              data={"telemetry_done": True, "telemetry_error": str(e)[:300]})
    (item_dir(it["id"]) / "telemetry.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    from . import cases
    facts = [{"fact": f, "evidence": " ".join(out.get("links", [])[:2])} for f in out.get("facts", [])]
    if out.get("mechanism_status") == "proven":
        facts.append({"fact": site.t("MECHANISM: ", "MECANISMO: ") + out["mechanism"], "evidence": " ".join(out.get("code_refs", [])[:3])})
    facts += [{"fact": site.t(f"BLIND SOURCE: {b}. Its zeros do not prove there were no errors.",
                              f"FUENTE CIEGA: {b}. Sus ceros no prueban ausencia de errores."), "evidence": "telemetry-evidence.json"}
              for b in blind]
    cases.add(cases.key_for(it), facts=facts, refuted=out.get("refuted"), remaining=out.get("remaining"), source="telemetry")
    db.update_item(it["id"], status="investigate", data={"telemetry": out, "telemetry_done": True, "telemetry_blind": blind})
    _tell_blind(blind)


# ---- stages -----------------------------------------------------------------
def _own_pr_review_thread(it: dict) -> dict | None:
    """A Slack message about review findings on one of HIS PRs (a thread rooted on his PR announcement, or a
    message naming his open PR) is handled by the PR babysitter (COMMENTS items), never by a build of its own
."""
    d = it["data"]
    if not d.get("channel"):
        return None
    root = d.get("thread_ts") or d.get("ts")
    try:
        msgs = slack.replies(d["channel"], root)[:1]
    except Exception:
        msgs = []
    blob = " ".join(slack.render(m.get("text") or "") for m in msgs) + " " + (d.get("text") or "")
    for m in PR_LINK.finditer(blob):
        repo, n = m.group(1), int(m.group(2))
        own = db.q1("SELECT 1 FROM prs WHERE own=1 AND repo=? AND number=?", repo, n)
        if own:
            c = db.q1("SELECT id, status FROM items WHERE kind='COMMENTS' AND repo=? AND pr=? ORDER BY id DESC", repo, n)
            return {"repo": repo, "pr": n, "comments_item": c["id"] if c else None, "comments_status": c["status"] if c else None}
    return None


def investigate(it: dict) -> None:
    if _folded(it):
        return
    own = _own_pr_review_thread(it)
    if own and it["data"].get("parent"):
        # the verification child of a reply about review findings on his PR: the babysitter fixes and answers the
        # threads; the parent reply only needs that outcome
        parent = db.item(it["data"]["parent"])
        note = (site.t(f"handled by the babysitter of {own['repo']}#{own['pr']}", f"lo maneja el babysit de {own['repo']}#{own['pr']}")
                + (site.t(f" (case #{own['comments_item']}, {own['comments_status']})", f" (caso #{own['comments_item']}, {own['comments_status']})")
                   if own.get("comments_item") else ""))
        db.update_item(it["id"], status="dropped", note=note)
        if parent and parent["kind"] == "REPLY" and parent["status"] not in db.TERMINAL:
            db.update_item(parent["id"], status="reply_draft", attempts=0, next_run_at=0, note=site.t("redraft: the babysitter handles the PR's threads", "redraft: los hilos del PR los maneja el babysit"),
                           data={"verified": True, "child": None,
                                 "dossier": {"problem": f"Review findings on his own PR {own['repo']}#{own['pr']}",
                                             "root_cause_status": "n/a", "in_fence": True, "needs_migration": False, "new_ux": False,
                                             "recommendation": "ask", "repos": [own["repo"]],
                                             "evidence": f"The PR babysitter (case #{own.get('comments_item')}) fixes every thread on that PR, "
                                                         f"answers each reviewer on GitHub and resolves the fixed ones; its status now: {own.get('comments_status')}. "
                                                         f"The Slack reply only acknowledges and points to the GitHub threads; it never promises a separate fix.",
                                             "questions": []}})
        return
    # an install with no production data connected has nothing to look at first: no detour, and no line in the case's
    # story saying it looked at production
    if (site.feature("telemetry") and not it["data"].get("telemetry_done") and needs_telemetry(it)
            and claude.can_spend("telemetry", it["urgency"] >= 3)):
        return db.update_item(it["id"], status="telemetry", note="telemetry first (Sentry/Metabase)")
    ctx = _context(it)
    (item_dir(it["id"]) / "context.md").write_text(ctx)
    tele = item_dir(it["id"]) / "telemetry.json"
    research = item_dir(it["id"]) / "research.json"
    blob = " ".join(it["data"].get("checks", [])) + " " + it["data"].get("summary", "")
    if it["data"].get("precedent_terms") and not (item_dir(it["id"]) / "precedents.json").exists():
        try:
            from . import precedents
            found = precedents.hunt(it["data"]["precedent_terms"], it["data"].get("school") or "")
            (item_dir(it["id"]) / "precedents.json").write_text(json.dumps(found, ensure_ascii=False, indent=1))
        except Exception as e:
            log("precedent hunt failed", item=it["id"], err=str(e)[:200])
    if not research.exists() and ORG_HINT.search(blob):
        try:  # roadmap, FRs, tickets: only the org tools know (v1 had MCP everywhere)
            from . import bridge
            found = bridge.org_research(it["title"], it["data"].get("checks", []))
            research.write_text(json.dumps(found, ensure_ascii=False, indent=1))
        except Exception as e:
            log("org research failed", item=it["id"], err=str(e)[:200])
    from . import learn
    prompt = ((learn.knowledge_text() + "\n\n") if learn.knowledge_text() else "") + (
              f"Investigate this report for {site.first_name()}'s squad. Repos under {READ_ROOT} (read-only). Fence: "
              f"{CFG['domain_fence']}\n\n{ctx}"
              + (f"\n\nORG RESEARCH (verbatim snippets fetched from Notion/Jira by a fetch-only model; judge them): "
                 f"{research.read_text()[:8000]}" if research.exists() else "")
              + (f"\n\nPRECEDENT HUNT (our store, Slack history, Jira; raw material, judge it: which of these is the same kind "
                 f"of request and how was it resolved, by whom, with links): "
                 f"{(item_dir(it['id']) / 'precedents.json').read_text()[:10000]}" if (item_dir(it["id"]) / "precedents.json").exists() else "")
              + (f"\n\nTELEMETRY FACTS (verified from Sentry/Metabase; treat as ground truth, do not "
                 f"contradict without code evidence): {tele}" if tele.exists() else "")
              + (f"\n\nTELEMETRY NOT RUN ({it['data']['telemetry_error'][:160]}): nobody looked at logs, errors or traces. "
                 f"Root cause from code alone is at most probable; name the evidence that is missing."
                 if it["data"].get("telemetry_error") and not tele.exists() else "") + (f"\nSlack-wide context (orientation on priorities and ownership only; not a fact source): {context.CTX_FILE}\n" if context.CTX_FILE.exists() else ""))
    dossier = muse.call("ap-investigate", prompt, cwd=READ_ROOT, schema=DOSSIER, lane="investigate", item_id=it["id"])
    (item_dir(it["id"]) / "dossier.json").write_text(json.dumps(dossier, ensure_ascii=False, indent=1))
    for p in dossier.get("precedents_found") or []:
        try:  # the store grows from every hunt, not only from closed cases
            from . import precedents
            precedents.add(request=p["request"], resolution=p["resolution"], kind=p.get("kind") or " ".join(it["data"].get("precedent_terms") or [])[:80],
                           school=p.get("school") or "", who=p.get("who") or "", links=[p["link"]] if p.get("link") else [],
                           source_item=it["id"], date=(p.get("date") or None))
        except Exception as e:
            log("precedent store failed", err=str(e)[:160])
    rec = dossier["recommendation"]
    squad_driven = _squad_driven(it)
    if not dossier["in_fence"] and squad_driven and dossier.get("root_cause_status") in ("proven", "probable"):
        # a shared surface (login landing, nav) the the team squad was tagged on or is driving, hitting
        # the team users: ours to fix
        dossier["in_fence"] = True
        dossier["fence_note"] = f"shared surface, raised to / driven by the {site.of_team('squad')}"
    if not dossier["in_fence"]:
        rec = "drop" if rec == "drop" else "info"
    # build by default for bugs and incidents we own: the draft PR is the proposal
    #. The intake's `proposal` flag only
    # holds back what is not such a bug: on 10-05 patrol #440's flag beat the dossier's own 'build', a squad
    # proposal nobody had reproduced reached him ("is this true? can you replicate it?") and the outcome
    # audit then flipped it to build anyway, as it had #177, #224, #227 and #312.
    if rec == "propose" and builds(dossier):
        rec = "build"
    if rec == "build" and (dossier["needs_migration"] or (it["data"].get("proposal") is True and not builds(dossier))):
        rec = "propose"
    nxt = {"build": "plan", "propose": "propose", "ask": "reply_draft", "drop": "dropped", "info": "dropped"}[rec]
    # the note says why a drop is a drop: 33 of 43 were out of the fence, and #439's 'dossier: drop (unknown)' on
    # 10-05 (admissions, not ours) was filed as 'dropped via an error path, not a real diagnosis'
    verdict = f"dossier: {rec} ({dossier['root_cause_status']})" + ("" if dossier["in_fence"] else ", out of fence")
    parent =db.item(it["data"]["parent"]) if it["data"].get("parent") else None
    if parent:
        # a verification child never drafts on its own:
        # its verdict goes to the case thread and the PARENT redrafts with the verified facts. Only a
        # child that has a fix to build keeps going; its PR lands in the same thread. A proposal is a message to that
        # same thread: while the parent reply is live it travels in the redraft, or the thread gets two drafts and the
        # proposal outlives the parent's drop (#255 and #287 on 09-29/30, #650 on 10-08 asked beside their parent and
        # again after it dropped as answered). A parent already ended leaves the child the only one on it.
        live_reply = parent["kind"] == "REPLY" and parent["status"] in ("awaiting_approval", "awaiting_human", "reply_draft")
        keep_going = nxt == "plan" or (nxt == "propose" and not live_reply)
        if not keep_going:
            db.update_item(it["id"], status="dropped", note="verdict delivered to the parent case", data={"dossier": dossier})
        else:
            db.update_item(it["id"], status=nxt, note=verdict, data={"dossier": dossier})
        if keep_going:
            _report_investigation(db.item(it["id"]), dossier, nxt)  # a fix is coming; say so in the thread
        # otherwise the verdict travels inside the parent's redraft: one message, not two
        if live_reply:
            db.update_item(parent["id"], status="reply_draft", attempts=0, next_run_at=0, note="redraft with the verified facts",
                           data={"dossier": dossier, "ask": bool(dossier.get("questions")), "verified": True, "verified_at": now(),
                                 "child": it["id"] if keep_going else None,
                                 "fix_in_progress": keep_going})
        return
    db.update_item(it["id"], status=nxt, note=verdict,
                   data={"dossier": dossier, "ask": rec == "ask"})
    _report_investigation(db.item(it["id"]), dossier, nxt)


def builds(ds: dict) -> bool:
    """An owned bug or incident with a proven or probable cause and no migration is built, whatever the intake
    guessed: the build reproduces it first and its draft PR is the proposal. The outcome audit applies the same rule."""
    return (ds.get("problem_kind") in ("bug", "incident") and bool(ds.get("in_fence")) and not ds.get("needs_migration")
            and ds.get("root_cause_status") in ("proven", "probable"))


def _report_investigation(it: dict, dossier: dict, nxt: str) -> None:
    """The finding reaches the user now, not when (if ever) a PR exists: a verdict for a case
    already in his feed goes into that thread; a new in-fence case opens its own."""
    from . import report
    if it["data"].get("reported_investigation") or (it["status"] in db.TERMINAL and nxt != "dropped"):
        return  # already said, or the case was ended while this investigation ran (db._ended_elsewhere)
    follow = {"plan": "A fix is being planned and built next; the PR will land in this thread.",
              "propose": "Next: a proposal for the squad (needs design/migration sign-off).",
              "reply_draft": "Next: facts only a human has are missing; a reply asking for exactly those follows.",
              "dropped": "Not being built (out of the squad's fence or nothing to do): say why in one line.",
              "dropped_to_parent": "This is the verdict of the checks; the draft in this thread is being redone with these facts "
                                   "and the new version replaces the previous ask."}[nxt]
    parent = it["data"].get("parent")
    # a finding the escalation of one of his cases split out is routed like the recurring alert it came from
    #
    routed = it["data"].get("pattern") or it["data"].get("from_escalation")
    if nxt == "dropped" and not parent and not dossier.get("in_fence") and not routed:
        return  # out-of-fence noise never reaches him unless he already has the case or it is a recurring alert
    if routed and not dossier.get("in_fence"):
        follow = ("Out of the squad's fence: the report must name the owning team and carry a short routing "
                  f"message for them in {site.first_name()}'s voice (he decides whether to send it).")
    try:
        report.post(it, "update" if parent else ("finding" if dossier["root_cause_status"] == "proven" else "surfaced"),
                    note=("Verdict of the checks promised in this case thread: " + "; ".join(it["data"].get("checks", []))
                          + ". " if parent else "") + follow)
        db.update_item(it["id"], data={"reported_investigation": True})
    except Exception as e:
        log("investigation report failed", item=it["id"], err=str(e)[:200])


def _squad_driven(it: dict) -> bool:
    d = it["data"]
    text = f"{d.get('text', '')} " + " ".join(u.get("text", "") for u in d.get("updates", []))
    tag = site.team_tag()  # the group as a rendered text carries it
    if any(f"<!subteam^{s}" in text or f"<@{s}>" in text or bool(tag and tag in text) for s in CFG.get("team_subteam_tags", [])):
        return True
    if d.get("channel") and d.get("thread_ts"):
        try:
            team = set(CFG.get("team_members", {}).keys())
            return any(m.get("user") in team for m in slack.replies(d["channel"], d["thread_ts"]))
        except Exception:
            return False
    return False


RELATED = {"type": "object", "required": ["prs"], "properties": {"prs": {"type": "array", "items": {
    "type": "object", "required": ["repo", "number", "relation", "why"],
    "properties": {"repo": {"type": "string"}, "number": {"type": "integer"},
                   "relation": {"type": "string", "enum": ["duplicate", "complements", "conflicts", "unrelated"]},
                   "why": {"type": "string"}, "what_to_do": {"type": "string"}}}}}}


def _terms(text: str) -> list[str]:
    """Distinctive identifiers in an analysis: constraint names, endpoints, snake_case symbols, file names."""
    found = re.findall(r"[a-z]+(?:_[a-z0-9]+){2,}|/api/v\d/[a-z0-9/_-]+|[A-Za-z]+(?:Error|Violation|Exception)|[a-z_]+\.py", text or "")
    out = []
    for f in found:
        f = f.strip("/")
        if len(f) >= 8 and f not in out and f not in ("conftest.py",):
            out.append(f)
    return out[:8]


def _bot_author(login: str) -> bool:
    """CusCopilot (app/cometa-ai-hub), dependabot and friends: their PRs are references, never owners of a
    fix."""
    return bool(login) and (login.startswith("app/") or bool(re.search(r"bot|copilot|ai-hub", login, re.I)))


def related_prs(it: dict, extra: list[dict] | None = None) -> list[dict]:
    """Open PRs by anyone in the fence repos that touch the same problem, judged as duplicate / complements /
    conflicts.
    Deterministic search, one judgment, stored on the case. `extra`: candidates the pre-build gate found by
    touched files or ticket key, judged together."""
    d = it["data"]
    ds = d.get("dossier") or {}
    text = f"{it['title']} {ds.get('problem', '')} {ds.get('root_cause', '')} {json.dumps(ds.get('evidence', ''), ensure_ascii=False)[:2000]}"
    terms = _terms(text)
    own_prs = {p.get("pr") for p in d.get("parts", []) if p.get("pr")}
    cands: dict = {}
    for c in extra or []:
        if c.get("number") in own_prs or c.get("repo") not in CFG["repos"]:
            continue
        cands.setdefault((c["repo"], c["number"]), {**c, "hits": []})["hits"] += list(c.get("hits") or [])
    if not terms and not cands:
        return []
    for repo in dossier_repos(ds) or list(CFG["repos"]):
        for term in terms[:6]:
            try:
                rows = gh.pr_search(repo, f'"{term}"', state="open", limit=6, fields="number,title,author,body,files,updatedAt")
            except Exception:
                rows = []
            for r in rows:
                if r["number"] in own_prs:
                    continue
                cands.setdefault((repo, r["number"]), {**r, "repo": repo, "hits": []})["hits"].append(term)
    if not cands:
        db.update_item(it["id"], data={"related_prs": [], "related_checked_at": now()})
        return []
    ranked = sorted(cands.values(), key=lambda c: -len(c["hits"]))[:6]
    blob = "\n\n".join(f"### {c['repo']}#{c['number']} by {c['author']['login']} (updated {c['updatedAt']}, matched: {', '.join(c['hits'])})\n"
                        f"{c['title']}\nfiles: {', '.join(f['path'] for f in (c.get('files') or [])[:25])}\n{(c.get('body') or '')[:2500]}" for c in ranked)
    with claude.pool_ctx("delivery"):  # part of delivering his PR, never a 'judgment' expense
        j = muse.call("ap-judge", f"{site.first_name()}'s assistant is about to fix a problem. These OPEN pull requests by teammates "
                      "matched the same "
                      "identifiers. For each, decide from their bodies and files whether it is a duplicate (fixes the same defect at the "
                      "same layer: we must not build), complements (touches the same problem at another layer or a neighbouring defect: "
                      "we build and say how they relate), conflicts (same files or contradicting approach: we build on top of it or wait), "
                      "or unrelated. Be concrete in `why` (quote the mechanism) and say in `what_to_do` how our fix should relate to it.\n\n"
                      f"OUR PROBLEM:\n{text[:4000]}\n\nCANDIDATES:\n{blob}", cwd=READ_ROOT, schema=RELATED, lane="judge", item_id=it["id"])
    rel = [p for p in j["prs"] if p["relation"] != "unrelated" and (p["repo"], p["number"]) in cands]
    for p in rel:
        c = cands[(p["repo"], p["number"])]
        p["author"], p["title"], p["url"] = c["author"]["login"], c["title"], site.pr_url(p["repo"], p["number"])
        if _bot_author(p["author"]):
            # a bot's PR never blocks or owns the fix: it is a reference to read with suspicion; we build ours
            p["bot"] = True
            if p["relation"] in ("duplicate", "conflicts"):
                p["relation"] = "complements"
            p["why"] = site.t(f"A bot's PR ({p['author']}), not reliable: {p['why']}", f"PR de un bot ({p['author']}), no confiable: {p['why']}")
            p["what_to_do"] = ("Build our own fix regardless. Read the bot's diff only as a hint; if it is wrong or partial, "
                               "say so in one line in the PR body. " + (p.get("what_to_do") or ""))
    db.update_item(it["id"], data={"related_prs": rel, "related_checked_at": now()})
    log("related PRs", item=it["id"], n=len(rel), relations=[(p["number"], p["relation"]) for p in rel])
    return rel


def _related_text(it: dict) -> str:
    rel = it["data"].get("related_prs") or []
    if not rel:
        return ""
    return "\n".join(f"- {p['url']} ({p['author']}): {p['relation']}. {p['why']} {p.get('what_to_do', '')}".strip() for p in rel)


def _revising(item_id: int) -> bool:
    """The plan stage was entered by the critic's revise. Any other entry (news, a retake, a reopen) starts a new
    plan/critique cycle: the old critique is not feedback on this plan and its rounds are spent on another plan
."""
    r = db.q("SELECT src FROM transitions WHERE item_id=? AND dst='plan' ORDER BY at DESC, rowid DESC LIMIT 1", item_id)
    return bool(r) and r[0]["src"] == "critique"


def plan(it: dict) -> None:
    d = item_dir(it["id"])
    revising = _revising(it["id"])
    feedback = it["data"].get("critique") if revising else None
    from . import pregate
    if it["kind"] in ("TASK", "INCIDENT", "IDEA") and (not it["data"].get("pregate") or pregate.stale(it)):
        # before any build spend: is it real, was it handled before, is someone on it (ap/pregate.py)
        if not pregate.run_gate(it):
            return
        it = db.item(it["id"])
    from . import repro
    if repro.needed(it):  # reproduce before any fix is planned (ap/repro.py); runs inside this stage, no new status
        return repro.reproduce(it)
    if not it["data"].get("related_checked_at"):
        try:
            rel = related_prs(it)
        except Exception as e:
            rel = []
            log("related PRs failed", item=it["id"], err=str(e)[:160])
        it = db.item(it["id"])
        for dup in [p for p in rel if p["relation"] == "duplicate"]:
            # a duplicate verdict from titles and file lists is not proof
            prf = cover.proof(it, dup["repo"], dup["number"])
            if prf:
                return cover.link(it, prf, site.t(f"Not building: {dup['url']} by {dup['author']} already fixes this.",
                                                  f"No construimos: {dup['url']} de {dup['author']} ya arregla esto."))
            dup["relation"] = "complements"
            dup["why"] = site.t(f"(the judge said duplicate, with no proof in the diff) {dup['why']}",
                                f"(el juez dijo duplicado, sin prueba en el diff) {dup['why']}")
        db.update_item(it["id"], data={"related_prs": rel})
        it = db.item(it["id"])
    related = _related_text(it)
    instr = it["data"].get("instructions")
    prompt = (f"Dossier (JSON): {d / 'dossier.json'}\nContext: {d / 'context.md'}\nRepos: {READ_ROOT}\n"
              + (f"\n{_instructions(it)}" if instr else "")
              + (f"\nRELATED OPEN PRs BY TEAMMATES (do not redo what they do; plan on top of or next to them as stated):\n{related}\n" if related else "")
              + f"Allowed repos: {', '.join(CFG['repos'])}.\n" + (f"\nSlack-wide context (orientation on priorities and ownership only; not a fact source): {context.CTX_FILE}\n" if context.CTX_FILE.exists() else "")
              + (f"\nPREVIOUS PLAN WAS REJECTED BY THE CRITIC. Required changes:\n{json.dumps(feedback, ensure_ascii=False)}\n" if feedback else "")
              + _repro_text(it) + _later_messages(it))
    p = muse.call("ap-plan", prompt, cwd=READ_ROOT, schema=PLAN, lane="plan", item_id=it["id"])
    # a part outside the repos is work no build can do (a Slack reply, a prod check): it is kept as `outside`, never a
    # stage failure (2026-10-05: #492's critic asked for a Slack reply to a teammate and a prod SELECT before building, the
    # planner wrote them as parts of repo 'gate', 'slack-draft' and 'slack', and plan failed 3x into blocked, $4 spent)
    p["outside"] = (p.get("outside") or []) + [
        {"kind": "decision", "what": f"{x['repo']}: " + "; ".join(x.get("steps") or x.get("scope") or [])[:600]}
        for x in p["parts"] if x["repo"] not in CFG["repos"]]
    p["parts"] = [x for x in p["parts"] if x["repo"] in CFG["repos"]]
    if not p["parts"]:  # nothing to build: his call or the squad's, drafted by propose with the plan in the case folder
        (d / "plan.json").write_text(json.dumps(p, ensure_ascii=False, indent=1))
        return db.update_item(it["id"], status="propose", note="plan: no code change", data={"plan": p})
    rep = it["data"].get("repro") or {}
    for i, part in enumerate(p["parts"]):
        if not worktree.valid_branch(part["branch"]):
            part["branch"] = (slug(part["branch"] or p["title"], 32) + f"-{it['id']}")[:39]
        if rep.get("verdict") == "reproduced" and part["repo"] == rep.get("repo"):
            part["branch"] = rep["branch"]  # the fix is built on top of the committed reproduction
        part["state"] = "pending"
    (d / "plan.json").write_text(json.dumps(p, ensure_ascii=False, indent=1))
    db.update_item(it["id"], status="critique", data={"plan": p, **({} if revising else {"plan_rounds": 0})})


def _keep_published(it: dict, new_parts: list[dict]) -> tuple[list[dict], int]:
    """A replan never forgets a PR the case already opened. A new part for a repo that already
    has our PR takes that PR (the work continues on its branch); a published part the new plan omits is kept. The
    build starts at the first part without a PR."""
    old = [p for p in it["data"].get("parts", []) if p.get("pr") and p.get("pr_url")]
    parts, used = [], set()
    for np_ in new_parts:
        prev = next((o for o in old if o["repo"] == np_["repo"] and id(o) not in used), None)
        if prev:
            used.add(id(prev))
            np_ = {**np_, **{k: prev[k] for k in ("pr", "pr_url", "branch", "state", "announced_open", "wt") if k in prev}}
        parts.append(np_)
    parts += [o for o in old if id(o) not in used]
    cur = next((i for i, x in enumerate(parts) if not x.get("pr")), 0)
    return parts, cur


def critique(it: dict) -> None:
    d = item_dir(it["id"])
    prompt = (f"Dossier: {d / 'dossier.json'}\nPlan: {d / 'plan.json'}\nContext: {d / 'context.md'}\n"
              + _instructions(it, "the plan must honor every one")
              + f"Repos (read-only): {READ_ROOT}" + (f"\nSlack-wide context (orientation on priorities and ownership only; not a fact source): {context.CTX_FILE}\n" if context.CTX_FILE.exists() else "")
              + _later_messages(it))
    c = claude.lean("critique", prompt, cwd=READ_ROOT, schema=CRITIQUE, add_dirs=(str(d), str(context.CTX_FILE.parent)), item_id=it["id"],
                    urgent=it["urgency"] >= 3)
    rounds = it["data"].get("plan_rounds", 0) + 1
    if c["verdict"] == "approve":
        _ask_decisions(it)
        parts, cur = _keep_published(it, it["data"]["plan"]["parts"])
        return db.update_item(it["id"], status="build", data={"parts": parts, "cur": cur, "critique_notes": c["notes"],
                                                              "critique_open": None})
    if c["verdict"] == "revise" and rounds < CFG["quality"]["max_plan_rounds"]:
        return db.update_item(it["id"], status="plan", data={"critique": c, "plan_rounds": rounds})
    if c["verdict"] == "drop":
        return db.update_item(it["id"], status="dropped", note="critic: drop", data={"critique": c})
    # out of plan rounds with "revise": build with the critic's open changes attached, which the build and the self
    # review answer one by one. Never demote a fix to a proposal for lack of polish. Only the notes used to reach the
    # build (10-16: #897's 'read schools#1091's diff first' and 'keep it a draft until a teammate signs off' were in
    # required_changes; the PR went out ready and verify_done sent it back for a second full cycle)
    _ask_decisions(it)
    parts, cur = _keep_published(it, it["data"]["plan"]["parts"])
    db.update_item(it["id"], status="build", note="critic: revise (rounds exhausted), building with notes",
                   data={"parts": parts, "cur": cur, "critique_notes": c["notes"], "critique": c,
                         "critique_open": c.get("required_changes") or None})


def _ask_decisions(it: dict) -> None:
    """The plan's owner decisions marked before_build go to the user when the build starts, not after it: the build goes on
    with the plan's recommended option and his answer in the case thread adjusts it. Nothing read before_build, so they
    first surfaced as delivery caveats. Zero tokens; once per distinct set of decisions."""
    todo = [x["what"] for x in (it["data"].get("plan") or {}).get("outside") or []
            if x.get("kind") == "decision" and x.get("before_build")]
    if not todo or it["data"].get("decisions_asked") == todo:
        return
    from . import report
    body = (site.t("I am starting the build with the option the plan recommends, but a person decides this and the code "
                   "depends on the answer. If the answer is different, reply in this thread and I will adjust it:\n",
                   "Arranco el build con la opción que recomienda el plan, pero esto lo decide una persona y el código depende "
                   "de la respuesta. Si la respuesta es otra, contestame en este hilo y lo ajusto:\n")
            + "\n".join(f"• {w[:700]}" for w in todo[:3]))
    try:
        report.post(it, "update", body=body, question=todo[0][:300])
        db.update_item(it["id"], data={"decisions_asked": todo})
    except Exception as e:
        log("decision ask failed", item=it["id"], err=str(e)[:160])


def _critique_open(it: dict, review: bool = False) -> str:
    """The critic's required changes still open when the plan ran out of rounds, as prompt lines ('' when none)."""
    ch = it["data"].get("critique_open")
    if not ch:
        return ""
    rule = ("each one the diff does not do and AP_LEDGER.json does not answer is a confirmed finding" if review else
            "do each one in the code, or answer it in AP_LEDGER.json with why not and name it in the PR body")
    return f"\nThe plan critic's required changes, still open when plan rounds ran out ({rule}):\n" + "\n".join(
        f"- {x}" for x in ch) + "\n"


def propose(it: dict) -> None:
    """Needs a human decision: draft the squad-group message (diagnosis + concrete proposal +
    open question), Opus-checked, left as a composer draft in the team channel."""
    d = item_dir(it["id"])
    dossier = it["data"].get("dossier", {})
    design = site.design_owner()
    prompt = (f"Write the message {site.first_name()} would post in his squad channel to propose this. Shape: link to what "
              f"triggered it, his read of the problem, the concrete solution he'd build (or options), and an open "
              "question to the group" + site.own(" (\"que les parece?\")", "") + ". "
              + (f"Defer design/product calls to {design} explicitly when UI/UX is involved. " if design else "")
              + f"Short.\n{PROPOSAL_WHERE}\n\nContext: {(d / 'context.md').read_text()[:6000]}\n\n"
              f"Dossier: {json.dumps(dossier, ensure_ascii=False)[:6000]}\n"
              f"Critic notes: {json.dumps(it['data'].get('critique', {}), ensure_ascii=False)[:2000]}"
              + (f"\nWork the plan puts outside any code change (cover it in the message): "
                 f"{json.dumps(it['data']['plan']['outside'], ensure_ascii=False)[:2000]}" if (it["data"].get("plan") or {}).get("outside") else "")
              + (f"\n{site.marker()} ASKED FOR CHANGES to the previous draft: {it['data']['revision']}"
                 if it["data"].get("revision") else "")
              + (f"\nTHE DRAFT CHECKER BLANKED YOUR PREVIOUS DRAFT as not a message for the squad: {it['data']['check_blanked']}"
                 if it["data"].get("check_blanked") else "")
              + _later_messages(it))
    schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    draft = muse.call("ap-draft", prompt, cwd=READ_ROOT, schema=schema, lane="draft-proposal", item_id=it["id"])["text"]
    db.update_item(it["id"], status="propose_check", data={"proposal_draft": draft})


PROPOSAL_WHERE = (f"Where it goes: a new top-level message in {site.first_name()}'s squad channel, read by the squad. Keep that audience: "
                  "it is not a reply to the person who raised it (answering them is what the squad decides), and it carries no "
                  "header or note about where it goes, only the message.")


def propose_check(it: dict) -> None:
    # the checker is told where the text lands (2026-10-01: #361's squad proposal came back as "buenas a teammate!..." for her
    # thread in another channel, and the ask would have posted it top-level in the squad channel)
    dossier = it["data"].get("dossier", {})
    schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}, "hold": approvals.HOLD}}
    draft = it["data"]["proposal_draft"]
    out = claude.lean("draft", f"{PROPOSAL_WHERE}\n\nEvidence:\n{json.dumps(dossier, ensure_ascii=False)[:8000]}\n\nDraft:\n{draft}",
                      cwd=READ_ROOT, schema=schema, tools=(), item_id=it["id"])
    checked = out["text"]
    from . import report
    if not checked.strip():
        # the checker blanks a draft that is no squad message (2026-10-19: #890's was a briefing to the user, the 09:52 ask
        # showed him an empty text and he answered 'la propuesta salió vacía'). Its objection is the drafter's to fix, once;
        # re-running the check on the same draft blanks it again. Blanked twice, he gets the objection, not a blank draft
        hold = (out.get("hold") or "").strip() or "the checker returned no text"
        if not it["data"].get("check_blanked"):
            return db.update_item(it["id"], status="propose", next_run_in=0, data={"check_blanked": hold[:1500]},
                                  note=site.t("propose_check: the draft was not a message for the team, drafting it again",
                                              "propose_check: el borrador no era un mensaje para el squad, lo rehago"))
        report.post(db.item(it["id"]), "finding", note="Proposed to the squad instead of built (needs sign-off), but the "
                    "squad message was rejected twice by the draft checker.", question=site.t(
                        f"The draft for the team failed the check twice: {hold[:400]} Will you write it, or tell me what to propose?",
                        f"El borrador para el squad no "
                        f"pasó el chequeo dos veces: {hold[:400]} ¿Lo escribís vos o me decís qué proponer?"))
        return db.update_item(it["id"], status="awaiting_human", data={"check_blanked": None})
    db.update_item(it["id"], data={"proposal_text": checked, "after_approval": "done", "check_blanked": None})
    payload = {"channel": CFG["team_channel"], "thread_ts": None, "text": checked}
    report.post(db.item(it["id"]), "finding", note="Proposed to the squad instead of built (needs sign-off).",
                sendable=payload, ask=("slack_send", payload, "propose"), hold=(out.get("hold") or "").strip())


def _covered_by(it: dict) -> tuple[dict, dict] | None:
    """Another live case whose open PR provably fixes this one (same planned files only make it a candidate)."""
    mine = {f for p in (it["data"].get("plan") or {}).get("parts", []) for f in p.get("scope", []) if not re.search(r"tests?/", f)}
    if not mine:
        return None
    for r in db.q("SELECT id FROM items WHERE id!=? AND status NOT IN ('done','dropped','expired','failed') "
                  "AND kind IN ('TASK','INCIDENT','IDEA')", it["id"]):
        o = db.item(r["id"])
        if not any(p.get("pr_url") for p in o["data"].get("parts", [])):
            continue
        theirs = {f for p in (o["data"].get("plan") or {}).get("parts", []) for f in p.get("scope", [])}
        if mine & theirs:
            for p in o["data"]["parts"]:
                if p.get("pr") and p.get("repo"):
                    prf = cover.proof(it, p["repo"], p["pr"])
                    if prf:
                        return o, prf
    return None


def _resume_text(wt, repo: str) -> str:
    """What an interrupted code session left behind: commits ahead of the base and the ledger. The next
    session continues from there."""
    try:
        base = CFG["repos"][repo]["default"]
        log_ = run(["git", "-C", str(wt), "log", "--oneline", f"origin/{base}..HEAD"]).stdout.strip()
        dirty = run(["git", "-C", str(wt), "status", "--porcelain"]).stdout.strip()
    except Exception:
        return ""
    ledger = Path(wt) / "AP_LEDGER.json"
    if not log_ and not dirty.replace("?? AP_LEDGER.json", "").strip():
        return ""
    return ("\n\nRESUME: a previous session on this worktree was interrupted. Do NOT start over. Existing commits (newest first):\n"
            f"{log_ or '(none)'}\nUncommitted changes:\n{dirty or '(none)'}\n"
            + (f"Its ledger: {ledger}\n" if ledger.exists() else "")
            + "Read the diff, keep what is correct, finish what is missing, fix what is wrong, then continue as planned.\n")


def _budget_line(it: dict) -> str:
    from . import prsize
    kind = prsize.budget_kind((it["data"].get("dossier") or {}).get("problem_kind"), it["kind"])
    return (f"{prsize.limit_for(kind, CFG)} changed lines net for {'a bug fix' if kind == 'bug' else 'a feature'} "
            f"(additions + deletions, excluding tests, lockfiles and generated types)")


def build(it: dict) -> None:
    covered = _covered_by(it)
    if covered:
        # same code already being fixed in an open PR that provably fixes this too: one fix, not two competing
        # ones
        other, prf = covered
        ups = other["data"].get("updates", []) + [{"text": site.t(f"#{it['id']} ({it['title'][:80]}) points at the same code",
                                                                  f"#{it['id']} ({it['title'][:80]}) apunta al mismo código"), "from_item": it["id"]}]
        db.update_item(other["id"], data={"updates": ups[-20:]})
        return cover.link(it, prf)
    if not it["data"].get("parts") and (it["data"].get("plan") or {}).get("parts"):
        # an item sent straight to build (requeue, critic fallback) carries the plan but not the parts
        db.update_item(it["id"], data={"parts": it["data"]["plan"]["parts"], "cur": 0})
        it = db.item(it["id"])
    if not it["data"].get("parts"):  # sent to build with nothing to build: the planner decides, not an IndexError
        return db.update_item(it["id"], status="plan", attempts=0, note=site.t("build with no parts: back to the plan", "build sin partes: vuelve al plan"))
    part = _cur(it)
    wt = worktree.for_branch(part["repo"], part["branch"], it["id"])
    d = item_dir(it["id"])
    prev = [p for p in it["data"]["parts"][:it["data"].get("cur", 0)] if p.get("pr_url")]
    ds = it["data"].get("dossier") or {}
    brief = (f"THE ASK (as {site.first_name()} would type it): {it['title'].replace('(slack) ', '')}. Find the real root cause and fix the "
             f"ENTIRE problem so it works out of the box for the customer, handle every edge case, don't overengineer, don't "
             f"leave scope, never guess, and verify everything for real before calling it done.\n"
             f"Customer problem (from the dossier): {(ds.get('problem') or it['data'].get('summary') or '')[:1500]}\n"
             f"Original report, raw: {(it['data'].get('text') or '')[:1500]}\n"
             f"Definition of Done: the customer's exact steps now produce the right result, reproduced for real before and "
             f"after the fix; anything saved is read back identical after reopening; every writer of the changed value is "
             f"covered; all gauntlet layers RUN or SKIPPED with a reason; an independent subagent review found no blocker.\n"
             f"Size: prefer the smallest change that fully solves it, one concern per PR. Review budget: {_budget_line(it)}; "
             f"over it, the gate sends the branch back to be reduced, and only a one-line `size_justification` in the "
             f"ledger lets it ship.\n\n")
    prompt = (brief + f"Implement part {it['data'].get('cur', 0) + 1}/{len(it['data']['parts'])} of the approved plan "
              f"in this worktree ({wt}, branch {part['branch']}, repo {part['repo']}). The plan is guidance from a model "
              f"without a shell: where running the code proves it wrong or too narrow, follow the evidence and record why.\n\n"
              f"Part (JSON): {json.dumps(part, ensure_ascii=False)}\n"
              f"Full plan: {d / 'plan.json'}\nDossier: {d / 'dossier.json'}\nOriginal context: {d / 'context.md'}\n"
              f"Critic notes: {it['data'].get('critique_notes', '')}\n" + _critique_open(it)
              + _instructions(it)
              + (f"Related open PRs by teammates (never duplicate their work; the PR body must say how this relates to them):\n{_related_text(it)}\n" if _related_text(it) else "")
              + (f"Earlier parts already opened as PRs (this part depends on them): {[p['pr_url'] for p in prev]}\n" if prev else "")
              + f"Sibling repos for reading contracts: {READ_ROOT}" + _later_messages(it) + _resume_text(wt, part["repo"]))
    out = claude.build("build", prompt, cwd=str(wt), add_dirs=(READ_ROOT, str(d)), item_id=it["id"],
                       urgent=it["urgency"] >= 3)
    ledger = wt / "AP_LEDGER.json"
    commits = run(["git", "-C", str(wt), "rev-list", "--count",
                   f"origin/{CFG['repos'][part['repo']]['default']}..HEAD"]).stdout.strip()
    part.update(wt=str(wt), state="built", build_usd=out.get("usd"))
    if ledger.exists():
        (d / f"ledger-{it['data'].get('cur', 0)}.json").write_text(ledger.read_text())
    dropped = _ledger_dropped(ledger) if commits in ("", "0") else ""
    if dropped:
        # a part the session decided not to build (an instruction or the evidence says it is not needed) is an outcome,
        # not a failure: the case goes on with its other parts (2026-10-20: #1012's cometa part, dropped by the
        # escalation's instruction, blocked the case although schools#1104 was published and green)
        part.update(state="dropped", dropped=dropped)
        _save_part(it, part)
        return _advance(db.item(it["id"]), f"part {it['data'].get('cur', 0) + 1} dropped: {dropped[:200]}")
    if commits in ("", "0"):
        return db.update_item(it["id"], status="blocked", note="build produced no commits",
                              data={"blocked_reason": "build produced no commits: " + out["text"][-800:]})
    _save_part(it, part, fix_rounds=0)
    db.update_item(it["id"], status="gate")


def _ledger_dropped(ledger: Path) -> str:
    try:
        return str(json.loads(ledger.read_text()).get("dropped") or "").strip() if ledger.exists() else ""
    except (ValueError, AttributeError):
        return ""


def _advance(it: dict, note: str = "") -> None:
    """The current part is delivered (CI green) or dropped: build the next part not yet built, else verify the case on
    the parts that have a PR. With none, there is nothing to deliver and the escalation decides."""
    cur = it["data"].get("cur", 0)
    parts = it["data"]["parts"]
    nxt = next((i for i in range(cur + 1, len(parts))  # a part with our PR is not rebuilt, a dropped one not retried
                if not parts[i].get("pr") and parts[i].get("state") != "dropped"), None)
    if nxt is not None:
        # the move says which part starts: without it #595's second part (cometa, after schools#1026 published) read
        # 'publish > ci_wait > build > gate' and was filed as a loop that never lands
        return db.update_item(it["id"], status="build", note=f"part {nxt + 1}/{len(parts)}: {parts[nxt].get('repo', '?')}",
                              data={"cur": nxt, "fix_rounds": 0})
    shipped = [i for i, p in enumerate(parts) if p.get("pr")]
    if not shipped:
        return db.update_item(it["id"], status="blocked", note="every part dropped, nothing built",
                              data={"blocked_reason": "every part dropped, nothing built: " + note})
    db.update_item(it["id"], status="verify_done", note=note,
                   data={"cur": cur if parts[cur].get("pr") else shipped[-1]})


def _sync_remote(part: dict) -> None:
    """Someone (the user, update-branch) may have pushed to the PR branch: fold it in before gating,
    or the final push is rejected as non-fast-forward."""
    wt = part["wt"]
    if run(["git", "-C", wt, "fetch", "--quiet", "origin", part["branch"]], timeout=300).returncode != 0:
        return log("gate: could not fetch the PR branch, gating the local head", branch=part["branch"])
    remote = run(["git", "-C", wt, "rev-parse", "--verify", "--quiet", f"origin/{part['branch']}"]).stdout.strip()
    if remote and run(["git", "-C", wt, "merge-base", "--is-ancestor", remote, "HEAD"]).returncode != 0:
        r = run(["git", "-C", wt, "merge", "--no-edit", remote], timeout=300)
        if r.returncode != 0:
            run(["git", "-C", wt, "merge", "--abort"])
            # a code session rewrote history (rebased the PR branch onto master): the remote is the truth of a
            # published branch, so rebuild local = remote + only the genuinely new commits (2026-09-28: #945's fix
            # sat blocked behind a rebase nobody asked for)
            default = CFG["repos"][part["repo"]]["default"]
            run(["git", "-C", wt, "fetch", "--quiet", "origin", default], timeout=300)
            new = []
            for line in run(["git", "-C", wt, "cherry", remote, "HEAD"]).stdout.splitlines():
                if not line.startswith("+ "):
                    continue  # '-' = same patch already on the remote (the rebased copy of its own commits)
                sha = line[2:].strip()
                if run(["git", "-C", wt, "merge-base", "--is-ancestor", sha, f"origin/{default}"]).returncode == 0:
                    continue  # a master commit pulled in by the rebase
                new.append(sha)
            backup = run(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()
            run(["git", "-C", wt, "reset", "--hard", remote])
            for sha in new:
                if run(["git", "-C", wt, "cherry-pick", "-x", sha], timeout=300).returncode != 0:
                    run(["git", "-C", wt, "cherry-pick", "--abort"])
                    run(["git", "-C", wt, "reset", "--hard", backup])
                    raise outbound.BranchMoved(site.t(f"the branch {part['branch']} changed on the remote and cannot be merged cleanly",
                                                      f"la branch {part['branch']} cambió en remoto y no se puede mergear limpio"))
            log("gate: local history rewritten; rebuilt on the remote head", branch=part["branch"], picked=len(new))


def _step_aside(it: dict, part: dict, e: Exception) -> None:
    """His own PR moved under our fix (he is working on it): step aside, babysit re-reads its threads and checks on
    the new head. A harness PR blocks with the reason."""
    if it["kind"] not in ("CI", "COMMENTS"):
        raise e
    st = db.kv_get(f"pr:{part['repo']}#{part['pr']}", {})
    db.kv_set(f"pr:{part['repo']}#{part['pr']}", {**st, "threads_sig": None})
    db.update_item(it["id"], status="dropped", note=site.t(f"{e}; the babysitter reads the PR again on the new head",
                                                           f"{e}; babysit relee el PR sobre el head nuevo"))


def gate(it: dict) -> None:
    part = _cur(it)
    if part.get("pr"):
        try:
            _sync_remote(part)
        except outbound.BranchMoved as e:  # 2026-10-08: #651 sat blocked after he merged master into his PR mid-fix
            return _step_aside(it, part, e)
    # a fix on HIS PR for a reviewer's confirmed finding needs no 50-minute self-review pass: the finding was
    # verified, the gates ran, CI and the reviewer check the rest
    after_gate = "publish" if it["kind"] in ("CI", "COMMENTS") else "self_find"

    def _mark_reviewed() -> bool:
        """Fixes on his PRs (CI, review comments) skip the long self-review but not CodeRabbit: one pass on the fix's
        own commits (prepush-review). False when the item moved instead (retry later, or a fix round for a major)."""
        if after_gate != "publish":
            return True
        cr = gates.coderabbit(Path(part["wt"]), part["repo"], base_commit=part.get("start_sha"))
        (item_dir(it["id"]) / "coderabbit.txt").write_text(cr["output"])
        if not cr["completed"]:
            n = it["data"].get("cr_failures", 0) + 1
            if n >= 6:
                db.update_item(it["id"], status="blocked", data={"cr_failures": n, "blocked_reason": site.t(
                    f"no completed CodeRabbit pass in 6 tries: {cr['error']}",
                    f"CodeRabbit CLI no completó una pasada en 6 intentos: {cr['error']}")})
            else:
                db.update_item(it["id"], status="gate", next_run_in=600, data={"cr_failures": n},
                               note=site.t(f"CodeRabbit did not complete ({cr['error'][:80]}), retrying in 10 min",
                                           f"CodeRabbit no completó ({cr['error'][:80]}), reintento en 10 min"))
            return False
        found = gates.majors(cr["findings"])
        if found and it["data"].get("cr_fix_rounds", 0) < 1:
            db.update_item(it["id"], data={"cr_fix_rounds": it["data"].get("cr_fix_rounds", 0) + 1})
            _to_fix(it, part, [f"CodeRabbit {f['file']}:{f.get('line', '')} {f['text'][:600]}" for f in found], "coderabbit")
            return False
        if found:  # after one round, what is left ships defended in the PR conversation by the babysitter
            part["defenses"] = (part.get("defenses") or []) + [{"what": f"CodeRabbit: {f['file']}", "why": site.t("still open after one round; the reviewer looks at it",
                                                                                                         "pendiente tras una ronda; lo revisa el reviewer")} for f in found]
        head = run(["git", "-C", part["wt"], "rev-parse", "HEAD"]).stdout.strip()
        gitdir = run(["git", "-C", part["wt"], "rev-parse", "--absolute-git-dir"]).stdout.strip()
        Path(gitdir, "cr-gate").write_text(head + "\n")
        return True
    head = run(["git", "-C", part["wt"], "rev-parse", "HEAD"]).stdout.strip()
    if after_gate == "publish" and head and part.get("gated_head") == head:
        # the gates already passed on this exact HEAD and only CodeRabbit is retried: each retry re-ran every layer, so
        # #593 paid two full dashboard tsc passes per CodeRabbit retry and the 10-07 12:15 one timed out
        if not _mark_reviewed():
            return
        _save_part(it, part)
        return db.update_item(it["id"], status=after_gate, data={"cr_failures": 0, "cr_fix_rounds": 0})
    res = gates.run_all(part["repo"], Path(part["wt"]), part.get("scope"), published=bool(part.get("pr")),
                        since=part.get("start_sha"))
    (item_dir(it["id"]) / f"gates-{it['data'].get('cur', 0)}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    part["gates"] = gates.ledger_text(res)
    from . import repro
    rp = repro.gate_check(it, part)
    if rp:
        res["passed"] = False
        res["layers"].append({"name": repro.GATE, "status": "fail", "detail": " ".join(rp)[:1500]})
    if not any(l["status"] == "fail" for l in res["layers"]):  # passed, or env errors only
        part["gated_head"] = res.get("head") or head
    if res["passed"]:
        if _size_over(it, part):
            return
        _save_part(it, part)
        if not _mark_reviewed():
            return
        _save_part(it, part)
        return db.update_item(it["id"], status=after_gate, data={"cr_failures": 0, "cr_fix_rounds": 0})
    problems = [f"{l['name']}: {l['detail']}" for l in res["layers"] if l["status"] == "fail"]
    if not problems:  # only env errors (tool did not run): CI verifies; never send those to a Claude fix
        if _size_over(it, part):
            return
        _save_part(it, part, gate_errors=[l["name"] for l in res["layers"] if l["status"] == "error"])
        if not _mark_reviewed():
            return
        _save_part(it, part)
        return db.update_item(it["id"], status=after_gate, note="gates: env errors only, CI will verify",
                              data={"cr_failures": 0, "cr_fix_rounds": 0})
    _to_fix(it, part, problems, "gate")


def _ledger(part: dict) -> dict:
    f = Path(part.get("wt") or "/nonexistent", "AP_LEDGER.json")
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        return {}


def _size_over(it: dict, part: dict) -> bool:
    """PR size budget (ap/prsize.py), checked once the gates pass. Over budget, the fix stage is asked once to
    reduce (one concern per PR) or to write a one-line justification; still over, publish needs that line (it goes
    in the PR body); a second round without it blocks. True when the item moved (fix or blocked)."""
    if it["kind"] not in ("TASK", "INCIDENT", "IDEA"):
        return False  # fixes on his own PRs (CI, review comments) are sized by the PR he already opened
    from . import prsize
    try:
        res = prsize.check(prsize.numstat_for(part["wt"], CFG["repos"][part["repo"]]["default"]),
                           (it["data"].get("dossier") or {}).get("problem_kind"), it["kind"], CFG)
    except Exception as e:
        log("size budget: could not measure", item=it["id"], err=str(e)[:120])
        return False
    led = _ledger(part)
    part["size"] = {"net": res["net"], "limit": res["limit"], "kind": res["kind"], "over": res["over"],
                    "top": res["counted"][:5], "binary": res["binary"][:5], "excluded": len(res["excluded"]),
                    "justification": (led.get("size_justification") or "").strip().splitlines()[0][:300] if led.get("size_justification") else "",
                    "split_out": led.get("split_out") or []}
    if not res["over"] or part.get("pr"):
        return False  # a published PR is never rewritten for size; its body carries the justification if any
    rounds = it["data"].get("size_rounds", 0)
    if part["size"]["justification"] and rounds >= 1:
        return False
    if rounds >= 2:
        _save_part(it, part)
        db.update_item(it["id"], status="blocked", note=site.t("size: over the budget with no justification",
                                                               "size: sobre el presupuesto y sin justificación"),
                       data={"blocked_reason": site.t(
                           f"PR over the size budget with no justification after 2 rounds: {prsize.summary(res)}",
                           f"PR sobre el presupuesto de tamaño sin justificación tras 2 rondas: {prsize.summary(res)}")})
        return True
    problem = (f"SIZE BUDGET: this branch is over the review budget: {prsize.summary(res)}. {site.first_name()}'s own fixes of this kind "
               f"are ~50-60 lines and merge the same day; big harness PRs sit unreviewed. Make it the SMALLEST change that "
               f"still fully solves the problem, one concern per PR: drop refactors, renames, cleanups, drive-by improvements "
               f"and speculative handling that the problem does not need; reuse existing helpers instead of new layers. If a "
               f"separable concern is in the diff, remove it from this branch and list it in AP_LEDGER.json `split_out` "
               f"(one line each, it becomes a follow-up). Keep every test that proves the fix. If the change genuinely cannot "
               f"get under the budget without leaving the problem half-solved, do NOT cut correctness: write AP_LEDGER.json "
               f"`size_justification` as ONE concrete line {site.own('in Spanish', PR_LANGUAGE)} saying which part is irreducible and why (it goes in "
               f"the PR body).")
    _save_part(it, part, fix_problems=[problem], fix_source="size", size_rounds=rounds + 1)
    db.update_item(it["id"], status="fix", note=site.t(f"size: {res['net']} > {res['limit']} net lines",
                                                       f"size: {res['net']} > {res['limit']} líneas netas"))
    return True


def _to_fix(it: dict, part: dict, problems: list[str], source: str) -> None:
    rounds = it["data"].get("fix_rounds", 0)
    if rounds >= CFG["quality"]["max_fix_rounds"]:
        _save_part(it, part)
        from .ingest import verdict_rev
        from . import repro
        # the rev of the code that decided it, so the block gets one more round when that code changes
        # (worker.requeue_harness_bugs): the self_review verdict, or the frozen reproduction's rules at the gate
        brev = verdict_rev("self_review") if source == "self_review" else \
            repro.rev() if any(p.startswith(f"{repro.GATE}:") for p in problems) else None
        return db.update_item(it["id"], status="blocked", note=f"{source}: fix rounds exhausted",
                              data={"blocked_reason": f"{source} still failing after {rounds} fix rounds:\n" + "\n".join(problems)[:3000],
                                    "blocked_rev": brev})
    _save_part(it, part, fix_rounds=rounds + 1, fix_problems=problems, fix_source=source)
    db.update_item(it["id"], status="fix")


def self_find(it: dict) -> None:
    """Fast-pool half of self-review: finders + CodeRabbit CLI, saved to files. Kept out of the
    Claude pool so Claude threads never sit waiting on the free model."""
    part = _cur(it)
    wt = Path(part["wt"])
    d = item_dir(it["id"])
    base = CFG["repos"][part["repo"]]["default"]
    diff = run(["git", "-C", str(wt), "diff", f"origin/{base}...HEAD"]).stdout
    (d / "self.diff").write_text(diff)
    purpose = ((d / "plan.json").read_text()[:5000] if (d / "plan.json").exists() else
               f"Fixing {it['kind']} on {part.get('pr_url', part['branch'])}: " + "; ".join(it["data"].get("fix_problems") or [])[:4000])
    from . import muse as _m
    _m._prio.hi = True
    big = diff.count("\n") > CFG["quality"].get("finder_min_diff_lines", 600)
    cands = []
    if big:
        try:
            with claude.exempt_ctx():  # own-PR finders run on Sonnet: cheap, capped, never behind the pacing cap
                cands = finders.run(wt, d / "self.diff", purpose, it["id"])
        except Exception as e:
            # the finder is advisory: it never blocks delivery (2026-09-29: the landing PR sat blocked on
            # "every finder pass failed"); CodeRabbit CLI + the Fable verdict still review the diff
            log("self_find: finder failed, continuing without candidates", item=it["id"], err=str(e)[:160])
            (d / "self-candidates-error.txt").write_text(str(e)[:2000])
    cr = gates.coderabbit(wt, part["repo"])
    (d / "coderabbit.txt").write_text(cr["output"])
    if not cr["completed"]:
        # the prepush-review contract: no completed CodeRabbit pass on this HEAD, no push. Retry later; after an hour
        # the case blocks and escalation takes it (never a silent push without review)
        n = it["data"].get("cr_failures", 0) + 1
        if n >= 6:
            return db.update_item(it["id"], status="blocked", data={"cr_failures": n, "blocked_reason": site.t(
                f"no completed CodeRabbit pass in 6 tries: {cr['error'] or cr['output'][-300:]}",
                f"CodeRabbit CLI no completó una pasada en 6 intentos: {cr['error'] or cr['output'][-300:]}")})
        return db.update_item(it["id"], status="self_find", next_run_in=600, data={"cr_failures": n},
                              note=site.t(f"CodeRabbit did not complete ({cr['error'][:80]}), retrying in 10 min",
                                          f"CodeRabbit no completó ({cr['error'][:80]}), reintento en 10 min"))
    (d / "self-candidates.json").write_text(json.dumps(cands, ensure_ascii=False, indent=1))
    (d / "coderabbit-findings.json").write_text(json.dumps(cr["findings"], ensure_ascii=False, indent=1))
    db.update_item(it["id"], data={"cr_failures": 0})
    part["find_head"] = run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    part["coderabbit_rc"] = cr["rc"]
    part["purpose"] = purpose[:1500]
    _save_part(it, part)
    db.update_item(it["id"], status="self_review")


def _severity(s: str | None) -> str:
    """A verdict's severity as critical/major/minor. Free text let a self-declared 'blocker (process gate, not a
    code defect)' and a 'high' miss the exact-match filter and ship."""
    s = (s or "").strip().lower()
    if s.startswith(("critical", "blocker", "blocking")):
        return "critical"
    return "major" if s.startswith(("major", "high")) else "minor"


def self_review(it: dict) -> None:
    part = _cur(it)
    wt = Path(part["wt"])
    d = item_dir(it["id"])
    head = run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    if part.get("find_head") != head or not (d / "self-candidates.json").exists():
        return db.update_item(it["id"], status="self_find", note="finders for this HEAD first")
    cr = {"rc": part.get("coderabbit_rc", 0)}
    purpose = part.get("purpose", "")
    prompt = (f"Worktree: {wt} (branch {part['branch']}). Diff: {d / 'self.diff'}. "
              + (f"Plan: {d / 'plan.json'}." if (d / "plan.json").exists() else f"Purpose: {purpose[:1500]}") + "\n"
              f"Muse finder candidates: {d / 'self-candidates.json'}\nCodeRabbit CLI output (rc={cr['rc']}): {d / 'coderabbit.txt'}"
              + _critique_open(it, review=True) + _later_messages(it))
    try:
        v = claude.lean("self_verify", prompt, model="fable", cwd=str(wt), schema=SELF_VERIFY, add_dirs=(str(d), READ_ROOT),
                        item_id=it["id"])
    except claude.ClaudeError as e:
        # the greenlight is advisory (delivery-first DoD): a Fable run that hits its budget on a big diff without a
        # verdict is retried once on Opus; if that fails too, the PR ships with the caveat
        log("self_review: fable gave no verdict, retrying on opus", item=it["id"], err=str(e)[:120])
        try:
            v = claude.lean("self_verify", prompt, model="opus", cwd=str(wt), schema=SELF_VERIFY, add_dirs=(str(d), READ_ROOT), item_id=it["id"])
        except claude.ClaudeError as e2:
            part["defenses"] = []
            _save_part(it, part)
            return db.update_item(it["id"], status="publish", note=site.t("self-review gave no verdict: publishing with a caveat",
                                                                          "self-review sin veredicto: publico con caveat"),
                                  data={"caveats": (it["data"].get("caveats") or []) + [
                                      site.t(f"self-review did not finish ({str(e2)[:80]})", f"self-review no concluyó ({str(e2)[:80]})")]})
    (d / "self-verify.json").write_text(json.dumps(v, ensure_ascii=False, indent=1))
    part["defenses"] = v["dismissed"]
    part["coderabbit"] = "ran" if cr["rc"] == 0 else f"failed rc={cr['rc']}"
    if v.get("solves_end_to_end") is False:
        # the user: the review is one question, "does it solve the problem end to end, out of the box?"
        v["confirmed"] = [{"file": "(whole PR)", "problem": site.t(f"Does not solve the problem end to end: {v.get('why', '')}",
                                                                   f"No resuelve el problema de punta a punta: {v.get('why', '')}"),
                           "fix": v.get("why", ""), "severity": "critical", "needs": v.get("gap_needs")}] + list(v["confirmed"])
    for c in v["confirmed"]:
        c["severity"] = _severity(c.get("severity"))
    # every major CodeRabbit finding is fixed or defended with a reason (prepush-review step 4); one the verdict did not
    # mention is a confirmed finding, never silently dropped
    crf = d / "coderabbit-findings.json"
    for f in gates.majors(json.loads(crf.read_text()) if crf.exists() else []):
        name = f["file"].rsplit("/", 1)[-1]
        said = " ".join(json.dumps(x, ensure_ascii=False) for x in list(v["confirmed"]) + list(v.get("dismissed") or []))
        if name and name not in said:
            v["confirmed"].append({"file": f["file"], "line": f.get("line", ""), "severity": "major",
                                   "problem": site.t(f"CodeRabbit (not addressed in the verdict): {f['text'][:600]}",
                                                     f"CodeRabbit (sin atender en el veredicto): {f['text'][:600]}"),
                                   "fix": site.t("fix it, or defend it with the reason in AP_LEDGER.json",
                                                 "arreglarlo, o defenderlo con el porqué en AP_LEDGER.json")})
    # what only the user can do (prod access, a deploy, another PR, a data repair) never goes to a fix round: the fix
    # session cannot do it, so the rounds ran out and the case blocked with no PR (2026-10-01: #339 asked twice for a
    # live ECS repro from a worktree with no AWS access, #225 for schools#958 to deploy). The draft ships with his steps.
    human = [c for c in v["confirmed"] if c.get("needs") == "human" and (c["severity"] != "minor" or c.get("unverified"))]
    v["confirmed"] = [c for c in v["confirmed"] if c not in human]
    if human:
        # each round restates his steps in new words: they replace the last round's, never pile up (#630 on 10-08 carried
        # 10 restatements of the same 2 steps into its case message and overflowed it)
        steps = [site.t(f"you have to do this yourself: {c['fix'][:400]}", f"lo tenés que hacer vos: {c['fix'][:400]}") for c in human][:5]
        old = set(it["data"].get("human_steps") or [])
        caveats = list(dict.fromkeys([c for c in it["data"].get("caveats") or [] if c not in old] + steps))
        db.update_item(it["id"], data={"human_steps": steps, "caveats": caveats})
    # a check never run blocks even when filed as minor; the verdict
    # says so per finding. A keyword match on the problem text blocked any finding that named the ledger's `repro` field
    #
    blocking = [c for c in v["confirmed"] if c["severity"] in ("critical", "major") or c.get("unverified")]
    frozen = bool(part.get("pr")) and merge_order.human_approved(part["repo"], part["pr"], "")
    published = bool(part.get("pr"))
    if published and it["data"].get("post_publish_rounds", 0) >= 1:
        # after one post-publish round only 'critical' still blocks (2026-09-30: cometa#2380 shipped with its
        # customer repro never run because it was filed as 'medium')
        blocking = [c for c in blocking if c["severity"] == "critical"]
    if blocking and it["data"].get("fix_rounds", 0) >= CFG["quality"]["max_fix_rounds"] and not any(
            c["severity"] == "critical" or c.get("unverified") for c in blocking):
        # no fix round left and the PR solves the problem: it ships with what is left as caveats, like verify_done and a
        # blocked case that has a PR. Blocking here lost the work (2026-10-07: #624, $40 and no PR, blocked on a sibling
        # procedure the plan never named, found in the last round after the gate had spent the first)
        caveats = [site.t(f"still open: {c['file']}:{c.get('line', '')} {c['problem'][:300]}",
                          f"queda abierto: {c['file']}:{c.get('line', '')} {c['problem'][:300]}") for c in blocking][:5]
        db.update_item(it["id"], data={"caveats": list(dict.fromkeys((it["data"].get("caveats") or []) + caveats))})
        v["confirmed"], blocking = [c for c in v["confirmed"] if c not in blocking], []
    if published and blocking:
        db.update_item(it["id"], data={"post_publish_rounds": it["data"].get("post_publish_rounds", 0) + 1})
    if blocking or (v["confirmed"] and it["data"].get("fix_rounds", 0) == 0 and not frozen and not published):
        return _to_fix(it, part, [f"{c['file']}:{c.get('line', '')} {c['problem']} -> {c['fix']}" for c in v["confirmed"]],
                       "self_review")
    gitdir = run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"]).stdout.strip()
    head = run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    Path(gitdir, "cr-gate").write_text(head + "\n")
    part["reviewed_head"] = head
    _save_part(it, part)
    db.update_item(it["id"], status="publish")


def fix(it: dict) -> None:
    from . import repro
    part = _cur(it)
    problems = it["data"].get("fix_problems") or []
    # the case's instructions bind the fix like the build: an escalation retake sends its steps there, not as problems
    # (2026-10-21: #1044 was retaken into fix with 'merge origin/main and push' and an empty list; the session saw
    # neither and committed nothing)
    prompt = (f"Fix these verified problems ({it['data'].get('fix_source')}) in {part['wt']} "
              f"(repo {part['repo']}, branch {part['branch']}):\n"
              + ("\n".join(f"- {p}" for p in problems) or "- none listed: do what the case's instructions below ask")
              + (f"\n\n{_instructions(it)}" if it["data"].get("instructions") else "")
              + repro.frozen_note(it, part)
              + ("\n\nThis branch is PUBLISHED as a pull request under review: add commits on top; never rebase, amend, "
                 "squash or force-push it (the review anchors depend on its history)." if part.get("pr") else "")
              + (f"\n\nPlan: {item_dir(it['id']) / 'plan.json'}" if (item_dir(it['id']) / 'plan.json').exists() else "")
              + _later_messages(it)
              + ("\n\nNOTE: uncommitted changes in the worktree may be a previous interrupted attempt at these same fixes: "
                 "read them first, keep what is right." if run(["git", "-C", part["wt"], "status", "--porcelain"]).stdout.replace("?? AP_LEDGER.json", "").strip() else ""))
    claude.build("fix", prompt, cwd=part["wt"], add_dirs=(READ_ROOT, str(item_dir(it["id"]))), item_id=it["id"],
                 urgent=it["urgency"] >= 3)
    if it["kind"] == "CI" and _nothing_to_push(part):
        return _no_commit(it, part)  # CI only turns green from a new head: gating the same one is time lost
    db.update_item(it["id"], status="gate")


def _nothing_to_push(part: dict) -> str | None:
    """The PR head, when a fix on his PR left HEAD on it with a clean tree: there is no commit to push."""
    if not part.get("pr"):
        return None
    wt = part["wt"]
    head = run(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()
    remote = run(["git", "-C", wt, "rev-parse", "--verify", "--quiet", f"origin/{part['branch']}"]).stdout.strip()
    dirty = run(["git", "-C", wt, "status", "--porcelain", "--untracked-files=no"]).stdout.strip()  # untracked never ships
    return head if head and head == remote and not dirty else None


def _no_commit(it: dict, part: dict) -> None:
    """A CI case whose fix rounds left the PR head as it was says so, instead of gating and pushing the same head
."""
    head = _nothing_to_push(part) or ""
    ahead = run(["git", "-C", part["wt"], "rev-list", "--count", f"origin/{part['branch']}..refs/heads/{part['branch']}"]).stdout.strip()
    local = site.t(f"; the local branch {part['branch']} has {ahead} unpushed commits (not the harness's)",
                   f"; la rama local {part['branch']} tiene {ahead} commits sin pushear (no son del arnés)") if ahead not in ("", "0") else ""
    asked = ("; ".join(it["data"].get("fix_problems") or [])
             or ((it["data"].get("instructions") or "").strip().splitlines() or [""])[-1])[:800]
    db.update_item(it["id"], status="blocked", note=site.t("fix: no round left a commit", "fix: ninguna ronda dejó un commit"),
                   data={"blocked_reason": site.t(
                       f"the fix left no commit: the PR is still at {head[:7]} and there is nothing to push{local}. "
                       f"Asked for: {asked}",
                       f"el fix no dejó ningún commit: el PR sigue en {head[:7]} y no hay nada que "
                       f"pushear{local}. Se pidió: {asked}")})


def _pr_body(it: dict, part: dict) -> tuple[str, str]:
    d = item_dir(it["id"])
    i = it["data"].get("cur", 0)
    led = {}
    f = d / f"ledger-{i}.json"
    if Path(part["wt"], "AP_LEDGER.json").exists():
        f.write_text(Path(part["wt"], "AP_LEDGER.json").read_text())
    if f.exists():
        try:
            led = json.loads(f.read_text())
        except Exception:
            pass
    title = led.get("pr_title") or (it["data"].get("plan") or {}).get("title") or it["title"]
    body = led.get("pr_body") or f"{(it['data'].get('dossier') or {}).get('problem', '')}\n"
    deps = []
    for j, p in enumerate(it["data"]["parts"][:i]):
        if p.get("pr_url"):
            deps.append(site.t(f"- Depends on {p['pr_url']} (merge and deploy it first)",
                               f"- Depende de {p['pr_url']} (mergear y deployar primero)"))
    if deps:
        body += site.t("\n\n## Dependencies\n", "\n\n## Dependencias\n") + "\n".join(deps)
    rel = [p for p in it["data"].get("related_prs") or [] if p["relation"] in ("complements", "conflicts")]
    if rel:
        body += site.t("\n\n## Related PRs\n", "\n\n## Relación con otros PRs\n") + "\n".join(
            f"- {p['url']} ({site.own('CusCopilot', p['author']) if p.get('bot') else p['author']}): {_bot_why(p)}" for p in rel)
    if part.get("defenses"):
        body += site.t("\n\nDiscarded on purpose:\n", "\n\nDescartado a propósito:\n") + "\n".join(
            f"- {x.get('what')}: {x.get('why')}" for x in part["defenses"][:10])
    from . import repro
    body += repro.pr_section(it)
    body += site.t("\n\n## Local checks\n", "\n\n## Checks locales\n") + part.get("gates", "")
    return title, _cap_body(it, part, _scrub_pr_body(it, title, body))


def _bot_why(p: dict) -> str:
    """Why a bot's PR is related, as the PR body says it: the prefix related_prs put on it, in whichever language it
    was written, becomes a short warning."""
    who = re.escape(p["author"])
    return re.sub(rf"PR de un bot \({who}\), no confiable: |A bot's PR \({who}\), not reliable: ",
                  lambda m: site.t("written by the bot, verify: ", "generado por el bot, verificar: "), p["why"])


def _cap_body(it: dict, part: dict, body: str) -> str:
    """The size justification (when over budget) goes in; then the deterministic cap (ap/prbody.py): Problem, Root
    cause, Changes, Edge cases and Testing stay, the rest moves to the case report, never the PR."""
    from . import prbody
    sz = part.get("size") or {}
    if sz.get("over") and sz.get("justification") and not re.search(r"## Tamaño|## Size", body):  # its own heading, either language
        body = body.rstrip() + site.t(f"\n\n## Size\n{sz['net']} net lines (budget {sz['limit']}): {sz['justification']}",
                                      f"\n\n## Tamaño\n{sz['net']} líneas netas (presupuesto {sz['limit']}): {sz['justification']}")
    body, overflow = prbody.trim(body, int(CFG["quality"].get("pr_body_max_chars", prbody.LIMIT)))
    if overflow:
        i = it["data"].get("cur", 0)
        (item_dir(it["id"]) / f"pr-body-overflow-{i}.md").write_text(overflow)
        prev = it["data"].get("pr_body_overflow")
        prev = dict(prev) if isinstance(prev, dict) else {}
        prev[str(i)] = overflow[:6000]
        db.update_item(it["id"], data={"pr_body_overflow": prev})
        log("pr body capped", item=it["id"], kept=len(body), moved=len(overflow))
    return body


def _scrub_pr_body(it: dict, title: str, body: str) -> str:
    """the user: a PR body is for the team. Never how the problem was found (Slack threads,
    reporters), never drafts or proposals for people, never harness machinery. Deterministic strip of the
    review-defense section (it lives in the case detail), then an Opus pass that removes the rest."""
    body = re.split(r"\n+(?:Descartado a prop[oó]sito|Discarded on purpose):", body)[0].rstrip()
    key = hashlib.sha1(body.encode()).hexdigest()[:12]
    cache = it["data"].get("scrub_cache") or {}
    if cache.get("key") == key:
        return cache["body"]
    try:
        with claude.pool_ctx("delivery"):
            out = claude.lean("pr_scrub", f"Title: {title}\n\nBody:\n{body}", cwd=READ_ROOT, model="sonnet",
                              schema={"type": "object", "required": ["body", "removed"], "properties": {
                                  "body": {"type": "string"}, "removed": {"type": "array", "items": {"type": "string"}}}},
                              tools=(), item_id=it["id"], timeout=300)
        cleaned = out["body"].strip()
        if len(cleaned) < 200:
            raise ValueError("scrub returned an empty body")
        db.update_item(it["id"], data={"scrub_cache": {"key": key, "body": cleaned}, "pr_scrub_removed": out.get("removed", [])[:10]})
        if out.get("removed"):
            log("pr body scrubbed", item=it["id"], removed=out["removed"][:6])
        return cleaned
    except Exception as e:
        log("pr scrub failed, keeping the stripped body", item=it["id"], err=str(e)[:120])
        return body


def rescrub_open_prs() -> int:
    """One pass over the harness's open PRs with the scrub (lifecycle catch-up after the rule changed)."""
    n = 0
    for r in db.q("SELECT id, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('dropped','expired','failed')"):
        it = db.item(r["id"])
        for p in it["data"].get("parts", []):
            if not (p.get("pr") and p.get("pr_url")):
                continue
            try:
                pr = gh.pr_view(p["repo"], p["pr"])
                if pr.get("state") != "OPEN":
                    continue
                cleaned = _cap_body(it, p, _scrub_pr_body(it, pr.get("title", ""), pr.get("body") or ""))
                if cleaned.strip() != (pr.get("body") or "").strip():
                    outbound.edit_pr(p["repo"], p["pr"], pr.get("title", ""), cleaned, it["id"])
                    n += 1
            except Exception as e:
                log("rescrub failed", item=it["id"], err=str(e)[:120])
    return n


def publish(it: dict) -> None:
    part = _cur(it)
    wt = Path(part["wt"])
    if it["kind"] in ("CI", "COMMENTS") and _nothing_to_push(part):
        # gates.coderabbit records no pass for a fix that changed nothing, so the push would block on it
        if it["kind"] == "COMMENTS":  # findings answered with steps, not code: the replies say so
            return db.update_item(it["id"], status="comments_reply", next_run_at=0, note=site.t("publish: the fix changed no code, nothing to push",
                                                                                                    "publish: el fix no cambió código, nada que pushear"))
        return _no_commit(it, part)
    head = run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    gitdir = run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"]).stdout.strip()
    marker = Path(gitdir, "cr-gate") if gitdir else None
    if head and (not marker or not marker.exists() or marker.read_text().strip() != head) and part.get("unreviewed_head") != head:
        # a HEAD that reached publish without passing gate and self-review goes through them first, once per HEAD; only
        # the stages before publish write the cr-gate marker (2026-10-06: the escalation retook #581, blocked at the gate,
        # straight into publish, which blocked 1 s later on 'cr-gate marker != HEAD')
        part["unreviewed_head"] = head
        _save_part(it, part)
        return db.update_item(it["id"], status="gate", note=site.t("publish: this HEAD did not go through the gate and the self-review, those first",
                                                                       "publish: este HEAD no pasó gate y self-review, primero eso"))
    if head and it["kind"] in ("CI", "COMMENTS") and marker and marker.exists() and marker.read_text().strip() == head \
            and CFG.get("quality", {}).get("coderabbit_gate", True) and gates.cr_pass_head(wt) != head \
            and part.get("unreviewed_cr_head") != head:
        # the push needs a completed CodeRabbit pass on this exact HEAD; a HEAD that changed after the pass (a merge of
        # main) goes back once to the stage that runs it instead of blocking
        part["unreviewed_cr_head"] = head
        _save_part(it, part)
        return db.update_item(it["id"], status="gate", note=site.t("publish: no CodeRabbit pass on this HEAD, running it first",
                                                                       "publish: sin pasada de CodeRabbit en este HEAD, la corro primero"))
    try:
        res = outbound.push(part["repo"], wt, part["branch"])
    except outbound.BranchMoved as e:
        return _step_aside(it, part, e)
    if res.get("shadow"):
        part["state"] = "ready_local"
        _save_part(it, part)
        if it["kind"] == "COMMENTS":  # still draft the thread replies so they can be reviewed in the DM
            return db.update_item(it["id"], status="comments_reply", note="shadow: fixes verified locally",
                                  data={"shadow_ready": True})
        return db.update_item(it["id"], status="awaiting_human", note="shadow: built and verified locally",
                              data={"shadow_ready": True})
    if part.get("pr") and it["kind"] in ("TASK", "INCIDENT", "IDEA"):
        # only PRs the harness authored have a ledger body to re-sync; on the user's own PRs
        # (CI/COMMENTS fixes) the body is his
        title, body = _pr_body(it, part)
        outbound.edit_pr(part["repo"], part["pr"], title, body, it["id"])
    if not part.get("pr"):
        changed = set(run(["git", "-C", str(wt), "diff", "--name-only", f"origin/{CFG['repos'][part['repo']]['default']}...HEAD"],
                          timeout=60).stdout.split())
        changed = {f for f in changed if not re.search(r"(^|/)(tests?|__tests__)/|test_[^/]+\.py$|\.(test|spec)\.", f)}
        for r in db.q("SELECT id, data FROM items WHERE id!=? AND kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('dropped','expired','failed')", it["id"]):
            for p in json.loads(r["data"]).get("parts", []):
                if p.get("pr") and p.get("repo") == part["repo"]:
                    try:
                        theirs = {f["path"] for f in gh.pr_view(p["repo"], p["pr"]).get("files") or []}
                    except Exception:
                        continue
                    if changed & theirs:
                        # same code already fixed in an open harness PR: fold, do not open a second one, but only
                        # when that PR provably fixes this case too (same files alone is a different fix)
                        prf = cover.proof(it, p["repo"], p["pr"])
                        if prf:
                            return cover.link(it, prf)
        title, body = _pr_body(it, part)
        pr = outbound.open_draft_pr(part["repo"], part["branch"], title, body, it["id"])
        part.update(pr=pr["number"], pr_url=pr["url"], opened_here=True)
        i = it["data"].get("cur", 0)
        for j, p in enumerate(it["data"]["parts"][:i]):
            if p.get("pr"):
                need = merge_order.need_for(part["repo"], gates.changed_files(wt, part["repo"]), p["repo"],
                                            gates.changed_files(Path(p["wt"]), p["repo"]) if p.get("wt") else [])
                merge_order.record(part["repo"], part["pr"], p["repo"], p["pr"], need, "harness plan order", "plan")
    part["state"] = "pushed"
    # only the push that opened the PR announces it: a CI or COMMENTS case pushes to a PR that existed (2026-10-20: 15
    # of 19 CI cases in two weeks opened a fresh "PR abierto (borrador)" message promising an announcement that never
    # came; cometa#3057 got two of them 45 min apart for two merges of main)
    if part.get("opened_here") and not part.get("announced_open"):
        part["announced_open"] = True
        try:  # the deliverable exists: the case message says so now, not after N verify rounds
            from . import report
            report.post(db.item(it["id"]), "update", body=site.t(
                f"PR opened: {part['pr_url']} (draft). CI is running; I keep verifying and will leave you the "
                f"announcement to send once it is green.",
                f"PR abierto: {part['pr_url']} (borrador). CI corriendo; sigo con la verificación y te dejo el aviso "
                f"listo para mandar cuando esté verde."))
        except Exception as e:
            log("open-PR note failed", item=it["id"], err=str(e)[:160])
    _save_part(it, part, ci_rounds=it["data"].get("ci_rounds", 0))
    if it["kind"] == "COMMENTS":
        # the reviewer hears back the moment the fix is pushed, not after CI; CI is
        # watched afterwards through wait_after_reply -> ci_wait
        return db.update_item(it["id"], status="comments_reply", next_run_at=0,
                              data={"wait_after_reply": {"waiting_for": "ci: " + site.t("the push's checks", "checks del push"), "wait_resume": "ci_wait", "wait_since": now()}})
    db.update_item(it["id"], status="ci_wait", next_run_in=240)


PENDING_STATES = ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "EXPECTED", "REQUESTED")
FAILED_STATES = ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR", "STARTUP_FAILURE", "ACTION_REQUIRED")


def ci_wait(it: dict) -> None:
    part = _cur(it)
    pr = gh.pr_view(part["repo"], part["pr"])
    checks = pr.get("statusCheckRollup") or []
    # CheckRuns carry status/conclusion, StatusContexts (Vercel, CodeRabbit) only carry state
    pending = [c for c in checks if (c.get("status") or "").upper() in PENDING_STATES
               or (c.get("state") or "").upper() in PENDING_STATES]
    failed = [c for c in checks if (c.get("conclusion") or "").upper() in FAILED_STATES
              or (c.get("state") or "").upper() in ("FAILURE", "ERROR")]
    if failed and not pending:
        return db.update_item(it["id"], status="ci_fix", data={"failed_checks": [c.get("name") or c.get("context") for c in failed]})
    if pending or (failed and pending):
        return db.update_item(it["id"], next_run_in=180)
    if not checks:
        since = it["data"].get("no_checks_since") or now()
        if now() - since < 1800:
            return db.update_item(it["id"], next_run_in=180, data={"no_checks_since": since})
    part["state"] = "green"
    _save_part(it, part)
    if it["kind"] == "CI":
        return db.update_item(it["id"], status="done", note="CI green after fix")
    if it["kind"] == "COMMENTS":
        if it["data"].get("comments_replied") and not it["data"].get("pending_comments"):
            # the threads were answered before CI went green: nothing is left (2026-10-01: #196 looped
            # ci_wait > comments_reply > waiting every 30 min, re-drafting the same replies each lap)
            return db.update_item(it["id"], status="done", note=site.t("CI green and the threads already answered", "CI verde y los hilos ya respondidos"),
                                  data={"wait_after_reply": None})
        return db.update_item(it["id"], status="comments_reply")
    _advance(db.item(it["id"]))


FLAKY = (V1 / "flaky-patterns.txt").read_text().splitlines() if (V1 / "flaky-patterns.txt").exists() else []


def _vercel_logs(pr: dict) -> list[str]:
    """Vercel preview failures only show 'failure' in GitHub; the real tsc/next error lives in
    the Vercel build log (gauntlet rule: read the FULL Vercel log, never guess)."""
    out = []
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("context") or c.get("name") or ""
        url = c.get("targetUrl") or c.get("detailsUrl") or ""
        if "vercel" in name.lower() and (c.get("state") or c.get("conclusion") or "").upper() in ("FAILURE", "ERROR") and url:
            dpl = "dpl_" + url.rstrip("/").split("/")[-1]
            scope = (CFG.get("vercel") or {}).get("scope")  # the team the deployments belong to, when the CLI needs it said
            r = run(["vercel", "inspect", dpl, "--logs", *(["--scope", scope] if scope else [])], timeout=180)
            lines = [l for l in (r.stdout + r.stderr).splitlines() if re.search(r"error|failed|TS\d{4}", l, re.I)]
            out.append(f"### {name} ({dpl})\n" + "\n".join(lines[-80:]))
    return out


CI_DIAG = {"type": "object", "required": ["category", "problems", "summary"], "properties": {
    "category": {"type": "string", "enum": ["code", "flaky", "base_breakage", "waits_for_deploy", "needs_push", "infra", "unknown"]},
    "problems": {"type": "array", "items": {"type": "string"}},
    "waits_for": {"type": "string"}, "summary": {"type": "string"}}}


def ci_fix(it: dict) -> None:
    part = _cur(it)
    pr = gh.pr_view(part["repo"], part["pr"])
    if pr.get("mergeable") == "CONFLICTING" and it["data"].get("conflict_rounds", 0) < 2:
        # conflicts with the base: the logs are noise until the base is merged in (2026-09-27: cometa#1452
        # sat a month DIRTY). One fix round whose only job is the merge; the diagnosis runs on the result.
        db.update_item(it["id"], data={"conflict_rounds": it["data"].get("conflict_rounds", 0) + 1})
        return _to_fix(db.item(it["id"]), part, [
            f"The branch conflicts with origin/{pr.get('baseRefName') or 'main'}. Merge origin/{pr.get('baseRefName') or 'main'} "
            f"into the branch (a merge commit, not a rebase: the PR has review history), resolve every conflict keeping "
            f"both the base's and this branch's intent, make lint/typecheck pass, and push. Do not change anything else."], "ci")
    if (it["data"].get("ci_diag") or {}).get("category") == "waits_for_deploy":
        ok, why = _deps_deployed(part["repo"], part["pr"], it["data"].get("waiting_for") or "")
        if not ok:  # red by design until the deploy: no flaky reruns, no re-diagnosis
            return db.update_item(it["id"], status="waiting", next_run_in=1800, note=site.t(f"still waiting: {why}", f"sigue esperando: {why}"),
                                  data={"waiting_for": it["data"].get("waiting_for") or f"waits_for_deploy: {why}",
                                        "wait_resume": "ci_wait", "wait_since": it["data"].get("wait_since") or now()})
    prev = it["data"].get("ci_diag") or {}
    same = prev.get("head") == pr["headRefOid"] and set(it["data"].get("failed_checks") or []) <= set(prev.get("checks") or [])
    if same and prev.get("category") in ("base_breakage", "flaky"):
        # the same checks failed again on the same commit after a rerun: the diff did not change, so neither did the
        # verdict. Back to waiting (or fold in a moved base) without another diagnosis; waiting's rerun budget is what
        # ends it (2026-10-16: #870, cometa#3057's dashboard typecheck broken by DEV drift on main, paid the same $0.63
        # 'base_breakage' diagnosis after each rerun; 2026-10-20: #833, cometa#3009 paid 4 identical 'flaky' ones)
        return _base_breakage(it, part, pr, prev) if prev["category"] == "base_breakage" else _wait(it, "flaky", prev)
    if same and prev.get("category") == "infra":
        # the same checks failed again on the same commit after a rerun: that infra is not transient, and only someone
        # with access fixes it. Block, so the escalation finds that person (2026-10-14: #776, cometa#2900's Vercel status
        # never posted for its head; 5 reruns and 3 diagnoses at $0.65 said the same 'ask a Vercel admin', and the wait
        # would have stayed silent for 3 days)
        return db.update_item(it["id"], status="blocked", note=site.t("infra failure again", "infra repetida"),
                              data={"blocked_reason": site.t(
                                  f"CI infrastructure failure that repeats on the same commit after a rerun: {prev.get('summary', '')[:600]}",
                                  f"falla de infraestructura de CI que se repite en el mismo commit "
                                  f"tras un rerun: {prev.get('summary', '')[:600]}")})
    runs = gh.failed_runs(part["repo"], pr["headRefOid"])
    logs = []
    for r in runs[:4]:
        tail = gh.run_log_tail(part["repo"], r["id"])
        key = f"reruns:{part['repo']}#{part['pr']}:{db.today()}"
        if any(re.search(p, tail, re.I) for p in FLAKY if p.strip()) and db.kv_get(key, 0) < 2:
            outbound.rerun(part["repo"], part["pr"], r["id"])
            db.kv_set(key, db.kv_get(key, 0) + 1)
            log("ci: flaky rerun", item=it["id"], run=r["id"])
            return db.update_item(it["id"], status="ci_wait", next_run_in=300)
        logs.append(f"### {r['name']} (run {r['id']})\n{tail}")
    logs += _vercel_logs(pr)
    if not logs:
        empty = it["data"].get("ci_empty_rounds", 0) + 1
        if empty >= 3:  # failing checks with no readable logs (cancelled, external): wait and recheck, don't loop
            return db.update_item(it["id"], status="waiting", next_run_in=1800,
                                  data={"ci_empty_rounds": 0, "waiting_for": "checks without logs", "wait_resume": "ci_wait"})
        return db.update_item(it["id"], status="ci_wait", next_run_in=180, data={"ci_empty_rounds": empty})
    rounds = it["data"].get("ci_rounds", 0) + 1
    (item_dir(it["id"]) / f"ci-{rounds}.log").write_text("\n\n".join(logs))
    deps = [dict(r) for r in db.q("SELECT dep_repo, dep_number, need, reason FROM merge_deps WHERE repo=? AND number=?",
                                  part["repo"], part["pr"])]
    diag = muse.call("ap-investigate", f"Diagnose the CI failures of {part['pr_url']} (branch {part['branch']}, worktree "
                     f"{part['wt']}). Categories: code (this branch's diff is wrong: list file, cause, fix), flaky, "
                     f"base_breakage (the default branch or a merge from it is broken, not this diff), waits_for_deploy "
                     f"(the Vercel preview regenerates API types from DEV backends: types this branch uses are missing "
                     f"because a backend change is not deployed to dev yet; that is red BY DESIGN, never 'fix' it by "
                     f"guarding imports), needs_push (something that only runs on a push never ran for this head, e.g. "
                     f"no Vercel status at all, not even pending or failed: a rerun of Actions does not send the push "
                     f"again, only a new head does), infra, unknown. Known merge dependencies of this PR: {json.dumps(deps, ensure_ascii=False)}"
                     f"\n\nLogs (GitHub Actions + Vercel build logs): {item_dir(it['id']) / f'ci-{rounds}.log'}",
                     cwd=part["wt"], schema=CI_DIAG, lane="ci-diagnose", item_id=it["id"])
    cat = diag["category"]
    diag = {**diag, "head": pr["headRefOid"], "checks": it["data"].get("failed_checks") or []}
    db.update_item(it["id"], data={"ci_rounds": rounds, "ci_diag": diag})
    if cat == "code":
        if rounds > CFG["quality"]["max_fix_rounds"]:  # our own fixes keep failing: that is the only dead end
            return db.update_item(it["id"], status="blocked", data={"blocked_reason": "CI still red after fixes:\n" + "\n".join(diag["problems"])[:1500]})
        return _to_fix(db.item(it["id"]), part, diag["problems"], "ci")
    default = CFG["repos"][part["repo"]]["default"]
    if cat == "needs_push":
        # reruns replay Actions on the same head and never send the push webhook again: a new head does, once. A
        # second head without it is the integration itself, for a person (2026-10-15: #776, cometa#2900 got no Vercel
        # status on a628dbd8, 3 reruns over 20 h and an escalation before anyone pushed)
        key = f"{part['repo']}#{part['pr']}"
        pushed = it["data"].get("pushed_for_ci") or {}
        if key in pushed:
            return db.update_item(it["id"], status="blocked", note=site.t("needs_push again", "needs_push repetido"), data={
                "blocked_reason": site.t(
                    f"what runs on a push did not run with a new head either ({pushed[key][:8]}, then "
                    f"{pr['headRefOid'][:8]}): {diag['summary'][:600]}",
                    f"lo que corre en el push tampoco corrió con un head nuevo ({pushed[key][:8]} y luego "
                    f"{pr['headRefOid'][:8]}): {diag['summary'][:600]}")})
        db.update_item(it["id"], data={"pushed_for_ci": {**pushed, key: pr["headRefOid"]}})
        if _behind_base(part):
            try:
                outbound.update_branch(part["repo"], pr)
                return db.update_item(it["id"], status="ci_wait", next_run_in=600, note="needs_push: branch updated")
            except Exception as e:
                log("ci: update-branch failed, pushing from the worktree", item=it["id"], err=str(e)[:120])
        return _to_fix(db.item(it["id"]), part, [
            f"CI waits on something only a push triggers ({diag['summary'][:400]}). Merge origin/{default} into this "
            f"branch (merge commit, never rebase); if it is already up to date, make an empty commit "
            f"(git commit --allow-empty -m 'chore: retrigger preview deploy'). Push. Change nothing else, rerun nothing."], "ci")
    if cat == "base_breakage":
        return _base_breakage(it, part, pr, diag)
    if cat == "waits_for_deploy" and it["kind"] == "COMMENTS" and not it["data"].get("comments_replied"):
        # the threads do not depend on the deploy: answer and resolve them now, then wait for CI
        db.update_item(it["id"], data={"wait_after_reply": {"waiting_for": f"{cat}: {diag.get('waits_for') or diag['summary'][:200]}",
                                                            "wait_resume": "ci_wait", "wait_since": now()}})
        return db.update_item(it["id"], status="comments_reply", note="reply now; CI red by design")
    if cat in ("waits_for_deploy", "flaky", "infra"):
        return _wait(it, cat, diag)
    reason = {"waits_for_deploy": site.t(f"CI red by design: it waits for the deploy to dev of {diag.get('waits_for') or 'its backend'}",
                                         f"CI rojo por diseño: espera el deploy a dev de {diag.get('waits_for') or 'su backend'}"),
              "flaky": site.t("CI flaky and no rerun budget left today", "CI flaky y ya sin presupuesto de reruns hoy"),
              "infra": site.t("CI infrastructure failure", "falla de infraestructura de CI"),
              "unknown": site.t("unknown cause", "causa desconocida")}[cat]
    db.update_item(it["id"], status="blocked", note=cat, data={"blocked_reason": f"{reason}: {diag['summary'][:600]}"})


def _wait(it: dict, cat: str, diag: dict) -> None:
    """Not a dead end: recheck periodically and resume CI watching."""
    return db.update_item(it["id"], status="waiting", next_run_in=1800, note=cat,
                          data={"waiting_for": f"{cat}: {diag.get('waits_for') or diag['summary'][:200]}",
                                "wait_resume": "ci_wait", "wait_since": now()})


def _base_breakage(it: dict, part: dict, pr: dict, diag: dict) -> None:
    """CI red because of the default branch, not this diff: fold a moved base in, else wait for it to change."""
    # the default branch moved on (a fix there, or regenerated types): fold it into the PR branch. GitHub only
    # says BEHIND for some states
    if _behind_base(part, pr["headRefOid"]) and not it["data"].get("base_merged_at", 0) > now() - 6 * 3600:
        default = CFG["repos"][part["repo"]]["default"]
        try:
            outbound.update_branch(part["repo"], pr)
            db.update_item(it["id"], data={"base_merged_at": now()})
            return db.update_item(it["id"], status="ci_wait", next_run_in=600, note="base breakage: branch updated")
        except Exception as e:
            log("ci: update-branch failed, merging locally", item=it["id"], err=str(e)[:120])
            return _to_fix(db.item(it["id"]), part, [f"Merge origin/{default} into this branch (merge commit, never rebase), resolve "
                                                      f"conflicts keeping both intents, push. The CI failure is base breakage already fixed on {default}."], "ci")
    # not a dead end: recheck periodically and resume CI watching
    return db.update_item(it["id"], status="waiting", next_run_in=1800, note="base_breakage",
                          data={"waiting_for": f"base_breakage: {diag.get('waits_for') or diag['summary'][:200]}",
                                "wait_resume": "ci_wait", "wait_since": now()})


def _behind_base(part: dict, head: str = "HEAD") -> bool:
    """Does origin's default branch have commits `head` lacks? Pass the PR's head: after an API update-branch the
    worktree's HEAD is the old commit and reads as behind until a stage pulls."""
    default = CFG["repos"][part["repo"]]["default"]
    run(["git", "-C", part["wt"], "fetch", "--quiet", "origin", default], timeout=300)
    if head != "HEAD":
        run(["git", "-C", part["wt"], "fetch", "--quiet", "origin", part["branch"]], timeout=300)
    return run(["git", "-C", part["wt"], "rev-list", "--count", f"{head}..origin/{default}"]).stdout.strip() not in ("", "0")


def _deps_deployed(repo: str, n: int, waiting_for: str = "") -> tuple[bool, str]:
    """Are this PR's merge dependencies merged AND live where they are needed (merge_order's rule)?"""
    deps = [dict(r) for r in db.q("SELECT dep_repo, dep_number, need FROM merge_deps WHERE repo=? AND number=?", repo, n)]
    if not deps:
        # the diagnosis names what it waits for: use it rather than declaring the wait over. It is free text: the
        # dependency can be written "schools PR #913" or as a link, and the PR itself is named too (2026-10-02: #31's
        # "schools PR #913... before/alongside cometa#1931" made cometa#1931 wait for its own merge)
        refs = {(m.group(1), int(m.group(2))) for m in PR_MENTION.finditer(waiting_for)}
        deps = [{"dep_repo": r, "dep_number": k, "need": "dev"} for r, k in sorted(refs) if r in CFG["repos"] and (r, k) != (repo, n)]
    if not deps:
        return False, site.t("no dependencies recorded: still waiting", "sin dependencias registradas: sigo esperando")
    for d in deps:
        ok, why = merge_order.dep_status(d["dep_repo"], d["dep_number"], d["need"] or "dev")
        if not ok:
            return False, why
    return True, site.t("dependencies deployed", "dependencias deployadas")


def _dep_on_its_way(it: dict) -> bool:
    """Does this case wait for a deploy of a named dependency that is open, or merged and not live yet? A dependency
    closed without merge, unreadable or unknown is not on its way."""
    d = it["data"]
    if not (d.get("waiting_for") or "").startswith("waits_for_deploy"):
        return False
    part = _cur(it)
    if not part.get("pr"):
        return False
    ok, why = _deps_deployed(part["repo"], part["pr"], d.get("waiting_for") or "")
    return not ok and why.endswith(" yet")


def ci_red(pr: dict) -> bool:
    """One answer to "is this PR's CI red?" for every writer that parks or resumes on it (tasks.waiting,
    babysit.comments_reply): two predicates that disagree flip the item forever."""
    return any((c.get("conclusion") or "").upper() in FAILED_STATES or (c.get("state") or "").upper() in ("FAILURE", "ERROR")
               for c in pr.get("statusCheckRollup") or [])


def waiting(it: dict) -> None:
    """Something outside our control keeps CI red (a backend deploy, base breakage, flaky infra).
    Waiting means waiting: no rerun until the thing we wait for has changed, and never more than
    a few reruns per item. Gives up after 3 days, unless it waits on a named dependency still on its
    way: that wait has a known end and checking it costs no tokens."""
    d = it["data"]
    if now() - d.get("wait_since", now()) > 3 * 86400 and not _dep_on_its_way(it):
        return db.update_item(it["id"], status="blocked", data={"blocked_reason": site.t(
            f"waiting for 3 days with no dependency on its way: {d.get('waiting_for')}",
            f"esperando hace 3 días sin dependencia en camino: {d.get('waiting_for')}")})
    part = _cur(it)
    if not part.get("pr"):
        return db.update_item(it["id"], status=d.get("wait_resume", "ci_wait"), next_run_in=600)
    pr = gh.pr_view(part["repo"], part["pr"])
    if not ci_red(pr):  # someone (a deploy, a rerun, a push) turned it green: resume normally
        return db.update_item(it["id"], status=d.get("wait_resume", "ci_wait"), next_run_at=0, note=site.t("CI is no longer red", "CI ya no está rojo"))
    kind = (d.get("waiting_for") or "").split(":", 1)[0]
    if kind == "waits_for_deploy" and it["kind"] == "COMMENTS" and not d.get("comments_replied"):
        # the review threads never depended on the deploy: answer them now, come back to wait afterwards
        db.update_item(it["id"], data={"wait_after_reply": {k: d.get(k) for k in ("waiting_for", "wait_resume", "wait_since")}})
        return db.update_item(it["id"], status="comments_reply", next_run_at=0, note=site.t("answering the threads; CI waits for the deploy", "respondo los hilos; el CI espera el deploy"))
    reruns = d.get("reruns", 0)
    last_rerun = d.get("last_rerun_at", 0)
    if kind == "waits_for_deploy":
        ok, why = _deps_deployed(part["repo"], part["pr"], d.get("waiting_for") or "")
        if not ok:
            extra = {"wait_detail": why}
            if d.get("reruns_after_deploy") and re.search(r"sin dependencias|no dependencies", d.get("last_rerun_why") or ""):
                extra.update({"reruns_after_deploy": 0, "last_rerun_why": None})  # that rerun was a bogus "deployed"
            return db.update_item(it["id"], next_run_in=1800, note=site.t(f"waiting: {why}", f"esperando: {why}"), data=extra)
        trigger = site.t(f"dependencies deployed ({why})", f"dependencias deployadas ({why})")
    elif kind == "waits_for_pr":
        # the escalation named the teammate PR that fixes the base: a rerun before it merges cannot change anything, and
        # once it merges the base comes in
        ref = d.get("wait_pr") or {}
        ok, why = merge_order.dep_status(ref.get("repo", ""), ref.get("number", 0), "merged")
        if "closed" in why:
            return db.update_item(it["id"], status="blocked", note=why, data={
                "blocked_from": "waiting", "blocked_reason": site.t(
                    f"the PR it waited for was closed without merging: {d.get('waiting_for')}",
                    f"el PR que esperaba se cerró sin merge: {d.get('waiting_for')}")[:900]})
        if not ok:
            return db.update_item(it["id"], next_run_in=1800, note=site.t(f"waiting: {why}", f"esperando: {why}"), data={"wait_detail": why})
        if _behind_base(part, pr["headRefOid"]):
            try:
                outbound.update_branch(part["repo"], pr)
                return db.update_item(it["id"], status="ci_wait", next_run_in=600, note=f"{why}: branch updated",
                                      data={"base_merged_at": now()})
            except Exception as e:
                log("waiting: update-branch failed, rerunning", item=it["id"], err=str(e)[:120])
        trigger = why
    else:
        # flaky / infra / base breakage / no logs: at most 3 reruns per item, 2 h apart. Red after the last one, nothing
        # we do changes it: block, so the escalation finds who can (2026-10-15: #776, cometa#2900, spent its reruns
        # before a diagnosis could see the same commit fail twice and then aged 13 awake hours silent toward the cap)
        if reruns >= 3:
            return db.update_item(it["id"], status="blocked", note=site.t("out of reruns", "reruns agotados"), data={
                "blocked_from": "waiting", "blocked_reason": site.t(
                    f"CI still red after {reruns} reruns: {d.get('waiting_for')}",
                    f"CI sigue rojo tras {reruns} reruns: {d.get('waiting_for')}")[:900]})
        if now() - last_rerun < 7200:
            return db.update_item(it["id"], next_run_in=1800, note=site.t(f"waiting without a rerun ({reruns} used)",
                                                                          f"esperando sin rerun ({reruns} usados)"))
        trigger = kind or "recheck"
        # a rerun replays the PR's merge with the base it had: a fix that landed on the base since comes in only by a
        # fold, on the same budget (2026-10-16: #870 spent its last rerun on the old head 17 min after cometa#3067 fixed
        # main's typecheck, and blocked; 2026-10-21: #1044 reran a flaky e2e 3 times on cometa#3009's merge 166 commits
        # behind main, then blocked and was retaken into a fix with no code to touch). Flaky folds once in 6 h: only its
        # stale merge is the base's part, and every fold is a commit on the PR
        fold = kind == "base_breakage" or (kind == "flaky" and d.get("base_merged_at", 0) < now() - 6 * 3600)
        if fold and _behind_base(part, pr["headRefOid"]):
            try:
                outbound.update_branch(part["repo"], pr)
                return db.update_item(it["id"], status="ci_wait", next_run_in=600, note=f"{kind}: branch updated",
                                      data={"base_merged_at": now(), "reruns": reruns + 1, "last_rerun_at": now(),
                                            "last_rerun_why": "base moved"})
            except Exception as e:
                log("waiting: update-branch failed, rerunning", item=it["id"], err=str(e)[:120])
    if d.get("reruns_after_deploy", 0) >= 1 and kind == "waits_for_deploy":
        return db.update_item(it["id"], status="ci_wait", next_run_in=600, note=site.t("already rerun after the deploy; checking the result",
                                                                                    "ya se relanzó tras el deploy; miro el resultado"))
    try:
        for r in gh.failed_runs(part["repo"], pr["headRefOid"])[:4]:
            outbound.rerun(part["repo"], part["pr"], r["id"])
        db.add_signal("rerun", {"repo": part["repo"], "pr": part["pr"], "why": trigger}, it["id"])
    except Exception as e:
        log("waiting: rerun failed", item=it["id"], err=str(e)[:200])
    db.update_item(it["id"], status=d.get("wait_resume", "ci_wait"), next_run_in=600, note=f"rerun: {trigger}",
                   data={"reruns": reruns + 1, "last_rerun_at": now(), "last_rerun_why": trigger,
                         "reruns_after_deploy": d.get("reruns_after_deploy", 0) + (1 if kind == "waits_for_deploy" else 0)})


def verify_done(it: dict) -> None:
    d = item_dir(it["id"])
    from . import repro
    if it["data"].get("deliver_blocked"):
        # a blocked case with a PR: only the reproduction's final run, then delivered either way. Red is said as a
        # caveat, never sent back to fix (it was blocked: that loop is how it got here)
        rp = repro.final_check(it)
        caveats = it["data"].get("caveats") or []
        if rp:
            caveats = [*caveats, site.t("the reproduction does NOT pass with the fix: ", "la reproducción NO pasa con el fix: ") + rp[0][:300]]
        return db.update_item(it["id"], status="announce", note=approvals.BLOCK_DELIVERED,
                              data={"caveats": caveats, "deliver_blocked": None})
    rp = repro.final_check(it)
    if rp:  # the reproduction is the definition of done: red is never announced
        return _to_fix(it, _cur(it), rp, "verify_done")
    it = db.item(it["id"])
    summary = {"parts": [{k: p.get(k) for k in ("repo", "branch", "pr_url", "gates", "defenses", "coderabbit", "dropped")}
                         for p in it["data"]["parts"]]}
    prompt = (f"Plan: {d / 'plan.json'}\nLedgers: {sorted(str(x) for x in d.glob('ledger-*.json'))}\n"
              f"Gate results: {sorted(str(x) for x in d.glob('gates-*.json'))}\nSelf-review: {d / 'self-verify.json'}\n"
              f"Parts: {json.dumps(summary, ensure_ascii=False)}\nCI is green on every part with a PR; a part whose "
              f"`dropped` is set was deliberately not built (judge whether the case is solved without it). Worktrees: "
              f"{[p.get('wt') for p in it['data']['parts']]}"
              + (f"\nSteps only {site.first_name()} can do, already handed to him as caveats (not gaps): {it['data']['human_steps']}"
                 if it["data"].get("human_steps") else "") + _later_messages(it))
    v = claude.lean("verify_done", prompt, cwd=_cur(it)["wt"], schema=DONE,
                    add_dirs=(str(d), READ_ROOT), tools=("Read", "Grep", "Glob", "Bash"), item_id=it["id"])
    if v["verdict"] == "not_done":
        part = _cur(it)
        body_only = all(re.search(r"\bbody\b|descripci[oó]n del pr|pr body|testing section|secci[oó]n de testing", g, re.I) for g in v["gaps"])
        head = run(["git", "-C", part["wt"], "rev-parse", "HEAD"], timeout=30).stdout.strip() if part.get("wt") else ""
        if body_only and part.get("pr") and it["data"].get("body_refreshed_for") != head:
            return db.update_item(it["id"], status="pr_body_refresh", data={"body_gaps": v["gaps"]})
        rounds = it["data"].get("verify_rounds", 0) + 1
        db.update_item(it["id"], data={"verify_rounds": rounds})
        # a fix round only when the case has one left: with the gate's rounds spent, _to_fix blocked it as 'verify_done
        # still failing after 2 fix rounds' with no done_summary, and the watchdog delivered that later (#484 on 10-05,
        # #23, #24, #143 before), while team_messages drafted squad asks for the blocked PR
        if rounds <= 1 and not body_only and it["data"].get("fix_rounds", 0) < CFG["quality"]["max_fix_rounds"]:
            return _to_fix(it, part, v["gaps"], "verify_done")
        # the PR is open and CI is green: deliver, carrying the remaining notes as caveats
        #
        return db.update_item(it["id"], status="announce",
                              data={"done_summary": v["summary"], "caveats": v["gaps"][:5]})
    db.update_item(it["id"], status="announce", data={"done_summary": v["summary"]})


def pr_body_refresh(it: dict) -> None:
    """Rewrite the PR title/body from the branch as it is now (diff stat, commits, real test counts
    from the gate ledgers), edit the PR, and verify again. 2026-09-24: #2164 spent three Fable fix
    rounds on '11 tests nuevos' vs 12."""
    part = _cur(it)
    wt = part["wt"]
    d = item_dir(it["id"])
    base = CFG["repos"][part["repo"]]["default"]
    stat = run(["git", "-C", wt, "diff", "--stat", f"origin/{base}...HEAD"], timeout=60).stdout[-4000:]
    commits = run(["git", "-C", wt, "log", "--format=%h %s", f"origin/{base}..HEAD"], timeout=60).stdout[-3000:]
    current = gh.pr_view(part["repo"], part["pr"])
    gates_txt = "\n".join(p.read_text()[-2500:] for p in sorted(d.glob("gates-*.json")))
    schema = {"type": "object", "required": ["title", "body"], "properties": {"title": {"type": "string"}, "body": {"type": "string"}}}
    out = claude.lean("publish_text", f"Rewrite this pull request's title and body so they describe the branch AS IT IS NOW. Keep the repo's "
                      f"PR conventions and the existing structure; fix every number (tests, files, commits) against the evidence; "
                      + site.own("Spanish", f"write it {PR_LANGUAGE}") + f"; no emojis; no AI trailers.\n\nGaps found by the reviewer:\n{json.dumps(it['data'].get('body_gaps'), ensure_ascii=False)}\n\n"
                      f"Current title: {current.get('title')}\nCurrent body:\n{(current.get('body') or '')[:6000]}\n\nCommits vs base:\n{commits}\n\n"
                      f"Diff stat:\n{stat}\n\nGate results (real test counts):\n{gates_txt}",
                      cwd=wt, schema=schema, tools=("Read", "Grep", "Glob"), add_dirs=(str(d),), item_id=it["id"])
    out["body"] = _cap_body(it, part, out["body"])
    outbound.edit_pr(part["repo"], part["pr"], out["title"][:200], out["body"], it["id"])
    led_f = Path(wt, "AP_LEDGER.json")
    try:  # keep the ledger in step so the next publish does not restore the stale text
        led = json.loads(led_f.read_text()) if led_f.exists() else {}
        led.update(pr_title=out["title"][:200], pr_body=out["body"])
        led_f.write_text(json.dumps(led, ensure_ascii=False, indent=1))
    except Exception as e:
        log("ledger update failed", err=str(e)[:120])
    head = run(["git", "-C", wt, "rev-parse", "HEAD"], timeout=30).stdout.strip()
    db.update_item(it["id"], status="verify_done", note="PR body regenerated", data={"body_refreshed_for": head})


# the two channels a PR is announced in, by name when the config knows it (no Slack lookup while the module loads)
TEAM_CH = site.channel_label("team_channel", "his team's channel")
PR_CH = site.channel_label("pr_channel", "the PR channel")
TEAM_MSG = {"type": "object", "required": ["team_text", "pr_text"], "properties": {
    "team_text": {"type": "string", "description": f"ONLY the message he pastes in {TEAM_CH} (his team), nothing else"},
    "pr_text": {"type": "string", "description": f"ONLY the short announcement he pastes in {PR_CH} (reviewers), nothing else"},
    "notes": {"type": "string", "description": f"anything for {site.first_name()}'s eyes (what you verified, what to change before sending); never inside the texts"}}}
# where the model's own remarks start inside a message, as it writes them in either language (input, not output)
_SPILL = ("\n---", f"\nNotas para {site.first_name()}", f"\nnotas para {site.first_name().lower()}", f"\nNota para {site.first_name()}",
          "\nAssistant's note", "\nassistant's note", "\nNota del asistente", f"\nNotes for {site.first_name()}",
          f"\nNote for {site.first_name()}", f"\nnotes for {site.first_name().lower()}")


def _clean(text: str, notes: str) -> tuple[str, str]:
    """The model sometimes appends its own remarks to the message: they belong in the notes."""
    text = (text or "").strip()
    for marker in _SPILL:
        if marker in text:
            text, _, spill = text.partition(marker)
            notes = (notes + "\n" + spill.strip()).strip()
            text = text.strip()
    return text, notes


def _own_announcements(n: int = 8, which: str = "team") -> list[str]:
    """His real messages (the ground truth for the voice), refreshed daily. which='pr': his announcements in
    the PR channel (short, scope for reviewers); which='team': his posts in the team channel (longer, the user
    problem and context; v1 learned this two-channel split on 2026-08-06); which='ops': his answers in the ops/CS
    channels (capitalized greeting with a real mention, team voice, no PRs or caveats). Only messages he typed
    (client_msg_id): the harness's own approved sends are not his voice."""
    import time as _t
    from . import voice_lint
    cached = db.kv_get("own_announcements") or {}
    if not (cached.get("at", 0) > now() - 86400 and cached.get("v") == 4):
        me, pr, team, ops = CFG["self_user_id"], [], [], []
        for ch, bucket in ((CFG.get("pr_channel"), pr), (CFG.get("team_channel"), team)):
            if not ch:
                continue
            try:
                for m in slack.history(ch, f"{_t.time() - 120 * 86400:.6f}", pages=12):
                    x = m.get("text") or ""
                    top = not m.get("thread_ts") or m["thread_ts"] == m["ts"]
                    if m.get("user") == me and m.get("client_msg_id") and top and ("pull/" in x or (bucket is team and len(x) > 250)):
                        bucket.append((m["ts"], x[:900]))
            except Exception as e:
                log("own announcements: history failed", channel=ch, err=str(e)[:100])
        for ch, name in (CFG.get("channel_names") or {}).items():
            if voice_lint.audience(ch) != "ops":
                continue
            try:  # ops answers live in threads: search finds replies too
                for m in slack.search(f"from:<@{me}> in:{name}", count=30):
                    x = m.get("text") or ""
                    if len(x) > 40 and (m.get("channel") or {}).get("id") in (ch, None):
                        ops.append((m["ts"], x[:700]))
            except Exception as e:
                log("own announcements: ops search failed", channel=name, err=str(e)[:100])
        cached = {"at": now(), "v": 4, "pr": [x for _, x in sorted(pr)][-16:], "team": [x for _, x in sorted(team)][-16:],
                  "ops": [x for _, x in sorted(ops)][-16:]}
        db.kv_set("own_announcements", cached)
    return (cached.get(which) or [])[-n:]


def _evidence_links(it: dict) -> list[tuple[str, str]]:
    """(label, url) for everything that proves the problem: the Slack thread(s), the alert message, the
    tickets."""
    from . import cases
    d = it["data"]
    out, seen = [], set()

    def add(label, url):
        if url and url not in seen:
            seen.add(url)
            out.append((label, url))

    srcs = [{"channel": d.get("channel"), "ts": d.get("thread_ts") or d.get("ts"), "pattern": bool(d.get("pattern")),
             "tickets": cases.tickets_in(f"{it['title']} {d.get('text', '')} {d.get('summary', '')} {d.get('jira', '')}")}]
    srcs += d.get("also_sources", [])
    for s in srcs:
        if s.get("channel") and s.get("ts"):
            try:
                url = slack.permalink(s["channel"], s["ts"])
            except Exception:
                url = site.slack_link(s["channel"], s["ts"])
            name = slack.channel_name(s["channel"])
            add(site.t(f"alert in {name}", f"alerta en {name}") if s.get("pattern") else site.t(f"thread in {name}", f"hilo en {name}"), url)
        for t in s.get("tickets") or []:
            add(f"ticket {t}", site.ticket_url(t))
    return out


def _enforce(text: str, evidence: list[tuple[str, str]], tag: str, pr_links: list[str]) -> str:
    """Deterministic guarantees the model sometimes skips: the evidence links are cited, the squad tag is
    there, the PR links close the message."""
    text = (text or "").strip()
    if not text:
        return text
    lines = text.split("\n")
    # 1. the PR links are the last lines
    body = [l for l in lines if not any(u in l for u in pr_links)]
    # 2. the squad tag
    if tag and tag not in text:
        cc = next((i for i, l in enumerate(body) if l.strip().lower().startswith("cc ")), None)
        if cc is not None:
            body[cc] = body[cc].rstrip() + f" {tag}"
        else:
            body += ["", f"cc {tag}"]
    # 3. the evidence, cited
    missing = [(label, url) for label, url in evidence if url not in text]
    if evidence and not any(url in text for _, url in evidence):
        ev = site.t("evidence: ", "evidencia: ") + " · ".join(f"<{url}|{label}>" for label, url in evidence[:4])
        cc = next((i for i, l in enumerate(body) if l.strip().lower().startswith("cc ")), None)
        if cc is not None:
            body.insert(cc, ev)
            body.insert(cc, "")
        else:
            body += ["", ev]
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body).rstrip() + "\n" + "\n".join(f"<{u}|{u.replace('https://', '')}>" for u in pr_links)


def _team_thread(d: dict) -> str | None:
    """A case born in a team-channel thread is answered inside that thread by the team message, not with a new post
    that links back to it."""
    from . import voice_lint
    in_team = voice_lint.audience(d.get("channel")) == "team" and not d.get("pattern")
    return (d.get("thread_ts") or d.get("ts")) if in_team else None


def _team_prompt_neutral(it: dict, revision: str | None, prev: dict, ev_text: str, ex_team: str, ex_pr: str, origin: str,
                         prs: list[str]) -> str:
    """team_message's prompt for every install but the first: the same two registers and evidence rules, in the
    install's language, with the voice left to its style guide and no company convention (the group cc, the ticket
    link in a reply)."""
    from . import voice_lint
    d, ds, name, key = it["data"], it["data"].get("dossier") or {}, site.first_name(), site.ticket_prefix()
    related = _related_text(it)
    return (f"Write the TWO messages {name} sends for a PR of theirs, in {site.language_name()} and in {name}'s own voice "
            "as the style guide and the real messages below show it (no lists, no code, no headings). They are two "
            "different registers:\n"
            f"A) `team_text`, for {TEAM_CH} (the team): a bit longer and with context, 80 to 140 words before the "
            "links, in 2 or 3 short paragraphs: 1) what was happening and who it affected, told as to a teammate; 2) the "
            "context: why it happened, since when, where it came from (customer, ticket, alert, who raised it) and what "
            "it means for the product; 3) what the fix does and how it behaves now, which one goes first if there are "
            "two PRs, and the ask for a review or an opinion. End with the PR links, each on its own line. Template for "
            f"this audience: {voice_lint.template('team')}\n"
            f"B) `pr_text`, for {PR_CH} (reviewers): short, at most 45 words before the links: the scope of the change "
            "and where it came from (ticket or customer), then the links, each on its own line. Template for this "
            f"audience: {voice_lint.template('pr')}\n"
            "Never mention internal tools, private threads or how it was investigated. Each text holds ONLY the message "
            f"as it will be pasted, with no notes or comments of yours; anything you want to tell {name} goes in `notes`. "
            "Caveats: only if they change how the product behaves, in one honest sentence.\n"
            "EVIDENCE: cite the evidence with its link: the Slack thread where it was raised, the alert message or the "
            "ticket. In `team_text` include every evidence link there is, inline in Slack format `<url|short text>` in "
            f"the sentence that says where it came from (e.g. 'raised in <url|this thread>', 'from <url|{key}-1334>'). In "
            f"`pr_text` name the ticket ({key}-xxxx). Never invent links: only these:\n"
            f"{ev_text}\n\n"
            f"REAL MESSAGES FROM {site.marker()} TO THE TEAM:\n{ex_team}\n\nREAL MESSAGES FROM {site.marker()} IN THE PR "
            f"CHANNEL:\n{ex_pr}\n\n"
            f"STYLE GUIDE:\n{claude.style_text()[:14000]}\n\n"
            f"Problem (from the analysis): {ds.get('problem', '')[:1200]}\nWhat was done: {d.get('done_summary', '')[:800]}\n"
            f"How it came in: {origin}\nTickets: {', '.join(sorted({t for a in d.get('also_sources', []) for t in a.get('tickets', [])}))}\n"
            + (f"TEAMMATES' RELATED PRS (in `team_text` say in one sentence how this PR relates to them, with the link; "
               f"never leave it out):\n{related}\n" if related else "")
            + (f"Caveats to state honestly in one clause: {d.get('caveats')}\n" if d.get("caveats") else "")
            + (f"\n{name} asked to change the previous version: {revision}\nPrevious version (team): "
               f"{prev.get('team_text', '')}\n"
               f"Previous version (PRs): {prev.get('pr_text', '')}\n" if revision else "")
            + "\nPRs:\n" + "\n\n".join(prs))


def team_message(it: dict, revision: str | None = None) -> dict | None:
    """The two messages the user sends for a PR the harness built. Stored on the case, shown on the board, each sent on
    his ✅ from the case thread."""
    from . import approvals, digest, voice_lint
    d = it["data"]
    parts = [p for p in d.get("parts", []) if p.get("pr_url")]
    if not parts:
        return None
    prs = []
    for p in parts:
        try:
            v = gh.pr_view(p["repo"], p["pr"])
            if v.get("state") != "OPEN":
                return None
            prs.append(f"{p['pr_url']} ({p['repo']}, {'draft' if v.get('isDraft') else 'ready'}): {v.get('title')}\n{(v.get('body') or '')[:1800]}")
        except Exception:
            prs.append(p["pr_url"])
    ds = d.get("dossier") or {}
    origin = digest._origin(it["title"], d)
    # the group cc on the PR-channel post is the first install's convention (as in voice_lint), not every team's
    tag = f"<!subteam^{CFG['team_subteam_tags'][0]}>" if CFG.get("team_subteam_tags") and site.internal() else ""
    ex_team = "\n\n".join(f"<<<\n{x}\n>>>" for x in _own_announcements(8, "team"))
    ex_pr = "\n\n".join(f"<<<\n{x}\n>>>" for x in _own_announcements(8, "pr"))
    prev = d.get("team_message") or {}
    evidence = _evidence_links(it)
    ev_text = "\n".join(f"- {label}: {url}" for label, url in evidence) or site.t(
        "- (no links: there is no thread, alert or ticket)", "- (sin links: no hay hilo, alerta ni ticket)")
    key = site.ticket_prefix()
    prompt = site.own((f"Escribí los DOS mensajes que {site.first_name()} manda por un PR suyo, EN SU VOZ (voseo, minúsculas, signos de "
              "exclamación dobles, letras repetidas como 'buenaas amigoss', jaja ocasional, cero formalidad, cero listas, "
              "cero código, cero títulos). Son dos registros distintos, como él hace siempre:\n"
              f"A) `team_text`, para {TEAM_CH} (su equipo): un poco más largo y con contexto, entre 80 y 140 palabras "
              "antes de los links, en 2 o 3 párrafos cortos: 1) qué pasaba y a quién le pegaba, contado como a un compañero; "
              "2) el contexto: por qué pasaba, desde cuándo, de dónde salió (colegio, ticket, alerta, quién lo levantó) y "
              "qué implica para el producto; 3) qué hace el fix y cómo se comporta ahora, con qué va primero si son dos PRs, "
              "y el pedido de review o de opinión ('diganme que les parece!'). Cierra con los links de los PRs cada uno "
              "en su propia línea, SIN cc al grupo del squad (en su equipo no lo usa). Plantilla de esta audiencia: "
              f"{voice_lint.template('team')}\n"
              f"B) `pr_text`, para {PR_CH} (reviewers): corto, máximo 45 palabras antes de los links: saludo, una frase "
              f"con el alcance del cambio y de dónde salió (ticket o colegio), 'cc {tag}' y los links cada uno en su línea. "
              f"Plantilla de esta audiencia: {voice_lint.template('pr')}\n"
              "Nunca menciones herramientas internas, hilos privados ni cómo se investigó. En cada texto va SOLO el mensaje "
              f"tal cual lo pega, sin notas ni comentarios tuyos; lo que quieras decirle a {site.first_name()} va en `notes`. Caveats: "
              "solo si cambian cómo se comporta el producto, en una frase honesta.\n"
              "EVIDENCIA: citá la evidencia con su link, como hace él: el hilo de Slack donde lo levantaron, el mensaje de la "
              "alerta de Datadog o el ticket. En `team_text` incluí todos los links de evidencia que existan, inline en formato "
              "Slack `<url|texto corto>` en la frase donde contás de dónde salió (ej: 'lo levantó paulina en <url|este hilo>', "
              f"'sale del <url|{key}-1334>'). En `pr_text` nombrá el ticket ({key}-xxxx) SIN su link: el link va en un reply al post. Nunca inventes links: solo estos:\n"
              f"{ev_text}\n\n"
              f"MENSAJES REALES DE {site.marker()} A SU EQUIPO:\n{ex_team}\n\nMENSAJES REALES DE {site.marker()} EN EL CANAL DE "
              f"PRS:\n{ex_pr}\n\n"
              f"STYLE GUIDE:\n{claude.style_text()[:14000]}\n\n"
              f"Problema (del análisis): {ds.get('problem', '')[:1200]}\nQué se hizo: {d.get('done_summary', '')[:800]}\n"
              f"Cómo llegó: {origin}\nTickets: {', '.join(sorted({t for a in d.get('also_sources', []) for t in a.get('tickets', [])}))}\n"
              + (f"PRs RELACIONADOS DE COMPAÑEROS (en `team_text` decí en una frase cómo se relaciona este PR con ellos, con el link; nunca lo omitas):\n{_related_text(it)}\n" if _related_text(it) else "")
              + (f"Caveats a decir honestamente en una cláusula: {d.get('caveats')}\n" if d.get("caveats") else "")
              + (f"\n{site.first_name()} pidió cambiar la versión anterior: {revision}\nVersión anterior (equipo): "
                 f"{prev.get('team_text', '')}\n"
                 f"Versión anterior (PRs): {prev.get('pr_text', '')}\n" if revision else "")
              + "\nPRs:\n" + "\n\n".join(prs)), _team_prompt_neutral(it, revision, prev, ev_text, ex_team, ex_pr, origin, prs))
    out = muse.call("ap-draft", prompt, cwd=READ_ROOT, schema=TEAM_MSG, lane="draft-team", item_id=it["id"])
    notes = (out.get("notes") or "").strip()
    team_text, notes = _clean(out["team_text"], notes)
    pr_text, notes = _clean(out["pr_text"], notes)
    pr_links = [p["pr_url"] for p in parts]
    # the squad group is cc'd only in the PR channel, and the ticket link goes in a reply to the PR-channel post
    # (what he changed in every draft he sent by hand, 2026-09-30)
    team_text = _enforce(team_text, evidence, "", pr_links)
    pr_text = _enforce(pr_text, [], tag, pr_links)
    ticket_links = [url for label, url in evidence if label.startswith("ticket")]
    team_thread = _team_thread(d)
    for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"]):
        if json.loads(a["content"] or "{}").get("keep_status"):
            approvals.supersede(a, json.loads(a["content"]), site.t("new version of the messages", "nueva versión de los mensajes"))
    nums = ", ".join(p["pr_url"].split("/")[-1] for p in parts)
    asks = {}
    texts = {}
    for which, text, channel in (("team", team_text, CFG.get("team_channel")), ("pr", pr_text, CFG.get("pr_channel"))):
        if not (text and channel):
            continue
        if which == "team" and team_thread:
            channel = d["channel"]
        payload, _, lint_note = approvals.lint_payload({"channel": channel, "thread_ts": team_thread if which == "team" else None,
                                                        "text": text, "which": which})
        texts[which] = payload["text"]
        label = (site.t("for the team", "para el equipo") if which == "team"
                 else site.t("for the PR channel", "para el canal de PRs"))
        where = site.t("in the thread", "en el hilo") if payload.get("thread_ts") else site.t(f"to <#{channel}>", f"a <#{channel}>")
        ask = approvals.request(it["id"], "slack_send", payload, it["status"],
                                text=site.t(f"📣 *Message {label}* ({nums}), ✅ and I send it {where}:\n\n{payload['text']}",
                                            f"📣 *Mensaje {label}* ({nums}), ✅ y lo mando {where}:\n\n{payload['text']}")
                                     + (site.t(f"\n\n_note (not part of the message): {notes[:600]}_",
                                               f"\n\n_nota (no va en el mensaje): {notes[:600]}_") if notes and which == "team" else "")
                                     # where the ticket link goes is the first install's convention
                                     + (site.own(f"\n\n_el link del ticket va en un reply al post: {' '.join(ticket_links)}_", "")
                                        if which == "pr" and ticket_links else ""),
                                thread_ts=d.get("report_ts"), keep_status=True, lint_note=lint_note)
        asks[which] = ask.get("ts")
    team_text, pr_text = texts.get("team", team_text), texts.get("pr", pr_text)
    db.update_item(it["id"], data={"team_message": {"text": team_text, "team_text": team_text, "pr_text": pr_text, "notes": notes,
                                                    "at": now(), "asks": asks, "ask_ts": asks.get("team"), "sent": prev.get("sent") or {}}})
    log("team messages drafted", item=it["id"])
    return {"team_text": team_text, "pr_text": pr_text}


def announce(it: dict) -> None:
    """Draft the accompanying Slack texts (v1 rule: every PR ships with them)."""
    try:
        if not (it["data"].get("team_message") or {}).get("text"):
            team_message(it)
    except Exception as e:
        log("team message failed", item=it["id"], err=str(e)[:160])
    links = [p["pr_url"] for p in it["data"]["parts"] if p.get("pr_url")]
    d = db.item(it["id"])["data"]
    tm = d.get("team_message") or {}
    if (not (d.get("channel") and d.get("thread_ts")) and tm.get("pr_text")) or (_team_thread(d) and tm.get("team_text")):
        # no source thread: the only announcement is the PR-channel post, and team_message already asks for it
        # (#436 on 10-05, as #24, #180 and #386 before: two asks to post the same PR in the PR channel). A source
        # thread in the team channel is answered by the team message itself (#492 on 10-06: a second ask, 'subí un PR
        # para esto', for the same thread a minute after the team message's)
        db.update_item(it["id"], status="announce_check", data={"announce_draft": None, "pr_links": links})
        return
    schema = {"type": "object", "required": ["thread_reply", "review_ask"], "properties": {
        "thread_reply": {"type": "string"}, "review_ask": {"type": "string"}}}
    prompt = (f"Draft (1) a short reply for the original Slack thread telling them the fix is up as a draft PR, and "
              f"(2) a one-line review ask for the PR channel. Links: {links}. What was fixed: {d.get('done_summary', '')}\n"
              + (f"Known caveats to mention honestly in one clause: {d.get('caveats')}\n" if d.get("caveats") else "")
              + f"Original message: {d.get('text', '')[:2000]}")
    texts = muse.call("ap-draft", prompt, cwd=READ_ROOT, schema=schema, lane="draft-announce", item_id=it["id"])
    db.update_item(it["id"], status="announce_check", data={"announce_draft": texts, "pr_links": links})


def announce_check(it: dict) -> None:
    with claude.pool_ctx("delivery"):
        _announce_check(it)


def _announce_check(it: dict) -> None:
    d = it["data"]
    links = d.get("pr_links") or [p["pr_url"] for p in d.get("parts", []) if p.get("pr_url")]
    schema = {"type": "object", "required": ["thread_reply"], "properties": {
        "thread_reply": {"type": "string"}, "review_ask": {"type": "string"}, "hold": approvals.HOLD}}
    checked = claude.lean("draft", f"Evidence: PRs {links}; summary: {d.get('done_summary', '')}\n\nDraft JSON:\n"
                          + json.dumps(d.get("announce_draft"), ensure_ascii=False), cwd=READ_ROOT, schema=schema, tools=(),
                          item_id=it["id"]) if d.get("announce_draft") else {}
    db.update_item(it["id"], data={"texts": checked, "pr_links": links, "after_approval": "awaiting_human"})
    from . import report
    if not checked:
        payload = None
    elif d.get("channel") and d.get("thread_ts"):
        payload = {"channel": d["channel"], "thread_ts": d["thread_ts"], "text": checked["thread_reply"]}
    elif checked.get("review_ask"):  # no source thread (patrol/backfill/jira): the review ask is the message
        payload = {"channel": CFG["pr_channel"], "thread_ts": None, "text": checked["review_ask"]}
    else:
        payload = None
    note = ("The fix is built, verified and its draft PR is open." if not d.get("caveats") else
            "The fix is built and its draft PR is open, but verification left these points open; say so, never call "
            f"it verified: {d['caveats']}")
    report.post(db.item(it["id"]), "finding", note=note, sendable=payload,
                ask=("slack_send", payload, "announce") if payload else None, hold=(checked.get("hold") or "").strip())
    if not payload:
        db.update_item(it["id"], status="awaiting_human")


VET = {"type": "object", "required": ["real", "why", "kind", "title", "summary", "urgency"], "properties": {
    "real": {"type": "boolean"}, "why": {"type": "string"},
    "kind": {"type": "string", "enum": ["REPLY", "TASK", "INCIDENT", "OPEN_LOOP", "IDEA"]},
    "title": {"type": "string"}, "summary": {"type": "string"}, "urgency": {"type": "integer"}}}
VET_START = {"TASK": "investigate", "INCIDENT": "investigate", "IDEA": "investigate", "OPEN_LOOP": "loop_track",
             "REPLY": "reply_draft"}


def vet(it: dict) -> None:
    """Opus reads the RAW conversation before a small model's proposal becomes a case
."""
    ctx = _context(it)
    owner = it["data"].get("from_reply")
    if owner:  # the answer in this thread is the reply case's; a second REPLY here was a duplicate ask (#412 on 10-02)
        ctx = (f"Case #{owner} already drafts the answer in this thread. This case is the open problem behind it: "
               f"real=true only if that problem (its cause, a fix, who owns it) is still open; kind is never REPLY.\n\n" + ctx)
    (item_dir(it["id"]) / "context.md").write_text(ctx)
    v = claude.lean("vet", ctx, cwd=READ_ROOT, schema=VET, tools=("Read",), add_dirs=(str(item_dir(it["id"])),),
                    item_id=it["id"], urgent=True)
    # the whole why: cut at 300 chars, #441's drop on 10-05 lost the part saying its screenshots could not be read
    db.add_signal("vet", {"real": v["real"], "why": v["why"][:2500], "kind": v.get("kind")}, it["id"])
    if not v["real"]:
        db.update_item(it["id"], status="dropped", note=f"vet: {v['why'][:200]}")
        from . import dm
        dm.close_case_thread(db.item(it["id"]), v["why"])
        return
    kind = v["kind"] if v.get("kind") in VET_START and not (owner and v["kind"] == "REPLY") else it["kind"]
    title = v["title"][:90] if v.get("title") and not v["title"].startswith("fake") else it["title"]
    db.x("UPDATE items SET kind=?, title=?, urgency=? WHERE id=?", kind, title, max(0, min(3, int(v.get("urgency", 1)))), it["id"])
    db.update_item(it["id"], status=VET_START[kind], note=f"vet: real ({kind})",
                   data={"summary": v.get("summary") or it["data"].get("summary"), "vetted": True})
