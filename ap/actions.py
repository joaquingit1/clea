"""What the user can do on a case that needs him, the same from the app (console POST /api/case/<id>/<action>) and from
the DM chat (chat._act). Before 2026-10-22 a case could ask for his "✅" with nothing to approve (#512: "tu ✅ para que
agregue el mensaje en Maestros y corrija el orden de merge"), and nothing consumed it: the chat's requeue only reset the
retry counter, so the case sat in awaiting_human, shown as "Esperando", with no button anywhere.

  go_ahead        he gives the ok (optionally with more instructions): a case with a PR goes to a fix round on it,
                  one without goes back to planning, with his words as binding instructions
  retry           a blocked case runs its stage again, fix rounds reset (his call, not the watchdog's)
  drop            he closes the case
  approve_review  a review that found nothing becomes his approve on GitHub, with his Touch ID (optional: a clean
                  review is done and never waits on him)
"""
from __future__ import annotations

from . import db, site

BY = site.t(f"{site.first_name()} from Clea", f"{site.first_name()} desde Clea")


def _instr(it: dict, line: str) -> str:
    old = (it["data"].get("instructions") or "").strip()
    return (old + "\n" if old else "") + f"[{site.first_name()}, {db.today()}] {line}"


def go_ahead(item_id: int, text: str = "", by: str = BY) -> str:
    """His ok on what the case asked for (its card_need), plus anything he adds."""
    it = db.item(item_id)
    if not it:
        raise ValueError(site.t(f"case #{item_id} does not exist", f"no existe el caso #{item_id}"))
    if it["status"] in db.TERMINAL:
        raise ValueError(site.t("that case is already closed", "ese caso ya está cerrado"))
    need = (it["data"].get("card_need") or "").strip()
    what = site.t(f"gave the ok to: {need}", f"dio el ok a: {need}") if need else site.t("gave the ok to go on", "dio el ok para seguir")
    line = what + (site.t(f". Added: {text.strip()}", f". Agregó: {text.strip()}") if text.strip() else "")
    parts = it["data"].get("parts") or []
    cur = it["data"].get("cur", 0)
    data = {"instructions": _instr(it, line), "card_need": "", "needs_you": False, "blocked_reason": None}
    if parts and parts[cur].get("pr"):
        # the work is on a published PR: a fix round on it, his words as the problem to solve. His ok is not an
        # automatic round, so the round budget starts over
        parts[cur] = {**parts[cur], "fix_rounds": 0}
        data.update(parts=parts, fix_rounds=0, fix_problems=[f"{site.first_name()} {line}"], fix_source=site.first_name().lower())
        db.update_item(item_id, status="fix", attempts=0, next_run_at=0, urgency=3, note=f"{by}: {what}"[:300], data=data)
        return site.t(f"#{item_id}: going on with a fix on its PR", f"#{item_id}: sigue con un arreglo en su PR")
    db.update_item(item_id, status="plan", attempts=0, next_run_at=0, urgency=3, note=f"{by}: {what}"[:300], data=data)
    return site.t(f"#{item_id}: planning again with your directions", f"#{item_id}: vuelve a planear con tus indicaciones")


def _blocked_stage(it: dict) -> str:
    d = it["data"]
    stage = d.get("blocked_stage") or d.get("blocked_from")
    if not stage:
        r = db.q1("SELECT src FROM transitions WHERE item_id=? AND dst='blocked' ORDER BY at DESC, rowid DESC LIMIT 1", it["id"])
        stage = r["src"] if r and r["src"] and r["src"] != "blocked" else None
    return stage or "gate"


def retry(item_id: int, text: str = "", by: str = BY) -> str:
    """A blocked case runs again from where it stopped; with text, his words go with it as instructions."""
    it = db.item(item_id)
    if not it or it["status"] != "blocked":
        raise ValueError(site.t("that case is not stuck", "ese caso no está trabado"))
    stage = _blocked_stage(it)
    data = {"blocked_reason": None, "fix_rounds": 0, "needs_you": False}
    if it["data"].get("parts"):
        parts = it["data"]["parts"]
        cur = it["data"].get("cur", 0)
        parts[cur] = {**parts[cur], "fix_rounds": 0}
        data["parts"] = parts
    if text.strip():
        data["instructions"] = _instr(it, site.t(f"asked to retry: {text.strip()}", f"pidió reintentar: {text.strip()}"))
    db.update_item(item_id, status=stage, attempts=0, next_run_at=0, urgency=3, note=f"{by}: " + site.t("retry", "reintentar"), data=data)
    return site.t(f"#{item_id}: retrying from {stage}", f"#{item_id}: reintenta desde {stage}")


def drop(item_id: int, text: str = "", by: str = BY) -> str:
    it = db.item(item_id)
    if not it:
        raise ValueError(site.t(f"case #{item_id} does not exist", f"no existe el caso #{item_id}"))
    for a in db.q("SELECT id FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", item_id):
        db.set_artifact_outcome(a["id"], "superseded", {"why": site.t(f"{site.first_name()} closed the case", f"{site.first_name()} cerró el caso")})
    db.update_item(item_id, status="dropped", note=f"{by}: " + site.t("closed it", "lo cerró") + (f" ({text.strip()[:200]})" if text.strip() else ""))
    return site.t(f"#{item_id}: closed", f"#{item_id}: cerrado")


def approve_review(item_id: int) -> dict:
    """What his approve of a clean review would sign: the PR at the head the review read. A push since then is
    unreviewed code, so the review reads the new commits first (review_prepare from that head) instead of approving
    them blind. The console sends it through the guard, which asks for his Touch ID (console.approve_review)."""
    from . import gh
    it = db.item(item_id)
    if not it or it["kind"] != "REVIEW":
        raise ValueError(site.t("that case is not a review", "ese caso no es una review"))
    if it["data"].get("comments"):
        raise ValueError(site.t("this review has comments: it is not an approve", "esta review tiene comentarios: no es un approve"))
    pr = gh.pr_view(it["repo"], it["pr"])
    if pr["state"] != "OPEN":
        raise ValueError(site.t(f"that PR is no longer open ({pr['state'].lower()})", f"ese PR ya no está abierto ({pr['state'].lower()})"))
    read = it["data"].get("head_sha")
    if not read or pr["headRefOid"] != read:
        db.update_item(item_id, status="review_prepare", attempts=0, next_run_at=0, urgency=3, data={"approve_since": read},
                       note=site.t("the PR changed after the review: reviewing what is new before approving",
                                   "el PR cambió después de la review: revisa lo nuevo antes de aprobar"))
        raise ValueError(site.t("The PR changed after the review: I will review what is new and let you know.",
                                "El PR cambió después de la review: reviso lo nuevo y te aviso."))
    return {"repo": it["repo"], "pr": it["pr"], "head_sha": read, "title": pr["title"]}


def run(item_id: int, action: str, text: str = "") -> dict:
    """The console's entry point for the actions that need no Touch ID."""
    if action == "go":
        return {"ok": True, "message": go_ahead(item_id, text)}
    if action == "retry":
        return {"ok": True, "message": retry(item_id, text)}
    if action == "drop":
        return {"ok": True, "message": drop(item_id, text)}
    raise ValueError(site.t(f"unknown action: {action}", f"acción desconocida: {action}"))
