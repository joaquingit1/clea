"""Code scan: the code itself is the intake. Patrol read one area a run with
Sonnet, kept the best finding and verified one a day: 48 runs in October, $62, 13 cases. This reads EVERY fence file
at origin/<default> (the mirror), one file (or a 600-line window of a long one) per call, on Haiku 5.5 ($0.10/$0.50
per MTok) and, beside it, the free DeepSeek V4.1 Flash on opencode, which may be rate-limited or down at any time:
its failure never stops the pass. A file is scanned again only when its blob changes.

Every finding must quote the code line it is about, and the quote is checked against the file (a model that invents
code is dropped here for free). What survives is verified by a stronger model before it becomes a case; the case then
goes through the normal pipeline, where reproduce must show a failing test before any build."""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import claude, db, muse, site
from .core import CFG, log, now, run
from .worktree import mirror_path



def fence() -> dict[str, list[str]]:
    """The fence as path prefixes per repo (config fence.paths; domain_fence says it in words). An install with no
    path fence reads every configured repo whole."""
    return site.fence_paths() or {r: [""] for r in CFG["repos"]}


def fenced(repo: str, path: str) -> bool:
    """Inside the fence: under one of the repo's prefixes, or one of its single files outside them (config
    fence.paths_re, a pattern per repo)."""
    extra = (site.fence().get("paths_re") or {}).get(repo)
    return any(path.startswith(d) for d in fence().get(repo, [])) or bool(extra and re.search(extra, path, re.I))


SKIP = re.compile(r"(\.test\.|\.spec\.|\.stories\.|__tests__|__mocks__|/tests?/|\.d\.ts$|/migrations/|/alembic/|"
                  r"__init__\.py$|/generated/|/types\.ts$|\.snap$)")
EXT = (".py", ".ts", ".tsx")
WINDOW, OVERLAP = 600, 40
DS_MODEL = "opencode/deepseek-v4.1-flash-free"

SCHEMA = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array", "items": {
    "type": "object", "required": ["line", "quote", "title", "trigger", "wrong", "expected", "severity", "confidence"],
    "properties": {
        "line": {"type": "integer"},
        "quote": {"type": "string", "description": "the offending code line copied exactly from the file"},
        "title": {"type": "string"},
        "trigger": {"type": "string", "description": "the concrete input, data or user action that hits it"},
        "wrong": {"type": "string", "description": "what the code does then"},
        "expected": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
        "confidence": {"type": "number"},
        "test": {"type": "string", "description": "a test that would fail today, in one or two sentences"}}}}}}

VERIFY = {"type": "object", "required": ["real", "reachable", "kind", "reason", "needs_migration", "new_ux"], "properties": {
    "real": {"type": "boolean"}, "reachable": {"type": "boolean", "description": "a real user or real data reaches it"},
    "kind": {"type": "string", "enum": ["TASK", "IDEA", "none"]},
    "reason": {"type": "string", "description": "the traced path from trigger to wrong result, with file:line, or why not"},
    "test": {"type": "string"}, "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
    "needs_migration": {"type": "boolean"}, "new_ux": {"type": "boolean"},
    "fix": {"type": "string", "description": "the smallest correct fix, one or two sentences"},
    # the field keeps its name (both prompt sets refer to it); what it asks for is the install's language
    "title_es": {"type": "string", "description": site.own(
        f"the case title for {site.first_name()}'s board: Spanish, plain, at most 70 "
        "characters, what is wrong for the user (e.g. 'El simulador de acreditación acepta un reemplazo que "
        "el backend rechaza')",
        f"the case title for {site.first_name()}'s board: in {site.language_name()}, plain, at most 70 characters, what "
        "is wrong for the user (e.g. 'The checkout accepts a discount code the backend rejects')")}}}

PROMPT = """You are reviewing ONE {what} of {subject} for real bugs.
Repo {repo} at its default branch, read-only, in your working directory. File: {path}{window}

First, understand what this code is FOR: open the types, DTOs, API clients or models it uses and the backend
validator or contract behind them (sibling repos are in the parent directory: {siblings}), so
you know what each field means, which can be null and what null means, which role may do what, and what scope
(school, cycle, period, level, classroom) every query and request must carry. Never guess what another file does.

Then find defects a user or the data would actually hit, looking through this lens:
{classes}

For each finding you must name the concrete trigger (the input, record state or user action) and what goes wrong.
Do not report: style, naming, refactors, performance without a wrong result, missing tests, "could be null" with
no path where it is null, or anything you would need to guess. Copy the offending line EXACTLY into quote (it is
checked against the file; an invented quote is discarded). Zero findings is the right answer for most files.

{body}"""

