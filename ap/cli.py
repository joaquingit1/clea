"""clea: inspect and drive this install.

  clea run                    run this install: its guard and its worker, restarted if they stop
  clea status                 mode, health, queue by status, budget, recent errors
  clea items [all]            open items (or all)
  clea item <id>              one item: data, transitions, calls
  clea step <id>              run the item's current stage now (foreground)
  clea set <id> <status>      move an item (e.g. retry a blocked one)
  clea mode shadow|act        shadow: write nothing, log what would have been done; act: do it
  clea pause | resume         stop/start all activity (worker keeps running)
  clea restart                graceful: finish in-flight stages, then restart (never kill work)
  clea ingest | triage | babysit | board
  clea shadow-log [n]         what act mode would have done
  clea worker                 run the worker in the foreground
  clea log [n]                tail today's log
"""
from __future__ import annotations

import json
import sys

from . import db
from .core import CFG, HOME, LOG_DIR, VAR, now


def _status() -> None:
    h = json.loads((VAR / "health.json").read_text()) if (VAR / "health.json").exists() else {}
    age = now() - h.get("at", 0)
    print(f"mode={CFG['mode']} paused={(VAR / 'PAUSED').exists()} worker={'alive' if age < 120 else f'STALE {age}s'} "
          f"muse_paused={h.get('muse_paused')}")
    for r in db.q("SELECT status, count(*) c FROM items GROUP BY status ORDER BY c DESC"):
        print(f"  {r['status']:<16} {r['c']}")
    for r in db.q("SELECT tier, calls, round(usd,2) usd, in_tok, out_tok FROM budget WHERE day=?", db.today()):
        print(f"  today {r['tier']}: {r['calls']} calls ${r['usd']} in={r['in_tok']} out={r['out_tok']}")
    print(f"  claude cap ${CFG['claude']['daily_usd_cap']}/day")
    from . import prefilter
    pf = prefilter.stats()
    print(f"  triage today: prefiltered={pf.get('prefiltered', 0)} model_triaged={pf.get('model_triaged', 0)} "
          f"fast_lane={pf.get('fast_lane', 0)} reasons={json.dumps(pf.get('reasons', {}))}")
    gv = db.q("SELECT json_extract(data,'$.verdict') v, count(*) c FROM signals WHERE kind='pregate' AND at>? GROUP BY v",
              now() - 7 * 86400)
    if gv:
        print("  pre-build gate (7d): " + " ".join(f"{r['v']}={r['c']}" for r in gv))
    for r in db.q("SELECT id, status, last_error FROM items WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 5"):
        print(f"  err #{r['id']} [{r['status']}] {r['last_error'][:140]}")


def _items(all_: bool) -> None:
    where = "" if all_ else "WHERE status NOT IN ('done','dropped','expired','failed')"
    for r in db.q(f"SELECT id, kind, status, urgency, title, repo, pr FROM items {where} ORDER BY id DESC LIMIT 60"):
        ref = f" {r['repo']}#{r['pr']}" if r["pr"] else ""
        print(f"#{r['id']:<4} {r['kind']:<9} {r['status']:<15} u{r['urgency']} {r['title'][:70]}{ref}")


def _item(i: int) -> None:
    it = db.item(i)
    if not it:
        print("no such item")
        return
    print(json.dumps({k: v for k, v in it.items() if k != "data"}, indent=1, default=str))
    print(json.dumps(it["data"], indent=1, ensure_ascii=False, default=str)[:6000])
    for r in db.q("SELECT * FROM transitions WHERE item_id=? ORDER BY at", i):
        print(f"  {r['src'] or '-'} -> {r['dst']}  {r['note'] or ''}")
    for r in db.q("SELECT tier, lane, ok, round(dur_s) d, round(usd,3) usd, note FROM calls WHERE item_id=?", i):
        print(f"  call {r['tier']}/{r['lane']} ok={r['ok']} {r['d']}s ${r['usd']} {r['note'] or ''}")


# what only the first install has: its supervisor, its learning loop and its deploy gate
FIRST_INSTALL_HELP = """  clea patrol | learn | evals | seed
  clea supervise              run the observer + supervisor session on the next 30-min tick (they run weekly otherwise)
  clea selftest               every item kind through every stage with fakes (deploy gate)
"""


