# Declarative Deployment Intent API Specification

| Field       | Value                                          |
|-------------|------------------------------------------------|
| Issue       | S-039                                          |
| Status      | Done                                           |
| Created     | 2026-05-29                                     |
| Depends on  | —                                              |
| Depended by | S-040, S-041, S-042, S-043, S-044, U-003, U-005, U-006, N-019, N-020 |
| References  | S-019, S-035, S-036, S-037, S-038, model-source-uri.md, host-draining.md |

## 1. Overview

Solar Control manages inference today **imperatively**: a client picks a host and submits a full backend config to `POST /api/hosts/{host_id}/instances`. Solar Control proxies that to the chosen Solar Host and observes the result. There is no concept of "desired state": no way to say *"serve `iris-osl:110m` with 2 replicas"* and have Solar Control figure out where and how.

This specification defines a **declarative deployment intent** model. A client (Solar WebUI or SuperNova deployment automation) submits a desired inference state — a served alias, a model source, a replica count, a priority, a strategy, and placement constraints — and Solar Control continuously **reconciles** the cluster toward that state by creating, replacing, migrating, and stopping instances across Solar Hosts.

This document is the **source of truth** for the intent contract. It defines:

- The desired-state schema (Section 4) and how it maps to existing instance configuration (Section 6).
- The ownership model that links running instances to an intent (Section 5).
- The intent lifecycle and status model (Sections 7 and 10).
- Reconciliation semantics, placement policy, and the one-replica-per-host rule (Section 8).
- Model source resolution and distribution reuse (Section 9).
- Deployment strategies `rolling` and `immediate` (Section 11).
- The create/list/get/delete API shapes (Section 12).
- Worked examples for common flows (Section 13).

### 1.1 Scope and downstream consumers

This is a specification-only issue. It produces no code. It is the contract for:

| Issue | Consumes |
|-------|----------|
| S-040 | Intent submission/list/delete API (Section 12) and persistence/validation (Sections 4, 5). |
| S-041 | Reconciliation engine semantics, ownership model, placement policy (Sections 5, 7, 8). |
| S-042 | `rolling` and `immediate` deployment strategies, health gate, failure behavior (Section 11). |
| U-003  | Solar WebUI intent form fields and live status fields/events (Sections 4, 10). |
| N-019/N-020 | SuperNova deployment intent builder and trigger (Sections 4, 12) — desired-state contract and status polling. |

### 1.2 Relationship to existing decisions

This spec realizes the README's architecture decisions without contradicting them:

- **Decision #14 (Deployment model):** declarative intent, no host specification, one replica per host, partial fulfillment, strategies `rolling`/`immediate`.
- **Decision #12 (Solar WebUI evolution):** shift from "configure instance X on host Y" to "submit intent, monitor how Solar Control arranges".
- **Decision #18 (Resource coordination):** Solar Control arranges instances autonomously; clients never manage hosts directly.
- **Section 6.6 (Declarative intent-based management):** "submit desired state, Solar Control arranges".

### 1.3 Topology (unchanged invariant)

Clients submit intents to **Solar Control only**. Solar Control reconciles by driving **Solar Hosts**. SuperNova and Solar WebUI never contact Solar Hosts directly.

```
┌──────────────┐                ┌──────────────┐
│ SuperNova    │                │ Solar WebUI  │
│ Control      │                │ (operations) │
│ (N-019/020)  │                │ (U-003)      │
└──────┬───────┘                └──────┬───────┘
       │    POST/GET/DELETE /api/intents (management API key)
       └───────────────┬───────────────┘
                       ▼
            ┌──────────────────────┐
            │    Solar Control     │
            │  Intent store (PG)   │
            │  Reconciler (S-041)  │
            │  Placement (S-038)   │
            │  Gateway registry    │
            └──────────┬───────────┘
       resolve+distribute │  create/start/stop/migrate instances
       (S-019 / pull)     │  (host-scoped instance API)
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
   ┌──────────┐     ┌──────────┐       ┌──────────┐
   │Solar Host│     │Solar Host│       │Solar Host│
   │ aiops01  │     │ aiops02  │       │  mac01   │
   └──────────┘     └──────────┘       └──────────┘
```

---

## 2. Concepts and Terminology

| Term | Definition |
|------|------------|
| **Intent** | A persisted desired-state record for one deployed alias. Owned and reconciled by Solar Control. |
| **Alias** | The served model name exposed to inference clients (e.g. `iris-osl:110m`). It is the instance config field `alias` and the key of the gateway registry (`solar:registry`). It is the **deployment identity** — there is at most one active intent per alias. |
| **Model source** | A model source URI (`repo://`, `huggingface://`, `local://`) per [model-source-uri.md](model-source-uri.md). It says *what files* to serve; the alias says *what name* to serve them under. |
| **Replica** | One running instance of the alias on one host. The one-replica-per-host rule means a replica count of N requires N distinct hosts. |
| **Managed instance** | An instance created by the reconciler on behalf of an intent, tagged with the ownership marker (Section 5). The opposite is a **manual instance** created via the imperative host-scoped API. |
| **Reconciliation** | The Solar Control loop that compares desired state (intents) with observed state (instances + gateway registry) and computes/executes actions to converge them. |
| **Desired state** | What the intent declares (replicas, source, priority, etc.). |
| **Observed state** | What Solar Control sees: managed instances and their status, the gateway registry, host health. |
| **Placement policy** | The host-selection algorithm shared with the S-038 reservation coordinator (Section 8.4). |

### 2.1 The intent-to-instance mapping (mental model)

An intent of `replicas: 2` for alias `iris-osl:110m` from `repo://iris-osl:v3` materializes as **two managed instances** on two different hosts, each with `config.alias = "iris-osl:110m"`, `config.model_source` resolved from `repo://iris-osl:v3`, and the ownership marker set. The gateway then load-balances inference traffic for `iris-osl:110m` across both, exactly as it does for manually-created instances sharing an alias today.

---

## 3. Design Principles

