"""Slack I/O. Reads with the user's user token (slack_sdk); every outgoing text goes
through v1's bin/slack.sh so the style gate, footer stripping, dedupe guard and
length splitting stay the single enforcement point."""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import threading
import time
from functools import lru_cache

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import site
from .core import CFG, V1, log, run

SLACK_SH = str(V1 / "bin/slack.sh")  # the first install's style gate; where it is absent texts go as written


_TOK: dict = {}


def token() -> str:
    """The user token, read from the keychain once per process."""
    import os
    if not _TOK.get("v"):
        if os.environ.get("AP_SLACK_TOKEN"):
            _TOK["v"] = os.environ["AP_SLACK_TOKEN"]
        elif site.internal():
            _TOK["v"] = subprocess.run(["security", "find-generic-password", "-s", site.keychain("slack"), "-w"],
                                       capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        else:  # the token the user pasted in the onboarding (ap/setup.slack_connect)
            from . import vault
            _TOK["v"] = vault.get("slack-user-token")
            if not _TOK["v"]:
                raise RuntimeError("Slack is not connected: connect it in Clea's settings")
    return _TOK["v"]


@lru_cache(maxsize=1)
def client() -> WebClient:
    return WebClient(token=token(), timeout=30)


def keychain(service: str) -> str:
    import os
    env_name = "AP_" + service.upper().replace("-", "_")
    if os.environ.get(env_name):
        return os.environ[env_name]
    p = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


import collections as _collections
import sys as _sys
import types as _types
# one limiter per process, surviving hot reloads: Slack's read tiers are shared by every thread
_RL = _sys.modules.setdefault("ap._slack_rl", _types.SimpleNamespace(lock=threading.Lock()))
_RL.__dict__.setdefault("bg", {})  # method -> times of the background calls in the last minute
# Tier 3 is ~50/min per method. Background threads get 25/min of each; the chat is never paced (if both together
# overrun the tier, Slack's 429 is logged and retried in _call: visible, never a silent stall).
RATE = {"conversations_replies": 24, "conversations_history": 24}  # the chat polls history ~25/min on top; 45 overran the tier (429s on 2026-10-01 10:24)
# 2026-10-01: at 25/min the background demand (ingest, board, approvals, replies, dm, triage, sentlearn) never fit:
# ~30 threads queued in _pace, reaped jobs restarted and queued again, and Slack intake, the board and the digest
# stalled overnight. Three changes: the same read from several jobs within CACHE_S is served once (they poll the same
# DM threads), the share is near the real tier, and a real 429 pauses every background reader together.
_RL.__dict__.setdefault("cache", {})
_RL.__dict__.setdefault("until", 0.0)
CACHE_S = 20
CACHED = ("conversations_replies", "conversations_history")


def _pace(method: str) -> None:
    """Background threads stay under their own share of each method; the chat (the user waiting) never waits
 and never spends the background's share."""
    cap = RATE.get(method)
    name = threading.current_thread().name
    # only the live conversation skips pacing; the background thread scan (chat-scan) is paced like any job
    #
    if not cap or (name.startswith("chat") and name != "chat-scan"):
        return
    while time.time() < getattr(_RL, "until", 0.0):  # Slack said slow down: every background reader waits it out together
        time.sleep(min(5, max(0.2, getattr(_RL, "until", 0.0) - time.time())))
    # his ✅ is the read he waits on: the approval poll has its own small share and never queues behind the background
    # jobs
    prio = threading.current_thread().name == "job-approvals"
    bucket, cap = (method + ":approvals", 12) if prio else (method, cap)
    while True:
        with _RL.lock:
            calls = _RL.bg.setdefault(bucket, _collections.deque())
            cutoff = time.time() - 60
            while calls and calls[0] < cutoff:
                calls.popleft()
            if len(calls) < cap:
                calls.append(time.time())
                return
            wait = calls[0] + 60 - time.time()
        time.sleep(max(0.2, min(wait, 5)))


def _call(method: str, **kw) -> dict:
    chat = threading.current_thread().name.startswith("chat")
    key = None
    if method in CACHED and not chat:
        key = (method, tuple(sorted((k, str(v)) for k, v in kw.items())))
        with _RL.lock:
            hit = _RL.__dict__.setdefault("cache", {}).get(key)
            if hit and time.time() - hit[0] < CACHE_S:
                return hit[1]
    _pace(method)
    for attempt in range(4):
        try:
            data = getattr(client(), method)(**kw).data
            if key:
                with _RL.lock:
                    _RL.__dict__.setdefault("cache", {})
                    if len(_RL.cache) > 2000:
                        _RL.cache = {k: v for k, v in _RL.cache.items() if time.time() - v[0] < CACHE_S}
                    _RL.cache[key] = (time.time(), data)
            return data
        except SlackApiError as e:
            if e.response.status_code == 429:
                wait = int(e.response.headers.get("Retry-After", 5)) + 1
                log("slack rate limited", method=method, wait=wait, thread=threading.current_thread().name)
                if not chat:
                    _RL.until = max(getattr(_RL, "until", 0.0), time.time() + wait)
                time.sleep(wait)
                continue
            raise
        except http.client.IncompleteRead:
            # a read cut mid-body (26 on 10-06 alone, 850 KB DM pages): every method here is a read, so read again.
            # Callers swallowed it as 'unavailable' and vet judged #526 and #533 without their conversation
            if attempt == 3:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError(f"slack {method}: rate limited")


def history(channel: str, oldest: str, pages: int = 5) -> list[dict]:
    out, cursor = [], None
    for _ in range(pages):
        d = _call("conversations_history", channel=channel, oldest=oldest, limit=200, cursor=cursor)
        out += d.get("messages", [])
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return sorted(out, key=lambda m: float(m["ts"]))


def latest_ts(channel: str) -> str | None:
    """The newest message ts in a channel (roots and broadcasts), or None when it has none."""
    msgs = _call("conversations_history", channel=channel, limit=1).get("messages") or []
    return msgs[0]["ts"] if msgs else None


def replies(channel: str, thread_ts: str, oldest: str | None = None, pages: int = 5) -> list[dict]:
    """The whole thread (root first), following the cursor: a long thread's newest replies are on page 2+."""
    kw = {"channel": channel, "ts": thread_ts, "limit": 200}
    if oldest:
        kw["oldest"] = oldest
    out, cursor = [], None
    for _ in range(pages):
        d = _call("conversations_replies", **kw, **({"cursor": cursor} if cursor else {}))
        out += d.get("messages", [])
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor or not d.get("has_more", True):
            break
    return out


def _block_texts(blocks, rich_text: bool = True) -> list[str]:
    """Text carried by Block Kit blocks: section/header/context text and fields, rich text runs (rich_text=False
    skips those: for a typed message they are the same words as its text, formatted differently)."""
    out = []

    def rich(el) -> str:
        if isinstance(el, list):
            return "".join(rich(e) for e in el)
        if not isinstance(el, dict):
            return ""
        t = el.get("type")
        if t == "text":
            return el.get("text") or ""
        if t == "link":
            return el.get("text") or el.get("url") or ""
        if t == "user":
            return f"<@{el.get('user_id')}>"
        if t == "usergroup":
            return f"<!subteam^{el.get('usergroup_id')}>"
        if t == "emoji":
            return f":{el.get('name')}:"
        if t == "rich_text_list":
            return "\n".join("• " + rich(e) for e in el.get("elements") or [])
        return rich(el.get("elements") or [])

    for b in blocks or []:
        t = b.get("type")
        if t in ("section", "header"):
            if (b.get("text") or {}).get("text"):
                out.append(b["text"]["text"])
            out += [f.get("text") for f in b.get("fields") or [] if f.get("text")]
        elif t == "context":
            out += [e.get("text") for e in b.get("elements") or [] if isinstance(e, dict) and e.get("text")]
        elif t == "rich_text" and rich_text:
            r = rich(b.get("elements") or [])
            if r.strip():
                out.append(r)
    return out


def message_text(m: dict) -> str:
    """Everything a human would read in a message: its text, then attachment pretext/title/text/fields and block
    text not already in it."""
    parts = [m.get("text") or ""]

    def add(piece) -> None:
        piece = (piece or "").strip()
        if piece and not any(piece in p for p in parts):
            parts.append(piece)

    for t in _block_texts(m.get("blocks"), rich_text=not (m.get("text") or "").strip()):
        add(t)
    for a in m.get("attachments") or []:
        before = len(parts)
        for k in ("pretext", "title", "text"):
            add(a.get(k))
        for f in a.get("fields") or []:
            if f.get("title") or f.get("value"):
                add(f"{f.get('title') or ''}: {f.get('value') or ''}".strip(": "))
        for t in _block_texts(a.get("blocks")):
            add(t)
        if len(parts) == before:
            add(a.get("fallback"))
    return "\n".join(p for p in parts if p.strip()).strip()


def attachments_note(m: dict) -> str:
    """Attachment metadata for a model: names and types of files, and inline images (a gif is a reaction)."""
    file, image = site.own("archivo", "file"), site.own("imagen", "image")
    bits = [f"{f.get('name') or f.get('title') or file} ({f.get('filetype') or f.get('mimetype') or '?'})"
            for f in (m.get("files") or [])[:6]]
    imgs = [b.get("image_url") for b in (m.get("blocks") or []) if b.get("type") == "image" and b.get("image_url")]
    bits += [f"{image} inline ({'gif' if str(u).lower().split('?')[0].endswith('.gif') else image})" for u in imgs[:3]]
    return f" [{site.own('adjuntos', 'attachments')}: {', '.join(bits)}]" if bits else ""


def thread_text(channel: str, thread_ts: str, max_chars: int = 24000, per_msg: int = 1200) -> str:
    """The WHOLE thread for a model, oldest first, with attachment metadata. Over `max_chars` the root stays and the newest messages fill the rest; the gap says
    how many messages it skipped, so nothing is silently dropped."""
    import datetime as _dt
    msgs = replies(channel, thread_ts)
    lines = [f"[{_dt.datetime.fromtimestamp(float(m['ts'])):%d/%m %H:%M}] "
             f"{user_name(m.get('user')) if m.get('user') else 'bot'}: {render(message_text(m))[:per_msg]}{attachments_note(m)}"
             for m in msgs]
    if sum(len(l) + 1 for l in lines) <= max_chars or len(lines) < 2:
        return "\n".join(lines)
    head, tail, used = lines[0], [], len(lines[0]) + 80
    for l in reversed(lines[1:]):
        if used + len(l) + 1 > max_chars:
            break
        tail.insert(0, l)
        used += len(l) + 1
    skipped = len(lines) - 1 - len(tail)
    return "\n".join([head, site.own(f"(... {skipped} mensajes intermedios omitidos por largo ...)",
                                      f"(... {skipped} messages in between left out for length ...)")] + tail)


def ims() -> list[dict]:
    return _call("conversations_list", types="im", limit=200, exclude_archived=True).get("channels", [])


def search(query: str, count: int = 50) -> list[dict]:
    d = _call("search_messages", query=query, sort="timestamp", sort_dir="desc", count=count)
    return (d.get("messages") or {}).get("matches", [])


@lru_cache(maxsize=512)
def user_name(uid: str) -> str:
    if not uid:
        return "?"
    try:
        u = _call("users_info", user=uid)["user"]
        return u.get("real_name") or u.get("name") or uid
    except Exception:
        return uid


def permalink(channel: str, ts: str) -> str:
    try:
        return _call("chat_getPermalink", channel=channel, message_ts=ts)["permalink"]
    except Exception:
        return site.slack_link(channel, ts)


def thread_ts_from_permalink(url: str) -> str | None:
    m = re.search(r"thread_ts=([0-9.]+)", url or "")
    return m.group(1) if m else None


def _subteam_name(sid: str, label: str | None) -> str:
    if sid in CFG.get("team_subteam_tags", []):
        return site.team_tag_label()
    return "@" + label.lstrip("@") if label else f"@grupo-{sid}"


@lru_cache(maxsize=256)
def channel_name(ch: str) -> str:
    known = CFG.get("channel_names") or {}
    if ch in known:
        return known[ch]
    if not ch:  # an install with no such channel configured: nothing to look up
        return ""
    if (ch or "").startswith("D"):
        return "DM"
    try:
        c = _call("conversations_info", channel=ch)["channel"]
        return "#" + c["name"] if c.get("name") else ch
    except Exception:
        from . import db
        r = db.q1("SELECT json_extract(payload,'$.channel_name') n FROM events WHERE channel=? AND json_extract(payload,'$.channel_name') IS NOT NULL LIMIT 1", ch)
        return f"#{r['n']}" if r and r["n"] else ch


@lru_cache(maxsize=512)
def user_is_bot(uid: str) -> bool:
    if not uid:
        return False
    try:
        u = _call("users_info", user=uid)["user"]
        return bool(u.get("is_bot") or u.get("is_app_user") or (u.get("profile") or {}).get("api_app_id"))
    except Exception:
        return False


def render(text: str) -> str:
    """Resolve <@U..> mentions and <!subteam^S..> group tags to names so models read real
    people and groups, not ids."""
    text = re.sub(r"<!subteam\^(S[A-Z0-9]+)(?:\|([^>]*))?>", lambda m: _subteam_name(m.group(1), m.group(2)), text or "")
    text = re.sub(r"<@(S[A-Z0-9]+)>", lambda m: _subteam_name(m.group(1), None), text)
    return re.sub(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + user_name(m.group(1)), text)


def is_human(m: dict) -> bool:
    return not m.get("bot_id") and m.get("subtype") in (None, "thread_broadcast")


# ---- outbound (always via slack.sh) ---------------------------------------
def _sh(*args: str) -> dict:
    """Read-only uses of slack.sh only (style gate). Writes go through the guard."""
    if args and args[0] in ("send", "edit", "del"):
        raise RuntimeError("slack.sh writes are not allowed: use the outbound guard")
    p = run([SLACK_SH, *args], timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"slack.sh {args[0]} rc={p.returncode}: {p.stderr.strip()[:400]}")
    return json.loads(p.stdout)


def _compose(text: str, topic: str) -> None:
    """slack.sh refuses >1200-char texts without a compose file (v1 provenance gate).
    v2 texts are assembled by code or composed by the assigned tier, so record them."""
    if len(text) > 1200 and os.path.exists(SLACK_SH):
        d = V1 / "state/compose"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"v2-{topic}-{int(time.time())}.txt").write_text(text)


