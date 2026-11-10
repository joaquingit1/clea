"""The OpenAI engine: the headless model call of ap/claude.py, run on the Codex CLI (`codex exec`).

A user of the public release may bring an OpenAI account instead of a Claude one. This module is only the engine: one
turn loop in, the dict the harness already reads from `claude -p --output-format json` out. Budgets, the calls ledger,
resume bookkeeping and model fallbacks stay with the caller, so nothing here touches the database.

What differs from the Claude CLI and shaped this file (each point observed on codex-cli 0.166.0):
  - events stream as JSONL while the session runs, so a killed or timed-out run still names its session;
  - a schema must be OpenAI-strict (every property required, no extra keys): strict_schema on the way in, unstrict on
    the way out, and the caller's loose schema stays the one that validates;
  - usage is the thread's running total, not the turn's, and there is no dollar figure: a resumed turn subtracts what
    the thread had spent before it, and cost is tokens times config openai.usd_per_mtok;
  - the user's own Codex setup (config.toml with its MCP servers and plugins, command rules, skills, hooks) would ride
    along in every call: CLEAN_ROOM leaves it out while the saved login in the default CODEX_HOME still works.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .core import CFG, check_schema, log, run

# Sign in with ChatGPT: the app holds the user's OAuth token and Codex sends it as the bearer of a provider of our own
# on the public Responses API (the shape OpenAI documents for app-server; `codex exec` takes the same overrides)
SIWC_PROVIDER = "openai_chatgpt_plan"
SIWC = {"name": "ChatGPT plan", "base_url": "https://api.openai.com/v1", "env_key": "ACCESS_TOKEN", "wire_api": "responses",
        "requires_openai_auth": False, "supports_websockets": False}

# Nothing of the user's personal Codex loads in a Clea session, the repo's own rules do. --ignore-user-config drops
# ~/.codex/config.toml (MCP servers, plugins, notify, trusted projects) and keeps the login. With only that flag a
# call still carried a 20 KB listing of the user's personal and plugin skills; the rest keeps those, the user's
# command rules, plugins, connectors and hooks out as well.
# The fallback name makes a repo written for Claude Code count: its CLAUDE.md loads where it has no AGENTS.md.
# Not droppable: ~/.codex/AGENTS.md, the user's standing instructions to Codex (what ~/.claude/CLAUDE.md is to the
# other engine). project_doc_max_bytes=0 only drops the repo's files, and nothing else leaves the global one out.
CLEAN_ROOM = ["--ignore-user-config", "--ignore-rules", "--disable", "plugins", "--disable", "apps", "--disable", "hooks",
              "-c", "skills.include_instructions=false", "-c", "skills.bundled.enabled=false",
              "-c", 'project_doc_fallback_filenames=["CLAUDE.md"]']

# an argument this long is still safe on Linux (131072 bytes each) and far under macOS's 1 MB for all of them together
MAX_ARG_BYTES = 100_000
REASON_CHARS = 800
COUNTS = ("input_tokens", "cached_input_tokens", "output_tokens")

# schema keywords OpenAI structured outputs accept next to type/properties/required/items/anyOf; the rest are dropped
KEEP = ("description", "enum", "const", "pattern", "format", "multipleOf", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "minItems", "maxItems", "$ref")

# the CLI's own wording (the message templates inside the 0.166.0 binary) and the API's error codes
USAGE_MARKS = ("usage limit", "usage_limit", "quota", "spend cap", "out of credits")
RATE_MARKS = ("rate limit", "rate_limit", "too many requests")
AUTH_MARKS = ("unauthorized", "not logged in", "not signed in", "invalid_api_key", "incorrect api key", "token_expired",
              "refresh token", "refresh_token", "could not be refreshed", "invalid_grant", "sign in again", "invalid_user")


def status() -> dict:
    """{"installed", "version", "logged_in", "method": "chatgpt" | "api_key" | None, "detail"}. Zero tokens: the CLI's
    own `--version` and `login status` (exit 0 when credentials are saved; the text goes to stderr). A key that is
    only in the environment is not a login. Never raises."""
    out = {"installed": False, "version": "", "logged_in": False, "method": None, "detail": ""}
    try:
        v = run(["codex", "--version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        out["detail"] = f"codex CLI not available: {e}"[:200]
        return out
    if v.returncode != 0:
        out["detail"] = (v.stderr or v.stdout).strip()[:200]
        return out
    out["installed"], out["version"] = True, (v.stdout.split() or [""])[-1]
    try:
        s = run(["codex", "login", "status"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        out["detail"] = f"codex login status failed: {e}"[:200]
        return out
    text = (s.stdout + "\n" + s.stderr).strip()
    out["detail"] = text.splitlines()[0][:200] if text else ""
    if s.returncode == 0:
        low = text.lower()
        out["logged_in"] = True
        out["method"] = "chatgpt" if "chatgpt" in low else "api_key" if "api key" in low else None
    return out


def toml_string(text: str) -> str:
    """`text` as a one-line TOML basic string, the value of a `-c key=value` override. Codex parses the value as TOML
    and, when that fails, silently keeps the raw argument: a system prompt with one stray quote would arrive wrapped
    in our own quoting. JSON's escapes are TOML's, except that TOML also forbids a raw DEL."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def auth_args(auth: str, token: str | None = None) -> tuple[list[str], dict]:
    """(-c overrides, env) for one run.
    subscription: the user's own `codex login`. No overrides; CODEX_API_KEY and OPENAI_API_KEY are blanked in the
                  child, so a key left in the shell never bills the API by surprise (run() can only add to the
                  environment, and Codex reads a blank key as none).
    api_key:      CODEX_API_KEY=token, for this run only.
    siwc:         a Sign in with ChatGPT access token, the bearer of our own Responses provider."""
    if auth == "subscription":
        return [], {"CODEX_API_KEY": "", "OPENAI_API_KEY": ""}
    if auth not in ("api_key", "siwc"):
        raise ValueError(f"unknown codex auth '{auth}'")
    if not token:  # a blank key would fall through to the saved login and spend the user's plan unasked
        raise ValueError(f"codex auth '{auth}' needs a token")
    # these two bring their own credential, so nothing of the user's own Codex is needed: a home of Clea's keeps their
    # standing instructions (~/.codex/AGENTS.md cannot be switched off any other way) and their history out, and a
    # saved login there is impossible to fall back to
    home = own_home()
    if auth == "api_key":
        return [], {"CODEX_API_KEY": token, "OPENAI_API_KEY": "", "CODEX_HOME": home}
    over = ["-c", f"model_provider={toml_string(SIWC_PROVIDER)}"]
    for k, v in SIWC.items():
        over += ["-c", f"model_providers.{SIWC_PROVIDER}.{k}={json.dumps(v)}"]
    return over, {"ACCESS_TOKEN": token, "CODEX_API_KEY": "", "OPENAI_API_KEY": "", "CODEX_HOME": home}


