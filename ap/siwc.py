"""Sign in with ChatGPT: the user's own ChatGPT plan pays for Clea's model calls.

This is OpenAI's documented flow for open-source, locally run apps ("ChatGPT plan usage"): an authorization-code
grant with PKCE for a public client. The browser opens on OpenAI's page, the user signs in and consents there, and
OpenAI redirects to a listener on 127.0.0.1 with a code this install exchanges for tokens. Clea never sees a
password. What it keeps (in the vault, never in a config file or a log): the client id OpenAI issued to this install
for that account, the tokens, and the ID token it sends back as a hint so a later sign-in skips the account picker.

The first sign-in registers the client (client_id=dynamic_agent_client + this host's id); later ones reuse the issued
id. Access tokens last an hour and are refreshed with a rotating refresh token, one refresh at a time. The token
calls only OpenAI's Responses API on the user's plan; Clea hands it to the Codex engine (ap/codex.py, auth 'siwc').
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import secrets as _rand
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import vault
from .core import VAR, log

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
REVOKE = ISSUER + "/api/accounts/oauth/revoke"
JWKS = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
REGISTER = "dynamic_agent_client"  # the first-time registration entrypoint, never the id to save or exchange with
APP_NAME = "Clea"
USAGE_URL = "https://chatgpt.com/settings/usage"
RECORD = "chatgpt"
PORTS = (1455, 0)  # OpenAI's example port first; any free one otherwise (only the port of the callback may vary)
REFRESH_EARLY_S = 300
GONE = ("invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated",
        "refresh_token_reused")

_lock = threading.Lock()   # one refresh at a time: the refresh token rotates, and two racing refreshes burn it
_attempt: dict = {}        # the sign-in in flight: state, verifier, nonce, redirect, server, result


class SignInNeeded(Exception):
    """There is no usable ChatGPT session: the user has to sign in (again)."""


class PlanNotEnabled(Exception):
    """Signed in, but the user did not allow this app to use their ChatGPT plan."""


# ---- storage ------------------------------------------------------------------------------------------------------
def record() -> dict:
    try:
        return json.loads(vault.get(RECORD) or "{}")
    except ValueError:
        return {}


def _save(rec: dict) -> None:
    vault.put(RECORD, json.dumps(rec))


def host_id() -> str:
    """This install's stable, opaque host id (OpenAI ties plan usage to the host as well as to the client). Chosen once,
    before the first sign-in, and kept with the runtime it names."""
    f = VAR / "host-id"
    try:
        v = f.read_text().strip()
        if v.startswith("urn:uuid:"):
            return v
    except FileNotFoundError:
        pass
    v = f"urn:uuid:{uuid.uuid4()}"
    f.write_text(v + "\n")
    return v


def status() -> dict:
    """What a screen may show: never a token."""
    r = record()
    live = bool(r.get("refresh_token"))
    return {"connected": live and PLAN_SCOPE in (r.get("scopes") or []), "signed_in": live, "email": r.get("email") or "",
            "plan_enabled": PLAN_SCOPE in (r.get("scopes") or []), "expires_at": r.get("expires_at"),
            "registered": bool(r.get("client_id"))}


# ---- HTTP ---------------------------------------------------------------------------------------------------------
def _post_form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode() or "{}"
            return json.loads(body) if body.strip().startswith("{") else {}
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode() or "{}")
        except ValueError:
            err = {}
        inner = err.get("error")
        if isinstance(inner, dict):  # the API's error object; the OAuth endpoints answer {"error": "<code>",...}
            code, desc = inner.get("code") or inner.get("type"), inner.get("message")
        else:
            code, desc = inner, err.get("error_description")
        raise OAuthError(code or f"http_{e.code}", desc or "") from None


class OAuthError(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code


# ---- ID token -----------------------------------------------------------------------------------------------------
def _b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_jwks: dict = {"at": 0, "keys": []}
_SHA256_DER = bytes.fromhex("3031300d060960864801650304020105000420")  # DigestInfo prefix for SHA-256 (RFC 8017, 9.2)


def _keys(force: bool = False) -> list:
    if force or time.time() - _jwks["at"] > 3600 or not _jwks["keys"]:
        with urllib.request.urlopen(JWKS, timeout=20) as r:
            _jwks.update(at=time.time(), keys=json.load(r).get("keys") or [])
    return _jwks["keys"]


def _rs256_ok(signing_input: bytes, sig: bytes, n: int, e: int) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256, the only algorithm OpenAI's discovery document lists for ID tokens. A signature
    check is one public-key exponentiation and a compare; the standard library has no RSA, and a verifier needs none."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    digest = hashlib.sha256(signing_input).digest()
    pad = k - len(_SHA256_DER) - len(digest) - 3
    if pad < 8:
        return False
    return hmac.compare_digest(em, b"\x00\x01" + b"\xff" * pad + b"\x00" + _SHA256_DER + digest)


def verify_id_token(token: str, client_id: str, nonce: str | None, keys: list | None = None, now: float | None = None) -> dict:
    """The ID token's claims, once its signature, issuer, audience, expiry and nonce hold. Raises ValueError otherwise."""
    try:
        h64, p64, s64 = token.split(".")
        head, claims, sig = json.loads(_b64u(h64)), json.loads(_b64u(p64)), _b64u(s64)
    except Exception:
        raise ValueError("the ID token is not a JWT") from None
    if head.get("alg") != "RS256":
        raise ValueError(f"unexpected ID token algorithm {head.get('alg')}")
    for attempt in (0, 1):  # an unknown key id is fetched once more: OpenAI rotates its signing keys
        ks = keys if keys is not None else _keys(force=bool(attempt))
        jwk = next((k for k in ks if k.get("kid") == head.get("kid") and k.get("kty") == "RSA"), None)
        if jwk or keys is not None:
            break
    if not jwk:
        raise ValueError("the ID token was signed with a key OpenAI does not publish")
    n, e = int.from_bytes(_b64u(jwk["n"]), "big"), int.from_bytes(_b64u(jwk["e"]), "big")
    if not _rs256_ok(f"{h64}.{p64}".encode(), sig, n, e):
        raise ValueError("the ID token's signature does not verify")
    t = time.time() if now is None else now
    aud = claims.get("aud")
    if claims.get("iss") != ISSUER:
        raise ValueError("the ID token was not issued by OpenAI")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("the ID token was issued to another client")
    if float(claims.get("exp") or 0) < t - 60:
        raise ValueError("the ID token is expired")
    if nonce is not None and claims.get("nonce") != nonce:
        raise ValueError("the ID token does not carry this sign-in's nonce")
    if not claims.get("sub"):
        raise ValueError("the ID token names no account")
    return claims


