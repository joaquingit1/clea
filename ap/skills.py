"""Skill routing for Claude sessions.

Code sessions used to boot the user's personal Claude Code: a 100+ skill listing (~8.7k tokens: decks, Tetris, bug
bounty) and the model was trusted to pick. It rarely did: builds loaded cometa-gauntlet in 7 of 20 runs although
build.md says "every run", fixes loaded prepush-review in 17 of 79, reviews in 0 of 303. Now the harness decides:

  inject  the exact sections a route needs go into the system prompt, in a fixed order (cache-stable per route,
          and they survive a context compaction because the system prompt is never compacted)
  pull    a curated plugin (plugin/clea) offers ~7 engineering skills on demand instead of the whole listing
  learn   every on-demand load is counted per route; a skill pulled in >= 40% of a route's sessions (10+ seen)
          is injected for that route from then on (promote()), with no model call
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import db, site
from .core import CFG, HOME, log

SKILLS = Path.home() / ".claude" / "skills"
GLOBAL_RULES = Path.home() / ".claude" / "CLAUDE.md"
PLUGIN = HOME / "plugin" / "clea"
CODE_ROLES = ("build", "fix", "reproduce")

# route -> [(skill, [section headings])], from the install's config (skills.routes): which sections of the user's own
# skills a kind of session always gets. A heading takes its whole section, nested subsections included; a
# "## X > ### Y" pair takes the parent's own intro plus only that subsection. An install with no routes injects none.
ROUTES = {route: [(skill, list(heads)) for skill, heads in packs]
          for route, packs in ((CFG.get("skills") or {}).get("routes") or {}).items()}
_EXAMPLE = (CFG.get("skills") or {}).get("on_demand_example") or ""
OVERRIDE = ("Where these rules and your role instructions below disagree on HOW to run something (container tests through "
            "bin/ap-dtest, types through bin/ap-tsc, never `docker`, `make test` or a raw `tsc`; no push, the harness "
            "publishes), the role instructions win. The full skills, with their tools, are loadable on demand as "
            "`clea:<name>`" + (f" (e.g. {_EXAMPLE})" if _EXAMPLE else "") + ".")


def repo_of(cwd: str | None) -> str | None:
    """Worktrees are named <repo>--<slug>; a repo checkout or mirror is named after the repo."""
    if not cwd:
        return None
    name = Path(cwd).name
    repo = name.split("--", 1)[0] if "--" in name else name
    return repo if repo in CFG["repos"] else None


def route_of(role: str, cwd: str | None) -> str | None:
    if role in CODE_ROLES:
        repo = repo_of(cwd)
        return f"code@{repo}" if repo and f"code@{repo}" in ROUTES else "code"
    if role in ("agent-ap-review", "review_verify", "self_verify", "verify_done", "critique"):
        return "review"
    return None


def _sections(text: str, wanted: str, seen: set | None = None) -> str:
    """One '## Heading' section (up to the next heading of the same or a higher level), or with '## A > ### B' the
    intro of A (up to its first subsection, once per pack: `seen`) plus subsection B."""
    lines = text.splitlines()

    def grab(head: str, stop_at_sub: bool = False) -> list[str]:
        level = len(head) - len(head.lstrip("#"))
        try:
            i = next(n for n, l in enumerate(lines) if l.strip() == head.strip())
        except StopIteration:
            return []
        out = [lines[i]]
        for l in lines[i + 1:]:
            m = re.match(r"^(#+)\s", l)
            if m and (len(m.group(1)) <= level or (stop_at_sub and len(m.group(1)) > level)):
                break
            out.append(l)
        return out

    if " > " in wanted:
        parent, child = wanted.split(" > ", 1)
        sub = grab(child)
        if not sub:
            return ""
        intro = [] if seen is not None and parent in seen else grab(parent, stop_at_sub=True) + [""]
        if seen is not None:
            seen.add(parent)
        return "\n".join(intro + sub).strip()
    return "\n".join(grab(wanted)).strip()


def _skill_text(name: str) -> str:
    f = SKILLS / name / "SKILL.md"
    return f.read_text() if f.exists() else ""


def promoted(route: str) -> list[str]:
    return list(db.kv_get(f"skill_promoted:{route}") or [])


def pack(route: str | None) -> str:
    """The routed rules for a session, byte-identical for the same route and the same skill files."""
    if not route:
        return ""
    parts, missing, seen = [], [], set()
    for skill, heads in ROUTES.get(route, []):
        src = _skill_text(skill)
        for h in heads:
            sec = _sections(src, h, seen)
            if sec:
                parts.append(sec)
            else:
                missing.append(f"{skill}: {h[:60]}")
    for skill in promoted(route):  # learned: pulled in most sessions of this route, so it is always there now
        body = _skill_text(skill).split("---", 2)[-1].strip()
        if body:
            parts.append(f"## Skill {skill} (always loaded for this kind of session)\n{body}")
    if missing:  # a renamed heading must not silently drop a rule
        log("skill route section missing", route=route, missing=missing)
    if not parts:
        return ""
    return ("# Engineering rules for this session (loaded by the harness; follow them, do not re-load them)\n"
            + OVERRIDE + "\n\n" + "\n\n".join(parts))


def house_rules() -> str:
    """the user's global CLAUDE.md, which every session saw while it loaded his user settings (writing style, no AI
    trailers, scope, definition of done, the the company frontend/backend contract). Verbatim: it is his, not ours."""
    if not GLOBAL_RULES.exists():
        return ""
    return f"# {site.first_name()}'s standing instructions (apply them)\n" + GLOBAL_RULES.read_text().strip()


# ---- learn: count on-demand loads per route, promote what most sessions pull ----

PROMOTE_SHARE, PROMOTE_MIN = 0.4, 10


def record(route: str | None, session_id: str | None, cwd: str) -> list[str]:
    """After a session: which skills did it pull with the Skill tool? Counted per route; zero tokens."""
    if not route or not session_id:
        return []
    proj = Path.home() / ".claude" / "projects" / str(Path(cwd).resolve()).replace("/", "-").replace(".", "-")
    f = proj / f"{session_id}.jsonl"
    pulled = set()
    try:
        for line in f.open(errors="ignore"):
            if '"Skill"' not in line:
                continue
            o = json.loads(line)
            for b in (o.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                    pulled.add(str((b.get("input") or {}).get("skill", "")).split(":")[-1])
    except Exception:
        return []
    stats = db.kv_get(f"skill_pulls:{route}") or {"sessions": 0, "pulls": {}}
    stats["sessions"] += 1
    for s in pulled:
        stats["pulls"][s] = stats["pulls"].get(s, 0) + 1
    db.kv_set(f"skill_pulls:{route}", stats)
    return sorted(pulled)


def promote() -> dict:
    """Weekly: a skill pulled in >= 40% of a route's sessions (10+ sessions seen) is injected for that route."""
    done = {}
    for route in ROUTES:
        stats = db.kv_get(f"skill_pulls:{route}") or {}
        n = stats.get("sessions", 0)
        if n < PROMOTE_MIN:
            continue
        have = promoted(route)
        new = [s for s, k in stats.get("pulls", {}).items() if k / n >= PROMOTE_SHARE and s not in have and (SKILLS / s).exists()]
        if new:
            db.kv_set(f"skill_promoted:{route}", have + sorted(new))
            done[route] = new
            log("skills promoted into a route", route=route, skills=new, sessions=n)
    return done
