"""The product's voice: one warm, persistent fast-model session that answers the user in real time (1-3 s), briefly and
in a professional register. Deep work happens elsewhere (orchestrator, cases, supervisor); the voice acknowledges instantly and
later delivers what the backend found, in the same short register. Like a voice agent: a fast talker in front, a
thinker behind it.

Mechanics: `claude -p --input-format stream-json` kept alive (no process start per message), recycled every 40 turns
or 30 minutes with a pre-warmed spare, restarted on any error. Falls back to a one-shot call if the stream fails."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from . import site
from .core import CFG, PROMPTS, log

SYSTEM = (f"You are the voice of {site.first_name()}'s autonomous engineering assistant, in his Slack DM. You speak TO him, as his "
          "assistant: professional, clear and direct, in complete sentences with normal capitalization and punctuation, "
          + site.own("in Rioplatense Spanish when he writes in Spanish", f"in {site.language_name()}")
          + ". Brief (1-3 lines), no preamble, no lists unless asked, no slang, no jokes, no emojis, no em dashes. Never imitate how "
          "he writes: his own style is for the messages drafted in his name, not for what you say to him. Never invent "
          "facts: only use what each message gives you. "
          "When you are told to decide if deeper work is needed, end your message with [DEEP] if it is.")
MAX_TURNS, MAX_AGE = 40, 1800
def _system_file(lean: bool = False) -> str:
    """The voice's register is fixed and its own (the user, 2026-11-10: what the assistant says to him is not informal;
    his style is for what goes out in his name). It used to be modelled on his hand-written messages and his style
    guide, so it texted him the way he texts his team."""
    from .core import VAR
    text = (SYSTEM + "\n\n## Register\nA competent assistant reporting to the person it works for. Plain words, no ceremony "
            "and no chattiness: neither a customer-service bot ("
            + site.own("no '¿En qué te puedo ayudar?', no '¡Ey, qué onda!', ", "")
            + "no exclamation-heavy openers, no offers of further help) nor a friend texting (no slang, no lowercase-only "
            "lines, no letter-doubling, no filler like 'ok so' or "
            + site.own("'dale', 'joya', 'un toque'", "'cool', 'yep', 'gonna'") + "). An acknowledgement is one short, complete sentence.")
    f = VAR / ("voice-ack.md" if lean else "voice-system.md")
    f.write_text(text)
    return str(f)


def _streams() -> bool:
    """Claude Code keeps a warm streaming session; another engine (Codex) answers each turn in one fast call."""
    from . import providers
    return providers.engine() == "claude"


def _oneshot(text: str, lean: bool) -> str:
    from pathlib import Path
    from . import claude
    from .core import VAR
    out = claude.lean("chat_fast", Path(_system_file(lean)).read_text() + "\n\n" + text, model="haiku", cwd=str(VAR),
                      tools=(), timeout=60)
    return (out.get("text") or "").strip()


class _Session:
    def __init__(self, lean: bool = False):
        from . import providers
        env = {**os.environ, "MAX_THINKING_TOKENS": "0"}  # an acknowledgement needs no thinking (it cost ~8 s a turn)
        for k, v in providers.claude_env().items():  # only the selected provider's credential reaches the process
            env.pop(k, None) if v is None else env.__setitem__(k, v)
        self.p = subprocess.Popen(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--model", CFG["claude"].get("haiku", "claude-haiku-4-5-20251001"), "--tools", "", "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--setting-sources", "",
             "--no-session-persistence", "--system-prompt-file", _system_file(lean)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
            env=env)
        self.turns, self.born = 0, time.time()

    def ask(self, text: str, timeout: float = 45) -> str:
        self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.p.stdin.flush()
        self.turns += 1
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("voice session ended")
            e = json.loads(line)
            if e.get("type") == "result":
                if e.get("is_error"):
                    raise RuntimeError(str(e.get("result"))[:200])
                return (e.get("result") or "").strip()
        raise TimeoutError("voice reply timeout")

    def stale(self) -> bool:
        return self.turns >= MAX_TURNS or time.time() - self.born > MAX_AGE or self.p.poll() is not None

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


import sys as _sys
import types as _types
_V = _sys.modules.setdefault("ap._voice_state", _types.SimpleNamespace(lock=threading.Lock(), cur=None, spare=None))
if not hasattr(_V, "ack"):
    _V.ack, _V.ack_lock = None, threading.Lock()
_lock = _V.lock


def _warm_spare():
    try:
        _V.spare = _Session()
    except Exception as e:
        log("voice: spare failed", err=str(e)[:100])


def state_delta(state: str) -> str:
    """The state text if the current session has not seen this exact version yet, else ''."""
    import hashlib
    h = hashlib.sha1(state.encode()).hexdigest()
    cur = _V.cur
    if cur is not None and getattr(cur, "state_hash", None) == h and not cur.stale():
        return ""
    if cur is not None:
        cur.state_hash = h
    return state


def say(text: str) -> str:
    """One reply in the fast voice. Thread-safe; recycles and warms sessions transparently."""
    if not _streams():
        return _oneshot(text, False)
    with _lock:
        if _V.cur is None or _V.cur.stale():
            if _V.cur:
                _V.cur.close()
            _V.cur, _V.spare = (_V.spare, None) if _V.spare and _V.spare.p.poll() is None else (_Session(), None)
            _V.cur.state_hash = None
            threading.Thread(target=_warm_spare, daemon=True).start()
        try:
            return _V.cur.ask(text)
        except Exception as e:
            log("voice: session error, restarting", err=str(e)[:120])
            _V.cur.close()
            _V.cur = _Session()
            _V.cur.state_hash = None
            return _V.cur.ask(text)


def ack(text: str) -> str:
    """The instant acknowledgement: a lean warm session (base voice + his real messages, no full guide)."""
    if not _streams():
        return _oneshot(text, True)
    with _V.ack_lock:
        if _V.ack is None or _V.ack.stale():
            if _V.ack:
                _V.ack.close()
            _V.ack = _Session(lean=True)
        try:
            return _V.ack.ask(text, timeout=15)
        except Exception as e:
            log("voice: ack session error, restarting", err=str(e)[:120])
            _V.ack.close()
            _V.ack = _Session(lean=True)
            return _V.ack.ask(text, timeout=15)


def keep_warm() -> None:
    """Both sessions exist and have answered once (cold start happens here, never on his message)."""
    if not _streams():
        return
    with _V.ack_lock:
        if _V.ack is None or _V.ack.stale():
            if _V.ack:
                _V.ack.close()
            _V.ack = _Session(lean=True)
            _V.ack.ask("(warm-up, reply with ok)", timeout=60)
    with _lock:
        if _V.cur is None or _V.cur.stale():
            if _V.cur:
                _V.cur.close()
            _V.cur = _Session()
            _V.cur.state_hash = None
            _V.cur.ask("(warm-up, reply with ok)", timeout=90)
