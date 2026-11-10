"""Observer: a Sonnet pass that reads what the harness actually did in the last hours, the way the user would, and
flags anything wrong or suboptimal, including failure modes nobody wrote a detector for. Input is a compact deterministic snapshot; output merges into
var/incidents.json as kind 'observed'. One call every few hours."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

from . import db, hot, incidents, site
from .core import V1, VAR, log, now

SCHEMA = {"type": "object", "required": ["problems"], "properties": {"problems": {"type": "array", "items": {
    "type": "object", "required": ["title", "evidence", "impact", "why_it_matters"],
    "properties": {"title": {"type": "string"}, "evidence": {"type": "string"},
                   "impact": {"type": "integer", "description": f"1 minor .. 10 {site.first_name()} would be angry"},
                   "why_it_matters": {"type": "string"}, "suspected_mechanism": {"type": "string"}}}}}}
_GUARD = site.path("guard", str(V1 / "guard"))  # the outbound guard's folder: only where one is installed
GUARD_LOG = _GUARD / "guard.log" if _GUARD else None


def guard_summary(entries: list[dict], since: int) -> str:
    """Outbound actions inside the window, each denial with its reason and payload, so a denial can be judged (a
    manual policy probe vs a blocked real send). 2026-09-30: the summary counted the last 400 lines whatever their
    time and gave no reasons, so two install-time probes became an incident with 'cause unclear'."""
    def ts(e: dict) -> float:
        try:
            return datetime.fromisoformat(e.get("at", "")).timestamp()
        except ValueError:
            return 0
    g = [e for e in entries if ts(e) >= since]
    sent = [e for e in g if e.get("allowed") and e.get("action") != "slack_edit_self"]
    denied = [e for e in g if e.get("allowed") is False]
    out = f"Outbound in window (guard): {len(sent)} actions ({', '.join(sorted({e.get('action', '') for e in sent}))}); denied {len(denied)}"
    return out + "".join(f"\n  denied {e.get('at', '')} {e.get('action', '')}: {e.get('why', '')[:120]} | payload "
                         f"{json.dumps(e.get('payload', {}), ensure_ascii=False)[:160]}" for e in denied[-10:])


LANE_ROLE = {"chat": f"{site.first_name()}'s own conversations with the agent",
             "chat_fast": f"{site.first_name()}'s own conversations with the agent",
             "console": f"{site.first_name()}'s own conversations with the agent from the console app (127.0.0.1:4848)",
             "console_fast": "the console's quick answer from the board state, before any deep look (Sonnet, no tools)",
             "ap-triage": "small checks of the triage agent with no lane of their own (promise loops)",
             "triage": "intake triage of new messages, before any case exists",
             "triage2": "second opinion on a message the intake triage ignored",
             "review_relevance": f"Haiku 5.5 check of a review ask found only by the '{site.team_label() or 'keyword'}' word: is the PR "
             "the squad's to review",
             "scan": "Haiku 5.5 reading one fence file (or a 600-line window) at origin/default for real bugs, code as intake",
             "scan-ds": "the free DeepSeek V4.1 Flash lane beside the Haiku scan; optional, a rate limit or dead key skips it",
             "scan_verify": "a stronger model trying to refute a scan finding before it becomes a case",
             "context": f"refresh of what {site.first_name()} is working on, read from Slack for the drafts; a failed one keeps the last, no message waits on it",
             "prioritize": "urgency and moot-check of the open cases",
             "telemetry": "a case's prod telemetry read before its investigation (Sonnet gathers logs/errors/Metabase, "
                          "Opus concludes); a row with no case came from another process (his session, a script)",
             "bridge": "fetching raw Jira, Sentry, Notion, Granola data and Slack files through the claude.ai connectors "
                       "(Datadog is not offered since its 10-12 shutdown)",
             "news_decide": "a case worker's continue/adjust/stop decision on news from its thread (Opus, read-only)",
             "escalate": "the escalation session of a blocked or urgent case: who must act and what to ask",
             "style": "the daily sync of his writing style from the Slack messages he wrote (prompts/style/slack.md)",
             "canary": "the weekly eval's spot-check of the cheap signal judge on a sample of its cases",
             "postmortem": "a teammate answered where we had an unsent draft: whose answer was better, what we lacked",
             "pregate": "the pre-build gate: build, ask first or drop, judged on the dossier",
             "reproduce": "a failing test that reproduces the bug before its fix (Opus, test-only commit). A row with no case "
                          "is a backfill: the reproduction of one of his open PRs written before reproductions were required, "
                          "which he asked for on 10-21; each ends in a `repro backfill` log line with its verdict",
             "report": "the case report he reads in his DM", "draft": "drafts of a Slack text (answer, proposal, notice)",
             "pr_scrub": "removes internal notes from a PR body before it is published",
             "publish_text": "the PR title and body", "self_verify": "a reviewer that verifies the self-review findings",
             "impact": "the Sonnet judge of news in a case thread: none, info, changes the task, redundant, blocker",
             "ap-deps": "merge-order inference between open PRs (which must merge or deploy first), not a dependency or CVE scan",
             "ap-patrol": "background code patrol looking for bugs", "ap-judge": "yes/no checks (covers, duplicates, lessons)",
             "ap-investigate": "investigations and CI diagnoses", "ap-learn": "mining lessons from past cases",
             "board": "the DM board refresh", "observe": "this observer", "sweep": "background sweeps of open loops",
             "verify": "background verification", "noop_audit": "the daily audit of idle cases",
             "dupcheck": "duplicate-case checks", "dupcase": "the duplicate check every new case gets before investigation",
             "vet": "the first look at a new case: real work or noise, before any investigation",
             "reply": "Opus draft of his answer in a conversation case (REPLY), read-only tools",
             "followup": "Sonnet look at news in a conversation he already answered: does it need him, before any Opus redraft"}


def _lane_role(lane: str) -> str:
    if lane.startswith("eval/"):
        return ("regression eval replaying past cases (the weekly run, Fridays 20:00, or the night rule gate): "
                "a measurement, not case work or intake")
    base, _, caller = lane.removeprefix("delivery/").removeprefix("agent-").partition(":")
    return LANE_ROLE.get(caller) or LANE_ROLE.get(base, "role not documented: do not infer it from the name")


def spend_summary(since: int, shown: set[int]) -> str:
    """The window's Claude spend and, by lane, every call the case lines above do not show. Each lane says
    what it does."""
    rows = db.q("SELECT lane, item_id, usd FROM calls WHERE tier='claude' AND at>?", since)
    out = f"Claude spend in window: ${round(sum(r['usd'] or 0 for r in rows), 2)} over {len(rows)} calls"
    # cases past the case lines' limit are named with their outcome: as bare ids in a lane they were filed twice on
    # 10-06 as 'spend on 10 cases that appear nowhere in the case list' (#537, #546, #550-#554: dupcase on new cases)
    rest = [r for r in db.q("SELECT i.id, i.kind, i.status, i.title, round(sum(c.usd), 2) u FROM calls c JOIN items i "
                            "ON i.id=c.item_id WHERE c.tier='claude' AND c.at>? GROUP BY i.id ORDER BY u DESC", since)
            if r["id"] not in shown]
    if rest:
        out += ("\n  cases that spent but are past the case lines above (cheapest): "
                + "; ".join(f"#{r['id']} {r['kind']} [{r['status']}] {r['title'][:40]} ${r['u']}" for r in rest[:60])
                + (f" +{len(rest) - 60} more" if len(rest) > 60 else ""))
    lanes: dict[str, list] = {}
    for r in rows:
        if not r["item_id"]:
            lanes.setdefault(r["lane"] or "?", []).append(r)
    if not lanes:
        return out
    # the dollar figure is the lane's total, said in words: '$5.94 x6' read as $5.94 per call, and 10-19's six deps
    # calls ($5.94 in all) were filed as $35.64, '36% of the window's spend'
    parts = []
    for lane, rs in sorted(lanes.items(), key=lambda kv: -sum(r["usd"] or 0 for r in kv[1]))[:15]:
        parts.append(f"{lane} [{_lane_role(lane)}] ${round(sum(r['usd'] or 0 for r in rs), 2)} total over {len(rs)} calls")
    return out + "\n  calls with no case, by lane (total per lane): " + "; ".join(parts) + _backfills(since)


def _backfills(since: int) -> str:
    """What the no-case reproduce calls bought: each backfill's PR and verdict."""
    recs = [json.loads(r["v"]) for r in db.q("SELECT v FROM kv WHERE k LIKE 'repro_pr:%'")]
    recs = sorted((r for r in recs if (r.get("at") or 0) > since), key=lambda r: r["at"])
    if not recs:
        return ""
    def one(r: dict) -> str:
        case = f" (case #{r['case']})" if r.get("case") else ""
        green = ", green with the PR" if r.get("green_at") else ", not green with the PR"
        return f"{r.get('pr')}{case} {r.get('verdict')}" + (green if r.get("verdict") == "reproduced" else "")
    return "\n  reproductions of his open PRs (backfill) in window: " + "; ".join(one(r) for r in recs)


