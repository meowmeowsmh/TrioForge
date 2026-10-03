"""Team mode: every model in the pool works the same task as a peer.

Auto-route picks ONE model per message. Team mode is the other thing: every
model in the routing pool starts at once on the same task, with the same tools
and the same directive. No senior, no junior, no read-only second opinion - a
model either does the job or it doesn't. The first finished answer ships; the
others keep going and their answers are appended as notes when they land.

This is the DeepSeek Harness idea in miniature: named providers, a request goes
to a peer, and the peers are equals. "Whatever AI is inside, do the job."

Only the directive lives here (what each model is asked), so it can be tested
without a model, a GPU or a network.
"""

from __future__ import annotations

#: What EVERY model is asked, verbatim. It has tools, so its one shot must be
#: the finished work: real files with real content, run and reported - never an
#: empty folder, a list, a plan, or a question handed back to the user.
TASK_DIRECTIVE = (
    "Do this task yourself now, with your tools: view/grep/glob to read, "
    "write/edit to create or change files, bash to run and verify. Produce the "
    "ACTUAL finished result - real files with real content, code that runs - not "
    "an empty folder, a list, a plan, or a description of what you would do. Do "
    "not ask the user for clarification and do not stop at a first guess: create "
    "the thing, run it, and report what you made with file paths and evidence.\n\n"
    "FOCUS: if the user names a place (e.g. \"on D:\" or \"in a new folder\"), "
    "build there and stay there. Do not wander: no enumerating every drive, no "
    "listing unrelated folders, no probing free space. Go straight to the named "
    "location, make the files, and report. On Windows the shell is cmd.exe - use "
    "dir/mkdir/echo, not PowerShell cmdlets (Select-Object, Format-Table).\n\n"
    "READ ONCE, THEN WRITE: read only enough to find the fix, then apply it with "
    "write/edit. Do not re-read a file you already saw this turn, and do not "
    "spend the whole turn investigating - the deliverable is the fixed file, not "
    "the investigation.\n\n"
)


def task_directive(task: str) -> str:
    """The task as any team model should see it: do the job, ship the result."""
    return TASK_DIRECTIVE + (task or "").strip()
