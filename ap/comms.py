"""How the harness talks to the user in his DM.

One shape for everything that asks for his attention, a card he can read in ten seconds without opening anything:

    🔧 *a customer: al cambiar un alumno de grupo sus notas no se ven en la boleta* · TICKET-1479
    Lo levantó a teammate a customer en #customer-ops el 12/10.
    • No es un bug: el cambio de grupo deja las notas en la clase vieja; no se borran.
    • Propongo recuperarlas con un script cuando el colegio mande la lista.
    *Necesito*: tu ✅ para mandar el borrador al equipo.
    <url|hilo original> · <url|TICKET-1479> · <url|schools#1038>

Rules, enforced here and not left to a prompt:
- the headline says who is affected and what happens, in his words (no file, class or function names);
- context first: who raised it, where, when;
- at most three plain bullets; file:line, traces, counts and quotes go in the case thread ("Detalle");
- one line saying what he has to do, or that he has to do nothing;
- links only as short labels, at most four, on the last line; never a raw URL; no previews (the guard posts with
  unfurl off);
- Spanish.
"""
from __future__ import annotations

import re

from . import site
from .core import CFG, log

TOP_MAX = 1500          # a top-level message longer than this moves its tail into the thread
EMOJI = {"need": "🟡", "fyi": "🔧", "done": "✅", "blocked": "⛔", "pr": "🔀"}

_CARD_KEY = site.ticket_key("card", r"\d+")  # a ticket key in backticks is a name he reads, not code
_CODE = re.compile(rf"`(?!{_CARD_KEY}`)[^`]*`|\b[\w/]+\.(?:py|ts|tsx|js|sql)(?::\d+(?:-\d+)?)?\b|\b[a-z]+_[a-z_]+\(\)")
_EN = re.compile(r"\b(the|and|was|were|is|are|with|which|that|this|from|still|wrote|again|only|said|would|could|should|"
                 r"has|have|been|into|their|there|because|while|after|before)\b", re.I)
_ES = re.compile(r"\b(el|la|los|las|que|con|del|por|para|una|uno|está|están|pero|como|cuando|sin|ya|también|hay|se)\b", re.I)


def is_english(text: str) -> bool:
    words = re.sub(r"<[^>]+>|`[^`]*`|https?://\S+", " ", text)
    en, es = len(_EN.findall(words)), len(_ES.findall(words))
    return en >= 6 and en > es


def plain(text: str) -> str:
    """No code in a headline or a bullet: drop backticked identifiers, file paths and function names."""
    t = _CODE.sub("", text or "").replace("`", "")
    return re.sub(r"\s{2,}", " ", re.sub(r"\(\s*\)", "", t)).strip(" ,;:")


def label(url: str) -> str:
    repo = site.repo_or(r"[\w-]+")
    m = re.search(rf"github\.com/(?:{site.owners_re()})/({repo})/pull/(\d+)", url)
    if m:
        return f"{m.group(1)}#{m.group(2)}"
    m = re.search(r"atlassian\.net/browse/([A-Z]+-\d+)", url)
    if m:
        return m.group(1)
    if "slack.com/archives/" in url:
        return site.t("thread", "hilo")
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else "link"


def label_links(text: str) -> str:
    """Every link becomes a short label: <url> and raw URLs get one; <url|label> stays."""
    text = re.sub(r"<(https?://[^|>]+)>", lambda m: f"<{m.group(1)}|{label(m.group(1))}>", text)
    return re.sub(r"(?<![<|])(https?://[^\s>|]+)", lambda m: f"<{m.group(1)}|{label(m.group(1))}>", text)


def links_line(it: dict) -> str:
    """The case's links, labeled, in one line: source thread, ticket, PRs."""
    from . import slack
    d = it["data"]
    out = []
    if d.get("channel") and (d.get("thread_ts") or d.get("ts")):
        try:
            out.append(f"<{slack.permalink(d['channel'], d.get('thread_ts') or d['ts'])}|{site.t('source thread', 'hilo original')}>")
        except Exception:
            pass
    key = d.get("jira")
    if key and site.ticket_url(key):
        out.append(f"<{site.ticket_url(key)}|{key}>")
    for p in d.get("parts", []):
        if p.get("pr_url"):
            out.append(f"<{p['pr_url']}|{label(p['pr_url'])}>")
    for p in d.get("linked_prs", []) or []:
        if p.get("pr_url") and p["pr_url"] not in " ".join(out):
            out.append(f"<{p['pr_url']}|{label(p['pr_url'])}>")
    return " · ".join(out[:4])


def origin(it: dict) -> str:
    """Who raised it, where, when: the context line."""
    from . import digest
    try:
        o = digest._origin_one(it["title"], it["data"])
    except Exception:
        o = ""
    return (o[:1].upper() + o[1:] + ".") if o else ""


