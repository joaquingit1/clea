"""A blocked case is never a dead end.

The miss: the schools prd consumer died by OOM at 03:09 and its case ended "blocked" after self-review said the
restart-policy PR alone did not solve the incident (the restart needs AWS, which the harness lacks). Nothing else
happened: no check whether prod was still broken, no owner found, no request drafted. A session then did, by hand:
verified live in Datadog (still down, 2.474 messages queued), read the infra code and its git history (who owns it),
and drafted the exact commands for the person with access.

sweep() (from hot.tick, one case at a time, own thread) gives every blocked TASK/INCIDENT/CI/COMMENTS one escalation session with
the same reach as his interactive sessions (claude.orchestrator). It decides between:
- the harness can still finish it: the case goes back to the stage that does it, with the instructions;
- a person with access must act: a draft in his voice, with the live evidence and the exact commands, to that
  person, as a ✅ ask in his DM (the guard sends it on ✅);
- nothing left to do (resolved meanwhile, or not worth it): the case closes with the reason.
One escalation per block; a case blocked again for a new reason gets a new one.
"""
from __future__ import annotations

import json
import re
import threading

from . import db, site, slack
from . import comms as _comms
from .core import CFG, DATA, VAR, log, now

# the per-case counters that block a case when spent (tasks.py). A retake starts all of them over: resetting only
# fix_rounds left #609's cr_failures at 6 from the afternoon's 'Too many files' passes, so its retake at 21:03 blocked
# again on its first CodeRabbit outage at 22:38, one gate run instead of six, and went to the user. #870's
# retake at 19:01 kept reruns at 3, so its new head's wait blocked at 20:05 without one rerun or fold
BREAKERS = ("fix_rounds", "cr_failures", "size_rounds", "ci_rounds", "reruns")
# the tag on the escalation's own instruction lines; tasks.ESCALATION_TAGS reads both forms
TAG = site.t("[escalation]", "[escalación]")

PLAN = {"type": "object", "required": ["still_broken", "evidence", "route", "summary"], "properties": {
    "still_broken": {"type": "boolean", "description": "the case's goal is not met yet, verified NOW with your tools (metrics, logs, PR/ticket state). An outage that recovered but whose cause or prevention is still unfixed is still_broken=true"},
    "evidence": {"type": "string", "description": "what you checked and saw, with numbers, times and links ("
                 + site.own("Spanish", site.language_name()) + ")"},
    "route": {"type": "string", "enum": ["harness", "human", "close"],
              "description": "harness = we can still finish it ourselves; human = someone with access must act; close = nothing left to do"},
    "summary": {"type": "string", "description": f"for {site.first_name()}: 2-4 bullets, {site.own('Spanish', f'in {site.language_name()}')}, "
                                                 "what is wrong now and what happens next"},
    "harness_next": {"type": "string", "enum": ["", "plan", "build", "fix", "publish"],
                     "description": "for route=harness: the stage that does the remaining work"},
    "harness_instructions": {"type": "string", "description": "for route=harness: exactly what that stage must do differently"},
    "wait_for_pr": {"type": "string", "description": "for route=harness when our PR's red CI only waits for a teammate's OPEN PR "
                                                     "to merge (the fix of a broken base): its URL. No stage, leave harness_next empty"},
    "person_name": {"type": "string", "description": "for route=human: who has the access, by name"},
    "person_slack_id": {"type": "string", "description": "for route=human: their Slack user id (U...), from the Slack tools"},
    "why_them": {"type": "string", "description": "for route=human: the evidence they own it (git log of the infra, CODEOWNERS, who ran it before)"},
    "new_cases": {"type": "array", "description": "other real problems you found on the way (not this case), each one a new case",
                  "items": {"type": "object", "required": ["title", "evidence"], "properties": {
                      "title": {"type": "string"}, "evidence": {"type": "string"}, "prod": {"type": "boolean"},
                      "harness": {"type": "boolean", "description": "the problem is in this harness itself (its code under ap/ "
                                  f"or prompts/, its config, its Claude Code login), not in {site.company() or 'the company'}'s product or infra"}}}},
    "message": {"type": "string", "description": f"for route=human: the message {site.first_name()} sends them, in his voice ("
                                                 + site.own("Spanish, voseo, greeting first, in the register he and that person use with each other",
                                                            f"{site.language_name()}, greeting first, in the register he and that person use with each other")
                                                 + "), self-contained: what broke, impact now, the exact commands in order (capture evidence first, "
                                                 "then the fix), the risk and its mitigation, what we do in parallel"},
}}

