"""Backfill: nothing pending from BEFORE the harness was running may be missed. Runs at worker start when the last sweep is older than ~20h (so it also covers
laptop downtime), and nightly.

Sources, each reconciled against current state before becoming an item:
  slack    14 days of his channels + threads + DMs + mentions: unanswered asks to him,
           promises he made, in-fence incidents nobody owns
  github   unresolved review threads on his open PRs (the comment cursor baselines history,
           so unresolved old threads come from here)
  jira     open tickets assigned to him (via the verified MCP bridge), matched to PRs
  v1       leftovers: pending bundles, open loops, escalations
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import bridge, context, cover, db, gh, muse, site, slack
from .core import CFG, V1, log, now

DAYS = 14


def _slack_corpus() -> str:
    oldest = f"{time.time() - DAYS * 86400:.6f}"
    chans = {c: slack.channel_name(c).lstrip("#")
             for c in (CFG.get("team_channel"), CFG.get("pr_channel"), *CFG.get("watched_channels", [])) if c}
    me, team = CFG["self_user_id"], set(CFG.get("team_members", {})) - {"_note"}
    mine = f" ({site.marker()})"
    queries = (f"<@{me}>",) + tuple(f"<!subteam^{s}>" for s in CFG.get("team_subteam_tags", []))
    out, threads = [], {}
    for ch, name in chans.items():
        try:
            msgs = slack.history(ch, oldest, pages=8)
        except Exception as e:
            log("backfill: history failed", ch=ch, err=str(e)[:120])
            continue
        lines = []
        for m in msgs:
            if not slack.is_human(m):
                continue
            text = m.get("text") or ""
            lines.append(f"[{ch}:{m['ts']}] {slack.user_name(m.get('user'))}{mine if m.get('user') == me else ''}: "
                         f"{slack.render(text)[:600]}" + (f" (+{m['reply_count']} replies)" if m.get("reply_count") else ""))
            if m.get("reply_count") and (any(q[:-1] in text for q in queries) or m.get("user") in team | {me}):
                threads[(ch, m["ts"])] = 1
        if lines:
            out.append(f"## #{name}\n" + "\n".join(lines[-400:]))
    # an ask found by search is usually a reply in a thread nobody else expanded (a support root, another channel),
    # so its answer was never in the corpus: 22 of 28 backfill cases up to 2026-10-01 were dropped by vet because
    # "the raw thread shows it was answered" (#346: a teammate's ask at 17:56, his "sii, ya lo estamos" at 17:57)
    # the self-DM is the harness's own reports, posted as him: never a source, as in ingest (all 10 backfill cases
    # from it, 09-27 to 10-06, were its digests and nudges; #526 'Merge schools#993' was the PR digest's merge order)
    mentions, self_dm = [], CFG["self_dm_channel"]
    for qy in queries:
        try:
            ms = slack.search(f"{qy} -in:<#{self_dm}> after:{time.strftime('%Y-%m-%d', time.localtime(time.time() - DAYS * 86400))}", count=100)
        except Exception:
            continue
        ms = [m for m in ms if (m.get("channel") or {}).get("id") != self_dm]
        mentions.append((qy, ms))
        for m in ms:
            ch, _, root = context._slack_ref(m.get("permalink", ""))
            if ch:
                threads[(ch, root)] = 1
    for ch, ts in sorted(threads, key=lambda t: float(t[1]))[-120:]:
        try:
            rs = slack.replies(ch, ts)
        except Exception:
            continue
        if len(rs) < 2:
            continue
        out.append(f"## thread {ch}:{ts}\n" + "\n".join(
            f"[{ch}:{r['ts']}] {slack.user_name(r.get('user'))}{mine if r.get('user') == me else ''}: "
            f"{slack.render(r.get('text'))[:500]}" for r in rs[:1] + rs[1:][-40:]))
    for qy, ms in mentions:
        out.append(f"## mentions ({qy})\n" + "\n".join(
            f"[{(m.get('channel') or {}).get('id')}:{m['ts']}] #{(m.get('channel') or {}).get('name')} "
            f"{slack.user_name(m.get('user'))}: {slack.render(m.get('text'))[:500]} ({m.get('permalink', '')})" for m in ms))
    return "\n\n".join(out)[-400000:]


def slack_sweep() -> int:
    corpus = _slack_corpus()
    open_items = "\n".join(f"- {r['kind']}: {r['title']}" for r in db.q(
        "SELECT kind, title FROM items WHERE status NOT IN ('done','dropped','expired','failed')"))
    schema = {"type": "object", "required": ["missing"], "properties": {"missing": context.MISSING}}
    prompt = (f"Two weeks of {site.first_name()}'s Slack (he is marked {site.marker()}; refs are [channel:ts]). Find everything still "
              f"PENDING for him as of now: questions or requests addressed to him or his squad with no answer from him "
              "afterwards, things he promised" + site.own(" (\"lo miro\", \"mañana lo hago\", \"le pido a X\")", "") + " with no sign they "
              f"happened, in-fence incidents or bugs reported that nobody closed, decisions waiting on him. Read the "
              f"LATER messages (an ask's whole thread is under '## thread <channel>:<root ts>'): if it was answered, "
              f"solved, or someone else took it, it is not pending. For each give "
              f"kind, title, why (with the evidence), ref as a Slack permalink "
              f"{site.slack_base()}/archives/<channel>/p<ts without dot>, urgency 0-3, in_fence.\n"
              f"Fence: {CFG['domain_fence']}\n\nAlready tracked (do not repeat):\n{open_items}\n\n{corpus}")
    out = muse.call("ap-investigate", prompt, cwd=site.primary_path(), schema=schema, lane="backfill",
                    timeout=2400)
    return context.create_missing(out["missing"], "backfill")


def github_unresolved() -> int:
    """Unresolved review threads on his open PRs whose last word is not his."""
    made = 0
    q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
      reviewThreads(first:100){nodes{isResolved isOutdated comments(first:20){nodes{databaseId author{login} body path line url}}}}}}}"""
    for r in db.q("SELECT repo, number, data FROM prs WHERE own=1"):
        p = gh.run(["gh", "api", "graphql", "-f", f"query={q}", "-F", f"o={site.repo_org(r['repo'])}",
                    "-F", f"r={site.repo_name(r['repo'])}", "-F", f"n={r['number']}"], timeout=60)
        if p.returncode != 0:
            continue
        nodes = (((json.loads(p.stdout).get("data") or {}).get("repository") or {}).get("pullRequest") or {}) \
            .get("reviewThreads", {}).get("nodes", [])
        pending = []
        for t in nodes:
            cs = t["comments"]["nodes"]
            if t["isResolved"] or not cs or (cs[-1]["author"] or {}).get("login") == CFG["self_github"]:
                continue
            c = cs[0]
            pending.append({"id": cs[-1]["databaseId"], "user": (cs[-1]["author"] or {}).get("login"), "path": c.get("path"),
                            "line": c.get("line"), "body": "\n---\n".join(x["body"][:1500] for x in cs)[-3000:],
                            "url": c.get("url"), "outdated": t["isOutdated"]})
        key = f"comments:{r['repo']}#{r['number']}"
        if pending and not db.open_item_for_thread(key):
            from .babysit import _needs
            _needs("COMMENTS", r["repo"], r["number"], {**gh.pr_view(r["repo"], r["number"])}, {"comments": pending, "from": "backfill"})
            made += 1
    return made


