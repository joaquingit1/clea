"""The simulation harness: one scratch install whose Slack and GitHub are sim.world, run step by step.

Nothing is mocked inside the engine. The real intake (ingest), triage, case pipeline (worker.run_stage and every
stage behind it), approvals and the guard's real policy code all run; only their edges are swapped:
  - Slack reads   -> World.api (through the engine's own slack._call, so its retry and paging logic runs too)
  - guard writes  -> the real guard module's check and act functions, with its HTTP calls pointed at the World
  - GitHub reads  -> World.prs
  - the models    -> a scripted model (deterministic, zero tokens: for intake, routing and state-machine behavior)
                     or the install's real provider (live: what the user would actually get)
  - the clock     -> real time plus an offset the scenario can move (debounces, quiet hours, windows that close)

The install lives in a temp home: its own config (a made-up company, 'acme'), database and worktrees. It can never
reach the user's real Slack, guard or database: the engine's Slack client and guard call are replaced before anything
runs, and the home is not the user's.

Import this module before anything from `ap`: it chooses the install's home first.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("CLEA_SIM_HOME") or tempfile.mkdtemp(prefix="clea-sim-"))
ME, DM = "U0SIMSELF01", "D0SIMSELF01"
TEAM_CH, PR_CH, ALERTS_CH, OPS_CH, RANDOM_CH = "C0SIMTEAM01", "C0SIMPRS001", "C0SIMALERT1", "C0SIMOPS001", "C0SIMRAND01"
GROUP = "S0SIMGROUP1"
ANA, BO, CARLA, DEV, MONITOR = "U0SIMANA001", "U0SIMBO0001", "U0SIMCARLA1", "U0SIMDEV001", "U0SIMBOT001"
ANA_DM, CARLA_DM = "D0SIMANA001", "D0SIMCARLA1"


def base_config() -> dict:
    """The install the onboarding would have written for Sam Rivera at acme: a team channel, a PR channel, an alerts
    channel and one watched ops channel; a user group; two repos; 'ask' autonomy."""
    return {
        "edition": "release", "setup": {"complete": True, "done": ["welcome", "model", "slack", "code", "team", "autonomy"]},
        "language": "en", "user": {"name": "Sam Rivera"},
        # which engine a live run thinks with: CLEA_SIM_ENGINE=codex runs it on the Codex CLI's own sign-in
        "provider": {"engine": os.environ.get("CLEA_SIM_ENGINE") or "claude", "auth": "subscription"},
        "mode": "act", "autonomy": "ask",
        "self_user_id": ME, "self_dm_channel": DM, "self_github": "sam-rivera", "github_org": "acme",
        "team_channel": TEAM_CH, "pr_channel": PR_CH, "alerts_channel": ALERTS_CH, "watched_channels": [OPS_CH],
        "audiences": {"team_channels": [TEAM_CH], "ops_channels": [OPS_CH]},
        "channel_names": {TEAM_CH: "#team-checkout", PR_CH: "#checkout-prs", ALERTS_CH: "#checkout-alerts", OPS_CH: "#support-ops",
                          RANDOM_CH: "#random"},
        "team_subteam_tags": [GROUP],
        "team_members": {ANA: {"name": "Ana Li", "github": "ana-li"}, BO: {"name": "Bo Chen", "github": "bo-chen"}},
        "github_team_handles": ["ana-li", "bo-chen"],
        "slack": {"workspace_url": "https://acme.slack.com", "workspace": "Acme", "team_id": "T0SIM", "keywords": ["checkout"],
                  "scan_dms": True, "mentions": True, "socket_mode": False, "usergroups": [{"id": GROUP, "handle": "checkout-team"}]},
        "repos": {"web": {"path": str(HOME / "repos" / "web"), "default": "main", "layer": "backend"},
                  "api": {"path": str(HOME / "repos" / "api"), "default": "main", "layer": "backend"}},
        "primary_repo": "web", "domain_fence": "The checkout and billing flows of the web app and its API.",
        "fence": {"keywords": ["checkout", "billing"]}, "tickets": {"prefixes": ["ENG"]},
        "features": {"unanswered_sweep": True, "team_messages": False},
    }


# The 'web' repo: a small real project with one real bug (the discount is truncated where its own docstring says
# rounded half up), for the scenarios that have to end in a pull request. Its tests pass as they are: none covers the bug.
SEED = {
    "README.md": "# web\n\nThe checkout service. Run the tests with `python3 -m unittest -q`.\n",
    "checkout/__init__.py": "",
    "checkout/coupons.py": '''"""Coupon math for the checkout. Amounts are integer cents."""


def discount(total_cents: int, percent: int) -> int:
    """The discount a percentage coupon takes off a total, in cents, rounded half up."""
    return int(total_cents * percent / 100)


def apply_coupon(total_cents: int, percent: int) -> int:
    """What the customer pays after the coupon."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return total_cents - discount(total_cents, percent)
