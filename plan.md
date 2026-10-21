## Description

solar-control today has no frontend – only the OpenAI-compatible gateway (`/v1`, `/cursor/v1`) and the management API behind the management key, with dashboard state served over the Socket.IO `/webui` namespace. Model availability exists only as live Redis and management-API views; nothing is logged over time, so there is no uptime history, and there is no way for maintainers to communicate incidents or maintenance to users – the messaging feature every public status page (GitHub, Discord) has is missing entirely.

The task adds a basic public status page following the standard status-page pattern (component list, current status, 90-day uptime bars, active messages), plus the data layer it needs: periodic instance-state sampling into a new history table, availability computed from instance states for virtual models, aliases and non-managed instances, and a 5-year retention job. Maintainer messages are manual, created via the management API, with settable start and end times so a planned weekend maintenance can be published in advance and expire automatically. Reads are public and unauthenticated; only writes sit behind the existing management key.

## Goal

- **State-history table + sampler:** a new Postgres table logging instance states over time (alias, instance_id, host_id, state, sampled_at), written by a periodic sampler loop modeled after the reconciler background-loop pattern in `app/services/reconciliation.py`, started in `app/main.py` lifespan, reading from the Redis registry (`RegistryStore.get_registry`) and the WS-first host snapshots (`solar:hosts:snapshots`, `host_health`).
- **Status summary endpoint:** a public GET endpoint computing, per virtual model (with its ordered `targets` and `target_status` semantics), per alias (`core`, `wind`) and per non-managed instance, the current availability based on instance states, plus uptime percentage over the last 90 days using 1d buckets via the existing `resolve_bucket`/`fill_buckets` ladder in `app/database/logs.py` (outages render as zero-filled gaps, never interpolated over).
- **Status page:** a minimal static page served by FastAPI following the standard status-page layout – component status, 90-day uptime bars, active and upcoming maintenance messages.
- **Messaging:** a new `status_messages` table (body, type: incident / maintenance / planned maintenance / resolution, start_time, end_time or TTL, created_at) with CRUD under the management routes guarded by the existing management-key auth (`X-API-Key` / Bearer). Messages with a future start time or a past end time are filtered from the public read automatically.
- **Retention:** a daily cleanup job deleting history rows older than 5 years – keep the last 5 years of data, start deleting beyond that.
- **Tests:** sampler behavior, uptime math (gap-free series, outage handling), message start/end filtering, and the retention window.

- **Out of scope:** no notification or subscription features (email/webhook/push), no incident post-mortems, no per-endpoint metrics UI, no changes to gateway routing or the existing `/webui` dashboard, and no programmatic message control – messages are always written by a maintainer.

## Additional notes

- Alias grounding from the live tree and memory: `core` serves the GLM core instance (ai04), `wind` is the global alias to `qwen3.6:35b`; per-alias availability comes from the Redis registry plus host snapshot state, and virtual-model target health reuses the `satisfied` / `violating` / `missing` computation already on `VirtualModelResponse.target_status`.
- `gateway_requests` already carries per-request model/instance/status rows with indexes that query 90 days fine, but uptime must be computed from the state-history samples, not request logs – a requestless outage still has to count as downtime.
- Intent phases (`ready`, `degraded`, `failed`) should surface in the status summary so intent-managed model groups show their reconciliation health, not just availability.
- Public reads mean the summary endpoint and status page ship with no auth dependency – consistent with the gateway's own public surface; if model-name disclosure ever becomes a concern, the keyed-detail variant is the fallback shape.