PROMPT = """{headline} Your job is what a senior engineer does when work gets stuck:
make sure it still gets solved, today, without {me} having to investigate it himself.

CASE #{id} ({kind}): {title}
STATE: {state}
PROBLEM: {problem}
OUR PRs: {prs}
SELF-VERIFY VERDICT: {verify}
OUR PENDING ASK TO {ME}: {ask}
ALREADY RETAKEN: {retaken}
Case folder (dossier, plan, diff, research): var/items/{id}/

0. First check whether someone else already took or shipped it: PRs in the repo merged or opened since the case started
   that touch the same files (gh pr list --repo {org}/<repo> --state all --search "updated:>=<date>"; gh pr diff),
   recent commits on master, and what people said in the case's Slack threads. If a teammate already shipped the same fix,
   route=close (say whose PR). If they are on it, route=human only to hand them what they do not have yet, never to brief
   them on what they already know.
1. Verify NOW whether the problem is still happening, with real evidence (Grafana logs/metrics/traces, data in Redshift, Sentry, the ticket, the PR).
2. Decide the route:
   - harness: the remaining work is something we can do ourselves (another code change, open the PR with a caveat, a fix the
     reviewer asked for). Say which stage and exactly what to do. If what remains is only waiting for a teammate's open
     PR to merge (it fixes the broken base our CI fails on), give its URL in wait_for_pr instead of a stage: no stage can
     wait, the case waits for that merge with no tokens and then folds the base in. Not available when ALREADY RETAKEN
     is yes: the harness had its second chance and blocked again, so a person decides (human) or it ends (close).
   - human: the fix needs access or authority we do not have (prod AWS, a deploy, a data fix in prod, a decision). Find the
     person who has it with evidence (git log/blame of the relevant infra or code, CODEOWNERS, Slack history), get their
     Slack id with the Slack tools, and write the message {me} sends them: what broke, the impact right now, the exact
     commands or queries in order (capture evidence before any restart), the risk and how to handle it, what we do in parallel.
   - close: it is resolved, someone else owns the fix, or not worth pursuing; say why with evidence. A pending ask is then
     withdrawn, so choose close only if what it would send is no longer needed.
Never invent commands: read the code/config for cluster, service, queue and variable names. Return through the schema."""


def _escalated(it: dict) -> bool:
    """Already escalated for this block. A crashed escalation counts too, until the code changes: retried every tick,
    the crashing case stayed first in line and starved every case behind it."""
    esc = it["data"].get("escalation") or {}
    if esc.get("failed_rev"):
        from .worker import code_rev
        return esc["failed_rev"] == code_rev()
    return bool(esc) and esc.get("reason") == (it["data"].get("blocked_reason") or "")[:300]


def _candidates() -> list[dict]:
    out = []
    # fixes on his PRs (CI, COMMENTS) block too, and with no escalation nothing told anyone (2026-10-07: #609, the CI fix
    # of cometa#2436, blocked at 16:12 on CodeRabbit and sat silent; #632 the same at 20:09)
    for r in db.q("SELECT id FROM items WHERE status='blocked' AND kind IN ('TASK','INCIDENT','CI','COMMENTS') "
                  "ORDER BY urgency DESC, updated_at"):
        it = db.item(r["id"])
        if not _escalated(it):
            out.append(it)
    # an urgent prod incident gets its escalation early, in parallel with the build: the code fix is prevention, but
    # someone may have to act on prod now
    for r in db.q(f"SELECT id FROM items WHERE kind='INCIDENT' AND urgency >= 3 AND status NOT IN {db.TERMINAL} "
                  "AND status != 'blocked' AND created_at < ? AND (json_extract(data,'$.escalation') IS NULL "
                  "OR json_extract(data,'$.escalation.failed_rev') IS NOT NULL)", now() - 900):
        it = db.item(r["id"])
        if not _escalated(it):
            out.append(it)
    return out


