# Contributing to Clea

Thanks for wanting to make Clea better. This page is the short version of how a change gets in.

## Set up

```
git clone https://github.com/joaquingit1/clea && cd clea
make install        # the engine in .venv and the app's screen
make test           # unit tests
make sim            # every simulated scenario, no accounts and no tokens
make demo           # the app on a simulated workspace
```

You do not need Slack, GitHub or a model account to work on the engine: the simulator runs it end to end against a
simulated workspace. See [docs/simulator.md](docs/simulator.md).

## What a good change looks like

- **It starts from a situation.** "A teammate edits their message after Clea drafted a reply" is a situation.
  "Refactor intake" is not. If there is no issue yet, open one or describe the situation in the pull request.
- **Behaviour comes with a scenario.** A change in what Clea does with a message, a pull request or a ticket adds or
  updates a scenario in `sim/scenarios.py`. Pure functions get a unit test in `tests/`.
- **Every write goes through the guard.** The engine never posts to Slack or writes through GitHub's API by itself.
  A new kind of write is a new guard action with a policy mode, and it is discussed in an issue first.
- **Nothing is sent to a real workspace by a test.** Tests and scenarios run on scratch installs.
- **It is small.** One situation per pull request. A large change is easier to land as a series.

## Style

- Python 3.12, standard library first. The engine's only runtime dependency is `slack_sdk`; adding one needs a reason.
- Lines up to 140 characters, `ruff` clean.
- Comments say why, not what. If a rule exists because of something that went wrong, say what went wrong.
- Text the user reads goes through `site.t(english, spanish)`, so an install reads in its own language.
- The app: React and Tailwind, strings in `ui/src/i18n/`, no new dependency for something a few lines can do.

## Pull requests

1. Fork, branch from `main`, make the change.
2. `make test && make sim` pass. If you touched `ui/`, `make app` builds.
3. Fill in the pull request template: what changes for the user, why, how you checked it.
4. A maintainer reviews. Expect questions about the situation and the edge cases more than about style.

## Reporting bugs and asking for features

Use the issue forms. The best bug report is a simulator scenario that fails.

## Security

Please do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