def jira_sweep() -> int:
    try:
        issues = bridge.jira_assigned()
    except Exception as e:
        log("backfill: jira failed", err=str(e)[:300])
        db.add_signal("backfill_source_failed", {"source": "jira", "err": str(e)[:300]})
        return 0
    prs = [json.loads(r["data"]) | {"repo": r["repo"]} for r in db.q("SELECT repo, data FROM prs WHERE own=1")]
    pr_list = "\n".join(f"- {p['repo']}#{p['number']}: {p['title']} ({p.get('headRefName')})" for p in prs)
    schema = {"type": "object", "required": ["tickets"], "properties": {"tickets": {"type": "array", "items": {
        "type": "object", "required": ["key", "state", "why"], "properties": {
            "key": {"type": "string"}, "state": {"type": "string", "enum": ["covered_by_pr", "needs_work", "stale", "not_mine"]},
            "prs": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"},
            "urgency": {"type": "integer"}}}}}}
    def classify(batch: list[dict]) -> list[dict]:
        return muse.call("ap-investigate", "Open Jira tickets assigned to "
                         f"{site.first_name()}:\n{json.dumps(batch, ensure_ascii=False)[:40000]}\n\n"
                         f"His open PRs:\n{pr_list}\n\n{context.render(limit=5000)}\n\nFor each ticket (one entry per key, every "
                         f"key): covered_by_pr (an open PR fixes it; list which), needs_work (nothing covers it yet), stale (old "
                         f"backlog with no recent activity or superseded), not_mine. Read code/PRs when needed.",
                         cwd=site.primary_path(), schema=schema, lane="backfill-jira")["tickets"]
    by_key = {i["key"]: i for i in issues}
    verdicts = [t for t in classify(issues) if t.get("key") in by_key]
    # A ticket the classifier left out is not a verdict (2026-10-06 06:25: 11 tickets in, '{"tickets": []}' out, so
    # TICKET-1411 and TICKET-1415, the tickets he asked about the day before, got no case and no log line). Ask once for the
    # missing ones; a ticket still without a verdict gets a case, where investigate judges it with the code at hand.
    missing = {k: i for k, i in by_key.items() if k not in {t["key"] for t in verdicts}}
    if missing:
        verdicts += [t for t in classify(list(missing.values())) if t.get("key") in missing]
    seen = {t["key"] for t in verdicts}
    verdicts += [{"key": k, "state": "needs_work", "why": site.t("the classifier gave no verdict for this ticket",
                                                                 "el clasificador no dio veredicto para este ticket")}
                 for k in by_key if k not in seen]
    log("backfill: jira verdicts", tickets=len(by_key), unclassified=len(by_key) - len(seen),
        states={s: sum(1 for t in verdicts if t["state"] == s) for s in {t["state"] for t in verdicts}})
    made = 0
    for t in verdicts:
        i = by_key.get(t["key"], {})
        tk = f"jira:{t['key']}"
        text = f"{t['key']} {i.get('summary')}\n{i.get('description', '')}"
        if db.open_item_for_thread(tk) or db.q1("SELECT 1 FROM items WHERE thread_key=? AND status='done' AND "
                                                 "json_extract(data,'$.linked_prs') LIKE '%pr_url%'", tk):
            continue  # a ticket linked to a covering PR waits for that merge (closing.py), not for a new case
        # a case that ended on this ticket as it still reads judged it: the verdict stands until the ticket changes
        # (2026-10-06: SIS-1623..1635, dropped by their dossiers at 08:38, were cases again at 08:43, and TICKET-1368, a
        # duplicate of #228, twice; past the 12 h dedupe window each sweep would pay their investigation again)
        if db.q1("SELECT 1 FROM items WHERE thread_key=? AND status IN ('dropped','done') AND "
                 "json_extract(data,'$.text')=?", tk, text):
            continue
        if t["state"] in ("needs_work", "covered_by_pr"):
            # 'covered_by_pr' from a listing of PR titles is a claim, not proof (2026-10-01: tickets left with no case
            # and nothing to close them). The ticket always gets a case; a PR that provably covers it links it, and
            # closing.py closes the ticket when that PR merges. Unproven, the case is worked like any other.
            iid = db.create_item("TASK", "investigate", f"{t['key']}: {i.get('summary', '')[:80]}", tk,
                                 urgency=max(1, min(3, int(t.get("urgency") or (3 if i.get("priority") in ("Highest", "High") else 1)))),
                                 data={"summary": t["why"], "text": text,
                                       "jira": t["key"], "from": "backfill"})
            made += 1
            if t["state"] == "covered_by_pr" and iid:
                db.add_signal("jira_covered", {"key": t["key"], "prs": t.get("prs"), "why": t["why"]}, iid)
                for ref in t.get("prs") or []:
                    m = re.search(rf"({site.repo_names_re()})(?:#|/pull/)(\d+)", ref or "")
                    prf = cover.proof(db.item(iid), m.group(1), int(m.group(2))) if m else None
                    if prf:
                        cover.link(db.item(iid), prf)
                        break
    return made


