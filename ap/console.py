"""The harness console: a local app.

Read-only JSON API over the harness DB plus the built UI (ui/dist), served on 127.0.0.1:4848. Zero tokens. Started
from hot.tick; the handler resolves this module at call time, so a hot reload changes the API without a restart.

  GET /api/board            the main screen: what needs him, ready PRs, what runs now, what got done and why
  GET /api/threads          every case as a thread (phase, headline, last news)
  GET /api/thread/<id>      one case as a story: origin, why, events, draft, PRs, reproduction, cost
  GET /api/activity         the latest things the harness did, with the reason
  GET /api/chats            his conversations with the orchestrator from the app
  GET /api/chat/<conv>      one conversation (and whether the orchestrator is still thinking)
  GET /api/config           the workspace (GitHub org, Jira site) the UI links to
  GET /api/notifications?after=<id>  notifications for Clea.app to show (ready-to-merge PRs); marks the app as polling
  POST /api/case/<id>/<go|retry|drop|approve_review>  {text?}: what he can do on a case that needs him (ap/actions.py);
                            approve_review waits on Touch ID, polled at GET /api/case/<id>/approve_review
  POST /api/chat            {conv?, text, item?}: talk to the orchestrator, like the Slack DM ("the app should also have
                            an interface to talk to the main agent, like we would on slack", 2026-10-20)

Writes only through the chat actions (chat._act), the same ones his DM can trigger; ✅ asks stay in Slack, where the
guard verifies them. A POST must carry X-AP-Console and a local Host, so no web page in his browser can drive it.
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import db, site
from .core import CFG, DATA, HOME, PROMPTS, VAR, log, now

PORT = int(os.environ.get("CLEA_PORT") or (CFG.get("console") or {}).get("port") or 4848)
DIST = HOME / "ui" / "dist"
_S = sys.modules.setdefault("ap._console_state", type(sys)("ap._console_state"))

ACTIVE = ("investigate", "telemetry", "plan", "critique", "build", "gate", "self_find", "self_review", "fix", "publish",
          "ci_fix", "verify_done", "pr_body_refresh", "announce", "announce_check", "reply_draft", "reply_check",
          "review_prepare", "review_find", "review_verify", "review_post", "comments_triage", "comments_verify",
          "comments_reply", "vet", "propose", "propose_check")
WAITING = ("ci_wait", "waiting", "loop_track")
STAGE = {  # what the stage means, in his words
    "investigate": site.t("Investigating", "Investigando"), "telemetry": site.t("Looking at production", "Mirando producción"),
    "plan": site.t("Reproducing and planning", "Reproduciendo y planeando"),
    "critique": site.t("Reviewing the plan", "Revisando el plan"), "build": site.t("Writing the fix", "Programando el fix"),
    "gate": site.t("Running the checks", "Corriendo los checks"),
    "self_find": site.t("Looking for problems in its own PR", "Buscando problemas en su propio PR"),
    "self_review": site.t("Reviewing itself", "Revisándose"), "fix": site.t("Fixing", "Corrigiendo"),
    "publish": site.t("Opening the PR", "Abriendo el PR"), "ci_wait": site.t("Waiting for CI", "Esperando el CI"),
    "ci_fix": site.t("Fixing CI", "Arreglando el CI"),
    "verify_done": site.t("Checking that it is fixed", "Verificando que quedó resuelto"),
    "pr_body_refresh": site.t("Updating the PR", "Actualizando el PR"), "announce": site.t("Writing the announcement", "Armando el aviso"),
    "announce_check": site.t("Reviewing the announcement", "Revisando el aviso"),
    "reply_draft": site.t("Writing the reply", "Escribiendo la respuesta"), "reply_check": site.t("Reviewing the reply", "Revisando la respuesta"),
    "review_prepare": site.t("Preparing the review", "Preparando la review"), "review_find": site.t("Reading the PR", "Leyendo el PR"),
    "review_verify": site.t("Checking the findings", "Verificando los hallazgos"),
    "review_post": site.t("Ready for your ok", "Lista para tu ok"), "comments_triage": site.t("Reading the comments", "Leyendo los comentarios"),
    "comments_verify": site.t("Checking comments", "Verificando comentarios"),
    "comments_reply": site.t("Answering comments", "Respondiendo comentarios"), "waiting": site.t("Waiting", "Esperando"),
    "loop_track": site.t("Following a promise", "Siguiendo una promesa"),
    "vet": site.t("Vetting", "Validando"), "propose": site.t("Writing the proposal", "Armando la propuesta"),
    "propose_check": site.t("Reviewing the proposal", "Revisando la propuesta"),
    "awaiting_approval": site.t("Waiting for your ok", "Espera tu ok"), "awaiting_human": site.t("Waiting for your decision", "Espera tu decisión"),
    "blocked": site.t("Stuck", "Trabado"),
    # the UI strips a leading "Hecho" or "Done" from the last note (Board.tsx), so this label must stay one of those
    "done": site.t("Done", "Hecho"), "dropped": site.t("Dropped", "Descartado"), "expired": site.t("Expired", "Venció"),
    "failed": site.t("Failed", "Falló"),
}
# the UI's sidebar matches the "Review" kind, which reads the same in both languages
KIND = {"TASK": "Bug", "INCIDENT": site.t("Incident", "Incidente"), "IDEA": "Idea", "REPLY": site.t("Reply", "Respuesta"), "REVIEW": "Review",
        "CI": "CI", "COMMENTS": site.t("Comments", "Comentarios"), "NUDGE": site.t("Reminder", "Recordatorio"),
        "OPEN_LOOP": site.t("Promise", "Promesa")}


def _phase(it: dict, has_ask: bool, cache: dict | None = None) -> str:
    """you / running / waiting (on others or on CI) / done / closed, from stale.what_it_needs: the same truth the
    digest and the DM chat use, verified against the PR states, not the stored status alone."""
    s = it["status"]
    if s in ("done", "dropped", "expired", "failed"):
        return "done" if s == "done" else "closed"
    if s == "blocked":
        return "you"
    from . import stale
    try:
        bucket = stale.what_it_needs(it, cache if cache is not None else {})[0]
    except Exception:
        bucket = "you" if has_ask or s in ("awaiting_approval", "awaiting_human") else "progress"
    if bucket == "done":
        return "done"
    if bucket == "you":
        return "you"
    if bucket == "review":
        return "waiting"
    return "running" if s in ACTIVE else "waiting"


def _plain(text: str) -> str:
    from . import comms
    return comms.plain(text or "")


def _name(it: dict) -> str:
    """The case in his words. PR babysitting cases are named by what happens to the PR, not 'ci cometa#3057: fix(...)'."""
    from . import comms
    m = re.match(r"^(ci|review|comments)\s+([\w-]+#\d+):\s*(?:\w+(?:\([^)]*\))?!?:\s*)?(.*)$", it["title"] or "", re.I)
    if m and it["kind"] in ("CI", "REVIEW", "COMMENTS"):
        what = {"ci": site.t("CI for", "CI de"), "review": site.t("Review of", "Review de"),
                "comments": site.t("Comments on", "Comentarios en")}[m.group(1).lower()]
        subj = m.group(3)
        if it.get("repo") and it.get("pr"):  # the stored title is cut at 90 chars: the PR's own title when we have it
            r = db.q1("SELECT data FROM prs WHERE repo=? AND number=?", it["repo"], it["pr"])
            if r:
                full = json.loads(r["data"] or "{}").get("title") or ""
                subj = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", full) or subj
            elif len(it["title"]) >= 95:
                subj = subj.rsplit(" ", 1)[0] + "…"  # cut mid-word at storage time
        subj = comms.plain(subj).rstrip(" (")
        return f"{what} {m.group(2)}: {subj[:1].lower() + subj[1:]}" if subj else f"{what} {m.group(2)}"
    d = it.get("data") or {}
    if not d.get("card_headline"):
        if d.get("gist"):  # the case's own summary ("Respuesta a a teammate sobre...") beats the raw message
            return comms.plain(d["gist"])[:110]
        raw = re.sub(r"^\(slack\)\s*", "", it["title"] or "")
        if raw.startswith("@"):  # a raw Slack message: who wrote it, without the @mentions it opens with
            body = re.sub(r"^(?:@[\wÁÉÍÓÚáéíóúñÑ.]+(?:\s[A-ZÁÉÍÓÚ][\wáéíóúñ]+)?(?:\s\([^)]*\))?[\s,:]*)+", "", raw).strip()
            who = (_author(d.get("author")) or "").split(" ")[0]
            body = re.sub(r"<https?://[^|>\s]+\|([^>]+)>", r"\1", body)  # Slack links keep their label
            body = re.sub(r"<?https?(?::\S*)?$|<?https?://\S+", "", body).strip(" .,:;")  # a raw or cut link says nothing
            body = re.sub(_DANGLING, "", body)  # nor does the word the cut left dangling
            body = comms.plain(body)[:100] or raw[:100]
            return f"{who}: {body}" if who and not who.startswith("U0") else body
    try:
        return comms.name(it)
    except Exception:
        return it["title"]


# the small words a cut message can end on, in the install's language
_DANGLING = site.t(r"\s+(a|an|the|to|in|of|on|with|and|or)$", r"\s+(a|en|de|con|el|la)$")


def _origin(it: dict) -> str:
    from . import comms
    try:
        return comms.origin(it).rstrip(".")
    except Exception:
        return ""


def _prs(it: dict) -> list[dict]:
    out, seen = [], set()
    for p in (it["data"].get("parts") or []) + (it["data"].get("linked_prs") or []):
        url = p.get("pr_url")
        if url and url not in seen:
            seen.add(url)
            m = re.search(r"/([\w-]+)/pull/(\d+)", url)
            st = db.kv_get(f"pr:{m.group(1)}#{m.group(2)}") if m else {}
            s = (st or {}).get("summary") or {}
            out.append({"url": url, "label": f"{m.group(1)}#{m.group(2)}" if m else url,
                        "ci": "green" if s.get("green") else "red" if s.get("failed") else "running" if s.get("pending") else None,
                        "approved": bool(s.get("approved")), "linked": p in (it["data"].get("linked_prs") or [])})
    if it.get("pr") and it.get("repo") and not out:
        url = site.pr_url(it["repo"], it["pr"])
        out.append({"url": url, "label": f"{it['repo']}#{it['pr']}", "ci": None, "approved": False, "linked": False})
    return out


def _asks() -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for a in db.q("SELECT id, item_id, ref, content, created_at FROM artifacts WHERE kind='approval' AND outcome IS NULL"):
        try:
            c = json.loads(a["content"] or "{}")
        except Exception:
            continue
        p = c.get("payload") or {}
        if isinstance(p, str):
            try:
                import ast
                p = ast.literal_eval(p)
            except Exception:
                p = {}
        out.setdefault(a["item_id"], []).append(_ask_view(a, c, p))
    return out


def _ask_view(a, c: dict, p: dict) -> dict:
    from . import slack
    kind = c.get("kind")
    where = ""
    if p.get("channel"):
        try:
            where = slack.channel_name(p["channel"])
        except Exception:
            where = p["channel"]
    pr = f"{p.get('repo', '')}#{p.get('pr', '')}"
    what = {"slack_send": (site.t(f"Send this message {'in the DM' if where == 'DM' else 'to ' + where}", f"Mandar este mensaje en {where}") if where
                           else site.t("Send this message", "Mandar este mensaje")),
            "github_review": site.t(f"Leave this review on {pr}", f"Dejar esta review en {pr}"),
            "github_replies": site.t("Answer the reviewers", "Responder a los reviewers")}.get(kind, kind or site.t("Approve", "Aprobar"))
    comments = [{"path": x.get("path"), "line": x.get("line"), "body": x.get("body")} for x in (p.get("comments") or [])]
    return {"id": a["id"], "kind": kind, "what": what, "text": p.get("text") or p.get("body") or "", "comments": comments,
            "slack": site.slack_link(CFG["self_dm_channel"], a["ref"]) if a["ref"] else None,
            "at": a["created_at"], "expired": bool(c.get("expired_at"))}


def _last_note(item_id: int) -> tuple[str, int]:
    r = db.q1("SELECT note, at, dst FROM transitions WHERE item_id=? ORDER BY rowid DESC LIMIT 1", item_id)
    if not r:
        return "", 0
    return _humanize(r["note"] or "", r["dst"]), r["at"]


NOTES = [  # the machine notes that recur, in his words (most frequent first, 2026-10-20)
    (r"^created( dropped)?:?\s*", ""),
    (r"^approved and executed$", site.t("you approved it and it went out", "aprobaste y se publicó")),
    (r"^CI green after fix$", site.t("CI went green after the fix", "el CI quedó verde después del arreglo")),
    (r"^PR MERGED$|^pr_merged$", site.t("the PR was merged", "el PR se mergeó")),
    (r"^verdict delivered to the parent case$", site.t("passed its verdict to the main case", "le pasó el veredicto al caso principal")),
    (r"^gate: fix rounds exhausted$|^self_review: fix rounds exhausted$",
     site.t("the checks kept failing after two fixes", "los checks siguieron fallando después de dos arreglos")),
    (r"^telemetry first.*$", site.t("looks at production first", "primero mira producción")),
    (r"^dossier: propose \((\w+)\)$", site.t(r"proposes a reply (\1)", r"propone una respuesta (\1)")),
    (r"^dossier: build \((\w+)\)$", site.t(r"decided to write the fix (\1)", r"decidió programar el fix (\1)")),
    (r"^dossier: drop \((\w+)\).*$", site.t(r"nothing needs doing (\1)", r"no hace falta hacer nada (\1)")),
    (r"^dossier: ask \((\w+)\)$", site.t(r"a fact is missing: asking for it (\1)", r"falta un dato: lo pregunta (\1)")),
    (r"^rejudge under new rules$", site.t("redid it under the new rules", "lo rehízo con las reglas nuevas")),
    (r"^new message, redrafting over the whole conversation$",
     site.t("a new message came in: redrafting the reply", "llegó un mensaje nuevo: rehace la respuesta")),
    (r"^redraft with the verified facts$", site.t("redrafting the reply with the verified facts", "rehace la respuesta con los datos verificados")),
    (r"^(\d+) bot threads? answered and resolved$",
     site.t(r"answered and resolved \1 bot comment(s)", r"respondió y resolvió \1 comentario(s) de bots")),
    (r"^(rerun: )?base_breakage$|^base breakage: branch updated$",
     site.t("the base branch was broken: retrying on an up to date base", "la base estaba rota: reintenta con la base al día")),
    (r"^(rerun: )?infra$", site.t("CI infrastructure failure: retrying", "falla de infraestructura del CI: reintenta")),
    (r"^watchdog: retry after block$", site.t("retrying after getting stuck", "reintenta después de trabarse")),
    (r"^critic: revise \(rounds exhausted\), building with notes$",
     site.t("the reviewer asked for changes: writing it with those notes", "el revisor pidió cambios: programa con esas notas")),
    (r"^PR exists: deliver with caveats$", site.t("the PR exists: delivering it with caveats", "el PR ya existe: lo entrega con salvedades")),
    (r"^gates: env errors only, CI will verify$",
     site.t("the errors came from the environment: CI will verify", "los errores eran del entorno: verifica el CI")),
    (r"^PR body regenerated$", site.t("updated the PR description", "actualizó la descripción del PR")),
    (r"^head moved before posting$",
     site.t("the PR changed before the review was posted: redoing it", "el PR cambió antes de publicar la review: la rehace")),
    (r"^vet: real \((\w+)\)$", site.t("it is a real request", "es un pedido real")),
    (r"^outcome audit: bug with a cause builds$", site.t("a bug with a known cause: writing the fix", "bug con causa: lo programa")),
    (r"^repro changed: one more round$", site.t("the reproduction changed: one more round", "cambió la reproducción: una ronda más")),
    (r"^an Alfred feature request is product's call$",
     site.t("it is a feature request: product decides", "es un pedido de feature: lo decide producto")),
]


def note_es(note: str) -> str:
    n = re.sub(r"\s+", " ", note or "").strip()
    for pat, rep in NOTES:
        if re.search(pat, n, re.I):
            n = re.sub(pat, rep, n, flags=re.I).strip()
            break
    n = re.sub(r"^(dossier|gates|publish|fix|critic|reaper|escalate|pregate)\s*:\s*", "", n, flags=re.I)
    n = n.replace("(proven)", site.t("(proven)", "(probado)")).replace("(unknown)", site.t("(unconfirmed)", "(sin confirmar)"))
    return n[:1].upper() + n[1:] if n else ""


def _humanize(note: str, dst: str) -> str:
    """A transition note in his words: the stage meaning, plus the note without machinery."""
    n = note_es(note)
    base = STAGE.get(dst, dst)
    return f"{base}: {n[:220]}" if n else base


def thread_summary(it: dict, asks: dict, cache: dict | None = None, prmap: dict | None = None, detail: bool = False) -> dict:
    d = it["data"]
    note, at = _last_note(it["id"])
    a = asks.get(it["id"], [])
    nxt = _next(it, a, prmap if prmap is not None else _prmap(), cache, detail)
    phase = _phase(it, bool(a), cache)
    if nxt and nxt["who"] == "you":
        phase = "you"
    elif phase == "you":  # 'you' only with something he can do: otherwise who it really waits on
        phase = "running" if it["status"] in ACTIVE else "waiting"
    return {"id": it["id"], "kind": KIND.get(it["kind"], it["kind"]), "status": it["status"], "stage": STAGE.get(it["status"], it["status"]),
            "phase": phase, "headline": _name(it), "origin": _origin(it), "ticket": d.get("jira"),
            "need": _need_line(nxt), "next": nxt,
            "last": note, "updated": it["updated_at"] or at,
            "created": it["created_at"], "urgency": it["urgency"], "prs": _prs(it), "asks": len(a)}


def _need_line(nxt: dict | None) -> str:
    """The one line the board and the sidebar show under a case that needs him."""
    if not nxt or nxt["who"] != "you":
        return ""
    if nxt["kind"] == "need":
        return nxt["detail"]
    if nxt["kind"] == "merge":
        return f"{nxt['title']}: {', '.join(p['label'] for p in nxt['prs'])}"
    line = f"{nxt['title']}: {nxt['detail']}"[:220] if nxt["detail"] else nxt["title"]
    if nxt["prs"]:
        # an ask on a case whose PR is ready: the card says both, or a mergeable PR hides behind 'Mandar este mensaje'
        #
        ready = ", ".join(p["label"] for p in nxt["prs"])
        rest = f"{line[0].lower()}{line[1:]}"
        line = site.t(f"{ready} ready to merge · {rest}", f"{ready} listo para mergear · {rest}")
    return line


def _prmap() -> dict:
    """His PRs as the board shows them, by url: the last snapshot, never a fresh 30 s pass."""
    snap = getattr(_S, "snap", None) or {}
    return {p["url"].rstrip("/"): p for p in (snap.get("board") or {}).get("prs") or []}


def _pr_row(p: dict) -> dict:
    return {k: p.get(k) for k in ("label", "url", "repo", "number", "state", "why", "approval", "head", "title", "after", "ci", "threads")}


def _next(it: dict, asks: list, prmap: dict, cache: dict | None = None, detail: bool = False) -> dict | None:
    """What happens next on the case and who has to move: you (with the actions he can take), others, or Clea. Every
    case that needs him says what for and offers something to press.
    actions: go (dale) / go_with (dale con indicaciones) / retry / retry_with / reply (chat) / drop / approve_review."""
    d, s = it["data"], it["status"]
    mine = [prmap[p["url"].rstrip("/")] for p in _prs(it) if p["url"].rstrip("/") in prmap]
    merge = [_pr_row(p) for p in mine if p["state"] == "merge"]
    if s in db.TERMINAL:
        if detail and it["kind"] == "REVIEW" and s == "done" and d.get("clean") and not d.get("approved_by_him"):
            from . import stale
            if stale._state(it["repo"], it["pr"], cache if cache is not None else {}) == "OPEN":
                return {"who": "none", "kind": "approve_review",
                        "title": site.t("The review found no problems", "La review no encontró problemas"),
                        "detail": site.t("Nothing waits on you. If you agree, approve it on GitHub from here.",
                                         "No te espera. Si te cierra, aprobalo en GitHub desde acá."), "actions": ["approve_review"], "prs": []}
        return None
    if asks:
        return {"who": "you", "kind": "ask", "title": asks[0]["what"], "detail": "", "actions": [], "prs": merge}
    if s == "blocked":
        return {"who": "you", "kind": "blocked", "title": site.t("It got stuck and will not go on by itself", "Se trabó y no sigue sola"),
                "detail": _plain(re.sub(r"/Users/\S+", site.t("that folder", "esa carpeta"), d.get("blocked_reason") or ""))[:700],
                "actions": ["retry", "retry_with", "reply", "drop"], "prs": merge}
    need = (d.get("card_need") or "").strip()
    if s == "awaiting_human" and need and it["kind"] != "REVIEW":
        return {"who": "you", "kind": "need", "title": site.t("Needs your ok", "Necesito tu ok"), "detail": _plain(need),
                "actions": ["go", "go_with", "reply", "drop"], "prs": merge}
    if merge:
        return {"who": "you", "kind": "merge", "title": site.t("Ready to merge", "Listo para mergear") if len(merge) == 1
                else site.t("Ready to merge", "Listos para mergear"),
                "detail": "", "actions": [], "prs": merge}
    held = [p for p in mine if p["state"] in ("decide", "blocked", "draft")]
    if held:
        title = {"decide": site.t("Decide the merge order", "Decidí el orden de merge"),
                 "blocked": site.t("The PR is stuck", "El PR está trabado"),
                 "draft": site.t("The PR is a draft: open it for review", "El PR es borrador: abrilo para review")}
        return {"who": "you", "kind": "pr", "title": title[held[0]["state"]], "detail": held[0]["why"],
                "actions": ["reply"], "prs": [_pr_row(p) for p in held]}
    others = [p for p in mine if p["state"] in ("review", "queued", "working")]
    if s in ACTIVE:
        return {"who": "clea", "kind": "running", "title": STAGE.get(s, s), "detail": "", "actions": [], "prs": []}
    if others:
        return {"who": "others", "kind": "waiting", "title": site.t("Waiting on others", "Espera a otros"),
                "detail": " · ".join(f"{p['label']}: {p['why']}" for p in others[:3]), "actions": [], "prs": []}
    if s in ("awaiting_human", "awaiting_approval") and it["kind"] != "REVIEW":
        return {"who": "you", "kind": "decide", "title": site.t("Waiting for your decision", "Espera tu decisión"),
                "detail": _plain(d.get("gist") or d.get("card_headline") or ""),
                "actions": ["go", "go_with", "reply", "drop"], "prs": []}
    return {"who": "clea", "kind": "waiting", "title": STAGE.get(s, s),
            "detail": _plain((d.get("waiting_for") or "").split(":", 1)[-1])[:200], "actions": [], "prs": []}


def threads(limit: int = 300, prmap: dict | None = None) -> list[dict]:
    asks = _asks()
    prmap = prmap if prmap is not None else _prmap()
    # a review that did not end done never happened as far as he is concerned: not listed
    rows = db.q("SELECT id FROM items WHERE kind != 'NUDGE' AND (status NOT IN ('done','dropped','expired','failed') OR updated_at > ?) "
                "AND NOT (kind='REVIEW' AND status IN ('dropped','expired','failed')) "
                "ORDER BY updated_at DESC LIMIT ?", now() - 7 * 86400, limit)
    cache = _S.__dict__.setdefault("pr_states", _TTL(PR_STATE_TTL))
    return [thread_summary(db.item(r["id"]), asks, cache, prmap) for r in rows]


PR_STATE_TTL = 900


class _TTL(dict):
    """stale.what_it_needs' PR-state cache, kept across refreshes: one `gh pr view` per PR per 15 min, not per refresh
    (a cold pass is ~70 calls and 25-45 s)."""

    def __init__(self, ttl: int):
        super().__init__()
        self.ttl, self.at = ttl, {}

    def __contains__(self, k):
        return super().__contains__(k) and time.time() - self.at.get(k, 0) < self.ttl

    def __setitem__(self, k, v):
        self.at[k] = time.time()
        super().__setitem__(k, v)


class _NoWait:
    """The PR-state cache as a click sees it: the last known state, fresh or not, and never a `gh pr view` inline. A
    missing or expired state is fetched in the background, so the case's next poll (5 s) has it."""

    def __init__(self, ttl: _TTL):
        self.ttl = ttl

    def __contains__(self, k):
        return True

    def __getitem__(self, k):
        if k not in self.ttl:
            _fetch_state(self.ttl, k)
        return dict.get(self.ttl, k, "?")

    def __setitem__(self, k, v):
        self.ttl[k] = v


