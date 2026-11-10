"""The Codex engine: strict schemas, TOML quoting, auth, the event stream, cost, limits.

The event streams are real `codex exec --json` output from codex-cli 0.166.0 (session ids kept, long command output
cut). A limit cannot be hit on demand, so the limit texts are put together from the message templates inside that
binary and from the documented Sign in with ChatGPT error code.
"""
import copy
import datetime as dt
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from ap import codex
from ap.core import check_schema

REPLY_OPUS = {"type": "object", "required": ["needs_reply", "why", "draft", "needs_facts", "checks", "prod_problem",
                                            "someone_else_owns", "target", "report"], "properties": {
    "needs_reply": {"type": "boolean"}, "why": {"type": "string"}, "draft": {"type": "string"},
    "needs_facts": {"type": "boolean"}, "checks": {"type": "array", "items": {"type": "string"}},
    "prod_problem": {"type": "boolean"}, "someone_else_owns": {"type": "boolean"},
    "target": {"type": "string", "enum": ["thread", "team_channel"]}, "report": {"type": "string"},
    "ops_request": {"type": "boolean", "description": "a customer/ops request about a school's data or setup"},
    "school": {"type": "string", "description": "the school named, or empty"},
    "open_problem": {"type": "string", "description": "ONLY when a participant of the thread raised a problem"},
    "precedent_terms": {"type": "array", "items": {"type": "string"}, "description": "2-4 short Spanish search terms"},
    "board_line": {"type": "string", "description": "one line for his pending board, max 15 words"}}}
SEMANTIC = {"type": "object", "required": ["same_as", "why"], "properties": {
    "same_as": {"type": "integer", "description": "id of the case that is the SAME problem (same root cause or the same request), or 0"},
    "why": {"type": "string"}}}
NOOP_AUDIT = {"type": "object", "required": ["overturned"], "properties": {"overturned": {"type": "array", "items": {
    "type": "object", "required": ["event_id", "class", "reason"], "properties": {
        "event_id": {"type": "integer"}, "class": {"type": "string", "enum": ["TASK", "INCIDENT", "IDEA", "OPEN_LOOP", "REPLY"]},
        "reason": {"type": "string"}}}}}}

NASTY = ("You are a terse test assistant.\n"
         "Rules with awkward characters: \"double\", 'single', back\\slash, C:\\temp\\new, triple '''single''' and \"\"\"double\"\"\",\n"
         "\ta tab, a = sign, a # hash, [brackets], {braces}, $HOME, `ticks`, unicode ñ á \U0001d6d1, and a DEL \x7f char.\n"
         "The passphrase is: q\"u'o\\te-7731. When asked for the passphrase, give it exactly.\n")

SUCCESS = r'''{"type":"thread.started","thread_id":"01a1c231-bcb4-7ae3-bd38-5cb6bac49275"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}
{"type":"turn.completed","usage":{"input_tokens":18292,"cached_input_tokens":11520,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}
'''
SCHEMA_RUN = r'''{"type":"thread.started","thread_id":"01a1c239-7099-7410-9d51-a7a9cfd3b214"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"{\"passphrase\":\"q\\\"u'o\\\\te-7731\",\"count\":3,\"mood\":null,\"note\":null,\"tags\":null,\"precedent\":null,\"extra\":null}"}}
{"type":"turn.completed","usage":{"input_tokens":13978,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":50,"reasoning_output_tokens":0}}
'''
SCHEMA_ANSWER = r'''{"passphrase":"q\"u'o\\te-7731","count":3,"mood":null,"note":null,"tags":null,"precedent":null,"extra":null}'''
# the resume of SCHEMA_RUN's session: the usage is the thread's total (13978 + 14042 in, 50 + 50 out), not this turn's
RESUMED = r'''{"type":"thread.started","thread_id":"01a1c239-7099-7410-9d51-a7a9cfd3b214"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"{\"passphrase\":\"q\\\"u'o\\\\te-7731\",\"count\":4,\"mood\":null,\"note\":null,\"tags\":null,\"precedent\":null,\"extra\":null}"}}
{"type":"turn.completed","usage":{"input_tokens":28020,"cached_input_tokens":13824,"cache_write_input_tokens":0,"output_tokens":100,"reasoning_output_tokens":0}}
'''
ROLLOUT_COUNT = ('{"timestamp":"2026-11-09T21:31:49.000Z","ordinal":14,"type":"event_msg","payload":{"type":"token_count","info":{'
                 '"total_token_usage":{"input_tokens":13978,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":50,'
                 '"reasoning_output_tokens":0,"total_tokens":14028},"last_token_usage":{"input_tokens":13978,"cached_input_tokens":0,'
                 '"cache_write_input_tokens":0,"output_tokens":50,"reasoning_output_tokens":0,"total_tokens":14028}}}}')