LENSES = {
    "ts": {
        "data": """- a nullable/optional API field used unguarded (generated types may say string while the backend sends null)
- a fallback or default (?? X, || X, = X) that overwrites a null/0/empty that MEANS something ("no rounding", "no
  limit", "not graded", "all periods")
- score/grade/weight math or comparison: string vs number, decimals, rounding, 0 treated as missing
- new Date('yyyy-MM-dd') or Date math that shifts a day in Mexico's timezone; boundaries off by one
- a request body or params that do not match what the backend validator requires (names, required, scoping ids)""",
        "access": """- missing permission gating on an action or a screen, a check against the wrong permission/role, an action
  shown to a role the backend will reject (or the reverse)
- the wrong scope: a request or filter missing or mixing school/cycle/period/level/grade/group/classroom, an id of
  one entity passed where another is expected (user id vs teacher id vs membership id)
- hardcoded limits, first page only, missing has_more/next walks, pagination or filters that drop records, a count
  that does not apply the same filters as its list""",
        "state": """- stale state: a useEffect/useMemo/useCallback with missing deps, a query key that ignores a filter, a
  mutation that does not invalidate what it changes, an optimistic update never rolled back
- render/refetch loops, an effect that sets the state it depends on, a request fired on every keystroke
- an empty or zero state shown while loading or on error (fake "no data", 0%, "no students")
- an error swallowed so the user sees success, a form that submits twice, a dialog that closes before the save""",
    },
    "py": {
        "data": """- None/empty paths not handled: a missing related row, an empty list, a period or cycle without dates, an
  enrollment without level/grade/group
- a default that overwrites a None that MEANS something (no rounding, no limit, not graded)
- score/grade/weight math: rounding, Decimal vs float, division by zero, 0 treated as missing, averages over the
  wrong set, weights that do not sum, cross-period consistency
- create vs update validation drift (one validates, the other does not; a PATCH that wipes or skips checks)
- date/timezone bugs (naive vs aware, date boundaries), off-by-one in ranges""",
        "access": """- wrong scoping: a query not filtered by school/membership/cycle/period/level/classroom, mixing other
  schools' or other levels' data, an id of one entity used as another (user id vs teacher id vs membership id)
- a permission check missing on a read or write, or checking the wrong role/object
- unbounded or hardcoded limits (a page size the caller controls with no cap, limit=9999), pagination or a count
  that does not apply the same filters as its list, a loop that stops early""",
        "integrity": """- races: read-modify-write without a lock or unique constraint, upserts that duplicate, N writes outside
  one transaction that can commit half
- an exception swallowed, mapped to the wrong status, or a 500 where the input is the caller's fault
- a filter or join that silently drops rows (inner join on a nullable relation, a subject/level not included)""",
    },
}


def lens_text(path: str, lens: str | None) -> str:
    group = LENSES["py" if path.endswith(".py") else "ts"]
    return "\n".join(group.values()) if lens is None else group.get(lens, "")


def lenses_for(path: str) -> list[str]:
    return list(LENSES["py" if path.endswith(".py") else "ts"])


def fence_files(repo: str) -> dict[str, str]:
    """path -> blob sha of every fence source file at the mirror's HEAD (origin/<default>)."""
    out = run(["git", "-C", str(mirror_path(repo)), "ls-tree", "-r", "HEAD"], timeout=120).stdout
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        if not path.endswith(EXT) or SKIP.search(path):
            continue
        if fenced(repo, path):
            files[path] = meta.split()[2]
    return files


