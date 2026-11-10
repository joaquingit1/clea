"""Text the engine writes comes out in the install's language.

A Spanish string literal in these modules has to sit inside site.t(en, es) or site.own(first, neutral), so an English
install gets the English side and the first install keeps its Spanish byte for byte. Comments, docstrings, log() calls
and regexes are free. Anything else that is Spanish on purpose (a marker the engine reads back from messages it already
posted, a value the UI maps, a word list for parsing people's input) goes in ALLOWED with the reason it stays.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MODULES = ("console", "digest", "approvals", "report", "comms", "dm", "board", "actions", "ingest", "cases", "alerts",
           "outcomes", "epics", "stale", "impact", "escalate", "closing", "babysit", "slack", "dedupe", "backfill",
           "merge_order", "cover", "outbound", "db", "chat", "voice")

# Wide on purpose: a false hit costs one allowlist line, a miss ships Spanish to an English install.
SPANISH = re.compile(
    r"[áéíóúñ¿¡]|\b(?:que|para|del|los|las|una|pero|cuando|hay|está|esta|con|por|sin|ya|lo|el|la|en|de|se|mando|hilo|"
    r"caso|hecho|listo|respondi\w*|pendiente|revisar|borrador\w*|todavía|acá|vos|sos|mirá|dale|mismo|misma|promesa|"
    r"nuevo|nueva|abiertos?|cerrados?|hilos?|rojo|verde|corriendo|listos?|aprobado|tuyos?|te|tu|nada|algo|alguien|"
    r"nunca|ahora|hoy|todo|todos|mergeo|sobre|hace|desde|entre|porque|sigue|seguimos|equipo|pedido|falta|ninguno|"
    r"ninguna)\b", re.I)

_DB_STOP = "stop word for matching titles people wrote in Spanish; never shown"
_FIRST_MARK = ("marker of a message the first install already posted, read back from the DM; the English marker sits "
               "beside it behind site.t")
_UI_CAUSE = "cause value stored on the case; ui/src maps probado/probable/sin confirmar/no aplica to labels"

# (module, literal) -> why it stays Spanish outside site.t
ALLOWED = {
    ("console", "SÍ"): "first-line verdict token of the quick answer protocol (SI/NO), never shown",
    ("console", "sin confirmar"): _UI_CAUSE,
    ("report", "sin confirmar"): _UI_CAUSE,
    ("digest", "CI rojo"): "opening of babysit's stored block reason; read back next to 'CI red'",
    ("digest", "mié"): "weekday abbreviation, applied only when site.lang() is es",
    ("digest", "sáb"): "weekday abbreviation, applied only when site.lang() is es",
    ("comms", "no completed CodeRabbit pass|CodeRabbit CLI no completó"):
        "pattern matching the CodeRabbit gate reason tasks.py writes, in either language",
    ("dm", "Pendientes tuyos"): _FIRST_MARK,
    ("dm", "Te dejé"): _FIRST_MARK,
    ("dm", "Qué encontré"): _FIRST_MARK,
    ("dm", "lo mando"): _FIRST_MARK,
    ("slack", "del"): "slack.sh subcommand name (delete), not text",
    ("dedupe", "mismo monitor"): "opening of a dedupe note the engine stored before; read back next to 'same monitor'",
    ("dedupe", "mismo thread"): "opening of a dedupe note the engine stored before; read back next to 'same thread'",
    ("dedupe", "frenado por noticia"): "opening of impact's stop note; read back next to 'stopped by news'",
    ("backfill", "sin asignar"): "assignee value as Granola writes it in Spanish meetings (input)",
    **{("db", w): _DB_STOP for w in ("para", "con", "los", "las", "del", "que", "una", "por", "sobre", "sin", "esta",
                                     "más", "desde", "entre", "revisar")},
}

# English sides of site.t that are input patterns: they accept the Spanish form too, on English installs only
ENGLISH_SIDE_ALLOWED = {
    ("comms", "la reproducción no quedó demostrada|no pude reproducirlo|reproduction (?:was )?not "
              "(?:shown|demonstrated|proven)|could(?: not|n't) reproduce"):
        "matches repro.py's blocked reason, which is still Spanish (repro.py is not converted)",
    ("dedupe", r"^\((?:slack|audit|daily|sin respuesta|unanswered)\)\s*|^(?:patrol|alerta|alert|pedido repetido|"
               r"repeated request):\s*"):
        "strips title prefixes the engine stored in either language (triage.py still writes 'pedido repetido:')",
}


def _text(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _call_name(node: ast.Call) -> tuple[str, str]:
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    base = getattr(getattr(fn, "value", None), "id", "")
    return base, name


def bare_spanish(src: str) -> list[tuple[int, str]]:
    """Spanish literals outside site.t/site.own, docstrings, log() and re.* calls."""
    tree = ast.parse(src)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)):
            skip.add(id(node.body[0].value))
        if isinstance(node, ast.Call):
            base, name = _call_name(node)
            if (base == "site" and name in ("t", "own")) or name == "log" or base in ("re", "_re"):
                skip.update(id(sub) for sub in ast.walk(node))
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)  # the f-string is judged whole, not piece by piece
    out = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        s = _text(node)
        if s and SPANISH.search(s):
            out.append((node.lineno, s))
    return out


def english_sides(src: str) -> list[tuple[int, str]]:
    """The first (English) argument of every site.t call, when it is a literal."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and _call_name(node) == ("site", "t") and node.args:
            s = _text(node.args[0])
            if s is not None:
                out.append((node.lineno, s))
    return out