def _fetch_state(ttl: _TTL, k) -> None:
    pending = _S.__dict__.setdefault("state_fetch", set())
    if k in pending:
        return
    pending.add(k)

    def run():
        from . import stale
        try:
            stale._state(k[0], k[1], ttl)
        finally:
            pending.discard(k)
    threading.Thread(target=run, name="console-pr-state", daemon=True).start()


def _cost(item_id: int) -> float:
    r = db.q1("SELECT SUM(usd) s FROM calls WHERE item_id=?", item_id)
    return round(float(r["s"] or 0), 2)


def _events(it: dict) -> list[dict]:
    """The case as a story: each step with what it means and why."""
    out = []
    for r in db.q("SELECT at, src, dst, note FROM transitions WHERE item_id=? ORDER BY at, rowid", it["id"]):
        note = re.sub(r"\s+", " ", r["note"] or "").strip()
        if not note and r["src"] == r["dst"]:
            continue
        out.append({"at": r["at"], "stage": r["dst"], "title": STAGE.get(r["dst"], r["dst"]), "note": note_es(note)[:600]})
    return out


def thread(item_id: int) -> dict | None:
    it = db.item(item_id)
    if not it:
        return None
    d = it["data"]
    ds = d.get("dossier") or {}
    asks = _asks()
    base = thread_summary(it, asks, _NoWait(_S.__dict__.setdefault("pr_states", _TTL(PR_STATE_TTL))), detail=True)
    rep = d.get("repro") or {}
    tech = {"problem": ds.get("problem") or d.get("summary") or "", "cause": ds.get("root_cause") or "",
            "recommendation": ds.get("recommendation") or ""}
    if not d.get("card_analysis") and any(tech.values()) and not d.get("explain_tried"):
        _explain_later(it)
    return {**base,
            "bullets": d.get("card_bullets") or _bullets(d.get("report_text") or ""),
            "analysis": d.get("card_analysis") or "",
            "cause": d.get("card_cause") or {"proven": "probado", "probable": "probable", "hypothesis": "probable",
                                             "unknown": "sin confirmar"}.get(ds.get("root_cause_status") or "", ""),
            "technical": tech if any(tech.values()) else None,
            "source": {"text": (d.get("text") or "")[:3000], "channel": d.get("channel"),
                       "link": _permalink(d), "author": _author(d.get("author"))},
            "report": _reword(d, (d.get("report_text") or "")[-6000:]),
            "detail": _reword(d, (d.get("detail_pending") or "")[:6000]),
            "asks": asks.get(item_id, []),
            "repro": {"verdict": rep.get("verdict"), "files": rep.get("test_files") or [], "failure": rep.get("expected_failure"),
                      "green": bool(rep.get("green_at"))} if rep else None,
            "caveats": d.get("caveats") or [], "blocked": d.get("blocked_reason"),
            "events": _events(it), "cost": _cost(item_id),
            "updates": [{"text": (u.get("text") or "")[:800]} for u in (d.get("updates") or [])[-12:]]}


