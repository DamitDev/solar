# SuperNova - Project Roadmap

## How to read this document

- Each **Phase** is a major deliverable milestone
- Each **Milestone** is a coherent batch of work within a phase
- Each **Issue** is a single assignable task (hours to a few days of work)
- **Repo** indicates which codebase the issue primarily affects
- **Size**: S (hours), M (1-2 days), L (3-5 days)
- **Depends on** lists blocking issue IDs
- Phases are sequential but milestones within a phase can overlap where dependencies allow
- Phase 1 (Data Repository) can start in parallel with Phase 0 (Solar Evolution)

---

## Phase 0: Solar Evolution for SuperNova

> Extend Solar v3.0 with model management, resource coordination, and job step execution.
> This is the foundation - everything else depends on it.

### Milestone 0.1: Host Roles & Resource Reporting

Adds structured metadata about what each host can do and what resources are available.

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-001 | Add `roles` field to solar-host config (`config.json`). Configurable list, e.g. `["inference", "training"]`. Include in registration event payload. | solar-host | S | - |
| S-002 | Add GPU type detection to host registration. Report `gpu_type` (`nvidia_cuda`, `apple_mps`, `cpu`) based on available hardware. | solar-host | S | - |
| S-003 | Add disk space reporting to `/health` endpoint. Report total, used, and available disk for the models directory. | solar-host | S | - |
| S-004 | Add available VRAM/RAM calculation to `/memory` endpoint. Compute `total - used_by_running_instances` as `available` field. | solar-host | S | - |
| S-005 | Extend host data model in Redis and PostgreSQL to store `roles`, `gpu_type`, and extended resource fields. Update host registration handler. | solar-control | M | S-001 |
| S-006 | Add role-based host filtering to `GET /api/hosts` (e.g. `?role=training`). Add resource availability to host status responses. | solar-control | S | S-005 |
| S-007 | Update Solar WebUI host cards to display roles, GPU type, and resource availability. | solar-webui | S | S-006 |

---

### Milestone 0.2: Model Source Abstraction

Replaces raw filesystem paths with a URI scheme. Enables Solar to pull models from multiple sources.

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-008 | Design model source URI specification. Document `repo://`, `huggingface://`, `local://` schemes, resolution behavior, caching, and error handling. | docs | S | - |
| S-009 | Implement managed models directory on solar-host. Env-configurable path (`MODELS_DIR`). Track downloaded models with manifest file. | solar-host | M | - |
| S-010 | Update instance creation on solar-host to accept `model_source` URI alongside existing `model` / `model_id` fields. Resolve `local://` URIs to filesystem paths (backward compatible). | solar-host | M | S-009 |
| S-011 | Implement URI parser and resolver dispatcher in solar-control. Route `repo://`, `huggingface://`, `local://` to appropriate resolver. | solar-control | M | S-008 |
| S-012 | Implement `huggingface://` resolver in solar-control. Send pull command to Solar Host, which downloads from HuggingFace Hub directly. | solar-control | M | S-011, S-015 |
| S-013 | Implement `repo://` resolver stub in solar-control. Returns error until Data Repository is available. Will be completed in Phase 1. | solar-control | S | S-011 |

---

### Milestone 0.3: Model File Management

Gives Solar the ability to manage model files on hosts programmatically.

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-014 | Implement `GET /models` on solar-host. List all models in the managed models directory with file sizes and metadata. | solar-host | S | S-009 |
| S-015 | Implement `POST /models/pull` on solar-host. Solar Host pulls models directly from source (Harbor via ORAS, HuggingFace Hub). Cache check via manifest, download on miss. | solar-host | M | S-009 |
| S-016 | *(Deprioritized)* Implement `GET /models/{name}/download` on solar-host. Streaming download of model files from managed models directory. Not needed for primary flow. | solar-host | M | S-009 |
| S-017 | Implement `DELETE /models/{name}` on solar-host. Remove model from managed models directory. Reject if model is in use by a running instance. | solar-host | S | S-014 |
| S-018 | Add free space validation before model operations. Reject pulls that would exceed available disk space. | solar-host | S | S-003, S-015 |
| S-019 | Implement model distribution in solar-control. `POST /api/models/distribute` - tell target host to pull model from authoritative source (Harbor/HuggingFace). | solar-control | S | S-015 |
| S-020 | Implement model availability query in solar-control. `GET /api/models/availability` - which models exist on which hosts. | solar-control | S | S-014 |

