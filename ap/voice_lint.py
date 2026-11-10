"""Deterministic outbound lint: every draft is checked against how the user actually edits drafts before he sends
them. The rules are
per audience, derived from the channel ids in config, and they fix what they can and report what they cannot:

  all      no em dashes (nor en dashes / spaced hyphens used as one); a plain-text "@Name" becomes a real <@UID>
           mention when the name maps to exactly one Slack user; a top-level post whose backlink points to a thread
           in the same channel goes INTO that thread instead
  pr       (#squad-sis-prs) the only place the squad group is cc'd; two lines + cc + links; the ticket link goes in
           a reply to the post, not in the post
  team     (#team-the team) no squad-group cc
  ops      (ops/CS channels) no squad-group cc, no PR links, no internal caveats (draft, falta review, deploy, CI,
           merge), team voice ("encontramos", "avísennos"), the greeting capitalized ("Buenas <@U..>!"), an apology
           when the answer took long
  dm       no squad-group cc

A release install runs only the rules every install shares: no dashes, real mentions, no unfilled slots, routing into
an existing thread, and its own learned rules. The group cc, the PR-post shape and the ops rules are the first
install's own conventions (its company's channels, its user's Spanish voice) and run there only.

Learned rules (knowledge/voice_lint.json, written by sentlearn.daily from recurring draft-vs-sent edits) add
phrase replacements and removals per audience on top of these.

Pure functions over text; the only I/O is the people lookup for mentions (config roster, the thread's
participants, the workspace directory cached daily), skipped entirely when the text has no plain "@".
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import site
from .core import CFG, log

from .core import KNOWLEDGE as _KNOWLEDGE
LEARNED_FILE = _KNOWLEDGE / "voice_lint.json"  # the install's own (the checkout itself for the first install)
AUDIENCES = ("pr", "team", "ops", "dm", "self", "other")


@dataclass
class Lint:
    text: str
    audience: str
    thread_ts: str | None = None
    fixes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def note(self) -> str:
        """One line for the ask (never inside the message): what was adjusted, what he should look at."""
        out = []
        if self.fixes:
            out.append(site.t("adjusted: ", "ajusté: ") + "; ".join(dict.fromkeys(self.fixes)))
        if self.problems:
            out.append(site.t("check: ", "revisar: ") + "; ".join(dict.fromkeys(self.problems)))
        return ("_" + " · ".join(out) + "_") if out else ""


# ---- audience ----------------------------------------------------------------------------------------------
def _aud_cfg() -> dict:
    return CFG.get("audiences") or {}


def audience(channel: str | None) -> str:
    ch = channel or ""
    a = _aud_cfg()
    if ch == CFG.get("self_dm_channel"):
        return "self"
    if ch == CFG.get("pr_channel") or ch in (a.get("pr_channels") or []):
        return "pr"
    if ch == CFG.get("team_channel") or ch in (a.get("team_channels") or []):
        return "team"
    ops = a.get("ops_channels")
    if ops is None:  # no explicit list: the channels whose name says ops / customer / account management
        ops = [c for c, n in (CFG.get("channel_names") or {}).items() if re.search(r"-ops$|customer|account-management", n or "")]
    if ch in ops:
        return "ops"
    if ch.startswith(("D", "G")) or ch in (a.get("dm_channels") or []):
        return "dm"
    return "other"


# ---- helpers -------------------------------------------------------------------------------------------------
def fold(s: str) -> str:
    """lowercase, no accents: 'a teammate' == 'diaz'."""
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower()


def _keep_case(src: str, repl: str) -> str:
    if src.isupper() and len(src) > 1:
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _tidy(text: str) -> str:
    """Whitespace and punctuation left behind by removals."""
    lines = []
    for l in text.split("\n"):
        l = re.sub(r"[ \t]{2,}", " ", l)
        l = re.sub(r"(?<=\S)[ \t]+([,.!?;])(?=\s|$)", r"\1", l)
        l = re.sub(r",\s*,", ",", l)
        l = re.sub(r",([.!?])", r"\1", l)
        l = re.sub(r"^\s*[,;]\s*", "", l)
        lines.append(l.rstrip())
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---- rules -------------------------------------------------------------------------------------------------
SUBTEAM = re.compile(r"<!subteam\^S[A-Z0-9]+(?:\|[^>]*)?>")
_TAG = site.team_tag()  # the team's group typed as plain text; an install with none matches nothing
PLAIN_GROUP = re.compile(rf"(?<![\w<]){re.escape(_TAG)}\b" if _TAG else r"(?!)", re.I)


SLOT = re.compile(r"(?<!['\"(/])<(?![@#!]|https?:|mailto:)[A-Za-zÀ-ÿ_][^<>|\n]{0,30}>"
                  r"|\[(?:link|url|nombre|n[uú]mero|ticket|fecha|pr|x+|n)\b[^\]\n]{0,60}\]|\bX{3,}\b", re.I)
# the same slots as a drafter writing in English leaves them ('[name]', '[date]'); the first install keeps SLOT as is
SLOT_ANY = re.compile(SLOT.pattern.replace("|ticket|", "|ticket|name|number|date|"), re.I)
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)  # '--tasks <arn>' in a command is the command's own syntax


def _slots(text: str, r: Lint) -> None:
    """A slot the drafter left for him (a value only he knows) is not sendable: with ✅ it would go out literally