def _bullets(report: str) -> list[str]:
    """The card's bullets from the case message, for cases reported before card_bullets was stored."""
    return [_plain(m)[:300] for m in re.findall(r"^[•·-]\s+(.+)$", report, re.M)][:3]


_OLD_BLIND = re.compile(r"⚠️ La telemetría no vio todo, sus ceros no prueban nada:[^\n]*")


def _reword(d: dict, text: str) -> str:
    """Texts stored before the production-gaps line was reworded (report.blind_es) read the new way."""
    if not _OLD_BLIND.search(text):
        return text
    from . import report
    return _OLD_BLIND.sub(report.blind_es(d.get("telemetry_blind") or []) or "", text)


def _explain_later(it: dict) -> None:
    """report.explain in the background, once per case: the page shows the bullets until it lands. One at a time,
    newest click first: opening cases fast used to start one Sonnet process per case at once."""
    db.update_item(it["id"], data={"explain_tried": now()})
    q = _S.__dict__.setdefault("explain_q", [])
    with _S.__dict__.setdefault("explain_lock", threading.Lock()):
        if it["id"] in q:
            return
        q.append(it["id"])
        if any(t.name == "console-explain" and t.is_alive() for t in threading.enumerate()):
            return
    threading.Thread(target=_explain_loop, name="console-explain", daemon=True).start()


def _explain_loop() -> None:
    from . import report
    q, lock = _S.explain_q, _S.explain_lock
    while True:
        with lock:
            if not q:
                return
            item_id = q.pop()  # the last opened is the one he is looking at
            dropped = q[:-EXPLAIN_BACKLOG]  # cases opened in passing and left behind are not worth a call now
            del q[:-EXPLAIN_BACKLOG]
        for d in dropped:
            db.update_item(d, data={"explain_tried": None})  # opening it again asks again
        try:
            report.explain(db.item(item_id))
        except Exception as e:
            log("console: explain failed", item=item_id, err=f"{type(e).__name__}: {e}"[:200])


EXPLAIN_BACKLOG = 3


def _permalink(d: dict) -> str | None:
    if d.get("channel") and (d.get("thread_ts") or d.get("ts")):
        return site.slack_link(d["channel"], d.get("thread_ts") or d.get("ts"))
    return None


def _author(uid: str | None) -> str | None:
    if not uid:
        return None
    try:
        from . import slack
        return slack.user_name(uid)
    except Exception:
        return uid


