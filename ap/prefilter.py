"""Cheap, deterministic triage prefilter: what never needs a model.

Why: 804 triage calls in one week, a gif treated as an ask, drafts written over a one-word ack.
Before any model sees an event, drop: gifs/stickers, emoji-only messages, one-word acks and laughter, bare
greetings, bot noise and system subtypes, and the user's own messages (except the ones that state a commitment,
which the triage turns into OPEN_LOOP tracking). A question mark with real words always passes.

Counters per day in kv `prefilter:<date>`: prefiltered (by reason), fast_lane (aimed, straight to the reply lane)
and model_triaged (sent to the triage model). `apctl status` prints them.
"""
from __future__ import annotations

import re
import unicodedata

from . import site

GIF_HOSTS = re.compile(r"https?://(?:\S+\.)?(?:giphy\.com|tenor\.com|gfycat\.com|imgur\.com/\S+\.gifv?)\S*"
                       r"|https?://\S+\.gifv?(?:\?\S*)?", re.I)
SLACK_TOKEN = re.compile(r"<[@#!][^>]*>")                    # <@U..>, <#C..|name>, <!subteam^..>, <!here>
SLACK_LINK = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>")  # <url|label> -> url
EMOJI_CODE = re.compile(r":[a-z0-9_+\-']+:(?::skin-tone-\d:)?", re.I)
EMOJI_CHARS = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️⬀-⯿⌀-⏿]")
LAUGH = re.compile(r"^(?:(?:j+[aeiou]+)+j*|(?:h+[aeiou]+)+h*|x+d+|l+o+l+|ja+|je+)$")
# compared after accent stripping and collapsing repeated letters ("graciass" -> "gracias", "okk" -> "ok")
ACK = {"ok", "oka", "okey", "okay", "okis", "oki", "k", "dale", "va", "vale", "listo", "lista", "listos", "genial",
       "perfecto", "perfecta", "perfect", "joya", "buenisimo", "buenisima", "excelente", "gracias", "thanks", "thank",
       "you", "thx", "ty", "muchas", "muchisimas", "mil", "de", "nada", "sip", "si", "yes", "yep", "claro", "entendido",
       "entendi", "copiado", "anotado", "bien", "super", "top", "great", "nice", "cool", "sale", "ya", "vi", "visto",
       "seen", "done", "hecho", "igualmente", "acuerdo", "crack", "capo", "genio", "grande", "amigo", "amigos",
       "team", "equipo", "chicos", "gente", "todos"}
GREET = {"hola", "holi", "holis", "buenas", "buen", "buenos", "dia", "dias", "tardes", "noches", "hi", "hello", "hey",
         "que", "tal", "como", "estas", "andas", "va", "todo", "morning", "gm"} | set(site.aliases())
# English goes in as whole phrases: "got it" and "no worries" are acks, "do it" and "no good" are not, so their words
# cannot join the sets above (a release install wrote a draft over a teammate's "got it")
ACK_PHRASES = ("got it", "gotcha", "sounds good", "sounds great", "sounds like a plan", "will do", "no worries",
               "no problem", "no prob", "np", "makes sense", "noted", "understood", "awesome", "amazing", "sure",
               "sure thing", "for sure", "of course", "thanks a lot", "thanks so much", "thank you so much",
               "thank you very much", "thanks a ton", "many thanks", "much appreciated", "appreciate it", "appreciated",
               "all good", "looks good", "looks great", "lgtm", "works for me", "that works", "good to know", "roger",
               "roger that", "ack", "cheers", "alright", "all right", "yup", "yeah", "yea", "good", "very good",
               "good job", "good one", "nice one", "nice work", "great work", "well done", "love it", "you too",
               "likewise", "my pleasure", "you re welcome", "youre welcome", "welcome", "fair enough", "agreed",
               "exactly", "absolutely", "definitely", "good stuff", "great stuff")
GREET_PHRASES = ("good morning", "good afternoon", "good evening", "hi there", "hey there", "hello there", "hi all",
                 "hi team", "hi everyone", "hello everyone", "hello all", "hey all", "hey team", "hey everyone",
                 "how are you", "how are you doing", "how s it going", "hows it going", "what s up", "whats up", "howdy")