def own_home() -> str:
    """Clea's own CODEX_HOME inside the install: sessions it can resume, and nothing personal."""
    from .core import DATA
    home = DATA / "codex"
    home.mkdir(parents=True, exist_ok=True)
    return str(home)


def _nullable(node: dict) -> dict:
    """An optional property, said the only way a strict schema can: required, and null when there is nothing."""
    t = node.get("type")
    if t is None or "const" in node:
        alts = node["anyOf"] if set(node) == {"anyOf"} else [node]
        return {"anyOf": alts if {"type": "null"} in alts else [*alts, {"type": "null"}]}
    types = t if isinstance(t, list) else [t]
    out = {**node, "type": types if "null" in types else [*types, "null"]}
    if "enum" in node and None not in node["enum"]:
        out["enum"] = [*node["enum"], None]
    return out


def strict_schema(schema: dict) -> dict:
    """The harness's JSON schemas are loose (optional properties, no additionalProperties). OpenAI structured outputs
    need every object to list all its properties as required with additionalProperties false. A strict copy: optional
    properties become nullable and required; enums, arrays, nested objects and descriptions survive; keywords the API
    rejects (default, title, minLength, allOf...) are dropped. An object with no properties can only come back empty."""
    out = {k: schema[k] for k in KEEP if k in schema}
    t = schema.get("type")
    if t is not None:
        out["type"] = list(t) if isinstance(t, list) else t
    for key in ("anyOf", "oneOf"):
        if key in schema:
            out["anyOf"] = [strict_schema(s) for s in schema[key]]
    if "$defs" in schema:
        out["$defs"] = {k: strict_schema(v) for k, v in schema["$defs"].items()}
    if "properties" in schema or t == "object" or (isinstance(t, list) and "object" in t):
        required = set(schema.get("required") or [])
        props = {k: strict_schema(v) for k, v in (schema.get("properties") or {}).items()}
        out["properties"] = {k: v if k in required else _nullable(v) for k, v in props.items()}
        out["required"] = list(props)
        out["additionalProperties"] = False
    if isinstance(schema.get("items"), dict):
        out["items"] = strict_schema(schema["items"])
    return out


