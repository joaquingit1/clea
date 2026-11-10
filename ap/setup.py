"""The onboarding: what a new install asks its user, and how the answers become the engine's config.

The engine reads its whole world from config (who the user is, which channels and repos, which team, which tickets,
how much it may do alone). The first install had that written by hand; every other install gets it here, from the
app's setup screens (ui/src/components/onboarding) through the console API (/api/setup/*). Each step checks what the
user gave against the real service before saving it (a Slack token against Slack, a key through the engine that will
use it), stores secrets in the vault and everything else in the install's config.json, and nothing here ever returns
a secret. `finish` marks the install set up; the worker then restarts into normal mode with the new config.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import providers, site, vault
from .core import CFG, DATA, PATH_PREFIX, env, log, save_config

STEPS = ("welcome", "model", "slack", "code", "team", "tickets", "autonomy")
OPTIONAL = ("tickets",)
ROLES = ("team", "prs", "alerts", "watch")
# what the engine does with each scope; a token without one still connects, and the screen says what stops working
SCOPES = {
    "channels:history": "read public channels", "groups:history": "read private channels", "im:history": "read your DMs",
    "mpim:history": "read group DMs", "channels:read": "list channels", "groups:read": "list private channels",
    "im:read": "list your DMs", "mpim:read": "list group DMs", "users:read": "show who wrote a message",
    "usergroups:read": "see your user groups", "search:read": "find mentions of you anywhere",
    "chat:write": "post in your own DM and send the replies you approve", "reactions:read": "read your approvals by reaction",
    "files:read": "open screenshots and files in threads", "pins:write": "pin the board in your DM",
}
_cache: dict = {}
_lock = threading.Lock()


class Bad(ValueError):
    """What the user gave does not work; the message is theirs to read. `missing`: the steps still to do (finish)."""

    def __init__(self, msg: str, missing: list | None = None):
        super().__init__(msg)
        self.missing = missing or []


def _t(en: str, es: str) -> str:
    return es if site.lang() == "es" else en


def _done(step: str) -> None:
    done = list((CFG.get("setup") or {}).get("done") or [])
    if step not in done:
        save_config({"setup": {"done": done + [step]}})


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# ---- state ---------------------------------------------------------------------------------------------------------
def running() -> bool:
    """The worker is past setup mode: its pools and scheduler run."""
    import sys
    w = sys.modules.get("ap.worker")
    return bool(w and getattr(w, "RUNNING", False))


def state() -> dict:
    s = CFG.get("setup") or {}
    done = set(s.get("done") or [])
    sl, gh, tm, jr = slack_state(), github_state(), team_state(), jira_state()
    prov = providers.selected()
    summaries = {
        "welcome": site.user_name() if (CFG.get("user") or {}).get("name") else "",
        "model": next((f"{p['vendor']}: {p['account'] or p['plan'] or p['id']}" for p in providers.states() if p["selected"] and p["ready"]), ""),
        "slack": (f"{sl['workspace']['name']}: " + _t(f"{len(sl['channels'])} channels", f"{len(sl['channels'])} canales")) if sl["connected"] and sl["channels"] else "",
        "code": (f"{gh['org']}: " + ", ".join(r["name"] for r in gh["repos"][:4]) + ("..." if len(gh["repos"]) > 4 else "")) if gh["repos"] else "",
        "team": (_t(f"{len(tm['members'])} teammates", f"{len(tm['members'])} personas") if tm["members"] else "") or tm["area"][:60],
        "tickets": (f"Jira: {', '.join(jr['projects'][:4]) or jr['base']}" if jr["enabled"] and jr["connected"] else ""),
        "autonomy": {"watch": _t("Watch", "Observar"), "ask": _t("Ask first", "Preguntar primero"),
                     "auto": _t("Autonomous", "Autónomo")}.get(CFG.get("autonomy") or "", ""),
    }
    ok = {"welcome": bool(summaries["welcome"]), "model": providers.ready(),
          "slack": sl["connected"] and any(c["role"] == "team" for c in sl["channels"]),
          "code": gh["connected"] and bool(gh["repos"]), "team": "team" in done,
          "tickets": "tickets" in done, "autonomy": "autonomy" in done}
    return {"edition": CFG.get("edition"), "complete": site.setup_complete(), "running": running(), "language": site.lang(),
            "user": {"name": (CFG.get("user") or {}).get("name") or ""},
            "steps": [{"id": k, "done": bool(ok[k]), "optional": k in OPTIONAL, "summary": summaries[k] if ok[k] else ""} for k in STEPS],
            "provider": prov, "providers": providers.states(), "slack": sl, "github": gh, "team": tm, "jira": jr,
            "autonomy": CFG.get("autonomy") or "ask",
            "sees_production": site.feature("telemetry")}  # what the app may say Clea reads before it answers


def missing() -> list[str]:
    return [s["id"] for s in state()["steps"] if not s["done"] and not s["optional"] and s["id"] != "autonomy"]


# ---- profile ---------------------------------------------------------------------------------------------------------
def save_profile(name: str, language: str) -> None:
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        raise Bad(_t("Tell Clea your name.", "Decile a Clea tu nombre."))
    if language not in ("en", "es"):
        raise Bad("language must be en or es")
    save_config({"user": {"name": name[:80]}, "language": language})
    _done("welcome")


# ---- Slack -----------------------------------------------------------------------------------------------------------
def _slack(token: str | None = None):
    from slack_sdk import WebClient
    tok = token or vault.get("slack-user-token")
    if not tok:
        raise Bad(_t("Connect Slack first.", "Conectá Slack primero."))
    return WebClient(token=tok, timeout=30)


def manifest_url() -> str:
    """Slack's 'create an app from a manifest' page, prefilled: the user's own app in their own workspace, acting as
    them (user scopes only, no bot), with Socket Mode so new messages arrive at once instead of on the next poll."""
    manifest = {
        "display_information": {"name": "Clea", "description": "Clea reads the channels you choose and drafts your work for your approval.",
                                "background_color": "#2135f4"},
        "oauth_config": {"scopes": {"user": sorted(SCOPES)}},
        "settings": {"socket_mode_enabled": True, "org_deploy_enabled": False, "token_rotation_enabled": False,
                     "event_subscriptions": {"user_events": ["message.channels", "message.groups", "message.im", "message.mpim"]}},
    }
    return "https://api.slack.com/apps?new_app=1&manifest_json=" + urllib.parse.quote(json.dumps(manifest, separators=(",", ":")))


def _self_dm(token: str, me: str) -> str:
    """The user's DM with themselves: where Clea reports and where the user talks to it from Slack. Found among their
    DMs (im:read); opening one would need a write scope the app does not otherwise ask for."""
    from slack_sdk.errors import SlackApiError
    try:
        cursor = None
        for _ in range(10):
            d = _slack(token).conversations_list(types="im", limit=1000, cursor=cursor).data
            for c in d.get("channels", []):
                if c.get("user") == me:
                    return c["id"]
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as e:
        log("setup: self DM not found", err=str(e.response.get("error"))[:80])
    return ""


def slack_connect(user_token: str, app_token: str = "") -> dict:
    from slack_sdk.errors import SlackApiError
    user_token, app_token = (user_token or "").strip(), (app_token or "").strip()
    if not user_token.startswith("xoxp-"):
        raise Bad(_t("That is not a User OAuth Token: it starts with xoxp-. It is on your Slack app's OAuth & Permissions page.",
                     "Eso no es un User OAuth Token: empieza con xoxp-. Está en la página OAuth & Permissions de tu app de Slack."))
    if app_token and not app_token.startswith("xapp-"):
        raise Bad(_t("The App-Level Token starts with xapp-.", "El App-Level Token empieza con xapp-."))
    try:
        r = _slack(user_token).auth_test()
    except SlackApiError as e:
        raise Bad(_t(f"Slack refused the token: {e.response.get('error')}.", f"Slack rechazó el token: {e.response.get('error')}.")) from None
    who = r.data
    have = {s.strip() for s in (r.headers.get("x-oauth-scopes") or "").split(",") if s.strip()}
    dm = _self_dm(user_token, who["user_id"])
    vault.put("slack-user-token", user_token)
    if app_token:
        vault.put("slack-app-token", app_token)
    save_config({"self_user_id": who["user_id"], "self_dm_channel": dm,
                 "slack": {"workspace_url": (who.get("url") or "").rstrip("/"), "workspace": who.get("team") or "",
                           "team_id": who.get("team_id") or "", "user_name": who.get("user") or "",
                           "scopes": sorted(have), "socket_mode": bool(app_token or vault.get("slack-app-token"))}})
    _cache.clear()
    log("setup: slack connected", team=who.get("team"), missing=sorted(set(SCOPES) - have))
    return slack_state()


def slack_state() -> dict:
    sl = CFG.get("slack") or {}
    connected = bool(CFG.get("self_user_id") and sl.get("team_id") and vault.get("slack-user-token")) or \
        (site.internal() and bool(CFG.get("self_user_id")))
    names = CFG.get("channel_names") or {}
    chans = []
    for cid, role in (((CFG.get("team_channel") or ""), "team"), ((CFG.get("pr_channel") or ""), "prs"),
                      ((CFG.get("alerts_channel") or ""), "alerts")):
        if cid:
            chans.append({"id": cid, "name": names.get(cid, cid).lstrip("#"), "role": role})
    for cid in (CFG.get("audiences") or {}).get("team_channels") or []:
        if cid and cid not in [c["id"] for c in chans]:
            chans.append({"id": cid, "name": names.get(cid, cid).lstrip("#"), "role": "team"})
    for cid in CFG.get("extra_alerts_channels") or []:
        chans.append({"id": cid, "name": names.get(cid, cid).lstrip("#"), "role": "alerts"})
    for cid in CFG.get("watched_channels") or []:
        if cid not in [c["id"] for c in chans]:
            chans.append({"id": cid, "name": names.get(cid, cid).lstrip("#"), "role": "watch"})
    groups = sl.get("usergroups") or [{"id": g, "handle": g} for g in CFG.get("team_subteam_tags") or []]
    have = set(sl.get("scopes") or [])
    return {"connected": connected,
            "workspace": {"name": sl.get("workspace") or "", "url": sl.get("workspace_url") or "", "team_id": sl.get("team_id") or ""} if connected else None,
            "user": {"id": CFG.get("self_user_id") or "", "name": sl.get("user_name") or site.user_name()} if connected else None,
            "missing_scopes": sorted(set(SCOPES) - have) if have else [],
            "socket": bool(sl.get("socket_mode") and (vault.get("slack-app-token") or site.internal())),
            "channels": chans, "usergroups": groups, "keywords": site.keywords(),
            "scan_dms": bool(sl.get("scan_dms", True)), "mentions": bool(sl.get("mentions", True)), "manifest_url": manifest_url()}


def _slack_error(e, what: str) -> Bad:
    """Slack's refusal in words the user can act on. A missing scope names the scope and where to add it."""
    err = (getattr(e, "response", None) or {}).get("error") or str(e)[:120]
    need = (getattr(e, "response", None) or {}).get("needed")
    if err == "missing_scope" and need:
        return Bad(_t(f"Your Slack token cannot {what}: it lacks the scope {need}. Add it under User Token Scopes in your Slack "
                      f"app, reinstall the app and paste the new token.",
                      f"Tu token de Slack no puede {what}: le falta el scope {need}. Agregalo en User Token Scopes de tu app de "
                      f"Slack, reinstalá la app y pegá el token nuevo."))
    if err in ("invalid_auth", "token_revoked", "account_inactive", "not_authed"):
        return Bad(_t("Slack no longer accepts the token. Connect Slack again.", "Slack ya no acepta el token. Conectá Slack de nuevo."))
    return Bad(_t(f"Slack answered: {err}.", f"Slack respondió: {err}."))


