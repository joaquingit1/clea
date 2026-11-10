# Safety and trust

Clea reads your team's conversations and can write code in your repositories. This page says exactly what it can do,
what stops it, and what leaves your Mac.

## What leaves your Mac

- **To your model provider** (Anthropic or OpenAI, whichever you chose): the text Clea is working on, which
  includes message text and code. It goes through your own account, under that vendor's terms with you.
- **To Slack, GitHub and Jira**: API calls with your own credentials.
- **Nowhere else.** There is no Clea server, no account, no analytics and no telemetry. The app listens on
  `127.0.0.1` only and refuses requests that do not come from this Mac.

## Every write goes through the guard

The engine that reads and thinks has no code that posts to Slack or writes through GitHub's API. Every message,
pull request, comment, review and merge is a request to the guard, a separate process, over a local socket. The guard:

- checks each request against `<CLEA_HOME>/guard/policy.json`, written by the setup from the trust level you chose:
  which actions exist at all, in which repositories, and which of them need you;
- for an action that needs you, verifies your approval **itself**. It opens the Touch ID dialog that names what goes
  where, or reads your reaction on the draft in your own DM and compares the text. The worker saying "it was
  approved" counts for nothing, and a draft that changed after you approved it is a different draft;
- limits the rest by rule: posts without approval go only to your own DM with yourself, pull request operations only
  to pull requests you authored, merges only when the merge rule holds;
- refuses a message to other people that names the assistant. What is sent in your name reads as yours.

The one write the worker performs directly is `git push`, and only to a branch Clea created: it refuses the default
branch, `main`, `master` and `develop`, pushes only a commit that passed the checks and the self review, and never
force-pushes (if the branch moved, it stops).

A model that is confused, wrong or misled by something it read can produce a bad draft. It cannot send one.

## What this is not

It is a separation of duties inside your own account, not a sandbox. Clea runs as you, with your Slack token and
your `gh` login, and builds code by running your repository's own tooling on your Mac. Run it on a machine you
trust, point it at repositories you would run yourself, and start at **Watch** or **Ask first**.

## Trust levels

You choose one in the first run and can change it in Settings.

| | Watch | Ask first (default) | Autonomous |
| --- | --- | --- | --- |
| Write in your own DM with yourself (reports, the board) | yes | yes | yes |
| Open **draft** pull requests | no | yes | yes |
| Push fixes to pull requests it opened | no | yes | yes |
| Send a message to another person | no | with your OK | with your OK |
| Post a review on a teammate's pull request | no | with your OK | plain comments on its own |
| Merge | no | with your OK | its own approved, green pull requests |

At every level a message to another person is sent only after you approve that exact text.

## Approvals

- In the app: approve, edit or reject. Approving asks for Touch ID (or your password on a Mac without it).
- In Slack: a reaction on the draft in your own DM.

## What a message can and cannot do

Text from Slack, GitHub or Jira can mislead a model, like any text. What bounds the damage is the guard: whatever a
message talks the model into is still a draft, and a draft needs your approval to reach anyone.

## Code

- Work happens in a git worktree of its own, never in your checkout.
- A pull request is opened as a draft, after the repository's own checks pass on that exact commit.

## Reporting a problem

See [SECURITY.md](../SECURITY.md).
