"""Squad PR reviews: Muse finders (wide, free) -> Opus verification in the user's
review voice -> a PENDING GitHub review he edits/submits (the submit is the greenlight).
The one v1 artifact class with a proven approval path, now the primary product."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import approvals, claude, db, finders, gh, outbound, site, worktree
from .core import CFG, item_dir, log, now

VERIFY_SCHEMA = {"type": "object", "required": ["comments", "body", "dropped"], "properties": {
    "comments": {"type": "array", "items": {"type": "object", "required": ["path", "line", "body", "severity"],
                 "properties": {"path": {"type": "string"}, "line": {"type": "integer"},
                                "body": {"type": "string"}, "severity": {"type": "string"},
                                "candidate_ids": {"type": "array", "items": {"type": "integer"}}}}},
    "body": {"type": "string"},
    "dropped": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "reason": {"type": "string"}}}}}}


def _existing_comments(repo: str, n: int) -> str:
    out = []
    for c in gh.review_comments(repo, n):
        out.append(f"- {c['user']['login']} on {c.get('path')}:{c.get('line')}: {c['body'][:400]}")
    for c in gh.issue_comments(repo, n):
        out.append(f"- {c['user']['login']} (conversation): {c['body'][:400]}")
    return "\n".join(out[-60:]) or "(none)"


# Specs are never reviewed (the user, 2026-10-22: "NEVER review specs (meant for frontend references only) like the ones
# diego or joao do"): design specs, UX prototypes and demos on mock data exist to be looked at in the preview, not
# merged (cometa#1740 'spec(dashboard):... no mergear', #2569 'Prototipo:...', #2695 '[DEMO · NO MERGEAR]...').
# By what the PR is, not by who wrote it: their real changes (cometa#2960, #2524) are reviewed like anyone's, and a
# real PR that merely mentions a prototype (cometa#2386 '... (prototipo + pantallas)') is not a spec
SPEC_TITLE = re.compile(r"^\s*(\[[^\]]*\]\s*)?(spec\b|prototipo\b|prototype\b)|\[demo\b|\bdemo\s*·|\bmaqueta\b|\bmockup\b", re.I)
SPEC_BODY = re.compile(r"spec(ificación)? de (diseño|experiencia|ux)|especificación de experiencia|un prototipo de ux|"
                       r"datos (100 ?% )?simulados|no se conecta al backend|demo\s*·\s*no mergear", re.I)


def is_spec(title: str, body: str = "") -> bool:
    return bool(SPEC_TITLE.search(title or "") or SPEC_BODY.search(re.sub(r"[*_`]", "", body or "")[:800]))


def prepare(it: dict) -> None:
    repo, n = it["repo"], it["pr"]
    pr = gh.pr_view(repo, n)
    me = CFG["self_github"]
    requested = any((rr.get("login") == me) for rr in pr.get("reviewRequests") or [])
    # a draft is reviewed only when its author asked the user by name (ingest.poll_github)
    if pr["state"] != "OPEN" or (pr["isDraft"] and not (requested or it["data"].get("review_requested"))):
        return db.update_item(it["id"], status="dropped", note=f"PR {pr['state']} draft={pr['isDraft']}")
    if is_spec(pr["title"], pr.get("body") or ""):
        return db.update_item(it["id"], status="dropped", note=site.t("spec or prototype: a reference for the frontend, not reviewed",
                                                                      "spec o prototipo: es referencia para el front, no se revisa"))
    author = (pr.get("author") or {}).get("login", "")
    if author == me:
        return db.update_item(it["id"], status="dropped", note="own PR")
    if gh.bot_author(pr.get("author")):
        return db.update_item(it["id"], status="dropped", note=f"bot PR ({author})")
    db.update_item(it["id"], data={"author": author, "author_name": ((pr.get("author") or {}).get("name") or "")})
    mine = [r for r in gh.reviews(repo, n) if (r.get("user") or {}).get("login") == me]
    if any(r.get("commit_id") == pr["headRefOid"] and r["state"] != "PENDING" for r in mine):
        return db.update_item(it["id"], status="dropped", note="already reviewed this head")
    if any(r["state"] == "PENDING" for r in mine):
        return db.update_item(it["id"], status="dropped", note=f"a pending review of {site.first_name()}'s already exists")
    human_approvals = {(r.get("user") or {}).get("login") for r in gh.reviews(repo, n)
                       if r["state"] == "APPROVED" and "bot" not in (r.get("user") or {}).get("login", "")
                       and "coderabbit" not in (r.get("user") or {}).get("login", "")}
    if human_approvals and not requested and not it["data"].get("source_event"):
        return db.update_item(it["id"], status="dropped", note=f"already approved by {sorted(human_approvals)}")
    wt = worktree.for_pr_review(repo, n, pr["headRefOid"])
    d = item_dir(it["id"])
    diff = gh.pr_diff(repo, n)
    (d / "diff.patch").write_text(diff)
    (d / "existing_comments.md").write_text(_existing_comments(repo, n))
    ctx = f"{repo}#{n}: {pr['title']}\nauthor: {pr['author']['login']}\nbase: {pr['baseRefName']}\n\n{(pr.get('body') or '')[:4000]}"
    # after his submitted review only what changed since is reviewed (2026-10-01: the author's next push on schools#956
    # started a review from scratch, $5.4 for two comments on code he had already reviewed at 9dcfc68)
    since = next((r["commit_id"] for r in reversed(mine) if r["state"] != "PENDING" and r.get("commit_id")), None)
    # or the head a clean review read, when he went to approve it and the PR had moved (actions.approve_review): only the
    # new commits stand between him and the approve
    since = since or it["data"].get("approve_since")
    data = {"wt": str(wt), "head_sha": pr["headRefOid"], "title": pr["title"], "url": pr["url"], "since_sha": None,
            "approve_since": None}
    (d / "delta.patch").unlink(missing_ok=True)
    if since:
        files = re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)  # the PR's files: a merge from the base is not the author's change
        p = worktree._git(wt, "diff", f"{since}..{pr['headRefOid']}", "--", *files, check=False)
        if p.returncode == 0 and not p.stdout.strip():
            return db.update_item(it["id"], status="dropped", note=f"no PR changes since his review at {since[:7]}")
        if p.returncode == 0:  # else (force-push, commit gone): the whole PR again
            (d / "delta.patch").write_text(p.stdout)
            data["since_sha"] = since
            ctx += (f"\n\nFOLLOW-UP REVIEW: {site.first_name()} already reviewed this PR at {since[:7]}. Review only what changed since "
                    f"(the diff you are given); the rest was reviewed then. Check whether the changes address his comments.")
    (d / "context.md").write_text(ctx)
    db.update_item(it["id"], status="review_find", data=data)


def find(it: dict) -> None:
    d = item_dir(it["id"])
    cands = finders.run(Path(it["data"]["wt"]), d / ("delta.patch" if it["data"].get("since_sha") else "diff.patch"),
                        (d / "context.md").read_text() + "\n\nExisting comments (do not repeat):\n"
                        + (d / "existing_comments.md").read_text(), it["id"])
    (d / "candidates.json").write_text(json.dumps(cands, ensure_ascii=False, indent=1))
    if not cands:
        return _clean(it)
    db.update_item(it["id"], status="review_verify", data={"candidates": len(cands)})


def _clean(it: dict, said: str = "") -> None:
    """A clean review is a deliverable too: before, it parked with a note nothing read, so he never heard of it