MAXLEN = 3500  # slack.sh's margin under Slack's ~4000 ceiling (Slack post silently splits past it)


def _gate(text: str, channel: str) -> str:
    """The local style/provenance gate (slack.sh check): fast, no network. Blocked text raises."""
    return check_text(text, channel)


def _sendable(text: str, channel: str, thread_ts: str | None) -> bool:
    """Double-fire guard: a byte-identical text to the same place within 30 min is not resent."""
    from . import db
    key = _sent_key(text, channel, thread_ts)
    prev = db.kv_get(key) or {}
    prev = prev if isinstance(prev, dict) else {"at": prev}  # marks written before they carried the message's ts
    if prev.get("at", 0) > time.time() - 1800:
        # the same text twice in half an hour is a double fire only while the first copy is still there. A case that
        # asks again in the same words after its earlier ask was retired and deleted is not one: refused, its stage
        # failed three times and the case blocked on a message nobody could see
        if not prev.get("ts") or fetch_message(channel, prev["ts"]) is not None:
            return False
    db.kv_set(key, {"at": int(time.time())})
    return True


def _sent_key(text: str, channel: str, thread_ts: str | None) -> str:
    import hashlib
    return f"sent:{channel}:{thread_ts or 'root'}:{hashlib.sha1(text.encode()).hexdigest()[:16]}"