---

### Milestone 0.4: Job Step Execution Engine

The core training capability. Solar Host can execute a sequence of Docker containers.

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-021 | Design shared volume layout for step execution. Define workspace directory structure (`/workspace/models/`, `/workspace/data/`, `/workspace/output/`, `/workspace/config/`). Document in spec. | docs | S | - |
| S-022 | Integrate Docker SDK (`docker-py`) into solar-host. Implement container lifecycle primitives: pull image, create container, start, stop, remove, stream logs. | solar-host | M | - |
| S-023 | Implement step executor on solar-host. Sequential container runner that executes a list of steps, mounting shared workspace volume, reporting per-step status. Fail-fast on step failure. | solar-host | L | S-022, S-021 |
| S-024 | Implement NVIDIA Container Toolkit GPU allocation for step containers. Pass `--gpus` and VRAM limits to Docker containers. | solar-host | M | S-022 |
| S-025 | Implement step log capture and streaming. Container stdout/stderr → Socket.IO `step_log` event → solar-control → SuperNova. Per-step log buffers. | solar-host | M | S-023 |
| S-026 | Implement step status reporting. New Socket.IO events: `step_started`, `step_completed`, `step_failed`, `job_completed`. Include step name, duration, exit code. | solar-host | M | S-023 |
| S-027 | Implement step execution REST API on solar-host. `POST /jobs` - accept step list and config, start execution. `GET /jobs/{id}` - status. `DELETE /jobs/{id}` - cancel. | solar-host | M | S-023 |
| S-028 | Build `supernova-step-download-model` Docker image. Resolves model via Data Repository API, pulls directly from Harbor (ORAS) to workspace. Args: repo URI, output path. | supernova-steps | M | Phase 1.2 |
| S-029 | Build `supernova-step-download-dataset` Docker image. Resolves dataset via Data Repository API, pulls directly from Harbor (ORAS) to workspace. Args: repo URI, output path. | supernova-steps | S | Phase 1.2 |
| S-030 | Build `supernova-step-upload-model` Docker image. Pushes trained model to Harbor (ORAS), then registers artifact with Data Repository API. Args: source path, harbor target, metadata. | supernova-steps | M | Phase 1.2 |
| S-031 | Build `supernova-step-convert-model` Docker image. Converts HuggingFace model to GGUF using llama.cpp conversion tools. Args: input path, output path, quantization params. | supernova-steps | M | - |
| S-032 | Implement job step submission proxy in solar-control. Accept step execution request, route to appropriate host based on roles and resources. Forward status events to clients. | solar-control | M | S-027, S-006 |
| S-033 | Add active job step awareness to solar-control host status. Show running jobs alongside inference instances. Broadcast job status to WebUI. | solar-control | S | S-032 |

---

### Milestone 0.5: Resource Management & Coordination

