"""The generic gate layer (ap/gates.py checks): a release install verifies any repo with what the repo itself says to
run, and a failure that was already on the base is not the diff's.

Every case is a scratch origin and a clone of it in tmp_path, gated by the real gates.run_all in a subprocess whose
CLEA_HOME holds a release config naming that clone (config is read at import)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# no hooks: a machine's global hooks run tools in the scratch repo (one here runs `pnpm lefthook`, which writes a
# pnpm-lock.yaml into a folder with a package.json)
ID = ["-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null"]


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *ID, *a], check=True, capture_output=True, text=True).stdout


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.endswith(".sh") or rel.endswith("bin/python"):
            p.chmod(0o755)


def repo(tmp: Path, name: str, base: dict, branch: dict) -> Path:
    """origin with `base` committed on main, and a clone on a case branch with `branch` committed on top."""
    origin = tmp / f"{name}-origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _write(origin, base)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")
    work = tmp / f"{name}-work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "checkout", "-qb", "case")
    if branch:  # none: a case branch with nothing on it yet, as a reproduce session finds it
        _write(work, branch)
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "change")
    return work


CALL = """
import json, sys
from pathlib import Path
from ap import gates, db
repo, wt, what = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
if what == "plan":
    out = gates.plan_checks(wt, repo, gates.changed_files(wt, repo))
elif what == "describe":
    from ap import setup
    out = setup.repo_checks(repo)
else:
    out = gates.run_all(repo, wt)
    out["base_cache"] = sorted(r["k"] for r in db.q("SELECT k FROM kv WHERE k LIKE 'gate_base:%'"))