def v1_leftovers() -> int:
    blobs = []
    for f in sorted((V1 / "state/queue").glob("pending-*.json"))[-40:] + sorted((V1 / "queue").glob("pending-*.json"))[-40:]:
        try:
            d = json.loads(f.read_text())
            blobs.append({"file": f.name, "kind": d.get("classification") or d.get("kind"), "title": d.get("title"),
                          "source": d.get("source_permalink") or d.get("permalink"), "summary": str(d)[:800]})
        except Exception:
            continue
    ol = V1 / "state/open-loops.json"
    if ol.exists():
        for l in json.loads(ol.read_text()).get("loops", []):
            if l.get("status") == "open":
                blobs.append({"file": "open-loops", "kind": "OPEN_LOOP", "title": l.get("action"), "source": l.get("source")})
    if not blobs:
        return 0
    # v1 is paused, so these files never change: the same blobs were judged again on every daily sweep (10 Claude calls,
    # 1.6 USD, 10-05 to 10-13) and one 'still relevant' re-opened a 13/08 a customer thread as #716 on 10-11 (#66 had been
    # it on 09-23). A set of leftovers is judged once; only a change in v1's files is judged again
    import hashlib
    seen = hashlib.sha1(json.dumps(blobs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    if db.kv_get("v1_leftovers_judged") == seen:
        return 0
    schema = {"type": "object", "required": ["missing"], "properties": {"missing": context.MISSING}}
    out = muse.call("ap-triage", f"Leftover work items from the previous version of the assistant (paused 2026-08-19). "
                    f"Given the current Slack context, return only the ones that are STILL relevant and pending today.\n\n"
                    f"{context.render(limit=5000)}\n\nLeftovers:\n{json.dumps(blobs, ensure_ascii=False)[:60000]}",
                    cwd=site.primary_path(), schema=schema, lane="backfill-v1")
    made = context.create_missing(out["missing"], "v1")
    db.kv_set("v1_leftovers_judged", seen)
    return made


def granola_sweep() -> int:
    """Next steps assigned to the user (or unassigned and the team) from this week's dailies."""
    import hashlib
    from . import bridge
    if not (CFG.get("granola") or {}).get("enabled"):
        return 0
    try:
        items = bridge.granola_next_steps()
    except bridge.BridgeUnverified as e:
        if db.kv_get("granola_unavailable_day") != db.today():
            log("granola: MCP unavailable", err=str(e)[:160])
            db.kv_set("granola_unavailable_day", db.today())
        return 0
    n = 0
    for x in items:
        who = (x.get("assignee") or "").lower()
        mine = any(k in who for k in site.name_fragments()) or who in ("unassigned", "", "sin asignar")
        if not mine:
            continue
        key = f"granola:{x.get('meeting_id')}:{hashlib.sha1(x['item'].encode()).hexdigest()[:10]}"
        if db.q1("SELECT 1 FROM items WHERE thread_key=?", key):
            continue
        db.create_item("TASK", "investigate", f"(daily) {x['item'][:80]}", key, urgency=2,
                       data={"text": f"{x['meeting']} ({x['date']}): {x['item']}\n" + site.t("Assigned to: ", "Asignado: ") + str(x.get('assignee')),
                            "summary": x["item"][:300], "source": "granola", "author": "granola",
                            "search_terms": [t for t in x.get("slack_terms") or [] if t.strip()][:3]})
        n += 1
    log("granola sweep", items=len(items), created=n)
    return n


def run(force: bool = False) -> dict:
    last = db.kv_get("backfill_at", 0)
    if not force and now() - last < 20 * 3600:
        return {"skipped": True}
    res = {}
    from . import ingest
    for name, fn in (("channels", ingest.channel_health), ("jira", jira_sweep), ("slack", slack_sweep), ("v1", v1_leftovers), ("granola", granola_sweep)):
        try:
            res[name] = fn()
        except Exception as e:
            res[name] = f"failed: {type(e).__name__}: {e}"[:300]
            log("backfill source failed", source=name, err=res[name])
    if not any(isinstance(v, str) and v.startswith("failed") for v in res.values()):
        db.kv_set("backfill_at", now())  # only a clean sweep counts; failures retry next hour
    db.kv_set("backfill_last", res)
    log("backfill done", **res)
    return res
