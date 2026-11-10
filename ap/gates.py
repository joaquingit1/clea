"""Deterministic quality gates, run by code (not by a model deciding it ran them).

Produces the gauntlet exit ledger: every layer is pass / fail / skip-with-reason.
The model-driven layers (Muse finders, Opus verification, CodeRabbit triage) live in
pipeline.self_review; this module is what no model can talk its way past.
"""
from __future__ import annotations

import time

import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from . import site
from .core import CFG, VAR, log, run
from .merge_order import has_migration
from .worktree import base_worktree, seed_tsbuildinfo

CACHE = VAR / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

SECRET = re.compile(r"(AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9]{32,})")
COMMENT = re.compile(r"^\s*(#|//|/\*|\*|\{/\*)")
TICKET_REF = re.compile(r"(\b[A-Z]{2,6}-\d{2,6}\b|(?<![\w/])#\d{3,5}\b|/pull/\d+)")


def layer(name, status, detail="") -> dict:
    return {"name": name, "status": status, "detail": str(detail)[-3000:]}


def git(wt: Path, *args, timeout=300):
    return run(["git", "-C", str(wt), *args], timeout=timeout)


def changed_files(wt: Path, repo: str) -> list[str]:
    d = CFG["repos"][repo]["default"]
    out = git(wt, "diff", "--name-only", "--diff-filter=ACMR", f"origin/{d}...HEAD").stdout
    return [f for f in out.splitlines() if f.strip()]


def plus(diff: str) -> list[tuple[str, str]]:
    """(file, line) for every line a unified diff adds."""
    out, cur = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((cur, line[1:]))
    return out


def added_lines(wt: Path, repo: str, since: str | None = None) -> list[tuple[str, str]]:
    d = CFG["repos"][repo]["default"]
    # the PR's net diff vs base: excludes whatever a base merge brought in (a --no-merges log
    # does not: main's own commits are ordinary commits, 2026-09-23 it flagged payments files)
    net = plus(git(wt, "diff", "-U0", f"origin/{d}...HEAD").stdout)
    if not since:
        return net
    # and only lines the harness itself added, never the user's pre-existing PR lines
    mine = set(plus(git(wt, "diff", "-U0", f"{since}..HEAD").stdout))
    return [l for l in net if l in mine]


def freshness(wt: Path, repo: str, published: bool = False) -> dict:
    """Rebase unpublished branches. Published ones are left alone (a rebase needs a force-push,
    a merge dismisses approvals); babysit updates them through GitHub."""
    d = CFG["repos"][repo]["default"]
    git(wt, "fetch", "--quiet", "origin", d)
    behind = int(git(wt, "rev-list", "--count", f"HEAD..origin/{d}").stdout.strip() or 0)
    if behind == 0:
        return layer("freshness vs base", "pass", "0 behind")
    if published:
        # never merge base into an existing PR here: it dismisses approvals and pulls in unrelated
        # code; babysit updates the branch through GitHub when it is BEHIND and approved
        return layer("freshness vs base", "pass", f"{behind} behind (published PR: base not merged here)")
    if git(wt, "rev-list", "--merges", "-1", f"origin/{d}..HEAD").stdout.strip():
        # stacked on another branch merged in (or on a merge of the base itself): a rebase would flatten that PR's commits
        # into this one's. The base must still merge cleanly: a conflicting PR goes out CONFLICTING and CI never runs
        # (2026-10-19: #917 merged main on 10-17, passed here '21 behind', was pushed with a conflict, and verify_done
        # found it after ci_wait, so the fix, gate, review and publish all ran a second time)
        clash = git(wt, "merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", f"origin/{d}")
        if clash.returncode == 1:
            files = [f for f in clash.stdout.splitlines()[1:] if f.strip()]
            return layer("freshness vs base", "fail", f"{behind} behind and origin/{d} does not merge cleanly (merge it in "
                         f"and resolve, never rebase this branch): conflicts in {', '.join(files[:10])}")
        return layer("freshness vs base", "pass", f"{behind} behind (a merged-in branch: not rebased)")
    r = git(wt, "rebase", f"origin/{d}", timeout=600)
    if r.returncode != 0:
        git(wt, "rebase", "--abort")
        return layer("freshness vs base", "fail", f"{behind} behind; rebase conflicts:\n{r.stdout[-1500:]}{r.stderr[-800:]}")
    return layer("freshness vs base", "pass", f"rebased over {behind} commits")


