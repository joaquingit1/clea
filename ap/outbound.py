"""Every externally visible action goes through here. Mode and capability flags are
enforced in code; in shadow mode each action is recorded (var/shadow-actions.jsonl)
instead of executed, so a shadow run shows exactly what act mode would have done.

Safety invariants (v1's, now code):
  - Slack posts only to the user's self-DM. Teammate-facing text is left as a composer
    draft or copy-ready text on the board; the user sends it himself.
  - Pushes only to branches the harness created (branches table), never to defaults,
    and only with a cr-gate marker equal to HEAD (the prepush-review contract).
  - PRs are opened as drafts and verified draft after creation.
  - GitHub reviews are created PENDING (no event); the user submits them.
  - Merges only through merge_order.gate.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import db, gh, site, slack
from .core import CFG, VAR, log, now, run
from .worktree import harness_owns

SHADOW_LOG = VAR / "shadow-actions.jsonl"


class Blocked(Exception):
    pass


class BranchMoved(Blocked):
    """origin/<branch> holds commits HEAD lacks: someone pushed while we worked on an older head."""


def remote_moved(wt: Path, branch: str) -> str | None:
    """The remote tip of <branch> when it is not in HEAD's history (a push would be rejected
    non-fast-forward), else None. A branch not on origin yet, or ls-remote failing, is None."""
    r = run(["git", "-C", str(wt), "ls-remote", "origin", f"refs/heads/{branch}"], timeout=120)
    tip = (r.stdout.split() or [""])[0]
    if r.returncode != 0 or not tip:
        return None
    ok = run(["git", "-C", str(wt), "merge-base", "--is-ancestor", tip, "HEAD"], timeout=60).returncode == 0
    return None if ok else tip


SELF_DM_CAPS = ("board_to_slack", "urgent_pings")


def acting(cap: str) -> bool:
    """Act mode enables every capability flag. Shadow mode still posts to the user's own
    self-DM (board + urgent pings) when shadow_self_dm is on: only he can see it, and it
    is how he watches a shadow run."""
    if not CFG["capabilities"].get(cap, False):
        return False
    return CFG.get("mode") == "act" or (cap in SELF_DM_CAPS and CFG.get("shadow_self_dm", False))


def _shadow(action: str, **detail) -> dict:
    with SHADOW_LOG.open("a") as f:
        f.write(json.dumps({"at": now(), "action": action, **detail}, ensure_ascii=False, default=str) + "\n")
    log(f"shadow: {action}", **{k: (str(v)[:120]) for k, v in detail.items()})
    return {"shadow": True}


def push(repo: str, wt: Path, branch: str) -> dict:
    if not harness_owns(repo, branch) or branch in (CFG["repos"][repo]["default"], "main", "master", "develop"):
        raise Blocked(f"refusing to push {repo}:{branch} (not a harness branch)")
    head = run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    gitdir = run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"]).stdout.strip()
    gate = Path(gitdir, "cr-gate")
    if not gate.exists() or gate.read_text().strip() != head:
        raise Blocked("cr-gate marker != HEAD: self-review has not passed on this exact commit")
    from .gates import cr_pass_head
    if CFG.get("quality", {}).get("coderabbit_gate", True) and cr_pass_head(wt) != head:
        # prepush-review: a completed CodeRabbit pass on this exact HEAD, not just the harness's own marker
        raise Blocked("no completed CodeRabbit pass on this HEAD (prepush-review)")
    if not acting("push_branches"):
        return _shadow("push", repo=repo, branch=branch, head=head)
    from .worktree import joaquin_busy_on
    busy = joaquin_busy_on(repo, branch)
    if busy:
        raise Blocked(f"no pusheo a {branch}: {busy}")
    tip = remote_moved(wt, branch)
    if tip:  # retrying the same push can only be rejected again
        raise BranchMoved(site.t(f"{branch} moved to {tip[:7]} while I was working on an older head",
                                 f"{branch} se movió a {tip[:7]} mientras trabajaba sobre un head anterior"))
    r = run(["git", "-C", str(wt), "push", "origin", f"HEAD:refs/heads/{branch}"], timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"push failed: {r.stderr[-600:]}")
    log("pushed", repo=repo, branch=branch, head=head)
    return {"head": head}


def edit_pr(repo: str, number: int, title: str, body: str, item_id: int) -> None:
    """Re-sync an existing PR's title/body from the ledger after fix rounds.
    A failed edit raises, so the stage retries it: swallowed, publish moved on to ci_wait and announce with the PR
    still carrying an earlier stage's text."""
    if not acting("draft_prs"):
        return _shadow("pr_edit", repo=repo, number=number, title=title, body=body[:4000])
    body_file = VAR / f"pr-body-{item_id}.md"
    body_file.write_text(body)
    from .guardcall import act
    try:
        act("gh_pr_op", repo=repo, pr=number, op="edit", title=title, body=body)
    except Exception as e:
        log("pr edit failed", repo=repo, pr=number, err=str(e)[-300:])
        raise


