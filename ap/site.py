"""The install's own facts, read from config at call time: who the user is, where their code, chat and tickets live,
and what this install is allowed to do. The engine names no person, company or repository; anything it needs to say
or link comes from here, and the onboarding is what fills the config behind it."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .core import CFG, DATA


# ---- edition ----------------------------------------------------------------------------------------------------
def internal() -> bool:
    """The first install: v1 config, personal skills and connectors, the self-improvement layer."""
    return CFG.get("edition") == "internal"


def setup_complete() -> bool:
    """Whether this install knows enough to work: who its user is, where to read and which engine thinks. The internal
    install was set up by hand and always is."""
    return internal() or bool((CFG.get("setup") or {}).get("complete"))


def self_improving() -> bool:
    """The layer that changes the harness from its own outcomes (observer, incidents, supervisor, ratchet, rule and
    style learning, evals). Off unless the install turns it on; a release never ships it on."""
    return bool((CFG.get("self_improvement") or {}).get("enabled"))


def feature(name: str) -> bool:
    return bool((CFG.get("features") or {}).get(name))


# ---- the user -----------------------------------------------------------------------------------------------------
def me() -> str:
    """The user's Slack id."""
    return CFG.get("self_user_id") or ""


def user_name() -> str:
    return ((CFG.get("user") or {}).get("name") or "").strip() or "the user"


def first_name() -> str:
    """The user's first name, or 'the user' while the install has no name for them."""
    name = ((CFG.get("user") or {}).get("name") or "").strip()
    return name.split(" ")[0] if name else "the user"


def lang() -> str:
    return (CFG.get("language") or "en")[:2].lower()


LANGUAGES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian"}


def t(en: str, es: str) -> str:
    """A string the code itself writes (a note, a label, an ask line), in the install's language. The engine was
    written for a Spanish install and its own strings were Spanish; every other language reads the English one."""
    return es if lang() == "es" else en


def own(first: str, neutral: str) -> str:
    """A piece of a prompt or of a schema that the first install wrote for itself (its language, its voice, examples
    from its own history): that install keeps it word for word, every other one reads the neutral piece."""
    return first if internal() else neutral


ASSISTANT = re.compile(r"\bclea\b", re.I)  # the assistant's name: the user's word for it, never one a teammate reads


def language_name() -> str:
    return LANGUAGES.get(lang(), "English")


def git_identity() -> list[str]:
    """`-c` arguments for a commit the harness makes in its own repo."""
    u = CFG.get("user") or {}
    out = []
    if u.get("name"):
        out += ["-c", f"user.name={u['name']}"]
    if u.get("email"):
        out += ["-c", f"user.email={u['email']}"]
    return out


# ---- code ---------------------------------------------------------------------------------------------------------
def org() -> str:
    return CFG.get("github_org") or ""


def repos() -> dict:
    return CFG.get("repos") or {}


def repo_org(repo: str) -> str:
    return (repos().get(repo) or {}).get("org") or org()


def slug(repo: str) -> str:
    """owner/name on GitHub. A repo's config may name its own owner (org) and its GitHub name when the short name the
    harness uses differs."""
    r = repos().get(repo) or {}
    return f"{r.get('org') or org()}/{r.get('github') or repo}"


def repo_url(repo: str) -> str:
    return f"https://github.com/{slug(repo)}"


def pr_url(repo: str, n: int | str) -> str:
    return f"{repo_url(repo)}/pull/{n}"


def _sig() -> tuple:
    return (org(), tuple(sorted(repos())), tuple(sorted((r.get("org") or "", r.get("github") or "") for r in repos().values())))


@lru_cache(maxsize=8)
def _pr_re(sig: tuple, any_repo: bool) -> re.Pattern:
    owners = sorted({o for o in [sig[0], *(o for o, _ in sig[2])] if o})
    owner = "|".join(re.escape(o) for o in owners) or r"[\w.-]+"
    names = r"[\w.-]+" if any_repo or not sig[1] else "|".join(re.escape(n) for n in sorted(sig[1], key=len, reverse=True))
    return re.compile(rf"github\.com/(?:{owner})/({names})/pull/(\d+)")


