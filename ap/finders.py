"""Parallel Muse review passes, one per defect class (Claude Code Review's find ->
verify -> rank shape). Used for squad reviews AND for self-review of the harness's own
diffs. Finders are free, so they run wide; Opus only verifies what they surface."""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, muse, site
from .core import CFG, log

FOCUS = {
    "correctness": "Logic errors, wrong conditions, off-by-one, broken control flow, unhandled errors, race conditions, wrong data mutations. Trace each changed function's callers.",
    "contract-nullability": "Frontend/backend contract breaks: fields the OpenAPI marks nullable/optional used without guards, request payloads that do not match the backend validator/DTO (read it in the sibling repo), changed response shapes that break consumers.",
    "sibling-parity": "Validation or behavior present on one path but missing on its siblings (create vs update vs override vs bulk, list vs detail, dashboard vs guardian surfaces).",
    "dates-timezones": "new Date('yyyy-MM-dd') UTC shifts, naive datetime comparisons, timezone-less parsing, period/cycle boundary errors. The repo has local-time parsers (parsePeriodDate, date-fns parse): flag bypasses.",
    "state-triad-ui": "Loading, error and empty states: fake zeros or " + site.own("'sin datos'", "'no data'") + " while a query is loading or failed, missing error handling, preview/demo-mode fixture breakage, stale cache after mutations.",
    "permissions-security": "Missing permission gating on new UI/endpoints, authz bypass, tenant/school scoping leaks, injection, secrets in code.",
    "pagination-performance": "Hardcoded fetch limits (9999/10000), 'fetch all' without walking has_more with a hard page cap that errors, N+1 queries, unbounded loops over large schools.",
    "tests-evidence": "New behavior without a test that would fail without the change, vacuous tests (assert on mocks, early-return paths), removed or skipped tests, tests that cannot catch the bug being fixed.",
    # the first install's team writes its product copy in es-MX; another team's copy convention is its repo's own
    "conventions": site.own("Team conventions that reviewers enforce: English code comments, no PR/ticket refs in source, es-MX user copy, reuse of existing components/helpers instead of new ones, no new UI concepts without design sign-off.",
                            "Team conventions that reviewers enforce: English code comments, no PR/ticket refs in source, user-facing copy in the language and tone the product already uses, reuse of existing components/helpers instead of new ones, no new UI concepts without design sign-off."),
}

FOCUS["all"] = "Check every one of these areas:\n" + "\n".join(f"- {k}: {v}" for k, v in FOCUS.items())

SCHEMA = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array", "items": {
    "type": "object", "required": ["file", "line", "severity", "claim", "evidence"],
    "properties": {"file": {"type": "string"}, "line": {"type": "integer"},
                   "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                   "claim": {"type": "string"}, "evidence": {"type": "string"},
                   "suggestion": {"type": "string"}}}}}}


def run(wt: Path, diff_file: Path, context: str, item_id: int, passes: list[str] | None = None) -> list[dict]:
    passes = passes or CFG["quality"]["review_finder_passes"]
    if getattr(muse._prio, "hi", False) and passes == CFG["quality"]["review_finder_passes"]:
        passes = ["all"]  # delivering his own PR fixes: one combined pass (free-model throughput is the bottleneck)
    if muse._claude_route("review-all") and passes == CFG["quality"]["review_finder_passes"]:
        # on Opus the nine narrow passes are waste: one combined pass, plus a correctness-only pass
        # when the diff is big enough that one reading will skim it
        big = diff_file.read_text(errors="ignore").count("\n") > 400
        passes = ["all"] + (["correctness"] if big else [])

    hi = bool(getattr(__import__("ap.muse", fromlist=["x"])._prio, "hi", False))
    from . import claude as _claude
    ev = getattr(_claude._pool_override, "eval", False)

    def one(name: str) -> list[dict]:
        muse._prio.hi = hi  # finder threads inherit the caller's priority
        _claude._pool_override.eval = ev  # and an eval run's tag (claude.eval_ctx)
        prompt = (f"FOCUS AREA: {name}\n{FOCUS[name]}\n\n"
                  f"The worktree at {wt} is checked out at the PR head. The unified diff is at {diff_file} "
                  f"(read it first). Line numbers you report are NEW-file line numbers.\n\n"
                  f"<pr_context>\n{context[:6000]}\n</pr_context>")
        try:
            ck = f"finder:{item_id}:{name}:{hashlib.sha1(diff_file.read_bytes()).hexdigest()[:12]}"
            hit = db.kv_get(ck)
            if hit is not None:  # same diff already reviewed by this pass (survives restarts)
                return hit
            own = getattr(muse._prio, "hi", False) or db.q1("SELECT 1 FROM items WHERE id=? AND kind IN ('TASK','INCIDENT','IDEA','CI','COMMENTS')", item_id)
            lane = f"review-own-{name}" if own else f"review-{name}"  # own PRs: Sonnet finders, Opus verifies after
            out = muse.call("ap-review", prompt, cwd=str(wt), schema=SCHEMA, lane=lane, item_id=item_id,
                            timeout=(600 if own else 900) if name == "all" else 600, retries=0 if own else 1)
            res = [{**f, "pass": name} for f in out["findings"]]
            db.kv_set(ck, res)
            return res
        except Exception as e:
            from . import claude
            if isinstance(e, (claude.BudgetExhausted, claude.ClaudeLimited)):
                raise  # a pause, not a verdict: the stage waits instead of counting a failure
            log("finder pass failed", pass_=name, err=str(e)[:200])
            return None

    with ThreadPoolExecutor(max_workers=len(passes), thread_name_prefix="finder") as ex:
        results = list(ex.map(one, passes))  # a raised pause propagates out of map()
    failed = [p for p, r in zip(passes, results) if r is None]
    found = [f for r in results if r for f in r]
    if failed and not found:
        # 'no findings' is a verdict only when every pass read the diff (2026-10-01: #378, schools#973, the combined
        # pass timed out, the correctness pass found nothing, and he was told it could be approved). The retry reruns
        # only the failed passes: the others are cached per diff
        raise RuntimeError(f"finder pass failed ({', '.join(failed)}) and the rest found nothing: not declaring it clean")
    # read-only lanes must not leave traces in the worktree (defense in depth)
    from .core import run as sh
    sh(["git", "-C", str(wt), "checkout", "--", "."])
    sh(["git", "-C", str(wt), "clean", "-fdq", "-e", "AP_LEDGER.json"])
    for i, f in enumerate(found):
        f["id"] = i
    log("finders done", item=item_id, passes=len(passes), findings=len(found))
    return found


def diff_right_lines(diff: str) -> dict[str, set[int]]:
    """NEW-side line numbers present in the diff hunks, per file (valid review anchors)."""
    out: dict[str, set[int]] = {}
    cur, line = None, 0
    for l in diff.splitlines():
        if l.startswith("+++ "):
            cur = l[6:] if l.startswith("+++ b/") else None
            if cur:
                out.setdefault(cur, set())
        elif l.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", l)
            line = int(m.group(1)) if m else 0
        elif cur is not None and not l.startswith("---"):
            if l.startswith("+"):
                out[cur].add(line)
                line += 1
            elif l.startswith("-"):
                pass
            else:
                out[cur].add(line)
                line += 1
    return out
