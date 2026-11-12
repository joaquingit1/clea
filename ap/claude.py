"""Tier 2: Claude, only where judgment or code writing earns its cost.

Two profiles:
  lean  - verification/critique/verdicts. Own system prompt file, explicit read-only
          tools, no MCP, no skills: ~5K tokens of prefix (v1 carried ~145K per turn).
  build - code writing/fixing in an isolated worktree. Keeps user settings so the
          cometa-gauntlet / old-coder / verify-before-done skills and the hooks load;
          drops MCP servers (not needed to write code); push/merge/PR commands denied
          (publishing is done by the harness after its own gates).
Every call is budget-gated against a daily cap and recorded in the calls ledger.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time

from . import db, providers, site
from .core import CFG, CODE, HOME, KNOWLEDGE, PROMPTS, RULES, STYLE, VAR, item_dir, log, run

EMPTY_MCP = '{"mcpServers":{}}'
BUILD_DENY = [
    # a headless build has no peers and nobody to ask (2026-09-25: Fable spent 55 min "waiting on peer
    # sessions' replies about worktree ownership" and produced no commits)
    "SendMessage", "ListAgents", "RemoteTrigger", "AskUserQuestion", "EnterPlanMode", "Monitor", "ScheduleWakeup",
    "Bash(git push:*)", "Bash(git push)", "Bash(gh pr create:*)", "Bash(gh pr merge:*)",
    "Bash(gh pr ready:*)", "Bash(gh pr review:*)", "Bash(gh api:*)", "Bash(*CR_GATE=skip*)",
    "Bash(git checkout main:*)", "Bash(git checkout master:*)", "Bash(git reset --hard origin:*)",
    "Bash(rm -rf /*)", "Bash(docker:*)", "Bash(make test:*)",
    # tsc goes through bin/ap-tsc, the gate's own layer (seeded, new errors vs base): a raw whole-app tsc ran cold for
    # 20 min per call in #624's and #813's builds, until the stage budget killed them
    "Bash(*exec tsc*)", "Bash(npx tsc*)", "Bash(*node_modules/.bin/tsc*)",
]
BUILD_ALLOW = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "TodoWrite", "Skill", "Task",
               "Bash(git:*)", "Bash(pnpm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(python3:*)",
               "Bash(uv:*)", "Bash(pytest:*)", "Bash(rg:*)", "Bash(grep:*)", "Bash(ls:*)", "Bash(cat:*)",
               "Bash(sed:*)", "Bash(jq:*)", "Bash(curl:*)", "Bash(gh pr view:*)", "Bash(gh pr diff:*)",
               "Bash(gh run view:*)", "Bash(gh pr checks:*)", "Bash(coderabbit:*)", "Bash(mkdir:*)",
               "Bash(find:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(diff:*)",
               # the gate's own test and type-check runners, by the two spellings a session uses for this checkout
               *[f"Bash({d}/bin/{tool}:*)" for tool in ("ap-dtest", "ap-tsc")
                 for d in dict.fromkeys((str(CODE).replace(str(Path.home()), "~", 1), str(CODE)))]]
# code sessions run every command in the foreground with a bound: a backgrounded command let the session end its turn
# and idle until the reaper killed the stage, and stacked heavy commands starved the host (10-08: #624's build waited
# on full dashboard tsc runs, two or three in flight, from 10:50 to its kill at 12:34, then again from 12:44 to 13:36)
CODE_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "BASH_MAX_TIMEOUT_MS": "1200000"}
# Clean room. Every session used to boot his personal Claude Code: 100+ skill listing, his user hooks,
# effort and a 400k auto-compact window, and the auto-memory of whatever directory it ran in (Clea's own: 4.7k tokens
# of supervisor notes in every triage, judge and report call, rewritten at 2x on each of ~3,000 calls in 14 days).
# Now only the repo's own settings load (its CLAUDE.md files, nested ones included, and its hooks); his global
# CLAUDE.md and the routed skill sections are in the system prompt (system_file); 7 engineering skills stay
# loadable on demand (plugin/clea). Measured on a schools worktree: 51.5k -> ~41k starting context.
# 2026-11-12: every tool a session can call is described to it on every turn. Of 176 sessions on 11-10, three called
# Agent and fourteen a task tool, yet their definitions (Agent 7.5k characters, the three task tools 13.5k, Skill 3.4k)
# and the listings that come with them (14 built-in skills, the agent types) rode along on all 1,798 turns of the code
# sessions: about 8k tokens a turn, a tenth of everything the install read that day. A code session gets what it uses.
# The first install keeps Skill (its engineering skills load on demand) and, in a build, Agent (its brief asks for a
# subagent review); an install without personal skills gets neither, and no skill listing, unless its repos carry
# skills of their own that its sessions should load (config claude.repo_skills).
CODE_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]


def code_tools(role: str, personal: bool, repo_skills: bool = False) -> list[str]:
    return CODE_TOOLS + (["Skill"] if personal or repo_skills else []) + (["Agent"] if personal and role == "build" else [])


NO_AUTO_MEMORY = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def effort_args(model_id: str) -> list[str]:
    """The effort each model ran with under his settings (Opus/Fable high, the rest xhigh), now explicit."""
    e = (CFG["claude"].get("effort") or {}).get(model_id)
    return ["--effort", e] if e else []


def code_settings(model_id: str) -> str:
    """Compact long code sessions at claude.code_compact_window instead of his 400k: builds drifted to 200-300k
    contexts where the ask was far behind. The routed rules live in the system prompt, which compaction keeps."""
    return json.dumps({"modelSettings": {model_id: {"autoCompactWindow": int(CFG["claude"].get("code_compact_window", 160000))}}})


def prompt_path(role: str) -> Path:
    """The file a role's system prompt starts from. The first install's prompts name its user, company and tools, and
    are rewritten by what it learns; a release reads the engine's neutral set (prompts/engine), and falls back to the
    other only for a role the neutral set does not cover yet."""
    agent = role.startswith("agent-")
    name = f"{role[6:] if agent else role}.md"
    own = (HOME / "opencode" / "agents" / name) if agent else (PROMPTS / "claude" / name)
    if site.internal():
        return own
    neutral = PROMPTS / "engine" / ("agents" if agent else "claude") / name
    return neutral if neutral.exists() else own


def render(text: str) -> str:
    """Fill a neutral prompt's placeholders with the install's facts ({{user}}, {{org}}, {{language}}...). A prompt
    with no placeholder is returned as it is."""
    if "{{" not in text:
        return text
    repos = ", ".join(sorted(site.repos())) or "(none configured yet)"
    team = ", ".join(m.get("name", "") for k, m in (CFG.get("team_members") or {}).items() if isinstance(m, dict) and m.get("name"))
    for k, v in {"user": site.first_name(), "user_full": site.user_name(), "org": site.org() or "the user's organization",
                 "repos": repos, "team": team or "(no teammates configured)", "language": site.language_name(),
                 "area": (CFG.get("domain_fence") or "").strip() or "everything in the configured repos and channels",
                 "keywords": ", ".join(site.keywords()) or "(none)"}.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# 2026-11-12: 76% of the turns of a day's sessions made one tool call, and 955 of 2,871 sat in runs of single read-only
# lookups (a sed, a cat, a grep, one file). A turn re-sends the whole conversation, so the cost of a session is its
# turns times its context, not what it reads: the new material of that day was 2M tokens, the re-sent context 135M.
LOOKUPS = ("\n\n## Lookups\nWhen you need several files, searches or read-only commands and none depends on another's "
           "result, ask for them together: several tool calls in the same turn, not one per turn. Every turn re-sends "
           "this whole conversation, so five lookups made one at a time cost about five times what the same five cost "
           "in one turn. Decide what you need to see next, then ask for all of it at once.")


def system_file(role: str, cwd: str | None = None) -> str:
    """Render prompts/claude/<role>.md with style guides and the role's learned-rules pack
    into var/prompts/. Written only when the content changes, so the prompt prefix stays
    byte-identical across calls (prompt-cache hits).
    Since 2026-10-22 sessions no longer load his user settings (ap/skills.py): his global CLAUDE.md and the route's
    skill sections (code and review sessions) are part of this file instead, ahead of the role's own text."""
    from . import skills
    personal = site.feature("personal_skills")  # the user's own Claude Code skills and global rules: the first install's
    route = skills.route_of(role, cwd) if personal else None
    # inside a repo the CLI loads ~/.claude/CLAUDE.md by itself (as a "project" file of every directory under his
    # home); everywhere else no setting source loads, so it rides in here
    house = skills.house_rules() if personal and skills.repo_of(cwd) is None else ""
    head = "\n\n".join(p for p in (house, skills.pack(route) if personal else "") if p)
    src = prompt_path(role)
    if role.startswith("agent-"):
        # a Muse lane answered by Claude: its system prompt IS the opencode agent file, read here. It used to be copied
        # into prompts/claude/ on every call; that copy was tracked, an edit to it was overwritten by the next call and
        # the dirty tree froze every hot reload. One source, never written into the repo.
        text = (src.read_text().split("---", 2)[-1].strip() if src.exists()
                else f"You are the {role[6:]} lane of {site.first_name()}'s assistant.")
    else:
        text = src.read_text()
    text = render(text)
    text = text.replace("{{STYLE_REVIEW}}", style_file("review.md"))
    text = text.replace("{{STYLE_SLACK}}", style_text())
    text += "\n\n## Truth hierarchy (applies to every claim you make)\n" + render(prompt_path("truth").read_text())
    text += LOOKUPS
    pack = RULES / f"claude-{role}.md"
    db.touch_rules([f"claude-{role}"])
    if pack.exists() and pack.read_text().strip():
        text += f"\n\n## Learned rules (from {site.first_name()}'s past corrections; apply them)\n" + pack.read_text()
    lessons = KNOWLEDGE / "lessons.md"
    if role in ("build", "fix", "self_verify", "critique") and lessons.exists():
        # causes and prevention rules from teammates' review findings on our PRs
        text += "\n\n## Lessons from reviews of our past PRs (never repeat these)\n" + lessons.read_text()[-6000:]
    if head:
        text = head + "\n\n# Your role\n" + text
    out = VAR / "prompts" / (f"{role}@{route}.md" if route else f"{role}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists() or out.read_text() != text:
        out.write_text(text)
    return str(out)


def agent_bash(role: str) -> tuple[list[str], list[str]]:
    """The read-only shell an opencode agent file grants its lane (frontmatter permission.bash), as Claude rules
    (allowed, denied). Its body became the system prompt of the Claude-routed lane but its shell did not: since the
    09-23 routing every investigate ran on Read/Grep/Glob while told to check git log/blame, gh and the live OpenAPI,
    and about 100 dossiers say 'not run (no shell in this lane)'."""
    import re
    if not role.startswith("agent-"):
        return [], []
    src = prompt_path(role)  # the same file its system prompt came from (a release reads the engine's neutral set)
    if not src.exists():
        return [], []
    front = src.read_text().split("---", 2)
    rules = re.findall(r'^\s+"([^"]+)":\s*(allow|deny)\s*$', front[1] if len(front) == 3 else "", re.M)
    def rule(p: str) -> str:  # a trailing * is a prefix (Claude's 'git log:*'); an inner one stays a glob
        head = p[:-1].rstrip() if p.endswith("*") else ""
        return f"Bash({head}:*)" if head and "*" not in head else f"Bash({p})"
    # "echo *" (draft, triage, judge) is how the free model printed: no reason to hand a Claude lane a shell for it
    return ([rule(p) for p, v in rules if v == "allow" and p not in ("*", "echo *")],
            [rule(p) for p, v in rules if v == "deny" and p != "*"])


class BudgetExhausted(Exception):
    pass


class ClaudeLimited(Exception):
    pass


class ClaudeError(Exception):
    pass


class ConnectorsMissing(ClaudeError):
    pass


class Interrupted(ClaudeError):
    """impact.worker killed the session on purpose to hand it thread news: the stage reruns and resumes, no failure."""


CLAUDE_JSON = Path.home() / ".claude.json"


def wrong_account() -> str | None:
    """The account the CLI is logged into, when it is not the work org (config claude.work_account) whose claude.ai
    connectors (Sentry, Redshift, Metabase, Jira; Grafana is local) telemetry and the bridge run on. Zero tokens: the CLI keeps the login
    in ~/.claude.json. 2026-09-30 20:11 to 10-01 12:59 the login was his personal account, which has none of them:
    every telemetry call of the alert storm paid ~$0.4 to find no Datadog tool, and the dossiers went out unproven."""
    want = (CFG["claude"].get("work_account") or {}).get("org_uuid")
    if not want:
        return None
    try:
        acct = json.loads(CLAUDE_JSON.read_text()).get("oauthAccount") or {}
    except Exception:
        return None  # unreadable: fail open, the transcript check still rejects an unevidenced result
    org = acct.get("organizationUuid")
    return None if org in (None, want) else (acct.get("emailAddress") or org)


def need_connectors(role: str) -> None:
    """Raise before spending when the logged-in account has no work connectors; tell him once per account switch."""
    who = wrong_account()
    if not who:
        if db.kv_get("wrong_account_told"):
            db.kv_set("wrong_account_told", None)  # back on the work account: a later switch is told again
        return
    if db.kv_get("wrong_account_told") != who:
        db.kv_set("wrong_account_told", who)
        want = (CFG["claude"].get("work_account") or {}).get("email") or site.t("the work account", "la cuenta del trabajo")
        log("claude logged into another account, connector calls held", who=who, role=role)
        try:
            from . import slack
            # the first install names its own connectors; any other install hears it in its language, naming none
            slack.send_self(site.own(f"⚠️ Claude Code está logueado como {who}, no {want}: esa cuenta no tiene los conectores de "
                                     f"Sentry, Redshift ni Jira. Freno la telemetría y las consultas a Jira (sin gastar) "
                                     f"hasta que hagas `claude /login` con {want}.",
                                     site.t(f"⚠️ Claude Code is signed in as {who}, not {want}: that account has none of the work "
                                            f"connectors. Telemetry and ticket lookups are on hold (nothing is spent) until you "
                                            f"run `claude /login` with {want}.",
                                            f"⚠️ Claude Code está logueado como {who}, no {want}: esa cuenta no tiene los "
                                            f"conectores de trabajo. Freno la telemetría y las consultas de tickets (sin gastar) "
                                            f"hasta que hagas `claude /login` con {want}.")), topic="wrong-account")
        except Exception as e:
            log("wrong account notice failed", err=str(e)[:160])
    raise ConnectorsMissing(f"{role}: logged into {who}, which has no work connectors")


def paused_until() -> int:
    return int(db.kv_get("claude_paused_until", 0))


def _pause(until: int) -> None:
    """The account's usage limit holds every model call until `until`. Each span is kept for the observer, which knew the
    Mac's sleeps but not these: #555's self_review waited out the 16:21-18:00 limit on 10-06 and was filed as 'a 1h38m
    unexplained idle gap', like the 09:04-13:00 one the run before."""
    db.kv_set("claude_paused_until", until)
    spans, t = db.kv_get("claude_pauses") or [], int(time.time())
    if until <= t:  # lifted early (the probe answered)
        if spans and spans[-1][1] > t:
            spans[-1][1] = t
    elif spans and spans[-1][1] >= t:  # the same limit, met again by another lane
        spans[-1][1] = max(spans[-1][1], until)
    else:
        spans.append([t, until])
    db.kv_set("claude_pauses", spans[-30:])


DELIVERY = ("build", "fix", "self_verify", "verify_done", "critique", "agent-ap-plan", "publish_text", "comments_verify",
            "agent-ap-comments", "ci", "agent-ap-ci-diagnose")
REVIEWS = ("agent-ap-review", "review_verify")


import threading as _th
_pool_override = _th.local()


def _pool(role: str) -> str:
    forced = getattr(_pool_override, "pool", None)
    if forced:
        return forced
    return "delivery" if role in DELIVERY else "reviews" if role in REVIEWS else "judgment"


class exempt_ctx:
    """Inside the block the pools do not apply (only the daily cap): answering reviewers on his PRs is
    never a budget question."""
    def __enter__(self):
        self.prev = getattr(_pool_override, "exempt", False)
        _pool_override.exempt = True

    def __exit__(self, *a):
        _pool_override.exempt = self.prev


class pool_ctx:
    """Charge everything inside the block to one budget pool (delivery steps that reuse judgment
    agents: plan, CI diagnosis, announce drafts, merge deps; 2026-09-25 they starved on 'judgment')."""
    def __init__(self, pool: str):
        self.pool, self.prev = pool, None

    def __enter__(self):
        self.prev = getattr(_pool_override, "pool", None)
        _pool_override.pool = self.pool

    def __exit__(self, *a):
        _pool_override.pool = self.prev


class caller_ctx:
    """Calls inside the block are recorded as '<role>:<caller lane>': the muse agents serve many lanes under one role
."""
    def __init__(self, lane: str | None):
        self.lane, self.prev = lane, None

    def __enter__(self):
        self.prev = getattr(_pool_override, "caller", None)
        _pool_override.caller = self.lane

    def __exit__(self, *a):
        _pool_override.caller = self.prev


class eval_ctx:
    """Calls inside the block replay past cases for a measurement (evals.py) and are recorded as 'eval/<lane>': under the
    lane's own name the weekly eval's 401 triage, 153 judge and 63 draft calls read as intake
    overhead, '90% of calls went to triage/judge, not case work'. Thread-local: set it inside each worker thread."""
    def __enter__(self):
        self.prev = getattr(_pool_override, "eval", False)
        _pool_override.eval = True

    def __exit__(self, *a):
        _pool_override.eval = self.prev


def call_budget(role: str) -> float:
    """One call's dollar cap: '<role>:<caller lane>' first, then the role. A muse agent serves lanes of very different
    size under one role."""
    caps = CFG["claude"]["per_call_usd"]
    caller = getattr(_pool_override, "caller", None)
    return caps.get(f"{role}:{caller}", caps.get(role, 2))


def spent_pool(pool: str) -> float:
    rows = db.q("SELECT lane, coalesce(sum(usd),0) s FROM calls WHERE tier='claude' AND at>=? GROUP BY lane",
                int(time.mktime(time.strptime(db.today(), "%Y-%m-%d"))))
    return sum(r["s"] for r in rows if _lane_pool(r["lane"]) == pool)


def _lane_pool(lane: str) -> str:
    lane = lane.split(":")[0]
    if lane.startswith("delivery/"):
        return "delivery"
    return "delivery" if lane in DELIVERY else "reviews" if lane in REVIEWS else "judgment"


SESSION_EXEMPT = ("report", "reply", "followup", "draft", "publish_text", "pr_scrub", "vet", "agent-ap-triage", "agent-ap-draft",
                  "agent-ap-judge", "dupcheck", "dupcase", "impact", "self_verify", "comments_verify", "verify", "agent-ap-deps",
                  "agent-ap-investigate", "agent-ap-plan", "critique", "telemetry", "sweep", "bridge", "pregate")
# only the heavy code and review sessions are paced (build, fix, patrol, squad-review finders); everything that
# moves a case forward or answers a person runs


def can_spend(role: str, urgent: bool = False) -> bool:
    """Budget in three pools so squad reviews and triage can never starve code. Pools come from
    config claude.pools_usd; delivery may also use the urgent overdraft."""
    c = CFG["claude"]
    if time.time() < paused_until():
        return False
    win = c.get("session_usd_cap")  # off by default: the account's session limit is a percentage the harness cannot
    # see, so a dollar cap only guesses (the user, 2026-09-29: "session limits are in percentage, not money; I have 50%
    # remaining"). The usage-limit probe is the real signal: pause on the limit message, resume when it clears.
    if win and not getattr(_pool_override, "exempt", False) and role not in SESSION_EXEMPT:
        # the session limit is a 5 h window: pace builds, plans and reviews so replies, reports and
        # publishing keep flowing
        spent5 = db.q1("SELECT coalesce(sum(usd),0) s FROM calls WHERE tier='claude' AND at>?", int(time.time()) - 5 * 3600)["s"]
        if spent5 + call_budget(role) > win:
            return False
    pools = c.get("pools_usd") or {}
    if not pools or getattr(_pool_override, "exempt", False):
        cap = c["daily_usd_cap"] + (c["urgent_overdraft_usd"] if urgent else 0)
        return db.spent_today("claude") + call_budget(role) <= cap
    pool = _pool(role)
    cap = pools.get(pool, 0) + (c["urgent_overdraft_usd"] if pool == "delivery" else 0)
    return spent_pool(pool) + call_budget(role) <= cap


CONTINUE = ("continue. The previous run of this exact task was interrupted (usage limit, timeout or a hung process). "
            "Resume from where you left off: re-read the working tree state if you changed files, do not redo finished "
            "steps, finish the task, and return the required structured output.")
WRAP_UP = ("Your budget for this task is spent. Stop reading and do not call any more tools: return the required structured "
           "output now, from what you have already found. Say in it what you could not check.")


def news_for(item_id) -> str:
    """Thread news the impact check routed to this case and the interrupted session has not seen yet (ap/impact.py)."""
    if not item_id:
        return ""
    it = db.item(item_id)
    if not it:
        return ""
    seen = it["data"].get("inbox_seen", 0)
    new = [n for n in it["data"].get("inbox", []) if n.get("at", 0) > seen]
    if not new:
        return ""
    db.update_item(item_id, data={"inbox_seen": max(n["at"] for n in new)})
    return ("\n\nYOU WERE INTERRUPTED FOR THIS NEWS FROM THE CASE THREAD. Decide first whether it changes your work: if the "
            "work is now redundant or wrong, say so in your output instead of finishing it; if it changes the approach, adapt:\n"
            + "\n".join(f"- {n['who']} ({n['impact']}): {n['text'][:600]} [{n['why']}]" for n in new))


def _resume_key(item_id, role) -> str:
    return f"resume:{item_id}:{role}"


def resume_for(item_id, role) -> str | None:
    """Session id of an interrupted run of this item+role, if any (24 h)."""
    r = db.kv_get(_resume_key(item_id, role)) or {}
    return r.get("sid") if r and r.get("at", 0) > time.time() - 86400 else None


def _project_dir(cwd: str) -> Path:
    """Where the CLI keeps the transcripts of sessions started in `cwd`: the path with every character that is not a
    letter or a digit turned into a dash (a space and a dot too: 'Application Support/Clea/install-0.2' has both)."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))


FILES_SHOWN = 25
_READERS = ("cat", "sed", "head", "tail", "less", "nl")
_PATHISH = re.compile(r"(?<![\w./-])(/?[\w.-]+(?:/[\w.-]+)+\.\w{1,6})(?![\w/-])")


def _repo_rel(path: str) -> tuple[str, str] | None:
    """(repo, path inside it) for a file in one of the install's repos: a case worktree, the read-only mirror, or the
    user's own clone. None for anything else (the case's own notes, memory files, /tmp)."""
    for marker in ("/worktrees/", "/mirror/"):
        if marker in path:
            name, _, rel = path.split(marker, 1)[1].partition("/")
            repo = name.split("--", 1)[0]
            return (repo, rel) if rel and repo in site.repos() else None
    for name, r in site.repos().items():
        root = str(r.get("path") or "").rstrip("/")
        if root and path.startswith(root + "/"):
            return name, path[len(root) + 1:]
    return None


def note_files(item_id, sid: str | None, cwd: str) -> None:
    """After a session of a case: which repo files it opened and which it changed, read from its transcript (no model
    call) and kept with the case. Each stage of a case used to start cold and find the same files again, a lookup a
    turn: on 2026-11-10 one service file was opened 19 times across the sessions of one case."""
    if item_id is None or not sid:
        return
    try:
        f = _project_dir(cwd) / f"{sid}.jsonl"
        if not f.exists():
            return
        seen: dict[str, dict] = {}

        def hit(path: str, changed: bool = False) -> None:
            path = path if path.startswith("/") else str(Path(cwd) / path)
            at = _repo_rel(path)
            if at:
                e = seen.setdefault(f"{at[0]}:{at[1]}", {"reads": 0, "changed": False})
                e["reads"] += 1
                e["changed"] = e["changed"] or changed
        for line in f.read_text(errors="replace").splitlines():
            if '"tool_use"' not in line:
                continue
            try:
                content = (json.loads(line).get("message") or {}).get("content") or []
            except ValueError:
                continue
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                if name in ("Read", "Edit", "Write", "MultiEdit") and inp.get("file_path"):
                    hit(str(inp["file_path"]), name != "Read")
                elif name == "Bash":
                    cmd = re.sub(r"^(?:cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*)+", "", str(inp.get("command") or "").strip())
                    if cmd.split(" ", 1)[0] in _READERS:
                        for p in _PATHISH.findall(cmd)[:6]:
                            hit(p)
        if not seen:
            return
        store = item_dir(item_id) / "files.json"
        known = json.loads(store.read_text()) if store.exists() else {}
        for k, e in seen.items():
            old = known.get(k) or {"reads": 0, "changed": False}
            known[k] = {"reads": old["reads"] + e["reads"], "changed": old["changed"] or e["changed"]}
        store.write_text(json.dumps(known, indent=1))
    except Exception as e:  # a convenience for the next session, never a reason for this one to fail
        log("files of the session not recorded", item=item_id, err=f"{type(e).__name__}: {e}"[:200])


def files_note(item_id, cwd: str) -> str:
    """What the next session of a case is told about the files its earlier sessions used, when it runs in a repo: the
    ones in that repo that still exist there, changed ones first, then the most read."""
    if item_id is None:
        return ""
    try:
        store = item_dir(item_id) / "files.json"
        here = _repo_rel(str(Path(cwd).resolve()) + "/.")
        if not store.exists() or not here:
            return ""
        repo, sub = here[0], here[1].rstrip(".").strip("/")
        rows = []
        for k, e in json.loads(store.read_text()).items():
            r, _, rel = k.partition(":")
            if r != repo or (sub and not rel.startswith(sub + "/")):
                continue
            local = rel[len(sub) + 1:] if sub else rel
            if (Path(cwd) / local).is_file():
                rows.append((not e.get("changed"), -int(e.get("reads") or 0), local, bool(e.get("changed"))))
        if not rows:
            return ""
        rows.sort()
        lines = "\n".join(f"- {p}" + (" (changed by an earlier session)" if ch else "") for _, _, p, ch in rows[:FILES_SHOWN])
        return ("\n\nFiles the earlier sessions of this case opened in this repo, most used first. They are a starting "
                "point, not a limit: open the ones you need together in one turn instead of searching for them again.\n" + lines)
    except Exception as e:
        log("files note not built", item=item_id, err=f"{type(e).__name__}: {e}"[:200])
        return ""


def _remember_session(item_id, role, sid: str | None, cwd: str, started: float) -> None:
    """the user: 'when you run out of tokens the thread shouldn't be reset. wait, then send continue to the
    same thread.' The session id comes from the run's JSON, or from the newest transcript the CLI wrote for that cwd."""
    if not sid:
        try:
            proj = _project_dir(cwd)  # was only "/" to "-": no transcript was ever found under a path with a space or a dot
            cands = [f for f in proj.glob("*.jsonl") if f.stat().st_mtime >= started - 5]
            if cands:
                sid = max(cands, key=lambda f: f.stat().st_mtime).stem
        except Exception:
            sid = None
    if sid and item_id is not None:
        db.kv_set(_resume_key(item_id, role), {"sid": sid, "cwd": cwd, "at": int(time.time())})
        log("session kept for resume", item=item_id, role=role, sid=sid[:8])


TRANSIENT = ("went to sleep", "connection error", "overloaded")  # API errors that end a call, not the session


AUTH_WORDS = ("invalid api key", "not logged in", "login expired", "please run /login", "authentication_error",
              "oauth token has expired", "invalid bearer token")


def _lane(role: str) -> str:
    """The name a call is recorded under: its role, the caller lane a shared agent served, and whether it is an eval."""
    caller = getattr(_pool_override, "caller", None)
    lane = (f"delivery/{role}" if _pool(role) == "delivery" and role not in DELIVERY else role) + (f":{caller}" if caller else "")
    return f"eval/{lane}" if getattr(_pool_override, "eval", False) else lane


def provider_down(why: str) -> "ClaudeLimited":
    """The provider cannot take calls until the user does something (signed out, a key revoked or missing). Every call
    waits like on a usage limit and the probe lifts the pause once a call goes through again; raised as a failure, each
    case would spend its three attempts and block on a login only its user can fix."""
    db.kv_set("provider_problem", {"at": int(time.time()), "why": why[:300]})
    _pause(int(time.time()) + 600)
    log("provider cannot take calls, pausing", why=why[:200])
    return ClaudeLimited(f"provider: {why[:300]}")


def _signed_out(t0: float) -> bool:
    """An auth error that is the provider's, not the machine's. A Mac in a dark wake has no keychain, so the CLI says
    'Not logged in' while the login is fine: that failure belongs to the sleep (the stage is deferred, as before). The
    first install keeps handling every such error as a failed call, which its incident tracking was built around."""
    from . import hot
    return not site.internal() and not hot.slept_near(t0, time.time())


def _provider_ok() -> None:
    if db.kv_get("provider_problem"):
        db.kv_set("provider_problem", None)


def _invoke(args: list[str], role: str, item_id: int | None, cwd: str, timeout: int, env: dict | None = None) -> dict:
    t0 = time.time()
    lane = _lane(role)
    try:
        penv = providers.claude_env()
    except providers.NotReady as e:
        raise provider_down(str(e)) from None
    try:
        from . import trace
        p = run(["claude", "-p", *args, "--output-format", "json"], cwd=cwd, timeout=timeout,
                extra_env={**(trace.otel_env(role) if site.feature("tracing") else {}), **penv, **(env or {})},
                tag={"item": item_id, "role": role})
    except subprocess.TimeoutExpired:
        db.record_call("claude", lane, item_id, False, time.time() - t0, 0, 0, 0, "timeout")
        _remember_session(item_id, role, None, cwd, t0)
        raise ClaudeError(f"{role}: timeout after {timeout}s")
    dur = time.time() - t0
    if p.returncode in (-9, 137) and not p.stdout.strip():  # killed (the reaper, or impact): the session survives on disk
        _remember_session(item_id, role, None, cwd, t0)
        import threading
        mark = db.kv_get(f"news_interrupt:{threading.current_thread().name}") or {}
        if item_id is not None and mark.get("item") == item_id and mark.get("at", 0) >= int(t0):
            db.kv_set(f"news_interrupt:{threading.current_thread().name}", {})
            db.record_call("claude", lane, item_id, False, dur, 0, 0, 0, "interrupted for thread news")
            raise Interrupted(f"{role}: interrupted for thread news after {round(dur)}s (kept for resume)")
        reaped = db.kv_get(f"kill_reason:{threading.current_thread().name}") or {}
        why = f"killed: {reaped['why']}" if reaped.get("at", 0) >= int(t0) else "killed"
        db.record_call("claude", lane, item_id, False, dur, 0, 0, 0, why)
        raise ClaudeError(f"{role}: session {why} after {round(dur)}s (kept for resume)")
    try:
        out = json.loads(p.stdout)
    except Exception:
        blob = (p.stdout + p.stderr)[-800:]
        db.record_call("claude", lane, item_id, False, dur, 0, 0, 0, blob[:200])
        if "limit" in blob.lower() and ("usage" in blob.lower() or "session" in blob.lower()):
            _remember_session(item_id, role, None, cwd, t0)
            _pause(int(time.time()) + CFG["claude"]["limit_backoff_s"])
            raise ClaudeLimited(blob)
        raise ClaudeError(f"{role}: unparseable output rc={p.returncode}: {blob}")
    u = out.get("usage") or {}
    in_tok = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    usd = float(out.get("total_cost_usd") or 0)
    ok = not out.get("is_error")
    model_used = args[args.index("--model") + 1] if "--model" in args else "?"
    res = str(out.get("result", ""))
    low = res.lower()
    usage_limit = "limit" in low and ("usage" in low or "session" in low or "reset" in low)
    # a usage limit ends with subtype "success": name it, so incidents and the observer do not count it as a stage
    # failure. Any other error says what
    # the CLI answered: 10-11's dark-wake "Not logged in" was recorded as "claude-sonnet-5 success", filed as a
    # patrol failure and read by the observer as a malformed model id
    why = " usage limit" if usage_limit else f" {res.strip().splitlines()[0][:60]}" if res.strip() else ""
    out["call_row"] = db.record_call("claude", lane, item_id, ok, dur, usd, in_tok, u.get("output_tokens", 0),
                                     f"{model_used}" + (f" {out.get('subtype', '')}{why}" if not ok else ""))
    if not ok and (out.get("api_error_status") == 401 or any(w in low for w in AUTH_WORDS)) and _signed_out(t0):
        _remember_session(item_id, role, out.get("session_id"), cwd, t0)
        raise provider_down(res.strip().splitlines()[0] if res.strip() else "the engine is not signed in")
    if not ok:
        if ("spend limit" in low or "switch to another model" in low) and not usage_limit:
            # a monthly spend cap is PER MODEL ("switch to another model to continue"; 2026-09-25 only
            # Fable was capped, Opus and Sonnet answered). Mark that model, never pause everything.
            # The usage-limit wording wins when both appear.
            m = args[args.index("--model") + 1] if "--model" in args else "?"
            log("model spend cap detected", model=m, text=res[:300])
            db.kv_set(f"model_limited:{m}", int(time.time()) + 6 * 3600)
            db.kv_set("claude_org_limit_at", int(time.time()))
            raise ModelLimited(m)
        if "limit" in low and ("usage" in low or "session" in low or "reset" in low):
            # the claude.ai usage limit: global, until the reset time it names (2026-09-27: it was taken for a
            # per-model spend cap, every model got marked for 6 h and a build sat idle after the reset)
            _pause(_reset_epoch(res) or int(time.time()) + CFG["claude"]["limit_backoff_s"])
            log("usage limit detected", text=res[:300], until=db.kv_get("claude_paused_until"))
            _remember_session(item_id, role, out.get("session_id"), cwd, t0)
            raise ClaudeLimited(res[:300])
        # budget-capped runs still carry useful partial work for build sessions
        if out.get("subtype") != "error_max_budget_usd":
            # the API dropped the call mid-response and its transcript is on disk: a resumable role continues it. Dropped,
            # #763's 5.6 h, $2.06 telemetry session ('Your computer went to sleep mid-response', 10-14 03:34) was rerun
            # from scratch at 03:51
            if any(w in low for w in TRANSIENT):
                _remember_session(item_id, role, out.get("session_id"), cwd, t0)
            raise ClaudeError(f"{role}: {out.get('subtype')} {res[:400]}")
    log(f"claude {role} done", usd=round(usd, 3), dur=round(dur), item=item_id, turns=out.get("num_turns"))
    if item_id is not None:
        db.kv_set(_resume_key(item_id, role), None)  # finished: nothing to resume
    _provider_ok()
    return out


def _codex(role: str, prompt: str, model: str, cwd: str, system: str, schema: dict | None, sandbox: str, network: bool,
           add_dirs: tuple, item_id: int | None, timeout: int, resumable: bool, session: str | None = None) -> dict:
    """The same call on the Codex engine (an install whose provider is OpenAI). Returns what _invoke returns: Codex's
    answer in the shape the stages already read, recorded in the same ledger, with the same pause on a plan limit or
    a lost sign-in."""
    from . import codex
    try:
        auth, token = providers.codex_auth()
    except providers.NotReady as e:
        raise provider_down(str(e)) from None
    mid = providers.model_id(model)
    sid = session or (resume_for(item_id, role) if resumable else None)
    if sid and not session:
        log("resuming interrupted session", item=item_id, role=role, sid=sid[:8])
        prompt = CONTINUE + news_for(item_id)
    lane, t0 = _lane(role), time.time()
    try:
        out = codex.invoke(prompt, model=mid, cwd=cwd, system=system, schema=schema, sandbox=sandbox, network=network,
                           add_dirs=tuple(add_dirs), resume=sid, effort=(CFG["openai"].get("effort") or {}).get(mid),
                           timeout=timeout, auth=auth, token=token, tag={"item": item_id, "role": role},
                           ephemeral=not (resumable or session or role == "chat"))  # nobody resumes a short judgment
    except subprocess.TimeoutExpired as e:
        db.record_call("claude", lane, item_id, False, time.time() - t0, 0, 0, 0, f"timeout {mid}")
        if getattr(e, "session_id", None) and item_id is not None and resumable:
            db.kv_set(_resume_key(item_id, role), {"sid": e.session_id, "cwd": cwd, "at": int(time.time())})
        raise ClaudeError(f"{role}: timeout after {timeout}s") from None
    dur, u, ok = time.time() - t0, out.get("usage") or {}, not out.get("is_error")
    res = str(out.get("result") or "")
    usd = float(out.get("total_cost_usd") or 0)
    out["call_row"] = db.record_call("claude", lane, item_id, ok, dur, usd,
                                     u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0),
                                     mid + ("" if ok else f" {out.get('subtype', '')} {res.strip().splitlines()[0][:60] if res.strip() else ''}"))
    if not ok:
        if out.get("session_id") and item_id is not None and resumable:
            db.kv_set(_resume_key(item_id, role), {"sid": out["session_id"], "cwd": cwd, "at": int(time.time())})
        lim = out.get("limit")
        if lim:  # the plan's usage limit (or a rate limit): every call waits, the probe lifts it
            _pause(int(lim.get("reset_at") or time.time() + CFG["claude"]["limit_backoff_s"]))
            log("usage limit detected", text=res[:300], until=db.kv_get("claude_paused_until"))
            raise ClaudeLimited(res[:300])
        if out.get("subtype") == "error_auth":
            raise provider_down(res.strip().splitlines()[0] if res.strip() else "Codex is not signed in")
        raise ClaudeError(f"{role}: {'session killed (kept for resume)' if res == 'killed' else res[:400]}")
    log(f"codex {role} done", usd=round(usd, 3), dur=round(dur), item=item_id, turns=out.get("num_turns"))
    if item_id is not None:
        db.kv_set(_resume_key(item_id, role), None)
    _provider_ok()
    return out


def _fast_file(role: str) -> str:
    """A fast role's small system prompt, with the install's facts filled in when it has placeholders."""
    src = prompt_path(role)
    text = render(src.read_text())
    if text == src.read_text():
        return str(src)
    out = VAR / "prompts" / f"fast-{role}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists() or out.read_text() != text:
        out.write_text(text)
    return str(out)


def style_text() -> str:
    """the user's Slack style guide (universal + slack sections, incl. the auto-learned part)."""
    return style_file("universal.md") + "\n" + style_file("slack.md")