Enables Solar to autonomously manage resources, migrate instances, and fulfill deployment intents.

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-034 | Implement resource reservation on solar-host. `POST /resources/reserve` - reserve specified VRAM/RAM for a job. `DELETE /resources/reserve/{id}` - release. Track reserved vs available. | solar-host | M | S-004 |
| S-035 | Implement resource query API on solar-control. `GET /api/resources` - aggregated view of all hosts' available resources, reservations, and running workloads. | solar-control | S | S-034, S-006 |
| S-036 | Implement instance priority levels. Add `priority` field to instances (`production`, `staging`, `ephemeral`). Lower-priority instances can be migrated or stopped to free resources. | solar-control, solar-host | M | S-005 |
| S-037 | Implement instance migration in solar-control. Stop instance on source host, recreate on target host with same config. Ensure model files are available on target. | solar-control | L | S-019, S-036 |
| S-038 | Implement resource reservation coordinator in solar-control. Accept resource request (e.g. "20 GB VRAM on training-capable host"), find best host, migrate instances if needed, reserve resources. Return host assignment or error. | solar-control | L | S-035, S-037 |
| S-039 | Design declarative intent API specification. Document intent schema: model alias, replica count, strategy, constraints. Define reconciliation behavior. | docs | M | - |
| S-040 | Implement intent submission API in solar-control. `POST /api/intents` - accept desired state. `GET /api/intents` - list active intents. `DELETE /api/intents/{id}` - remove. | solar-control | M | S-039 |
| S-041 | Implement intent reconciliation engine in solar-control. Compare desired state (intents) with current state (running instances). Compute actions: create, migrate, stop. Enforce one-replica-per-host rule. | solar-control | L | S-040, S-037 |
| S-042 | Implement deployment strategies in reconciliation. `rolling`: update one host at a time, verify healthy before next. `immediate`: stop all, replace, start. | solar-control | M | S-041 |
| S-043 | Implement host draining. Durable drain state on the host, drain/resume/progress endpoints, placement excludes draining hosts, reconciler evacuates managed replicas. Manual instances block the drain; a drain never reduces serving capacity. | solar-control | L | S-037, S-041 |
| S-044 | Implement intent update. `PUT /api/intents/{id}` with full-replace semantics, immutable alias, in-flight rollout reset, and update-safe reconciliation. | solar-control | M | S-040, S-042 |
| S-049 | Drift-safe backend comparison and the churn circuit breaker. JSON-structural comparison with host-mirrored coercion, API-boundary canonicalization of `chat_template_kwargs`, bounded drift-driven REPLACE rounds (`BackendDriftUnsettled` after `max_drift_replace_attempts`). | solar-control | M | S-044 |
| S-050 | Host resource snapshot over the WS push plus the Redis read model. `host_health` carries the full snapshot; `solar:hosts:snapshots` cache-first fetch with HTTP fallback (`snapshot_source: ws/http/none`); `endpoints_update` events. | solar-control, solar-host | M | S-041 |
| S-051 | Model pull progress telemetry and progress-aware cold-start bounds. Host `progress_cb` + `pull_progress` WS events; `solar:hosts:pulls` cache + `GET /api/pulls`; reconciler waits with sliced progress-aware bounds (`recoverable` last_error). | solar-control, solar-host | M | S-041 |
| S-052 | Intent validation layer. Accelerator vocabulary with aliases, per-backend field ownership, device contract, modality rules, and fleet-aware advisory validation reusing the placement filter chain. | solar-control | M | S-040, S-044 |
| S-053 | Drift circuit-breaker persistence. Hydrate `drift_replace_attempts`, `drift_unsettled_keys` and `shortfall_reason` in `_row_to_response` so the breaker survives a reload, plus a pin test that every key `_update_status` writes is hydrated on read. | solar-control | S | S-049 |
| S-054 | Update grandfathering for modality rules, path-scoped drift comparison (resolved path keys only, absolute instance values), and `gpu_type` normalization on the reconciler read path. | solar-control | M | S-049, S-052 |
| S-055 | Recoverable cold-start failures on the inner pull bound. A resolver timeout raised inside the action is marked `recoverable` when Redis pull progress is still fresh, so the webui shows "still working" instead of a hard error. | solar-control | S | S-051 |
| S-056 | Progress-aware resolver timeouts. Replace the total pull bound in the model resolvers with a read-idle bound so a healthy slow download is not cancelled and a stalled one fails promptly. | solar-control | M | S-055 |
| S-057 | Zero-downtime drain evacuation. EVACUATE creates and starts the target replica before stopping the source (create-then-stop); `execute_migration` stays stop-before-create for manual moves. | solar-control | M | S-043 |

---

### Milestone 0.6: Artifact Upload

Establishes the flat file-per-layer artifact layout as the platform contract and adds an ingestion path for models that cannot be published to HuggingFace. See [docs/specs/artifact-upload.md](docs/specs/artifact-upload.md).

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-045 | Fix the `upload_model` artifact layout. Push one OCI layer per file instead of a single `model.tar.gz`, so pipeline-produced models are pullable and servable by Solar Host. | supernova-steps | S | S-030 |
| S-046 | Fix nested-path digest verification after a Harbor pull. Walk the pull target recursively, key on relative paths, and reject traversal titles. | solar-host | S | S-015 |
| S-047 | Add an artifact upload relay to Solar Control. Session API, chunked streaming into Harbor with no disk staging, Redis session state, manifest assembly, and Data Repository registration with rollback. Includes the `aiops-k8s` ingress and secret changes. | solar-control, aiops-k8s | L | S-045, D-007 |