_USED = {"exact": "sent_by_him_with_our_draft", "light_edit": "sent_by_him_with_our_draft", "rewrite": "sent_by_him_rewritten"}


def _ask_outcome(a: dict) -> str:
    """'sent_by_him' split by how much of our draft he sent."""
    o = a["outcome"] or "pending"
    if o == "sent_by_him":
        p = db.q1("SELECT json_extract(data,'$.cls') cls FROM signals WHERE kind='style_pair' AND item_id=? AND at>=? "
                  "ORDER BY id DESC", a["item_id"], a["created_at"])
        o = _USED.get(p["cls"] if p else None, "sent_by_him_own_text")
    return o


def ask_outcomes(since: int) -> str:
    """Asks to him and what he did with each."""
    acts = db.q("SELECT item_id, outcome, created_at FROM artifacts WHERE kind='approval' AND created_at>? ORDER BY created_at", since)
    out: dict[str, int] = {}
    for a in acts:
        o = _ask_outcome(a)
        out[o] = out.get(o, 0) + 1
    return f"Asks to {site.first_name()} in window: {len(acts)}; outcomes: " + json.dumps(out)


def sleep_summary(since: float) -> str:
    """When the Mac slept inside the window (hot.note_sleep's record: [wake time, length]). The worker runs on his laptop
    and nothing runs while it sleeps; 2026-10-01: asleep 18:52-19:31 and 19:41-21:33, the observer filed '~2h40m dead
    zone: no case touched anywhere between 18:50 and 21:31'."""
    s = [(w - g, w) for w, g in (db.kv_get("sleeps") or []) if w > since and g >= 300]
    if not s:
        return "Machine asleep in window: never."
    hm = lambda t: time.strftime("%H:%M", time.localtime(t))
    return (f"Machine asleep in window ({round(sum(w - a for a, w in s) / 3600, 1)} h; the worker runs on his laptop, nothing "
            "runs while it sleeps, so a gap inside these spans is not a stall): " + ", ".join(f"{hm(a)}-{hm(w)}" for a, w in s))


