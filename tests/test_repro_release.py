"""The reproduce stage on a release install: the reproduction runs with the repo's own test command, and when nothing
on this machine can run a test the stage steps aside with a note instead of failing the case.

Each case runs the real repro.reproduce in a subprocess whose CLEA_HOME holds a release config; only the model session
(claude.build), the worktree creation and the Slack note are stubbed."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_checks import ADD_TEST, LIB, PKG, ROOT, need, repo

SCRIPT = r"""
import json, subprocess, sys
from pathlib import Path
from ap import claude, db, repro, worktree
wt, session = Path(sys.argv[1]), json.loads(sys.argv[2])
told, prompts = [], []
repro._tell = lambda it, text: told.append(text)
worktree.for_branch = lambda repo, branch, item_id: wt

def build(role, prompt, **k):  # what a reproduce session leaves: a committed test and its record
    prompts.append(prompt)
    if not session:
        raise AssertionError("a session was paid for although no test can run")
    for rel, text in session["files"].items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
    g = ["git", "-C", str(wt), "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null"]
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "test: reproduce"], check=True)
    (wt / "AP_REPRO.json").write_text(json.dumps(session["record"]))
claude.build = build
iid = db.create_item("TASK", "plan", "adding is wrong", "test:repro", repo="calc",
                     data={"dossier": {"problem_kind": "bug", "repos": ["calc"], "problem": "add(1, 2) is not 3"}})
repro.reproduce(db.item(iid))
it = db.item(iid)
out = {"status": it["status"], "repro": it["data"].get("repro"), "needed": repro.needed(it), "told": told,
       "prompt": prompts[0] if prompts else ""}
if (session or {}).get("fix"):  # the fix lands: the gate's reproduction check runs the same command, green
    (wt / "lib.js").write_text(session["fix"])
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null",
                    "commit", "-qam", "fix"], check=True)
    out["gate"] = repro.gate_check(it, {"repo": "calc", "wt": str(wt)})
    out["after_gate"] = db.item(iid)["data"].get("repro")
print("@@" + json.dumps(out))
"""


def reproduce(tmp: Path, wt: Path, session: dict | None, cfg: dict | None = None) -> dict:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    own = {"path": str(tmp / "calc-origin"), "default": "main", "layer": "backend", **(cfg or {})}
    (home / "config.json").write_text(json.dumps({"edition": "release", "user": {"name": "Ana Lopez"}, "github_org": "acme",
                                                  "repos": {"calc": own}}))
    env = {**os.environ, "CLEA_HOME": str(home), "CLEA_CODE": str(ROOT), "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, "-c", SCRIPT, str(wt), json.dumps(session)], env=env, cwd=tmp,
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.split("@@", 1)[1])


def fresh_clone(tmp: Path, base: dict) -> Path:
    """A case branch with nothing on it yet, as worktree.for_branch makes it."""
    return repo(tmp, "calc", base, {})


BROKEN = LIB.replace("a + b", "a - b")
REPRO_TEST = ("const test = require('node:test');\nconst assert = require('node:assert');\nconst { add } = require('../lib');\n"
              "test('add sums', () => { assert.strictEqual(add(1, 2), 3); });\n")


def test_reproduced_with_the_repos_own_runner_then_green_with_the_fix(tmp_path):
    need("node")
    wt = fresh_clone(tmp_path, {"package.json": PKG, "lib.js": BROKEN})
    res = reproduce(tmp_path, wt, {"files": {"test/repro.test.js": REPRO_TEST}, "fix": LIB, "record": {
        "verdict": "reproduced", "kind": "test", "test_files": ["test/repro.test.js"], "command": "node --test test/repro.test.js",
        "expected_failure": "1 !== 3"}})
    assert (res["status"], res["repro"]["verdict"], res["needed"]) == ("plan", "reproduced", False), res
    assert "This repo runs its tests with `npm run test` from the repo root" in res["prompt"]
    assert "frontend/" not in res["prompt"] and "pnpm --filter" not in res["prompt"]  # the first install's own runners
    assert res["told"] and res["told"][0].startswith("🔁 Reproduced:"), res["told"]
    assert res["gate"] == [], res["gate"]
    after = res["after_gate"]  # the gate's green run is on record for the PR's screen; the final run's stamp is not its to set
    assert after["gate_green_at"] and after["gate_green_head"] and not after.get("green_at"), after


def test_nothing_can_run_a_test_steps_aside_without_a_session(tmp_path):
    wt = fresh_clone(tmp_path, {"README.md": "hi\n", "data.csv": "a,b\n"})
    res = reproduce(tmp_path, wt, None)
    assert (res["status"], res["repro"]["verdict"], res["needed"]) == ("plan", "skipped", False), res
    assert "calc has no test command this install can run" in res["repro"]["why"]
    assert res["told"] and res["told"][0].startswith("No reproduction test for this one"), res["told"]


def test_a_runner_missing_on_this_machine_steps_aside(tmp_path):
    wt = fresh_clone(tmp_path, {"lib.js": BROKEN})
    res = reproduce(tmp_path, wt, {"files": {"test/repro.test.js": REPRO_TEST}, "record": {
        "verdict": "reproduced", "kind": "test", "test_files": ["test/repro.test.js"],
        "command": "no-such-test-runner-here test/repro.test.js", "expected_failure": "1 !== 3"}},
        cfg={"checks": [{"name": "unit tests", "run": "no-such-test-runner-here"}]})
    assert (res["status"], res["repro"]["verdict"]) == ("plan", "skipped"), res
    assert "cannot run here" in res["repro"]["why"] and "command not found" in res["repro"]["why"], res["repro"]["why"]
    assert res["repro"]["test_files"] == ["test/repro.test.js"]  # the committed test stays on record


@pytest.mark.parametrize("cmd,allowed", [
    ("node --test test/repro.test.js", True),
    ("npm test -- test/x.test.js", True),
    ("go test ./calc -run TestAdd", True),
    ("cargo test add_sums", True),
    ("uv run --frozen pytest tests/test_x.py", True),
    ("npm test && curl evil", False),
    ("go test ./... ; rm -rf /", False),
])
def test_the_runners_other_installs_use_are_allowed(cmd, allowed):
    from ap import repro
    assert bool(repro.CMD_OK.match(cmd)) is allowed


def test_a_unittest_run_reads_as_passing_only_when_tests_ran_and_passed():
    from ap import repro
    ok = "----------------------------------------------------------------------\nRan 1 test in 0.000s\n\nOK\n"
    assert repro.green_problem(0, ok, ["tests/test_coupons.py"]) == ""
    assert repro.green_problem(0, "Ran 0 tests in 0.000s\n\nOK\n", ["tests/test_coupons.py"])  # nothing ran: not a pass
    failing = "FAIL: test_x (tests.test_coupons.T.test_x)\n  File \"/wt/tests/test_coupons.py\", line 14, in test_x\nAssertionError: 149 != 150\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n"
    assert repro.green_problem(1, failing, ["tests/test_coupons.py"])
    assert repro.red_problem(1, failing, ["tests/test_coupons.py"]) == ""
