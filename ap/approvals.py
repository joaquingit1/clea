"""Ask-first gate for outward actions.

request() posts a NEW self-DM message with the exact preview (a new message notifies;
board edits don't). poll() reads his answer:
  ✅ / 👍 reaction, or a reply like "dale" / "ok" / "si" / "mandalo"  -> execute, as him
  ❌ / 👎 reaction, or a reply like "no" / "descartalo"              -> discard
  any other reply in the thread                                      -> revision: the item
                                                                         goes back to its
                                                                         drafting stage with
                                                                         his instruction
Only replies carrying client_msg_id count (API-posted messages never have it: v1 rule), and
only reactions by his own user id. Every outcome is a learning signal.
"""
from __future__ import annotations

import json
import threading
import re
from datetime import datetime

from . import db, gh, site, slack
from .core import CFG, log, now

APPROVE_WORDS = re.compile(r"^\s*(?:(?:dale|ok|okay|okey|oka|si|sí|s[ií]+|mandalo|mandale|mand[aá]|enviar|envialo|publicalo|"
                           r"va|listo|perfecto|joya|genial|aprobado|go|yes|yep|yeah|yup|sure|send(?: it)?|ship it|do it|"
                           r"go ahead|go for it|approved?|lgtm|sounds good|looks good|perfect|great|good|post it|"
                           r"publish(?: it)?|please do)\b|👍|✅|🚀|👌)", re.I)
REJECT_WORDS = re.compile(r"^\s*(?:(?:no|nop|nope|nah|descartalo|descart[aá]|borralo|cancel(?:a|alo)?|cancel it|discard(?: it)?|"
                          r"drop it|delete it|skip(?: it)?|don['’]?t(?: send(?: it)?)?|do not send(?: it)?|no thanks|"
                          r"never ?mind|leave it)\s*[.!]*\s*$|❌|👎)", re.I)
# an answer that opens with a yes and then asks for something is a change request, not a go ("ok pero sacale el saludo",
# "yes but drop the last line"): read as approval, the draft went out as it was
BUT = re.compile(r"\b(?:pero|aunque|salvo|excepto|menos|sin|antes|cambi\w+|sac[aá]\w*|agreg\w+|quit[aá]\w*|pon[eé]\w*|"
                 r"but|except|though|however|instead|without|first|chang\w+|remov\w+|add|drop|replace|rephrase|shorter|"
                 r"longer|after|unless|wait)\b", re.I)
KIND_LABEL = {"slack_send": site.t("send this message", "mando este mensaje"),
              "github_review": site.t("leave this review", "dejo esta review"),
              "github_replies": site.t("answer these comments", "respondo estos comentarios")}
# the answer line every ask ends with; code that cuts an ask back to its body splits on it, in either language
ASK_LINE = site.t("✅ send it · ❌ don't · or reply here with what to change", "✅ lo mando · ❌ no · o respondé acá qué cambiar")
_ASK_LINE_AT = re.compile(r"\n\n✅ (?:lo mando|send it)")


def ask_body(text: str) -> str:
    """An ask's text without its answer line (and whatever follows it)."""
    return _ASK_LINE_AT.split(text or "", maxsplit=1)[0]
# every schema of the `draft` checker carries it: with only the message fields, its objection went into the message
#
HOLD = {"type": "string", "description": f"for {site.first_name()}, never sent: why this should not go out as is (empty when fine)"}


def _describe(kind: str, p: dict) -> tuple[str, str]:
    """(title line, body) for the ask message: short header, then the exact text to send."""
    if kind == "slack_send":
        where = (f"<{slack.permalink(p['channel'], p['thread_ts'])}|{site.t('in the thread', 'en el hilo')}>" if p.get("thread_ts")
                 else site.t(f"to <#{p['channel']}>", f"en <#{p['channel']}>"))
        return site.t(f"Send this message {where}", f"Mando este mensaje {where}"), "\n".join(f"> {l}" for l in p["text"].splitlines())
    if kind == "github_review":
        n = len(p["comments"])
        link = f"<{site.pr_url(p['repo'], p['pr'])}|{p['repo']}#{p['pr']}>"
        if p.get("event") == "APPROVE":
            return (site.t(f"Approve {link} for you (the review found no problems)",
                           f"Apruebo {link} en tu nombre (la review no encontró problemas)"),
                    f"> {p['body']}" if p.get("body") else "")
        head = site.t(f"Leave this review on {link} ({p.get('moved') or ''}{n} comment{'s' if n != 1 else ''}, no approval)",
                      f"Dejo esta review en {link} ({p.get('moved') or ''}{n} comentario{'s' if n != 1 else ''}, sin approve)")
        body = [f"> {p['body']}"] + [f"*{c['path'].split('/')[-1]}:{c['line']}*\n" + "\n".join(f"> {l}" for l in c["body"].splitlines())
                                     for c in p["comments"]]
        return head, "\n\n".join(body)
    if kind == "github_replies":
        n = len(p["replies"])
        link = f"<{site.pr_url(p['repo'], p['pr'])}|{p['repo']}#{p['pr']}>"
        head = site.t(f"Answer {n} comment{'s' if n != 1 else ''} on {link}", f"Respondo {n} comentario{'s' if n != 1 else ''} en {link}")
        to = site.t("to", "a")
        return head, "\n\n".join(f"{to} {r.get('user', '')}:\n" + "\n".join(f"> {l}" for l in r["text"].splitlines())
                                   for r in p["replies"])
    return kind, json.dumps(p, ensure_ascii=False)[:1500]


def _quoted_forms(t: str) -> list[str]:
    """The ways a draft is embedded in an ask text (verbatim, and the two quote styles in use)."""
    lines = t.split("\n")
    return ["\n".join("> " + l if l.strip() else ">" for l in lines), "> " + t.replace("\n", "\n> "),
            "\n".join(f"> {l}" for l in lines), t]