."""
    found = list(dict.fromkeys(m.group(0) for m in (SLOT if site.internal() else SLOT_ANY).finditer(CODE.sub("", text))))
    if found:
        r.problems.append(site.t(f"a value only you know is missing ({', '.join(found[:3])}): reply with it, ✅ sends it as is",
                                 f"falta un dato tuyo ({', '.join(found[:3])}): respondé con el dato, con ✅ sale tal cual"))


def _drop_group_cc(text: str, r: Lint) -> str:
    if not (SUBTEAM.search(text) or PLAIN_GROUP.search(text)):
        return text
    out = []
    for l in text.split("\n"):
        new = PLAIN_GROUP.sub("", SUBTEAM.sub("", l))
        if new != l:
            new = re.sub(r"(?:^|[,;]?[ \t]+)/?cc:?[ \t,]*$", "", new.rstrip(), flags=re.I)  # a cc left with nobody after it
            new = new.rstrip(" \t,;")
            new = re.sub(r"[ \t]{2,}", " ", new).rstrip()
            if not re.sub(r"[\W_]", "", new):
                continue  # the line was only the cc
        out.append(new)
    r.fixes.append(site.t("removed the group cc (it only goes in the PR channel)", "saqué el cc al grupo (solo va en el canal de PRs)"))
    return "\n".join(out)


DASH = re.compile(r"[ \t]*—[ \t]*|[ \t]+–[ \t]+")  # an en dash between digits (10–12) is a range, not a dash
SPACED_HYPHEN = re.compile(r"(?<=[^\s\d-]) - (?=[^\s\d])")


def _no_dashes(text: str, r: Lint) -> str:
    if not (DASH.search(text) or SPACED_HYPHEN.search(text)):
        return text
    lines = []
    for l in text.split("\n"):
        l = re.sub(r"^\s*[—–]\s+", "", l)  # a dash opening a line is a bullet: drop it
        l = DASH.sub(", ", l)
        l = SPACED_HYPHEN.sub(", ", l)
        lines.append(l)
    r.fixes.append(site.t("removed the dashes (—)", "saqué las rayas (—)"))
    return "\n".join(lines)


GH_PR = r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+[^\s|>]*"
PR_LINK = re.compile(rf"<{GH_PR}(?:\|[^>]*)?>|{GH_PR}")
JIRA_LINK = re.compile(r"<?https?://[\w.-]*atlassian\.net/browse/[A-Z]+-\d+[^\s>]*(?:\|[^>]*)?>?")
CAVEAT = re.compile(r"\b(?:draft|falta(?:n)?\s+(?:el\s+|la\s+|los\s+|un\s+)?(?:code\s+)?(?:review|approve|aprobaci[oó]n|deploy|merge)|"
                    r"en\s+review|code\s+review|deploy\w*|mergea\w*|mergead\w*|merge|\bCI\b|pipeline|pull\s+request|\bPRs?\b|"
                    r"staging|feature\s+flag)\b", re.I)


# what pointed at a removed link: 'subí un fix acá:' would be left announcing nothing
LEAD_IN = re.compile(r"(?:(?:^|[\s,]+)(?:ac[aá]|aqu[ií]|ah[ií]|en|link))*[\s,]*:?[ \t]*$", re.I)
LINK_LAST = re.compile(rf"(?:{PR_LINK.pattern})[ \t]*$")


def _no_pr_links(text: str, r: Lint) -> str:
    if not PR_LINK.search(text):
        return text
    out = []
    for l in text.split("\n"):
        new = PR_LINK.sub("", l)
        filler = re.sub(r"\b(?:PRs?|links?|ac[aá]|ah[ií]|est[aá]n?|el|los|del|de|y)\b", "", new, flags=re.I)
        if new != l and not re.sub(r"[\W_]", "", filler):
            if out and out[-1].rstrip().endswith(":"):  # the line before introduced the link
                out[-1] = LEAD_IN.sub("", out[-1], count=1)
            continue  # the line was only the link (and its label)
        if LINK_LAST.search(l):  # the link closed the line: what introduced it goes too
            new = LEAD_IN.sub("", new, count=1)
        out.append(new)
    r.fixes.append(site.t("removed the PR links (ops has no use for them)", "saqué los links de PR (a ops no le sirven)"))
    return "\n".join(out)


def _no_caveats(text: str, r: Lint) -> str:
    """Sentences about our internal process (draft, review, deploy, CI) do not reach ops/CS. Removed when the
    rest of the message still says something; reported when removing them would leave nothing."""
    if not CAVEAT.search(text):
        return text
    kept_lines, removed = [], []
    for l in text.split("\n"):
        parts = re.split(r"(?<=[.!?])\s+", l)
        keep = []
        for p in parts:
            if CAVEAT.search(p):
                removed.append(p.strip())
            else:
                keep.append(p)
        if keep or not l.strip():
            kept_lines.append(" ".join(keep))
    rest = "\n".join(kept_lines)
    if not re.search(r"\w{3,}", SUBTEAM.sub("", re.sub(r"<[^>]+>", "", rest))):
        r.problems.append(site.t("it is about internal process (draft, review, deploy, CI) and nothing is left without "
                                 "that: rewrite it without the caveats",
                                 "habla del proceso interno (draft, review, deploy, CI) y no queda mensaje sin eso: reescribilo sin caveats"))
        return text
    for s in removed:
        r.fixes.append(site.t(f"removed the internal caveat \"{s[:60]}\"", f"saqué el caveat interno \"{s[:60]}\""))
    return rest


TEAM_VOICE = [  # first person singular -> the team, as he writes to ops/CS. Unaccented forms only where they cannot
    # be a subjunctive ("encontre" yes; "arregle"/"revise" no: "que lo arregle a teammate")
    (r"encontr[eé]", "encontramos"), (r"arreglé", "arreglamos"), (r"corregí", "corregimos"),
    (r"revisé", "revisamos"), (r"av[ií]senme", "avísennos"), (r"av[ií]same", "avisanos"),
    (r"me\s+avisan", "nos avisan"), (r"me\s+avis[aá]s", "nos avisás"), (r"estoy\s+viendo", "estamos viendo"),
    (r"lo\s+estoy\s+cerrando", "lo estamos cerrando"),
]


def _team_voice(text: str, r: Lint) -> str:
    changed = []
    for pat, repl in TEAM_VOICE:
        rx = re.compile(rf"(?<![\w]){pat}(?![\w])", re.I)

        def sub(m, repl=repl):
            changed.append(f"{m.group(0)}→{repl}")
            return _keep_case(m.group(0), repl)
        text = rx.sub(sub, text)
    if changed:
        r.fixes.append(site.t("team voice (", "voz de equipo (") + ", ".join(dict.fromkeys(changed)) + ")")
    return text


GREETING = re.compile(r"^(\s*)(buen[oa]s+|hola+|buen\s+d[ií]a)\b", re.I)


def _ops_greeting(text: str, r: Lint) -> str:
    m = GREETING.match(text)
    if not m:
        r.problems.append(site.t("no greeting: open a message to ops with \"Buenas <@person>!\"",
                                 "sin saludo: a ops/CS abrí con \"Buenas <@persona>!\""))
        return text
    word = m.group(2)
    if word[:1].islower():
        text = text[:m.start(2)] + word[:1].upper() + word[1:] + text[m.end(2):]
        r.fixes.append(site.t("capitalized the greeting", "saludo con mayúscula"))
    return text


# ---- mentions ----------------------------------------------------------------------------------------------
NOT_PEOPLE = {"here", "channel", "everyone", "canal", "todos"} | ({_TAG.lstrip("@").lower()} if _TAG else set())
AT_NAME = re.compile(r"(?<![\w<@.])@([A-Za-zÀ-ÿ][\wÀ-ÿ'.-]*(?:[ \t]+[A-Za-zÀ-ÿ][\wÀ-ÿ'-]*){0,2})")


def _tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(name))


def resolve_name(name: str, people) -> str | None:
    """The one Slack user whose name starts with these words ('paulina' or 'paulina rangel' -> a teammate a teammate
    a teammate). `people` is uid -> names, or a list of such tiers searched in order (the conversation's people before
    the whole workspace), so a first name that is ambiguous in the workspace still resolves to the person in the
    thread. None when nobody matches, or when the first tier with a match has more than one."""
    want = _tokens(name)
    if not want:
        return None
    for tier in (people if isinstance(people, list) else [people]):
        hits = {uid for uid, names in tier.items() for n in names if _tokens(n)[:len(want)] == want}
        if hits:
            return hits.pop() if len(hits) == 1 else None
    return None


def _mentions(text: str, r: Lint, people, channel: str, thread_ts: str | None) -> str:
    if not AT_NAME.search(text):
        return text
    if people is None:
        people = people_for(channel, thread_ts)

    def sub(m):
        group = m.group(1)
        words = group.split()
        if fold(words[0].rstrip(".,!?:;")) in NOT_PEOPLE:
            return m.group(0)
        for k in range(len(words), 0, -1):  # longest name first; the rest of the words stay as text
            prefix = re.match(r"\S+(?:[ \t]+\S+){%d}" % (k - 1), group).group(0)
            cand = prefix.rstrip(".,!?:;")
            uid = resolve_name(cand, people)
            if uid:
                r.fixes.append(site.t(f"@{cand} → real mention", f"@{cand} → mención real"))
                return f"<@{uid}>{prefix[len(cand):]}{group[len(prefix):]}"
        who = words[0].rstrip('.,!?:;')
        r.problems.append(site.t(f"could not match @{who} to a Slack user: mention them by hand",
                                 f"no pude resolver @{who} a una persona de Slack: mencionalo a mano"))
        return m.group(0)
    return AT_NAME.sub(sub, text)


def people_for(channel: str | None, thread_ts: str | None) -> list[dict[str, list[str]]]:
    """Two tiers of uid -> names: (1) the squad roster in config and the thread's participants, (2) the workspace
    directory (cached daily)."""
    out: dict[str, list[str]] = {}
    for uid, m in (CFG.get("team_members") or {}).items():
        if uid.startswith("U") and isinstance(m, dict):
            out.setdefault(uid, []).append(re.sub(r"\s*\(.*?\)", "", m.get("name") or ""))
    if os.environ.get("AP_SELFTEST"):
        return [out]
    from . import slack
    if channel and thread_ts:
        try:
            for msg in slack.replies(channel, thread_ts):
                uid = msg.get("user")
                if uid and uid not in out:
                    out[uid] = [slack.user_name(uid)]
        except Exception as e:
            log("voice lint: thread participants failed", err=str(e)[:120])
    return [out, directory()]


def directory() -> dict[str, list[str]]:
    """Every active human in the workspace: uid -> [real name, display name], refreshed daily."""
    from . import db, slack
    from .core import now
    c = db.kv_get("slack_directory") or {}
    if c.get("at", 0) > now() - 86400 and c.get("users"):
        return c["users"]
    users, cursor = {}, None
    try:
        for _ in range(20):
            d = slack._call("users_list", limit=200, cursor=cursor)
            for u in d.get("members") or []:
                if u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT":
                    continue
                p = u.get("profile") or {}
                names = [n for n in (u.get("real_name") or p.get("real_name"), p.get("display_name")) if n]
                if names:
                    users[u["id"]] = names
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        log("voice lint: directory failed", err=str(e)[:120])
        return c.get("users") or {}
    db.kv_set("slack_directory", {"at": now(), "users": users})
    return users


# ---- routing -----------------------------------------------------------------------------------------------
PERMALINK = re.compile(r"<?https://[\w.-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?thread_ts=(\d+\.\d+)[^\s|>]*)?(?:\|[^>]*)?>?")


def _route(text: str, channel: str, thread_ts: str | None, r: Lint) -> tuple[str, str | None]:
    """A top-level post that points at a thread in the same channel belongs inside that thread: he replies in the
    existing conversation instead of opening a new one with a backlink."""
    if thread_ts or r.audience == "pr":
        return text, thread_ts
    for m in PERMALINK.finditer(text):
        if m.group(1) != channel:
            continue
        root = m.group(4) or f"{m.group(2)}.{m.group(3)}"
        lines = [l for l in text.split("\n") if not (m.group(0) in l and not PERMALINK.sub("", l).strip(" :-"))]
        r.fixes.append(site.t("goes as a reply in the existing thread, not as a new post",
                              "va como respuesta en el hilo existente, no como post nuevo"))
        return "\n".join(lines), root
    return text, thread_ts


def _pr_shape(text: str, r: Lint, thread_ts: str | None) -> str:
    """The shape of a top-level PR-channel post. A reply in a PR's thread (a re-approval nudge, the ticket link) is
    free-form and never cc's the group again."""
    if thread_ts:
        return text
    lines = [l for l in text.split("\n") if l.strip()]
    body = [l for l in lines if not (PR_LINK.fullmatch(l.strip()) or l.strip().lower().startswith("cc "))]
    if len(body) > 2:
        r.problems.append(site.t(f"the PR post has {len(body)} lines: it should be two lines, the cc and the links",
                                 f"el post de PRs tiene {len(body)} líneas: son dos líneas + cc + links"))
    if JIRA_LINK.search(text):
        r.problems.append(site.t("the ticket link goes in a reply to the post, not in the post",
                                 "el link del ticket va en un reply al post, no en el post"))
    tags = CFG.get("team_subteam_tags") or []
    if tags and PR_LINK.search(text) and not SUBTEAM.search(text):
        tag = f"<!subteam^{tags[0]}>"
        out, done = [], False
        for l in text.split("\n"):
            if not done and PR_LINK.fullmatch(l.strip()):
                out.append(f"cc {tag}")
                done = True
            out.append(l)
        text = "\n".join(out) if done else text.rstrip() + f"\ncc {tag}"
        r.fixes.append(site.t("added the team cc", "agregué el cc al squad"))
    return text


