"""Regression evals mined from REAL history (OpenHands: benchmark-trained critics
scored below random on real work; real outcomes are the only trustworthy supervision).

Suites (evals/cases/<suite>.jsonl, tracked):
  triage   - past messages in the user's channels, labeled by what he actually did
             (answered in thread => needs_joaquin)
  review   - past squad PRs with human review comments that flagged real problems:
             at the reviewed commit, do the finders catch them?
  signal   - cases the learning loop derived from outcomes (expected behavior text)
Each case goes through its lane's muse route (Claude for the agent lanes since 09-23, so it is paid: the 10-16 weekly
run cost $45) and is recorded as eval/<lane> (claude.eval_ctx). Triage cases are repeated CFG.learning.eval_repeats
times so the score is stable enough to detect regressions (v1's single-shot suites had no power).
A weekly Claude canary checks a sample of the cheap judge's verdicts.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import claude, db, finders, gh, muse, site, slack, triage, worktree
from .core import CFG, DATA, HOME, RULES, VAR, log, now

CASES = DATA / "evals/cases"
RESULTS = DATA / "evals/results"
CASES.mkdir(parents=True, exist_ok=True)
RESULTS.mkdir(parents=True, exist_ok=True)
SELF = CFG["self_user_id"]


def _load(suite: str) -> list[dict]:
    f = CASES / f"{suite}.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


def _append(suite: str, cases: list[dict]) -> int:
    have = {c["id"] for c in _load(suite)}
    new = [c for c in cases if c["id"] not in have]
    with (CASES / f"{suite}.jsonl").open("a") as f:
        for c in new:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return len(new)


# ---- seeding from history ----------------------------------------------------
def seed_triage(days: int = 21, max_cases: int = 80) -> int:
    """Root messages by others in the squad channels; label = did the user reply in the thread."""
    oldest = f"{time.time() - days * 86400:.6f}"
    cases = []
    chans = [CFG["team_channel"]] + CFG.get("watched_channels", [])
    for ch in chans:
        for m in slack.history(ch, oldest, pages=10):
            if m.get("user") in (SELF, None) or not slack.is_human(m) or float(m["ts"]) > time.time() - 2 * 86400:
                continue
            replied = False
            if m.get("reply_count"):
                try:
                    replied = any(r.get("user") == SELF for r in slack.replies(ch, m["ts"])[1:])
                except Exception:
                    continue
            mention = f"<@{SELF}>" in (m.get("text") or "")
            cases.append({"id": f"triage:{ch}:{m['ts']}", "channel": ch, "ts": m["ts"], "user": m.get("user"),
                          "text": m.get("text", "")[:3000], "expected": {"needs_joaquin": replied or mention}})
    pos = [c for c in cases if c["expected"]["needs_joaquin"]]
    neg = [c for c in cases if not c["expected"]["needs_joaquin"]]
    random.seed(7)
    pick = pos[: max_cases // 2] + random.sample(neg, min(len(neg), max_cases - min(len(pos), max_cases // 2)))
    return _append("triage", pick)


def seed_review(days: int = 60, max_cases: int = 40) -> int:
    """Human review comments from the squad on merged squad PRs = defects the finders should catch."""
    handles = set(CFG.get("github_team_handles", [])) | {CFG["self_github"]}
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    cases = []
    for repo in CFG["repos"]:
        try:
            prs = gh.pr_list(repo, f"is:merged merged:>={since}", limit=100)
        except Exception:
            continue
        for p in prs:
            if (p.get("author") or {}).get("login") not in handles:
                continue
            for c in gh.review_comments(repo, p["number"]):
                who = c["user"]["login"]
                if who not in handles or who == (p.get("author") or {}).get("login") or c.get("in_reply_to_id"):
                    continue
                if len(c["body"]) < 40:
                    continue
                cases.append({"id": f"review:{repo}#{p['number']}:{c['id']}", "repo": repo, "pr": p["number"],
                              "commit": c.get("original_commit_id") or c.get("commit_id"), "path": c.get("path"),
                              "line": c.get("original_line") or c.get("line"), "comment": c["body"][:1500],
                              "reviewer": who})
    random.seed(11)
    random.shuffle(cases)
    return _append("review", cases[:max_cases])


def add_case_from_signal(lane: str, signal_id: int, expected: str) -> None:
    s = db.q1("SELECT * FROM signals WHERE id=?", signal_id)
    if not s:
        return
    _append("signal", [{"id": f"signal:{signal_id}", "lane": lane, "signal": json.loads(s["data"]), "kind": s["kind"],
                        "expected": expected}])


# ---- runners --------------------------------------------------------------------
class Stopped(Exception):
    """The caller's stop condition came true between cases: the run is cut short and measured nothing."""


def _check(stop) -> None:
    if stop and stop():
        raise Stopped


JUDGE = {"type": "object", "required": ["pass", "why"], "properties": {"pass": {"type": "boolean"}, "why": {"type": "string"}}}