def pr_link_re(any_repo: bool = False) -> re.Pattern:
    """A link to a PR of this install's org; group 1 is the repo, group 2 the number. By default only the configured
    repos match (a PR in a repo the harness does not work in is not its to act on)."""
    return _pr_re(_sig(), any_repo)


def repo_path(repo: str) -> str:
    return os.path.expanduser((repos().get(repo) or {}).get("path") or "")


def primary_repo() -> str:
    """The repo a judgment about no repo in particular runs in."""
    p = CFG.get("primary_repo")
    return p if p in repos() else next(iter(repos()), "")


def root() -> str:
    """Where a session that reads across repos runs: the folder the repos live in (config paths.repos_root, or their
    common parent), or the install's home when it has no repo yet."""
    r = (CFG.get("paths") or {}).get("repos_root")
    if r:
        return os.path.expanduser(r)
    paths = [repo_path(n) for n in repos() if repo_path(n)]
    if paths:
        try:
            return os.path.commonpath(paths) if len(paths) > 1 else str(Path(paths[0]).parent)
        except ValueError:
            pass
    return str(DATA)


def primary_path() -> str:
    """The primary repo's checkout, or root() when there is none: a cwd that always exists."""
    p = repo_path(primary_repo()) if primary_repo() else ""
    return p if p and os.path.isdir(p) else root()


# ---- chat ---------------------------------------------------------------------------------------------------------
def slack_base() -> str:
    return ((CFG.get("slack") or {}).get("workspace_url") or "https://slack.com").rstrip("/")


def slack_link(channel: str, ts: str, thread_ts: str | None = None) -> str:
    url = f"{slack_base()}/archives/{channel}/p{str(ts).replace('.', '')}"
    return url + (f"?thread_ts={thread_ts}&cid={channel}" if thread_ts and thread_ts != ts else "")


def keywords() -> list[str]:
    """Words that make a message this install's business wherever it is written (the user's area, product or team)."""
    return [k for k in ((CFG.get("slack") or {}).get("keywords") or []) if k and k.strip()]


@lru_cache(maxsize=8)
def _kw_re(words: tuple) -> re.Pattern | None:
    return re.compile("|".join(re.escape(w) for w in words), re.I) if words else None


def keyword_re() -> re.Pattern | None:
    return _kw_re(tuple(keywords()))


# ---- tickets ------------------------------------------------------------------------------------------------------
def jira_base() -> str:
    return (CFG.get("jira_base") or "").rstrip("/")


def ticket_url(key: str) -> str:
    return f"{jira_base()}/browse/{key}" if jira_base() else ""


def ticket_prefixes() -> list[str]:
    return [p for p in ((CFG.get("tickets") or {}).get("prefixes") or []) if p]


@lru_cache(maxsize=8)
def _ticket_re(prefixes: tuple) -> re.Pattern:
    body = "|".join(re.escape(p) for p in prefixes) if prefixes else r"[A-Z][A-Z0-9]{1,9}"
    return re.compile(rf"\b((?:{body})-\d{{1,6}})\b", re.I if prefixes else 0)


def ticket_re() -> re.Pattern:
    """A ticket key of this install's trackers. With no prefixes configured, any KEY-123 in capitals."""
    return _ticket_re(tuple(ticket_prefixes()))


# ---- how prompts name the user, the team and the company ------------------------------------------------------------
def marker() -> str:
    """How the user's own messages are tagged in a transcript a model reads."""
    return first_name().upper()


def aliases() -> list[str]:
    """What people call the user in chat, lower case (config user.aliases, else the first name)."""
    u = CFG.get("user") or {}
    return [a.lower() for a in u.get("aliases") or ([first_name()] if (u.get("name") or "").strip() else []) if a]


