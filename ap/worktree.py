"""Isolated git worktrees under ~/cometa/.autopilot-wt. The harness never touches
the user's own checkouts (v1 ran ticks inside ~/cometa/cometa, on whatever branch he had)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import db, site
from .core import CFG, DATA, WORKTREES, log, now, run


def _git(repo_path: str | Path, *args: str, timeout: int = 300, check: bool = True):
    return run(["git", "-C", str(repo_path), *args], timeout=timeout, check=check)


def repo_path(repo: str) -> Path:
    return Path(CFG["repos"][repo]["path"])


def default_branch(repo: str) -> str:
    return CFG["repos"][repo]["default"]


def valid_branch(name: str) -> bool:
    # dash-form <= 39 chars (the e2e preview gate breaks on longer or slashed names)
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,38}", name))


def fetch(repo: str) -> None:
    _git(repo_path(repo), "fetch", "--quiet", "origin", default_branch(repo), timeout=300)


def for_branch(repo: str, branch: str, item_id: int) -> Path:
    """Fresh work branch off origin/<default> in its own worktree (reused if it exists)."""
    if not valid_branch(branch):
        raise ValueError(f"invalid branch name {branch!r}")
    path = WORKTREES / f"{repo}--{branch}"
    if path.exists():
        # the reusing case owns it now: gc reads this row to keep a tree whose case has not stored its path yet
        db.x("UPDATE branches SET item_id=? WHERE repo=? AND branch=?", item_id, repo, branch)
        return path
    fetch(repo)
    _git(repo_path(repo), "worktree", "add", "-b", branch, str(path), f"origin/{default_branch(repo)}")
    seed_tsbuildinfo(path, repo)
    db.x("INSERT OR REPLACE INTO branches VALUES(?,?,?,?)", repo, branch, item_id, now())
    log("worktree created", repo=repo, branch=branch, item=item_id)
    return path


def seed_tsbuildinfo(path: Path, repo: str) -> list[str]:
    """Give a worktree the base worktree's incremental tsc state for every frontend project that has none, so its first
    tsc rechecks the branch's changes instead of the whole program. A cold dashboard tsc takes 103 s alone and 20 min
    or more beside other work; the gate kills it at 1500 s before it writes its.tsbuildinfo, so every retry started
    cold again. Paths in it are relative and tsc checks every file's hash against
    it, so a copy from another checkout is only a head start, never a wrong answer. Gitignored (frontend/**/*.tsbuildinfo)."""
    base = WORKTREES / f"{repo}--base" / "frontend"
    if path == base.parent or not base.exists():
        return []
    seeded = []
    for top in ("apps", "packages"):
        for src in base.glob(f"{top}/*/tsconfig.tsbuildinfo"):
            dst = path / "frontend" / src.relative_to(base)
            if dst.parent.exists() and not dst.exists():
                shutil.copyfile(src, dst)
                seeded.append(str(src.relative_to(base).parent))
    return seeded


def for_existing_branch(repo: str, branch: str, item_id: int | None = None) -> Path:
    """DETACHED worktree at origin/<branch> for fixing the user's own PRs. Detached because
    he may have the branch checked out himself; pushes go to HEAD:refs/heads/<branch>, which
    only fast-forwards his copy (see joaquin_busy_on for when we refuse to touch it).
    A new case starts at the PR head: the previous case's AP_LEDGER.json and unpushed commits are not its work