def run_triage(cases: list[dict], repeats: int, stop=None) -> dict:
    schema = {"type": "object", "required": ["needs_joaquin"], "properties": {"needs_joaquin": {"type": "boolean"}}}

    def one(c):
        with claude.eval_ctx():  # a pool thread: the caller's eval tag does not reach it
            return _one(c)

    def _one(c):
        _check(stop)
        ok = ran = 0
        text = (f"### event source=slack channel={c['channel']} author={slack.user_name(c['user'])}\n<message>\n"
                f"{slack.render(c['text'])}\n</message>\nDoes this need {site.first_name()} (answer, action, review)?")
        for _ in range(repeats):
            try:
                out = muse.call("ap-triage", text, cwd=site.primary_path(), schema=schema, lane="triage")
                ok += out["needs_joaquin"] == c["expected"]["needs_joaquin"]
                ran += 1
            except Exception:
                pass  # a failed call measured nothing: it is not a wrong answer
        return {"id": c["id"], "score": ok / ran if ran else None, "expected": c["expected"]}

    with ThreadPoolExecutor(max_workers=3) as ex:
        res = [r for r in ex.map(one, cases) if r["score"] is not None]
    pos = [r for r in res if r["expected"]["needs_joaquin"]]
    return {"n": len(res), "accuracy": sum(r["score"] for r in res) / max(1, len(res)),
            "recall_needs": sum(r["score"] for r in pos) / max(1, len(pos)), "cases": res}


def run_review(cases: list[dict], repeats: int = 1, passes: list[str] | None = None, stop=None) -> dict:
    """Recall of real peer-caught defects: a finding hits if a Muse judge says it raises the
    same concern (same file, nearby line) as the human comment."""
    out = []
    for c in cases:
        _check(stop)
        try:
            wt = worktree.for_pr_review(c["repo"], c["pr"], c["commit"])
        except Exception as e:
            log("eval: review worktree failed", case=c["id"], err=str(e)[:160])
            continue
        base = gh.api(f"repos/{site.slug(c['repo'])}/pulls/{c['pr']}")["base"]["sha"]
        diff_file = VAR / "cache" / f"evaldiff-{c['repo']}-{c['pr']}-{c['commit'][:10]}.patch"
        if not diff_file.exists():
            from .core import run as sh
            sh(["git", "-C", str(wt), "fetch", "--quiet", "origin", base])
            diff_file.write_text(sh(["git", "-C", str(wt), "diff", f"{base}...{c['commit']}"]).stdout)
        hits = 0
        for _ in range(repeats):
            found = finders.run(wt, diff_file, f"PR {c['repo']}#{c['pr']}", item_id=None, passes=passes)
            near = [f for f in found if f["file"] == c["path"]]
            if not near:
                continue
            j = muse.call("ap-judge", f"Human reviewer comment on {c['path']}:{c['line']}:\n{c['comment']}\n\n"
                          f"Automated findings on the same file:\n{json.dumps(near, ensure_ascii=False)[:8000]}\n\n"
                          "pass=true if any finding raises substantially the same concern.",
                          cwd=str(HOME), schema=JUDGE, lane="judge")
            hits += j["pass"]
        out.append({"id": c["id"], "score": hits / repeats})
    return {"n": len(out), "recall": sum(r["score"] for r in out) / max(1, len(out)), "cases": out}


def run_signal(cases: list[dict], lane_filter: str | None = None, stop=None) -> dict:
    """Signal-derived cases: replay the situation through the lane's pack and judge against expected."""
    res = []
    for c in cases:
        if lane_filter and not c["lane"].startswith(lane_filter.split("-")[0]):
            continue
        _check(stop)
        prompt = (f"Situation (from a past outcome): {json.dumps(c['signal'], ensure_ascii=False)[:5000]}\n\n"
                  "Describe concretely what the correct behavior of the assistant is in this situation.")
        try:
            ans = muse.call("ap-judge", prompt, cwd=str(HOME), lane=c["lane"])
            j = muse.call("ap-judge", f"Expected behavior: {c['expected']}\n\nProduced: {ans[:4000]}\n\n"
                          "pass=true if the produced behavior matches the expected behavior in substance.",
                          cwd=str(HOME), schema=JUDGE, lane="judge")
            # what the canary is told it gets (prompts/claude/canary.md): with only id and score, every weekly canary
            # answered 'NOT EVALUATED' with a placeholder judge_accuracy 0
            res.append({"id": c["id"], "score": float(j["pass"]), "expected": str(c["expected"])[:1500],
                        "produced": ans[:1500], "judge_why": str(j.get("why", ""))[:500]})
        except Exception:
            continue
    return {"n": len(res), "score": sum(r["score"] for r in res) / max(1, len(res)), "cases": res}