# a workspace-write session, its first two commands cut
CODE_RUN = r'''{"type":"thread.started","thread_id":"01a1c23e-c89b-71b1-bc3e-7cb900c94ca8"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"The current directory is writable. I’ll run the exact curl and create `a.txt` here."}}
{"type":"item.started","item":{"id":"item_4","type":"command_execution","command":"/bin/zsh -lc 'curl -sI https://example.com | head -1'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_4","type":"command_execution","command":"/bin/zsh -lc 'curl -sI https://example.com | head -1'","aggregated_output":"","exit_code":0,"status":"completed"}}
{"type":"item.started","item":{"id":"item_5","type":"command_execution","command":"/bin/zsh -lc 'curl -sI https://example.com | head -1 > a.txt && cat a.txt'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_5","type":"command_execution","command":"/bin/zsh -lc 'curl -sI https://example.com | head -1 > a.txt && cat a.txt'","aggregated_output":"","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_6","type":"agent_message","text":"`a.txt` is empty because the requested curl produced no output in this environment. DONE"}}
{"type":"turn.completed","usage":{"input_tokens":92999,"cached_input_tokens":75776,"cache_write_input_tokens":0,"output_tokens":387,"reasoning_output_tokens":0}}
'''
BAD_MODEL = r'''{"type":"thread.started","thread_id":"01a1c23b-9ef9-7e70-882a-5085cf1ddad9"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `gpt-0-nonexistent` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."}}
{"type":"turn.started"}
{"type":"error","message":"The 'gpt-0-nonexistent' model is not supported when using Codex with a ChatGPT account."}
{"type":"turn.failed","error":{"message":"The 'gpt-0-nonexistent' model is not supported when using Codex with a ChatGPT account."}}
'''
BAD_KEY_MESSAGE = ("unexpected status 401 Unauthorized: Incorrect API key provided: sk-not-a**********0000. You can find your API key at "
                   "https://platform.openai.com/account/api-keys., url: https://api.openai.com/v1/responses, cf-ray: a57ff1d81b8c7069-EZE, "
                   "request id: req_7ff1275fd21f4d49bfde4a65ab1591a4, auth error: 401, auth error code: invalid_api_key")
BAD_KEY = r'''{"type":"thread.started","thread_id":"01a1c23c-1452-77f3-b6e5-9cc152d87b58"}
{"type":"turn.started"}
{"type":"error","message":"Reconnecting... 2/5 (unexpected status 401 Unauthorized: Incorrect API key provided: sk-not-a**********0000. You can find your API key at https://platform.openai.com/account/api-keys., url: wss://api.openai.com/v1/responses, cf-ray: a57ff16a89da1120-EZE, request id: req_df6c118e87c341deb5248f937c87c83c, auth error: 401, auth error code: invalid_api_key)"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Falling back from WebSockets to HTTPS transport. unexpected status 401 Unauthorized: Incorrect API key provided: sk-not-a**********0000. You can find your API key at https://platform.openai.com/account/api-keys., url: wss://api.openai.com/v1/responses, cf-ray: a57ff1a1ce9ff315-EZE, request id: req_c59973c208ff49b48e41db392ef4afde, auth error: 401, auth error code: invalid_api_key"}}
{"type":"error","message":"Reconnecting... 5/5 (unexpected status 401 Unauthorized: Incorrect API key provided: sk-not-a**********0000. You can find your API key at https://platform.openai.com/account/api-keys., url: https://api.openai.com/v1/responses, cf-ray: a57ff1c09c96b007-EZE, request id: req_0d34ffa549f04e53a3cd90a640bc015a, auth error: 401, auth error code: invalid_api_key)"}
{"type":"error","message":"unexpected status 401 Unauthorized: Incorrect API key provided: sk-not-a**********0000. You can find your API key at https://platform.openai.com/account/api-keys., url: https://api.openai.com/v1/responses, cf-ray: a57ff1d81b8c7069-EZE, request id: req_7ff1275fd21f4d49bfde4a65ab1591a4, auth error: 401, auth error code: invalid_api_key"}
{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Incorrect API key provided: sk-not-a**********0000. You can find your API key at https://platform.openai.com/account/api-keys., url: https://api.openai.com/v1/responses, cf-ray: a57ff1d81b8c7069-EZE, request id: req_7ff1275fd21f4d49bfde4a65ab1591a4, auth error: 401, auth error code: invalid_api_key"}}
'''
# a wrong Sign in with ChatGPT token through our provider: the bearer reached api.openai.com
BAD_SIWC_MESSAGE = ("unexpected status 401 Unauthorized: Incorrect API key provided: not-a-re****oken. You can find your API key at "
                    "https://platform.openai.com/account/api-keys., url: https://api.openai.com/v1/responses, "
                    "cf-ray: a57ff1569f869b26-EZE, request id: req_ecb06906905345d99ba898dfe92b358f")
