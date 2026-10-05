---
name: commit
title: Commit & Push
description: Stage, write a real commit message, and push - with an explicit check that the user actually asked for it.
when_to_use: the user asks to commit, save the work, push, or ship it
triggers: commit, push, git, stage, merge, ship it
version: 1.0.0
---

# Commit & Push

**Only ever run this when the user asked for it.** "It works" is not "commit
it". If you are unsure whether they meant it, do not commit - ask.

## Before staging

1. `git status` and `git diff` - read what you are about to commit. Never stage
   blind, and never `git add -A` without looking.
2. **Check for secrets.** A credential, token, `.env`, `*_credentials.json`, or
   database dump in the diff means stop and tell the user. Confirm any such file
   is in `.gitignore` and untracked before it can be committed.
3. Exclude generated noise: `__pycache__`, `.pyc`, build output, logs, editor
   and OS files, large binaries.
4. Confirm the check you claim passed actually ran. If there is a test suite,
   run it before the commit, not after.

## Stage deliberately

Stage the files that belong to *this* change by name. Two unrelated changes
should be two commits - a mixed commit cannot be reverted cleanly.

## Write the message

One subject line, imperative mood, under ~72 characters. It says what the change
does, not which files moved and not "fix bug".

```
<area>: <what changed and why it matters>

<body: why this approach, what it replaces, anything surprising.
Wrap at ~72. Omit if the subject is genuinely self-explanatory.>
```

Good: `gmail: a replayed sign-in link is not a failure`
Bad: `fixed stuff`, `update gmail.py`, `changes`

Do not add tool attribution, "Generated with", or a co-author trailer unless the
user asked for one.

## Then

- Push only if the user asked to push. State the branch and the commit hash.
- Never force-push, never rewrite pushed history, never commit to a branch you
  were not asked to touch.
- Never commit directly to a protected branch if the project uses pull requests.
- Report the result honestly: which commit, which branch, and whether the push
  succeeded. If a pre-commit hook or the remote rejected it, show the actual
  error - do not retry with `--no-verify` to get past it.