def unreviewed_prs(days: int = 30) -> dict[tuple[str, str], list[dict]]:
    """(repo, path) -> merged PRs of the last `days` that touched that fence file with no human approval (CodeRabbit's
    alone satisfies the rulesets). the user, 10-23: those are the likeliest places for bugs. On 10-23: 36 such schools
    PRs (125 fence files, 9,380 added lines) and 8 cometa ones in 30 days. Cached an hour; windows of 10 days because
    GitHub's GraphQL 502s on one big page of files and reviews."""
    hit = db.kv_get("scan_unreviewed") or {}
    if hit.get("at", 0) > time.time() - 3600:
        return {tuple(k.split("|", 1)): v for k, v in hit["files"].items()}
    import datetime as dt
    out: dict[tuple[str, str], list[dict]] = {}
    today = dt.date.today()
    for repo in fence():
        if repo not in CFG["repos"]:
            continue
        for k in range(0, days, 10):
            a, b = today - dt.timedelta(days=k + 9), today - dt.timedelta(days=k)
            r = run(["gh", "pr", "list", "-R", site.slug(repo), "--state", "merged", "--search", f"merged:{a}..{b}",
                     "--limit", "100", "--json", "number,author,reviews,files,mergeCommit,mergedAt"], timeout=180)
            if r.returncode != 0:
                log("scan: merged PR list failed", repo=repo, window=f"{a}..{b}", err=(r.stderr or "")[-200:])
                continue
            for pr in json.loads(r.stdout or "[]"):
                author = (pr.get("author") or {}).get("login", "")
                human = any(v.get("state") == "APPROVED" and (v.get("author") or {}).get("login") not in (author, "coderabbitai")
                            and not (v.get("author") or {}).get("login", "").endswith("[bot]") for v in pr.get("reviews") or [])
                if human or not pr.get("mergeCommit"):
                    continue
                for f in pr.get("files") or []:
                    path = f["path"]
                    if path.endswith(EXT) and not SKIP.search(path) and fenced(repo, path):
                        out.setdefault((repo, path), []).append({"n": pr["number"], "sha": pr["mergeCommit"]["oid"],
                                                                 "at": pr.get("mergedAt")})
    db.kv_set("scan_unreviewed", {"at": time.time(), "files": {f"{r}|{p}": v for (r, p), v in out.items()}})
    return out


def pr_diff_for(repo: str, path: str, prs: list[dict], limit: int = 12000) -> str:
    """The unreviewed PRs' own changes to this file, newest first, from the mirror's history."""
    parts = []
    for pr in sorted(prs, key=lambda p: p.get("at") or "", reverse=True):
        d = run(["git", "-C", str(mirror_path(repo)), "diff", f"{pr['sha']}^", pr["sha"], "--", path], timeout=60).stdout
        if d.strip():
            parts.append(f"### {repo}#{pr['n']} (merged {(pr.get('at') or '')[:10]}, no human approval)\n{d}")
    return "\n".join(parts)[:limit]


def windows(text: str) -> list[tuple[int, int]]:
    n = len(text.splitlines())
    if n <= WINDOW + 100:
        return [(1, n)]
    out, a = [], 1
    while a <= n:
        out.append((a, min(n, a + WINDOW - 1)))
        a += WINDOW - OVERLAP
    return out


DIFF_LENS = """- this file was changed by the pull request(s) below, merged WITHOUT any human review (only a bot approved).
  Find what THOSE changes broke: a caller or consumer not updated for a new signature, field or meaning; a case the
  old code handled that the new code drops; a condition inverted or narrowed; a default that changed; a migration of
  behavior applied to one path but not its twin (create vs update, list vs count, v1 vs v2). Judge the code as it is
  now (numbered below); the diff only tells you where to look."""


def _prompt(repo: str, path: str, text: str, a: int, b: int, full: bool, lens: str | None = None,
            diff: str = "") -> str:
    lines = text.splitlines()
    body = "\n".join(f"{i:5d}  {lines[i - 1]}" for i in range(a, b + 1))
    subject = (CFG.get("scan") or {}).get("subject") or (f"{site.company()}'s code" if site.company() else "this codebase")
    return PROMPT.format(what="file" if full else "part of a file", subject=subject, siblings=site.repos_listed(), repo=repo, path=path,
                         window="" if full else f" (lines {a}-{b} of {len(lines)}; read the rest with your tools when needed)",
                         classes=DIFF_LENS if lens == "diff" else lens_text(path, lens),
                         body=f"```\n{body}\n```" + (f"\n\nThe unreviewed changes:\n```diff\n{diff}\n```" if lens == "diff" else ""))


