"""Self-improvement that compounds instead of bloating.

Signals (automatic, no questions to the user): what he did with each artifact (pending
review submitted/edited/deleted, draft sent as-is/edited/ignored, PR merged/closed),
reviewer and CodeRabbit comments on the harness's own PRs (misses), findings Opus
dropped (finder false positives), triage overturns, blocked items, and corrections he
typed in his own interactive Claude sessions.

Mechanism (Bugbot learned rules + Copilot memory expiry + ACE delta updates):
  nightly: Muse mines unmined signals -> candidate rules (one imperative sentence, a
  trigger, cited evidence) or reinforcements/contradictions of existing rules
  lifecycle (code): candidate -> active when evidence >= N AND the lane's eval suite
  does not regress with it; retired on repeated contradictions or long disuse
  packs: active rules compile into small per-lane packs under rules/ (tracked in git,
  every change is a commit). Prompts themselves stay frozen for cache stability.
"""
from __future__ import annotations

import datetime as dt
import glob
from pathlib import Path
import json
import os
import re
import time

from . import db, muse, site
from .core import CFG, DATA, HOME, KNOWLEDGE, RULES, STYLE, log, now, run

CORRECTION = re.compile(r"(^|\b)(no,|nope|still|again|wrong|you didn'?t|don'?t guess|not what|eso no|no era|"
                        r"otra vez|todav[ií]a|sigue (roto|fallando|igual)|mal\b|why did you|por qu[eé] (hiciste|no))", re.I)
LANES = ["triage", "investigate", "plan", "review", "draft", "deps", "patrol",
         "claude-review_verify", "claude-self_verify", "claude-critique", "claude-draft", "claude-build",
         "claude-fix", "claude-verify_done"]
MINE_SCHEMA = {"type": "object", "required": ["new", "reinforce", "contradict"], "properties": {
    "new": {"type": "array", "items": {"type": "object", "required": ["lane", "rule", "trigger", "evidence"],
            "properties": {"lane": {"type": "string", "enum": LANES}, "rule": {"type": "string"},
                           "trigger": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "integer"}}}}},
    "reinforce": {"type": "array", "items": {"type": "object", "required": ["rule_id", "evidence"], "properties": {
        "rule_id": {"type": "integer"}, "evidence": {"type": "array", "items": {"type": "integer"}}}}},
    "contradict": {"type": "array", "items": {"type": "object", "required": ["rule_id", "evidence"], "properties": {
        "rule_id": {"type": "integer"}, "evidence": {"type": "array", "items": {"type": "integer"}}}}},
    "eval_cases": {"type": "array", "items": {"type": "object", "required": ["lane", "signal_id", "expected"],
                   "properties": {"lane": {"type": "string"}, "signal_id": {"type": "integer"},
                                  "expected": {"type": "string"}}}}}}