# ---- sign-in ------------------------------------------------------------------------------------------------------
def authorize_url(attempt: dict, rec: dict) -> str:
    q = {"response_type": "code", "redirect_uri": attempt["redirect_uri"], "scope": SCOPE, "resource": RESOURCE,
         "state": attempt["state"], "nonce": attempt["nonce"], "code_challenge_method": "S256",
         "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(attempt["verifier"].encode()).digest()).rstrip(b"=").decode(),
         "ext_agent_host_id": host_id()}
    if rec.get("client_id"):  # a returning account: its issued id, and hints so the account picker is skipped
        q["client_id"] = rec["client_id"]
        if rec.get("id_token"):
            q["id_token_hint"] = rec["id_token"]
        if rec.get("email"):
            q["login_hint"] = rec["email"]
    else:  # first time on this account: register the client; the callback returns its issued id
        q["client_id"] = REGISTER
        q["agent_name_hint"] = APP_NAME
    return AUTHORIZE + "?" + urllib.parse.urlencode(q)


PAGE = ("<!doctype html><meta charset=utf-8><title>Clea</title><body style='font:16px -apple-system,system-ui;margin:18vh auto;"
        "max-width:420px;text-align:center;color:#1a1a1a'><h2 style='font-weight:600'>{title}</h2><p style='color:#666'>{body}</p>")


