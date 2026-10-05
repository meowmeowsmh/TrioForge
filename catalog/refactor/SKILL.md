---
name: refactor
title: Refactor & Simplify
description: Make recently changed code clearer and smaller without changing what it does.
when_to_use: cleaning up code, removing duplication, making a function easier to read, after a feature works
triggers: refactor, simplify, clean up, cleanup, deduplicate, make this readable, tidy up, simplify this code
version: 1.0.0
---

# Refactor & Simplify

Take working code and make it easier to understand without changing its behaviour.
The bar for success is that a reader cannot tell, from outside, that you were
there - same inputs, same outputs, same errors - but the inside reads in one
pass.

## The order

1. **Lock the behaviour first.** Run the tests. If none exist, add one that
   pins the current output. You are about to move things; the test is the only
   proof you did not move the *meaning*.
2. **Read the whole function, then act.** Simplification is local; understanding
   is not. You must see every caller before you change a signature.
3. **Make one kind of change per step**, re-running the test after each. Rename,
   extract, and delete are three separate steps, not one big rewrite.
4. **Stop early.** Clear enough is done. There is always one more abstraction
   available, and the last one is the one that hurts.

## What to do

- **Delete dead code.** A parameter nobody passes, a branch that cannot be
  reached, a variable written and never read, an import unused. Removing code is
  the highest-value refactor there is.
- **Rename for intent.** `d` → `days_since`. The name should say *why*, not
  *what*. A comment that restates the code is a sign the name is weak.
- **Extract a function only when it removes duplication or names a concept.**
  A function used once, for its own sake, is not simplification - it is
  indirection.
- **Flatten one level of nesting.** An `if` that guards the whole body becomes
  an early return. A loop body that is 40 lines becomes a call. Deep nesting is
  what makes code unreadable; every level you remove is a win.
- **Inline a helper that no longer earns its keep** after other helpers changed
  around it.
- **Split one job into two functions** when a function does "fetch, then
  transform, then save" and all three are interleaved.

## What never to do

- Do **not** change behaviour. "Simplify" is not "also fix that bug" - fix it in
  a separate change, or say so loudly.
- Do **not** introduce a framework, a new library, or a new pattern to "clean
  up". Simplification removes; it does not add dependencies.
- Do **not** reformat wholesale. Formatting churn buries the real change in a
  diff and makes review impossible.
- Do **not** rename public things without updating every reference. A rename
  that misses one caller is a bug you introduced.

## Output

Make the edits, run the test, and report in three lines: what you changed, what
you deliberately left alone and why, and the test result. No narration of how
clever the refactor was - if it is clever, do it again until it is obvious.
