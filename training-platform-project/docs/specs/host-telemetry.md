# Host Telemetry Specification

| Field       | Value                          |
|-------------|--------------------------------|
| Issue       | S-050, S-051                   |
| Status      | Draft                          |
| Created     | 2026-08-06                     |
| Depends on  | S-041, S-044                   |
| Depended by | U-009                          |

## 1. Overview

Host-facing data arrives by **WebSocket push**; Redis is the control-side
read model; HTTP proxying is a **degraded fallback only**. This is the
canonical rule for every host data path in Solar Control:

1. The host pushes the data over the `/hosts` namespace whenever it
   changes (or on a fixed cadence).
2. Control stores the latest push in Redis under a per-host key.
3. API reads serve the Redis copy when the host is connected and the entry
   is fresh.
4. Only when the Redis copy is missing or stale does control proxy HTTP to
   the host (`snapshot_source: "http"`), and only when the host is
   unreachable does the response degrade (`snapshot_source: "none"` with an
   `error` string).

Rationale: control must not perform per-tick HTTP round-trips per host —
the reconciler observes every managed instance every tick, and proxying
made the resources view slow and fragile. The WS push costs one emit per
host per interval regardless of how many consumers exist.

## 2. Events

All events arrive on the `/hosts` namespace from the host's WS client;
control rebroadcasts user-facing events to `/webui`.

| Event | Direction | Payload | Notes |
|-------|-----------|---------|-------|
| `registration` | host → control | host identity + api key | session binding |
| `host_health` | host → control | `memory`, `gpu_type`, `roles`, disk summary, legacy `reservations` summary, **full `resources` snapshot** (C5) | every 10 s; the `resources` block is byte-identical to the host's `GET /resources` |
| `instances_update` | host → control | flat instance list (id/alias/status/port/backend_type/model_source/…) | on every instance change |
| `instance_state` | host → control | per-instance state (slots, prefill progress, …) | batched |
| `log_batch` | host → control | log lines | rebroadcast to `/webui` |
| `step_log` | host → control | job-step log lines | rebroadcast to `/webui` |
| `pull_progress` | host → control | `{host_id, host_name, timestamp, data}` with `data = {source_uri, phase, bytes_done, bytes_total, speed_bps, error?}` | C4; throttled while `downloading`, exactly one terminal `completed`/`failed` |
| job lifecycle | host → control | job create/update events | |
| `endpoints_update` | control → `/webui` | `{endpoints: [...]}` | C5; emitted on endpoint create/update |

## 3. Redis read models

| Key | Field | Value |
|-----|-------|-------|
| `solar:hosts:sids` | host id | socket id of the current session |
| `solar:hosts:connected` | host id | "1"/"0" |
| `solar:hosts:instances` | host id | flat instances array (see `instances_update`) |
| `solar:hosts:snapshots` | host id | `{"at": <iso8601>, "resources": {...}}` (C5) |
| `solar:hosts:pulls` | `{host_id}\|{source_uri}` | `{"at": <iso8601>, "data": {...}}` (C4) |

Freshness is decided on read (`host_snapshot_max_age_s`); entries carry no
TTL, matching the instances map. The `at` field is always **control's**
receive time, never the host's, so clock skew across the fleet cannot make
an entry look fresh forever.

Because nothing expires on its own, every keyed map is reclaimed
explicitly:

- Host removal purges all three maps for that host (`purge_host_state`).
- Host **disconnect** purges only `solar:hosts:pulls`. Progress can arrive
  only over the socket that just went away, so an in-flight entry is dead;
  the instances and snapshot maps still describe the host and are
  freshness-gated on read.
- `GET /api/pulls` prunes as it serves: terminal entries past
  `pull_progress_terminal_grace_s`, and non-terminal ones the host has
  stopped reporting for well past `pull_progress_stale_after_s`. A host
  that dies mid-pull never sends a terminal event, so without this its
  frozen `downloading` row would be served as live progress forever.

## 4. Consumers

- `_fetch_host_resource_snapshot` (`app/routes/management/resources.py`)
  serves `snapshot_source: "ws" | "http" | "none"`.
- `GET /api/pulls` (`app/routes/management/pulls.py`) serves the latest
  pull progress per `(host, source_uri)`.
- The reconciler's `_observe` reads instances from the WS cache
  (`solar:hosts:instances`) and attaches the full config while a spec
  change is pending.