def lint_payload(payload: dict, text: str | None = None) -> tuple[dict, str | None, str]:
    """Every Slack draft passes the deterministic voice lint before he sees it (ap/voice_lint.py): the payload is
    fixed in place, the ask text that embeds it is kept in sync, and the note says what changed and what he should
    look at. A text he typed himself (verbatim) is never touched."""
    if payload.get("verbatim") or not (payload.get("text") or "").strip():
        return payload, text, ""
    from . import voice_lint
    original, voiced = payload["text"], ""
    if payload.get("channel") != CFG["self_dm_channel"] and site.ASSISTANT.search(original):
        payload = {**payload, "text": _own_voice(original)}
        voiced = site.t("Rewritten once: the draft named the assistant.", "Reescrito una vez: el borrador nombraba al asistente.")
    try:
        lr = voice_lint.lint(payload["text"], payload.get("channel"), thread_ts=payload.get("thread_ts"))
    except Exception as e:
        log("voice lint failed", err=f"{type(e).__name__}: {e}"[:200])
        if not voiced:
            return payload, text, ""
        lr = voice_lint.Lint(text=payload["text"], audience="", thread_ts=payload.get("thread_ts"))
    fixed = {**payload, "text": lr.text, "thread_ts": lr.thread_ts}
    if text is not None and lr.text != original:
        for old, new in zip(_quoted_forms(original), _quoted_forms(lr.text)):
            text = text.replace(old, new)
    return fixed, text, "\n".join(x for x in (lr.note(), voiced) if x)