print("@@" + json.dumps(out))
"""


def gate(tmp: Path, name: str, wt: Path, cfg: dict | None = None, edition: str = "release", what: str = "run") -> dict:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    own = {"path": str(tmp / f"{name}-origin"), "default": "main", "layer": "backend", **(cfg or {})}
    (home / "config.json").write_text(json.dumps({"edition": edition, "user": {"name": "Ana Lopez"}, "github_org": "acme",
                                                  "repos": {name: own}}))
    env = {**os.environ, "CLEA_HOME": str(home), "CLEA_CODE": str(ROOT), "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, "-c", CALL, name, str(wt), what], env=env, cwd=tmp, capture_output=True,
                       text=True, timeout=900)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.split("@@", 1)[1])


class Layers(dict):
    def __missing__(self, key):
        raise AssertionError(f"no layer {key!r} in {json.dumps(list(self.values()), indent=1)[:3000]}")


def by_name(res: dict) -> dict:
    return Layers({l["name"]: l for l in res["layers"]})


def need(tool: str) -> None:
    if not shutil.which(tool, path="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")):
        pytest.skip(f"{tool} is not installed on this machine")


# ---- node: `node --test` through the package's test script, no dependencies to install -----------------------------
PKG = json.dumps({"name": "calc", "version": "1.0.0", "scripts": {"test": "node --test"}})
LIB = "exports.add = (a, b) => a + b;\nexports.mul = (a, b) => a * b;\n"
ADD_TEST = ("const test = require('node:test');\nconst assert = require('node:assert');\nconst { add } = require('../lib');\n"
            "test('adds two numbers', () => { assert.strictEqual(add(1, 2), 3); });\n")
MUL_TEST = ("const test = require('node:test');\nconst assert = require('node:assert');\nconst { mul } = require('../lib');\n"
            "test('multiplies two numbers', () => { assert.strictEqual(mul(2, 3), 6); });\n")


def test_node_branch_that_breaks_a_test_fails(tmp_path):
    need("node")
    wt = repo(tmp_path, "calc", {"package.json": PKG, "lib.js": LIB, "test/add.test.js": ADD_TEST},
              {"lib.js": LIB.replace("a + b", "a - b")})
    res = gate(tmp_path, "calc", wt)
    t = by_name(res)["npm run test"]
    assert not res["passed"] and t["status"] == "fail", t
    assert "passes on the base" in t["detail"] and "adds two numbers" in t["detail"], t["detail"]


def test_node_failure_already_on_the_base_is_not_the_diffs(tmp_path):
    need("node")
    broken = LIB.replace("a * b", "a + b")  # mul is wrong on main already
    wt = repo(tmp_path, "calc", {"package.json": PKG, "lib.js": broken, "test/add.test.js": ADD_TEST,
                                 "test/mul.test.js": MUL_TEST},
              {"lib.js": broken + "exports.sub = (a, b) => a - b;\n"})
    res = gate(tmp_path, "calc", wt)
    t = by_name(res)["npm run test"]
    assert res["passed"] and t["status"] == "skip", t
    assert "fails on the base too" in t["detail"] and "compared by failing test names (1 here, 1 on the base, 0 new)" in t["detail"], t["detail"]
    assert "multiplies two numbers" in t["detail"]  # the base's own failure, quoted
    assert len(res["base_cache"]) == 1
    again = by_name(gate(tmp_path, "calc", wt))["npm run test"]  # a fix round: the base is not run again
    assert again["status"] == "skip" and "(base result cached)" in again["detail"], again["detail"]


def test_node_one_new_failure_beside_an_old_one_fails(tmp_path):
    need("node")
    broken = LIB.replace("a * b", "a + b")
    wt = repo(tmp_path, "calc", {"package.json": PKG, "lib.js": broken, "test/add.test.js": ADD_TEST,
                                 "test/mul.test.js": MUL_TEST},
              {"lib.js": broken.replace("a + b;\nexports.mul", "a - b;\nexports.mul")})
    t = by_name(gate(tmp_path, "calc", wt))["npm run test"]
    assert t["status"] == "fail", t
    assert "(2 here, 1 on the base, 1 new)" in t["detail"] and "- adds two numbers" in t["detail"], t["detail"]


# ---- python: pytest from the project's own.venv, scoped to the changed test file -------------------------------------
def test_python_branch_that_adds_a_failing_test_fails(tmp_path):
    # the project's interpreter: a launcher for the python running this suite, which has pytest
    venv = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    base = {"pyproject.toml": '[project]\nname = "calc"\nversion = "0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            ".gitignore": ".venv/\n", "calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"}
    wt = repo(tmp_path, "pycalc", base, {"tests/test_more.py": "from calc import add\n\n\ndef test_add_more():\n    assert add(2, 2) == 5\n"})
    _write(wt, {".venv/bin/python": venv})
    res = gate(tmp_path, "pycalc", wt)
    layers = by_name(res)
    assert layers["py_compile (changed files)"]["status"] == "pass"
    t = layers["pytest (tests of the changed files)"]
    assert not res["passed"] and t["status"] == "fail", t
    assert "test_add_more" in t["detail"] and "passes on the base" in t["detail"], t["detail"]
    assert "ruff check (changed files)" not in layers  # the repo never adopted ruff
    assert not list(wt.rglob("__pycache__")), "the gate left bytecode in the worktree"


# ---- go ----------------------------------------------------------------------------------------------------------------
def test_go_branch_that_breaks_a_test_fails(tmp_path):
    need("go")
    base = {"go.mod": "module example.com/calc\n\ngo 1.21\n", "calc.go": "package calc\n\nfunc Add(a, b int) int { return a + b }\n",
            "calc_test.go": 'package calc\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal("bad")\n\t}\n}\n'}
    wt = repo(tmp_path, "gocalc", base, {"calc.go": "package calc\n\nfunc Add(a, b int) int { return a - b }\n"})
    layers = by_name(gate(tmp_path, "gocalc", wt))
    assert layers["go vet"]["status"] == "pass", layers["go vet"]
    assert layers["go test"]["status"] == "fail" and "TestAdd" in layers["go test"]["detail"], layers["go test"]


# ---- configured checks -----------------------------------------------------------------------------------------------
CONFIGURED = {"checks": [
    {"name": "unit", "run": "sh ./check.sh", "paths": ["src/"]},
    {"name": "docs lint", "run": "false", "paths": ["docs/"]},  # the diff does not touch docs/: never run
    {"name": "changed files", "run": "ls {files}", "dir": "src"},
]}


def test_configured_checks_run_instead_of_detection(tmp_path):
    base = {"check.sh": "test \"$(cat src/value.txt)\" = ok\n", "src/value.txt": "ok\n", "docs/a.md": "a\n",
            "package.json": PKG}  # detection would run npm; the configured list wins
    wt = repo(tmp_path, "conf", base, {"src/value.txt": "broken\n"})
    res = gate(tmp_path, "conf", wt, CONFIGURED)
    layers = by_name(res)
    assert set(layers) >= {"unit", "changed files"} and "docs lint" not in layers and "npm run test" not in layers, layers
    assert layers["unit"]["status"] == "fail" and "passes on the base" in layers["unit"]["detail"]
    assert layers["changed files"]["status"] == "pass" and "value.txt" in layers["changed files"]["detail"]


def test_configured_checks_add_to_the_first_installs_layers(tmp_path):
    wt = repo(tmp_path, "conf", {"check.sh": "exit 0\n", "src/value.txt": "ok\n"}, {"src/value.txt": "fine\n"})
    layers = by_name(gate(tmp_path, "conf", wt, CONFIGURED, edition="internal"))
    assert layers["unit"]["status"] == "pass" and "docs lint" not in layers, layers


def test_an_empty_checks_list_runs_nothing(tmp_path):
    wt = repo(tmp_path, "conf", {"package.json": PKG, "lib.js": LIB}, {"lib.js": LIB + "\n"})
    res = gate(tmp_path, "conf", wt, {"checks": []})
    assert res["passed"] and by_name(res)["checks"]["detail"] == "Nothing was run: this repo's config lists no checks (repos.conf.checks is empty)."


def test_a_missing_tool_is_an_environment_error(tmp_path):
    wt = repo(tmp_path, "conf", {"src/a.txt": "a\n"}, {"src/a.txt": "b\n"})
    res = gate(tmp_path, "conf", wt, {"checks": [{"name": "lint", "run": "no-such-linter-on-this-machine --check"}]})
    lint = by_name(res)["lint"]
    assert lint["status"] == "error" and lint["detail"].startswith("DID NOT RUN:"), lint
    assert not res["passed"]


def test_a_repo_with_nothing_to_run_says_so(tmp_path):
    wt = repo(tmp_path, "plain", {"README.md": "hi\n", "data.csv": "a,b\n"}, {"data.csv": "a,b\n1,2\n"})
    res = gate(tmp_path, "plain", wt)
    assert res["passed"], res
    assert by_name(res)["checks"] == {"name": "checks", "status": "skip", "detail": (
        "Nothing was run: found no package.json, Python files, go.mod, Cargo.toml or Makefile test target for the changed files.")}


# ---- detection, without running anything -----------------------------------------------------------------------------
def test_detection_table(tmp_path):
    base = {"web/package.json": json.dumps({"name": "web", "scripts": {"lint": "eslint .", "test": "echo \"Error: no test specified\" && exit 1"},
                                            "devDependencies": {"typescript": "5.0.0"}}),
            "web/pnpm-lock.yaml": "lockfileVersion: '9.0'\n", "web/tsconfig.json": "{}\n", "web/src/a.ts": "export const a = 1\n",
            "api/pyproject.toml": "[tool.ruff]\nline-length = 100\n[tool.ruff.format]\n[dependency-groups]\ndev = [\"pytest\"]\n",
            "api/uv.lock": "version = 1\n", "api/app/calc.py": "x = 1\n", "api/tests/test_calc.py": "def test_x():\n    pass\n",
            "Makefile": "test:\n\techo hi\n", "README.md": "x\n"}
    wt = repo(tmp_path, "mono", base, {"web/src/a.ts": "export const a = 2\n", "api/app/calc.py": "x = 2\n"})
    plan = {c["name"]: c for c in gate(tmp_path, "mono", wt, what="plan")}
    assert plan["tsc --noEmit (web/)"]["run"] == "pnpm exec tsc --noEmit"
    assert plan["pnpm run lint (web/)"]["setup"] == "pnpm install --frozen-lockfile"
    assert "pnpm run test (web/)" not in plan  # npm's placeholder test script
    assert plan["pytest (tests of the changed files) (api/)"]["run"] == "uv run --frozen pytest -q {files}"
    assert plan["pytest (tests of the changed files) (api/)"]["files"] == ["tests/test_calc.py"]
    assert {"py_compile (changed files)", "ruff check (changed files)", "ruff format (changed files)"} <= set(plan)
    assert "make test" not in plan  # a test runner was detected: the Makefile is not the entry point
    # the settings screen's view of the same repo, read from its checkout (the origin, on main): every workspace folder
    shown = gate(tmp_path, "mono", wt, what="describe")
    assert shown["source"] == "detected" and shown["note"] == ""
    rows = {c["name"]: c for c in shown["checks"]}
    assert rows["pytest (api/)"] == {"name": "pytest (api/)", "run": "uv run --frozen pytest -q", "dir": "api", "setup": "",
                                     "kind": "test", "changed_files": False, "paths": []}
    assert rows["py_compile (changed files)"]["changed_files"] and "tsc --noEmit (web/)" in rows
    configured = gate(tmp_path, "mono", wt, {"checks": [{"name": "unit", "run": "make test", "paths": ["api/"]}]}, what="describe")
    assert configured["source"] == "config" and [c["name"] for c in configured["checks"]] == ["unit"]


UNITTEST_BASE = {
    "calc/__init__.py": "",
    "calc/core.py": "def half(n):\n    return n // 2\n",
    "tests/__init__.py": "",
    "tests/test_core.py": "import unittest\n\nfrom calc.core import half\n\n\nclass T(unittest.TestCase):\n"
                          "    def test_even(self):\n        self.assertEqual(half(4), 2)\n",
}


def test_a_unittest_suite_is_run_and_a_branch_that_breaks_it_fails(tmp_path):
    """A repo on the standard library's unittest declares no runner: its test files are what says to run them."""
    wt = repo(tmp_path, "ut", UNITTEST_BASE, {"calc/core.py": "def half(n):\n    return n // 3\n"})
    res = gate(tmp_path, "ut", wt)
    layers = by_name(res)
    assert layers["unittest"]["status"] == "fail" and "test_even" in layers["unittest"]["detail"], layers
    assert not res["passed"]
    ok = repo(tmp_path, "ut2", UNITTEST_BASE, {"calc/core.py": "def half(n):\n    return n // 2  # same\n"})
    res = gate(tmp_path, "ut2", ok)
    assert by_name(res)["unittest"]["status"] == "pass" and res["passed"], res["layers"]


