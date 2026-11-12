# Setup

## Requirements

- macOS on Apple Silicon
- `git`, and the GitHub CLI signed in: `gh auth login`
- A model, one of the options below
- Permission to create a Slack app in your workspace (the first run gives you the manifest)

Running from source also needs Python 3.12 or newer and Node 22 or newer.

## Install the app

Download `Clea-<version>-arm64.dmg` from the [releases page](https://github.com/joaquingit1/clea/releases/latest),
open it and drag Clea to Applications. The app carries the engine, the Python it runs on and the Touch ID helper;
it starts the engine when you open it and stops it when you quit.

### The first time you open it

The download is signed ad hoc and not notarized by Apple yet, so macOS refuses the first launch with "Apple could
not verify Clea is free of malware". To allow it:

1. Open Clea once and dismiss the message.
2. Open System Settings, Privacy & Security, scroll to Security and choose **Open Anyway** next to Clea.

Or, from a terminal: `xattr -dr com.apple.quarantine /Applications/Clea.app`.

To check what you downloaded, compare `shasum -a 256 Clea-<version>-arm64.dmg` with the hash in the release notes,
or build the same image yourself with `make dmg`.

## Or run from source

```
git clone https://github.com/joaquingit1/clea && cd clea
make install
make run
```

`make run` opens Clea's window, which starts the worker and the guard and keeps them alive. `make serve` runs the
engine alone and serves the same screen to a browser at http://127.0.0.1:4848 (set `CLEA_PORT` to change the port).

Both ways keep the install's data in `~/Library/Application Support/Clea/install`, so you can move between them;
set `CLEA_HOME` to keep it elsewhere.

## The first run

Each step saves as you go; you can leave and come back.

1. **Welcome.** Your name and the language Clea writes to you in (English or Spanish).
2. **Model.** How Clea thinks. Each option shows whether it is ready and what is missing.
3. **Slack.** Create the Slack app from the manifest, paste its token ([where to find it](#the-slack-app)), and choose
   what Clea reads: channels (each with a role: your team's channel, where pull requests are posted, where alerts arrive, or just watched), your
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

Connecting it takes three steps, and the Slack step of the first run links to each:

1. **Create the app.** The first run opens Slack's "create an app" page with Clea's manifest already filled in. Pick
   your workspace, choose Next, then Create.
2. **Install it.** On your new app's page choose Install to Workspace (also under Install App in the left sidebar)
   and allow it.
3. **Copy the User OAuth Token.** This is the key Slack gives your app to act as you. On your app's page, open
   **OAuth & Permissions** in the left sidebar: the token is at the top, under **OAuth Tokens**, and starts with
   `xoxp-`. Paste it into Clea, which keeps it in the macOS Keychain. If you closed the tab, your app is listed at
   https://api.slack.com/apps.

The App-Level Token (`xapp-`) is optional. With it, new messages arrive at once over Socket Mode; without it Clea
checks Slack every 60 seconds. Create it on your app's page under Basic Information, App-Level Tokens, with the
`connections:write` scope.

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


## What a new install takes on

Clea starts from the day you set it up, not from your backlog. It takes on what moved in the two days before setup
and everything after: a ticket, a review request or one of your own pull requests that was last touched before that
is left alone until something happens on it (a push, a comment, a new assignment). To change the window, set the
number of days in `<CLEA_HOME>/config.json`; `0` starts from the moment setup finished:

```json
{ "intake": { "backlog_days": 2 } }
```

## What keeps the cost down

- **A free look before a paid stage.** Before a session starts on a pull request, Clea asks GitHub whether there is
  still anything to do. A pull request that merged or closed, a check that is no longer red or threads that
  were resolved meanwhile end the case without a model call.
- **Mechanical fixes start on the mid-tier model.** When every problem in a fix round has one obvious fix that changes
  no behaviour (formatting, lint, an import, a type annotation, a generated file), the round starts on Sonnet. If it
  fails, or the problem comes back, the case goes to Opus and stays there. Builds, plans, reviews and anything that
  needs judgement always run on Opus. To keep every fix on Opus:

```json
{ "quality": { "light_fixes": false } }
```

- **Sessions carry less and look things up together.** A code session is given the tools it uses (read, edit,
  search, shell) and not the ones it does not, and is told to ask for independent lookups in one turn. Each stage of
  a case also hands the next one the list of files it opened. If your repositories ship their own Claude Code skills
  (`.claude/skills`) and you want code sessions to load them, turn them on:

```json
{ "claude": { "repo_skills": true } }
```

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