def unstrict(obj, schema: dict):
    """Undo strict_schema on a parsed answer: drop keys that came back null where the original schema did not require
    them, recursively, so callers see what they would have seen from a loose schema."""
    for alt in schema.get("anyOf") or schema.get("oneOf") or []:
        if not check_schema(unstrict(obj, alt), alt):
            return unstrict(obj, alt)
    if isinstance(obj, dict):
        props, required = schema.get("properties") or {}, set(schema.get("required") or [])
        return {k: unstrict(v, props.get(k) or {}) for k, v in obj.items() if v is not None or k in required}
    if isinstance(obj, list):
        return [unstrict(el, schema.get("items") or {}) for el in obj]
    return obj


def parse_events(stdout: str) -> dict:
    """What a `codex exec --json` stream says, complete or cut short: the session, the last assistant message, the
    thread's token totals, how many steps the model took (messages and tool calls), the CLI's errors in order (its
    retries and warnings included) and the reason of a failed turn. `completed` is the turn.completed event: a stream
    without one failed, was killed or timed out."""
    out = {"session_id": None, "message": "", "usage": {}, "steps": 0, "errors": [], "failed": "", "completed": False}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind, item = ev.get("type"), ev.get("item") or {}
        if kind == "thread.started":
            out["session_id"] = ev.get("thread_id")
        elif kind == "item.completed" and item.get("type") == "agent_message":
            out["message"], out["steps"] = item.get("text") or "", out["steps"] + 1
        elif kind == "item.completed" and item.get("type") == "error":
            out["errors"].append(str(item.get("message") or ""))
        elif kind == "item.completed" and item.get("type") != "reasoning":
            out["steps"] += 1
        elif kind == "turn.completed":
            out["usage"], out["completed"] = ev.get("usage") or {}, True
        elif kind == "turn.failed":
            out["failed"] = str((ev.get("error") or {}).get("message") or "")
        elif kind == "error":
            out["errors"].append(str(ev.get("message") or ""))
    return out


def thread_usage(session_id: str, home: str | None = None) -> dict:
    """The token totals a thread had run up, from the last count in its rollout ({} when there is none to read).
    Codex reports usage per thread: the resume of a 13,978-token turn reported 28,020 input tokens for a turn that
    used 14,042, so a resumed call is charged its total minus this."""
    home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        for f in (home / "sessions").glob(f"*/*/*/rollout-*{session_id}.jsonl"):
            for line in reversed(f.read_text(errors="replace").splitlines()):
                if '"token_count"' in line:
                    info = (json.loads(line).get("payload") or {}).get("info") or {}
                    return info.get("total_token_usage") or {}
    except (OSError, ValueError, AttributeError) as e:
        log("codex rollout unreadable, resumed usage counts the whole thread", sid=session_id[:8], err=str(e)[:120])
    return {}


