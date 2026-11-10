# Changelog

Notable changes to Clea. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions
follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-10

The first public release.

### Highlights

- **A first run for any team.** Eight steps in the app connect your model, your Slack, your repositories, your
  team and (optionally) Jira, and set how much Clea may do on its own. Nothing is edited by hand.
- **Bring your own model.** Claude Code on your Mac, an Anthropic API key, Codex on your Mac, Sign in with ChatGPT,
  or an OpenAI API key.
- **The guard.** A separate process checks every write against your policy and verifies your approval itself.
  A message to another person is only ever sent after you approve that exact text.
- **From a reported bug to a draft pull request.** Investigate, plan, build in a worktree, run the repository's own
  checks (Node, Python, Go, Rust and Makefile projects are detected), self review, and open a draft.
- **One conversation, one case.** A thread, a DM or a ticket never becomes two pieces of work, and the same report
  in two channels folds into one.
- **The simulator.** `python -m sim` runs the real engine against a simulated Slack and GitHub: 51 scenarios on a
  scripted model, and `--live` on your real one. `make demo` opens the app on a simulated workspace.
- **English and Spanish.** The app and everything Clea writes to you follow the install's language.
- **A desktop app** that starts and stops the engine by itself.

[0.2.0]: https://github.com/joaquingit1/clea/releases/tag/v0.2.0
