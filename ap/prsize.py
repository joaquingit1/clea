"""PR size budget: net changed lines a reviewer has to read, checked at the gate stage.

Why: harness bug-fix PRs were +221 to +1,342 lines while the user's own fixes of the same kind are
+48/+61 and merge the same day; seven harness PRs sat with zero human reviews. Tests, lockfiles, generated API
types and binaries do not count (a reviewer does not read them line by line); renames count only their edits.
Limits live in config.json quality.size_budget ({"bug": 250, "feature": 600} by default).
"""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT_BUDGET = {"bug": 250, "feature": 600}
BUG_KINDS = ("bug", "incident")

TEST_RE = re.compile(r"(^|/)(tests?|__tests__|__mocks__|__snapshots__|e2e|fixtures)/|(^|/)test_[^/]+\.py$|_test\.py$"
                     r"|(^|/)conftest\.py$|\.(test|spec)\.[cm]?[jt]sx?$|\.snap$")
LOCKFILES = {"pnpm-lock.yaml", "package-lock.json", "yarn.lock", "npm-shrinkwrap.json", "bun.lockb", "uv.lock",
             "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock", "requirements.lock"}
GENERATED_RE = re.compile(r"(^|/)frontend/packages/trpc/src/.*types\.ts$|(^|/)(__generated__|generated)/|\.generated\.\w+$"
                          r"|\.gen\.[jt]sx?$|_pb2(_grpc)?\.pyi?$|\.min\.(js|css)$")


def new_path(raw: str) -> str:
    """The destination path of a numstat entry: 'a/{old => new}/f.py' -> 'a/new/f.py', 'old.py => new.py' -> 'new.py'."""
    m = re.search(r"\{([^{}]*) => ([^{}]*)\}", raw)
    if m:
        return re.sub(r"/{2,}", "/", raw[:m.start()] + m.group(2) + raw[m.end():]).lstrip("/")
    if " => " in raw:
        return raw.split(" => ", 1)[1]
    return raw


def classify(path: str) -> str:
    """code | test | lockfile | generated (only 'code' counts against the budget)."""
    if Path(path).name in LOCKFILES:
        return "lockfile"
    if GENERATED_RE.search(path):
        return "generated"
    if TEST_RE.search(path):
        return "test"
    return "code"


def measure(numstat: str) -> dict:
    """Parse `git diff --numstat -M` output. Binary files show '-\t-' and never count; a pure rename shows 0 0."""
    counted, excluded, binary = [], [], []
    for line in (numstat or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, raw = parts
        path = new_path(raw.strip())
        renamed = path != raw.strip()
        if a == "-" or d == "-":
            binary.append(path)
            continue
        try:
            n = int(a) + int(d)
        except ValueError:
            continue
        kind = classify(path)
        entry = {"path": path, "lines": n, "kind": kind, **({"renamed": True} if renamed else {})}
        (counted if kind == "code" else excluded).append(entry)
    return {"net": sum(e["lines"] for e in counted), "counted": sorted(counted, key=lambda e: -e["lines"]),
            "excluded": excluded, "binary": binary}


def budget_kind(problem_kind: str | None, item_kind: str = "") -> str:
    if (problem_kind or "").lower() in BUG_KINDS or (not problem_kind and item_kind == "INCIDENT"):
        return "bug"
    if not problem_kind and item_kind == "TASK":
        return "bug"  # a TASK without a dossier kind is almost always a report of something broken
    return "feature"


def limit_for(kind: str, cfg: dict | None = None) -> int:
    b = {**DEFAULT_BUDGET, **(((cfg or {}).get("quality") or {}).get("size_budget") or {})}
    return int(b.get(kind, b["feature"]))


def check(numstat: str, problem_kind: str | None, item_kind: str = "", cfg: dict | None = None) -> dict:
    m = measure(numstat)
    kind = budget_kind(problem_kind, item_kind)
    limit = limit_for(kind, cfg)
    return {**m, "kind": kind, "limit": limit, "over": m["net"] > limit}


def numstat_for(wt: Path | str, base: str) -> str:
    from .core import run
    return run(["git", "-C", str(wt), "diff", "--numstat", "-M", f"origin/{base}...HEAD"], timeout=60).stdout


def summary(res: dict, top: int = 5) -> str:
    """One line a model (or a PR reader) can act on: the total, the limit and the biggest files."""
    biggest = ", ".join(f"{e['path']} ({e['lines']})" for e in res["counted"][:top])
    from . import site
    return site.t(f"{res['net']} net lines (budget {res['limit']} for {'a bug' if res['kind'] == 'bug' else 'a feature'}; "
                  f"not counting tests, lockfiles, generated types or binaries). Biggest: {biggest or '-'}",
                  f"{res['net']} líneas netas (presupuesto {res['limit']} para {'un bug' if res['kind'] == 'bug' else 'una feature'}; "
                  f"sin tests, lockfiles, tipos generados ni binarios). Más grandes: {biggest or '-'}")