def _own_voice(draft: str) -> str:
    """A message for other people is the user's own: it never names the assistant ("Clea checked it for me"). The send
    gate (slack.check_text) refused such a text, but only after the user had read it and approved it, so the approval
    ended in an error. Here, before anyone sees the draft, a small model rewrites it once in the first person; a draft
    that still names the assistant fails its stage instead of reaching an ask."""
    from . import claude
    try:
        out = claude.lean("draft", f"This Slack message will be sent by {site.first_name()} under their own name, to other "
                          "people. It mentions their assistant (Clea), which the reader must never see: nobody but "
                          f"{site.first_name()} knows about it. Rewrite it in the first person, as what {site.first_name()} "
                          "did, checked or thinks, with the same facts, language, tone and length. Change nothing else.\n\n"
                          f"<message>\n{draft}\n</message>", model="sonnet", cwd=site.root(), tools=(),
                          schema={"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
        fixed = (out.get("text") or "").strip()
    except Exception as e:
        raise ValueError(f"the draft names the assistant and the rewrite failed: {type(e).__name__}: {e}"[:300])
    if not fixed or site.ASSISTANT.search(fixed):
        raise ValueError("the draft names the assistant (a message for other people is written in the user's own voice)")
    log("approval: draft rewritten without the assistant's name")
    return fixed


def ask_suffix(note: str = "") -> str:
    """What request() appends to an ask's text: the lint note and the answer line. report.post budgets for it, since
    an ask edited into the case root cannot grow past slack.MAXLEN (#507 on 10-06 failed announce_check 3 times)."""
    return (("\n\n" + note if note else "") + "\n\n" + ASK_LINE
            + ("\n" + site.t("_test mode: even if you approve, nothing is published yet_",
                             "_modo prueba: aunque digas dale, todavía no se publica_") if CFG.get("mode") != "act" else ""))


def request(item_id: int, kind: str, payload: dict, revise_status: str, text: str | None = None,
            thread_ts: str | None = None, edit_ts: str | None = None, keep_status: bool = False,
            lint_note: str | None = None) -> dict:
    """Ask the user; the item waits in awaiting_approval until he answers. With `text` (a case
    report that already carries the sendable text verbatim) the report itself is the ask;
    with `thread_ts` it lands inside that case's self-DM thread. A slack_send payload is linted first;
    `lint_note` is the note of a lint the caller already ran (report.post), shown instead of a second one."""
    from .outbound import acting
    if kind == "slack_send" and not str(payload.get("text") or "").strip():
        # never an ask to send nothing; the stage
        # fails and retries instead of showing him a blank message to approve
        raise ValueError(f"approval for item {item_id}: the draft to send is empty")
    note = ""
    if kind == "slack_send":
        payload, text, note = lint_payload(payload, text)
        note = lint_note if lint_note is not None else note
    if text is None:
        head, body = _describe(kind, payload)
        text = f"🟡 *{head}?*\n{body}"
    text += ask_suffix(note)
    ts, stale = None, []
    if acting("urgent_pings"):
        if edit_ts:  # the case root itself becomes the (single, current) ask; side asks are other messages and stay
            # (2026-10-01: each announce killed the case's own team and PR-channel asks, 14 on 7 cases, and the
            # babysit's nudge for cometa#2436 then asked the squad a second time from #409)
            for old in db.q("SELECT * FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL", item_id):
                if not json.loads(old["content"] or "{}").get("keep_status"):
                    db.set_artifact_outcome(old["id"], "superseded", {"why": "replaced by the edited case message"})
            try:  # put on the case message before this ask existed: they answer nothing here
                stale = _his_reactions(edit_ts)
            except Exception as e:
                log("approval: reactions failed", err=str(e)[:160])
            slack.edit_self(edit_ts, text, topic=f"approval-{item_id}")
            ts, thread_ts = edit_ts, None
        else:
            ts = slack.send_self(text, thread_ts=thread_ts, topic=f"approval-{item_id}")["ts"]
    art = db.add_artifact(item_id, "approval", ts or "", json.dumps(
        {"kind": kind, "payload": payload, "revise_status": revise_status, "seen_reply": ts or "0",
         "stale_reactions": stale, "thread_ts": thread_ts or ts, "ask_text": text[:3500], "keep_status": keep_status,
         "act": CFG.get("mode") == "act"}, ensure_ascii=False))
    if keep_status:  # a side ask (e.g. the team message for a PR): the case keeps its own lifecycle
        db.update_item(item_id, data={"needs_you": True})
    else:
        db.update_item(item_id, status="awaiting_approval", data={"approval_artifact": art, "needs_you": True})
    log("approval requested", item=item_id, kind=kind, ts=ts)
    return {"artifact": art, "ts": ts}


def _his_reactions(ts: str) -> list[str]:
    """His reactions on a self-DM message, skin tones stripped."""
    r = slack._call("reactions_get", channel=CFG["self_dm_channel"], timestamp=ts)
    return [rx["name"].split("::")[0] for rx in (r.get("message") or {}).get("reactions", [])
            if CFG["self_user_id"] in rx.get("users", [])]


def _cursor(a, meta: dict) -> str:
    """Only what he says after an ask was asked answers it. An ask edited into the case root has the root's ts, and
    as a cursor that replays every older reply in the thread."""
    return max(meta.get("seen_reply") or a["ref"] or "0", f"{a['created_at']:.6f}", key=float)


def _decision(ts: str, seen: str, thread_ts: str | None = None, stale: list | tuple = ()) -> tuple[str | None, str, str]:
    """(decision, reply_text, newest_seen_ts) from reactions + his thread replies. `stale`: reactions the message
    already carried when the ask was edited into it."""
    ch = CFG["self_dm_channel"]
    me = CFG["self_user_id"]
    g = CFG.get("greenlight", {})
    try:
        for name in _his_reactions(ts):
            if name in stale:
                continue
            if name in g.get("approve_reactions", ["white_check_mark", "+1"]) + ["heavy_check_mark", "rocket", "ok_hand"]:
                return "approve", "", seen
            if name in g.get("reject_reactions", ["x", "-1"]):
                return "reject", "", seen
    except Exception as e:
        log("approval: reactions failed", err=str(e)[:160])
    newest = seen
    for m in slack.replies(ch, thread_ts or ts):
        if m["ts"] == ts or float(m["ts"]) <= float(seen) or m.get("user") != me or not m.get("client_msg_id"):
            continue
        newest = max(newest, m["ts"], key=float)
        text = (m.get("text") or "").strip()
        if APPROVE_WORDS.match(text) and len(text) < 40 and not BUT.search(text):
            return "approve", text, newest
        if REJECT_WORDS.match(text) and len(text) < 40:
            return "reject", text, newest
        return "revise", text, newest
    return None, "", newest


_LOCAL = threading.local()


def execute(kind: str, p: dict, ask_ts: str | None = None) -> dict:
    """Every outbound write goes through the guard (it re-checks his ✅ on `ask_ts` itself, or, for an approval made in
    the console app, opens its own Touch ID dialog)."""
    from .outbound import acting, _shadow
    from .guardcall import act as _act
    local = {"approval": "local"} if getattr(_LOCAL, "on", False) else {}
    act = lambda action, **kw: _act(action, **kw, **(local if action in ("slack_post", "gh_review", "gh_comment_other_pr") else {}))
    if kind == "slack_send":
        if not acting("send_approved_slack"):
            return _shadow("slack_send", **p)
        text = slack.check_text(p["text"], p["channel"])
        if p["channel"] == CFG["self_dm_channel"]:
            return act("slack_post_self", text=text, thread_ts=p.get("thread_ts"))
        return act("slack_post", channel=p["channel"], text=text, thread_ts=p.get("thread_ts"), ask_ts=ask_ts)
    if kind == "github_review":
        if not acting("submit_approved_reviews"):
            return _shadow("github_review", **p)
        return act("gh_review", repo=p["repo"], pr=p["pr"], head_sha=p["head_sha"], body=p["body"],
                   comments=p["comments"], event=p.get("event") or "COMMENT", ask_ts=ask_ts)
    if kind == "github_replies":
        if not acting("submit_approved_reviews"):
            return _shadow("github_replies", **p)
        out = 0
        for r in p["replies"]:
            resolve = r.get("thread_id") if (r.get("thread_id") and ((p.get("resolve_bot_threads") and r.get("bot")) or r.get("resolve"))) else None
            act("gh_reply_own_pr", repo=p["repo"], pr=p["pr"], body=r["text"],
                reply_to=(r.get("reply_to") or r["comment_id"]) if r.get("review_comment") else None, resolve_thread_id=resolve)
            out += 1
        return {"posted": out}
    raise ValueError(kind)


def decide_local(art_id: int, decision: str, text: str = "") -> str:
    """An ask answered in the console app instead of with a reaction in Slack. Same effects as the
    Slack path; an approval reaches the guard marked local, and the guard asks for his Touch ID before it sends.
    decision: approve | reject | revise (text = what to change)."""
    a = db.q1("SELECT * FROM artifacts WHERE id=? AND kind='approval'", art_id)
    if not a:
        raise ValueError(site.t("that ask does not exist", "no existe ese pedido"))
    if a["outcome"] is not None:
        raise ValueError(site.t(f"that ask is already settled ({a['outcome']})", f"ese pedido ya está resuelto ({a['outcome']})"))
    if decision not in ("approve", "reject", "revise"):
        raise ValueError(decision)
    if decision == "revise" and not text.strip():
        raise ValueError(site.t("say what to change", "falta qué cambiar"))
    meta = json.loads(a["content"])
    it = db.item(a["item_id"])
    _LOCAL.on = True
    try:
        (_apply_side if meta.get("keep_status") else _apply)(a, meta, it, decision, text if decision == "revise" else "")
    finally:
        _LOCAL.on = False
    row = db.q1("SELECT outcome, outcome_data FROM artifacts WHERE id=?", art_id)
    if decision == "approve" and (row["outcome"] or "") != "approved":
        err = json.loads(row["outcome_data"] or "{}").get("err") or site.t("it did not run", "no se ejecutó")
        raise RuntimeError(err)
    log("approval decided in the console", art=art_id, item=it["id"] if it else None, decision=decision)
    return row["outcome"] or decision


def _confirm(ts: str, text: str, item_id: int) -> None:
    try:  # unique text: slack.sh blocks byte-identical sends within 30 min
        slack.send_self(f"{text} (#{item_id})", thread_ts=ts)
    except Exception as e:
        log("approval confirmation failed", item=item_id, err=str(e)[:200])


def _side_sent(meta: dict, it: dict) -> str:
    """What a sent side message means for its case, whoever sent it (the guard on his ✅, or he himself)."""
    which = (meta.get("payload") or {}).get("which") or "team"
    if which == "closing":
        from . import closing
        closing.after_notified(db.item(it["id"]))
        return which
    it = db.item(it["id"])
    tm = it["data"].get("team_message") or {}
    db.update_item(it["id"], data={"team_message": {**tm, "sent": {**(tm.get("sent") or {}), which: now()}}})
    return which


def _approved_effects(it: dict, meta: dict, sent_text: str) -> None:
    """What an executed slack_send teaches, whether the guard sent it on his ✅ or he sent his own version:
    an ops answer becomes a precedent."""
    if not (it["kind"] == "REPLY" and it["data"].get("ops_request") and meta["kind"] == "slack_send"):
        return
    try:
        from . import precedents
        d = it["data"]
        precedents.add(request=d.get("gist") or d.get("summary") or d.get("text", "")[:300], resolution=(sent_text or "")[:800],
                       kind=" ".join(d.get("precedent_terms") or [])[:80], school=d.get("school") or "", who=site.first_name(),
                       links=[slack.permalink(d["channel"], d["thread_ts"])] if d.get("channel") and d.get("thread_ts") else [],
                       source_item=it["id"])
    except Exception as e:
        log("precedent record failed", item=it["id"], err=str(e)[:160])


def _apply_side(a, meta, it, decision, text) -> None:
    """A side ask (team message for a PR): send on ✅, drop on ❌, redraft on a reply. The case's status
    is never touched."""
    root = meta.get("thread_ts") or a["ref"]
    if decision == "approve":
        try:
            res = execute(meta["kind"], meta["payload"], ask_ts=a["ref"])
        except Exception as e:
            if getattr(_LOCAL, "on", False) and "local approval" in str(e):
                raise  # he said no (or walked away) at the Touch ID dialog: the ask stays open, nothing failed
            db.set_artifact_outcome(a["id"], "failed", {"err": str(e)[:500]})
            _confirm(root, site.t(f"⚠️ it failed to run: {str(e)[:200]}", f"⚠️ falló al ejecutar: {str(e)[:200]}"), it["id"])
            return
        db.set_artifact_outcome(a["id"], "approved", {"executed": str(res)[:300]})
        which = _side_sent(meta, it)
        _confirm(root, site.t("✅ posted in the thread", "✅ avisado en el hilo") if which == "closing" else site.t("✅ sent", "✅ mandado"), it["id"])
        return
    if decision == "reject":
        db.set_artifact_outcome(a["id"], "rejected")
        tm = it["data"].get("team_message") or {}
        which = (meta.get("payload") or {}).get("which") or "team"
        db.update_item(it["id"], data={"team_message": {**tm, "rejected": {**(tm.get("rejected") or {}), which: True}}})
        return
    db.set_artifact_outcome(a["id"], "revised", {"feedback": text[:500]})
    from . import tasks
    tasks.team_message(db.item(it["id"]), revision=text)


def _apply(a, meta, it, decision, text) -> None:
    root = meta.get("thread_ts") or a["ref"]
    if decision == "approve":
        try:
            res = execute(meta["kind"], meta["payload"], ask_ts=a["ref"])
        except Exception as e:
            if getattr(_LOCAL, "on", False) and "local approval" in str(e):
                raise  # he said no (or walked away) at the Touch ID dialog: the ask stays open, nothing failed
            db.set_artifact_outcome(a["id"], "failed", {"err": str(e)[:500]})
            db.update_item(it["id"], status="blocked", data={"blocked_reason": site.t(f"approved, but it failed to run: {e}", f"aprobado pero falló al ejecutar: {e}")[:600]})
            _confirm(root, site.t(f"⚠️ it failed to run: {str(e)[:200]}", f"⚠️ falló al ejecutar: {str(e)[:200]}"), it["id"])
            return
        db.update_item(it["id"], status=it["data"].get("after_approval", "done"), note="approved and executed",
                       data={"executed": str(res)[:300], "needs_you": False})
        _approved_effects(it, meta, meta["payload"].get("text", ""))
        db.set_artifact_outcome(a["id"], "approved", {"reply": text})
        db.add_signal("approval", {"outcome": "approved", "kind": meta["kind"], "payload": meta["payload"]}, it["id"], a["id"])
        _confirm(root, site.t("✅ done", "✅ hecho") if CFG.get("mode") == "act" else site.t("✅ noted (test mode)", "✅ registrado (modo prueba)"), it["id"])
    elif decision == "reject":
        db.update_item(it["id"], status="dropped", note=f"{site.first_name()} rejected it")
        db.set_artifact_outcome(a["id"], "rejected", {"reply": text})
        db.add_signal("approval", {"outcome": "rejected", "kind": meta["kind"], "payload": meta["payload"],
                                   "reply": text}, it["id"], a["id"])
    elif it["kind"] == "NUDGE":  # his reply IS the message he wants sent
        db.set_artifact_outcome(a["id"], "revised", {"instruction": text})
        request(it["id"], "slack_send", {**meta["payload"], "text": text, "verbatim": True}, "awaiting_approval", thread_ts=root)
    else:
        db.update_item(it["id"], status=meta["revise_status"], note="revision requested",
                       data={"revision": text, "revision_of": meta["payload"]})
        db.set_artifact_outcome(a["id"], "revised", {"instruction": text})
        db.add_signal("approval", {"outcome": "revised", "kind": meta["kind"], "payload": meta["payload"],
                                   "instruction": text}, it["id"], a["id"])
        _confirm(root, site.t("👌 redoing it", "👌 lo rehago"), it["id"])


def reissue_shadow_asks() -> int:
    """Asks sent in shadow mode say "no se ejecuta"; in act mode, send them again."""
    if CFG.get("mode") != "act":
        return 0
    n = 0
    for a in db.q("SELECT * FROM artifacts WHERE kind='approval' AND outcome IS NULL"):
        meta = json.loads(a["content"])
        if meta.get("act"):
            continue
        it = db.item(a["item_id"])
        if not it or it["status"] != "awaiting_approval":
            continue
        db.set_artifact_outcome(a["id"], "superseded", {"why": "reissued in act mode"})
        request(it["id"], meta["kind"], meta["payload"], meta["revise_status"])
        n += 1
    return n


EXPIRE_S = 2 * 86400


def _save_meta(art_id: int, meta: dict) -> None:
    db.x("UPDATE artifacts SET content=? WHERE id=?", json.dumps(meta, ensure_ascii=False), art_id)


def _expire(a, meta: dict, it: dict) -> None:
    """Two days without an answer never ends the work. The ask stays live, so the case stays on the board; the next digest re-asks it once
    (reask_expired). A re-ask that also goes unanswered just stays on the board until he acts or it goes moot."""
    if meta.get("expired_at"):
        return
    meta["expired_at"] = now()
    _save_meta(a["id"], meta)
    db.add_signal("approval_expired", {"kind": meta["kind"], "payload": meta["payload"], "reasked": bool(meta.get("reasked"))},
                  it["id"], a["id"])
    db.update_item(it["id"], note=site.t("no answer in 2 days: it stays on the board", "sin respuesta en 2 días: sigue en el tablero")
                   + ("" if meta.get("reasked") else site.t(", asked again in the next digest", ", se re-pregunta en el próximo digest")),
                   data={"ask_expired": True})


def reask_expired() -> int:
    """Called when a full digest is due (dm.keep_board_last): every expired, never re-asked ask is asked again,
    once, in its case thread. Zero tokens."""
    n = 0
    for a in db.q("SELECT * FROM artifacts WHERE kind='approval' AND outcome IS NULL ORDER BY id"):
        meta = json.loads(a["content"] or "{}")
        if not meta.get("expired_at") or meta.get("reasked") or meta.get("keep_status"):
            continue
        it = db.item(a["item_id"])
        if not it or it["status"] != "awaiting_approval":
            continue
        stage = meta.get("revise_status") if meta["kind"] == "slack_send" else None
        if (stage == "propose" or (stage == "reply_draft" and it["kind"] == "REPLY")) and not it["data"].get("expiry_redraft"):
            # a message drafted 2 days ago speaks to a conversation that has moved on: #686's 'dalee, hoy lo mando!' of
            # 10-09 would have been re-asked on 10-11 with its 'hoy' gone, and #664's squad proposal to migrate alerts
            # 'antes del 12/10' was re-asked on 10-13. It redrafts over today's thread instead (a reply also closes
            # when he answered himself); a redraft that expires again is re-asked as it is
            _retire_for_redraft(it, a, meta, site.t("no answer in 2 days: redrafting it over today's conversation",
                                                 "sin respuesta en 2 días: lo rehago sobre la conversación de hoy"), "expired")
            if stage == "reply_draft":
                data = {"last_ts": None, "drafted_at": now(), "redraft_at": now()}
            else:
                asked = datetime.fromtimestamp(meta.get("first_asked_at") or a["created_at"]).strftime("%a %d/%m")
                data = {"revision": f"the draft asked on {asked} went unanswered and today is "
                                    f"{datetime.now().strftime('%a %d/%m')}: rewrite it for today, with what happened "
                                    "since, and drop or restate any date or deadline it gave that has passed",
                        "revision_of": meta["payload"]}
            db.update_item(it["id"], status=stage, attempts=0, next_run_at=0, note=site.t("no answer in 2 days: redrafting it", "sin respuesta en 2 días: lo rehago"),
                           data={**data, "expiry_redraft": True})
            n += 1
            continue
        root = it["data"].get("report_ts")
        text = (site.t("🔁 _no answer for 2 days, asking you once more_\n", "🔁 _sin respuesta hace 2 días, te lo pregunto una vez más_\n")
                + ask_body(meta.get("ask_text"))) if meta.get("ask_text") else None
        try:
            if root and a["ref"] == root:  # the case root is the ask: edit it in place, never delete the case thread
                db.set_artifact_outcome(a["id"], "expired", {"why": "re-asked in the digest"})
                res = request(it["id"], meta["kind"], meta["payload"], meta.get("revise_status", "reply_draft"),
                              text=text, edit_ts=root, lint_note="")  # ask_text already carries its lint note
            else:
                supersede(a, meta, site.t("no answer in 2 days: asking you again", "sin respuesta en 2 días: te lo vuelvo a preguntar"), outcome="expired")
                res = request(it["id"], meta["kind"], meta["payload"], meta.get("revise_status", "reply_draft"),
                              text=text, thread_ts=root)
        except Exception as e:
            log("re-ask failed", item=it["id"], err=str(e)[:160])
            continue
        new = db.q1("SELECT content FROM artifacts WHERE id=?", res["artifact"])
        m2 = json.loads(new["content"] or "{}")
        m2.update(reasked=True, first_asked_at=meta.get("first_asked_at") or a["created_at"])
        _save_meta(res["artifact"], m2)
        db.add_signal("approval_reasked", {"kind": meta["kind"]}, it["id"], res["artifact"])
        log("approval re-asked after expiry", item=it["id"])
        n += 1
    return n


def supersede(a, meta: dict, why: str, outcome: str = "superseded") -> None:
    """The ask is moot: record it and say so on its own message, so a stale ask is never
    mistaken for a live one. outcome 'sent_by_him' / 'scheduled_by_him' when he
    did it himself: that is an approval, not a discarded draft."""
    db.set_artifact_outcome(a["id"], outcome, {"why": why})
    if a["ref"]:
        from . import dm
        try:  # the DM only holds live asks: a dead one goes, with its own confirmations
            if not dm.delete_ask(a["ref"], meta.get("thread_ts")):
                slack.edit_self(a["ref"], f"❎ _{why}_\n\n" + (meta.get("ask_text") or "").split("\n\n✅")[0], topic="note")
        except Exception as e:
            log("supersede cleanup failed", err=str(e)[:160])


def _retire_for_redraft(it: dict, a, meta: dict, why: str, outcome: str = "superseded") -> None:
    """An ask its case is about to redraft. When the ask is the case root, superseding would delete the case thread and
    the redraft would post the case anew: #746 on 10-13 became three case messages in 4.5 h, one per teammate reply in
    its thread, each a new notification with the Detalle gone. The root stays, says it is being redone (its ✅ line
    goes, so nothing answers the old draft) and the redraft edits it in place."""
    if a["ref"] != it["data"].get("report_ts"):
        return supersede(a, meta, why, outcome=outcome)
    db.set_artifact_outcome(a["id"], outcome, {"why": why})
    try:
        slack.edit_self(a["ref"], f"🔄 _{why}_\n\n" + (meta.get("ask_text") or "").split("\n\n✅")[0], topic="note")
    except Exception as e:
        log("redraft mark failed", item=it["id"], err=str(e)[:160])


BLOCK_DELIVERED = "PR exists: deliver with caveats"  # worker.deliver_blocked's note: the block goes on, as a caveat


def _block_cleared(a, meta: dict) -> bool:
    """An escalation ask raised while its case was blocked describes that block: once the case leaves it (retaken, the
    cause fixed), the ask asks for something that no longer applies, and a new block gets its own escalation. Early escalations of a case in progress are not tied to a block.
    A delivery with caveats leaves 'blocked' with its cause intact, so it clears nothing."""
    if (meta.get("payload") or {}).get("which") != "escalation":
        return False
    before = db.q1("SELECT dst FROM transitions WHERE item_id=? AND at<=? ORDER BY at DESC, rowid DESC LIMIT 1",
                   a["item_id"], a["created_at"])
    return bool(before and before["dst"] == "blocked" and db.q1(
        "SELECT 1 FROM transitions WHERE item_id=? AND at>? AND src='blocked' AND coalesce(note, '') != ?",
        a["item_id"], a["created_at"], BLOCK_DELIVERED))


def _thread_moved(it: dict) -> str | None:
    """Someone else answered in the case thread after our draft: the draft may be moot or wrong, so
    the case redrafts over the new conversation."""
    d = it["data"]
    if it["kind"] != "REPLY" or d.get("dm") or not (d.get("channel") and d.get("thread_ts")):
        return None
    since = float(d.get("drafted_at") or it["updated_at"])
    try:
        msgs = slack.replies(d["channel"], d["thread_ts"])
    except Exception:
        return None
    others = [m for m in msgs if float(m["ts"]) > since and m.get("user") not in (CFG["self_user_id"], None)
              and slack.is_human(m) and not slack.user_is_bot(m.get("user"))]
    if not others:
        return None
    if now() - float(others[-1]["ts"]) < 600:
        return None  # the conversation is still moving; one redraft when it settles, not one per message
    if now() - float(d.get("redraft_at") or 0) < 1800:
        return None  # at most one redraft per half hour per case
    who, at = slack.user_name(others[-1].get('user')), db.fmt_time(others[-1]['ts'])[-5:]
    return site.t(f"{who} answered in the thread at {at}; redrafting with that", f"{who} respondió en el hilo a las {at}; lo rehago con eso")


_LINK = re.compile(r"https?://[^\s<>|]+")


def _links(text: str) -> set[str]:
    return {u.rstrip("/.,)") for u in _LINK.findall(text or "")}


_FIND_AT: dict = {}
_CHECKED_AT: dict = {}
CHECK_MAX_S = 600


def _due(a) -> bool:
    """Is this live ask's Slack check due in this pass? A check is about four paced reads (his answer in the target
    thread, his own send anywhere, the thread moving, his reply in the DM) out of the 30 replies reads a minute every
    background job shares, so a pass grew with the backlog: 65 live asks, half of them days old, made the 45 s pass
    run past its 600 s budget six times on 10-08, and a teammate's answer reached the case 20-50 min late (#647 11:03
    seen 11:25, #320 11:41 seen 12:33). An ask is checked about as often as it is young: every pass in its first
    minutes, then age/24 apart, at most CHECK_MAX_S."""
    t = now()
    if t - _CHECKED_AT.get(a["id"], 0) < min(CHECK_MAX_S, (t - a["created_at"]) / 24):
        return False
    _CHECKED_AT[a["id"]] = t
    return True


def _resolved_by_joaquin(it: dict, meta: dict, since: float) -> str | None:
    """Did he already do what the ask proposes? Returns the reason, or None. Keyed on where the ask would post, not on
    the item kind."""
    if meta["kind"] != "slack_send":
        return None
    from . import replies, sentlearn
    pl = meta["payload"]
    if it["kind"] == "REPLY":
        # from the trigger, or from the ask if news moved the case's last_ts past it: his answer between the ask and a
        # teammate's later message is still his
        d = {**it["data"], "last_ts": f"{min(float(it['data'].get('last_ts') or it['data'].get('ts') or since), since):.6f}"}
    else:  # did he post in the ask's target thread (or, top-level, a message with the same links) since the ask?
        d = {"channel": pl["channel"], "thread_ts": pl.get("thread_ts"), "ts": f"{since:.6f}"}
    said = replies.joaquin_said_after(d)
    if it["kind"] != "REPLY" and not pl.get("thread_ts"):
        # a busy channel: only his message carrying one of the ask's links (the PR, the source thread) is this message
        links = _links(pl.get("text"))
        said = [m for m in said if links & _links(m.get("text"))]
    match, where = None, d["channel"]
    if not said:
        # he copies the draft, edits it and sends it himself, often somewhere else than where the ask pointed (inside
        # the source thread instead of a new post): his own message anywhere the draft could have gone, by similarity
        #. Up to three Slack reads, so
        # at most every 3 minutes per ask (poll runs every 45 s over every live ask; history is rate-tier 3)
        key = (it["id"], pl.get("channel"), pl.get("thread_ts"), float(since))
        if now() - _FIND_AT.get(key, 0) < 180:
            return None
        _FIND_AT[key] = now()
        match = sentlearn.find_sent(pl, since, it["data"] if it["kind"] != "NUDGE" else None)
        if not match:
            return None
        said, where = match["msgs"], match["channel"]
    mine = [m for m in said if m.get("client_msg_id")] or said
    sent = "\n".join(m.get("text", "") for m in mine)
    sentlearn.record_pair(it["id"], pl.get("text") or "", sent, where, sent_ts=mine[0]["ts"],
                          match=match or sentlearn.similarity(pl.get("text") or "", sent), via="resolved",
                          moved_to_thread=not pl.get("thread_ts") and bool(match and match.get("thread_ts")))
    at = db.fmt_time(mine[0]['ts'])[-5:]
    return site.t(f"you already sent it yourself at {at}", f"ya lo mandaste vos a las {at}")


def _still_open(a) -> bool:
    return db.q1("SELECT outcome FROM artifacts WHERE id=?", a["id"])["outcome"] is None


def _gone(e: Exception) -> bool:
    return "message_not_found" in str(e) or "thread_not_found" in str(e)


def _reask_lost(a, meta: dict, it: dict) -> None:
    """The ask's message no longer exists (deleted with a tidy or a closed thread): ask again on a fresh message instead
    of failing every poll. Side asks too: they failed every poll for days, never expired,
    and never reached him."""
    db.set_artifact_outcome(a["id"], "lost", {"why": "slack message gone"})
    root = it["data"].get("report_ts")
    if root and not slack.fetch_message(CFG["self_dm_channel"], root):
        root = None
        db.update_item(it["id"], data={"report_ts": None})
    request(it["id"], meta["kind"], meta["payload"], meta.get("revise_status", "reply_draft"),
            text=ask_body(meta.get("ask_text", "")) or None, thread_ts=root,
            keep_status=bool(meta.get("keep_status")))
    log("approval re-asked", item=it["id"], side=bool(meta.get("keep_status")))


def _review_by_policy(a, meta: dict, it: dict) -> None:
    """A squad review drafted before the comment-only rule still waited on his ✅ like before: 20
    reviews with findings, the oldest from 10-02, never reached GitHub, and each open case kept ingest from reviewing the
    PR's new commits (the user: "we haven't been doing any reviews... find out the root cause"). Under the rule it posts
    now, as comments, at the head it read; a PR that moved since gets a fresh review of what it is today instead of
    days-old comments; a merged or closed one is left to stale.sweep. A guard refusal is rewritten, never asked."""
    from . import reviews
    meta["policy_tried"] = now()
    _save_meta(a["id"], meta)
    p = meta["payload"]
    try:
        pr = gh.pr_view(p["repo"], p["pr"])
    except Exception as e:
        log("review policy: pr view failed", item=it["id"], err=str(e)[:160])
        meta.pop("policy_tried")
        return _save_meta(a["id"], meta)
    if pr["state"] != "OPEN":
        return
    if pr["headRefOid"] != p["head_sha"]:
        supersede(a, meta, site.t("the PR changed since this review: redoing it over what it is today, and it posts on its own",
                                  "el PR cambió desde esta review: la rehago sobre lo que es hoy y se publica sola"))
        db.update_item(it["id"], status="review_prepare", attempts=0, next_run_at=0,
                       note=site.t("old unposted review and the PR changed: new review (rule: it posts without your ok)",
                                   "review vieja sin publicar y el PR cambió: review nueva (regla: se publica sin tu ok)"),
                       data={"needs_you": False})
        return
    from .guardcall import GuardDenied
    try:
        res = reviews._send_review(p)
    except GuardDenied as e:
        supersede(a, meta, site.t("the guard would not let it post: rewriting it", "el guard no la dejó publicar: la reescribo"))
        return reviews.refused(it, p, str(e))
    except Exception as e:  # GitHub or the network: the next pass tries again, three times, then a fresh review
        meta["policy_errors"] = meta.get("policy_errors", 0) + 1
        log("review policy: post failed", item=it["id"], err=str(e)[:200], attempt=meta["policy_errors"])
        if meta["policy_errors"] < 3:
            meta.pop("policy_tried", None)
            return _save_meta(a["id"], meta)
        supersede(a, meta, site.t(f"GitHub refused it three times ({str(e)[:120]}): redoing it", f"GitHub no la aceptó tres veces ({str(e)[:120]}): la rehago"))
        return db.update_item(it["id"], status="review_prepare", attempts=0, next_run_at=0,
                              note=site.t("the old review could not be posted: new review", "la review vieja no se pudo publicar: review nueva"), data={"needs_you": False})
    supersede(a, meta, site.t("posted as comments (rule: team reviews go out without your ok)",
                              "publicada como comentarios (regla: las reviews del squad salen sin tu ok)"), outcome="approved")
    db.add_artifact(it["id"], "posted_review", f"{p['repo']}#{p['pr']}",
                    json.dumps({**p, "result": res}, ensure_ascii=False, default=str))
    db.update_item(it["id"], status="done", note=f"review posted as comments ({len(p.get('comments') or [])}) by policy",
                   data={"needs_you": False})


def _reviews_by_policy() -> None:
    """First thing in every pass: the rest is minutes of paced Slack reads, and these need none."""
    if not CFG.get("reviews", {}).get("post_comment_only"):
        return
    for a in db.q("SELECT a.* FROM artifacts a JOIN items i ON i.id=a.item_id WHERE a.kind='approval' AND a.outcome IS NULL "
                  "AND i.kind='REVIEW' AND json_extract(a.content,'$.kind')='github_review' "
                  "AND json_extract(a.content,'$.policy_tried') IS NULL ORDER BY a.id"):
        meta = json.loads(a["content"])
        if (meta.get("payload") or {}).get("event", "COMMENT") != "COMMENT":
            continue
        try:
            _review_by_policy(a, meta, db.item(a["item_id"]))
        except Exception as e:
            log("review policy failed", item=a["item_id"], err=f"{type(e).__name__}: {e}"[:200])


def poll() -> int:
    n, scheduled = 0, None
    _reviews_by_policy()
    for a in db.q("SELECT * FROM artifacts WHERE kind='approval' AND outcome IS NULL"):
        # a pass takes minutes of paced Slack reads while other jobs retire asks: only one still open now is polled
        # (2026-10-19: the digest re-asked 15 review asks mid-pass, deleting the old messages, and this pass, holding
        # the old rows, found each message gone and asked it a second time)
        if not _still_open(a):
            continue
        meta = json.loads(a["content"])
        it = db.item(a["item_id"])
        if meta.get("keep_status"):
            if not it or it["status"] in ("dropped", "failed"):
                supersede(a, meta, site.t("the case closed", "el caso se cerró"))
                continue
            if _block_cleared(a, meta):
                supersede(a, meta, site.t("the case got past that block and moved on; if it gets stuck again, it is escalated again",
                                          "el caso salió de ese bloqueo y siguió; si se traba de nuevo, se escala de nuevo"))
                continue
            if not _due(a):
                continue
            try:  # he sent his own version (team message, PR-channel post, closing notice): as if approved
                why = _resolved_by_joaquin(it, meta, a["created_at"])
            except Exception as e:
                why = None
                log("side ask resolved-check failed", item=it["id"], err=str(e)[:160])
            if why:
                supersede(a, meta, why, outcome="sent_by_him")
                db.add_signal("approval", {"outcome": "sent_by_him", "kind": meta["kind"], "payload": meta["payload"]}, it["id"], a["id"])
                _side_sent(meta, it)
                n += 1
                continue
            try:
                decision, text, newest = _decision(a["ref"], _cursor(a, meta), meta.get("thread_ts"), meta.get("stale_reactions") or ())
            except Exception as e:
                if not _gone(e):
                    log("side ask poll failed", item=it["id"], err=str(e)[:160])
                elif _still_open(a):  # never answerable now: it ends like an unanswered one, or is asked again
                    if it["status"] == "done" or now() - a["created_at"] > 5 * 86400:
                        db.set_artifact_outcome(a["id"], "lost", {"why": "slack message gone"})
                    else:
                        _reask_lost(a, meta, it)
                continue
            if decision:
                n += 1
                try:
                    _apply_side(a, meta, it, decision, text)
                except Exception as e:
                    log("side ask apply failed", item=it["id"], err=f"{type(e).__name__}: {e}"[:300])
            elif now() - a["created_at"] > 5 * 86400:
                db.set_artifact_outcome(a["id"], "expired")
            continue
        if meta.get("kind") == "github_replies" and it:
            # the user: replies to reviewers on his PRs go out without his ok. An ask that predates the rule
            # is executed by policy, so no thread waits on him.
            try:
                res = execute("github_replies", meta["payload"])
                db.set_artifact_outcome(a["id"], "approved", {"by": "policy: reviewer replies need no ok", "executed": str(res)[:200]})
                db.update_item(it["id"], status=it["data"].get("after_approval", "done"), note="replies posted (policy)", data={"needs_you": False})
                _confirm(meta.get("thread_ts") or a["ref"], site.t("✅ answered on GitHub (rule: no ok needed)", "✅ respondido en GitHub (regla: sin tu ok)"), it["id"])
            except Exception as e:
                log("policy reply failed", item=it["id"], err=str(e)[:200])
            continue
        if not it or it["status"] != "awaiting_approval":
            # the case moved on without this ask, but he may have answered it himself first: that is his send, not a
            # discarded draft
            try:
                why = _resolved_by_joaquin(it, meta, a["created_at"]) if it and a["ref"] else None
            except Exception as e:
                why = None
                log("resolved-check failed", item=it["id"], err=str(e)[:160])
            if why:
                supersede(a, meta, why, outcome="sent_by_him")
                db.add_signal("approval", {"outcome": "sent_by_him", "kind": meta["kind"], "payload": meta["payload"]}, it["id"], a["id"])
                continue
            supersede(a, meta, {"reply_draft": site.t("new messages came in, redrafting it over the whole conversation",
                                                      "hubo mensajes nuevos, lo rehago sobre toda la conversación"),
                                "vet": site.t("looking at it again with the whole thread and its attachments",
                                              "lo re-evalúo leyendo el hilo completo y los adjuntos"),
                                "comments_reply": site.t("it was a bot's thread on your PR: I answer and resolve it myself, and you get a "
                                                         "report instead of a review request",
                                                         "era un hilo de un bot en tu PR: lo respondo y resuelvo solo, y te llega un reporte en vez de un pedido de review")
                                }.get(it["status"] if it else "", site.t("no longer applies", "ya no aplica")))
            continue
        if not a["ref"] or not _due(a):  # no ref: shadow without self-DM, nothing to poll, show on board only
            continue
        outcome = "sent_by_him"
        try:
            why = _resolved_by_joaquin(it, meta, a["created_at"])
            if not why and meta.get("kind") == "slack_send":
                if scheduled is None:  # one read per pass, not one per ask
                    from .worker import _his_scheduled
                    scheduled = _his_scheduled()
                pl = meta.get("payload") or {}
                for s in scheduled:
                    if s.get("channel_id") == pl.get("channel") and (not pl.get("thread_ts") or s.get("thread_ts") == pl.get("thread_ts")):
                        at = db.fmt_time(s.get('post_at', 0))[-5:]
                        why = site.t(f"you already scheduled a message there for {at}", f"ya programaste un mensaje ahí para las {at}")
                        outcome = "scheduled_by_him"
                        break
        except Exception as e:
            why = None
            log("resolved-check failed", item=it["id"], err=str(e)[:160])
        if why:  # he did it himself: the case moves on exactly as if the ask had been approved
            supersede(a, meta, why, outcome=outcome)
            db.add_signal("approval", {"outcome": outcome, "kind": meta["kind"], "payload": meta["payload"]}, it["id"], a["id"])
            sent_pair = db.q1("SELECT data FROM signals WHERE kind='style_pair' AND item_id=? ORDER BY id DESC", it["id"])
            _approved_effects(it, meta, json.loads(sent_pair["data"]).get("sent", "") if sent_pair else meta["payload"].get("text", ""))
            db.update_item(it["id"], status=it["data"].get("after_approval", "done"), note=why, data={"needs_you": False})
            from . import dm
            dm.close_case_thread(db.item(it["id"]), why)
            continue
        moved = _thread_moved(it)
        if moved:
            _retire_for_redraft(it, a, meta, moved)
            db.update_item(it["id"], status="reply_draft", attempts=0, next_run_at=0, note=moved,
                           data={"last_ts": None, "drafted_at": now(), "redraft_at": now()})
            continue
        try:
            decision, text, newest = _decision(a["ref"], _cursor(a, meta), meta.get("thread_ts"), meta.get("stale_reactions") or ())
        except Exception as e:
            if not _still_open(a):  # retired while this ask was being read: its message went with it
                continue
            if _gone(e):
                _reask_lost(a, meta, it)
            else:
                log("approval poll failed", item=it["id"], err=str(e)[:200])
            continue
        if not _still_open(a):  # retired meanwhile: whatever retired it owns what comes next
            continue
        if not decision:
            if now() - a["created_at"] > EXPIRE_S:
                _expire(a, meta, it)
            continue
        n += 1
        try:
            _apply(a, meta, it, decision, text)
        except Exception as e:  # one bad ask must never stop the others
            log("approval apply failed", item=it["id"], err=f"{type(e).__name__}: {e}"[:300])
    return n