def usage_of(total: dict, before: dict | None = None) -> dict:
    """One call's tokens in the shape the harness records, from the thread's totals after it and before it. OpenAI
    counts cached input inside input_tokens and the harness adds the fresh and the cached figures, so they are split
    here. Reasoning is already inside output_tokens."""
    spent = {k: max(0, int(total.get(k) or 0) - int((before or {}).get(k) or 0)) for k in COUNTS}
    cached = min(spent["cached_input_tokens"], spent["input_tokens"])
    return {"input_tokens": spent["input_tokens"] - cached, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0, "output_tokens": spent["output_tokens"]}


def cost_usd(model: str, usage: dict) -> float:
    """Dollars for one call at API prices (config openai.usd_per_mtok: model -> [input, output] per million tokens,
    cached input at a tenth of input). On a ChatGPT plan nothing is billed per token: the figure is what the budget
    pools and the ledger compare against. A model with no price costs 0."""
    price = ((CFG.get("openai") or {}).get("usd_per_mtok") or {}).get(model)
    if not price:
        return 0.0
    tokens_in = usage["input_tokens"] + 0.1 * usage["cache_read_input_tokens"]
    return round((tokens_in * price[0] + usage["output_tokens"] * price[1]) / 1e6, 6)


def _reset_at(text: str, now: dt.datetime) -> int | None:
    """When a limit lifts, from what the error says: an epoch or a seconds count in the API's JSON, the API's 'try
    again in 1.2s' (or '6m0s', or '2 hours 31 minutes'), or the local clock time Codex prints ('try again at 3:45 PM',
    'at Nov 12th, 2026 9:05 AM' when it is not today). None when the text names no time: the caller backs off and probes."""
    m = re.search(r'"?resets?_at"?\s*[:=]\s*(\d{9,11})\b', text)
    if m:
        return int(m.group(1))
    m = re.search(r'"?resets?_in_seconds"?\s*[:=]\s*(\d+)', text)
    if m:
        return int(now.timestamp()) + int(m.group(1))
    m = re.search(r"again in\s*((?:\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?|ms|[dhms])[,\s]*)+)", text, re.I)
    if m:
        units = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}
        secs = sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|[dhms])", m.group(1).lower()))
        return int(now.timestamp() + secs) + 1
    m = re.search(r"again at (?:([a-z]{3})[a-z]* (\d{1,2})[a-z]*,? (\d{4}),? )?(\d{1,2}):(\d{2})\s*([ap]m)?", text, re.I)
    if not m:
        return None
    mon, day, year, hour, minute, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0) if ampm else int(hour)
    try:
        if mon:
            when = dt.datetime.strptime(f"{mon} {day} {year}", "%b %d %Y").replace(hour=hour, minute=int(minute))
        else:
            when = now.replace(hour=hour, minute=int(minute), second=0, microsecond=0)
            when += dt.timedelta(days=1) if when <= now else dt.timedelta()
    except ValueError:
        return None
    return int(when.timestamp()) + 60


def limit_of(text: str, now: dt.datetime | None = None) -> dict | None:
    """{"kind": "usage" | "rate", "reset_at", "text", "model"} when the CLI's error is a limit, else None. usage: the
    ChatGPT plan's allowance, a workspace's credits or spend cap, or an API account out of quota, which holds every
    call until it resets. rate: requests or tokens per minute (429), which clears by itself. `model` is set when the
    plan limit is that model's alone ('usage limit for gpt-6-astra. Switch to another model'): the others still answer.
    Only the CLI's own errors are read, never what the model wrote: a session that discusses limits is not limited."""
    low = text.lower()
    usage = any(w in low for w in USAGE_MARKS)
    if not usage and not any(w in low for w in RATE_MARKS) and not re.search(r"\b429\b", low):
        return None
    one = re.search(r"usage limit for (\S+?)\. Switch to another model", text)
    return {"kind": "usage" if usage else "rate", "reset_at": _reset_at(text, now or dt.datetime.now()), "text": text[:300],
            "model": one.group(1) if one else None}


