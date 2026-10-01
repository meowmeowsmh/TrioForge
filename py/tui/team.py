"""Senior/junior collaboration.

Auto-route picks ONE model per message. This is the other thing: the local model
(the **junior**) actually does the work while the cloud model (the **senior**)
watches, questions, corrects and — when the junior has no idea — takes over. They
iterate, and the result is the improved work, not a hand-off.

The division of labour, as described:

* both know the answer   -> the senior guides the junior to make the code better
* neither gets there     -> the senior takes control and does it
* the junior is stuck    -> the senior takes over
* the junior gets it     -> the senior approves it and it ships

Only the policy lives here (who speaks, what the senior is asked, how its reply
is read), so it can be tested without a model, a GPU or a network.
"""

from __future__ import annotations

#: How many times the junior may be sent back with feedback before the senior
#: does the job itself. Two is enough to fix a wrong path or a missing step
#: without paying for an argument.
MAX_ROUNDS = 2

SENIOR_SYSTEM = """\
You are the SENIOR engineer. A junior model (small, local, fast but weak) was \
given a task. It has real tools - ls, view, write, edit, bash, grep, glob - and \
was expected to USE them and produce the finished work.

A reply that only asks the user for clarification, greets, restates the task, or \
says it needs more information is NOT work. It is a failure. Never approve it.

Answer with EXACTLY ONE of these, nothing else on the first line:

APPROVE
    the junior actually did the task: it read or changed real files, ran real \
commands, and produced a complete answer with evidence.

TAKEOVER
    the junior did nothing, only asked questions, guessed, or is wrong in a way \
    it cannot fix. You will do the task yourself, with your own tools.

FEEDBACK: <instruction>
    a specific instruction for the junior's next attempt: which tool to run, on \
    which path, and what to look for. "Ask the user" is never acceptable.

"""
def review_messages(task: str, answer: str):
    """The messages the senior receives: the task and the junior's attempt."""
    return [
        {"role": "system", "content": SENIOR_SYSTEM},
        {"role": "user", "content": "TASK:\n{}\n\nJUNIOR'S WORK:\n{}".format(
            (task or "").strip(), (answer or "").strip() or "(nothing - it produced no answer)")},
    ]


def verdict(reply: str):
    """Read the senior's reply -> ``(kind, detail)``.

    kind is ``"approve"``, ``"takeover"`` or ``"feedback"``. Anything that is not
    one of the two keywords is treated as feedback, because a senior that simply
    talks is guiding - and an empty reply must never block the work, so it
    approves.
    """
    text = (reply or "").strip()
    if not text:
        return "approve", ""
    head = text.split("\n", 1)[0].strip().upper()
    if head.startswith("APPROVE"):
        return "approve", ""
    if head.startswith("TAKEOVER"):
        return "takeover", ""
    if head.startswith("FEEDBACK"):
        detail = text.split(":", 1)[1].strip() if ":" in text else ""
        return "feedback", detail or text
    return "feedback", text


def junior_task(task: str, feedback: str, round_no: int) -> str:
    """What the junior is asked on round ``round_no`` (>1 includes the feedback)."""
    task = (task or "").strip()
    if round_no <= 1 or not (feedback or "").strip():
        return task
    return ("{}\n\n[SENIOR FEEDBACK - fix these before answering]\n{}".format(
        task, feedback.strip()))
