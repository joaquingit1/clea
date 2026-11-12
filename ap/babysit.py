"""Babysit the user's own open PRs (periodic, mostly deterministic).

Per PR: CI red -> CI item (flaky rerun or fix through the task pipeline's gates);
behind base while approved -> update branch; new review comments -> COMMENTS item
(verify each, fix the valid ones, draft replies/defenses); approved + green -> merge
ONLY through merge_order.gate (dependency order + deploy levels + migration hold);
ready but unreviewed / approvals dismissed -> templated nudge drafts (his phrasing);
merged -> announcement draft. Text to humans is always a draft on the board.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import approvals, claude, db, gh, merge_order, muse, outbound, site, worktree, slack
from .core import CFG, item_dir, log, now, run
from .tasks import MECHANICAL, READ_ROOT, SELF_VERIFY, light_round

BOTS = merge_order.BOTS
# a case in these owns its PR's CI and threads: babysit opens no CI/COMMENTS item beside it. `waiting` is one of them
# (CI red outside our control, tasks.waiting rechecks and resumes): left out, babysit saw #555 waiting on a base
# breakage at 18:21 on 10-06 and opened #585 for the same schools#1003, two loops rerunning the same red CI.
ACTIVE = ("build", "gate", "self_find", "self_review", "fix", "publish", "ci_wait", "ci_fix", "waiting", "verify_done",
          "announce", "comments_triage", "comments_verify", "comments_reply")


_READY_TOLD: list[tuple[str, str]] = []  # (label, url) of PRs that came up ready in this pass, notified together at its end


def _state(repo, n) -> dict:
    return db.kv_get(f"pr:{repo}#{n}", {})


def _set_state(repo, n, st) -> None:
    db.kv_set(f"pr:{repo}#{n}", st)


def _managed(repo: str, n: int) -> bool:
    # a blocked case with a PR still owns it: its escalation retakes it or worker.deliver_blocked delivers it within
    # minutes (2026-10-16: #870 blocked on cometa#3057 at 17:05:10, babysit opened #894 on the same PR at 17:06:49, so a
    # retake would have had two cases pushing one branch). A blocked CI/COMMENTS item stays out: those can sit for days.
    for r in db.q(f"SELECT data, status FROM items WHERE status IN {ACTIVE} "
                  "OR (status='blocked' AND kind IN ('TASK','INCIDENT','IDEA'))"):
        for p in json.loads(r["data"]).get("parts", []):
            if p.get("repo") == repo and p.get("pr") == n:
                return True
    return False


def _needs(kind: str, repo: str, n: int, pr: dict, extra: dict) -> None:
    key = f"{kind.lower()}:{repo}#{n}"
    if db.open_item_for_thread(key):
        return
    wt = worktree.for_existing_branch(repo, pr["headRefName"])
    part = {"repo": repo, "branch": pr["headRefName"], "wt": str(wt), "pr": n, "pr_url": pr["url"], "state": "pushed",
            "start_sha": pr["headRefOid"]}
    status = "ci_fix" if kind == "CI" else "comments_triage"
    iid = db.create_item(kind, status, f"{kind.lower()} {repo}#{n}: {pr['title'][:70]}", key, repo=repo, pr=n,
                         urgency=2, data={"parts": [part], "cur": 0, "plan": {"title": pr["title"]}, **extra})
    db.x("INSERT OR IGNORE INTO branches VALUES(?,?,?,?)", repo, pr["headRefName"], iid, now())
    log(f"babysit: {kind} item", item=iid, repo=repo, pr=n)


def _check_rollup(pr: dict) -> tuple[bool, bool, bool]:
    checks = pr.get("statusCheckRollup") or []
    pending = any(c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING") for c in checks)
    failed = any((c.get("conclusion") or "").upper() in ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR")
                 or (c.get("state") or "").upper() in ("FAILURE", "ERROR") for c in checks)
    return bool(checks) and not pending and not failed, pending, failed


def run_once() -> dict:
    stats = {"prs": 0, "merged": 0, "items": 0}
    own = {(r["repo"], r["number"]): json.loads(r["data"]) for r in db.q("SELECT * FROM prs WHERE own=1")}
    full = {}
    for (repo, n) in own:
        try:
            full[(repo, n)] = gh.pr_view(repo, n)
        except Exception as e:
            log("babysit: view failed", repo=repo, pr=n, err=str(e)[:160])
    merge_order.refresh(full, unread=set(own) - set(full))
    _READY_TOLD.clear()
    for (repo, n), pr in full.items():
        stats["prs"] += 1
        try:
            _one(repo, n, pr, stats)
        except Exception as e:
            log("babysit: PR failed", repo=repo, pr=n, err=f"{type(e).__name__}: {e}"[:300])
    if _READY_TOLD:  # one notification per pass: 18 PRs were ready on 10-22, 18 banners would be noise
        one = len(_READY_TOLD) == 1
        mac_notify(_READY_TOLD[0][0] if one else ", ".join(t.split(":")[0] for t, _ in _READY_TOLD),
                   site.t("Ready to merge", "Listo para mergear") if one
                   else site.t(f"{len(_READY_TOLD)} PRs ready to merge", f"{len(_READY_TOLD)} PRs listos para mergear"),
                   _READY_TOLD[0][1] if one else None)
    return stats


def _one(repo: str, n: int, pr: dict, stats: dict) -> None:
    st = _state(repo, n)
    if pr["state"] != "OPEN":
        if st.get("merge_ready") or st.get("merge_block"):
            _set_state(repo, n, {**st, "merge_ready": None, "merge_block": None})
        return
    green, pending, failed = _check_rollup(pr)
    managed = _managed(repo, n)
    if not st and not managed and site.backlog(pr.get("updatedAt")):
        # open since before this install started and untouched since: not work it was asked to take on. The first
        # push, comment or review on it makes it current, and then its red CI and open threads are picked up as usual
        return
    # 1. CI red, or the branch conflicts with its base (nothing turns that green by itself)
    conflicting = pr.get("mergeable") == "CONFLICTING"
    if (failed or conflicting) and not managed and st.get("ci_sha") != pr["headRefOid"]:
        busy = worktree.joaquin_busy_on(repo, pr["headRefName"])
        if busy:
            # digest reads the opening ("CI rojo" / "CI red") to tell red CI from other blocks
            st["blocked"] = (site.t(f"{'conflicts with main' if conflicting else 'CI red'}, but {busy}: not touching that branch",
                                    f"{'conflictos con main' if conflicting else 'CI rojo'}, pero {busy}: no toco esa branch"))
        else:
            st["blocked"] = None
            _needs("CI", repo, n, pr, {})
            st["ci_sha"] = pr["headRefOid"]
            stats["items"] += 1
    # 2. review threads: every unresolved thread whose last word isn't the user's is work
    #    (his rule: never leave unresolved CodeRabbit threads on his PRs); plus new conversation
    #    comments from humans (cursor-based)
    threads = gh.unresolved_threads(repo, n, CFG["self_github"])
    issue_cs = [c for c in gh.issue_comments(repo, n) if c["user"]["login"] != CFG["self_github"]
                and not any(b in c["user"]["login"].lower() for b in ("vercel", "github-actions", "coderabbit"))]
    if "last_comment_id" not in st:
        st["last_comment_id"] = max([c["id"] for c in issue_cs], default=0)
    new_issue = [c for c in issue_cs if c["id"] > st["last_comment_id"]]
    work = [{"id": t_["last_comment_id"], "reply_to": t_["comment_id"], "thread_id": t_["thread_id"],
             "user": t_["last_author"], "bot": _is_bot(t_["first_author"]), "path": t_["path"],
             "line": t_["line"], "body": t_["body"], "url": t_["url"], "outdated": t_["outdated"]} for t_ in threads]
    work += [{"id": c["id"], "user": c["user"]["login"], "bot": False, "path": None, "line": None,
              "body": c["body"][:3000], "url": c.get("html_url")} for c in new_issue]
    sig = sorted(w["id"] for w in work)
    bot_open = [w for w in work if w.get("bot") and w.get("path")]
    live = db.q1("SELECT id, status, data FROM items WHERE kind='COMMENTS' AND repo=? AND pr=? AND status NOT IN "
                 "('done','dropped','expired','failed') ORDER BY id DESC", repo, n)
    if work and live and live["status"] not in ("waiting", "ci_wait", "awaiting_approval") and st.get("threads_sig") != sig:
        # mid-pipeline (fix, gate, self-review, reply): queue the new threads for the next round
        #
        ld = json.loads(live["data"] or "{}")
        known = {c["id"] for c in ld.get("comments", [])} | {c["id"] for c in ld.get("pending_comments", [])} | set(ld.get("bot_replied_ids") or [])
        fresh = [w for w in work if w["id"] not in known]
        if fresh:
            db.update_item(live["id"], data={"pending_comments": (ld.get("pending_comments") or []) + fresh})
            log("babysit: new threads queued for the live item", item=live["id"], n=len(fresh))
        st["threads_sig"] = sig
    elif work and live and st.get("threads_sig") != sig:
        # new threads landed while the item waits (deploy, CI, his ok): feed them to that item instead
        # of losing them behind the 'managed' guard
        ld = json.loads(live["data"] or "{}")
        done_ids = set(ld.get("bot_replied_ids") or []) | {r["comment_id"] for r in ld.get("comment_replies", [])}
        fresh = [w for w in work if w["id"] not in done_ids]
        if fresh:
            db.update_item(live["id"], status="comments_triage", attempts=0, next_run_at=0,
                           note=site.t(f"{len(fresh)} new threads", f"{len(fresh)} hilos nuevos"),
                           data={"comments": fresh, "comments_replied": False, "round_reported": False,
                                 "wait_after_reply": {k: ld.get(k) for k in ("waiting_for", "wait_resume", "wait_since")}
                                 if live["status"] == "waiting" else ld.get("wait_after_reply")})
            log("babysit: new threads fed to live item", item=live["id"], n=len(fresh))
        st["threads_sig"] = sig
    elif work and not managed and st.get("threads_sig") != sig:
        _needs("COMMENTS", repo, n, pr, {"comments": work})
        st["threads_sig"] = sig
        stats["items"] += 1
    elif bot_open and not managed and now() - st.get("threads_retry_at", 0) > 86400:
        # same threads as last time and still unresolved by a bot: the earlier item did not honor
        # the rule (e.g. "only nits"), so answer them now
        _needs("COMMENTS", repo, n, pr, {"comments": work})
        st["threads_retry_at"] = now()
        stats["items"] += 1
    if new_issue:
        st["last_comment_id"] = max(c["id"] for c in issue_cs)
    st["unresolved_threads"] = len(threads)
    # 3. merge / update / nudge
    approved = merge_order.human_approved(repo, n, pr["headRefOid"]) if not pr["isDraft"] else False
    ready = False
    if approved and pr.get("mergeStateStatus") == "BEHIND" and st.get("updated_sha") != pr["headRefOid"]:
        outbound.update_branch(repo, pr)
        st["updated_sha"] = pr["headRefOid"]
    elif (not pr["isDraft"] and pr.get("mergeStateStatus") == "BLOCKED" and st.get("ci_kick_sha") != pr["headRefOid"]
          and gh.missing_required(repo, pr)):
        # a required check that never ran on this head (a workflow added after it was pushed) only runs on a new push:
        # bring main in, once per head. CodeRabbit re-approves the new head
        try:
            outbound.update_branch(repo, pr)
            log("babysit: required check missing, branch updated to run it", repo=repo, pr=n)
        except Exception as e:
            log("babysit: update for missing required check failed", repo=repo, pr=n, err=str(e)[:200])
        st["ci_kick_sha"] = pr["headRefOid"]
    if approved and green:
        try:
            outbound.merge(repo, pr)
            stats["merged"] += 1
            st["merge_block"] = None
            merged = site.t(f"merged {pr['title']} 🚀 {pr['url']}", f"mergeado {pr['title']} 🚀 {pr['url']}")
            st["announce"] = {"text": merged, "at": now()}
            _ask_text(repo, n, pr, merged, "announce", new_message=True)
        except outbound.Blocked as b:
            st["merge_block"] = str(b)
    elif not pr["isDraft"] and green and not approved:
        # a green, thread-free, unapproved PR of his always has exactly one live ask for eyes per head:
        # re-approval phrasing when a HUMAN approval was dismissed by a push, first-ask phrasing otherwise.
        # _ask_text dedupes per (kind, head), so this is idempotent
        revs = gh.reviews(repo, n)
        human_dismissed = any(r["state"] == "DISMISSED" and (r.get("user") or {}).get("login") != CFG["self_github"]
                              and not _is_bot((r.get("user") or {}).get("login", "")) for r in revs)
        ready_for = time.time() - time.mktime(time.strptime(pr["updatedAt"][:19], "%Y-%m-%dT%H:%M:%S"))
        bot_wait = not threads and _coderabbit_waits(repo, n, pr, revs, st)
        if pr.get("reviewDecision") == "APPROVED":
            ready = _ready_to_merge(repo, n, pr, st)
        elif bot_wait:
            pass  # CodeRabbit's verdict on this head comes first; a teammate is asked only if the repo needs one
        elif human_dismissed:
            ask = site.t("my last push dismissed the approvals, could you approve it again please?",
                         "se me fueron los approves con el último push, me lo re-aprueban porfa?")
            st["nudge"] = {"text": f"{ask} {pr['url']}", "at": now()}
            st["reapproval_sha"] = pr["headRefOid"]
            _ask_text(repo, n, pr, ask, "reapproval")
        elif not _human_activity(repo, n) and ready_for > CFG.get("pr_babysit", {}).get("nudge_unreviewed_after_min", 30) * 60:
            ask = site.t("could you take a look when you get a chance?", "le pueden dar una mirada cuando puedan?")
            st["nudge"] = {"text": f"{ask} {pr['url']}", "at": now()}
            st["nudged_day"] = db.today()
            _ask_text(repo, n, pr, ask, "nudge")
    if not ready:
        st["merge_ready"] = None  # merge_ready_told survives: a CI re-run after a base update is not news
    st["summary"] = {"title": pr["title"], "url": pr["url"], "green": green, "pending": pending, "failed": failed,
                     "approved": approved, "review": pr.get("reviewDecision"), "draft": pr["isDraft"], "merge_state": pr.get("mergeStateStatus"),
                     "conflicting": conflicting, "base": pr.get("baseRefName")}
    _set_state(repo, n, st)


def _coderabbit_waits(repo: str, n: int, pr: dict, revs: list[dict], st: dict) -> bool:
    """Is CodeRabbit's verdict on this head still to come? On a green PR with no open thread CodeRabbit either
    approves the head or has stopped reviewing it ('Review paused' after many pushes), and only '@coderabbitai review'
    restarts it: asked once per head, after 20 min for its own review of a push to land, then 1 h for the answer.
    In cometa its approval satisfies the ruleset, so cometa#1931 sat green, deps live, all 9 threads resolved and
    paused since 10-02 while a 'le pueden dar una mirada' ask for teammates waited on him, until he asked on 10-05."""
    head = pr["headRefOid"]
    cr = [r for r in revs if "coderabbit" in ((r.get("user") or {}).get("login") or "").lower()]
    status = [c for c in pr.get("statusCheckRollup") or [] if "coderabbit" in (c.get("context") or c.get("name") or "").lower()]
    if not cr and not status:
        return False  # CodeRabbit does not review this repo
    if any(r["state"] == "APPROVED" and r.get("commit_id") == head for r in cr):
        return False
    seen = st.get("cr_head") or {}
    if seen.get("sha") != head:
        st["cr_head"] = seen = {"sha": head, "at": now(), "asked": False}
    if any((c.get("state") or c.get("status") or "").upper() in ("PENDING", "IN_PROGRESS", "QUEUED") for c in status):
        return True  # reviewing right now
    if not seen["asked"]:
        if now() - seen["at"] < 1200:
            return True
        outbound.request_bot_review(repo, n, "@coderabbitai review")
        seen.update(asked=True, at=now())
        log("babysit: asked CodeRabbit to review the head", repo=repo, pr=n, head=head[:10])
        return True
    return now() - seen["at"] < 3600


def _ready_to_merge(repo: str, n: int, pr: dict, st: dict) -> bool:
    """The ruleset is satisfied without a teammate (GitHub's reviewDecision counts CodeRabbit's approval where the repo
    allows it) but the harness merges only on a human approval, so the merge is his: once the merge order holds too,
    he is told once per head and the asks for teammates' eyes are withdrawn. Returns whether it is ready."""
    ok, why = merge_order.gate(repo, pr)
    rest = [w for w in why if w != "no current human approval" and not w.startswith("contains a migration")]
    st["merge_block"] = "; ".join(rest)[:200] or None
    if rest:
        return False
    if (st.get("merge_ready") or {}).get("sha") != pr["headRefOid"]:
        st["merge_ready"] = {"sha": pr["headRefOid"], "at": now()}
    # told at most once a day per PR: the harness keeps merging main into his branches (a new head each time) and CI
    # re-runs (not ready for a few minutes), so "once per head" re-sent this every ~40 min (2026-10-06: 15 identical
    # pings for cometa#2499 in one day). The board lists ready PRs anyway.
    if now() - (st.get("merge_ready_told") or 0) > 86400:
        st["merge_ready_told"] = now()
        for old in db.q("SELECT id FROM items WHERE kind='NUDGE' AND repo=? AND pr=? AND status='awaiting_approval'", repo, n):
            a = db.q1("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", old["id"])
            if a:
                approvals.supersede(a, json.loads(a["content"]), site.t("it is already approved: no need to ask for a review",
                                                                        "ya está aprobado: no hace falta pedir review"))
            db.update_item(old["id"], status="done", note="approved without a teammate")
        # no DM of its own: the board lists ready PRs.
        # A Mac notification instead
        _READY_TOLD.append((f"{repo}#{n}: {pr['title']}", pr["url"]))
        log("babysit: ready to merge (on the board + Mac notification)", repo=repo, pr=n)
    return True