def auth_failed(text: str) -> bool:
    """Not logged in, a rejected key or an expired or revoked token: nothing will work until the user signs in again."""
    low = text.lower()
    return bool(re.search(r"\b401\b", low)) or any(w in low for w in AUTH_MARKS)


def _command(model: str, system: str | None, sandbox: str, network: bool, add_dirs: tuple[str, ...], resume: str | None,
             effort: str | None, ephemeral: bool, auth_over: list[str], schema_file: Path | None, last_file: Path) -> list[str]:
    """The argv of one run. The prompt is not in it: it goes in on stdin ('-'), so its size and a leading dash never
    matter. `exec resume` takes neither -s, -C nor --add-dir, so the sandbox and its writable roots are config
    overrides in both cases and the working directory is the process's own."""
    if sandbox not in ("read-only", "workspace-write"):
        raise ValueError(f"codex sandbox '{sandbox}': only read-only and workspace-write are allowed")
    cmd = ["codex", "exec", *(["resume"] if resume else []), "--json", "--skip-git-repo-check", *CLEAN_ROOM,
           *(["--ephemeral"] if ephemeral else []), "-m", model,
           "-c", f'sandbox_mode="{sandbox}"', "-c", 'approval_policy="never"', *auth_over]
    if sandbox == "workspace-write":
        cmd += ["-c", f"sandbox_workspace_write.network_access={json.dumps(bool(network))}"]
        if add_dirs:
            cmd += ["-c", f"sandbox_workspace_write.writable_roots=[{', '.join(toml_string(str(d)) for d in add_dirs)}]"]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={toml_string(effort)}"]
    if system:
        cmd += ["-c", f"developer_instructions={toml_string(system)}"]
    if schema_file:
        cmd += ["--output-schema", str(schema_file)]
    return [*cmd, "-o", str(last_file), *([resume] if resume else []), "-"]


def _failure(ev: dict, p: subprocess.CompletedProcess) -> tuple[str, str, dict | None]:
    """(reason, subtype, limit) of a run whose turn did not complete. The reason is the CLI's last word: the failed
    turn's message, else its last error that is not a retry notice, else stderr (a resume of an unknown session
    prints nothing else). A SIGKILL before any of that (the reaper, or thread news) is 'killed': the session is on
    disk and the result still names it."""
    if p.returncode in (-9, 137) and not ev["failed"]:
        return "killed", "error", None
    final = [e for e in ev["errors"] if e and not e.startswith("Reconnecting")] or [e for e in ev["errors"] if e]
    reason = (ev["failed"] or (final[-1] if final else ""))[:REASON_CHARS] or p.stderr.strip()[-REASON_CHARS:] \
        or p.stdout.strip()[-REASON_CHARS:] or f"codex exited {p.returncode} with no output"
    retries = "\n".join(ev["errors"])  # read too: the last word of a run that gave up retrying need not repeat the status
    limit = limit_of(reason) or limit_of(retries)
    return reason, "error_limit" if limit else "error_auth" if auth_failed(reason) or auth_failed(retries) else "error", limit


def _structured(text: str, schema: dict):
    """The final message as the caller's loose schema would have had it, or None when it is not that."""
    try:
        obj = unstrict(json.loads(text), schema)
    except ValueError:
        return None
    errs = check_schema(obj, schema)
    if errs:
        log("codex answer does not fit its schema", errs=errs[:5])
    return None if errs else obj


