# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

This project is pre-1.0. Fixes land on `main`; there are no backport branches.

## Reporting a vulnerability

Please report privately through GitHub's
[private vulnerability reporting](https://github.com/yalayoloyellow/minimalma/security/advisories/new)
rather than opening a public issue.

Include what you found, how to reproduce it, and what an attacker gets. You can
expect an acknowledgement within a few days and an assessment within two weeks.
If a fix is warranted you will be credited in the release notes unless you
prefer otherwise.

## Scope

In scope:

- Remote code execution, path traversal, or command injection through uploaded
  files, filenames, captions or callback data.
- SQL injection.
- Authorisation flaws — anything that lets a non-curator publish, decline or
  read the review queue, or lets one listener read another's history.
- Token or credential disclosure through logs, errors or exports.
- Denial of service that a single unprivileged Telegram user can trigger.

Out of scope:

- Anything requiring access to the host running the bot, or to the
  configuration file itself.
- Abuse a curator could commit — curators are trusted by design.
- Telegram platform issues; report those to Telegram.
- Spam or flooding beyond what the built-in rate limits cover, on a station
  configured with those limits raised.

## What the project already does

- Every SQL statement is parameterised; no query is built by string
  interpolation of user data.
- Subprocesses are argument lists, never a shell, always with a timeout.
- Uploaded files are size-capped, parsed defensively, and never executed;
  parser failures return empty metadata rather than propagating.
- Curator checks are enforced on every moderation callback, server side, not
  by hiding buttons.
- The config file is written 0600 and the token is never logged.
- Per-user daily limits bound submissions and discovery requests.

## What it does not do

- There is no sandbox around `ffmpeg`. If you are worried about a malicious
  file reaching a decoder, run the bot as an unprivileged user in a container.
- There is no virus scanning of uploads.
- Telegram user ids are treated as authentic because Telegram authenticates
  them. There is no additional identity check.
