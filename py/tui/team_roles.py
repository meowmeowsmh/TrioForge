"""Role-based team mode: a senior/junior pair works ONE task as a pipeline.

Parallel team mode (py/tui/team.py) runs every model in the pool on the same task
and ships the first answer. This is the OTHER team shape - a working team made of
TWO agents with different ranks, like ruflo's pair-programming (senior-dev /
junior-dev):

    SENIOR  (the strong model)      JUNIOR  (the fast model)
      - planner                       - coder
      - reviewer                      - fixer

    planner -> coder -> reviewer -> fixer (only when the review finds issues)

    - planner   (senior) reads the task and writes a concrete, ordered plan.
    - coder     (junior) executes the plan with tools - writes the real code,
                 asks in one line only when genuinely stuck, learns by doing.
    - reviewer  (senior) checks the result strictly, then ships it or lists issues.
    - fixer     (junior) applies those issues and ships the corrected work.

The coordinator (textual_app) owns the loop: it runs each job with the right agent
from the pool - the strong model plans and reviews, the fast model builds and
fixes - feeding each job the previous job's output. Only the DIRECTIVES live here,
so the pipeline can be tested with no model, no GPU and no network.
"""

from __future__ import annotations

#: The pipeline order. The coordinator runs these in sequence.
ROLE_ORDER = ("planner", "coder", "reviewer", "fixer")

#: Which AGENT each job belongs to. A "working team" is a senior/junior pair:
#: the senior is thorough and strict (plans + reviews), the junior is the fast
#: worker (codes + fixes) that learns by doing.
ROLE_AGENT = {
    "planner": "senior",
    "coder": "junior",
    "reviewer": "senior",
    "fixer": "junior",
}

#: One-line job descriptions (for /help and the status line).
ROLE_DESCRIPTIONS = {
    "planner": "senior: read the task, write a concrete plan",
    "coder": "junior: write the code, ask only when genuinely stuck",
    "reviewer": "senior: strict review - ship it or list concrete issues",
    "fixer": "junior: apply the issues, ship the corrected work",
}

#: What each role is asked, verbatim. Every role names its agent (senior/junior)
#: so the model knows its rank, and its output is an artefact the next role
#: consumes - never a question handed back to the user.
ROLE_DIRECTIVES = {
    "planner": (
        "You are the SENIOR on a two-person team, acting as the PLANNER. Read the "
        "task, then produce a concrete, ordered plan to finish it yourself.\n\n"
        "The plan must name real files and folders, list the tools to use at each "
        "step (view/grep/glob to read, write/edit to create or change, bash to run "
        "and verify), and state how each step's result is checked. Keep it short - "
        "a numbered list, not an essay. Do NOT start writing files yet and do not "
        "ask the user anything: your junior teammate executes the plan."
    ),
    "coder": (
        "You are the JUNIOR on a two-person team, acting as the CODER. Execute the "
        "plan you are given, now, with your tools: view/grep/glob to read, "
        "write/edit to create or change files, bash to run and verify.\n\n"
        "Produce the ACTUAL finished result - real files with real content, code "
        "that runs - not an empty folder, a list, a plan, or a description. Follow "
        "the plan step by step. If you are genuinely stuck, ask in ONE line what "
        "the senior should clarify, then keep going on the parts you can do - do "
        "not stop at a first guess and do not hand back a question instead of work."
    ),
    "reviewer": (
        "You are the SENIOR on a two-person team, acting as the REVIEWER. Read the "
        "task, the plan, and what the junior actually made. Judge only the RESULT "
        "against the task - strictly.\n\n"
        "If the work is done and correct, answer with exactly one line: "
        "SHIP. If it has concrete problems - a bug, a missing requirement, code "
        "that does not run, a file that is empty or stubbed - answer with "
        "ISSUES: followed by a numbered list, each item a specific, fixable "
        "instruction (name the file and the exact change). Do not redo the work "
        "yourself and do not list style nits as blockers."
    ),
    "fixer": (
        "You are the JUNIOR on a two-person team, acting as the FIXER. Your senior "
        "reviewer found issues in your work. Apply every issue, one by one, with "
        "your tools, and verify each fix actually lands.\n\n"
        "Ship the CORRECTED result: real files, real changes, evidence that the "
        "issues are gone and the thing runs. If an issue is already fixed or does "
        "not apply, say so in one line and move on - do not relitigate it."
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
