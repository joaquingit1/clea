"""Which model a call starts on, and when it steps up. The simulator stands in for claude.build and muse.call whole,
so the routing inside them is held here: a light fix round starts on Sonnet and is still a code session Opus can take,
review-comment triage runs on the mid tier, and a mid-tier call that fails is asked again of Opus."""
from __future__ import annotations

import pytest

from ap import claude, muse, tasks


@pytest.fixture
def sessions(monkeypatch):
    ran = []
    monkeypatch.setattr(claude, "can_spend", lambda role, urgent=False: True)
    monkeypatch.setattr(claude.db, "kv_get", lambda key, default=None: 0 if key.startswith("model_limited:") else default)

    def once(role, prompt, model, cwd, item_id, add_dirs, timeout):
        ran.append(model)
        return {"text": "ok"}
    monkeypatch.setattr(claude, "_build_once", once)
    return ran


def test_a_fix_runs_on_opus_unless_the_round_is_light(sessions):
    claude.build("fix", "p", cwd="/tmp")
    claude.build("fix", "p", cwd="/tmp", light=True)
    claude.build("build", "p", cwd="/tmp", light=True)  # a build is never light, whatever the caller says
    assert sessions == ["opus", "sonnet", "opus"]


def test_a_light_round_falls_to_opus_when_sonnet_is_capped(sessions, monkeypatch):
    def once(role, prompt, model, cwd, item_id, add_dirs, timeout):
        sessions.append(model)
        if model == "sonnet":
            raise claude.ModelLimited("capped")
        return {"text": "ok"}
    monkeypatch.setattr(claude, "_build_once", once)
    claude.build("fix", "p", cwd="/tmp", light=True)
    assert sessions == ["sonnet", "opus"]


@pytest.mark.parametrize("data,mechanical,want", [
    ({}, True, (True, False)),                       # the first mechanical round
    ({}, False, (False, False)),                     # a round that needs judgement
    ({"fix_light": True}, True, (False, True)),      # the last round was light and the case is back
    ({"fix_stepped_up": True}, True, (False, True)), # once stepped up, for good
])
def test_only_the_first_mechanical_round_is_light(data, mechanical, want):
    assert tasks.light_round(data, mechanical) == want


def test_light_fixes_can_be_turned_off(monkeypatch):
    monkeypatch.setitem(tasks.CFG["quality"], "light_fixes", False)
    assert tasks.light_round({}, True) == (False, False)


def test_comment_triage_is_mid_tier_and_judgement_stays_on_opus():
    assert muse._claude_route("comments")[0] == "sonnet"
    assert muse._claude_route("ci-diagnose")[0] == "sonnet"
    for lane in ("investigate", "plan", "reply", "draft-comments", "review-correctness"):
        assert muse._claude_route(lane)[0] == "opus", lane


@pytest.fixture
def lean(monkeypatch):
    asked = []
    monkeypatch.setattr(muse, "rules_pack", lambda lane: "")

    def fake(role, prompt, *, model="opus", **k):
        asked.append(model)
        if model == "sonnet" and fake.fails:
            raise fake.fails
        return {"ok": model}
    fake.fails = None
    monkeypatch.setattr(claude, "lean", fake)
    return asked, fake


def test_a_failed_mid_tier_call_is_asked_again_of_opus(lean):
    asked, fake = lean
    fake.fails = claude.ClaudeError("agent-ap-investigate: no structured_output")
    out = muse._via_claude("ap-investigate", "p", {"type": "object"}, "/tmp", "comments", None, 60, "sonnet", ())
    assert asked == ["sonnet", "opus"] and out == {"ok": "opus"}


