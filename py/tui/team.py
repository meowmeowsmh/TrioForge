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

SENIOR_SYSTEM = (
    "You are the SENIOR engineer. A junior model (small, local, fast but weak) was "
    "given a task. It has real tools - ls, view, write, edit, bash, grep, glob - and "
    "was expected to USE them and produce the finished work.\n\n"
    "A reply that only asks the user for clarification, greets, restates the task, or "
    "says it needs more information is NOT work. It is a failure. Never approve it.\n\n"
    "Also judge WHAT the junior actually ran. If its tool activity does not make sense "
    "for the platform (e.g. it ran ls /etc/profile, ps aux or touched /usr/bin on a "
    "Windows machine), that is wasted work: send FEEDBACK naming the right commands, or "
    "TAKEOVER. The junior must not just sit there listing things - it must do the "
    "task.\n\n"
    "Answer with EXACTLY ONE of these, nothing else on the first line:\n\n"
    "APPROVE\n"
    "    the junior actually did the task: it read or changed real files, ran real "
    "commands, and produced a complete answer with evidence.\n\n"
    "TAKEOVER\n"
    "    the junior did nothing, only asked questions, guessed, or is wrong in a way "
    "it cannot fix. You will do the task yourself, with your own tools.\n\n"
    "FEEDBACK: <instruction>\n"
    "    a specific instruction for the junior's next attempt: which tool to run, on "
    "which path, and what to look for. \"Ask the user\" is never acceptable.\n\n"
)
def _step_line(step) -> str:
    """One line summarising a junior's tool call, so the senior sees WHAT it ran."""
    args = step.args or {}
    name = step.name or "tool"
    if name == "bash":
        return "bash: {}".format(str(args.get("command", "")).strip()[:120])
    if name in ("view", "write", "edit"):
        return "{}: {}".format(name, args.get("file_path", "?"))
    if name == "grep":
        return "grep: {!r}".format(args.get("pattern", "?"))
    if name == "glob":
        return "glob: {}".format(args.get("pattern", "?"))
    if name == "ls":
        return "ls: {}".format(args.get("path", "."))
    if name == "memory":
        return "memory: {}".format(args.get("action", "?"))
    return name


def review_messages(task: str, answer: str, steps=None):
    """The messages the senior receives: task, the junior's tool activity, its answer."""
    activity = ""
    if steps:
        activity = ("\n\nJUNIOR'S TOOL ACTIVITY (what it actually ran):\n"
                    + "\n".join("- {}".format(_step_line(s)) for s in steps))
    return [
        {"role": "system", "content": SENIOR_SYSTEM},
        {"role": "user", "content": "TASK:\n{}\n\nJUNIOR'S WORK:\n{}{}".format(
            (task or "").strip(), (answer or "").strip() or "(nothing - it produced no answer)", activity)},
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
    """What the junior is asked on round ``round_no`` (>1 includes the feedback).

    The directive up front is the difference between a junior that *does* the work
    and one that lists a few files and hands back a description. A small local
    model needs to be told to make the actual change, not to browse.
    """
    directive = ("Do this task yourself now, with your tools: view/grep/glob to "
                 "read, write/edit to change the code, bash to run it. Make the "
                 "actual change and run it - do NOT just list files or describe "
                 "what you would do. Report what you changed, with file paths and "
                 "evidence.\n\n")
    task = (task or "").strip()
    if round_no <= 1 or not (feedback or "").strip():
        return directive + task
    return (directive + task + "\n\n"
            "[SENIOR FEEDBACK - fix these before answering]\n{}".format(
                feedback.strip()))
