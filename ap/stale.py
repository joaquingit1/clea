"""Stale board entries. Runs every 10 min in its own thread (hot.tick).

1. Any live ask or case bound to a PR that is MERGED or CLOSED: ask retired, case closed, its DM message closed in place.
   Zero tokens.
2. Open loops (his promises): one cheap Sonnet check at most every 6 h against the real state of his PRs. Fulfilled ->
   done; partly fulfilled -> the board line says what is left.
"""
from __future__ import annotations

import json
import re

from . import approvals, db, gh, site
from .core import log, now

_PR = re.compile(rf"({site.repo_names_re()})#(\d{{2,5}})")
LOOP = {"type": "object", "required": ["fulfilled", "remaining", "prs"], "properties": {
    "fulfilled": {"type": "boolean"}, "remaining": {"type": "string", "description": f"what is still pending, in <=12 {site.own('Spanish', site.language_name())} words; empty if fulfilled"},
    "prs": {"type": "array", "items": {"type": "string"}, "description": "when merging PRs from the list is ALL that remains: "
            "those PRs as repo#number; otherwise empty"}}}


def _state(repo: str, n: int, cache: dict) -> str:
    if (repo, n) not in cache:
        try:
            cache[(repo, n)] = gh.pr_view(repo, n).get("state") or "?"
        except Exception:
            cache[(repo, n)] = "?"
    return cache[(repo, n)]


def _close(it: dict, why: str, status: str = "done") -> None:
    from . import dm
    for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"]):
        approvals.supersede(a, json.loads(a["content"] or "{}"), why)
    db.update_item(it["id"], status=status, note=why, data={"needs_you": False})
    try:
        dm.close_case_thread(db.item(it["id"]), why)
    except Exception:
        pass


def _followed_up(d: dict) -> str | None:
    """The ts of his first message in the loop's conversation (the thread; a DM as a whole) after the promise, by the rule
    ingest applies live (LOOP_FOLLOWUP_S). The 6 h check below sees only his PRs, so a conversational promise it can
    never verify stayed 'awaiting you' when he had followed up before ingest knew the loop."""
    from .ingest import LOOP_FOLLOWUP_S, SELF
    ch, ts = d.get("channel"), d.get("promise_ts") or d.get("ts")
    if not ch or not ts:
        return None
    root = d.get("thread_ts") or ts
    r = db.q1("SELECT ts FROM events WHERE channel=? AND user=? AND (? OR thread_ts=? OR ts=?) AND CAST(ts AS REAL)>=? "
              "ORDER BY CAST(ts AS REAL) LIMIT 1", ch, SELF, ch.startswith("D"), root, root, float(ts) + LOOP_FOLLOWUP_S)
    return r["ts"] if r else None


WATCH = ("ci_wait", "ci_fix", "waiting")  # watching a PR's CI: over once it is merged or closed


SKIPPED_REVIEW = ("PR closed with review still pending", f"pending review deleted by {site.first_name()}")


def _relabel_skipped_reviews() -> int:
    """Once: reviews closed as 'done' before the skips became 'dropped' (above and reviews.track) move to 'dropped', so
    the case list stops showing them as delivered. Only items whose last move was that skip; updated_at is kept."""
    if db.kv_get("skipped_reviews_relabeled"):
        return 0
    rows = db.q("SELECT i.id, t.note FROM items i JOIN transitions t ON t.item_id=i.id AND t.dst='done' "
                "WHERE i.kind IN ('REVIEW','NUDGE') AND i.status='done' "
                "AND t.at=(SELECT MAX(at) FROM transitions WHERE item_id=i.id)")
    # sweep()'s own note for a review the PR outran, in either language
    ids = [r["id"] for r in rows if r["note"] in SKIPPED_REVIEW
           or re.fullmatch(r"[\w-]+#\d+ (?:(?:mergeado|cerrado): ya no hace falta|(?:merged|closed): no longer needed)", r["note"] or "")]
    for i in ids:
        db.x("UPDATE items SET status='dropped' WHERE id=? AND status='done'", i)
        db.x("INSERT INTO transitions(item_id, src, dst, at, note) VALUES(?,?,?,?,?)", i, "done", "dropped", now(),
             site.t("skipped review: it never reached GitHub", "review salteada: nunca llegó a GitHub"))
    db.kv_set("skipped_reviews_relabeled", {"at": now(), "n": len(ids)})
    return len(ids)


