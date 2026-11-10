"""MCP bridge: the org tools (Jira, Sentry, Notion, Granola, Slack files...) are claude.ai
connectors reachable only from Claude, not from opencode/Muse. A small Sonnet call FETCHES
raw data through them (no judgment); Muse does the analysis for free.

Verified, never trusted: the call's transcript must contain a real mcp__ tool_use, or the
result is rejected.
"""
from __future__ import annotations

import csv
import glob
import io
import json
import os

from . import claude, db, site
from .core import CFG, VAR, log, run

JIRA_CLOUD = site.jira().get("cloud_id") or ""  # the Atlassian site the Jira connector is asked for (config jira.cloud_id)


class BridgeUnverified(Exception):
    pass


def available() -> bool:
    """The org tools are reached through the claude.ai connectors of the user's own Claude Code login. An install
    says it has them (features.org_research); one that runs on an API key or on Codex has none, and a call would
    spend a model turn to find that out."""
    from . import providers
    return site.feature("org_research") and providers.engine() == "claude"


def _mcp_calls(session_id: str) -> list[str]:
    files = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
    names = []
    for f in files:
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "assistant":
                    for c in (r.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name", "").startswith("mcp__"):
                            names.append(c["name"])
    return names


def fetch(name: str, instruction: str, tools: list[str], item_schema: dict) -> list[dict]:
    schema = {"type": "object", "required": ["items", "tool_calls_made"], "properties": {
        "tool_calls_made": {"type": "integer"}, "error": {"type": "string"},
        "items": {"type": "array", "items": item_schema}}}
    prompt = (f"{instruction}\nUse ToolSearch to load the tools first. Return every result via the schema, verbatim "
              "values only, no commentary. If any tool call fails, put the exact error in `error` instead of "
              "returning an empty list.")
    if not available():
        raise BridgeUnverified(f"{name}: this install has no org connectors (features.org_research)")
    if not claude.can_spend("bridge"):
        raise claude.BudgetExhausted("bridge")
    try:
        claude.need_connectors("bridge")
    except claude.ConnectorsMissing as e:
        raise BridgeUnverified(f"{name}: {e}")
    args = [prompt, "--model", CFG["claude"].get("bridge_model", "claude-sonnet-5"), "--tools", "ToolSearch",
            "--allowedTools", "ToolSearch", *tools, "--disable-slash-commands",
            "--max-budget-usd", str(CFG["claude"]["per_call_usd"].get("bridge", 0.8)), "--json-schema", json.dumps(schema)]
    out = claude._invoke(args, "bridge", None, str(VAR), 300)
    so = out.get("structured_output") or {}
    calls = _mcp_calls(out.get("session_id", ""))
    if not calls:
        raise BridgeUnverified(f"{name}: no MCP tool call in the transcript; refusing the answer")
    if so.get("error"):
        raise BridgeUnverified(f"{name}: tool error: {so['error'][:300]}")
    items = so.get("items") or []
    (VAR / "cache").mkdir(exist_ok=True)
    (VAR / "cache" / f"bridge-{name}.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
    log("bridge fetch", source=name, items=len(items), calls=len(calls))
    return items


def _texts(node) -> list[str]:
    if isinstance(node, dict):
        return ([node["text"]] if isinstance(node.get("text"), str) else []) + [t for v in node.values() for t in _texts(v)]
    return [t for v in node for t in _texts(v)] if isinstance(node, list) else []


def _acli_description(key: str) -> str:
    try:
        p = run(["acli", "jira", "workitem", "view", key, "--fields", "description", "--json"], timeout=60)
        d = json.loads(p.stdout) if p.returncode == 0 else {}
        desc = (d.get("fields") or d).get("description")
        return (desc if isinstance(desc, str) else " ".join(_texts(desc)))[:600]
    except Exception:
        return ""


def _acli_search(jql: str) -> list[dict]:
    """Jira through the acli CLI, logged into his work account on its own (OAuth), whatever account Claude Code is on.
    Zero tokens. The connector route died with the 10-01 switch to his personal account: every sweep failed for four
    days and TICKET-1411, 1415 and 1419, assigned to him, never became cases."""
    p = run(["acli", "jira", "workitem", "search", "--jql", jql, "--fields", "key,summary,status,priority", "--csv",
             "--paginate"], timeout=180)
    if p.returncode != 0 or not p.stdout.lstrip().lower().startswith(("key,", "priority,", "status,", "summary,")):
        raise RuntimeError(f"acli: rc={p.returncode} {(p.stderr or p.stdout).strip()[:300]}")
    rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(io.StringIO(p.stdout))]
    return [r | {"description": _acli_description(r["key"])} for r in rows if r.get("key")]


def jira_assigned() -> list[dict]:
    jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
    try:
        items = _acli_search(jql)
        log("bridge fetch", source="jira-assigned", items=len(items), via="acli")
        return items
    except Exception as e:
        log("bridge: acli jira failed, trying the connector", err=str(e)[:300])
    return fetch("jira-assigned",
                 f'Run searchJiraIssuesUsingJql with cloudId "{JIRA_CLOUD}", jql "{jql}", maxResults 100, fields [summary,status,priority,'
                 f'updated,description], responseContentFormat markdown. For each issue give key, summary, status, '
                 f'priority, updated and the first 600 characters of the description.',
                 ["mcp__claude_ai_Atlassian__searchJiraIssuesUsingJql"],
                 {"type": "object", "properties": {"key": {"type": "string"}, "summary": {"type": "string"},
                                                   "status": {"type": "string"}, "priority": {"type": "string"},
                                                   "updated": {"type": "string"}, "description": {"type": "string"}}})


def sentry_unresolved() -> list[dict]:
    return fetch("sentry-academic",
                 f"Find the {site.company()} organization in Sentry, then search unresolved issues from the last 7 days whose "
                 "title, culprit or URL relates to the academic domain (academic, classroom, evaluation, score, boleta, "
                 "schedule, teacher, grades). For each give id, title, culprit, count, last_seen, permalink.",
                 ["mcp__claude_ai_Sentry__find_organizations", "mcp__claude_ai_Sentry__find_projects",
                  "mcp__claude_ai_Sentry__search_issues"],
                 {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                                                   "culprit": {"type": "string"}, "count": {"type": ["integer", "string"]},
                                                   "last_seen": {"type": "string"}, "permalink": {"type": "string"}}})