def _src(mod: str) -> str:
    return (ROOT / "ap" / f"{mod}.py").read_text()


@pytest.mark.parametrize("mod", MODULES)
def test_no_bare_spanish_literal(mod):
    bad = [f"ap/{mod}.py:{line}: {s[:120]!r}" for line, s in bare_spanish(_src(mod)) if (mod, s) not in ALLOWED]
    assert not bad, ("Spanish text outside site.t/site.own; wrap it, or add it to ALLOWED with the reason it stays:\n"
                     + "\n".join(bad))


@pytest.mark.parametrize("mod", MODULES)
def test_english_side_has_no_spanish(mod):
    # the sim's detector flags these words in an English install's output
    sim = re.compile(r"[áéíóúñ¿¡]|\b(?:que|para|del|los|las|una|pero|cuando|hay|hilo|mando|mandar|mensaje|respuesta|"
                     r"borrador|listo|texto|sigue|pregunta|esperando|promesa|novedad|detalle|revisar|caso|hecho|"
                     r"pendiente|ya|redactaba|nuevo|nueva|sin respuesta|lo rehago|en el|de la|por el|te dej[eé]|"
                     r"aprobado|descartado|cerrado|enviado|respondido|verificando|falta|listo para|con eso|a las)\b",
                     re.I)
    bad = [f"ap/{mod}.py:{line}: {s[:120]!r}" for line, s in english_sides(_src(mod))
           if sim.search(s) and (mod, s) not in ENGLISH_SIDE_ALLOWED]
    assert not bad, "the English side of site.t reads as Spanish:\n" + "\n".join(bad)


def test_allowlists_hold_only_live_entries():
    found = {(m, s) for m in MODULES for _, s in bare_spanish(_src(m))}
    stale = [k for k in ALLOWED if k not in found]
    assert not stale, f"ALLOWED entries no longer in the code, drop them: {stale}"
    found_en = {(m, s) for m in MODULES for _, s in english_sides(_src(m))}
    stale_en = [k for k in ENGLISH_SIDE_ALLOWED if k not in found_en]
    assert not stale_en, f"ENGLISH_SIDE_ALLOWED entries no longer in the code, drop them: {stale_en}"


def test_detector_catches_a_new_bare_literal():
    src = ('from . import site\n'
           'def f(n):\n'
           '    """Docstrings may say lo mando."""\n'
           '    log("lo mando")\n'
           '    ok = site.t("send it", "lo mando")\n'
           '    return f"listo, {n} casos"\n')
    assert [s for _, s in bare_spanish(src)] == ["listo, {} casos"]
