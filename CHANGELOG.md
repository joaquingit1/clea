# Changelog

Notable changes to Clea. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **A new install starts from now.** It used to take on everything it could see on its first start: every open pull
  request with red CI or an open thread, every pull request waiting for a review, a week of tickets and two weeks of
  Slack. It now takes on what moved in the two days before setup and everything after (`intake.backlog_days`).
- **A free look at GitHub before each paid stage.** A case whose pull request merged or closed, whose check is no
  longer red or whose threads were resolved ends there, without a model call.
- **Mechanical fix rounds start on Sonnet** and step up to Opus for the rest of the case when one fails or its problem
  returns (`quality.light_fixes`). Sorting review comments runs on Sonnet; Opus still confirms every fix and writes
  every defense. A CI diagnosis that comes back "unknown" is asked again of Opus.
- **Lighter sessions.** A code session no longer carries the subagent, task and skill tools it almost never called
  (about a third of its opening context, re-sent on every turn; `claude.repo_skills` keeps a repository's own skills).
  Every session is told to make independent lookups in one turn, and each stage of a case passes the next the files
  it opened, so they are not searched for again.

## [0.2.1] - 2026-11-10

### Added

- **A download.** `Clea-0.2.1-arm64.dmg` is Clea.app with the engine, its own Python and the Touch ID helper inside:
  nothing else to install, and the first run happens in the app's own window. `make dmg` builds the same image. It is
  signed ad hoc and not notarized yet, so macOS asks you to allow it the first time.
- `make run` now opens Clea's window from a checkout; `make serve` runs the engine alone for a browser.

### Changed

- **The Slack step says where the token is.** It names the page and the section (OAuth & Permissions, OAuth Tokens,
  User OAuth Token), explains what the token is, and links back to your Slack apps.
- **The setup screen answers at once.** It ran every probe (the CLIs, their sign-ins, the Keychain) on each request
  and took 3 s or more, far longer next to a busy worker. The answers are now kept and refreshed in the background.

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

[0.2.1]: https://github.com/joaquingit1/clea/releases/tag/v0.2.1
[0.2.0]: https://github.com/joaquingit1/clea/releases/tag/v0.2.0