def pause_summary(since: float) -> str:
    """When the user paused the worker inside the window (hot.pauses): no stage runs then, by his choice."""
    s = [(w - g, w) for w, g in hot.pauses() if w > since and g >= 300]
    if not s:
        return f"Worker paused by {site.first_name()} in window: never."
    hm = lambda t: time.strftime("%a %H:%M", time.localtime(t))
    return (f"Worker paused by {site.first_name()} (no stage runs while paused, so a case not moving inside these spans is his choice, "
            "not a stall): " + ", ".join(f"{hm(a)}-{hm(w)}" for a, w in s))


def limit_summary(since: float) -> str:
    """When the Claude account's usage limit held every model call inside the window (claude._pause's record). Without
    it a stage waiting out the limit read as a stall: 2026-10-06, #555's self_review held 16:21-18:00 was filed as 'a
    1h38m unexplained idle gap', 'outside both stated sleep windows'."""
    s = [(a, w) for a, w in (db.kv_get("claude_pauses") or []) if w > since]
    if not s:
        return "Claude usage limit in window: never."
    hm = lambda t: time.strftime("%H:%M", time.localtime(t))
    return ("Claude usage limit in window (no model call runs until it resets, so stages wait and a case gap inside these "
            "spans is not a stall): " + ", ".join(f"{hm(a)}-{hm(w)}" for a, w in s))


def scheduler_summary() -> str:
    """When the worker's scheduler last ticked (health.json, written every 10 s). Without it a dead scheduler read as a
    quiet harness: on 10-04 at 02:14, 27 h after the scheduler died, the observer saw only the Slack socket's triage
    calls and filed '29 triage calls produced zero cases' and '4 cases stalled with zero escalation'."""
    try:
        at = json.loads((VAR / "health.json").read_text())["at"]
    except Exception:
        return "Worker scheduler: no heartbeat file."
    age = now() - at
    return (f"Worker scheduler: last tick {time.strftime('%a %d %H:%M', time.localtime(at))}"
            + (f", {round(age / 3600, 1)} h ago: nothing is scheduled or worked (ingest, stages, asks) while it is down, "
               "subtract the sleeps above" if age > 900 else ", alive"))