def board() -> dict:
    from . import digest, stale
    own = digest._own_prs()
    prs = _own_pr_states(own)  # first: each case's next step reads its PRs' states
    ts = threads(prmap={p["url"].rstrip("/"): p for p in prs})
    _S.last_threads = ts
    needs = [t for t in ts if t["phase"] == "you"]
    needs.sort(key=lambda t: -(t["created"] or 0))  # newest first = most recently opened
    running = [t for t in ts if t["phase"] == "running"]
    waiting = [t for t in ts if t["phase"] == "waiting"]
    since = now() - 2 * 86400
    shipped = [t for t in ts if t["phase"] == "done" and (t["updated"] or 0) > since]
    dropped = [t for t in ts if t["status"] in ("dropped",) and (t["updated"] or 0) > since]
    try:
        stacked = digest._stacked_on_mine(own)
    except Exception:
        stacked = []
    return {"at": now(), "greeting": _greeting(), "needs": needs, "running": running, "waiting": waiting,
            "shipped": shipped, "dropped": dropped, "prs": prs,
            "blocking": [_strip_slack(s) for s in stacked], "stats": _stats(), "health": _health()}


def _own_pr_states(own: dict) -> list[dict]:
    """His open PRs with the digest's exact rules: merge (green, approved, no threads), review (green, no threads, needs a
    human approve), draft, blocked (needs him), working (CI, threads, deploy wait). Each carries the case that made it."""
    from . import digest
    case, repro, ticket = {}, {}, {}
    for r in db.q("SELECT id, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA')"):
        d = json.loads(r["data"])
        for p in d.get("parts", []):
            if p.get("pr_url"):
                case[p["pr_url"].rstrip("/")] = r["id"]
                repro[p["pr_url"].rstrip("/")] = _repro_view(d.get("repro"))
                if d.get("jira"):
                    ticket[p["pr_url"].rstrip("/")] = d["jira"]
    out = []
    for (repo, n), info in sorted(own.items()):
        st, s, pr = info["st"], info["st"].get("summary") or {}, info["pr"]
        url = site.pr_url(repo, n)
        threads = st.get("unresolved_threads") or 0
        rows = db.q("SELECT kind, status, data FROM items WHERE repo=? AND pr=? AND status NOT IN ('done','dropped','expired','failed') "
                    "AND kind IN ('CI','COMMENTS')", repo, n)
        waiting_for = next((json.loads(x["data"]).get("waiting_for") for x in rows if x["status"] == "waiting"), None)
        blocked = db.q1("SELECT 1 FROM items WHERE repo=? AND pr=? AND status='blocked'", repo, n)
        deps = [f"{d['dep_repo']}#{d['dep_number']}" for d in db.q("SELECT dep_repo, dep_number FROM merge_deps WHERE repo=? AND number=?", repo, n)]
        if st.get("blocked") or blocked:
            state, why = "blocked", re.sub(r"/Users/\S+", site.t("that branch", "esa branch"),
                                            st.get("blocked") or site.t("I could not unblock it, see its case", "no lo pude destrabar, mirá su caso"))
        elif s.get("green") and threads == 0 and not pr.get("isDraft") and s.get("review") == "APPROVED" \
                and not _real_block(st.get("merge_block")) and not s.get("conflicting"):
            # the ruleset is satisfied: by a person (green Merge) or by CodeRabbit's approve alone (orange Merge)
            state, why = "merge", (site.t("Green, approved by the team", "Verde, aprobado por el equipo") if s.get("approved")
                                   else site.t("Green, approved only by CodeRabbit", "Verde, solo con el approve de CodeRabbit"))
        elif s.get("green") and threads == 0 and not pr.get("isDraft") and s.get("review") == "APPROVED" \
                and re.search(r"CYCLE|needs a human", st.get("merge_block") or ""):
            # approved and green, held only by an order no rule can settle: his call, never "en curso" (10-21: cometa#2648,
            # approved, sat under "en curso" for days behind a cycle with cometa#2900, a second PR for the same bug)
            state, why = "decide", _cycle_es(repo, n, st.get("merge_block") or "")
        elif s.get("green") and threads == 0 and not pr.get("isDraft") and s.get("review") == "APPROVED" and deps:
            state, why = "queued", site.t("Approved, ", "Aprobado, ") + _order_es(st.get("merge_block") or "")
        elif s.get("green") and threads == 0 and not pr.get("isDraft") and s.get("review") == "APPROVED":
            state, why = "working", site.t("Approved, ", "Aprobado, ") + _order_es(st.get("merge_block") or "")
        elif s.get("green") and threads == 0 and not pr.get("isDraft"):
            state, why = "review", site.t("Green, needs an approve", "Verde, falta un approve")
        elif s.get("green") and threads == 0:
            state, why = "draft", site.t("Green draft, needs your ok to open it", "Borrador verde, falta tu ok para abrirlo")
        else:
            state = "working"
            dep = waiting_for.split(':', 1)[-1].strip()[:60] if waiting_for else ""
            why = (site.t(f"Waiting for the deploy: {dep}", f"Espera el deploy: {dep}") if waiting_for else
                   site.t("Fixing CI", "Arreglando el CI") if rows and s.get("failed") else
                   site.t(f"{threads} review thread{'s' if threads != 1 else ''}", f"{threads} hilo{'s' if threads != 1 else ''} de review")
                   if threads else site.t("CI running", "CI corriendo") if s.get("pending") else site.t("In progress", "En curso"))
        bf = db.kv_get(f"repro_pr:{repo}#{n}")  # a reproduction written after the PR (repro.backfill_pr)
        if bf and not (repro.get(url) or {}).get("e2e"):
            repro[url] = _repro_view(bf)
        out.append({"label": f"{repo}#{n}", "url": url, "title": digest._pr_subject(pr), "state": state, "why": _plain(why),
                    "repo": repo, "number": n, "head": pr.get("headRefOid"), "repro": repro.get(url), "ticket": ticket.get(url),
                    "updated": max(_epoch(pr.get("updatedAt")), (db.item(case[url]) or {}).get("updated_at", 0) if case.get(url) else 0),
                    "approval": "human" if s.get("approved") else "coderabbit" if s.get("review") == "APPROVED" else None,
                    "after": deps, "created": pr.get("createdAt"), "case": case.get(url),
                    "ci": "green" if s.get("green") else "red" if s.get("failed") else "running" if s.get("pending") else None,
                    "threads": threads})
    ready = {r["label"] for r in out if r["state"] == "merge"}
    for r in out:  # a queued PR whose predecessors are all ready only waits on his Merge of them
        if r["state"] == "queued" and r["after"] and all(a in ready for a in r["after"]):
            first = ', '.join(r['after'])
            r["why"] = site.t(f"Approved; merge {first} first and it is ready", f"Aprobado; mergeá primero {first} y queda listo")
    out.sort(key=lambda r: r.get("created") or "", reverse=True)  # newest first = most recently OPENED (10-20: an old PR
    # sorted by last change jumped to the top every time main was merged into it)
    return out


def _real_block(block: str | None) -> bool:
    """A merge-order hold that is real. GitHub's 'merge state UNKNOWN' is not one: it computes mergeability lazily and
    fixes itself, and the guard re-checks the PR (and GitHub refuses an unmergeable head) at merge time."""
    if not block:
        return False
    rest = [b.strip() for b in block.split(";") if b.strip() and not re.match(r"merge state UNKNOWN$", b.strip())]
    return bool(rest)