def anchor(text: str, f: dict) -> dict | None:
    """The finding with its line moved to where its quote really is, or None when the quote is not in the file."""
    q = re.sub(r"\s+", " ", (f.get("quote") or "")).strip()
    if len(q) < 6:
        return None
    lines = text.splitlines()
    norm = [re.sub(r"\s+", " ", l).strip() for l in lines]
    hits = [i + 1 for i, l in enumerate(norm) if q in l or (l and l in q and len(l) > 12)]
    if not hits:  # a multi-line quote: its first line
        head = re.sub(r"\s+", " ", (f.get("quote") or "").strip().splitlines()[0]).strip()
        hits = [i + 1 for i, l in enumerate(norm) if len(head) >= 6 and head in l]
    if not hits:
        return None
    line = min(hits, key=lambda h: abs(h - int(f.get("line") or 0)))
    return {**f, "line": line}


def _haiku(root: Path, prompt: str, model: str = "haiku5") -> list[dict]:
    out = claude.lean("scan", prompt, model=model, cwd=str(root), schema=SCHEMA, add_dirs=(str(root.parent),), timeout=900)
    return out.get("findings") or []


_ds_down = {"until": 0.0}
_ds_lock = threading.Lock()


def _deepseek(root: Path, prompt: str) -> list[dict] | None:
    """None when the lane is down (rate limit, expired key, timeout): the pass goes on without it."""
    if time.time() < _ds_down["until"]:
        return None
    try:
        out = muse.call("ap-patrol", prompt, cwd=str(root), schema=SCHEMA, lane="scan-ds", model=DS_MODEL,
                        timeout=600, retries=1)
        return out.get("findings") or []
    except Exception as e:
        with _ds_lock:
            # an expired key or a 401 will not fix itself within a pass: off for an hour; a rate limit for 10 min
            hard = any(m in str(e).lower() for m in ("expired", "401", "no text"))
            _ds_down["until"] = time.time() + (3600 if hard else 600)
        log("scan: deepseek lane down, going on with haiku", err=str(e)[:200])
        return None


def scan_file(repo: str, path: str, lanes=("haiku", "deepseek"), root: Path | None = None, model: str = "haiku5",
              lensed: bool = True, unreviewed: dict | None = None) -> dict:
    """root: a checkout to read instead of the mirror (the recall benchmark scans the file as it was before a fix)."""
    root = root or mirror_path(repo)
    text = (root / path).read_text(errors="replace")
    wins = windows(text)
    found, ran, dropped = [], set(), 0
    jobs = [(a, b, lens) for a, b in wins for lens in (lenses_for(path) if lensed else [None])]
    diff = pr_diff_for(repo, path, unreviewed.get((repo, path), [])) if unreviewed and root == mirror_path(repo) else ""
    if diff:
        jobs += [(a, b, "diff") for a, b in wins]
    with ThreadPoolExecutor(4) as ex:
        futs = []
        for a, b, lens in jobs:
            prompt = _prompt(repo, path, text, a, b, len(wins) == 1, lens, diff)
            if "haiku" in lanes:
                futs.append(("haiku", ex.submit(_haiku, root, prompt, model)))
            if "deepseek" in lanes:
                futs.append(("deepseek", ex.submit(_deepseek, root, prompt)))
        for lane, fu in futs:
            try:
                res = fu.result()
            except Exception as e:
                log("scan: lane failed", lane=lane, path=path, err=str(e)[:200])
                continue
            if res is None:
                continue
            ran.add(lane)
            for f in res:
                g = anchor(text, f)
                if g is None:
                    dropped += 1
                    continue
                found.append({**g, "lane": lane, "repo": repo, "file": path})
    return {"findings": merge(found), "lanes": sorted(ran), "unanchored": dropped}


def merge(found: list[dict]) -> list[dict]:
    """One finding per (file, line +-3): both lanes on the same line is agreement, kept as lanes=[...]."""
    out: list[dict] = []
    for f in sorted(found, key=lambda f: (f["file"], f["line"])):
        same = next((g for g in out if g["file"] == f["file"] and abs(g["line"] - f["line"]) <= 3), None)
        if same:
            if f["lane"] not in same["lanes"]:
                same["lanes"].append(f["lane"])
            if f.get("confidence", 0) > same.get("confidence", 0) and f["lane"] != same["lane"]:
                same["alt"] = {k: f[k] for k in ("title", "trigger", "wrong")}
            continue
        out.append({**f, "lanes": [f["lane"]]})
    return out


