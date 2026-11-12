"""Who thinks for this install, and on whose account.

An install picks one provider. Each is a pair: the engine that runs the agent loop on this machine (Claude Code or
Codex, both the vendor's own unmodified CLI) and how that engine is paid for:

  claude-subscription  the user's own signed-in Claude Code. Their Claude plan pays. The sign-in is Claude Code's
                       own (`claude auth login`, on Anthropic's page); Clea starts it in a terminal and only ever
                       reads `claude auth status`. Anthropic does not let another app offer a claude.ai login or
                       hold its credentials, so there is no "sign in with Claude" here and there must not be one.
  claude-api           an Anthropic API key (Console billing; some plans include API credits), handed to the engine
                       as ANTHROPIC_API_KEY for that one process.
  openai-chatgpt       Sign in with ChatGPT (ap/siwc.py): the user's ChatGPT plan pays, through OpenAI's own OAuth.
  codex-login          the user's own signed-in Codex CLI (`codex login`).
  openai-api           an OpenAI API key, handed to Codex as CODEX_API_KEY for that one process.

Keys live in the vault (Keychain). Every engine process gets exactly the credential of the selected provider and has
the others removed from its environment, so a key lying in the user's shell profile never pays for a run by surprise.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

from . import site, vault
from .core import CFG, PATH_PREFIX, VAR, Probes, env, log, save_config

PROVIDERS = [
    {"id": "claude-subscription", "vendor": "Anthropic", "engine": "claude", "auth": "subscription"},
    {"id": "claude-api", "vendor": "Anthropic", "engine": "claude", "auth": "api_key"},
    {"id": "openai-chatgpt", "vendor": "OpenAI", "engine": "codex", "auth": "siwc"},
    {"id": "codex-login", "vendor": "OpenAI", "engine": "codex", "auth": "subscription"},
    {"id": "openai-api", "vendor": "OpenAI", "engine": "codex", "auth": "api_key"},
]
INSTALL = {"claude": "https://code.claude.com/docs/en/setup", "codex": "https://developers.openai.com/codex/cli"}
KEY = {"claude": "anthropic-api-key", "codex": "openai-api-key"}
# credentials an engine would pick up from the environment by itself; each run keeps only its provider's
CLAUDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
EMPTY_MCP = '{"mcpServers":{}}'
_cache = Probes()
_login: dict = {}


class NotReady(Exception):
    """The selected provider cannot run a call right now (signed out, key missing): the stage waits, the user is told."""


def by_id(pid: str) -> dict:
    p = next((p for p in PROVIDERS if p["id"] == pid), None)
    if not p:
        raise ValueError(f"unknown provider {pid}")
    return p


def selected() -> dict:
    """{'engine', 'auth'} of the provider this install uses. The first install predates the choice: it runs Claude
    Code on whatever login the machine has, as it always did."""
    p = CFG.get("provider") or {}
    if p.get("engine"):
        return {"engine": p["engine"], "auth": p.get("auth") or "subscription"}
    return {"engine": "claude", "auth": "subscription"} if site.internal() else {"engine": "", "auth": ""}


def engine() -> str:
    return selected()["engine"]


def selected_id() -> str:
    s = selected()
    return next((p["id"] for p in PROVIDERS if p["engine"] == s["engine"] and p["auth"] == s["auth"]), "")


# ---- what each engine's own CLI says ------------------------------------------------------------------------------
def _cached(key: str, ttl: float, fn):
    return _cache.get(key, ttl, fn)


def refresh() -> None:
    _cache.clear()


def _which(name: str) -> str:
    return shutil.which(name, path=PATH_PREFIX + ":" + os.environ.get("PATH", "")) or ""


def _out(cmd: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env(), stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def claude_cli() -> dict:
    """Claude Code as installed here: version and who is signed in (`claude auth status`, zero tokens). Nothing of
    the credential itself is read."""
    def read():
        if not _which("claude"):
            return {"installed": False, "version": "", "logged_in": False}
        _, ver = _out(["claude", "--version"])
        rc, raw = _out(["claude", "auth", "status"])
        try:
            st = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        except ValueError:
            st = {}
        exp = st.get("loginExpiresAt")
        try:
            exp = int(time.mktime(time.strptime(exp[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone) if exp else None
        except ValueError:
            exp = None
        return {"installed": True, "version": ver.strip().split(" ")[0], "logged_in": bool(st.get("loggedIn")) and rc == 0,
                "method": st.get("authMethod") or "", "email": st.get("email") or "", "plan": st.get("subscriptionType") or "",
                "org": st.get("orgName") or "", "expires_at": exp}
    return _cached("claude", 15, read)


def codex_cli() -> dict:
    def read():
        try:
            from . import codex
            return codex.status()
        except Exception as e:  # the engine module is missing or broke: the provider shows as unavailable, never a crash
            return {"installed": bool(_which("codex")), "version": "", "logged_in": False, "method": None, "detail": str(e)[:120]}
    return _cached("codex", 15, read)


def _key_meta(eng: str) -> dict:
    return (CFG.get("provider_keys") or {}).get(eng) or {}


# ---- state for the setup screen -----------------------------------------------------------------------------------
def _t(en: str, es: str) -> str:
    return es if site.lang() == "es" else en


def state(pid: str) -> dict:
    p = by_id(pid)
    eng = p["engine"]
    cli = claude_cli() if eng == "claude" else codex_cli()
    out = {**p, "cli": {"name": eng, "installed": bool(cli.get("installed")), "version": cli.get("version") or "",
                        "install_url": INSTALL[eng]},
           "ready": False, "selected": pid == selected_id(), "account": "", "plan": "", "needs": None, "detail": "",
           "expires_at": None}
    if not cli.get("installed"):
        name = "Claude Code" if eng == "claude" else "Codex"
        return {**out, "needs": "install", "detail": _t(f"{name} is not installed on this Mac.", f"{name} no está instalado en esta Mac.")}
    if pid == "claude-subscription":
        if cli.get("logged_in") and cli.get("method") in ("claude.ai", "oauth_token"):
            return {**out, "ready": True, "account": cli.get("email") or "", "plan": cli.get("plan") or "", "expires_at": cli.get("expires_at"),
                    "detail": _t("Signed in to Claude Code. Usage draws from this Claude plan.",
                                 "Sesión iniciada en Claude Code. El uso sale de este plan de Claude.")}
        if cli.get("logged_in"):  # Claude Code itself runs on a key or a cloud provider: it works, and it bills that
            return {**out, "ready": True, "account": cli.get("email") or "", "plan": "API",
                    "detail": _t("Claude Code on this Mac is set up with an API credential, not a Claude plan. Usage is billed there.",
                                 "Claude Code en esta Mac usa una credencial de API, no un plan de Claude. El uso se cobra ahí.")}
        return {**out, "needs": "login", "detail": _t("Sign in to Claude Code with your Claude account. It happens in Claude Code, on "
                                                      "Anthropic's page. Clea never sees your password or token.",
                                                      "Iniciá sesión en Claude Code con tu cuenta de Claude. Pasa en Claude Code, en la "
                                                      "página de Anthropic. Clea nunca ve tu contraseña ni tu token.")}
    if pid == "codex-login":
        if cli.get("logged_in") and cli.get("method") == "chatgpt":
            return {**out, "ready": True, "plan": "ChatGPT", "detail": _t("Signed in to Codex with ChatGPT. Usage draws from this ChatGPT plan.",
                                                                           "Sesión iniciada en Codex con ChatGPT. El uso sale de este plan de ChatGPT.")}
        if cli.get("logged_in"):
            return {**out, "ready": True, "plan": "API", "detail": _t("Codex on this Mac is signed in with an API key. Usage is billed there.",
                                                                       "Codex en esta Mac usa una API key. El uso se cobra ahí.")}
        return {**out, "needs": "login", "detail": _t("Sign in to Codex with your ChatGPT account. It happens in Codex itself.",
                                                      "Iniciá sesión en Codex con tu cuenta de ChatGPT. Pasa en Codex mismo.")}
    if pid == "openai-chatgpt":
        from . import siwc
        st = _cached("siwc", 15, siwc.status)
        if st["connected"]:
            return {**out, "ready": True, "account": st["email"], "plan": "ChatGPT",
                    "detail": _t("Using your ChatGPT plan.", "Usando tu plan de ChatGPT.")}
        if st["signed_in"]:
            return {**out, "needs": "login", "account": st["email"],
                    "detail": _t("Signed in, but ChatGPT plan usage is not allowed for Clea yet. Continue with ChatGPT and allow it.",
                                 "Sesión iniciada, pero falta permitir que Clea use tu plan de ChatGPT. Continuá con ChatGPT y permitilo.")}
        return {**out, "needs": "login", "account": st["email"],
                "detail": _t("Use your ChatGPT plan in Clea. You sign in on OpenAI's page and choose how much of your plan Clea may use.",
                             "Usá tu plan de ChatGPT en Clea. Iniciás sesión en la página de OpenAI y elegís cuánto de tu plan puede usar Clea.")}
    meta = _key_meta(eng)  # claude-api, openai-api
    if _cached(f"key:{eng}", 15, lambda: bool(vault.get(KEY[eng]))) and meta.get("validated_at"):
        return {**out, "ready": True, "account": meta.get("hint") or "", "plan": "API",
                "detail": _t("API key saved and checked. Usage is billed to that key.", "API key guardada y verificada. El uso se cobra a esa key.")}
    who = "Anthropic" if eng == "claude" else "OpenAI"
    return {**out, "needs": "key", "detail": _t(f"Paste an {who} API key. It is stored in your Mac's Keychain and billed by {who}.",
                                                 f"Pegá una API key de {who}. Se guarda en el Keychain de tu Mac y la cobra {who}.")}


def states() -> list[dict]:
    return [state(p["id"]) for p in PROVIDERS]


def ready() -> bool:
    pid = selected_id()
    return bool(pid) and state(pid)["ready"]


# ---- choosing -----------------------------------------------------------------------------------------------------
def select(pid: str, api_key: str | None = None) -> dict:
    """Make this the install's provider. A key is checked with one tiny real call through the engine that will use it
    (what proves it works is a run, not a format check) before it is stored."""
    p = by_id(pid)
    eng = p["engine"]
    if p["auth"] == "api_key":
        key = (api_key or "").strip()
        if key:
            check_key(eng, key)
            vault.put(KEY[eng], key)
            save_config({"provider_keys": {eng: {"hint": vault.hint(key), "validated_at": int(time.time())}}})
        elif not (vault.get(KEY[eng]) and _key_meta(eng).get("validated_at")):
            raise ValueError(_t("Paste the API key first.", "Pegá la API key primero."))
    refresh()
    st = state(pid)
    if not st["ready"]:
        raise ValueError(st["detail"])
    save_config({"provider": {"engine": eng, "auth": p["auth"]}})
    refresh()
    log("provider selected", id=pid)
    from . import claude
    if claude.stopped():
        # the user answered the stop by choosing how Clea reaches a model (another key, a plan, another vendor), and
        # that choice was just checked: a key by a real call, a login by its CLI
        claude.resume(f"provider chosen: {pid}")
    return state(pid)


def check_key(eng: str, key: str) -> None:
    """Raises ValueError with the vendor's own reason when the key does not work."""
    if eng == "claude":
        if not claude_cli().get("installed"):
            raise ValueError(_t("Claude Code is not installed on this Mac.", "Claude Code no está instalado en esta Mac."))
        p = subprocess.run(["claude", "-p", "Reply with exactly: ok", "--model", CFG["claude"]["haiku5"], "--output-format", "json",
                            "--tools", "", "--strict-mcp-config", "--mcp-config", EMPTY_MCP, "--setting-sources", "",
                            "--no-session-persistence", "--max-budget-usd", "0.05"],
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, cwd=str(VAR),
                           env=env({**{k: None for k in CLAUDE_ENV}, "ANTHROPIC_API_KEY": key}))
        try:
            out = json.loads(p.stdout)
        except ValueError:
            raise ValueError((p.stderr or p.stdout).strip()[-300:] or "Claude Code did not answer") from None
        if out.get("is_error"):
            raise ValueError(str(out.get("result") or "the key was refused").split("·")[0].strip())
        return
    from . import codex
    out = codex.invoke("Reply with exactly: ok", model=CFG["openai"]["haiku"], cwd=str(VAR), auth="api_key", token=key, timeout=120)
    if out.get("is_error"):
        raise ValueError(str(out.get("result") or "the key was refused")[:300])