."""
    db.add_signal("review_clean", {"repo": it["repo"], "pr": it["pr"]}, it["id"])
    since = it["data"].get("since_sha")
    what = (site.t(f"what is new since your review at {since[:7]}", f"lo nuevo desde tu review en {since[:7]}") if since
            else site.t("the diff", "el diff"))
    gist = site.t(f"review with no findings in {what}: it can be approved", f"review sin hallazgos en {what}: se puede aprobar")
    # done, not awaiting_human: nothing to post means nothing to wait on him for (2026-10-22, the user on #1092: "why is
    # it waiting for me... reviews shouldn't wait usually"). A clean review approves the head it read (approve_clean)
    db.update_item(it["id"], status="done", note=gist, data={"outcome_note": gist, "gist": gist, "comments": 0, "clean": True})
    try:
        approved = approve_clean(db.item(it["id"]))
    except Exception as e:  # the review is done either way; stale's catch-up does not retry a tried one
        log("review: clean approve failed", item=it["id"], err=str(e)[:160])
        approved = False
    try:
        from . import report
        url = it["data"].get("url") or site.pr_url(it["repo"], it["pr"])
        title = it['data'].get('title', '')[:80]
        report.post(db.item(it["id"]), "update", body=site.t(
                        f"Reviewed <{url}|{it['repo']}#{it['pr']}> ({title}): no findings in {what}. ",
                        f"Revisé <{url}|{it['repo']}#{it['pr']}> ({title}): sin hallazgos en {what}. ")
                    + (f"{said.strip()[:400]} " if said.strip() else "")
                    + (site.t("I approved it.", "Lo aprobé.") if approved
                       else site.t("It is not waiting on you: if it looks right, approve it from the case.",
                                   "No te espera: si te cierra, aprobalo desde el caso.")))
    except Exception as e:  # the review result is stored either way
        log("review: clean notice failed", item=it["id"], err=str(e)[:160])


def approve_clean(it: dict) -> bool:
    """A clean review approves the PR at the head it read. The guard allows only a bare
    approve of a teammate's open, ready PR at that exact head. A push since the review is unread code: the review reads
    it first (review_prepare), as actions.approve_review does. Tried once per item."""
    from .guardcall import act
    from .outbound import _shadow, acting
    d = it["data"]
    # paused by config
    if not CFG.get("reviews", {}).get("approve_clean", False):
        return False
    if not d.get("clean") or d.get("comments") or d.get("approved_by_him") or d.get("approve_tried"):
        return False
    p = {"repo": it["repo"], "pr": it["pr"], "head_sha": d.get("head_sha")}
    if not acting("submit_approved_reviews"):  # shadow: say what it would do, change nothing
        _shadow("github_approve_clean", **p)
        return False
    db.update_item(it["id"], data={"approve_tried": now()})
    pr = gh.pr_view(it["repo"], it["pr"])
    if pr["state"] != "OPEN" or pr["isDraft"]:
        return False
    if pr["headRefOid"] != d.get("head_sha"):
        # a newer review of the PR is already open: it reads the new commits and approves if they are clean
        if db.q1(f"SELECT 1 FROM items WHERE thread_key=? AND id!=? AND status NOT IN {db.TERMINAL}", it["thread_key"], it["id"]):
            return False
        db.update_item(it["id"], status="review_prepare", attempts=0, next_run_at=0, data={"approve_since": d.get("head_sha"), "approve_tried": None},
                       note=site.t("the PR changed after the review: reviewing what is new before approving",
                                   "el PR cambió después de la review: revisa lo nuevo antes de aprobar"))
        return False
    me = CFG["self_github"]
    if any((r.get("user") or {}).get("login") == me and r.get("state") == "APPROVED" and r.get("commit_id") == p["head_sha"]
           for r in gh.reviews(it["repo"], it["pr"])):  # two clean reviews of one head approved it twice
        db.update_item(it["id"], data={"approved_by_him": now(), "approved_by": "clea"})
        return True
    try:
        act("gh_approve_clean", **p)
    except Exception as e:
        log("review: clean approve refused", item=it["id"], err=str(e)[:200])
        return False
    db.update_item(it["id"], note=site.t("approved: review with no findings", "aprobado: review sin hallazgos"), data={"approved_by_him": now(), "approved_by": "clea"})
    db.add_signal("review_approved", {"repo": it["repo"], "pr": it["pr"], "by": "clea (clean review)"}, it["id"])
    return True


def _nickname(it: dict) -> str:
    nick = CFG.get("nicknames", {})
    login = (it["data"].get("author") or "").lower()
    first = (it["data"].get("author_name") or "").split(" ")[0].lower()
    return nick.get(login) or nick.get(first) or ""


def _opener(text: str, n: int = 3) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split()[:n])


def _recent_openers(limit: int = 12) -> tuple[list[str], list[str]]:
    """Body and comment openers of his latest reviews (what to avoid repeating)."""
    bodies, comments = [], []
    for a in db.q("SELECT content, outcome_data FROM artifacts WHERE kind='approval' AND json_extract(content,'$.kind')='github_review' "
                  "ORDER BY id DESC LIMIT ?", limit):
        p = (json.loads(a["content"] or "{}").get("payload") or {})
        bodies.append(_opener(p.get("body", ""), 4))
        comments += [_opener(c.get("body", "")) for c in p.get("comments") or []]
    return [b for b in dict.fromkeys(bodies) if b][:8], [c for c in dict.fromkeys(comments) if c][:15]


def _voice_hints(it: dict) -> str:
    """2026-10-19: every review opened 'le pase el diff' and every comment 'puede ser'. The draft sees what was used
    lately, and the author's approved nickname (config nicknames) or none."""
    bodies, comments = _recent_openers()
    nick = _nickname(it)
    return ("\n\nVOICE: recently used review-body openings, do not reuse: " + "; ".join(f'"{b}"' for b in bodies)
            + ". Recently used comment openings, avoid: " + "; ".join(f'"{c}"' for c in comments)
            + ". No two comments in this review may start with the same words."
            + (f" The author's approved nickname is '{nick}': you may greet them with it in the body (e.g. "
               + site.own(f"'buen laburo {nick}!'", f"'nice work {nick}!'") + ")."
               if nick else " No approved nickname for this author: do not greet anyone by name."))