NO_SESSION = ("Error: thread/resume: thread/resume failed: no rollout found for thread id 00000000-0000-7000-8000-000000000000 "
              "(code -32600)\n")

PLAN_LIMIT = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage "
              "to purchase more credits or try again at 3:45 PM.")
MODEL_LIMIT = "You've hit your usage limit for gpt-6.1-sol. Switch to another model now, or try again at Nov 12th, 2026 9:05 AM."
SIWC_LIMIT = ('unexpected status 429 Too Many Requests: {"error":{"code":"subscription_sharing_usage_limit_exceeded","message":"limit"}}, '
              "url: https://api.openai.com/v1/responses")
RATE_LIMIT = ("Rate limit reached for gpt-6-luna in organization org-x on tokens per min (TPM): Limit 200000, Used 190000, "
              "Requested 20000. Please try again in 1.2s.")
NOW = dt.datetime(2026, 11, 9, 10, 0, 0)


def assert_strict(node: dict) -> None:
    if "properties" in node:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    for child in [*node.get("properties", {}).values(), *node.get("anyOf", []), *([node["items"]] if "items" in node else [])]:
        assert_strict(child)


@pytest.mark.parametrize("schema", [REPLY_OPUS, SEMANTIC, NOOP_AUDIT])
def test_strict_schema_is_strict_everywhere_and_leaves_the_original_alone(schema):
    before = copy.deepcopy(schema)
    assert_strict(codex.strict_schema(schema))
    assert schema == before


def test_optional_properties_become_nullable_and_required_ones_keep_their_shape():
    props = codex.strict_schema(REPLY_OPUS)["properties"]
    assert props["target"] == {"type": "string", "enum": ["thread", "team_channel"]}
    assert props["school"] == {"type": ["string", "null"], "description": "the school named, or empty"}
    assert props["precedent_terms"]["type"] == ["array", "null"] and props["precedent_terms"]["items"] == {"type": "string"}
    row = codex.strict_schema(NOOP_AUDIT)["properties"]["overturned"]["items"]
    assert row["properties"]["class"]["enum"] == ["TASK", "INCIDENT", "IDEA", "OPEN_LOOP", "REPLY"]
    assert row["required"] == ["event_id", "class", "reason"]


