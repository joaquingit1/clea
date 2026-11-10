"""The living board: ONE self-DM message edited in place (v1 proved per-item bundles
drown; 1 of 29 ever got a reaction). Full draft texts go as thread replies under the
board, once per item; genuinely new urgent needs get a fresh ping (edits don't notify)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from . import db, merge_order, outbound, site, slack
from . import comms as _comms
from .core import CFG, item_dir, log, now

MAX = 3400


def _needs_you() -> list[tuple[dict, str]]:
    out = []
    for r in db.q("SELECT id FROM items WHERE status IN ('awaiting_approval','awaiting_human','blocked') ORDER BY urgency DESC, updated_at DESC"):
        it = db.item(r["id"])
        d = it["data"]
        k = it["kind"]
        if it["status"] == "awaiting_approval":
            line = site.t(f"waiting for your ok: {_comms.name(it)} (I asked you in a separate message)",
                          f"esperando tu ok: {_comms.name(it)} (te lo pregunté en un mensaje aparte)")
        elif it["status"] == "blocked":
            line = site.t(f"stuck: {it['title'][:60]} ({d.get('blocked_reason', '')[:120]})",
                          f"atascado: {it['title'][:60]} ({d.get('blocked_reason', '')[:120]})")
        elif k == "REVIEW":
            url = d.get("url") or site.pr_url(it["repo"], it["pr"])
            if d.get("comments") and (d.get("posted") or {}).get("shadow"):
                n = d["comments"]
                pr = f"<{url}|{it['repo']}#{it['pr']}>"
                line = site.t(f"(shadow) review of {pr} ready, {n} comment{'s' if n != 1 else ''}: not created on GitHub (text in the thread)",
                              f"(shadow) review de {pr} armada, {n} comentario{'s' if n != 1 else ''}: "
                              f"no se creo en GitHub (texto en el hilo)")
            elif d.get("comments"):
                n = d["comments"]
                pr = f"<{url}|{it['repo']}#{it['pr']}>"
                line = site.t(f"review ready {pr}: {n} comment{'s' if n != 1 else ''} in a pending review, edit it and submit it on GitHub",
                              f"review lista {pr}: {n} comentario{'s' if n != 1 else ''} en una review pendiente, editala y submitteala en GitHub")
            else:
                pr, passes = f"<{url}|{it['repo']}#{it['pr']}>", len(CFG['quality']['review_finder_passes'])
                line = site.t(f"review {pr}: no findings in {passes} passes, it can be approved",
                              f"review {pr}: sin hallazgos en {passes} pasadas, se puede aprobar")
        elif k == "REPLY":
            link = slack.permalink(d["channel"], d.get("thread_ts") or d["ts"]) if d.get("channel") else ""
            line = site.t(f"someone is waiting in <{link}|this thread>: {it['title'][:60]} (draft in this message's thread)",
                          f"te esperan en <{link}|este hilo>: {it['title'][:60]} (borrador en el hilo de este mensaje)")
        elif k in ("TASK", "INCIDENT", "IDEA"):
            if d.get("pr_links"):
                line = site.t(f"draft PR ready: {it['title'][:60]} ", f"PR draft listo: {it['title'][:60]} ") + " ".join(f"<{u}|PR>" for u in d["pr_links"])
            elif d.get("shadow_ready"):
                line = site.t(f"(shadow) built and verified locally: {it['title'][:60]}", f"(shadow) armado y verificado local: {it['title'][:60]}")
            elif d.get("proposal_text"):
                line = site.t(f"proposal for the team: {it['title'][:60]} (draft below)", f"propuesta para el squad: {it['title'][:60]} (borrador abajo)")
            else:
                line = f"{_comms.name(it)}"
        elif k == "COMMENTS":
            pr = f"<{d['parts'][0].get('pr_url')}|{it['repo']}#{it['pr']}>"
            line = site.t(f"replies to comments on {pr} (drafts below)", f"respuestas a comentarios en {pr} (borradores abajo)")
        elif k == "OPEN_LOOP":
            line = site.t("yours to do: ", "pendiente tuyo: ") + d.get('loop', {}).get('action', it['title'])[:90]
        else:
            line = it["title"][:80]
        out.append((it, line))
    return out


def _thread_text(it: dict) -> str | None:
    d = it["data"]
    if it["kind"] == "REPLY" and d.get("reply_text"):
        return f"*{it['title'][:80]}*\n{d['reply_text']}"
    if d.get("proposal_text"):
        return site.t(f"*proposal: {_comms.name(it)}*\n", f"*propuesta: {_comms.name(it)}*\n") + d['proposal_text']
    if d.get("texts"):
        t = d["texts"]
        return (f"*{_comms.name(it)}*\n" + site.t("source thread:\n", "hilo original:\n") + t.get('thread_reply', '')
                + site.t("\n\nreview request:\n", "\n\npedido de review:\n") + t.get('review_ask', ''))
    if it["kind"] == "REVIEW" and (d.get("posted") or {}).get("shadow"):
        f = item_dir(it["id"]) / "review.json"
        if f.exists():
            rv = json.loads(f.read_text())
            return (f"*review {it['repo']}#{it['pr']} (shadow)*\n{rv['body']}\n\n"
                    + "\n\n".join(f"{c['path']}:{c['line']}\n{c['body']}" for c in rv["comments"]))
    if d.get("comment_replies"):
        return site.t(f"*replies {it['repo']}#{it['pr']}*\n", f"*respuestas {it['repo']}#{it['pr']}*\n") + "\n\n".join(f"{r['text']}" for r in d["comment_replies"])
    if d.get("loop_draft"):
        return f"*{d['loop_draft'].get('where', '')}*\n{d['loop_draft']['text']}"
    return None


def render() -> str:
    t = dt.datetime.now()
    needs = _needs_you()
    active = db.q("SELECT id, kind, status, title FROM items WHERE status NOT IN "
                  "('done','dropped','expired','failed','awaiting_human','blocked') ORDER BY urgency DESC, updated_at DESC")
    start = int(dt.datetime.combine(dt.date.today(), dt.time()).timestamp())
    today = {r["status"]: r["c"] for r in db.q(
        "SELECT dst AS status, count(*) c FROM transitions WHERE at>=? GROUP BY dst", start)}
    spend = db.spent_today("claude")
    muse_calls = (db.q1("SELECT calls FROM budget WHERE day=? AND tier='muse'", db.today()) or {"calls": 0})["calls"]
    lines = [f"*autopilot v2* · {CFG['mode']} · {t:%H:%M}"]
    if needs:
        lines.append(site.t(f"\n*needs you ({len(needs)})*", f"\n*te necesita ({len(needs)})*"))
        lines += [f"• {l}" for _, l in needs[:12]]
    blocks, nudges = [], []
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'pr:%'"):
        st = json.loads(r["v"])
        s = st.get("summary") or {}
        if st.get("merge_block") and (s.get("approved") or s.get("review") == "APPROVED"):
            blocks.append(f"• <{s.get('url')}|{r['k'][3:]}> " + site.t(f"approved, but I am not merging: {st['merge_block'][:160]}",
                                                                          f"aprobado pero no mergeo: {st['merge_block'][:160]}"))
        elif st.get("merge_ready"):
            blocks.append(f"• <{s.get('url')}|{r['k'][3:]}> " + site.t("ready to merge: the merge is yours", "listo para mergear: el merge es tuyo"))
        if st.get("nudge") and now() - st["nudge"]["at"] < 8 * 3600:
            nudges.append(f"• {st['nudge']['text']}")
        if st.get("announce") and now() - st["announce"]["at"] < 8 * 3600:
            nudges.append(f"• {st['announce']['text']}")
        if st.get("blocked"):
            blocks.append(f"• {r['k'][3:]}: {st['blocked']}")
    if blocks:
        lines.append("\n*merge / babysit*")
        lines += blocks[:6]
    if nudges:
        lines.append(site.t("\n*to copy*", "\n*para copiar*"))
        lines += nudges[:6]
    order = merge_order.plan_text()
    if order:
        lines.append(site.t("\n*merge order*", "\n*orden de merge*"))
        lines += [f"• {o}" for o in order[:6]]
    if active:
        lines.append(site.t(f"\n*in progress ({len(active)})*", f"\n*en curso ({len(active)})*"))
        lines += [f"• {r['title'][:60]} [{r['status']}]" for r in active[:8]]
    done, delivered, cap = today.get('done', 0), today.get('awaiting_human', 0), CFG['claude']['daily_usd_cap']
    lines.append(site.t(f"\ntoday: {done} closed · {delivered} delivered · claude ${spend:.2f}/{cap} · muse {muse_calls} calls (free)",
                        f"\nhoy: {done} cerrados · {delivered} entregados · "
                        f"claude ${spend:.2f}/{cap} · muse {muse_calls} llamadas (gratis)"))
    text = "\n".join(lines)
    return text if len(text) <= MAX else text[:MAX - 20] + site.t("\n… (cut)", "\n… (recortado)")


def refresh(force: bool = False) -> None:
    from .digest import render as digest_render
    text = digest_render()
    h = hashlib.sha1(text.split("\n", 1)[-1].encode()).hexdigest()  # ignore the clock line
    st = db.kv_get("board", {})
    if not force and st.get("hash") == h:
        return
    if not force and now() - st.get("at", 0) < CFG["schedule"]["board_min_interval_s"]:
        return
    ts = st.get("ts")
    if ts and st.get("day") != db.today():
        from . import dm
        dm._delete_thread(ts)  # yesterday's note and its detail reply go; the DM has exactly one board
        ts = None
    ts = outbound.board_post(text, ts)
    # read again and merged: the scheduler's digest_due shares this key, and written from scratch every changed board
    # between the digest hour and the 5-min watchdog wiped it, with the re-asks of expired asks it carries. On 10-15
    # the 17:30 digest was lost that way, #734's ask (expired 14:18) waited for 08:30 and eight came in one burst
    db.kv_set("board", {**(db.kv_get("board") or {}), "ts": ts, "hash": h, "at": now(), "day": db.today()})
    # (no separate pings: whatever needs him is in the board and in its case thread)