def verify(f: dict, model: str = "opus", root: Path | None = None) -> dict:
    repo = f["repo"]
    root = root or mirror_path(repo)
    prompt = (f"A code scanner reported this possible bug in {repo} (default branch, read-only, your working "
              f"directory):\n{json.dumps({k: f[k] for k in ('file', 'line', 'quote', 'title', 'trigger', 'wrong', 'expected', 'test') if k in f}, ensure_ascii=False, indent=1)}\n\n"
              "Decide whether it is real. Read the file, its callers and the contracts it depends on (backend validators "
              "and DTOs in the sibling repos under the parent directory when the bug crosses a boundary). Try hard to "
              "refute it: a guard elsewhere, a type or validator that makes the trigger impossible, a caller that never "
              "passes that input. real=true only if you can trace the trigger to the wrong result in code. reachable=true "
              "only if a real user or real production data can hit it. kind=TASK for a clear fix in the "
              f"{site.of_team('domain', written=True)} "
              "without migrations or new UX, IDEA if it needs a product decision, none if not real. test: the failing "
              "test that proves it, concretely.")
    return claude.lean("scan_verify", prompt, model=model, cwd=str(root), schema=VERIFY,
                       add_dirs=(str(root.parent),), timeout=900)


def pilot(sample: list[tuple[str, str]], lanes=("haiku", "deepseek"), workers: int = 4) -> list[dict]:
    """Scan a sample (no cases created, no state but call records) and return the merged findings."""
    t0 = now()
    res = []
    with ThreadPoolExecutor(workers) as ex:
        for (repo, path), r in zip(sample, ex.map(lambda rp: scan_file(*rp, lanes=lanes), sample)):
            res.append({"repo": repo, "file": path, **r})
    log("scan pilot", files=len(sample), secs=now() - t0, findings=sum(len(r["findings"]) for r in res))
    return res


# --- the worker job ---------------------------------------------------------------------------------------------

def _cfg() -> dict:
    return {"daily_usd": 6.0, "files_per_run": 24, "verify_model": "opus", "verify_daily": 12, "max_open": 12, "case_usd": 8.0,
            "min_confidence": 0.5, "lanes": ["haiku", "deepseek"], **(CFG.get("scan") or {})}


def _spent_today(lane: str) -> float:
    return db.q1("SELECT coalesce(sum(usd),0) s FROM calls WHERE lane=? AND at>=?", lane,
                 int(time.mktime(time.strptime(db.today(), "%Y-%m-%d"))))["s"]


def due_files(limit: int) -> list[tuple[str, str, str]]:
    """(repo, path, blob) never scanned or changed since their scan: changed first (new code is where bugs land),
    then never scanned, the files that needed the most fixes this year first."""
    seen = {(r["repo"], r["path"]): r["blob"] for r in db.q("SELECT repo, path, blob FROM scan_files")}
    changed, fresh = [], []
    for repo in fence():
        if repo not in CFG["repos"]:
            continue
        for path, blob in fence_files(repo).items():
            old = seen.get((repo, path))
            if old == blob:
                continue
            (changed if old else fresh).append((repo, path, blob))
    heat = fix_history()
    try:
        risky = unreviewed_prs()
    except Exception as e:
        log("scan: unreviewed PRs unavailable", err=str(e)[:200])
        risky = {}
    # declaration files (wiring, exceptions, DTOs, enums) ride along in every fix without holding its logic
    decl = re.compile(r"/(dependencies|exceptions|dtos?|enums?|constants|types|schemas?|index)\.(py|tsx?)$")
    # first what an unreviewed merge touched, then what needed the most fixes this year
    key = lambda d: (-(len(risky.get((d[0], d[1]), [])) > 0), -heat.get((d[0], d[1]), 0) / (4 if decl.search(d[1]) else 1))
    changed.sort(key=key)
    fresh.sort(key=key)
    return (changed + fresh)[:limit]


def fix_history() -> dict[tuple[str, str], int]:
    """How many fix commits touched each file this year: bugs cluster, so files that needed fixes are read first."""
    out: dict[tuple[str, str], int] = {}
    for repo in fence():
        if repo not in CFG["repos"]:
            continue
        log_ = run(["git", "-C", str(mirror_path(repo)), "log", "HEAD", "--since=2026-01-01", "--no-merges", "-i",
                    "-E", "--grep=^(fix|bug|hotfix)", "--format=", "--name-only"], timeout=120).stdout
        for path in filter(None, log_.splitlines()):
            out[(repo, path)] = out.get((repo, path), 0) + 1
    return out