def timeline(item_id: int, since: float) -> str:
    """The case's transitions, the asks and messages it put in his DM, in time order, latest kept when long. From transitions alone
    a case waiting on his ✅ read as stalled: an escalated blocked case keeps its status, so #225, with its request to the
    owner pending in his DM since 13:06, became 'blocked 3 days, no visible handoff'. Each move names the
    stage it entered: with notes only, #436's build read '08:42 > 08:43 > 09:02 > 09:03' and was filed as 'diagnosis
    re-run without converging' an hour before its PR opened. Notes keep their reason: cut at 40 chars,
    #439's 'telemetry skipped: telemetry: logged int' was read as a parse failure."""
    ev = [(t["at"], t["dst"] + (f" ({t['note'][:100]})" if t["note"] else "")) for t in
          db.q("SELECT at, dst, note FROM transitions WHERE item_id=? AND at>?", item_id, since)]
    # an ask's outcome as the window's totals count it, and when it came: a raw 'sent_by_him' at the ask's time read as
    # 'drafted a reply he had already sent'
    for a in db.q("SELECT item_id, created_at, outcome, outcome_at, content FROM artifacts WHERE item_id=? AND "
                  "kind='approval' AND created_at>?", item_id, since):
        when = f" at {time.strftime('%H:%M', time.localtime(a['outcome_at']))}" if a["outcome"] and a["outcome_at"] else ""
        ev.append((a["created_at"], f"ask to {site.first_name()} ({_ask_kind(a)}): {_ask_outcome(a)}{when}"))
    # a case with nothing to approve reaches him by its own DM message (a new one notifies) and its 🔔 pings: with asks
    # alone #680's clean review, told at 09:27, read 'awaiting_human with no visible notification'. The
    # case message an ask is posted with is that ask, but the 🔔 ping is its own notifying message: skipped beside the
    # ask too, #734's 14:11 ask and ping read 'pending 4h+ with no re-ping, while other cases got immediate pings'
    d = (db.item(item_id) or {}).get("data") or {}
    asked = [at for at, n in ev if n.startswith(f"ask to {site.first_name()}")]
    rt = d.get("report_ts")
    if rt and float(rt) > since and not any(abs(float(rt) - a) < 10 for a in asked):
        ev.append((float(rt), f"case message to {site.first_name()}"))
    if d.get("ping_at") and float(d["ping_at"]) > since:
        ev.append((float(d["ping_at"]), f"ping to {site.first_name()}"))
    # an escalation that names him as the person who must act tells him in his DM, with no ask to show for it
    ev += [(at, f"escalation message to {site.first_name()}: {head[:100]}") for at, head in d.get("told") or [] if at > since]
    tl =" > ".join(f"{time.strftime('%H:%M', time.localtime(at))} {n}".strip() for at, n in sorted(ev))
    return before_window(item_id, since) + (tl if len(tl) <= 500 else "... " + tl[-500:])


ASK_FOR = {"team": "✅ to post in the team channel", "pr": "✅ to post in the PR channel",
           "closing": "✅ to post the closing notice", "escalation": "escalation question",
           "slack_send": "✅ to send a Slack reply", "github_review": "✅ to post a GitHub review"}


def _ask_kind(a) -> str:
    """What the ask in his DM waits on him for. As bare 'team' and 'pr', #624's two asks in his DM were read as asks
    sent to the team and PR channels 'not to the user directly' and filed as diffuse ownership."""
    c = json.loads(a["content"] or "{}")
    k = (c.get("payload") or {}).get("which") or c.get("kind") or "?"
    return ASK_FOR.get(k, k)