def mac_notify(text: str, subtitle: str, url: str | None = None) -> None:
    """A macOS notification. Clea.app shows it (GET /api/notifications, a click opens the PR): an osascript banner is
    filed under Script Editor, which was not even registered with Notification Center on 10-22, so it could not be
    told apart from no banner. When the app has not polled in 5 minutes (closed), osascript is the fallback.
    Best effort: a failed notification never blocks babysit."""
    if CFG.get("mode") == "shadow" or not CFG.get("pr_babysit", {}).get("mac_notify", True):
        return
    q = db.kv_get("notify_queue") or []
    q.append({"id": (q[-1]["id"] + 1) if q else 1, "at": now(), "subtitle": subtitle, "body": text[:200], "url": url})
    db.kv_set("notify_queue", q[-100:])
    if now() - (db.kv_get("notify_polled_at") or 0) < 300:
        return
    try:  # argv, not string splicing, so a quote in a PR title cannot break the AppleScript
        r = run(["osascript", "-e", "on run argv", "-e",
                 'display notification (item 1 of argv) with title "Clea" subtitle (item 2 of argv) sound name "Glass"',
                 "-e", "end run", text[:200], subtitle], timeout=15)
        if r.returncode:
            log("mac notification failed", rc=r.returncode, err=r.stderr[-200:])
    except Exception as e:
        log("mac notification failed", err=str(e)[:200])


