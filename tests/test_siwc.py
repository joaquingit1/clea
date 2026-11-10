"""Sign in with ChatGPT against a local stand-in for OpenAI's authorization server: the whole flow (registration,
code exchange, ID token validation, plan permission, refresh rotation, a revoked session, sign-out) with real HTTP
and a real RSA signature, and no network."""
import base64
import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from ap import siwc, vault

TMP = Path(tempfile.mkdtemp(prefix="clea-siwc-"))


@pytest.fixture(autouse=True)
def scratch_install(monkeypatch):
    """Every test keeps its secrets and host id in a temp dir: never the Keychain, never the checkout (the engine may
    already be imported with the checkout as its home when this file runs after another test file)."""
    monkeypatch.setenv("CLEA_SECRETS", "file")
    monkeypatch.setattr(vault, "_file", lambda: TMP / "secrets.json")
    monkeypatch.setattr(siwc, "VAR", TMP)


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def key():
    pem = TMP / "key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(pem), "2048"], check=True, capture_output=True)
    mod = subprocess.run(["openssl", "rsa", "-in", str(pem), "-noout", "-modulus"], check=True, capture_output=True, text=True).stdout
    n = int(mod.strip().split("=", 1)[1], 16)
    jwk = {"kty": "RSA", "kid": "test-key", "alg": "RS256", "n": b64u(n.to_bytes((n.bit_length() + 7) // 8, "big")), "e": "AQAB"}

    def sign(claims: dict, kid: str = "test-key") -> str:
        head = b64u(json.dumps({"alg": "RS256", "kid": kid}).encode())
        body = b64u(json.dumps(claims).encode())
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(pem)], input=f"{head}.{body}".encode(),
                             check=True, capture_output=True).stdout
        return f"{head}.{body}.{b64u(sig)}"
    return jwk, sign


def claims(**over):
    return {"iss": siwc.ISSUER, "aud": "oaiapp_test", "sub": "user-1", "email": "dev@example.com", "exp": time.time() + 600,
            "nonce": "n1", **over}


def test_id_token_validation(key):
    jwk, sign = key
    tok = sign(claims())
    assert siwc.verify_id_token(tok, "oaiapp_test", "n1", keys=[jwk])["sub"] == "user-1"
    h, p, s = tok.split(".")
    forged = f"{h}.{b64u(json.dumps(claims(sub='someone-else')).encode())}.{s}"
    for bad, why in ((forged, "signature"), (sign(claims(aud="other")), "another client"), (sign(claims(iss="https://evil")), "not issued"),
                     (sign(claims(exp=time.time() - 3600)), "expired"), (sign(claims(nonce="x")), "nonce"),
                     (sign(claims(), kid="unknown"), "does not publish"), ("not-a-jwt", "not a JWT")):
        with pytest.raises(ValueError, match=why):
            siwc.verify_id_token(bad, "oaiapp_test", "n1", keys=[jwk])


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    calls: list = []
    refresh_ok = True
    scope = siwc.SCOPE
    sign = None
    nonce = None

    def do_POST(self):
        f = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        FakeOpenAI.calls.append((self.path, f))
        if self.path == "/revoke":
            return self._send(200, b"")
        if f["grant_type"] == "refresh_token" and not FakeOpenAI.refresh_ok:
            return self._send(400, json.dumps({"error": "invalid_grant", "error_description": "refresh token reused"}).encode())
        n = len([c for c in FakeOpenAI.calls if c[0] == "/token"])
        out = {"access_token": f"at-{n}", "refresh_token": f"rt-{n}", "token_type": "Bearer", "expires_in": 3600,
               "scope": FakeOpenAI.scope, "id_token": FakeOpenAI.sign(claims(nonce=FakeOpenAI.nonce))}
        self._send(200, json.dumps(out).encode())

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def openai(key, monkeypatch):
    jwk, sign = key
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(siwc, "TOKEN", base + "/token")
    monkeypatch.setattr(siwc, "REVOKE", base + "/revoke")
    monkeypatch.setattr(siwc, "_keys", lambda force=False: [jwk])
    FakeOpenAI.calls, FakeOpenAI.refresh_ok, FakeOpenAI.scope, FakeOpenAI.sign = [], True, siwc.SCOPE, staticmethod(sign)
    vault.delete(siwc.RECORD)
    siwc.cancel()
    yield FakeOpenAI
    siwc.cancel()
    srv.shutdown()


def browser_returns(att_url: str, **query) -> str:
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(att_url).query))
    FakeOpenAI.nonce = q["nonce"]
    back = q["redirect_uri"] + "?" + urllib.parse.urlencode({"state": q["state"], **query})
    with urllib.request.urlopen(back, timeout=10) as r:
        return r.read().decode()