ACK_TAIL = {"all", "everyone", "everybody", "guys", "folks", "again", "man", "mate", "both", "then"}  # only after an ack: "thanks all"
COMMITMENT = re.compile(r"\b(te|les|le) (escribo|aviso|respondo|paso|mando|comento|confirmo)\b|\b(lo|los|la|las) "
                        r"(miro|veo|reviso|hago|cierro|subo|mando|arreglo|pruebo|termino|agrego)\b|\bme fijo\b|"
                        r"\bmañana\b|\bvoy a\b|\bvamos a\b|\bqueda pendiente\b|\b(le|les) pido\b|\ben un rato\b|"
                        r"\bdespu[eé]s (lo|te|les)\b|\bdeber[ií]a estar\b|\bva a estar\b|\ba punto de\b|\bestamos cerrando\b|"
                        r"\bqued[oó] en\b|\bpendiente\b|\bme qued[eé] (haciendo|viendo|con|armando)\b|"
                        r"\bestoy (armando|haciendo|viendo|revisando|terminando|mirando|con)\b|"
                        # the same in English: what he says he will do, is doing or expects soon
                        r"\bi['’]?ll\b|\bwe['’]?ll\b|\b(i|we) will\b|\blet me\b|\blemme\b|\btomorrow\b|\blater today\b|"
                        r"\b(i['’]?m|i am|we['’]?re|we are) (going to|gonna|working on|looking (at|into)|checking|fixing|"
                        r"reviewing|finishing|testing|about to|on it|still)\b|\bwill (do|check|look|fix|review|send|update|"
                        r"confirm|follow|get back)\b|\bpending\b|\bwip\b|\bin a (bit|sec|second|minute|few|while|moment)\b|"
                        r"\bshould be (ready|done|live|up|out|fixed|merged|deployed)\b|\bget(ting)? back to\b|"
                        r"\bfollow(ing)? up\b|\bcircle back\b|\bon it\b|\bby (eod|eow|tomorrow|end of (day|week)|"
                        r"monday|tuesday|wednesday|thursday|friday)\b", re.I)
# his own first-person promises ("ahora lo cierro", "lo veo y te aviso", "mañana lo miro"): narrower than COMMITMENT,
# which also keeps status lines; these open a deterministic OPEN_LOOP in ingest
PROMISE = re.compile(r"\b(lo|la|los|las) (miro|veo|reviso|hago|cierro|subo|arreglo|pruebo|termino|agrego|chequeo|investigo|"
                     r"resuelvo|levanto|armo|paso|mando|sigo)\b|\b(te|le|les) (aviso|escribo|respondo|confirmo|paso|mando)\b|"
                     r"\b(y|despu[eé]s|luego|ahora) (te|le|les) (digo|cuento|comento)\b|\bme fijo\b|"
                     r"\b(te|le|les) vuelvo a (escribir|avisar)\b|\bvoy a (mirar|ver|revisar|hacer|cerrar|subir|arreglar|probar|"
                     r"terminar|agregar|chequear|investigar|armar|pasar|mandar|escribir|avisar|responder|confirmar)\b", re.I)
# the same promises in English ("I'll check the logs and get back to you", "will do", "on it"). A bare "will" counts
# only where he is its subject (the start of a sentence, after a comma or a short opener): "it will take a while" and
# "Ana will check" promise nothing of his
_EN_VERB = (r"(?:look|check|review|fix|take|send|get|let|follow|ping|update|investigate|dig|handle|do|test|try|write|"
            r"reply|answer|confirm|share|push|merge|deploy|open|close|finish|add|circle|sort|ask|find|see|read|run|"
            r"verify|dm|message|email|call|talk|sync|post|upload|prepare|draft|report|raise|file|create|make|bring|"
            r"reach|have a look)")
_EN_FILL = r"(?:(?:just|also|then|now|quickly|go ahead and|go and|go|try to|try and|definitely|probably)\s+){0,3}"
PROMISE_EN = re.compile(
    rf"\b(?:i['’]?ll|i will|i['’]?m going to|i am going to|i['’]?m gonna|let me|lemme)\s+{_EN_FILL}{_EN_VERB}\b|"
    rf"(?:^\s*|[,;:]\s*|\b(?:and|but|ok|okay|yes|yep|yeah|sure|so|then|cool|great|thanks|perfect)[,!]?\s+)will\s+{_EN_FILL}{_EN_VERB}\b|"
    r"\b(?:i['’]?m |i am )?on it\b|\bget(?:ting)? back to (?:you|u|him|her|them)\b|"
    r"\b(?:i['’]?ll|i will|will) (?:keep|let) (?:you|u|him|her|them|everyone) (?:posted|updated|know)\b|"
    r"\b(?:i['’]?ll|i will|will|let me) circle back\b", re.I)
