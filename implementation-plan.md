# Implementation Plan: Solar Status Page

Branch: `ralph/status-page`
Source: `prd.json` (8 user stories, implemented in priority order)

## Overview

A public status page for `solar-control`. It shows current availability and 90-day uptime for each virtual model, alias (`core`, `wind`) and non-managed instance. Maintainers can post status messages that appear and expire automatically.

The work has four parts:

- **Data layer.** A history table and a sampler that records instance states over time.
- **API.** A public status summary and message management.
- **UI.** The `solar-status` SPA.
- **Housekeeping.** A retention job.

**Out of scope:**

- Notifications and subscriptions.
- Incident post-mortems.
- A per-endpoint metrics UI.
- Changes to gateway routing or the existing `/webui` dashboard.
- Programmatic message control.

**Access model:**

- Reads are public and unauthenticated.
- Writes sit behind the existing management key (`X-API-Key` / Bearer).



## Summary


| ID     | Story                                     | Area          | Depends on     |
| ------ | ----------------------------------------- | ------------- | -------------- |
| US-001 | State-history table and data access layer | Backend / DB  | None           |
| US-002 | Instance-state sampler loop               | Backend       | US-001         |
| US-003 | Public status summary endpoint            | Backend / API | US-001, US-002 |
| US-004 | Status messages table and CRUD            | Backend / API | None           |
| US-005 | Automatic message visibility window       | Backend / API | US-003, US-004 |
| US-006 | Status page: components and uptime bars   | Frontend      | US-003         |
| US-007 | Status page: active/upcoming messages     | Frontend      | US-005, US-006 |
| US-008 | 5-year retention cleanup job              | Backend       | US-001         |


All stories also require `make test-solar-control` and `make lint-solar-control` to pass. The frontend stories use `make test-solar-status` and `make lint-solar-status` instead.

---



## US-001: State-history table and data access layer

*As a developer, I need a Postgres table that logs instance states over time, so availability and uptime can be computed even without request traffic.*

- Add a new SQLAlchemy row in `app/database/tables.py`. Columns: `id`, `alias`, `instance_id`, `host_id`, `state`, `sampled_at` (timestamptz).
- Add indexes for two query patterns:
  - Filtering by alias or instance over a 90-day `sampled_at` window.
  - Cleanup by `sampled_at`.
- Generate an Alembic migration in `app/database/migrations/versions/`. `alembic upgrade head` must succeed.
- Add a `StateHistoryDB` class in `app/database/` with batch insert and windowed-read helpers. Follow the `*DB` singleton pattern (e.g. `virtual_model_db`).



## US-002: Instance-state sampler background loop

*As a developer, I need a periodic sampler that records every registered instance's state, so history accumulates continuously, including during outages with no requests.*

- Model the sampler on the `Reconciler` in `app/services/reconciliation.py`:
  - `start()` calls `asyncio.create_task`.
  - `stop()` cancels and awaits the task.
  - The sleep is an `asyncio.Event` wait with a timeout.
- Start and stop it in the `app/main.py` lifespan, next to the existing background jobs.
- Each tick does the following:
  - Read `RegistryStore.get_registry` and the WS-first host snapshots (`solar:hosts:snapshots`, with freshness from `entry_age_s`).
  - Write one row per instance: alias, instance_id, host_id, state, sampled_at.
- State comes from the host snapshot health/status (e.g. running/offline). Alias comes from registry membership.
- Stale or missing snapshots must not crash the loop.
- The sampling interval is configurable in `app/config.py` `Settings`.
- Unit tests cover:
  - One row per instance per tick.
  - State values.
  - An empty registry.
  - Stale snapshots.



## US-003: Public status summary endpoint

*As a visitor, I want a public endpoint with current availability and 90-day uptime per component, so the status page renders without authentication.*

- `GET /api/status` in `app/routes/management/status.py` returns the summary JSON. It replaces the current 501 placeholder.
- Add a narrow auth exempttion for this one route in `app/auth.py`. All other `/api/*` routes still require the management key.
- The response covers three kinds of component, each with current availability from instance states:
  - **Virtual models.** Includes ordered targets and `target_status` (`satisfied` / `violating: <reason>` / `missing`), reusing the computation on `VirtualModelResponse.target_status`.
  - **Aliases.** `core` and `wind`.
  - **Non-managed instances.**
- 90-day uptime per component:
  - Computed from state-history samples in 1-day buckets, using the `resolve_bucket` / `fill_buckets` ladder in `app/database/logs.py`.
  - Gaps are zero-filled and never interpolated.
  - It must not use `gateway_requests`, so a requestless outage still counts as downtime.
