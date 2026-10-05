# TrioForge Plugins

Drop a single `.py` file here to extend TrioForge. Plugins are loaded at
startup and can add routes, register blueprints, or **give the agent tools it
can call**.

## Three ways to extend TrioForge

A single `.py` file is the most powerful option, and usually the wrong first
choice. There are three tiers — pick the cheapest one that does the job.

| | **Skill** | **MCP server** | **Plugin** (this folder) |
|---|---|---|---|
| What it is | Markdown instructions | Someone else's tool server | Python code |
| Lives in | `skills/<name>/SKILL.md` | 🧰 MCP Servers panel | `plugins/<name>.py` |
| You write | prose | nothing — one click | real code |
| Gives the AI | know-how | tools | tools + routes + UI |
| Best for | *how* to do a job well | Playwright, GitHub, docs, databases | first-class UX, OAuth, your own service |

**Reach down the table only when the tier above cannot do it.**

- **A skill** changes how the AI works — code review standards, frontend taste,
  test discipline. It is a prompt with a name. Most of what you would call an
  "AI plugin" is exactly this, and it needs no code at all. See `skills/README.md`.
- **An MCP server** gives the AI tools that already exist. Browser control, live
  docs, GitHub, Postgres, Figma — the same servers Claude and Cursor use. Do not
  rewrite these as plugins; connect to them. See the 🧰 panel.
- **A plugin** is for when you want something TrioForge-shaped: a one-click
  Google sign-in, a credential panel, an HTTP route of your own. That is why
  `plugins/gmail.py` is a plugin and not an MCP server.

All three end up in the same place: one tool list handed to the model. Skills
also contribute their description to the system prompt, so the model knows a
skill exists before it decides to load it.

## Anatomy of a plugin

```python
# my_plugin.py

MANIFEST = {
    "name": "my-plugin",           # required — unique id
    "title": "My Plugin",          # optional — display name
    "version": "1.0.0",
    "description": "What it does",
}

TOOLS = [                          # optional — what the AGENT may call
    {
        "type": "function",
        "function": {
            "name": "my_lookup",
            "description": "Look something up.",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
                "additionalProperties": False,
            },
        },
    },
]


def dispatch(tool_name, args):     # runs the tool the agent picked
    if tool_name == "my_lookup":
        return {"result": "..."}
    return {"error": "unknown tool"}


def register(app):                 # optional — add HTTP routes
    from flask import jsonify

    @app.route("/api/plugin/my-plugin/ping")
    def _ping():
        return jsonify({"ok": True})
```

- `MANIFEST` — names the plugin. Files starting with `_` are skipped.
- `TOOLS` + `dispatch()` — this is what makes it a **Connector**: the agent is
  handed those tool definitions and can call them, so "read my Gmail" becomes a
  real function call instead of a refusal. Any plugin with tools shows up in the
  **🔌 Connectors** panel automatically.
- `register(app)` — optional; only if the plugin needs HTTP routes (e.g. the
  OAuth endpoints Gmail uses).
- `credentials` inside `MANIFEST` — optional list of `{key, label, type,
  placeholder, hint}` fields; the Connectors panel renders them as inputs, so a
  connector never has to build its own UI.

## Loading

Plugins load automatically when the app starts. The UI lists them via
`GET /api/plugins`, and the tools via `GET /api/connectors`. A plugin that fails
to import or whose `register()` throws is **reported and skipped** — it never
crashes the app.

## Security

Plugins are trusted Python code that runs inside the app process with the
same permissions as TrioForge itself. **Only install plugins you trust** —
the same rule as browser extensions or VSCode extensions.

Credentials and tokens written by a connector are stored in
`json_configuration/` and are git-ignored — never commit them.

---

# Setting up the Gmail connector (the full walkthrough)

`plugins/gmail.py` lets the agent read your Gmail. The sign-in itself is one
button; the **one-time** part is creating the Google app it signs into. Expect
about 5 minutes, once, ever.

## Step 1 — Create the project

1. Go to **console.cloud.google.com**
2. Top-left **project selector → New project** → name it anything (e.g. `TrioForge`) → **Create**
3. **Note the project ID / number** — you will need to stay in this same project
   for every step below. (The number also appears at the start of your Client ID,
   e.g. `976694364481-cvq6…`.)

> ⚠️ **The #1 mistake:** doing a later step in a *different* project. If the
> Gmail API is enabled in project A but the OAuth client lives in project B, the
> sign-in fails with `403: Gmail API has not been used in project …`.

## Step 2 — Turn on the Gmail API

**APIs & Services → Library →** search **Gmail API** → open it → **ENABLE**.

Direct link (swap in your project number):

```
https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=YOUR_PROJECT_NUMBER
```

## Step 3 — Consent screen + add yourself as a test user

1. **Google Auth Platform → Branding** (older console: *OAuth consent screen*)
   - App name: `TrioForge` — this is what Google shows on the sign-in page
   - User support email + developer contact: your address
2. **Google Auth Platform → Audience**
   - Publishing status stays **Testing** — that is fine for personal use
   - **Test users → + Add users →** add your own Gmail address → **Save**

> ⚠️ **Skip this and you get** `403: access_denied — the app is currently being
> tested and can only be accessed by developer-approved testers`.

## Step 4 — Create the OAuth client

**APIs & Services → Credentials → + Create credentials → OAuth client ID**

- Application type: **Web application**
- Name: anything (e.g. `TrioForge desktop`)
- **Authorized redirect URIs → + ADD URI**, paste **exactly**:

```
https://127.0.0.1:5003/api/connectors/gmail/oauth2callback
```

- Click **CREATE**, then copy the **Client ID** and **Client secret**

> ⚠️ Common causes of `400: redirect_uri_mismatch`:
> `http://` instead of `https://` · `localhost` instead of `127.0.0.1` ·
> a trailing `/` · port other than `5003` · **forgetting to click Save**.
> Put it in **Authorized redirect URIs**, not "Authorized JavaScript origins".

## Step 5 — Paste them into TrioForge

**🔌 Connectors** → paste **Client ID** + **Client secret** → **💾 Save app**.

That is the last time anyone is asked. If a build ships the app in
`DEFAULT_CLIENT_ID` (or `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` env vars),
users never see these fields at all.

## Step 6 — Sign in

Click **🔗 Sign in with Google** → pick your account → **Continue** → the tab
says **"Gmail connected"**. Done.

Then just ask: *"summarize my unread emails"*.

## Troubleshooting

| What Google says | What it means | Fix |
|---|---|---|
| `Error 400: redirect_uri_mismatch` | The redirect URI on the client isn't an exact match | Add `https://127.0.0.1:5003/api/connectors/gmail/oauth2callback` to Authorized redirect URIs and **Save** |
| `Error 403: access_denied` … *developer-approved testers* | You're not on the test-user list | Audience → Test users → add your Gmail address |
| `Gmail API has not been used in project …` | API disabled, or enabled in another project | Enable **Gmail API** in the project that owns the OAuth client |
| `Error 403: … insufficientPermissions` | Signed in with a scope your token doesn't cover | 🔌 → Disconnect → Sign in with Google again |
| Signed in, but the panel still says Not connected | The Gmail API is still off | Enable it, then refresh the page — the saved token connects by itself |

**Note:** Google warns that OAuth changes "may take five minutes to a few hours
to take effect". If a fix seems ignored, wait five minutes and retry.