def sweep() -> int:
    """Called from hot.tick. Starts at most one escalation at a time, in its own thread."""
    settle_stored()
    S = db.kv_get("escalate_running") or {}
    if S.get("at") and now() - S["at"] < 1800:
        return 0
    cands = _candidates()
    if not cands:
        return 0
    from . import claude
    try:
        claude.need_connectors("escalate")  # it verifies live in Grafana/Sentry; on another account it could only guess
    except claude.ConnectorsMissing:
        _tell_held(cands)
        return 0
    it = cands[0]
    db.kv_set("escalate_running", {"at": now(), "item": it["id"]})
    threading.Thread(target=_safe, args=(it["id"],), name="escalate", daemon=True).start()
    return 1


def _tell_held(cands: list[dict]) -> None:
    """The escalation is the only way a block reaches a person, so while it waits for the work account each block is
    told to him once, zero tokens."""
    for it in cands:
        reason = (it["data"].get("blocked_reason") or "")[:300]
        if it["status"] != "blocked" or it["data"].get("escalation_held") == reason:
            continue
        db.update_item(it["id"], data={"escalation_held": reason})
        _tell(it, site.t(f"⛔ *{_comms.name(it)}*: stuck ({_comms.reason(reason)}). I will not escalate it until "
                         f"Claude Code is back on the work account (`claude /login`).",
                         f"⛔ *{_comms.name(it)}*: trabado ({_comms.reason(reason)}). No lo escalo hasta que "
                         f"Claude Code vuelva a la cuenta del trabajo (`claude /login`)."))


def _safe(item_id: int) -> None:
    try:
        run(item_id)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        log("escalate failed", item=item_id, err=err)
        from .worker import code_rev
        it = db.item(item_id)
        db.update_item(item_id, data={"escalation": {"reason": (it["data"].get("blocked_reason") or "")[:300], "at": now(),
                                                     "route": "failed", "err": err, "failed_rev": code_rev()}})
    finally:
        db.kv_set("escalate_running", {})


# the harness's part is over: blocked, or its result waits on him. A close verdict ends the case and withdraws its asks
# (2026-10-01: #366's escalation found a teammate's schools#973, approved and green, fixing the OOM, and said close; the case
# was awaiting his ✅, so only 'nothing for a person to do' was logged and its draft asking the team whether the memory
# split had changed, which #973 had answered, stayed in his DM, read as 'third trigger, zero engagement')
PARKED = ("blocked", "awaiting_approval", "awaiting_human")


def _live_asks(item_id: int) -> list:
    return db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", item_id)