- Intent-managed model groups also show their reconciliation phase (`ready` / `degraded` / `failed`), taken from the intents table.
- Unit tests cover uptime math (gap-free series, outages) and the response shape.
- *Note:* the endpoint is public by default. If model-name disclosure becomes a concern, the documented fallback is a keyed-detail variant.



## US-004: Status messages table and management CRUD

*As a maintainer, I want to create, list, update and delete status messages through the management API, so I can communicate incidents and maintenance.*

- Add a `status_messages` table and an Alembic migration. Fields:
  - `body`
  - `type`: `incident` | `maintenance` | `planned maintenance` | `resolution`
  - `start_time`
  - `end_time` (optional, open-ended)
  - `created_at`
- Add CRUD routes under the `/api` management router, registered with `include_router` in `app/routes/management/__init__.py`. Follow the virtual-models CRUD pattern:
  - `POST` returns 201.
  - List, `PUT` and `DELETE` are included.
- Auth comes from the existing middleware. Do not add per-route dependencies.
- Validation:
  - `type` must be in the allowed set.
  - `start_time` must not be after `end_time` when both are set.
- Unit tests cover the CRUD happy path and validation errors.
- *Note:* messages are always written by a maintainer, with no programmatic control.



## US-005: Automatic message visibility window

*As a visitor, I want messages to appear and expire on their own based on start and end times, so maintainers never publish or unpublish by hand.*

- The public, unauthenticated message read is part of the `GET /api/status` response. It returns active messages, where `start_time <= now < end_time`.
- Messages with a past `end_time` are filtered out automatically, for all types.
- **Planned-maintenance** messages with a future `start_time` are exposed as *upcoming*, so a weekend maintenance can be announced ahead of time.
- Other types (incident, maintenance, resolution) follow the strict window. They are hidden while their `start_time` is in the future.
- There is no publish or unpublish state. Visibility depends only on `start_time` and `end_time`.
- Unit tests cover the boundary filtering for each message type.
- *Note:* `plan.md` says future-start messages are filtered but also wants upcoming maintenance shown. The PRD resolves this by letting only planned maintenance surface early.



## US-006: Status page: component status and 90-day uptime bars

*As a visitor, I want to see each component's current status and 90-day history at a glance.*

- Replace `solar-status` `+page.svelte` with the status page. It fetches `GET /api/status` through the existing `fetchJson` client (`src/lib/api/client.ts`).
- Component list:
  - Covers virtual models, aliases (`core`, `wind`) and non-managed instances.
  - Each has a colored status indicator: operational, degraded or down.
  - Intent phase is shown where available.
- 90-day uptime bar per component:
  - One bar per 1-day bucket.
  - Outage days render as zero or failure-colored gaps, never interpolated.
- Show the 90-day uptime percentage for each component.
- Handle loading, API-unreachable and empty states. No management key is needed.
- Tests use vitest and `@testing-library/svelte`. `pnpm --filter solar-status check` must pass.
- Verify both serving modes:
  - Cross-origin dev: `make dev-status`.
  - Embedded: `pnpm --filter solar-status build`, then `make dev-control`, then open `/status`.



## US-007: Status page: active and upcoming messages

*As a visitor, I want to see active incidents and upcoming maintenance, so I know about current problems and planned downtime.*

- A messages section renders active messages with body, type and start/end window.
- Planned-maintenance messages before their `start_time` render in a separate *upcoming* section.
- Type-styled badges distinguish incident, maintenance, planned maintenance and resolution.
- Show an empty state when there are no messages.
- Component tests cover message rendering and the empty state. `pnpm --filter solar-status check` and `make test-solar-status` must pass.



## US-008: 5-year retention cleanup job

*As a developer, I need a daily cleanup job that deletes history older than 5 years, so the table stays bounded.*

- Add a daily background job in the `app/main.py` lifespan, following the existing background-loop pattern. It deletes state-history rows with `sampled_at` older than 5 years.
- The retention window and run interval are configurable in `app/config.py` `Settings`.
- A unit test covers the boundary. Rows just inside the 5-year window are kept. Rows beyond it are deleted.

---



## Suggested order and checkpoints

1. **Data foundation (US-001, US-002).** Table, migration, `StateHistoryDB` and sampler. After this, history accumulates.
2. **Read API (US-003).** The public summary endpoint with uptime and intent phases.
3. **Messaging (US-004, US-005).** CRUD and the visibility window. Messages are folded into `/api/status`.
4. **UI (US-006, US-007).** Component list, uptime bars and the messages section. Verify both serving modes.
5. **Housekeeping (US-008).** Retention job. It can be done any time after US-001.

Every story is currently marked `passes: false`.