''',
    "tests/__init__.py": "",
    "tests/test_coupons.py": '''import unittest

from checkout.coupons import apply_coupon, discount


class CouponTests(unittest.TestCase):
    def test_whole_cents(self):
        self.assertEqual(discount(1000, 15), 150)
        self.assertEqual(apply_coupon(1000, 15), 850)

    def test_rejects_a_percent_out_of_range(self):
        with self.assertRaises(ValueError):
            apply_coupon(1000, 120)


if __name__ == "__main__":
    unittest.main()
''',
}


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-c", "user.email=sim@example.com", "-c", "user.name=sim", *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout


def origin_of(name: str) -> Path:
    return HOME / "remotes" / f"{name}.git"


def seed_repo(name: str, path: Path) -> None:
    """The repo as a clone with an origin of its own (a bare repo beside it): what the engine fetches from and pushes
    to. Anything a previous scenario left in either is gone."""
    origin, work = origin_of(name), HOME / "remotes" / f"{name}-seed"
    for d in (path, origin, work):
        shutil.rmtree(d, ignore_errors=True)
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    for rel, text in (SEED if name == "web" else {"README.md": f"# {name}\n"}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(HOME, "clone", "-q", "--bare", str(work), str(origin))
    _git(HOME, "clone", "-q", str(origin), str(path))
    shutil.rmtree(work)


def _write_home(config: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "config.json").write_text(json.dumps(config, indent=2))
    for name in config.get("repos") or {}:
        repo = Path(config["repos"][name]["path"])
        if not (repo / ".git").exists() or not origin_of(name).exists():
            seed_repo(name, repo)


_write_home(base_config())
os.environ.update(CLEA_HOME=str(HOME), CLEA_SECRETS="file", CLEA_GUARD_SOCK=str(HOME / "var" / "no-guard.sock"),
                  GH_CONFIG_DIR=str(HOME / "gh"), GH_TOKEN="sim-not-a-token")  # a stray `gh` call can reach nothing real
sys.path.insert(0, str(ROOT))

from ap import core, db  # noqa: E402
from sim.world import World  # noqa: E402

assert core.DATA == HOME and core.CFG["edition"] == "release", "the simulation must run on its own install"
_real_time = time.time


class _Client:
    """slack_sdk.WebClient's shape, as far as the engine uses it: client.<method>(**kw).data"""
    token = "xoxp-sim"

    def __init__(self, world: World):
        self._w = world

    def __getattr__(self, method):
        def call(**kw):
            return type("R", (), {"data": self._w.api(method, **kw)})()
        return call


class Sim:
    def __init__(self, live: bool = False):
        from ap import approvals, claude, gh, guardcall, impact, ingest, muse, slack, triage, worker  # noqa: F401
        self.live = live
        self.world = World(workspace="acme", me=ME)
        self.model = Script(self)
        self.offset = 0.0
        self._wire()
        self.reset()

    # ---- wiring ---------------------------------------------------------------------------------------------------
    def _wire(self) -> None:
        from ap import claude, gh, guardcall, impact, muse, slack
        time.time = lambda: _real_time() + self.offset
        slack.client = lambda: _Client(self.world)
        slack.token = lambda: "xoxp-sim"
        slack.RATE, slack.CACHED = {}, ()        # no pacing, no read cache: a scenario posts and reads within milliseconds
        slack.download_files = lambda *a, **k: []
        impact.SETTLE_S = 0.05
        self.guard = self._load_guard()
        guardcall.act = self._guard_act
        for name in ("pr_list", "pr_search"):
            setattr(gh, name, lambda repo, search="", *a, **k: self._prs(repo, search))
        gh.pr_view = lambda repo, n: self._pr(repo, n)
        gh.pr_diff = lambda repo, n: self._pr(repo, n).get("diff", "")
        gh.reviews = lambda repo, n: list(self._pr(repo, n).get("reviews", []))
        gh.review_comments = gh.issue_comments = lambda repo, n: []
        gh.review_threads = lambda repo, n: []
        gh.unresolved_threads = lambda repo, n, me: []
        gh.failed_runs = lambda repo, sha: []
        gh.required_checks = lambda repo: []
        gh.deployed_shas = lambda repo, envname: []
        gh.api = lambda path, *a, **k: (_ for _ in ()).throw(RuntimeError(f"the simulation has no GitHub API route {path}"))
        if not self.live:
            claude.lean = self.model.lean
            claude.build = self.model.build
            claude.orchestrator = self.model.orchestrator
            muse.call = self.model.muse
            claude.probe_limited = lambda: []

            def _never(*a, **k):
                raise AssertionError("a scripted simulation reached a real model call")
            claude._invoke = claude._codex = _never

    def _load_guard(self):
        """The guard's real module, loaded in this process with its Slack and GitHub calls pointed at the world and its
        Touch ID dialog answered by the scenario. What it allows, refuses and sends is the real policy code's doing."""
        from ap import setup
        setup.write_policy()
        spec = importlib.util.spec_from_file_location("clea_guard", ROOT / "guard" / "ap_guard.py")
        g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(g)
        g.slack = self.world.guard_slack
        g.gh = self.world.guard_gh
        g._local_approved = lambda action, p: None if self.world.touch_id == "approve" else "local approval declined"
        g.log = lambda entry: self.guard_log.append(entry)
        self.guard_log: list = []
        return g

    def _guard_act(self, action: str, **payload) -> dict:
        from ap.guardcall import GuardDenied
        resp = self.guard.handle({"action": action, "payload": payload})
        if not resp.get("ok"):
            err = resp.get("error", "")
            raise (GuardDenied if err.startswith("guard:") else RuntimeError)(err)
        return resp.get("result") or {}

    def _pr(self, repo: str, n: int) -> dict:
        pr = self.world.prs.get((repo, int(n)))
        if pr is None:
            raise RuntimeError(f"gh pr view {repo}#{n}: not found")
        return pr

    def _prs(self, repo: str, search: str) -> list[dict]:
        rows = [p for (r, _), p in self.world.prs.items() if r == repo]
        if "is:open" in search:
            rows = [p for p in rows if p["state"] == "OPEN"]
        m = re.search(r"author:([\w-]+)", search)
        if m:
            rows = [p for p in rows if p["author"]["login"] == m.group(1)]
        m = re.search(r"review-requested:([\w-]+)", search)
        if m:
            rows = [p for p in rows if any(r.get("login") == m.group(1) for r in p.get("reviewRequests") or [])]
        return rows

    # ---- one scenario's clean slate ---------------------------------------------------------------------------------
    def reset(self, config: dict | None = None) -> "Sim":
        """A fresh workspace and an empty database; `config` is merged over the base install for this scenario."""
        from ap import approvals, impact, slack
        for t in ("events", "items", "transitions", "artifacts", "signals", "calls", "budget", "kv", "prs", "merge_deps", "branches",
                  "console_msgs", "rules"):
            db.x(f"DELETE FROM {t}")
        cfg = base_config()
        if config:
            cfg = core._deep(cfg, config)
        (HOME / "config.json").write_text(json.dumps(cfg, indent=2))
        core.reload_config()
        from ap import setup
        setup.write_policy()
        self.guard._reload_policy() if hasattr(self.guard, "_reload_policy") else None
        self.guard.POLICY = json.loads((HOME / "guard" / "policy.json").read_text())
        self.offset = 0.0
        w = self.world = World(workspace="acme", me=ME, clock=_real_time())
        w._ts = self._ts
        self.guard.slack, self.guard.gh = w.guard_slack, w.guard_gh
        self.guard_log.clear()
        self.model.reset()
        for fn in (slack.user_name, slack.channel_name, slack.user_is_bot):
            fn.cache_clear()
        impact._pending.clear()
        approvals._FIND_AT.clear()
        approvals._CHECKED_AT.clear()
        w.user(ME, "sam", "Sam Rivera")
        w.users[ME]["github"] = "sam-rivera"
        w.user(ANA, "ana", "Ana Li")
        w.user(BO, "bo", "Bo Chen")
        w.user(CARLA, "carla", "Carla Ruiz")      # support: outside the team
        w.user(DEV, "dev", "Dev Patel")           # another team's engineer
        w.user(MONITOR, "monitor", "Monitor", bot=True)
        w.channel(TEAM_CH, "team-checkout", members=(ANA, BO))
        w.channel(PR_CH, "checkout-prs", members=(ANA, BO, DEV))
        w.channel(ALERTS_CH, "checkout-alerts", members=(ANA, BO))
        w.channel(OPS_CH, "support-ops", members=(ANA, CARLA, DEV))
        w.channel(RANDOM_CH, "random", members=(ANA, BO, CARLA, DEV))  # the user is in it; Clea was not told to read it
        w.dm(DM, ME)
        w.dm(ANA_DM, ANA)
        w.dm(CARLA_DM, CARLA)
        w.group(GROUP, "checkout-team", users=(ME, ANA, BO))
        # intake cursors start now: nothing before the scenario exists
        return self

    def _ts(self) -> str:
        w = self.world
        w.clock = max(w.clock + 0.000100, time.time())
        return f"{w.clock:.6f}"

    def wait(self, seconds: float) -> None:
        """Time passes: the engine's clock and the workspace's move together."""
        self.offset += seconds
        self.world.clock = max(self.world.clock, time.time())

    # ---- running the engine -----------------------------------------------------------------------------------------
    def ingest(self) -> None:
        """One pass of everything the scheduler's intake jobs do, in their order."""
        from ap import impact, ingest, triage
        ingest.poll_channels()
        ingest.poll_threads()
        ingest.search_mentions()
        deadline = _real_time() + 20
        while any(impact._pending.values()) and _real_time() < deadline:
            time.sleep(0.05)  # the thread-news judge runs beside the intake; a pass is over when it is
        if db.q1("SELECT 1 FROM events WHERE triaged=0 LIMIT 1"):
            triage.run_batch()

    def step(self, horizon_s: float = 900) -> dict | None:
        """Run the next due stage of any case, moving the clock forward to it when it is scheduled within `horizon_s`
        (a debounce, a short wait). Returns the case as it was claimed, or None when nothing is due."""
        from ap import worker
        r = db.q1(f"SELECT min(next_run_at) t FROM items WHERE status IN ({','.join('?' * len(worker.STAGES))}) AND claimed_by IS NULL",
                  *worker.STAGES)
        if r is None or r["t"] is None:
            return None
        due = r["t"] - core.now()
        if due > horizon_s:
            return None
        if due > 0:
            self.wait(due + 1)
        it = db.claim_next(list(worker.STAGES), "sim")
        if it is None:
            return None
        worker.run_stage(it)
        return it

    def run(self, max_steps: int = 40, horizon_s: float = 900) -> int:
        """Ingest, then run stages until nothing is due within the horizon. Returns how many stages ran."""
        self.ingest()
        n = 0
        while n < max_steps and self.step(horizon_s):
            n += 1
        return n

    def fresh_repos(self) -> None:
        """The repos as they were seeded, with no worktree, branch or mirror left from an earlier scenario."""
        for d in ("worktrees", "mirror"):
            shutil.rmtree(HOME / d, ignore_errors=True)
        for name, r in (core.CFG.get("repos") or {}).items():
            seed_repo(name, Path(r["path"]))

    # ---- the user ---------------------------------------------------------------------------------------------------
    def asks(self, case: int | None = None) -> list[dict]:
        rows = db.q("SELECT * FROM artifacts WHERE kind='approval' AND outcome IS NULL" + (" AND item_id=?" if case else ""),
                    *([case] if case else []))
        return [{"id": r["id"], "case": r["item_id"], "ts": r["ref"], **json.loads(r["content"] or "{}")} for r in rows]

    def approve(self, ask_id: int, touch_id: str = "approve") -> str:
        """The user approves an ask in the app: the guard opens its Touch ID dialog, which the scenario answers."""
        from ap import approvals
        self.world.touch_id = touch_id
        return approvals.decide_local(ask_id, "approve")

    def decide(self, ask_id: int, decision: str, text: str = "") -> str:
        from ap import approvals
        return approvals.decide_local(ask_id, decision, text)

    def poll_approvals(self) -> int:
        from ap import approvals
        return approvals.poll()

    # ---- looking at what happened -----------------------------------------------------------------------------------
    def cases(self, kind: str | None = None) -> list[dict]:
        rows = db.q("SELECT id FROM items" + (" WHERE kind=?" if kind else "") + " ORDER BY id", *([kind] if kind else []))
        return [db.item(r["id"]) for r in rows]

    def case(self, item_id: int) -> dict:
        return db.item(item_id)

    def notes(self, item_id: int) -> list[str]:
        return [f"{r['src'] or '-'}>{r['dst']}: {r['note'] or ''}" for r in db.q("SELECT * FROM transitions WHERE item_id=? ORDER BY rowid", item_id)]

    def model_calls(self, role: str | None = None) -> list[tuple]:
        if self.live:
            rows = db.q("SELECT lane FROM calls WHERE tier='claude'")
            return [(r["lane"], "") for r in rows if role is None or r["lane"].split(":")[0] == role]
        return [c for c in self.model.calls if role is None or c[0] == role]

    def sent_to_others(self) -> list[dict]:
        """Everything that left in the user's name to anywhere but their own DM."""
        return [o for o in self.world.outbox if o["kind"] != "slack_post" or o["channel"] != DM
                if o["kind"] not in ("slack_edit", "slack_delete", "pins.add", "pins.remove")]


# ---- what must hold after every scenario, whatever it was about ------------------------------------------------------------------
# Words an English text does not contain. The simulated workspace and the scripted model write English, so a hit in
# anything Clea wrote is a string the engine's own code produced in the language it was first written for.
SPANISH = re.compile(r"[áéíóúñ¿¡]|\b(?:que|para|del|los|las|una|pero|cuando|hay|hilo|mando|mandar|mensaje|respuesta|borrador|"
                     r"listo|texto|sigue|pregunta|esperando|promesa|novedad|detalle|revisar|caso|hecho|pendiente|ya|"
                     r"redactaba|nuevo|nueva|sin respuesta|lo rehago|en el|de la|por el|te dej[eé]|aprobado|descartado|"
                     r"cerrado|enviado|respondido|verificando|falta|listo para|con eso|a las)\b", re.I)


def invariants(s: "Sim") -> list[str]:
    """Failures no scenario should have to ask about: the install's language, one case per conversation, and nothing
    in the user's name that the user did not approve."""
    bad: list[str] = []
    if core.CFG.get("language", "en") == "en":
        wrote = [("to the user's DM" if o.get("channel") == DM else "to others", o.get("text") or "")
                 for o in s.world.outbox if o["kind"] in ("slack_post", "slack_edit")]
        wrote += [(f"title of case #{c['id']}", c["title"]) for c in s.cases()]
        wrote += [(f"history of case #{c['id']}", n) for c in s.cases() for n in s.notes(c["id"])]
        seen = set()
        for where, text in wrote:
            for line in text.splitlines():
                if line.lstrip().startswith(">"):
                    continue  # a quoted draft or message: a person's or the model's words
                hit = SPANISH.search(line)
                if hit and line not in seen:
                    seen.add(line)
                    bad.append(f"Spanish in an English install ({where}): {line.strip()[:140]!r}")
    for r in db.q(f"SELECT thread_key, count(*) n, group_concat(id) ids FROM items WHERE status NOT IN {db.TERMINAL} "
                  "AND thread_key IS NOT NULL AND thread_key != '' GROUP BY thread_key HAVING n > 1"):
        bad.append(f"{r['n']} open cases on one conversation ({r['thread_key']}): #{r['ids']}")
    if core.CFG.get("autonomy") != "auto":
        ok = {(json.loads(r["content"] or "{}").get("payload") or {}).get("channel")
              for r in db.q("SELECT content FROM artifacts WHERE kind='approval' AND outcome IN ('approved', 'approve', 'sent', 'done')")}
        for o in s.sent_to_others():
            if o["kind"] == "slack_post" and o.get("channel") not in ok:
                bad.append(f"sent to {o.get('channel')} with no approved ask: {(o.get('text') or '')[:100]!r}")
    return bad


QUESTION = re.compile(r"\?|\b(can you|could you|please|would you|do you know|any idea|help|need you|pod[eé]s|necesito|ayud)", re.I)
BUG = re.compile(r"\b(bug|error|broken|fails?|failing|crash|500|exception|not working|se rompi[oó]|falla)\b", re.I)


class Script:
    """The scripted model: what a model would have answered, decided by simple rules a scenario can replace per role.
    It exists to drive the real pipeline deterministically. It judges nothing well; live mode is for that."""

    def __init__(self, sim: Sim):
        self.sim = sim
        self.reset()

    def reset(self) -> None:
        self.calls: list[tuple] = []     # (role, prompt)
        self.rules: dict = {}            # role -> fn(prompt, schema) -> answer

    def on(self, role: str, fn) -> None:
        self.rules[role] = fn

    @staticmethod
    def fake(schema: dict, key: str = ""):
        t = schema.get("type")
        t = t[0] if isinstance(t, list) else t
        if "enum" in schema:
            return schema["enum"][0]
        if t == "object":
            return {k: Script.fake(s, k) for k, s in schema.get("properties", {}).items()}
        if t == "array":
            return []
        return {"integer": 1, "number": 0.9, "boolean": False, "null": None}.get(t, f"sim {key}")

    LINE = re.compile(r"^\[\d\d/\d\d \d\d:\d\d\] ([^:]+): (.*)$")

    def _asked(self, prompt: str) -> str:
        """What the last person to speak said since the user last spoke (their trailing messages, a burst counts as
        one), from the conversation a stage rendered into its prompt ('[dd/mm hh:mm] Name: text' lines). A careless
        reader's rule: whoever spoke last is the one waiting, and an earlier question someone else answered is not."""
        me = {"sam", "sam rivera", "joaquin"}
        said: list[tuple[str, str]] = []
        for line in prompt.splitlines():
            m = self.LINE.match(line.strip())
            if not m:
                continue
            who = m.group(1).strip().lower()
            said = [] if who in me else said + [(who, m.group(2))]
        if not said:
            return "" if self.LINE.search(prompt.replace("\n", "\n")) and any(self.LINE.match(l.strip()) for l in prompt.splitlines()) else prompt[-600:]
        last = said[-1][0]
        tail = []
        for who, text in reversed(said):
            if who != last:
                break
            tail.append(text)
        return "\n".join(reversed(tail))

    def lean(self, role: str, prompt: str, *, schema: dict | None = None, **k):
        self.calls.append((role, prompt))
        n = len(self.calls)  # no two drafts are byte-identical, as no two real ones are
        if role in self.rules:
            return self.rules[role](prompt, schema)
        if role == "reply":
            ask = bool(QUESTION.search(self._asked(prompt)))
            return {**self.fake(schema), "needs_reply": ask, "why": "they ask him something" if ask else "nothing is asked of him",
                    "draft": f"Sure, on it. I will confirm by end of day. (draft {n})" if ask else "", "needs_facts": False, "checks": [],
                    "prod_problem": False, "someone_else_owns": False, "target": "thread", "report": f"Someone asks for an answer. (report {n})",
                    "ops_request": False, "school": "", "open_problem": "", "precedent_terms": [], "board_line": "Reply to a teammate"}
        if role == "followup":
            ask = bool(QUESTION.search(self._asked(prompt)))
            return {"needs_him": ask, "why": "a new question" if ask else "only thanks or chatter"}
        if role == "report":
            return {"headline": "Sim case", "bullets": ["what was found"], "need": "your approval to send the draft",
                    "analysis": "A simulated analysis.", "text": f"Simulated report {n}.", "board_line": "Sim case", "detail": ""}
        if role in ("impact",):
            return {"impact": "none", "why": "chatter"}
        if role == "sweep":
            return {"ours": True, "why": "a question in the user's area nobody answered"}
        if role in ("dupcase",):
            return {"same_as": 0, "why": "different"}
        if role == "agent-ap-triage":
            return self.triage(prompt, schema)
        return self.fake(schema) if schema else {"text": "ok"}

    def triage(self, prompt: str, schema: dict | None):
        out = []
        for eid in dict.fromkeys(int(x) for x in re.findall(r"event_id=(\d+)", prompt)):
            ev = db.q1("SELECT text FROM events WHERE id=?", eid)
            text = (ev["text"] if ev else "") or ""
            cls = "TASK" if BUG.search(text) else "REPLY" if QUESTION.search(text) else "IGNORE"
            out.append({"event_id": eid, "class": cls, "confidence": 0.9, "needs_joaquin": cls != "IGNORE", "urgency": 2,
                        "title": text[:70] or "event", "summary": text[:200], "repo_hint": None, "pr_refs": [],
                        "duplicate_of": None, "duplicate_of_event": None, "reason": "sim rule", "demand_key": None})
        return {"results": out}

    def muse(self, agent: str, prompt: str, *, schema: dict | None = None, lane: str | None = None, **k):
        self.calls.append((f"agent-{agent}" if not agent.startswith("agent-") else agent, prompt))
        role = f"agent-{agent}"
        if role in self.rules:
            return self.rules[role](prompt, schema)
        if agent == "ap-triage" and (lane or "triage") == "triage":
            return self.triage(prompt, schema)
        return self.fake(schema) if schema else "sim line"

    def build(self, role: str, prompt: str, **k):
        self.calls.append((role, prompt))
        return {"text": "sim build", "usd": 0.0, "session": None, "capped": False}

    def orchestrator(self, prompt: str, **k):
        self.calls.append(("chat", prompt))
        return {**self.fake(k.get("schema") or {}), "_session": None}