def _epoch(iso: str | None) -> int:
    try:
        import datetime as dt
        return int(dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _repro_view(rec: dict | None) -> dict | None:
    """Did the fix prove itself against the bug? End to end = the reproduction test failed before the fix and passed on
    the fix's final head."""
    if not rec:
        return None
    files = rec.get("test_files")
    if isinstance(files, str):
        files = re.findall(r"[\w./-]+\.(?:test|spec)\.[\w]+|[\w./-]+test_[\w]+\.py", files) or [files]
    return {"e2e": rec.get("verdict") == "reproduced" and bool(rec.get("green_at")), "verdict": rec.get("verdict"),
            "kind": rec.get("kind"), "files": (files or [])[:3], "failure": (rec.get("expected_failure") or "")[:240],
            "why": (rec.get("why") or "")[:400], "red_at": rec.get("at"), "green_at": rec.get("green_at"),
            "green_head": rec.get("green_head"), "backfill": bool(rec.get("backfill")), "branch": rec.get("branch"),
            "green_fail": (rec.get("green_fail") or "")[-600:] or None, "green_on": rec.get("green_on"),
            "rejected": (rec.get("rejected") or "")[:400] or None, "question": (rec.get("question") or "")[:400] or None}


def _cycle_es(repo: str, n: int, block: str) -> str:
    """A merge-order cycle in his words: what the other PR is to this one and that he picks which goes first (or which
    one stays, when both fix the same bug)."""
    others = list(dict.fromkeys(re.findall(r"dependency CYCLE with ([\w-]+#\d+)", block)))
    if not others:
        return site.t("Approved, but the merge order needs your call", "Aprobado, pero el orden de merge necesita que decidas")
    reasons = " ".join(r["reason"] or "" for r in db.q("SELECT reason FROM merge_deps WHERE (repo=? AND number=?) OR "
                                                         "(dep_repo=? AND dep_number=?)", repo, n, repo, n))
    if re.search(r"same bug|mismo bug|duplicate|duplicad", reasons, re.I):
        return site.t(f"Approved, but {', '.join(others)} fixes the same bug: decide which one to keep",
                      f"Aprobado, pero {', '.join(others)} arregla el mismo bug: decidí con cuál te quedás")
    return site.t(f"Approved, but it clashes with {', '.join(others)} (they touch the same files): decide which goes first",
                  f"Aprobado, pero choca con {', '.join(others)} (tocan los mismos archivos): decidí cuál va primero")


def _order_es(block: str) -> str:
    """merge_order's reason in his words: what has to merge first, or that the order needs his call."""
    if not block:
        return site.t("but it has conflicts with the base branch", "pero tiene conflictos con la base")
    cyc = re.search(r"dependency CYCLE with ([\w-]+#\d+)", block)
    if cyc:
        return site.t(f"but it depends in a circle with {cyc.group(1)}: someone has to decide which goes first",
                      f"pero depende en círculo con {cyc.group(1)}: hay que decidir cuál va primero")
    first = re.findall(r"needs ([\w-]+#\d+) (?:not merged|merged|deployed)", block)
    if first:
        after = ', '.join(dict.fromkeys(first))
        return site.t(f"goes after {after}", f"va después de {after}")
    req = re.search(r"required check (.+?) never ran", block)
    if req:
        return site.t(f"but the required check {req.group(1)} never ran on this PR (it was added later): I update the "
                      f"branch so it runs, and CodeRabbit approves again",
                      f"pero el check obligatorio {req.group(1)} nunca corrió en este PR (llegó después, con el CI unificado): "
                      f"actualizo la rama para que corra y CodeRabbit vuelve a aprobar")
    if "migration" in block:
        return site.t("but it has a migration: it follows the deploy order", "pero trae una migración: va con el orden de deploy")
    st = re.search(r"merge state (\w+)", block)
    if st:
        return site.t(f"but GitHub has not confirmed it can be merged yet (state {st.group(1)}); this updates by itself",
                      f"pero GitHub todavía no confirma que se pueda mergear (estado {st.group(1)}); se actualiza solo")
    return site.t("but it waits on the merge order", "pero espera el orden de merge")


def _strip_slack(line: str) -> dict:
    m = re.search(r"<(https?://[^|>]+)\|([^>]+)>", line)
    rest = re.sub(r"^•\s*<[^>]+>\s*·\s*", "", line)
    return {"url": m.group(1) if m else None, "label": m.group(2) if m else "", "text": rest}


def _greeting() -> str:
    h = time.localtime().tm_hour
    return (site.t("Good morning", "Buen día") if h < 13 else site.t("Good afternoon", "Buenas tardes") if h < 20
            else site.t("Good evening", "Buenas noches"))


def _stats() -> dict:
    week = now() - 7 * 86400
    return {"prs_week": db.q1("SELECT COUNT(*) n FROM transitions WHERE src='publish' AND dst IN ('ci_wait','awaiting_human','announce') AND at>?", week)["n"],
            "done_week": db.q1("SELECT COUNT(DISTINCT item_id) n FROM transitions WHERE dst='done' AND at>?", week)["n"],
            "cost_today": round(float(db.q1("SELECT SUM(usd) s FROM calls WHERE at>?", now() - 86400)["s"] or 0), 2),
            "open": db.q1("SELECT COUNT(*) n FROM items WHERE status NOT IN ('done','dropped','expired','failed') AND kind != 'NUDGE'")["n"]}


def _health() -> dict:
    from . import claude
    h = db.kv_get("hot_reload") or {}
    try:
        wrong = claude.wrong_account()
    except Exception:
        wrong = None
    pu = db.kv_get("claude_paused_until", 0) or 0
    return {"rev": h.get("rev"), "paused_until": pu if pu > now() else None, "wrong_account": wrong,
            "worker_beat": db.kv_get("worker_beat") or db.kv_get("hot_loaded_at")}


def activity(limit: int = 80) -> list[dict]:
    out = []
    for r in db.q("SELECT t.item_id, t.at, t.src, t.dst, t.note, i.kind FROM transitions t JOIN items i ON i.id=t.item_id "
                  "WHERE i.kind != 'NUDGE' ORDER BY t.at DESC LIMIT ?", limit):
        it = db.item(r["item_id"])
        out.append({"id": r["item_id"], "at": r["at"], "stage": r["dst"], "title": STAGE.get(r["dst"], r["dst"]),
                    "headline": _name(it), "note": note_es(r["note"] or "")[:300], "kind": KIND.get(r["kind"], r["kind"])})
    return out


# ---- chat with the orchestrator -------------------------------------------------------------------------------------
def chat_prompt() -> str:
    """How the thinker is told who is talking and from where. The label examples are the install's own repo and ticket
    names, so the model copies a shape that exists here."""
    who = site.first_name()
    repo = site.primary_repo() or "repo"
    prefix = (site.ticket_prefixes() or ["TICKET"])[0]
    thread = site.t("thread", "hilo")
    return (f"{who} is talking to you, Clea (the agent that runs his autonomous engineering harness), from the Clea app on his "
            f"Mac, not Slack. Same job and same tools as in his DM. Answer in Markdown: short paragraphs, bullets "
            f"when listing, links as [label](url) with short labels ({repo}#2383, {prefix}-1479, {thread}).")


def _ensure_table() -> None:
    if not getattr(_S, "table", False):
        db.x("CREATE TABLE IF NOT EXISTS console_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT, role TEXT, text TEXT, "
             "at INTEGER, item_id INTEGER, meta TEXT)")
        _S.table = True


def chats(limit: int = 40) -> list[dict]:
    _ensure_table()
    rows = db.q("SELECT conv, MIN(id) first, MAX(at) last, MAX(item_id) item_id, COUNT(*) n FROM console_msgs "
                "GROUP BY conv ORDER BY last DESC LIMIT ?", limit)
    out = []
    for r in rows:
        f = db.q1("SELECT text FROM console_msgs WHERE id=?", r["first"])
        out.append({"conv": r["conv"], "title": re.sub(r"\s+", " ", f["text"] if f else "")[:80], "at": r["last"],
                    "item": r["item_id"], "n": r["n"], "thinking": r["conv"] in _thinking()})
    return out


def _thinking() -> dict:
    return _S.__dict__.setdefault("thinking", {})


def conversation(conv: str) -> dict:
    _ensure_table()
    msgs = [{"id": r["id"], "role": r["role"], "text": r["text"], "at": r["at"], "meta": json.loads(r["meta"] or "{}")}
            for r in db.q("SELECT * FROM console_msgs WHERE conv=? ORDER BY id", conv)]
    t = _thinking().get(conv)
    return {"conv": conv, "messages": msgs, "thinking": bool(t), "since": (t or {}).get("since") if isinstance(t, dict) else t,
            "step": (t or {}).get("step") if isinstance(t, dict) else None}


def say(conv: str | None, text: str, item: int | None = None) -> str:
    _ensure_table()
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    conv = conv or f"app:{int(time.time() * 1000)}"
    if item is None:
        r = db.q1("SELECT item_id FROM console_msgs WHERE conv=? AND item_id IS NOT NULL LIMIT 1", conv)
        item = r["item_id"] if r else None
    db.x("INSERT INTO console_msgs (conv, role, text, at, item_id) VALUES (?,?,?,?,?)", conv, "user", text[:20000], now(), item)
    gen = _S.__dict__.setdefault("gen", {})
    gen[conv] = gen.get(conv, 0) + 1
    _thinking()[conv] = {"since": now(), "step": THINKING, "steps": 0}
    threading.Thread(target=_answer_safe, args=(conv, item, gen[conv]), name="console-chat", daemon=True).start()
    return conv


THINKING = site.t("Thinking", "Pensando")  # the UI shows its own word for either form (Chat.tsx)


def _answer_safe(conv: str, item: int | None, my: int) -> None:
    lk = _S.__dict__.setdefault("conv_locks", {}).setdefault(conv, threading.Lock())
    with lk:  # one thinker per conversation; a newer message answers the whole burst
        if _S.gen.get(conv) != my:
            return
        try:
            sys.modules["ap.console"]._answer(conv, item, my)
        except Exception as e:
            from . import claude
            if isinstance(e, (claude.ClaudeLimited, claude.BudgetExhausted)):
                pu = db.kv_get("claude_paused_until", 0) or 0
                at = time.strftime('%H:%M', time.localtime(pu))
                when = site.t(f" It comes back at {at}.", f" Vuelve a las {at}.") if pu > now() else ""
                _store(conv, "system", site.t(f"No quota to think about it right now.{when} Write to me again once it is back.",
                                              f"Sin cuota para pensarlo ahora.{when} Escribime de nuevo cuando vuelva."), item)
            else:
                log("console chat failed", conv=conv, err=f"{type(e).__name__}: {e}"[:200])
                _store(conv, "system", site.t(f"I got stuck processing it ({type(e).__name__}). Try again.",
                                              f"Se me trabó al procesarlo ({type(e).__name__}). Probá de nuevo."), item)
        finally:
            if _S.gen.get(conv) == my:
                _thinking().pop(conv, None)


def _store(conv: str, role: str, text: str, item: int | None, meta: dict | None = None) -> None:
    db.x("INSERT INTO console_msgs (conv, role, text, at, item_id, meta) VALUES (?,?,?,?,?,?)",
         conv, role, text, now(), item, json.dumps(meta or {}, ensure_ascii=False))


QUICK = {"type": "object", "required": ["confident", "answer"], "properties": {
    "confident": {"type": "boolean", "description": "true only when the STATE fully answers him"},
    "answer": {"type": "string", "description": "the answer in Markdown when confident, else empty"}}}
TRANSCRIPTS = Path.home() / ".claude" / "projects" / re.sub(r"[/.]", "-", str(DATA))


def _quick_state() -> str:
    """What the board knows, compact: enough to answer 'what are you doing / what is open / what needs me'."""
    snap = snapshot()
    b, ts = snap["board"], snap["threads"]
    line = lambda t: (f"- [{t['phase']}] {t['headline']} ({t['kind']}, {t['stage']}; {t['last'][:120]})"
                      + (f" PRs: {', '.join(p['label'] for p in t['prs'])}" if t["prs"] else "") + (f" | needs: {t['need'][:120]}" if t["need"] else ""))
    groups = {k: [t for t in ts if t["phase"] == k] for k in ("running", "you", "waiting")}
    prs = "\n".join(f"- {p['label']} [{p['state']}] {p['title']}: {p['why']} ({p['url']})" for p in b["prs"])
    done = "\n".join(line(t) for t in sorted(b["shipped"], key=lambda t: -t["updated"])[:12])
    return (f"NOW: {time.strftime('%a %d %H:%M')}. Claude paused until: {b['health'].get('paused_until') or 'no'}.\n"
            f"WORKING NOW ({len(groups['running'])}):\n" + "\n".join(line(t) for t in groups["running"]) +
            f"\nNEEDS {site.first_name().upper()} ({len(groups['you'])}, newest first):\n" + "\n".join(line(t) for t in groups["you"][:25]) +
            f"\nWAITING ON CI/DEPLOY/REVIEWERS ({len(groups['waiting'])}):\n" + "\n".join(line(t) for t in groups["waiting"][:15]) +
            f"\nHIS OPEN PRS ({len(b['prs'])}):\n{prs}\nRESOLVED IN 48H:\n{done}\nSTATS: {json.dumps(b['stats'])}")


READY_MAX_AGE = 1500  # a pre-started session older than this is replaced (the CLI's auth and config may have moved)


class _QuickSession:
    """One pre-started Haiku session that answers ONE question and is thrown away. Thinking is off (it cost 10 s on 'decí ok'), and the answer streams."""

    def __init__(self):
        from . import claude, providers
        from .core import env
        self.p = subprocess.Popen(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--model", providers.model_id("haiku"), "--tools", "", "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--setting-sources", "",
             "--no-session-persistence", "--system-prompt-file", claude._fast_file("console_fast")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            start_new_session=True, env=env({**providers.claude_env(), "MAX_THINKING_TOKENS": "0"}))
        self.born = time.time()

    def ready(self) -> bool:
        return self.p.poll() is None and time.time() - self.born < READY_MAX_AGE

    def stream(self, text: str, timeout: float = 40):
        """Yields ('text', delta) as Haiku writes, then ('result', event)."""
        self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("quick session ended")
            e = json.loads(line)
            if e.get("type") == "stream_event":
                d = (e.get("event") or {}).get("delta") or {}
                if d.get("type") == "text_delta":
                    yield "text", d.get("text", "")
            elif e.get("type") == "result":
                if e.get("is_error"):
                    raise RuntimeError(str(e.get("result"))[:200])
                yield "result", e
                return
        raise TimeoutError("quick answer timeout")

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


def _quick_ok() -> bool:
    """The instant tier is a pre-started Claude Code session: only an install that thinks with Claude, and is set up."""
    from . import providers, site
    return site.setup_complete() and providers.engine() == "claude"


def _spawn_ready() -> None:
    if getattr(_S, "quick_spawning", False) or not _quick_ok():
        return
    _S.quick_spawning = True

    def start():
        try:
            _S.quick_ready = _QuickSession()
        except Exception as e:
            log("console quick session failed to start", err=str(e)[:100])
        finally:
            _S.quick_spawning = False
    threading.Thread(target=start, name="console-quick-warm", daemon=True).start()


def _keep_ready() -> None:
    """Every tick: a started session is waiting (after a reload, a crash or its age limit), so no question starts cold."""
    sess = getattr(_S, "quick_ready", None)
    if sess is None or not sess.ready():
        if sess is not None:
            sess.close()
            _S.quick_ready = None
        _spawn_ready()


def _take_session() -> "_QuickSession":
    """The pre-started session, and a new one starting for the next question."""
    with _S.__dict__.setdefault("quick_lock", threading.Lock()):
        sess = getattr(_S, "quick_ready", None)
        _S.quick_ready = None
        _spawn_ready()
    if sess is None or not sess.ready():
        if sess is not None:
            sess.close()
        sess = _QuickSession()  # none ready (first question after a restart): a cold one, once
    return sess


ACTION = re.compile(r"\b(cre[aá]|arm[aá]|abr[ií]|mand[aá]|envi[aá]|public[aá]|descart[aá]|borr[aá]|cerr[aá]|prioriz[aá]|paus[aá]|"
                    r"reanud[aá]|fren[aá]|par[aá]|corr[eé]|reencol[aá]|merge[aá]|aprob[aá]|rechaz[aá]|respond[eé]|contest[aá]|"
                    r"escrib[ií]|avis[aá]|record[aá]me|hac[eé]|arregl[aá]|investig[aá]|revis[aá]|mir[aá]|fij[aá]te|cheque[aá])\b", re.I)


# the same in English: an imperative at the start of a sentence, or asked for ("please", "can you", "go ahead and")
ACTION_EN = re.compile(
    r"(?:^|[.!?;:]\s+|\b(?:please|pls|can you|could you|would you|will you|go ahead and|i need you to|i want you to|you should|"
    r"and then|then)\s+)(?:please\s+)?(?:create|open|make|send|post|publish|discard|drop|delete|close|prioriti[sz]e|pause|resume|"
    r"stop|run|rerun|retry|requeue|merge|approve|reject|reply|answer|write|tell|notify|remind me|fix|investigate|review|"
    r"look (?:at|into)|check|take|start|cancel|ship|revert)\b", re.I | re.M)


def wants_action(text: str) -> bool:
    """An instruction, not a question about the board: the quick tier cannot act, so it never answers one."""
    return bool(ACTION.search(text or "") or ACTION_EN.search(text or ""))


def _quick(convo: str, on_text=None, on_ack=None, force_no: bool = False) -> dict | None:
    """Haiku, and only here: the deep answer stays on the orchestrator's model. First line SI or
    NO. SI: the answer streams through on_text. NO: one line that takes up what he asked and says Clea will think it
    through, through on_ack; then the full agent answers."""
    if not _quick_ok():
        return None  # no warm Claude session on this install: the full agent answers
    t0 = time.time()
    prompt = (f"STATE:\n{_quick_state()}\n\nCONVERSATION (answer his last message):\n{convo}\n\n"
              + ("This is an instruction to act or to look into something: your first line is NO." if force_no
                 else "First line SI or NO, as your instructions say."))
    sess = _take_session()
    buf, verdict, ev = "", None, {}
    try:
        for kind, val in sess.stream(prompt):
            if kind == "result":
                ev = val
                break
            buf += val
            if verdict is None and "\n" in buf:
                verdict = buf.split("\n", 1)[0].strip().upper().strip("*: ")
            if verdict and (verdict.startswith("SI") or verdict.startswith("SÍ")) and on_text and not force_no:
                on_text(buf.split("\n", 1)[1].strip())
    except Exception as e:
        log("console quick answer failed", err=f"{type(e).__name__}: {e}"[:160])
        return None
    finally:
        sess.close()
    u = ev.get("usage") or {}
    db.record_call("claude", "console_fast", None, True, time.time() - t0, float(ev.get("total_cost_usd") or 0),
                   u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                   u.get("output_tokens", 0), "haiku one-shot")
    verdict = verdict or buf.strip().split("\n", 1)[0].strip().upper()
    ok = (verdict.startswith("SI") or verdict.startswith("SÍ")) and not force_no
    rest = buf.split("\n", 1)[1].strip() if "\n" in buf else ""
    if not ok and rest and on_ack:
        on_ack(rest.split("\n")[0].strip())
    return {"confident": ok and bool(rest), "answer": rest if ok else "", "ack": "" if ok else rest}


_KB = CFG.get("knowledge_base") or {}  # the company's own knowledge base, when the install has one (name, tool, tools)
STEP = [  # what a tool call means, in his words, for the live progress line
    (r"sqlite3", site.t("Checking the case database", "Revisando la base de casos")),
    (r"\bgh (pr|api|search|run|issue)", site.t("Looking at GitHub", "Mirando GitHub")),
    (r"\bgit (log|show|diff|grep)", site.t("Looking at the code history", "Mirando el historial del código")),
    (r"var/log|\.log\b", site.t("Reading the logs", "Leyendo los logs")),
    (r"\b(rg|grep)\b", site.t("Searching the code", "Buscando en el código")), (r"Slack", site.t("Reading Slack", "Leyendo Slack")),
    (r"grafana", site.t("Looking at production in Grafana", "Mirando producción en Grafana")),
    (r"Redshift", site.t("Querying production data", "Consultando los datos de producción")),
    *([(re.escape(_KB["tool"]), site.t(f"Querying {_KB['name']}", f"Consultando {_KB['name']}"))] if _KB.get("tool") and _KB.get("name") else []),
    (r"Sentry", site.t("Looking at Sentry", "Mirando Sentry")), (r"Metabase", site.t("Querying Metabase", "Consultando Metabase")),
]


def _step(name: str, inp: dict) -> str:
    blob = f"{name} {json.dumps(inp, ensure_ascii=False)[:400]}"
    for pat, label in STEP:
        if re.search(pat, blob, re.I):
            return label
    if name == "Read":
        f = Path(str(inp.get('file_path', ''))).name
        return site.t(f"Reading {f or 'a file'}", f"Leyendo {f or 'un archivo'}")
    if name in ("Grep", "Glob"):
        return site.t("Searching the code", "Buscando en el código")
    if name == "ToolSearch":
        return site.t("Getting tools ready", "Preparando herramientas")
    return site.t("Working", "Trabajando")


def _follow(conv: str, sid: str, stop: threading.Event) -> None:
    """Tail the session transcript and publish each tool call as a step (Codex's live 'Ran …, Read …' line)."""
    path, pos, seen = TRANSCRIPTS / f"{sid}.jsonl", 0, 0
    while not stop.wait(0.8):
        try:
            if not path.exists():
                continue
            with path.open() as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except Exception:
            continue
        for ln in chunk.splitlines():
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            for c in ((ev.get("message") or {}).get("content") or []) if ev.get("type") == "assistant" else []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    seen += 1
                    t = _thinking().get(conv)
                    if t:
                        t.update(step=_step(c.get("name", ""), c.get("input") or {}), steps=seen)


def _answer(conv: str, item: int | None, my: int) -> None:
    """Like the agent apps: an acknowledgement at once, a quick answer from the board when that is enough (seconds),
    and only otherwise the full agent with tools, with its steps shown live."""
    from . import chat, claude
    t0 = time.time()
    hist = db.q("SELECT role, text, meta FROM console_msgs WHERE conv=? ORDER BY id DESC LIMIT 24", conv)[::-1]
    convo = "\n".join(f"{site.first_name().upper() if h['role'] == 'user' else 'CLEA'}: {h['text'][:2000]}" for h in hist
                       if h["role"] != "system" and not json.loads(h["meta"] or "{}").get("ack"))
    last = next((h["text"] for h in reversed(hist) if h["role"] == "user"), "")
    _thinking()[conv] = {"since": now(), "step": THINKING, "steps": 0}
    quick: dict = {}

    def ack():
        try:
            text = chat._fast(last, "")["reply"].strip()
            if text and _S.gen.get(conv) == my:
                _store(conv, "agent", text, item, {"ack": True})
        except Exception as e:
            log("console ack failed", err=f"{type(e).__name__}: {e}"[:120])

    def own_ack(text: str):  # Haiku's NO line: what he asked, in its words, and that Clea is on it
        if text and _S.gen.get(conv) == my:
            quick["acked"] = True
            _store(conv, "agent", text, item, {"ack": True})

    qt = None
    live: dict = {}

    def on_text(text: str):  # the answer appears as Haiku writes it, one row updated in place
        if not text or _S.gen.get(conv) != my:
            return
        quick["r"] = quick.get("r") or "streaming"  # an ack still in flight is dropped from here on
        if "row" not in live:
            live["row"] = db.x("INSERT INTO console_msgs (conv, role, text, at, item_id, meta) VALUES (?,?,?,?,?,?)",
                               conv, "agent", text, now(), item, json.dumps({"fast": True, "streaming": True}))
            live["t"] = 0
        elif time.time() - live["t"] > 0.12:
            db.x("UPDATE console_msgs SET text=? WHERE id=?", text, live["row"])
            live["t"] = time.time()

    if not item:  # a question about one case goes straight to the agent that can open the case
        act = wants_action(last)
        qt = threading.Thread(target=lambda: quick.update(r=_quick(convo, on_text, own_ack, force_no=act)), daemon=True)
        qt.start()
    else:
        threading.Thread(target=ack, daemon=True).start()  # a case question goes straight to the agent
    if qt is not None:
        _thinking()[conv].update(step=site.t("Looking at the board", "Mirando el tablero"))
        qt.join(60)
        r = quick.get("r")
        if isinstance(r, dict) and r.get("confident") and (r.get("answer") or "").strip():
            if _S.gen.get(conv) != my:
                return
            meta = json.dumps({"fast": True, "secs": max(1, round(time.time() - t0))})
            if "row" in live:
                db.x("UPDATE console_msgs SET text=?, meta=? WHERE id=?", r["answer"].strip(), meta, live["row"])
            else:
                _store(conv, "agent", r["answer"].strip(), item, json.loads(meta))
            log("console quick answer", conv=conv, secs=round(time.time() - t0))
            return
        if "row" in live:  # it started answering, then the stream failed: the full agent answers instead
            db.x("DELETE FROM console_msgs WHERE id=?", live["row"])
        if not quick.get("acked"):  # the quick reply failed before saying anything: the voice says it is on it
            ack()
    case = ""
    if item:
        it = db.item(item)
        if it:
            case = (f"THIS CONVERSATION IS ABOUT CASE #{it['id']} ({it['kind']}, {it['status']}): {it['title']}. Its folder is "
                    f"var/items/{it['id']}/ and its rows in items/transitions/artifacts. Start from it; do the analysis with your "
                    f"tools when he asks for it.\n\n")
    prompt = (f"{chat_prompt()}\n\nCONVERSATION SO FAR (his last message is the one to answer):\n{convo}\n\n{case}"
              f"HARNESS STATE NOW:\n{chat._state_summary()}\n\n"
              f"Verify with your tools before answering (DB, logs, gh, Slack): the summary is a starting point, not the truth. "
              f"Answer his question or confirm what you will do; turn instructions into actions. Only cases marked NEEDS=YOU "
              f"need him. Complaints or corrections about the harness itself become an 'incident'. Changing what needs his ✅ "
              f"is not yours to do: tell him the one command. Never promise what no action does.")
    sid = db.kv_get(f"console_sid:{conv}")
    import uuid
    follow_sid = sid or str(uuid.uuid4())
    stop = threading.Event()
    threading.Thread(target=_follow, args=(conv, follow_sid, stop), daemon=True).start()
    _thinking()[conv].update(step=THINKING)
    try:
        try:
            out = claude.orchestrator(prompt, cwd=str(DATA), schema=chat.SCHEMA, session=sid, item_id=item, caller="console",
                                      new_session_id=None if sid else follow_sid)
        except claude.ClaudeError:
            if not sid:
                raise
            follow_sid = str(uuid.uuid4())  # an expired session starts fresh, and the progress follows the new one
            stop.set()
            stop = threading.Event()
            threading.Thread(target=_follow, args=(conv, follow_sid, stop), daemon=True).start()
            out = claude.orchestrator(prompt, cwd=str(DATA), schema=chat.SCHEMA, item_id=item, caller="console",
                                      new_session_id=follow_sid)
    finally:
        stop.set()
    if out.get("_session"):
        db.kv_set(f"console_sid:{conv}", out["_session"])
    done = []
    for a in out.get("actions", [])[:6]:
        try:
            done.append(chat._act(a))
        except Exception as e:
            done.append(f"{a.get('type')} failed: {str(e)[:120]}")
    if _S.gen.get(conv) != my:
        log("console chat: newer message arrived, answer dropped", conv=conv)
        return
    acts = [d for d in done if d != "noted"]
    reply = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"[\2](\1)", out.get("reply", "").strip())  # Slack links to Markdown
    steps = (_thinking().get(conv) or {}).get("steps", 0)
    _store(conv, "agent", reply or site.t("Done, nothing to add.", "Listo, nada más que agregar."), item, {"actions": acts, "secs": round(time.time() - t0), "steps": steps})
    log("console chat answered", conv=conv, actions=acts, secs=round(time.time() - t0), steps=steps)