def open_draft_pr(repo: str, branch: str, title: str, body: str, item_id: int) -> dict:
    if not acting("draft_prs"):
        return _shadow("pr_create_draft", repo=repo, branch=branch, title=title, body=body[:4000])
    body_file = VAR / f"pr-body-{item_id}.md"
    body_file.write_text(body)
    ready = CFG.get("prs_ready_directly", True)  # the user, 2026-09-29: "stop doing drafts, do them ready directly"
    from .guardcall import act
    res = act("gh_pr_create", repo=repo, head=branch, base=CFG["repos"][repo]["default"], title=title, body=body, draft=not ready)
    url, n = res["html_url"], int(res["number"])
    db.add_artifact(item_id, "draft_pr", url, title)
    return {"url": url, "number": n}


def mark_ready(repo: str, n: int) -> bool:
    if not acting("draft_prs"):
        return False
    from .guardcall import act
    try:
        act("gh_pr_op", repo=repo, pr=n, op="ready")
        return True
    except Exception as e:
        log("mark_ready failed", repo=repo, pr=n, err=str(e)[:120])
        return False


def pending_review(repo: str, n: int, head_sha: str, body: str, comments: list[dict], item_id: int) -> dict:
    if not acting("pending_reviews"):
        return _shadow("pending_review", repo=repo, pr=n, body=body, comments=comments)
    from .guardcall import act
    res = act("gh_pending_review", repo=repo, pr=n, head_sha=head_sha, body=body, comments=comments)
    ref = f"{repo}#{n}:review:{res['id']}"
    db.add_artifact(item_id, "pending_review", ref, json.dumps({"body": body, "comments": comments}, ensure_ascii=False))
    return {"review_id": res["id"], "url": res.get("html_url")}


def merge(repo: str, pr: dict, item_id: int | None = None) -> dict:
    from .merge_order import gate
    ok, why = gate(repo, pr)
    if not ok:
        raise Blocked("; ".join(why))
    if not acting("auto_merge"):
        return _shadow("merge", repo=repo, pr=pr["number"], head=pr["headRefOid"])
    from .guardcall import act
    act("gh_merge", repo=repo, pr=pr["number"], head_sha=pr["headRefOid"], method=CFG["merge"]["method"])
    db.add_signal("merged", {"repo": repo, "pr": pr["number"]}, item_id)
    return {"merged": True}


def update_branch(repo: str, pr: dict) -> dict:
    if not acting("update_branch"):
        return _shadow("update_branch", repo=repo, pr=pr["number"])
    from .guardcall import act
    act("gh_pr_op", repo=repo, pr=pr["number"], op="update_branch", head_sha=pr["headRefOid"])
    return {"updated": True}


def request_bot_review(repo: str, pr: int, body: str) -> dict:
    """A review command to a bot on his own PR ('@coderabbitai review'): bot threads need no ask (his rule, 09-23)."""
    if not acting("submit_approved_reviews"):
        return _shadow("bot_review_request", repo=repo, pr=pr, body=body)
    from .guardcall import act
    act("gh_reply_own_pr", repo=repo, pr=pr, body=body)
    return {"requested": True}


def rerun(repo: str, pr: int, run_id: int) -> dict:
    """The caller's PR, which the guard checks: deriving it from the run looked up a gh.prs_for_run that never existed,
    so every rerun since the guard went out as PR 0 and was denied with a 404 (#405, 10-01 21:47)."""
    if not acting("rerun_flaky_ci"):
        return _shadow("rerun", repo=repo, pr=pr, run_id=run_id)
    from .guardcall import act
    act("gh_pr_op", repo=repo, pr=pr, op="rerun", run_id=run_id)
    return {"rerun": True}