def test_strict_schema_drops_what_the_api_rejects_and_makes_optional_enums_and_objects_nullable():
    loose = {"type": "object", "title": "T", "required": ["a"], "properties": {
        "a": {"type": "string", "minLength": 2, "default": "x"},
        "mood": {"type": "string", "enum": ["calm", "tense"]},
        "tags": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "already": {"type": ["integer", "null"]},
        "extra": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}, "m": {"type": "string"}}},
        "either": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
    strict = codex.strict_schema(loose)
    assert "title" not in strict and strict["properties"]["a"] == {"type": "string"}
    assert strict["properties"]["mood"] == {"type": ["string", "null"], "enum": ["calm", "tense", None]}
    assert strict["properties"]["tags"]["maxItems"] == 3
    assert strict["properties"]["already"] == {"type": ["integer", "null"]}
    assert strict["properties"]["extra"]["type"] == ["object", "null"]
    assert strict["properties"]["extra"]["properties"]["m"] == {"type": ["string", "null"]}
    assert strict["properties"]["either"] == {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    answer = {"a": "ok", "mood": None, "tags": None, "already": None, "extra": {"n": 1, "m": None}, "either": None}
    assert check_schema(answer, strict) == []
    assert codex.unstrict(answer, loose) == {"a": "ok", "extra": {"n": 1}}


@pytest.mark.parametrize("schema,answer,loose", [
    (REPLY_OPUS,
     {"needs_reply": True, "why": "asks him", "draft": "voy", "needs_facts": False, "checks": [], "prod_problem": False,
      "someone_else_owns": False, "target": "thread", "report": "", "ops_request": None, "school": None, "open_problem": None,
      "precedent_terms": ["eliminar clases masivo"], "board_line": None},
     {"needs_reply": True, "why": "asks him", "draft": "voy", "needs_facts": False, "checks": [], "prod_problem": False,
      "someone_else_owns": False, "target": "thread", "report": "", "precedent_terms": ["eliminar clases masivo"]}),
    (SEMANTIC, {"same_as": 0, "why": "new"}, {"same_as": 0, "why": "new"}),
    (NOOP_AUDIT, {"overturned": [{"event_id": 7, "class": "TASK", "reason": "asks for a fix"}]},
     {"overturned": [{"event_id": 7, "class": "TASK", "reason": "asks for a fix"}]}),
])
def test_a_strict_answer_round_trips_to_what_the_loose_schema_would_have_given(schema, answer, loose):
    assert check_schema(answer, codex.strict_schema(schema)) == []
    back = codex.unstrict(answer, schema)
    assert back == loose and check_schema(back, schema) == []


def test_unstrict_keeps_a_required_null_for_the_validator_to_reject():
    back = codex.unstrict({"same_as": None, "why": "x"}, SEMANTIC)
    assert back == {"same_as": None, "why": "x"} and check_schema(back, SEMANTIC)


def test_toml_string_survives_every_quote_backslash_and_control_character():
    quoted = codex.toml_string(NASTY)
    assert "\n" not in quoted and "\x7f" not in quoted
    assert tomllib.loads(f"v = {quoted}")["v"] == NASTY


def test_auth_args_for_the_three_modes():
    # the user's own login runs in the user's own Codex home; a key or a ChatGPT token runs in Clea's
    assert codex.auth_args("subscription") == ([], {"CODEX_API_KEY": "", "OPENAI_API_KEY": ""})
    home = codex.own_home()
    assert Path(home).is_dir() and Path(home) != Path.home() / ".codex"
    assert codex.auth_args("api_key", "sk-test") == ([], {"CODEX_API_KEY": "sk-test", "OPENAI_API_KEY": "", "CODEX_HOME": home})
    over, env = codex.auth_args("siwc", "tok")
    assert env == {"ACCESS_TOKEN": "tok", "CODEX_API_KEY": "", "OPENAI_API_KEY": "", "CODEX_HOME": home}
    assert over[::2] == ["-c"] * 7
    conf = tomllib.loads("\n".join(over[1::2]))
    assert conf == {"model_provider": "openai_chatgpt_plan", "model_providers": {"openai_chatgpt_plan": {
        "name": "ChatGPT plan", "base_url": "https://api.openai.com/v1", "env_key": "ACCESS_TOKEN", "wire_api": "responses",
        "requires_openai_auth": False, "supports_websockets": False}}}


@pytest.mark.parametrize("auth,token", [("api_key", None), ("siwc", ""), ("oauth", "tok")])
def test_auth_args_refuses_a_missing_token_and_an_unknown_mode(auth, token):
    with pytest.raises(ValueError):
        codex.auth_args(auth, token)


def test_parse_events_success_schema_and_code_runs():
    ev = codex.parse_events(SUCCESS)
    assert ev["session_id"] == "01a1c231-bcb4-7ae3-bd38-5cb6bac49275" and ev["message"] == "ok" and ev["completed"]
    assert ev["usage"]["input_tokens"] == 18292 and ev["steps"] == 1 and not ev["errors"] and not ev["failed"]
    assert codex.parse_events(SCHEMA_RUN)["message"] == SCHEMA_ANSWER
    code = codex.parse_events(CODE_RUN)
    assert code["steps"] == 4 and code["message"].endswith("DONE") and code["completed"]


def test_parse_events_failure_and_a_stream_cut_short():
    ev = codex.parse_events(BAD_MODEL)
    assert not ev["completed"] and ev["session_id"] == "01a1c23b-9ef9-7e70-882a-5085cf1ddad9"
    assert ev["failed"] == "The 'gpt-0-nonexistent' model is not supported when using Codex with a ChatGPT account."
    assert len(ev["errors"]) == 2 and ev["errors"][0].startswith("Model metadata")
    cut = codex.parse_events(CODE_RUN.split('{"type":"item.completed","item":{"id":"item_5"')[0] + '{"type":"item.comp')
    assert not cut["completed"] and cut["session_id"] == "01a1c23e-c89b-71b1-bc3e-7cb900c94ca8" and cut["steps"] == 2


def test_usage_splits_cached_input_and_a_resumed_turn_is_charged_its_own_tokens():
    first = codex.parse_events(SCHEMA_RUN)["usage"]
    assert codex.usage_of(codex.parse_events(SUCCESS)["usage"]) == {
        "input_tokens": 6772, "cache_read_input_tokens": 11520, "cache_creation_input_tokens": 0, "output_tokens": 5}
    assert codex.usage_of(codex.parse_events(RESUMED)["usage"], first) == {
        "input_tokens": 218, "cache_read_input_tokens": 13824, "cache_creation_input_tokens": 0, "output_tokens": 50}


def test_cost_is_tokens_times_the_configured_price_and_zero_for_an_unpriced_model(monkeypatch):
    monkeypatch.setitem(codex.CFG, "openai", {"usd_per_mtok": {"gpt-6-luna": [0.1, 0.5], "gpt-6.1-sol": [2, 10]}})
    usage = {"input_tokens": 6772, "cache_read_input_tokens": 11520, "cache_creation_input_tokens": 0, "output_tokens": 5}
    assert codex.cost_usd("gpt-6-luna", usage) == pytest.approx((6772 * 0.1 + 11520 * 0.01 + 5 * 0.5) / 1e6, abs=1e-6)
    million = {"input_tokens": 10**6, "cache_read_input_tokens": 10**6, "cache_creation_input_tokens": 0, "output_tokens": 10**6}
    assert codex.cost_usd("gpt-6.1-sol", million) == pytest.approx(2 + 0.2 + 10)
    assert codex.cost_usd("gpt-9-unknown", million) == 0.0


def test_thread_usage_reads_the_last_count_of_the_rollout(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sid = "01a1c239-7099-7410-9d51-a7a9cfd3b214"
    assert codex.thread_usage(sid) == {}
    day = tmp_path / "sessions" / "2026" / "11" / "09"
    day.mkdir(parents=True)
    (day / f"rollout-2026-11-09T18-31-47-{sid}.jsonl").write_text(
        '{"type":"session_meta","payload":{"id":"x"}}\n' + ROLLOUT_COUNT.replace("13978", "1") + "\n" + ROLLOUT_COUNT + "\n"
        + '{"type":"event_msg","payload":{"type":"task_complete"}}\n')
    assert codex.thread_usage(sid)["input_tokens"] == 13978 and codex.thread_usage(sid)["output_tokens"] == 50


@pytest.mark.parametrize("text,kind,reset_at,model", [
    (PLAN_LIMIT, "usage", dt.datetime(2026, 11, 9, 15, 45).timestamp() + 60, None),
    (MODEL_LIMIT, "usage", dt.datetime(2026, 11, 12, 9, 5).timestamp() + 60, "gpt-6.1-sol"),
    ("You've hit your usage limit. Try again later.", "usage", None, None),
    ("Your workspace is out of credits. Add credits to continue.", "usage", None, None),
    ("You hit your spend cap set in your workspace. Increase your spend cap to continue.", "usage", None, None),
    ("Quota exceeded. Check your plan and billing details.", "usage", None, None),
    (SIWC_LIMIT, "usage", None, None),
    (RATE_LIMIT, "rate", NOW.timestamp() + 2, None),
    ("exceeded retry limit, last status: 429 Too Many Requests, request id: req_1", "rate", None, None),
    ('{"error":{"type":"usage_limit_reached","resets_at":1794779578}}', "usage", 1794779578, None),
    ('{"error":{"type":"usage_limit_reached","resets_in_seconds":3600}}', "usage", NOW.timestamp() + 3600, None),
    ("Rate limit reached. Please try again in 6m0s.", "rate", NOW.timestamp() + 361, None),
    ("You've hit your usage limit. Try again in 2 hours 31 minutes.", "usage", NOW.timestamp() + 9061, None),
])
def test_limits_are_told_apart_and_dated(text, kind, reset_at, model):
    limit = codex.limit_of(text, NOW)
    assert (limit["kind"], limit["reset_at"], limit["model"]) == (kind, reset_at, model)
    assert limit["text"] == text[:300]


def test_a_clock_time_already_past_means_tomorrow():
    late = dt.datetime(2026, 11, 9, 16, 0, 0)
    assert codex.limit_of(PLAN_LIMIT, late)["reset_at"] == dt.datetime(2026, 11, 10, 15, 45).timestamp() + 60


@pytest.mark.parametrize("text", [BAD_KEY_MESSAGE, BAD_SIWC_MESSAGE, "Not logged in",
                                  "Your access token could not be refreshed because your refresh token has expired. "
                                  "Please log out and sign in again."])
def test_auth_failures_are_recognised_and_are_not_limits(text):
    assert codex.auth_failed(text) and codex.limit_of(text) is None


@pytest.mark.parametrize("text", [NO_SESSION, "The 'gpt-0-nonexistent' model is not supported when using Codex with a ChatGPT account.",
                                  "stream disconnected before completion: error sending request"])
def test_plain_failures_are_neither_auth_nor_limit(text):
    assert not codex.auth_failed(text) and codex.limit_of(text) is None


def fake_run(stdout: str = "", last: str | None = None, rc: int = 0, stderr: str = "", raises: Exception | None = None):
    """Stands in for core.run: records the call, writes the last-message file like the CLI does, returns the stream."""
    calls = []

    def run(cmd, cwd=None, timeout=600, input=None, extra_env=None, tag=None):
        schema = json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text()) if "--output-schema" in cmd else None
        calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout, "input": input, "env": extra_env, "tag": tag, "schema": schema})
        if raises:
            raise raises
        if last is not None:
            Path(cmd[cmd.index("-o") + 1]).write_text(last)
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    return run, calls


