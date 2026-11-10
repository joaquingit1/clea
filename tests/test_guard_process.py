"""The release guard as it runs: a separate process, its socket, its policy file, the install's secret store, and a
Slack that is a local HTTP server. What the guard lets out and what it refuses is checked from the outside, the way
the worker meets it."""
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
ME, DM, TEAM = "U0TESTME001", "D0TESTDM001", "C0TESTTEAM1"
TOKEN = "xo" + "xp-test-token-not-real"  # split so no scanner reads it as a real token


class FakeSlack:
    """auth.test, the history of the user's own DM (where asks live) and chat.postMessage, recorded."""

    def __init__(self, owner: str = ME):
        self.owner, self.posted, self.auth, self.dm = owner, [], [], {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, d):
                raw = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.auth.append(self.headers.get("Authorization"))
                method = u.path.strip("/").split("/")[-1]
                if method == "auth.test":
                    return self._send({"ok": True, "user_id": fake.owner})
                if method in ("conversations.history", "conversations.replies"):
                    m = fake.dm.get(q.get("latest") or q.get("ts"))
                    return self._send({"ok": True, "messages": [m] if m and q.get("channel") == DM else []})
                if method == "conversations.info":
                    return self._send({"ok": True, "channel": {"name": "team"}})
                self._send({"ok": False, "error": "unknown_method"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.auth.append(self.headers.get("Authorization"))
                method = urlparse(self.path).path.strip("/").split("/")[-1]
                if method == "chat.postMessage":
                    fake.posted.append(body)
                    return self._send({"ok": True, "ts": f"17000000{len(fake.posted):02d}.000100", "channel": body["channel"]})
                self._send({"ok": False, "error": "unknown_method"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def ask(self, ts: str, text: str, approved: bool) -> None:
        self.dm[ts] = {"ts": ts, "text": text, "reactions": [{"name": "white_check_mark", "users": [ME], "count": 1}] if approved else []}

    def close(self):
        self.srv.shutdown()


def _install(home: Path, slack_url: str) -> None:
    (home / "guard").mkdir(parents=True)
    (home / "var").mkdir()
    (home / "guard" / "policy.json").write_text(json.dumps({
        "self_user_id": ME, "self_dm_channel": DM, "self_github": "me", "language": "en",
        "approve_reactions": ["white_check_mark", "+1"], "repos": {"web": "acme/web"},
        "actions": {"slack_post_self": "self", "slack_post": "ask"},
        "slack_api": slack_url, "github_api": slack_url}))
    (home / "secrets.json").write_text(json.dumps({"slack-user-token": "b64:" + base64.b64encode(TOKEN.encode()).decode()}))
    os.chmod(home / "secrets.json", 0o600)


def _start(home: Path) -> subprocess.Popen:
    env = {**os.environ, "CLEA_HOME": str(home), "CLEA_SECRETS": "file", "CLEA_CONFIRM": str(home / "no-helper"),
           "GH_CONFIG_DIR": str(home / "gh"), "GH_TOKEN": "gh-test-token-not-real"}
    env.pop("CLEA_GUARD_SOCK", None)
    env.pop("CLEA_GUARD_POLICY", None)
    return subprocess.Popen([sys.executable, str(ROOT / "guard" / "ap_guard.py")], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def _call(home: Path, action: str, **payload) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(20)
        c.connect(str(home / "var" / "guard.sock"))
        c.sendall((json.dumps({"action": action, "payload": payload}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


@pytest.fixture
def guard(tmp_path):
    # a unix socket path has a short limit: the install lives under /tmp, not under pytest's long tmp_path
    import tempfile
    home = Path(tempfile.mkdtemp(prefix="clea-g-", dir="/tmp"))
    slack = FakeSlack()
    _install(home, slack.url)
    proc = _start(home)
    sock = home / "var" / "guard.sock"
    deadline = time.time() + 20
    while not sock.exists() and proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert sock.exists(), f"the guard did not come up: {proc.stderr.read()[-400:] if proc.poll() is not None else 'timeout'}"
    try:
        yield home, slack
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        slack.close()
        import shutil
        shutil.rmtree(home, ignore_errors=True)


def test_the_socket_is_the_users_alone(guard):
    home, _ = guard
    assert oct((home / "var" / "guard.sock").stat().st_mode)[-3:] == "600"


def test_own_dm_goes_out_with_the_installs_token(guard):
    home, slack = guard
    r = _call(home, "slack_post_self", text="a case report")
    assert r["ok"], r
    assert slack.posted[-1]["channel"] == DM and slack.posted[-1]["text"] == "a case report"
    assert slack.auth[-1] == f"Bearer {TOKEN}"


def test_a_message_to_others_without_approval_is_refused(guard):
    home, slack = guard
    r = _call(home, "slack_post", channel=TEAM, text="on it, fix by tonight")
    assert not r["ok"] and r["error"].startswith("guard:"), r
    r = _call(home, "slack_post", channel=TEAM, text="on it, fix by tonight", ask_ts="1700000001.000100")
    assert not r["ok"] and "not found" in r["error"], r  # an ask the worker made up
    slack.ask("1700000002.000100", "Send this in the thread?\n> on it, fix by tonight", approved=False)
    r = _call(home, "slack_post", channel=TEAM, text="on it, fix by tonight", ask_ts="1700000002.000100")
    assert not r["ok"] and "no approving reaction" in r["error"], r
    assert slack.posted == []


def test_the_approved_text_goes_out_and_only_that_text(guard):
    home, slack = guard
    slack.ask("1700000003.000100", "Send this in the thread?\n> on it, fix by tonight", approved=True)
    r = _call(home, "slack_post", channel=TEAM, text="on it, fix by tonight and I will also refund everyone", ask_ts="1700000003.000100")
    assert not r["ok"] and "not the text that was approved" in r["error"], r
    assert slack.posted == []
    r = _call(home, "slack_post", channel=TEAM, text="on it, fix by tonight", thread_ts="1699999999.000100", ask_ts="1700000003.000100")
    assert r["ok"], r
    assert slack.posted == [{"channel": TEAM, "text": "on it, fix by tonight", "thread_ts": "1699999999.000100", "as_user": "true",
                             "unfurl_links": False, "unfurl_media": False}]


def test_an_approval_in_the_app_needs_the_confirmation_helper(guard):
    home, slack = guard
    r = _call(home, "slack_post", channel=TEAM, text="on it", approval="local")  # no helper installed: nobody confirmed
    assert not r["ok"] and "local approval unavailable" in r["error"], r
    assert slack.posted == []


def test_a_message_that_names_the_assistant_never_goes_out(guard):
    home, slack = guard
    slack.ask("1700000004.000100", "Send this?\n> Clea checked it for me: all good", approved=True)
    r = _call(home, "slack_post", channel=TEAM, text="Clea checked it for me: all good", ask_ts="1700000004.000100")
    assert not r["ok"] and "names the assistant" in r["error"], r
    assert slack.posted == []


def test_what_the_policy_does_not_list_is_refused(guard):
    home, slack = guard
    for action, payload in (("gh_merge", {"repo": "web", "pr": 1}), ("gh_pr_create", {"repo": "web", "title": "x", "body": "", "head": "b", "base": "main"}),
                            ("no_such_action", {})):
        r = _call(home, action, **payload)
        assert not r["ok"] and "not in policy" in r["error"], (action, r)
    r = _call(home, "slack_post_self", text="x", channel=TEAM)  # 'self' is the user's own DM and nowhere else
    assert not r["ok"] and "own DM only" in r["error"], r
    assert slack.posted == []


def test_a_guard_whose_policy_names_someone_else_does_not_start(tmp_path):
    import shutil
    import tempfile
    home = Path(tempfile.mkdtemp(prefix="clea-g-", dir="/tmp"))
    slack = FakeSlack(owner="U0SOMEONEELSE")
    try:
        _install(home, slack.url)
        proc = _start(home)
        out, err = proc.communicate(timeout=30)
        assert proc.returncode != 0 and "not the Slack token's owner" in err, (proc.returncode, err[-300:])
        assert not (home / "var" / "guard.sock").exists()
    finally:
        slack.close()
        shutil.rmtree(home, ignore_errors=True)
