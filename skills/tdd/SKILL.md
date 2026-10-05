---
name: tdd
title: Test-Driven Development
description: Write the failing test first, then the smallest change that passes, then refactor - red, green, refactor.
when_to_use: adding a feature, fixing a bug, or any change where correctness must be demonstrated
triggers: test, tests, unit test, pytest, tdd, test-driven, write a test, add a test, coverage
version: 1.0.0
---

# Test-Driven Development

The discipline is one sentence: **write a test that fails for the right reason
before writing the code that makes it pass.** Everything below is about not
cheating on that.

## The cycle

**Red.** Write the smallest test that expresses one piece of desired behaviour.
Run it. Read the failure. It must fail because the behaviour is missing - not
because of an import error, a typo, or a fixture you forgot. A test that fails
for the wrong reason proves nothing.

**Green.** Write the least code that makes that test pass. Not the general
solution, not the abstraction you can see coming - the least. If you are tempted
to write a second case first, write its test first instead.

**Refactor.** With the test green, improve the shape: names, duplication,
placement. The test is your permission slip - if it stays green, you did not
break behaviour.

**Repeat.** One behaviour per cycle. A test that asserts five things is five
tests wearing a coat.

## Writing a test that actually holds

- **Name it for the behaviour, not the function.** `test_empty_cart_totals_zero`
  beats `test_total_2`.
- **One reason to fail.** If two independent bugs could turn it red, split it.
- **Arrange, act, assert** - visible as three blocks. If the act is more than one
  line, the unit is too big.
- **Test the boundary, not the middle.** Empty, one, many, exactly-at-the-limit,
  one-past-the-limit, negative, zero, `None`, wrong type, unicode.
- **Assert on values, not on how they were produced.** Asserting that a mock was
  called locks in your implementation and breaks on every refactor.
- **No logic in a test.** No `if`, no loops generating cases, no helper that
  hides the assertion. A test you have to debug is not a test.

## For a bug fix, the order is not optional

1. Write a test that reproduces the bug. **Run it and watch it fail.**
2. Fix the bug.
3. Run the test. It passes.
4. Revert the fix mentally: if the test would still pass, the test is wrong, and
   you have not fixed the bug - you have moved it.

## In this repository

- Tests live in `tests/`. Run the suite with `tests/regress.py` and expect
  `ALL REGRESSION TESTS PASSED`.
- Match the existing test style in `tests/` rather than introducing a second
  framework. The project deliberately keeps its test dependencies light.

## When TDD genuinely does not fit

Exploratory work, a spike to learn an API, or a one-line change to a value.
Say so out loud when you skip it, and add the test immediately afterwards. What
is never acceptable is skipping it silently and claiming the change is verified
when nothing was run.