def hygiene(wt: Path, repo: str, files: list[str], scope: list[str] | None, since: str | None = None) -> list[dict]:
    out = []
    if has_migration(repo, files):
        out.append(layer("no migrations", "fail", "diff touches migrations; the harness never builds migrations (owners do)"))
    else:
        out.append(layer("no migrations", "pass"))
    if scope:
        outside = [f for f in files if not any(f.startswith(s) for s in scope)
                   and not re.search(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|\.(test|spec)\.[jt]sx?$", f)]
        # the plan's file list is a guess; a fix legitimately touches neighbours (2026-09-25: TICKET-1355
        # blocked 3 rounds for editing the incidences controller). Only the domain fence fails.
        forbidden = [f for f in outside if re.search(r"inscripci|admission|admisi|billing|payment|charge|financ|cobro|students-platform", f, re.I)]
        out.append(layer("scope fence", "fail" if forbidden else "pass",
                         (site.t("outside the domain: ", "fuera del dominio: ") + ", ".join(forbidden[:6])) if forbidden else
                         ((site.t("files outside the plan (ok): ", "archivos fuera del plan (ok): ") + ", ".join(outside[:6]))
                          if outside else "")))
    problems = [p for f, line in added_lines(wt, repo, since) for p in line_problems(f, line)]
    out.append(layer("diff hygiene", "fail" if problems else "pass", "\n".join(problems[:30])))
    return out


def line_problems(f: str, line: str) -> list[str]:
    """The diff hygiene rules for one added line. Also applied to the reproduction test when it is accepted: once
    accepted it can never be edited, so a line the gate rejects there blocked every fix round."""
    out = []
    is_test = bool(re.search(r"(test|spec|__tests__|fixtures)", f))
    if re.match(r"^(<<<<<<<|=======|>>>>>>>)( |$)", line):
        out.append(f"{f}: conflict marker")
    if SECRET.search(line):
        out.append(f"{f}: possible secret")
    if re.search(r"\b(it|test|describe)\.only\(", line):
        out.append(f"{f}: .only left in tests")
    if not is_test and re.search(r"\bconsole\.log\(|\bdebugger;", line):
        out.append(f"{f}: console.log/debugger")
    if f.endswith(".py") and not is_test and re.search(r"^\s*(print\(|breakpoint\(|import pdb)", line):
        out.append(f"{f}: print/breakpoint")
    if COMMENT.match(line):
        if "—" in line:
            out.append(f"{f}: em dash in a code comment (style rule)")
        if TICKET_REF.search(line):
            out.append(f"{f}: PR/ticket reference in a code comment")
    return out


# ---- frontend (cometa/frontend) ------------------------------------------
def _ensure_frontend_deps(fe: Path) -> str | None:
    # reinstall when the lockfile changed, not only when node_modules is missing: a branch that
    # merged main picked up @json-render/* and every tsc/vitest layer failed on stale deps
    # (cometa#1931, 2026-09-23), which is a harness artifact, not a finding
    lock = fe / "pnpm-lock.yaml"
    marker = fe / "node_modules" / ".ap-lock-hash"
    want = hashlib.sha1(lock.read_bytes()).hexdigest() if lock.exists() else ""
    if not (fe / "node_modules").exists() or not marker.exists() or marker.read_text() != want:
        r = run(["pnpm", "install", "--frozen-lockfile", "--prefer-offline", "--ignore-scripts"], cwd=fe, timeout=1500)
        if r.returncode != 0:
            return "pnpm install failed: " + (r.stderr or r.stdout)[-800:]
        marker.write_text(want)
    # a package the apps import through its build output is rebuilt when its source is not the one its dist was built
    # from: on 10-15 #624 merged main (recreo's table.tsx changed, the lockfile did not) and vitest failed a test outside
    # the diff against the 18:25 dist; the case escalated to a teammate over a harness artifact
    for pkg in _built_packages(fe):
        stamp, src = pkg / "dist" / ".ap-src-hash", _src_hash(fe, pkg)
        if stamp.exists() and stamp.read_text() == src:
            continue
        r = run(["pnpm", "--filter", _pkg_name(fe, pkg.relative_to(fe).as_posix()), "run", "build"], cwd=fe, timeout=900)
        if r.returncode != 0:
            return f"{pkg.name} build failed: " + (r.stderr or r.stdout)[-800:]
        stamp.parent.mkdir(exist_ok=True)
        stamp.write_text(src)
    return None


def _built_packages(fe: Path) -> list[Path]:
    """Workspace packages consumed through a build output (main/types under dist/), with a build script."""
    out = []
    for pj in sorted((fe / "packages").glob("*/package.json")):
        d = json.loads(pj.read_text())
        if "build" in d.get("scripts", {}) and "dist/" in f"{d.get('main', '')} {d.get('types', '')}":
            out.append(pj.parent)
    return out


def _src_hash(fe: Path, pkg: Path) -> str:
    """The package's source as the worktree has it: committed tree plus uncommitted and untracked changes."""
    rel = pkg.relative_to(fe).as_posix()
    parts = [git(fe, "rev-parse", f"HEAD:./{rel}").stdout, git(fe, "diff", "HEAD", "--", rel).stdout,
             git(fe, "ls-files", "--others", "--exclude-standard", "--", rel).stdout]
    return hashlib.sha1("\0".join(parts).encode()).hexdigest()


TSC_LINE = re.compile(r"^(?P<file>[^(\s]+)\(\d+,\d+\): error (?P<code>TS\d+): (?P<msg>.*)$")


class GateToolError(Exception):
    """The tool itself did not run (wrong binary, crash, missing deps): never a pass."""


def _pkg_name(fe: Path, project: str) -> str:
    return json.loads((fe / project / "package.json").read_text())["name"]


def _vitest_failures(txt: str) -> str:
    """What a fix session needs: each failing test and its assertion or error line, then the summary. The raw tail
    was a React stack trace with the test names and messages cut off."""
    lines = txt.splitlines()
    keep = []
    for i, l in enumerate(lines):
        if re.search(r"^\s*(FAIL|×|✗)\s|AssertionError|TestingLibraryElementError|Error: |expected .+ to |Expected|Received|"
                     r"Unable to find|Test Files|Tests\s+\d", l) and not re.search(r"^\s*❯ |node_modules", l):
            keep.append(l.strip()[:300])
    out = "\n".join(dict.fromkeys(keep))
    return (out[:3500] + "\n…") if len(out) > 3500 else (out or txt[-2500:])


WORKER_START = re.compile(r"Failed to start (?:forks|threads) worker|Timeout waiting for worker to respond")


def _vitest_lock():
    """One vitest run at a time across gates (a file lock, so it holds across threads)."""
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def held():
        CACHE.mkdir(parents=True, exist_ok=True)
        with open(CACHE / "vitest.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    return held()


def _tool(cmd: list[str], cwd: Path, timeout: int):
    """run() for a gate layer: a tool that times out did not run (an environment error, the layer says so), it does
    not crash the stage. A dashboard tsc over 1500 s raised out of the gate on 10-06/07 (#593): the stage retried
    from the start, the layers that had passed were lost, and the case sat in gate."""
    try:
        return run(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GateToolError(f"{' '.join(cmd[:6])} timed out after {timeout}s")


def _tsc_errors(fe: Path, project: str) -> tuple[set[str], str]:
    # pnpm exec, not npx: npx resolved an unrelated npm package named "tsc", exited 1 and printed no
    # diagnostics, which the old gate read as "0 new errors" (false pass, 2026-09-23).
    r = _tool(["pnpm", "--filter", _pkg_name(fe, project), "exec", "tsc", "--noEmit"], fe,
              CFG["quality"]["gate_timeout_s"])
    errs = set()
    for line in (r.stdout + r.stderr).splitlines():
        m = TSC_LINE.match(line.strip())
        if m:
            errs.add(f"{m['file']}|{m['code']}|{m['msg'][:160]}")
    if r.returncode != 0 and not errs:
        raise GateToolError(f"tsc exited {r.returncode} without diagnostics: {(r.stdout + r.stderr)[-800:]}")
    return errs, r.stdout[-3000:]


def _tsc_baseline(repo: str, project: str) -> set[str]:
    base = base_worktree(repo)
    sha = git(base, "rev-parse", "HEAD").stdout.strip()
    f = CACHE / f"tsc-{project.replace('/', '_')}-{sha[:12]}.json"
    if f.exists():
        return set(json.loads(f.read_text()))
    fe = base / "frontend"
    if _ensure_frontend_deps(fe):
        raise GateToolError("base worktree deps failed; cannot compute the tsc baseline")
    errs, _ = _tsc_errors(fe, project)
    f.write_text(json.dumps(sorted(errs)))
    return errs


def typecheck(wt: Path, repo: str, proj: str) -> dict:
    """The tsc layer: new errors vs the base, on a worktree seeded from the base's.tsbuildinfo. Code sessions run it
    too (bin/ap-tsc): on 10-15 #813's build ran a cold whole-dashboard tsc twice, each cut at the 20 min Bash timeout
    beside the gate's own, and the build stage was killed at its 1 h budget for a two-constant fix."""
    try:
        base_errs = _tsc_baseline(repo, proj)  # first: it brings the base's.tsbuildinfo to the base the branch is seeded from
        seeded = seed_tsbuildinfo(wt, repo)
        t0 = time.time()
        errs, _ = _tsc_errors(wt / "frontend", proj)
        log("gate tsc", project=proj, secs=round(time.time() - t0), seeded=proj in seeded)
    except GateToolError as e:
        return layer(f"tsc {proj}", "fail", f"TSC DID NOT RUN: {e}")
    new = errs - base_errs
    return layer(f"tsc {proj} (new errors vs base)", "fail" if new else "pass",
                 "\n".join(sorted(new)[:40]) if new else f"{len(errs)} total, 0 new")


def frontend(wt: Path, repo: str, files: list[str]) -> list[dict]:
    fe_files = [f[len("frontend/"):] for f in files if f.startswith("frontend/")]
    if not fe_files:
        return []
    fe = wt / "frontend"
    err = _ensure_frontend_deps(fe)
    if err:
        return [layer("frontend deps", "fail", err)]
    out = []
    lintable = [f for f in fe_files if re.search(r"\.(ts|tsx|js|jsx)$", f) and not f.startswith("apps/backoffice/")]
    if lintable:
        try:
            r = _tool(["pnpm", "exec", "eslint", "--no-warn-ignored", *lintable], fe, 900)
            # eslint: 0 clean, 1 lint errors, 2 = it could not run (config/crash): never a pass
            out.append(layer("eslint (changed files)", {0: "pass", 1: "fail"}.get(r.returncode, "fail"),
                             (r.stdout + ("\nESLINT DID NOT RUN: " + r.stderr[-800:] if r.returncode > 1 else ""))[-2500:]))
        except GateToolError as e:
            out.append(layer("eslint (changed files)", "fail", f"ESLINT DID NOT RUN: {e}"))
    else:
        out.append(layer("eslint (changed files)", "skip", "no lintable files"))
    projects = sorted({"/".join(f.split("/")[:2]) for f in fe_files if f.startswith(("apps/", "packages/"))})
    for proj in projects:
        if (fe / proj / "tsconfig.json").exists() and (fe / proj / "package.json").exists():
            out.append(typecheck(wt, repo, proj))
    # Scoped tests (gauntlet rule): the changed test files plus test files that import a changed
    # module. Not `vitest --changed`: in this repo it walks into.js files with JSX and dies with
    # a parse error unrelated to the diff (verified 2026-09-23).
    apps = sorted({f.split("/")[1] for f in fe_files if f.startswith("apps/")})
    for app in apps:
        app_dir = fe / "apps" / app
        pkg = json.loads((app_dir / "package.json").read_text())
        script = "unit-test" if "unit-test" in pkg.get("scripts", {}) else ("test" if "test" in pkg.get("scripts", {}) else None)
        if not script:
            out.append(layer(f"vitest {app}", "skip", "no unit-test script"))
            continue
        rel = [f[len(f"apps/{app}/"):] for f in fe_files if f.startswith(f"apps/{app}/")]
        tests = {f for f in rel if re.search(r"\.(test|spec)\.[jt]sx?$", f)}
        # generic names (index, utils, types...) matched half the app: one case scoped 367 test files
        generic = {"index", "utils", "types", "constants", "helpers", "hooks", "styles", "config", "schema", "schemas", "api"}
        stems = {Path(f).stem for f in rel if not re.search(r"\.(test|spec)\.", f) and re.search(r"\.[jt]sx?$", f)
                 and Path(f).stem.lower() not in generic}
        if stems:
            g = run(["git", "grep", "-l", "-E", "|".join(re.escape(s) for s in stems), "--", "*.test.ts", "*.test.tsx",
                     "*.spec.ts", "*.spec.tsx"], cwd=app_dir, timeout=60)
            tests |= {l for l in g.stdout.splitlines() if l}
        if not tests:
            out.append(layer(f"vitest {app}", "fail", "no test covers the changed files (new behavior needs a test)"))
            continue
        base_args = ["pnpm", "--filter", pkg["name"], "run", script] + (["--run"] if script == "unit-test" else [])
        try:
            # two workers and one vitest run at a time across all gates: with the default (one worker per CPU) and
            # parallel gates, workers timed out at startup and four cases blocked on "Failed to start forks worker",
            # an environment failure counted as failing tests
            with _vitest_lock():
                r = _tool(base_args + ["--maxWorkers=2"] + sorted(tests)[:40], fe, CFG["quality"]["gate_timeout_s"])
                if WORKER_START.search(r.stdout + r.stderr):
                    log("gate: vitest workers did not start, retrying with one", app=app)
                    r = _tool(base_args + ["--maxWorkers=1"] + sorted(tests)[:40], fe, CFG["quality"]["gate_timeout_s"])
        except GateToolError as e:
            out.append(layer(f"vitest {app}", "error", f"VITEST DID NOT RUN: {e}"))
            continue
        txt = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
        if WORKER_START.search(txt):  # the machine, not the code: CI runs it; no fix round is spent on it
            out.append(layer(f"vitest {app}", "error", "VITEST DID NOT RUN: workers did not start (machine overloaded)"))
            continue
        summary = [l for l in txt.splitlines() if re.search(r"Test Files|Tests\s+\d|FAIL|✗|×", l)][-12:]
        out.append(layer(f"vitest {app} ({len(tests)} scoped files)", "pass" if r.returncode == 0 else "fail",
                         "\n".join(summary) if r.returncode == 0 else _vitest_failures(txt)))
    return out


# ---- python backends ------------------------------------------------------
def _ruff_version(root: Path) -> str | None:
    cfg = root / ".pre-commit-config.yaml"
    if not cfg.exists():
        return None
    m = re.search(r"ruff-pre-commit\s*\n\s*rev:\s*v?([\d.]+)", cfg.read_text())
    return m.group(1) if m else None


def python(wt: Path, repo: str, files: list[str]) -> list[dict]:
    py = [f for f in files if f.endswith(".py")]
    if not py:
        return []
    sub = site.layer_dir(repo, "backend")  # a monorepo keeps its Python under a folder of its own
    root = wt / sub if sub else wt
    rel = [f[len(sub):] for f in py]
    ver = _ruff_version(wt)
    ruff = ["uvx", f"ruff@{ver}" if ver else "ruff"]
    out = []
    r = run([*ruff, "check", *rel], cwd=root, timeout=300)
    out.append(layer("ruff check", "pass" if r.returncode == 0 else "fail", r.stdout[-2500:]))
    r = run([*ruff, "format", "--check", *rel], cwd=root, timeout=300)
    out.append(layer("ruff format", "pass" if r.returncode == 0 else "fail", r.stdout[-1500:]))
    r = run(["python3", "-m", "py_compile", *rel], cwd=root, timeout=120)
    out.append(layer("py_compile", "pass" if r.returncode == 0 else "fail", r.stderr[-1500:]))
    # container tests, CI-equivalent and scoped (ap/dtest.py); api-cometa stays CI-only
    from . import dtest
    try:
        recipe = dtest.recipe(repo)
        if recipe:
            d = recipe[1](wt, files)
            out.append(layer(recipe[0], d["status"], d["detail"]))
        else:
            out.append(layer("container tests", "skip", f"{repo}: CI verifies"))
    except Exception as e:
        out.append(layer("container tests", "fail", f"DID NOT RUN: {type(e).__name__}: {e}"))
    return out


# ---- generic checks: what the repo itself says to run ------------------------------------------------------------
# frontend() and python() above were written for the first install's repos. Any other repo is checked here: the checks
# its config lists (repos.<name>.checks), else what its own files say (a lockfile and its scripts, a Python project,
# go.mod, Cargo.toml, a Makefile test target), run in the case's worktree. A failure is run again on the merge-base:
# a suite that was already red is not this diff's to fix, and a fix session sent after it burns rounds on someone
# else's bug.

# the tools run as CI would run them, and write nothing next to the code: an untracked __pycache__ in a repo that
# does not ignore it would fail the next gate's clean-tree check
CHECK_ENV = {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPYCACHEPREFIX": str(CACHE / "pycache")}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# a changed file that touches an ecosystem's code or its manifests makes that ecosystem's checks relevant
ECOSYSTEM = {
    "node": re.compile(r"\.[mc]?[jt]sx?$|\.(vue|svelte|astro|json|css|scss|less|html)$|(^|/)(\.npmrc|pnpm-lock\.yaml|"
                       r"yarn\.lock|bun\.lockb?)$"),
    "python": re.compile(r"\.pyi?$|(^|/)(pyproject\.toml|setup\.cfg|requirements[^/]*\.txt|uv\.lock|poetry\.lock|"
                         r"pytest\.ini|tox\.ini|\.?ruff\.toml)$"),
    "go": re.compile(r"\.go$|(^|/)go\.(mod|sum)$"),
    "rust": re.compile(r"\.rs$|(^|/)Cargo\.(toml|lock)$"),
}
MANIFESTS = {"node": ("package.json",), "python": ("pyproject.toml", "setup.py", "setup.cfg"), "go": ("go.mod",),
             "rust": ("Cargo.toml",)}
NOT_CODE = re.compile(r"\.(md|mdx|rst|adoc|txt)$|(^|/)(docs?|\.github)/|(^|/)(LICENSE|CHANGELOG|AUTHORS|CODEOWNERS)[^/]*$", re.I)
VENDORED = re.compile(r"(^|/)(node_modules|vendor|third_party|\.venv|venv|target)/")
TOOL_OUTPUT = re.compile(r"(^|/)(node_modules|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache|\.venv|target)/?$")
LOCKS = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"),
         ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"))
TEST_FILE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")
TESTY = re.compile(r"test|spec|pytest|jest|vitest|mocha", re.I)


def _sub(d: str, f: str) -> bool:
    return not d or f.startswith(d + "/")


def _rel(d: str, f: str) -> str:
    return f[len(d) + 1:] if d else f


def _nearest(wt: Path, f: str, names: tuple) -> str | None:
    """The closest folder holding one of `names`, from the file's own folder up to the repo root ('')."""
    parts = f.split("/")[:-1]
    for i in range(len(parts), -1, -1):
        d = "/".join(parts[:i])
        if any((wt / d / n).is_file() for n in names):
            return d
    return None


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _roots(wt: Path, files: list[str] | None, eco: str) -> dict[str, list[str]]:
    """Project roots of one ecosystem -> the changed files under each. With no diff (files None: describing the repo),
    every root its tracked manifests show, three levels deep at most."""
    out: dict[str, list[str]] = {}
    if files is None:
        ls = git(wt, "ls-files", timeout=120).stdout.splitlines()
        for f in ls:
            if f.rsplit("/", 1)[-1] in MANIFESTS[eco] and f.count("/") <= 3 and not VENDORED.search(f):
                out.setdefault(f.rsplit("/", 1)[0] if "/" in f else "", [])
        return dict(sorted(out.items())[:20])
    for f in files:
        if VENDORED.search(f) or not (ECOSYSTEM[eco].search(f) or f.rsplit("/", 1)[-1] in MANIFESTS[eco]):
            continue
        d = _nearest(wt, f, MANIFESTS[eco])
        if d is None and eco == "python":
            d = ""  # loose scripts with no project file still get compiled
        if d is not None:
            out.setdefault(d, []).append(f)
    return out


def _node_checks(wt: Path, d: str) -> list[dict]:
    try:
        pkg = json.loads((wt / d / "package.json").read_text())
    except (OSError, ValueError):
        return []
    top = _nearest(wt, f"{d}/package.json" if d else "package.json", tuple(l for l, _ in LOCKS))
    lock_dir = d if top is None else top
    lock, pm = next(((l, p) for l, p in LOCKS if (wt / lock_dir / l).is_file()), (None, "npm"))
    root_pkg = pkg
    if lock_dir != d:
        try:
            root_pkg = json.loads((wt / lock_dir / "package.json").read_text())
        except (OSError, ValueError):
            root_pkg = {}
    dep_keys = ("dependencies", "devDependencies", "optionalDependencies")
    deps = {k for p in (pkg, root_pkg) for key in dep_keys for k in (p.get(key) or {})}
    setup = None
    if deps or root_pkg.get("workspaces"):
        # frozen: the gate installs what the branch locks, and never rewrites a lockfile in the worktree
        setup = {"npm": "npm ci --no-audit --no-fund" if lock else "npm install --no-audit --no-fund --no-package-lock",
                 "pnpm": "pnpm install --frozen-lockfile",
                 "yarn": "yarn install --immutable" if (wt / lock_dir / ".yarnrc.yml").is_file() else "yarn install --frozen-lockfile",
                 "bun": "bun install --frozen-lockfile"}[pm]
    base = {"dir": d, "setup": setup, "setup_dir": lock_dir, "setup_marker": "node_modules", "source": "detected",
            "setup_inputs": sorted(x for x in {f"{lock_dir}/{lock}".lstrip("/") if lock else "",
                                               f"{lock_dir}/package.json".lstrip("/"), f"{d}/package.json".lstrip("/")} if x)}
    run_s = {"npm": "npm run", "pnpm": "pnpm run", "yarn": "yarn run", "bun": "bun run"}[pm]
    exec_s = {"npm": "npm exec --no --", "pnpm": "pnpm exec", "yarn": "yarn run", "bun": "bun run"}[pm]
    scripts = pkg.get("scripts") or {}
    where = f" ({d}/)" if d else ""
    out = []
    tc = next((s for s in ("typecheck", "type-check", "tsc") if s in scripts), None)
    if tc:
        out.append({**base, "name": f"{run_s} {tc}{where}", "run": f"{run_s} {tc}", "kind": "typecheck"})
    elif (wt / d / "tsconfig.json").is_file() and "typescript" in deps:
        # only when typescript is a dependency: an exec of a tsc that is not installed is a tool error on every run
        out.append({**base, "name": f"tsc --noEmit{where}", "run": f"{exec_s} tsc --noEmit", "kind": "typecheck"})
    if "lint" in scripts:
        out.append({**base, "name": f"{run_s} lint{where}", "run": f"{run_s} lint", "kind": "lint"})
    if "test" in scripts and "no test specified" not in str(scripts["test"]):
        out.append({**base, "name": f"{run_s} test{where}", "run": f"{run_s} test", "kind": "test"})
    return out


def _venv_python(wt: Path, d: str) -> str:
    """The project's own interpreter when it keeps one in the worktree, else python3 from PATH."""
    for cand in (wt / d / ".venv/bin/python", wt / ".venv/bin/python"):
        if cand.exists():
            return str(cand)
    return "python3"


def _ruff_cmd(wt: Path, d: str) -> str:
    ver = _ruff_version(wt)
    if ver:
        return f"uvx ruff@{ver}"  # the version the repo pins in pre-commit: another one flags other rules
    for cand in (wt / d / ".venv/bin/ruff", wt / ".venv/bin/ruff"):
        if cand.exists():
            return shlex.quote(str(cand))
    import shutil
    from .core import env
    return "ruff" if shutil.which("ruff", path=env()["PATH"]) else "uvx ruff"


def _ruff_config(wt: Path, dirs: list[str]) -> tuple[bool, bool]:
    """(the repo configures ruff, it formats with ruff). A repo that never adopted ruff is not held to its rules."""
    pre = _read(wt / ".pre-commit-config.yaml")
    uses, fmt = bool(re.search(r"\bruff\b", pre)), bool(re.search(r"id:\s*ruff-format\b", pre))
    for d in dict.fromkeys(["", *dirs]):
        py = _read(wt / d / "pyproject.toml")
        uses = uses or (wt / d / "ruff.toml").is_file() or (wt / d / ".ruff.toml").is_file() or "[tool.ruff" in py
        fmt = fmt or "[tool.ruff.format" in py
    return uses, fmt


def _unittest_start(wt: Path, d: str) -> str | None:
    """Where `unittest discover` should start for this project: "" for its root, a folder name for a tests folder,
    None when it has no unittest test files."""
    root = wt / d
    def uses(files) -> bool:
        return any("unittest" in _read(f) for f in list(files)[:40])
    for name in ("tests", "test"):
        if (root / name).is_dir() and uses(sorted((root / name).rglob("test*.py"))):
            return name
    return "" if uses(sorted(root.glob("test*.py"))) else None


def _has_pytest(wt: Path, d: str) -> bool:
    root = wt / d
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return True
    if re.search(r"\bpytest\b", _read(root / "pyproject.toml")) or "[tool:pytest]" in _read(root / "setup.cfg") \
            or "[pytest]" in _read(root / "tox.ini"):
        return True
    reqs = list(root.glob("requirements*.txt")) + list(root.glob("requirements/*.txt"))
    return any(re.search(r"^\s*pytest\b", _read(r), re.M) for r in reqs)


def _pytest_scope(wt: Path, d: str, changed: list[str]) -> list[str] | None:
    """Test files for the changed Python files, relative to the project root: the changed tests themselves and the
    tests named after a changed module (test_<name>.py, <name>_test.py). None, the whole suite, when a changed module
    has no such test, or conftest or a package's __init__ changed: what imports them cannot be told cheaply."""
    tests: set[str] = set()
    tracked = None
    for f in changed:
        if not f.endswith(".py"):
            continue
        name = f.rsplit("/", 1)[-1]
        if name in ("conftest.py", "__init__.py"):
            return None
        if TEST_FILE.search(f):
            if (wt / f).is_file():
                tests.add(_rel(d, f))
            continue
        if tracked is None:
            tracked = [t for t in git(wt, "ls-files", "--", d or ".", timeout=120).stdout.splitlines() if TEST_FILE.search(t)]
        stem = name[:-3]
        named = [t for t in tracked if t.rsplit("/", 1)[-1] in (f"test_{stem}.py", f"{stem}_test.py")]
        if not named:
            return None
        tests |= {_rel(d, t) for t in named}
    return sorted(tests) if tests and len(tests) <= 50 else None


def _python_checks(wt: Path, roots: dict[str, list[str]], files: list[str] | None) -> list[dict]:
    py = [f for f in (files or []) if f.endswith(".py") and not VENDORED.search(f)]
    first = next(iter(roots), "")
    interp = _venv_python(wt, first)
    out = []
    if py or files is None:
        out.append({"name": "py_compile (changed files)", "run": f"{shlex.quote(interp)} -m py_compile {{files}}", "dir": "",
                    "files": py, "kind": "compile", "source": "detected"})
        ruff, fmt = _ruff_config(wt, list(roots))
        if ruff:
            cmd = _ruff_cmd(wt, first)
            out.append({"name": "ruff check (changed files)", "run": f"{cmd} check {{files}}", "dir": "", "files": py,
                        "kind": "lint", "source": "detected"})
            if fmt:
                out.append({"name": "ruff format (changed files)", "run": f"{cmd} format --check {{files}}", "dir": "",
                            "files": py, "kind": "lint", "source": "detected"})
    for d, changed in roots.items():
        where = f" ({d}/)" if d else ""
        if not _has_pytest(wt, d):
            # a suite on the standard library's unittest declares nothing: the test files are the declaration (a repo
            # of this kind passed the gate with only py_compile run, its own tests never started)
            start = _unittest_start(wt, d)
            if start is not None:
                out.append({"dir": d, "kind": "test", "source": "detected", "name": f"unittest{where}",
                            "run": f"{shlex.quote(_venv_python(wt, d))} -m unittest discover -q" + (f" -s {shlex.quote(start)} -t ." if start else "")})
            continue
        c = {"dir": d, "kind": "test", "source": "detected"}
        if (wt / d / "uv.lock").is_file():
            run_s = "uv run --frozen pytest -q"  # --frozen: run on the lock as committed, never rewrite it
        elif (wt / d / "poetry.lock").is_file():
            run_s = "poetry run pytest -q"
            c.update(setup="poetry install --no-interaction", setup_dir=d, setup_marker=None,
                     setup_inputs=[f"{d}/poetry.lock".lstrip("/"), f"{d}/pyproject.toml".lstrip("/")])
        else:
            run_s = f"{shlex.quote(_venv_python(wt, d))} -m pytest -q"
        scope = _pytest_scope(wt, d, changed) if files is not None else None
        if scope:
            out.append({**c, "name": f"pytest (tests of the changed files){where}", "run": f"{run_s} {{files}}", "files": scope})
        else:
            out.append({**c, "name": f"pytest{where}", "run": run_s})
    return out


def _configured_checks(repo: str, files: list[str] | None) -> list[dict] | None:
    """The repo's own `checks` list, narrowed to the ones this diff makes relevant; None when the config has none."""
    listed = (CFG["repos"].get(repo) or {}).get("checks")
    if not isinstance(listed, list):
        return None
    out = []
    for c in listed:
        if not isinstance(c, dict) or not str(c.get("run") or "").strip():
            continue
        d = str(c.get("dir") or "").strip().strip("/")
        if d.startswith("/") or ".." in d.split("/"):
            continue  # a check runs inside the worktree, never outside it
        paths = [str(p).strip() for p in (c.get("paths") or []) if str(p).strip()]
        mine = None if files is None else [f for f in files if not paths or any(f.startswith(p.lstrip("/")) for p in paths)]
        if mine is not None and paths and not mine:
            continue
        flist = None
        if "{files}" in c["run"]:
            flist = [] if mine is None else [_rel(d, f) for f in mine if _sub(d, f)]
            if mine is not None and not flist:
                continue
        name = str(c.get("name") or c["run"])[:80]
        out.append({"name": name, "run": str(c["run"]), "dir": d, "files": flist, "setup": c.get("setup") or None,
                    "setup_dir": d, "timeout_s": c.get("timeout_s"), "paths": paths, "source": "config",
                    "kind": "test" if TESTY.search(f"{name} {c['run']}") else "configured"})
    return out


def plan_checks(wt: Path, repo: str, files: list[str] | None) -> list[dict]:
    """What the generic layer runs for this diff (files None: everything it could run in this repo). The repo's own
    `checks` list wins over detection, and an empty list runs none."""
    own = _configured_checks(repo, files)
    if own is not None:
        return own
    out: list[dict] = []
    for d in _roots(wt, files, "node"):
        out += _node_checks(wt, d)
    py_roots = _roots(wt, files, "python")
    if py_roots or (files is not None and any(f.endswith(".py") for f in files)):
        out += _python_checks(wt, py_roots, files)
    for d in _roots(wt, files, "go"):
        where = f" ({d}/)" if d else ""
        out += [{"name": f"go vet{where}", "run": "go vet ./...", "dir": d, "kind": "lint", "source": "detected"},
                {"name": f"go test{where}", "run": "go test ./...", "dir": d, "kind": "test", "source": "detected"}]
    for d in _roots(wt, files, "rust"):
        where = f" ({d}/)" if d else ""
        out += [{"name": f"cargo check{where}", "run": "cargo check", "dir": d, "kind": "compile", "source": "detected"},
                {"name": f"cargo test{where}", "run": "cargo test", "dir": d, "kind": "test", "source": "detected",
                 "after": f"cargo check{where}"}]  # the same compile errors twice say nothing new
    # make test only when nothing else runs a test: it is the one test entry point such a repo has
    if not any(c["kind"] == "test" for c in out) and re.search(r"^test\s*:", _read(wt / "Makefile"), re.M) \
            and (files is None or any(not NOT_CODE.search(f) for f in files)):
        out.append({"name": "make test", "run": "make test", "dir": "", "kind": "test", "source": "detected"})
    return out


def _render(c: dict, files: list[str] | None = None) -> str:
    fl = c.get("files") if files is None else files
    return c["run"].replace("{files}", shlex.join(fl or [])) if "{files}" in c["run"] else c["run"]


def _exec(cmd: str, cwd: Path, timeout: int) -> tuple[int | None, str]:
    """(exit code, output) of one check; None when the tool did not run at all (missing, not executable, timed out)."""
    if not cwd.is_dir():
        return None, f"folder {cwd.name} does not exist"
    try:
        r = run(["/bin/sh", "-c", cmd], cwd=cwd, timeout=timeout, extra_env=CHECK_ENV)
    except subprocess.TimeoutExpired:
        return None, f"`{cmd[:80]}` timed out after {timeout}s"
    except OSError as e:
        return None, f"`{cmd[:80]}` could not start: {e}"
    out = ANSI.sub("", (r.stdout or "") + ("\n" + r.stderr if r.stderr else ""))
    if r.returncode in (126, 127):  # the shell's own codes: the command, or one its script calls, is not installed
        return None, f"`{cmd[:80]}` exited {r.returncode} (a command it needs is not installed): {out.strip()[-400:]}"
    return r.returncode, out


def _judge(c: dict, rc: int | None, out: str) -> tuple[str, str] | None:
    """The layer for a run that needs no comparison (passed, did not run); None for a failure to compare."""
    if rc is None:
        return "fail", f"DID NOT RUN: {out}"
    if re.search(r"\bpytest\b", c["run"]):
        if re.search(r"No module named pytest", out):
            return "fail", f"DID NOT RUN: pytest is not installed for this interpreter: {out.strip()[-300:]}"
        if rc == 5:
            return "skip", "pytest collected no tests"
        if rc in (3, 4):  # internal error, usage error (an addopts plugin that is not installed): pytest never ran a test
            return "fail", f"DID NOT RUN: pytest exited {rc} before running tests: {_brief(out, 800)}"
    if "-m unittest" in c["run"] and (rc == 5 or re.search(r"^Ran 0 tests", out, re.M)):
        return "skip", "unittest found no tests"
    if re.search(r"\bruff\b", c["run"]) and rc == 2:
        return "fail", f"DID NOT RUN: ruff could not run (exit 2): {_brief(out, 800)}"
    if rc == 0:
        return "pass", _passline(out)
    return None


MARKER = re.compile(r"^\s*(?:(?:FAIL|FAILED|ERROR|not ok)\b|[✖×✕]|--- FAIL)|^E\s{2,}|\b(Error|error|panicked at|assert\w*|"
                    r"Expected|Received|expected)\b|^\s*--> ")


def _brief(out: str, limit: int = 2500) -> str:
    """What a fix session needs from a failing run: the lines that name failures and their messages, then the tail
    with the runner's summary. A raw tail is often a stack trace with every test name cut off."""
    # stack frames inside the runtime or the dependencies never point at the fix
    lines = [l.rstrip() for l in ANSI.sub("", out).splitlines()
             if l.strip() and not re.match(r"\s+at .*(\(node:|\(internal/|node_modules/)", l)]
    keep = "\n".join(dict.fromkeys(l.strip()[:300] for l in lines if MARKER.search(l) and "node_modules" not in l))
    keep = keep[:int(limit * 0.6)]
    tail = "\n".join(l[:300] for l in lines[-15:])
    return ((keep + "\n…\n") if keep else "") + tail[-(limit - len(keep) - 3):]


def _passline(out: str) -> str:
    lines = [l.strip() for l in ANSI.sub("", out).splitlines() if l.strip()]
    summary = [l for l in lines if re.search(r"\b\d+ (passed|tests?|checks?)\b|^ℹ (tests|pass|fail) \d|^ok\s|test result:|"
                                             r"All checks passed|^PASS\b|\d+ files? (already formatted|left unchanged)", l)]
    return "\n".join((summary or lines)[-4:])[:400]


PARSERS = [
    ("test names", re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", re.M)),  # pytest's short summary
    ("test names", re.compile(r"^(?:FAIL|ERROR): (\S+ \(\S+\))", re.M)),  # unittest
    ("test names", re.compile(r"^\s*--- FAIL: (\S+)", re.M)),  # go test
    ("test names", re.compile(r"^test (\S+) \.\.\. FAILED", re.M)),  # cargo test
    ("test names", re.compile(r"^\s*(?:not ok \d+ - (.+?)|✖ (?!failing tests)(.+?)(?: \([\d.]+m?s\))?)\s*$", re.M)),  # node --test
    ("test names", re.compile(r"^\s*(?:[×✕] (.+?)(?: \(?\d+ ?m?s\)?)?|FAIL\s+(\S.*?))\s*$", re.M)),  # jest, vitest
    ("compiler errors", re.compile(r"^(\S+?)\(\d+,\d+\): error (TS\d+: .{0,160})", re.M)),  # tsc
    ("compiler errors", re.compile(r"^error(?:\[E\d+\])?: (.+)\n\s*--> ([^:\n]+):\d+:\d+", re.M)),  # rustc
    ("lint findings", re.compile(r"^\s*\d+:\d+\s+error\s+(.+?)\s{2,}(\S+)\s*$", re.M)),  # eslint stylish (rule, message)
    ("lint findings", re.compile(r"^(\S+?):\d+:\d+:? (.+)$", re.M)),  # ruff, go vet, most file:line:col tools
]


def _unrooted(out: str, roots: list[Path]) -> str:
    """The output with the worktree's own path taken out, as written and resolved (a tool may print either: /var and
    /private/var on macOS)."""
    txt = ANSI.sub("", out)
    for r in roots:
        for p in sorted({str(r), str(r.resolve())}, key=len, reverse=True):
            txt = txt.replace(p + "/", "").replace(p, "")
    return txt


def _failures(out: str, roots: list[Path]) -> tuple[str, set[str]]:
    """(what was parsed, the failure names) from a run's output, with no line numbers or worktree paths in them so a
    branch and its base compare. ('', empty) when no known shape is in the output."""
    txt = _unrooted(out, roots)
    for kind, rx in PARSERS:
        found = {"|".join(g.strip() for g in m.groups() if g) for m in rx.finditer(txt)}
        found.discard("")
        if found:
            return kind, found
    return "", set()


def _norm(out: str, roots: list[Path]) -> str:
    """The run's last lines without what changes between two runs of the same failure: paths, numbers, timings."""
    txt = _unrooted(out, roots)
    lines = [re.sub(r"\s+", " ", re.sub(r"\d+(\.\d+)?", "0", l)).strip() for l in txt.splitlines() if l.strip()]
    return "\n".join(lines[-15:])


class _CheckRun:
    """One gate run of the generic checks: the setups it did, and the base worktree a failure is compared on."""

    def __init__(self, wt: Path, repo: str, files: list[str]):
        self.wt, self.repo, self.files = wt, repo, files
        self.setups: dict = {}
        self._sha: str | None = None
        self.base: Path | None = None
        self.base_err = ""

    def timeout(self, c: dict) -> int:
        try:
            return int(c.get("timeout_s") or CFG["quality"].get("check_timeout_s") or 1200)
        except (TypeError, ValueError):
            return 1200

    def setup(self, root: Path, c: dict, persist: bool) -> str | None:
        """Run the check's setup (an install) once per folder and run; the error text when it failed. In the case's
        worktree an install is skipped while its lockfile and manifests are what it last installed."""
        cmd = c.get("setup")
        if not cmd:
            return None
        cwd = root / (c.get("setup_dir") or "")
        key = (str(cwd), cmd)
        if key in self.setups:
            return self.setups[key]
        inputs = c.get("setup_inputs") or []
        want = hashlib.sha1(b"\0".join((root / f).read_bytes() if (root / f).is_file() else b"" for f in inputs)).hexdigest() \
            if inputs else ""
        stamp = CACHE / "setup" / hashlib.sha1(f"{cwd}\0{cmd}".encode()).hexdigest()[:16]
        have = not c.get("setup_marker") or (cwd / c["setup_marker"]).exists()
        if persist and want and have and stamp.is_file() and stamp.read_text() == want:
            err = None
        else:
            rc, out = _exec(cmd, cwd, max(self.timeout(c), 1500))
            err = None if rc == 0 else (out if rc is None else f"exit {rc}: {_brief(out, 1200)}")
            if err is None and persist and want:
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.write_text(want)
        self.setups[key] = err
        return err

    def sha(self) -> str:
        if self._sha is None:
            d = CFG["repos"][self.repo]["default"]
            self._sha = git(self.wt, "merge-base", "HEAD", f"origin/{d}").stdout.strip()
        return self._sha

    def base_wt(self) -> Path | None:
        if self.base is None and not self.base_err:
            if not self.sha():
                self.base_err = "no merge-base with the default branch"
                return None
            path = CACHE / "base-checks" / f"{re.sub(r'[^\w.-]', '_', self.repo)}-{self.sha()[:10]}-{os.getpid()}-{time.time_ns() % 10**8}"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._sweep(path.parent)
            r = git(self.wt, "worktree", "add", "--detach", "--quiet", str(path), self.sha())
            if r.returncode != 0:
                self.base_err = f"could not check out the base: {(r.stderr or r.stdout).strip()[-300:]}"
                return None
            self.base = path
        return self.base

    def _sweep(self, parent: Path) -> None:
        """Base checkouts a killed gate left behind (close() never ran): gone after a day, with their worktree entry."""
        import shutil
        old = [p for p in parent.iterdir() if p.is_dir() and time.time() - p.stat().st_mtime > 86400]
        for p in old:
            shutil.rmtree(p, ignore_errors=True)
        if old:
            git(self.wt, "worktree", "prune")

    def close(self) -> None:
        if self.base:
            git(self.wt, "worktree", "remove", "--force", str(self.base))
            if self.base.exists():
                import shutil
                shutil.rmtree(self.base, ignore_errors=True)
                git(self.wt, "worktree", "prune")
            self.base = None

    def on_base(self, c: dict) -> dict:
        """The same check on the merge-base, cached per (repo, base sha, check): fix rounds reuse it."""
        if not self.sha():
            return {"did_not_run": "no merge-base with the default branch"}
        base_files = None
        if c.get("files") is not None:
            # a file the branch adds does not exist on the base: there it checks what does
            base_files = [f for f in c["files"] if git(self.wt, "cat-file", "-e", f"{self.sha()}:{(c['dir'] + '/' if c['dir'] else '') + f}").returncode == 0]
        ident = {"name": c["name"], "run": c["run"].replace(str(self.wt), "<wt>"), "dir": c["dir"], "files": base_files,
                 "setup": c.get("setup")}
        key = f"gate_base:{self.repo}:{self.sha()}:{hashlib.sha1(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:12]}"
        from . import db
        hit = db.kv_get(key)
        if hit:
            return {**hit, "cached": True}
        if base_files == []:
            res = {"rc": 0, "ok": True, "nothing": True}
            db.kv_set(key, res)
            return res
        base = self.base_wt()
        if not base:
            return {"did_not_run": self.base_err}
        err = self.setup(base, c, persist=False)
        if err:
            return {"did_not_run": f"setup `{c['setup']}` failed on the base: {err[:300]}"}
        rc, out = _exec(_render(c, base_files), base / c["dir"], self.timeout(c))
        j = _judge(c, rc, out)
        if j and "DID NOT RUN" in j[1]:
            # not cached: a tool missing for a moment says nothing about the base. Said without the marker, which
            # would turn the branch's real failure into an environment error
            return {"did_not_run": j[1].replace("DID NOT RUN: ", "")[:400]}
        parser, ids = _failures(out, [base])
        res = {"rc": rc, "ok": j is not None, "parser": parser, "ids": sorted(ids)[:500], "norm": _norm(out, [base]),
               "head": _brief(out, 700)}
        db.kv_set(key, res)
        return res

    def check(self, c: dict) -> list[dict]:
        out = []
        err = self.setup(self.wt, c, persist=True)
        if err:
            where = f" ({c['setup_dir']}/)" if c.get("setup_dir") else ""
            mine = [f for f in self.files if f in (c.get("setup_inputs") or [])]
            # the diff changed what is installed: its own fault; otherwise the machine (network, registry, tool)
            out.append(layer(f"setup {c['setup']}{where}", "fail",
                             f"the install fails with this diff's changes to {', '.join(mine)}:\n{err}" if mine else
                             f"DID NOT RUN: {err}"))
            return out + [layer(c["name"], "skip", f"not run: `{c['setup']}` failed")]
        t0 = time.time()
        rc, txt = _exec(_render(c), self.wt / c["dir"], self.timeout(c))
        log("gate check", repo=self.repo, check=c["name"], rc=rc, secs=round(time.time() - t0))
        j = _judge(c, rc, txt)
        if j:
            return out + [layer(c["name"], *j)]
        base = self.on_base(c)
        cached = " (base result cached)" if base.get("cached") else ""
        sha = (self.sha() or "")[:10]
        if base.get("did_not_run"):
            return out + [layer(c["name"], "fail", f"exit {rc}. The base could not be checked ({base['did_not_run'][:200]}), "
                                                   f"so this counts as the diff's failure.\n{_brief(txt)}")]
        if base.get("ok"):
            return out + [layer(c["name"], "fail", f"exit {rc}; passes on the base ({sha}){cached}.\n{_brief(txt)}")]
        parser, ids = _failures(txt, [self.wt])
        if parser and parser == base.get("parser") and base.get("ids"):
            new = sorted(ids - set(base["ids"]))
            how = f"compared by failing {parser} ({len(ids)} here, {len(base['ids'])} on the base, {len(new)} new)"
            if new:
                # names first and capped: layer() keeps a detail's last 3000 characters, so a long one loses its head
                names = "\n".join(f"- {n[:200]}" for n in new[:25])[:900]
                return out + [layer(c["name"], "fail", f"exit {rc}; new failures vs the base ({sha}), {how}{cached}:\n"
                                                       f"{names}\n\n{_brief(txt, 1700)}")]
        else:
            same = rc == base.get("rc") and _norm(txt, [self.wt]) == base.get("norm")
            how = "compared by exit status and normalized output tail" + ("" if same else " (they differ)")
            if not same:
                return out + [layer(c["name"], "fail", f"exit {rc}; the base ({sha}) fails too but differently, {how}{cached}.\n"
                                                       f"{_brief(txt)}")]
        return out + [layer(c["name"], "skip", f"fails on the base too ({sha}), {how}{cached}: not this diff's failure.\n"
                                               f"Base:\n{base.get('head', '')[:700]}")]


def checks(wt: Path, repo: str, files: list[str]) -> list[dict]:
    """The generic layer: one gate layer per check that applies to the diff, or one plain line saying nothing ran."""
    try:
        plan = plan_checks(wt, repo, files)
    except Exception as e:
        return [layer("checks", "fail", f"DID NOT RUN: could not work out the repo's checks: {type(e).__name__}: {e}")]
    if not plan:
        listed = (CFG["repos"].get(repo) or {}).get("checks")
        why = ("this repo's config lists no checks (repos." + repo + ".checks is empty)" if listed == [] else
               "none of the checks in this repo's config applies to the changed files" if isinstance(listed, list) else
               "found no package.json, Python files, go.mod, Cargo.toml or Makefile test target for the changed files")
        return [layer("checks", "skip", f"Nothing was run: {why}.")]
    r = _CheckRun(wt, repo, files)
    out: list[dict] = []
    failed: set[str] = set()
    try:
        for c in plan:
            if c.get("after") in failed:
                out.append(layer(c["name"], "skip", f"not run: {c['after']} failed"))
                continue
            for l in r.check(c):
                if l["name"] in {x["name"] for x in out}:
                    continue  # one failed install, reported once for every check that needed it
                out.append(l)
                if l["status"] == "fail":
                    failed.add(l["name"])
    finally:
        r.close()
    return out


def how_tests_run(repo: str, wt: Path) -> list[dict]:
    """How this repo runs its tests (configured, else detected), for the reproduce stage: [{name, run, dir}]."""
    return [{"name": c["name"], "run": c["run"], "dir": c.get("dir") or ""} for c in plan_checks(wt, repo, None)
            if c.get("kind") == "test"]


# ---- CodeRabbit CLI: the prepush-review contract, enforced -----------------------------------------------------
# ~/.claude/skills/prepush-review: one completed pass on the exact HEAD being pushed, fresh base, every major finding
# fixed or defended. Before 2026-10-01 the harness ran it but treated it as advisory: a failed pass (3 of 26 cases)
# still pushed, fixes on his PRs (CI, review comments) never ran it, and nothing checked the majors were handled.
# Now a completed pass writes <gitdir>/cr-pass with its HEAD, and outbound.push refuses a HEAD without one.
import json as _json


def _cr_parse(out: str) -> tuple[bool, list[dict], str]:
    completed, findings, err = False, [], ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = _json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "finding":
            findings.append({"severity": (ev.get("severity") or "").lower(), "file": ev.get("fileName") or "",
                             "line": ev.get("lineNumber") or ev.get("startLine") or "",
                             "text": (ev.get("comment") or ev.get("title") or ev.get("description") or ev.get("codegenInstructions") or "")[:1500]})
        elif ev.get("type") == "complete":
            completed = ev.get("outcome") == "completed" or ev.get("status") == "review_completed"
        elif ev.get("type") == "error":
            err = f"{ev.get('errorType')}: {ev.get('message')}"[:300]
    return completed, findings, err


def coderabbit(wt: Path, repo: str, base_commit: str | None = None, attempts: int = 3) -> dict:
    """One pass, retried on a connection/limit failure. base_commit: a fix on an existing PR reviews only its own
    commits (fix X means X only)."""
    d = CFG["repos"][repo]["default"]
    if not CFG.get("quality", {}).get("coderabbit_gate", True):
        # an install that does not use the review bot (quality.coderabbit_gate off) has no pass to wait for: the self
        # review ran the CLI anyway, and without the CLI on the machine the pass never completed and the case blocked
        return {"rc": 0, "output": "no review bot on this install: the self review reads the diff alone", "completed": True,
                "findings": [], "error": "", "head": git(wt, "rev-parse", "HEAD").stdout.strip()}
    git(wt, "fetch", "-q", "origin", d)  # a stale base hides what the GitHub app will see
    head = git(wt, "rev-parse", "HEAD").stdout.strip()
    if base_commit and git(wt, "diff", "--quiet", base_commit, "HEAD").returncode == 0:
        # the fix changed nothing (a finding answered with steps, not code): CodeRabbit answers 'review_skipped', which
        # never completes, so #632 blocked after 6 passes on its own unchanged PR head
        return {"rc": 0, "output": "nothing to review: HEAD has no changes since the fix started", "completed": True,
                "findings": [], "error": "", "head": head}
    mb = git(wt, "merge-base", "HEAD", f"origin/{d}").stdout.strip()
    if base_commit and mb and git(wt, "merge-base", "--is-ancestor", mb, base_commit).returncode != 0:
        # the fix merged the base in (a conflict fix): base_commit..HEAD carries every base commit since the PR's
        # fork, so #609's 8-file PR went to CodeRabbit as 1343 files ('Too many files', 6 passes, blocked). The PR's net
        # diff is what the GitHub app reviews; hygiene already reads it the same way (added_lines)
        base_commit = None
    cfgs = [c for c in ("CLAUDE.md", ".coderabbit.yaml") if (wt / c).exists()]
    cmd = ["coderabbit", "review", "--agent", "--committed"] + (["--base-commit", base_commit] if base_commit else ["--base", f"origin/{d}"])
    if cfgs:
        cmd += ["-c", *cfgs]
    out, completed, findings, err, rc = "", False, [], "", 1
    for i in range(attempts):
        try:
            r = _tool(cmd, wt, 1800)
        except GateToolError as e:
            r = subprocess.CompletedProcess(cmd, 1, "", str(e))
        rc = r.returncode
        out = (r.stdout + ("\n" + r.stderr if r.returncode else ""))[-20000:]
        completed, findings, err = _cr_parse(r.stdout)
        if completed:
            break
        log("coderabbit pass did not complete, retrying", repo=repo, attempt=i + 1, err=err or out[-200:])
        time.sleep(60 * (i + 1))
    if completed:
        gitdir = git(wt, "rev-parse", "--absolute-git-dir").stdout.strip()
        Path(gitdir, "cr-pass").write_text(head + "\n" + _json.dumps(findings, ensure_ascii=False))
    return {"rc": 0 if completed else (rc or 1), "output": out, "completed": completed, "findings": findings,
            "error": err, "head": head}


def cr_pass_head(wt: Path) -> str:
    gitdir = git(wt, "rev-parse", "--absolute-git-dir").stdout.strip()
    f = Path(gitdir, "cr-pass")
    return f.read_text().split("\n", 1)[0].strip() if f.exists() else ""


def majors(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] in ("critical", "major", "high")]


def run_all(repo: str, wt: Path, scope: list[str] | None = None, published: bool = False,
            since: str | None = None) -> dict:
    """since: for fixes on an existing PR, the head it started from; hygiene then covers only the
    harness's own commits (pre-existing lines are not its scope: "fix X means X only")."""
    dirty = []
    for l in git(wt, "status", "--porcelain").stdout.splitlines():
        path = l[3:].strip()
        name = path.rsplit("/", 1)[-1]
        if path.endswith("AP_LEDGER.json") or name.startswith(".ap-") or name == "AP_REPRO.json":
            if l.startswith("??") and name != "AP_LEDGER.json":
                # a session's scratch file (.ap-env.test,.ap-src-fix.patch) or the reproduce session's record, already
                # stored in the case, is not the fix's business (13 fix rounds went to deleting AP_REPRO.json, #916, #1216)
                try:
                    (wt / path).unlink()
                except Exception:
                    pass
            continue
        if l.startswith("??") and not site.internal() and TOOL_OUTPUT.search(path):
            continue  # what the checks' own installs and runs leave in a repo that does not ignore it
        dirty.append(l)
    if dirty:
        return {"passed": False, "head": "", "layers": [layer("committed tree", "fail",
                "uncommitted changes (commit or discard them):\n" + "\n".join(dirty[:20]))]}
    ledger = [freshness(wt, repo, published)]
    if ledger[0]["status"] == "fail":
        return {"passed": False, "layers": ledger, "head": git(wt, "rev-parse", "HEAD").stdout.strip()}
    files = changed_files(wt, repo)
    if not files:
        ledger.append(layer("non-empty diff", "fail", "no changes vs base"))
        return {"passed": False, "layers": ledger, "head": ""}
    ledger += hygiene(wt, repo, files, scope, since)
    if site.internal():
        # the first install keeps its own layers, built for its repos; a `checks` list in a repo's config adds to them
        ledger += frontend(wt, repo, files)
        ledger += python(wt, repo, files)
        if (CFG["repos"].get(repo) or {}).get("checks"):
            ledger += checks(wt, repo, files)
    else:
        ledger += checks(wt, repo, files)
    for l in ledger:  # a tool that did not run is an environment error, not a code failure
        if l["status"] == "fail" and ("DID NOT RUN" in l["detail"] or "no test covers" in l["detail"] and published):
            l["status"] = "error"
    _not_run_streaks(ledger)
    passed = all(l["status"] not in ("fail", "error") for l in ledger)
    head = git(wt, "rev-parse", "HEAD").stdout.strip()
    log("gates", repo=repo, wt=wt.name, passed=passed,
        layers={l["name"]: l["status"] for l in ledger})
    return {"passed": passed, "layers": ledger, "head": head, "files": files,
            "sig": hashlib.sha1(json.dumps(ledger).encode()).hexdigest()[:10]}


NOT_RUN_STREAK = 3


def _not_run_streaks(ledger: list[dict]) -> None:
    """A layer whose tool did not run passes on as an env error ('CI will verify'): right for a hiccup, wrong as a
    steady state. From 09-23 to 10-06 the container tests never ran once (the docker plugins were not found) and
    every schools case shipped on that line with nobody told. The same layer not running on NOT_RUN_STREAK gate runs
    in a row files an incident; a run of it resets the count. Keyed by the tool, not the layer's full name: a run is
    'tsc apps/dashboard (new errors vs base)' and a non-run 'tsc apps/dashboard', so passes never reset the count."""
    from . import db, incidents
    for l in ledger:
        key = f"gate_not_run:{l['name'].split(' (')[0]}"
        if l["status"] == "pass":
            db.kv_set(key, 0)
        elif l["status"] == "error" and "DID NOT RUN" in l["detail"]:
            n = (db.kv_get(key, 0) or 0) + 1
            db.kv_set(key, n)
            if n >= NOT_RUN_STREAK:
                try:
                    incidents.file("gate_not_run", l["name"], site.t(
                        f"Gate '{l['name']}' does not run: {n} times in a row it passed as 'CI will verify'",
                        f"Gate '{l['name']}' no corre: {n} veces seguidas pasó como 'CI will verify'"), l["detail"], impact=9)
                except Exception as e:
                    log("gates: incident failed", err=str(e)[:120])


def ledger_text(res: dict) -> str:
    return "\n".join(f"- {l['name']}: {l['status']}" + (f" ({l['detail'].splitlines()[0][:120]})" if l['status'] != 'pass' and l['detail'] else "")
                     for l in res["layers"])


if __name__ == "__main__":  # bin/ap-tsc <app or packages/x> <worktree>: the gate's tsc layer for a code session
    import sys
    proj = sys.argv[1] if "/" in sys.argv[1] else f"apps/{sys.argv[1]}"
    wt = Path(sys.argv[2]).resolve()
    err = _ensure_frontend_deps(wt / "frontend")
    res = layer("frontend deps", "fail", err) if err else typecheck(wt, site.repo_with("frontend"), proj)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    sys.exit(0 if res["status"] == "pass" else 1)
