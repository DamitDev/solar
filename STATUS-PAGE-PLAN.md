# Plan: LLM Model Status SPA (`/status` on solar-control)

Status page for the LLM models provided by solar-host instances: a Svelte SPA
served by solar-control at `/status`.

**Scope of this plan: the foundation only** — app scaffold, serving modes,
build embedding, and repo/CI wiring. Page features and data model are
specified separately; the base is built so they can be added without
structural changes.

## Key facts from the codebase

- solar-control serves **no static files today** (`app/main.py` is JSON-only; no
  `/status` route exists).
- Model/host data already exists server-side:
  - `GET /api/models/availability` (`app/routes/management/models.py`) —
    aggregated per-host model view, but fetched **live** from each host (slow).
  - Redis-cached host/instance state + Socket.IO events (`host_status`,
    `instance_state`, `pull_progress`), with a single-source payload builder in
    `app/services/host_status.py`.
  - solar-host exposes `GET /models` (stored models) and `/instances` (runtime
    state: status, context size, capabilities, VRAM).
- **No Svelte anywhere** — `solar-webui` is React. This is a new Svelte app,
  not an extension of solar-webui.

## Architecture

New standalone app `apps/solar-status` (Svelte 5 + Vite + TS), next to
solar-control. It is built to static assets and **embedded into the
solar-control Docker image**, which serves it at `/status`. No new Docker
image for the SPA itself; solar-control remains the only runtime dependency.

### Serving modes (two separate cases)

**1. Development — SPA served separately.** `make dev-status` starts the Vite
dev server on its own port (e.g. 5174, alongside solar-control on 8000),
serving the app at its root (`/`) — **no proxying**. The SPA is passed an
**absolute** API base URL (`VITE_API_BASE_URL=http://localhost:8000`) and calls
the control API directly. This cross-origin browser call requires CORS on
solar-control (see Phase 1.3). solar-control runs **without** any SPA build
in this mode.

**2. solar-control run alone — SPA served by control.** When a built `dist/`
is present (notably inside the Docker image), solar-control serves it at
`/status` via `app.frontend()`. This is the production path; it also works
locally after a manual `pnpm --filter solar-status build`. The SPA is passed a
**relative** API base URL (`../api`, from `.env.production`), which resolves
against the page's own mount point to the same-origin `/api`.

**The SPA is mode-agnostic.** It has a single code path: it receives a base
URL string and appends endpoint paths (`${apiBase}/status`). Only the
injected value differs:

- **API base**: mandatory build-time env var `VITE_API_BASE_URL` — absolute URL
  in dev (`.env.development`), relative `../api` in the embedded build
  (`.env.production`); no baked-in code fallback, startup fails fast when it
  is missing. Overridable per build (e.g. Docker build arg).
