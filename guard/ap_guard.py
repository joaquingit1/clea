#!/usr/bin/env python3
"""Clea's outbound guard: the ONLY path to writes that leave the user's machine in their name.

A separate process from the worker. It alone holds the write credentials (the Slack user token, the GitHub token) and
checks every request against a policy file before acting: what may go out on its own, what needs the user's approval
(a Touch ID dialog this process opens itself, or their reaction on the ask in their own DM), and what never goes out.
The worker asks; it cannot post, merge or review by itself. Stdlib only, on purpose: it shares no code with the
harness it guards.

One guard per install. CLEA_HOME names the install; the policy is <home>/guard/policy.json (written by the
onboarding), the socket <home>/var/guard.sock, the log <home>/var/log/guard.log.

Request: one JSON line {"action":..., "payload": {...}} -> one JSON line {"ok": bool, "result"|"error":...}.

Policy modes, per action:
  self          only the user's own DM with themselves
  own_pr        only on pull requests the user authored
  ask           needs the user's approval of this exact text: a Touch ID dialog (approval "local", from the app), or
                their approving reaction on the ask message in their own DM
  merge_rule    the user's own PR, at the head that was approved, green: with a person's approval, or with only a
                bot's approval when the user confirms by Touch ID
  comment_only  a COMMENT review on someone else's PR: never an approve or a request for changes, never the user's
                own PR, never text that names the assistant
  approve_clean a bare approve (no text) of someone else's open, ready PR at the exact head a clean review read
  auto          allowed, logged
An action the policy does not list is refused.
"""
from __future__ import annotations

import html
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser(os.environ.get("CLEA_HOME") or "")
if not HOME:
    raise SystemExit("guard: CLEA_HOME is not set (the install this guard works for)")
SOCK = os.environ.get("CLEA_GUARD_SOCK") or os.path.join(HOME, "var", "guard.sock")
LOG = os.path.join(HOME, "var", "log", "guard.log")
POLICY_FILE = os.environ.get("CLEA_GUARD_POLICY") or os.path.join(HOME, "guard", "policy.json")
POLICY = json.load(open(POLICY_FILE))
POLICY_AT = os.stat(POLICY_FILE).st_mtime
ES = (POLICY.get("language") or "en").startswith("es")
# a bundle, so the Touch ID dialog shows Clea and its icon (guard/build.sh compiles it from ap_confirm.swift)
CONFIRM = os.environ.get("CLEA_CONFIRM") or os.path.join(HERE, "Clea.app", "Contents", "MacOS", "Clea")
ASSISTANT = re.compile(r"\bclea\b|\bclaude\b|\bcodex\b|\bchatgpt\b|anthropic|openai|\bautopilot\b|\bharness\b", re.I)
BOT = re.compile(r"bot|coderabbit|copilot", re.I)
_lock = threading.Lock()


def _t(en: str, es: str) -> str:
    return es if ES else en


# ---- credentials -------------------------------------------------------------------------------------------------
def _vault(name: str) -> str:
    """The install's secret as the app stored it: the Keychain item 'clea-<name>', or the install's own 0600 secrets
    file (a machine with no Keychain, a test install). Values are base64 behind a 'b64:' prefix."""
    import base64
    import getpass
    if os.environ.get("CLEA_SECRETS") == "file" or not os.path.exists("/usr/bin/security"):
        try:
            raw = json.load(open(os.path.join(HOME, "secrets.json"))).get(name, "")
        except (OSError, ValueError):
            raw = ""
    else:
        p = subprocess.run(["security", "find-generic-password", "-a", getpass.getuser(), "-s", f"clea-{name}", "-w"],
                           capture_output=True, text=True)
        raw = p.stdout.strip() if p.returncode == 0 else ""
    if raw.startswith("b64:"):
        try:
            return base64.b64decode(raw[4:]).decode()
        except Exception:
            return ""
    return raw


def _secrets() -> dict:
    gh_tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    return {"slack_token": _vault("slack-user-token"), "github_token": gh_tok}


SECRETS = _secrets()