def test_a_unittest_repo_with_no_project_file_can_run_a_reproduction_test(tmp_path):
    """Asked how the repo runs its tests, with no diff in hand (what the reproduce stage asks before the fix exists),
    loose Python counts as a project: its unittest suite is the answer, not 'no test command'."""
    wt = repo(tmp_path, "ut3", UNITTEST_BASE, {"calc/core.py": "def half(n):\n    return n // 2  # same\n"})
    env = {**os.environ, "CLEA_HOME": str(tmp_path / "home-ut3"), "CLEA_CODE": str(ROOT), "PYTHONPATH": str(ROOT)}
    (tmp_path / "home-ut3").mkdir()
    (tmp_path / "home-ut3" / "config.json").write_text(json.dumps({
        "edition": "release", "language": "en", "repos": {"ut3": {"path": str(wt), "default": "main"}}, "primary_repo": "ut3"}))
    p = subprocess.run([sys.executable, "-c", "import json, sys\nfrom pathlib import Path\nfrom ap import gates\n"
                        "print(json.dumps(gates.how_tests_run('ut3', Path(sys.argv[1]))))", str(wt)],
                       capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert p.returncode == 0, p.stderr[-800:]
    runs = json.loads(p.stdout.strip().splitlines()[-1])
    assert len(runs) == 1 and "unittest discover" in runs[0]["run"], runs


def test_an_install_without_the_review_bot_waits_for_no_pass(tmp_path):
    wt = repo(tmp_path, "cr", UNITTEST_BASE, {"calc/core.py": "def half(n):\n    return n // 2  # same\n"})
    env = {**os.environ, "CLEA_HOME": str(tmp_path / "home-cr"), "CLEA_CODE": str(ROOT), "PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    (tmp_path / "home-cr").mkdir()
    (tmp_path / "home-cr" / "config.json").write_text(json.dumps({
        "edition": "release", "language": "en", "repos": {"cr": {"path": str(wt), "default": "main"}}, "primary_repo": "cr"}))
    p = subprocess.run([sys.executable, "-c", "import json, sys\nfrom pathlib import Path\nfrom ap import gates\n"
                        "print(json.dumps(gates.coderabbit(Path(sys.argv[1]), 'cr')))", str(wt)],
                       capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert p.returncode == 0, p.stderr[-800:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["completed"] and out["findings"] == [] and "no review bot" in out["output"]  # and no CLI was needed: PATH has none
