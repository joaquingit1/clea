"""The self-DM digest: short, grouped by what the user cares about, plain Spanish.

the user: "the messages are unclear, cluttered and messy. Why not do a digest
that's easier for the eyes?" So: no internal stage names, no truncated titles, no English
model text, no ids of internals. Sections, each only when non-empty:
  Necesito tu ok   numbered, one line each (the details live in the ask message)
  Tus PRs          grouped by workstream (PR pairs linked by merge order)
  Encontrado       problems the harness found on its own and what it did
  Pendiente tuyo   his own promises / asks directed at him
  footer           one line: what moved today + spend
Every human-facing line is a short Spanish label written by Muse from structured facts
(free), cached per state; if a label fails validation, a plain code-built line is used.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from . import db, merge_order, muse, site
from .core import CFG, log

EN_WORDS = re.compile(r"\b(the|and|of|with|for|failed|pending|waiting|blocked|shadow|stage|item|gate|deploy(ed)?)\b", re.I)
DAYS = site.t("Monday Tuesday Wednesday Thursday Friday Saturday Sunday", "lunes martes miércoles jueves viernes sábado domingo").split()
# the line every green draft PR's group opens with: the board's own text, so it reads its lines back by this
DRAFTS_LEAD = site.t("• green drafts, tell me and I will mark them ready", "• borradores verdes, decime y los paso a ready")
_DAYS_AGO = re.compile(r"hace (\d+) días|(\d+) days ago")


_MISSES: list[tuple[str, str, int]] = []


def _label(key: str, facts: str, max_words: int, fallback: str) -> str:
    """Cached Spanish label, or the plain fallback NOW. Never calls a model inline: rendering runs
    in the scheduler loop and must be instant.
    Misses are queued for fill_labels(), which runs as a background job."""
    h = hashlib.sha1(facts.encode()).hexdigest()[:12]
    hit = db.kv_get(f"label:{key}:{h}")
    if hit:
        return hit
    _MISSES.append((f"label:{key}:{h}", facts, max_words))
    return fallback


def fill_labels(budget_s: int = 240) -> int:
    """Background: write up to a few missing labels per run, time-boxed."""
    import time as _t
    t0, n = _t.time(), 0
    pending = list(dict.fromkeys(_MISSES))
    _MISSES.clear()
    for ck, facts, max_words in pending:
        if _t.time() - t0 > budget_s:
            break
        try:
            out = muse.call("ap-draft", site.own(
                f"Escribí UNA línea corta (máximo {max_words} palabras) en español rioplatense, "
                f"clara y concreta, para un resumen de Slack de un ingeniero. Sin jerga interna, sin inglés, "
                f"sin guiones largos, sin comillas, sin emojis. Solo la línea.\n\nHechos:\n{facts}",
                f"Write ONE short line (at most {max_words} words) in {site.language_name()}, clear and concrete, for an "
                f"engineer's Slack summary. No internal jargon, no em dashes, no quotes, no emojis. Only the line.\n\n"
                f"Facts:\n{facts}"),
                            cwd=site.primary_path(), lane="digest", timeout=120, retries=0)
            line = out.strip().splitlines()[0].strip(" -•*\"'") if isinstance(out, str) and out.strip() else ""
        except Exception:
            continue
        # English words in a Spanish label mean the model drifted; an English install wants them
        if line and len(line.split()) <= max_words + 3 and "\u2014" not in line and not (site.lang() == "es" and EN_WORDS.search(line)):
            db.kv_set(ck, line)
            n += 1
    return n


def _pr_link(repo: str, n: int) -> str:
    return f"<{site.pr_url(repo, n)}|{repo}#{n}>"


def _own_prs() -> dict[tuple[str, int], dict]:
    out = {}
    for r in db.q("SELECT repo, number, data FROM prs WHERE own=1"):
        st = db.kv_get(f"pr:{r['repo']}#{r['number']}", {}) or {}
        out[(r["repo"], r["number"])] = {"pr": json.loads(r["data"]), "st": st}
    return out


def _pr_subject(pr: dict) -> str:
    from . import comms
    return _short(comms.plain(re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", pr.get("title") or "")), 60)


def _age(pr: dict) -> str:
    c = pr.get("createdAt") or ""
    try:
        days = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(c.replace("Z", "+00:00"))).days
    except Exception:
        return ""
    return site.t(f" · {days} days ago", f" · hace {days} días") if days >= 2 else ""


def _stacked_on_mine(prs: dict) -> list[str]:
    """His open PRs that someone else's open PR is built on. One PR list
    per repo, cached 30 min."""
    from . import comms, gh
    from .core import now
    cache = db.kv_get("stacked_cache") or {}
    if now() - cache.get("at", 0) > 1800:
        rows = {}
        for repo in sorted({r for r, _ in prs}):
            try:
                rows[repo] = [{"n": x["number"], "base": (x.get("base") or {}).get("ref"), "who": (x.get("user") or {}).get("login"),
                               "body": (x.get("body") or "")[:3000]}
                              for x in gh.api(f"repos/{site.slug(repo)}/pulls?state=open&per_page=100") or []]
            except Exception as e:
                log("digest: stacked lookup failed", repo=repo, err=str(e)[:120])
        cache = {"at": now(), "rows": rows}
        db.kv_set("stacked_cache", cache)
    nick = CFG.get("nicknames", {})
    out = []
    for (repo, n), info in sorted(prs.items()):
        head = info["pr"].get("headRefName")
        on = [x for x in cache.get("rows", {}).get(repo, []) if x["n"] != n and x["who"] != CFG.get("self_github")
              and (x["base"] == head or re.search(rf"(apilad[oa] sobre|stacked on|depends on|sobre)\s+#{n}\b", x["body"], re.I))]
        if on:
            who = ", ".join(dict.fromkeys(nick.get((x["who"] or "").lower(), x["who"]) for x in on))
            refs = ", ".join(f"#{x['n']}" for x in on)
            title = comms.plain(re.sub(r"^\w+(\([^)]*\))?:\s*", "", info["pr"].get("title", "")))
            prs_ = f"{len(on)} PR{'s' if len(on) != 1 else ''}"
            out.append(f"• <{site.pr_url(repo, n)}|{repo}#{n}> · {_short(title, 60)} · {who} "
                       + site.t(f"has {prs_} on top of it ({refs}): needs review/merge", f"tiene {prs_} encima ({refs}): falta review/merge"))
    return out


def _pr_state_facts(repo: str, n: int, info: dict) -> str:
    st, s = info["st"], info["st"].get("summary") or {}
    facts = [f"PR {repo}#{n}: {info['pr'].get('title')}",
             "CI: " + (site.own("verde", "green") if s.get('green') else site.own("rojo", "red") if s.get('failed') else site.own("corriendo", "running")),
             site.own("aprobado: ", "approved: ") + (site.own("sí", "yes") if s.get('approved') else "no")]
    if st.get("merge_block"):
        facts.append(site.own(f"no se mergea: {st['merge_block'][:300]}", f"not merged: {st['merge_block'][:300]}"))
    if st.get("blocked"):
        facts.append(st["blocked"])
    for r in db.q("SELECT kind, status, data FROM items WHERE repo=? AND pr=? AND status NOT IN "
                  "('done','dropped','expired','failed')", repo, n):
        d = json.loads(r["data"])
        what = {"CI": site.own("arreglando el CI", "fixing CI"),
                "COMMENTS": site.own("atendiendo comentarios de review", "handling review comments")}.get(r["kind"], r["kind"])
        facts.append(site.own(f"en curso: {what} (etapa {r['status']})", f"in progress: {what} (stage {r['status']})"))
        if d.get("blocked_reason"):
            facts.append(site.own("motivo: ", "reason: ") + d['blocked_reason'][:200])
        if d.get("ci_diag"):
            facts.append(site.own("diagnóstico CI: ", "CI diagnosis: ") + f"{d['ci_diag'].get('category')}: {d['ci_diag'].get('summary', '')[:200]}")
    for d in db.q("SELECT dep_repo, dep_number, need FROM merge_deps WHERE repo=? AND number=?", repo, n):
        dep = f"{d['dep_repo']}#{d['dep_number']}"
        facts.append(site.own(f"se mergea después de {dep} (tiene que estar en {d['need']})", f"merges after {dep} (it has to be in {d['need']})"))
    return "\n".join(facts)


def _workstreams(prs: dict) -> list[list[tuple[str, int]]]:
    """Group own PRs that are linked by merge order (backend + its frontend = one line)."""
    parent = {k: k for k in prs}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k
    for d in db.q("SELECT repo, number, dep_repo, dep_number FROM merge_deps"):
        a, b = (d["repo"], d["number"]), (d["dep_repo"], d["dep_number"])
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    groups: dict = {}
    for k in prs:
        groups.setdefault(find(k), []).append(k)
    return sorted(groups.values(), key=lambda g: min(n for _, n in g))


def _ask_link(item_id: int) -> str:
    """Permalink to the ask message, built locally: the board renders every minute and one
    API call per ask made slack.sh time out."""
    a = db.q1("SELECT ref FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL ORDER BY id DESC", item_id)
    if a and a["ref"]:
        return f" · <{site.slack_link(CFG['self_dm_channel'], a['ref'])}|{site.t('open', 'ver')}>"
    return ""


def _pr_line(repo: str, n: int, info: dict) -> str:
    """One deterministic line per PR: CI, approval, threads, what is in flight. No model."""
    st, s, pr = info["st"], info["st"].get("summary") or {}, info["pr"]
    bits = [site.t("CI green", "CI verde") if s.get("green") else site.t("CI red", "CI rojo") if s.get("failed")
            else site.t("CI running", "CI corriendo")]
    bits.append(site.t("approved", "aprobado") if s.get("approved") else site.t("draft", "borrador") if pr.get("isDraft")
                else site.t("not approved", "sin approve"))
    if st.get("unresolved_threads"):
        nt = st['unresolved_threads']
        bits.append(site.t(f"{nt} open review thread{'s' if nt != 1 else ''}", f"{nt} hilo{'s' if nt != 1 else ''} de review abiertos"))
    for r in db.q("SELECT kind, status FROM items WHERE repo=? AND pr=? AND status NOT IN "
                  "('done','dropped','expired','failed') AND kind IN ('CI','COMMENTS')", repo, n):
        bits.append({"CI": site.t("fixing CI", "arreglando el CI"), "COMMENTS": site.t("answering comments", "respondiendo comentarios")}[r["kind"]]
                    + (site.t(" (waiting for your ok)", " (esperando tu ok)") if r["status"] == "awaiting_approval" else ""))
    if st.get("merge_block"):
        bits.append(site.t(f"not merging it: {st['merge_block'][:90]}", f"no lo mergeo: {st['merge_block'][:90]}"))
    # babysit writes this reason in the install's language; either opening means red CI
    if st.get("blocked") and not st["blocked"].startswith(("CI rojo", "CI red")):
        bits.append(st["blocked"][:90])
    elif st.get("blocked"):
        bits[0] = site.t("CI red, not touching it: ", "CI rojo, no lo toco: ") + st["blocked"].split(": ", 1)[-1][:80]
    deps = [f"{d['dep_repo']}#{d['dep_number']}" for d in db.q(
        "SELECT dep_repo, dep_number FROM merge_deps WHERE repo=? AND number=?", repo, n)]
    if deps:
        bits.append(site.t("after ", "después de ") + ", ".join(deps))
    return f"• {_pr_link(repo, n)} {pr.get('title', '')[:45]}: " + ", ".join(bits)


def _render_sections() -> str:
    """v1's board: the one-glance
    view of what waits on HIM, each line linking to the case message that carries the context and
    the copy-ready text. The board itself carries no drafts. Sections with zero items are omitted."""
    t = dt.datetime.now()
    asks = db.q("SELECT i.id, i.kind, i.title, i.repo, i.pr, a.content FROM items i JOIN artifacts a ON a.item_id=i.id "
                "AND a.kind='approval' AND a.outcome IS NULL WHERE i.status='awaiting_approval' ORDER BY i.urgency DESC, i.id")
    approve, send = [], []
    for a in asks:
        meta = json.loads(a["content"])
        gist = (db.item(a["id"])["data"].get("gist") or "").strip()
        ref = f" {_pr_link(a['repo'], a['pr'])}" if a["pr"] else ""
        link = _ask_link(a["id"])
        if meta["kind"] == "github_review":
            approve.append(site.t(f"• Review of{ref}", f"• Review de{ref}") + (f": {gist}" if gist else "") + link)
        elif meta["kind"] == "github_replies":
            approve.append(site.t(f"• Replies to reviewers on your PR{ref}", f"• Respuestas a reviewers en tu PR{ref}") + (f": {gist}" if gist else "") + link)
        else:
            where = {"REPLY": site.t("Slack reply", "Respuesta en Slack"), "NUDGE": site.t("Nudge in the PR channel", "Nudge en el canal de PRs"),
                     "INCIDENT": site.t("Message to the team", "Mensaje al squad"), "IDEA": site.t("Proposal to the team", "Propuesta al squad"),
                     "TASK": site.t("PR announcement", "Aviso del PR")}.get(a["kind"], site.t("Message", "Mensaje"))
            title = gist or a["title"].replace("(slack) ", "")[:70]
            send.append(f"• {title if gist else where + ': ' + title}{ref}{link}")
    decide = []
    for r in db.q("SELECT id, kind, title, repo, pr, data FROM items WHERE status='awaiting_human' ORDER BY urgency DESC, id"):
        d = json.loads(r["data"])
        what = (site.t("proposal", "propuesta") if r["kind"] == "IDEA"
                else site.t("PR ready, needs your review/announcement", "PR listo, falta tu review/anuncio") if d.get("pr_links")
                else site.t("draft waiting", "borrador pendiente") if d.get("loop_draft") else site.t("waiting on you", "te espera"))
        decide.append(f"• {r['title'].replace('(slack) ', '').replace('(ctx) ', '')[:70]}: {what}" + (f" {_pr_link(r['repo'], r['pr'])}" if r["pr"] else ""))
    for r in db.q("SELECT k, v FROM kv WHERE k LIKE 'pr:%'"):
        st = json.loads(r["v"])
        if st.get("merge_block") and (st.get("summary") or {}).get("approved"):
            decide.append(site.t(f"• {r['k'][3:]} approved, but I am not merging it: {st['merge_block'][:120]}",
                                 f"• {r['k'][3:]} aprobado pero no lo mergeo: {st['merge_block'][:120]}"))
    n = len(approve) + len(send) + len(decide)
    lines = [site.t(f":pushpin: *Waiting on you ({n})* · updated {t:%H:%M}", f":pushpin: *Pendientes tuyos ({n})* · actualizado {t:%H:%M}")
             + (site.t(" · test mode, nothing gets published", " · modo prueba, no publica nada") if CFG["mode"] != "act" else "")]
    wd = db.kv_get("watchdog") or {}
    for text in (wd.get("pauses") or {}).values():
        lines.append(f"⏸ {text}")
    for text in (wd.get("problems") or [])[:3]:
        lines.append(f"⚠️ {text}")
    if approve:
        lines += ["", site.t("*To approve* (GitHub, one click):", "*Para aprobar* (GitHub, un click):")] + approve[:8]
    if send:
        lines += ["", site.t("*To send* (text ready, waiting for your ok):", "*Para mandar* (texto listo, esperando tu ok):")] + send[:8]
    if decide:
        lines += ["", site.t("*To decide:*", "*Para decidir:*")] + decide[:6]
    bugs = db.q("SELECT id, json_extract(data,'$.blocked_stage') st, last_error err FROM items WHERE status='blocked' "
                "AND json_extract(data,'$.harness_bug_rev') IS NOT NULL LIMIT 3")
    if bugs:
        lines += ["", site.t("*🛠 A harness bug is holding up work* (it retries on its own once the code is fixed)",
                             "*🛠 Bug del harness frenando trabajo* (se reintenta solo al corregir el código)")]
        lines += [site.t(f"• #{b['id']} at {b['st']}: {str(b['err'])[:90]}", f"• #{b['id']} en {b['st']}: {str(b['err'])[:90]}") for b in bugs]
    prs = _own_prs()
    if prs:
        lines += ["", site.t("*Your PRs*", "*Tus PRs*")] + [_pr_line(r, n_, prs[(r, n_)]) for r, n_ in sorted(prs)][:8]
    active = db.q("SELECT kind, title, status FROM items WHERE status NOT IN ('done','dropped','expired','failed',"
                  "'awaiting_human','awaiting_approval','blocked','loop_track','waiting') AND kind IN "
                  "('TASK','INCIDENT','IDEA','REPLY') ORDER BY urgency DESC, updated_at DESC LIMIT 4")
    if active:
        stage = {"telemetry": site.t("checking telemetry", "mirando Datadog"), "investigate": site.t("investigating", "investigando"),
                 "plan": site.t("planning", "planificando"), "critique": site.t("reviewing the plan", "revisando el plan"),
                 "build": site.t("building the fix", "construyendo el fix"), "gate": site.t("running checks", "corriendo checks"),
                 "self_find": site.t("reviewing the diff", "revisando el diff"), "self_review": site.t("reviewing the diff", "revisando el diff"),
                 "fix": site.t("fixing", "corrigiendo"), "publish": site.t("publishing", "publicando"), "ci_wait": site.t("waiting for CI", "esperando CI"),
                 "ci_fix": site.t("fixing CI", "arreglando CI"), "reply_draft": site.t("drafting", "redactando"),
                 "propose": site.t("putting a proposal together", "armando propuesta"), "announce": site.t("drafting the announcement", "redactando aviso")}
        lines += ["", site.t("*In progress*", "*En curso*")] + [f"• {r['title'].replace('(slack) ', '').replace('(ctx) ', '')[:60]}: {stage.get(r['status'], r['status'])}" for r in active]
    loops = db.q("SELECT title, data FROM items WHERE kind='OPEN_LOOP' AND status NOT IN ('done','dropped','expired','failed') "
                 "ORDER BY urgency DESC LIMIT 5")
    if loops:
        lines += ["", site.t("*Yours to do*", "*Pendiente tuyo*")]
        for m in loops:
            d = json.loads(m["data"])
            lines.append(f"• {(d.get('loop') or {}).get('action') or m['title'].replace('(slack) ', '').replace('(ctx) ', '')[:80]}")
            if d.get("loop_prs"):  # only merging these is left: the draft written before they existed is stale
                lines.append(site.t("   _waiting for:_ ", "   _espera:_ ") + ", ".join(_pr_link(rp, n) for rp, n in d["loop_prs"]))
            elif d.get("loop_draft"):
                lines.append(site.t("   _draft:_ ", "   _borrador:_ ") + d['loop_draft']['text'][:220])
    start = int(dt.datetime.combine(dt.date.today(), dt.time()).timestamp())
    moved = db.q1("SELECT count(DISTINCT item_id) c FROM transitions WHERE at>=?", start)["c"]
    spent, cap = f"{db.spent_today('claude'):.0f}", CFG['claude']['daily_usd_cap']
    lines.append(site.t(f"\n_today: {moved} things moved · claude ${spent} of ${cap}_", f"\n_hoy: {moved} cosas avanzaron · claude ${spent} de ${cap}_"))
    text = "\n".join(lines)
    return text if len(text) <= 3400 else text[:3380] + "\n…"


def pr_digest(force: bool = False) -> str | None:
    """the user's open PRs by readiness.
    Deterministic from babysit state; posted when the ready set changes or once each morning."""
    import hashlib
    prs = _own_prs()
    if not prs:
        return None
    ready_merge, ready_review, drafts_green, working, stuck = [], [], [], [], []
    for (repo, n), info in sorted(prs.items()):
        st, s, pr = info["st"], info["st"].get("summary") or {}, info["pr"]
        link = f"{_pr_link(repo, n)} {pr.get('title', '')[:50]}"
        threads = st.get("unresolved_threads") or 0
        rows = db.q("SELECT kind, status, data FROM items WHERE repo=? AND pr=? AND status NOT IN "
                    "('done','dropped','expired','failed') AND kind IN ('CI','COMMENTS')", repo, n)
        inflight = [r["kind"] for r in rows]
        waiting_for = next((json.loads(r["data"]).get("waiting_for") for r in rows if r["status"] == "waiting"), None)
        deps = [f"{d['dep_repo']}#{d['dep_number']}" for d in db.q(
            "SELECT dep_repo, dep_number FROM merge_deps WHERE repo=? AND number=?", repo, n)]
        blocked = [r["id"] for r in db.q("SELECT id FROM items WHERE repo=? AND pr=? AND status='blocked'", repo, n)]
        if blocked or st.get("blocked"):
            stuck.append(f"• {link}: {st.get('blocked') or site.t('I could not unblock it on my own, see the case thread', 'no pude destrabarlo solo, mirá el hilo del caso')}")
        elif s.get("green") and threads == 0 and not pr.get("isDraft") and s.get("approved"):
            ready_merge.append(f"• {link}" + (site.t(f" (after {', '.join(deps)})", f" (después de {', '.join(deps)})") if deps else "")
                               + (f": {st['merge_block'][:80]}" if st.get("merge_block") else ""))
        elif s.get("green") and threads == 0 and not pr.get("isDraft"):
            ready_review.append(f"• {link}" + (site.t(f" (after {', '.join(deps)})", f" (después de {', '.join(deps)})") if deps else ""))
        elif s.get("green") and threads == 0 and pr.get("isDraft"):
            drafts_green.append(f"• {link}")
        else:
            what = []
            if waiting_for:
                what.append(site.t(f"CI red by design, {waiting_for[:90]}; I check it again every 30 min",
                                   f"CI rojo por diseño, {waiting_for[:90]}; lo re-chequeo cada 30 min"))
            elif s.get("failed") or "CI" in inflight:
                what.append(site.t("fixing CI", "arreglando el CI") if "CI" in inflight or "COMMENTS" in inflight else site.t("CI red", "CI rojo"))
            if threads:
                what.append(site.t(f"{threads} review thread{'s' if threads != 1 else ''}, answering", f"{threads} hilo{'s' if threads != 1 else ''} de review, respondiendo")
                            if "COMMENTS" in inflight else site.t(f"{threads} open review threads", f"{threads} hilos de review abiertos"))
            if s.get("pending") and not s.get("failed"):
                what.append(site.t("CI running", "CI corriendo"))
            working.append(f"• {link}: " + (", ".join(what) or site.t("in progress", "en curso")))
    lines = [site.t(f":rocket: *Your PRs* · {dt.datetime.now():%H:%M}", f":rocket: *Tus PRs* · {dt.datetime.now():%H:%M}")]
    if ready_merge:
        lines += ["", site.t("*Ready to merge* (green, approved, no threads; I merge them in deploy order):",
                             "*Listos para mergear* (verdes, aprobados, sin hilos; los mergeo en orden de deploy):")] + ready_merge
    if ready_review:
        lines += ["", site.t("*Ready for review* (green, no threads, need an approval):",
                             "*Listos para review* (verdes, sin hilos, falta approve):")] + ready_review
    if drafts_green:
        lines += ["", site.t("*Green drafts* (tell me if I should mark them ready):", "*Borradores verdes* (decime si los paso a ready):")] + drafts_green
    if working:
        lines += ["", site.t("*Working on them:*", "*Los estoy trabajando:*")] + working
    if stuck:
        lines += ["", site.t("*Stuck, I need you:*", "*Trabados, te necesito:*")] + stuck
    text = "\n".join(lines)
    ready_sig = hashlib.sha1("\n".join(ready_merge + ready_review + drafts_green + stuck).encode()).hexdigest()
    today = dt.date.today().isoformat()
    last = db.kv_get("pr_digest", {})
    morning = dt.datetime.now().hour >= 9 and last.get("day") != today
    if ready_sig == last.get("sig") and not morning and not force:
        return None
    db.kv_set("pr_digest", {"sig": ready_sig, "day": today, "at": db.now()})
    return text


def facts() -> str:
    """The deterministic board (sections + links) used as the facts for the narrative."""
    parts = [_render_sections()]
    pr = pr_digest_text()
    if pr:
        parts.append(pr)
    return "\n\n".join(p for p in parts if p)


def pr_digest_text() -> str | None:
    """PR readiness as facts (no posting side effects)."""
    st = db.kv_get("pr_digest", {})
    text = pr_digest(force=True)
    db.kv_set("pr_digest", st)  # the board never consumes the change signature
    return text


SPLIT = "<<<board-detail>>>"


def _origin(title: str, d: dict) -> str:
    base = _origin_one(title, d)
    extra = []
    for a in d.get("also_sources", []):
        one = _origin_one(a.get("title", ""), {k: a.get(k) for k in ("channel", "ts", "author", "from", "pattern") if a.get(k)}
                          | ({"text": " ".join(a.get("tickets", []))} if a.get("tickets") else {}))
        if a.get("tickets") and not any(t in one for t in a["tickets"]):
            one = (one + " " if one else "") + "(" + ", ".join(a["tickets"]) + ")"
        if one and one not in extra and one != base:
            extra.append(one)
    return base + (site.t(" · also: ", " · también: ") + "; ".join(extra) if extra else "")


def _origin_one(title: str, d: dict) -> str:
    """Where a case came from, for the board."""
    from . import slack, cases
    if d.get("pattern"):
        st = d.get("pattern_stats") or {}
        ch, count = slack.channel_name(CFG.get('alerts_channel') or ''), st.get('count', '?')
        since = db.fmt_time(st['first_seen'])[:5] if st.get('first_seen') else '?'
        return site.t(f"alert in {ch} ({count} events since {since})", f"alerta de Datadog en {ch} ({count} eventos desde {since})")
    if d.get("from") == "patrol" or title.startswith("patrol:"):
        return site.t("found by my own code patrol (nobody reported it)", "patrulla propia del código (nadie lo reportó)")
    someone = site.t("someone", "alguien")
    if d.get("jira"):  # a ticket first: a case that came from a ticket is that ticket, wherever it was raised
        who_, ch = slack.user_name(d['author']) if d.get('author') else someone, slack.channel_name(d['channel']) if d.get("channel") else ""
        where = site.t(f" (raised by {who_} in {ch})", f" (lo levantó {who_} en {ch})") if d.get("channel") else ""
        return f"ticket {d['jira']}{where}"
    m = cases.TICKET.search(f"{title} {d.get('text', '')}")
    if m and not d.get("channel"):
        return f"ticket {m.group(1)}"
    if d.get("channel"):
        when = db.fmt_time(d["ts"])[:5] if d.get("ts") else ""
        ch = slack.channel_name(d["channel"])
        day = dt.datetime.fromtimestamp(float(d["ts"])).strftime("%b %-d") if d.get("ts") else ""  # English writes the month
        where = site.t(f"{'a DM' if ch == 'DM' else ch} on {day}", f"{ch} el {when}")
        if d.get("author") and slack.user_is_bot(d["author"]):
            return f"ticket {m.group(1)} ({where})" if m else site.t(f"automatic notice in {where}", f"aviso automático en {where}")
        who = slack.user_name(d.get("author")) if d.get("author") else someone
        return site.t(f"raised by {who} in {where}", f"lo levantó {who} en {where}") + (f" ({m.group(1)})" if m else "")
    if d.get("from") == "granola":
        return site.t("next step from a daily meeting (Granola)", "próximo paso de una daily (Granola)")
    if d.get("from") in ("backfill", "ctx"):
        return site.t("came up in the review of the last few days of Slack", "surgió del repaso de Slack de los últimos días")
    return ""


def _channel(key: str) -> str:
    """A configured channel by its name, as he reads it."""
    from . import slack
    return slack.channel_name(CFG.get(key) or "")


def _ask_url(item_id: int) -> str | None:
    a = db.q1("SELECT ref FROM artifacts WHERE item_id=? AND kind='approval' AND outcome IS NULL ORDER BY id DESC", item_id)
    return site.slack_link(CFG["self_dm_channel"], a["ref"]) if a and a["ref"] else None


def _short(s: str, n: int = 78) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:(") + "…"


def _line(label: str, url: str | None) -> str:
    return f"• {_short(label, 92)}" + (f"  <{url}|→>" if url else "")


def _section(title: str, items: list[str], cap: int = 5) -> list[str]:
    if not items:
        return []
    out = [f"*{title}* · {len(items)}"] + items[:cap]
    if len(items) > cap:
        out.append(site.t(f"  _and {len(items) - cap} more_", f"  _y {len(items) - cap} más_"))
    return out + [""]


def comms_name(item_id: int) -> str:
    from . import comms
    it = db.item(item_id)
    return comms.name(it)[:80] if it else ""


def render() -> str:
    """The board the user reads: short headers with counts, one item
    per line, at most one link per line at the end, no prose. Deterministic, zero tokens."""
    t = dt.datetime.now()
    reviews, texts, decide = [], [], []
    for a in db.q("SELECT i.id, i.kind, i.title, i.repo, i.pr, a.content FROM items i JOIN artifacts a ON a.item_id=i.id "
                  "AND a.kind='approval' AND a.outcome IS NULL WHERE i.status='awaiting_approval' ORDER BY i.urgency DESC, i.id"):
        meta = json.loads(a["content"])
        gist = (db.item(a["id"])["data"].get("gist") or "").strip()
        ref = f"{a['repo']}#{a['pr']}" if a["pr"] else ""
        url = _ask_url(a["id"])
        if meta["kind"] == "github_review":
            cs = (meta.get("payload") or {}).get("comments") or []
            n = len(cs)
            from . import comms
            what = comms.plain(re.sub(r"^review \S+:\s*", "", a["title"]))  # the PR's subject, not a code fragment
            reviews.append(_line(f"{ref} · {_short(what, 60)} · " + site.t(f"{n} comment{'s' if n != 1 else ''}", f"{n} comentario{'s' if n != 1 else ''}") if n else
                                 f"{ref} · {_short(what, 60)} · " + site.t("no findings, approve", "sin hallazgos, aprobar"), url))
        elif meta["kind"] == "github_replies":
            reviews.append(_line(f"{ref}: " + site.t("replies to reviewers", "respuestas a reviewers"), url))
        else:
            what = gist or comms_name(a["id"])
            texts.append(_line((site.t("unanswered · ", "sin respuesta · ") if meta.get("expired_at") else "") + what, url))
    from . import stale
    _cache: dict = {}
    for r in db.q("SELECT id, kind, title, repo, pr, data FROM items WHERE status='awaiting_human' ORDER BY urgency DESC, id"):
        d = json.loads(r["data"])
        if any(p.get("pr_url") for p in d.get("parts", [])):
            continue  # it has a PR: it lives under 'Tus PRs', the decision was made
        if stale.what_it_needs(db.item(r["id"]), _cache)[0] != "you":
            continue  # only what really needs him is 'para decidir'
        ref = f"{r['repo']}#{r['pr']}" if r["pr"] else ""
        what = d.get("gist") or comms_name(r["id"])
        decide.append(_line(f"{what}" + (f" ({ref})" if ref else ""),
                            site.slack_link(CFG["self_dm_channel"], d["report_ts"]) if d.get("report_ts") else None))
    prs = _own_prs()
    purpose, origin = {}, {}
    for r in db.q("SELECT id, title, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA')"):
        d = json.loads(r["data"])
        for p in d.get("parts", []):
            if p.get("pr_url"):
                k = p["pr_url"].rstrip("/").split("/pull/")[-1] + "@" + p.get("repo", "")
                purpose[k] = r["title"].replace("patrol: ", "")
                origin[k] = (_origin(r["title"], d), d.get("report_ts"))
    ready_review, ready_merge, drafts, needs_you, working = [], [], [], [], []
    for (repo, n), info in sorted(prs.items()):
        st, s, pr = info["st"], info["st"].get("summary") or {}, info["pr"]
        ref = f"<{site.pr_url(repo, n)}|{repo}#{n}>"
        threads = st.get("unresolved_threads") or 0
        rows = db.q("SELECT kind, status, data FROM items WHERE repo=? AND pr=? AND status NOT IN ('done','dropped','expired','failed') "
                    "AND kind IN ('CI','COMMENTS')", repo, n)
        waiting_for = next((json.loads(x["data"]).get("waiting_for") for x in rows if x["status"] == "waiting"), None)
        blocked = db.q1("SELECT 1 FROM items WHERE repo=? AND pr=? AND status='blocked'", repo, n)
        deps = [f"{d['dep_repo']}#{d['dep_number']}" for d in db.q("SELECT dep_repo, dep_number FROM merge_deps WHERE repo=? AND number=?", repo, n)]
        after = site.t(f" (after {', '.join(deps)})", f" (después de {', '.join(deps)})") if deps else ""
        if st.get("blocked") or blocked:
            why = st.get("blocked") or site.t("I could not unblock it, see its case", "no lo pude destrabar, mirá su caso")
            why = re.sub(r"/Users/\S+", site.t("that branch", "esa branch"), why)
            needs_you.append(f"• {ref}: {_short(why, 80)}")
        elif s.get("green") and threads == 0 and not pr.get("isDraft") and s.get("approved"):
            ready_merge.append(f"• {ref} · {_pr_subject(pr)}{after}{_age(pr)}")
        elif s.get("green") and threads == 0 and not pr.get("isDraft"):
            ready_review.append(f"• {ref} · {_pr_subject(pr)}{after}{_age(pr)}")
        elif s.get("green") and threads == 0 and pr.get("isDraft"):
            what = purpose.get(f"{n}@{repo}") or pr.get("title", "")
            org, rts = origin.get(f"{n}@{repo}", ("", None))
            case = f"  <{site.slack_link(CFG['self_dm_channel'], rts)}|→>" if rts else ""
            drafts.append(f"• {ref} · {_short(what, 64)}" + (site.t("\n     source: ", "\n     origen: ") + _short(org, 90) if org else "") + case)
        else:
            why = (site.t("waiting for the deploy: ", "espera el deploy: ") + waiting_for.split(':', 1)[-1].strip()[:60] if waiting_for else
                   site.t("fixing CI", "arreglando el CI") if rows and s.get("failed")
                   else site.t(f"{threads} review thread{'s' if threads != 1 else ''}", f"{threads} hilo{'s' if threads != 1 else ''} de review") if threads else
                   site.t("CI running", "CI corriendo") if s.get("pending") else site.t("in progress", "en curso"))
            working.append(f"• {ref}: {why}")
    from . import comms
    building = [f"• {comms.name(db.item(r['id']))[:80]}" for r in db.q(
        "SELECT id FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status IN ('plan','critique','build','gate','self_find',"
        "'self_review','fix','publish','ci_wait','ci_fix','verify_done','pr_body_refresh') ORDER BY urgency DESC LIMIT 6")]
    when = f"{t:%a %d, %H:%M}"
    if site.lang() == "es":  # strftime names the day in English
        when = (when.replace("Mon", "lun").replace("Tue", "mar").replace("Wed", "mié").replace("Thu", "jue").replace("Fri", "vie")
                .replace("Sat", "sáb").replace("Sun", "dom"))
    lines = [site.t(f":pushpin: *Pending* · {when}", f":pushpin: *Pendientes* · {when}"), ""]
    reviews = list(dict.fromkeys(reviews))  # one line per ask (cometa#2664 showed twice)
    # his PRs that only need him or a reviewer: the board's main section, not the reply (2026-10-20: cometa#2668, green,
    # approved by CodeRabbit, two of andre's PRs on top, was only in the per-topic reply and he nearly missed it)
    try:
        stacked = _stacked_on_mine(_own_prs())
    except Exception as e:
        stacked = []
        log("digest: stacked section failed", err=str(e)[:120])
    # what matters first: a PR someone builds on, then the one waiting longest
    blocking = {re.search(r"\|([\w-]+#\d+)>", s).group(1) for s in stacked if re.search(r"\|([\w-]+#\d+)>", s)}
    def _rank(line: str) -> tuple:
        ref = (re.search(r"\|([\w-]+#\d+)>", line) or [None, ""])[1]
        m = _DAYS_AGO.search(line)  # _age's text, in either language
        days = int((m.group(1) or m.group(2)) if m else 0)
        return (ref not in blocking, -days)
    lines += _section(site.t("Ready to merge", "Listos para mergear"), sorted(ready_merge, key=_rank), 8)
    lines += _section(site.t("Ready, need a human approval (ask for a review)", "Listos, falta un approve humano (pedí review)"),
                      sorted(ready_review, key=_rank), 8)
    try:
        lines += _section(site.t("Holding others up", "Frenan a otros"), stacked, 6)
    except Exception as e:
        log("digest: stacked section failed", err=str(e)[:120])
    reviews = list(dict.fromkeys(reviews))
    lines += _section(site.t("Team reviews to approve", "Reviews del squad para aprobar"), reviews)
    lines += _section(site.t("Texts ready, waiting for your ok", "Textos listos, esperando tu ok"), texts)
    lines += _section(site.t("To decide", "Para decidir"), decide, 4)
    pr_lines = []
    if ready_merge:
        pr_lines.append(site.t("• ready to merge: ", "• listos para mergear: ") + ", ".join(x[2:] for x in ready_merge))
    if ready_review:
        pr_lines.append(site.t("• ready for review: ", "• listos para review: ") + ", ".join(x[2:] for x in ready_review))
    if drafts:
        pr_lines.append(f"{DRAFTS_LEAD} ({len(drafts)}):")
        pr_lines += ["   " + x[2:].replace("\n     ", "\n       ") for x in drafts[:6]]
    pr_lines += needs_you + working
    epic_lines = []
    if prs:
        from . import epics
        drafts_only = [l for l in pr_lines if l.startswith(DRAFTS_LEAD) or l.startswith("   ")]
        if drafts_only:
            lines += [site.t("*Your PRs*", "*Tus PRs*")] + drafts_only + [""]
        if needs_you:
            lines += [site.t("*Need you*", "*Te necesitan*")] + needs_you + [""]
        try:
            own = {k: v["pr"] for k, v in prs.items() if not v["pr"].get("isDraft")}
            states = {k: v["st"] for k, v in prs.items()}
            epic_lines = [site.t(f"*Your PRs by topic* · {len(own)} open", f"*Tus PRs por tema* · {len(own)} abiertos")] + epics.render(own, states)
        except Exception as e:
            log("epics render failed", err=str(e)[:120])
            epic_lines = [site.t(f"*Your PRs* · {len(prs)}", f"*Tus PRs* · {len(prs)}")] + [l for l in pr_lines if l not in drafts_only]
    lines += _section(site.t("Building now", "Estoy construyendo"), building, 6)
    wd = db.kv_get("watchdog") or {}
    tail = [text for text in (wd.get("pauses") or {}).values()]
    if tail:
        lines.append("_" + " · ".join(x[:120] for x in tail) + "_")
    # the epics block is the long part: it travels as ONE reply under the board (Slack cuts ~4000 chars;
    # 2026-09-28: the board got truncated and the drafts vanished), marked so board_post can split
    text = "\n".join(lines).rstrip()
    msgs = []
    for r in db.q("SELECT id, title, data FROM items WHERE kind IN ('TASK','INCIDENT','IDEA') AND status NOT IN ('dropped','failed') "
                  "AND json_extract(data,'$.team_message.text') IS NOT NULL ORDER BY updated_at DESC"):
        d = json.loads(r["data"]); tm = d["team_message"]
        sent, rej = tm.get("sent") or {}, tm.get("rejected") if isinstance(tm.get("rejected"), dict) else {}
        links = ", ".join(f"<{p['pr_url']}|{p['repo']}#{p['pr']}>" for p in d.get("parts", []) if p.get("pr_url"))
        block = []
        for which, label in (("team", _channel("team_channel")), ("pr", _channel("pr_channel"))):
            body = tm.get(f"{which}_text") or (tm.get("text") if which == "team" else "")
            if not body or sent.get(which) or rej.get(which):
                continue
            ts_ = (tm.get("asks") or {}).get(which) or (tm.get("ask_ts") if which == "team" else None)
            ask = site.slack_link(CFG["self_dm_channel"], ts_) if ts_ else None
            block.append(f"_{label}_" + (f"  <{ask}|{site.t('✅ here', '✅ acá')}>" if ask else "") + "\n> " + body.replace("\n", "\n> "))
        if block:
            rts = d.get("report_ts")
            card = site.slack_link(CFG["self_dm_channel"], rts) if rts else None
            msgs.append(f"<{card}|{links.split('|')[-1].rstrip('>') if links else site.t('case', 'caso')}>" if card else links)
    if msgs:  # the drafts live in each case's card (✅ there); the board only says where
        text += site.t(f"\n\n*PR messages ready to send* · {len(msgs)}\n", f"\n\n*Mensajes de PR listos para mandar* · {len(msgs)}\n") + " · ".join(msgs[:6]) + \
            (site.t(f" _and {len(msgs) - 6} more, in their cases_", f" _y {len(msgs) - 6} más, en sus casos_") if len(msgs) > 6 else "")
    if epic_lines:
        text += "\n" + SPLIT + "\n" + "\n".join(epic_lines)
    return text
