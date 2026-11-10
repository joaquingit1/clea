"""Pre-build gate: the three questions the user asks his team before anyone builds, answered with evidence before any build spend.

Why: in one week 44 TASK and 32 INCIDENT cases ended dropped and ~$330 of $863 went to items that ended dropped,
expired or waiting; the harness missed a teammate's open PR (schools#931) and built a duplicate a teammate then
did better (cometa#2387 beat our #2380).

  (a) real?      bug/need vs expected behavior, config, data or user error, with a code citation
  (b) precedent? our precedent store, recent merged PRs touching the same files (mirror git log + read-only
                 `gh pr list --state merged`), Slack search
  (c) on it?     open PRs by humans touching the same files or naming the same ticket in the last days
                 (tasks.related_prs, extended with those candidates), Jira assignee when a ticket key exists

Evidence is gathered deterministically (git, gh, Slack search: read-only, zero tokens); one Sonnet call answers
the three questions; code then applies hard rules (a human duplicate PR always links; a drop needs a citation, an
explained precedent or a merged fix found in the evidence, otherwise it becomes a question to the thread; the user's
own instructions always build).
Outcome: build / ask (a short question for the thread) / drop (with reason) / link (to the existing work).
The verdict is stored on the item (data.pregate + gate.json) and printed in every case report.
"""
from __future__ import annotations

import json
import re
import time

from . import cases, cover, db, gh, site, slack, worktree
from .core import CFG, item_dir, log, now, run