def _recent_channels() -> list[dict]:
    """Channels the user wrote in lately, from message search: what a token without channels:read can still show, so
    the picker is never empty for someone whose Slack app was made before the list scopes were asked for."""
    me, seen, out = CFG.get("self_user_id"), set(), []
    for page in (1, 2, 3):
        d = _slack().search_messages(query=f"from:<@{me}>", count=100, page=page, sort="timestamp").data
        for m in (d.get("messages") or {}).get("matches", []):
            c = m.get("channel") or {}
            if c.get("id") and c["id"] not in seen and (c.get("is_channel") or c.get("is_group")) and not c.get("is_mpim"):
                seen.add(c["id"])
                out.append({"id": c["id"], "name": c.get("name") or c["id"], "private": bool(c.get("is_private")), "member": True,
                            "members": 0, "purpose": ""})
        if page >= ((d.get("messages") or {}).get("paging") or {}).get("pages", 1):
            break
    return out


def _all_channels() -> list[dict]:
    from slack_sdk.errors import SlackApiError

    def read():
        out, cursor = [], None
        try:
            for _ in range(20):
                d = _slack().conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=1000, cursor=cursor).data
                out += d.get("channels", [])
                cursor = (d.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as e:
            if e.response.get("error") != "missing_scope":
                raise _slack_error(e, "list channels") from None
            try:
                return _recent_channels()
            except SlackApiError as e2:
                raise _slack_error(e2, _t("list channels", "listar canales")) from None
        return [{"id": c["id"], "name": c.get("name") or c["id"], "private": bool(c.get("is_private")), "member": bool(c.get("is_member")),
                 "members": c.get("num_members") or 0, "purpose": ((c.get("purpose") or {}).get("value") or "")[:140]} for c in out]
    return _cached("channels", 300, read)


def slack_channels(q: str = "") -> list[dict]:
    q = (q or "").strip().lower().lstrip("#")
    rows = [c for c in _all_channels() if q in c["name"].lower()]
    rows.sort(key=lambda c: (not c["member"], not c["name"].lower().startswith(q) if q else False, -c["members"], c["name"]))
    return rows[:80]


def slack_usergroups() -> list[dict]:
    from slack_sdk.errors import SlackApiError

    def read():
        me = CFG.get("self_user_id")
        try:
            gs = _slack().usergroups_list(include_users=True, include_count=True).data.get("usergroups", [])
        except SlackApiError as e:
            if e.response.get("error") == "missing_scope":
                return []  # the screen already lists the missing scope; no groups to offer is not an error
            raise _slack_error(e, "list user groups") from None
        rows = [{"id": g["id"], "handle": g.get("handle") or "", "name": g.get("name") or "", "members": g.get("user_count") or len(g.get("users") or []),
                 "mine": me in (g.get("users") or [])} for g in gs if not g.get("date_delete")]
        return sorted(rows, key=lambda g: (not g["mine"], g["handle"]))
    return _cached("usergroups", 300, read)


def _all_users() -> list[dict]:
    from slack_sdk.errors import SlackApiError

    def read():
        out, cursor = [], None
        try:
            for _ in range(20):
                d = _slack().users_list(limit=1000, cursor=cursor).data
                out += d.get("members", [])
                cursor = (d.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as e:
            raise _slack_error(e, _t("list people", "listar personas")) from None
        return [{"id": u["id"], "name": u.get("name") or "", "real_name": u.get("real_name") or (u.get("profile") or {}).get("real_name") or "",
                 "title": ((u.get("profile") or {}).get("title") or "")[:80]}
                for u in out if not u.get("deleted") and not u.get("is_bot") and not u.get("is_app_user") and u["id"] != "USLACKBOT"]
    return _cached("users", 600, read)


def slack_users(q: str = "") -> list[dict]:
    q = (q or "").strip().lower().lstrip("@")
    rows = [u for u in _all_users() if q in u["name"].lower() or q in u["real_name"].lower()]
    return sorted(rows, key=lambda u: u["real_name"].lower())[:50]


def slack_save(channels: list, usergroups: list, keywords: list, scan_dms: bool, mentions: bool) -> dict:
    seen, rows = set(), []
    for c in channels or []:
        if not isinstance(c, dict) or not re.fullmatch(r"[CG][A-Z0-9]{6,}", str(c.get("id") or "")) or c.get("role") not in ROLES:
            raise Bad("each channel needs an id and one of the roles team, prs, alerts, watch")
        if c["id"] not in seen:
            seen.add(c["id"])
            rows.append(c)
    by = {r: [c["id"] for c in rows if c["role"] == r] for r in ROLES}
    if not by["team"]:
        raise Bad(_t("Pick your team's channel: it is where Clea treats every question as yours to answer.",
                     "Elegí el canal de tu equipo: ahí Clea toma cada pregunta como tuya para responder."))
    words = list(dict.fromkeys(re.sub(r"\s+", " ", str(k)).strip() for k in keywords or [] if str(k).strip()))[:30]
    groups = [{"id": g["id"], "handle": str(g.get("handle") or "")[:60]} for g in usergroups or []
              if isinstance(g, dict) and re.fullmatch(r"S[A-Z0-9]{6,}", str(g.get("id") or ""))]
    # the engine knows one channel of each kind by name and reads the rest as watched ones; a second team channel is
    # read like the first through the audiences list (who reads a channel decides how a draft is written)
    save_config({"team_channel": by["team"][0], "pr_channel": (by["prs"] or [""])[0], "alerts_channel": (by["alerts"] or [""])[0],
                 "extra_alerts_channels": by["alerts"][1:], "watched_channels": by["watch"] + by["team"][1:] + by["prs"][1:],
                 "audiences": {"team_channels": by["team"]},
                 "channel_names": {c["id"]: "#" + str(c.get("name") or c["id"]).lstrip("#") for c in rows},
                 "team_subteam_tags": [g["id"] for g in groups],
                 "slack": {"usergroups": groups, "keywords": words, "scan_dms": bool(scan_dms), "mentions": bool(mentions)}})
    # config lists are replaced, not merged: a channel taken out of the selection stops being read
    _done("slack")
    return slack_state()


# ---- code ------------------------------------------------------------------------------------------------------------
def _gh(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, env=env(), stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _gh_cli() -> dict:
    def read():
        rc, out, _ = _gh("--version", timeout=15)
        if rc != 0:
            return {"installed": False, "version": "", "login": ""}
        ver = (re.search(r"gh version (\S+)", out) or [None, ""])[1]
        rc, out, _ = _gh("api", "user", "-q", ".login", timeout=30)
        return {"installed": True, "version": ver, "login": out.strip() if rc == 0 else ""}
    return _cached("gh", 20, read)


def github_state() -> dict:
    cli = _gh_cli()
    repos = [{"name": n, "full_name": site.slug(n), "path": r.get("path") or "", "default": r.get("default") or ""}
             for n, r in site.repos().items()]
    return {"cli": {"installed": cli["installed"], "version": cli["version"]}, "connected": bool(cli["login"]),
            "login": cli["login"], "org": site.org(), "repos": repos}


def github_orgs() -> dict:
    cli = _gh_cli()
    if not cli["login"]:
        raise Bad(_t("Sign in to GitHub first: run `gh auth login` in a terminal.", "Iniciá sesión en GitHub primero: corré `gh auth login` en una terminal."))
    rc, out, err = _gh("api", "user/orgs", "--paginate", "-q", ".[] | {login, name: (.description // \"\")}", timeout=60)
    orgs = [json.loads(l) for l in out.splitlines() if l.strip()] if rc == 0 else []
    return {"login": cli["login"], "orgs": [{"login": cli["login"], "name": _t("Your own repositories", "Tus propios repositorios")}] + orgs}


SCAN_ROOTS = ("", "code", "Code", "src", "dev", "Developer", "projects", "Projects", "work", "repos", "git", "github", "Documents", "Sites")
SKIP_DIRS = {"node_modules", "Library", "Applications", "Pictures", "Music", "Movies", "Downloads", ".Trash", "venv", ".venv", "dist", "build"}


def local_clones() -> dict[str, str]:
    """owner/name (lowercase) -> path of a clone on this machine, found by a bounded look in the usual folders (three
    levels, a few seconds). The user can always type a path; this only saves them from doing it."""
    def read():
        found: dict[str, str] = {}
        deadline = time.time() + 6
        home = Path.home()
        roots = [home / r for r in SCAN_ROOTS if (home / r).is_dir()]
        seen = set()

        def look(d: Path, depth: int) -> None:
            if time.time() > deadline or d in seen:
                return
            seen.add(d)
            cfg = d / ".git" / "config"
            if cfg.is_file():
                m = re.search(r"url\s*=\s*\S*github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?\s*$", cfg.read_text(errors="ignore"), re.M)
                if m:
                    found.setdefault(f"{m.group(1)}/{m.group(2)}".lower(), str(d))
                return  # a repo's subfolders are its own
            if depth <= 0:
                return
            try:
                kids = [k for k in d.iterdir() if k.is_dir() and not k.name.startswith(".") and k.name not in SKIP_DIRS]
            except OSError:
                return
            for k in sorted(kids)[:200]:
                look(k, depth - 1)

        for r in roots:
            look(r, 2 if r == home else 3)  # ~/<folder>/<repo> is the commonest layout; deeper only under a code folder
        return found
    return _cached("clones", 120, read)


def github_repos(org: str, q: str = "") -> list[dict]:
    org = (org or "").strip()
    if not re.fullmatch(r"[\w.-]+", org):
        raise Bad("pick an organization")
    def read():
        rc, out, err = _gh("repo", "list", org, "--limit", "300", "--no-archived", "--json",
                           "name,nameWithOwner,defaultBranchRef,isPrivate,pushedAt,primaryLanguage", timeout=90)
        if rc != 0:
            raise Bad(_t(f"GitHub did not list {org}'s repositories: {err.strip()[:200]}", f"GitHub no listó los repos de {org}: {err.strip()[:200]}"))
        return json.loads(out or "[]")
    clones = local_clones()
    rows = [{"name": r["name"], "full_name": r["nameWithOwner"], "default": (r.get("defaultBranchRef") or {}).get("name") or "main",
             "private": bool(r.get("isPrivate")), "pushed_at": r.get("pushedAt") or "", "path": clones.get(r["nameWithOwner"].lower(), ""),
             "language": (r.get("primaryLanguage") or {}).get("name") or ""} for r in _cached(f"repos:{org}", 120, read)]
    q = (q or "").strip().lower()
    rows = [r for r in rows if q in r["name"].lower()]
    rows.sort(key=lambda r: (not r["path"], r["pushed_at"]), reverse=False)
    rows.sort(key=lambda r: r["pushed_at"], reverse=True)
    rows.sort(key=lambda r: not r["path"])  # the ones already on this machine first, each group newest first
    return rows[:100]


def github_save(org: str, repos: list) -> dict:
    cli = _gh_cli()
    if not cli["login"]:
        raise Bad(_t("Sign in to GitHub first: run `gh auth login` in a terminal.", "Iniciá sesión en GitHub primero: corré `gh auth login` en una terminal."))
    if not re.fullmatch(r"[\w.-]+", org or ""):
        raise Bad("pick an organization")
    if not repos:
        raise Bad(_t("Pick at least one repository.", "Elegí al menos un repositorio."))
    out = {}
    for r in repos:
        name = str(r.get("name") or "")
        full = str(r.get("full_name") or f"{org}/{name}")
        if not re.fullmatch(r"[\w.-]+", name) or not re.fullmatch(r"[\w.-]+/[\w.-]+", full):
            raise Bad(f"bad repository name {name!r}")
        path = os.path.expanduser(str(r.get("path") or "").strip())
        if path:
            if not (Path(path) / ".git").exists():
                raise Bad(_t(f"{path} is not a git checkout of {name}.", f"{path} no es un checkout de git de {name}."))
        else:
            path = str(DATA / "repos" / name)  # cloned on finish
        owner, gh_name = full.split("/", 1)
        prev = site.repos().get(name) or {}
        out[name] = {**{k: v for k, v in prev.items() if k not in ("path", "default", "org", "github")},
                     "path": path, "default": str(r.get("default") or "main"), "layer": prev.get("layer") or "backend",
                     **({"org": owner} if owner != org else {}), **({"github": gh_name} if gh_name != name else {})}
    save_config({"repos": out, "github_org": org, "self_github": cli["login"], "primary_repo": next(iter(out))}, replace=("repos",))
    if site.setup_complete():
        write_policy()  # the guard writes only to the repos the install works in
    _done("code")
    return github_state()


def clone_missing() -> list[str]:
    """Clone the chosen repos that are not on this machine yet into the install's home. Returns what failed."""
    failed = []
    for name, r in site.repos().items():
        path = Path(os.path.expanduser(r.get("path") or ""))
        if (path / ".git").exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = _gh("repo", "clone", site.slug(name), str(path), timeout=1800)
        if rc != 0:
            failed.append(f"{name}: {err.strip()[-200:]}")
            log("setup: clone failed", repo=name, err=err.strip()[-200:])
    return failed


def repo_checks(name: str) -> dict:
    """What the gate would run for a configured repo, read from its checkout, for the settings screen: its own `checks`
    list when the config has one, else what its files show. A check whose `dir` is set runs in that folder; one marked
    `changed_files` runs on the diff's files only; detected workspace folders run only when a diff touches them."""
    from . import gates
    r = site.repos().get(name)
    if r is None:
        raise Bad(_t(f"{name} is not one of this install's repositories.", f"{name} no es uno de los repositorios de esta instalación."))
    path = Path(site.repo_path(name))
    source = "config" if isinstance(r.get("checks"), list) else "detected"
    if not (path / ".git").exists():
        return {"repo": name, "source": source, "checks": [],
                "note": _t("The repository is not on this machine yet.", "El repositorio todavía no está en esta máquina.")}
    try:
        found = gates.plan_checks(path, name, None)
    except Exception as e:
        return {"repo": name, "source": source, "checks": [], "note": f"{type(e).__name__}: {str(e)[:200]}"}
    checks = [{"name": c["name"], "run": c["run"].replace(" {files}", ""), "dir": c.get("dir") or "",
               "setup": c.get("setup") or "", "kind": c.get("kind") or "", "changed_files": "{files}" in c["run"],
               "paths": c.get("paths") or []} for c in found]
    note = "" if checks else (_t("This repository's config lists no checks.", "La configuración de este repositorio no lista checks.")
                              if source == "config" else
                              _t("Nothing found to run: no test script, Python project, go.mod, Cargo.toml or Makefile test target.",
                                 "No encontré nada para correr: ni script de tests, ni proyecto Python, go.mod, Cargo.toml o target test en el Makefile."))
    return {"repo": name, "source": source, "checks": checks, "note": note}


# ---- team ------------------------------------------------------------------------------------------------------------
def team_state() -> dict:
    members = [{"slack_id": k, "name": v.get("name") or "", "github": v.get("github") or ""}
               for k, v in (CFG.get("team_members") or {}).items() if isinstance(v, dict)]
    return {"members": members, "area": (CFG.get("domain_fence") or "").strip() if not site.internal() else "",
            "keywords": list((CFG.get("fence") or {}).get("keywords") or [])}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _recent_authors(ch: str) -> list[str]:
    try:
        msgs = _slack().conversations_history(channel=ch, limit=200).data.get("messages", [])
    except Exception as e:
        log("setup: team suggest failed", err=str(e)[:160])
        return []
    n: dict[str, int] = {}
    for m in msgs:
        if m.get("user") and not m.get("bot_id") and not m.get("subtype"):
            n[m["user"]] = n.get(m["user"], 0) + 1
    return sorted(n, key=lambda u: -n[u])


def team_suggest() -> dict:
    """The people in the team channel, each with the GitHub login whose name matches theirs in the org, when one does."""
    ch = CFG.get("team_channel")
    if not ch:
        return {"members": []}
    try:
        ids = _slack().conversations_members(channel=ch, limit=200).data.get("members", [])
    except Exception as e:
        # a token without the scope that lists a channel's members (channels:read, groups:read) can still read the
        # channel: the people who write there are the team, most active first
        log("setup: team channel members unreadable, suggesting who writes there", err=str(e)[:120])
        ids = _recent_authors(ch)
    users = {u["id"]: u for u in _all_users()}
    logins: dict[str, str] = {}
    if site.org() and site.org() != (CFG.get("self_github") or ""):
        rc, out, _ = _gh("api", "graphql", "-f", "query={organization(login:\"%s\"){membersWithRole(first:100){nodes{login name}}}}" % site.org(), timeout=60)
        if rc == 0:
            try:
                for n in json.loads(out)["data"]["organization"]["membersWithRole"]["nodes"]:
                    logins[_norm(n.get("name") or "")] = n["login"]
            except (KeyError, TypeError, ValueError):
                pass
    me = CFG.get("self_user_id")
    out = [{"slack_id": i, "name": users[i]["real_name"] or users[i]["name"], "github": logins.get(_norm(users[i]["real_name"]), "")}
           for i in ids if i in users and i != me]
    return {"members": sorted(out, key=lambda m: m["name"].lower())[:40]}


def team_save(members: list, area: str, keywords: list) -> dict:
    tm = {}
    for m in members or []:
        sid = str(m.get("slack_id") or "")
        if not re.fullmatch(r"[UW][A-Z0-9]{6,}", sid):
            raise Bad("each teammate needs their Slack id")
        tm[sid] = {"name": str(m.get("name") or "")[:80], "github": re.sub(r"^@", "", str(m.get("github") or "").strip())[:60]}
    words = list(dict.fromkeys(re.sub(r"\s+", " ", str(k)).strip() for k in keywords or [] if str(k).strip()))[:30]
    save_config({"team_members": tm, "github_team_handles": sorted({m["github"] for m in tm.values() if m["github"]}),
                 "domain_fence": re.sub(r"\s+\n", "\n", str(area or "")).strip()[:2000], "fence": {"keywords": words}},
                replace=("team_members",))
    _done("team")
    return team_state()


# ---- tickets (Jira) --------------------------------------------------------------------------------------------------
def jira_call(path: str, method: str = "GET", body: dict | None = None, base: str | None = None, email: str | None = None,
              token: str | None = None) -> dict:
    """One Jira Cloud REST call with the user's API token. Read-only here: the setup lists and previews, it changes nothing."""
    base = (base or site.jira_base()).rstrip("/")
    email = email or (CFG.get("jira") or {}).get("email") or ""
    token = token or vault.get("jira-token")
    if not (base and email and token):
        raise Bad(_t("Connect Jira first.", "Conectá Jira primero."))
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode(),
                                          "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        why = {401: _t("the email or the API token is wrong", "el email o el API token están mal"),
               403: _t("that account may not do this", "esa cuenta no tiene permiso para esto"),
               404: _t("that is not a Jira Cloud site", "eso no es un sitio de Jira Cloud")}.get(e.code, f"HTTP {e.code}")
        raise Bad(_t(f"Jira answered: {why}.", f"Jira respondió: {why}.")) from None
    except (urllib.error.URLError, OSError) as e:
        raise Bad(_t(f"Could not reach {base}: {getattr(e, 'reason', e)}.", f"No se pudo llegar a {base}: {getattr(e, 'reason', e)}.")) from None


def jira_connect(base: str, email: str, token: str) -> dict:
    base = (base or "").strip().rstrip("/")
    if base and not base.startswith("http"):
        base = "https://" + base
    if not re.fullmatch(r"https://[\w.-]+(\.atlassian\.net|\.jira\.com|[\w.-]+)", base):
        raise Bad(_t("The Jira site looks like https://yourcompany.atlassian.net.", "El sitio de Jira es algo como https://tuempresa.atlassian.net."))
    email, token = (email or "").strip(), (token or "").strip()
    if not email or not token:
        raise Bad(_t("Jira needs your email and an API token.", "Jira necesita tu email y un API token."))
    me = jira_call("/rest/api/3/myself", base=base, email=email, token=token)
    vault.put("jira-token", token)
    save_config({"jira_base": base, "jira": {"email": email, "account_id": me.get("accountId") or "", "account": me.get("displayName") or email}})
    _cache.pop("jira-projects", None)
    return {"jira": jira_state(), "projects": jira_projects()}


def jira_projects() -> list[dict]:
    def read():
        out, start = [], 0
        for _ in range(10):
            d = jira_call(f"/rest/api/3/project/search?maxResults=100&startAt={start}&orderBy=name")
            out += [{"key": p["key"], "name": p.get("name") or p["key"]} for p in d.get("values", [])]
            if d.get("isLast", True):
                break
            start += 100
        return out
    return _cached("jira-projects", 300, read)


def jira_state() -> dict:
    j = CFG.get("jira") or {}
    connected = bool(site.jira_base() and j.get("email") and vault.get("jira-token"))
    return {"enabled": bool(j.get("enabled")), "connected": connected, "base": site.jira_base(), "email": j.get("email") or "",
            "account": j.get("account") or "", "projects": list(j.get("projects") or []), "assigned": bool(j.get("assigned", True)),
            "mentions": bool(j.get("mentions", False)), "jql": j.get("jql") or ""}


def jira_jql(projects: list, assigned: bool, mentions: bool, jql: str = "") -> str:
    """What the install consumes from Jira, as one query: open tickets that are the user's (assigned, or where they are
    mentioned or watching), in the chosen projects, plus whatever extra JQL they wrote."""
    mine = [c for c, on in (("assignee = currentUser()", assigned), ("(watcher = currentUser() OR text ~ currentUser())", mentions)) if on]
    parts = ["statusCategory != Done"]
    keys = [k for k in projects or [] if re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", str(k))]
    if keys:
        parts.append("project in (" + ", ".join(keys) + ")")
    if mine:
        parts.append("(" + " OR ".join(mine) + ")")
    q = " AND ".join(parts)
    extra = (jql or "").strip()
    return f"({q}) OR ({extra})" if extra and not mine and not keys else (f"{q} AND ({extra})" if extra else q)


def jira_search(jql: str, limit: int = 50) -> list[dict]:
    d = jira_call("/rest/api/3/search/jql", "POST", {"jql": jql, "maxResults": limit,
                                                     "fields": ["summary", "status", "priority", "description", "updated", "assignee"]})
    return d.get("issues") or []


def jira_preview(projects: list, assigned: bool, mentions: bool, jql: str = "") -> dict:
    issues = jira_search(jira_jql(projects, assigned, mentions, jql) + " ORDER BY updated DESC", limit=50)
    return {"count": len(issues), "sample": [{"key": i["key"], "summary": (i["fields"].get("summary") or "")[:120],
                                               "status": ((i["fields"].get("status") or {}).get("name") or "")} for i in issues[:3]]}


def jira_save(enabled: bool, projects: list, assigned: bool, mentions: bool, jql: str = "") -> dict:
    keys = [str(k) for k in projects or [] if re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", str(k))]
    if enabled:
        if not jira_state()["connected"]:
            raise Bad(_t("Connect Jira first.", "Conectá Jira primero."))
        jira_preview(keys, assigned, mentions, jql)  # a JQL that Jira refuses is refused here, not at 3 a.m. in the poll
    save_config({"jira": {"enabled": bool(enabled), "projects": keys, "assigned": bool(assigned), "mentions": bool(mentions),
                          "jql": (jql or "").strip()[:1000]},
                 "tickets": {"prefixes": keys, "close_prefixes": []}})
    _done("tickets")
    return jira_state()


# ---- how much it may do alone ------------------------------------------------------------------------------------------
AUTONOMY = {
    # reads and drafts everything, sends nothing
    "watch": {"mode": "shadow", "capabilities": {"auto_merge": False}, "reviews": {"post_comment_only": False, "approve_clean": False},
              "prs_ready_directly": False},
    # opens draft PRs from its own branches; every message, review and merge waits for the user
    "ask": {"mode": "act", "capabilities": {"auto_merge": False}, "reviews": {"post_comment_only": False, "approve_clean": False},
            "prs_ready_directly": False},
    # also comments on teammates' PRs, opens PRs ready for review and merges its own approved, green ones
    "auto": {"mode": "act", "capabilities": {"auto_merge": True}, "reviews": {"post_comment_only": True, "approve_clean": False},
             "prs_ready_directly": True},
}


def autonomy_save(level: str) -> None:
    if level not in AUTONOMY:
        raise Bad("level must be watch, ask or auto")
    save_config({"autonomy": level, **AUTONOMY[level]})
    _done("autonomy")
    write_policy()


SELF_ACTIONS = {"slack_post_self": "self", "slack_edit_self": "self", "slack_delete_self": "self", "slack_pin_self": "self"}


def policy() -> dict:
    """What the guard will let out, from what the user chose. The worker's own config says what it will try; this is
    what stops it when it is wrong. At every level Clea may write in the user's own DM with themselves. 'watch' allows
    nothing else. 'ask' adds draft PRs and work on the user's own PRs, and puts every message to someone else, every
    review and every merge behind the user's approval. 'auto' also lets plain review comments on teammates' PRs go."""
    level = CFG.get("autonomy") or "ask"
    actions = dict(SELF_ACTIONS)
    if level in ("ask", "auto"):
        actions.update({"slack_post": "ask", "gh_pr_create": "auto", "gh_pr_op": "own_pr", "gh_reply_own_pr": "own_pr",
                        "gh_review": "ask", "gh_comment_other_pr": "ask", "gh_merge": "merge_rule", "gh_pending_review": "auto"})
    if level == "auto":
        actions["gh_review_comment"] = "comment_only"
    return {"self_user_id": CFG.get("self_user_id") or "", "self_dm_channel": CFG.get("self_dm_channel") or "",
            "self_github": CFG.get("self_github") or "", "language": site.lang(),
            "approve_reactions": list((CFG.get("greenlight") or {}).get("approve_reactions") or ["white_check_mark"]),
            "repos": {n: site.slug(n) for n in site.repos()}, "actions": actions}


def write_policy() -> None:
    if site.internal():
        return  # the first install's guard and policy are its own, outside this tree
    d = DATA / "guard"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "policy.json.tmp"
    tmp.write_text(json.dumps(policy(), indent=2, ensure_ascii=False) + "\n")
    tmp.replace(d / "policy.json")


# ---- finish ------------------------------------------------------------------------------------------------------------
def finish() -> dict:
    need = missing()
    if need:
        return {"ok": False, "missing": need}
    if "autonomy" not in ((CFG.get("setup") or {}).get("done") or []):
        autonomy_save("ask")
    write_policy()
    save_config({"setup": {"complete": True, "completed_at": int(time.time())}})
    log("setup: complete", provider=providers.selected_id(), repos=sorted(site.repos()), autonomy=CFG.get("autonomy"))
    return {"ok": True, "missing": []}


# ---- the console's routes ----------------------------------------------------------------------------------------------
def handle(method: str, path: str, query: dict, body: dict) -> dict | list:
    """/api/setup/* for the console. Raises Bad (a 400 the user reads) for anything they can fix."""
    sub = path[len("/api/setup"):].strip("/")
    q = lambda k: (query.get(k) or [""])[0]
    if method == "GET":
        if sub == "":
            return state()
        if sub == "provider/login":
            return providers.login_state()
        if sub == "slack/channels":
            return slack_channels(q("q"))
        if sub == "slack/usergroups":
            return slack_usergroups()
        if sub == "slack/users":
            return slack_users(q("q"))
        if sub == "github/orgs":
            return github_orgs()
        if sub == "github/repos":
            return github_repos(q("org"), q("q"))
        if sub == "team/suggest":
            return team_suggest()
        raise KeyError(path)
    try:
        if sub == "profile":
            save_profile(body.get("name", ""), body.get("language") or site.lang())
            return {"ok": True}
        if sub == "provider":
            return {"ok": True, "provider": providers.select(str(body.get("id") or ""), body.get("api_key"))}
        if sub == "provider/login":
            return providers.login_start(str(body.get("id") or ""))
        if sub == "provider/login/cancel":
            providers.login_cancel()
            return {"ok": True}
        if sub == "provider/logout":
            providers.forget(str(body.get("id") or ""))
            return {"ok": True}
        if sub == "slack/connect":
            return {"ok": True, "slack": slack_connect(body.get("user_token", ""), body.get("app_token", ""))}
        if sub == "slack":
            return {"ok": True, "slack": slack_save(body.get("channels"), body.get("usergroups"), body.get("keywords"),
                                                    body.get("scan_dms", True), body.get("mentions", True))}
        if sub == "github":
            return {"ok": True, "github": github_save(str(body.get("org") or ""), body.get("repos") or [])}
        if sub == "team":
            return {"ok": True, "team": team_save(body.get("members"), body.get("area", ""), body.get("keywords"))}
        if sub == "jira/connect":
            return {"ok": True, **jira_connect(body.get("base", ""), body.get("email", ""), body.get("token", ""))}
        if sub == "jira/preview":
            return jira_preview(body.get("projects"), body.get("assigned", True), body.get("mentions", False), body.get("jql", ""))
        if sub == "jira":
            return {"ok": True, "jira": jira_save(body.get("enabled", True), body.get("projects"), body.get("assigned", True),
                                                  body.get("mentions", False), body.get("jql", ""))}
        if sub == "autonomy":
            autonomy_save(str(body.get("level") or ""))
            return {"ok": True}
        if sub == "finish":
            out = finish()
            if not out["ok"]:
                raise Bad(_t("Some steps are not finished yet.", "Faltan pasos por terminar."), out["missing"])
            return out
        if sub == "reopen":
            return {"ok": True}
    except Bad:
        raise
    except ValueError as e:  # a provider's or a validator's own refusal
        raise Bad(str(e)) from None
    raise KeyError(path)