- **Asset base**: conditional Vite `base` — `/` for the dev server,
  `/status/` for the production build (embedded under control's `/status`).
  The mount point is injected as a build-time `__BASE_PATH__` define
  (mirroring `paths.base`) and anchors the relative API base (see viability
  section; Vite's built-in `import.meta.env.BASE_URL` is `'./'` in this
  toolchain and not the mount point).

### Relative API base (`../api`) viability

Relative URL resolution is a **browser** behavior: `fetch()`/`EventSource()`
resolve against `document.baseURI` (≈ the current page URL) at call time — the
SPA's own knowledge of its mount point is not consulted. Document-relative
results:

- Page at `/status`, `/status/`, or one-level routes (`/status/help`) →
  `../api` correctly resolves to `/api` (the last path segment is stripped,
  then `..` pops one directory).
- Deeper routes (`/status/a/b`) or trailing-slash routes (`/status/help/`) →
  resolves to `/status/api` — wrong, because the current URL has extra
  directory levels.

**Solution: the SPA resolves the base itself, against its mount point.** Vite
injects the configured asset base as `import.meta.env.BASE_URL` (`/status/` in
the embedded build, `/` in dev) — the SPA knows its mount point at runtime and
resolves the injected relative base once at startup:

```ts
// vite.config.ts injects the mount point: define: { __BASE_PATH__: JSON.stringify(embedded ? '/status' : '/') }
const mount = new URL(__BASE_PATH__, location.origin);
const apiBase = new URL(import.meta.env.VITE_API_BASE_URL, mount); // mandatory, fails fast when missing
// embedded (.env.production: ../api): http://host/api  (any route depth)
// dev (.env.development: absolute base ignores the mount anchor)
```

With this anchor, resolution is independent of the active client-side route
and no `<base>` tag is needed. The SPA remains mode-agnostic: one code path,
only the injected base URL string differs.

Rationale for a standalone app (vs. inside `apps/solar-control`):

- Follows the repo's "one app, one toolchain, one set of gates" convention
  (`solar-webui`, `supernova-steps`).
- Own `test-solar-status` / `lint-solar-status` targets with CI isolation —
  Svelte lint failures don't block solar-control PRs, and control PRs don't
  pay a pnpm install.
- Keeps `app/` in solar-control purely Python (it is the setuptools package
  root; a nested Node project would be shipped inside the installed package).

## Phase 1 — Backend (solar-control)

1. **Aggregation endpoint**: a read-only `GET /api/status` JSON endpoint under
   `/api`, aggregating host/instance/model state from Redis-cached state + DB
   host rows (not live host fan-out — see Risks). Exact payload schema is a
   feature decision; define it when the page's requirements are specified.
2. **Static serving** (serving mode 2 only): FastAPI's built-in
   `app.frontend()` (available since 0.141.x, which solar-control pins) —
   register via an `APIRouter` included with prefix `/status`:
   `router.frontend("/", directory=dist, fallback="auto")`. `fallback="auto"`
   serves `index.html` for client-side-routed navigations while missing assets
   still 404 — no hand-written SPA catch-all needed. Regular path operations
   always take precedence, so `/api` and `/v1` are safe.
   **dist/ lookup**: new optional config setting `STATUS_DIST_DIR`
   (`app/config.py`), default resolved relative to the solar-control package
   pointing at the sibling SPA checkout (`apps/solar-status/dist`), Docker
   sets `STATUS_DIST_DIR=/app/status-ui` explicitly. Register the frontend
   router **only if the directory exists** at startup — without a build,
   `/status` 404s and control starts/tests fine without Node (avoid
   `check_dir=False`, which 500s at request time instead).
3. **CORS (for dev mode)**: because the dev SPA calls control cross-origin
   (no proxy), solar-control needs a configurable CORS policy — allow-listed
   origins via env (e.g. `CORS_ORIGINS`, default empty = disabled). Production
   is same-origin and unaffected; the dev Vite origin is added locally.

## Phase 2 — Frontend (`apps/solar-status`)

1. Scaffold Svelte 5 + Vite + TS; add `apps/solar-status` to
   `pnpm-workspace.yaml` (members are exact paths — `apps/solar-webui` is
   listed explicitly). Conditional Vite `base`: `/` for the dev server,
   `/status/` for the production build (embedded under control's `/status`).
2. Data layer: a typed fetch client that resolves the API base URL once at
   startup (mode-agnostic — see the viability section above). Endpoint paths
   are appended to the resolved base.
3. Tailwind for styling consistency with solar-webui; vitest +
   `@testing-library/svelte` for tests.
4. **Dev server** (serving mode 1): Vite dev server on its own port, app
   served at `/`, **no proxy** — the SPA calls solar-control directly on the
   absolute `VITE_API_BASE_URL` (default `http://localhost:8000` in
   `.env.development`); CORS on control must include the dev origin
   (Phase 1.3).

## Phase 3 — Build / CI / repo wiring

Standard new-app checklist from AGENTS.md:

1. Makefile: `dev-status`, `test-solar-status`, `lint-solar-status` (target
   names must match CI matrix app names); wire into aggregate `test`/`lint`.
2. CI: `ci.yaml` dispatcher case for `apps/solar-status/*` + `ALL=1` list.
   Also map solar-control's status route/schema paths (`app/routes/management`
   status files, `app/services/host_status.py`) to additionally trigger the
   `solar-status` gates, so API contract changes run the SPA's tests too.
3. `quality-gates.yaml`: new install branch for `solar-status` (pnpm +
   Node 24, mirroring the solar-webui branch).
4. AGENTS.md: add `apps/solar-status` to the structure and commands sections
   (user-facing README in Hungarian per conventions).
5. solar-control Dockerfile: add a Node build stage that runs the SPA build
   (`pnpm --filter solar-status build` → `apps/solar-status/dist/`; repo-root
   build context for lockfile access, like solar-webui), and copy `dist/`
   into the runtime image at a fixed path (e.g. `/app/status-ui`) with
   `ENV STATUS_DIST_DIR=/app/status-ui`. `.github/release-manifest.json`:
   solar-control likely needs `context: "."` for this; no new component for
   the SPA itself.

## Phase 4 — Verification

- `make lint-solar-status test-solar-status`; solar-control unit tests for the
  new endpoints (mock Redis stores); full `make test lint`.
- Serving mode 1 (dev): `make dev-control` + `make dev-status`, open
  `http://localhost:5174/` — the SPA calls control directly on the absolute
  `VITE_API_BASE_URL` (CORS allow-list must include the dev origin).
- Serving mode 2 (embedded): build the SPA, run `make dev-control` alone, open
  `http://localhost:8000/status` — control serves the built assets directly.

## Decisions to confirm

1. **Auth**: public read-only page vs API-key gate? Page leaks infra details —
   recommend optional key via env (mirroring `SOLAR_WEBUI_AUTH_KEY`).

Feature decisions (payload schema, realtime updates, page contents) are
intentionally out of scope here and follow once the foundation is in place.

## Risks

- **Cross-boundary contract**: `/api/status` lives in solar-control, the UI in
  solar-status. Path-based CI dispatch won't couple them automatically — hence
  the extra dispatcher mapping in Phase 3.2. Without it, a schema change can
  break the SPA silently.
- solar-control's Dockerfile gains a Node build stage and its release-manifest
  entry likely needs `context: "."` — wider build context; verify image size /
  `.dockerignore` implications.
- Direct browser→control calls in dev require CORS on solar-control — new
  middleware plus an env knob; keep it disabled by default so the
  same-origin production path is unaffected.
- `/status` path collision check: none today; `app.frontend()` gives priority
  to regular path operations anyway, but keep API routes prefixed
  (`/api`, `/v1`) for clarity.
- Live fan-out availability endpoint is slow; the status page should rely on
  cached/pushed state.