def _sent_at(text: str, channel: str, thread_ts: str | None, ts: str | None) -> None:
    """Remember which message carries this text, so the guard can tell a copy that is still there from one that is gone."""
    from . import db
    if ts:
        db.kv_set(_sent_key(text, channel, thread_ts), {"at": int(time.time()), "ts": ts})


def send_self(text: str, thread_ts: str | None = None, topic: str = "note", guard: bool = True) -> dict:
    """Post in the self-DM through the Slack client (retries, 30 s timeout), never the shell script
. Long texts become a root
    message plus thread replies, like slack.sh did."""
    _compose(text, topic)
    ch = CFG["self_dm_channel"]
    text = gated = _gate(text, ch)
    if guard and not _sendable(text, ch, thread_ts):
        raise RuntimeError("slack send: byte-identical message already sent to this channel in the last 30 min (double-fire guard)")
    from . import comms
    from .guardcall import act
    text = comms.label_links(text)  # never a raw URL in his DM (ap/comms.py)
    comms.check(text, topic)
    tail = ""
    if not thread_ts:  # the DM shows a short message; the rest waits in its thread
        text, tail = comms.top_level(text)
    parts = _split(text)
    first = act("slack_post_self", text=parts[0], thread_ts=thread_ts)
    if guard:
        _sent_at(gated, ch, thread_ts, first.get("ts"))
    root = first["ts"] if not thread_ts else thread_ts
    posted = [first.get("ts")]
    for extra in parts[1:] + (_split(tail) if tail else []):
        posted.append(act("slack_post_self", text=extra, thread_ts=root).get("ts"))
    _own_posts(posted)
    return first


