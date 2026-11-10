"""Run the simulation: python -m sim [-k substring] [--live] [--serve [PORT]] [--report FILE] [-v]

Every scenario runs the real engine on a scratch install against a simulated Slack and GitHub (see sim/harness.py).
Prints one line per scenario and writes a Markdown report of what was posted, what Clea did, and what left in the
user's name. Exit code 0 only when every selected scenario passes.

--live runs on the install's real model provider instead of the scripted model (it spends tokens); only scenarios
whose outcome does not depend on a scripted answer are selected.

--serve opens the app on the simulated workspace, on a port of its own: the board shows each case moving while the
scenarios play, and stays up on what the last one left until Ctrl-C. `python -m sim -k demo --serve` is the app with
no account connected.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from sim.harness import DM, HOME, ME, Sim, invariants  # first: it points the engine at the scratch install
from sim import scenarios as _sc
from ap import db  # noqa: E402  (only after the harness)


def describe(s: Sim) -> str:
    """What happened, as a person would read it."""
    w = s.world
    name = lambda u: (w.users.get(u) or {}).get("real_name", u or "bot")
    chan = lambda c: ("DM with " + name(w.channels[c].get("user")) if w.channels[c]["kind"] == "im" else "#" + w.channels[c]["name"])
    out = []
    for cid, msgs in w.messages.items():
        if cid == DM or not msgs:
            continue
        out.append(f"**{chan(cid)}**")
        for m in msgs:
            who = name(m.get("user")) if m.get("user") else "bot"
            via = "" if m.get("client_msg_id") or m.get("bot_id") else " _(sent by Clea, approved)_" if m.get("user") == ME else ""
            out.append(f"- {'  ↳ ' if m.get('thread_ts') and m['thread_ts'] != m['ts'] else ''}{who}: {m['text'][:160]}{via}")
    out.append("**What Clea did**")
    for c in s.cases():
        out.append(f"- case #{c['id']} {c['kind']} `{c['status']}`: {c['title'][:80]}")
        for n in s.notes(c["id"])[1:]:
            out.append(f"  - {n[:160]}")
    if not s.cases():
        out.append("- nothing: no case")
    for a in s.asks():
        p = a["payload"]
        if p.get("channel") in w.channels:
            out.append(f"- waiting for approval: send in {chan(p['channel'])}: “{(p.get('text') or '')[:200]}”")
        else:  # a review or a merge: it names a pull request, not a channel
            notes = "".join(f" [{c.get('path')}:{c.get('line')}] {c.get('body', '')[:160]}" for c in p.get("comments") or [])
            out.append(f"- waiting for approval: {a['kind']} on {p.get('repo')}#{p.get('pr')}: “{(p.get('body') or p.get('text') or '')[:200]}”{notes}")
    calls = {}
    for r, _ in s.model_calls():
        calls[r] = calls.get(r, 0) + 1
    out.append("- model calls: " + (", ".join(f"{k} x{v}" for k, v in sorted(calls.items())) or "none"))
    sent = s.sent_to_others()
    out.append("- left in the user's name: " + ("; ".join(f"{o['kind']} in {chan(o['channel']) if o.get('channel') in w.channels else o.get('repo')}" for o in sent) or "nothing"))
    return "\n".join(out)


def _to_wall_clock(sim: Sim) -> None:
    """A scenario moves the clock forward instead of waiting, so what it left is stamped ahead of the real time the
    browser reads ("updated in 16 min"). Bring every stamp back by what the clock was moved, and the clock with them."""
    ahead = int(sim.offset)
    if ahead <= 0:
        return
    for (table,) in db.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for col in db.q(f"PRAGMA table_info({table})"):
            name = col["name"]
            if name == "at" or name == "ts" or name.endswith("_at") or name.endswith("_ts"):
                db.x(f"UPDATE {table} SET {name} = {name} - ? WHERE typeof({name}) IN ('integer', 'real') AND {name} > 1500000000", ahead)
    sim.offset = 0.0


def open_app(port: int):
    """The app on the simulated workspace, from the start of the run: the board shows each case moving while the
    scenarios play, and stays up afterwards. Approving a draft there sends it to the simulated Slack: nothing real is
    reachable from this process. None when the app is not built."""
    import http.server
    import threading
    from ap import console
    if not (console.DIST / "index.html").exists():
        print("The app is not built yet: run `make app` (or `cd ui && pnpm install && pnpm build`) and try again.")
        return None
    threading.Thread(target=console._refresh_loop, name="console-refresh", daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), console._H)
    threading.Thread(target=srv.serve_forever, name="console-http", daemon=True).start()
    print(f"The app is open on the simulated workspace: http://127.0.0.1:{port}")
    return srv


def keep_open(sim: Sim, port: int) -> None:
    """The run is over: leave the app up on what the last scenario left, on the real clock, until Ctrl-C."""
    import threading
    _to_wall_clock(sim)
    print(f"\nStill open: http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m sim")
    ap.add_argument("-k", default="", help="only scenarios whose name or group contains this")
    ap.add_argument("--live", action="store_true", help="use the install's real model provider (spends tokens)")
    ap.add_argument("--serve", nargs="?", type=int, const=4850, default=0, metavar="PORT",
                    help="open the app on the simulated workspace while the scenarios play, and keep it up after (default port 4850)")
    ap.add_argument("--report", default=str(HOME / "report.md"))
    ap.add_argument("-v", action="store_true", help="print each scenario's story")
    args = ap.parse_args()
    sim = Sim(live=args.live)
    app = open_app(args.serve) if args.serve else None
    # a scenario marked live=True also holds on a real model; live="only" needs one (it is about what the model does)
    picked = [sc for sc in _sc.SCENARIOS if (args.k in sc["name"] or args.k == sc["group"])
              and (sc["live"] if args.live else sc["live"] != "only")]
    report, failed = [f"# Clea simulation ({'live' if args.live else 'scripted'}), {time.strftime('%Y-%m-%d %H:%M')}", ""], 0
    for sc in picked:
        sim.reset(sc["config"])
        if app:
            db.kv_set("hot_reload", {"rev": "sim"})  # what the app shows where an install shows the commit it runs
        ok = _sc.Checks()
        t0, err = time.perf_counter(), ""
        try:
            sc["fn"](sim, ok)
        except Exception:
            err = traceback.format_exc()
            ok.failed.append("the scenario crashed: " + err.strip().splitlines()[-1])
        try:
            ok.failed += [] if args.live else invariants(sim)  # a live model writes what it likes; the scripted run is exact
        except Exception as e:
            ok.failed.append(f"the invariants could not be checked: {type(e).__name__}: {e}")
        good = not ok.failed
        failed += not good
        print(f"{'PASS' if good else 'FAIL'}  {sc['group']:<10} {sc['name']:<34} {ok.passed} checks, {time.perf_counter() - t0:.1f}s")
        for f in ok.failed:
            print(f"        - {f[:400]}")
        if args.v or not good:
            if err:
                print("        " + err.replace("\n", "\n        ")[-1800:])
        report += [f"## {'✅' if good else '❌'} {sc['name']} ({sc['group']})", "", sc["intent"], ""]
        report += [f"- FAILED: {f}" for f in ok.failed] + ([""] if ok.failed else [])
        try:
            report += [describe(sim), ""]
        except Exception as e:
            report += [f"(could not describe the run: {e})", ""]
    print(f"\n{len(picked) - failed} of {len(picked)} scenarios pass. Report: {args.report}")
    Path(args.report).write_text("\n".join(report))
    if app and picked:
        keep_open(sim, args.serve)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