# ---- the rule gate ----------------------------------------------------------------
def gate_rule(rule: dict, bases: dict | None = None, stop=None) -> dict:
    """Accept a candidate rule only if its lane's suite does not regress with it active. A suite whose calls all failed
    measured nothing: no verdict, the rule stays a candidate for the next lifecycle. `bases`
    keeps each lane's base score for one lifecycle: the pack only changes when the lifecycle compiles it. `stop` is
    checked before every case: a gate started at night ends when work hours begin, no verdict."""
    with claude.eval_ctx():
        try:
            return _gate_rule(rule, bases, stop)
        except Stopped:
            log("eval gate: stopped before a verdict, rule stays a candidate", rule=rule["id"], lane=rule["lane"])
            return {"accept": None, "base": None, "with": None, "stopped": True}


def _gate_rule(rule: dict, bases: dict | None, stop=None) -> dict:
    lane = rule["lane"]
    suite = {"triage": "triage", "review": "review"}.get(lane.split("-")[0])
    pack_file = RULES / f"{lane}.md"
    before = pack_file.read_text() if pack_file.exists() else ""

    def scores() -> dict | None:
        """Each measured case's score by id; None when nothing was measured."""
        if suite == "triage":
            cs = _load("triage")[:30]
            r = run_triage(cs, 2, stop=stop) if cs else None
        elif suite == "review":
            cs = _load("review")[:6]
            r = run_review(cs, 1, stop=stop) if cs else None
        else:
            cs = [c for c in _load("signal") if c["lane"] == lane][:10]
            r = run_signal(cs, lane, stop=stop) if cs else None
        if r is None:
            return {"": 1.0}  # an empty suite has nothing to regress
        return {c["id"]: c["score"] for c in r["cases"]} or None

    bases = {} if bases is None else bases
    base_s = bases[lane] if lane in bases else scores()
    if base_s is not None:
        bases[lane] = base_s
    if base_s is None:
        log("eval gate: not measured, rule stays a candidate", rule=rule["id"], lane=lane)
        return {"accept": None, "base": None, "with": None}
    pack_file.write_text(before + f"- {rule['text']} (when: {rule['trigger']})\n")
    try:
        with_s = scores()
    finally:
        pack_file.write_text(before) if before else pack_file.unlink(missing_ok=True)
    # paired: both runs are scored on the cases measured in both. A case whose calls failed in one run (a reaped judge,
    # an API drop) only left that run's average, so a lost passing case read as a regression (2026-10-19: rule 103 was
    # rejected on 7 of 8 against a base of 9 of 10, the two missing were the 07:21 reaper kill and a 07:54 API error)
    common = sorted(base_s.keys() & (with_s or {}).keys())
    if not common:
        log("eval gate: not measured, rule stays a candidate", rule=rule["id"], lane=lane)
        return {"accept": None, "base": None, "with": None}
    base = sum(base_s[k] for k in common) / len(common)
    with_rule = sum(with_s[k] for k in common) / len(common)
    accept = with_rule >= base - 0.02
    log("eval gate", rule=rule["id"], lane=lane, base=round(base, 3), with_rule=round(with_rule, 3), accept=accept)
    return {"accept": accept, "base": base, "with": with_rule}


def weekly() -> dict:
    with claude.eval_ctx():
        return _weekly()


def _weekly() -> dict:
    rep = CFG["learning"]["eval_repeats"]
    res = {"at": now(), "triage": run_triage(_load("triage"), rep), "review": run_review(_load("review")[:15], 1),
           "signal": run_signal(_load("signal"))}
    summary = {k: {kk: vv for kk, vv in v.items() if kk != "cases"} for k, v in res.items() if isinstance(v, dict)}
    # canary: Claude spot-checks a sample of the cheap judge on signal cases
    try:
        sample = random.sample(res["signal"]["cases"], min(5, len(res["signal"]["cases"])))
        if sample and claude.can_spend("canary"):
            c = claude.lean("canary", json.dumps(sample, ensure_ascii=False)[:12000], cwd=str(HOME), tools=(),
                            schema={"type": "object", "required": ["judge_accuracy"], "properties": {
                                "judge_accuracy": {"type": "number"}, "notes": {"type": "string"}}})
            summary["canary"] = c
    except Exception as e:
        summary["canary_error"] = str(e)[:200]
    (RESULTS / f"{dt.date.today()}.json").write_text(json.dumps({"summary": summary, "detail": res}, ensure_ascii=False, indent=1))
    prev = sorted(RESULTS.glob("*.json"))[-2:-1]
    if prev:
        p = json.loads(prev[0].read_text())["summary"]
        for k in ("triage", "review"):
            for metric in ("accuracy", "recall", "recall_needs"):
                if metric in summary.get(k, {}) and metric in p.get(k, {}) and summary[k][metric] < p[k][metric] - 0.05:
                    db.add_signal("eval_regression", {"suite": k, "metric": metric, "was": p[k][metric], "now": summary[k][metric]})
    db.kv_set("evals_last", summary)
    log("evals weekly", **summary)
    return summary
