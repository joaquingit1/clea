"""Merge ordering: nothing auto-merges out of order.

Edges "A needs B at level L" (L in merged|dev|prod) come from:
  explicit  - PR body says depends/requires/after <repo>#n or links a PR in such a line
  stack     - A's base branch is B's head branch
  plan      - the harness itself split a task into ordered PRs
  inferred  - Muse reads the open/in-flight PR set and finds contract dependencies
              (frontend uses a field/endpoint a backend PR adds, code uses a migration...)
  layer     - fallback: a related backend PR always precedes a frontend PR
A dependency is satisfied only when B is merged AND (for dev/prod) the deploy job that
ships B's merge commit succeeded (gh.is_deployed). Frontend merges go live in prod, so a
frontend PR needs its backend deps at `prod`.
Migration PRs are never auto-merged (predetermined owners), and "hold" markers win.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import db, gh, muse, site
from .core import CFG, log, now

_OWNER, _REPOS = site.owners_re(), site.repo_names_re()
REF_RE = rf"(?:(?:(?:{_OWNER})/)?({_REPOS}))?#(\d+)|github\.com/(?:{_OWNER})/({_REPOS})/pull/(\d+)"
REF = re.compile(REF_RE)
# "<this> depends on <ref>": the keyword sits right before the reference
FORWARD = re.compile(r"(depends on|depende de|requires|requiere|needs|necesita|blocked by|bloqueado por|"
                     r"(?:merge |mergear )?(?:after|despu[eé]s de)|once|cuando (?:est[eé]|se mergee))\s*[:\-]?\s*(?:el |la |PR |de )*(?:<?https?://)?"
                     r"(?=" + REF_RE + r")", re.I)
# "<ref> goes after this": "este PR primero... #n después", "#n after this", "then #n"
REVERSE = re.compile(r"(?:(este|this)\s+(?:PR\s+)?(primero|first|va primero|goes first))|"
                     r"(?:" + REF_RE + r")\s*(?:va\s+)?(despu[eé]s|after(?:wards)?|luego|then|second|segundo)", re.I)
HOLD = re.compile(r"\b(ap:hold|do not merge|no mergear|\[hold\]|wip)\b", re.I)
BOTS = ("coderabbitai", "github-actions", "vercel", "claude", "cursor", "copilot", "dependabot")


def layers(repo: str, files: list[str]) -> set[str]:
    rc = CFG["repos"][repo]
    if "layers" not in rc:
        return {rc["layer"]}
    out = set()
    for f in files:
        for prefix, layer in rc["layers"].items():
            if f.startswith(prefix):
                out.add(layer)
    return out or {"other"}


def has_migration(repo: str, files: list[str]) -> bool:
    pats = CFG["repos"][repo].get("migrations", [])
    return any(p in ("/" + f) for f in files for p in pats)


def explicit_deps(repo: str, body: str) -> tuple[list, list]:
    """(this_depends_on, depends_on_this) from unambiguous phrasing only. Anything vaguer
    ("reemplaza a #n", "ver #n") is left to the Muse inference, which reads direction."""
    fwd, rev = [], []
    for line in (body or "").splitlines():
        for m in FORWARD.finditer(line):
            ref = REF.match(line, m.end())
            if ref:
                fwd.append((ref.group(1) or ref.group(3) or repo, int(ref.group(2) or ref.group(4)), line.strip()[:200]))
        if REVERSE.search(line):
            for ref in REF.finditer(line):
                r, n = ref.group(1) or ref.group(3) or repo, int(ref.group(2) or ref.group(4))
                if not any((r, n) == (a, b) for a, b, _ in fwd):
                    rev.append((r, n, line.strip()[:200]))
    return fwd, rev


def _files(pr: dict) -> list[str]:
    return [f["path"] for f in pr.get("files") or []]


def record(repo, n, dep_repo, dep_n, need, reason, source) -> None:
    if (repo, n) == (dep_repo, dep_n):
        return
    db.x("INSERT INTO merge_deps VALUES(?,?,?,?,?,?,?) ON CONFLICT(repo,number,dep_repo,dep_number) "
         "DO UPDATE SET need=CASE WHEN excluded.need='prod' OR merge_deps.need='prod' THEN 'prod' "
         "WHEN excluded.need='dev' OR merge_deps.need='dev' THEN 'dev' ELSE 'merged' END, "
         "reason=excluded.reason, source=excluded.source", repo, n, dep_repo, dep_n, need, reason[:300], source)


def need_for(pr_repo: str, pr_files: list[str], dep_repo: str, dep_files: list[str]) -> str:
    """Frontend consumers need the backend live in prod (frontend merge = prod deploy)."""
    if "frontend" in layers(pr_repo, pr_files) and "backend" in layers(dep_repo, dep_files):
        return CFG["merge"]["frontend_needs_backend_env"]
    return "merged"


def refresh(open_prs: dict[tuple[str, int], dict], unread=()) -> None:
    """Rebuild explicit/stack/inferred edges for the given open PRs (own + in-flight). `unread`: PRs whose view
    failed this round; the inference input keeps their last view."""
    by_head = {(r, p["headRefName"]): (r, n) for (r, n), p in open_prs.items()}
    for (r, n), p in open_prs.items():
        fwd, rev = explicit_deps(r, p.get("body", ""))
        for dr, dn, why in fwd:
            dep = open_prs.get((dr, dn))
            record(r, n, dr, dn, need_for(r, _files(p), dr, _files(dep)) if dep else "merged", why, "explicit")
        for dr, dn, why in rev:
            other = open_prs.get((dr, dn))
            record(dr, dn, r, n, need_for(dr, _files(other), r, _files(p)) if other else "merged", why, "explicit")
        base = by_head.get((r, p.get("baseRefName")))
        if base:
            record(r, n, base[0], base[1], "merged", f"stacked on {p['baseRefName']}", "stack")
    live = {f"{r}#{n}" for (r, n) in open_prs}
    for d in db.q("SELECT repo, number, dep_repo, dep_number, need FROM merge_deps"):
        if f"{d['repo']}#{d['number']}" in live and f"{d['dep_repo']}#{d['dep_number']}" in live:
            continue
        # absence from open_prs is not proof (a network outage drops views): only GitHub's word prunes
        #. A merged dependency
        # is pruned only once it is live where it is needed (2026-10-02: schools#913 merged at night, its prod edge to
        # cometa#1931 was pruned with it, and the gate and the board lost the order while #913 was only on dev)
        try:
            gone = (gh.pr_view(d["repo"], d["number"]).get("state") in ("CLOSED", "MERGED")
                    or gh.pr_view(d["dep_repo"], d["dep_number"]).get("state") == "CLOSED"
                    or dep_status(d["dep_repo"], d["dep_number"], d["need"])[0])
        except Exception:
            gone = False
        if gone:
            db.x("DELETE FROM merge_deps WHERE repo=? AND number=? AND dep_repo=? AND dep_number=?",
                 d["repo"], d["number"], d["dep_repo"], d["dep_number"])
    _prune_guesses()
    _layer_fallback(open_prs)
    # the model pass runs in its own job (worker: merge_infer): babysit must never wait on it. A failed view is not a
    # change in the PR set either (2026-10-11: one or two 401s per babysit round flipped the set, and every flip
    # changed merge_infer_sig and paid a new inference, 10 deps calls in 2.5 h over the same open PRs)
    prev = db.kv_get("merge_infer_input") or {}
    inp = {f"{r}#{n}": p for (r, n), p in open_prs.items()}
    inp.update({k: prev[k] for k in (f"{r}#{n}" for r, n in unread) if k in prev and k not in inp})
    db.kv_set("merge_infer_input", inp)


def _prune_guesses() -> None:
    """Guessed edges the harness drops on its own, so a bad guess never needs a person to clean it up:
    - with the model pass off, an inferred same-repo 'merged' edge: that is two PRs touching the same files, which
      orders nothing;
    - always, the inferred side of a two-way cycle: both directions cannot be true, and explicit, stack and plan
      edges are what a person wrote (cometa#2900/#2648, #2499/#3057 and #3057/#1656 sat as 'needs a human to decide').
    A cross-repo inferred edge (a frontend consuming a backend field) is a contract and stays."""
    rows = db.q("SELECT * FROM merge_deps")
    pair = {(d["repo"], d["number"], d["dep_repo"], d["dep_number"]): d for d in rows}
    drop = []
    for d in rows:
        if d["source"] != "inferred":
            continue
        overlap = d["repo"] == d["dep_repo"] and d["need"] == "merged" and not CFG["merge"].get("infer_with_model", True)
        back = pair.get((d["dep_repo"], d["dep_number"], d["repo"], d["number"]))
        if overlap or back:
            drop.append(d)
    for d in drop:
        db.x("DELETE FROM merge_deps WHERE repo=? AND number=? AND dep_repo=? AND dep_number=?",
             d["repo"], d["number"], d["dep_repo"], d["dep_number"])
    if drop:
        log("merge_order: dropped guessed edges", edges=[f"{d['repo']}#{d['number']}->{d['dep_repo']}#{d['dep_number']}"
                                                         for d in drop])


def infer_pending() -> None:
    """Background job: model-inferred ordering edges for the last PR set babysit saw."""
    # off since 2026-10-22 (merge.infer_with_model): $58 in 14 days for 9 edges, 8 of them same-repo file overlap that
    # formed three two-way cycles holding ready PRs (refresh drops those itself, _prune_guesses). Explicit lines,
    # stacks, plans and backend-before-frontend stay
    if not CFG["merge"].get("infer_with_model", True):
        return
    raw = db.kv_get("merge_infer_input") or {}
    open_prs = {(k.split("#")[0], int(k.split("#")[1])): v for k, v in raw.items()}
    _infer(open_prs)


STOP = {"feat", "fix", "chore", "refactor", "update", "backend", "frontend"}


def _tokens(p: dict) -> set[str]:
    words = re.split(r"[^a-z0-9]+", f"{p.get('headRefName', '')} {p.get('title', '')}".lower())
    stop = STOP | site.stop_words("merge_order")
    return {w for w in words if len(w) >= 5 and w not in stop}


def _layer_fallback(open_prs: dict) -> None:
    """Same author, related names, backend + frontend => backend first, live in prod."""
    for (r, n), p in open_prs.items():
        if "frontend" not in layers(r, _files(p)):
            continue
        for (dr, dn), d in open_prs.items():
            if (dr, dn) == (r, n) or "backend" not in layers(dr, _files(d)):
                continue
            same_author = (p.get("author") or {}).get("login") == (d.get("author") or {}).get("login")
            shared = _tokens(p) & _tokens(d)
            if same_author and len(shared) >= 2:
                record(r, n, dr, dn, CFG["merge"]["frontend_needs_backend_env"],
                       f"related backend change (shared: {', '.join(sorted(shared)[:4])})", "layer")


def _infer(open_prs: dict) -> None:
    """Muse pass over PR sets that mix layers or repos. Only ADDS edges (conservative)."""
    if len(open_prs) < 2:
        return
    summary = []
    for (r, n), p in sorted(open_prs.items()):
        summary.append({"pr": f"{r}#{n}", "title": p.get("title"), "author": (p.get("author") or {}).get("login"),
                        "head": p.get("headRefName"), "base": p.get("baseRefName"),
                        "layers": sorted(layers(r, _files(p))), "migration": has_migration(r, _files(p)),
                        "files": _files(p)[:60], "body": (p.get("body") or "")[:1500]})
    blob = json.dumps(summary, ensure_ascii=False)
    sig = hashlib.sha256(blob.encode()).hexdigest()
    if db.kv_get("merge_infer_sig") == sig:
        return
    # a push or a body edit on one PR reran the whole set (2026-10-14: 22 open PRs, a deps call about every 20 min at
    # ~$0.6 while fix rounds pushed, 36% of the window's spend): only a new shape of the set (a PR in or out, its base,
    # title, layers or migration) reruns at once; changed content waits until the last inference is 3 h old.
    # Explicit 'depends on' lines in a body are parsed on every refresh, with no model call
    shape = hashlib.sha256(json.dumps([{k: s[k] for k in ("pr", "title", "base", "layers", "migration")} for s in summary],
                                      ensure_ascii=False).encode()).hexdigest()
    if db.kv_get("merge_infer_shape") == shape and now() - db.kv_get("merge_infer_at", 0) < 3 * 3600:
        return
    schema = {"type": "object", "required": ["edges"], "properties": {"edges": {"type": "array", "items": {
        "type": "object", "required": ["pr", "depends_on", "need", "reason", "confidence"],
        "properties": {"pr": {"type": "string"}, "depends_on": {"type": "string"},
                       "need": {"type": "string", "enum": ["merged", "dev", "prod"]},
                       "reason": {"type": "string"}, "confidence": {"type": "number"}}}}}}
    mirrors = (CFG.get("paths") or {}).get("mirror") or str(__import__("ap.worktree", fromlist=["x"]).MIRROR)
    prompt = (f"Open and in-flight pull requests across {site.org()} repos ({site.repos_note()}). "
              "Find every ordering constraint: a frontend change consuming an "
              "endpoint/field/enum another PR adds or changes; code depending on a migration; a PR that builds on "
              f"another's code; cleanup that must follow a rollout. Use the read-only mirrors under {mirrors} to read code when "
              "the file lists are not enough (read-only). Report only real dependencies with evidence; do not "
              "invent. need=prod when the dependent code will run in prod against the dependency (frontend on "
              "backend), dev when only dev matters, merged for same-repo code ordering.\n\nPRs:\n" + blob)
    try:
        out = muse.call("ap-deps", prompt, cwd=str(__import__("ap.worktree", fromlist=["x"]).MIRROR), schema=schema, lane="deps")
    except Exception as e:
        log("merge_order: inference failed", err=str(e)[:300])
        return
    log("merge_order: inferred", edges=len(out["edges"]),
        kept=sum(e["confidence"] >= 0.5 for e in out["edges"]))
    for e in out["edges"]:
        if e["confidence"] < 0.5:
            continue
        a = site.short_ref(e["pr"]).split("#")
        b = site.short_ref(e["depends_on"]).split("#")
        try:
            record(a[0], int(a[1]), b[0], int(b[1]), e["need"], e["reason"], "inferred")
        except (IndexError, ValueError):
            continue
    db.kv_set("merge_infer_sig", sig)
    db.kv_set("merge_infer_shape", shape)
    db.kv_set("merge_infer_at", now())


def dep_status(dep_repo: str, dep_n: int, need: str) -> tuple[bool, str]:
    try:
        d = gh.pr_view(dep_repo, dep_n)
    except Exception as e:
        return False, f"{dep_repo}#{dep_n}: unreadable ({str(e)[:80]})"
    if d["state"] == "CLOSED":
        return False, f"{dep_repo}#{dep_n} was closed without merge"
    if d["state"] != "MERGED":
        return False, f"{dep_repo}#{dep_n} not merged yet"
    if need == "merged":
        return True, f"{dep_repo}#{dep_n} merged"
    rc = CFG["repos"][dep_repo]
    if rc.get("frontend_live_on_merge") and layers(dep_repo, _files(d)) == {"frontend"}:
        return True, f"{dep_repo}#{dep_n} merged (frontend live on merge)"
    msha = (gh.api(f"repos/{site.slug(dep_repo)}/pulls/{dep_n}") or {}).get("merge_commit_sha")
    if msha and gh.is_deployed(dep_repo, need, msha):
        return True, f"{dep_repo}#{dep_n} live in {need}"
    return False, f"{dep_repo}#{dep_n} merged but not live in {need} yet"


def human_approved(repo: str, n: int, head_sha: str) -> bool:
    latest = {}
    for r in gh.reviews(repo, n):
        login = (r.get("user") or {}).get("login", "")
        if any(b in login.lower() for b in BOTS) or login == CFG["self_github"]:
            continue
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = r["state"]
    return "APPROVED" in latest.values() and "CHANGES_REQUESTED" not in latest.values()


def gate(repo: str, pr: dict) -> tuple[bool, list[str]]:
    """May the harness merge this PR right now? Returns (ok, blocking reasons)."""
    n, files = pr["number"], _files(pr)
    why = []
    if (pr.get("author") or {}).get("login") != CFG["self_github"] and CFG["merge"]["only_own_prs"]:
        why.append(f"not {site.first_name()}'s PR")
    if pr.get("isDraft"):
        why.append("draft")
    if pr.get("baseRefName") != CFG["repos"][repo]["default"]:
        why.append(f"base is {pr.get('baseRefName')} (stacked: lands via its root)")
    # hold = title or label, or an explicit ap:hold token in the body (a body QUOTING another PR's
    # "no mergear" is not a hold; cometa#1931 matched that way on 2026-09-23)
    if HOLD.search((pr.get("title") or "") + " " + " ".join(l["name"] for l in pr.get("labels") or [])) \
            or re.search(r"(^|\s)ap:hold\b", pr.get("body") or "", re.I):
        why.append("hold marker")
    if has_migration(repo, files) and CFG["merge"]["never_auto_merge_migrations"]:
        why.append("contains a migration (owner merges and deploys it)")
    checks = pr.get("statusCheckRollup") or []
    bad = [c.get("name") or c.get("context") for c in checks
           if (c.get("conclusion") or c.get("state")) not in ("SUCCESS", "NEUTRAL", "SKIPPED", "success", None)
           or c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING")]
    if bad:
        why.append(f"checks not green: {', '.join(map(str, bad[:4]))}")
    missing = gh.missing_required(repo, pr) if pr.get("mergeStateStatus") == "BLOCKED" else []
    if missing:
        why.append(f"required check {', '.join(missing)} never ran on this head")
    elif pr.get("mergeStateStatus") not in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        why.append(f"merge state {pr.get('mergeStateStatus')}")
    try:
        open_threads = gh.unresolved_threads(repo, n, CFG["self_github"])
    except Exception:
        open_threads = [{"unknown": True}]
    if open_threads:
        why.append(f"{len(open_threads)} unresolved review thread(s): each needs a fix or a defense first")
    if CFG["merge"]["require_human_approval"] and not human_approved(repo, n, pr["headRefOid"]):
        why.append("no current human approval")
    for d in db.q("SELECT * FROM merge_deps WHERE repo=? AND number=?", repo, n):
        if db.q1("SELECT 1 FROM merge_deps WHERE repo=? AND number=? AND dep_repo=? AND dep_number=?",
                 d["dep_repo"], d["dep_number"], repo, n):
            why.append(f"order: dependency CYCLE with {d['dep_repo']}#{d['dep_number']}; needs a human to decide")
            continue
        ok, msg = dep_status(d["dep_repo"], d["dep_number"], d["need"])
        if not ok:
            why.append(f"order: needs {msg} ({d['source']}: {d['reason'][:80]})")
    return (not why), why


def plan_text() -> list[str]:
    """Human-readable merge order for the board: one line per blocked edge."""
    lines = []
    for d in db.q("SELECT * FROM merge_deps ORDER BY repo, number"):
        st = db.q1("SELECT data FROM prs WHERE repo=? AND number=?", d["repo"], d["number"])
        if not st:
            continue
        lines.append(f"{d['dep_repo']}#{d['dep_number']} ({d['need']}) -> {d['repo']}#{d['number']}: {d['reason'][:70]}")
    return lines
