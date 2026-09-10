# Security Policy

## Reporting a Vulnerability
Please do **not** open a public issue. Email **limcherng1@gmail.com**
with a description, reproduction steps, and impact. You'll get an
acknowledgement within 48 hours.

We aim to ship a fix or mitigation within 30 days of confirming an
issue, and will credit you in the release notes unless you ask us not to.
If a fix will take longer, we'll say so and agree on a disclosure date.

TrioForge is maintained by a single developer — responses may be slower
around holidays, but every report gets a reply.

## Scope

**In scope (please report):**
- Escaping a workspace folder (path traversal via `read_file` / `write_file` / `edit_file`)
- `run_command` executing outside the configured workspace folder
- Prompt injection that leads to actions the user did not authorize
- Plugin code breaking out of the app process or reading data it shouldn't
- Remote access being reachable without the user enabling it
- API keys or file contents leaking into logs, errors, or the UI
- Authentication bypass (note: there is currently no auth — see below)

**Known limitations (by design, not vulnerabilities):**
- The coding agent can run arbitrary shell commands **inside** a
  folder you grant it access to. `run_command` is available even at
  `read` access — do not point a workspace at folders you don't trust.
- "Full computer access" lets the agent control the keyboard, mouse,
  and open apps. It is opt-in.
- **Remote access has no built-in authentication.** Binding to `0.0.0.0`
  exposes the app to your network. Do not expose it to the internet
  without putting an authenticating reverse proxy in front of it.
- Cloud providers (Claude, DeepSeek, Groq, Gemini, OpenRouter) receive
  the contents of any file the model reads. "Local only" applies to
  Ollama and llama.cpp.
- Plugins are trusted Python and run inside the app process.

## Supported Versions
The `main` branch is actively supported. There are no tagged releases yet.