def _own_posts(ts_list: list) -> None:
    """The harness posts as him: its own thread replies must not look like his new messages to the chat scan."""
    from . import db
    own = (db.kv_get("own_posted_ts") or [])[-400:]
    db.kv_set("own_posted_ts", own + [t for t in ts_list if t])


def is_own_post(ts: str) -> bool:
    from . import db
    return ts in set(db.kv_get("own_posted_ts") or [])


def edit_self(ts: str, text: str, topic: str = "board") -> dict:
    _compose(text, topic)
    ch = CFG["self_dm_channel"]
    text = _gate(text, ch)
    from . import comms
    labeled = comms.label_links(text)
    text = labeled if len(labeled) <= MAXLEN else text  # an edit cannot grow past the limit; labels are the first to give way
    comms.check(text, topic)
    if len(text) > MAXLEN:
        raise RuntimeError(f"slack edit: text is {len(text)} chars (limit {MAXLEN}); an edit cannot grow a thread")
    from .guardcall import act
    return act("slack_edit_self", ts=ts, text=text)


def delete_self(ts: str) -> bool:
    ch = CFG["self_dm_channel"]
    from .guardcall import act
    try:
        act("slack_delete_self", ts=ts)
        time.sleep(0.4)  # Slack delete is tier-3 rate limited
        return True
    except RuntimeError as e:
        if "message_not_found" in str(e) or "cant_delete_message" in str(e):
            return False
        raise