def invoke(prompt: str, *, model: str, cwd: str, system: str | None = None, schema: dict | None = None,
           sandbox: str = "read-only", network: bool = False, add_dirs: tuple[str, ...] = (), resume: str | None = None,
           effort: str | None = None, timeout: int = 900, auth: str = "subscription", token: str | None = None,
           tag: dict | None = None, ephemeral: bool = False) -> dict:
    """One Codex turn loop, headless. Returns the shape the harness reads from `claude -p --output-format json`:
      {"result": str,                    # the final assistant message, or the CLI's reason when is_error
       "structured_output": obj | None,  # the parsed answer when a schema was given and the answer fits it
       "session_id": str | None,         # to resume this conversation later (also set on a failed or killed run)
       "is_error": bool, "subtype": "success" | "error" | "error_limit" | "error_auth",
       "usage": {"input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens": 0, "output_tokens"},
       "total_cost_usd": float, "num_turns": int,   # num_turns: the model's messages and tool calls
       "limit": None | {"kind": "usage" | "rate", "reset_at": int | None, "text": str, "model": str | None}}
    sandbox is read-only for judgment (the whole disk readable, no writes, no network) or workspace-write for code
    (cwd, add_dirs and temp writable; network only when asked); full access is refused. ephemeral leaves no session
    on disk, for calls nobody will resume: each one otherwise adds about 140 KB to the user's own Codex history.
    Never raises for a model or CLI failure. subprocess.TimeoutExpired propagates (the caller resumes), carrying the
    session id the run got as far as printing in its `session_id` attribute."""
    if auth in ("api_key", "siwc") and not token:
        return _result(parse_events(""), model, f"no {auth} token to run Codex with", "error_auth")
    auth_over, env = auth_args(auth, token)
    before = thread_usage(resume, env.get("CODEX_HOME")) if resume else {}
    tmp = Path(tempfile.mkdtemp(prefix="clea-codex-"))
    try:
        schema_file, last_file = (tmp / "schema.json") if schema else None, tmp / "last.txt"
        if schema_file:
            schema_file.write_text(json.dumps(strict_schema(schema)))
        if system and len(toml_string(system).encode()) > MAX_ARG_BYTES:
            # no file can carry developer instructions and keep Codex's own (model_instructions_file replaces them)
            prompt, system = f"<system_instructions>\n{system}\n</system_instructions>\n\n{prompt}", None
        cmd = _command(model, system, sandbox, network, add_dirs, resume, effort, ephemeral, auth_over, schema_file, last_file)
        try:
            p = run(cmd, cwd=cwd, timeout=timeout, input=prompt, extra_env=env, tag=tag)
        except subprocess.TimeoutExpired as e:
            partial = e.output.decode(errors="replace") if isinstance(e.output, bytes) else e.output or ""
            e.session_id = parse_events(partial)["session_id"]
            raise
        except OSError as e:  # no codex on PATH, or the working directory is gone
            p = subprocess.CompletedProcess(cmd, 127, "", f"codex could not start: {e}")
        ev = parse_events(p.stdout)
        if not ev["completed"]:
            return _result(ev, model, *_failure(ev, p))
        out = _result(ev, model, last_file.read_text() if last_file.exists() else "", before=before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if schema:
        out["structured_output"] = _structured(out["result"], schema)
    return out


def _result(ev: dict, model: str, text: str, subtype: str = "success", limit: dict | None = None,
            before: dict | None = None) -> dict:
    """The dict invoke returns. A failed run reports no usage: Codex prints it only when a turn completes."""
    usage = usage_of(ev["usage"], before)
    return {"result": text or ev["message"], "structured_output": None, "session_id": ev["session_id"],
            "is_error": subtype != "success", "subtype": subtype, "usage": usage, "total_cost_usd": cost_usd(model, usage),
            "num_turns": ev["steps"], "limit": limit}