def name_fragments() -> list[str]:
    """Pieces that identify the user in a name someone else typed, lower case (config user.match, else the first
    name): an assignee field carries accents and nicknames."""
    u = CFG.get("user") or {}
    return [a.lower() for a in u.get("match") or ([first_name()] if (u.get("name") or "").strip() else []) if a]


def team_name() -> str:
    """The user's team or area as a plain word ('payments'); '' when the install names none."""
    return (CFG.get("team_name") or "").strip()


def team_label() -> str:
    """The team as people write it (config team_label: accents, capitals), else its plain word."""
    return (CFG.get("team_label") or "").strip() or team_name()


def of_team(noun: str, written: bool = False) -> str:
    """'<team> <noun>' for a prompt ('payments squad'), or the noun alone when the install names no team. `written`:
    the team as people write it instead of the plain word."""
    t = team_label() if written else team_name()
    return f"{t} {noun}" if t else noun


def company() -> str:
    """The company the user works at, as a prompt names it; '' when the install does not say."""
    return (CFG.get("company") or "").strip()


def research_sources() -> str:
    """Where an answer's findings come from, as a report lists them (config research_sources: the install's own
    knowledge tools go here)."""
    return (CFG.get("research_sources") or "").strip() or "code, precedents"


def db_source_re() -> str:
    """How the install's own database shows up in a telemetry source's name (config telemetry.db_source), as a regex;
    '' when it has no name of its own."""
    return (CFG.get("telemetry") or {}).get("db_source") or ""


def repos_note() -> str:
    """One line telling a model what each repo is and how it ships (config merge.repos_note, else what the repo
    config says)."""
    own = (CFG.get("merge") or {}).get("repos_note")
    if own:
        return own
    bits = []
    for name, r in repos().items():
        layers = sorted(set((r.get("layers") or {}).values())) or [r.get("layer") or "code"]
        bits.append(f"{name}: {' and '.join(layers)}" + (", live on merge" if r.get("frontend_live_on_merge") else ""))
    return "; ".join(bits)


def repos_listed() -> str:
    """The configured repos by name, the primary one first."""
    return ", ".join(dict.fromkeys([primary_repo(), *repos()])) if repos() else ""


# ---- what the modules build their own patterns from ---------------------------------------------------------------
# A module that needs a pattern of its own shape (extra groups, a different tail) composes it from these pieces, so
# the shape stays the module's and the names stay the install's.
def owners() -> list[str]:
    """The GitHub owners of this install's repos: the org, plus any owner a repo names for itself."""
    return sorted({o for o in [org(), *((r.get("org") or "") for r in repos().values())] if o}, key=len, reverse=True)


def owners_re() -> str:
    """The owners as a regex alternation; any owner when none is configured."""
    return "|".join(re.escape(o) for o in owners()) or r"[\w.-]+"


def repo_names() -> list[str]:
    """Configured repo names, longest first: in an alternation a name must never shadow a longer one."""
    return sorted(repos(), key=len, reverse=True)


def repo_names_re() -> str:
    """The configured repos as a regex alternation; any name when no repo is configured."""
    return "|".join(re.escape(n) for n in repo_names()) or r"[\w.-]+"


def repo_name(repo: str) -> str:
    """The repo's name on GitHub, when it differs from the short name the harness uses."""
    return (repos().get(repo) or {}).get("github") or repo


def repo_or(generic: str) -> str:
    """A module's own repo-name class, widened by the configured names it would miss (a digit, a dot, capitals)."""
    return f"{generic}|{repo_names_re()}"


def short_ref(ref: str) -> str:
    """owner/repo#n as repo#n: models answer with either form."""
    ref = ref or ""
    for o in owners():
        ref = ref.replace(f"{o}/", "")
    return ref


