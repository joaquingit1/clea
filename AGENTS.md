# Notes for coding agents

You are working on Clea, an engineering agent that runs on a developer's Mac. Read this before changing anything.

## Commands

```
make install   # once: the engine in .venv and the app build
make test      # unit tests (pytest), a few seconds
make sim       # every scenario on the simulated workspace, about a minute, no network and no tokens
make app       # typecheck and build ui/
```

Run `make test && make sim` before you say a change works. A single scenario: `.venv/bin/python -m sim -k name -v`.

## Layout

- `ap/` the engine. `ap/site.py` is the only place that knows about the install (names, language, features);
  the rest asks it. `ap/setup.py` is the first run. `ap/console.py` is the app's API.
- `guard/ap_guard.py` the only code that writes to Slack or GitHub's API. Stdlib only, on purpose.
- `sim/` the simulator: `world.py` (the simulated Slack and GitHub), `harness.py` (the engine wired to it),
  `scenarios.py` (what must be true).
- `ui/` the app, `desktop/` the Electron window, `prompts/engine/` the prompts, `tests/` unit tests.

## Rules that are not negotiable

1. The engine never writes to Slack or GitHub's API directly. A write is `guardcall.act(action, ...)`, and the guard
   decides. Do not add a client call that posts, comments, reviews or merges anywhere under `ap/`.
2. Tests and scenarios never touch a real install or a real workspace. They run on scratch homes
   (`CLEA_SIM_HOME`, or a temporary `CLEA_HOME`).
3. Tokens are never logged, written to the database or to `config.json`.
4. One conversation is one case. If your change can open a second case for a thread, a DM or a ticket that already
   has an open one, it is wrong.
5. Text the user reads goes through `site.t(english, spanish)`. Nothing an English install shows may be Spanish.

## How to change behaviour

Write the scenario first (`sim/scenarios.py`), watch it fail, make it pass, then run the whole simulation: the
global invariants (one open case per conversation, nothing sent without an approved ask) run after every scenario
and catch what a single scenario does not.

## Style

Python 3.12, standard library first, 140 columns, comments that say why. Small diffs.