_URL = re.compile(r"https?://|<https?:")


def promise(text: str) -> bool:
    """Does this message of his promise a next step? A question ("lo miro yo?"), a negation ("no lo veo") or a
    hand-over done in the same message ("te paso el link: <url>") is not a promise."""
    for sent in re.split(r"(?<=[.!?¿\n])", text or ""):
        for m in PROMISE.finditer(sent):
            if sent.strip().endswith("?") or re.search(r"\bno\s*$", sent[:m.start()], re.I):
                continue
            if m.group(0).split()[-1].lower() in ("paso", "mando") and _URL.search(text):
                continue
            return True
        for m in PROMISE_EN.finditer(sent):
            if sent.strip().endswith("?") or re.search(r"\b(?:if|when|unless|whether|not|never|don['’]?t|didn['’]?t)\s*$",
                                                       sent[:m.start()], re.I):
                continue  # asked, conditional or denied: "should I check?", "if I'm on it by then", "not on it"
            return True
    return False


_REPO = site.repo_or(r"[\w-]+")
OWN_KEEP = re.compile(rf"[?¿]|<@[US][A-Z0-9]+|<!subteam\^|github\.com/(?:{site.owners_re()})/(?:{_REPO})/pull/\d+")
SYSTEM_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name", "bot_add",
                   "bot_remove", "pinned_item", "unpinned_item", "reminder_add", "group_join", "group_leave",
                   "message_deleted", "tombstone", "joiner_notification", "bot_message", "huddle_thread",
                   "channel_archive", "channel_unarchive", "ekm_access_denied"}


def _fold(word: str) -> str:
    w = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode().lower()
    return re.sub(r"(.)\1+", r"\1", w)


ACK_F = {_fold(w) for w in ACK}
GREET_F = {_fold(w) for w in GREET}
ACK_TAIL_F = {_fold(w) for w in ACK_TAIL}


def _folded(phrases) -> list[tuple]:
    return sorted({tuple(_fold(w) for w in p.split()) for p in phrases}, key=len, reverse=True)  # longest first


ACK_P, GREET_P = _folded(ACK_PHRASES), _folded(GREET_PHRASES)


def _pieces(ws: list[str]) -> tuple[list[str], bool] | None:
    """The folded words as a run of known pieces ('greet', 'ack', 'laugh') and whether a whole phrase was among them;
    None when some word is none of those, which is what makes the message worth a model."""
    out, i, phrase = [], 0, False
    while i < len(ws):
        hit = None
        for kind, table in (("greet", GREET_P), ("ack", ACK_P)):
            hit = next((p for p in table if tuple(ws[i:i + len(p)]) == p), None)
            if hit:
                break
        if hit:
            out.append(kind)
            phrase = phrase or len(hit) > 1 or ws[i] not in ACK_F | GREET_F
            i += len(hit)
            continue
        w = ws[i]
        if w in GREET_F:
            out.append("greet")
        elif w in ACK_F or (out and out[-1] == "ack" and w in ACK_TAIL_F):
            out.append("ack")
        elif LAUGH.match(w):
            out.append("laugh")
        else:
            return None
        i += 1
    return out, phrase


def _is_gif_file(f) -> bool:
    if isinstance(f, str):
        return f.lower().endswith((".gif", ".gifv"))
    ft = (f.get("filetype") or "").lower()
    mt = (f.get("mimetype") or "").lower()
    name = (f.get("name") or "").lower()
    return ft == "gif" or mt == "image/gif" or name.endswith(".gif")


def words(text: str) -> tuple[list[str], str]:
    """(folded content words, residue) after removing Slack tokens, gif links, emoji and punctuation."""
    t = SLACK_LINK.sub(r"\1", text or "")
    t = SLACK_TOKEN.sub(" ", t)
    t = GIF_HOSTS.sub(" ", t)
    t = EMOJI_CODE.sub(" ", t)
    t = EMOJI_CHARS.sub(" ", t)
    toks = [w for w in re.split(r"[^\wáéíóúñü]+", t, flags=re.I) if w]
    return [_fold(w) for w in toks], t