### Milestone 0.7: Artifact Deletion

Adds the destructive counterpart to the upload path: models and versions can be removed from the catalog, with Harbor blobs deleted first and metadata unregistered second, orchestrated by Solar Control. See [docs/specs/artifact-delete.md](docs/specs/artifact-delete.md).

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-048 | Add a catalog delete relay to Solar Control. `CatalogDeleteService` deletes the Harbor artifact first (reusing `OciPushClient.delete_tag`), then unregisters in the Data Repository; version-aware running-instance guard returns 409; whole-repo delete reports per-version results and best-effort repository cleanup. | solar-control | M | D-019 |

---

### Milestone 0.8: GPU-Aware Placement

Makes placement and reservations work per physical GPU: hosts report per-GPU telemetry, intents declare `gpu_count`, solar-control picks the exact devices, and solar-host enforces them via `CUDA_VISIBLE_DEVICES`. See [docs/specs/gpu-aware-placement.md](docs/specs/gpu-aware-placement.md).

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-058 | Implement GPU-aware placement end to end. Per-GPU host telemetry, `gpu_count` on intent resource requirements, `find_gpu_assignment` bin-packing in the shared placement policy, `CUDA_VISIBLE_DEVICES` enforcement and spawn-time verification in solar-host backend runners, GPU-aware reservations (S-034/S-038 + cold-start claims), `gpu_ids` threading through create/migrate/evacuate, minimal WebUI (per-GPU resources cards, `gpu_count` input, assigned devices), tests and doc amendments. | solar-host, solar-control, solar-webui | L | S-034, S-035, S-037, S-038, S-041 |

---

### Milestone 0.9: Cursor Proxy

Makes DeepSeek thinking-mode usable from Cursor without per-user local
proxies: a native `/cursor/v1` endpoint on solar-control that exposes
reasoning-effort model aliases (`deepseek-v4-flash:max/high`,
`krumpli:max/high`), restores multi-round tool-call reasoning from a shared
Redis cache, mirrors thinking into Cursor-visible blocks, and attributes
traffic to the caller's endpoint key. See
[docs/specs/cursor-proxy.md](docs/specs/cursor-proxy.md).

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| S-059 | Implement the `/cursor/v1` proxy endpoint end to end: port deepseek-cursor-proxy transform/streaming/reasoning modules (Redis store, async), alias table with effort injection, aiohttp self-HTTP to the /v1 gateway forwarding the caller's API key, auth (endpoint keys only, management key rejected), OpenAI error shapes, tests and docs. No DB migration, no webui surface. | solar-control | M | - |

---

## Phase 1: Data Repository

> Centralized storage for models and training datasets. Can start in parallel with Phase 0.

### Milestone 1.1: Infrastructure & Scaffolding

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| D-001 | Create `supernova` project in Harbor. Repositories are created on first push (one per artifact, e.g. `supernova/iris-osl`). Configure access credentials. | infra | S | - |
| D-002 | Design PostgreSQL schema for Data Repository metadata. Tables: `artifacts`, `artifact_versions`, `artifact_metadata`. Define indexes for search. | data-repository | M | - |
| D-003 | ORAS Python library evaluation. Build POC: push a model directory as OCI artifact to Harbor, pull it back, verify integrity. Document findings and API patterns. | data-repository | M | D-001 |
| D-004 | Scaffold Data Repository FastAPI project. Project structure, configuration, database connection, health endpoint, Docker/Helm setup. | data-repository | M | - |
| D-005 | Provision PostgreSQL schema from D-002 in the cluster. Alembic migration setup for future schema changes. | data-repository | S | D-002, D-004 |

---

