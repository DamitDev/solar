# Cursor Proxy (/cursor/v1) — Implementation Plan

| Field       | Value                                   |
|-------------|-----------------------------------------|
| Issues      | S-059                                   |
| Status      | Implemented (2026-08-19)                |
| Spec        | [docs/specs/cursor-proxy.md](../specs/cursor-proxy.md) |

## 0. Deliverable

`/cursor/v1` on solar-control: a single-component change (solar-control
only — no solar-host, no webui, no DB migration, no chart change).

## 1. Sequencing

1. **Spike** — probe the live gateway (`deepseek-v4-flash:284b`) for
   `reasoning_effort`/`thinking` acceptance, `reasoning_content` flow, and
   the missing-reasoning tool-call case. Result: effort honored (max vs
   high measurably different), `thinking` dict accepted, reasoning flows
   streamed and non-streamed, NO 400 on missing reasoning (SGLang does not
   enforce the DeepSeek-API pass-back rule).
2. **Port** — copy transform/streaming/reasoning modules from
   deepseek-cursor-proxy (repo at HEAD, local retek/krumpli patch NOT
   carried; the aliases are reimplemented natively).
3. **Adapt** — async store (Redis), async transform call sites, alias
   table + effort override, aiohttp self-HTTP service.
4. **Wire** — router, auth (/cursor/v1 joins api_keys path, management key
   rejected), main.py (router, error shape, session close).
5. **Tests** — aliases, store, transform (repair + rewrite), router, auth,
   service (key-forwarding invariant).
6. **Docs** — issue S-059, spec, this plan, ROADMAP milestone.
7. **Release** — lint + full solar-control suite; deploy via GitOps image
   bump on the Commander's go-ahead.

## 2. Key decisions (and why)

- **Port into solar-control, not a sidecar.** Same process, no extra
  deployment unit; the self-HTTP hop uses the pod's own /v1.
- **Single-key trust model** (design correction 2026-08-19): the proxy
  never holds a key; it forwards the caller's Authorization header to the
  /v1 hop, so telemetry lands on the caller's endpoint and endpoint model
  scoping gates access. The originally planned webui-administered upstream
  key and its DB table were dropped as over-engineered.
- **Redis reasoning cache.** solar-control is stateless with multiple
  replicas; SQLite per-pod would break the repair across replicas.
- **Alias → effort in the model name.** Cursor cannot set effort on custom
  models; two effort levels × two naming schemes (recognizable
  `deepseek-v4-flash:*` + unknown-name `krumpli:*` for Cursor's 1M
  fallback) cover both behaviors.

## 3. Pitfalls handled

- aiohttp (the codebase's client) used for the self-HTTP call; session
  closed in lifespan shutdown.
- Streaming mirrors the upstream handler exactly: `[DONE]` stores all
  reasoning, mid-stream exit stores partial reasoning, display adapter
  flush chunk before `[DONE]`.
- The repair test stores under the namespace `prepare_upstream_request`
  actually computes (`reasoning_cache_namespace`), not a hardcoded string.
- Fake redis shims needed `stop=-1` handled in `zrange` for `clear()`.
- Upstream errors pass through verbatim; stream-mode errors emit SSE
  `data:` events so Cursor stays in stream-parsing mode.

## 4. Verification

- 36 new unit tests across six files
  (`tests/test_cursor_{aliases,reasoning_store,transform,router,auth,service}.py`)
  including the key invariant: the self-HTTP call forwards the caller's
  Authorization header verbatim, and efforts `max`/`high` reach the
  upstream payload per alias.
- `make test-solar-control` full suite + `make lint-solar-control`.
- Post-deploy smoke: base URL check with an endpoint key, model list,
  streamed chat with details block, tool-call round sanity via curl.