def run(item_id: int, dry: bool = False) -> dict:
    from . import claude
    it = db.item(item_id)
    d = it["data"]
    reason = (d.get("blocked_reason") or "")[:300]
    verify = ""
    vf = VAR / "items" / str(item_id) / "self-verify.json"
    if vf.exists():
        verify = vf.read_text()[:2500]
    ask = "\n".join((json.loads(a["content"] or "{}").get("payload") or {}).get("text", "")[:1500] for a in _live_asks(item_id))
    # an urgent incident escalated early is not blocked: told it was, with an empty reason, the session went looking for
    # the block and filed the case's own live build as a harness defect
    if it["status"] == "blocked":
        headline, state = f"A case in {site.first_name()}'s engineering harness is BLOCKED.", f"blocked because: {(d.get('blocked_reason') or '')[:1500]}"
    else:
        headline = (f"An urgent prod incident in {site.first_name()}'s engineering harness is escalated early, while the harness "
                    "still works on it.")
        state = (f"in progress at stage {it['status']}, NOT blocked: the harness is doing the code fix (its live sessions and "
                 "worktree are that work, not a stall). You run in parallel to decide whether a person must act on prod now; "
                 "route=harness only adds instructions to the running work.")
    prompt = PROMPT.format(me=site.first_name(), ME=site.marker(), org=site.org() or "<owner>",
                           id=item_id, kind=it["kind"], title=it["title"], headline=headline, state=state,
                           problem=((d.get("dossier") or {}).get("problem") or d.get("summary") or "")[:2000],
                           prs=", ".join(p.get("pr_url") or f"{p.get('repo')} ({'built, not opened' if p.get('wt') else 'planned, not built yet'}: {p.get('branch')})"
                                         for p in d.get("parts", [])) or "none",
                           verify=verify or "none", ask=ask or "none",
                           retaken="yes, it blocked again after that" if d.get("retaken_at") else "no")
    out = claude.orchestrator(prompt, cwd=str(DATA), schema=PLAN, timeout=900, budget_usd=6, item_id=item_id,
                              caller="escalate")
    log("escalate: plan", item=item_id, route=out["route"], still_broken=out["still_broken"], who=out.get("person_name"))
    if dry:
        return out
    it = db.item(item_id)  # the case may have moved during the session: decide on its state now, not the one it was shown
    d = it["data"]
    db.update_item(item_id, data={"escalation": {"reason": reason, "at": now(), "route": out["route"],
                                                 "evidence": out["evidence"][:1500], "who": out.get("person_name")}})
    for nc in (out.get("new_cases") or [])[:3]:  # findings on the way become cases (dedupe folds a known one)
        if nc.get("harness"):
            # a harness defect is the supervisor's incident, not a case: the product pipeline can only drop it out of
            # fence (2026-10-05/06: #509 telemetry on the personal account, #510, #511 and #535 the dead container
            # gate, about 5.6 USD of investigate, telemetry and plan, all dropped, none fixed that way)
            from . import incidents
            incidents.file("harness_finding", nc["title"][:80].lower(), nc["title"], nc["evidence"], impact=6)
            log("escalate: harness finding filed as an incident", item=item_id, title=nc["title"][:80])
            continue
        nid = db.create_item("INCIDENT" if nc.get("prod") else "TASK", "investigate", nc["title"][:90],
                             f"escalation:{item_id}:{nc['title'][:40]}", urgency=2,
                             data={"text": nc["evidence"][:4000], "summary": nc["evidence"][:600], "from_escalation": item_id})
        log("escalate: new case from a finding", item=item_id, new=nid, title=nc["title"][:80])
    if (out["route"] == "close" or not out["still_broken"]) and it["status"] in PARKED:
        _close(it, out["evidence"], out["summary"])
    elif out["route"] == "close" or not out["still_broken"]:  # in progress and nothing to escalate: the build goes on
        log("escalate: nothing for a person to do", item=item_id)
    elif out["route"] == "harness" and it["status"] != "blocked":  # still in progress: the instructions ride along
        instr = ((d.get("instructions") or "") + f"\n{TAG} {out.get('harness_instructions') or out['summary']}").strip()
        db.update_item(item_id, data={"instructions": instr})
    elif out["route"] == "harness" and d.get("retaken_at"):
        # one retake per case: its fix rounds start over once. Every block has new findings, so a new reason and a new
        # escalation, and each harness verdict reset the breaker (2026-10-06: #512 blocked on 'fix rounds exhausted' at
        # 08:14 and 08:56, was retaken both times with fix_rounds=0 and ran 4 more review rounds, $23 and no PR by 13:00)
        log("escalate: retake spent, stays blocked", item=item_id)
        _tell(it, site.t(f"⛔ *{_comms.name(it)}*: it got stuck again after I retook it ({_comms.reason(reason)}). "
                         f"I will not retake it on my own again.\n{out['summary']}",
                         f"⛔ *{_comms.name(it)}*: se trabó otra vez después de retomarlo ({_comms.reason(reason)}). "
                         f"No lo retomo solo de nuevo.\n{out['summary']}"))
    elif out["route"] == "harness" and it["status"] == "blocked" and d.get("blocked_by") == "scan_budget":
        # a scan case's spend cap is no breaker a retake resets: every new round checks it first (worker.run_stage), so
        # #1216's retake to publish blocked again 3 s later and spent its one retake for nothing
        log("escalate: scan case at its cap, stays blocked", item=item_id)
        _tell(it, site.t(f"⛔ *{_comms.name(it)}*: {_comms.reason(reason)}. I will not retake it on my own: going on is your call.\n{out['summary']}",
                         f"⛔ *{_comms.name(it)}*: {_comms.reason(reason)}. No lo retomo solo: seguir es tu decisión.\n{out['summary']}"))
    elif out["route"] == "harness" and (ref := _pr_ref(out.get("wait_for_pr"))) and any(p.get("pr") for p in d.get("parts", [])):
        # waiting is not a stage: told 'if #3067 is not merged, leave it waiting', a fix stage could only re-review and
        # push the same head (2026-10-16: #870, cometa#3057 red on main's typecheck, $5.5 of fix and self_review at 16:01
        # while a teammate's fix was still open). The wait counts as the retake: blocked again, a person decides
        db.update_item(item_id, status="waiting", next_run_in=1800, note=site.t(f"escalation: waiting for {ref[0]}#{ref[1]}", f"escalación: espera {ref[0]}#{ref[1]}"),
                       data={"waiting_for": f"waits_for_pr: {ref[0]}#{ref[1]}: {out['summary'][:200]}",
                             "wait_pr": {"repo": ref[0], "number": ref[1]}, "wait_resume": "ci_wait", "wait_since": now(),
                             **dict.fromkeys(BREAKERS, 0), "retaken_at": now()})
        _tell(it, site.t(f"⏸ *{_comms.name(it)}*: waiting for {out['wait_for_pr']} to merge; then I bring in the base and it goes on.\n{out['summary']}",
                         f"⏸ *{_comms.name(it)}*: espera que se mergee {out['wait_for_pr']}; ahí traigo la base y sigue.\n{out['summary']}"))
    elif out["route"] == "harness" and out.get("harness_next"):
        instr = ((d.get("instructions") or "") + f"\n{TAG} {out.get('harness_instructions') or out['summary']}").strip()
        db.update_item(item_id, status=out["harness_next"], next_run_in=0,
                       data={"instructions": instr, **dict.fromkeys(BREAKERS, 0), "retaken_at": now()},
                       note=site.t("escalation: retaking it", "escalación: lo retomo"))
        _tell(it, site.t(f"🔁 *{_comms.name(it)}*: it was stuck, I am retaking it.\n{out['summary']}",
                         f"🔁 *{_comms.name(it)}*: estaba trabado, lo retomo.\n{out['summary']}"))
    else:
        _ask_human(it, out)
    return out