def has_layer(repo: str, layer: str) -> bool:
    """Whether a repo holds that layer (frontend, backend): its config says `layer` for a single-layer repo or
    `layers` (path prefix -> layer) for a monorepo. A repo the config does not know holds none."""
    r = repos().get(repo) or {}
    return r.get("layer") == layer or layer in (r.get("layers") or {}).values()


def layer_dir(repo: str, layer: str) -> str:
    """The path prefix of a layer inside a monorepo ('backend/'), or '' when the repo is that layer from its root."""
    for prefix, name in ((repos().get(repo) or {}).get("layers") or {}).items():
        if name == layer:
            return prefix
    return ""


def repo_with(layer: str, monorepo: bool | None = None) -> str:
    """The first configured repo holding that layer (`monorepo` narrows to repos that do or do not split by path),
    or the primary repo when none says so."""
    for name, r in repos().items():
        if has_layer(name, layer) and (monorepo is None or bool(r.get("layers")) == monorepo):
            return name
    return primary_repo()


def slack_host_re() -> str:
    """The workspace's host as a regex; any workspace when none is configured."""
    url = ((CFG.get("slack") or {}).get("workspace_url") or "").rstrip("/")
    return re.escape(re.sub(r"^https?://", "", url)) if url else r"[\w-]+\.slack\.com"


@lru_cache(maxsize=32)
def _compiled(pattern: str, flags: int) -> re.Pattern:
    return re.compile(pattern, flags)


def channel_name_re() -> re.Pattern | None:
    """Channels that are this install's business by their name alone (config slack.channel_name_re). None: no channel
    is matched by name, only the ones the config lists."""
    pat = (CFG.get("slack") or {}).get("channel_name_re") or ""
    return _compiled(pat, 0) if pat else None


def ticket_bot() -> str:
    """The Slack user id of the bot that files tickets in threads; '' when the workspace has none."""
    return (CFG.get("slack") or {}).get("ticket_bot") or ""


def team_tag() -> str:
    """How people type the team's Slack group ('@backend'); '' when the install names none."""
    return (CFG.get("slack") or {}).get("team_tag") or ""


def team_tag_note() -> str:
    """What a rendered team-group tag carries after its name, so a model knows whose group it is; case texts are
    searched for it to tell that the group was tagged."""
    return f"({first_name()}'s squad group)"


def team_tag_label() -> str:
    """What a team-group tag is rendered as in a text a model reads."""
    return f"{team_tag() or '@team'} {team_tag_note()}"


def channel_label(key: str, fallback: str) -> str:
    """A configured channel (team_channel, pr_channel) by its name when the config knows it, else a plain
    description: for text built while a module loads, when Slack cannot be asked."""
    return (CFG.get("channel_names") or {}).get(CFG.get(key) or "") or fallback


def design_owner() -> str:
    """Who has the final call on design and product decisions; '' when the install names nobody."""
    return (CFG.get("actionables") or {}).get("design_final_call") or ""


def ticket_prefix() -> str:
    """The prefix an example ticket key carries in a prompt: the first one this install closes, else its first."""
    return (prefixes("close") or ticket_prefixes() or ["ABC"])[0]


def keychain(name: str) -> str:
    """The keychain service a secret is stored under (slack: the user token, slack_app: the socket-mode app token,
    langfuse_pk and langfuse_sk: tracing)."""
    s, lf = CFG.get("slack") or {}, CFG.get("langfuse") or {}
    return {"slack": s.get("keychain") or "clea-slack-user-token", "slack_app": s.get("app_keychain") or "clea-slack-app-token",
            "langfuse_pk": lf.get("keychain_pk") or "clea-langfuse-pk", "langfuse_sk": lf.get("keychain_sk") or "clea-langfuse-sk"}[name]