# ---- learned rules -----------------------------------------------------------------------------------------
def learned_rules() -> list[dict]:
    try:
        return json.loads(LEARNED_FILE.read_text()).get("rules") or []
    except Exception:
        return []


def _apply_learned(text: str, aud: str, rules: list[dict], r: Lint) -> str:
    for rule in rules:
        if rule.get("audience") not in (aud, "all"):
            continue
        src = rule.get("from") or rule.get("phrase") or ""
        if len(src) < 4:
            continue
        rx = re.compile(rf"(?<!\w){re.escape(src)}(?!\w)", re.I)
        if not rx.search(text):
            continue
        if rule.get("kind") == "replace":
            text = rx.sub(lambda m: _keep_case(m.group(0), rule["to"]), text)
            r.fixes.append(site.t(f"learned: \"{src}\" → \"{rule['to']}\"", f"aprendido: \"{src}\" → \"{rule['to']}\""))
        elif rule.get("kind") == "drop":
            text = rx.sub("", text)
            r.fixes.append(site.t(f"learned: removed \"{src}\"", f"aprendido: saqué \"{src}\""))
    return text


# ---- entry point -------------------------------------------------------------------------------------------
def lint(text: str, channel: str | None, *, thread_ts: str | None = None, people=None,
         learned: list[dict] | None = None) -> Lint:
    """Fix what can be fixed deterministically, report the rest. Idempotent: lint(lint(x).text) changes nothing."""
    aud = audience(channel)
    r = Lint(text=text or "", audience=aud, thread_ts=thread_ts)
    if not (text or "").strip() or aud == "self":
        return r
    first = site.internal()  # its own conventions: the group cc, the PR-post shape and the ops voice (module docstring)
    t = text
    t = _apply_learned(t, aud, learned_rules() if learned is None else learned, r)
    t = _no_dashes(t, r)
    if first and aud != "pr":
        t = _drop_group_cc(t, r)
    t = _mentions(t, r, people, channel or "", thread_ts)
    if first and aud == "pr":
        t = _pr_shape(t, r, thread_ts)
    if first and aud == "ops":
        t = _no_pr_links(t, r)
        t = _no_caveats(t, r)
        t = _team_voice(t, r)
        t = _ops_greeting(t, r)
    _slots(t, r)
    t, r.thread_ts = _route(t, channel or "", thread_ts, r)
    r.text = _tidy(t)
    return r


