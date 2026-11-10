"""Sent-vs-draft learning.

1. match: for a draft (an approval ask of kind slack_send), find the user's OWN message at the draft's target after
   the draft was created: his manual messages carry `client_msg_id` (API posts, including the harness's approved
   sends, never do). Candidates are the target thread, the target channel's top level and the case's source thread
   (he answers inside the existing thread instead of posting a new message with a backlink). A pure similarity
   function classifies each candidate: exact, light_edit, rewrite (same subject, his own words) or unrelated.
2. resolve: a LIVE ask he fulfilled this way is resolved as "sent by him" by approvals.poll through
   approvals._resolved_by_joaquin (the one resolution path; this module only supplies the matcher), so the case
   moves on exactly as if he had approved it.
3. sweep (every 10 min from hot.tick): drafts that are no longer live (superseded by a redraft, expired, lost)
   still teach: their (draft, sent) pair is stored as a style_pair signal, once per sent message, newest draft first.
4. daily (from learn.nightly, before style_sync): recurring edits across pairs become (a) a patterns summary that
   style_sync folds into the guide's auto-learned section and (b) deterministic lint rules in
   knowledge/voice_lint.json (phrase replacements and removals per audience) that voice_lint applies to every draft.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from collections import Counter, defaultdict

from .core import CFG, log, now

SEND_WINDOW_S = 2 * 86400  # how long after a draft his message can still be "the message he sent for it"


# ---- similarity ----------------------------------------------------------------------------------------------
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
_URL = re.compile(r"https?://[^\s<>|]+")
_TICKET = re.compile(r"\b[A-Z]{2,6}-\d{1,6}\b")


def anchors(text: str) -> set[str]:
    """What identifies the subject regardless of wording: links, ticket keys, PR numbers, mentions."""
    t = text or ""
    out = {u.rstrip("/.,)") for u in _URL.findall(t)}
    out |= {k.upper() for k in _TICKET.findall(t)}
    out |= {f"#{n}" for n in re.findall(r"/pull/(\d+)", t)} | {f"#{n}" for n in re.findall(r"(?<![\w&])#(\d{2,6})\b", t)}
    out |= set(re.findall(r"<@(U[A-Z0-9]+)", t))
    return out


def normalize(text: str) -> list[str]:
    """Word tokens with links reduced to their labels, no accents, lowercase, stretched letters collapsed
    ('buenaas' == 'buenas'), so his texture edits do not read as a different message."""
    from .voice_lint import fold
    # a link counts by its label, unless the label is the URL itself ('github.com/acme/schools/pull/919'):
    # two PR posts share 'github acme pull' without being the same message
    t = _SLACK_LINK.sub(lambda m: "" if not m.group(2) or re.search(r"\w[./]\w", m.group(2)) else m.group(2), text or "")
    t = _URL.sub("", t)
    t = re.sub(r"<[@#!][^>]*>", " ", t)
    t = fold(t)
    t = re.sub(r"(\w)\1+", r"\1", t)
    return re.findall(r"[a-z0-9]+", t)


STOP = set("que de la el en y a los las un una lo le se por para con es no si ya me te nos mi tu su al del como pero "
           "esta este eso esto hay son fue ser hoy ahi aca asi mas muy tambien porque cuando "
           # his openers and fillers, stretched letters already collapsed: they say nothing about the subject
           "buenas hola amigos team gracias fix jaja jajaj dale listo".split())


def _content(tokens: list[str]) -> set[str]:
    return {w for w in tokens if (len(w) >= 4 or w.isdigit()) and w not in STOP}


def similarity(draft: str, sent: str, strict: bool = False) -> dict:
    """{score, cls}: score is the word-sequence ratio; cls in exact / light_edit / rewrite / unrelated.
    A rewrite (his own words, same subject) needs several shared subject words; with `strict` (a busy channel's top
    level, where every PR post looks alike) it also needs a shared anchor: the same link, ticket, PR or person."""
    a, b = normalize(draft), normalize(sent)
    if not a or not b:
        return {"score": 0.0, "cls": "unrelated", "overlap": 0.0, "shared_anchors": []}
    score = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    ca, cb = _content(a), _content(b)
    common = len(ca & cb)
    overlap = common / max(1, min(len(ca), len(cb)))
    shared = sorted(anchors(draft) & anchors(sent))
    if a == b or score >= 0.93:
        cls = "exact"
    elif score >= 0.6:
        cls = "light_edit"
    elif strict and not shared:
        cls = "unrelated"
    elif (shared and overlap >= 0.15 and common >= 2) or (overlap >= 0.4 and common >= 3) or (score >= 0.35 and overlap >= 0.25 and common >= 3):
        cls = "rewrite"
    else:
        cls = "unrelated"
    return {"score": round(score, 3), "cls": cls, "overlap": round(overlap, 3), "shared_anchors": shared}


def best_match(draft: str, since: float, msgs: list[dict], self_uid: str | None = None,
               until: float | None = None, strict: bool = False) -> dict | None:
    """His manual message(s) after `since` that are the sent version of `draft`, or None. He often splits one
    thought across consecutive messages, so the run of his messages is also tried as one text."""
    me = self_uid or CFG["self_user_id"]
    mine = sorted((m for m in msgs if m.get("user") == me and m.get("client_msg_id") and float(m["ts"]) > since
                   and (until is None or float(m["ts"]) <= until) and (m.get("text") or "").strip()),
                  key=lambda m: float(m["ts"]))
    if not mine:
        return None
    cands = [([m], m["text"]) for m in mine]
    if len(mine) > 1:
        run = [mine[0]]
        for m in mine[1:]:
            if float(m["ts"]) - float(run[-1]["ts"]) <= 600:
                run.append(m)
            else:
                if len(run) > 1:
                    cands.append((list(run), "\n".join(x["text"] for x in run)))
                run = [m]
        if len(run) > 1:
            cands.append((list(run), "\n".join(x["text"] for x in run)))
    rank = {"exact": 3, "light_edit": 2, "rewrite": 1, "unrelated": 0}
    best = None
    for group, text in cands:
        s = similarity(draft, text, strict=strict)
        if s["cls"] == "unrelated":
            continue
        key = (rank[s["cls"]], s["score"], -len(group))
        if best is None or key > best["_key"]:
            best = {**s, "_key": key, "text": text, "msgs": group, "ts": group[0]["ts"]}
    if best:
        best.pop("_key")
    return best


# ---- where his message could be ----------------------------------------------------------------------------
def candidates(payload: dict, since: float, item_data: dict | None = None,
               cache: dict | None = None) -> list[tuple[str, str | None, list[dict]]]:
    """(channel, thread_ts, messages) for every place his sent version of this draft can live. `cache` (one sweep)
    reads each thread and each channel once: many drafts of one case share their targets. With a cache, channel
    history is read from cache['_oldest']; best_match filters by time anyway."""
    from . import slack
    out, seen = [], set()

    def add(ch, th):
        if not ch or (ch, th) in seen:
            return
        seen.add((ch, th))
        if cache is not None and (ch, th) in cache:
            out.append((ch, th, cache[(ch, th)]))
            return
        try:
            oldest = (cache or {}).get("_oldest", since)
            msgs = slack.replies(ch, th) if th else slack.history(ch, f"{oldest:.6f}", pages=4 if cache is not None else 2)
        except Exception as e:
            log("sentlearn: read failed", channel=ch, err=str(e)[:120])
            return
        if cache is not None:
            cache[(ch, th)] = msgs
        out.append((ch, th, msgs))
    add(payload.get("channel"), payload.get("thread_ts"))
    d = item_data or {}
    if d.get("channel") and (d.get("thread_ts") or d.get("ts")) and not d.get("dm"):
        add(d["channel"], d.get("thread_ts") or d.get("ts"))  # he answered in the source thread instead
    if not payload.get("thread_ts"):
        from .voice_lint import PERMALINK
        for m in PERMALINK.finditer(payload.get("text") or ""):  # the thread the draft linked back to
            add(m.group(1), m.group(4) or f"{m.group(2)}.{m.group(3)}")
    return out


def find_sent(payload: dict, since: float, item_data: dict | None = None, cache: dict | None = None) -> dict | None:
    best = None
    for ch, th, msgs in candidates(payload, since, item_data, cache):
        m = best_match(payload.get("text") or "", since, msgs, until=since + SEND_WINDOW_S, strict=th is None)
        if m and (best is None or (m["score"], m["cls"] != "rewrite") > (best["score"], best["cls"] != "rewrite")):
            best = {**m, "channel": ch, "thread_ts": th}
    return best


# ---- pairs -------------------------------------------------------------------------------------------------
def record_pair(item_id: int | None, draft: str, sent: str, channel: str, *, sent_ts: str | None = None,
                match: dict | None = None, artifact_id: int | None = None, via: str = "", moved_to_thread: bool = False) -> bool:
    """Store one (draft, sent) pair as a style_pair signal, once per sent message. Returns False when known."""
    from . import db
    from .voice_lint import audience
    if sent_ts and db.q1("SELECT 1 FROM signals WHERE kind='style_pair' AND json_extract(data,'$.sent_ts')=?", sent_ts):
        return False
    db.add_signal("style_pair", {"draft": draft, "sent": sent, "channel": channel, "audience": audience(channel),
                                 "sent_ts": sent_ts, "cls": (match or {}).get("cls"), "score": (match or {}).get("score"),
                                 "via": via, "moved_to_thread": moved_to_thread}, item_id, artifact_id)
    return True


def sweep(days: int = 3) -> dict:
    """Pairs from drafts that are no longer live: a redraft superseded them, the ask expired or its message was
    lost, yet he sent his own version. Newest draft first, so the pair keeps the draft closest to what he sent."""
    from . import db
    stats = {"checked": 0, "paired": 0}
    rows = db.q("SELECT * FROM artifacts WHERE kind='approval' AND outcome IS NOT NULL "
                "AND outcome NOT IN ('approved','sent_by_him','scheduled_by_him') AND created_at>? ORDER BY created_at DESC",
                now() - days * 86400)
    cache: dict = {"_oldest": now() - days * 86400}
    for a in rows:
        try:
            meta = json.loads(a["content"] or "{}")
        except Exception:
            continue
        if meta.get("kind") != "slack_send" or meta.get("pair_checked"):
            continue
        payload = meta.get("payload") or {}
        if payload.get("channel") == CFG.get("self_dm_channel") or not payload.get("text"):
            continue
        it = db.item(a["item_id"]) or {"data": {}}
        stats["checked"] += 1
        m = find_sent(payload, float(a["created_at"]), it.get("data") if it.get("kind") != "NUDGE" else None, cache)
        final = m is not None or now() - a["created_at"] > SEND_WINDOW_S
        if m and record_pair(a["item_id"], payload["text"], m["text"], m["channel"], sent_ts=m["ts"], match=m,
                             artifact_id=a["id"], via=f"sweep:{a['outcome']}", moved_to_thread=not payload.get("thread_ts") and bool(m.get("thread_ts"))):
            stats["paired"] += 1
        if final:
            meta["pair_checked"] = True
            db.x("UPDATE artifacts SET content=? WHERE id=?", json.dumps(meta, ensure_ascii=False), a["id"])
    if stats["paired"]:
        log("sentlearn sweep", **stats)
    return stats


_RUN = threading.Lock()


def tick() -> None:
    """From hot.tick: at most every 10 min, in its own thread (Slack reads never stall the scheduler)."""
    from . import db
    if now() - (db.kv_get("sentlearn_at", 0) or 0) < 600:
        return
    if any(t.name == "job-sentlearn" and t.is_alive() for t in threading.enumerate()):
        return
    db.kv_set("sentlearn_at", now())

    def run():
        try:
            sweep()
        except Exception as e:
            log("sentlearn sweep failed", err=f"{type(e).__name__}: {e}"[:200])
    threading.Thread(target=run, name="job-sentlearn", daemon=True).start()


# ---- recurring edits -> style guide + deterministic lint ----------------------------------------------------
def _span(tokens: list[str], i: int, j: int) -> str:
    return " ".join(tokens[i:j])


def _words(text: str) -> list[str]:
    """Surface words (accents kept) for phrase rules; links and mentions dropped."""
    t = _SLACK_LINK.sub(" ", text or "")
    t = _URL.sub(" ", t)
    t = re.sub(r"<[@#!][^>]*>", " ", t)
    return re.findall(r"[\wÀ-ÿ]+", t.lower())


FEATURES = {  # name -> (predicate(draft, sent) -> bool, human description)
    "drop_group_cc": (lambda d, s: "<!subteam^" in d and "<!subteam^" not in s, "sacó el cc al grupo del squad"),
    "drop_pr_links": (lambda d, s: "/pull/" in d and "/pull/" not in s, "sacó los links de PR"),
    "drop_ticket_link": (lambda d, s: "atlassian.net/browse" in d and "atlassian.net/browse" not in s, "sacó el link del ticket"),
    "real_mention": (lambda d, s: bool(re.search(r"(?<![<\w])@[A-Za-zÀ-ÿ]", d)) and "<@U" in s and not re.search(r"(?<![<\w])@[A-Za-zÀ-ÿ]", s),
                     "cambió @Nombre en texto por una mención real"),
    "add_apology": (lambda d, s: not re.search(r"perd[oó]n|disculp", d, re.I) and bool(re.search(r"perd[oó]n|disculp", s, re.I)),
                    "agregó \"perdón por la demora\""),
    # both directions, or one early add outweighs every later cut (10-21: the lint still asked for the apology he
    # had cut on 10-06, 10-14 twice and 10-15)
    "drop_apology": (lambda d, s: bool(re.search(r"perd[oó]n|disculp", d, re.I)) and not re.search(r"perd[oó]n|disculp", s, re.I),
                     "sacó \"perdón por la demora\""),
    "capitalized_greeting": (lambda d, s: bool(re.match(r"\s*(buen|hola)", d)) and bool(re.match(r"\s*(Buen|Hola)", s)),
                             "saludo con mayúscula"),
    "drop_caveats": (lambda d, s: bool(re.search(r"\b(draft|falta review|deploy|CI)\b", d, re.I)) and not re.search(r"\b(draft|falta review|deploy|CI)\b", s, re.I),
                     "sacó los caveats internos (draft, review, deploy, CI)"),
    "team_voice": (lambda d, s: bool(re.search(r"\b(encontr[eé]|av[ií]senme|arregl[eé])\b", d, re.I)) and bool(re.search(r"\b(encontramos|av[ií]sennos|arreglamos)\b", s, re.I)),
                   "pasó a voz de equipo (encontramos, avísennos)"),
    "to_question": (lambda d, s: s.count("?") > d.count("?"), "convirtió afirmaciones en preguntas"),
    "shorter": (lambda d, s: len(s) < 0.7 * len(d), "lo acortó más de un 30%"),
    "moved_to_thread": (lambda d, s: False, "respondió dentro del hilo en vez de un post nuevo"),  # set from the pair's location
}


def edit_features(pair: dict) -> list[str]:
    d, s = pair.get("draft") or "", pair.get("sent") or ""
    out = [k for k, (fn, _) in FEATURES.items() if fn(d, s)]
    if pair.get("moved_to_thread"):
        out.append("moved_to_thread")
    return out


def phrase_edits(draft: str, sent: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Short replacements (a -> b, up to 3 words each) and removals (2 to 5 words) between draft and sent."""
    a, b = _words(draft), _words(sent)
    reps, drops = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "replace" and i2 - i1 == j2 - j1:  # word for word: each changed word is its own edit
            reps += [(x, y) for x, y in zip(a[i1:i2], b[j1:j2]) if x != y]
        elif op == "replace" and i2 - i1 <= 3 and j2 - j1 <= 3:
            reps.append((_span(a, i1, i2), _span(b, j1, j2)))
        elif op == "delete" and 2 <= i2 - i1 <= 5:
            drops.append(_span(a, i1, i2))
    return reps, drops