def pin_self(ts: str, pin: bool = True) -> None:
    from .guardcall import act
    act("slack_pin_self", ts=ts, pin=pin)


def _split(text: str, n: int = MAXLEN) -> list[str]:
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def check_text(text: str, channel: str) -> str:
    """Run the style gate without sending; returns the sanitized text or raises."""
    if channel != CFG["self_dm_channel"] and site.ASSISTANT.search(text or ""):
        # Clea is the agent's name for the user only; a teammate reads the user, never his agent
        raise ValueError(site.t(f"the text for the team mentions Clea: it is written in {site.first_name()}'s voice, without naming the agent",
                                f"el texto para el equipo menciona a Clea: se escribe con la voz de {site.first_name()}, sin nombrar al agente"))
    if not os.path.exists(SLACK_SH):
        return text
    p = run([SLACK_SH, "check", text, channel], timeout=30)
    if p.returncode != 0:
        raise ValueError(p.stderr.strip()[:400])
    return p.stdout


def fetch_message(channel: str, ts: str) -> dict | None:
    """The raw message at `ts` (history with latest inclusive); replies need their root."""
    try:
        msgs = _call("conversations_history", channel=channel, latest=ts, inclusive=True, limit=1).get("messages") or []
        if msgs and msgs[0]["ts"] == ts:
            return msgs[0]
        root = _call("conversations_replies", channel=channel, ts=ts, limit=1).get("messages") or []
        return root[0] if root and root[0]["ts"] == ts else None
    except Exception as e:
        log("slack: fetch_message failed", channel=channel, ts=ts, err=str(e)[:120])
        return None


