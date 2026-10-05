---
name: security-review
title: Security Review
description: Scan a change for injection, secret leaks, unsafe input handling and broken access control before it ships.
when_to_use: handling user input, database queries, file paths, shell commands, auth, secrets, or anything parsing untrusted data
version: 1.0.0
---

# Security Review

Review the change for the classes of bug that turn into incidents, not for a
generic "is this secure" feeling. Each item below is a concrete pattern you can
grep for in the diff.

## Injection

- **SQL** - is any query built by concatenation or `.format()` with a value that
  came from outside? Parameterise it. Table and column names cannot be
  parameterised, so an interpolated identifier is still injection: allow-list it.
- **Shell** - `shell=True`, `os.system`, `subprocess` with a string, backticks,
  `Runtime.exec`, `child_process.exec`. Prefer an argument list with
  `shell=False`. Never pass user text through a shell.
- **HTML/template** - unescaped output, `|safe`, `innerHTML`, `dangerouslySetInnerHTML`,
  `v-html`. If it must be raw, sanitise with a real library, never a regex.
- **Path** - a path taken from input and joined onto a base. `../` escapes the
  base even after `.replace("..", "")` (try `....//`). Resolve the real path and
  confirm it is still inside the base directory.
- **Deserialisation / eval** - `pickle`, `yaml.load` (not `safe_load`), `eval`,
  `exec`, `Function()`, `new Function`. Untrusted input here is remote code
  execution.
- **Template injection** - user text rendered through a template engine that
  evaluates expressions (Jinja `{{ }}`, Handlebars, Velocity).

## Secrets

- No key, token, password or connection string in source, in a test, in a
  comment, in a sample config, or in a commit message.
- A secret file must be in `.gitignore` **and** untracked. Check it is not
  already in history - `git log -p -- <file>` - and never echo the value into
  logs, errors, or an API response.
- Compare secrets with a constant-time comparison where a timing oracle matters.

## Access control

- Every new route or endpoint: is it authenticated, and is it authorised for
  *this* object? An ID from the request must be checked against the caller, not
  merely fetched. `/api/orders/1234` must verify 1234 belongs to the caller.
- A check on the page is not a check on the API. Server side, always.
- Mass assignment: never bind a request body straight onto a model that has
  privileged fields (`is_admin`, `role`, `balance`). Allow-list the fields.

## Input handling

- Validate on the server. Client-side validation is a convenience for honest
  users only.
- Bound every input: length, count, numeric range. An unbounded loop over
  user-controlled size is a denial of service.
- Reject unexpected types. A JSON object where a string was expected should be an
  error, not a coercion.
- Uploads: check the extension **and** the magic bytes, store outside the web
  root, and never trust the supplied filename.

## Cryptography and transport

- No MD5/SHA1 for passwords or signatures. No custom crypto. No random from a
  non-cryptographic source for tokens - use the platform's secure random.
- HTTPS everywhere, and certificate verification ON. `verify=False` is not a fix
  for a certificate error.
- Cookies that carry a session need `HttpOnly`, `Secure`, and `SameSite`.

## Output

Report only what is exploitable, in this shape:

```
path/to/file.py:123  [critical|high|medium]  <class of issue>
Attack: <the concrete input or request that exploits it>
Fix: <the specific change>
```

If a category is clean, do not mention it. If the change handles no untrusted
input, say so in one line - a short review of a safe diff is correct, and an
inflated one trains people to ignore you.