def forget(pid: str) -> None:
    """Remove what Clea itself holds for a provider: a stored key, or the ChatGPT session. The user's own Claude Code
    and Codex logins are theirs; Clea never signs them out."""
    p = by_id(pid)
    if p["auth"] == "api_key":
        vault.delete(KEY[p["engine"]])
        save_config({"provider_keys": {p["engine"]: {"hint": "", "validated_at": 0}}})
    elif pid == "openai-chatgpt":
        from . import siwc
        siwc.sign_out()
    if selected_id() == pid:
        save_config({"provider": {"engine": "", "auth": ""}})
    refresh()


# ---- the vendor's own sign-in -------------------------------------------------------------------------------------
LOGIN_CMD = {"claude-subscription": ("claude", "claude auth login", "Claude Code", "Anthropic"),
             "codex-login": ("codex", "codex login", "Codex", "OpenAI")}


def login_start(pid: str) -> dict:
    by_id(pid)
    if pid == "openai-chatgpt":
        from . import siwc
        siwc.start()
        _login.update(id=pid, at=time.time())
        return login_state()
    if pid not in LOGIN_CMD:
        raise ValueError("this provider has no sign-in")
    cli, cmd, name, vendor = LOGIN_CMD[pid]
    if not _which(cli):
        raise ValueError(_t(f"{name} is not installed on this Mac.", f"{name} no está instalado en esta Mac."))
    # The vendor's CLI signs the user in, in a terminal window they can see: Clea neither drives the flow nor reads
    # what it stores. A.command file opens in Terminal by itself (no automation permission to ask for).
    script = VAR / f"sign-in-{cli}.command"
    script.write_text("#!/bin/zsh\nclear\n"
                      f"echo 'Clea: signing you in to {name}. This is {vendor}'\"'\"'s own sign-in. Clea never sees your password or token.'\n"
                      f"echo\nexport PATH=\"{PATH_PREFIX}:$PATH\"\n{cmd}\necho\n"
                      "echo 'You can close this window and go back to Clea.'\n")
    script.chmod(0o700)
    subprocess.run(["open", str(script)], capture_output=True, timeout=20)
    _login.clear()
    _login.update(id=pid, at=time.time(), state="waiting", error=None)
    threading.Thread(target=_await_cli_login, args=(pid, _login["at"]), name="provider-login", daemon=True).start()
    log("provider sign-in opened in a terminal", id=pid)
    return login_state()