def overrides(cmd: list[str]) -> dict:
    """The -c overrides of a command, parsed the way Codex parses them."""
    return tomllib.loads("\n".join(cmd[i + 1] for i, a in enumerate(cmd) if a == "-c"))


def test_invoke_read_only_schema_call(monkeypatch, tmp_path):
    run, calls = fake_run(SCHEMA_RUN, last=SCHEMA_ANSWER)
    monkeypatch.setattr(codex, "run", run)
    monkeypatch.setitem(codex.CFG, "openai", {"usd_per_mtok": {"gpt-6-luna": [0.1, 0.5]}})
    schema = {"type": "object", "required": ["passphrase", "count"], "properties": {
        "passphrase": {"type": "string"}, "count": {"type": "integer"}, "mood": {"type": "string", "enum": ["calm", "tense"]},
        "note": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
        "precedent": {"type": ["object", "null"], "properties": {"request": {"type": "string"}}},
        "extra": {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}}}
    out = codex.invoke("What is the passphrase?", model="gpt-6-luna", cwd=str(tmp_path), system=NASTY, schema=schema, effort="low",
                       tag={"item": 7, "role": "verify"})
    assert out == {"result": SCHEMA_ANSWER, "structured_output": {"passphrase": "q\"u'o\\te-7731", "count": 3},
                   "session_id": "01a1c239-7099-7410-9d51-a7a9cfd3b214", "is_error": False, "subtype": "success",
                   "usage": {"input_tokens": 13978, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 50},
                   "total_cost_usd": pytest.approx(0.001423), "num_turns": 1, "limit": None}
    call = calls[0]
    cmd = call["cmd"]
    assert cmd[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"] and cmd[-1] == "-" and "resume" not in cmd
    assert all(flag in cmd for flag in codex.CLEAN_ROOM) and cmd[cmd.index("-m") + 1] == "gpt-6-luna"
    assert not any("danger" in a or a in ("-s", "--sandbox", "--ephemeral") for a in cmd)
    conf = overrides(cmd)
    assert conf["sandbox_mode"] == "read-only" and conf["approval_policy"] == "never" and conf["model_reasoning_effort"] == "low"
    assert conf["developer_instructions"] == NASTY and "sandbox_workspace_write" not in conf
    assert call["input"] == "What is the passphrase?" and call["cwd"] == str(tmp_path)
    assert call["env"] == {"CODEX_API_KEY": "", "OPENAI_API_KEY": ""} and call["tag"] == {"item": 7, "role": "verify"}
    assert_strict(call["schema"])
    assert not Path(cmd[cmd.index("-o") + 1]).parent.exists()


def test_invoke_keeps_the_raw_text_when_the_answer_does_not_fit(monkeypatch, tmp_path):
    run, _ = fake_run(SUCCESS, last="ok")
    monkeypatch.setattr(codex, "run", run)
    out = codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path), schema=SEMANTIC)
    assert out["structured_output"] is None and out["result"] == "ok" and not out["is_error"]
    run, _ = fake_run(SUCCESS, last='{"same_as": "seven", "why": "x"}')
    monkeypatch.setattr(codex, "run", run)
    assert codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path), schema=SEMANTIC)["structured_output"] is None


