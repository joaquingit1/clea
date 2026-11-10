"""'Covered by a PR' needs proof.

A PR covers a case only when one of these holds:
  1. the PR title, body or branch names one of the case's ticket keys;
  2. the PR body links the case's Slack thread;
  3. a model that read the PR diff against the problem says it fixes it, with a reason.
A covered case is not dropped: it ends 'done' with `linked_prs`, and ap/closing.py tells its thread and closes its
tickets when those PRs are merged and live. `linked_prs` is kept apart from `parts` on purpose: `parts` are the
harness's own PRs, which other jobs mark ready, rebase or close as duplicates; a teammate's PR must never get that.
"""
from __future__ import annotations

import re

from . import cases, db, gh, muse, site
from .core import log, now

_REPO = site.repo_or("[a-z-]+")
PR_URL = re.compile(rf"https://github\.com/(?:{site.owners_re()})/({_REPO})/pull/(\d+)")
VERDICT = {"type": "object", "required": ["covers", "reason"], "properties": {
    "covers": {"type": "boolean", "description": "true only if this diff fixes the reported problem itself"},
    "reason": {"type": "string", "description": "the mechanism in the diff that fixes it (file and change), or why not"}}}


def _norm(text: str) -> str:
    # branch names carry keys as cus-1385 / CUS_1385
    ps = "|".join(re.escape(p) for p in site.ticket_prefixes())
    if not ps:  # no known prefix: nothing says which word plus digits is a key
        return text or ""
    return re.sub(rf"\b({ps})[-_]?(\d{{2,6}})\b", lambda m: f"{m.group(1).upper()}-{m.group(2)}", text or "", flags=re.I)


def case_tickets(it: dict) -> set[str]:
    """The tickets a case is about: its source, its thread's messages and its plan. Never what its investigation
    cited (dossier, checks): a board review lists every ticket it read without owning any."""
    d = it["data"]
    pieces = [it.get("title"), it.get("thread_key"), d.get("text"), d.get("summary"), d.get("jira"),
              (d.get("plan") or {}).get("title")] + [u.get("text") for u in d.get("updates") or []] \
        + [" ".join(a.get("tickets") or []) for a in d.get("also_sources", [])]
    # one piece at a time: a Status bot 'esto puede ya existir' list is cut to the end of its piece, not of the case
    return {k for p in pieces if p for k in cases.tickets_in(_norm(p))}


def _thread_refs(it: dict) -> set[str]:
    d = it["data"]
    out = set()
    for ch, ts in [(d.get("channel"), d.get("thread_ts") or d.get("ts"))] + [(a.get("channel"), a.get("ts")) for a in d.get("also_sources", [])]:
        if ch and ts:
            out.add(f"{ch}/p{str(ts).replace('.', '')}")
    return out


def pr_ref(url: str) -> tuple[str, int] | None:
    m = PR_URL.search(url or "")
    return (m.group(1), int(m.group(2))) if m else None


def pr_refs(text: str) -> list[tuple[str, int]]:
    """Every PR the text names, in order: a link can be a set (a backend PR and the frontend PR that uses it)."""
    return list(dict.fromkeys((m.group(1), int(m.group(2))) for m in PR_URL.finditer(text or "")))


def proof(it: dict, repo: str, n: int, problem: str = "", model: bool = True) -> dict | None:
    """The evidence that PR repo#n addresses this case, or None. Deterministic checks first; the model only reads
    the diff when they say nothing."""
    try:
        pr = gh.pr_view(repo, n)
    except Exception as e:
        log("cover: pr_view failed", item=it.get("id"), repo=repo, pr=n, err=str(e)[:120])
        return None
    base = {"repo": repo, "pr": n, "pr_url": pr.get("url") or site.pr_url(repo, n),
            "author": (pr.get("author") or {}).get("login"), "at": now()}
    pr_text = _norm(f"{pr.get('title', '')}\n{pr.get('headRefName', '')}\n{pr.get('body') or ''}")
    named = case_tickets(it) & set(cases.tickets_in(pr_text))
    if named:
        named_ = ', '.join(sorted(named))
        return {**base, "how": "ticket", "reason": site.t(f"the PR names {named_}", f"el PR nombra {named_}")}
    thread = next((t for t in _thread_refs(it) if t in (pr.get("body") or "")), None)
    if thread:
        return {**base, "how": "thread", "reason": site.t(f"the PR links the case's Slack thread ({thread})",
                                                          f"el PR enlaza el hilo de Slack del caso ({thread})")}
    if not model:
        return None
    try:
        diff = gh.pr_diff(repo, n)
    except Exception as e:
        log("cover: diff failed", item=it.get("id"), repo=repo, pr=n, err=str(e)[:120])
        return None
    d = it["data"]
    ds = d.get("dossier") or {}
    problem = problem or "\n".join(x for x in (ds.get("problem"), ds.get("root_cause"), d.get("summary"), (d.get("text") or "")[:2500]) if x)
    v = muse.call("ap-judge", "Does this pull request fix THIS reported problem? Read the diff, not the title. Say covers=true only "
                  "when the change itself removes the cause of this exact problem; touching the same files, the same screen or a "
                  "neighbouring defect is not enough. `reason`: the file and change that fix it, or what is missing.\n\n"
                  f"PROBLEM:\n{problem[:5000]}\n\nPR {repo}#{n}: {pr.get('title', '')}\n{(pr.get('body') or '')[:2000]}\n\n"
                  f"DIFF:\n{diff[:30000]}", cwd=site.primary_path(), schema=VERDICT, lane="judge",
                  item_id=it.get("id"))
    if v.get("covers") and (v.get("reason") or "").strip():
        return {**base, "how": "diff", "reason": v["reason"].strip()[:400]}
    db.add_signal("cover_unproven", {"repo": repo, "pr": n, "why": (v.get("reason") or "")[:300]}, it.get("id"))
    return None


def link(it: dict, prf: dict, note_extra: str = "") -> None:
    """Close the case as covered by a proven PR, keeping the PR linked so closing.py finishes it on merge."""
    linked = [p for p in it["data"].get("linked_prs") or [] if p.get("pr_url") != prf["pr_url"]] + [prf]
    db.update_item(it["id"], status="done", note=site.t(f"covered by {prf['pr_url']} ({prf['how']}: {prf['reason'][:160]})",
                                                        f"lo cubre {prf['pr_url']} ({prf['how']}: {prf['reason'][:160]})"),
                   data={"linked_prs": linked, "covered_by": prf})
    log("cover: linked", item=it["id"], pr=prf["pr_url"], how=prf["how"])
    if note_extra:
        try:
            from . import report
            report.post(db.item(it["id"]), "finding", note=f"{note_extra} Prueba: {prf['reason']}")
        except Exception as e:
            log("cover: report failed", item=it["id"], err=str(e)[:120])