def style_file(name: str) -> str:
    """One of the install's style guides (how its user writes: STYLE/<name>), or the engine's neutral one when the
    install has none yet. A guide is learned from or written by one person, so the engine ships only the neutral set."""
    for f in (STYLE / name, PROMPTS / "style.default" / name):
        if f.exists():
            return f.read_text()
    return ""


def lean(role: str, prompt: str, *, model: str = "opus", cwd: str, schema: dict | None = None,
         tools: tuple[str, ...] = ("Read", "Grep", "Glob"), add_dirs: tuple[str, ...] = (),
         item_id: int | None = None, urgent: bool = False, timeout: int = 900) -> dict:
    if not can_spend(role, urgent):
        raise BudgetExhausted(role)
    for _ in range(3):
        m = pick(model)
        try:
            return _lean_once(role, prompt, m, cwd, schema, tools, add_dirs, item_id, timeout)
        except ModelLimited:
            log("model spend-capped, falling back", role=role, model=m)
    raise ClaudeLimited(f"{role}: no model available")


FAST_ROLES = ("chat_fast", "console_fast", "review_relevance", "scan")


RESUMED = ("telemetry", "self_verify", "verify_done", "critique")  # long judgment sessions resume; short ones just rerun


def _lean_once(role, prompt, model, cwd, schema, tools, add_dirs, item_id, timeout) -> dict:
    fast = role in FAST_ROLES  # real-time chat: tiny system prompt, no settings/hooks, no session file (about 3 s)
    sysfile = _fast_file(role) if fast else system_file(role, cwd)
    if providers.engine() == "codex":
        out = _codex(role, prompt, model, cwd, Path(sysfile).read_text(), schema, "read-only", False, add_dirs, item_id, timeout,
                     resumable=role in RESUMED)
        if schema and out.get("structured_output") is None:
            raise ClaudeError(f"{role}: no structured_output")
        return out["structured_output"] if schema else {"text": out.get("result", "")}
    sid = resume_for(item_id, role) if role in RESUMED else None
    if sid:
        log("resuming interrupted session", item=item_id, role=role, sid=sid[:8])
        prompt = CONTINUE + news_for(item_id)
    args = [prompt, *(["--resume", sid] if sid else []), "--model", providers.model_id(model), "--system-prompt-file", sysfile,
            "--strict-mcp-config", "--mcp-config", EMPTY_MCP, "--disable-slash-commands",
            "--max-budget-usd", str(call_budget(role))]
    env = None
    if fast:
        args += ["--setting-sources", "", "--no-session-persistence"]
        e = (CFG["claude"].get("role_effort") or {}).get(role)
        args += ["--effort", e] if e else []
    else:
        # in a repo worktree the repo's CLAUDE.md and memory index stay (reviews and verifies read that code); anywhere
        # else (Clea's own dirs, the mirror root) nothing of his setup loads (see the clean-room note above)
        from . import skills
        in_repo = skills.repo_of(cwd) is not None
        args += ["--setting-sources", "project" if in_repo else "", *effort_args(providers.model_id(model))]
        env = None if in_repo else NO_AUTO_MEMORY
    allow, deny = agent_bash(role) if tools else ([], [])
    if allow:  # 'default' mode, not his settings' 'auto': headless, any command outside these rules is denied
        tools = (*tools, "Bash")
        args += ["--permission-mode", "default", "--allowedTools", *allow, *(["--disallowedTools", *deny] if deny else [])]
    args += ["--tools", *tools] if tools else ["--tools", ""]
    for d in add_dirs:
        args += ["--add-dir", d]
    if schema:
        args += ["--json-schema", json.dumps(schema)]
    if tools and not sid and not fast:
        args[0] = prompt + files_note(item_id, cwd)
    out = _invoke(args, role, item_id, cwd, timeout, env=env)
    if tools and not fast:
        note_files(item_id, out.get("session_id"), cwd)
    if schema and out.get("structured_output") is None and out.get("subtype") == "error_max_budget_usd" \
            and out.get("session_id") and not fast:
        # the cap cut the session before it answered, and everything it read was thrown away (10-11 13:11: a $1.56
        # patrol, 12 capped calls and $18 in 14 days): resume it once on a small budget for the answer it has
        log("capped before answering: asking the session to wrap up", role=role, usd=out.get("total_cost_usd"))
        wrap = [WRAP_UP, "--resume", out["session_id"], *args[3 if sid else 1:]]
        wrap[wrap.index("--max-budget-usd") + 1] = str(max(0.2, round(call_budget(role) / 4, 2)))
        capped, out = out, _invoke(wrap, role, item_id, cwd, min(timeout, 600), env=env)
        if out.get("structured_output") is not None and capped.get("call_row"):
            # the cap cost a wrap-up, not the answer: the capped call is no failure (10-18 15:17's patrol, answered
            # at 15:33 with its 2 findings, was filed as a recurrence of the incident 015b820 fixed)
            db.x("UPDATE calls SET ok=1, note=note || ' (wrapped up)' WHERE id=?", capped["call_row"])
    if schema:
        so = out.get("structured_output")
        if so is None:  # a capped call has none: say so, 'no structured_output' hid the cap for 10 days
            raise ClaudeError(f"{role}: " + (f"capped at ${call_budget(role)} before answering"
                                             if out.get("subtype") == "error_max_budget_usd" else "no structured_output"))
        return so
    return {"text": out.get("result", "")}