def _help() -> str:
    from . import site
    return __doc__ + (FIRST_INSTALL_HELP if site.internal() else "")


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_help())
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        _status()
    elif cmd == "incidents":
        from . import incidents
        for i in incidents.collect():
            if i["status"] in ("open", "recurred", "unpromoted"):
                fix = next((a["commit"] for a in reversed(i["attempts"]) if a.get("status") == "fixed"), "")
                lost = f" (fix {fix} never reached main: re-land it)" if i["status"] == "unpromoted" else ""
                print(f"{i['sig']} [{i['status']}] impact={i['impact']:.1f} n={i['count']} {i['kind']}: {i['title']}{lost}")
    elif cmd == "incident":
        from . import incidents
        incidents.mark(rest[0], rest[1], rest[2] if len(rest) > 2 else "", " ".join(rest[3:]))
        print("ok")
    elif cmd == "items":
        _items(bool(rest and rest[0] == "all"))
    elif cmd == "item":
        _item(int(rest[0]))
    elif cmd == "step":
        from .worker import run_stage
        it = db.item(int(rest[0]))
        run_stage(it)
        _item(it["id"])
    elif cmd == "set":
        db.update_item(int(rest[0]), status=rest[1], note="manual", attempts=0, last_error=None, next_run_at=0)
    elif cmd == "mode":
        assert rest and rest[0] in ("shadow", "act")
        from .core import save_config
        save_config({"mode": rest[0]})
        print(f"mode -> {rest[0]} (worker picks it up within 5 min, or restart it)")
    elif cmd == "restart":
        (VAR / "RESTART").touch()
        print("graceful restart requested: the worker stops taking work and restarts once in-flight stages finish")
    elif cmd == "pause":
        (VAR / "PAUSED").touch()
    elif cmd == "resume":
        from . import hot
        hot.resume()
    elif cmd == "ingest":
        from . import ingest
        print(ingest.poll_channels(), ingest.poll_threads(), ingest.search_mentions(), ingest.poll_github())
    elif cmd == "triage":
        from . import triage
        print(triage.run_batch(int(rest[0]) if rest else 20))
    elif cmd == "babysit":
        from . import babysit
        print(babysit.run_once())
    elif cmd == "board":
        from . import board, digest
        print(digest.render())
        if rest and rest[0] == "post":
            board.refresh(force=True)
    elif cmd == "patrol":
        from . import patrol
        print(patrol.run_once())
    elif cmd == "learn":
        from . import learn
        print(learn.nightly())
    elif cmd == "evals":
        from . import evals
        print(evals.weekly())
    elif cmd == "seed":
        from . import evals
        print("triage cases +", evals.seed_triage())
        print("review cases +", evals.seed_review())
    elif cmd == "shadow-log":
        n = int(rest[0]) if rest else 20
        f = VAR / "shadow-actions.jsonl"
        for line in (f.read_text().splitlines()[-n:] if f.exists() else []):
            print(line[:600])
    elif cmd == "outcomes":  # run the outcome audit now and print the report
        from . import outcomes
        outcomes.audit()
        print((VAR / "doctor-report.md").read_text())
    elif cmd == "supervise":  # on demand: guard/run_supervisor.sh picks the flag up on its next tick
        (VAR / "supervisor.run-now").touch()
        print("the supervisor runs on its next tick (launchd, every 30 min)")
    elif cmd == "dm-tidy":  # one-off cleanup of the self-DM (the lifecycle keeps it tidy on its own)
        from . import dm
        print(dm.tidy())
    elif cmd == "selftest":
        from .selftest import main as st_main
        raise SystemExit(st_main())
    elif cmd == "worker":
        from .worker import main as wmain
        wmain()
    elif cmd == "run":  # one install's guard and worker, kept alive (what the app or a login item starts)
        from .launch import main as lmain
        lmain()
    elif cmd == "log":
        n = int(rest[0]) if rest else 40
        f = LOG_DIR / f"ap-{db.today()}.log"
        print("\n".join(f.read_text().splitlines()[-n:]) if f.exists() else "(no log today)")
    else:
        print(_help())


if __name__ == "__main__":
    main(sys.argv[1:])


def entry() -> None:
    """Console-script entry point (`clea...`)."""
    import sys
    main(sys.argv[1:])