# ---- approvals from the app ------------------------------------------------------------------------------------------
def decide(art_id: int, decision: str, text: str = "") -> dict:
    """Approve, discard or ask for changes on an ask from the app. An approval waits on his Touch ID in the guard (up to
    2.5 min), so it runs in the background and the app polls ask_state."""
    st = _S.__dict__.setdefault("asks", {})
    if (st.get(art_id) or {}).get("state") == "waiting":
        return st[art_id]
    st[art_id] = {"state": "waiting" if decision == "approve" else "working", "decision": decision, "at": now()}

    def run():
        from . import approvals
        try:
            out = approvals.decide_local(art_id, decision, text)
            st[art_id] = {"state": "done", "decision": decision, "outcome": out, "at": now()}
        except Exception as e:
            msg = str(e)
            declined = "local approval" in msg
            st[art_id] = {"state": "declined" if declined else "error", "decision": decision, "at": now(),
                          "error": site.t("Not sent: you did not confirm with Touch ID.", "No se envió: no confirmaste con Touch ID.")
                          if declined else msg[:300]}
            log("console ask failed", art=art_id, decision=decision, err=msg[:200])
        threading.Thread(target=refresh, daemon=True).start()  # the board moves on without waiting a pass

    threading.Thread(target=run, name="console-ask", daemon=True).start()
    return st[art_id]


