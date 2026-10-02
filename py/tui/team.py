"""Senior/junior collaboration.

Auto-route picks ONE model per message. Team mode runs two at once: the local
model (the **junior**) and a cloud model (the **senior**) both start together on
the same task. The senior is fast and strong, so it is authoritative - its answer
ships the moment it is ready - while the junior keeps working offline and its
independent answer is appended as a note when it lands.

Both are real workers, not a brows-and-describes pair. The difference is only who
writes files: the junior runs read-only so two agents never clobber the same
paths, and the senior is the one that actually makes the changes. That is why the
junior is told to put its answer inline, while the senior is told to create and
run the real thing.

Only the directives live here (what each model is asked), so they can be tested
without a model, a GPU or a network.
"""

from __future__ import annotations

#: What the SENIOR is asked. It is authoritative and has write tools, so its one
#: shot must be the finished work: real files with real content, run and reported
#: - never an empty folder, a list, a plan, or a question handed back to the user.
SENIOR_TASK_DIRECTIVE = (
    "Do this task yourself now, with your tools: view/grep/glob to read, "
    "write/edit to create or change files, bash to run and verify. Produce the "
    "ACTUAL finished result - real files with real content, code that runs - not "
    "an empty folder, a list, a plan, or a description of what you would do. Do "
    "not ask the user for clarification and do not stop at a first guess: create "
    "the thing, run it, and report what you made with file paths and evidence.\n\n"
)

#: What the JUNIOR is asked. It runs read-only (no write tools), so the finished
#: work has to live in its answer text, not on disk.
JUNIOR_OPINION_DIRECTIVE = (
    "Answer this task yourself, now, from what you know. Give the COMPLETE answer "
    "inline - the actual code or content the task asks for, not a list of files "
    "or a description of how you would do it. Do not ask the user for "
    "clarification.\n\n"
)


def senior_task(task: str) -> str:
    """The task as the senior should see it: do the work, ship the result."""
    return SENIOR_TASK_DIRECTIVE + (task or "").strip()


def junior_opinion(task: str) -> str:
    """The task as the read-only junior should see it: answer fully, inline."""
    return JUNIOR_OPINION_DIRECTIVE + (task or "").strip()