### Milestone 1.2: Core Repository API

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| D-006 | Implement Harbor API integration layer. Wrapper module for artifact verification (HEAD request to validate harbor_ref exists), deletion, and metadata queries against Harbor. Define custom OCI media types. Consumers (step containers, Solar) do ORAS push/pull directly. | data-repository | M | D-003 |
| D-007 | Implement model registration endpoint. `POST /api/models/{name}/versions` - accept harbor_ref + metadata, verify artifact exists in Harbor (HEAD), create metadata record in Postgres. Return version ID. | data-repository | M | D-005, D-006 |
| D-008 | Implement model version detail endpoint. `GET /api/models/{name}/versions/{version}` - return metadata and harbor_ref for direct ORAS pull by clients. Support `latest` alias. | data-repository | S | D-005 |
| D-009 | Implement dataset registration endpoint. `POST /api/datasets/{name}/versions` - accept harbor_ref + metadata, verify artifact exists in Harbor (HEAD), create metadata record in Postgres. | data-repository | M | D-005, D-006 |
| D-010 | Implement dataset version detail endpoint. `GET /api/datasets/{name}/versions/{version}` - return metadata and harbor_ref for direct ORAS pull by clients. Support `latest` alias. | data-repository | S | D-005 |
| D-011 | Implement metadata CRUD. `GET/PUT /api/models/{name}`, `GET/PUT /api/datasets/{name}`. Store and retrieve: type, description, training config, eval metrics, lineage (source trainer, source dataset, parent model). | data-repository | M | D-005 |
| D-012 | Implement version listing and management. `GET /api/models/{name}/versions` - list all versions with metadata. Auto-increment version numbers. Support `latest` alias. | data-repository | M | D-005 |
| D-013 | Implement search and browse endpoints. `GET /api/models` - list, filter, search by name/type/metadata. `GET /api/datasets` - same. Pagination. | data-repository | M | D-005 |
| D-014 | Implement URI resolution endpoint. `GET /api/resolve?uri=repo://iris-osl:v3` - returns artifact metadata and harbor_ref for direct ORAS pull. Used by Solar Control's `repo://` resolver. | data-repository | S | D-008 |
| D-015 | Deploy Data Repository to aiops-k8s. Create Helm chart, ArgoCD app, environment values for dev/uat. | data-repository, aiops-k8s | M | D-007 |

---

### Milestone 1.3: Solar Integration

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| D-016 | Complete `repo://` resolver in solar-control. Connect to Data Repository's resolve endpoint, obtain harbor_ref, send pull command to Solar Host via `POST /models/pull`. Host pulls from Harbor (ORAS) directly. Cache by version. | solar-control | M | S-013, D-014 |
| D-017 | End-to-end integration test. Upload model to Data Repository → create intent with `repo://` URI in Solar Control → model pulled to host → instance started → inference served. | test | M | D-016, S-040 |
| D-018 | Add model catalog endpoint to solar-control for Solar WebUI. `GET /api/catalog/models` - proxy to Data Repository's model list with deployment status enrichment. | solar-control | S | D-013 |
| D-019 | Add artifact-level delete to the Data Repository. `DELETE /api/models/{name}` and `DELETE /api/datasets/{name}` remove the artifact row and cascade its versions; pure unregister, no Harbor side effects. | data-repository | S | - |

---

## Phase 2: SuperNova Control API

> The Slurm-like orchestrator. Depends on Phase 0 + Phase 1.

### Milestone 2.1: Job Management Core

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| N-001 | Scaffold SuperNova Control FastAPI project. Project structure, configuration, Postgres connection, health endpoint, Docker/Helm setup. | supernova-control | M | - |
| N-002 | Design and implement PostgreSQL schema for jobs. Tables: `jobs`, `job_steps`, `job_logs`, `job_metrics`. Store full submission config as JSONB for audit. | supernova-control | M | - |
| N-003 | Implement job state machine. States: `submitted` → `queued` → `preparing` → `step:download_model` → `step:download_dataset` → `step:train` → `step:upload_model` → `evaluating` → `completed` / `failed`. State transitions with timestamps. | supernova-control | M | N-002 |
| N-004 | Implement job submission endpoint. `POST /api/jobs` - validate input (check trainer image tag, verify repo URIs exist via Data Repository, validate steps), create job record, return job ID. | supernova-control | M | N-003, D-014 |
| N-005 | Implement job queue with priority. Jobs sorted by priority then submission time. `GET /api/jobs/queue` - view queue. | supernova-control | S | N-003 |
| N-006 | Implement job scheduler. Background task: pick next queued job, query Solar Control for available training resources (`GET /api/resources`), request resource reservation, assign job to host. Handle reservation failures (requeue with backoff). | supernova-control | L | N-005, S-038 |
| N-007 | Implement model selection strategies as TrainingArguments passthrough. Support `best_metric` (evaluate by metric + direction, default HuggingFace Trainer behavior) and `last_checkpoint`. Strategy is specified in the training template/config, passed through to the Etalon image's TrainingArguments, and the Etalon image reports the winner in `job.json` → `steps.train.best_checkpoint_path`. (S-021 workspace spec defines the inter-step contract.) | supernova-control | M | - |
| N-008 | Implement job status endpoints. `GET /api/jobs/{id}` - full status with per-step progress. `GET /api/jobs` - list with filters (status, trainer, date range). | supernova-control | S | N-003 |
| N-009 | Implement job cancellation. `POST /api/jobs/{id}/cancel` - request cancellation via Solar Control, update state. | supernova-control | S | N-003, S-032 |
| N-010 | Deploy SuperNova Control to aiops-k8s. Create Helm chart, ArgoCD app, environment values, Postgres schema migration, Solar Control API key config. | supernova-control, aiops-k8s | M | N-001 |