def _pr_ref(url: str | None) -> tuple[str, int] | None:
    m = re.search(r"([a-z-]+)(?:/pull/|#)(\d+)", url or "")
    return (m.group(1), int(m.group(2))) if m and m.group(1) in CFG["repos"] else None


def _close(it: dict, evidence: str, summary: str) -> bool:
    """One close per case: whoever moves it out of PARKED first withdraws its asks and tells him. The escalation thread
    and hot.tick's settle_stored both apply a stored close, and the thread's ask deletion takes seconds on Slack
."""
    why = site.t(f"escalation: no longer needed. {evidence[:200]}", f"escalación: ya no hace falta. {evidence[:200]}")
    from . import approvals
    with db.tx():
        cur = db.item(it["id"])
        if not cur or cur["status"] not in PARKED:
            return False
        db.update_item(it["id"], status="dropped", note=why)
    for a in _live_asks(it["id"]):
        approvals.supersede(a, json.loads(a["content"] or "{}"), why)
    _tell(it, site.t(f"✅ *{_comms.name(it)}*: closing it.\n{summary}", f"✅ *{_comms.name(it)}*: lo cierro.\n{summary}"))
    return True


def settle_stored() -> int:
    """A close verdict stored before parked cases obeyed it is applied now, zero tokens, when nothing moved the case
    since."""
    n = 0
    for r in db.q("SELECT id FROM items WHERE status IN ('blocked','awaiting_approval','awaiting_human') "
                  "AND json_extract(data,'$.escalation.route')='close'"):
        it = db.item(r["id"])
        at = it["data"]["escalation"].get("at") or 0
        moved = db.q1("SELECT max(at) m FROM transitions WHERE item_id=?", it["id"])["m"] or 0
        if moved > at or any(a["created_at"] > at for a in _live_asks(it["id"])):
            continue  # a case that moved after the verdict (a build that went on, news) is judged afresh
        ev = it["data"]["escalation"].get("evidence") or ""
        if _close(it, ev, ev[:400]):
            log("escalate: stored close applied", item=it["id"])
            n += 1
    return n


