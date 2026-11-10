"""Reproduce first, fix second, prove the fix against the same reproduction last.

Flow, for every TASK/INCIDENT/IDEA whose dossier says it is a bug or an incident:

  investigate -> plan [reproduce first, inside the plan stage] -> critique -> build... gate ->... verify_done -> announce

- reproduce(): one code session on a fresh branch writes ONLY a test that reproduces the reported symptom with the
  reporter's data shape, commits it, and records AP_REPRO.json. The harness then runs that test itself: the
  reproduction counts only when the harness saw it fail for the stated reason (a model saying "it fails" is not
  evidence). Outcomes: reproduced -> plan on the same branch; not reproduced (the symptom is not there, or the
  hypothesis is false) -> the case closes with the evidence, nothing is built; cannot reproduce (needs data, access
  or the reporter) -> blocked with the exact question, and escalation takes it.
- gate_check(): the reproduction test is never edited by the fix and must pass on the fix's HEAD. The one exception is
  a branch that merged in other code after the reproduction (stacked on another PR): its fakes may follow that code's
  interface, and the adapted test must fail on the merged-in code without the fix.
- final_check(): verify_done runs it once more on the final HEAD; red sends the case back to fix.
A symptom that no test can express (infra, prod data) is reproduced as an observed probe with its evidence; its
final proof is the probe after deploy, which the case carries as a caveat.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

from . import db, site, worktree
from . import comms as _comms
from .core import CFG, CODE, V1, log, now, run, slug

WORK = ("TASK", "INCIDENT", "IDEA")


def _tilde(p: Path) -> str:
    """A path the way a prompt writes it: under the home folder as ~/..."""
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


# the container-test runner of this engine; the first install also accepts the one in its fixed location, which its
# prompts and its sessions' permissions name
_DTESTS = [CODE / "bin" / "ap-dtest"] + ([V1 / "v2" / "bin" / "ap-dtest"] if site.internal() else [])
DTEST = _tilde(_DTESTS[0])
_DTEST_RE = "|".join(dict.fromkeys(re.escape(form) for p in _DTESTS for form in (_tilde(p), str(p))))
# a test runner and nothing chained after it (no shell runs it, but a `;` there means the command is not what it says).
# Past the first install's own forms: the runners a repo of any other install uses for one test file or name
CMD_OK = re.compile(rf"^(?!.*(?:;|&&|\|\||`|\$\())(?:(?:{_DTEST_RE}) \S+"
                    r"|pnpm (?:--filter \S+ )?(?:exec )?vitest(?: run)?"
                    r"|pnpm --filter \S+ (?:run )?test"
                    r"|(?:python3? -m )?pytest"
                    r"|(?:\S*/)?python3? -m pytest|(?:uv run(?: --frozen)?|poetry run) pytest|(?:\S*/)?python3? -m unittest"
                    r"|(?:npm|pnpm|yarn|bun)(?: run)? test|node --test|npx (?:--no(?:-install)? )?(?:vitest|jest|mocha)"
                    r"|(?:pnpm|yarn) (?:exec )?(?:vitest|jest|mocha)|bun test|go test|cargo test|make test)\b")
# test-failure markers, case-sensitive: a JSON '"status": "fail"' from the container runner is not a failing test
#
FAILED = re.compile(r"\bFAILED\b|^\s*FAIL\b|\bAssertionError\b|\b\d+ failed\b|AssertionError:|Expected:|\bexpected .+ to (?:be|equal|have|contain)"
                    r"|^[ℹ#] fail [1-9]|^\s*--- FAIL:|^not ok \d", re.M)
# pnpm's *_FIRST_FAIL line only says the wrapped command exited non-zero, which every red vitest run does
#
INFRA = re.compile(r"DID NOT RUN|did not bring the engine up|DockerUnavailable|Cannot connect to the Docker daemon|docker: "
                   r"|ERR_PNPM(?!_RECURSIVE_(?:EXEC|RUN)_FIRST_FAIL)"
                   r"|ModuleNotFoundError|ImportError|SyntaxError|collected 0 items|No test files found|command not found"
                   r"|No such file or directory|error: unknown flag", re.I)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # vitest colors its output: ' FAIL ' lines and the tails we quote
# the container never started the test (dtest's "env" results, Docker itself down): no session can change that, so the
# harness reruns its own red run on the committed test instead of paying a round (2026-10-16: #897's test met his
# postgres_school_test, up 28 h; the second Opus round, $1.18, met it again and the case blocked)
ENV_DOWN = re.compile(r'"env": true|is running \(your own tests\?\)|did not bring the engine up|Cannot connect to the Docker daemon'
                      r'|docker lock busy')
ENV_WAIT, ENV_RECHECK = 7200, 1200
# the runner itself is missing on this machine (exec failed, or the shell or npm said so): no session round fixes that
NO_TOOL = re.compile(r"DID NOT RUN: command not found|command not found|: not found\b|No module named pytest")
BLOCKED = site.t("the reproduction was not proven:", "la reproducción no quedó demostrada:")
GATE = site.t("reproduction", "reproducción")  # the gate layer gate_check's problems go in
TEST = re.compile(r"(^|/)(tests?|__tests__)/|test_|_test\.|\.test\.|\.spec\.")


def rev() -> str:
    """Hash of this module and dtest (whose result says whether the test ran at all): a rejection block gets one more
    round when the code that rejected it changes (worker.requeue_harness_bugs)."""
    import hashlib
    here = Path(__file__).parent
    return hashlib.sha1((here / "repro.py").read_bytes() + (here / "dtest.py").read_bytes()).hexdigest()[:12]


def needed(it: dict) -> bool:
    ds = it["data"].get("dossier") or {}
    return it["kind"] in WORK and ds.get("problem_kind") in ("bug", "incident") \
        and (it["data"].get("repro") or {}).get("verdict") not in ("reproduced", "observed", "skipped")


def _repo(it: dict) -> str:
    from .tasks import dossier_repos
    repos = dossier_repos(it["data"].get("dossier") or {})
    return repos[0] if repos else (it.get("repo") or site.repo_with("backend"))


def _branch(it: dict) -> str:
    return (slug(it["title"], 28) + f"-{it['id']}")[:39]


PROMPT = """REPRODUCE THIS BUG. Do not fix it.