def _ask_text(repo: str, n: int, pr: dict, text: str, kind: str, new_message: bool = False) -> None:
    """v1 rules 5-7: a PR nudge goes as a thread reply under the user's own PR-channel message for
    that PR when one exists, else as a new PR-channel message with the link; merges are
    announced. Always his OK first (it is text to humans), once per head sha."""
    from . import approvals
    tk = f"{kind}:{repo}#{n}:{pr['headRefOid'][:10]}"
    if db.q1("SELECT 1 FROM items WHERE thread_key=?", tk):
        return
    if kind != "announce":  # one live ask per PR: an older nudge/reapproval for the same PR is superseded
        if _case_asks_channel(pr["url"]):
            return
        for old in db.q("SELECT id FROM items WHERE kind='NUDGE' AND repo=? AND pr=? AND status='awaiting_approval'", repo, n):
            a = db.q1("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", old["id"])
            if a:
                approvals.supersede(a, json.loads(a["content"]), site.t("replaced by a newer ask for this PR",
                                                                        "reemplazado por un pedido más nuevo para este PR"))
            db.update_item(old["id"], status="done", note="superseded by a newer ask")
    chan = CFG.get("announce_channel") or CFG["pr_channel"] if kind == "announce" else CFG["pr_channel"]
    thread_ts = None
    if not new_message:
        try:
            mine = [m for m in slack.search(f"{pr['url']} from:<@{CFG['self_user_id']}>")
                    if (m.get("channel") or {}).get("id") == chan]
            if mine:
                thread_ts = slack.thread_ts_from_permalink(mine[-1].get("permalink")) or mine[-1]["ts"]
        except Exception as e:
            log("babysit: pr-channel search failed", err=str(e)[:120])
    if thread_ts is None:
        text = f"{text} {pr['url']}"
    iid = db.create_item("NUDGE", "awaiting_approval", f"{kind} {repo}#{n}", tk, repo=repo, pr=n, urgency=1,
                         data={"after_approval": "done", "pr_url": pr["url"]})
    approvals.request(iid, "slack_send", {"channel": chan, "thread_ts": thread_ts, "text": text}, "awaiting_approval")