1. **Declarative, not imperative.** Clients declare the end state. Solar Control owns *how* and *where*. Clients never name a host in an intent.
2. **Solar Control reconciles, Solar Host executes.** Reconciliation, placement, migration, and strategy logic live in Solar Control. Solar Hosts only run local primitives (create/start/stop/pull) requested by Solar Control.
3. **One replica per host per alias.** There is no value in running the same alias twice on the same hardware (README Decision #14). This is a hard invariant enforced during every reconciliation and every strategy step.
4. **Source-based distribution.** Models are made available on a host by pulling from the authoritative source (Harbor/HuggingFace), reusing S-019 distribution and the `POST /models/pull` flow. No host-to-host byte copying. See [model-source-uri.md](model-source-uri.md).
5. **Idempotent convergence.** Reconciliation can run repeatedly and at any time without creating duplicates or oscillating. Actions are derived from the desired-vs-observed diff, keyed by `(intent_id, host_id)`.
6. **Explicit ownership.** Managed instances carry an ownership marker so reconciliation never touches manually-created instances and can always recompute its own footprint after a restart (Section 5).
7. **Priority-aware, conservative displacement.** A higher-priority intent may displace lower-priority workloads to claim capacity, but `production` is never displaced automatically and equal/higher priority is never displaced (Section 8.5).
8. **Partial fulfillment is a valid outcome.** If fewer hosts are available than requested replicas, the reconciler places as many as it can and reports the shortfall rather than failing the whole intent.
9. **Status is first-class.** Every intent exposes observed replicas, readiness, per-replica detail, conditions, errors, timestamps, and strategy progress so clients can drive UIs and automation.

---

## 4. Intent Schema

### 4.1 Request body (client-submitted)

The client supplies the **desired** fields only. Server-managed fields (`id`, `status`, timestamps) are ignored if present on create.

```json
{
  "alias": "iris-osl:110m",
  "model_source": "repo://iris-osl:v3",
  "replicas": 2,
  "priority": "production",
  "strategy": "rolling",
  "backend": {
    "backend_type": "huggingface_classification",
    "dtype": "auto",
    "max_length": 512,
    "labels": ["osl", "oslt", "type", "priority", "user_grade"]
  },
  "placement": {
    "roles": ["inference"],
    "gpu_type": "nvidia_cuda",
    "host_allow": [],
    "host_deny": []
  },
  "resources": {
    "vram_gb": 6
  },
  "metadata": {
    "source": "supernova",
    "job_id": "job-a1b2c3d4"
  }
}
```

### 4.2 Field reference

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `alias` | string | yes | — | Served model name; deployment identity. Unique among active intents. Maps to instance `config.alias`. |
| `model_source` | string (URI) | yes | — | Model source URI per [model-source-uri.md](model-source-uri.md). Must be `repo://`, `huggingface://`, or `local://`. Maps to instance `config.model_source`. |
| `replicas` | integer | no | `1` | Desired replica count. `>= 0`. `0` means "registered intent, no running instances" (useful to pre-create then scale up). One replica per host. |
| `priority` | enum | no | `production` | `production` \| `staging` \| `ephemeral`. See Section 4.3. Maps to instance `config.priority` (S-036). |
| `strategy` | enum | no | `rolling` | `rolling` \| `immediate`. Applies when replacing replicas (version/config change, scale-down churn). See Section 11. |
| `backend` | object | yes | — | Backend/instance config template (Section 6). Carries `backend_type` and runtime params, plus the optional `model_file` and `file_filters` model selectors (Section 4.7.1). Must **not** include `alias`, `model_source`, `host`, `port`, `api_key`. |
| `placement` | object | no | `{ "roles": ["inference"] }` | Placement constraints. See Section 4.5. |
| `resources` | object | no | `{}` | Resource hints for placement. See Section 4.6. |
| `metadata` | object (string→string) | no | `{}` | Free-form audit labels (e.g. originating job, requester). Stored, surfaced in status, never interpreted by placement. |

### 4.3 Priorities

Priorities are the policy signal for placement and displacement (S-036). The values are exactly:

| Priority | Meaning | Displacement behavior |
|----------|---------|------------------------|
| `production` | Live, customer-facing inference. | **Never** displaced/migrated/stopped automatically by reconciliation. May displace `staging` and `ephemeral` to claim capacity. |
| `staging` | Pre-production / validation. | May be displaced by `production`. May displace `ephemeral`. Migrated (not stopped) when possible. |
| `ephemeral` | Short-lived / experimental. | Lowest. First to be stopped or migrated when capacity is needed. May carry a TTL in future. |

Rules:

- Displacement is allowed **only** toward strictly lower priority (`production > staging > ephemeral`). Equal priority is never displaced.
- The one-replica-per-host rule holds regardless of priority.
- Default `production` follows S-036 ("preferably `production` unless a safer established default exists"). Clients submitting throwaway deployments should set `ephemeral` explicitly.

### 4.4 Strategies

| Strategy | Use | Summary |
|----------|-----|---------|
| `rolling` (default) | Zero-downtime updates of an existing alias. | Replace one host/replica at a time; bring up the replacement, wait until healthy, then retire the old one. Preferred for `production`. |
| `immediate` | Fast replacement where brief downtime is acceptable. | Stop all managed replicas for the intent, then create replacements per placement policy. |

Strategy governs **how an alias transitions** from one model version/config to another and how churn during scaling is sequenced. Detailed behavior, health gate, and failure handling are in Section 11.

### 4.5 Placement constraints

```json
"placement": {
  "roles": ["inference"],
  "gpu_type": "nvidia_cuda",
  "host_allow": ["host-uuid-a", "host-uuid-b"],
  "host_deny": ["host-uuid-c"]
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `roles` | string[] | `["inference"]` | Host must have **all** listed roles. Matches the host `roles` field (S-001/S-005). |
| `gpu_type` | string \| null | `null` (any) | If set, host `gpu_type` must match (`nvidia_cuda`, `apple_mps`, `cpu`). Useful to pin GGUF/CUDA-only or Mac-only models. |
| `host_allow` | string[] | `[]` (all) | If non-empty, placement is restricted to these host IDs. |
| `host_deny` | string[] | `[]` | Hosts to exclude. Applied after `host_allow`. |

Implicit constraint (not configurable): **anti-affinity by alias** — at most one replica of a given alias per host. It is always on.

### 4.6 Resource requirements

```json
"resources": { "vram_gb": 6, "ram_gb": 4 }
```

| Field | Type | Description |
|-------|------|-------------|
| `vram_gb` | number \| null | Estimated VRAM the instance needs. Used to filter/rank candidate hosts by `memory_available_gb`. |
| `ram_gb` | number \| null | Estimated system RAM (mainly relevant for Mac/`apple_mps` unified memory and CPU backends). |

Semantics by phase:

- **v1 (today):** best-effort hint. Placement reads per-host `memory_available_gb` / `disk_available_gb` (already reported via `host_health`) and skips hosts that cannot fit the request. No hard reservation is taken.
- **After S-035/S-038:** the reconciler should consult `GET /api/resources` for the aggregated `available = total - Σeffective` view (per S-034: `effective = max(actual, requested)` per running job) and may take a reservation through the S-038 coordinator before creating an instance. This spec keeps the field stable so the upgrade is non-breaking.

If `resources` is omitted, placement uses only role/gpu/allow-deny filters and ranks by free memory.

### 4.7 Validation rules

The S-040 API must reject invalid intents with `400`/`422` (Section 12.5):

- `alias` present, non-empty, matches the existing alias charset used for served names.
- `alias` unique among non-deleted intents → otherwise `409 Conflict`.
- `model_source` parses as a valid URI per [model-source-uri.md](model-source-uri.md) §2 (`repo://`, `huggingface://`, `local://`). The URI is **not** resolved at submit time (resolution happens during reconciliation); only the syntax is validated.
- `replicas >= 0`.
- `priority` ∈ {`production`, `staging`, `ephemeral`}.
- `strategy` ∈ {`rolling`, `immediate`}.
- `backend.backend_type` ∈ the supported backend types (`llamacpp`, `huggingface_causal`, `huggingface_classification`, `huggingface_embedding`, `huggingface_vision`).
- `backend` must not contain `alias`, `model_source`, `host`, `port`, or `api_key` (these are server-derived).
- `backend.model_file` (if set) requires `backend_type == "llamacpp"` and must be a non-empty string.
- `backend.file_filters` (if set) must be a list of non-empty patterns, and a non-empty list requires a `huggingface://` `model_source`.
- `placement.roles` non-empty; `gpu_type` (if set) is a known type.

### 4.7.1 Model file selection and download filters

A `model_source` names a whole artifact, but a llama.cpp instance needs one GGUF **file**, and a HuggingFace GGUF repository typically ships many quantisations. Two optional `backend` fields close that gap:

| Field | Backend | Description |
|-------|---------|-------------|
| `model_file` | `llamacpp` | Filename, relative path or `*` glob selecting the GGUF inside the pulled model directory. Resolved by Solar Host into the instance's `model` path. |
| `file_filters` | any | HuggingFace Hub `allow_patterns`; only matching files are downloaded. Requires a `huggingface://` source — ORAS pulls a Harbor artifact whole and `local://` is already on disk. |

```json
{
  "alias": "qwen3-vl:235b",
  "model_source": "huggingface://unsloth/Qwen3-VL-235B-Instruct-GGUF",
  "backend": {
    "backend_type": "llamacpp",
    "model_file": "*UD-Q4_K_XL*.gguf",
    "mmproj": "mmproj-BF16.gguf",
    "file_filters": ["*UD-Q4_K_XL*", "mmproj-BF16.gguf"]
  }
}
```

Resolution happens on Solar Host, which owns the filesystem — see [model-source-uri.md](model-source-uri.md) §4.3. `mmproj` accepts the same patterns. Omitting `model_file` keeps the previous behaviour: the largest GGUF at the root of a Harbor artifact is served.

---

## 5. Ownership Model

Reconciliation must reliably answer: *"which running instances belong to this intent?"* — across reconciler restarts, host reconnects, and concurrent manual operations. The chosen mechanism is an **explicit ownership marker on the instance config** (analogous to how S-036 adds `priority` to instance config).

### 5.1 Instance config additions

Two optional fields are added to the Solar Host instance config (all backend types) and carried through Solar Control. They are part of the persisted instance config and surfaced in instance/host status payloads:

| Field | Type | Description |
|-------|------|-------------|
| `managed_by` | string \| null | Owner subsystem. `"intent"` for reconciler-managed instances; `null`/absent for manual instances. |
| `intent_id` | string \| null | The owning intent's `id`. Set iff `managed_by == "intent"`. |

These join the existing `priority` addition from S-036. Manual instances created via the imperative API leave both unset and are therefore never adopted, modified, or stopped by reconciliation.

> Cross-repo note: this is a small additive change in `solar-host` (instance config models) and `solar-control` (it already proxies config as an opaque dict and need only persist/round-trip these fields and read them back from host status). It mirrors the S-036 `priority` addition and should land alongside S-040/S-041.

### 5.2 Identifying an intent's managed instances

The set of managed instances for intent `I` is:

```
managed(I) = { instance : instance.config.managed_by == "intent"
                          and instance.config.intent_id == I.id }
```

This set is recomputed from observed state (host instance snapshots in Redis + gateway registry) on every reconciliation; it is never trusted from memory alone. This makes the reconciler **stateless and restart-safe**: after a Solar Control restart it rediscovers its footprint by reading instance configs.

### 5.3 One-replica-per-host evaluation

The anti-affinity rule is evaluated against **all** instances serving the alias on a host, managed or not:

- The reconciler will not place a second managed replica of `alias` on a host that already runs any instance (managed or manual) serving `alias`.
- If a **manual** instance already serves `alias` on a host, that host is treated as occupied for the alias. The reconciler does not adopt it; it counts toward neither `observed_replicas` nor `ready_replicas` of the intent, and a `Conflict` condition is raised noting the manual instance (Section 10.3). This surfaces the situation to operators without the reconciler silently fighting a human.

### 5.4 Adoption and disown

- **Adoption** of pre-existing manual instances (matching by alias only) is **out of scope for v1** and intentionally avoided to prevent ambiguity. It is listed as a future extension (Section 14).
- **Disown:** deleting an intent (Section 7.4) stops/removes its managed instances. An optional `DELETE` mode may *orphan* instances (clear `intent_id`/`managed_by` instead of stopping) — see Section 12.4.

---

## 6. Mapping an Intent to Instance Configuration

The reconciler composes a concrete Solar Host `InstanceConfig` for each replica from the intent. The composition is deterministic so the same intent always yields the same config (modulo host/port):

| Instance config field | Source |
|------------------------|--------|
| `backend_type` | `intent.backend.backend_type` |
| `alias` | `intent.alias` |
| `model_source` | `intent.model_source` (resolved per Section 9 before instance creation) |
| `priority` | `intent.priority` (S-036) |
| `managed_by` | `"intent"` (Section 5) |
| `intent_id` | `intent.id` |
| backend runtime params (e.g. `ctx_size`, `n_gpu_layers`, `dtype`, `max_length`, `labels`, `pooling`, ...) | copied verbatim from `intent.backend.*` |
| `host` | host default (`0.0.0.0`); not client-controlled |
| `port` | auto-assigned by Solar Host |
| `api_key` | never set; instances always use the host API key (stripped by host config) |

Notes:

- Intents use `model_source` **exclusively**. The legacy raw `model` / `model_id` path fields are not part of the intent contract (they remain for manual/backward-compatible flows only). `backend.model_file` is the intent-level way to pick a file inside the resolved source (Section 4.7.1).
- The `backend` template is validated against the matching backend config model at submit time (best-effort) and again when the instance is created on the host (authoritative).
- Backend-specific required fields (e.g. `model_type` for llama.cpp embedding vs. reranker) travel inside `backend`. The intent layer does not enumerate them; it defers to the existing per-backend config models.

---

## 7. Intent Lifecycle

### 7.1 State machine

An intent has a top-level lifecycle `phase`:

```
                 submit
                   │
                   ▼
              ┌─────────┐   reconcile starts    ┌──────────────┐
              │ pending │──────────────────────▶│ reconciling  │
              └─────────┘                       └──────┬───────┘
                                                       │
                          all desired replicas ready   │  some but not all ready
                       ┌───────────────────────────────┼─────────────────────────┐
                       ▼                               ▼                         ▼
                  ┌─────────┐                     ┌──────────┐              ┌─────────┐
                  │  ready  │                     │ degraded │              │ failed  │
                  └────┬────┘                     └────┬─────┘              └────┬────┘
                       │  spec change / drift          │ retry/recover           │ retry
                       └───────────────┬───────────────┘                         │
                                       ▼                                         │
                                 (back to reconciling) ◀─────────────────────────┘

        DELETE /api/intents/{id}
                   │
                   ▼
              ┌──────────┐   managed instances stopped   ┌──────────┐
              │ deleting │──────────────────────────────▶│ deleted  │
              └──────────┘                               └──────────┘
```

### 7.2 Phases

| Phase | Meaning |
|-------|---------|
| `pending` | Intent stored and validated; reconciliation has not yet acted (the state right after S-040 create, before S-041 runs). |
| `reconciling` | Reconciler is actively creating/replacing/stopping instances or pulling models toward the desired state. |
| `ready` | `ready_replicas == desired_replicas` (and `desired_replicas > 0`), all on the target `model_source`. For `replicas: 0`, `ready` means zero managed instances exist. |
| `degraded` | At least one but not all desired replicas are ready (e.g. partial fulfillment / shortfall, a failed host, or a stalled rolling step). Service is partially available. |
| `failed` | Reconciliation cannot make progress and zero replicas are ready (e.g. invalid `model_source` at resolve time, no eligible hosts, repeated create failures past backoff). |
| `deleting` | `DELETE` received; reconciler is stopping/removing managed instances. |
| `deleted` | All managed instances removed (or orphaned); the intent record is removed or tombstoned. |

### 7.3 Pre-reconciliation behavior (S-040 only)

S-040 ships before S-041. Until the reconciliation engine exists, accepted intents are stored and reported as `pending` with `reconcile = "idle"`. They must **not** silently create workloads. This is explicit so the WebUI and SuperNova can distinguish "stored" from "reconciled".

### 7.4 Deletion semantics

- `DELETE /api/intents/{id}` transitions the intent to `deleting` and returns `202 Accepted`.
- The reconciler stops and removes the intent's managed instances (subject to strategy: by default an immediate teardown; deletion does not need rolling).
- Once `managed(I)` is empty, the intent becomes `deleted` and is removed (or tombstoned for audit; see Section 12.4).
- Deletion is **idempotent**: deleting an already-deleting/deleted intent returns success.

---

## 8. Reconciliation Semantics (S-041)

### 8.1 The loop

Reconciliation is **level-triggered** (it acts on the current diff, not on a queue of edits), runs:

- **Periodically** (a configurable interval, e.g. every few seconds), and
- **On events**: intent create/update/delete, host connect/disconnect, `instances_update`, and `host_health` changes.

A single replica reconcile is small and idempotent; the loop converges over multiple passes rather than doing everything in one tick.

For each active (non-deleted) intent `I`:

```
1. observe()    → managed(I), gateway registry for I.alias, host statuses/resources
2. diff()       → compare desired (replicas, model_source, backend, priority) vs observed
3. plan()       → ordered action list: create | replace | stop | migrate | no-op
4. act()        → execute one (or a bounded number of) actions via Solar Host primitives,
                  honoring the strategy (Section 11)
5. status()     → update intent status, conditions, replica_set, timestamps, last_error
```

### 8.2 Diff and actions

Let `desired = I.replicas`, `observed = |managed(I)|`, and let `current[h]` be the managed instance on host `h`.

| Condition | Action |
|-----------|--------|
| `observed < desired` and eligible hosts exist | **Create**: select target host(s) via placement (Section 8.4), ensure model present (Section 9), create + start managed instance, wait healthy. |
| `observed < desired` and no eligible host | **Shortfall**: record `shortfall = desired - placeable`; phase `degraded` (if some ready) or `failed` (if none). Retry on later ticks as capacity appears. |
| `observed > desired` | **Stop**: remove surplus managed instances. Selection order: failed/unhealthy first, then most-recently-created, then least-loaded (so long-lived healthy replicas survive). |
| managed instance's `model_source` ≠ `I.model_source` (or backend/priority drift) | **Replace**: transition the replica to the new config per `strategy` (Section 11). |
| managed instance is `failed`/`stopped` unexpectedly (drift) | **Recreate**: restart or recreate on the same or a new host (with backoff). |
| managed instance runs on a **draining** host | **Evacuate**: migrate it to another placement candidate via S-037. If none exists, leave it serving and report the stall — a drain never reduces capacity. See [host-draining.md](host-draining.md) §4.2. |
| managed instance is `failed`/`stopped` on a **draining** host | **Stop** (stop + delete) rather than recreate, so the next `create` places the replica elsewhere. Recreating in place would fight the drain. |
| desired state already met | **No-op**. |

Drift detection compares the intent's `model_source` and backend fields against the observed instance configuration. Since a spec can change under a running deployment (Section 12.5), a backend-only edit must be detected even for fields the cached instance view does not carry: while a spec change is pending, the comparison uses the instance's full configuration rather than the cached summary.

### 8.3 Idempotency and safety

- Actions are keyed by `(intent_id, host_id)`. The reconciler never creates two managed replicas of the same alias on one host.
- A concurrency guard (per-intent lock/lease) prevents two Solar Control replicas from reconciling the same intent simultaneously (Solar Control runs 2+ replicas).
- Create is guarded by an existence check (re-observe) so a crash between "create" and "status write" does not produce duplicates on the next tick.
- Failed create/start attempts use bounded exponential backoff recorded in `last_error` and a per-replica `retry` count; the intent does not thrash.

### 8.4 Placement policy (shared with S-038)

Placement selects target hosts for new/replacement replicas. It MUST be a **single shared helper** also used by the S-038 reservation coordinator (the issue explicitly warns against a second independent algorithm).

```
candidates = hosts where:
    status == online / connected
    host is not draining or drained                  # host-draining.md §4.1
    roles ⊇ placement.roles
    (placement.gpu_type is null or host.gpu_type == placement.gpu_type)
    (host_allow empty or host.id in host_allow)
    host.id not in host_deny
    host is NOT already serving I.alias              # one-replica-per-host
    fits(host, resources)                            # memory_available_gb / disk_available_gb (v1)
                                                     # or available = total-Σeffective (S-035, effective = max(actual, requested) per S-034)

rank candidates by:
    1. host not currently serving any managed replica of I (always true after the filter)
    2. most free VRAM (memory_available_gb), then free disk
    3. fewest running instances (spread), then stable tiebreak by host id

select the top (desired - observed) candidates.
```

If `candidates` is smaller than needed, evaluate displacement (Section 8.5). If still insufficient, fulfill partially and record the shortfall.

### 8.5 Priority-aware displacement (conservative)

When capacity is insufficient, the reconciler may free a host by displacing a **strictly lower-priority** workload, then place the new replica there. Rules:

- Only `staging`/`ephemeral` instances may be displaced, and only by an intent of strictly higher priority. `production` is never displaced automatically.
- Prefer the lowest priority first (`ephemeral` before `staging`).
- **Migrate, don't drop, when possible.** Use S-037 migration to move the displaced instance to another eligible host (which itself respects one-replica-per-host and roles). Only stop it if no migration target exists and policy allows (e.g. `ephemeral`).
- Active training jobs (S-032/S-033) are **non-displaceable** — a host running a training step is not a displacement target (consistent with S-037).
- Displacement decisions and their effects are recorded in the intent's status (`conditions`, `events`) and the displaced workload's own status.

This is the same priority semantics the S-038 reservation coordinator uses to free capacity for training; intent reconciliation reuses it for inference placement.

### 8.6 Convergence and partial fulfillment

- The reconciler always moves the cluster *toward* the desired state in bounded steps and re-evaluates. It tolerates transient host unavailability by marking hosts stale rather than failing the intent.
- Partial fulfillment (`ready_replicas < desired_replicas`) is a stable, reported state (`degraded` + `shortfall`), not an error. When capacity returns, later ticks fill the gap automatically.

---

## 9. Model Source Resolution and Distribution

Reconciliation reuses the existing model-source machinery rather than redefining it. See [model-source-uri.md](model-source-uri.md) Sections 3 and 5.

Before creating/starting a replica on host `h`:

1. **Ensure the model is present on `h`.** The reconciler issues the existing distribution flow — equivalent to `POST /api/models/distribute` (S-019) with `{ target_host_id: h, source_uri: I.model_source }` — which resolves `repo://` via the Data Repository, then calls Solar Host `POST /models/pull`. The host checks its manifest cache and downloads only on a miss. `huggingface://` pulls directly from the Hub; `local://` requires no pull.
2. **Create the instance with the resolved local path.** Per model-source-uri.md §7, Solar Host accepts only `local://` (or legacy fields) at instance creation; `repo://`/`huggingface://` must be pulled first. The reconciler therefore creates the instance using the resolved path returned by the pull (or passes `model_source` through Solar Control's instance-create resolution, which performs the same pull-then-resolve).
3. **Availability awareness.** The reconciler may consult `GET /api/models/availability` (S-020) to skip distribution when a host already has the model.

Failure handling: distribution/pull errors (`404` artifact not found, `502` source unreachable, `507` insufficient disk, auth failures) are surfaced into the intent's `last_error` and the per-replica entry, and the host is skipped for that tick. A `404` for `model_source` (artifact does not exist) is treated as a terminal configuration error → intent `failed` with a clear message (retrying will not help until the intent is updated).

```
Reconciler                Solar Control             Solar Host        Harbor / HF
    │  need replica on h        │                        │                 │
    │  (alias, model_source)    │                        │                 │
    │──────────────────────────▶│ distribute(h, src)     │                 │
    │                           │  resolve repo://       │                 │
    │                           │──── POST /models/pull ▶│                 │
    │                           │                        │  cache miss →   │
    │                           │                        │── pull ────────▶│
    │                           │                        │◀── bytes ───────│
    │                           │◀── {path, cached} ─────│                 │
    │                           │  create instance       │                 │
    │                           │  (local:// path,       │                 │
    │                           │   alias, intent_id)    │                 │
    │                           │──── POST /instances ──▶│ start           │
    │                           │◀── {instance, running} │                 │
    │  replica healthy          │  (registry registers   │                 │
    │◀──────────────────────────│   alias → instance)    │                 │
```

---

## 10. Status and Observability

### 10.1 Intent status object

Every intent read returns the desired fields plus a server-managed `status` object:

```json
{
  "id": "3f1c0c1e-8b2a-4e2a-9c77-1d2e3f4a5b6c",
  "alias": "iris-osl:110m",
  "model_source": "repo://iris-osl:v3",
  "replicas": 2,
  "priority": "production",
  "strategy": "rolling",
  "backend": { "backend_type": "huggingface_classification", "max_length": 512 },
  "placement": { "roles": ["inference"], "gpu_type": "nvidia_cuda" },
  "resources": { "vram_gb": 6 },
  "metadata": { "source": "supernova", "job_id": "job-a1b2c3d4" },

  "status": {
    "phase": "ready",
    "reconcile": "succeeded",
    "desired_replicas": 2,
    "observed_replicas": 2,
    "ready_replicas": 2,
    "updated_replicas": 2,
    "available": true,
    "shortfall": 0,
    "replica_set": [
      {
        "host_id": "host-uuid-a",
        "host_name": "damcpaiops01",
        "instance_id": "inst-uuid-1",
        "state": "running",
        "model_source": "repo://iris-osl:v3",
        "healthy": true,
        "message": null,
        "updated_at": "2026-05-29T08:31:10Z"
      },
      {
        "host_id": "host-uuid-b",
        "host_name": "damcpaiops02",
        "instance_id": "inst-uuid-2",
        "state": "running",
        "model_source": "repo://iris-osl:v3",
        "healthy": true,
        "message": null,
        "updated_at": "2026-05-29T08:31:42Z"
      }
    ],
    "conditions": [
      { "type": "Progressing", "status": false, "reason": "ReconcileComplete", "message": "All replicas updated", "last_transition": "2026-05-29T08:31:42Z" },
      { "type": "Available", "status": true, "reason": "MinimumReplicasAvailable", "message": "2/2 ready", "last_transition": "2026-05-29T08:31:42Z" }
    ],
    "strategy_progress": null,
    "last_error": null,
    "created_at": "2026-05-29T08:30:55Z",
    "updated_at": "2026-05-29T08:31:42Z",
    "last_reconciled_at": "2026-05-29T08:31:42Z",
    "ready_at": "2026-05-29T08:31:42Z"
  }
}
```

### 10.2 Status field reference

| Field | Type | Description |
|-------|------|-------------|
| `phase` | enum | Lifecycle phase (Section 7.2). |
| `reconcile` | enum | `idle` \| `in_progress` \| `succeeded` \| `failed` — the state of the most recent reconcile pass. |
| `desired_replicas` | int | Echo of `replicas`. |
| `observed_replicas` | int | Count of `managed(I)` instances that exist (any state). |
| `ready_replicas` | int | Managed instances that are `running` **and** registered in the gateway registry for `alias`. |
| `updated_replicas` | int | Ready replicas already on the target `model_source`/backend config (drives rolling progress). |
| `available` | bool | `ready_replicas >= 1` (the alias can serve traffic). |
| `shortfall` | int | `desired_replicas - placeable` when hosts are insufficient; `0` otherwise. |
| `replica_set` | object[] | Per-replica detail (host, instance, state, source, health, message, timestamp). |
| `conditions` | object[] | Coarse machine-readable conditions (Section 10.3). |
| `strategy_progress` | object \| null | Non-null during an in-flight `rolling`/`immediate` update (Section 11). |
| `last_error` | object \| null | Most recent error: `{ code, message, host_id?, source_uri?, at }`. |
| `created_at` / `updated_at` | ISO 8601 | Record create / last spec or status change. |
| `last_reconciled_at` | ISO 8601 | Timestamp of the last reconcile pass. |
| `ready_at` | ISO 8601 \| null | When the intent first reached `ready` for the current spec. |

All timestamps are ISO 8601 UTC (e.g. `2026-05-29T08:31:42Z`), consistent with existing Solar Control payloads.

### 10.3 Conditions

Conditions are a small, stable set (Kubernetes-style) for machine consumption; richer human detail lives in `replica_set`/`last_error`:

| `type` | Meaning when `status: true` |
|--------|------------------------------|
| `Available` | At least the minimum replicas are ready and serving. |
| `Progressing` | A reconcile/strategy step is in flight. |
| `Conflict` | A manual instance occupies the alias on a candidate host (Section 5.3), or another active intent claims the alias. |
| `Degraded` | Desired replicas cannot all be made ready (shortfall, host loss, repeated failures). |

Each condition carries `status` (bool), `reason` (short CamelCase code), `message` (human text), and `last_transition` (ISO 8601).

### 10.4 Real-time events (recommended for U-003)

Reconciliation status should be observable live in Solar WebUI without polling. This spec recommends new Socket.IO events on the existing `/webui` namespace (to be implemented with S-041/U-003):

| Event | Payload | When |
|-------|---------|------|
| `intent_update` | `{ intent }` (full record incl. `status`) | On any intent status/spec change. |
| `intent_removed` | `{ id, alias }` | When an intent reaches `deleted`. |

Existing events remain the source for low-level changes the reconciler reacts to and the UI can correlate: `instances_update`, `instance_state`, `host_status`, `host_health`. No new host→control events are required for intents; reconciliation derives everything it needs from existing host telemetry plus the gateway registry.

---

## 11. Deployment Strategies (S-042)

Strategies govern **replacing** an alias's replicas with a new model version/config and sequencing churn. They do not change the one-replica-per-host invariant.

### 11.1 Health gate (shared definition)

A replacement replica is considered **healthy** when **all** hold:

1. Solar Host reports the instance `status == running`.
2. The instance is registered in the gateway registry (`solar:registry`) under `alias` (only `running` instances are registered today).
3. (Optional, if available) a lightweight readiness probe to the instance's supported endpoint succeeds.

A configurable timeout bounds the wait; exceeding it marks the step failed (see failure behavior below).

### 11.2 `rolling`

Goal: keep the alias available throughout the change. One host/replica at a time.

```
for each host slot that must change (new version, or scale target):
    1. choose target host via placement (may be the same host or a new one)
    2. ensure model present on target (Section 9)
    3. create + start the new replica (managed, intent_id set)
    4. wait until the new replica is healthy (11.1), up to timeout
    5. retire the old replica being replaced (stop + remove)
    6. update strategy_progress; proceed to next slot
```

- **Scale up** is a series of additive create steps (no retirement).
- **Scale down** removes one replica at a time (retire lowest-value: unhealthy → newest → least-loaded).
- **Version/config change** replaces each replica one at a time; at most one replica is "in transition" so capacity dips by at most one.
- Preferred for `production`.

### 11.3 `immediate`

Goal: fast replacement; brief downtime acceptable.

```
1. stop all managed replicas for the intent
2. (re)place replacements per placement policy
3. ensure model present on each target (Section 9)
4. create + start replacements
```

- Causes a gap where the alias has zero ready replicas until replacements come up.
- Acceptable where downtime is expected/requested or for non-`production` intents.

### 11.4 `strategy_progress`

While a `rolling`/`immediate` update is in flight, `status.strategy_progress` is populated:

```json
"strategy_progress": {
  "strategy": "rolling",
  "target_model_source": "repo://iris-osl:v4",
  "step": "2/2",
  "updated": 1,
  "in_progress": 1,
  "failed": 0,
  "message": "Replacing replica on damcpaiops02"
}
```

### 11.5 Failure behavior

| Strategy | On a replica failing its health gate |
|----------|--------------------------------------|
| `rolling` | **Stop and hold.** Do not retire the old replica; do not proceed to the next slot. Already-updated replicas keep running (service stays available on the new version for those, old version for the rest). Intent → `degraded`, `Progressing: true` stalls with `last_error`. Reconciler retries with backoff; an operator/automation may change the spec to roll back (submit prior `model_source`). |
| `immediate` | Replacements are attempted on placement; if some fail, the intent is `degraded`/`failed` with `ready_replicas` reflecting what came up. Because `immediate` already stopped the old replicas, there is no old version to fall back to — this is the documented downside of `immediate`. |

In both strategies, partial failure never leaves duplicate replicas on a host and never silently abandons the intent — the diff is re-evaluated next tick.

### 11.5.1 Editing an intent during a rollout

`strategy_progress` records the target the rollout was planned against, and the reconciler drives that state machine instead of re-diffing while it is set. An update (Section 12.5) that changes the spec therefore **clears `strategy_progress`**: the next tick re-diffs against the new spec and initiates a fresh rollout under the `strategy` in the updated spec. Replicas already converted by the abandoned rollout are not reverted — they are simply re-evaluated for drift against the new spec like any other replica.

This is also the supported rollback path: submitting the previous `model_source` while a rollout is stalled restarts the rollout toward the old version instead of waiting out the failed one.

### 11.6 Test scenarios (for S-042)

The S-042 implementation should cover at least: scale up, scale down, model version change (rolling, verifying one-at-a-time and availability), model version change (immediate), failed health check (rolling holds and retains availability), failed health check (immediate degraded), and shortfall (fewer hosts than replicas).

---

## 12. API (S-040, update in S-044)

All endpoints are under `/api/intents`, require the management API key (`X-API-Key` or `Authorization: Bearer`, same middleware as other `/api/*` routes), and use JSON. IDs are UUID strings; timestamps are ISO 8601 UTC.

### 12.1 `POST /api/intents` — create

Submit a desired-state intent. Body is the request schema (Section 4.1).

- **201 Created** → the full intent record with `status.phase = "pending"` (until S-041 reconciles).
- **400 / 422** → validation error (Section 4.7).
- **409 Conflict** → an active intent already exists for `alias`.

Response:

```json
{
  "id": "3f1c0c1e-8b2a-4e2a-9c77-1d2e3f4a5b6c",
  "alias": "iris-osl:110m",
  "model_source": "repo://iris-osl:v3",
  "replicas": 2,
  "priority": "production",
  "strategy": "rolling",
  "backend": { "backend_type": "huggingface_classification", "max_length": 512 },
  "placement": { "roles": ["inference"], "gpu_type": "nvidia_cuda" },
  "resources": { "vram_gb": 6 },
  "metadata": { "source": "supernova", "job_id": "job-a1b2c3d4" },
  "status": {
    "phase": "pending",
    "reconcile": "idle",
    "desired_replicas": 2,
    "observed_replicas": 0,
    "ready_replicas": 0,
    "updated_replicas": 0,
    "available": false,
    "shortfall": 0,
    "replica_set": [],
    "conditions": [],
    "strategy_progress": null,
    "last_error": null,
    "created_at": "2026-05-29T08:30:55Z",
    "updated_at": "2026-05-29T08:30:55Z",
    "last_reconciled_at": null,
    "ready_at": null
  }
}
```

### 12.2 `GET /api/intents` — list

List active intents. Optional filters (if they fit existing API patterns): `alias`, `priority`, `phase`, `metadata.<key>`.

- **200 OK** → `[{ ...intent }, ...]` (array of full records, including `status`). Each entry carries enough to render a list without per-item fetches: `alias`, `model_source`, `replicas`, `priority`, `strategy`, `status.phase`, `status.ready_replicas`, timestamps.

### 12.3 `GET /api/intents/{id}` — get one

- **200 OK** → the full intent record (Section 10.1).
- **404 Not Found** → unknown id.

`GET /api/intents/{id}` is included because it fits existing conventions and gives N-020/U-003 a cheap status-polling endpoint.

### 12.4 `DELETE /api/intents/{id}` — delete

Remove an intent and scale down its managed instances.

- Optional query `?orphan=true` → clear `managed_by`/`intent_id` on the managed instances instead of stopping them (leaves them running but unmanaged). Default (`orphan=false`) stops and removes them.
- **202 Accepted** → intent transitions to `deleting`; body returns the record with `status.phase = "deleting"`.
- **404 Not Found** → unknown id (a previously-deleted id may return 404 or a tombstone depending on retention; deletion is idempotent for the caller).

### 12.5 `PUT /api/intents/{id}` — update (S-044)

Change an existing deployment: scale, model version, strategy, priority, backend configuration, placement, resources, or metadata. Accepts the **same request schema as create** (Section 4.1) with **full-replace** semantics — a field omitted from the request is reset to its documented default, exactly as on create. Clients must therefore send the complete spec, not a diff.

- **200 OK** → the updated intent record (Section 10.1).
- **404 Not Found** → unknown or already-deleted intent.
- **409 Conflict** → the intent is being deleted (`phase = deleting`), or the alias is taken by another active intent.
- **422 Unprocessable Entity** → validation failure, in the same structured shape as create. Update applies **every** create rule (Section 4.7); the two validators are shared so they cannot drift apart.

Semantics:

- **`alias` is immutable.** It is the served name and the deployment's identity; a request that changes it is rejected. Serving a different name means a new intent.
- Changing `replicas` converges through the normal diff (Section 8.2): scale up creates, scale down stops surplus. `replicas: 0` remains valid and means "stop all replicas, keep the intent" — it is not a deletion.
- Changing `model_source`, `backend` or `priority` is drift, and converges under the `strategy` **in the updated spec** (Section 11).
- An in-flight rollout is reset so it cannot keep converging toward a superseded target (Section 11.5.1).
- Server-managed state (`status`, phases, timestamps) is not client-writable; the update touches the spec only.
- The reconciler is woken on success, so the change is picked up immediately rather than on the next periodic tick.
- Reconciliation of a spec that changes underneath a running pass is safe: the per-intent lock (Section 8.3) serialises passes across Solar Control replicas, each pass loads the intent inside that lock, and a recorded failure backoff is invalidated by a spec change so a corrective edit is retried at once instead of waiting out the previous spec's backoff.

### 12.6 Error model

Errors follow existing Solar Control conventions:

- Auth failures use the OpenAI-style envelope already returned by the auth middleware.
- Validation and operational errors use FastAPI's flat `{ "detail": "..." }`. Where useful, validation responses may include a structured list:

```json
{ "detail": "Invalid intent", "errors": [ { "field": "model_source", "message": "unsupported scheme 'http://'" } ] }
```

- Per-replica/reconcile errors are **not** HTTP errors — a created intent that cannot be fulfilled returns `201`/`200` and reports problems in `status` (`phase`, `conditions`, `last_error`). The API call succeeds; the deployment state is observable.

---

## 13. Examples

### 13.1 Initial deployment

Deploy `iris-osl:110m` from `repo://iris-osl:v3`, 2 replicas, production, rolling.

`POST /api/intents`:

```json
{
  "alias": "iris-osl:110m",
  "model_source": "repo://iris-osl:v3",
  "replicas": 2,
  "priority": "production",
  "strategy": "rolling",
  "backend": { "backend_type": "huggingface_classification", "max_length": 512 }
}
```

Reconciler actions: place on 2 distinct `inference` hosts → distribute `repo://iris-osl:v3` to each (S-019/pull) → create + start managed instances (`intent_id` set) → wait healthy. Status walks `pending → reconciling → ready` with `ready_replicas` going `0 → 1 → 2`.

### 13.2 Scale up (2 → 3)

`PUT /api/intents/{id}` (or future update) with `replicas: 3`.

Diff: `observed 2 < desired 3`. Placement picks a third eligible host (none already serving the alias). `rolling` simply **adds** one replica (no retirement). If only 2 hosts qualify → `degraded`, `shortfall: 1`, `Available: true` (2/3 serving). Fills in automatically when a third host becomes eligible.

### 13.3 Scale down (3 → 2)

`replicas: 2`. Diff: `observed 3 > desired 2`. Reconciler stops one managed instance (unhealthy → newest → least-loaded order), leaving 2 healthy replicas. No model re-pull needed.

### 13.4 Model version update (rolling)

Change `model_source` from `repo://iris-osl:v3` to `repo://iris-osl:v4` (strategy `rolling`).

Per host, one at a time: distribute `:v4` → create new replica → wait healthy → retire the `:v3` replica → next host. Throughout, the alias keeps serving (mix of v3/v4 briefly). `strategy_progress.updated` climbs `0 → 1 → 2`; `phase` returns to `ready` when `updated_replicas == desired_replicas` on `:v4`. If a v4 replica fails its health gate, the v3 replica is **not** retired and the intent holds in `degraded` (Section 11.5).

### 13.5 Model version update (immediate)

Same change with `strategy: immediate`: stop both v3 replicas, then place + pull + start v4 replicas. Brief downtime while v4 comes up. Use when downtime is acceptable.

### 13.6 Capacity pressure with priority displacement

Submit a `production` intent for `wp:27b` needing a `nvidia_cuda` host, but both NVIDIA hosts are full — one runs an `ephemeral` test alias. Placement finds no free candidate → displacement: the `ephemeral` instance is migrated (S-037) to another eligible host if one exists, else stopped (allowed for `ephemeral`); the freed host then receives the `production` replica. A `production` instance on the other host is **never** touched. The displaced workload's status reflects the migration/stop; the new intent records the displacement in `conditions`/events.

### 13.7 Delete intent

`DELETE /api/intents/{id}`: phase → `deleting`; reconciler stops + removes both managed instances; the alias disappears from the gateway registry; phase → `deleted`. With `?orphan=true`, the two instances keep running but lose `managed_by`/`intent_id` (they become manual instances) and the intent is removed.

---

## 14. Security

- **Authentication.** All `/api/intents` endpoints require the management API key, enforced by the existing Solar Control auth middleware (same as other `/api/*` routes). No new auth surface.
- **No host bypass.** Intents are accepted and reconciled only by Solar Control. SuperNova and Solar WebUI never translate an intent into a direct Solar Host call. This preserves the topology invariant (Section 1.3) and Decision #4.
- **Input validation.** Strict validation of `model_source` syntax, `priority`/`strategy` enums, `backend` shape, and placement constraints (Section 4.7) prevents malformed configs from reaching hosts. Model-source resolution and credential handling follow [model-source-uri.md](model-source-uri.md) §8 (Harbor/HF credentials live on Solar Host; never in the intent).
- **Blast-radius controls.** Priority-aware displacement is conservative by construction (Section 8.5): `production` is never auto-displaced and equal/higher priority is never displaced, limiting the damage a careless intent can do.

---

## 15. Impact on Existing Issues

| Issue | Relationship |
|-------|--------------|
| S-036 (instance priority) | Intents set `config.priority`. This spec adds two sibling instance-config fields (`managed_by`, `intent_id`) that should land with the same cross-repo change pattern. |
| S-019 (model distribution) | Reused as-is by reconciliation to ensure models are present before instance creation (Section 9). |
| S-035 (aggregated resources) | Practical input to placement; until available, placement reads per-host `memory_available_gb`/`disk_available_gb` from `host_health` (Section 4.6, 8.4). |
| S-037 (instance migration) | Used by priority-aware displacement to move (not drop) displaced lower-priority instances (Section 8.5). |
| S-038 (reservation coordinator) | Shares the placement policy helper (Section 8.4). Intent reconciliation must not fork a second placement algorithm. |
| S-040 | Implements create/list/get/delete (Section 12), persistence, validation, and the `pending`/unreconciled status contract (Section 7.3). |
| S-041 | Implements the reconciliation engine, ownership model, idempotency, and one-replica-per-host (Sections 5, 8). |
| S-042 | Implements `rolling`/`immediate` strategies, the health gate, and failure behavior (Section 11). |
| S-043 (host draining) | Adds the draining host filter to placement (Section 8.4) and the evacuate/stop rules for managed replicas on a draining host (Section 8.2). Defined in [host-draining.md](host-draining.md). |
| S-044 (intent update) | Implements `PUT /api/intents/{id}` (Section 12.5), the rollout reset (Section 11.5.1), and update-safe reconciliation (Section 8.3). |
| U-003 | Builds the intent form (Section 4 fields) and live status view (Sections 10.1–10.4). |
| U-005 | Surfaces host draining in the WebUI Resources page (see [host-draining.md](host-draining.md) §5–§7). |
| U-006 | Builds the intent edit form on top of Section 12.5. |
| N-019/N-020 | Build deployment intents from job config and submit/poll them (Sections 4.1, 12). N-020 may update an existing deployment instead of deleting and resubmitting. |

### 15.1 Cross-repo change summary

| Repo | Change |
|------|--------|
| `solar-host` | Add optional `managed_by` and `intent_id` to instance config models (all backends), persisted and echoed in instance/host status. Sibling to S-036 `priority`. |
| `solar-control` | New intent store (Postgres `intents` table) + API (S-040), reconciliation engine (S-041), strategies (S-042), placement helper shared with S-038, and `intent_update`/`intent_removed` `/webui` events. Round-trip `managed_by`/`intent_id` in the proxied instance config. Later: intent update endpoint (S-044) and host draining (S-043). |
| `solar-webui` | Intent submission form and status view (U-003), intent editing (U-006), host draining controls (U-005). |
| `supernova-control` | Build/submit/poll intents (N-019/N-020). |

---

## 16. Future Extensions

- **Adoption of manual instances.** Optionally let an intent adopt a matching manual instance (by alias) by stamping ownership, instead of treating it as a conflict (Section 5.4).
- **Additional strategies.** `canary` / `blue-green` (deploy a candidate alias alongside production, shift gradually), beyond v1's `rolling`/`immediate`.
- **Autoscaling.** Replica count driven by load/latency signals from the gateway rather than a fixed number.
- **Reservation-backed placement.** Take a hard reservation via S-038 before creating an instance, replacing the best-effort memory check.
- **TTL for `ephemeral`.** Auto-expire ephemeral intents after a duration.
- **`repo://` latest tracking.** An intent could track `repo://iris-osl` (no version) and auto-roll when a newer version registers, once model-source-uri.md's latest-version extension lands.
- **Weighted/affinity placement.** Beyond spread: pin certain aliases to certain GPU classes or co-locate complementary models.

---

## 17. Related Specifications

- [Model Source URI Specification](model-source-uri.md) — `repo://`, `huggingface://`, `local://` resolution, `POST /models/pull`, distribution.
- [Host Draining Specification](host-draining.md) — taking a host out of service: drain states, preflight, evacuation, and the placement filter reconciliation honours.
- [Job Step Workspace Specification](job-step-workspace.md) — training pipeline workspace (context for SuperNova's produce→deploy flow).
- README §6.6 (Declarative Intent-Based Management), §3 (Topology), Decisions #12, #14, #18.
- S-040 (intent API), S-041 (reconciliation engine), S-042 (deployment strategies), S-044 (intent update) — implementation consumers.
- S-019 (distribution), S-035 (resources), S-036 (priority), S-037 (migration), S-038 (reservation coordinator) — reused/aligned mechanisms.
- U-003 (Solar WebUI intent form), N-019/N-020 (SuperNova deployment automation) — downstream clients.