def test_other_lanes_and_unpayable_calls_do_not_step_up(lean):
    asked, fake = lean
    fake.fails = claude.ClaudeError("agent-ap-triage: no structured_output")
    with pytest.raises(claude.ClaudeError):
        muse._via_claude("ap-triage", "p", {"type": "object"}, "/tmp", "triage", None, 60, "sonnet", ())
    fake.fails = claude.ClaudeError("agent-ap-investigate: success Credit balance is too low")
    with pytest.raises(claude.ClaudeError):
        muse._via_claude("ap-investigate", "p", {"type": "object"}, "/tmp", "comments", None, 60, "sonnet", ())
    fake.fails = claude.Interrupted("stopped")
    with pytest.raises(claude.Interrupted):
        muse._via_claude("ap-investigate", "p", {"type": "object"}, "/tmp", "comments", None, 60, "sonnet", ())
    assert asked == ["sonnet", "sonnet", "sonnet"]


def test_a_call_can_name_its_tier(monkeypatch):
    got = {}
    monkeypatch.setattr(muse, "_via_claude", lambda agent, prompt, schema, cwd, lane, item_id, timeout, model, tools: got.update(model=model, tools=tools) or {})
    muse.call("ap-investigate", "p", cwd="/tmp", schema={"type": "object"}, lane="ci-diagnose", claude_model="opus")
    assert got == {"model": "opus", "tools": ("Read", "Grep", "Glob")}


# ---- what a code session carries, and what one session of a case leaves for the next ----------------------------------
import json
from pathlib import Path


def test_a_code_session_carries_only_the_tools_it_uses():
    assert claude.code_tools("fix", personal=False) == ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
    assert claude.code_tools("build", personal=False) == ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
    # the first install: its engineering skills on demand, and a subagent for the review its build brief asks for
    assert claude.code_tools("fix", personal=True)[-1:] == ["Skill"]
    assert claude.code_tools("build", personal=True)[-2:] == ["Skill", "Agent"]
    # an install whose repos carry skills of their own can keep them loadable, without the subagent
    assert claude.code_tools("build", personal=False, repo_skills=True) == ["Read", "Edit", "Write", "Grep", "Glob", "Bash", "Skill"]
    for tool in ("TaskCreate", "TaskUpdate", "TaskList"):
        assert tool not in claude.code_tools("build", personal=True)


def test_every_role_is_told_to_ask_for_its_lookups_together():
    assert "several tool calls in the same turn" in Path(claude.system_file("fix")).read_text()


def test_the_transcript_folder_of_a_path_with_spaces_and_dots(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    d = claude._project_dir("/Users/sam/Library/Application Support/Clea/install-v0.2.0/worktrees/web--fix")
    assert d.name.endswith("-Library-Application-Support-Clea-install-v0-2-0-worktrees-web--fix")


@pytest.fixture
def case(monkeypatch, tmp_path):
    """A worktree of repo 'web', a transcript folder for it, and a place for the case's notes."""
    wt = tmp_path / "data" / "worktrees" / "web--fix-coupons"
    for rel in ("checkout/coupons.py", "checkout/cart.py", "tests/test_coupons.py"):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text("x = 1\n")
    notes = tmp_path / "items" / "7"
    notes.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(claude, "item_dir", lambda i: notes)
    monkeypatch.setattr(claude.site, "repos", lambda: {"web": {"path": str(tmp_path / "clones" / "web")}})

    def session(sid: str, cwd: Path, *uses) -> None:
        proj = claude._project_dir(str(cwd))
        proj.mkdir(parents=True, exist_ok=True)
        lines = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": n, "input": inp}]}}
                 for i, (n, inp) in enumerate(uses)]
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    return wt, notes, session