def _case_asks_channel(url: str) -> bool:
    """The case that built the PR already has its own PR-channel message for it waiting on his ✅ (its team
    message): a nudge would ask the squad twice."""
    for a in db.q("SELECT a.content FROM artifacts a JOIN items i ON i.id=a.item_id WHERE a.kind='approval' "
                  "AND a.outcome IS NULL AND i.kind!='NUDGE' AND a.content LIKE ?", f"%{url}%"):
        payload = json.loads(a["content"] or "{}").get("payload") or {}
        if payload.get("channel") == CFG["pr_channel"] and re.search(re.escape(url) + r"(?!\d)", payload.get("text") or ""):
            return True
    return False


BOT_LOGIN = re.compile(r"\[bot\]|coderabbit|github-actions|vercel|sonar|codecov|dependabot|renovate|copilot|-bot$|^bot", re.I)


def _is_bot(login: str) -> bool:
    return bool(BOT_LOGIN.search(login or ""))


def _human_activity(repo: str, n: int) -> bool:
    for r in gh.reviews(repo, n):
        login = (r.get("user") or {}).get("login", "")
        if login != CFG["self_github"] and not any(b in login.lower() for b in BOTS):
            return True
    return False


# ---- COMMENTS pipeline stages ---------------------------------------------------
TRIAGE = {"type": "object", "required": ["comments"], "properties": {"comments": {"type": "array", "items": {
    "type": "object", "required": ["id", "verdict", "reason"],
    "properties": {"id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["valid", "invalid", "question", "nit", "noise"]},
                   "reason": {"type": "string"}, "fix": {"type": "string"}}}}}}