def ask_state(art_id: int) -> dict:
    st = (_S.__dict__.get("asks") or {}).get(art_id)
    if st:
        return st
    r = db.q1("SELECT outcome FROM artifacts WHERE id=?", art_id)
    return {"state": "done" if r and r["outcome"] else "open", "outcome": r["outcome"] if r else None}


# ---- merging his PRs from the app -------------------------------------------------------------------------------------
def merge(repo: str, n: int) -> dict:
    """Squash-merge one of his ready PRs from the app. Only a PR the board shows as ready, at the head it
    showed; the guard asks for Touch ID, and GitHub refuses a head that moved since."""
    key = f"{repo}#{n}"
    st = _S.__dict__.setdefault("merges", {})
    if (st.get(key) or {}).get("state") == "waiting":
        return st[key]
    pr = next((p for p in snapshot()["board"]["prs"] if p["repo"] == repo and p["number"] == n), None)
    if not pr or pr["state"] != "merge":
        return {"state": "error", "error": site.t("That PR is no longer ready to merge: reload the board.",
                                                  "Ese PR ya no está listo para mergear: recargá el tablero.")}
    st[key] = {"state": "waiting", "at": now()}

    def run():
        from .guardcall import act
        try:
            res = act("gh_merge", repo=repo, pr=n, head_sha=pr["head"], method="squash", approval="local",
                      approved_by=pr["approval"], title=pr["title"])
            st[key] = {"state": "done", "at": now(), "sha": (res or {}).get("sha")}
            log("console: PR merged from the app", repo=repo, pr=n, approval=pr["approval"])
            db.add_signal("merge", {"repo": repo, "pr": n, "by": f"{site.first_name().lower()} (app, touch id)", "approval": pr["approval"]})
        except Exception as e:
            msg = str(e)
            st[key] = {"state": "declined" if "local approval" in msg else "error", "at": now(),
                       "error": site.t("Not merged: you did not confirm with Touch ID.", "No se mergeó: no confirmaste con Touch ID.")
                       if "local approval" in msg else msg.replace("guard: ", "")[:300]}
            log("console: merge failed", repo=repo, pr=n, err=msg[:200])
        threading.Thread(target=refresh, daemon=True).start()

    threading.Thread(target=run, name="console-merge", daemon=True).start()
    return st[key]


def approve_review(item_id: int) -> dict:
    """His approve of a clean review, from the case page, with Touch ID in the guard (like Merge). Offered on a done
    review, never asked: a review does not wait on him."""
    st = _S.__dict__.setdefault("approves", {})
    if (st.get(item_id) or {}).get("state") == "waiting":
        return st[item_id]
    from . import actions
    try:
        p = actions.approve_review(item_id)
    except ValueError as e:
        st[item_id] = {"state": "error", "at": now(), "error": str(e)}
        return st[item_id]
    st[item_id] = {"state": "waiting", "at": now()}

    def run():
        from .guardcall import act
        try:
            act("gh_review", repo=p["repo"], pr=p["pr"], head_sha=p["head_sha"], body="", comments=[], event="APPROVE",
                approval="local")
            st[item_id] = {"state": "done", "at": now()}
            db.update_item(item_id, note=site.t("you approved the PR from Clea", "aprobaste el PR desde Clea"), data={"approved_by_him": now()})
            db.add_signal("review_approved", {"repo": p["repo"], "pr": p["pr"], "by": f"{site.first_name().lower()} (app, touch id)"}, item_id)
            log("console: review approved from the app", item=item_id, pr=f"{p['repo']}#{p['pr']}")
        except Exception as e:
            msg = str(e)
            st[item_id] = {"state": "declined" if "local approval" in msg else "error", "at": now(),
                           "error": site.t("Not approved: you did not confirm with Touch ID.", "No se aprobó: no confirmaste con Touch ID.")
                           if "local approval" in msg else msg.replace("guard: ", "")[:300]}
            log("console: review approve failed", item=item_id, err=msg[:200])

    threading.Thread(target=run, name="console-approve", daemon=True).start()
    return st[item_id]