---

### Milestone 2.2: Training Execution Flow

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| N-011 | Implement step pipeline orchestration. Translate job config into step execution commands for Solar Control. Map `steps` array + `base_model`/`training_data` URIs into concrete container configs with args and env vars. **S-021 ref:** Use workspace spec Sections 4.3 (per-step env vars), 5.2 (job.json schema), and 7.3 (training.json derivation) to construct step payloads. | supernova-control | L | N-006, S-032 |
| N-012 | Implement per-step status tracking. Listen to Solar Control step events (via REST polling or callback), update job_steps table, transition job state machine per step. | supernova-control | M | N-011 |
| N-013 | Implement training log collection. Store step logs in `job_logs` table. Expose via `GET /api/jobs/{id}/logs?step=train`. | supernova-control | M | N-012 |
| N-014 | _(Deprecated — eval_metrics are now written by the train step directly to `job.json` per S-021 workspace spec. Kept for numbering continuity.)_ | — | — | — |
| N-015 | Implement post-training model upload orchestration. After training step completes, read `job.json` → `steps.train.best_checkpoint_path` (written by Etalon image per S-021), trigger `upload_model` step with the selected checkpoint. Record artifact version in job metadata. | supernova-control | M | N-006, S-030 |
| N-016 | Build Etalon Docker image for `aiops-categorizer` branch. Dockerfile + GitHub Actions workflow. Push to Harbor as `etalon-categorizer:{tag}`. **S-021 ref:** Image must comply with workspace contract ([spec](docs/specs/job-step-workspace.md)) — read `TRAINING_CONFIG` env var, use `/workspace/` paths in training JSON, write `steps.train` (including `best_checkpoint_path`) to `job.json`, respect `WANDB` env gate. See Section 9 for Dockerfile pattern and Section 10 for migration notes. | etalon, infra | M | - |
| N-017 | Build Etalon Docker image for `icinga-classifier` branch. Dockerfile + GitHub Actions workflow. Push to Harbor as `etalon-icinga-classifier:{tag}`. **S-021 ref:** Same workspace contract requirements as N-016. | etalon, infra | M | - |
| N-018 | Set up GitHub Actions CI template for Etalon image builds. Reusable workflow triggered on release/tag. Build on local self-hosted runners. | etalon, infra | M | - |

---

### Milestone 2.3: Deployment Automation

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| N-019 | Implement deployment intent builder. After successful training + upload, construct deployment intent from job config (`target`, `replicas`, `strategy`). | supernova-control | S | N-015 |
| N-020 | Implement deployment trigger. Submit intent to Solar Control's intent API. Poll or receive callbacks for deployment status. | supernova-control | M | N-019, S-042 |
| N-021 | Implement deployment status tracking. Store deployment state in job record. Expose via `GET /api/jobs/{id}/deployment`. | supernova-control | S | N-020 |
| N-022 | Implement rollback endpoint. `POST /api/deployments/{id}/rollback` - submit new intent with the previous model version from Data Repository. | supernova-control | M | N-021, D-012 |

---

