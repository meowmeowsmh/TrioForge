"""Role-based team mode: several models work ONE task as a pipeline, not peers.

Parallel team mode (py/tui/team.py) runs every model in the pool on the same task
and ships the first answer. This is the OTHER team shape - a working team with
distinct roles, where the work flows through the models in order:

    planner  ->  coder  ->  reviewer  ->  fixer (only when the review finds issues)

    - planner   reads the task and writes a concrete, ordered plan.
    - coder     executes the plan with the agent tools and ships the real result.
    - reviewer  checks the result against the task + plan, then either ships it or
                lists concrete, fixable issues.
    - fixer     applies those issues one by one and ships the corrected result.

The coordinator (textual_app) owns the loop: it runs each role with a model from
the pool, feeds each role the previous role's output, and ships the final answer.
Pool assignment mirrors auto-route: a strong model plans and reviews, a cheap/fast
model builds and fixes.

Like py/tui/team.py, ONLY the directives live here, so the pipeline can be tested
with no model, no GPU and no network.
"""

from __future__ import annotations

#: The pipeline order. The coordinator runs these in sequence.
ROLE_ORDER = ("planner", "coder", "reviewer", "fixer")

#: One-line job descriptions (for /help and the status line).
ROLE_DESCRIPTIONS = {
    "planner": "read the task, write a concrete plan",
    "coder": "execute the plan with tools, ship the finished work",
    "reviewer": "check the work, then ship it or list concrete issues",
    "fixer": "apply the reviewer's issues, ship the corrected work",
}

#: What each role is asked, verbatim. The model has tools, so every role's output
#: is an artefact the next role consumes - never a question handed back to the user.
ROLE_DIRECTIVES = {
    "planner": (
        "You are the PLANNER on a small team. Read the task, then produce a "
        "concrete, ordered plan to finish it yourself.\n\n"
        "The plan must name real files and folders, list the tools to use at each "
        "step (view/grep/glob to read, write/edit to create or change, bash to run "
        "and verify), and state how each step's result is checked. Keep it short - "
        "a numbered list, not an essay. Do NOT start writing files yet and do not "
        "ask the user anything: the next teammate executes your plan."
    ),
    "coder": (
        "You are the CODER on a small team. Execute the plan you are given, now, "
        "with your tools: view/grep/glob to read, write/edit to create or change "
        "files, bash to run and verify.\n\n"
        "Produce the ACTUAL finished result - real files with real content, code "
        "that runs - not an empty folder, a list, a plan, or a description. Follow "
        "the plan step by step, then report what you made with file paths and the "
        "evidence that it runs. Do not ask for clarification and do not stop at a "
        "first guess: make the thing, run it, and hand over a real result."
    ),
    "reviewer": (
        "You are the REVIEWER on a small team. Read the task, the plan, and what "
        "the coder actually made. Judge only the RESULT against the task.\n\n"
        "If the work is done and correct, answer with exactly one line: "
        "SHIP. If it has concrete problems - a bug, a missing requirement, code "
        "that does not run, a file that is empty or stubbed - answer with "
        "ISSUES: followed by a numbered list, each item a specific, fixable "
        "instruction (name the file and the exact change). Do not redo the work "
        "yourself and do not list style nits as blockers."
    ),
    "fixer": (
        "You are the FIXER on a small team. The reviewer found issues in the "
        "work. Apply every issue, one by one, with your tools, and verify each "
        "fix actually lands.\n\n"
        "Ship the CORRECTED result: real files, real changes, evidence that the "
        "issues are gone and the thing runs. If an issue is already fixed or "
        "does not apply, say so in one line and move on - do not relitigate it."
    ),
}


def role_prompt(role: str, task: str, artifacts: dict[str, str] | None = None) -> str:
    """The exact prompt a role receives: its directive + the task + prior artefacts.

    ``artifacts`` maps a stage name to that stage's output (e.g.
    ``{"plan": ..., "code": ...}``). Each is labelled so the model knows what it
    is consuming. The coordinator feeds each role only what came before it.
    """
    directive = ROLE_DIRECTIVES[role]
    parts = [directive, "### TASK", (task or "").strip()]
    for stage, text in (artifacts or {}).items():
        if text and text.strip():
            parts.extend([f"### {stage.upper()}", text.strip()])
    return "\n\n".join(parts).strip()


def pipeline(role: str, task: str, artifacts: dict[str, str]) -> str:
    """Back-compat alias: the prompt for ``role`` given the task and prior work."""
    return role_prompt(role, task, artifacts)