def _store(repo: str, path: str, blob: str, res: dict) -> int:
    new = 0
    for f in res["findings"]:
        if f.get("severity") == "minor" or f.get("confidence", 0) < _cfg()["min_confidence"] and len(f["lanes"]) < 2:
            continue
        near = db.q1("SELECT id FROM scan_findings WHERE repo=? AND path=? AND abs(line-?)<=3 AND status NOT IN ('rejected')",
                     repo, path, f["line"])
        if near:
            continue
        db.x("INSERT INTO scan_findings(repo,path,blob,line,title,severity,lanes,data,status,created_at,updated_at) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?)", repo, path, blob, f["line"], f["title"][:200], f["severity"],
             ",".join(f["lanes"]), json.dumps(f, ensure_ascii=False), "new", now(), now())
        new += 1
    db.x("INSERT OR REPLACE INTO scan_files VALUES(?,?,?,?,?,?)", repo, path, blob, now(), ",".join(res["lanes"]),
         len(res["findings"]))
    return new


def run_job() -> dict:
    """One scan step: up to files_per_run due files while today's scan spend is under daily_usd, then verify."""
    c = _cfg()
    if _spent_today("scan") >= c["daily_usd"]:
        return {"scanned": 0, "why": "daily scan budget spent", **verify_job()}
    due = due_files(c["files_per_run"])
    stored = 0
    with ThreadPoolExecutor(4) as ex:
        try:
            risky = unreviewed_prs()
        except Exception:
            risky = {}
        for (repo, path, blob), res in zip(due, ex.map(lambda d: _safe_scan(d[0], d[1], tuple(c["lanes"]), risky), due)):
            if res is not None:
                stored += _store(repo, path, blob, res)
    out = {"scanned": len(due), "new_findings": stored, **verify_job()}
    log("scan", **out)
    return out


def _safe_scan(repo, path, lanes, unreviewed=None):
    try:
        return scan_file(repo, path, lanes=lanes, unreviewed=unreviewed)
    except Exception as e:
        log("scan: file failed", repo=repo, path=path, err=str(e)[:200])
        return None


def open_scan_cases() -> int:
    """Scan cases still taking build capacity. A published PR waiting on review or merge does not: counting those
    capped the scan at max_open PRs in flight in total, not at max_open being built."""
    from .worktree import PARKED
    idle = tuple(db.TERMINAL) + PARKED + ("ci_wait",)
    return db.q1(f"SELECT count(*) n FROM items WHERE json_extract(data,'$.scan') IS NOT NULL "
                 f"AND status NOT IN ({','.join('?' * len(idle))})", *idle)["n"]


def verify_job() -> dict:
    """Verify the best new findings while the build lane has room: a case nobody can build only waits and goes stale."""
    c = _cfg()
    done_today = db.q1("SELECT count(*) n FROM scan_findings WHERE verdict IS NOT NULL AND updated_at>=?",
                       int(time.mktime(time.strptime(db.today(), "%Y-%m-%d"))))["n"]
    room = min(c["verify_daily"] - done_today, c["max_open"] - open_scan_cases())
    made = rejected = 0
    if room <= 0:
        return {"verified": 0, "cases": 0}
    rows = db.q("SELECT * FROM scan_findings WHERE status='new' ORDER BY (severity='critical') DESC, "
                "(instr(lanes, ',') > 0) DESC, json_extract(data,'$.confidence') DESC, id LIMIT ?", room)
    for r in rows:
        f = json.loads(r["data"])
        if fence_files(r["repo"]).get(r["path"]) != r["blob"]:
            db.x("UPDATE scan_findings SET status='stale', updated_at=? WHERE id=?", now(), r["id"])
            continue  # the file changed since: its rescan reports what is still there
        try:
            v = verify(f, model=c["verify_model"])
        except Exception as e:
            log("scan: verify failed", finding=r["id"], err=str(e)[:200])
            break
        ok = v["real"] and v["reachable"] and v["kind"] == "TASK" and not v.get("needs_migration") and not v.get("new_ux")
        if ok and v.get("severity") == "minor" and not c.get("build_minor"):
            # the verifier's severity, not the scanner's, decides: a minor waits until majors run out (scan.build_minor)
            db.x("UPDATE scan_findings SET status='minor', verdict=?, updated_at=? WHERE id=?",
                 json.dumps(v, ensure_ascii=False), now(), r["id"])
            continue
        db.x("UPDATE scan_findings SET status=?, verdict=?, updated_at=? WHERE id=?", "real" if ok else "rejected",
             json.dumps(v, ensure_ascii=False), now(), r["id"])
        if not ok:
            rejected += 1
            continue
        r = dict(r, verdict=json.dumps(v, ensure_ascii=False))
        r["item_id"] = make_case(r, f, v)
        made += bool(r["item_id"])
    return {"verified": made + rejected, "cases": made}