VARY = {"type": "object", "required": ["body", "comments"], "properties": {
    "body": {"type": "string"}, "comments": {"type": "array", "items": {"type": "string"}}}}


def _vary(it: dict, out: dict) -> dict:
    """Deterministic check, one cheap rewrite only when it fails: comments of one review never share an opening and
    the body does not repeat a recent opening. Only the openings change; the findings stay word for word."""
    bodies, _ = _recent_openers()
    firsts = [_opener(c["body"]) for c in out["comments"]]
    dup = len(firsts) != len(set(firsts)) or (_opener(out["body"], 4) in bodies)
    if not dup:
        return out
    nick = _nickname(it)
    try:
        r = claude.lean("review_verify", "Rewrite how each comment and the body START so that no two comments open the same way and "
                        f"the body does not open like any of these recent ones (nor a near variant): {bodies}. Each comment keeps "
                        "its finding, symbols and question; adjust the rest of the sentence so it reads naturally with the new "
                        "opening (grammar must stay right). "
                        + site.own("Openers to choose from: 'puede ser que...?', 'lo dejaste asi a "
                                   "proposito?', 'una duda: ...?', 'qué pasa si...?', 'no habria que...?', 'ojo que..., no?', 'acá... no "
                                   "deberia...?', 'capaz conviene...'. Lowercase, no opening ¿, rioplatense Spanish, never English. ",
                                   "Openers to choose from: 'could it be that...?', 'was this on purpose?', 'one question: "
                                   "...?', 'what happens if...?', 'shouldn't we...?', 'careful here..., right?', 'might be "
                                   "worth...'. Keep the language the comments are already written in. ")
                        + (f"The body opens by greeting the author as '{nick}' (e.g. "
                           + site.own(f"'buen laburo {nick}!', 'muy buena esta {nick}!'", f"'nice work {nick}!', 'good one {nick}!'")
                           + "), the only allowed name. " if nick else "Greet no one by name. ")
                        + f"\nBODY: {out['body']}\nCOMMENTS:\n" + "\n".join(f"{i}. {c['body']}" for i, c in enumerate(out["comments"])),
                        model="sonnet", cwd=it["data"]["wt"], schema=VARY, tools=(), item_id=it["id"], timeout=180)
        if len(r.get("comments") or []) == len(out["comments"]):
            out = {**out, "body": r["body"] or out["body"],
                   "comments": [{**c, "body": nb or c["body"]} for c, nb in zip(out["comments"], r["comments"])]}
            log("review: openings varied", item=it["id"])
    except Exception as e:
        log("review: vary failed, keeping the draft", item=it["id"], err=str(e)[:120])
    return out


