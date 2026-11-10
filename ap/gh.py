"""GitHub via the gh CLI (already authenticated as the user)."""
from __future__ import annotations

import json
import time

from . import site
from .core import CFG, run

ORG = site.org()  # empty: this install has no GitHub configured
PR_FIELDS = ("number,title,body,author,baseRefName,headRefName,headRefOid,isDraft,state,mergeable,"
             "mergeStateStatus,reviewDecision,statusCheckRollup,files,additions,deletions,url,"
             "updatedAt,mergedAt,closedAt,labels,reviewRequests,createdAt")


def api(path: str, method: str = "GET", body: dict | None = None, paginate: bool = False):
    if method != "GET":  # writes go through the outbound guard (ap/guardcall.py)
        raise RuntimeError(f"gh.api is read-only; use the guard for {method} {path}")
    cmd = ["gh", "api", "-X", method, path]
    if paginate:
        cmd.append("--paginate")
    p = run(cmd + (["--input", "-"] if body is not None else []), timeout=120,
            input=json.dumps(body) if body is not None else None)
    if p.returncode != 0:
        raise RuntimeError(f"gh api {method} {path}: {(p.stderr or p.stdout)[-600:]}")
    out = p.stdout.strip()
    if not out:
        return None
    if paginate and out.startswith("[") and "][" in out:
        out = out.replace("][", ",")
    return json.loads(out)


def pr_view(repo: str, n: int) -> dict:
    p = run(["gh", "pr", "view", str(n), "-R", site.slug(repo), "--json", PR_FIELDS], timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"gh pr view {repo}#{n}: {p.stderr[-400:]}")
    return json.loads(p.stdout)


def pr_list(repo: str, search: str, limit: int = 50) -> list[dict]:
    p = run(["gh", "pr", "list", "-R", site.slug(repo), "--search", search, "--limit", str(limit),
             "--json", "number,title,author,headRefOid,isDraft,updatedAt,baseRefName,headRefName,url,createdAt"], timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"gh pr list {repo}: {p.stderr[-400:]}")
    return json.loads(p.stdout)


def bot_author(author: dict | None) -> bool:
    """A PR `author` object (pr_view / pr_list) that is a bot: dependabot is 'app/dependabot' with is_bot."""
    login = (author or {}).get("login", "")
    return bool((author or {}).get("is_bot")) or "bot" in login.lower() or login.startswith("app/")


SEARCH_FIELDS = "number,title,author,url,updatedAt,headRefName,body,files,mergedAt,isDraft"


def pr_search(repo: str, search: str, state: str = "open", limit: int = 30, fields: str = SEARCH_FIELDS) -> list[dict]:
    """Read-only PR search in any state (open, merged, closed, all), with files and bodies for overlap checks."""
    p = run(["gh", "pr", "list", "-R", site.slug(repo), "--state", state, "--search", search, "--limit", str(limit),
             "--json", fields], timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"gh pr list {repo} ({state}): {p.stderr[-400:]}")
    return json.loads(p.stdout or "[]")


def pr_diff(repo: str, n: int) -> str:
    p = run(["gh", "pr", "diff", str(n), "-R", site.slug(repo)], timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"gh pr diff {repo}#{n}: {p.stderr[-400:]}")
    return p.stdout


def reviews(repo: str, n: int) -> list[dict]:
    return api(f"repos/{site.slug(repo)}/pulls/{n}/reviews", paginate=True) or []


def review_comments(repo: str, n: int) -> list[dict]:
    return api(f"repos/{site.slug(repo)}/pulls/{n}/comments", paginate=True) or []


def issue_comments(repo: str, n: int) -> list[dict]:
    return api(f"repos/{site.slug(repo)}/issues/{n}/comments", paginate=True) or []


def review_state(repo: str, n: int, review_id: int) -> dict | None:
    try:
        return api(f"repos/{site.slug(repo)}/pulls/{n}/reviews/{review_id}")
    except RuntimeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return None  # pending review deleted by the user = rejected
        raise


def review_comments_of(repo: str, n: int, review_id: int) -> list[dict]:
    return api(f"repos/{site.slug(repo)}/pulls/{n}/reviews/{review_id}/comments", paginate=True) or []


def compare_contains(repo: str, sha: str, deployed_sha: str) -> bool:
    """True if deployed_sha includes sha (deployed is ahead of or identical to sha)."""
    if sha == deployed_sha:
        return True
    try:
        d = api(f"repos/{site.slug(repo)}/compare/{sha}...{deployed_sha}")
    except RuntimeError:
        return False
    return d.get("status") in ("ahead", "identical")


_deploy_cache: dict = {}