def test_first_sign_in_registers_and_stores(openai):
    st = siwc.start(open_browser=False)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(st["url"]).query))
    assert st["state"] == "waiting" and q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "Clea"
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["redirect_uri"].endswith("/auth/callback")
    assert q["code_challenge_method"] == "S256" and q["resource"] == "https://api.openai.com/v1"
    assert q["ext_agent_host_id"].startswith("urn:uuid:") and siwc.PLAN_SCOPE in q["scope"] and "offline_access" in q["scope"]
    page = browser_returns(st["url"], code="abc", client_id="oaiapp_test")
    assert "signed in" in page and siwc.state()["state"] == "done"
    path, form = openai.calls[0]
    assert path == "/token" and form["grant_type"] == "authorization_code" and form["client_id"] == "oaiapp_test"
    assert form["code"] == "abc" and form["redirect_uri"] == q["redirect_uri"] and len(form["code_verifier"]) >= 43
    rec = siwc.record()
    assert rec["client_id"] == "oaiapp_test" and rec["subject"] == "user-1" and rec["email"] == "dev@example.com"
    assert siwc.status() == {"connected": True, "signed_in": True, "email": "dev@example.com", "plan_enabled": True,
                             "expires_at": rec["expires_at"], "registered": True}
    assert siwc.access_token() == "at-1" and len(openai.calls) == 1  # still fresh: no refresh


def test_wrong_state_and_declined(openai):
    st = siwc.start(open_browser=False)
    redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(st["url"]).query))["redirect_uri"]
    with urllib.request.urlopen(redirect + "?state=forged&code=abc&client_id=oaiapp_test", timeout=10) as r:
        assert "did not finish" in r.read().decode()
    assert siwc.state()["state"] == "error" and not openai.calls and not siwc.record()
    st = siwc.start(open_browser=False)
    browser_returns(st["url"], error="access_denied")
    assert siwc.state()["state"] == "error" and "declined" in siwc.state()["error"] and not openai.calls


def test_plan_not_allowed_is_not_connected(openai):
    openai.scope = "openid profile email offline_access"
    st = siwc.start(open_browser=False)
    browser_returns(st["url"], code="abc", client_id="oaiapp_test")
    assert siwc.state()["state"] == "error" and "plan usage was not allowed" in siwc.state()["error"]
    assert siwc.status()["signed_in"] and not siwc.status()["connected"]
    with pytest.raises(siwc.PlanNotEnabled):
        siwc.access_token()


def test_refresh_rotates_and_a_dead_session_asks_to_sign_in(openai):
    st = siwc.start(open_browser=False)
    browser_returns(st["url"], code="abc", client_id="oaiapp_test")
    rec = siwc.record()
    rec["expires_at"] = int(time.time()) + 60  # about to expire
    siwc._save(rec)
    assert siwc.access_token() == "at-2"
    path, form = openai.calls[-1]
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-1" and form["client_id"] == "oaiapp_test"
    assert "scope" not in form and siwc.record()["refresh_token"] == "rt-2"
    # returning sign-in reuses the issued client id and sends the hints, never the registration entrypoint
    st = siwc.start(open_browser=False)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(st["url"]).query))
    assert q["client_id"] == "oaiapp_test" and "agent_name_hint" not in q and q["login_hint"] == "dev@example.com" and q["id_token_hint"]
    siwc.cancel()
    rec = siwc.record()
    rec["expires_at"] = 0
    siwc._save(rec)
    openai.refresh_ok = False
    with pytest.raises(siwc.SignInNeeded):
        siwc.access_token()
    left = siwc.record()
    assert "refresh_token" not in left and left["client_id"] == "oaiapp_test" and not siwc.status()["connected"]


def test_sign_out_revokes_and_keeps_the_registration(openai):
    st = siwc.start(open_browser=False)
    browser_returns(st["url"], code="abc", client_id="oaiapp_test")
    assert siwc.sign_out() is True
    path, form = openai.calls[-1]
    assert path == "/revoke" and form == {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": "oaiapp_test"}
    assert siwc.record()["client_id"] == "oaiapp_test" and not siwc.status()["signed_in"]
    with pytest.raises(siwc.SignInNeeded):
        siwc.access_token()