def before_window(item_id: int, since: float) -> str:
    """What an older case did before the window: its calls, the status it has held since then and the asks it left
    pending in his DM. With the window alone a case waiting two days on his ✅ read 'calls 0 | no timeline': #293 drafted
    its answer for a customer's 15:30 publication at 14:11 on 09-30, and on 10-02 at 15:41 the observer filed 'deadline
    15:30 blown, zero work done in 49.8h' (#297 and #255 the same)."""
    last = db.q1("SELECT dst, at FROM transitions WHERE item_id=? AND at<=? ORDER BY at DESC LIMIT 1", item_id, since)
    if not last:
        return ""
    day = lambda t: time.strftime("%a %d %H:%M", time.localtime(t))
    c = db.q1("SELECT count(*) n, round(coalesce(sum(usd),0),1) u FROM calls WHERE item_id=? AND at<=?", item_id, since)
    # each ask says what it is: a delivery's team-channel and PR-channel asks share their second, and as bare times
    # #624's read 'pending since Thu 08 15:27, Thu 08 15:27', filed as the same ping fired twice. A re-ask
    # says when the ask it replaces first reached him: with its own time only, six asks posted Wed 07 and re-asked on
    # expiry Fri 09 were filed as 'a ~2-day gap between awaiting_approval and the ask'
    asks, first = [], {}
    for a in db.q("SELECT created_at, content, outcome FROM artifacts WHERE item_id=? AND kind='approval' AND "
                  "created_at<=? ORDER BY created_at", item_id, since):
        k = _ask_kind(a)
        first.setdefault(k, a["created_at"])
        if a["outcome"] in ("expired", "superseded"):
            continue
        if not a["outcome"]:
            asks.append(f"{day(a['created_at'])} ({k}"
                        + (f", re-ask, first asked {day(first[k])}" if first[k] < a["created_at"] - 60 else "") + ")")
        first.pop(k)
    return (f"before window: calls {c['n']} ${c['u']}, {last['dst']} since {day(last['at'])}"
            + (f", ask to {site.first_name()} pending since {', '.join(asks)}" if asks else "") + " | ")


def case_prs(d: dict) -> str:
    """The PRs a case delivers, and the PRs a promise waits on (stale.py binds a loop to them as loop_prs). From parts
    alone #26, bound to cometa#1931 with a nudge to its reviewers pending, read 'PRs -' and was filed as 8.8 days
    overdue with no PR and no driver."""
    prs = [re.sub(rf"^.*github\.com/(?:{site.owners_re()})/", "", p["pr_url"]) for p in d.get("parts", []) if p.get("pr_url")]
    return ", ".join(prs + [f"waits on {rp}#{n}" for rp, n in d.get("loop_prs") or []])


def running() -> dict[int, str]:
    """The stage each worker thread is running now, by case. A case mid-build looked idle between its moves: the 19 min
    of #436's build call read as 'a stalled/blocked call that then resumed'."""
    out = {}
    for r in db.q("SELECT k FROM kv WHERE k LIKE 'stage_started:%'"):
        st = db.kv_get(r["k"]) or {}
        if st.get("item"):
            out[st["item"]] = f"running now: {st.get('status')} since {time.strftime('%H:%M', time.localtime(st.get('at', 0)))} | "
    return out


def case_line(r, since: float, live: dict[int, str]) -> str:
    d = json.loads(r["data"] or "{}")
    tl = timeline(r["id"], since)
    c = db.q1("SELECT count(*) n, round(coalesce(sum(usd),0),1) u FROM calls WHERE item_id=? AND at>?", r["id"], since)
    # a call that timed out across a sleep is the sleep's, as for the incident collector: #137's and #367's impact
    # judges, 2100 s over the 10-09 night, read 'failed 1' and were filed as a lane re-processing closed cases
    bad = [incidents.failed_asleep(f) for f in db.q("SELECT at, dur_s, note FROM calls WHERE item_id=? AND at>? AND ok=0",
                                                     r["id"], since)]
    failed = f"failed {bad.count(False)}" + (f" (+{sum(bad)} timed out across a sleep)" if any(bad) else "")
    prs = case_prs(d)
    # age counts the nights the Mac slept through: #763, 8.4 h old and 0.5 h of it awake, was filed on 10-14 as 'an
    # incident sat unworked for 8.4h'
    age, awake = (now() - r["created_at"]) / 3600, hot.awake_since(r["created_at"], now()) / 3600
    age_s = f"{round(age, 1)}h" + (f" (awake {round(awake, 1)}h)" if age - awake >= 1 else "")
    # a reply's verification child carries the parent's title and text by design: without the link #608 (verifying
    # #607's answer) was filed as 'a duplicate case from the same Slack message'
    kin = (f"verifies #{d['parent']}'s answer ({len(d.get('checks') or [])} checks) | " if d.get("parent") else
           f"its answer is verified by #{d['child']} | " if d.get("child") else "")
    return (f"- #{r['id']} {r['kind']} [{r['status']}] {r['title'][:70]} | {kin}age {age_s} | "
            f"calls {c['n']} ${c['u']} {failed} | PRs {prs or '-'} | {live.get(r['id'], '')}{tl}")