def deployed_shas(repo: str, envname: str) -> list[str]:
    """Head SHAs of recent successful deploy jobs for repo/env (cached 3 min)."""
    spec = (CFG["repos"][repo].get("deploy") or {}).get(envname)
    if not spec:
        return []
    ck = (repo, envname)
    hit = _deploy_cache.get(ck)
    if hit and time.time() - hit[0] < 180:
        return hit[1]
    # successful runs only, and enough of them to reach the deploys that are manual dispatches among many dev pushes
    runs = api(f"repos/{site.slug(repo)}/actions/workflows/{spec['workflow']}/runs?status=success&per_page=40") or {}
    shas = []
    for r in runs.get("workflow_runs", []):
        jobs = api(f"repos/{site.slug(repo)}/actions/runs/{r['id']}/jobs") or {}
        if any(_job_is(j["name"], spec["job"]) and j.get("conclusion") == "success" for j in jobs.get("jobs", [])):
            shas.append(r["head_sha"])
        if len(shas) >= 3:
            break
    _deploy_cache[ck] = (time.time(), shas)
    return shas


def _job_is(name: str, job: str) -> bool:
    """A job calling a reusable workflow is named '<job> / <inner job>' (schools' prod-deploy became 'prod-deploy /
    Terraform Apply'): matched only exactly, no deploy was ever found, and every frontend PR waiting on a backend PR
    sat in 'En fila para mergear' after its backend was live."""
    return name == job or name.startswith(job + " / ")


def is_deployed(repo: str, envname: str, merge_sha: str) -> bool:
    return any(compare_contains(repo, merge_sha, s) for s in deployed_shas(repo, envname))


def failed_runs(repo: str, head_sha: str) -> list[dict]:
    d = api(f"repos/{site.slug(repo)}/actions/runs?head_sha={head_sha}&per_page=30") or {}
    return [r for r in d.get("workflow_runs", []) if r.get("conclusion") in ("failure", "timed_out")]


def run_log_tail(repo: str, run_id: int, lines: int = 250) -> str:
    p = run(["gh", "run", "view", str(run_id), "-R", site.slug(repo), "--log-failed"], timeout=240)
    return "\n".join(p.stdout.splitlines()[-lines:])


THREADS_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
  reviewThreads(first:100){nodes{id isResolved isOutdated comments(first:30){nodes{databaseId author{login} body path line url}}}}}}}"""


def review_threads(repo: str, n: int) -> list[dict]:
    p = run(["gh", "api", "graphql", "-f", f"query={THREADS_Q}", "-F", f"o={site.repo_org(repo)}",
             "-F", f"r={site.repo_name(repo)}", "-F", f"n={n}"], timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"review threads {repo}#{n}: {p.stderr[-300:]}")
    pr = ((json.loads(p.stdout).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    out = []
    for t in pr.get("reviewThreads", {}).get("nodes", []):
        cs = t["comments"]["nodes"]
        if not cs:
            continue
        out.append({"thread_id": t["id"], "resolved": t["isResolved"], "outdated": t["isOutdated"],
                    "first_author": (cs[0]["author"] or {}).get("login", ""),
                    "last_author": (cs[-1]["author"] or {}).get("login", ""),
                    "comment_id": cs[0]["databaseId"], "last_comment_id": cs[-1]["databaseId"],
                    "path": cs[0].get("path"), "line": cs[0].get("line"), "url": cs[0].get("url"),
                    "body": "\n---\n".join(f"{(c['author'] or {}).get('login')}: {c['body'][:2000]}" for c in cs)[-4000:]})
    return out


def unresolved_threads(repo: str, n: int, me: str) -> list[dict]:
    """Threads still needing action: unresolved and the last word is not the user's."""
    return [t for t in review_threads(repo, n) if not t["resolved"] and t["last_author"] != me]


def required_checks(repo: str) -> list[str]:
    """Status-check contexts the default branch's rules require, cached an hour. 10-23: cometa#3325 folded four PR
    workflows into one required 'CI'; open PRs whose heads predate it never ran it and sat BLOCKED forever."""
    from . import db
    key = f"required_checks:{repo}"
    hit = db.kv_get(key) or {}
    if hit.get("at", 0) > time.time() - 3600:
        return hit["contexts"]
    try:
        rules = api(f"repos/{site.slug(repo)}/rules/branches/{CFG['repos'][repo]['default']}") or []
        ctx = [c["context"] for r in rules if r.get("type") == "required_status_checks"
               for c in (r.get("parameters") or {}).get("required_status_checks", [])]
    except Exception:
        return hit.get("contexts", [])
    db.kv_set(key, {"at": time.time(), "contexts": ctx})
    return ctx


def missing_required(repo: str, pr: dict) -> list[str]:
    """Required contexts with no check run or status at all on the PR's head (not failing: absent)."""
    seen = {c.get("name") or c.get("context") for c in pr.get("statusCheckRollup") or []}
    return [c for c in required_checks(repo) if c not in seen]