def comments_triage(it: dict) -> None:
    part = it["data"]["parts"][0]
    d = item_dir(it["id"])
    (d / "comments.json").write_text(json.dumps(it["data"]["comments"], ensure_ascii=False, indent=1))
    approved = merge_order.human_approved(it["repo"], it["pr"], "")
    frozen = (f"\nTHIS PR IS ALREADY APPROVED: it is frozen except for critical issues and real bugs ({site.first_name()}'s rule). "
              "Style, polish and minor suggestions are NOT fixed now: classify them as invalid with a short reason "
              "(deferred as follow-up), so they get answered and resolved without touching the code.\n") if approved else ""
    t = muse.call("ap-investigate", frozen + f"Review comments left on {site.first_name()}'s PR {part['pr_url']} (worktree {part['wt']} at "
                  f"the PR head). For each comment decide: valid (a real problem to fix), invalid (reviewer is wrong or "
                  f"the tradeoff is deliberate; say why), question (needs an answer, no code change), nit (cheap "
                  f"optional polish), noise (bot summaries, walkthroughs). Read the code before deciding.\n"
                  f"Comments: {d / 'comments.json'}", cwd=part["wt"], schema=TRIAGE, lane="comments", item_id=it["id"])
    (d / "comments-triage.json").write_text(json.dumps(t, ensure_ascii=False, indent=1))
    by_id = {c["id"]: c for c in it["data"]["comments"]}
    actionable = [c for c in t["comments"] if c["verdict"] in ("valid", "invalid", "question")
                  or (c["verdict"] == "nit" and by_id.get(c["id"], {}).get("bot") and by_id.get(c["id"], {}).get("path"))]
    # a bot's review thread is never left as "only a nit": the user's rule is fix or defense + resolve
    if not actionable:
        return db.update_item(it["id"], status="done", note="only nits/noise")
    db.update_item(it["id"], status="comments_verify", data={"frozen": frozen})