def test_invoke_code_session_resumed_with_network_and_extra_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sid = "01a1c239-7099-7410-9d51-a7a9cfd3b214"
    day = tmp_path / "sessions" / "2026" / "11" / "09"
    day.mkdir(parents=True)
    (day / f"rollout-2026-11-09T18-31-47-{sid}.jsonl").write_text(ROLLOUT_COUNT + "\n")
    run, calls = fake_run(RESUMED, last="done")
    monkeypatch.setattr(codex, "run", run)
    out = codex.invoke("continue", model="gpt-6.1-sol", cwd=str(tmp_path), sandbox="workspace-write", network=True,
                       add_dirs=("/repos/a b", '/repos/q"uote'), resume=sid, timeout=5400)
    cmd = calls[0]["cmd"]
    assert cmd[:3] == ["codex", "exec", "resume"] and cmd[-2:] == [sid, "-"] and calls[0]["timeout"] == 5400
    assert not any(a in ("-C", "--cd", "-s", "--add-dir") for a in cmd)
    conf = overrides(cmd)
    assert conf["sandbox_mode"] == "workspace-write" and conf["approval_policy"] == "never"
    assert conf["sandbox_workspace_write"] == {"network_access": True, "writable_roots": ["/repos/a b", '/repos/q"uote']}
    assert out["session_id"] == sid and out["result"] == "done"
    assert out["usage"] == {"input_tokens": 218, "cache_read_input_tokens": 13824, "cache_creation_input_tokens": 0, "output_tokens": 50}