def verify(it: dict) -> None:
    d = item_dir(it["id"])
    prompt = (f"PR {it['repo']}#{it['pr']} ({it['data'].get('url')}). Worktree (read-only) at the PR head: "
              f"{it['data']['wt']}. Unified diff: {d / 'diff.patch'}. PR context: {d / 'context.md'}. "
              f"Existing comments already on the PR (never repeat them): {d / 'existing_comments.md'}.\n\n"
              + (f"FOLLOW-UP REVIEW: {site.first_name()} already reviewed this PR at {it['data']['since_sha'][:7]}. Only the changes since "
                 f"then ({d / 'delta.patch'}) are under review; keep a finding only on a line that diff changes.\n\n"
                 if it["data"].get("since_sha") else "")
              + f"Candidate findings (JSON):\n{(d / 'candidates.json').read_text()[:60000]}"
              + (f"\n\n{site.marker()} ASKED FOR CHANGES to your previous version: {it['data']['revision']}\n"
                 f"Previous version: {json.dumps(it['data'].get('revision_of'), ensure_ascii=False)[:6000]}"
                 if it["data"].get("revision") else ""))
    if not Path(it["data"]["wt"]).exists():  # gc removed it while the review waited
        repo, n = it["repo"], it["pr"]
        db.update_item(it["id"], data={"wt": str(worktree.for_pr_review(repo, n, it["data"]["head_sha"]))})
        it = db.item(it["id"])
    prompt += _voice_hints(it)
    out = claude.lean("review_verify", prompt, cwd=it["data"]["wt"], schema=VERIFY_SCHEMA,
                      add_dirs=(str(d), site.root()), item_id=it["id"],
                      urgent=it["urgency"] >= 3)
    out = _vary(it, out)
    anchors = finders.diff_right_lines((d / "diff.patch").read_text())
    delta = (finders.diff_right_lines((d / "delta.patch").read_text())
             if it["data"].get("since_sha") and (d / "delta.patch").exists() else None)
    comments, orphans = [], []
    for c in out["comments"]:
        if delta is not None and c["line"] not in delta.get(c["path"], set()):
            # a follow-up comments only on what changed since his review: the rest he already reviewed
            out["dropped"].append({"id": -1, "reason": f"follow-up: {c['path']}:{c['line']} unchanged since his review"})
        elif c["line"] in anchors.get(c["path"], set()):
            comments.append({"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]})
        else:
            orphans.append(f"{c['path']}:{c['line']}: {c['body']}")
    if not comments and not orphans:
        # every candidate fell at verification: a clean review, not an ask to post an empty one (10-15: #852 asked him
        # to submit '0 comentarios, sin approve' on schools#1077, read as a duplicate of the review he had just sent)
        return _clean(it, out["body"])
    body = out["body"] + ("\n\n" + "\n".join(orphans) if orphans else "")
    (d / "review.json").write_text(json.dumps({"body": body, "comments": comments, "dropped": out["dropped"]},
                                              ensure_ascii=False, indent=1))
    db.update_item(it["id"], status="review_post", data={"kept": len(comments) + len(orphans),
                                                          "dropped": len(out["dropped"])})


def post(it: dict) -> None:
    d = item_dir(it["id"])
    rv = json.loads((d / "review.json").read_text())
    head, read = gh.pr_view(it["repo"], it["pr"])["headRefOid"], it["data"]["head_sha"]
    # a push after the review read the PR leaves it pinned to the head it read, as when the PR moves while the ask waits
    # for him (hours); going back to review_prepare redid both finders, 2.2-2.6 USD each time (10-07 #602 and #633, 10-08
    # #644). What came after is reviewed once he submits: prepare then reads only the commits since his review
    moved = site.t(f"the PR is now at {head[:7]}, this review is of {read[:7]}; ",
                   f"el PR ya va por {head[:7]}, esta review es sobre {read[:7]}; ") if head != read else ""
    if moved:
        log("review: head moved, posting the review of the head it read", item=it["id"], read=read[:7], head=head[:7])
    # ask-first (the user 2026-09-23): the review is only submitted after his OK in the self-DM
    top = (rv["comments"][0].get("body", "").splitlines()[0] if rv["comments"] else rv["body"].splitlines()[0])[:80]
    since = it["data"].get("since_sha")
    db.update_item(it["id"], data={"comments": len(rv["comments"]), "after_approval": "done",
                                   "gist": moved + (site.t(f"follow-up of what is new since your review at {since[:7]}: ",
                                                           f"seguimiento de lo nuevo desde tu review en {since[:7]}: ") if since else "")
                                   + site.t(f"{len(rv['comments'])} comment{'s' if len(rv['comments']) != 1 else ''}, no approval: {top}",
                                            f"{len(rv['comments'])} comentario{'s' if len(rv['comments']) != 1 else ''}, sin approve: {top}")})
    payload = {"repo": it["repo"], "pr": it["pr"], "head_sha": read, "moved": moved, "body": rv["body"], "comments": rv["comments"]}
    if CFG.get("reviews", {}).get("post_comment_only"):
        return _post_now(it, payload)
    approvals.request(it["id"], "github_review", payload, "review_verify")


def _send_review(p: dict) -> dict:
    """Through the guard, which allows it only as a COMMENT review on a teammate's PR that never names the assistant."""
    from .guardcall import act
    from .outbound import _shadow, acting
    if not acting("submit_approved_reviews"):
        return _shadow("github_review_comment", **p)
    return act("gh_review_comment", repo=p["repo"], pr=p["pr"], head_sha=p["head_sha"], body=p["body"],
               comments=p["comments"], event="COMMENT")


def _post_now(it: dict, payload: dict) -> None:
    """the user, 2026-10-22: squad reviews post right away as comments. 118 of 141 drafts died waiting for his OK: the PR
    merged a median 3.1 h after the draft was ready and his OK came a median 66 h later. Never an approve or a request
    for changes (the guard refuses both); a refusal is rewritten once, never asked (refused)."""
    from .guardcall import GuardDenied
    try:
        res = _send_review(payload)
    except GuardDenied as e:
        return refused(it, payload, str(e))
    db.add_artifact(it["id"], "posted_review", f"{it['repo']}#{it['pr']}",
                    json.dumps({**payload, "result": res}, ensure_ascii=False, default=str))
    db.update_item(it["id"], status="done", note=f"review posted as comments ({len(payload['comments'])}) without an ask")


def refused(it: dict, payload: dict, why: str) -> None:
    """The guard would not post the review. A review never waits on him: the verifier rewrites it once with the refusal as the
    note, and a second refusal (or one no rewrite can fix, like his own PR) ends the case."""
    n = it["data"].get("guard_refusals", 0)
    log("review: guard refused the post", item=it["id"], why=why[:200], attempt=n + 1)
    if n >= 1 or re.search(r"his own PR|not his PR", why):
        return db.update_item(it["id"], status="dropped", note=site.t(f"could not post it: {why}", f"no se pudo publicar: {why}")[:300],
                              data={"needs_you": False})
    db.update_item(it["id"], status="review_verify", attempts=0, next_run_at=0,
                   note=site.t(f"the guard would not post it, rewriting it: {why}", f"el guard no la dejó publicar, la reescribe: {why}")[:300],
                   data={"guard_refusals": n + 1, "needs_you": False, "revision_of": payload,
                         "revision": f"(not {site.first_name()}: the posting guard) it refused this review: {why}. Rewrite it so it can "
                                     "post as "
                                     "plain comments: never name the assistant (Clea, Claude, autopilot, harness), never approve "
                                     "or request changes."})


def track(it: dict) -> None:
    """awaiting_human for REVIEW: learn from what the user did with the pending review."""
    art = db.q1("SELECT * FROM artifacts WHERE item_id=? AND kind='pending_review' AND outcome IS NULL", it["id"])
    if not art and not it["data"].get("comments"):  # a clean review parked before it ended as done (_clean)
        return db.update_item(it["id"], status="done", note=it["data"].get("gist") or site.t("review with no findings", "review sin hallazgos"),
                              data={"clean": True})
    pr = gh.pr_view(it["repo"], it["pr"])
    # every way out below where nothing reached GitHub is 'dropped', never 'done' (2026-10-22, the user: "if it wasnt
    # done, then don't even show it")
    if not art:
        if pr["state"] != "OPEN":
            db.update_item(it["id"], status="dropped", note=f"PR {pr['state']}")
        return
    rid = int(art["ref"].split(":review:")[1])
    st = gh.review_state(it["repo"], it["pr"], rid)
    ours = json.loads(art["content"])
    if st is None:
        db.set_artifact_outcome(art["id"], "discarded")
        db.add_signal("review_outcome", {"outcome": "discarded", "ours": ours}, it["id"], art["id"])
        return db.update_item(it["id"], status="dropped", note=f"pending review deleted by {site.first_name()}")
    if st["state"] == "PENDING":
        if pr["state"] != "OPEN":
            db.set_artifact_outcome(art["id"], "ignored")
            db.add_signal("review_outcome", {"outcome": "ignored", "ours": ours}, it["id"], art["id"])
            db.update_item(it["id"], status="dropped", note="PR closed with review still pending")
        return
    submitted = gh.review_comments_of(it["repo"], it["pr"], rid)
    final = [{"path": c["path"], "line": c.get("line"), "body": c["body"]} for c in submitted]
    db.set_artifact_outcome(art["id"], "submitted", {"state": st["state"], "body": st.get("body"), "comments": final})
    db.add_signal("review_outcome", {"outcome": "submitted", "state": st["state"], "ours": ours,
                                     "final": {"body": st.get("body"), "comments": final}}, it["id"], art["id"])
    db.update_item(it["id"], status="done", note=f"submitted as {st['state']}")