def test_what_a_session_opened_is_handed_to_the_next_one(case, tmp_path):
    wt, notes, session = case
    mirror = tmp_path / "data" / "mirror" / "web"
    mirror.mkdir(parents=True)
    session("s1", mirror,                                             # the investigation read the mirror
            ("Read", {"file_path": str(mirror / "checkout/coupons.py")}),
            ("Read", {"file_path": str(mirror / "checkout/coupons.py")}),
            ("Bash", {"command": f'cd "{mirror}" && sed -n \'1,80p\' checkout/cart.py'}),
            ("Read", {"file_path": str(mirror / "checkout/gone.py")}),   # not in the worktree: never listed
            ("Read", {"file_path": str(notes / "plan.json")}),           # the case's own notes are not repo files
            ("Bash", {"command": "pytest tests -q"}))                    # not a read
    claude.note_files(7, "s1", str(mirror))
    session("s2", wt, ("Edit", {"file_path": str(wt / "tests/test_coupons.py")}))   # the build changed a test
    claude.note_files(7, "s2", str(wt))
    known = json.loads((notes / "files.json").read_text())
    assert known["web:checkout/coupons.py"] == {"reads": 2, "changed": False} and known["web:tests/test_coupons.py"]["changed"]
    note = claude.files_note(7, str(wt))
    listed = [l[2:] for l in note.splitlines() if l.startswith("- ")]
    assert listed == ["tests/test_coupons.py (changed by an earlier session)", "checkout/coupons.py", "checkout/cart.py"], note
    assert "in one turn" in note


def test_no_files_no_note_and_a_broken_transcript_never_fails_a_session(case, tmp_path):
    wt, notes, session = case
    assert claude.files_note(7, str(wt)) == "" and claude.files_note(None, str(wt)) == ""
    claude.note_files(7, "missing", str(wt))          # no transcript for that session
    claude.note_files(7, None, str(wt))               # the run gave no session id
    assert not (notes / "files.json").exists()
    assert claude.files_note(7, str(tmp_path)) == ""  # a session outside any repo gets no list


# ---- a billing refusal stops the install ------------------------------------------------------------------------------
@pytest.fixture
def cli(monkeypatch):
    """claude._invoke with the CLI, the ledger and the key-value store stood in: the CLI answers what the test says."""
    kv, calls = {}, []
    monkeypatch.setattr(claude.db, "kv_get", lambda k, default=None: kv.get(k, default))
    monkeypatch.setattr(claude.db, "kv_set", lambda k, v: kv.__setitem__(k, v))
    monkeypatch.setattr(claude.db, "record_call", lambda *a: 1)
    monkeypatch.setattr(claude.providers, "claude_env", lambda: {})
    monkeypatch.setattr(claude.providers, "selected_id", lambda: "claude-api")
    monkeypatch.setattr(claude, "_remember_session", lambda *a: None)
    monkeypatch.setattr(claude, "_signed_out", lambda t0: True)

    class Proc:
        returncode, stderr = 1, ""

        def __init__(self, out):
            self.stdout = json.dumps(out)

    def answer(out):
        def run(cmd, **k):
            calls.append(cmd)
            return Proc(out)
        monkeypatch.setattr(claude, "run", run)
    return kv, calls, answer


@pytest.mark.parametrize("said,status", [
    ("Credit balance is too low", None),                                           # Anthropic, a key out of credit
    ("You exceeded your current quota, please check your plan and billing details", None),  # OpenAI
    ("request failed", 402),
])
def test_a_billing_refusal_stops_the_install_and_no_further_call_leaves(cli, said, status):
    kv, calls, answer = cli
    answer({"is_error": True, "subtype": "success", "result": said, "api_error_status": status, "session_id": "s"})
    with pytest.raises(claude.ClaudeLimited, match="stopped"):
        claude._invoke(["p", "--model", "claude-opus-5-5"], "reply", 7, "/tmp", 60)
    assert claude.stopped()["why"] == said and claude.stopped()["provider"] == "claude-api"
    with pytest.raises(claude.ClaudeLimited, match="stopped"):
        claude._invoke(["p", "--model", "claude-opus-5-5"], "reply", 8, "/tmp", 60)
    assert len(calls) == 1  # the second one never reached the CLI


def test_a_usage_limit_or_a_lost_login_is_not_a_stop(cli):
    kv, calls, answer = cli
    answer({"is_error": True, "subtype": "success", "result": "Not logged in. Please run /login", "api_error_status": 401})
    with pytest.raises(claude.ClaudeLimited, match="provider"):
        claude._invoke(["p", "--model", "claude-opus-5-5"], "reply", 7, "/tmp", 60)
    assert claude.stopped() is None and kv.get("provider_problem")  # the ten-minute pause a probe lifts, as before