def comments_verify(it: dict) -> None:
    part = it["data"]["parts"][0]
    d = item_dir(it["id"])
    frozen = it["data"].get("frozen", "")
    v = claude.lean("self_verify", frozen + f"Worktree: {part['wt']}. These are reviewer comments on {site.first_name()}'s PR plus a "
                    "cheaper "
                    f"model's triage. Confirm which ones require a code fix (confirmed) and which are wrong or "
                    f"deliberate (dismissed, with the defense).\nComments: {d / 'comments.json'}\n"
                    f"Triage: {d / 'comments-triage.json'}\nAlso answer `mechanical` for the confirmed ones together. "
                    + MECHANICAL, cwd=part["wt"],
                    schema={**SELF_VERIFY, "properties": {**SELF_VERIFY["properties"], "mechanical": {"type": "boolean"}}},
                    add_dirs=(str(d), READ_ROOT), item_id=it["id"])
    (d / "comments-verify.json").write_text(json.dumps(v, ensure_ascii=False, indent=1))
    part["defenses"] = v["dismissed"]
    it["data"]["parts"][0] = part
    if v["confirmed"]:
        humans = sorted({c.get("user") for c in it["data"].get("comments", []) if c.get("user") and not c.get("bot") and not _is_bot(c.get("user") or "")})
        built_by_us = db.q1("SELECT 1 FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND json_extract(data,'$.parts') LIKE ?",
                            f"%{part.get('pr_url')}%")
        if humans and built_by_us and not it["data"].get("lesson_done"):
            try:
                from . import learn
                learn.lesson_from_review(it, v["confirmed"], ", ".join(humans))
            except Exception as e:
                log("review lesson failed", item=it["id"], err=str(e)[:160])
            db.update_item(it["id"], data={"lesson_done": True})
        light, stepped = light_round(it["data"], bool(v.get("mechanical")))
        return db.update_item(it["id"], status="fix", data={
            "parts": it["data"]["parts"], "fix_rounds": 0, "fix_source": "review comments", "fix_light": light, "fix_stepped_up": stepped,
            "fix_problems": [f"{c['file']}:{c.get('line', '')} {c['problem']} -> {c['fix']}" for c in v["confirmed"]]})
    db.update_item(it["id"], status="comments_reply", data={"parts": it["data"]["parts"]})