VERDICTS = ("build", "ask", "drop", "link")
NOT_A_BUG = ("expected_behavior", "config", "data", "user_error")
OPEN_DAYS = 14
MERGED_DAYS = 120
FILE_RE = re.compile(r"(?<![\w/])((?:[\w.-]+/)+[\w.-]+\.(?:py|ts|tsx|js|jsx|sql))(?::(\d+))?")
CITE_RE = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|sql|json|ya?ml)(?::|#L)\d+")

SCHEMA = {"type": "object", "required": ["is_real", "precedent", "someone_on_it", "verdict", "reason"], "properties": {
    "is_real": {"type": "object", "required": ["answer", "evidence"], "properties": {
        "answer": {"type": "string", "enum": ["bug", "need", "expected_behavior", "config", "data", "user_error", "unknown"]},
        "evidence": {"type": "string", "description": "why, with file:line citations from the repos (read them)"},
        "citations": {"type": "array", "items": {"type": "string"}, "description": "file:line you read that proves the answer"}}},
    "precedent": {"type": "object", "required": ["answer", "evidence"], "properties": {
        "answer": {"type": "string", "enum": ["none", "solved_before", "explained_before", "partial"]},
        "evidence": {"type": "string"}, "links": {"type": "array", "items": {"type": "string"}}}},
    "someone_on_it": {"type": "object", "required": ["answer", "evidence"], "properties": {
        "answer": {"type": "string", "enum": ["no", "open_pr", "assigned", "discussing"]},
        "who": {"type": "string"}, "link": {"type": "string"}, "evidence": {"type": "string"}}},
    "verdict": {"type": "string", "enum": list(VERDICTS)},
    "reason": {"type": "string", "description": site.own("one sentence, Spanish", f"one sentence, in {site.language_name()}")},
    "question": {"type": "string", "description": site.own(
        "verdict=ask only: ONE short question for the thread, in Spanish, voseo, the exact fact only a human has",
        "verdict=ask only: ONE short question for the thread, in the language the thread is written in, the exact fact "
        "only a human has")}}}


# ---- evidence (deterministic, read-only) ---------------------------------------------------------------------------
def _dossier_text(it: dict) -> str:
    ds = it["data"].get("dossier") or {}
    return " ".join([it["title"], ds.get("problem", ""), ds.get("root_cause", ""),
                     json.dumps(ds.get("evidence", ""), ensure_ascii=False),
                     json.dumps(ds.get("hypotheses", ""), ensure_ascii=False)[:3000]])


def files_of(text: str) -> list[str]:
    out = []
    for m in FILE_RE.finditer(text or ""):
        f = m.group(1).lstrip("./")
        if f not in out and not re.search(r"(^|/)(tests?|__tests__)/|test_[^/]+\.py$|\.(test|spec)\.", f):
            out.append(f)
    return out[:8]


def _resolve(repo: str, f: str) -> str | None:
    """A cited path (often relative to a package) to the tracked path in the repo's read-only mirror."""
    from .report import _file_index
    idx = _file_index([str(worktree.mirror_path(repo))])
    if f in idx:
        return f
    hits = [p for p in idx if p.endswith("/" + f)]
    return hits[0] if len(hits) == 1 else None


def merged_touching(repos: list[str], files: list[str], days: int = MERGED_DAYS) -> list[dict]:
    """Commits on the default branch (squash-merged PRs) that touched the same files recently."""
    out = []
    for repo in repos:
        mirror = worktree.mirror_path(repo)
        for f in files:
            path = _resolve(repo, f)
            if not path:
                continue
            r = run(["git", "-C", str(mirror), "log", f"--since={days}.days", "-n", "5", "--date=short",
                     "--format=%h%x09%an%x09%ad%x09%s", "--", path], timeout=60)
            for line in r.stdout.splitlines():
                sha, who, date, subj = (line.split("\t", 3) + ["", "", ""])[:4]
                pr = re.search(r"\(#(\d+)\)\s*$", subj)
                out.append({"repo": repo, "file": path, "sha": sha, "who": who, "date": date, "subject": subj[:160],
                            **({"url": site.pr_url(repo, pr.group(1))} if pr else {})})
    return out[:15]


def merged_by_search(repos: list[str], terms: list[str], days: int = MERGED_DAYS) -> list[dict]:
    since = time.strftime("%Y-%m-%d", time.localtime(now() - days * 86400))
    out, seen = [], set()
    for repo in repos:
        for t in terms[:3]:
            try:
                rows = gh.pr_search(repo, f'"{t}" merged:>={since}', state="merged", limit=5,
                                    fields="number,title,author,url,mergedAt")
            except Exception as e:
                log("pregate: merged search failed", repo=repo, err=str(e)[:120])
                continue
            for r in rows:
                if (repo, r["number"]) not in seen:
                    seen.add((repo, r["number"]))
                    out.append({"repo": repo, "number": r["number"], "title": r["title"], "url": r.get("url"),
                                "author": (r.get("author") or {}).get("login"), "merged": r.get("mergedAt"), "term": t})
    return out[:12]


def _human(login: str) -> bool:
    from .tasks import _bot_author
    return bool(login) and login != CFG.get("self_github") and not _bot_author(login)


def open_prs_on(repos: list[str], files: list[str], tickets: list[str], days: int = OPEN_DAYS) -> list[dict]:
    """Open PRs by humans (not the user, not bots) updated in the last `days` that touch the same files or name the
    same ticket (title, body or branch). Shaped as tasks.related_prs candidates."""
    since = time.strftime("%Y-%m-%d", time.localtime(now() - days * 86400))
    out = []
    for repo in repos:
        try:
            rows = gh.pr_search(repo, f"updated:>={since}", state="open", limit=50)
        except Exception as e:
            log("pregate: open PR list failed", repo=repo, err=str(e)[:120])
            continue
        paths = [_resolve(repo, f) or f for f in files]
        for r in rows:
            login = (r.get("author") or {}).get("login", "")
            if not _human(login):
                continue
            theirs = [x["path"] for x in r.get("files") or []]
            hits = [f"file:{p}" for p in paths if any(t == p or t.endswith("/" + p) or p.endswith("/" + t) for t in theirs)]
            blob = f"{r.get('title', '')} {r.get('body') or ''} {r.get('headRefName', '')}".upper()
            hits += [f"ticket:{k}" for k in tickets if k.upper() in blob]
            if hits:
                out.append({**r, "repo": repo, "hits": hits})
    return out[:10]


def slack_mentions(terms: list[str], days: int = 180) -> list[dict]:
    after = time.strftime("%Y-%m-%d", time.localtime(now() - days * 86400))
    out, seen = [], set()
    for t in terms[:2]:
        try:
            for m in slack.search(f'"{t}" after:{after}', count=8):
                ch = (m.get("channel") or {}).get("id")
                if not ch or ch == CFG.get("self_dm_channel") or m.get("ts") in seen:
                    continue
                seen.add(m.get("ts"))
                out.append({"when": db.fmt_time(m["ts"]), "channel": (m.get("channel") or {}).get("name"),
                            "who": slack.user_name(m.get("user")) if m.get("user") else m.get("username"),
                            "text": slack.render(m.get("text") or "")[:300], "link": m.get("permalink")})
        except Exception as e:
            log("pregate: slack search failed", term=t, err=str(e)[:120])
    return out[:10]


def jira_status(tickets: list[str]) -> list[dict]:
    """Assignee and status of the case's tickets (read-only, through the MCP bridge)."""
    if not tickets:
        return []
    from . import bridge
    try:
        return bridge.fetch("pregate-jira", f'For each of these Jira keys call getJiraIssue with cloudId "{bridge.JIRA_CLOUD}": '
                            f'{", ".join(tickets[:3])}. Fields: summary, status, assignee, updated. Return key, summary, status, '
                            f'assignee display name (or "unassigned") and updated for each.',
                            ["mcp__claude_ai_Atlassian__getJiraIssue"],
                            {"type": "object", "required": ["key"], "properties": {
                                "key": {"type": "string"}, "summary": {"type": "string"}, "status": {"type": "string"},
                                "assignee": {"type": "string"}, "updated": {"type": "string"}}})
    except Exception as e:
        log("pregate: jira lookup failed", err=str(e)[:160])
        return [{"error": str(e)[:200]}]


def gather(it: dict) -> dict:
    from . import precedents
    from .tasks import _terms, dossier_repos
    d = it["data"]
    ds = d.get("dossier") or {}
    text = _dossier_text(it)
    repos = dossier_repos(ds) or list(CFG["repos"])
    files = files_of(text)
    terms = _terms(text) or [w for w in re.findall(r"[A-Za-z_]{8,}", it["title"])][:3]
    tickets = cases.tickets_in(f"{it['title']} {d.get('text', '')} {d.get('summary', '')} {d.get('jira', '')}")
    ev = {"repos": repos, "files": files, "terms": terms, "tickets": tickets}
    steps = (("stored_precedents", lambda: precedents.search(f"{text} {d.get('text', '')}", limit=5)),
             ("merged_touching_files", lambda: merged_touching(repos, files)),
             ("merged_by_search", lambda: merged_by_search(repos, terms)),
             ("open_prs_by_humans", lambda: open_prs_on(repos, files, tickets)),
             ("slack", lambda: slack_mentions(terms)),
             ("jira", lambda: jira_status(tickets)))
    for key, fn in steps:
        try:
            ev[key] = fn()
        except Exception as e:  # every source is best effort; the verdict says what could not be checked
            ev[key] = []
            ev.setdefault("errors", []).append(f"{key}: {str(e)[:160]}")
    hunt = item_dir(it["id"]) / "precedents.json"
    if hunt.exists():
        ev["precedent_hunt"] = hunt.read_text()[:4000]
    return ev


# ---- decision (pure: tested and selftested) ------------------------------------------------------------------------
# a question for the thread when the model gave none: the first install's own words, else a plain one in the
# install's language (it names no customer kind)
DEFAULT_QUESTION = site.own("antes de tocar código: ¿esto pasa siempre o solo con ciertos datos/config del colegio? "
                            "¿me pasan un caso concreto (colegio, usuario, pantalla) para reproducirlo?",
                            site.t("before touching code: does this always happen, or only with certain data or settings? "
                                   "Could you share a concrete case (account, user, screen) so I can reproduce it?",
                                   "antes de tocar código: ¿esto pasa siempre o solo con ciertos datos o configuraciones? "
                                   "¿me pasan un caso concreto (cuenta, usuario, pantalla) para reproducirlo?"))


def _urls(ev: dict) -> set[str]:
    out = set()
    for k in ("open_prs_by_humans", "merged_touching_files", "merged_by_search", "slack", "stored_precedents"):
        for x in ev.get(k) or []:
            for f in ("url", "link"):
                if x.get(f):
                    out.add(x[f])
            for l in x.get("links") or []:
                out.add(l)
    for t in ev.get("tickets") or []:
        out.add(t)
    return out


def decide(model: dict | None, related: list[dict], ev: dict, *, instructions: str | None = None,
           has_thread: bool = True) -> dict:
    """The final verdict from the model's answers and hard rules. Never drops without evidence."""
    m = model or {}
    real = m.get("is_real") or {}
    prec = m.get("precedent") or {}
    onit = m.get("someone_on_it") or {}
    verdict = m.get("verdict") if m.get("verdict") in VERDICTS else "build"
    reason = (m.get("reason") or "").strip()
    out = {"model_verdict": m.get("verdict"), "is_real": real, "precedent": prec, "someone_on_it": onit}
    cited = bool(CITE_RE.search(" ".join([real.get("evidence", "")] + list(real.get("citations") or []))))
    out["cited"] = cited
    human_dup = next((p for p in related if p.get("relation") == "duplicate" and not p.get("bot")), None)
    known = _urls(ev) | {p.get("url") for p in related if p.get("url")}

    def found(url: str) -> bool:  # the link is one the deterministic lookups returned, not one the model wrote
        return bool(url) and (url in known or any(url.endswith(k) or k in url for k in known if k))
    link = None
    if human_dup:
        verdict, link = "link", {"url": human_dup["url"], "who": human_dup.get("author"), "why": human_dup.get("why", "")}
        reason = reason if m.get("verdict") == "link" else site.t(f"{human_dup['url']} by {human_dup.get('author')} already fixes this",
                                                                  f"{human_dup['url']} de {human_dup.get('author')} ya arregla esto")
    elif verdict == "link":
        url = onit.get("link") or next(iter(prec.get("links") or []), "")
        if found(url):
            link = {"url": url, "who": onit.get("who"), "why": onit.get("evidence", "")}
        else:
            out["overridden"] = site.t("link with no PR or ticket the evidence can verify: building it",
                                       "link sin un PR/ticket verificable en la evidencia: se construye")
            verdict = "build"
    if verdict == "drop":
        explained = prec.get("answer") == "explained_before" and bool(prec.get("links"))
        # a merged fix the evidence found is a drop too (10-21: #1062's gate cited schools#1106, a teammate's fix merged 2 h
        # before the report, and was turned into a generic "¿me pasan un caso?" ask that cost a draft and his review)
        solved = prec.get("answer") == "solved_before" and any(found(u) for u in prec.get("links") or [])
        if not ((real.get("answer") in NOT_A_BUG and cited) or explained or solved):
            out["overridden"] = site.t("drop with no code citation and no explained precedent: asking in the thread",
                                       "drop sin cita de código ni precedente explicado: se pregunta en el hilo")
            verdict = "ask"
    question = (m.get("question") or "").strip()
    if verdict == "ask" and not question:
        question = DEFAULT_QUESTION
    if instructions and verdict != "build":
        out["overridden"] = site.t(f"{site.first_name()} gave instructions for this case: building it (the gate said {verdict})",
                                   f"{site.first_name()} dio instrucciones para este caso: se construye (el gate decía {verdict})")
        out["advisory_verdict"] = verdict
        verdict = "build"
    if verdict == "ask" and not has_thread:
        out["no_thread"] = True  # nowhere to ask: the question goes to the user in the case report
    out.update(verdict=verdict, reason=reason or {"build": site.t("a real bug, with no precedent and no work in progress",
                                                                  "bug real, sin precedente ni trabajo en curso")}.get(verdict, ""),
               question=question if verdict == "ask" else "", link=link, at=now())
    return out


def line(g: dict) -> str:
    """One line for the case report: the verdict and the three answers."""
    if not g:
        return ""
    v = {"build": site.t("build", "construir"), "ask": site.t("ask", "preguntar"), "drop": site.t("do not build", "no construir"),
         "link": site.t("already exists", "ya existe")}.get(g.get("verdict"), g.get("verdict"))
    real = (g.get("is_real") or {}).get("answer") or "?"
    cite = next(iter((g.get("is_real") or {}).get("citations") or []), "")
    prec = (g.get("precedent") or {}).get("answer") or "?"
    onit = (g.get("someone_on_it") or {}).get("answer") or "?"
    extra = f" → {g['link']['url']}" if g.get("link") else (f" → «{g['question']}»" if g.get("question") else "")
    return (f"Gate: {v} ({g.get('reason', '')[:140]}) · real: {real}{' ' + cite if cite else ''} · "
            + site.t(f"precedent: {prec} · someone on it: {onit}{extra}", f"precedente: {prec} · alguien en esto: {onit}{extra}"))


def _covered_note(g: dict) -> str:
    """The report line for a case someone else's work already covers."""
    url, who, why = g["link"]["url"], g["link"].get("who"), g.get("reason", "")
    return site.t(f"Not building: {url}" + (f" by {who}" if who else "") + f" already covers this. {why}",
                  f"No construimos: {url}" + (f" de {who}" if who else "") + f" ya cubre esto. {why}")


# ---- stage entry -------------------------------------------------------------------------------------------------------
def stale(it: dict) -> bool:
    """Thread news named someone else's PR after the verdict: 'is someone on it' is asked again before the build. The
    interrupted stage only resumes its session with the news, and a plan has no way to say 'covered'."""
    d = it["data"]
    at = (d.get("pregate") or {}).get("at", 0)
    ours = {p.get("pr_url") for p in d.get("parts") or []}
    return any(n.get("at", 0) > at and n.get("impact") in ("changes_task", "redundant", "blocker")
               and any(m.group(0) not in ours for m in cover.PR_URL.finditer(n.get("text") or ""))
               for n in d.get("inbox") or [])


def run_gate(it: dict) -> bool:
    """Runs once per item, at the start of `plan`. True: go on and plan the build. False: the item moved."""
    from . import claude, report, tasks
    d = it["data"]
    folder = item_dir(it["id"])
    ev = gather(it)
    (folder / "gate-evidence.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1, default=str))
    try:
        related = tasks.related_prs(it, extra=ev.get("open_prs_by_humans"))
    except Exception as e:
        related = []
        log("pregate: related PRs failed", item=it["id"], err=str(e)[:160])
    it = db.item(it["id"])
    ds = d.get("dossier") or {}
    prompt = ("Three questions before anyone builds a fix for this case. Answer each with evidence; read the code in the "
              f"read-only repos ({tasks.READ_ROOT}) to cite file:line. Be skeptical: a report is not a bug until the code says so.\n"
              "(a) is_real: is it really a bug or a real need, or expected behavior / school config / data / user error?\n"
              "(b) precedent: was it solved or explained before (store, merged PRs on the same files, Slack)? A merged fix "
              "that already covers it means the report is stale or a regression: say which.\n"
              "(c) someone_on_it: is a teammate already on it (open PR by a human on the same files or ticket, Jira assignee, "
              "a teammate saying so in the thread)?\n"
              "verdict: build (real, ours, nobody on it) | ask (a fact only a human has decides it: write the ONE question) | "
              "drop (not a bug, or already solved and deployed: cite it) | link (someone else's open work covers it: give "
              "its link). Prefer build when unsure between build and drop.\n\n"
              f"DOSSIER: {json.dumps({k: ds.get(k) for k in ('problem', 'problem_kind', 'root_cause', 'root_cause_status', 'evidence', 'repos')}, ensure_ascii=False)[:6000]}\n\n"
              f"CONTEXT (the report and its thread):\n{(folder / 'context.md').read_text()[:8000] if (folder / 'context.md').exists() else d.get('text', '')[:3000]}"
              f"{tasks._later_messages(it)}\n\n"
              f"EVIDENCE GATHERED (verbatim, read-only lookups):\n{json.dumps(ev, ensure_ascii=False, default=str)[:12000]}\n\n"
              f"OPEN PRs JUDGED AGAINST THIS PROBLEM: {json.dumps(related, ensure_ascii=False)[:4000]}")
    model = None
    try:
        with claude.pool_ctx("delivery"):
            model = claude.lean("pregate", prompt, model="sonnet", cwd=tasks.READ_ROOT, schema=SCHEMA,
                                tools=("Read", "Grep", "Glob"), add_dirs=(str(folder),), item_id=it["id"], timeout=600)
    except Exception as e:  # the gate never blocks delivery on its own failure: the hard rules still apply
        log("pregate: model failed, deciding on hard rules", item=it["id"], err=str(e)[:160])
        ev.setdefault("errors", []).append(f"model: {str(e)[:160]}")
    g = decide(model, related, ev, instructions=d.get("instructions"), has_thread=bool(d.get("channel")))
    refs = cover.pr_refs((g.get("link") or {}).get("url", "")) if g["verdict"] == "link" else []
    if refs:
        # a PR covers the case only with proof: it names the ticket, links the thread, or its diff fixes it. Each PR of
        # the link is tried (10-15: #814's link named schools#1054 and cometa#2951, only the backend half was judged, it
        # alone does not stop the per-row calls, and the case built a duplicate of a teammate's pair for 11.8 USD)
        prf = next((p for p in (cover.proof(it, *r, problem=ds.get("problem") or "") for r in refs) if p), None)
        if prf:
            g["proof"] = prf
        else:
            g.update(verdict="build", advisory_verdict="link", link=None,
                     overridden=site.t("link with no proof that the PR fixes this case (no ticket, thread or diff): building it",
                                       "link sin prueba de que el PR arregla este caso (ni ticket, ni hilo, ni el diff): se construye"))
    if ev.get("errors"):
        g["unchecked"] = ev["errors"]
    (folder / "gate.json").write_text(json.dumps(g, ensure_ascii=False, indent=1, default=str))
    db.add_signal("pregate", {"verdict": g["verdict"], "model": g.get("model_verdict"), "overridden": g.get("overridden")}, it["id"])
    db.update_item(it["id"], data={"pregate": g})
    log("pregate verdict", item=it["id"], verdict=g["verdict"], reason=g.get("reason", "")[:120])
    v = g["verdict"]
    if v == "build":
        return True
    parent = db.item(d["parent"]) if d.get("parent") else None
    if parent:
        # a verification child never talks on its own (tasks.investigate): the verdict travels in the parent's redraft
        db.update_item(it["id"], status="dropped", note=site.t(f"gate: {v}, verdict to the parent case", f"gate: {v}, veredicto al caso padre"))
        if parent["kind"] == "REPLY" and parent["status"] in ("awaiting_approval", "awaiting_human", "reply_draft"):
            ds2 = {**ds, "recommendation": "ask" if v == "ask" else "drop", "pregate": line(g),
                   "questions": [g["question"]] if v == "ask" else []}
            db.update_item(parent["id"], status="reply_draft", attempts=0, next_run_at=0,
                           note=site.t("redraft with the gate's verdict", "redraft con el veredicto del gate"),
                           data={"dossier": ds2, "verified": True, "verified_at": now(), "child": None, "fix_in_progress": False,
                                 "ask": v == "ask"})
        return False
    if v == "link" and g.get("proof"):
        # covered, not dropped: the case stays linked to the PR and closing.py finishes it when it merges
        cover.link(it, g["proof"], _covered_note(g))
        return False
    if v == "link":  # not a PR (a teammate's ticket or assignment): nothing to link for closing
        url, who = g['link']['url'], g["link"].get("who")
        db.update_item(it["id"], status="dropped", note=site.t(f"covered by {url}" + (f" by {who}" if who else ""),
                                                               f"lo cubre {url}" + (f" de {who}" if who else "")))
        note = _covered_note(g)
    elif v == "drop":
        db.update_item(it["id"], status="dropped", note=f"gate: {g.get('reason', '')[:160]}")
        note = site.t(f"Not building: {g.get('reason', '')} (evidence in the detail).",
                      f"No construimos: {g.get('reason', '')} (evidencia en el detalle).")
    else:  # ask
        if g.get("no_thread"):
            db.update_item(it["id"], status="awaiting_human", note=site.t("gate: a fact is missing and there is no thread to ask in",
                                                                         "gate: falta un dato, sin hilo donde preguntar"))
            note = site.t(f"Before building, a fact only a person has is missing; there is no source thread. Question: {g['question']}",
                          f"Antes de construir falta un dato que solo tiene una persona; no hay hilo de origen. Pregunta: {g['question']}")
        else:
            ds2 = {**ds, "questions": [g["question"]], "recommendation": "ask"}
            db.update_item(it["id"], status="reply_draft", note=site.t("gate: asking in the thread before building",
                                                                       "gate: preguntar en el hilo antes de construir"),
                           data={"dossier": ds2, "ask": True})
            return False  # the reply lane drafts the question and reports it (one message, not two)
    try:
        report.post(db.item(it["id"]), "finding", note=note, question=(g.get("question") or "") if v == "ask" else "")
    except Exception as e:
        log("pregate: report failed", item=it["id"], err=str(e)[:160])
    return False