# ---- audience templates (what the draft prompt receives) ---------------------------------------------------
TEMPLATES = {  # the first install's, in its user's voice; every other install reads neutral_templates()
    "ops": ("Ops / CS (colegios, customer ops, account managers). Abrí con \"Buenas @Nombre Apellido!\" con mayúscula: "
            "el nombre tal cual aparece en el hilo (se convierte en una mención real de Slack). "
            "Voz de equipo: \"encontramos\", \"lo arreglamos\", \"avísennos\" (nunca \"encontré\", \"avísenme\"). Nada de links "
            "a PRs, nada de \"draft\", \"falta review\", \"deploy\", \"CI\", \"merge\" ni caveats internos: qué pasaba y qué "
            "cambia para ellos. Si el trabajo no está terminado, una respuesta de espera honesta: \"ya lo estamos cerrando! "
            "Te escribo cuando esté listo\". Sin cc al grupo del squad."),
    "team": (f"{site.channel_label('team_channel', 'El canal del equipo')} (su equipo). Sin cc al grupo del squad. Si ya hay un hilo "
             "sobre el tema, la respuesta va "
             "ADENTRO de ese hilo, no un post nuevo con un link al hilo. Una serie de fixes se cuenta como historia "
             f"(\"buenaas devueltaaa! otro fix, ahora para {site.ticket_prefix()}-1355!\", \"sigo con la seguidilla jaj\"). Si tocás "
             "código que "
             "alguien escribió así a propósito, preguntá en vez de afirmar (\"esto está así por algo?… si alguno se "
             "acuerda de alguna razón me avisa!!!\")."),
    "pr": (f"{site.channel_label('pr_channel', 'El canal de PRs')} (reviewers). Dos líneas: saludo + alcance del cambio y de dónde salió; "
           "después "
           "\"cc <!subteam^...>\" (el único canal donde va el cc al squad) y los links de los PRs, cada uno en su línea. "
           "El link del ticket va en un reply al post, no en el post."),
    "dm": ("DM 1 a 1. Corto y directo, voseo, sin cc a grupos, sin ceremonia. Si la otra persona es de ops/CS, las "
           "reglas de ops aplican igual (voz de equipo, sin PRs ni caveats internos)."),
    "other": ("Canal compartido. Sin cc al grupo del squad; si ya hay un hilo, respondé adentro del hilo."),
}