### Milestone 2.4: Trainer Templates & Automation

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| N-023 | Design trainer template schema. JSON format mapping: template name → Etalon image, default training config, default steps, default deployment config, resource requirements. | supernova-control | S | - |
| N-024 | Implement template CRUD endpoints. `GET/POST /api/templates`, `GET/PUT/DELETE /api/templates/{id}`. Store in PostgreSQL. | supernova-control | M | N-023 |
| N-025 | Create `aiops-categorizer` template. Pre-filled config for IRIS ticket categorizer training (5 targets: osl, oslt, type, priority, user_grade). | supernova-control | S | N-024, N-016 |
| N-026 | Create `icinga-classifier` template. Pre-filled config for Icinga alert classifier training. | supernova-control | S | N-024, N-017 |
| N-027 | Implement scheduled training runs. `POST /api/schedules` - cron expression + template + overrides. Background scheduler triggers job submission. Store in PostgreSQL. | supernova-control | M | N-024, N-004 |
| N-028 | Implement dataset creation as a pre-step. For scheduled runs, support a `create_dataset` step that runs a configured script (e.g. `create_dataset.py` container for IRIS categorizer). **S-021 ref:** This is an additional step type that writes to `/workspace/data/` (like `download_dataset` but sources from DB, not Harbor). Must write `steps.create_dataset` to `job.json`. | supernova-control | M | N-027 |
| N-029 | Implement retention policy management. `POST /api/retention` - define rules (keep last N versions, keep versions newer than X days). Background task runs cleanup via Data Repository delete API. | supernova-control | M | D-007 |

---

## Phase 3: SuperNova WebUI

> User-friendly interface for training lifecycle management. Depends on Phase 2.

### Milestone 3.1: Core UI

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| W-001 | Scaffold SuperNova WebUI project. React + TypeScript + Vite + Tailwind. Project structure, routing, API client, proxy config for SuperNova Control and Data Repository. | supernova-webui | M | - |
| W-002 | Implement API client module. Typed HTTP client for SuperNova Control API endpoints (jobs, templates, schedules, deployments). | supernova-webui | M | W-001 |
| W-003 | Implement job submission wizard. Multi-step form: select template (or manual config) → select/upload dataset → configure training params → configure deployment → review → submit. | supernova-webui | L | W-002 |
| W-004 | Implement job queue view. Table showing all jobs with status, trainer, submitted time, duration. Filterable by status, trainer. Real-time status updates. | supernova-webui | M | W-002 |
| W-005 | Implement job detail view. Full job info: config, per-step status with progress indicators, eval metrics, deployment status. | supernova-webui | M | W-002 |
| W-006 | Implement live log streaming. Connect to SuperNova Control for job logs. Display with auto-scroll, per-step log tabs, search. | supernova-webui | M | W-005 |

---

### Milestone 3.2: Model & Data Catalog

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| W-007 | Implement model browser. List models from Data Repository with version history, metadata, training lineage. Show deployment status from Solar. | supernova-webui | M | W-002 |
| W-008 | Implement dataset browser. List datasets from Data Repository with metadata. | supernova-webui | M | W-002 |
| W-009 | Implement dataset upload interface. Drag-and-drop or file picker, metadata form, progress indicator. | supernova-webui | M | W-002 |
| W-010 | Implement one-click deploy to Solar. From model detail view, configure replicas and strategy, submit deployment intent. | supernova-webui | S | W-007 |
| W-011 | Implement training run comparison view. Side-by-side comparison of two training jobs: config diff, metric comparison, output comparison. | supernova-webui | M | W-005 |

---

### Milestone 3.3: Operations

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| W-012 | Implement scheduled training configuration UI. List schedules, create/edit with cron builder, template selection, overrides. | supernova-webui | M | W-002 |
| W-013 | Implement deployment history and rollback UI. Timeline of deployments per model alias, rollback button to previous version. | supernova-webui | M | W-002 |
| W-014 | Implement authentication. API key or Keycloak integration based on what's available. Protect all routes. | supernova-webui | M | W-001 |
| W-015 | Deploy SuperNova WebUI to aiops-k8s. Helm chart, ArgoCD app, environment config. | supernova-webui, aiops-k8s | M | W-001 |

---

## Phase 4: Solar WebUI Evolution