def test_invoke_workspace_write_has_no_network_unless_asked_and_full_access_is_refused(monkeypatch, tmp_path):
    run, calls = fake_run(CODE_RUN, last="x")
    monkeypatch.setattr(codex, "run", run)
    out = codex.invoke("build", model="gpt-6.1-sol", cwd=str(tmp_path), sandbox="workspace-write", ephemeral=True)
    assert overrides(calls[0]["cmd"])["sandbox_workspace_write"] == {"network_access": False}
    assert "--ephemeral" in calls[0]["cmd"] and out["num_turns"] == 4
    with pytest.raises(ValueError):
        codex.invoke("build", model="gpt-6.1-sol", cwd=str(tmp_path), sandbox="danger-full-access")
    assert len(calls) == 1


def test_invoke_sends_an_oversized_system_prompt_on_stdin(monkeypatch, tmp_path):
    run, calls = fake_run(SUCCESS, last="ok")
    monkeypatch.setattr(codex, "run", run)
    big = "rule \"x\"\n" * 20000
    codex.invoke("do it", model="gpt-6-luna", cwd=str(tmp_path), system=big)
    assert "developer_instructions" not in overrides(calls[0]["cmd"])
    assert max(len(a.encode()) for a in calls[0]["cmd"]) < codex.MAX_ARG_BYTES
    assert calls[0]["input"].startswith("<system_instructions>\n" + big) and calls[0]["input"].endswith("\n\ndo it")


@pytest.mark.parametrize("stdout,stderr,rc,result,subtype,sid", [
    (BAD_MODEL, "", 1, "The 'gpt-0-nonexistent' model is not supported when using Codex with a ChatGPT account.", "error",
     "01a1c23b-9ef9-7e70-882a-5085cf1ddad9"),
    (BAD_KEY, "2026-11-09T21:34:39Z ERROR codex_models_manager::manager: failed to refresh available models", 1, BAD_KEY_MESSAGE,
     "error_auth", "01a1c23c-1452-77f3-b6e5-9cc152d87b58"),
    ("", NO_SESSION, 1, NO_SESSION.strip(), "error", None),
    (CODE_RUN.split('{"type":"item.completed","item":{"id":"item_5"')[0], "", -9, "killed", "error",
     "01a1c23e-c89b-71b1-bc3e-7cb900c94ca8"),
    ("", "", 137, "killed", "error", None),
    ("", "", 1, "codex exited 1 with no output", "error", None),
])
def test_invoke_failures_carry_the_reason_and_the_session(monkeypatch, tmp_path, stdout, stderr, rc, result, subtype, sid):
    run, _ = fake_run(stdout, rc=rc, stderr=stderr)
    monkeypatch.setattr(codex, "run", run)
    out = codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path), schema=SEMANTIC)
    assert (out["is_error"], out["subtype"], out["result"], out["session_id"]) == (True, subtype, result, sid)
    assert out["structured_output"] is None and out["limit"] is None and out["total_cost_usd"] == 0.0
    assert out["usage"] == {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}


def test_invoke_reports_a_plan_limit_with_its_reset(monkeypatch, tmp_path):
    stream = "\n".join(json.dumps(e) for e in [{"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
                                               {"type": "error", "message": MODEL_LIMIT},
                                               {"type": "turn.failed", "error": {"message": MODEL_LIMIT}}])
    run, _ = fake_run(stream, rc=1)
    monkeypatch.setattr(codex, "run", run)
    out = codex.invoke("x", model="gpt-6.1-sol", cwd=str(tmp_path))
    assert out["is_error"] and out["subtype"] == "error_limit" and out["result"] == MODEL_LIMIT and out["session_id"] == "t-1"
    assert out["limit"] == {"kind": "usage", "reset_at": dt.datetime(2026, 11, 12, 9, 5).timestamp() + 60, "text": MODEL_LIMIT,
                            "model": "gpt-6.1-sol"}


def test_invoke_lets_a_timeout_through_with_the_session_it_printed(monkeypatch, tmp_path):
    partial = SUCCESS.splitlines()[0].encode() + b"\n"
    run, _ = fake_run(raises=subprocess.TimeoutExpired(["codex"], 900, output=partial))
    monkeypatch.setattr(codex, "run", run)
    with pytest.raises(subprocess.TimeoutExpired) as e:
        codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path))
    assert e.value.session_id == "01a1c231-bcb4-7ae3-bd38-5cb6bac49275"