class _Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return
        ok, msg = finish({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
        body = PAGE.format(title="You are signed in" if ok else "Sign-in did not finish",
                           body="You can close this tab and go back to Clea." if ok else msg).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # the query string carries the authorization code: never logged
        pass


def start(open_browser: bool = True) -> dict:
    """Begin a sign-in: listen on the loopback callback, then send the user's browser to OpenAI. Returns the attempt's
    public state; poll state() until it is done or failed. A second call while one is waiting returns the same attempt."""
    with _lock:
        if _attempt.get("state_name") == "waiting" and time.time() - _attempt.get("at", 0) < 600:
            return state()
        cancel()
        srv = None
        for port in PORTS:
            try:
                srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Callback)
                break
            except OSError:
                continue
        if srv is None:
            raise RuntimeError("could not open a local port for the sign-in callback")
        att = {"state": _rand.token_urlsafe(32), "nonce": _rand.token_urlsafe(32), "verifier": _rand.token_urlsafe(64),
               "redirect_uri": f"http://127.0.0.1:{srv.server_address[1]}/auth/callback", "server": srv, "at": time.time(),
               "state_name": "waiting", "error": None}
        att["url"] = authorize_url(att, record())
        _attempt.clear()
        _attempt.update(att)
        threading.Thread(target=srv.serve_forever, name="siwc-callback", daemon=True).start()
    if open_browser:
        subprocess.run(["open", att["url"]], capture_output=True, timeout=20)
    log("siwc: sign-in started", port=srv.server_address[1], returning=bool(record().get("client_id")))
    return state()


def state() -> dict:
    """The sign-in in flight, as a screen may see it. The URL is the authorization page (it may carry the ID token hint,
    so it is shown to the user's own app only and never logged)."""
    if not _attempt:
        return {"state": "idle", "url": None, "error": None}
    if _attempt.get("state_name") == "waiting" and time.time() - _attempt.get("at", 0) > 600:
        _fail("the sign-in was not finished in the browser")
    return {"state": _attempt.get("state_name"), "url": _attempt.get("url") if _attempt.get("state_name") == "waiting" else None,
            "error": _attempt.get("error")}


def cancel() -> None:
    srv = _attempt.get("server")
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    _attempt.clear()


def _fail(msg: str) -> tuple[bool, str]:
    _attempt.update(state_name="error", error=msg)
    srv = _attempt.pop("server", None)
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    log("siwc: sign-in failed", why=msg[:200])
    return False, msg


def finish(q: dict) -> tuple[bool, str]:
    """The browser came back. Validate the attempt, exchange the code, validate who signed in and what they allowed,
    then store the session."""
    if _attempt.get("state_name") != "waiting":
        return False, "No sign-in is waiting for this page. Start again from Clea."
    if not hmac.compare_digest(q.get("state") or "", _attempt["state"]):
        return _fail("The sign-in response did not match this attempt. Start again from Clea.")
    if q.get("error"):
        return _fail("You declined, so Clea cannot use your ChatGPT plan." if q["error"] == "access_denied"
                     else f"OpenAI answered: {q.get('error_description') or q['error']}")
    rec = record()
    saved = rec.get("client_id")
    issued = q.get("client_id")
    if saved and issued and issued != saved:
        return _fail("OpenAI returned a different app registration than the one saved for this account.")
    client_id = saved or issued
    if not client_id or client_id == REGISTER:
        return _fail("OpenAI did not return this app's registration. Start again from Clea.")
    if not q.get("code"):
        return _fail("OpenAI returned no authorization code.")
    try:
        tok = _post_form(TOKEN, {"grant_type": "authorization_code", "client_id": client_id, "code": q["code"],
                                 "code_verifier": _attempt["verifier"], "redirect_uri": _attempt["redirect_uri"],
                                 "resource": RESOURCE})
        claims = verify_id_token(tok.get("id_token") or "", client_id, _attempt["nonce"])
    except (OAuthError, ValueError, OSError) as e:
        return _fail(f"The sign-in could not be completed: {e}")
    if rec.get("subject") and rec["subject"] != claims["sub"]:
        return _fail("That is a different ChatGPT account than the one connected. Disconnect it first to switch.")
    scopes = sorted((tok.get("scope") or "").split())
    _save({"email": claims.get("email") or rec.get("email") or "", "issuer": ISSUER, "subject": claims["sub"],
           "client_id": client_id, "ext_agent_host_id": host_id(), "id_token": tok["id_token"],
           "access_token": tok.get("access_token"), "refresh_token": tok.get("refresh_token"),
           "token_type": tok.get("token_type") or "Bearer", "expires_in": tok.get("expires_in"),
           "expires_at": int(time.time() + float(tok.get("expires_in") or 3600)), "scopes": scopes,
           "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    srv = _attempt.pop("server", None)
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    if PLAN_SCOPE not in scopes:
        _attempt.update(state_name="error", error="You are signed in, but ChatGPT plan usage was not allowed for Clea. "
                                                  "Continue with ChatGPT again and allow it, or use an API key.")
        return False, _attempt["error"]
    _attempt.update(state_name="done", error=None)
    log("siwc: signed in", plan=True)
    return True, ""


# ---- using the session --------------------------------------------------------------------------------------------
def access_token() -> str:
    """A valid access token, refreshed when it is about to expire. Raises SignInNeeded when the session is gone and
    PlanNotEnabled when the user never allowed plan usage: a caller stops, it never falls back to another way to pay."""
    with _lock:
        rec = record()
        if not rec.get("refresh_token"):
            raise SignInNeeded("not signed in with ChatGPT")
        if PLAN_SCOPE not in (rec.get("scopes") or []):
            raise PlanNotEnabled("ChatGPT plan usage is not enabled for Clea")
        if rec.get("access_token") and time.time() < float(rec.get("expires_at") or 0) - REFRESH_EARLY_S:
            return rec["access_token"]
        try:
            tok = _post_form(TOKEN, {"grant_type": "refresh_token", "client_id": rec["client_id"],
                                     "refresh_token": rec["refresh_token"], "resource": RESOURCE})
        except OAuthError as e:
            if e.code in GONE:
                # the renewable session is over: clear the tokens, keep who it was so the next sign-in is one click
                _save({k: v for k, v in rec.items() if k not in ("access_token", "refresh_token", "expires_at", "expires_in")})
                log("siwc: session ended", code=e.code)
                raise SignInNeeded(f"the ChatGPT session ended ({e.code})") from None
            raise
        rec.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token") or rec["refresh_token"],
                   expires_in=tok.get("expires_in"), expires_at=int(time.time() + float(tok.get("expires_in") or 3600)),
                   saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if tok.get("scope"):
            rec["scopes"] = sorted(tok["scope"].split())
        if tok.get("id_token"):
            rec["id_token"] = tok["id_token"]
        _save(rec)  # the new access token and its replacement refresh token together, or the session is lost
        return rec["access_token"]


def sign_out() -> bool:
    """End the renewable session at OpenAI, then forget the tokens. The registration (issued client id, account) stays,
    so signing in again does not register a second client. Returns whether OpenAI confirmed the revocation."""
    rec = record()
    confirmed = True
    if rec.get("refresh_token"):
        try:
            _post_form(REVOKE, {"token": rec["refresh_token"], "token_type_hint": "refresh_token", "client_id": rec["client_id"]})
        except Exception as e:
            confirmed = False
            log("siwc: revocation not confirmed", err=str(e)[:160])
    if rec:
        _save({k: v for k, v in rec.items() if k not in ("access_token", "refresh_token", "expires_at", "expires_in", "scopes")})
    return confirmed