def sweep() -> list[str]:
    notes, cache = [], {}
    if n := _relabel_skipped_reviews():
        notes.append(site.t(f"{n} skipped reviews moved from done to dropped", f"{n} reviews salteadas pasaron de hechas a descartadas"))
    # clean reviews of the last 3 days that ended before a clean review approved on its own (reviews.approve_clean)
    from . import reviews as _rv
    for r in db.q("SELECT id FROM items WHERE kind='REVIEW' AND status='done' AND updated_at>? AND json_extract(data,'$.clean') "
                  "AND json_extract(data,'$.approve_tried') IS NULL AND json_extract(data,'$.approved_by_him') IS NULL", now() - 3 * 86400):
        try:
            if _rv.approve_clean(db.item(r["id"])):
                notes.append(site.t(f"#{r['id']}: review with no findings, approved", f"#{r['id']}: review sin hallazgos, aprobada"))
        except Exception as e:
            log("stale: clean approve failed", item=r["id"], err=str(e)[:160])
    # 1. PR-bound items. CI and COMMENTS watchers too (2026-09-30: #302 kept cycling waiting > ci_wait > comments_reply
    # every 30 min on cometa#2380 after it was closed; this list had REVIEW/NUDGE only and the worker's closed-PR check
    # TASK/INCIDENT/IDEA only). A merged PR still gets its reviewers answered; a closed one gets nothing.
    for r in db.q("SELECT id, kind, status, repo, pr FROM items WHERE repo IS NOT NULL AND pr IS NOT NULL AND claimed_by IS NULL AND "
                  "((kind IN ('REVIEW','NUDGE') AND status IN ('awaiting_approval','awaiting_human','review_prepare','review_find',"
                  "'review_verify','review_post')) OR (kind IN ('CI','COMMENTS') AND status NOT IN "
                  f"({','.join('?' * len(db.TERMINAL))})))", *db.TERMINAL):
        if r["kind"] == "REVIEW":
            from .reviews import is_spec
            if is_spec(re.sub(r"^review [\w-]+#\d+:\s*", "", db.item(r["id"])["title"] or "")):
                _close(db.item(r["id"]), site.t("it is a spec or prototype: a reference for the frontend, not reviewed",
                                                "es un spec o prototipo: referencia para el front, no se revisa"), status="dropped")
                notes.append(site.t(f"#{r['id']}: spec, not reviewed", f"#{r['id']}: spec, no se revisa"))
                continue
        st = _state(r["repo"], r["pr"], cache)
        if st == "CLOSED" or (st == "MERGED" and (r["kind"] in ("REVIEW", "NUDGE") or r["status"] in WATCH)):
            # a review the PR outran never reached GitHub: dropped, not done (2026-10-22, the user: "you shouldn't show or
            # marked reviews as done, if they have been skipped due to the pr already being merged")
            ref = f"{r['repo']}#{r['pr']}"
            _close(db.item(r["id"]), site.t(f"{ref} {'merged' if st == 'MERGED' else 'closed'}: no longer needed",
                                            f"{ref} {'mergeado' if st == 'MERGED' else 'cerrado'}: ya no hace falta"),
                   status="dropped" if r["kind"] in ("REVIEW", "NUDGE") else "done")
            notes.append(f"#{r['id']}: {r['repo']}#{r['pr']} {st}")
    # 1b. asks of a delivered case whose PRs are all off the table. Closed without merging: every ask goes and the case is
    # dropped (2026-10-06: schools#989 was closed because he shipped schools#990 by hand, and its team-message drafts
    # stayed in the digest). Merged: the squad and PR-channel messages present the PR for review ('va un fix, diganme que
    # les parece'), which can no longer happen, so they go; the closing notice and the rest stay (2026-10-20: #1011 and
    # #1012 merged schools#1103/#1104 within the hour and their four announcement asks stayed live for a ✅ to post them)
    for r in db.q("SELECT DISTINCT item_id FROM artifacts WHERE kind='approval' AND outcome IS NULL AND item_id IS NOT NULL"):
        it = db.item(r["item_id"])
        prs = [(p["repo"], p["pr"]) for p in (it or {}).get("data", {}).get("parts", []) if p.get("pr") and p.get("repo")]
        states = [_state(rp, n, cache) for rp, n in prs]
        if not prs or not all(s in ("MERGED", "CLOSED") for s in states):
            continue
        closed = all(s == "CLOSED" for s in states)
        why = f"{', '.join(f'{rp}#{n}' for rp, n in prs)} " + (site.t("closed without merging", "se cerró sin mergear") if closed
                                                                else site.t("already merged: the announcement no longer applies",
                                                                            "ya se mergeó: el anuncio no aplica"))
        gone = 0
        for a in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"]):
            meta = json.loads(a["content"] or "{}")
            if closed or (meta.get("keep_status") and (meta.get("payload") or {}).get("which") in ("team", "pr")):
                approvals.supersede(a, meta, why)
                gone += 1
        if closed and it["status"] not in db.TERMINAL:
            db.update_item(it["id"], status="dropped", note=why, data={"needs_you": False})
        if gone or closed:
            notes.append(f"#{it['id']}: {why}")
    # 1c. open cases raised in a thread where Status created the ticket: the case is that ticket (once per case)
    from . import cases as _cases
    for r in db.q(f"SELECT id FROM items WHERE kind IN ('TASK','INCIDENT','IDEA','REPLY') AND status NOT IN {db.TERMINAL} "
                  "AND json_extract(data,'$.channel') IS NOT NULL AND json_extract(data,'$.jira') IS NULL "
                  "AND json_extract(data,'$.ticket_checked') IS NULL ORDER BY id DESC LIMIT 15"):
        it = db.item(r["id"]); d = it["data"]
        db.update_item(it["id"], data={"ticket_checked": now()})
        key = _cases.ticket_in_thread(d["channel"], d.get("thread_ts") or d.get("ts"))
        if key and _cases.bind_ticket(it["id"], key):
            notes.append(site.t(f"#{it['id']}: it is ticket {key}", f"#{it['id']}: es el ticket {key}"))
    # 2. decisions / loops that only reference PRs, all of which are closed or merged
    for r in db.q("SELECT id, kind, title, data FROM items WHERE status IN ('awaiting_human','awaiting_approval','loop_track') AND kind IN ('OPEN_LOOP','TASK','IDEA','INCIDENT')"):
        d = json.loads(r["data"] or "{}")
        if any(p.get("pr_url") for p in d.get("parts", [])):
            continue
        refs = {(m.group(1), int(m.group(2))) for m in _PR.finditer(f"{r['title']} {d.get('text', '')} {d.get('summary', '')}")}
        if refs and all(_state(rp, n, cache) in ("MERGED", "CLOSED") for rp, n in refs):
            _close(db.item(r["id"]), site.t("the PRs it mentioned are already merged or closed", "los PRs que mencionaba ya están mergeados o cerrados"))
            notes.append(site.t(f"#{r['id']}: PRs settled", f"#{r['id']}: PRs resueltos"))
            continue
        if r["kind"] != "OPEN_LOOP":
            continue
        # a promise that only waits on PRs merging is bound to them: their state decides, zero tokens (2026-10-01: #26
        # was re-judged every 6 h by an unattributed call that knew schools#913 and cometa#1931 were open, kept that
        # only as text, and the case showed no activity in 8 days while its 09-25 'lo retomo hoy' draft stayed up)
        bound = [_state(rp, n, cache) for rp, n in d.get("loop_prs") or []]
        if bound and all(st == "MERGED" for st in bound):
            _close(db.item(r["id"]), site.t("fulfilled: the PRs it was waiting on are merged", "cumplido: los PRs que faltaban ya están mergeados"))
            notes.append(site.t(f"#{r['id']}: loop fulfilled", f"#{r['id']}: loop cumplido"))
            continue
        followed = None if bound else _followed_up(d)
        if followed:
            _close(db.item(r["id"]), site.t("you followed up in the conversation", "él siguió en la conversación"))
            notes.append(site.t(f"#{r['id']}: you followed up in the conversation ({followed})", f"#{r['id']}: él siguió en la conversación ({followed})"))
            continue
        if "OPEN" in bound or now() - (d.get("loop_checked_at") or 0) < 6 * 3600:
            continue
        # 3. his promise vs his real PRs (cheap)
        own = [(x["repo"], x["number"], json.loads(x["data"] or "{}").get("title", "")) for x in db.q("SELECT repo, number, data FROM prs WHERE own=1")]
        listing = "\n".join(f"- {rp}#{n} [{_state(rp, n, cache)}] {t[:90]}" for rp, n, t in own[:40])
        said = d.get("promise_ts") or d.get("ts")
        try:
            from . import claude
            v = claude.lean("sweep", f"{site.first_name()} promised this (an open loop): {r['title']}\nContext: {(d.get('text') or d.get('summary') or '')[:800]}\n"
                            + (f"Said on {db.fmt_time(float(said))}; now is {db.fmt_time(now())}. A promise tied to a moment that has "
                               f"passed (a meeting, {site.own(chr(39) + 'hoy' + chr(39), 'today')}) is over: fulfilled=true.\n" if said else "")
                            + f"\nHis pull requests and their state now:\n{listing}\n\nIs the promise fulfilled? If only part is done, say what remains.",
                            model="sonnet", cwd=site.primary_path(), schema=LOOP, tools=(), item_id=r["id"])
        except Exception as e:
            log("stale: loop check failed", item=r["id"], err=str(e)[:100])
            continue
        db.update_item(r["id"], data={"loop_checked_at": now()})
        if v.get("fulfilled"):
            _close(db.item(r["id"]), site.t("fulfilled: what was promised is merged", "cumplido: lo prometido ya está mergeado"))
            notes.append(site.t(f"#{r['id']}: loop fulfilled", f"#{r['id']}: loop cumplido"))
        elif v.get("remaining"):
            mine = {(rp, n) for rp, n, _ in own}
            prs = [[m.group(1), int(m.group(2))] for m in map(_PR.fullmatch, v.get("prs") or []) if m]
            prs = prs if prs and all(tuple(p) in mine for p in prs) else []
            db.update_item(r["id"], data={"gist": f"{r['title'].replace('(slack) ', '')[:50]}: " + site.t("still to do: ", "falta ") + v['remaining'][:80],
                                          "loop_prs": prs})
            log("stale: loop still open", item=r["id"], remaining=v["remaining"][:80], waits_on=[f"{a}#{b}" for a, b in prs])
    if notes:
        log("stale sweep", notes=notes)
    return notes