class ModelLimited(Exception):
    pass


FALLBACK = {"fable": ["fable", "opus", "sonnet"], "opus": ["opus", "fable", "sonnet"], "sonnet": ["sonnet", "opus"], "haiku": ["haiku", "sonnet"],
            "haiku5": ["haiku5", "haiku", "sonnet"]}
# code is written by Opus 5.5 (the user, 2026-09-29: "keep fable to the bare minimum, just a greenlight, the rest
# can be done with opus 5.5"). Fable gives the one final verdict before a PR is published (self_review), nothing else.
# One exception since 2026-11-12 ("try sonnet 5.5 for the mechanical work, and escalating if it fails"): a fix round
# whose every problem was judged mechanical (tasks._to_fix) starts on Sonnet, and the case goes back to Opus for good
# the first time such a round fails or its problem returns. A build is never light.
CODE_CHAIN = ["opus"]
LIGHT = "sonnet"


def _reset_epoch(text: str) -> int | None:
    """'resets 11pm', 'resets at 3:30 pm', 'resets 23:00' -> epoch of that time today/tomorrow (local)."""
    import datetime as _dt
    import re as _re
    m = _re.search(r"reset\w*\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, _re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    now = _dt.datetime.now()
    t = now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
    if t <= now:
        t += _dt.timedelta(days=1)
    return int(t.timestamp()) + 60


def _requeue_limited() -> None:
    """Items that a limit or a budget pool pushed to a later retry run now that the way is clear."""
    db.x("UPDATE items SET next_run_at=0 WHERE claimed_by IS NULL AND next_run_at>? AND (last_error LIKE 'ClaudeLimited%' "
         "OR last_error LIKE 'BudgetExhausted%' OR last_error LIKE 'ModelLimited%')", int(time.time()))
    log("limit cleared: blocked items requeued")


def probe_limited() -> list[str]:
    """A $0.01 call to each model still marked spend-capped, and to the global usage-limit pause; a success
    clears the mark (the mark is a guess, the probe is the truth: 2026-09-28 the pause outlived a re-login
    by an hour and every draft sat idle)."""
    cleared = []
    if paused_until() > time.time() and time.time() - db.kv_get("probe_at:pause", 0) >= 300:
        db.kv_set("probe_at:pause", int(time.time()))
        if _probe(providers.model_id("sonnet")):
            _pause(0)
            _provider_ok()
            cleared.append("pause")
            log("usage-limit pause cleared by probe")
            _requeue_limited()
    for key, mid in ((k, k.split(":", 1)[1]) for k in (r["k"] for r in db.q("SELECT k FROM kv WHERE k LIKE 'model_limited:%'"))):
        until = db.kv_get(key, 0)
        # a mark is a guess: probe every 5 min (one cent) so a reset never idles the harness for 20
        if time.time() >= until or time.time() - db.kv_get(f"probe_at:{mid}", 0) < 300:
            continue
        db.kv_set(f"probe_at:{mid}", int(time.time()))
        if _probe(mid):
            db.kv_set(key, 0)
            cleared.append(mid)
            log("model spend cap cleared by probe", model=mid)
            _requeue_limited()
    return cleared


def _probe(mid: str) -> bool:
    """One tiny call on the selected provider: does it answer? A cent, or nothing on a plan."""
    try:
        if providers.engine() == "codex":
            from . import codex
            auth, token = providers.codex_auth()
            return not codex.invoke("Reply with exactly: ok", model=mid, cwd=str(VAR), auth=auth, token=token, timeout=120).get("is_error")
        p = run(["claude", "-p", "reply with: ok", "--model", mid, "--output-format", "json", "--tools", "", "--strict-mcp-config",
                 "--mcp-config", EMPTY_MCP, "--max-budget-usd", "0.05"], cwd=str(VAR), timeout=120, extra_env=providers.claude_env())
        return not json.loads(p.stdout).get("is_error")
    except Exception:  # signed out, no key, a timeout, garbage: not answering
        return False


def pick(model: str) -> str:
    """First model of the fallback chain that is not under a spend cap."""
    for m in FALLBACK.get(model, [model]):
        if time.time() >= db.kv_get(f"model_limited:{providers.model_id(m)}", 0):
            return m
    raise ClaudeLimited(f"every model in the {model} chain is spend-capped")


def build(role: str, prompt: str, *, model: str = "fable", cwd: str, item_id: int | None = None,
          add_dirs: tuple[str, ...] = (), urgent: bool = False, timeout: int = 5400, light: bool = False) -> dict:
    if not can_spend(role, urgent):
        raise BudgetExhausted(role)
    chain = CODE_CHAIN if role in ("build", "fix") else FALLBACK.get(model, [model])
    if light and role == "fix":
        chain = [LIGHT] + chain  # a mechanical fix round (see CODE_CHAIN); capped, it runs on Opus as before
    for m in chain:
        if time.time() < db.kv_get(f"model_limited:{providers.model_id(m)}", 0):
            continue
        try:
            return _build_once(role, prompt, m, cwd, item_id, add_dirs, timeout)
        except ModelLimited:
            log("model spend-capped", role=role, model=m, chain=chain)
    raise ClaudeLimited(f"{role}: " + (site.t("Opus is capped, the build waits", "Opus con tope, el build espera")
                                       if chain == CODE_CHAIN else "no model available"))


def _build_once(role, prompt, model, cwd, item_id, add_dirs, timeout) -> dict:
    if providers.engine() == "codex":
        # a code session writes in its worktree and installs what the repo needs; pushing, PRs and merges stay with the
        # harness (the sandbox cannot reach outside the worktree, and the guard holds the write credentials)
        out = _codex(role, prompt, model, cwd, Path(system_file(role, cwd)).read_text(), None, "workspace-write", True,
                     add_dirs, item_id, timeout, resumable=True)
        return {"text": out.get("result", ""), "usd": out.get("total_cost_usd"), "session": out.get("session_id"), "capped": False}
    sid = resume_for(item_id, role)
    if sid:
        log("resuming interrupted session", item=item_id, role=role, sid=sid[:8])
        prompt = CONTINUE + news_for(item_id)
    from . import skills
    personal = site.feature("personal_skills")
    repo_skills = bool(CFG["claude"].get("repo_skills"))  # the repos' own.claude/skills, for an install that wants them
    mid = providers.model_id(model)
    args = [prompt, *(["--resume", sid] if sid else []), "--model", mid, "--append-system-prompt-file", system_file(role, cwd),
            "--strict-mcp-config", "--mcp-config", EMPTY_MCP, "--permission-mode", "acceptEdits",
            "--max-budget-usd", str(CFG["claude"]["per_call_usd"].get(role, 10)),
            "--allowedTools", *BUILD_ALLOW, "--disallowedTools", *BUILD_DENY,
            # clean room: the repo's settings only, the routed rules in the system prompt, 7 skills on demand
            "--setting-sources", "project", *(["--plugin-dir", str(skills.PLUGIN)] if personal else []),
            *([] if personal or repo_skills else ["--disable-slash-commands"]),
            "--tools", *code_tools(role, personal, repo_skills), *effort_args(mid), "--settings", code_settings(mid)]
    for d in add_dirs:
        args += ["--add-dir", d]
    if not sid:
        args[0] = prompt + files_note(item_id, cwd)
    out = _invoke(args, role, item_id, cwd, timeout, env=CODE_ENV)
    note_files(item_id, out.get("session_id"), cwd)
    try:  # zero tokens: which skills did it still pull on demand? (skills.promote injects the usual ones)
        if personal:
            skills.record(skills.route_of(role, cwd), out.get("session_id"), cwd)
    except Exception as e:
        log("skill pull record failed", err=str(e)[:160])
    return {"text": out.get("result", ""), "usd": out.get("total_cost_usd"), "session": out.get("session_id"),
            "capped": out.get("subtype") == "error_max_budget_usd"}


# Datadog was shut down on 2026-10-12 and the user's rule is never to query it: its tools are not offered, so no call
# spends time on it and no prompt can send one there (10-15: #839's gather spent its turn deciding to skip it, and its
# stage note 'Opus + Datadog/Sentry' read as a Datadog query). Prod logs, metrics and the DB come from Grafana
TELEMETRY_TOOLS = [
    "ToolSearch", "Read", "Grep", "Glob",
    "mcp__claude_ai_Sentry__find_organizations", "mcp__claude_ai_Sentry__find_projects",
    "mcp__claude_ai_Sentry__search_issues", "mcp__claude_ai_Sentry__search_events",
    "mcp__claude_ai_Sentry__get_sentry_resource", "mcp__claude_ai_Sentry__analyze_issue_with_seer",
    # prod logs, metrics and traces: the local Grafana MCP (read-only, --disable-write); Datadog was shut down 10-12.
    # Data: the company's warehouse connector, Metabase only as a fallback since it is flaky
    #
    # (the install's own sources: config telemetry.tools)
    *((CFG.get("telemetry") or {}).get("tools") or []),
]


ORCH_TOOLS = [
    "Read", "Grep", "Glob", "ToolSearch",
    "Bash(sqlite3 -readonly:*)", "Bash(tail:*)", "Bash(grep:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(wc:*)",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)",
    "Bash(gh pr view:*)", "Bash(gh pr list:*)", "Bash(gh pr checks:*)", "Bash(gh pr diff:*)", "Bash(gh search:*)",
    "Bash(gh run view:*)", "Bash(gh run list:*)", "Bash(gh issue view:*)",
    "mcp__claude_ai_Slack__slack_read_channel", "mcp__claude_ai_Slack__slack_read_thread",
    "mcp__claude_ai_Slack__slack_search_public_and_private", "mcp__claude_ai_Slack__slack_search_public",
    "mcp__claude_ai_Slack__slack_search_channels", "mcp__claude_ai_Slack__slack_search_users",
    "mcp__claude_ai_Slack__slack_read_user_profile",
    *((CFG.get("knowledge_base") or {}).get("tools") or []),  # the company's own knowledge base, when the install has one
    *[t for t in TELEMETRY_TOOLS if t.startswith("mcp__")],
]
ORCH_DENY = ["Bash(gh api:*)", "Bash(gh pr merge:*)", "Bash(gh pr comment:*)", "Bash(gh pr edit:*)", "Bash(gh pr close:*)",
             "Bash(gh pr review:*)", "Bash(git push:*)", "Bash(git commit:*)", "Bash(rm:*)", "Bash(curl:*)",
             "mcp__claude_ai_Slack__slack_send_message", "mcp__claude_ai_Slack__slack_schedule_message",
             "mcp__claude_ai_Slack__slack_add_reaction", "Edit", "Write"]