def neutral_templates() -> dict[str, str]:
    """The audiences as any install has them: who reads and what the universal rules ask, with no voice and no
    company convention (TEMPLATES above is the first install's). The voice comes from the install's style guide."""
    return {
        "ops": ("Ops, support or customer-facing people. Address the person who asked by name as it appears in the "
                "thread (it becomes a real Slack mention). Say what was happening and what changes for them, in plain "
                "words. If the work is not finished, say so honestly and when they will hear back."),
        "team": (f"{site.channel_label('team_channel', 'The team channel')} (the user's team). If there is already a "
                 "thread on the topic, the answer goes INSIDE that thread, not in a new post linking to it. When the "
                 "change touches code someone wrote that way on purpose, ask instead of asserting."),
        "pr": (f"{site.channel_label('pr_channel', 'The PR channel')} (reviewers). Short: what the change does and where "
               "it came from, then the PR links, each on its own line."),
        "dm": "A 1:1 DM. Short and direct, no group mentions.",
        "other": "A shared channel. If there is already a thread, answer inside it.",
    }


def template(aud: str) -> str:
    own = TEMPLATES if site.internal() else neutral_templates()
    return own.get(aud) or own["other"]


def prompt_block(channel: str | None, examples: list[str] | None = None) -> str:
    """The audience of the conversation, its template, and his real messages to that audience."""
    aud = audience(channel)
    if aud == "self":
        return ""
    from . import slack
    ex = "\n\n".join(f"<<<\n{x}\n>>>" for x in (examples or []))
    head = site.own(f"MENSAJES REALES DE {site.marker()} A ESTA AUDIENCIA (calibrá contra estos)",
                    f"REAL MESSAGES FROM {site.marker()} TO THIS AUDIENCE (calibrate against these)")
    return (f"<audience type=\"{aud}\" channel=\"{slack.channel_name(channel) if channel else ''}\">\n{template(aud)}\n"
            + (f"\n{head}:\n{ex}\n" if ex else "")
            + "</audience>")