def harvest_session_corrections(hours: int = 26) -> int:
    """the user's corrections in his INTERACTIVE sessions (entrypoint=cli) become signals."""
    cutoff = time.time() - hours * 3600
    n = 0
    seen = db.kv_get("corrections_seen", [])
    for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        if os.path.getmtime(f) < cutoff:
            continue
        last_assistant, interactive = "", None
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "user" and interactive is None:
                    interactive = r.get("entrypoint") == "cli"
                    if not interactive:
                        break
                if r.get("type") == "assistant":
                    for c in (r.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "text":
                            last_assistant = c["text"][-600:]
                elif r.get("type") == "user":
                    content = (r.get("message") or {}).get("content")
                    text = content if isinstance(content, str) else ""
                    if not text or text.startswith("<") or len(text) > 2000:
                        continue
                    uid = r.get("uuid")
                    if uid in seen or not CORRECTION.search(text[:300]):
                        continue
                    db.add_signal("session_correction", {"project": os.path.basename(os.path.dirname(f)),
                                                         "said": text[:1500], "after": last_assistant})
                    seen.append(uid)
                    n += 1
    db.kv_set("corrections_seen", seen[-5000:])
    return n


def _active_rules_text() -> str:
    rows = db.q("SELECT id, lane, text, status, json_array_length(evidence) ev FROM rules WHERE status IN ('active','candidate')")
    return "\n".join(f"[{r['id']}] ({r['lane']}, {r['status']}, evidence {r['ev']}) {r['text']}" for r in rows) or "(none yet)"


def mine(limit: int = 120) -> dict:
    sigs = db.q("SELECT * FROM signals WHERE mined=0 AND kind NOT IN ('triage') ORDER BY id LIMIT ?", limit)
    # triage verdicts are only informative when later contradicted; include overturned ones
    sigs += db.q("SELECT * FROM signals WHERE mined=0 AND kind='triage' AND json_extract(data,'$.class')!=json_extract(data,'$.first')")
    if not sigs:
        return {"signals": 0}
    blob = "\n".join(f"#{s['id']} {s['kind']} item={s['item_id']}: {s['data'][:1800]}" for s in sigs)
    prompt = ("Existing rules:\n" + _active_rules_text() + "\n\nNew outcome signals (evidence ids after #):\n" + blob
              + "\n\nLanes: triage, investigate, plan, review (finders), draft (Slack text), deps, patrol, and Claude "
                f"roles claude-review_verify (final review comments in {site.first_name()}'s voice), claude-self_verify, "
                "claude-critique, claude-draft, claude-build (implementation), claude-fix, claude-verify_done.\n"
                "Propose new rules only for patterns with concrete evidence; prefer reinforcing or contradicting "
                "existing rules. Also propose eval cases: signals that make good regression tests, with the "
                "expected correct behavior.")
    out = muse.call("ap-learn", prompt, cwd=str(HOME), schema=MINE_SCHEMA, lane="learn")
    t = now()
    for r in out["new"]:
        ev = [e for e in r["evidence"] if any(s["id"] == e for s in sigs)]
        if not ev:
            continue
        db.x("INSERT INTO rules(lane,text,trigger,status,evidence,created_at) VALUES(?,?,?,?,?,?)",
             r["lane"], r["rule"][:400], r["trigger"][:300], "candidate", json.dumps(ev), t)
    for key, col in (("reinforce", "evidence"), ("contradict", "contradictions")):
        for r in out[key]:
            row = db.q1("SELECT * FROM rules WHERE id=?", r["rule_id"])
            if not row:
                continue
            if key == "reinforce":
                ev = sorted(set(json.loads(row["evidence"])) | set(r["evidence"]))
                db.x("UPDATE rules SET evidence=?, last_used_at=? WHERE id=?", json.dumps(ev), t, row["id"])
            else:
                db.x("UPDATE rules SET contradictions=contradictions+? WHERE id=?", len(r["evidence"]), row["id"])
    from . import evals
    for c in out.get("eval_cases") or []:
        evals.add_case_from_signal(c["lane"], c["signal_id"], c["expected"])
    db.x(f"UPDATE signals SET mined=1 WHERE id IN ({','.join(str(s['id']) for s in sigs)})")
    db.x("UPDATE signals SET mined=1 WHERE kind='triage' AND mined=0")  # agreed verdicts carry no lesson
    log("learn: mined", signals=len(sigs), new=len(out["new"]), reinforce=len(out["reinforce"]),
        contradict=len(out["contradict"]))
    return {"signals": len(sigs), "new": len(out["new"])}


def _job_spent() -> bool:
    """The nightly learn job is near the reaper's budget for it (hot.JOB_MAX): its gate stops between cases, the rest
    stays candidate. Gating rule after rule until work hours outran the budget, and the reaper only killed the judge
    call in flight while the thread went on."""
    from . import hot
    started = db.kv_get("job_started:learn", 0) or 0
    return started > (db.kv_get("job_done:learn", 0) or 0) and hot.awake_since(started, now()) > hot.JOB_MAX["learn"] - 900


def lifecycle() -> dict:
    from . import core, evals
    L = CFG["learning"]
    changed = {"activated": [], "retired": []}
    bases = {}
    stop = lambda: core.work_hours() or _job_spent()
    for r in db.q("SELECT * FROM rules WHERE status='candidate'"):
        if len(json.loads(r["evidence"])) < L["activate_min_evidence"]:
            continue
        if stop():
            # the gate is a night job: a lifecycle the Mac slept through gated rules into the day, 60 triage calls each,
            # beside the cases on the same account (2026-10-08: 40 candidates, 06:00 to past 12:40, 437 triage calls
            # and 15 USD in work hours). What is left stays candidate for the next night.
            log("learn: rule gate stops for " + ("work hours" if core.work_hours() else "the learn job's budget"), rule=r["id"])
            break
        verdict = evals.gate_rule(dict(r), bases, stop=stop)
        if verdict["accept"] is None:
            continue  # nothing measured (calls failed): judged again next lifecycle
        if verdict["accept"]:
            db.x("UPDATE rules SET status='active', activated_at=?, last_used_at=? WHERE id=?", now(), now(), r["id"])
            changed["activated"].append(r["id"])
        else:
            db.x("UPDATE rules SET status='rejected' WHERE id=?", r["id"])
            db.add_signal("rule_rejected_by_evals", {"rule": r["text"], "detail": verdict})
    stale = now() - L["retire_unused_days"] * 86400
    for r in db.q("SELECT * FROM rules WHERE status='active'"):
        if r["contradictions"] >= 2 + len(json.loads(r["evidence"])) // 3 or (r["last_used_at"] or r["activated_at"] or 0) < stale:
            db.x("UPDATE rules SET status='retired' WHERE id=?", r["id"])
            changed["retired"].append(r["id"])
    compile_packs()
    return changed


def compile_packs() -> None:
    cap = CFG["learning"]["pack_max_chars"]
    for lane in LANES:
        rows = db.q("SELECT * FROM rules WHERE status='active' AND lane=? ORDER BY json_array_length(evidence) DESC", lane)
        lines, size = [], 0
        for r in rows:
            line = f"- {r['text']} (when: {r['trigger']})"
            if size + len(line) > cap:
                break
            lines.append(line)
            size += len(line)
        f = RULES / f"{lane}.md"
        text = "\n".join(lines) + ("\n" if lines else "")
        if (f.read_text() if f.exists() else "") != text:
            f.write_text(text)
    r = run(["git", "-C", str(DATA), "status", "--porcelain", "rules"])
    if os.environ.get("AP_SELFTEST"):
        return
    if r.stdout.strip():
        run(["git", "-C", str(DATA), "add", "rules"])
        run(["git", "-C", str(DATA), "commit", "-q", "-m", f"learn: rule packs {dt.date.today()}"])


BETTER = {"type": "object", "required": ["lessons"], "properties": {"lessons": {"type": "array", "items": {
    "type": "object", "required": ["cause", "rule"], "properties": {"cause": {"type": "string"}, "rule": {"type": "string"},
                                                                "lane": {"type": "string", "enum": ["claude-build", "plan", "claude-critique", "claude-self_verify"]}}}}}}


def learn_from_superseded() -> int:
    """A harness PR closed in favor of a teammate's PR for the same problem is the strongest signal there is: compare
    both diffs and turn every difference into a prevention rule."""
    from . import gh
    n = 0
    for r in db.q("SELECT id, data FROM items WHERE status='dropped' AND updated_at>? AND json_extract(data,'$.parts') LIKE '%pr_url%'",
                  now() - 3 * 86400):
        d = json.loads(r["data"] or "{}")
        if d.get("superseded_learned"):
            continue
        for p in d.get("parts", []):
            if not p.get("pr"):
                continue
            try:
                cs = gh.api(f"repos/{site.slug(p['repo'])}/issues/{p['pr']}/comments") or []
            except Exception:
                cs = []
            # the teammate's closing comment, in either language (input)
            m = next((x for x in (re.search(r"(?:en favor de|in favor of) #(\d+)", c.get("body") or "", re.I) for c in cs) if x), None)
            if not m:
                continue
            theirs = int(m.group(1))
            try:
                ours_diff = run(["gh", "pr", "diff", str(p["pr"]), "-R", site.slug(p["repo"])], timeout=120).stdout[:40000]
                their_diff = run(["gh", "pr", "diff", str(theirs), "-R", site.slug(p["repo"])], timeout=120).stdout[:40000]
            except Exception:
                continue
            out = muse.call("ap-judge", "Our assistant's pull request was closed because a teammate's PR for the same problem was better. "
                            "Compare them and write one prevention rule per concrete difference that made theirs better (mechanism, "
                            "coverage of all writers, edge cases, validation, tests, readability). Each rule imperative, specific, "
                            "reusable beyond this bug; pick the lane whose prompt should carry it.\n\n"
                            f"OURS ({p['repo']}#{p['pr']}):\n{ours_diff}\n\nTHEIRS ({p['repo']}#{theirs}):\n{their_diff}",
                            cwd=str(HOME), schema=BETTER, lane="learn", item_id=r["id"])
            kf = KNOWLEDGE / "lessons.md"
            for les in out.get("lessons", [])[:6]:
                lane = les.get("lane") or "claude-build"
                if not db.q1("SELECT 1 FROM rules WHERE text=?", les["rule"][:400]):
                    db.x("INSERT INTO rules(lane,text,trigger,status,evidence,created_at,activated_at) VALUES(?,?,?,?,?,?,?)",
                         lane, les["rule"][:400], f"superseded by {p['repo']}#{theirs}", "active", json.dumps([r["id"]]), now(), now())
                kf.write_text((kf.read_text() if kf.exists() else "") + f"- {db.fmt_time(now())[:10]} {p['repo']}#{p['pr']} "
                              + site.t(f"superseded by #{theirs}: {les['cause'][:200]} → rule: {les['rule'][:300]}\n",
                                       f"superado por #{theirs}: {les['cause'][:200]} → regla: {les['rule'][:300]}\n"))
                n += 1
        db.update_item(r["id"], data={"superseded_learned": True})
    if n:
        compile_packs()
    return n


def nightly() -> dict:
    res = {"corrections": harvest_session_corrections()}
    try:
        res["superseded"] = learn_from_superseded()
    except Exception as e:
        res["superseded"] = f"failed: {e}"[:160]
    while True:
        m = mine()
        res.setdefault("mined", []).append(m)
        if m.get("signals", 0) < 120:
            break
    res["lifecycle"] = lifecycle()
    try:  # recurring edits he makes to drafts -> deterministic lint rules + patterns for the style guide
        from . import sentlearn
        res["sent_vs_draft"] = sentlearn.daily()
    except Exception as e:
        res["sent_vs_draft"] = f"failed: {e}"[:200]
        log("sent-vs-draft daily failed", err=str(e)[:200])
    try:
        res["style"] = style_sync()
    except Exception as e:
        res["style"] = f"failed: {e}"[:200]
        log("style sync failed", err=str(e)[:200])
    db.kv_set("learn_last", {"at": now(), "res": res})
    log("learn: nightly done", **res)
    return res


STYLE_AUTO_START = "\n\n<!-- auto-learned: start -->\n"
STYLE_AUTO_END = "\n<!-- auto-learned: end -->\n"


def style_sync() -> dict:
    """v1's daily style sync: his own messages of the day + drafted-vs-sent pairs refine the
    auto-learned section of prompts/style/slack.md (Opus, read-only toward Slack)."""
    from . import claude, slack
    from .core import HOME
    cfg = CFG.get("style_sync") or {}
    if not cfg.get("enabled", True):
        return {"skipped": True}
    me = CFG["self_user_id"]
    day = time.strftime("%Y-%m-%d", time.localtime(now() - 86400))
    excluded = set(cfg.get("exclude_channels", [])) | {CFG["self_dm_channel"]}
    own = []
    try:
        for m in slack.search(f"from:<@{me}> after:{day}", count=100):
            ch = (m.get("channel") or {}).get("id")
            if ch in excluded or not (m.get("text") or "").strip():
                continue
            own.append({"channel": (m.get("channel") or {}).get("name") or ch, "text": slack.render(m["text"])[:800]})
    except Exception as e:
        log("style sync: search failed", err=str(e)[:160])
    pairs = [json.loads(r["data"]) for r in db.q("SELECT data FROM signals WHERE kind='style_pair' AND at>?", now() - 86400)]
    from . import sentlearn
    recurring = sentlearn.patterns_text()  # counted over weeks of pairs, not judged from one day
    if not own and not pairs and not recurring:
        return {"messages": 0, "pairs": 0}
    guide = STYLE / "slack.md"
    text = guide.read_text()
    old = text.split(STYLE_AUTO_START)[1].split(STYLE_AUTO_END)[0] if STYLE_AUTO_START in text else ""
    schema = {"type": "object", "required": ["section", "changelog"], "properties": {
        "section": {"type": "string"}, "changelog": {"type": "string"}}}
    out = claude.lean("style", f"His messages ({len(own)}):\n{json.dumps(own, ensure_ascii=False)[:14000]}\n\n"
                      f"Drafted vs sent pairs ({len(pairs)}):\n{json.dumps(pairs, ensure_ascii=False)[:8000]}\n\n"
                      + (f"Recurring edits he makes to drafts, counted deterministically over the last weeks of pairs (each "
                         f"line: audience, edit, how many of his sent messages show it). Keep each as a rule in the section, "
                         f"per audience:\n{recurring}\n\n" if recurring else "")
                      + f"Current auto-learned section:\n{old}", cwd=str(HOME), schema=schema, tools=())
    section = out["section"].strip()
    base = text.split(STYLE_AUTO_START)[0] if STYLE_AUTO_START in text else text.rstrip()
    guide.write_text(base + STYLE_AUTO_START + section + STYLE_AUTO_END)
    # committed by the hourly knowledge_commit job (never a hooked commit from a runtime path)
    log("style sync", messages=len(own), pairs=len(pairs), changelog=out["changelog"][:200])
    return {"messages": len(own), "pairs": len(pairs), "changelog": out["changelog"]}


POSTMORTEM = {"type": "object", "required": ["verdict", "why", "knowledge", "rule", "missing"], "properties": {
    "verdict": {"type": "string", "enum": ["theirs_better", "equal", "ours_better"]}, "why": {"type": "string"},
    "knowledge": {"type": "array", "items": {"type": "string"}}, "rule": {"type": ["string", "null"]},
    "missing": {"type": "array", "items": {"type": "string"}},
    "precedent": {"type": ["object", "null"], "properties": {"request": {"type": "string"}, "resolution": {"type": "string"},
                                                            "kind": {"type": "string"}},
                  "description": "when this was a customer/ops request: the request in one line, how it was resolved (procedure, who does it, tool/script), and a short kind label"}}}


def knowledge_text(limit: int = 40) -> str:
    """Squad knowledge learned from teammates' answers, injected into reply/investigate prompts."""
    kb = db.kv_get("knowledge", [])
    if not kb:
        return ""
    return "<squad_knowledge> (facts learned from teammates' answers; each with its source)\n" + "\n".join(
        f"- {k['fact']} [{k.get('source', '')}]" for k in kb[-limit:]) + "\n</squad_knowledge>"


LESSON = {"type": "object", "required": ["ours", "cause", "rule"], "properties": {
    "ours": {"type": "boolean", "description": "true when the defect the reviewer found was introduced by our build (not pre-existing)"},
    "cause": {"type": "string", "description": "why our build/verify produced or missed it, in one or two sentences"},
    "rule": {"type": "string", "description": "one-line prevention rule for the build or self-verify prompts, imperative, concrete"},
    "check": {"type": "string", "description": "a deterministic check the harness could run, if any; empty otherwise"}}}
from .core import KNOWLEDGE as KNOWLEDGE_DIR  # the install's own (the checkout itself for the first install)
# the user's Claude Code memory, so his own sessions learn the lesson too: only where the install says it has one
MEMORY_LESSONS = site.path("memory_lessons", str(Path.home() / ".claude/projects" / str(Path.home()).replace("/", "-")
                                               / "memory/autopilot-lessons.md"))


def lesson_from_review(it: dict, confirmed: list[dict], reviewer: str) -> dict | None:
    """the user: 'if the error was ours, not only fix it but find what caused it, and append
    the fix/check to memory so it never happens again.' Runs when a HUMAN reviewer's comment on a PR the
    harness built is confirmed valid. The rule goes to the rules table (build lane, active), to the
    knowledge file and to the memory dir Claude Code reads."""
    from .core import item_dir
    part = it["data"]["parts"][0]
    prompt = ("A teammate reviewed a pull request the assistant built and found a real problem. Decide whether the "
              "defect was OURS (introduced or overlooked by the build) and, if so, why the build and the self-verify "
              "missed it and which one-line rule would have prevented it.\n\n"
              f"PR: {part.get('pr_url')} (worktree {part.get('wt')})\nReviewer: {reviewer}\n"
              f"Confirmed problems: {json.dumps(confirmed, ensure_ascii=False)[:4000]}\n"
              f"Case file: {item_dir(it['id'])}")
    out = muse.call("ap-judge", prompt, cwd=part.get("wt") or site.root(), schema=LESSON, lane="judge", item_id=it["id"])
    if not out.get("ours"):
        db.add_signal("review_lesson", {"ours": False, "cause": out.get("cause")}, it["id"])
        return out
    rule = out["rule"].strip()[:400]
    if rule and not db.q1("SELECT 1 FROM rules WHERE text=?", rule):
        db.x("INSERT INTO rules(lane,text,trigger,status,evidence,created_at,activated_at) VALUES(?,?,?,?,?,?,?)",
             "claude-build", rule, f"review by {reviewer} on {part.get('pr_url')}", "active", json.dumps([it["id"]]), now(), now())
    line = (f"- {db.fmt_time(now())[:10]} {part.get('pr_url')} ({reviewer}): {out['cause'].strip()} → "
            + site.t(f"rule: {rule}", f"regla: {rule}") + (f" · check: {out['check'].strip()}" if out.get("check") else ""))
    try:
        kf = KNOWLEDGE_DIR / "lessons.md"
        kf.parent.mkdir(parents=True, exist_ok=True)
        kf.write_text((kf.read_text() if kf.exists() else site.t("# Lessons from reviews of the harness's PRs\n\n",
                                                                 "# Lecciones de reviews sobre PRs del harness\n\n")) + line + "\n")
    except Exception as e:
        log("lesson: knowledge write failed", err=str(e)[:100])
    try:
        if MEMORY_LESSONS and not MEMORY_LESSONS.exists():
            MEMORY_LESSONS.write_text("---\nname: autopilot-lessons\ndescription: Lessons the harness learned from human review comments on PRs it built (cause + prevention rule); appended automatically\nmetadata:\n  type: project\n---\n\nEach line: PR, reviewer, why our build missed it, the rule now active in the build prompts. See [[autopilot-v2-harness]].\n\n")
            idx = MEMORY_LESSONS.parent / "MEMORY.md"
            if idx.exists() and "autopilot-lessons.md" not in idx.read_text():
                idx.write_text(idx.read_text().rstrip("\n") + "\n- [Autopilot lessons from reviews](autopilot-lessons.md) — causes and prevention rules from human comments on harness PRs, appended automatically\n")
        if MEMORY_LESSONS:
            MEMORY_LESSONS.write_text(MEMORY_LESSONS.read_text() + line + "\n")
    except Exception as e:
        log("lesson: memory write failed", err=str(e)[:100])
    db.add_signal("review_lesson", {"ours": True, "cause": out["cause"], "rule": rule}, it["id"])
    log("review lesson recorded", item=it["id"], rule=rule[:80])
    return out


def compare_answer(it: dict, our_draft: str, their_msgs: list[dict], thread_text: str) -> dict | None:
    """A teammate answered where we had a draft: judge whose was better and keep what we lacked
."""
    from . import claude, slack
    from .core import HOME
    theirs = "\n".join(f"[{db.fmt_time(m['ts'])}] {slack.user_name(m.get('user'))}: {slack.render(m.get('text', ''))[:1500]}" for m in their_msgs)
    out = claude.lean("postmortem", f"Thread:\n<thread>\n{thread_text[:8000]}\n</thread>\n\nOur draft (never sent):\n<ours>\n{our_draft}\n</ours>\n\n"
                      f"Their answer(s):\n<theirs>\n{theirs}\n</theirs>", cwd=str(HOME), schema=POSTMORTEM, tools=(), item_id=it["id"])
    db.add_signal("postmortem", {"verdict": out["verdict"], "why": out["why"][:400], "missing": out["missing"],
                                 "ours": our_draft[:800], "theirs": theirs[:1200], "rule": out.get("rule")}, it["id"])
    kb = db.kv_get("knowledge", [])
    have = {k["fact"] for k in kb}
    src = f"{slack.user_name(their_msgs[-1].get('user'))}, <#{it['data'].get('channel')}> {db.fmt_time(their_msgs[-1]['ts'])}"
    for fact in out.get("knowledge") or []:
        if fact and fact not in have:
            kb.append({"fact": fact[:400], "source": src, "at": now(), "item": it["id"]})
    db.kv_set("knowledge", kb[-300:])
    if out.get("rule") and out["verdict"] == "theirs_better":
        sig = db.q1("SELECT id FROM signals WHERE kind='postmortem' AND item_id=? ORDER BY id DESC", it["id"])
        db.x("INSERT INTO rules(lane,text,trigger,status,evidence,created_at) VALUES(?,?,?,?,?,?)",
             "claude-reply", out["rule"][:400], f"case #{it['id']}: {it['title'][:120]}", "candidate",
             json.dumps([sig["id"]] if sig else []), now())
    log("postmortem", item=it["id"], verdict=out["verdict"], knowledge=len(out.get("knowledge") or []), rule=bool(out.get("rule")))
    return out