def orchestrator(prompt: str, *, cwd: str, schema: dict, session: str | None = None, timeout: int = 600,
                 budget_usd: float | None = None, item_id: int | None = None, caller: str | None = None,
                 new_session_id: str | None = None) -> dict:
    """The DM thinker with the same reach as his interactive sessions: DB, logs, git, gh, Slack and knowledge-base reads, plus
    production evidence. Read-only by allow-list; writes only through the chat actions. One Claude session per
    conversation (--resume), so a follow-up remembers what it already looked up.
    item_id/caller: a case's decision made with this session is recorded on the case under 'chat:<caller>'. Recorded
    as bare 'chat' with no case, the thread-news decisions and escalations read as his own conversations."""
    if not can_spend("chat", True):
        raise BudgetExhausted("chat")
    # the first install thinks with its user's own Claude Code setup: the claude.ai connectors of the work account
    # (Slack, the company's data and docs) and the transcripts of the user's sessions. Any other install gets the
    # harness's own state, the repos, git and gh, read-only, and nothing of the user's personal setup
    connected = site.internal()
    dirs = [site.root()] + ([str(Path.home() / ".claude" / "projects")] if connected else [])
    if providers.engine() == "codex":
        with caller_ctx(caller or getattr(_pool_override, "caller", None)):
            out = _codex("chat", prompt, "opus", cwd, Path(_fast_file("chat")).read_text(), schema, "read-only", False,
                         tuple(dirs), item_id, timeout, resumable=False, session=session)
        if out.get("structured_output") is None:
            raise ClaudeError("chat: no structured_output")
        return {**out["structured_output"], "_session": out.get("session_id")}
    tools = ORCH_TOOLS if connected else [t for t in ORCH_TOOLS if not t.startswith("mcp__") and t != "ToolSearch"]
    # new_session_id: the caller names the transcript up front, so it can follow the session live (the console's progress)
    args = [prompt, *(["--resume", session] if session else ["--session-id", new_session_id] if new_session_id else []),
            "--model", providers.model_id("opus"),
            "--append-system-prompt-file", _fast_file("chat"),
            "--disable-slash-commands", "--max-budget-usd", str(budget_usd or CFG["claude"]["per_call_usd"].get("chat", 2)),
            "--tools", "Read", "Grep", "Glob", "Bash", *(["ToolSearch"] if connected else []),
            "--allowedTools", *tools, "--disallowedTools", *ORCH_DENY, "--json-schema", json.dumps(schema)]
    if not connected:
        args += ["--strict-mcp-config", "--mcp-config", EMPTY_MCP, "--setting-sources", ""]
    for d in dirs:
        args += ["--add-dir", d]
    with caller_ctx(caller or getattr(_pool_override, "caller", None)):
        out = _invoke(args, "chat", item_id, cwd, timeout)
    so = out.get("structured_output")
    if so is None:
        raise ClaudeError("chat: no structured_output")
    return {**so, "_session": out.get("session_id")}


