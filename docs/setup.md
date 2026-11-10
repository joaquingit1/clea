# Setup

## Requirements

- macOS
- Python 3.12 or newer, `git`, and Node 22 or newer (to build the app once)
- The GitHub CLI, signed in: `gh auth login`
- A model, one of the options below
- Permission to create a Slack app in your workspace (the first run gives you the manifest)

## Install and run

```
git clone https://github.com/joaquingit1/clea && cd clea
make install
make run
```

Open http://127.0.0.1:4848. `make run` starts the worker and the guard and keeps them alive. The install's data lives
in `~/Library/Application Support/Clea/install`; set `CLEA_HOME` to keep it elsewhere, and `CLEA_PORT` to change the
port. `make desktop` opens the same screen in a native window that starts and stops the engine by itself.

## The first run

Each step saves as you go; you can leave and come back.

1. **Welcome.** Your name and the language Clea writes to you in (English or Spanish).
2. **Model.** How Clea thinks. Each option shows whether it is ready and what is missing.
3. **Slack.** Create the Slack app from the manifest, paste its token, and choose what Clea reads: channels (each
   with a role: your team's channel, where pull requests are posted, where alerts arrive, or just watched), your
   user groups, keywords, direct messages and mentions.
4. **Code.** Your GitHub organization and the repositories Clea may work on. Repositories already on this Mac are
   used where they are; the others are cloned.
5. **Team.** Your teammates (suggested from the team channel) and a sentence or two on what your team owns, so Clea
   knows what is yours to pick up.
6. **Tickets** (optional). Connect Jira and choose which tickets become work: projects, tickets assigned to you,
   tickets that mention you, or a JQL of your own.
7. **Trust.** Watch, Ask first or Autonomous. See [safety.md](safety.md).
8. **Review and start.**

Everything can be changed later in Settings.

## Models and sign-in

| Option | What it uses | Billing |
| --- | --- | --- |
| Claude Code on this Mac | The Claude Code you already installed and signed in to. Clea runs it; it never sees the credential. | Your Claude plan |
| Anthropic API key | The Anthropic API | Per token |
| Codex on this Mac | The Codex CLI you signed in to with ChatGPT | Your ChatGPT plan |
| Sign in with ChatGPT | OpenAI's own sign-in page | Your ChatGPT plan |
| OpenAI API key | The OpenAI API | Per token |

Anthropic does not offer a way for another app to sign users in with their Claude account, so using a Claude plan
means running your own Claude Code. API keys are stored in the macOS Keychain.

## The Slack app

The app is yours, in your workspace, with user scopes only (no bot user). Clea acts as you, so it sees what you see.

| Scope | Why |
| --- | --- |
| `channels:history`, `groups:history`, `im:history`, `mpim:history` | Read the channels and DMs you chose |
| `channels:read`, `groups:read`, `im:read`, `mpim:read` | List them in the setup |
| `users:read`, `usergroups:read` | Show who wrote a message; see your user groups |
| `search:read` | Find mentions of you |
| `reactions:read` | Read your approvals by reaction |
| `files:read` | Open screenshots and files in threads |
| `chat:write` | Post in your own DM, and send the replies you approve |
| `pins:write` | Pin the board in your own DM |

Socket Mode is on, so new messages arrive at once.

## Checks before a pull request

Clea detects how each repository checks itself (Node, Python, Go, Rust, Makefile). To name the commands yourself, list
them per repository in `<CLEA_HOME>/config.json`. A listed check wins over detection:

```json
{
  "repos": {
    "web": {
      "checks": [
        {"name": "lint", "run": "pnpm lint"},
        {"name": "tests", "run": "pnpm test", "dir": "apps/checkout", "paths": ["apps/checkout/"]}
      ]
    }
  }
}
```

`dir` is where the command runs inside the repository, and `paths` limits a check to diffs that touch those paths.
