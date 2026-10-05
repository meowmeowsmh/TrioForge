---
name: code-review
title: Code Review
description: Review a diff or file for real bugs and risks, with confidence filtering so only actionable findings are reported.
when_to_use: reviewing code, a pull request, a diff, or checking work before it ships
triggers: code review, pull request, review, diff, merge request, code smells, pr
version: 1.0.0
---

# Code Review

Review as someone who will be paged at 3am for this code. The goal is not to
prove you read it - it is to stop real defects before they reach production.

## Workflow

1. **Read the whole change first, without commenting.** Form one sentence: what
   is this change trying to do? If you cannot, that is finding #1.
2. **Read the callers, not just the callee.** A function that changed behaviour
   is only correct if every call site still holds. Grep for the name.
3. **Check the paths that are not the happy path.** Empty input, one item, a
   missing key, the network failing, the file not existing, two writes at once,
   a negative or zero number, unicode, and a very large input.
4. **Report. Then stop.** Do not rewrite the project.

## What counts as a finding

Only these. Everything else is noise.

- **Correctness** - the code does not do what it says, off-by-one, inverted
  condition, wrong variable, mutation of a shared value, `==` on floats.
- **Crash risk** - unguarded index, `None` dereference, bare `except` swallowing
  a real error, a resource opened without closing, a parse of untrusted input.
- **Security** - injection through string-built SQL/shell/HTML, a secret in the
  source, a path built from user input without normalising, a missing auth check
  on a new route, `eval`, unsafe deserialisation.
- **Data loss** - an overwrite where an append was meant, a delete with no
  transaction, a migration that is not reversible.
- **Concurrency** - a race between check and use, a shared cache without a lock,
  work assumed to finish in order.
- **Contract breaks** - a changed signature, return type, or error type that
  callers have not been updated for.

## What is not a finding

Style preferences, naming taste, "consider extracting a helper", missing
docstrings, formatting, an import order, or anything you would phrase as "you
could also". If a human reviewer would say "sure, fine either way", delete it.

## Confidence filter

For each candidate finding, ask: **would it still be a bug if the author
explained their intent?** If no, drop it. State a finding only when you can name
the input or the sequence that triggers it.

Mark each finding with one of:

- `blocking` - wrong, unsafe, or data-losing. Must change before merge.
- `should-fix` - a genuine defect on an edge case, or a contract break.
- `nit` - real but trivial. Maximum three, or they crowd out the rest.

## Output

For each finding, in this shape and nothing more:

```
path/to/file.py:123  [blocking]  <one-line claim>
Why: <the input or sequence that breaks it>
Fix: <the smallest change that resolves it>
```

If you found nothing in a category, do not list the category. If you found
nothing at all, say "No blocking findings." and name the one area you would
test first. Do not invent findings to look thorough - a clean review is a valid
result and a manufactured nit costs the author more than it saves.