def telemetry(prompt: str, *, cwd: str, schema: dict, item_id: int | None = None, urgent: bool = False,
              model: str = "opus") -> dict:
    """Exception to 'Muse analyzes': Datadog/Sentry deep-dives run on Opus directly with the org's
    MCP connectors, because exact numbers and judgment matter. Read-only tools
    only; the transcript must show real MCP calls or the result is rejected."""
    if not site.feature("telemetry") or providers.engine() != "claude":
        raise ConnectorsMissing("telemetry: this install has no production data connectors")
    if not can_spend("telemetry", urgent):
        raise BudgetExhausted("telemetry")
    need_connectors("telemetry")
    args = [prompt, "--model", CFG["claude"][model], "--append-system-prompt-file", system_file("telemetry"),
            "--tools", "ToolSearch", "Read", "Grep", "Glob", "--allowedTools", *TELEMETRY_TOOLS,
            "--disable-slash-commands", "--max-budget-usd", str(CFG["claude"]["per_call_usd"].get("telemetry", 4)),
            "--json-schema", json.dumps(schema)]
    out = _invoke(args, "telemetry", item_id, cwd, 1500)
    from .bridge import _mcp_calls
    if not _mcp_calls(out.get("session_id", "")):
        raise ClaudeError("telemetry: no MCP call in the transcript; refusing an unevidenced analysis")
    so = out.get("structured_output")
    if so is None:
        raise ClaudeError("telemetry: no structured_output")
    return so