def in_second(channel: str, ts: str, root: str | None = None) -> list[dict]:
    """Messages posted in the same second as `ts`: replies of `root` when given, channel roots otherwise."""
    sec = ts.split(".")[0]
    try:
        pool = (replies(channel, root) if root and root != ts else
                _call("conversations_history", channel=channel, oldest=sec, latest=f"{int(sec) + 1}", inclusive=True,
                      limit=20).get("messages") or [])
    except Exception as e:
        log("slack: in_second failed", channel=channel, ts=ts, err=str(e)[:120])
        return []
    return [m for m in pool if m["ts"].split(".")[0] == sec]


def resolve_root(channel: str, ts: str) -> str:
    """The thread root for a message ts: itself when it is not a reply. A reply ts used as a
    root returns a one-message 'thread'."""
    m = fetch_message(channel, ts)
    return (m or {}).get("thread_ts") or ts


def _sign_in_page(path: "Path", ext: str) -> bool:
    """Slack answers a file URL the token may not read with its sign-in page, status 200 and all."""
    with open(path, "rb") as fh:
        head = fh.read(20000)
    return (ext != "html" and head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))) or b"<title>Slack</title>" in head


def download_files(msgs: list[dict], dest: "Path", limit: int = 6, missed: list | None = None) -> list[str]:
    """Attached files (images, PDFs) of the given messages into dest/, converted so the Read
    tool can open them (gif -> png via sips). Returns local paths; a file Slack would not hand over goes to missed
    instead (id, name, type, message ts: bridge.read_files reads it through the connector), and he is told (once a week while it lasts) that the token cannot read them. Every download since 09-23 saved
    the sign-in page (the token has no files:read): 162 'images' were HTML, and on 10-05 the vet of #441 spent
    four Reads to find it could not see a teammate's screenshot answer, with nothing telling it or him why."""
    from pathlib import Path as _P
    dest = _P(dest)
    dest.mkdir(parents=True, exist_ok=True)
    tok = client().token
    out = []
    for m in msgs:
        for f in m.get("files") or []:
            if len(out) >= limit:
                return out
            url = f.get("url_private_download") or f.get("url_private")
            if not url or (f.get("size") or 0) > 15_000_000:
                continue
            ext = (f.get("filetype") or "bin").lower()
            path = dest / f"{m['ts'].replace('.', '_')}-{f.get('id', 'f')}.{ext}"
            if not path.exists():
                r = run(["curl", "-sSL", "--max-time", "60", "-H", f"Authorization: Bearer {tok}", "-o", str(path), url], timeout=90)
                if r.returncode != 0 or not path.exists():
                    continue
            if _sign_in_page(path, ext):
                path.unlink()  # never cached: once the token can read files the next look downloads it
                if missed is not None:
                    missed.append({"id": f.get("id"), "name": f.get("name") or path.name, "type": ext, "ts": m["ts"]})
                _tell_files_unreadable()
                continue
            if ext == "gif":
                png = path.with_suffix(".png")
                if not png.exists():
                    run(["sips", "-s", "format", "png", str(path), "--out", str(png)], timeout=60)
                if png.exists():
                    path = png
            out.append(str(path))
    return out


def _tell_files_unreadable() -> None:
    from . import db
    if time.time() - (db.kv_get("slack_files_told") or 0) < 7 * 86400:  # once a week while it lasts
        return
    db.kv_set("slack_files_told", time.time())
    log("slack: attachments unreadable, the token has no files:read")
    try:
        send_self(site.t("⚠️ I cannot open Slack attachments (images, PDFs, spreadsheets): Clea's token does not have the "
                         "files:read scope, so Slack returns its login page. Cases say so when an attachment could not be read. "
                         "To fix it: add files:read to the Slack app, reinstall it and update the token in the "
                         f"keychain ({site.keychain('slack')}).",
                         "⚠️ No puedo abrir los adjuntos de Slack (imágenes, PDFs, planillas): el token de Clea no tiene "
                         "el scope files:read y Slack devuelve la página de login. Los casos lo dicen cuando un adjunto no se "
                         "pudo leer. Para arreglarlo: sumá files:read a la app de Slack, reinstalala y actualizá el token en el "
                         f"keychain ({site.keychain('slack')})."), topic="files")
    except Exception as e:
        log("slack: files notice failed", err=str(e)[:160])