# ---- tickets, per use ---------------------------------------------------------------------------------------------
def prefixes(use: str = "") -> list[str]:
    """Ticket prefixes for one use (config tickets.<use>_prefixes): every configured prefix when the config does not
    name that use, none when it names it empty."""
    t = CFG.get("tickets") or {}
    own = t.get(f"{use}_prefixes") if use else None
    every = t.get("prefixes") or (CFG.get("jira") or {}).get("projects") or []  # the consumed projects are known keys too
    return [p for p in (every if own is None else own) if p]


def ticket_key(use: str = "", digits: str = r"\d{1,6}") -> str:
    """A ticket key as a regex piece with no group and no anchors, for the patterns a module shapes itself. With no
    prefix configured, any KEY-123 in capitals (compile it case-sensitive then: see ticket_flags)."""
    ps = prefixes(use)
    body = "|".join(re.escape(p) for p in ps) if ps else r"[A-Z][A-Z0-9]{1,9}"
    return rf"(?:{body})-{digits}"


def ticket_digits(use: str = "") -> str:
    """How many digits a ticket a case can BE has. With the install's prefixes known, one is enough (a young project's
    ENG-7 opened one case from Slack and another from Jira); with any capitals-and-number accepted, two keep 'UTF-8'
    and 'SHA-1' out. The first install keeps its two."""
    return r"\d{1,6}" if prefixes(use) and not internal() else r"\d{2,6}"


def ticket_flags(use: str = "") -> int:
    """re.I only when the prefixes are known: the any-key fallback in lower case would match 'utf-8'."""
    return re.I if prefixes(use) else 0


def drop_re() -> re.Pattern | None:
    """Cases this install never opens, by what their title or text says (config tickets.drop_title)."""
    pat = (CFG.get("tickets") or {}).get("drop_title") or ""
    return _compiled(pat, re.I) if pat else None


def drop_note() -> str:
    """Why such a case was dropped, for its history (config tickets.drop_note)."""
    return ((CFG.get("tickets") or {}).get("drop_note")
            or "it matches what this install never takes as a case (tickets.drop_title, tickets.drop_authors)")


def drop_authors() -> list[str]:
    return [a.lower() for a in ((CFG.get("tickets") or {}).get("drop_authors") or []) if a]


def jira() -> dict:
    return CFG.get("jira") or {}


# ---- the fence: what part of the watched channels and repos is the user's ------------------------------------------
def fence() -> dict:
    return CFG.get("fence") or {}


def fence_re(key: str = "") -> re.Pattern | None:
    """The fence as a pattern over text: the install's own pattern for this use (config fence.<key>), else its fence
    keywords as plain words. None when the install has no fence: everything it watches is in scope."""
    f = fence()
    pat = (f.get(key) if key else "") or "|".join(re.escape(w.strip()) for w in f.get("keywords") or [] if w and w.strip())
    return _compiled(pat, re.I) if pat else None


def in_fence(text: str, key: str = "") -> bool:
    rx = fence_re(key)
    return True if rx is None else bool(rx.search(text or ""))


def fence_paths() -> dict:
    """repo -> path prefixes inside the fence (config fence.paths). A configured repo it does not name is in whole
    when the install names no path at all, and out when it names paths for other repos."""
    return {r: list(p) for r, p in (fence().get("paths") or {}).items() if p}


def stop_words(use: str) -> set[str]:
    """Words too common in this install's titles and branches to tell two pieces of work apart (its product, its
    area). Config fence.stop_words: one list for every use, or a list per use."""
    w = fence().get("stop_words") or []
    return set((w.get(use) or w.get("*") or []) if isinstance(w, dict) else w)


# ---- places on this machine ---------------------------------------------------------------------------------------
def path(key: str, internal_default: str = "") -> Path | None:
    """A file or folder outside the install that only some installs have (config paths.<key>). The first install's
    own location applies to it alone; anywhere else an unset path is None and the feature that reads it is off."""
    v = (CFG.get("paths") or {}).get(key) or (internal_default if internal() else "")
    return Path(v).expanduser() if v else None