# ---- Slack --------------------------------------------------------------------
def board_post(text: str, ts: str | None) -> str | None:
    """Create or edit the living board in the self-DM; returns its ts. The part after the split marker
    (the per-epic PR block) is one reply under the board, edited in place."""
    from .digest import SPLIT
    (VAR / "board.txt").write_text(text.replace(SPLIT + "\n", ""))
    if not acting("board_to_slack"):
        return ts
    root, *details = text.split("\n" + SPLIT + "\n")
    if not details and len(root) > 3400:  # no marker: split at the last section boundary that fits
        cut = root.rfind("\n\n", 0, 3380)
        root, details = (root[:cut], [root[cut + 2:]]) if cut > 0 else (root[:3380].rstrip() + "\n…", [])
    root = root if len(root) <= 3400 else root[:3380].rstrip() + "\n…"
    details = [x for d_ in details for x in slack._split(d_, 3400)]
    if ts:
        try:
            slack.edit_self(ts, root)
        except Exception as e:
            err = str(e)
            gone = any(w in err for w in ("message_not_found", "cant_update_message", "edit_window_closed"))
            if not gone:  # network/timeout: keep the board, the next refresh retries
                log("board edit failed, keeping it", err=err[:200])
                return ts
            log("board message gone, reposting", err=err[:200])
            try:  # one board, never a stack of them
                from . import dm
                dm._delete_thread(ts)
            except Exception:
                pass
            db.kv_set("board_detail", {})
            ts = None
    if not ts:
        ts = slack.send_self(root, topic="board", guard=False)["ts"]  # the board legitimately reposts identical text
        # recorded at once, not when refresh returns: a detail reply that timed out lost the new board's ts, so each
        # refresh found the old one gone and posted a whole new board, which dm.reconcile swept as unowned (10-10 dark
        # wake: 5 boards from 05:12 to 09:59, 13 over the weekend, each with 11 replies). No hash: refresh finishes it
        db.kv_set("board", {**(db.kv_get("board") or {}), "ts": ts, "hash": None, "day": db.today()})
        db.kv_set("board_detail", {"root": ts, "parts": []})
        try:  # the board moves (it is always the last message): the pin is the stable way to open it
            slack.pin_self(ts)
        except Exception as e:
            log("board pin failed", err=str(e)[:100])
    det = db.kv_get("board_detail") or {}
    parts = list(det.get("parts") or ([det["ts"]] if det.get("ts") else [])) if det.get("root") == ts else []
    for i, detail in enumerate(details):
        if i < len(parts):
            try:
                slack.edit_self(parts[i], detail)
                continue
            except Exception as e:
                if not any(w in str(e) for w in ("message_not_found", "cant_update_message")):
                    log("board detail edit failed, keeping it", err=str(e)[:120])
                    continue
        dts = slack.send_self(detail, thread_ts=ts, topic=f"board-detail-{i}", guard=False)["ts"]
        if i < len(parts):
            parts[i] = dts
        else:
            parts.append(dts)
        db.kv_set("board_detail", {"root": ts, "parts": parts})  # an interrupted post edits these next time
    from . import dm
    for extra in parts[len(details):]:
        dm._delete(extra)
    parts = parts[:len(details)]
    db.kv_set("board_detail", {"root": ts, "parts": parts})
    try:  # the board thread holds exactly the current replies: leftovers of an interrupted post go
        for r in slack.replies(CFG["self_dm_channel"], ts):
            if r["ts"] != ts and r["ts"] not in parts and r.get("user") == CFG["self_user_id"] and not r.get("client_msg_id"):
                dm._delete(r["ts"])
    except Exception as e:
        log("board thread sweep failed", err=str(e)[:100])
    return ts


def ping(text: str, urgent: bool = False) -> None:
    """A NEW self-DM message (notifies, unlike board edits). Only for urgent/new needs-you items."""
    if not acting("urgent_pings"):
        _shadow("ping", text=text)
        return
    slack.send_self(text, topic="ping")


def composer_draft(channel: str, thread_ts: str | None, text: str, item_id: int) -> dict:
    """Leave teammate-facing text as a Slack composer draft (the user presses Enter).
    Only the claude.ai Slack MCP can create drafts, so a tiny Haiku call with exactly
    that one tool does it. The text passes slack.sh's style gate first."""
    clean = slack.check_text(text, channel)
    art = db.add_artifact(item_id, "slack_draft", f"{channel}:{thread_ts or ''}", clean)
    if not acting("slack_composer_drafts"):
        _shadow("slack_draft", channel=channel, thread_ts=thread_ts, text=clean)
        return {"artifact": art, "shadow": True}
    prompt = (f"Call slack_send_message_draft exactly once with channel_id={channel}"
              + (f", thread_ts={thread_ts}" if thread_ts else "")
              + " and the message text between <text> tags, verbatim (no edits). Then reply DONE.\n<text>\n"
              + clean + "\n</text>")
    r = run(["claude", "-p", prompt, "--model", CFG["claude"]["haiku"],
             "--allowedTools", "mcp__claude_ai_Slack__slack_send_message_draft", "--tools", "",
             "--disable-slash-commands", "--output-format", "json", "--max-budget-usd", "0.3"], timeout=180)
    ok = '"is_error":false' in r.stdout.replace(" ", "") and "DONE" in r.stdout
    if not ok:
        log("composer draft failed; board carries the text", out=r.stdout[-300:])
    return {"artifact": art, "draft_created": ok}


def close_duplicate_pr(repo: str, number: int, keeper_url: str, item_id: int) -> None:
    """Close a harness PR that duplicates an earlier harness PR, saying so."""
    if not acting("draft_prs"):
        return _shadow("pr_close_duplicate", repo=repo, number=number, keeper=keeper_url)
    from .guardcall import act
    act("gh_pr_op", repo=repo, pr=number, op="close",
        comment=site.t(f"Closing this PR: it duplicates the fix in {keeper_url} (same file, same cause).",
                       f"Cierro este PR: duplica el fix de {keeper_url} (mismo archivo, misma causa)."))
    log("duplicate PR closed", repo=repo, pr=number, keeper=keeper_url, item=item_id)
