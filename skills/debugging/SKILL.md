---
name: debugging
title: Debugging
description: Find a root cause from a symptom by bisecting and instrumenting, instead of guessing at fixes.
when_to_use: something is broken, throwing, failing, hanging, or behaving differently than expected
version: 1.0.0
---

# Debugging

The failure mode to avoid is *shotgun debugging*: change something plausible,
re-run, change something else. That burns a turn per guess and often "fixes" the
symptom by accident while leaving the cause in place.

## The loop

1. **Reproduce it reliably, in the smallest form.**
   A bug you cannot trigger on demand is a bug you cannot verify you fixed. Find
   the exact input or sequence. Delete everything that is not needed to trigger
   it. If it only happens once, suspect state: a cache, a stale file, an
   ordering assumption.

2. **Read the actual error. All of it.**
   The last frame that is *yours* is where you start, not the first frame. For a
   traceback, read bottom-up to find your code, then read the values involved.
   `TypeError: 'NoneType' object is not subscriptable` is not "a None problem" -
   it names the exact expression that was None.

3. **State the hypothesis as a falsifiable claim.**
   "`cfg['model']` is missing when the config file is absent" - not "something is
   wrong with config loading". A claim you cannot test is a guess.

4. **Test the claim, before changing anything.**
   Add a print/log/assert at the boundary and run it. Or run the one function in
   isolation. The point is to learn which half of the system is wrong.

5. **Bisect the space, not the code.**
   Halve the input until it is minimal. Comment out half the pipeline. Check
   whether it fails on a fresh clone or a clean database - that separates "our
   bug" from "your machine".

6. **Fix the cause, not the symptom.**
   `try/except: pass` around a crash, a `None` check that papers over a missing
   value, or a retry around a logic error are all symptom fixes. If the fix does
   not explain *why* the original failure happened, it is not done.

7. **Prove it, then prove it stays fixed.**
   Re-run the original reproduction and show it now passes. Add a test that fails
   on the old code and passes on the new. Then check you did not break the
   neighbours - run the suite.

## Traps that waste the most time

- **Changing two things at once.** Then you cannot tell which one worked.
- **Trusting an assumption you never tested.** "That list is always sorted",
  "this only runs on Windows", "the user is logged in here". Print the value.
- **Fixing where the error was raised instead of where the bad value was born.**
  A crash on line 200 usually means line 40 passed a wrong value. Trace it back
  to the source.
- **Debugging the framework.** Read your own code first. It is almost always
  yours.
- **Assuming a stale process, cache or bytecode.** Restart, clear, reinstall -
  and say so explicitly if that is what you suspect.

## When to stop and ask

If you have formed and tested three distinct hypotheses and the behaviour is
unchanged, stop guessing. Report: what you reproduced, what you ruled out and
how, and the smallest unresolved question. That is a useful result. A fourth
speculative edit is not.