# what each stage name means, said for the names the snapshot shows: bare, #870's 'gate > self_find', running 4 min
# after its gate passed, was read as 'fell back to self_find (re-running discovery from scratch)' and filed as $22.8 burned
# on a fix that silently reset
STAGE_MEANING = {
    "investigate": "builds the dossier", "telemetry": "reads prod telemetry", "vet": "decides whether a case is worth work",
    "plan": "writes the change plan", "critique": "a critic reads the plan (back to plan = a revision)",
    "build": "writes the code in a worktree", "gate": "deterministic checks on the diff (size, lint, types, tests)",
    "fix": "repairs what gate or self-review flagged, then back to gate",
    "self_find": "self-review, after the gate passed: finders read the diff", "self_review": "self-review: judges the findings",
    "publish": "pushes the branch and opens the draft PR", "ci_wait": "waits on CI", "ci_fix": "repairs a CI failure",
    "verify_done": "checks the delivered work against the ask", "pr_body_refresh": "rewrites the PR body",
    "announce": "drafts the delivery notice", "announce_check": "checks that notice", "waiting": "waits on a named dependency",
    "propose": f"drafts a proposal for {site.first_name()}", "propose_check": "checks the proposal before it is asked",
    "review_prepare": "checks out a PR to review", "review_find": "finders read the PR", "review_verify": "verifies the findings",
    "review_post": "posts the review once he approves", "reply_draft": "drafts a Slack answer", "reply_check": "checks that answer",
    "loop_track": "follows a promise until its PR lands", "comments_triage": "sorts PR comments",
    "comments_verify": "verifies the answers to them", "comments_reply": "posts those answers once approved",
    "awaiting_approval": "waits on his ✅", "awaiting_human": "waits on his answer", "blocked": "stopped, its note says why",
}


def stage_legend(text: str) -> str:
    used = [s for s in STAGE_MEANING if re.search(rf"(?<![\w-]){s}(?![\w-])", text)]
    return "Stage names (a code case runs plan > critique > build > gate <> fix > self_find > self_review > publish): " + \
        "; ".join(f"{s} = {STAGE_MEANING[s]}" for s in used)


def snapshot(hours: int = 6) -> str:
    since = now() - hours * 3600
    lines = [f"Window: last {hours} h (now {time.strftime('%a %d %H:%M')}).", sleep_summary(since), pause_summary(since), limit_summary(since),
             scheduler_summary()]
    shown: set[int] = set()
    live = running()
    # every case touched: its timeline, calls, cost, PRs, and what it told the user. Cases that spent in the window come
    # first: by recency alone, 40 old asks re-touched at the digest pushed out #428 and #437, and their reply calls were
    # filed as 'spend on cases invisible in the case list'
    cols = "i.id, i.kind, i.status, i.title, i.created_at, i.updated_at, i.data"
    for r in db.q(f"SELECT {cols} FROM items i LEFT JOIN "
                  "(SELECT item_id, sum(usd) u FROM calls WHERE at>? GROUP BY item_id) s ON s.item_id=i.id "
                  "WHERE i.updated_at>? OR s.item_id IS NOT NULL "
                  "ORDER BY s.item_id IS NOT NULL DESC, coalesce(s.u, 0) DESC, i.updated_at DESC LIMIT 40", since, since):
        shown.add(r["id"])
        lines.append(case_line(r, since, live))
    # the cases the hints name get a line too, first: a case idle all window has no move in it, so 'stuck x1 (#644)'
    # pointed at a case missing from the list and was filed as 'stuck cases absent from the visible case list'
    hints = signal_hints(hours)
    ids = re.findall(r"\d+", " ".join(re.findall(r"\(#(\d+(?:, #\d+)*)\)", hints)))  # cases()'s ' (#644, #646)', not PR numbers
    named = [int(n) for n in dict.fromkeys(ids) if int(n) not in shown][:10]
    if named:
        rows = db.q(f"SELECT {cols} FROM items i WHERE i.id IN ({','.join('?' * len(named))})", *named)
        lines[4:4] = [case_line(r, since, live) for r in rows]
        shown.update(r["id"] for r in rows)
    # what went to the user's DM and what he did with it
    tail = [ask_outcomes(since)]
    # guard log: what actually left the machine
    try:
        if GUARD_LOG:
            tail.append(guard_summary([json.loads(l) for l in GUARD_LOG.read_text().splitlines()[-2000:]], since))
    except Exception:
        pass
    # spend and pauses
    tail.append(spend_summary(since, shown))
    tail.append(hints)
    tail.append(stage_legend("\n".join(lines)))
    # the cap cuts the case lines, never the window-wide tail the case lines are read against
    end = "\n" + "\n".join(tail)
    return "\n".join(lines)[:max(0, 24000 - len(end))] + end


