"""PR body cap: a deterministic trimmer (no model).

Why: harness PR bodies ran 5k-18k characters while the user's own bug fixes carry a short body and
merge the same day. A reviewer needs Problem, Root cause, Changes, Edge cases and Testing (with counts); the rest
(long ledgers, local check dumps, notes) moves to the case report, never the PR.

Recognized headings: markdown `#`..`######` lines and bold-only lines (`**Cambios**`), in Spanish or English.
"""
from __future__ import annotations

import re
import unicodedata

from . import site

LIMIT = 4000
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$|^\s{0,3}\*\*([^*]+?)\*\*\s*:?\s*$")
# first match wins: "Problema raíz" is a root cause (the CUS form's name for it), "Casos borde" is not "casos"
KINDS = [
    ("other", ("checks locales", "local checks", "descartado", "discarded", "notas", "notes", "ledger", "coderabbit", "riesgo",
               "risk", "fuera de alcance", "out of scope")),
    ("root_cause", ("causa", "raiz", "root", "diagnostic", "por que", "why it happened")),
    ("edge_cases", ("borde", "edge", "limite", "corner")),
    ("testing", ("test", "prueba", "verific", "validacion", "qa", "como probar", "how to test")),
    ("changes", ("cambi", "change", "solucion", "solution", "fix", "implementa", "que hace", "what it does")),
    ("problem", ("problem", "contexto", "context", "resumen", "summary", "que pasaba", "descripcion", "description",
                 "motivacion", "motivation")),
    ("extra", ("dependencia", "dependenc", "relacion con", "related", "tamano", "size")),
]
CORE = ("problem", "root_cause", "changes", "edge_cases", "testing")
KEEP = CORE + ("extra",)
PREAMBLE_MAX = 600


def _norm(title: str) -> str:
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()


def kind_of(title: str) -> str:
    t = _norm(title)
    for kind, keys in KINDS:
        if any(k in t for k in keys):
            return kind
    return "other"


def parse(body: str) -> tuple[list[str], list[dict]]:
    """(preamble lines, sections [{heading, title, kind, lines}]); fenced code never starts a section."""
    pre: list[str] = []
    secs: list[dict] = []
    fence = False
    for line in (body or "").splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        m = None if fence else HEADING.match(line)
        if m and not line.strip().startswith("```"):
            title = (m.group(2) or m.group(3) or "").strip()
            secs.append({"heading": line, "title": title, "kind": kind_of(title), "lines": []})
        elif secs:
            secs[-1]["lines"].append(line)
        else:
            pre.append(line)
    return pre, secs


def _render(pre: list[str], secs: list[dict]) -> str:
    out = "\n".join(pre).strip()
    for s in secs:
        text = "\n".join(s["lines"]).strip("\n")
        if text.count("```") % 2:
            text += "\n```"
        out += ("\n\n" if out else "") + s["heading"] + ("\n" + text if text.strip() else "")
    return out.strip()


def _drop_order(s: dict) -> list[int]:
    """Indices to remove from a section, last first; in Testing the lines with numbers (counts) go last."""
    first = next((i for i, l in enumerate(s["lines"]) if l.strip()), None)
    idx = [i for i in range(len(s["lines"])) if i != first]  # the first real line of a kept section always stays
    if s["kind"] == "testing":
        return sorted(idx, key=lambda i: (bool(re.search(r"\d", s["lines"][i])), -i))
    return sorted(idx, key=lambda i: -i)


def trim(body: str, limit: int = LIMIT) -> tuple[str, str]:
    """(body for the PR, overflow for the case report). A body at or under `limit` is returned untouched."""
    body = (body or "").strip()
    if len(body) <= limit:
        return body, ""
    pre, secs = parse(body)
    moved: list[str] = []
    kept = [s for s in secs if s["kind"] in KEEP]
    for s in secs:
        if s["kind"] not in KEEP:
            moved.append(s["heading"] + "\n" + "\n".join(s["lines"]).strip("\n"))
    if kept and len("\n".join(pre)) > PREAMBLE_MAX:
        # a long intro before the first section: keep its first paragraph, the rest is detail
        para = "\n".join(pre).strip().split("\n\n")
        first = para[0][:PREAMBLE_MAX]
        moved.insert(0, "\n".join(pre).strip()[len(first):].strip())
        pre = [first]
    if not secs:  # no structure at all: keep the head, move the tail
        cut = body.rfind("\n", 0, limit - 2)
        cut = cut if cut > limit // 2 else limit - 2
        return body[:cut].rstrip() + " …", body[cut:].strip()
    cuts: dict[str, list[str]] = {}
    text = _render(pre, kept)
    while len(text) > limit:
        cand = [s for s in kept if _drop_order(s)]
        if not cand:
            break
        s = max(cand, key=lambda x: (x["kind"] not in CORE, len("\n".join(x["lines"]))))  # extras shrink first
        i = _drop_order(s)[0]
        cuts.setdefault(s["title"], []).insert(0, s["lines"][i])
        del s["lines"][i]
        text = _render(pre, kept)
    if len(text) > limit:  # single huge lines left: hard cut the longest at a word boundary
        while len(text) > limit:
            s = max(kept, key=lambda x: max((len(l) for l in x["lines"]), default=0))
            j = max(range(len(s["lines"])), key=lambda k: len(s["lines"][k])) if s["lines"] else None
            if j is None or len(s["lines"][j]) < 40:
                text = text[:limit - 2].rstrip() + " …"
                break
            line = s["lines"][j]
            keep_n = max(20, len(line) - (len(text) - limit) - 2)
            keep_n = line.rfind(" ", 0, keep_n) if line.rfind(" ", 0, keep_n) > 20 else keep_n
            cuts.setdefault(s["title"], []).append(line[keep_n:].strip())
            s["lines"][j] = line[:keep_n].rstrip() + " …"
            text = _render(pre, kept)
    for title, lines in cuts.items():
        moved.append(f"### {title} " + site.t("(cut from the PR)", "(recortado del PR)") + "\n" + "\n".join(lines))
    return text, "\n\n".join(m for m in moved if m.strip())