def case_action(item_id: int, action: str, text: str = "") -> dict:
    if action == "approve_review":
        return approve_review(item_id)
    from . import actions
    try:
        out = actions.run(item_id, action, text)
    except ValueError as e:
        return {"state": "error", "error": str(e)}
    threading.Thread(target=refresh, daemon=True).start()
    return {"state": "done", **out}


def merge_state(repo: str, n: int) -> dict:
    return (_S.__dict__.get("merges") or {}).get(f"{repo}#{n}") or {"state": "open"}


SLOW_S = 3


def _slow_watch(path: str) -> threading.Timer:
    """A request still running after SLOW_S logs where it is and which threads are on the CPU, so a slow page names its
    cause."""
    me = threading.get_ident()

    def report():
        import traceback
        frames = sys._current_frames()
        names = {t.ident: t.name for t in threading.enumerate()}
        own = "".join(traceback.format_stack(frames[me])[-6:]) if me in frames else ""
        others = []
        for ident, f in frames.items():
            if ident == me:
                continue
            top = traceback.extract_stack(f)[-1]
            if top.name not in ("wait", "select", "read", "recv_into", "sleep", "_wait_for_tstate_lock", "accept",
                                "readinto", "poll", "_communicate", "get"):
                others.append(f"{names.get(ident, ident)}: {Path(top.filename).name}:{top.lineno} {top.name}")
        log("console slow request", path=path[:80], secs=SLOW_S, at=own[-1500:], busy=others[:15])

    t = threading.Timer(SLOW_S, report)
    t.daemon = True
    t.start()
    return t


# ---- HTTP ------------------------------------------------------------------------------------------------------------
class _H(http.server.BaseHTTPRequestHandler):
    # the running server keeps the handler class it started with: every request is routed through the CURRENT module's
    # _H, so a hot reload changes the routes too
    def do_GET(self):
        mod = sys.modules["ap.console"]
        watch = mod._slow_watch(self.path)
        try:
            mod._H.get_impl(self)
        finally:
            watch.cancel()

    def do_POST(self):
        sys.modules["ap.console"]._H.post_impl(self)

    def get_impl(self):
        mod = sys.modules["ap.console"]
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        if path.startswith("/api/") and (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, b"{}", "application/json")  # DNS rebinding: only a local Host reads the data
        if path.startswith("/api/"):
            _S.last_hit = time.time()
        if path == "/api/setup" or path.startswith("/api/setup/"):
            return mod._H.setup_impl(self, "GET", path, {})
        try:
            if path == "/api/board":
                return self._json(mod.snapshot()["board"])
            if path == "/api/threads":
                return self._json(mod.snapshot()["threads"])
            if path == "/api/activity":
                return self._json(mod.activity())
            if path == "/api/chats":
                return self._json(mod.chats())
            if path == "/api/config":  # the workspace this install works in: the app's code names no organization
                from . import gh
                return self._json({"github_org": gh.ORG, "jira_base": CFG.get("jira_base") or ""})
            if path == "/api/notifications":  # Clea.app shows these as macOS notifications (babysit.mac_notify)
                after = int((urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("after") or ["0"])[0] or 0)
                db.kv_set("notify_polled_at", now())
                return self._json([x for x in db.kv_get("notify_queue") or [] if x["id"] > after])
            m = re.match(r"^/api/ask/(\d+)$", path)
            if m:
                return self._json(mod.ask_state(int(m.group(1))))
            m = re.match(r"^/api/case/(\d+)/approve_review$", path)
            if m:
                return self._json((_S.__dict__.get("approves") or {}).get(int(m.group(1))) or {"state": "open"})
            m = re.match(r"^/api/pr/([\w-]+)/(\d+)$", path)
            if m:
                return self._json(mod.merge_state(m.group(1), int(m.group(2))))
            m = re.match(r"^/api/chat/([\w:.-]+)$", path)
            if m:
                return self._json(mod.conversation(m.group(1)))
            m = re.match(r"^/api/thread/(\d+)$", path)
            if m:
                t = mod.thread(int(m.group(1)))
                return self._json(t) if t else self._send(404, b"{}", "application/json")
        except Exception as e:
            log("console api failed", path=path, err=f"{type(e).__name__}: {e}"[:200])
            return self._send(500, json.dumps({"error": str(e)[:300]}).encode(), "application/json")
        f = (mod.DIST / path.lstrip("/")).resolve()
        if not str(f).startswith(str(mod.DIST.resolve())) or not f.is_file():
            f = mod.DIST / "index.html"  # client-side routes
        if not f.exists():
            return self._send(200, b"<p>The app is not built yet. Run <code>make app</code> (or <code>cd ui &amp;&amp; pnpm install &amp;&amp; pnpm build</code>) and reload.</p>", "text/html")
        self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")

    def post_impl(self):
        mod = sys.modules["ap.console"]
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost") or self.headers.get("X-AP-Console") != "1":
            return self._send(403, b"{}", "application/json")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0) or b"{}")
            if self.path.startswith("/api/setup/"):
                return mod._H.setup_impl(self, "POST", self.path.split("?", 1)[0], body)
            if self.path == "/api/chat":
                conv = mod.say(body.get("conv"), body.get("text", ""), body.get("item"))
                return self._json({"conv": conv})
            m = re.match(r"^/api/ask/(\d+)$", self.path)
            if m:
                return self._json(mod.decide(int(m.group(1)), body.get("decision", ""), body.get("text", "")))
            m = re.match(r"^/api/pr/([\w-]+)/(\d+)/merge$", self.path)
            if m:
                return self._json(mod.merge(m.group(1), int(m.group(2))))
            m = re.match(r"^/api/case/(\d+)/(go|retry|drop|approve_review)$", self.path)
            if m:
                return self._json(mod.case_action(int(m.group(1)), m.group(2), body.get("text", "")))
        except Exception as e:
            log("console post failed", path=self.path, err=f"{type(e).__name__}: {e}"[:200])
            return self._send(400, json.dumps({"error": str(e)[:300]}).encode(), "application/json")
        self._send(404, b"{}", "application/json")

    def setup_impl(self, method: str, path: str, body: dict):
        """The onboarding and settings routes (ap/setup.py). What the user can fix comes back as a 400 they can read;
        a secret in a request body never reaches the log."""
        from . import setup
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._json(setup.handle(method, path, query, body if isinstance(body, dict) else {}))
        except setup.Bad as e:
            return self._send(400, json.dumps({"error": str(e), "missing": e.missing}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except KeyError:
            return self._send(404, b"{}", "application/json")
        except Exception as e:
            log("console setup failed", path=path, err=f"{type(e).__name__}: {e}"[:200])
            return self._send(500, json.dumps({"error": f"{type(e).__name__}: {str(e)[:200]}"}).encode(), "application/json")

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


SNAP_EVERY = 20


SNAP_FILE = VAR / "console-snapshot.json"


def snapshot() -> dict:
    """The board and the thread list come from a snapshot refreshed in the background: computing them checks every PR's
    state (25-45 s cold), and the app polls every few seconds. A new worker serves the last one saved to disk at once
."""
    if getattr(_S, "snap", None) is None and db.is_live():  # a scratch DB never reads the live board
        try:
            _S.snap = json.loads(SNAP_FILE.read_text())
        except (OSError, ValueError):
            pass
    if getattr(_S, "snap", None) is None:
        with _lock():  # the background pass may be computing it: wait for that one instead of starting another
            if getattr(_S, "snap", None) is None:
                refresh()
    return _S.snap


def _lock():
    return _S.__dict__.setdefault("snap_lock", threading.RLock())


def refresh() -> None:
    with _lock():
        b = board()
        _S.snap = {"board": b, "threads": _S.__dict__.get("last_threads") or [], "at": now()}
    if db.is_live():
        try:
            tmp = SNAP_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_S.snap, ensure_ascii=False, default=str))
            tmp.replace(SNAP_FILE)
        except OSError as e:
            log("console snapshot not saved", err=str(e)[:120])


IDLE_AFTER = 300


def _refresh_loop() -> None:
    """Refresh only while the app is open (a request in the last 5 min): a closed app costs nothing. Opening it again
    serves the last snapshot at once and the next pass catches up within SNAP_EVERY."""
    from . import site
    while True:
        try:
            if not site.setup_complete():
                pass  # a new install has no board yet: the app shows the onboarding
            elif time.time() - getattr(_S, "last_hit", 0) < IDLE_AFTER or getattr(_S, "snap", None) is None:
                sys.modules["ap.console"].refresh()
        except Exception as e:
            log("console refresh failed", err=f"{type(e).__name__}: {e}"[:200])
        time.sleep(SNAP_EVERY)


def serve_once() -> None:
    if getattr(_S, "started", False):
        srv = getattr(_S, "srv", None)
        if srv is None:  # started by a version that did not keep a reference: find the live server once
            import gc
            srv = next((o for o in gc.get_objects() if isinstance(o, http.server.HTTPServer)
                        and getattr(o, "server_address", (None, None))[1] == PORT), None)
            _S.srv = srv
        if srv is not None and srv.RequestHandlerClass is not _H:
            srv.RequestHandlerClass = _H
            log("console handler swapped to the reloaded code")
        _keep_ready()
        return
    if not db.is_live():
        return  # a scratch DB (selftest) never serves the user's app, even when the port happens to be free
    _S.started = True
    threading.Thread(target=_refresh_loop, name="console-refresh", daemon=True).start()
    _spawn_ready()  # the first question already finds a started session
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _H)
        _S.srv = srv
        threading.Thread(target=srv.serve_forever, name="console-http", daemon=True).start()
        log("console serving", url=f"http://127.0.0.1:{PORT}")
    except OSError as e:
        log("console port busy", err=str(e)[:80])
