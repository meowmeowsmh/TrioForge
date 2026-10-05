---
name: api-design
title: API Design
description: Design a REST/HTTP API that is consistent, predictable and hard to misuse.
when_to_use: designing or reviewing an endpoint, a route, a schema, a public API, or an integration contract
triggers: api, endpoint, route, rest, schema, http, design an api, integration, webhook
version: 1.0.0
---

# API Design

An API is a promise. The cost of a bad one is paid every single day by everyone
who has to call it. Design for the caller, not for the convenience of the
implementation.

## The core rules

- **Resources are nouns, never verbs.** `/orders`, not `/getOrders`. The HTTP
  method is the verb: `GET /orders`, `POST /orders`, `PATCH /orders/42`.
- **Plural nouns, consistently.** `/orders/42/items`, not `/order/42/item`.
- **One resource per endpoint.** If a call does two things, it is two endpoints
  or one compound resource you named badly.
- **Errors are data, not prose.** `{ "error": {"code": "out_of_stock",
  "message": "…", "details": …} }`. A machine-readable `code` plus a
  human-readable `message`. Use the right HTTP status, always - do not return
  `200` with `"error": true`.
- **Use the right status codes.** `200`/`201` success, `204` no content,
  `400` bad request, `401` unauthenticated, `403` forbidden, `404` missing,
  `409` conflict, `422` unprocessable (semantic), `429` rate limited. `500`
  means *our* bug, and should carry no details.
- **Never change a released field's meaning.** Adding a field is fine. Renaming
  or repurposing one is a break. Version only when you must (`/v2/`), and
  remember: most "v2" rewrites would have been avoided by designing v1 carefully.
- **IDs are strings.** Even if they are numeric today. A number that later
  becomes a UUID, or exceeds 2^53 in JSON, is a bug waiting.
- **Timestamps are ISO-8601 UTC** (`2026-10-06T02:00:00Z`). Not epoch, not
  locale strings.
- **Pagination from day one.** `?limit=&cursor=` (or `?page=`), with a field
  that tells the caller there is more. Every list endpoint. Adding pagination
  later is a breaking change.

## The tests that expose a bad design

- **The rename test:** can the caller predict every field name without reading
  the docs? If not, the names are inconsistent.
- **The "one call" test:** to do one real task, how many round trips? If the
  answer is five, the API serves the schema, not the user.
- **The misuse test:** is the easiest way to use it the right way? A flag whose
  default is dangerous fails this.
- **The surprise test:** would a caller be surprised by what `DELETE` does, or
  what happens when a field is omitted? Surprise is a design defect.

## Output

Present: the endpoints with method + path, the request/response shape for each,
and the error codes. Then list any decision you are unsure about as a question
with a recommendation, rather than hiding it.