def _await_cli_login(pid: str, started: float) -> None:
    while _login.get("id") == pid and _login.get("at") == started and time.time() - started < 900:
        time.sleep(2)
        refresh()
        if state(pid)["ready"]:
            _login.update(state="done")
            return
    if _login.get("id") == pid and _login.get("at") == started:
        _login.update(state="error", error=_t("The sign-in was not finished.", "El inicio de sesión no se completó."))


def login_state() -> dict:
    if not _login.get("id"):
        return {"id": None, "state": "idle", "url": None, "error": None}
    if _login["id"] == "openai-chatgpt":
        from . import siwc
        st = siwc.state()
        if st["state"] == "done":
            refresh()
        return {"id": _login["id"], **st}
    return {"id": _login["id"], "state": _login.get("state"), "url": None, "error": _login.get("error")}


def login_cancel() -> None:
    if _login.get("id") == "openai-chatgpt":
        from . import siwc
        siwc.cancel()
    _login.clear()


# ---- what an engine process runs with -----------------------------------------------------------------------------
def claude_env() -> dict:
    """Environment changes for a `claude` process: only the selected provider's credential reaches it."""
    s = selected()
    if site.internal() and not (CFG.get("provider") or {}).get("engine"):
        return {}  # the first install, as it always ran
    if s["engine"] != "claude":
        raise NotReady("this install thinks with another engine")
    if s["auth"] == "api_key":
        key = vault.get(KEY["claude"])
        if not key:
            raise NotReady("the Anthropic API key is missing: add it in Settings")
        return {**{k: None for k in CLAUDE_ENV}, "ANTHROPIC_API_KEY": key}
    return {k: None for k in CLAUDE_ENV}  # the plan pays: a key in the shell profile must not


def codex_auth() -> tuple[str, str | None]:
    """(auth, token) for ap.codex.invoke."""
    s = selected()
    if s["engine"] != "codex":
        raise NotReady("this install thinks with another engine")
    if s["auth"] == "siwc":
        from . import siwc
        try:
            return "siwc", siwc.access_token()
        except (siwc.SignInNeeded, siwc.PlanNotEnabled) as e:
            raise NotReady(f"ChatGPT: {e}. Continue with ChatGPT in Settings") from None
    if s["auth"] == "api_key":
        key = vault.get(KEY["codex"])
        if not key:
            raise NotReady("the OpenAI API key is missing: add it in Settings")
        return "api_key", key
    return "subscription", None


def model_id(alias: str) -> str:
    """The engine's model for a tier of the harness (fable, opus, sonnet, haiku, haiku5: the names the stages use)."""
    return (CFG["openai"] if engine() == "codex" else CFG["claude"])[alias]