."""
    path = WORKTREES / f"{repo}--{branch.replace('/', '-')[:60]}"
    _git(repo_path(repo), "fetch", "--quiet", "origin", branch, timeout=300)
    if not path.exists():
        _git(repo_path(repo), "worktree", "add", "--detach", str(path), f"origin/{branch}")
        seed_tsbuildinfo(path, repo)
    else:
        (path / "AP_LEDGER.json").unlink(missing_ok=True)
        dirty = _git(path, "status", "--porcelain").stdout.strip()
        if dirty:
            log("worktree: uncommitted changes kept, not at the PR head", path=str(path), dirty=dirty[:200])
        else:
            _git(path, "checkout", "--quiet", "--detach", f"origin/{branch}")
    if item_id:
        db.x("INSERT OR IGNORE INTO branches VALUES(?,?,?,?)", repo, branch, item_id, now())
    return path


def for_pr_review(repo: str, n: int, head_sha: str) -> Path:
    """Detached read-only checkout of a PR head for reviewing."""
    path = WORKTREES / f"{repo}--review-{n}"
    _git(repo_path(repo), "fetch", "--quiet", "origin", f"pull/{n}/head", timeout=300)
    if not path.exists():
        _git(repo_path(repo), "worktree", "add", "--detach", str(path), head_sha)
    else:
        _git(path, "checkout", "--quiet", "--detach", head_sha)
    return path


def base_worktree(repo: str) -> Path:
    """A worktree pinned to origin/<default>, used for gate baselines."""
    path = WORKTREES / f"{repo}--base"
    fetch(repo)
    if not path.exists():
        _git(repo_path(repo), "worktree", "add", "--detach", str(path), f"origin/{default_branch(repo)}")
    else:
        _git(path, "checkout", "--quiet", "--detach", f"origin/{default_branch(repo)}")
    return path


def joaquin_busy_on(repo: str, branch: str) -> str | None:
    """Reason not to push to <branch>: one of HIS checkouts of it (main clone or his own
    worktrees) has uncommitted changes or commits not on origin yet."""
    out = _git(repo_path(repo), "worktree", "list", "--porcelain").stdout
    for block in out.split("\n\n"):
        lines = dict(l.split(" ", 1) for l in block.splitlines() if " " in l)
        wt, ref = lines.get("worktree", ""), lines.get("branch", "")
        if ref != f"refs/heads/{branch}" or wt.startswith(str(WORKTREES)):
            continue
        if _git(Path(wt), "status", "--porcelain", check=False).stdout.strip():
            return site.t(f"you have uncommitted changes in {wt}", f"tenes cambios sin commitear en {wt}")
        ahead = _git(Path(wt), "rev-list", "--count", f"origin/{branch}..HEAD", check=False).stdout.strip()
        if ahead not in ("", "0"):
            return site.t(f"you have {ahead} unpushed commits in {wt}", f"tenes {ahead} commits sin pushear en {wt}")
    return None


def harness_owns(repo: str, branch: str) -> bool:
    return db.q1("SELECT 1 FROM branches WHERE repo=? AND branch=?", repo, branch) is not None


def remove(path: Path) -> None:
    # rm first, then prune: `git worktree remove` lstat'ed every file of a 2.7 GB node_modules, ~1 min a tree
    run(["rm", "-rf", str(path)], timeout=1800)
    for repo in CFG["repos"]:
        if path.name.startswith(repo + "--"):
            _git(repo_path(repo), "worktree", "prune", check=False)


def restore(repo: str, branch: str, path: str) -> None:
    """A published part's work tree, gone from under its live case (the gc took them until 10-20), comes back detached
    at the pushed head, as for_existing_branch makes one: pushes go to HEAD:refs/heads/<branch>."""
    _git(repo_path(repo), "worktree", "prune", check=False)
    _git(repo_path(repo), "fetch", "--quiet", "origin", branch, timeout=300)
    _git(repo_path(repo), "worktree", "add", "--detach", path, f"origin/{branch}")
    seed_tsbuildinfo(Path(path), repo)
    log("worktree restored", repo=repo, branch=branch, path=path)


PARKED = ("awaiting_approval", "awaiting_human", "blocked", "waiting", "propose_check", "announce_check")


def _branch_of(path: Path) -> str | None:
    b = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    return b if b and b != "HEAD" else None


def _drop_branch(repo: str, branch: str) -> bool:
    """Delete a local branch the harness made. Never one of his: only rows of the branches table, and git itself refuses
    a branch checked out in any worktree."""
    if not harness_owns(repo, branch):
        return False
    # commits no remote has stay (he may have committed on it himself); a repro backfill branch is never pushed by design
    # and its proof lives in the kv record
    unpushed = _git(repo_path(repo), "rev-list", "--count", branch, "--not", "--remotes", check=False).stdout.strip()
    if unpushed not in ("", "0") and not branch.startswith("repro-pr-"):
        return False
    return _git(repo_path(repo), "branch", "-D", branch, check=False).returncode == 0


def gc() -> dict:
    """Free the disk. A cometa work tree is ~3 GB, 2.7 of it frontend/node_modules (pnpm copies, not links), and trees
    stayed 10 days after their case ended: on 10-23 ~/cometa/.autopilot-wt held 86 trees and 106 GB, 76 of them for
    finished or no cases, with under 30 GB free (the user: "are we actually properly deleting branches from my pc after
    we finish working with them?" They were never deleted.)

    - a tree no open case points at goes once it has been quiet 6 h (1 h when the disk is low), with its local branch;
      a tree whose case has not stored its path yet is held by that case's branches row
    - a tree a live case points at stays however old it is: a case parked on his ✅ for weeks still fixes in it
.
      Parked and idle a day (or the disk is low), it loses only node_modules, which every gate and repro reinstalls
      from the pnpm store as for a fresh tree
    - local branches the harness made whose case ended and whose tree is gone are deleted"""
    term = db.TERMINAL
    refs: dict[str, list] = {}
    for r in db.q("SELECT i.id, i.status, i.updated_at, i.claimed_by, json_extract(p.value,'$.wt') AS wt "
                  "FROM items i, json_each(i.data,'$.parts') p "
                  "UNION SELECT id, status, updated_at, claimed_by, json_extract(data,'$.wt') FROM items"):
        if r["wt"]:
            refs.setdefault(r["wt"], []).append(r)
    owner = {(r["repo"], r["branch"]): r["item_id"] for r in db.q("SELECT repo, branch, item_id FROM branches")}
    status = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM items")}
    free0 = shutil.disk_usage(WORKTREES).free
    low = free0 < CFG.get("disk_low_gb", 40) * 2**30
    removed, stripped, branches = [], [], []
    for p in sorted(WORKTREES.iterdir()):
        if not p.is_dir() or p.name.endswith("--base"):
            continue
        rs = refs.get(str(p), [])
        live = [r for r in rs if r["status"] not in term]
        repo = next((r for r in CFG["repos"] if p.name.startswith(r + "--")), None)
        branch = _branch_of(p) if repo else None
        if not live and repo and branch and status.get(owner.get((repo, branch))) not in (None, *term):
            continue  # its case is building in it and stores the path at the end
        if live:
            idle = now() - max(r["updated_at"] or 0 for r in live)
            nm = p / "frontend" / "node_modules"
            if (nm.exists() and all(r["status"] in PARKED and not r["claimed_by"] for r in live)
                    and (low or idle > 86400)):
                shutil.rmtree(nm, ignore_errors=True)
                stripped.append(p.name)
            continue
        quiet = now() - max([p.stat().st_mtime] + [r["updated_at"] or 0 for r in rs])
        if quiet < (3600 if low else 6 * 3600):
            continue
        try:
            remove(p)
        except Exception as e:  # one tree must not end the run
            log("gc: remove failed", tree=p.name, err=str(e)[:200])
            continue
        removed.append(p.name)
        if repo and branch and _drop_branch(repo, branch):
            branches.append(f"{repo}:{branch}")
    for repo in CFG["repos"]:
        _git(repo_path(repo), "worktree", "prune", check=False)
        listed = _git(repo_path(repo), "worktree", "list", "--porcelain", check=False).stdout
        out = set(l.split(" ", 1)[1].removeprefix("refs/heads/") for l in listed.splitlines() if l.startswith("branch "))
        local = set(_git(repo_path(repo), "branch", "--format=%(refname:short)", check=False).stdout.split())
        for (rp, br), iid in owner.items():
            if rp == repo and br in local and br not in out and status.get(iid) in (None, *term) and _drop_branch(repo, br):
                branches.append(f"{repo}:{br}")
    res = {"removed": removed, "stripped": stripped, "branches": branches,
           "free_gb": round(shutil.disk_usage(WORKTREES).free / 2**30, 1), "freed_gb": round((shutil.disk_usage(WORKTREES).free - free0) / 2**30, 1)}
    if removed or stripped or branches or low:
        log("gc: disk freed", low=low, **{k: (len(v) if isinstance(v, list) else v) for k, v in res.items()},
            trees=removed[:40], nm=stripped[:20])
    return res


MIRROR = Path((CFG.get("paths") or {}).get("mirror") or DATA / "mirror").expanduser()


def mirror_path(repo: str) -> Path:
    return MIRROR / repo


def ensure_mirrors() -> None:
    """Read-only worktrees of every repo pinned to origin/<default>: what the models read as
    'the product'. the user's checkouts in ~/cometa are feature branches and must never be cited as current behavior."""
    MIRROR.mkdir(parents=True, exist_ok=True)
    for repo, spec in CFG["repos"].items():
        m, d = mirror_path(repo), spec["default"]
        if not (m / ".git").exists():
            run(["git", "-C", str(repo_path(repo)), "fetch", "-q", "origin", d], timeout=300)
            r = run(["git", "-C", str(repo_path(repo)), "worktree", "add", "--detach", str(m), f"origin/{d}"], timeout=300)
            if r.returncode != 0:
                log("mirror: worktree add failed", repo=repo, err=r.stderr[-200:])
    refresh_mirrors()


def refresh_mirrors() -> dict:
    out = {}
    for repo, spec in CFG["repos"].items():
        m, d = mirror_path(repo), spec["default"]
        if not (m / ".git").exists():
            continue
        run(["git", "-C", str(m), "fetch", "-q", "origin", d], timeout=300)
        run(["git", "-C", str(m), "checkout", "-q", "--detach", f"origin/{d}"], timeout=120)
        run(["git", "-C", str(m), "clean", "-fdq"], timeout=60)
        out[repo] = run(["git", "-C", str(m), "rev-parse", "--short", "HEAD"]).stdout.strip()
    return out
