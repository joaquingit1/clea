"""Patrol: when the queue is idle, Muse hunts for real defects in the the team domain
(free, so it can read deeply), one area per run in rotation. The single best finding per
day is Opus-verified; a confirmed, in-fence, no-migration, no-new-UX bug becomes a TASK
(and flows into the normal build pipeline); anything else becomes an IDEA."""
from __future__ import annotations

import json

from . import claude, db, muse, site
from .core import CFG, log, now
from .tasks import READ_ROOT


def areas() -> list[tuple[str, str]]:
    """The folders patrol reads, one per run in rotation: the install's own list (config fence.patrol_areas), else its
    fence paths, else every configured repo from its root."""
    own = [(r, a) for r, a in site.fence().get("patrol_areas") or [] if r in CFG["repos"]]
    if own:
        return own
    fenced = [(r, a.rstrip("/")) for r, ps in site.fence_paths().items() if r in CFG["repos"] for a in ps]
    return fenced or [(r, "") for r in CFG["repos"]]


SCHEMA = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array", "items": {
    "type": "object", "required": ["title", "file", "evidence", "impact", "severity", "fix_sketch"],
    "properties": {"title": {"type": "string"}, "file": {"type": "string"}, "line": {"type": "integer"},
                   "evidence": {"type": "string"}, "impact": {"type": "string"},
                   "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                   "fix_sketch": {"type": "string"}, "needs_migration": {"type": "boolean"},
                   "new_ux": {"type": "boolean"}}}}}}
VERIFY = {"type": "object", "required": ["real", "reason", "kind"], "properties": {
    "real": {"type": "boolean"}, "reason": {"type": "string"},
    "kind": {"type": "string", "enum": ["TASK", "IDEA", "none"]}}}


def run_once() -> dict:
    # Slack first: areas people are complaining about jump the rotation
    hints = " ".join((db.kv_get("slack_context") or {}).get("patrol_hints", [])).lower()
    AREAS = areas()
    if not AREAS:
        return {"area": None, "found": 0}
    hinted = [a for a in AREAS if any(w in hints for w in a[1].lower().split("/")[-1].split("-") if len(w) > 4)
              and f"{a[0]}:{a[1]}" not in db.kv_get("patrol_hint_done", [])]
    if hinted:
        repo, area = hinted[0]
        db.kv_set("patrol_hint_done", (db.kv_get("patrol_hint_done", []) + [f"{repo}:{area}"])[-20:])
    else:
        i = db.kv_get("patrol_idx", 0) % len(AREAS)
        repo, area = AREAS[i]
        db.kv_set("patrol_idx", i + 1)
    root = CFG["repos"][repo]["path"]
    out = muse.call("ap-patrol", f"Patrol area: {root}/{area} (repo {repo}). Read it deeply, trace into the "
                    f"backend contracts it depends on under {READ_ROOT} when relevant, and report real defects.",
                    cwd=root, schema=SCHEMA, lane="patrol")
    db.add_signal("patrol_findings", {"area": area, "findings": out["findings"]})
    found = [f for f in out["findings"] if f["severity"] in ("critical", "major")
             and not f.get("needs_migration")]
    seen = set(db.kv_get("patrol_seen", []))
    found = [f for f in found if f"{f['file']}:{f['title'][:60]}" not in seen]
    log("patrol", area=area, findings=len(out["findings"]), major=len(found))
    if not found:
        return {"area": area, "found": 0}
    best = sorted(found, key=lambda f: f["severity"] != "critical")[0]
    seen.add(f"{best['file']}:{best['title'][:60]}")
    db.kv_set("patrol_seen", sorted(seen)[-500:])
    if db.kv_get("patrol_verified_day") == db.today() or not claude.can_spend("verify"):
        db.add_signal("patrol_unverified", {"area": area, "finding": best})
        return {"area": area, "found": len(found), "verified": False}
    v = claude.lean("verify", f"Patrol finding in {root} (repo {repo}):\n{json.dumps(best, ensure_ascii=False)}\n\n"
                    "Is this a real defect with user-visible or data impact? kind=TASK if it is a clear, in-fence fix "
                    "without migrations or new UX; IDEA if it needs a decision; none if not real.",
                    cwd=root, schema=VERIFY, add_dirs=(READ_ROOT,))
    db.kv_set("patrol_verified_day", db.today())
    if not v["real"] or v["kind"] == "none":
        db.add_signal("patrol_false_positive", {"finding": best, "reason": v["reason"]})
        return {"area": area, "found": len(found), "verified": False}
    key = f"patrol:{repo}:{best['file']}:{best['title'][:40]}"
    if not db.open_item_for_thread(key):
        db.create_item(v["kind"], "investigate", f"patrol: {best['title'][:80]}", key, repo=repo, urgency=1,
                       data={"summary": best["impact"], "text": json.dumps(best, ensure_ascii=False),
                             "proposal": v["kind"] == "IDEA", "patrol": True})
    return {"area": area, "found": len(found), "verified": True, "kind": v["kind"]}