def what_it_needs(it: dict, cache: dict | None = None) -> tuple[str, str]:
    """The single source of truth for 'does this need the user?'. Returns (bucket, why):
    'you' (a live ask or a decision only he can make), 'review' (a PR of ours waits on others), 'progress' (being worked),
    'done' (merged / closed / resolved)."""
    cache = cache if cache is not None else {}
    d = it["data"]
    live_ask = db.q1("SELECT 1 FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", it["id"])
    prs = [(p["repo"], p["pr"]) for p in d.get("parts", []) if p.get("pr")] + [tuple(p) for p in d.get("loop_prs") or []]
    states = [_state(r, n, cache) for r, n in prs]
    if prs and all(s == "MERGED" for s in states):
        return "done", site.t("merged", "mergeado")
    if prs and all(s in ("MERGED", "CLOSED") for s in states):
        return "done", site.t("closed", "cerrado")
    if live_ask:
        return "you", site.t("an ask of yours is waiting", "tiene un pedido tuyo pendiente")
    if it["status"] == "awaiting_human" and (d.get("card_need") or "").strip():
        # a question only he can answer outranks a PR waiting on others (2026-10-22: #512 asked for his ✅ on two
        # fixes while its PRs were open, and showed as 'Esperando' with nothing to press)
        return "you", d["card_need"][:120]
    if it["kind"] == "REVIEW":  # a review never waits on him: posted, clean or redone (reviews.refused)
        return "progress", it["status"]
    if prs and any(s == "OPEN" for s in states):
        return "review", site.t("open PR waiting for review or merge", "PR abierto esperando review o merge")
    if it["kind"] == "OPEN_LOOP" and it["status"] != "awaiting_human":
        return "progress", (d.get("gist") or it["title"])[:120]  # loop_track: the harness is still watching it
    if it["status"] in ("awaiting_human",):
        # a loop parks here once his promise went stale and the message to send is drafted: only he can send it
        #
        return "you", ((d.get("gist") or site.t("your promise is overdue, the message is ready", "tu promesa venció, el mensaje está listo"))[:120]
                       if it["kind"] == "OPEN_LOOP" else site.t("your decision", "decisión tuya"))
    return "progress", it["status"]
