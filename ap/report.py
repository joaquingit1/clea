"""Case reports (v1's feed contract, which is what the user actually used): ONE self-DM thread
per case. The root message says what surfaced, what is and is not verified and what went out
to be checked; verdicts, PRs and new drafts land as replies in that thread. A report that
carries a text to send IS the approval ask (✅ sends it), so a case never becomes a report
plus a separate card. Written by Opus from the gathered evidence only."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import approvals, bridge, claude, db, site, slack
from .core import item_dir, log, CFG

SCHEMA = {"type": "object", "required": ["headline", "bullets", "need", "analysis", "text", "board_line", "detail"], "properties": {
    "headline": {"type": "string", "description": site.own(
        "the case in his words, max 14 words: who is affected and what happens to them (e.g. 'a customer: al cambiar un alumno de grupo sus notas no se ven en la boleta'). No file, class, function or table names, no English.",
        f"the case in plain words, in {site.language_name()}, max 14 words: who is affected and what happens to them. No file, class, function or table names.")},
    "bullets": {"type": "array", "maxItems": 3, "items": {"type": "string"}, "description": "at most 3 plain bullets, max 25 words each: the verdict (is it a bug, whose side), what was done or proposed, what is still unverified. Zero code identifiers, paths or ids; those go in detail."},
    "need": {"type": "string", "description": site.own(
        f"what {site.first_name()} has to do, one short sentence (e.g. 'tu ✅ para mandar el borrador al equipo', 'decidir si lo tomamos'). Empty when he has nothing to do.",
        f"what {site.first_name()} has to do, one short sentence in {site.language_name()}. Empty when there is nothing for them to do.")},
    "text": {"type": "string", "description": "the full report (the shapes below), max 200 words; it goes in the case thread under the card, so it may name files and ids"},
    "detail": {"type": "string", "description": "the evidence: file:line, trace ids, counts, contrast cases, quotes. Posted as one reply under the case; empty when there is nothing beyond the message"},
    "analysis": {"type": "string", "description": site.own(
        "for the case page in his app, 2 to 4 plain Spanish sentences (voseo, no English, no code identifiers, no em dashes): what happens and to whom, the cause and how sure it is (probado / probable / sin confirmar), and what was done or is recommended. Empty when the case has no analysis (a pure message draft).",
        f"for the case page in their app, 2 to 4 plain sentences in {site.language_name()} (no code identifiers, no em dashes): what happens and to whom, the cause and how sure it is (proven, probable or unconfirmed), and what was done or is recommended. Empty when the case has no analysis (a pure message draft).")},
    "board_line": {"type": "string", "description": site.own(
        "one line for his pending-items board, max 15 words: what and for whom, e.g. 'Respuesta a a teammate sobre Graven/horarios, con los datos reales de Metabase'",
        f"one line for their pending-items board, in {site.language_name()}, max 15 words: what and for whom")}}}
DETAIL_HEAD = site.t("*Details*\n", "*Detalle*\n")  # the header of the one evidence reply under a case
FILES = ("dossier.json", "gate.json", "telemetry.json", "plan.json", "self-verify.json", "comments-verify.json")


def _evidence(it: dict) -> str:
    from .tasks import _context
    d, out = it["data"], [_context(it)]
    folder = item_dir(it["id"])
    for f in FILES:
        if (folder / f).exists():
            out.append(f"<{f}>\n{(folder / f).read_text()[:12000]}\n</{f}>")
    for k in ("reply_muse", "checks", "critique", "done_summary", "verify_done", "executed", "blocked_reason", "pr_body_overflow"):
        if d.get(k):
            out.append(f"<{k}>\n{json.dumps(d[k], ensure_ascii=False)[:4000]}\n</{k}>")
    prs = [p.get("pr_url") for p in d.get("parts", []) if p.get("pr_url")]
    if prs:
        out.append("PRs: " + ", ".join(prs))
    parent = db.item(d["parent"]) if d.get("parent") else None
    if parent:
        out.append(f"<case_so_far>\n{parent['data'].get('report_text', '')[:6000]}\n</case_so_far>")
    if d.get("report_text"):
        out.append(f"<already_reported_in_this_thread>\n{d['report_text'][:6000]}\n</already_reported_in_this_thread>")
    return "\n\n".join(out)


def _root_item(it: dict) -> dict:
    return db.item(it["data"]["parent"]) if it["data"].get("parent") else it


CITE = re.compile(r"(?<![\w/])((?:[\w.-]+/)+[\w.-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|md|sql)):(\d+)")


_INDEX: dict[str, tuple[float, list[str]]] = {}


def _file_index(roots: list[str]) -> list[str]:
    """Tracked files of each root (git ls-files), cached 10 min: citations are matched by path suffix
    because models cite paths relative to a package, not to the repo root."""
    import time as _t
    from .core import run
    out: list[str] = []
    for root in roots:
        hit = _INDEX.get(root)
        if not hit or _t.time() - hit[0] > 600:
            r = run(["git", "-C", root, "ls-files"], timeout=60)
            files = r.stdout.splitlines() if r.returncode == 0 else []
            _INDEX[root] = (_t.time(), files)
            hit = _INDEX[root]
        out += hit[1]
    return out


def _check_citations(text: str, extra_roots: list[str]) -> str:
    """Every file:line the report cites must exist in the mirrors (or the PR worktrees); a citation
    that resolves nowhere is marked, never presented as fact."""
    from . import worktree
    index = _file_index([str(worktree.mirror_path(r)) for r in CFG["repos"]] + extra_roots)

    def ok(path: str) -> bool:
        p = path.lstrip("./")
        return any(f == p or f.endswith("/" + p) for f in index)

    def fix(m):
        return m.group(0) if ok(m.group(1)) else site.t(f"{m.group(0)} (file not found on main, unverified citation)",
                                                                 f"{m.group(0)} (archivo no encontrado en main, cita sin verificar)")
    return CITE.sub(fix, text)


def _quote(text: str) -> str:
    return "\n".join("> " + l if l.strip() else ">" for l in text.strip().splitlines())


def telemetry_line(d: dict) -> str:
    """Why the case was investigated without Datadog/Sentry, always visible in its message, never model-dependent.
    tasks.telemetry stored the reason and nothing read it: the wrong-account notice goes out once per login switch
    (18:34 on 10-01), so 21 h later #421, a 5xx spike on /api/v1/academic, reached him as a code-only guess that
    'the traces have to be opened', with nothing saying the CLI was still on his personal account."""
    err = d.get("telemetry_error")
    if d.get("telemetry") and d.get("telemetry_blind"):
        return blind_es(d["telemetry_blind"])
    if not err or d.get("telemetry"):
        return ""
    m = re.search(r"logged into (\S+), which has no work connectors", err)
    why = (site.t(f"Claude Code is logged in as {m[1]}, which has no Datadog or Sentry: after `claude /login` with the work "
                  f"account, the next cases will have it",
                  f"Claude Code está logueado como {m[1]}, que no tiene Datadog ni Sentry: con `claude /login` en la cuenta "
                  f"del trabajo los próximos casos la tienen") if m else err[:160])
    return site.t(f"⚠️ No telemetry: this did not look at real logs or errors. {why}.",
                  f"⚠️ Sin telemetría, esto no miró logs ni errores reales. {why}.")


_SOURCES = [(r"sentry", "Sentry"), (r"prometheus|metric", site.t("the metrics", "las métricas")), (r"tempo|trace", site.t("the traces", "las trazas")),
            (r"loki|\blogs?\b", site.t("the logs", "los logs")), (r"cloudwatch", "CloudWatch"), (r"redshift", "Redshift"), (r"metabase", "Metabase"),
            ("|".join(filter(None, [site.db_source_re(), "postgres"])), site.t("the database", "la base")), (r"jira|atlassian", "Jira"), (r"github", "GitHub"),
            (r"vercel", "Vercel"), (r"grafana", "Grafana")]
_WHY = [(site.t("no access", "sin permiso"), r"oauth|authori[sz]|permission|forbidden|insufficient scope|requires? authentication|not authorized|403"),
        (site.t("did not get to it", "no llegué"), r"budget|not attempted|not queried|not re-queried|ran out"),
        (site.t("no tool to query it", "sin herramienta para consultarlo"), r"no \w*\s*(query|sql)[\w-]* tool|no .*tool .*(exposed|available|loaded|exists)|not reachable"),
        (site.t("the query failed", "la consulta falló"), r"error|too many connections|exceeded|does not exist|returned no data|failed")]


def blind_es(blind: list[str]) -> str:
    """What production evidence the case could not see, in his words. Sources the case did not need ('not used: Redshift answered') are not gaps."""
    groups: dict[str, list[str]] = {}
    for b in blind:
        if re.search(r"not used|answered (every|the)|inapplicable|not a tool failure", b, re.I):
            continue
        src = b.split(":", 1)[0]
        name = next((n for pat, n in _SOURCES if re.search(pat, src, re.I)), re.sub(r"\s*\(.*$", "", src).strip()[:30])
        why = next((w for w, pat in _WHY if re.search(pat, b, re.I)), site.t("could not read it", "no se pudo leer"))
        if name not in sum(groups.values(), []):
            groups.setdefault(why, []).append(name)
    if not groups:
        return ""
    def _and(xs):
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + site.t(" and ", " y ") + xs[-1]
    parts = [f"{_and(names)} ({why})" for why, names in groups.items()]
    return site.t(f"⚠️ I could not see everything in production: {'; '.join(parts)}. No errors there does not rule the problem out.",
                  f"⚠️ No vi todo en producción: {'; '.join(parts)}. Que ahí no aparezcan errores no descarta el problema.")


EXPLAIN = {"type": "object", "required": ["analysis", "cause"], "properties": {
    "analysis": SCHEMA["properties"]["analysis"],
    "cause": {"type": "string", "enum": ["probado", "probable", "sin confirmar", "no aplica"]}}}


def explain(it: dict) -> str:
    """The Spanish analysis for a case reported before report.post wrote one. Once per case, Sonnet, no tools: it rewords what the case already knows."""
    d = it["data"]
    ds = d.get("dossier") or {}
    tel = (d.get("telemetry") or {}).get("summary") if isinstance(d.get("telemetry"), dict) else ""
    # a reply case has no dossier: what was read and checked for it is in the reply lane's own answer. Without it the
    # analysis said "I did not read the code" under bullets that quoted the code
    ro = d.get("reply_opus") if isinstance(d.get("reply_opus"), dict) else {}
    facts = "\n".join(x for x in [f"Problem: {ds.get('problem') or d.get('summary') or ''}",
                                   f"Root cause ({ds.get('root_cause_status') or '?'}): {ds.get('root_cause') or ''}",
                                   f"Recommendation: {ds.get('recommendation') or ''}", f"Production evidence: {tel or ''}",
                                   f"What was found: {ro.get('report') or d.get('report_text') or ''}",
                                   "Checks made: " + " | ".join(str(c) for c in ro.get("checks") or []),
                                   f"Reply drafted: {d.get('reply_text') or ''}",
                                   "Card bullets: " + " | ".join(d.get("card_bullets") or [])] if x.split(": ", 1)[1].strip())
    out = claude.lean("report", f"Case: {d.get('card_headline') or it['title']}\n\n{facts[:6000]}\n\nWrite the analysis for "
                      f"{site.first_name()}'s case page from these facts only.", model="sonnet", cwd=str(Path.home()), schema=EXPLAIN,
                      tools=(), item_id=it["id"])
    text = (out.get("analysis") or "").strip()
    db.update_item(it["id"], data={"card_analysis": text[:1200], "card_cause": out.get("cause")})
    return text


def caveats_line(d: dict, limit: int = 1500) -> str:
    """What a delivered PR still leaves open, always visible in its message, never model-dependent. #484's announce on
    10-05 told him 'built, verified' for a PR whose verification had blocked on its PR body's test counts. Bounded:
    it sits in the case root, which an edit cannot grow past slack.MAXLEN (#630 on 10-08: 12 caveats, 2900 chars,
    announce_check failed 3x)."""
    cav = [re.sub(r"\s+", " ", c).strip()[:240] for c in d.get("caveats") or [] if c and c.strip()]
    if not cav:
        return ""
    line = (site.t("⚠️ Still open: ", "⚠️ Queda abierto: ") + cav[0])[:limit]
    for i, c in enumerate(cav[1:], 1):
        more = site.t(f" · (+{len(cav) - i} more)", f" · (+{len(cav) - i} más)")
        if len(line) + 3 + len(c) + (len(more) if i < len(cav) - 1 else 0) > limit:
            return line + more
        line += " · " + c
    return line


PING_EVERY_S = 6 * 3600


def deliverable_sig(it: dict) -> str:
    """The state of what the case delivers: its PRs, whether they are merged and live (the closing sweep only starts
    then), whether its tickets are closed. A change here is news; anything else is progress."""
    root = _root_item(it)
    items = [root] + [db.item(r["id"]) for r in db.q("SELECT id FROM items WHERE json_extract(data,'$.parent')=?", root["id"])]
    prs = sorted({p["pr_url"] for x in items if x for p in x["data"].get("parts", []) if p.get("pr_url")})
    closing = (root["data"].get("closing") or {}).get("state") or ""
    closed = "closed" if prs and root["status"] == "done" else ""
    return f"prs={','.join(prs)};closing={closing};{closed}"


def _ping(it: dict, thread: str, text: str, need: str = "") -> bool:
    """An edit in place does not notify, but
    a ping per update was 29 top-level messages a day that he did not read (2 replies, 2 reactions in 5 days). A case
    update pings ONLY when something needs him (`need`: what the ask or question is) or its deliverable changed state
    (PR opened, merged and live, ticket closed); everything else is the silent edit. Never more than one ping per case
    per 6 hours, unless the last ping announced a different need: the cap stops repeats, not a new decision. The previous ping of the case
    is deleted, so there is never more than one per case. Returns True when it pinged."""
    from . import dm
    from .core import now
    root = _root_item(it)
    d = root["data"]
    sig = deliverable_sig(it)
    base = d.get("ping_sig")
    if base is None:  # a case from before this rule: its baseline is "nothing delivered yet"
        base = "prs=;closing=;"
    if not need and sig == base:
        log("report: silent update", item=it["id"])
        return False
    new_need = bool(need and d.get("ping_need") and need != d["ping_need"])
    if now() - float(d.get("ping_at") or 0) < PING_EVERY_S and not new_need:
        log("report: ping held (one per case per 6 h)", item=it["id"], need=need[:60])
        return False
    from . import comms
    url = site.slack_link(CFG["self_dm_channel"], thread)
    # a self-contained card, not "🔔 <first line> → caso"
    cur = db.item(it["id"])["data"]
    headline = cur.get("card_headline") or re.sub(r"[*_`>]", "", text.strip().split("\n", 1)[0])[:140]
    want = cur.get("card_need") or (need.split(":", 1)[0] if need and ":" in need and " " not in need.split(":", 1)[0] else need)
    msg = comms.card(it, headline, [], want or "", "need" if want else "fyi")
    see = site.t("open case", "ver caso")
    msg += f" · <{url}|{see}>" if comms.links_line(it) else f"\n<{url}|{see}>"
    old = d.get("ping_ts")
    if old:
        dm._delete(old)
    try:
        ts = slack.send_self(msg, topic=f"ping-{root['id']}", guard=False)["ts"]
        db.update_item(root["id"], data={"ping_ts": ts, "ping_at": now(), "ping_sig": sig, "ping_need": need})
        return True
    except Exception as e:
        log("report: ping failed", item=it["id"], err=str(e)[:100])
        return False


def post(it: dict, shape: str, note: str = "", sendable: dict | None = None,
         ask: tuple[str, dict, str] | None = None, body: str | None = None, question: str = "", hold: str = "") -> str | None:
    """shape: 'surfaced' (A), 'finding' (B), 'update' (C). sendable: {"text", "channel",
    "thread_ts"}; ask: (approval kind, payload, revise_status) when the text needs his OK; question: what only he can
    answer when there is nothing to approve (the case waits on his reply in its thread); hold: the draft checker's
    objection to sending it as is, shown under the draft, never in it."""
    from .tasks import READ_ROOT
    if body is None:
        prompt = (f"Report shape: {shape}. {note}\n\n"
                  + (f"The draft checker says it should not go out as is: {hold}\n\n" if hold else "") + _evidence(it)
                  + (f"\n\n<draft_to_send>\n{sendable['text']}\n</draft_to_send>" if sendable else ""))
        wts = [p["wt"] for p in it["data"].get("parts", []) if p.get("wt")]
        from . import worktree
        repos = [str(worktree.mirror_path(r)) for r in CFG["repos"]]
        out = claude.lean("report", prompt, cwd=READ_ROOT, schema=SCHEMA, tools=("Read", "Grep", "Glob"),
                          model="sonnet" if shape == "update" else "opus",  # short updates are mechanical; findings stay on Opus
                          add_dirs=tuple(dict.fromkeys([str(item_dir(it["id"])), *wts, *repos])), item_id=it["id"], urgent=True)
        body = out["text"]
        card_fields = {k: out.get(k) for k in ("headline", "bullets", "need", "analysis")}
        detail = (out.get("detail") or "").strip()
        if detail and not detail.startswith("fake"):
            db.update_item(it["id"], data={"detail_pending": detail[:3400]})
        if out.get("board_line") and not str(out["board_line"]).startswith("fake"):
            db.update_item(it["id"], data={"gist": out["board_line"][:120]})
    else:
        card_fields = {}
    body = _check_citations(body.strip(), [p["wt"] for p in it["data"].get("parts", []) if p.get("wt")])
    d = it["data"]
    # 2026-10-13: the DM became "a sea of unexplained links": the case message is now a card (ap/comms.py) and the
    # report body, the pre-build verdict, telemetry and caveats go in the thread's single "Detalle" reply
    from . import comms, pregate
    extra_lines = [x for x in (pregate.line(d.get("pregate") or {}), telemetry_line(d),
                               caveats_line(d) if any(p.get("pr_url") for p in d.get("parts", [])) else "") if x]
    thread_body = "\n\n".join([body] + extra_lines + ([detail] if card_fields and detail else []))
    if card_fields.get("headline"):
        kind = "need" if (ask or question or card_fields.get("need")) else ("pr" if any(p.get("pr_url") for p in d.get("parts", [])) else "fyi")
        text = comms.card(it, card_fields["headline"], card_fields.get("bullets") or [], card_fields.get("need") or question, kind)
        db.update_item(it["id"], data={"detail_pending": thread_body[:3400], "card_headline": card_fields["headline"][:200],
                                       "card_need": (card_fields.get("need") or question or "")[:300],
                                       "card_bullets": [str(b)[:300] for b in card_fields.get("bullets") or []][:3],
                                       **({"card_analysis": card_fields["analysis"][:1200]} if card_fields.get("analysis") else {})})
        body = text
        cav_line = ""
        links = []
    else:  # a body given by the caller (or no card fields): the old layout, links labeled
        text = body
        for x in extra_lines:
            if x not in text:
                text += "\n\n" + x
        cav_line = caveats_line(d) if any(p.get("pr_url") for p in d.get("parts", [])) else ""
        links = [comms.links_line(it)] if comms.links_line(it) else []
    lint_note = None
    if sendable:
        # the deterministic voice lint runs before anything shows the draft (composer draft, report, ask)
        fixed, _, lint_note = approvals.lint_payload(sendable)
        if fixed["text"] != sendable["text"] and it["kind"] == "REPLY" and d.get("reply_text") == sendable["text"]:
            db.update_item(it["id"], data={"reply_text": fixed["text"]})
        sendable = {**sendable, **fixed}
        if ask:
            ask = (ask[0], {**ask[1], "text": fixed["text"], "thread_ts": fixed.get("thread_ts")}, ask[2])
        left = bridge.leave_draft(sendable["channel"], sendable.get("thread_ts"), sendable["text"])
        where = site.t("in the thread", "en el hilo") if sendable.get("thread_ts") else site.t(f"to <#{sendable['channel']}>", f"en <#{sendable['channel']}>")
        lead = (site.t(f"I left you this draft {where}", f"Te dejé este draft {where}") if left
                else site.t(f"Text ready to send {where}", f"Texto listo para mandar {where}"))
        text += f"\n\n{lead}:\n\n" + _quote(sendable["text"])
        if hold:
            text += site.t(f"\n\n⚠️ before you send it: {hold[:600]}", f"\n\n⚠️ antes de mandarlo: {hold[:600]}")
        if lint_note and not ask:
            text += "\n\n" + lint_note
    if links:
        text += "\n\n" + "\n".join(links)
    comms.check(text, f"report-{it['id']}")
    root = _root_item(it)
    thread = root["data"].get("report_ts")
    if thread and (root["data"].get("report_deleted") or not slack.fetch_message(CFG["self_dm_channel"], thread)):
        thread = None  # the message is gone (case closed earlier): start a fresh one
    parent = db.item(it["data"]["parent"]) if it["data"].get("parent") else None
    if parent and not thread and parent["status"] in ("reply_draft", "awaiting_approval", "awaiting_human") \
            and not any(p.get("pr_url") for p in it["data"].get("parts", [])):
        # the parent's redraft carries this verdict; no second message (2026-09-27: 4 top-level "updates"
        # in 5 minutes for one thread). A child whose parent is gone posts as its own case below.
        log("case report skipped: the parent's redraft carries it", item=it["id"])
        return None
    reserve = len(approvals.ask_suffix(lint_note or "")) if ask else 0  # request() appends it to the edited root
    if len(text) + reserve > 3400:  # Slack edit has no auto-split: keep the draft and links, trim the body
        keep = text[len(body):]
        body = body[:max(200, 3380 - len(keep) - reserve)].rstrip() + " …"
        text = body + keep
    if cav_line and cav_line in text and len(text) + reserve > 3400:  # then the caveats, the part that grows with rounds
        text = text.replace(cav_line, caveats_line(d, limit=max(200, len(cav_line) - (len(text) + reserve - 3400))), 1)
    # the user: an outdated report is corrected by editing it, never by stacking correction
    # replies. The case is ONE message that always shows the current state; its thread only carries
    # his own replies and the send confirmations.
    if thread:
        if ask:
            ts = approvals.request(it["id"], ask[0], ask[1], ask[2], text=text, edit_ts=thread, lint_note=lint_note)["ts"]
        else:
            slack.edit_self(thread, text, topic=f"report-{it['id']}")
            ts = thread
        _ping(it, thread, text, need=f"{ask[0]}:{ask[2]}" if ask else question)
    else:
        if ask:
            ts = approvals.request(it["id"], ask[0], ask[1], ask[2], text=text, lint_note=lint_note)["ts"]
        else:
            ts = slack.send_self(text, topic=f"report-{it['id']}")["ts"]
        # a new case message notifies by itself; its later updates ping only on news (see _ping)
        db.update_item(root["id"], data={"ping_sig": deliverable_sig(it)})
    so_far = body[-8000:]
    detail = db.item(it["id"])["data"].get("detail_pending")
    if detail:  # ONE detail reply per case, edited in place on updates
        dts = root["data"].get("detail_ts")
        try:
            if dts and slack.fetch_message(CFG["self_dm_channel"], dts):
                slack.edit_self(dts, DETAIL_HEAD + detail, topic=f"detail-{it['id']}")
            else:
                dts = slack.send_self(DETAIL_HEAD + detail, thread_ts=ts, topic=f"detail-{it['id']}")["ts"]
            db.update_item(root["id"], data={"detail_ts": dts})
        except Exception as e:
            log("detail reply failed", item=it["id"], err=str(e)[:120])
        db.update_item(it["id"], data={"detail_pending": None})
    db.update_item(root["id"], data={"report_ts": thread or ts, "report_text": so_far,
                                     **({} if thread else {"report_deleted": None, "report_closed": None})})
    if root["id"] != it["id"]:
        db.update_item(it["id"], data={"report_ts": thread or ts})
    from . import cases
    cases.add(cases.key_for(it), report_ts=ts, source="report")
    log("case report", item=it["id"], shape=shape, thread=thread or ts, ask=bool(ask))
    return ts
