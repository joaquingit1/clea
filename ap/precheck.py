"""Before a case pays for its next stage, ask GitHub (for free) whether there is still anything to do.

A case is opened on what was true then: a red check, an open thread, a pull request waiting for review. By the time
its expensive stage is claimed, that may be over: the pull request merged, someone reran CI and it passed, a teammate
resolved the threads. The stage used to find that out with a model session
. One `gh pr view` answers the same question.

Only facts decide here, never a judgement: a case is settled when GitHub says the thing it exists for is gone.
Anything unclear, or any error, lets the stage run as before."""
from __future__ import annotations

from . import db, gh, site
from .core import CFG, log

# the stages that start a model session on a pull request
PAID = ("review_find", "review_verify", "ci_fix", "fix", "gate", "self_find", "self_review",
        "comments_triage", "comments_verify", "comments_reply")
FAILED = ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR")


def _over(repo: str, n: int, pr: dict) -> str:
    """Why nothing is left to do on a pull request that is no longer open, in the words the app shows."""
    how = site.t("merged", "mergeado") if pr.get("state") == "MERGED" else site.t("closed", "cerrado")
    return site.t(f"{repo}#{n} {how}: no longer needed", f"{repo}#{n} {how}: ya no hace falta")


def _red(pr: dict) -> bool:
    return any((c.get("conclusion") or "").upper() in FAILED or (c.get("state") or "").upper() in ("FAILURE", "ERROR")
               for c in pr.get("statusCheckRollup") or [])


def _settle(it: dict, status: str, note: str, **extra) -> bool:
    db.update_item(it["id"], status=status, note=note, **extra)
    db.add_signal("precheck_settled", {"from": it["status"], "to": status, "why": note[:300]}, it["id"])
    log("precheck: settled before a paid stage", item=it["id"], stage=it["status"], to=status, why=note[:160])
    return True


def settled(it: dict) -> bool:
    """True when the case was closed or handed on here, so its stage must not run."""
    if it["status"] not in PAID:
        return False
    try:
        return _check(it)
    except Exception as e:  # a check that cannot be made is not a verdict
        log("precheck: could not check", item=it["id"], err=f"{type(e).__name__}: {e}"[:200])
        return False


def _check(it: dict) -> bool:
    d, kind, status = it["data"], it["kind"], it["status"]
    if kind == "REVIEW":
        if not (it.get("repo") and it.get("pr")):
            return False
        pr = gh.pr_view(it["repo"], it["pr"])
        return pr["state"] != "OPEN" and _settle(it, "dropped", _over(it["repo"], it["pr"], pr))
    parts = [p for p in d.get("parts") or [] if p.get("pr")]
    if not parts:
        return False
    if kind in ("TASK", "INCIDENT", "IDEA"):
        # its own pull requests were merged or closed by hand while it was still polishing them
        if len(parts) != len(d.get("parts") or []):
            return False  # a part with no pull request yet is still being built
        views = [gh.pr_view(p["repo"], p["pr"]) for p in parts]
        if all(v["state"] != "OPEN" for v in views):
            how = "pr_merged" if all(v["state"] == "MERGED" for v in views) else "pr_closed"
            db.add_signal(how, {"prs": [v.get("url") for v in views]}, it["id"])
            return _settle(it, "done", how)
        return False
    if kind not in ("CI", "COMMENTS"):
        return False
    part = (d.get("parts") or [{}])[d.get("cur", 0)]
    repo, n = part.get("repo"), part.get("pr")
    if not (repo and n):
        return False
    pr = gh.pr_view(repo, n)
    if pr["state"] != "OPEN":
        return _settle(it, "dropped", _over(repo, n, pr))
    if kind == "CI":
        # a second case on the same pull request is not decided here: worker.dedupe_items folds true duplicates within
        # minutes, and a comments case that is only waiting (for a deploy, for his ok) does not fix this one's CI
        if status == "ci_fix" and pr.get("statusCheckRollup") and not _red(pr) and pr.get("mergeable") != "CONFLICTING":
            # red when the case was opened, not now (a rerun, a push by hand): ci_wait reads it and closes or waits
            return _settle(it, "ci_wait", site.t(f"CI is no longer red on {pr['headRefOid'][:7]}: checking it again instead of fixing",
                                                 f"el CI ya no está rojo en {pr['headRefOid'][:7]}: lo vuelvo a mirar en vez de arreglar"),
                           next_run_at=0)
    if kind == "COMMENTS" and status == "comments_triage" and not d.get("pending_comments"):
        asked = d.get("comments") or []
        ids = {c.get("thread_id") for c in asked}
        if asked and None not in ids and not _red(pr) and pr.get("mergeable") != "CONFLICTING":
            still = {t["thread_id"] for t in gh.unresolved_threads(repo, n, CFG["self_github"])}
            if not ids & still:
                return _settle(it, "done", site.t("its threads were resolved or answered meanwhile: nothing left to do",
                                                  "sus hilos se resolvieron o respondieron mientras tanto: no queda nada"))
    return False