def noise_reason(text: str, *, user: str = "", self_id: str = "", bot: bool = False, subtype: str | None = None,
                 files=(), media=()) -> str | None:
    """Why this message never needs a model, or None when it might. Pure function: no I/O."""
    text = text or ""
    if self_id and user == self_id:
        # his own messages only matter to the triage as open loops: a commitment, a question he is waiting on,
        # a tag or a PR he posted for someone
        return None if (COMMITMENT.search(text) or OWN_KEEP.search(text)) else "own"
    if bot or (user or "").startswith("B"):
        return "bot"
    if subtype and subtype in SYSTEM_SUBTYPES:
        return f"subtype:{subtype}"
    files = list(files or [])
    media = list(media or [])
    ws, _ = words(text)
    has_gif = bool(GIF_HOSTS.search(text)) or any(_is_gif_file(f) for f in files) or any(
        str(u).lower().split("?")[0].endswith(".gif") for u in media)
    other_files = [f for f in files if not _is_gif_file(f)]
    if other_files:
        return None  # a screenshot or a document with no text can be a bug report
    if not ws:
        if has_gif:
            return "gif"
        if SLACK_TOKEN.search(text) or not text.strip():
            # a bare @mention is "look at this thread"; an empty text is an alert or a message made only of
            # attachments/blocks: the model decides both
            return None
        return "emoji"
    question = "?" in text or "¿" in text
    pieces = _pieces(ws) if len(ws) <= 6 else None
    if pieces and (len(ws) <= 4 or pieces[1]):  # four loose words at most; a known phrase may run a little longer
        kinds = pieces[0]
        if all(k == "greet" for k in kinds):
            return "greeting"
        if not question:
            if has_gif:
                return "gif"
            return "laughter" if all(k == "laugh" for k in kinds) else "ack"
    if len(ws) <= 3 and not question and all(LAUGH.match(w) for w in ws):
        return "laughter"
    return None


def from_message(m: dict, self_id: str) -> str | None:
    """noise_reason for a raw Slack message dict."""
    return noise_reason(m.get("text") or "", user=m.get("user") or "", self_id=self_id,
                        bot=bool(m.get("bot_id")) and not m.get("user"), subtype=m.get("subtype"),
                        files=m.get("files") or [], media=media_urls(m))


def media_urls(m: dict) -> list[str]:
    """Image URLs carried in blocks/attachments (the Slack GIF picker posts an image block, not a file)."""
    out = []
    for b in m.get("blocks") or []:
        if b.get("type") == "image" and b.get("image_url"):
            out.append(b["image_url"])
    for a in m.get("attachments") or []:
        for k in ("image_url", "thumb_url"):
            if a.get(k):
                out.append(a[k])
    return out[:4]


def file_meta(m: dict) -> list[dict]:
    return [{"name": f.get("name") or f.get("title") or "", "filetype": f.get("filetype") or "",
             "mimetype": f.get("mimetype") or "", "size": f.get("size") or 0} for f in (m.get("files") or [])][:8]


def from_event(ev, self_id: str) -> str | None:
    """noise_reason for a stored event row (payload carries files/subtype/media since 2026-09-30; older rows only names)."""
    import json
    p = json.loads(ev["payload"] or "{}")
    return noise_reason(ev["text"] or "", user=ev["user"] or "", self_id=self_id, bot=bool(p.get("bot")),
                        subtype=p.get("subtype"), files=p.get("files_meta") or p.get("files") or [], media=p.get("media") or [])


# ---- counters ------------------------------------------------------------------------------------------------------
def count(kind: str, reason: str | None = None, n: int = 1) -> None:
    from . import db
    k = f"prefilter:{db.today()}"
    with db.tx():  # ingest and triage count from different threads
        c = db.kv_get(k) or {"prefiltered": 0, "model_triaged": 0, "fast_lane": 0, "reasons": {}}
        c[kind] = c.get(kind, 0) + n
        if reason:
            c["reasons"][reason] = c["reasons"].get(reason, 0) + n
        db.kv_set(k, c)


def drop_event(ev_id: int, reason: str) -> None:
    """Mark an event as triaged without a model; the reason is kept as a signal (auditable by the noop audit)."""
    from . import db
    db.x("UPDATE events SET triaged=1 WHERE id=?", ev_id)
    db.add_signal("prefilter", {"event_id": ev_id, "reason": reason})
    count("prefiltered", reason)


def stats(day: str | None = None) -> dict:
    from . import db
    return db.kv_get(f"prefilter:{day or db.today()}") or {"prefiltered": 0, "model_triaged": 0, "fast_lane": 0, "reasons": {}}