def leave_draft(channel: str, thread_ts: str | None, text: str) -> bool:
    """Leave `text` as a composer draft in the user's Slack (he sends it with Enter; nothing is
    posted). v1 did this and it was the part he used: "te dejé este draft en el hilo"."""
    if not available() or not claude.can_spend("bridge"):
        return False
    where = f"channel_id {channel}" + (f", thread_ts {thread_ts}" if thread_ts else "")
    prompt = (f"Use ToolSearch to load mcp__claude_ai_Slack__slack_send_message_draft, then call it ONCE with "
              f"{where} and exactly this message text (verbatim, no edits):\n<text>\n{text}\n</text>\n"
              "Reply with the tool's result only.")
    args = [prompt, "--model", CFG["claude"].get("bridge_model", "claude-sonnet-5"), "--tools", "ToolSearch",
            "--allowedTools", "ToolSearch", "mcp__claude_ai_Slack__slack_send_message_draft",
            "--disable-slash-commands", "--max-budget-usd", str(CFG["claude"]["per_call_usd"].get("bridge", 0.8))]
    try:
        out = claude._invoke(args, "bridge", None, str(VAR), 180)
    except Exception as e:
        log("composer draft failed", err=str(e)[:200])
        return False
    ok = "mcp__claude_ai_Slack__slack_send_message_draft" in _mcp_calls(out.get("session_id", ""))
    log("composer draft", channel=channel, ok=ok)
    return ok