def log(entry: dict) -> None:
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with _lock, open(LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---- HTTP --------------------------------------------------------------------------------------------------------
def _http(url: str, method: str = "GET", body=None, headers=None) -> dict:
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode() or "{}"
    return json.loads(raw)


SLACK_READS = ("conversations.history", "conversations.replies", "reactions.get", "auth.test", "conversations.info")


def slack(method: str, **kw) -> dict:
    url = f"{POLICY.get('slack_api', 'https://slack.com/api').rstrip('/')}/{method}"
    hdr = {"Authorization": f"Bearer {SECRETS['slack_token']}"}
    args = {k: v for k, v in kw.items() if v is not None}
    d = _http(url + "?" + urllib.parse.urlencode(args), headers=hdr) if method in SLACK_READS else _http(url, "POST", args, headers=hdr)
    if not d.get("ok"):
        raise RuntimeError(f"slack {method}: {d.get('error')}")
    return d


def gh(path: str, method: str = "GET", body=None):
    return _http(f"{POLICY.get('github_api', 'https://api.github.com').rstrip('/')}/{path.lstrip('/')}", method, body,
                 {"Authorization": f"Bearer {SECRETS['github_token']}", "Accept": "application/vnd.github+json"})


def gh_graphql(query: str, variables: dict) -> dict:
    return _http(f"{POLICY.get('github_api', 'https://api.github.com').rstrip('/')}/graphql", "POST",
                 {"query": query, "variables": variables}, {"Authorization": f"Bearer {SECRETS['github_token']}"})


def _slug(repo: str) -> str:
    """owner/name of a repo the install works in. The policy lists them; a repo it does not list is not this guard's
    to write to, whatever the worker says."""
    full = (POLICY.get("repos") or {}).get(repo)
    if not full:
        raise RuntimeError(f"guard: {repo} is not one of this install's repositories")
    return full


# ---- policy checks -----------------------------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)
    s = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", s)
    s = re.sub(r"<([^>]+)>", r"\1", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _ask_approved(ask_ts: str, text: str) -> str | None:
    """None when the user reacted with an approval on the ask message in their own DM and that message carries `text`;
    otherwise the reason. Read here, with the guard's own token: the worker's word that it was approved counts for
    nothing."""
    if not ask_ts:
        return "no ask_ts"
    ch = POLICY["self_dm_channel"]
    msgs = slack("conversations.history", channel=ch, latest=ask_ts, inclusive="true", limit=1).get("messages") or []
    if not msgs or msgs[0].get("ts") != ask_ts:
        msgs = slack("conversations.replies", channel=ch, ts=ask_ts, limit=1).get("messages") or []
    if not msgs or msgs[0].get("ts") != ask_ts:
        return "ask message not found"
    m = msgs[0]
    ok = any(r.get("name", "").split("::")[0] in POLICY["approve_reactions"] and POLICY["self_user_id"] in r.get("users", [])
             for r in m.get("reactions", []))
    if not ok:
        return "no approving reaction from the user on the ask"
    if _norm(text)[:600] not in _norm(m.get("text", "")):
        return "payload text is not the text that was approved"
    return None


def _display(s: str) -> str:
    return re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", html.unescape(s or ""))


def _channel_name(ch: str) -> str:
    try:
        name = (slack("conversations.info", channel=ch).get("channel") or {}).get("name")
        return f"#{name}" if name else ch
    except Exception:
        return ch


def _local_approved(action: str, p: dict) -> str | None:
    """An approval made in the app: a system Touch ID (or Mac password) dialog that this process opens and that names
    what goes where. Only a person can pass it, so a worker that asks for its own approval still cannot send."""
    text = re.sub(r"\s+", " ", _display(p.get("text") or p.get("body") or "")).strip()
    pr = f"{p.get('repo')}#{p.get('pr')}"
    if action == "slack_post":
        where = _channel_name(p.get("channel", ""))
        what = _t(f"send in {where}", f"mandar en {where}")
    elif action == "gh_merge":
        what = _t(f"merge {pr}", f"mergear {pr}") + ("" if p.get("_human") else _t(" on a bot's approval alone", " con solo el approve de un bot"))
        text = text or str(p.get("title") or "")
    elif action in ("gh_review", "gh_comment_other_pr"):
        n = len(p.get("comments") or [])
        what = _t(f"post your review on {pr}", f"publicar tu review en {pr}") + (
            _t(f" ({n} comment{'s' if n != 1 else ''})", f" ({n} comentario{'s' if n != 1 else ''})") if n else "")
        if p.get("event") == "APPROVE":  # the dialog says it approves: the text alone is empty
            what = _t(f"approve {pr} on GitHub with your account", f"aprobar {pr} en GitHub con tu cuenta")
            text = text or _t("no comments", "sin comentarios")
    else:
        what = action
    reason = f"{what}: “{text[:140]}{'…' if len(text) > 140 else ''}”"
    if not os.path.exists(CONFIRM):
        return "local approval unavailable (the confirmation helper is not installed)"
    try:
        r = subprocess.run([CONFIRM, reason, "--cancel-title", _t("Don't send", "No enviar")], capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        return "local approval timed out"
    if r.returncode == 0:
        return None
    return "local approval declined" if r.returncode == 1 else f"local approval unavailable: {r.stderr.strip()[:120]}"


def _pr_author(repo: str, n: int) -> str:
    return (gh(f"repos/{_slug(repo)}/pulls/{n}").get("user") or {}).get("login", "")


def _check(action: str, p: dict) -> str | None:
    mode = (POLICY.get("actions") or {}).get(action)
    me = POLICY["self_github"]
    if mode is None:
        return f"action {action} not in policy"
    if mode == "self":
        return None if p.get("channel", POLICY["self_dm_channel"]) == POLICY["self_dm_channel"] else "own DM only"
    if mode == "own_pr":
        return None if _pr_author(p["repo"], int(p["pr"])) == me else "not the user's PR"
    if mode == "ask":
        if action == "slack_post" and re.search(r"\bclea\b", p.get("text") or "", re.I):
            return "the text names the assistant: a message to other people is the user's own"  # whoever approved it
        if p.get("approval") == "local":
            return _local_approved(action, p)
        return _ask_approved(p.get("ask_ts"), p.get("text") or p.get("body") or "")
    if mode == "merge_rule":
        full, n = _slug(p["repo"]), int(p["pr"])
        pr = gh(f"repos/{full}/pulls/{n}")
        if (pr.get("user") or {}).get("login") != me:
            return "not the user's PR"
        revs = gh(f"repos/{full}/pulls/{n}/reviews?per_page=100")
        human = [r for r in revs if r.get("state") == "APPROVED" and "bot" not in (r.get("user") or {}).get("type", "").lower()
                 and not BOT.search((r.get("user") or {}).get("login", "")) and (r.get("user") or {}).get("login") != me]
        local = p.get("approval") == "local"
        if p.get("head_sha") and p["head_sha"] != pr["head"]["sha"]:
            return "head moved since it was approved"
        if not human:
            if not local:
                return "no human approval"
            bot_ok = any(r.get("state") == "APPROVED" and BOT.search((r.get("user") or {}).get("login", ""))
                         and r.get("commit_id") == pr["head"]["sha"] for r in revs)
            if not bot_ok:
                return "no approval on this head (neither a person nor a review bot)"
        st = gh(f"repos/{full}/commits/{pr['head']['sha']}/status")
        if st.get("state") not in ("success", "pending") and st.get("total_count", 0):
            return f"checks {st.get('state')}"
        return _local_approved(action, {**p, "_human": bool(human)}) if local else None
    if mode == "comment_only":
        if p.get("event", "COMMENT") != "COMMENT":
            return "comment-only: no approve or request-changes without the user's approval"
        if _pr_author(p["repo"], int(p["pr"])) == me:
            return "the user's own PR"
        said = " ".join([p.get("body") or ""] + [c.get("body") or "" for c in p.get("comments") or []])
        if ASSISTANT.search(said):
            return "the review names the assistant"
        return None
    if mode == "approve_clean":
        if p.get("body") or p.get("comments"):
            return "approve_clean: a bare approve only, no text"
        pr = gh(f"repos/{_slug(p['repo'])}/pulls/{int(p['pr'])}")
        if (pr.get("user") or {}).get("login") == me:
            return "the user's own PR"
        if pr.get("state") != "open" or pr.get("draft"):
            return "approve_clean: the PR is not open and ready"
        if not p.get("head_sha") or p["head_sha"] != pr["head"]["sha"]:
            return "approve_clean: head moved since the review read it"
        return None
    if mode == "auto":
        return None
    return f"unknown mode {mode}"


# ---- actions -----------------------------------------------------------------------------------------------------
def act(action: str, p: dict):
    dm = POLICY["self_dm_channel"]
    full = _slug(p["repo"]) if p.get("repo") else ""
    n = int(p["pr"]) if p.get("pr") is not None else 0
    if action == "slack_post_self":  # no link previews in the user's DM: unfurled cards bury the text
        return slack("chat.postMessage", channel=dm, text=p["text"], thread_ts=p.get("thread_ts"), as_user="true",
                     unfurl_links=False, unfurl_media=False)
    if action == "slack_edit_self":  # attachments=[] also drops the preview Slack attached when it was first posted
        return slack("chat.update", channel=dm, ts=p["ts"], text=p["text"], as_user="true", unfurl_links=False,
                     unfurl_media=False, attachments=[])
    if action == "slack_delete_self":
        return slack("chat.delete", channel=dm, ts=p["ts"], as_user="true")
    if action == "slack_pin_self":
        return slack("pins.add" if p.get("pin", True) else "pins.remove", channel=dm, timestamp=p["ts"])
    if action == "slack_post":  # as the user, with their own token: the approved text and nothing else
        r = slack("chat.postMessage", channel=p["channel"], text=p["text"], thread_ts=p.get("thread_ts"), as_user="true",
                  unfurl_links=False, unfurl_media=False)
        return {"ts": r.get("ts"), "channel": r.get("channel")}
    if action == "gh_pr_create":
        return gh(f"repos/{full}/pulls", "POST", {"title": p["title"], "body": p["body"], "head": p["head"], "base": p["base"],
                                                   "draft": bool(p.get("draft"))})
    if action == "gh_pr_op":
        op = p["op"]
        if op == "edit":
            return gh(f"repos/{full}/pulls/{n}", "PATCH", {"title": p["title"], "body": p["body"]})
        if op == "close":
            if p.get("comment"):
                gh(f"repos/{full}/issues/{n}/comments", "POST", {"body": p["comment"]})
            return gh(f"repos/{full}/pulls/{n}", "PATCH", {"state": "closed"})
        if op == "comment":
            return gh(f"repos/{full}/issues/{n}/comments", "POST", {"body": p["body"]})
        if op in ("ready", "draft"):
            node = gh(f"repos/{full}/pulls/{n}")["node_id"]
            mut = "markPullRequestReadyForReview" if op == "ready" else "convertPullRequestToDraft"
            return gh_graphql(f"mutation($id:ID!){{{mut}(input:{{pullRequestId:$id}}){{pullRequest{{isDraft}}}}}}", {"id": node})
        if op == "update_branch":
            return gh(f"repos/{full}/pulls/{n}/update-branch", "PUT", {"expected_head_sha": p["head_sha"]})
        if op == "rerun":
            return gh(f"repos/{full}/actions/runs/{int(p['run_id'])}/rerun-failed-jobs", "POST", {})
        raise ValueError(f"op {op}")
    if action == "gh_reply_own_pr":
        if p.get("reply_to"):
            out = gh(f"repos/{full}/pulls/{n}/comments/{int(p['reply_to'])}/replies", "POST", {"body": p["body"]})
        else:
            out = gh(f"repos/{full}/issues/{n}/comments", "POST", {"body": p["body"]})
        if p.get("resolve_thread_id"):
            gh_graphql("mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}", {"id": p["resolve_thread_id"]})
        return {"id": out.get("id")}
    if action == "gh_comment_other_pr":
        return gh(f"repos/{full}/issues/{n}/comments", "POST", {"body": p["body"]})
    if action == "gh_pending_review":  # no event: stays PENDING, visible only to the user until they submit it
        return gh(f"repos/{full}/pulls/{n}/reviews", "POST", {"commit_id": p["head_sha"], "body": p["body"], "comments": p.get("comments", [])})
    if action == "gh_review_comment":
        return gh(f"repos/{full}/pulls/{n}/reviews", "POST", {"commit_id": p["head_sha"], "body": p["body"], "event": "COMMENT",
                                                              "comments": p.get("comments", [])})
    if action == "gh_approve_clean":
        return gh(f"repos/{full}/pulls/{n}/reviews", "POST", {"commit_id": p["head_sha"], "body": "", "event": "APPROVE"})
    if action == "gh_review":
        return gh(f"repos/{full}/pulls/{n}/reviews", "POST", {"commit_id": p["head_sha"], "body": p["body"],
                                                              "event": p.get("event", "COMMENT"), "comments": p.get("comments", [])})
    if action == "gh_merge":
        return gh(f"repos/{full}/pulls/{n}/merge", "PUT", {"merge_method": p.get("method", "squash"), "sha": p.get("head_sha")})
    raise ValueError(f"unknown action {action}")


def _reload_policy() -> None:
    """The user changed what Clea may do (the settings screen rewrites the policy file): the next request is judged by
    the new policy. Only the file decides; nothing a request says can change it. A changed user is not a policy
    change: the guard keeps refusing until it is restarted and checks the token's owner again."""
    global POLICY, POLICY_AT, ES
    try:
        at = os.stat(POLICY_FILE).st_mtime
        if at == POLICY_AT:
            return
        new = json.load(open(POLICY_FILE))
    except (OSError, ValueError):
        return  # half-written: the next request reads it
    if new.get("self_user_id") != POLICY.get("self_user_id"):
        log({"fatal": "the policy names another user; restart the guard"})
        raise SystemExit("guard: the policy names another user")
    POLICY, POLICY_AT, ES = new, at, (new.get("language") or "en").startswith("es")
    log({"policy_reloaded": True, "actions": sorted(new.get("actions") or {})})


def handle(req: dict) -> dict:
    _reload_policy()
    action, p = req.get("action"), req.get("payload") or {}
    try:
        why = _check(action, p)
    except Exception as e:
        why = f"check failed: {e}"
    if why:
        log({"action": action, "allowed": False, "why": why, "payload": {k: str(v)[:200] for k, v in p.items()}})
        return {"ok": False, "error": f"guard: {why}"}
    try:
        res = act(action, p)
        rid = (res or {}).get("ts") or (res or {}).get("id") or (res or {}).get("number")
        log({"action": action, "allowed": True, "channel": p.get("channel"), "ts": (res or {}).get("ts"), "id": rid,
             "repo": p.get("repo"), "pr": p.get("pr"), "text": (p.get("text") or p.get("body") or "")[:160]})
        return {"ok": True, "result": res}
    except Exception as e:
        log({"action": action, "allowed": True, "failed": str(e)[:300]})
        return {"ok": False, "error": str(e)[:500]}


# ---- server ------------------------------------------------------------------------------------------------------
def serve() -> None:
    # the identity the policy trusts must be the token's owner, or every approval check is checking the wrong person
    who = slack("auth.test").get("user_id")
    if who != POLICY["self_user_id"]:
        log({"fatal": f"policy self_user_id {POLICY['self_user_id']} != token owner {who}"})
        raise SystemExit(f"guard: the policy's user is not the Slack token's owner ({who})")
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)  # the user's own processes only
    srv.listen(16)
    log({"started": True, "identity": who, "actions": sorted(POLICY.get("actions") or {})})

    def client(conn):
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            try:
                resp = handle(json.loads(buf.decode()))
            except Exception as e:
                resp = {"ok": False, "error": f"bad request: {e}"}
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())

    while True:
        c, _ = srv.accept()
        threading.Thread(target=client, args=(c,), daemon=True).start()


if __name__ == "__main__":
    serve()