Case #{id}: {title}
What was reported or hypothesised:
{problem}

Dossier (root cause hypothesis, evidence, repos): {dossier}
Case thread and context: {context}

You are in a fresh worktree of {repo} ({wt}, branch {branch}). Your only job is a reproduction a reviewer can rerun:
1. Reproduce the EXACT reported symptom, with the reporter's data shape (the school's configuration, roles, values,
   request), not a simplified case that happens to fail. If it came from an alert or a patrol finding, reproduce the
   hypothesised failure itself; if you cannot make it fail, that is the answer (the hypothesis is false).
2. Write it as an automated test next to the existing tests for that code, following the repo's patterns (factories,
   fixtures). Change NO production code. Run it and confirm it fails on the current code for the reported reason.
{how_tests}
3. Commit only the test (message "test: reproduce <short symptom>"). The test ships in the PR and nobody may edit it
   afterwards, so its comments carry no ticket or PR references ({ticket}-123, #456) and no em dashes.
4. If the symptom is real but no automated test can express it (infra, a prod-only data state), gather the live
   evidence (logs, metrics, read-only queries) and report kind "observed".
5. Write {wt}/AP_REPRO.json (do not commit it):
   {{"verdict": "reproduced" | "not_reproduced" | "cannot_reproduce",
     "kind": "test" | "observed",
     "test_files": ["path relative to the repo root"],
     "command": "{command_hint}",{dir_field}
     "expected_failure": "the assertion/error line that shows the reported symptom",
     "evidence": "what you checked and saw (numbers, ids, outputs)",
     "question": "for cannot_reproduce: the precise question and who can answer it",
     "why": "one paragraph"}}
"""


def _prompt(repo: str, wt: Path, **fields) -> str:
    """PROMPT with what the install decides: how its tests run (the first install: its container recipe, said only
    where some repo has one, and its frontend's vitest; any other: the repo's own test commands, from its config or its
    files) and what a ticket key looks like."""
    from . import dtest
    backends = "/".join(r for r, c in CFG["repos"].items() if c.get("layer") == "backend")
    container = (f"   {backends[:1].upper()}{backends[1:]} tests run in the container: `{DTEST} {repo} {wt} <test file>`.\n"
                 if backends and any(dtest.recipe(r) for r in CFG["repos"]) else "")
    first = f"{container}   Frontend tests: `pnpm --filter <app> exec vitest run <file>` from the frontend/ folder."
    how = site.own(first, "") or _own_runners_note(repo, wt)
    return PROMPT.format(repo=repo, wt=wt, how_tests=how, ticket=site.ticket_prefix(),
                         command_hint=site.own("the exact command that runs ONLY the reproduction (one of the two forms above)",
                                               "the exact command that runs ONLY the reproduction with this repo's test runner, "
                                               "no shell operators"),
                         dir_field=site.own("", '\n     "dir": "the folder the command runs from, relative to the repo root '
                                                '(empty for the root)",'), **fields)


def _own_runners_note(repo: str, wt: Path) -> str:
    """How this repo runs its tests, for a reproduce session of an install other than the first."""
    from .gates import how_tests_run
    try:
        runners = how_tests_run(repo, wt)
    except Exception:
        runners = []
    said = "; ".join(f"`{r['run'].replace(' {files}', ' <test files>')}` from {r['dir'] + '/' if r['dir'] else 'the repo root'}"
                     for r in runners[:4])
    return (f"   This repo runs its tests with {said or 'no command found'}. Run the reproduction with that same runner, "
            f"narrowed to your test (for example `node --test <file>`, `npx vitest run <file>`, `python3 -m pytest <file>`, "
            f"`go test ./<package> -run <TestName>`, `cargo test <name>`), and say its folder in \"dir\" when it is not "
            f"the repo root.")


def _session(it: dict, rounds: int) -> tuple[Path, dict | None]:
    """The reproduce session and its AP_REPRO.json. Returns (worktree, record), or a None record when its verdict
    already moved the case (observed, not reproduced, cannot reproduce)."""
    from . import claude
    from .core import item_dir
    from .tasks import _context
    d = it["data"]
    repo, branch = _repo(it), (d.get("repro") or {}).get("branch") or _branch(it)
    wt = worktree.for_branch(repo, branch, it["id"])
    (wt / "AP_REPRO.json").unlink(missing_ok=True)
    base = fork_point(wt, repo)
    why_not = no_runner(repo, wt)
    if why_not:  # no session is paid for a test nothing here can run
        step_aside(it, {"repo": repo, "branch": branch, "wt": str(wt), "base": base, "at": now()}, why_not)
        return wt, None
    feedback = (d.get("repro") or {}).get("rejected")
    prompt = _prompt(repo, wt, id=it["id"], title=it["title"],
                     problem=((d.get("dossier") or {}).get("problem") or d.get("text") or "")[:3000],
                     dossier=item_dir(it["id"]) / "dossier.json", context=_context(it), branch=branch)
    if feedback:
        prompt += f"\n\nYOUR PREVIOUS REPRODUCTION WAS NOT ACCEPTED: {feedback}\nFix exactly that."
    from .tasks import _later_messages
    prompt += _later_messages(it)
    claude.build("reproduce", prompt, model="opus", cwd=str(wt), add_dirs=(site.root(), str(item_dir(it["id"]))),
                 item_id=it["id"], urgent=it["urgency"] >= 3, timeout=2400)
    f = wt / "AP_REPRO.json"
    try:
        r = json.loads(f.read_text())
    except Exception:
        r = {"verdict": "invalid", "why": "the session wrote no AP_REPRO.json"}
    f.unlink(missing_ok=True)  # stored in the case now; left in the build's worktree it failed the gate's clean tree
    rec = {"repo": repo, "branch": branch, "wt": str(wt), "base": base, "at": now(), **{k: r.get(k) for k in
           ("verdict", "kind", "test_files", "command", "dir", "expected_failure", "evidence", "question", "why")}}
    if not rec.get("dir"):
        rec.pop("dir")  # the first install's records keep their shape
    if r.get("verdict") == "reproduced" and r.get("kind") == "observed":
        rec["verdict"] = "observed"
        db.update_item(it["id"], status="plan", note=site.t("reproduced (observed evidence, no test possible)",
                                                            "reproducido (evidencia observada, sin test posible)"), data={"repro": rec})
        _tell(it, site.t(f"🔁 Reproduced with evidence (no test can express it): {str(r.get('evidence'))[:400]}",
                         f"🔁 Reproducido con evidencia (no se puede expresar como test): {str(r.get('evidence'))[:400]}"))
        return wt, None
    if r.get("verdict") == "not_reproduced":
        db.update_item(it["id"], status="dropped", note=site.t(f"does not reproduce: {str(r.get('why'))[:200]}",
                                                               f"no se reproduce: {str(r.get('why'))[:200]}"), data={"repro": rec})
        _tell(it, site.t(f"🚫 It does not reproduce, so nothing gets built.\n{str(r.get('evidence') or r.get('why'))[:600]}",
                         f"🚫 No se reproduce, no construyo nada.\n{str(r.get('evidence') or r.get('why'))[:600]}"))
        return wt, None
    if r.get("verdict") == "cannot_reproduce":
        db.update_item(it["id"], status="blocked", data={"repro": rec, "blocked_reason": site.t(
                       f"could not reproduce it: {str(r.get('question') or r.get('why'))[:900]}",
                       f"no pude reproducirlo: {str(r.get('question') or r.get('why'))[:900]}")})
        _tell(it, site.t(f"❓ I could not reproduce it yet: {str(r.get('question') or r.get('why'))[:500]}",
                         f"❓ No lo pude reproducir todavía: {str(r.get('question') or r.get('why'))[:500]}"))
        return wt, None
    return wt, rec


def no_runner(repo: str, wt: Path) -> str:
    """Why no reproduction test can be run for this repo on this machine ("" when one can). The first install always
    has its runners; any other needs a test command in the repo's config or in its own files."""
    if site.internal():
        return ""
    from . import dtest
    from .gates import how_tests_run
    try:
        if dtest.recipe(repo) or how_tests_run(repo, wt):
            return ""
    except Exception as e:
        return f"could not read how {repo} runs its tests ({type(e).__name__}: {str(e)[:200]})"
    return (f"{repo} has no test command this install can run: no `checks` in its config and no test script, pytest, "
            f"go.mod, Cargo.toml or Makefile test target in the repo")


def step_aside(it: dict, rec: dict, why: str) -> None:
    """The bug goes on to be planned without a reproduction test, saying why. Blocking a case on a test this machine
    cannot run would leave a real bug unfixed for a missing tool."""
    rec = {**rec, "verdict": "skipped", "why": why}
    db.update_item(it["id"], status="plan", data={"repro": rec, "repro_rounds": 0},
                   note=site.t(f"no reproduction test: {why[:160]}", f"sin test de reproducción: {why[:160]}"))
    _tell(it, site.t(f"No reproduction test for this one, so the fix goes ahead without it: {why[:400]}",
                     f"Sin test de reproducción para este caso, el fix sigue sin él: {why[:400]}"))


def reproduce(it: dict) -> None:
    from .core import item_dir
    d = it["data"]
    rounds = d.get("repro_rounds", 0)
    prev = d.get("repro") or {}
    if prev.get("wt") and prev.get("test_files") and ENV_DOWN.search(prev.get("rejected") or ""):
        wt, rec = Path(prev["wt"]), dict(prev)  # the test is committed, only its red run did not start: rerun that
    else:
        wt, rec = _session(it, rounds)
        if rec is None:
            return
    ok, why, out = verify_red(wt, rec)
    if not ok and not site.internal() and NO_TOOL.search(out):
        # the test is committed but its runner is not on this machine: no session round can install it
        return step_aside(it, rec, f"the test {rec.get('test_files')} is committed, but `{str(rec.get('command'))[:120]}` "
                                   f"cannot run here: {out.strip()[-300:]}")
    if not ok and ENV_DOWN.search(out):
        since = rec.get("env_since") or now()
        rec.update(rejected=why, verdict="rejected", env_since=since)
        if now() - since < ENV_WAIT:
            log("repro: the test did not start, rechecking the environment", item=it["id"], why=why[:200])
            return db.update_item(it["id"], status="plan", next_run_in=ENV_RECHECK, data={"repro": rec},
                                  note=site.t(f"the environment did not run the test, retrying: {why[:120]}",
                                              f"el entorno no corrió el test, reintento: {why[:120]}"))
        return db.update_item(it["id"], status="blocked", data={"repro": rec, "blocked_rev": rev(), "blocked_reason": site.t(
                              f"{BLOCKED} {why} (still the case after {(now() - since) // 60} min)",
                              f"{BLOCKED} {why} (sigue así desde hace {(now() - since) // 60} min)")})
    rec.pop("env_since", None)
    rec.pop("rejected", None)
    if not ok:
        rec["rejected"] = why
        rec["verdict"] = "rejected"  # never kept as "reproduced": only the harness's own red run grants that
        if rounds >= 1:
            db.update_item(it["id"], status="blocked", data={"repro": rec, "repro_rounds": rounds + 1, "blocked_rev": rev(),
                           "blocked_reason": f"{BLOCKED} {why}"})
            return
        log("repro rejected, one more round", item=it["id"], why=why)
        return db.update_item(it["id"], status="plan", data={"repro": rec, "repro_rounds": rounds + 1},
                              note=site.t(f"reproduction rejected: {why[:120]}", f"reproducción rechazada: {why[:120]}"))
    rec["verdict"] = "reproduced"
    rec["head"] = run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    rec["red_tail"] = out[-1500:]
    (item_dir(it["id"]) / "repro-red.txt").write_text(out[-20000:])
    db.update_item(it["id"], status="plan", note=site.t("reproduced: the test fails as reported",
                                                        "reproducido: el test falla como en el reporte"),
                   data={"repro": rec, "repro_rounds": 0})
    _tell(it, site.t(f"🔁 Reproduced: `{', '.join(rec['test_files'] or [])}` fails on the current code:\n> {str(rec.get('expected_failure'))[:300]}",
                     f"🔁 Reproducido: `{', '.join(rec['test_files'] or [])}` falla en el código actual:\n> {str(rec.get('expected_failure'))[:300]}"))


def fork_point(wt: Path, repo: str) -> str:
    """Where the case branch left origin/<default> (worktree.for_branch makes it there), so every commit on it is the
    reproduction's, a killed earlier session's included. HEAD at session start is not: 2026-10-13 #727's test commit,
    left by a session lost to the reaper, was HEAD when the next round started, so its diff was empty and verify_red
    blocked a test that failed red ('no hay ningún commit con el test') twice."""
    mb = run(["git", "-C", str(wt), "merge-base", "HEAD", f"origin/{CFG['repos'][repo]['default']}"])
    return mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() \
        else run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()


def _own_runner(cmd: str) -> bool:
    """A test command a repo's config lists (repos.<name>.checks), or one narrowed from it: the install wrote it."""
    for r in CFG["repos"].values():
        for c in (r or {}).get("checks") or []:
            run_s = str((c or {}).get("run") or "").replace("{files}", "").strip()
            if run_s and re.search(r"test|spec|pytest|jest|vitest|mocha", f"{c.get('name')} {run_s}", re.I) \
                    and (cmd == run_s or cmd.startswith(run_s + " ")) and not re.search(r";|&&|\|\||`|\$\(", cmd):
                return True
    return False


def _at(wt: Path, rec: dict) -> Path:
    """Where the reproduction's command runs: the worktree, or the folder of it the record names."""
    d = str(rec.get("dir") or "").strip().strip("/")
    return wt / d if d and ".." not in d.split("/") and (wt / d).is_dir() else wt


def _run(wt: Path, cmd: str) -> tuple[int, str]:
    cmd = cmd.strip()
    if not CMD_OK.match(cmd) and not _own_runner(cmd):
        return -1, site.t(f"command not allowed to run the reproduction: {cmd[:200]}",
                          f"comando no permitido para correr la reproducción: {cmd[:200]}")
    cmd = cmd.replace("~/", str(Path.home()) + "/", 1)
    if site.internal():
        cwd = wt / "frontend" if cmd.startswith("pnpm") and (wt / "frontend").exists() else wt
        extra = None
    else:
        from .gates import CHECK_ENV
        cwd, extra = wt, CHECK_ENV  # CI mode, and no bytecode left in the worktree for the gate to call dirt
    try:
        r = run(shlex.split(cmd), cwd=cwd, timeout=2400, extra_env=extra)
    except FileNotFoundError as e:  # the runner is not installed: an environment fact, not a crash of the stage
        return 127, f"DID NOT RUN: command not found: {e.filename or cmd.split()[0]}"
    except subprocess.TimeoutExpired:
        return 124, "DID NOT RUN: the test command timed out after 2400s"
    # stderr first: the tails kept as evidence (red_tail, green_tail) then end on the runner's summary line, not on
    # vite's config warnings
    return r.returncode, ANSI.sub("", (r.stderr or "") + "\n" + (r.stdout or ""))


def verify_red(wt: Path, rec: dict) -> tuple[bool, str, str]:
    """The harness itself sees the reproduction fail, for a test reason, with only test files committed."""
    files = rec.get("test_files") or []
    if not files or not rec.get("command"):
        return False, site.t("test_files or command is missing", "faltan test_files o command"), ""
    since = rec.get("base") or f"origin/{CFG['repos'][rec['repo']]['default']}"  # the branch head before the session
    changed = [x for x in run(["git", "-C", str(wt), "diff", "--name-only", f"{since}..HEAD"]).stdout.split() if x]
    if not changed:
        return False, site.t("there is no commit with the test", "no hay ningún commit con el test"), ""
    prod = [x for x in changed if not TEST.search(x)]
    if prod:
        return False, site.t(f"the reproduction touched code that is not tests: {prod[:5]}",
                             f"la reproducción tocó código que no es de tests: {prod[:5]}"), ""
    from .gates import line_problems, plus
    dirty = [p for f, line in plus(run(["git", "-C", str(wt), "diff", "-U0", f"{since}..HEAD"]).stdout) for p in line_problems(f, line)]
    if dirty:  # the gate would fail on it later, and by then the test can no longer be edited
        return False, site.t(f"the test does not pass the gate's diff hygiene: {'; '.join(dirty[:5])}",
                             f"el test no pasa la higiene del diff del gate: {'; '.join(dirty[:5])}"), ""
    rc, out = _run(_at(wt, rec), rec["command"])
    if rc == -1:
        return False, out, out
    why = red_problem(rc, out, files)
    return not why, why, out


def red_problem(rc: int, out: str, files: list[str]) -> str:
    """Why a run of the reproduction does not show it failing as a test ("" when it does)."""
    if rc == 0:
        return site.t("the test passes on the current code: it reproduces nothing",
                      "el test pasa en el código actual: no reproduce nada")
    if INFRA.search(out):
        return site.t(f"the test did not run (environment error): {out[-300:]}",
                      f"el test no corrió (error de entorno): {out[-300:]}")
    if not FAILED.search(out):
        return site.t(f"it ended with an error, but not as a failing test: {out[-300:]}",
                      f"terminó con error pero no como test fallado: {out[-300:]}")
    if not any(Path(f).name in out for f in files):
        return site.t(f"what fails is not the reproduction test ({files}): {out[-300:]}",
                      f"lo que falla no es el test de reproducción ({files}): {out[-300:]}")
    return ""


PASSED = re.compile(r"\b\d+ passed\b|^[ℹ#] pass [1-9]|^ok\s+\S|^test result: ok\b", re.M)


def green_problem(rc: int, out: str, files: list[str]) -> str:
    """Why a run does not show the reproduction passing ("" when it does). Exit 0 alone is not a pass: on 10-21 a
    cometa worktree whose vitest config could not load its plugin exited 0 with no test run, and cometa#2900 read as
    green against a test that fails on it."""
    if rc != 0:  # "sigue fallando": the gate's words for this before 225c3b8 moved them here, which tests still read
        return site.t(f"the reproduction test still fails: {out[-800:]}", f"el test de reproducción sigue fallando: {out[-800:]}")
    if not PASSED.search(out) or FAILED.search(out):
        return site.t(f"it exited 0 but the output does not show the test passing: {out[-500:]}",
                      f"terminó en 0 pero la salida no muestra el test pasando: {out[-500:]}")
    return ""


def frozen_note(it: dict, part: dict) -> str:
    """The rule gate_check enforces, told to every fix session on the reproduction's repo. Only plan and build were
    told: on 10-13 #717's verify_done asked to update 'scope-test case 2 and the binding fake', both in the frozen
    test, the 74-min fix round did, the gate rejected the edit, and with its rounds spent the PR shipped with caveats.
    When the branch already builds on newer code (merged_in, the gate's own test), the session is told the exception
    applies now. It was only told 'when you are asked to build on another PR', yet the gate's freshness layer rebases
    onto a newer default branch on its own, and gate_check accepts that too (207a594): 10-19 #916 was rebased over
    cometa#3025, which renamed the permission hook the test mocks, and two fix rounds left the test crashing on the
    stale mock and blocked, with the adapted test proven red-without/green-with in a throwaway copy."""
    rec = it["data"].get("repro") or {}
    if rec.get("verdict") != "reproduced" or rec.get("repo") != part.get("repo"):
        return ""
    upstream = merged_in(Path(part["wt"]), rec) if rec.get("head") and part.get("wt") else ""
    return (f"\n\nFROZEN REPRODUCTION TEST: {rec.get('test_files')} is the committed reproduction of this bug. Never edit "
            f"it, not even its fakes or fixtures; the gate rejects any change to it. If a problem above asks to change or "
            f"extend it, put the new case or fake in another test file and leave this one as it is. It must still pass "
            f"unchanged: {rec.get('command')}\n"
            + (f"Exception that applies now: this branch builds on newer code ({upstream[:10]}) than the test was written "
               f"on (another branch merged in, or a rebase onto a newer {CFG['repos'][rec['repo']]['default']}). If the "
               f"test's fakes or render path no longer fit that code's interface (a mocked module missing an export the "
               f"code now uses, a renamed hook, a changed shape), adapt only those to it, keeping every assertion about "
               f"the symptom; never bend production code back to the old interface to fit the test. The gate accepts the "
               f"adapted test only if it fails on {upstream[:10]} without your change and passes with it."
               if upstream else
               f"Only exception: when you are asked to build on another branch (another PR), merge it in with a merge "
               f"commit; then adapt only this test's fakes and render path to that code's interface, keeping what it "
               f"asserts about the symptom. The gate accepts the adapted test only if it fails on the merged-in code "
               f"without your change and passes with it."))


def merged_in(wt: Path, rec: dict) -> str:
    """The tip of the latest branch merged into the case branch after the reproduction (another PR it builds on, or a
    newer default branch), "" when none. A rebase builds on its new base just the same, but leaves no merge and drops
    the reproduction commit from the history, so the diff from it carries the base's own edits to the test file
: then the new base is what it builds on."""
    d = f"origin/{CFG['repos'][rec['repo']]['default']}"
    merges = run(["git", "-C", str(wt), "rev-list", "--first-parent", "--merges", "HEAD", f"^{rec['head']}", f"^{d}"]).stdout.split()
    if merges:
        return run(["git", "-C", str(wt), "rev-parse", f"{merges[0]}^2"]).stdout.strip()
    if run(["git", "-C", str(wt), "merge-base", "--is-ancestor", rec["head"], "HEAD"]).returncode == 1:
        return run(["git", "-C", str(wt), "merge-base", "HEAD", d]).stdout.strip()
    return ""


def red_on(wt: Path, rec: dict, upstream: str) -> str:
    """Why the reproduction as it is at HEAD does not fail on `upstream`'s production code ("" when it does). The
    production files are put back to upstream's for one run, then restored to HEAD."""
    rows = [l.split("\t", 1) for l in run(["git", "-C", str(wt), "diff", "--name-status", "--no-renames", upstream, "HEAD"]).stdout.splitlines() if "\t" in l]
    prod = [(s[0], f) for s, f in rows if not TEST.search(f)]
    g = ["git", "-C", str(wt)]
    try:
        for s, f in prod:
            if s == "A":
                (wt / f).unlink(missing_ok=True)
        if [f for s, f in prod if s != "A"]:
            run(g + ["checkout", upstream, "--", *[f for s, f in prod if s != "A"]])
        rc, out = _run(_at(wt, rec), rec["command"])
    finally:
        if prod:
            run(g + ["reset", "-q", "HEAD", "--", *[f for _, f in prod]])
        if [f for s, f in prod if s != "D"]:
            run(g + ["checkout", "HEAD", "--", *[f for s, f in prod if s != "D"]])
        for s, f in prod:
            if s == "D":
                (wt / f).unlink(missing_ok=True)
    return out if rc == -1 else red_problem(rc, out, rec["test_files"])


def gate_check(it: dict, part: dict) -> list[str]:
    """On the fix's HEAD: the reproduction is untouched and passes. Adapted is accepted only on a branch that merged in
    other code after it, and only if it still fails on that code: on 10-13 #636 was asked to build on #2900's state
    model, the frozen test mocked the old hook shape, and the fix kept the old code and argued against the request."""
    rec = it["data"].get("repro") or {}
    if rec.get("verdict") != "reproduced" or rec.get("repo") != part.get("repo"):
        return []
    wt = Path(part["wt"])
    edited = [x for x in run(["git", "-C", str(wt), "diff", "--name-only", rec["head"], "HEAD", "--", *rec["test_files"]]).stdout.split() if x]
    if edited:
        upstream = merged_in(wt, rec)
        if not upstream:
            return [site.t(f"{GATE}: the fix modified the reproduction test {edited} (it is not to be touched; the code has to make it pass)",
                           f"{GATE}: el fix modificó el test de reproducción {edited} (no se toca; el código tiene que hacerlo pasar)")]
        why = red_on(wt, rec, upstream)
        if why:
            return [site.t(f"{GATE}: the adapted test {edited} does not fail on the merged-in code ({upstream[:10]}) without the fix: {why[:600]}",
                           f"{GATE}: el test adaptado {edited} no falla sobre el código mergeado ({upstream[:10]}) sin el fix: {why[:600]}")]
        db.update_item(it["id"], data={"repro": {**rec, "adapted_on": upstream}})
    rc, out = _run(_at(wt, rec), rec["command"])
    why = green_problem(rc, out, rec["test_files"])
    if why:
        return [site.t(f"{GATE}: with the fix, {why}", f"{GATE}: con el fix, {why}")]
    return []


def final_check(it: dict) -> list[str]:
    """verify_done: the same reproduction, on the final HEAD, green. Recorded for the PR and the closing."""
    rec = it["data"].get("repro") or {}
    if rec.get("verdict") != "reproduced":
        return []
    part = next((p for p in it["data"].get("parts", []) if p.get("repo") == rec.get("repo") and p.get("wt")), None)
    if not part:
        return [site.t(f"{GATE}: there is no part in {rec.get('repo')} to check it on",
                       f"{GATE}: no hay parte en {rec.get('repo')} para verificarla")]
    rc, out = _run(_at(Path(part["wt"]), rec), rec["command"])
    why = green_problem(rc, out, rec["test_files"])
    if why:
        return [site.t(f"{GATE}: with the final fix, {why}", f"{GATE}: con el fix final, {why}")]
    db.update_item(it["id"], data={"repro": {**rec, "green_at": now(), "green_head": run(["git", "-C", part["wt"], "rev-parse", "HEAD"]).stdout.strip(),
                                             "green_tail": out[-800:]}})
    return []


def pr_section(it: dict) -> str:
    rec = it["data"].get("repro") or {}
    if rec.get("verdict") == "reproduced":
        return (site.own("\n\n## Reproducción\n", "\n\n## Reproduction\n") + "\n".join(f"- `{f}`" for f in rec.get("test_files") or [])
                + site.own(f"\n- Falla en master como en el reporte: `{str(rec.get('expected_failure'))[:200]}`",
                           f"\n- Fails on the base branch as reported: `{str(rec.get('expected_failure'))[:200]}`")
                + (site.own("\n- Pasa con este fix. Sus mocks se adaptaron a la interfaz de la rama sobre la que va, y así adaptado "
                            "falla sobre esa rama sin el fix.",
                            "\n- Passes with this fix. Its mocks were adapted to the interface of the branch it builds on, and "
                            "adapted that way it fails on that branch without the fix.") if rec.get("adapted_on") else
                   site.own("\n- Pasa con este fix (el test no se modificó).", "\n- Passes with this fix (the test was not changed).")))
    if rec.get("verdict") == "observed":
        return site.own(f"\n\n## Reproducción\nObservado en producción (no expresable como test): {str(rec.get('evidence'))[:600]}",
                        f"\n\n## Reproduction\nObserved in production (no test can express it): {str(rec.get('evidence'))[:600]}")
    if rec.get("verdict") == "skipped":
        return f"\n\n## Reproduction\nNo reproduction test: {str(rec.get('why'))[:300]}"  # only other installs skip
    return ""


def _tell(it: dict, text: str) -> None:
    from . import slack
    rt = it["data"].get("report_ts")
    try:
        slack.send_self(f"*{_comms.name(it)}*: {text}", thread_ts=rt, topic=f"repro-{it['id']}")
    except Exception as e:
        log("repro: DM failed", item=it["id"], err=str(e)[:120])


# ---- reproduction for a PR that already exists ----------------------------------------------------------------------
BACKFILL_NOTE = """

THIS BUG ALREADY HAS A FIX IN AN OPEN PR, {slug}#{n}, written before reproductions were required. Your worktree
is origin/{default} WITHOUT that fix. Reproduce the reported symptom on this code exactly as above. The harness then runs
the same test with the PR merged in, and it must pass there. So exercise only behaviour and entry points that exist on
this branch (never import a function, export or file the PR adds), and assert what the user sees (the wrong value, the
error, the missing message), not how the PR implements the fix. Read the PR to learn where the bug lives
(`gh pr diff {n} -R {slug}`, `gh pr view {n} -R {slug}`), but change no production code.
Every module you mock must be a partial mock that keeps the real exports (vitest: `vi.mock(path, async (importOriginal) =>
({{ ...(await importOriginal()), <only what you fake> }}))`; Python: patch the one attribute, never replace the module), so
the code the PR adds still finds what it imports. A full mock of a UI kit broke the green run of {primary}#2900 on a
component the fix added, before any assertion ran.
PR title: {title}
PR body (the fix's own account of the bug, may be wrong):
{body}
"""


def backfill_key(repo: str, n: int) -> str:
    return f"repro_pr:{repo}#{n}"


def backfill_pr(repo: str, n: int, case_id: int | None = None, rounds: int = 2) -> dict:
    """Reproduce the bug of one of his open PRs after the fact. The same session and the
    same proof as reproduce(): a test-only commit on a fresh branch off the default branch, which the harness itself
    sees fail for the reported reason. Then green: the PR's head merged into that branch (what will land), the same
    command, must pass. The record lives under backfill_key, never in the case's own repro (the case's fix branch does
    not carry this test, and gate_check/final_check would demand it there). Nothing is pushed."""
    from . import claude
    from .core import item_dir
    key = backfill_key(repo, n)
    pr = json.loads(run(["gh", "pr", "view", str(n), "-R", site.slug(repo), "--json", "title,body,headRefOid"]).stdout)
    it = db.item(case_id) if case_id else None
    default = CFG["repos"][repo]["default"]
    branch = f"repro-pr-{repo}-{n}"[:39]
    wt = worktree.for_branch(repo, branch, case_id or 0)
    front = site.layer_dir(repo, "frontend")
    if front and (wt / front).exists():
        from .gates import _ensure_frontend_deps
        err = _ensure_frontend_deps(wt / front)
        if err:
            rec = {"repo": repo, "pr": f"{repo}#{n}", "verdict": "invalid", "why": err, "at": now(), "backfill": True}
            db.kv_set(key, rec)
            return rec
    base = fork_point(wt, repo)
    if it:
        from .tasks import _context
        title, problem = it["title"], ((it["data"].get("dossier") or {}).get("problem") or it["data"].get("text") or "")
        context, dossier = _context(it), item_dir(it["id"]) / "dossier.json"
    else:
        title, problem, context, dossier = pr["title"], pr["body"] or "", f"(no case: the PR came from one of {site.first_name()}'s own sessions)", "(none)"
    feedback, rec = "", {}
    for rnd in range(rounds):
        (wt / "AP_REPRO.json").unlink(missing_ok=True)
        prompt = _prompt(repo, wt, id=case_id or f"{repo}#{n}", title=title, problem=problem[:3000], dossier=dossier, context=context,
                         branch=branch)
        prompt += BACKFILL_NOTE.format(repo=repo, slug=site.slug(repo), primary=site.primary_repo(), n=n, default=default,
                                       title=pr["title"], body=(pr["body"] or "")[:5000])
        if feedback:
            prompt += f"\n\nYOUR PREVIOUS REPRODUCTION WAS NOT ACCEPTED: {feedback}\nFix exactly that."
        dirs = [site.root()] + ([str(item_dir(it["id"]))] if it else [])
        try:
            claude.build("reproduce", prompt, model="opus", cwd=str(wt), add_dirs=tuple(dirs), item_id=None, timeout=2400)
        except Exception as e:
            rec = {"verdict": "invalid", "why": site.t(f"the session failed: {str(e)[:300]}", f"la sesión falló: {str(e)[:300]}")}
            break
        try:
            r = json.loads((wt / "AP_REPRO.json").read_text())
        except Exception:
            r = {"verdict": "invalid", "why": "the session wrote no AP_REPRO.json"}
        (wt / "AP_REPRO.json").unlink(missing_ok=True)
        rec = {"repo": repo, "branch": branch, "wt": str(wt), "base": base, "at": now(), **{k: r.get(k) for k in
               ("verdict", "kind", "test_files", "command", "expected_failure", "evidence", "question", "why")}}
        if r.get("verdict") != "reproduced" or r.get("kind") == "observed":
            if r.get("verdict") == "reproduced":
                rec["verdict"] = "observed"
            break
        ok, why, out = verify_red(wt, rec)
        if ok:
            rec.update(verdict="reproduced", head=run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip(), red_tail=out[-1500:])
            break
        rec.update(verdict="rejected", rejected=why)
        feedback = why
    rec.update(pr=f"{repo}#{n}", backfill=True, case=case_id, pr_head=pr["headRefOid"])
    if rec.get("verdict") == "reproduced":
        rec.update(_green_with_pr(wt, rec, n))
    db.kv_set(key, rec)
    log("repro backfill", pr=f"{repo}#{n}", verdict=rec.get("verdict"), green=bool(rec.get("green_at")))
    return rec


def _green_with_pr(wt: Path, rec: dict, n: int) -> dict:
    """The reproduction with the PR's fix: its head merged into the reproduction branch (main + fix + test), the merge
    undone afterwards. A PR that conflicts with the default branch runs at its own head plus the test instead."""
    g = ["git", "-C", str(wt)]
    run(g + ["fetch", "-q", "origin", f"pull/{n}/head"])
    pr_head = run(g + ["rev-parse", "FETCH_HEAD"]).stdout.strip()
    on = "main+PR"
    if run(g + ["merge", "-q", "--no-ff", "--no-commit", pr_head]).returncode != 0:
        run(g + ["merge", "--abort"])
        on = "PR"
        run(g + ["checkout", "-q", "--detach", pr_head])
        run(g + ["checkout", rec["head"], "--", *rec["test_files"]])
    try:
        front = site.layer_dir(rec["repo"], "frontend")
        if front and (wt / front).exists():
            from .gates import _ensure_frontend_deps
            _ensure_frontend_deps(wt / front)
        rc, out = _run(_at(wt, rec), rec["command"])
    finally:
        if on == "main+PR":
            run(g + ["merge", "--abort"])
        else:
            run(g + ["checkout", "-q", "-f", rec["branch"]])
        run(g + ["reset", "-q", "--hard", rec["head"]])
    if not green_problem(rc, out, rec["test_files"]):
        return {"green_at": now(), "green_head": pr_head, "green_on": on, "green_tail": out[-800:]}
    mock = re.search(r'No "(\w+)" export is defined on the "([^"]+)" mock', out)
    if mock:  # the test's fake, not the fix, failed: it proves nothing either way
        return {"green_fail": site.t(f"inconclusive: the test's mock of {mock.group(2)} does not define {mock.group(1)}, which "
                                     f"the PR uses (no assertion was reached)\n{out[-800:]}",
                                     f"inconclusive: el mock de {mock.group(2)} del test no define {mock.group(1)}, que usa el PR "
                                     f"(no llegó a ninguna assertion)\n{out[-800:]}"),
                "green_on": on, "green_head": pr_head, "inconclusive": True}
    return {"green_fail": out[-1500:], "green_on": on, "green_head": pr_head}