def test_invoke_never_raises_when_codex_cannot_start_or_a_token_is_missing(monkeypatch, tmp_path):
    run, calls = fake_run(raises=FileNotFoundError(2, "No such file or directory", "codex"))
    monkeypatch.setattr(codex, "run", run)
    out = codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path))
    assert out["is_error"] and out["subtype"] == "error" and out["result"].startswith("codex could not start")
    out = codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path), auth="siwc")
    assert out["is_error"] and out["subtype"] == "error_auth" and len(calls) == 1


def test_invoke_passes_each_auth_mode_to_the_child(monkeypatch, tmp_path):
    run, calls = fake_run(SUCCESS, last="ok")
    monkeypatch.setattr(codex, "run", run)
    codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path), auth="api_key", token="sk-test")
    codex.invoke("x", model="gpt-6-luna", cwd=str(tmp_path), auth="siwc", token="tok")
    assert calls[0]["env"] == {"CODEX_API_KEY": "sk-test", "OPENAI_API_KEY": "", "CODEX_HOME": codex.own_home()}
    assert "model_provider" not in overrides(calls[0]["cmd"])
    assert calls[1]["env"]["ACCESS_TOKEN"] == "tok" and overrides(calls[1]["cmd"])["model_provider"] == "openai_chatgpt_plan"
    assert "tok" not in " ".join(calls[1]["cmd"]) and "sk-test" not in " ".join(calls[0]["cmd"])


def test_status_reads_version_and_login_without_a_model_call(monkeypatch):
    def cli(rc, out, err):
        def run(cmd, timeout=600, **kw):
            return subprocess.CompletedProcess(cmd, *((0, "codex-cli 0.166.0\n", "") if cmd[1:] == ["--version"] else (rc, out, err)))
        return run
    monkeypatch.setattr(codex, "run", cli(0, "", "Logged in using ChatGPT\n"))
    assert codex.status() == {"installed": True, "version": "0.166.0", "logged_in": True, "method": "chatgpt",
                              "detail": "Logged in using ChatGPT"}
    monkeypatch.setattr(codex, "run", cli(0, "API key configured - sk-fake-***-0000\nAuthentication is checked when you send a request.\n", ""))
    assert codex.status()["method"] == "api_key" and codex.status()["detail"] == "API key configured - sk-fake-***-0000"
    monkeypatch.setattr(codex, "run", cli(1, "", "Not logged in\n"))
    assert codex.status() == {"installed": True, "version": "0.166.0", "logged_in": False, "method": None, "detail": "Not logged in"}

    def missing(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", "codex")
    monkeypatch.setattr(codex, "run", missing)
    assert codex.status()["installed"] is False and codex.status()["logged_in"] is False


@pytest.mark.skipif(os.environ.get("CLEA_LIVE_CODEX") != "1", reason="live Codex call: set CLEA_LIVE_CODEX=1")
def test_live_schema_call_and_resume(tmp_path):
    model = os.environ.get("CLEA_LIVE_CODEX_MODEL", "gpt-6-luna")
    schema = {"type": "object", "required": ["word"], "properties": {"word": {"type": "string"}, "note": {"type": "string"}}}
    first = codex.invoke("Reply with the code word.", model=model, cwd=str(tmp_path), schema=schema, effort="low", timeout=300,
                         system="The code word is 'heliotrope'. Give it when asked, in lowercase. Leave note out.")
    assert not first["is_error"], first["result"]
    assert first["structured_output"]["word"] == "heliotrope" and first["session_id"] and first["usage"]["output_tokens"] > 0
    second = codex.invoke("Which code word did you just give me? Answer the same way.", model=model, cwd=str(tmp_path), schema=schema,
                          effort="low", timeout=300, resume=first["session_id"])
    assert not second["is_error"], second["result"]
    assert second["structured_output"]["word"] == "heliotrope" and second["session_id"] == first["session_id"]
    spent = [u["input_tokens"] + u["cache_read_input_tokens"] for u in (first["usage"], second["usage"])]
    assert spent[1] < 1.5 * spent[0]  # its own turn, not the thread's running total
