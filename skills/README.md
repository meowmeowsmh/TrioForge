# TrioForge Skills

A **skill** is Markdown that teaches the AI how to do one job well. No Python, no
build step, no registration — drop a folder in here and it exists.

```
skills/
    frontend-design/
        SKILL.md
    my-quick-note.md
```

Both shapes work. A folder with a `SKILL.md` is the normal form; a loose `.md`
file is fine for something short. `README.md` is ignored.

## The format

```markdown
---
name: my-skill
description: One line — this is what the AI always sees.
when_to_use: reviewing code, writing tests, anything about money
---

# My Skill

The actual instructions. Anything goes: rules, checklists, examples,
things to never do, an output format to follow.
```

Everything between the `---` markers is the header. Only three keys matter:

| Key | Required | What it does |
|---|---|---|
| `name` | no | The id the AI calls. Defaults to the folder or file name. |
| `description` | no | Shown to the AI at all times. Defaults to the first paragraph. |
| `when_to_use` | no | Extra hint for when this skill applies. |

`title` and `version` are also read if you want them, for display in the panel.

A file with **no header at all** still works — its first heading becomes the
title and its first paragraph the description.

## How it reaches the model

This is the part worth understanding, because it is why you can install thirty of
these without wrecking your context window.

1. Every skill's **name and description** go into the system prompt. That list is
   cheap — a few hundred tokens for a whole shelf of skills.
2. The **body is not loaded**. It stays on disk.
3. When a task matches, the model calls the `use_skill` tool with the skill name
   and the body arrives at that moment.

So the cost is paid only for the one skill the model actually chose, and adding
a thirty-first skill costs one line, not a page. `list_skills` lets it search the
list when it is not sure.

## Writing one that works

- **Write `when_to_use` for the model, not for a human.** "reviewing a diff,
  checking work before it ships" beats "code review related tasks".
- **Say what *not* to do.** The most useful line in `skills/code-review` is the
  list of things that are not findings. Negative constraints are what change
  behaviour.
- **Give an output shape.** If you want findings in a specific format, show the
  format. Otherwise you get prose.
- **Keep it under a page.** A skill is instructions, not documentation. If it
  needs chapters, it is a document — put it somewhere the AI reads on request.
- **One job per skill.** "Frontend design and also deployment" is two skills.

## Managing them

- **📚 Skills** in the app lists everything, shows each body on demand, and has
  **↻ Reload from disk** — so you can write a skill in your editor and see it
  without restarting.
- `GET /api/skills` · `POST /api/skills/reload` · `GET /api/skills/<name>`

## What ships in the box

| Skill | Use it for |
|---|---|
| `frontend-design` | Building any UI, so it doesn't look AI-generated |
| `code-review` | Reviewing a diff, with confidence filtering |
| `debugging` | Finding a root cause instead of guessing at fixes |
| `tdd` | Red-green-refactor, and writing tests that actually hold |
| `security-review` | Injection, secrets, path traversal, access control |
| `commit` | Staging, message quality, and only when asked |

They are ordinary files — read them, edit them, delete the ones you disagree
with. That is the point.