def make_case(r: dict, f: dict, v: dict) -> int | None:
    """A verified finding becomes a case that starts at plan with its dossier already written (the verifier traced it;
    an investigate pass on top cost ~$3 a case and said the same)."""
    key = f"scan:{r['repo']}:{r['path']}:{r['line']}"
    if db.open_item_for_thread(key):
        return None
    where = f"{r['repo']}/{r['path']}:{r['line']}"
    dossier = {
        "problem": f"{f['title']}. Trigger: {f['trigger']}. Today: {f['wrong']}. Expected: {f['expected']}.",
        "hypotheses": [{"h": f["title"], "confidence": 0.9, "evidence": f"{where} `{f['quote'].strip()[:200]}`"}],
        "root_cause": v["reason"], "root_cause_status": "proven", "repos": [r["repo"]],
        "needs_migration": False, "new_ux": False, "in_fence": True, "problem_kind": "bug", "recommendation": "build",
        "evidence": [f"{where}: {f['quote'].strip()[:300]}", f"failing test: {v.get('test') or f.get('test') or ''}",
                     f"fix: {v.get('fix') or ''}"], "reason": "code scan finding, verified"}
    # a board title like his other cases': Spanish and short, not the scanner's English line behind a 'scan:' prefix
    #
    title = (v.get("title_es") or f["title"]).strip()[:90]
    iid = db.create_item("TASK", "plan", title, key, repo=r["repo"], urgency=1,
                         data={"summary": f["title"], "text": json.dumps(f, ensure_ascii=False), "dossier": dossier,
                               "scan": r["id"], "from": "patrol", "patrol": True})
    if db.item(iid)["thread_key"] != key:  # folded into an open case doing the same work: never overwrite its dossier
        db.x("UPDATE scan_findings SET status='dup', item_id=?, updated_at=? WHERE id=?", iid, now(), r["id"])
        return None
    from .core import item_dir
    d = item_dir(iid)
    (d / "dossier.json").write_text(json.dumps(dossier, ensure_ascii=False, indent=1))
    (d / "context.md").write_text(f"Code scan finding (no Slack thread, no reporter): {where}\n\n"
                                  f"```\n{f['quote'].strip()}\n```\n\nScanner: {json.dumps(f, ensure_ascii=False, indent=1)}\n\n"
                                  f"Verifier ({_cfg()['verify_model']}): {json.dumps(v, ensure_ascii=False, indent=1)}\n")
    db.x("UPDATE scan_findings SET status='case', item_id=?, updated_at=? WHERE id=?", iid, now(), r["id"])
    log("scan: case made", item=iid, finding=r["id"], where=where)
    return iid


# stages that check or deliver the attempt already paid for. The cap stops the next round (plan, build, fix, ci_fix),
# never the gate of what a round just built: #1214 crossed the cap inside its first build ($7.02) and was blocked 3 s
# into the gate, #1216 inside its first fix, so the money was spent and the code never tested. Every way out
# of these stages to new work passes through a capped stage, so a case past its cap gets one check, not more rounds.
SETTLES = ("gate", "self_review", "publish", "ci_wait", "verify_done", "pr_body_refresh", "announce", "announce_check",
           "waiting")


def over_budget(it: dict) -> str | None:
    """A scan case is a small, verified, test-first fix: one that has already cost case_usd is not converging, and more
    rounds only sink money (October: 35 built cases averaged $27, 4.3 fix rounds and 3.2 Fable self-reviews each)."""
    if not it["data"].get("scan") or it.get("status") in SETTLES:
        return None
    spent = db.q1("SELECT coalesce(sum(usd),0) s FROM calls WHERE item_id=?", it["id"])["s"]
    cap = _cfg()["case_usd"] + float(it["data"].get("scan_budget_extra") or 0)
    return site.t(f"scan case over its cap: ${spent:.2f} of ${cap:.2f}",
                  f"caso de scan pasó su tope: ${spent:.2f} de ${cap:.2f}") if spent >= cap else None