def mine_rules(pairs: list[dict], min_support: int = 3, min_consistency: float = 0.75) -> tuple[list[dict], list[str]]:
    """(lint rules, pattern lines). A phrase rule needs `min_support` distinct pairs and must hold in at least
    `min_consistency` of the pairs of that audience whose draft contains the phrase (he does it consistently,
    not once)."""
    rep_n, drop_n, feat_n, aud_n = Counter(), Counter(), Counter(), Counter()
    has = defaultdict(int)
    for p in pairs:
        aud = p.get("audience") or "other"
        aud_n[aud] += 1
        reps, drops = phrase_edits(p.get("draft") or "", p.get("sent") or "")
        for f, t in set(reps):
            rep_n[(aud, f, t)] += 1
        for ph in set(drops):
            drop_n[(aud, ph)] += 1
        for f in edit_features(p):
            feat_n[(aud, f)] += 1
    words_by_pair = [(p.get("audience") or "other", " " + " ".join(_words(p.get("draft") or "")) + " ") for p in pairs]

    def containing(aud, phrase):
        return sum(1 for a, w in words_by_pair if a == aud and f" {phrase} " in w)
    rules = []
    for (aud, f, t), n in rep_n.items():
        if n >= min_support and len(f) >= 4 and n / max(1, containing(aud, f)) >= min_consistency:
            rules.append({"audience": aud, "kind": "replace", "from": f, "to": t, "support": n})
    for (aud, ph), n in drop_n.items():
        if n >= min_support and len(ph) >= 6 and n / max(1, containing(aud, ph)) >= min_consistency:
            rules.append({"audience": aud, "kind": "drop", "phrase": ph, "support": n})
    patterns = [f"en {aud}: {FEATURES[f][1]} ({n} de {aud_n[aud]} mensajes)" for (aud, f), n in feat_n.most_common()
                if n >= min_support and n / aud_n[aud] >= 0.5]
    return sorted(rules, key=lambda r: -r["support"]), patterns


def daily(days: int = 21) -> dict:
    """Recurring edits of the last `days` of pairs -> knowledge/voice_lint.json (rules + patterns)."""
    from . import db
    from .voice_lint import LEARNED_FILE
    pairs = []
    for r in db.q("SELECT data FROM signals WHERE kind='style_pair' AND at>?", now() - days * 86400):
        p = json.loads(r["data"] or "{}")
        if p.get("draft") and p.get("sent"):
            if not p.get("audience"):
                from .voice_lint import audience
                p["audience"] = audience(p.get("channel"))
            pairs.append(p)
    rules, patterns = mine_rules(pairs)
    LEARNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_FILE.write_text(json.dumps({"at": now(), "pairs": len(pairs), "rules": rules, "patterns": patterns},
                                       ensure_ascii=False, indent=1) + "\n")
    log("sentlearn daily", pairs=len(pairs), rules=len(rules), patterns=len(patterns))
    return {"pairs": len(pairs), "rules": len(rules), "patterns": patterns}


def patterns_text() -> str:
    from .voice_lint import LEARNED_FILE
    try:
        p = json.loads(LEARNED_FILE.read_text()).get("patterns") or []
    except Exception:
        return ""
    return "\n".join(f"- {x}" for x in p)