def _dm_channel(uid: str) -> str | None:
    """His existing DM with that person (the token has im:read, not im:write, so a new DM cannot be opened)."""
    cursor = None
    try:
        for _ in range(10):
            d = slack._call("conversations_list", types="im", limit=1000, cursor=cursor)
            for c in d.get("channels", []):
                if c.get("user") == uid:
                    return c["id"]
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        log("escalate: DM lookup failed", uid=uid, err=str(e)[:160])
    return None


def _ask_human(it: dict, out: dict) -> None:
    from . import approvals
    uid = (out.get("person_slack_id") or "").strip()
    who = out.get('person_name') or site.t('someone with access', 'alguien con acceso')
    head = (site.t(f"🚨 *{_comms.name(it)}*: needs {who}", f"🚨 *{_comms.name(it)}*: necesita a {who}")
            + (f" ({out.get('why_them')[:160]})" if out.get("why_them") else "") + f"\n{out['summary']}")
    if uid == CFG["self_user_id"]:
        # the person is him: the message is for him to read, not a DM for him to approve sending to himself (2026-10-07:
        # #624's 15:48 ask proposed posting its decision request into his own self-DM on ✅)
        _tell(it, f"{head}\n\n" + out.get("message", ""))
        return
    ch = _dm_channel(uid) if uid.startswith("U") else None
    if not ch:  # no addressee resolved: the draft still reaches him, to forward
        _tell(it, f"{head}\n\n" + site.t("I could not find their DM; the message to send them:\n> ",
                                         "No encontré su DM; el mensaje para mandarle:\n> ") + out.get("message", "").replace("\n", "\n> "))
        return
    payload = {"channel": ch, "thread_ts": None, "text": out["message"], "which": "escalation"}
    approvals.request(it["id"], "slack_send", payload, it["status"],
                      text=f"{head}\n\n" + site.t("I send them this by DM, ✅ and it goes:\n\n> ", "Le mando esto por DM, ✅ y sale:\n\n> ")
                      + out["message"].replace("\n", "\n> "),
                      thread_ts=None, keep_status=True)
    log("escalate: ask posted", item=it["id"], to=out.get("person_name"))


def _tell(it: dict, text: str) -> None:
    """Each message is recorded on the case (data.told) for the observer's timeline: unrecorded, #1044's 08:52 message
    telling him the PR only needed `gh pr update-branch` read as 'sat silently for hours with no new escalation', twice
."""
    try:
        slack.send_self(text, thread_ts=None, topic=f"escalate-{it['id']}")
    except Exception as e:
        log("escalate: DM failed", item=it["id"], err=str(e)[:120])
        return
    told = (db.item(it["id"]) or {}).get("data", {}).get("told") or []
    db.update_item(it["id"], data={"told": told[-4:] + [[now(), text.split("\n")[0][:120]]]})
