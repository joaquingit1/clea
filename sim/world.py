"""A Slack workspace and a GitHub org that exist only in memory.

The simulation runs the real engine (intake, triage, the case pipeline, the guard's policy) against this world instead
of the user's real Slack and GitHub. Two faces of the same state:
  - what the engine reads: the Slack Web API methods it calls (conversations.history, conversations.replies,
    search.messages, users.info, reactions.get...), with Slack's own semantics where the engine depends on them
    (history returns thread roots only, a reply's ts asked as a thread returns that one message, search lags behind);
  - what the guard writes: chat.postMessage / chat.update / chat.delete / pins and the GitHub REST calls, applied to
    the same state and recorded in `outbox`, so a scenario can assert exactly what left in the user's name.
Nothing here touches the network.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from slack_sdk.errors import SlackApiError


class _Resp(dict):
    """Enough of slack_sdk's SlackResponse for the engine's error handling."""

    def __init__(self, data: dict, status: int = 200, headers: dict | None = None):
        super().__init__(data)
        self.data, self.status_code, self.headers = data, status, headers or {}


def api_error(error: str, status: int = 200, **extra) -> SlackApiError:
    headers = {"Retry-After": "0"} if status == 429 else {}
    return SlackApiError(f"The request to the Slack API failed. The server responded with: {{'ok': False, 'error': '{error}'}}",
                         _Resp({"ok": False, "error": error, **extra}, status, headers))


@dataclass
class World:
    workspace: str = "acme"
    me: str = "U0SIMSELF01"
    clock: float = field(default_factory=time.time)  # the last ts handed out: every message gets a later one
    users: dict = field(default_factory=dict)
    channels: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)
    messages: dict = field(default_factory=dict)  # channel id -> [message,...] in posting order
    prs: dict = field(default_factory=dict)       # (repo, number) -> pull request
    outbox: list = field(default_factory=list)    # every write made in the user's name, in order
    calls: list = field(default_factory=list)     # every Slack read: (method, args)
    search_lag_s: float = 0.0                     # Slack's search index trails real time
    faults: dict = field(default_factory=dict)    # method -> [error,...] raised on its next calls
    touch_id: str = "approve"                     # what the user does at the Touch ID dialog: approve | decline

    # ---- building the workspace ---------------------------------------------------------------------------------
    def user(self, uid: str, name: str, real_name: str = "", bot: bool = False) -> str:
        self.users[uid] = {"id": uid, "name": name, "real_name": real_name or name.title(), "is_bot": bot, "deleted": False,
                           "profile": {"real_name": real_name or name.title(), "title": ""}}
        return uid

    def channel(self, cid: str, name: str, private: bool = False, members: tuple = ()) -> str:
        self.channels[cid] = {"id": cid, "name": name, "kind": "group" if private else "channel", "members": set(members) | {self.me}}
        self.messages.setdefault(cid, [])
        return cid

    def dm(self, cid: str, other: str) -> str:
        self.channels[cid] = {"id": cid, "name": "", "kind": "im", "user": other, "members": {self.me, other}}
        self.messages.setdefault(cid, [])
        return cid

    def group(self, gid: str, handle: str, users: tuple = ()) -> str:
        self.groups[gid] = {"id": gid, "handle": handle, "name": handle, "users": list(users)}
        return gid

    def pr(self, repo: str, number: int, author: str, title: str, **extra) -> dict:
        self.prs[(repo, number)] = {"number": number, "title": title, "author": {"login": author}, "state": "OPEN", "isDraft": False,
                                    "headRefOid": f"{number:040x}", "headRefName": f"feature-{number}", "baseRefName": "main",
                                    "body": "", "url": f"https://github.com/{self.workspace}/{repo}/pull/{number}",
                                    "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock)),
                                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock)),
                                    "reviewRequests": [], "files": [], "statusCheckRollup": [], "reviewDecision": "", **extra}
        return self.prs[(repo, number)]

    # ---- people writing -----------------------------------------------------------------------------------------
    def _ts(self) -> str:
        self.clock = max(self.clock + 0.000100, time.time())
        return f"{self.clock:.6f}"

    def post(self, channel: str, user: str, text: str, thread_ts: str | None = None, **extra) -> str:
        """Someone writes. A person's message carries a client_msg_id (their Slack client made it); a bot's carries a
        bot_id; one posted through the API in the user's name (what the guard does) carries neither."""
        if channel not in self.channels:
            raise KeyError(f"no channel {channel}")
        ts = self._ts()
        m = {"type": "message", "ts": ts, "text": text, "reactions": [], **extra}
        if (self.users.get(user) or {}).get("is_bot") or extra.get("bot_id"):
            m.setdefault("bot_id", "B" + user[1:])
            m.setdefault("subtype", "bot_message")
            m["username"] = (self.users.get(user) or {}).get("name", user)
        else:
            m["user"] = user
            m.setdefault("client_msg_id", f"cm-{ts}")
        if thread_ts:
            root = self._find(channel, thread_ts)
            if root is None:
                raise KeyError(f"no message {thread_ts} in {channel}")
            m["thread_ts"] = thread_ts
            root["thread_ts"] = thread_ts
            root["reply_count"] = root.get("reply_count", 0) + 1
            root["latest_reply"] = ts
        self.messages[channel].append(m)
        return ts

    def api_post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        """A message posted through the Slack API with the user's token: theirs, with no client_msg_id."""
        ts = self.post(channel, self.me, text, thread_ts)
        self._find(channel, ts).pop("client_msg_id", None)
        return ts

    def react(self, channel: str, ts: str, user: str, name: str) -> None:
        m = self._find(channel, ts)
        r = next((r for r in m["reactions"] if r["name"] == name), None)
        if r is None:
            m["reactions"].append({"name": name, "users": [user], "count": 1})
        elif user not in r["users"]:
            r["users"].append(user)
            r["count"] += 1

    def edit(self, channel: str, ts: str, text: str) -> None:
        m = self._find(channel, ts)
        m["text"], m["edited"] = text, {"user": m.get("user"), "ts": self._ts()}

    def delete(self, channel: str, ts: str) -> None:
        self.messages[channel] = [m for m in self.messages[channel] if m["ts"] != ts]

    def advance(self, seconds: float) -> None:
        """Time passes with nobody writing (the engine's clock is this one: see sim/harness)."""
        self.clock += seconds

    def _find(self, channel: str, ts: str) -> dict | None:
        return next((m for m in self.messages.get(channel, []) if m["ts"] == ts), None)

    def thread(self, channel: str, root: str) -> list[dict]:
        return [m for m in self.messages.get(channel, []) if m["ts"] == root or m.get("thread_ts") == root]

    def sent(self, kind: str | None = None) -> list[dict]:
        return [o for o in self.outbox if kind is None or o["kind"] == kind]

    # ---- what the engine reads: Slack Web API ---------------------------------------------------------------------
    def api(self, method: str, **kw) -> dict:
        self.calls.append((method, kw))
        queue = self.faults.get(method) or []
        if queue:
            err = queue.pop(0)
            raise api_error(err, 429 if err == "ratelimited" else 200)
        fn = getattr(self, "_m_" + method.replace(".", "_"), None)
        if fn is None:
            raise NotImplementedError(f"the simulated Slack has no method {method}")
        return {"ok": True, **fn(**kw)}

    def _chan(self, channel: str) -> dict:
        c = self.channels.get(channel)
        if c is None:
            raise api_error("channel_not_found")
        return c

    def _page(self, msgs: list, limit, cursor) -> dict:
        start = int(cursor or 0)
        limit = int(limit or 100)
        out = msgs[start:start + limit]
        more = start + limit < len(msgs)
        return {"messages": [self._public(m) for m in out], "has_more": more,
                "response_metadata": {"next_cursor": str(start + limit) if more else ""}}

    @staticmethod
    def _public(m: dict) -> dict:
        out = {k: v for k, v in m.items() if k != "reactions" or v}
        return out

    def _m_conversations_history(self, channel, oldest=None, latest=None, inclusive=False, limit=100, cursor=None, **_):
        self._chan(channel)
        lo, hi = float(oldest or 0), float(latest or 1e12)
        inc = str(inclusive).lower() in ("true", "1")
        # roots only: a reply shows in the channel only when it was also sent to the channel (thread_broadcast)
        rows = [m for m in self.messages[channel]
                if (not m.get("thread_ts") or m["thread_ts"] == m["ts"] or m.get("subtype") == "thread_broadcast")
                and (lo <= float(m["ts"]) if inc else lo < float(m["ts"])) and (float(m["ts"]) <= hi if inc else float(m["ts"]) < hi)]
        rows.sort(key=lambda m: float(m["ts"]), reverse=True)  # newest first, like Slack
        return self._page(rows, limit, cursor)

    def _m_conversations_replies(self, channel, ts, oldest=None, limit=100, cursor=None, **_):
        self._chan(channel)
        target = self._find(channel, ts)
        if target is None:
            raise api_error("thread_not_found")
        if target.get("thread_ts") and target["thread_ts"] != ts:
            return self._page([target], limit, cursor)  # a reply asked for as a thread is a thread of one
        lo = float(oldest or 0)
        rows = [m for m in self.thread(channel, ts) if m["ts"] == ts or float(m["ts"]) > lo]
        rows.sort(key=lambda m: float(m["ts"]))
        return self._page(rows, limit, cursor)

    def _m_conversations_list(self, types="public_channel", limit=100, cursor=None, exclude_archived=True, **_):
        want = set(str(types).split(","))
        kinds = {"public_channel": "channel", "private_channel": "group", "im": "im", "mpim": "mpim"}
        rows = []
        for c in self.channels.values():
            if c["kind"] not in {kinds[t] for t in want if t in kinds}:
                continue
            row = {"id": c["id"], "name": c.get("name", ""), "is_private": c["kind"] == "group", "is_member": self.me in c["members"],
                   "num_members": len(c["members"]), "purpose": {"value": ""}}
            if c["kind"] == "im":
                row.update(is_im=True, user=c["user"], is_user_deleted=bool((self.users.get(c["user"]) or {}).get("deleted")))
            rows.append(row)
        return {"channels": rows, "response_metadata": {"next_cursor": ""}}

    def _m_conversations_info(self, channel, **_):
        c = self._chan(channel)
        return {"channel": {"id": c["id"], "name": c.get("name", ""), "is_im": c["kind"] == "im", "is_private": c["kind"] == "group"}}

    def _m_conversations_members(self, channel, **_):
        return {"members": sorted(self._chan(channel)["members"])}

    def _m_users_info(self, user, **_):
        u = self.users.get(user)
        if u is None:
            raise api_error("user_not_found")
        return {"user": u}

    def _m_users_list(self, **_):
        return {"members": list(self.users.values()), "response_metadata": {"next_cursor": ""}}

    def _m_usergroups_list(self, **_):
        return {"usergroups": [{**g, "user_count": len(g["users"])} for g in self.groups.values()]}

    def _m_auth_test(self, **_):
        return {"user_id": self.me, "user": self.users[self.me]["name"], "team": self.workspace.title(), "team_id": "T0SIM",
                "url": f"https://{self.workspace}.slack.com/"}

    def _m_reactions_get(self, channel, timestamp, **_):
        m = self._find(channel, timestamp)
        if m is None:
            raise api_error("message_not_found")
        return {"type": "message", "channel": channel, "message": dict(m)}

    def permalink(self, channel: str, ts: str) -> str:
        m = self._find(channel, ts) or {}
        url = f"https://{self.workspace}.slack.com/archives/{channel}/p{ts.replace('.', '')}"
        tts = m.get("thread_ts")
        return url + (f"?thread_ts={tts}&cid={channel}" if tts and tts != ts else "")

    def _m_chat_getPermalink(self, channel, message_ts, **_):
        if self._find(channel, message_ts) is None:
            raise api_error("message_not_found")
        return {"permalink": self.permalink(channel, message_ts)}

    def _m_chat_scheduledMessages_list(self, **_):
        return {"scheduled_messages": []}

    def _m_search_messages(self, query, count=20, page=1, sort="timestamp", sort_dir="desc", **_):
        """The slice of Slack's search grammar the engine uses: plain terms and mention tokens (all must appear),
        from:<@U>, in:#name, -in:<#C>, is:dm, after:YYYY-MM-DD. Only conversations the user is in; nothing newer
        than the index lag."""
        terms, frm, in_name, not_in, dm_only, after = [], None, None, set(), False, 0.0
        for tok in re.findall(r'"[^"]+"|\S+', query):
            if tok.startswith("from:"):
                frm = re.sub(r"[<@>]", "", tok[5:])
            elif tok.startswith("-in:"):
                not_in.add(re.sub(r"[<#>]", "", tok[4:]))
            elif tok.startswith("in:"):
                in_name = tok[3:].lstrip("#")
            elif tok == "is:dm":
                dm_only = True
            elif tok.startswith("after:"):
                after = time.mktime(time.strptime(tok[6:], "%Y-%m-%d")) + 86400
            else:
                terms.append(tok.strip('"').lower())
        hits = []
        for cid, msgs in self.messages.items():
            c = self.channels[cid]
            if self.me not in c["members"] or cid in not_in or (dm_only and c["kind"] != "im") or (in_name and c.get("name") != in_name):
                continue
            for m in msgs:
                if float(m["ts"]) > time.time() - self.search_lag_s or float(m["ts"]) < after:
                    continue  # not indexed yet, or before the date the query asks for
                if frm and m.get("user") != frm:
                    continue
                low = (m.get("text") or "").lower()
                if all(t in low for t in terms):
                    hits.append({"ts": m["ts"], "user": m.get("user"), "username": (self.users.get(m.get("user")) or {}).get("name"),
                                 "text": m.get("text"), "permalink": self.permalink(cid, m["ts"]),
                                 "channel": {"id": cid, "name": c.get("name", ""), "is_channel": c["kind"] == "channel",
                                             "is_group": c["kind"] == "group", "is_im": c["kind"] == "im", "is_mpim": False,
                                             "is_private": c["kind"] != "channel"}})
        hits.sort(key=lambda h: float(h["ts"]), reverse=sort_dir == "desc")
        count, page = int(count), int(page)
        pages = max(1, -(-len(hits) // count))
        return {"messages": {"matches": hits[(page - 1) * count:page * count], "total": len(hits),
                             "paging": {"count": count, "total": len(hits), "page": page, "pages": pages}}}

    # ---- what the guard writes ------------------------------------------------------------------------------------
    def guard_slack(self, method: str, **kw) -> dict:
        """The guard's Slack calls (dotted method names, the user's token). Reads go to the same state."""
        kw = {k: v for k, v in kw.items() if v is not None}
        if method == "chat.postMessage":
            ts = self.api_post(kw["channel"], kw["text"], kw.get("thread_ts"))
            self.outbox.append({"kind": "slack_post", "channel": kw["channel"], "thread_ts": kw.get("thread_ts"), "text": kw["text"], "ts": ts})
            return {"ok": True, "ts": ts, "channel": kw["channel"], "message": dict(self._find(kw["channel"], ts))}
        if method == "chat.update":
            if self._find(kw["channel"], kw["ts"]) is None:
                raise RuntimeError("slack chat.update: message_not_found")
            self._find(kw["channel"], kw["ts"])["text"] = kw["text"]
            self.outbox.append({"kind": "slack_edit", "channel": kw["channel"], "ts": kw["ts"], "text": kw["text"]})
            return {"ok": True, "ts": kw["ts"], "channel": kw["channel"]}
        if method == "chat.delete":
            if self._find(kw["channel"], kw["ts"]) is None:
                raise RuntimeError("slack chat.delete: message_not_found")
            self.delete(kw["channel"], kw["ts"])
            self.outbox.append({"kind": "slack_delete", "channel": kw["channel"], "ts": kw["ts"]})
            return {"ok": True}
        if method in ("pins.add", "pins.remove"):
            self.outbox.append({"kind": method, "channel": kw["channel"], "ts": kw["timestamp"]})
            return {"ok": True}
        try:
            return self.api(method.replace(".", "_"), **{"timestamp" if k == "timestamp" else k: v for k, v in kw.items()})
        except SlackApiError as e:
            raise RuntimeError(f"slack {method}: {e.response.get('error')}") from None

    def guard_gh(self, path: str, method: str = "GET", body=None):
        """The guard's GitHub REST calls for the repos of this org."""
        m = re.match(r"repos/[\w.-]+/([\w.-]+)/(.*)$", path.lstrip("/"))
        if not m:
            raise NotImplementedError(f"the simulated GitHub has no route {method} {path}")
        repo, rest = m.group(1), m.group(2)
        if rest == "pulls" and method == "POST":
            n = max([k[1] for k in self.prs if k[0] == repo] or [100]) + 1
            pr = self.pr(repo, n, self.users[self.me].get("github") or "sim-user", body["title"], body=body["body"],
                         headRefName=body["head"], baseRefName=body["base"], isDraft=bool(body.get("draft")))
            self.outbox.append({"kind": "gh_pr_create", "repo": repo, "number": n, "title": body["title"], "draft": bool(body.get("draft"))})
            return {"number": n, "html_url": pr["url"], "node_id": f"PR_{n}"}
        pm = re.match(r"pulls/(\d+)(/.*)?$", rest)
        im = re.match(r"issues/(\d+)/comments$", rest)
        if im and method == "POST":
            self.outbox.append({"kind": "gh_comment", "repo": repo, "number": int(im.group(1)), "body": body["body"]})
            return {"id": len(self.outbox)}
        if pm:
            n, sub = int(pm.group(1)), pm.group(2) or ""
            pr = self.prs.get((repo, n))
            if pr is None:
                raise RuntimeError("HTTP Error 404: Not Found")
            if sub == "" and method == "GET":
                return {"number": n, "user": {"login": pr["author"]["login"]}, "state": pr["state"].lower(), "draft": pr["isDraft"],
                        "head": {"sha": pr["headRefOid"]}, "node_id": f"PR_{n}", "title": pr["title"]}
            if sub == "" and method == "PATCH":
                pr.update({k: v for k, v in body.items() if k in ("title", "body")})
                if body.get("state") == "closed":
                    pr["state"] = "CLOSED"
                self.outbox.append({"kind": "gh_pr_edit", "repo": repo, "number": n, **body})
                return {"number": n}
            if sub.startswith("/reviews") and method == "GET":
                return list(pr.get("reviews", []))
            if sub == "/reviews" and method == "POST":
                self.outbox.append({"kind": "gh_review", "repo": repo, "number": n, "event": body.get("event", "PENDING"),
                                    "body": body.get("body", ""), "comments": body.get("comments", [])})
                return {"id": len(self.outbox), "html_url": pr["url"] + "#review"}
            if sub == "/merge" and method == "PUT":
                pr["state"] = "MERGED"
                self.outbox.append({"kind": "gh_merge", "repo": repo, "number": n, "sha": body.get("sha")})
                return {"merged": True, "sha": pr["headRefOid"]}
        cm = re.match(r"commits/(\w+)/status$", rest)
        if cm:
            return {"state": "success", "total_count": 1}
        raise NotImplementedError(f"the simulated GitHub has no route {method} {path}")