def card(it: dict, headline: str, bullets: list[str], need: str, kind: str = "fyi", extra: str = "") -> str:
    head = plain(headline) or plain(it["title"].replace("(slack) ", "")) or it["title"]
    key = it["data"].get("jira")
    lines = [f"{EMOJI.get(kind, '🔧')} *{head[:140]}*" + (f" · {key}" if key and key not in head else "")]
    o = origin(it)
    if o:
        lines.append(o)
    lines += [f"• {plain(b)[:220]}" for b in bullets[:3] if plain(b)]
    if extra:
        lines.append(extra)
    lines.append(site.t(f"*Needed from you*: {need.strip()}", f"*Necesito*: {need.strip()}") if need and need.strip()
                 else site.t("*Needed from you*: nothing for now, I will tell you if that changes.",
                             "*Necesito*: nada por ahora, te aviso si cambia."))
    ll = links_line(it)
    if ll:
        lines.append(ll)
    return "\n".join(lines)


def top_level(text: str) -> tuple[str, str]:
    """(what stays in the DM, what moves into the thread). Cuts at a paragraph boundary under TOP_MAX."""
    if len(text) <= TOP_MAX:
        return text, ""
    paras = text.split("\n\n")
    keep, rest = [], []
    for p in paras:
        (keep if not rest and len("\n\n".join(keep + [p])) <= TOP_MAX else rest).append(p)
    if not keep:
        return text[:TOP_MAX].rsplit("\n", 1)[0] + " …", text
    return "\n\n".join(keep) + site.t("\n\n_(continued in the thread)_", "\n\n_(sigue en el hilo)_"), "\n\n".join(rest)


def check(text: str, where: str) -> None:
    """Log what a prompt let through; the observer turns repeats into incidents."""
    if site.lang() == "es" and is_english(text):  # an English install writes English on purpose
        log("comms: English in his DM", where=where, head=text[:120])


# the prefixes the engine puts on titles; an English install also strips the Spanish ones it may have stored
_TITLE_PREFIX = site.t(r"^(\(slack\)|\(audit\)|\(daily\)|patrol:|alerta|alert\b|pedido repetido:|repeated request:)\s*",
                       r"^(\(slack\)|\(audit\)|\(daily\)|patrol:|alerta|pedido repetido:)\s*")


def name(it: dict) -> str:
    """How a case is called in his DM: its plain headline, never the internal title ('patrol:...', '(slack)...')."""
    h = (it.get("data") or {}).get("card_headline")
    if h:
        return plain(h)[:110]
    t = re.sub(_TITLE_PREFIX, "", it.get("title") or "", flags=re.I)
    key = (it.get("data") or {}).get("jira")
    t = plain(t)[:110]
    return f"{key}: {t}" if key and key not in t else t


_REASONS = [
    (r"Failed to start (?:forks|threads) worker|Timeout waiting for worker",
     site.t("the frontend tests did not start on this machine (the environment, not the code)",
            "las pruebas del front no arrancaron en la máquina (es el entorno, no el código)")),
    (r"no completed CodeRabbit pass|CodeRabbit CLI no completó",
     site.t("CodeRabbit has not reviewed the latest change", "falta la revisión de CodeRabbit sobre el último cambio")),
    (r"CONTAINER TESTS DID NOT RUN|docker", site.t("the container tests could not run (Docker)", "los tests en contenedor no pudieron correr (Docker)")),
    (r"build produced no commits", site.t("the coding session left no changes", "la sesión de código no dejó ningún cambio")),
    (r"fix rounds exhausted|still failing after \d+ fix rounds",
     site.t("the checks still fail after two fix attempts", "los checks siguen fallando después de dos intentos de arreglo")),
    (r"CI still red", site.t("CI is still red after the fixes", "el CI sigue en rojo después de los arreglos")),
    # repro.py's blocked reasons, written in the install's language
    (site.t(r"la reproducción no quedó demostrada|no pude reproducirlo|reproduction (?:was )?not (?:shown|demonstrated|proven)|"
            r"could(?: not|n't) reproduce", r"la reproducción no quedó demostrada|no pude reproducirlo"),
     site.t("I could not reproduce the bug yet", "no pude reproducir el bug todavía")),
]


def reason(raw: str) -> str:
    """A blocked reason he can read: known causes in one plain sentence, never a stack trace or a tool dump."""
    raw = raw or ""
    hits = [txt for pat, txt in _REASONS if re.search(pat, raw, re.I)]
    if hits:
        return "; ".join(dict.fromkeys(hits[:2]))
    first = re.split(r"\n|⎯|\s{3,}", raw.strip(), 1)[0]
    return plain(first)[:160] or site.t("no reason recorded", "sin motivo registrado")
