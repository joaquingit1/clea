"""Outcome audit: the checks the user was doing by hand. The watchdog guards mechanics (claims,
stalls, processes); this guards RESULTS. Zero tokens. Deterministic remediation where one exists
(requeue, re-judge); everything else becomes a finding with evidence in var/doctor-report.md and one
calm line on the board. It never edits harness code: fixes to the harness stay a human-reviewed change."""
from __future__ import annotations

import collections
import json
import re

from . import db, site
from .core import VAR, log, now

REPORT = VAR / "doctor-report.md"
H = 3600


def _items(where: str, *args) -> list[dict]:
    return [db.item(r["id"]) for r in db.q(f"SELECT id FROM items WHERE {where}", *args)]


def audit() -> dict:
    from .tasks import builds
    findings, fixed = [], []
    since = now() - 48 * H

    # 1. yield: owned bugs with a proven/probable cause should end in a PR, not a proposal. A drop is not audited: it is
    # a later verdict by the stage that knew more (vet, cover, escalation, news); every drop the audit reopened was
    # right (#131 covered, #145 answered, #539 shipped by a teammate, rebuilt into 'fix rounds exhausted' on 10-06). An ask
    # is the same kind of verdict: the dossier or the gate found a fact only a human has, and rebuilding superseded
    # the question an hour later, the gate asked it again and the audit overturned it again (#837 on 10-15)
    for it in _items("kind IN ('TASK','INCIDENT','IDEA') AND updated_at>?", since):
        ds = it["data"].get("dossier") or {}
        has_pr = any(p.get("pr_url") for p in it["data"].get("parts", []))
        if (builds(ds) and not has_pr and it["status"] in ("propose", "propose_check", "awaiting_approval")
                and not it["data"].get("parent") and not it["data"].get("ask")):
            plan = it["data"].get("plan")
            if plan and not plan.get("parts"):
                # the planner read the code and found nothing to build in our repos (plan.outside): its proposal is the
                # outcome. Forcing a build ran it with no parts (IndexError) and retired his ask (#225, #535 on 10-06)
                findings.append(("yield", site.t(f"#{it['id']} bug with a cause but no code parts: the proposal stays",
                                                 f"#{it['id']} bug con causa sin partes de código: queda la propuesta"),
                                 f"outside={len(plan.get('outside') or [])}"))
                continue
            if plan:
                db.update_item(it["id"], status="build", attempts=0, next_run_at=0, note="outcome audit: bug with a cause builds",
                               data={"parts": it["data"]["plan"]["parts"], "cur": 0, "fix_rounds": 0})
            else:
                db.update_item(it["id"], status="plan", attempts=0, next_run_at=0, note="outcome audit: bug with a cause builds")
            for a in db.q("SELECT id FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"]):
                db.set_artifact_outcome(a["id"], "superseded", {"why": site.t("the fix is being built", "se construye el fix")})
            fixed.append(site.t(f"#{it['id']} builds its fix again ({ds['root_cause_status']})",
                                f"#{it['id']} vuelve a construir su fix ({ds['root_cause_status']})"))

    # 2. code that never landed: builds without commits, fix rounds exhausted, blocked with a PR plan
    blocked = _items("status='blocked' AND updated_at>?", since)
    sig = collections.defaultdict(list)
    for it in blocked:
        why = (it["data"].get("blocked_reason") or it.get("last_error") or "")
        key = re.sub(r"[0-9a-f]{7,}|\d+", "#", why.split("\n")[0])[:90]
        sig[key].append(it["id"])
    for key, ids in sig.items():
        findings.append(("blocked", site.t(f"{len(ids)} case(s) blocked by: {key}", f"{len(ids)} caso(s) bloqueados por: {key}"), f"items={ids}"))

    # 3. the same harness exception across different items = a harness bug, not bad luck
    errs = collections.defaultdict(set)
    for r in db.q("SELECT item_id, data FROM signals WHERE kind='harness_bug' AND at>?", since):
        d = json.loads(r["data"] or "{}")
        errs[(d.get("stage"), re.sub(r"\d+", "#", (d.get("err") or "")[:80]))].add(r["item_id"])
    for (stage, err), ids in errs.items():
        if len(ids) >= 2:
            findings.append(("harness_bug", site.t(f"the same error at {stage} in {len(ids)} cases: {err}",
                                                   f"el mismo error en {stage} en {len(ids)} casos: {err}"), f"items={sorted(ids)}"))

    # 4. intake: messages aimed at him with no case after 15 min (the re-judge should have caught them)
    from . import ingest
    missed = 0
    for ev in db.q("SELECT * FROM events WHERE item_id IS NULL AND created_at BETWEEN ? AND ? AND source!='slack-alerts'",
                   since, now() - 900):
        payload = json.loads(ev["payload"] or "{}")
        msg = {"ts": ev["ts"], "user": ev["user"], "text": ev["text"], "thread_ts": ev["thread_ts"] or None}
        extra = {"channel_kind": payload.get("channel_kind") or ""}
        if ev["user"] and not ev["user"].startswith("B") and ingest._aimed(ev["channel"], msg, extra):
            dm = extra["channel_kind"] == "dm" or (ev["channel"] or "").startswith("D")
            tk = f"slack:{ev['channel']}:dm" if dm else f"slack:{ev['channel']}:{ev['thread_ts'] or ev['ts']}"
            if dm and db.q1("SELECT 1 FROM items WHERE thread_key=?", tk):
                continue  # DMs are one case per conversation: covered
            if not db.q1("SELECT 1 FROM items WHERE thread_key=? OR thread_key LIKE ?", tk, tk + ":%"):
                missed += 1
    if missed:
        n = ingest.rejudge()
        fixed.append(site.t(f"the re-judge recovered {n} of {missed} messages aimed at you with no case",
                            f"re-judge recuperó {n} de {missed} mensajes dirigidos a vos sin caso"))
        if n < missed:
            findings.append(("intake", site.t(f"{missed - n} message(s) aimed at you still have no case after the re-judge",
                                              f"{missed - n} mensaje(s) dirigidos a vos siguen sin caso tras el re-judge"), ""))

    # 5. proactive work: patrol must run; the harness must produce its own PRs, not only reviews
    last_patrol = db.kv_get("job_ok:patrol", 0)
    if now() - last_patrol > 6 * H:
        findings.append(("patrol", site.t("patrol has not run in more than 6 h", "patrol no corrió en más de 6 h"), f"last_ok={last_patrol}"))
    own_prs = sum(1 for it in _items("kind IN ('TASK','INCIDENT','IDEA') AND updated_at>?", now() - 72 * H)
                  if any(p.get("pr_url") for p in it["data"].get("parts", [])))
    if own_prs == 0:
        findings.append(("yield", site.t("no PR of our own in 72 h (only reviews and drafts)", "ningún PR propio en 72 h (solo reviews y borradores)"), ""))

    # 6. spend without output
    usd = db.q1("SELECT coalesce(sum(usd),0) s FROM calls WHERE tier='claude' AND at>?", now() - 24 * H)["s"]
    prs_24 = sum(1 for it in _items("kind IN ('TASK','INCIDENT','IDEA') AND updated_at>?", now() - 24 * H)
                 if any(p.get("pr_url") for p in it["data"].get("parts", [])))
    if usd > 40 and prs_24 == 0:
        findings.append(("cost", site.t(f"${usd:.0f} spent in 24 h with no PR of our own", f"${usd:.0f} gastados en 24 h sin ningún PR propio"), ""))

    lines = ["# Doctor report", f"generated {db.fmt_time(now())}", ""]
    for kind, text, ev in findings:
        lines += [f"- [{kind}] {text}", f"  evidence: {ev}" if ev else ""]
    if fixed:
        lines += ["", "## Remediated automatically"] + [f"- {f}" for f in fixed]
    REPORT.write_text("\n".join(l for l in lines if l is not None))
    db.kv_set("outcomes", {"at": now(), "findings": [f[1] for f in findings], "fixed": fixed})
    log("outcome audit", findings=len(findings), fixed=len(fixed))
    return {"findings": findings, "fixed": fixed}