def signal_hints(hours: int) -> str:
    """The deterministic signals still open, as hints only. Never this observer's own earlier findings: labelled
    deterministic, they read as fresh evidence."""
    top = [i for i in incidents.collect(hours) if i["kind"] != "observed" and i["status"] not in ("fixed", "wontfix")][:12]
    def cases(i: dict) -> str:  # 10-04: 'casos trabados en reply_draft x2' gave no case to look at
        ids = list(dict.fromkeys(re.findall(r"\bitem (\d+)", " ".join(i.get("evidence") or []))))
        return f" (#{', #'.join(ids[:8])})" if ids else ""
    return "Deterministic signals (hints): " + "; ".join(f"{i['kind']}: {i['title'][:70]} x{i['count']}{cases(i)}" for i in top)


# a window the Mac slept through has nothing to judge: on 10-14 it was awake 20 min of 00:23-06:23 (dark wakes), and the
# pass filed six incidents ('#763 sat unworked 8.4h', '10 post+delete cycles', 'context ran stale') out of the night
MIN_AWAKE_S = 3600


def run(hours: int = 6) -> int:
    from . import claude
    awake = hot.awake_since(now() - hours * 3600, now())
    if awake < MIN_AWAKE_S:
        log("observer: window slept through, skipped", awake_min=round(awake / 60))
        return 0
    paused = hot.paused_since(now() - hours * 3600, now())
    if awake - paused < MIN_AWAKE_S:
        log("observer: window paused by the user, skipped", paused_min=round(paused / 60))
        return 0
    snap = snapshot(hours)
    (VAR / "observer-input.txt").write_text(snap)
    out = claude.lean("observe", f"You review the last hours of an autonomous engineering agent that works as {site.first_name()} (a "
                      "senior engineer at "
                      f"{site.company() or 'his company'}): it reads Slack/GitHub/Jira, opens PRs, answers reviewers and reports to his "
                      "Slack DM. Read the activity "
                      f"below as {site.first_name()} would and list every problem you see: work that stalled or took too long, wrong or "
                      "low-quality "
                      "outcomes, noise or duplicates sent to him, things he had to do himself, money or tokens spent without result, "
                      "loops, cases closed wrongly, missing follow-through. Be concrete and cite the case ids and times. Do not report "
                      "things that are fine. Guess the mechanism behind each if you can.\n\n" + snap,
                      model="sonnet", cwd=site.primary_path(), schema=SCHEMA, tools=())
    with incidents._Lock():
        return _merge(out)


def _merge(out: dict) -> int:
    data = json.loads(incidents.OUT.read_text()) if incidents.OUT.exists() else {"incidents": [], "state": {}}
    added = 0
    for p in out.get("problems", []):
        sig = incidents._sig("observed", p["title"][:80].lower())
        if any(i["sig"] == sig for i in data["incidents"]):
            continue
        data["incidents"].append({"sig": sig, "kind": "observed", "title": p["title"][:160], "impact": float(p.get("impact", 3)),
                                  "count": 1, "first": now(), "last": now(), "status": "open", "attempts": [],
                                  "evidence": [p["evidence"][:600], f"why: {p['why_it_matters'][:300]}",
                                               f"mechanism?: {p.get('suspected_mechanism', '')[:300]}"]})
        added += 1
    data["incidents"].sort(key=lambda i: -i["impact"])
    incidents.OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    db.kv_set("observer_at", now())
    log("observer", problems=len(out.get("problems", [])), added=added)
    return added