> Update Solar WebUI for declarative model and training awareness.
> Can overlap with Phase 3.

| ID | Issue | Repo | Size | Depends on |
|----|-------|------|------|------------|
| U-001 | Add training activity indicators to host views. Show running job steps alongside inference instances. Badge or status for "training in progress". | solar-webui | M | S-033 |
| U-002 | Add model catalog view. New page listing models from Data Repository via solar-control catalog endpoint. Show versions, deployment status, metadata. | solar-webui | M | D-018 |
| U-003 | Implement declarative intent submission UI. Replace or supplement per-host instance creation with intent form: model, replicas, strategy. | solar-webui | L | S-040 |
| U-004 | Implement resource utilization dashboard. Visual breakdown of each host's resource allocation: inference instances, training jobs, reserved, free. | solar-webui | M | S-035 |
| U-005 | Add host draining controls to the Resources page. Drain/resume actions, drain state badges, blocker list before confirming, and stalled-drain reporting. | solar-webui | M | S-043 |
| U-006 | Add intent editing UI. Generalise the intent form into create/edit modes with full hydration, read-only alias, and a warning when an edit restarts an in-flight rollout. | solar-webui | M | S-044 |
| U-007 | Add model and dataset upload. Category-specific requirements and metadata, directory picker with junk filtering and per-file review, streamed upload with per-file progress. | solar-webui | M | S-047 |
| U-008 | Add catalog delete UI. Version list in the model detail panel, repository and version delete with a confirmation modal (blocked state for running instances, host-cache warning, typed confirmation for repo delete, per-version results). | solar-webui | M | S-048 |
| U-009 | webui: process logs on start failures (log_tail + View process logs), live model pull progress, field-level validation errors and advisory warnings, shortfall_reason and strategy_progress messages. Event-driven endpoint pages with fallback polling. | solar-webui | M | S-049, S-051, S-052 |
| U-010 | webui: `huggingface_vision` in the backend-mode picker. Recorded, not implemented in the five-case-fixes PR. | solar-webui | S | - |
| U-011 | webui: intent form error routing (render-tracked banner filter, auto-open collapsed sections, scroll to first error) and advisory warnings carried across the create redirect. | solar-webui | M | S-052, U-009 |

---

## Phase 5: Advanced Features (Future)

No detailed issue breakdown yet. High-level items for future planning:

- Multi-GPU / distributed training support
- Automated retraining triggers (data-threshold-based)
- A/B testing framework (candidate model alongside production)
- Model performance monitoring (accuracy drift detection)
- Dataset management tools (versioning, splits, augmentation)

---

## Summary Statistics

| Phase | Issues | Estimated Effort |
|-------|--------|-----------------|
| Phase 0: Solar Evolution | 44 issues (S-001 → S-044) | ~37-48 days |
| Phase 1: Data Repository | 18 issues (D-001 → D-018) | ~15-20 days |
| Phase 2: SuperNova Control | 29 issues (N-001 → N-029) | ~25-35 days |
| Phase 3: SuperNova WebUI | 15 issues (W-001 → W-015) | ~15-20 days |
| Phase 4: Solar WebUI | 6 issues (U-001 → U-006) | ~7-11 days |
| **Total** | **112 issues** | **~99-134 days** |

Note: Effort estimates assume single developer. With Phase 0 and Phase 1 running in parallel across team members, calendar time can be reduced significantly.

---

## Parallelization Opportunities

```
                  Month 1             Month 2             Month 3             Month 4
                  ──────────────────  ──────────────────  ──────────────────  ──────────
Developer A:      [Phase 0.1─0.3]     [Phase 0.4──0.5]    [Phase 2.1──2.2]    [Phase 2.3─2.4]
Developer B:      [Phase 1.1──1.2]    [Phase 1.3]         [Phase 2 support]   [Phase 3]
Developer C:      [Etalon images]     [Step images]       [Phase 3]           [Phase 4]
                                                                              ─────────
                                                                              MVP ready
```

Phase 0 and Phase 1 can run in parallel from day one. Phase 2 starts once Phase 0.4 (step engine) and Phase 1.2 (repository API) are complete. Phase 3 can begin once Phase 2.1 (core API) ships its first endpoints.
