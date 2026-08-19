# Cursor Proxy (/cursor/v1) — Spec

**Issue:** S-059 · **Repo:** solar-control · **Status:** implemented (2026-08)

## 1. Overview

Solar Control exposes an OpenAI-compatible /v1 gateway that transparently
routes to hosted model instances. Cursor (the IDE) cannot control DeepSeek
reasoning effort on custom endpoints, hides `reasoning_content` from the
UI, and omits it from multi-round tool-call history. This spec adds a
compatibility endpoint, `/cursor/v1`, that adapts Cursor traffic for
DeepSeek thinking-mode models — integrating the logic of
[yxlao/deepseek-cursor-proxy](https://github.com/yxlao/deepseek-cursor-proxy)
(MIT) into solar-control instead of running per-user local proxies with
ngrok tunnels.

## 2. Model surface

Four Cursor-facing aliases, all mapping to the single upstream model
`deepseek-v4-flash:284b`:

| Alias | reasoning_effort |
|---|---|
| `deepseek-v4-flash:max` | `max` |
| `deepseek-v4-flash:high` | `high` |
| `krumpli:max` | `max` |
| `krumpli:high` | `high` |

`/cursor/v1/models` advertises only these four entries with the upstream
instance's `max_model_len` (1M on the served flash instance, read from the
live registry when available). Any other model name in a request is an
OpenAI-shaped 404 (`model_not_found`).

## 3. Authentication and telemetry (single-key trust model)

- `/cursor/v1/*` is authenticated by the existing auth middleware against
  the `api_keys` table (same resolution + Redis caching as `/v1/*`).
- The **management API key is rejected** on `/cursor/v1` — only tenant
  endpoint keys are accepted, so admins control which endpoints/users may
  consume the flash model through their existing endpoint model scoping.
- The proxy performs a **self-HTTP call** to solar-control's own `/v1`
  gateway (`CURSOR_UPSTREAM_BASE_URL`, default `http://127.0.0.1:8015/v1`)
  forwarding the caller's `Authorization` header **verbatim**. Solar
  Control resolves the key again on the /v1 hop (Redis-cached), applies
  the endpoint's model patterns (an endpoint that cannot serve
  `deepseek-v4-flash:284b` receives `model_not_found`), and attributes
  gateway telemetry (usage, request logs, per-endpoint stats) to that
  endpoint.
- The proxy itself holds no credentials anywhere — no DB table, no
  webui-administered key.

## 4. Request/response transformation

Ported from deepseek-cursor-proxy (async-adapted):

- Normalize messages: `functions`/`function_call` → `tools`/`tool_choice`,
  flatten multi-part content, strip Cursor thinking `<details>` blocks.
- Inject `thinking: {type: enabled}` and
  `reasoning_effort: <alias effort>` (normalized: low/medium/high → high,
  max/xhigh → max) into the upstream payload.
- Convert `max_completion_tokens` → `max_tokens`; force
  `stream_options.include_usage`.
- Multi-round tool-call reasoning repair: assistant turns that
  Cursor sends without `reasoning_content` are looked up in the cache by
  conversation-scope keys and patched back; unrecoverable histories are
  trimmed with a recovery notice (mirroring the upstream proxy's
  `missing_reasoning_strategy: recover`).
- Response rewriting (non-stream and streamed SSE):
  - `reasoning_content` mirrored into Cursor-visible collapsible
    `<details><summary>Thinking</summary>…</details>` blocks
    (`display_reasoning`, `collapsible_reasoning`)
  - `model` in every response chunk rewritten back to the requested alias
  - assistant reasoning recorded into the cache for the next tool round
- Upstream errors pass through untouched with their original status and
  OpenAI error body; streaming errors are emitted as SSE `data:` events.

## 5. Reasoning cache (multi-replica safe)

- Backend: Redis (shared across solar-control replicas), never a per-pod
  file. Store class: `app.cursor_proxy.reasoning_store.ReasoningStore`.
- Keys: `solar:cursor:reasoning:<derived key>`; an insertion-order sorted
  set `solar:cursor:reasoning:index` enforces `max_rows`; TTL
  (`max_age_seconds`, default 30 days) is set per key.
- Scope math (conversation scope, turn-context signature, tool-call
  signatures) is ported verbatim from the upstream. The cache namespace
  includes a hash of the caller's Authorization header, isolating
  conversations per user.

## 6. Configuration (env)

All settings live in `app.config.Settings` (env / `.env`), no webui
surface:

| Setting | Default | Meaning |
|---|---|---|
| `cursor_upstream_base_url` | `http://127.0.0.1:8015/v1` | self-HTTP /v1 target |
| `cursor_upstream_model` | `deepseek-v4-flash:284b` | upstream model |
| `cursor_thinking` | `enabled` | thinking mode |
| `cursor_reasoning_effort` | `max` | fallback effort |
| `cursor_request_timeout_s` | `300` | upstream timeout |
| `cursor_max_request_body_bytes` | 20 MiB | request size cap (413) |
| `cursor_missing_reasoning_strategy` | `recover` | repair or reject |
| `cursor_reasoning_cache_max_age_s` | 30 days | cache TTL |
| `cursor_reasoning_cache_max_rows` | 100 000 | cache cap |
| `cursor_display_reasoning` | `true` | mirror thinking into UI |
| `cursor_collapsible_reasoning` | `true` | `<details>` blocks |

## 7. File map (solar-control)

- `app/cursor_proxy/` — ported package: `transform.py`, `streaming.py`,
  `reasoning_store.py` (Redis), `aliases.py`, `config.py`, `service.py`
  (aiohttp self-HTTP), `logging.py`
- `app/routes/cursor.py` — `/cursor/v1` router (models + chat/completions)
- `app/auth.py` — `/cursor/v1/` joins the api_keys auth path; management
  key rejected there
- `app/main.py` — router inclusion, OpenAI error shape for `/cursor/v1`,
  session close in lifespan
- `app/config.py` — env settings above
- Upgrade note: no DB migration required (no new tables).

## 8. Operational notes

- The path rides the existing ingress as `/cursor/v1` on
  `solar-api.damit.cloud`.
- Deployment is a plain solar-control image bump; no chart/values change
  unless a custom `CURSOR_UPSTREAM_BASE_URL` (non-default port) is needed.
- Client setup in Cursor: base URL `https://solar-api.damit.cloud/cursor/v1`,
  model = any of the four aliases, API key = the user's endpoint key.