def granola_next_steps() -> list[dict]:
    """Dailies as an input (v1, 2026-08-11): the AI summary's "Próximos pasos" with assignees.
    Meetings in the "shared with me" tab are invisible until the user opens them in the app."""
    hint = (CFG.get("granola") or {}).get("daily_title_hint", "Daily")
    return fetch("granola",
                 f"Use the Granola tools: list this week's meetings (no involvement filter), then for every meeting "
                 f"updated in the last 2 days (prefer titles containing '{hint}') read its summary and extract each "
                 f"next-step / action item. One result per item: meeting title, date (YYYY-MM-DD), the item text "
                 f"verbatim, the assignee as written (or 'unassigned'), the meeting id, and 1-3 short "
                 + site.own("Spanish search terms (a school, a feature, a ticket id as said in the meeting)",
                            "search terms in the language the team writes in (a customer, a feature, a ticket id as said "
                            "in the meeting)")
                 + " that would find the Slack conversations about the item.",
                 ["mcp__claude_ai_Granola__list_meetings", "mcp__claude_ai_Granola__get_meetings",
                  "mcp__claude_ai_Granola__query_granola_meetings"],
                 {"type": "object", "required": ["meeting", "date", "item", "assignee", "meeting_id", "slack_terms"], "properties": {
                     "meeting": {"type": "string"}, "date": {"type": "string"}, "item": {"type": "string"},
                     "assignee": {"type": "string"}, "meeting_id": {"type": "string"},
                     "slack_terms": {"type": "array", "items": {"type": "string"}}}})


READABLE = {"png", "jpg", "jpeg", "gif", "webp", "heic", "pdf"}


def read_files(files: list[dict]) -> dict[str, str]:
    """Slack attachments the bot token cannot download (no files:read) read through the claude.ai Slack connector,
    which can: a verbatim transcript per file id, cached for good (a file never changes). 2026-10-13: #757's
    investigation asked a teammate to share the 12/10 screenshot she had attached to the linked thread; through the
    connector it showed 'Error al cargar la boleta'."""
    cache = VAR / "cache" / "slack-files"
    cache.mkdir(parents=True, exist_ok=True)
    got = {f["id"]: (cache / f"{f['id']}.txt").read_text() for f in files if f.get("id") and (cache / f"{f['id']}.txt").exists()}
    todo = [f for f in files if f.get("id") and f["id"] not in got and (f.get("type") or "").lower() in READABLE][:4]
    if not todo:
        return got
    items = fetch("slack-files",
                  "Read each of these Slack files with slack_read_file (file_id) and transcribe what it shows, verbatim: every "
                  "visible text (error messages, banners, toasts, labels, names, numbers, URLs), then one sentence on the "
                  "screen's state (which page, what is empty, loading or failing). A PDF: its text, at most 3000 characters.\n"
                  + "\n".join(f"- file_id {f['id']} ({f.get('name')})" for f in todo),
                  ["mcp__claude_ai_Slack__slack_read_file"],
                  {"type": "object", "required": ["file_id", "transcript"], "properties": {
                      "file_id": {"type": "string"}, "transcript": {"type": "string"}}})
    for x in items:
        if x.get("file_id") in {f["id"] for f in todo} and (x.get("transcript") or "").strip():
            got[x["file_id"]] = x["transcript"].strip()[:6000]
            (cache / f"{x['file_id']}.txt").write_text(got[x["file_id"]])
    return got


def org_research(question: str, checks: list[str]) -> list[dict]:
    """Facts only the org tools hold (roadmap in Notion, tickets and FRs in Jira, Slack canvases).
    Sonnet fetches verbatim snippets with sources; Opus judges them in the investigation."""
    return fetch("org-research",
                 f"Question under investigation: {question}\nChecks requested: {checks}\n"
                 f"Use Notion search (notion-search, then notion-fetch on the best 2-3 hits) and Jira "
                 f"(searchJiraIssuesUsingJql with cloudId \"{JIRA_CLOUD}\", e.g. text ~ \"<keywords>\" ORDER BY updated DESC, "
                 f"maxResults 20) to find what exists about this. Return one result per relevant finding: source "
                 f"(notion page title + url, or Jira key), a verbatim snippet (<= 600 chars) and the date it was last "
                 f"updated. Include negative results explicitly (e.g. 'no Jira issue mentions X') as a finding.",
                 ["mcp__claude_ai_Notion__notion-search", "mcp__claude_ai_Notion__notion-fetch",
                  "mcp__claude_ai_Atlassian__searchJiraIssuesUsingJql", "mcp__claude_ai_Atlassian__getJiraIssue"],
                 {"type": "object", "required": ["source", "snippet"], "properties": {
                     "source": {"type": "string"}, "snippet": {"type": "string"}, "updated": {"type": "string"}}})