def _still_red(it: dict) -> bool:
    from .tasks import ci_red
    try:
        return ci_red(gh.pr_view(it["repo"], it["pr"]))
    except Exception:
        return True  # unknown: keep waiting; tasks.waiting rechecks in 30 min


def comments_reply(it: dict) -> None:
    d = item_dir(it["id"])
    part = it["data"]["parts"][0]
    head = run(["git", "-C", part["wt"], "rev-parse", "--short", "HEAD"]).stdout.strip()
    schema = {"type": "object", "required": ["replies"], "properties": {"replies": {"type": "array", "items": {
        "type": "object", "required": ["comment_id", "text"], "properties": {
            "comment_id": {"type": "integer"}, "text": {"type": "string"}}}}}}
    drafts = muse.call("ap-draft", f"Draft {site.first_name()}'s reply to each actionable review comment on his PR. Fixed ones: "
                       f"open flat on the substance and point to the fix commit {head}"
                       + site.own(" (\"si, era eso: ...\" / \"tenes razon, era eso...\"; never 'buen catch')", "")
                       + ". Dismissed ones: a short question-framed defense with the reason. Questions: answer. "
                       + site.own("Lowercase, no ¿, no code blocks.", "Lowercase, no code blocks.")
                       + f"\nComments: {(d / 'comments.json').read_text()[:8000]}\n"
                       f"Verification: {(d / 'comments-verify.json').read_text()[:8000] if (d / 'comments-verify.json').exists() else ''}",
                       cwd=READ_ROOT, schema=schema, lane="draft-comments", item_id=it["id"])
    by_id = {c["id"]: c for c in it["data"].get("comments", [])}
    replies = [{"comment_id": r["comment_id"], "text": r["text"], "user": by_id.get(r["comment_id"], {}).get("user"),
                "reply_to": by_id.get(r["comment_id"], {}).get("reply_to"),
                "thread_id": by_id.get(r["comment_id"], {}).get("thread_id"),
                "bot": by_id.get(r["comment_id"], {}).get("bot", False) or _is_bot(by_id.get(r["comment_id"], {}).get("user") or ""),
                "review_comment": bool(by_id.get(r["comment_id"], {}).get("path"))} for r in drafts["replies"]
               if r["comment_id"] in by_id]
    bots = [r for r in replies if r["bot"]]
    humans = [r for r in replies if not r["bot"]]
    # a human's finding that we fixed in the code gets its thread resolved right after the reply he approves
    #
    try:
        triage = json.loads((d / "comments-triage.json").read_text()) if (d / "comments-triage.json").exists() else {"comments": []}
        valid = {c["id"] for c in triage["comments"] if c.get("verdict") == "valid"}
    except Exception:
        valid = set()
    fixed = bool(it["data"].get("fix_source") == "review comments" and it["data"].get("fix_problems"))
    for r in humans:
        r["resolve"] = fixed and r["comment_id"] in valid
    # CodeRabbit threads: the user's rule is that none stay unresolved and they need no ask
    #: each was verified (fix committed or reasoned
    # defense), so reply and resolve directly. Human reviewers' threads still go through his OK.
    posted = set(it["data"].get("bot_replied_ids") or [])
    bots = [r for r in bots if r["comment_id"] not in posted]  # a retry after a later failure must not re-post
    if bots:
        from .approvals import execute
        execute("github_replies", {"repo": it["repo"], "pr": it["pr"], "replies": bots, "resolve_bot_threads": True})
        posted |= {r["comment_id"] for r in bots}
        db.update_item(it["id"], data={"bot_replied_ids": sorted(posted)})
        db.add_signal("coderabbit_resolved", {"repo": it["repo"], "pr": it["pr"], "n": len(bots)}, it["id"])
    db.update_item(it["id"], data={"comment_replies": replies, "after_approval": "done", "bot_replies_done": len(posted)})
    # this is HIS PR: he gets a case report (what was flagged, by whom, valid or not, what was done and
    # what he may still want to change), never a "review" of his own work
    from . import report
    root_ts = it["data"].get("report_ts")
    try:
      if not it["data"].get("round_reported"):
        root_ts = report.post(db.item(it["id"]), "finding", note=(
            f"Review threads on {site.first_name()}'s OWN pull request. For each thread: what was flagged and by whom (CodeRabbit, a "
            "GitHub Actions scanner, a teammate), whether it is valid, and what was done: fixed in commit <sha>, or "
            "answered and resolved as a false positive with the reason. Every thread, bot or teammate, is already "
            "answered on GitHub and the fixed ones resolved (his standing rules). Close with what he may still want to "
            "decide, e.g. fix a dismissed point anyway. First line: " + site.t("`Your PR <repo>#<n>: <gist>`.", "`Tu PR <repo>#<n>: <gist>`.")))
        db.update_item(it["id"], data={"round_reported": True})
    except Exception as e:
        log("comments report failed", item=it["id"], err=str(e)[:200])
    db.update_item(it["id"], data={"comments_replied": True})
    # the user: "when fixing my PRs due to people's comments, answer their GitHub comments directly,
    # don't wait for my approval, and resolve the thread if applicable". Human threads are answered like bot
    # threads; a thread we fixed is resolved, a question or a defense is answered and left open for them.
    posted_h = set(it["data"].get("human_replied_ids") or [])
    fresh_h = [r for r in humans if r["comment_id"] not in posted_h]
    if fresh_h:
        from .approvals import execute
        execute("github_replies", {"repo": it["repo"], "pr": it["pr"], "replies": fresh_h})
        posted_h |= {r["comment_id"] for r in fresh_h}
        db.update_item(it["id"], data={"human_replied_ids": sorted(posted_h)})
        db.add_signal("human_threads_answered", {"repo": it["repo"], "pr": it["pr"], "n": len(fresh_h),
                                                  "resolved": sum(1 for r in fresh_h if r.get("resolve"))}, it["id"])
    humans = []  # nothing left for his ✅
    pending = it["data"].get("pending_comments") or []
    if pending and not humans:
        # threads that arrived during this round: same item, next round, right away
        return db.update_item(it["id"], status="comments_triage", attempts=0, next_run_at=0,
                              note=site.t(f"{len(pending)} new threads", f"{len(pending)} hilos nuevos"),
                              data={"comments": pending, "pending_comments": [], "comments_replied": False, "round_reported": False,
                                    "fix_source": None, "fix_problems": None})
    if humans:
        approvals.request(it["id"], "github_replies", {"repo": it["repo"], "pr": it["pr"], "replies": humans}, "comments_reply",
                          thread_ts=root_ts)
    elif it["data"].get("wait_after_reply") and _still_red(it):
        # the wait is consumed here: tasks.waiting resumes to ci_wait once CI is not red, and ci_wait closes a
        # COMMENTS item whose threads are answered (2026-10-01: a stale wait_after_reply sent #196 back to
        # 'waiting' with CI green, every 30 min)
        w = it["data"]["wait_after_reply"]
        db.update_item(it["id"], status="waiting", next_run_in=1800, note="threads answered; CI red by design",
                       data={**w, "wait_after_reply": None})
    else:
        db.update_item(it["id"], status="done", note=f"{len(bots)} bot threads answered and resolved")
