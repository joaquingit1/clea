# Security

Clea reads private conversations and can write in a person's name, so security reports get priority over everything
else.

## Reporting a vulnerability

Report it privately through GitHub: **Security** tab, **Report a vulnerability**
(https://github.com/joaquingit1/clea/security/advisories/new). Please do not open a public issue or discussion.

Include what you can of:

- what an attacker can do, and what they need to start from (a message in a channel Clea reads, a pull request,
  local access to the Mac);
- the steps, or a simulator scenario that shows it (`sim/scenarios.py`);
- the version or commit.

Reports are read first, before anything else in the tracker. A confirmed report is fixed before it is disclosed, and you are credited
in the release notes unless you prefer otherwise.

## What counts

Especially interesting:

- anything that makes the guard send, post, review or merge without the approval the policy requires, or with a
  text other than the one approved;
- a message, pull request or ticket that makes Clea act outside the repositories and channels of the install;
- a way for a web page or another local process to drive the app's API or the guard's socket;
- a token, message or piece of private code written somewhere it should not be (logs, the database, a prompt sent
  to a provider the user did not choose).

Out of scope: a model writing a poor or wrong **draft** (that is what the approval is for), and anything that needs
the attacker to already run code as the user on the Mac.

## Supported versions

The latest release and `main`.

## How Clea is built to fail safe

See [docs/safety.md](docs/safety.md).
