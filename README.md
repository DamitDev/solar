# SuperNova - AI Training Platform

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Current State Analysis](#2-current-state-analysis)
3. [Architecture Vision](#3-architecture-vision)
4. [Component Breakdown](#4-component-breakdown)
5. [Data Repository - Decision Record](#5-data-repository---decision-record)
6. [Solar Evolution for SuperNova](#6-solar-evolution-for-supernova)
7. [Roadmap](#7-roadmap)
8. [Decisions Log](#8-decisions-log)
9. [Open Questions](#9-open-questions)

---

## 1. Problem Statement

### The bottleneck

All AI model training knowledge lives in one person's head. The current workflow requires:

- Deep understanding of the Etalon training framework (branch-per-use-case architecture)
- Manual dataset creation from IRIS DB or other sources
- Manual Docker compose setup with environment variables
- Manual checkpoint selection and copy to production
- Manual deployment through Solar Host filesystem paths
- Intimate knowledge of HuggingFace Transformers, DeepSpeed/Accelerate, and model architectures

### The goal

Build a **job-based training platform (SuperNova)** that:

- Allows team members to submit training jobs without deep ML knowledge
- Manages the full lifecycle: data in -> training -> model out -> deployed
- Integrates with the existing Solar System for seamless model deployment
- Provides a centralized model and training data catalog
- Runs on the existing on-prem GPU infrastructure (damcpaiops01/02)

---

## 2. Current State Analysis

### 2.1 Infrastructure Inventory


| Host               | Role        | Hardware                        | IP               | Roles                   |
| ------------------ | ----------- | ------------------------------- | ---------------- | ----------------------- |
| damcpmacstudio01   | Solar Host  | Mac Studio M3 Ultra, 512 GB RAM | 172.16.240.8     | `inference`             |
| damcpmacstudio02   | Solar Host  | Mac Studio M3 Ultra, 256 GB RAM | 172.16.240.9     | `inference`             |
| damcpaiops01       | Solar Host  | Nvidia RTX 4090, 24 GB VRAM     | 172.16.240.5     | `inference`, `training` |
| damcpaiops02       | Solar Host  | Nvidia RTX 4090, 24 GB VRAM     | 172.16.240.6     | `inference`, `training` |
| damclk8saiops01-03 | K8s Cluster | -                               | 172.16.252.41-43 | Business logic only     |


**Key constraint:** Training hosts (NVIDIA) also serve production inference. The resource management system must coordinate both workloads on shared GPU hardware.

### 2.2 Current Training Workflows

#### Etalon Framework (`/mnt/nvme/AI/etalon`)

A unified training framework built on HuggingFace Trainer. Uses a **branch-per-use-case** model:


| Branch              | Purpose                       | Model Type                          | Dataset Format         |
| ------------------- | ----------------------------- | ----------------------------------- | ---------------------- |
| `aiops-categorizer` | Ticket classification         | DeBERTa-v2 (SequenceClassification) | Parquet (from IRIS DB) |
| `icinga-classifier` | Alert classification          | InformerAlertClassifier (custom)    | HDF5 / Parquet         |
| `icinga`            | Time series forecasting       | Informer / PatchTST                 | Parquet                |
| `T12`               | Image classification          | ViTForImageClassification           | -                      |
| `hu-DeBERTa-v2`     | MLM pre-training              | DeBERTa-v2                          | -                      |
| `worksheet`         | Worksheet prediction          | -                                   | JSON                   |
| `solaris-eden`      | Image classification (gaming) | ViT                                 | -                      |


#### Training run anatomy (aiops-categorizer example)

```
1. Clone etalon, checkout aiops-categorizer branch
2. Place base model in models/IRIS-BERT-base/
3. Run utils/create_dataset.py (connects to IRIS DB)
   -> data/tickets-dataset/train.parquet
4. Configure .env:
   - CONFIG=configs/base_osl.json
   - ACCELERATE_CONFIG=configs/accelerate_deepspeed_config.json
5. docker compose up --build
   -> accelerate launch trainer.py -c $CONFIG
6. Monitor via console logs
7. Best checkpoint lands in outputs/<name>/best/
8. Manually copy model files to production path
9. Restart Solar Host instance
```

#### Current model formats in production


| Model                | Format                        | Served Via                              |
| -------------------- | ----------------------------- | --------------------------------------- |
| iris-osl:110m        | HF Transformers (safetensors) | Solar Host (huggingface_classification) |
| iris-oslt:110m       | HF Transformers (safetensors) | Solar Host (huggingface_classification) |
| iris-type:110m       | HF Transformers (safetensors) | Solar Host (huggingface_classification) |
| iris-priority:110m   | HF Transformers (safetensors) | Solar Host (huggingface_classification) |
| iris-user-grade:110m | HF Transformers (safetensors) | Solar Host (huggingface_classification) |
| gpt-oss:20b          | GGUF                          | Solar Host (llamacpp)                   |
| gpt-oss:120b         | GGUF                          | Solar Host (llamacpp)                   |
| thinker-v3:30b       | GGUF                          | Solar Host (llamacpp)                   |
| wp:27b               | GGUF                          | Solar Host (llamacpp)                   |


### 2.3 Solar System Current State (v3.0)

Solar Control v3.0 is a **stateless, multi-replica** coordinator deployed in its own Kubernetes namespace (`solar` on `damit-prod`). It runs at least 2 replicas behind a single API endpoint.

**Architecture:**

- **Stateless control plane**: All shared state lives in Redis (host connections, model registry, routing, health) and PostgreSQL (gateway logs, API endpoints, host registry)
- **Socket.IO** for real-time communication: `/hosts` namespace (host ↔ control) and `/webui` namespace (dashboard ↔ control). Redis adapter enables cross-replica broadcasting.
- **Multi-tenant API keys**: `api_endpoints` table - each key is a separate entity (e.g. dev, uat, prod) with isolated log collection and usage metrics
- **Two-phase host registration**: Hosts connect via Socket.IO, unknown hosts are held pending until admin approval
- **Management API key** (`MANAGEMENT_API_KEY`) for `/api/`* operations and WebUI Socket.IO

**What Solar can do today:**

- Host models via llama.cpp (GGUF) and HuggingFace backends (causal, classification, embedding)
- OpenAI-compatible gateway: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/classify`, `/v1/rerank`
- Load-balance requests across multiple hosts (prefer idle → lowest load → round-robin)
- Endpoint-aware routing (only route to instances supporting the requested endpoint)
- Auto-retry on host failure
- Manage instances via REST API (create, start, stop, delete, restart)
- Real-time monitoring via Socket.IO (logs, instance state, host health)
- Per-endpoint gateway stats, request history, and usage analytics
- Multi-replica HA with no sticky sessions required

**What Solar cannot do today (gaps to address for SuperNova):**

- No model file upload/download API (models must exist on host filesystem)
- No model source abstraction (raw filesystem paths only)
- No model catalog or discovery
- No automated model distribution between hosts
- No programmatic "deploy model X from repository to host Y"
- No free space / resource reservation API
- No configurable host roles or capability metadata
- No training container / job step orchestration
- No automated instance management (declarative/intent-based)
- No Data Repository awareness

### 2.4 Existing CI/CD & Infrastructure

- **ArgoCD** App-of-Apps for Kubernetes deployments
- **Harbor** (`imgrepo.damit.hu`) for Docker images, Helm charts (OCI), and SuperNova model/dataset artifacts (ORAS)
- **GitHub Actions** for CI on local self-hosted runners (build, push image, push chart)
- **Sealed Secrets** for secret management
- **Keycloak** for authentication (aiops-gateway) - it's there but it's not used (mainly because it is only available on local network, no internet access)
- **Redis** used by solar-control for shared state and Socket.IO cross-replica broadcasting
- **PostgreSQL** cluster used by multiple services (solar-control gateway logs, will also host SuperNova job history and Data Repository metadata)
- **API keys** for inter-service authentication across the ecosystem

---

## 3. Architecture Vision

### System topology

SuperNova is the **brain** (Slurm-like orchestrator). Solar is the **muscle** (execution layer).

```
                    ┌──────────────────────────────────────────────┐
                    │                SuperNova                     │
                    │                                              │
                    │  ┌──────────────┐   ┌────────────────────┐   │
                    │  │  SuperNova   │──▶│  SuperNova Control │   │
                    │  │  WebUI       │   │  API               │   │
                    │  └──────────────┘   └─────────┬──────────┘   │
                    │                               │              │
                    └───────────────────────────────┼──────────────┘
                                                    │
                              ┌─────────────────────┼
                              │                     │                    
                              ▼                     ▼                    
                    ┌──────────────────┐   ┌──────────────────┐         
                    │ Data Repository  │   │  Solar Control   │         
                    │ (Harbor/ORAS +   │◀──│  API (x2+)       │         
                    │  Postgres)       │   │  Redis+Postgres  │         
                    └──────────────────┘   └────────┬─────────┘         
                                                    │                    
                              ┌─────────────────────┼──────────┐        
                              │                     │          │        
                              ▼                     ▼          ▼        
                    ┌──────────────────┐   ┌──────────┐ ┌──────────┐   
                    │  Solar WebUI     │   │Solar Host│ │Solar Host│   
                    │  (operations)    │   │(aiops01) │ │(mac01)   │   
                    └──────────────────┘   │inference │ │inference │   
                                           │+training │ │only      │   
                                           └──────────┘ └──────────┘
```

### Data flow: training job lifecycle

```
1. User submits training job via SuperNova WebUI
                    │
2. SuperNova Control API queues job
                    │
3. SuperNova submits job to Solar Control with resource requirements
                    │
4. Solar Control arranges resources (may migrate inference instances to free GPU)
   Reports back if resources cannot be fulfilled.
                    │
5. Solar Host executes job steps sequentially:
   a. download_model  → Resolve via Data Repository, pull from Harbor (ORAS)
   b. download_dataset → Resolve via Data Repository, pull from Harbor (ORAS)
   c. train           → Run Etalon container (streams logs via Socket.IO → Solar Control → SuperNova)
   d. convert_model   → (optional) Convert HF output to GGUF
   e. upload_model    → Push to Harbor (ORAS), register with Data Repository
                    │
6. SuperNova receives completion, applies model selection strategy
   (best F1, last checkpoint, etc.)
                    │
7. If deployment config present, SuperNova submits deployment intent
   to Solar Control (target model alias, replicas, strategy)
                    │
8. Solar Control resolves host placement (one replica per host),
   resolves model via Data Repository, pulls from Harbor (ORAS),
   creates/updates instances (rolling or immediate), serves inference
```

### Component responsibilities


| Component                 | Role                                                                                                                                                                    | Analogy              |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| **SuperNova Control API** | Job orchestration, scheduling, decision-making, deployment strategy                                                                                                     | Slurm controller     |
| **SuperNova WebUI**       | Training job submission, monitoring, model catalog, dataset browser                                                                                                     | Slurm dashboard      |
| **Solar Control API**     | Stateless, multi-replica execution layer (Redis + Postgres). Host management, model distribution, job step execution, inference routing. Socket.IO for real-time comms. | Compute node manager |
| **Solar Host**            | Process manager for inference instances AND job step containers, resource reporting. Connects to Solar Control via Socket.IO.                                           | Compute node         |
| **Solar WebUI**           | Operations monitoring, declarative instance management, model catalog                                                                                                   | Operations dashboard |
| **Data Repository**       | Centralized storage for models and training datasets with metadata and versioning                                                                                       | Artifact registry    |


---

## 4. Component Breakdown

### 4.1 SuperNova Control API

The orchestrator. Runs on Kubernetes. Does the thinking, not the work.

**Core responsibilities:**

- Accept and queue training job submissions (with step-based pipeline definition)
- Schedule jobs based on resource availability (queries Solar Control)
- Monitor job progress via logs and metrics streamed through Solar Control
- Trigger deployments via Solar Control (declare target model, replicas, strategy)
- Manage scheduled/automated training runs (with pre-configured dataset creation scripts)
- Maintain trainer template registry
- Manage artifact retention policies
- Store full job history in PostgreSQL for audits and reporting

**Job submission interface:**

```json
{
  "name": "iris-osl-retrain-2026-03",
  "base_model": "repo://IRIS-BERT-base:v1",
  "training_data": "repo://iris-tickets:2026-03",
  "trainer": "etalon-categorizer:latest",
  "training_config": {
    "target_label": "osl",
    "max_steps": 50000,
    "batch_size": 64,
    "learning_rate": 2.5e-5
  },
  "model_selection": {
    "strategy": "best_metric",
    "metric": "f1",
    "direction": "max"
  },
  "steps": [
    "download_model",
    "download_dataset",
    "train",
    "upload_model"
  ],
  "deployment": {
    "target": "iris-osl:110m",
    "replicas": 2,
    "strategy": "rolling"
  }
}
```

**Job steps:** Each step is a specialized Docker container executed sequentially on the Solar Host. This makes training pipelines composable and future-proof.


| Step               | Container                                   | Purpose                                                    |
| ------------------ | ------------------------------------------- | ---------------------------------------------------------- |
| `download_model`   | System image                                | Resolve via Data Repository, pull base model from Harbor (ORAS) |
| `download_dataset` | System image                                | Resolve via Data Repository, pull training data from Harbor (ORAS) |
| `train`            | Etalon image (e.g. `etalon-categorizer:v2`) | Execute training run                                            |
| `convert_model`    | System image                                | Convert HF model to GGUF (optional, for llama.cpp serving)      |
| `upload_model`     | System image                                | Push trained model to Harbor (ORAS), register with Data Repository |


Steps can be composed freely - e.g. a job that only needs GGUF conversion without training would use `["download_model", "convert_model", "upload_model"]`.

**Model selection strategies:**

- `best_metric`: Select checkpoint with best eval metric (e.g. highest F1, lowest loss). HuggingFace Trainer has built-in support; Etalon's training config will be extended for this.
- `last_checkpoint`: Use the final checkpoint regardless of metrics.

**Deployment strategies:**

- `rolling` (default): Replace instances one by one, ensuring zero downtime. Solar Control decides which hosts to update and in what order.
- `immediate`: Stop all current instances of the target model, replace with new version, start. Faster but causes brief downtime.

**Replica placement rule:** Each replica must run on a separate host - there is no value in running the same model twice on the same hardware. If the requested replica count exceeds available hosts, Solar Control starts as many as possible (one per host) and reports the shortfall.

Solar Control handles all host selection and scheduling autonomously. SuperNova only declares the desired end state.

**Key design details:**

- **Trainers** are Docker images built from Etalon branches, tagged and stored in Harbor (e.g. `etalon-categorizer:v2`, `etalon-icinga-classifier:v1`). Built via GitHub Actions on local runners.
- **Etalon is a black box**: Solar Host mounts input volumes (data, base model, config) and collects output volumes (checkpoints, trained model). Etalon doesn't know about the platform.
- **Dataset creation**: Automated runs can include pre-configured scripts (e.g. `create_dataset.py` for IRIS tickets). User-submitted jobs follow a **bring-your-own-dataset** approach — push to Harbor (ORAS) and register with Data Repository first, reference in job config.
- **Experiment tracking**: No W&B dependency. SuperNova parses console logs and checkpoint eval metrics directly. Mature Etalon branches produce stable, parseable output.
- **Communication**: SuperNova talks to Solar Control API only (authenticated via API key). Never directly to Solar Hosts.
- **Job history**: All job submissions, parameters, results, and metrics stored in PostgreSQL for audit trails and reporting.

### 4.2 SuperNova WebUI

Standalone web application. Separate from Solar WebUI - different user stories.

**Solar WebUI** is for operations: "What's running? What are my resources? I want this model deployed."
**SuperNova WebUI** is for training: "Retrain iris-osl with new data. What's in the queue? How did last training go?"

**Features:**

- Training job submission wizard (guided form)
- Job queue and monitoring dashboard (status, live logs, eval metrics)
- Job history and run comparison
- Model catalog browser (versions, metadata, deployment status)
- Dataset browser and upload interface
- One-click deployment to Solar
- Scheduled training configuration

**Tech stack:** React, TypeScript, Vite, Tailwind CSS (consistent with Solar WebUI and Orchestrator WebUI).

**Deployment:** Both SuperNova Control API and SuperNova WebUI are deployed via the existing `aiops-k8s` GitOps repo using the ArgoCD App-of-Apps pattern, same as all other AIOps services.

### 4.3 Data Repository

Standalone microservice. Centralized catalog for model artifacts and training datasets.

**Two types of content:**

1. **Models**: Trained models (HF Transformers format, GGUF files) with versioning and metadata
2. **Datasets**: Training datasets (Parquet, HDF5, JSON) with metadata

**Architecture:**

- **Blob storage**: Harbor (`imgrepo.damit.hu`) via ORAS — models and datasets stored as OCI artifacts under the `supernova` project. Consumers push/pull directly to/from Harbor. Zero new storage infrastructure.
- **Metadata storage**: PostgreSQL (in existing cluster) — artifact names, versions, category (model/dataset), training config, eval metrics, lineage, cross-references
- **API**: FastAPI REST metadata catalog. Does NOT proxy blob data. Provides artifact registration (with Harbor ref verification), metadata CRUD, catalog/search, and URI resolution.
- **Harbor client**: All Harbor interactions (artifact verification, deletion, ORAS push/pull) use the shared [`harbor-oci-client`](https://github.com/DamitDev/harbor-oci-client) Python library, also used by Solar Control and step containers.

**OCI artifact layout in Harbor:**

Each artifact is a repository directly under the `supernova` project. The category (model vs dataset) is tracked in Data Repository metadata, not in the Harbor path.

```
imgrepo.damit.hu/supernova/iris-osl:v3             (model)
imgrepo.damit.hu/supernova/IRIS-BERT-base:v1        (base model)
imgrepo.damit.hu/supernova/iris-tickets:2026-03     (dataset)
```

**Core API:**

- Artifact registration — accept harbor_ref + metadata after a client pushes to Harbor; verify the artifact exists in Harbor before accepting
- Metadata CRUD (category, description, training config, eval metrics, lineage, source trainer)
- Version management (maps to OCI tags in Harbor)
- Catalog — list, search, browse models and datasets (category-filtered queries via PostgreSQL)
- URI resolution: `repo://iris-osl:v3` → returns harbor_ref (`imgrepo.damit.hu/supernova/iris-osl:v3`) + metadata for direct ORAS pull
- Consumed by SuperNova Control (job validation, metadata queries), Solar Control (model resolution), and step containers (resolve before direct Harbor pull/push)

**Interaction pattern:** Consumers (step containers, Solar Control) interact with Data Repository for metadata only. Blob upload/download happens directly between the consumer and Harbor via ORAS. This keeps the Data Repository lightweight — no temp storage, no streaming, no bandwidth bottleneck.

**Retention:** Managed by SuperNova, not the Data Repository itself. Retention policy is configurable per-job at submission time. Cleanup is executed as a step-based action (specialized Docker container), keeping the Data Repository API stateless and simple.

**Deployment:** Deployed via `aiops-k8s` GitOps repo. No separate UI — SuperNova WebUI provides browsing.

### 4.4 Solar System (Evolution for SuperNova)

Solar v3.0 already provides a solid foundation: stateless multi-replica control plane, Redis-backed state, Socket.IO communication, multi-tenant API keys, and per-endpoint metrics. The following capabilities need to be added for SuperNova integration. See [Section 6](#6-solar-30-evolution) for full details.

**New Solar Host capabilities needed:**

- Job step execution (run specialized Docker containers in sequence: download, train, convert, upload)
- Configurable host roles: `inference`, `training`, or both
- Resource reporting (available VRAM/RAM, disk space, active job steps)
- Model file management (upload, download, list) via Harbor (ORAS) with Data Repository for metadata

**New Solar Control capabilities needed:**

- Model source abstraction: `repo://`, `huggingface://`, `local://`
- Data Repository awareness (resolve model refs, get harbor_ref for direct ORAS pull)
- Resource reservation and coordination (free GPU for training, migrate inference if needed)
- Job step orchestration on Solar Hosts (via SuperNova instructions, communicated through Socket.IO)
- Declarative intent-based instance management (submit desired state, Solar arranges)
- Host-to-host model distribution

**Solar WebUI evolution:**

- Awareness of training activity on hosts (indicate when a host is running a training job)
- Model catalog from Data Repository
- Shift from per-host instance configuration to declarative intent monitoring

---

## 5. Data Repository - Decision Record

### Decision: Build custom, backed by Harbor (ORAS) + PostgreSQL

**Evaluated options:**


| Option                                 | Fit      | Verdict                                                                                |
| -------------------------------------- | -------- | -------------------------------------------------------------------------------------- |
| MatrixHub (HF clone)                   | High     | Too alpha (2025), project goals far from done. Revisit in future.                      |
| **Custom API (Harbor + ORAS backend)** | **Best** | **Exact fit. Harbor already deployed, ORAS for OCI artifacts, Postgres for metadata.** |
| Harbor + ORAS (raw, no facade)         | Medium   | Already deployed, but no metadata/UI without a custom API layer.                       |
| MLflow                                 | Low      | Too heavy, conflicts with custom platform design.                                      |
| Jozu Hub                               | Medium   | Enterprise overkill for current scale.                                                 |
| DVC                                    | Low      | Git-native, not a service API.                                                         |
| GitLab + LFS                           | Low      | Not designed for ML model management.                                                  |


**Rationale:**

- A lightweight custom API fits the team's pattern (Solar was built custom too)
- Stores both models AND training datasets in one service
- **Harbor** (`imgrepo.damit.hu`) is already deployed and handles blob storage, integrity, replication, and access control. No new storage infrastructure needed.
- **ORAS** (OCI Registry As Storage) enables pushing/pulling arbitrary artifacts (model files, datasets) to Harbor as OCI artifacts with custom media types
- **PostgreSQL** handles metadata, versioning, lineage, and search (in existing cluster)
- The Data Repository API is a thin facade: ORAS for blobs, Postgres for metadata, REST API for consumers
- Tight integration with both Solar Control (model consumer) and SuperNova Control (model producer + training data consumer)
- No company bucket storage (S3/MinIO) available; Harbor is the existing artifact infrastructure
- The [`harbor-oci-client`](https://github.com/DamitDev/harbor-oci-client) shared Python library provides Harbor API and ORAS operations to all Python consumers (Data Repository, Solar Control, step containers), avoiding code duplication across repos

**Harbor operations:**

- A `supernova` project will be created in Harbor for model and dataset OCI artifacts. Project lead has direct authority on Harbor for this.
- Harbor storage quotas, replication, and backup are managed by the DevOps team - out of SuperNova's scope.
- No separate UI for the Data Repository. SuperNova WebUI provides model and dataset browsing.
- Artifact retention is managed by SuperNova (configurable per-job, cleanup via step-based actions).

**Future consideration:** If MatrixHub matures, evaluate it as a HuggingFace-compatible layer on top of or alongside the custom repo. The custom repo's API contract would remain stable regardless.

---

## 6. Solar Evolution for SuperNova

Solar v3.0 already provides a strong foundation: stateless multi-replica control plane (Redis + Postgres), Socket.IO real-time communication, multi-tenant API keys with per-endpoint metrics, and two-phase host registration. It is deployed in its own Kubernetes namespace (`solar` on `damit-prod`) with at least 2 replicas.

The following capabilities need to be built on top of this foundation to support SuperNova.

### 6.1 Configurable Host Roles

**Current state:** Host type (NVIDIA/Mac) inferred from memory type. No explicit capability tracking.

**Target state:** Configurable `roles` per host.

```json
{
  "roles": ["inference", "training"],
  "gpu_type": "nvidia_cuda",
  "training_capable": true
}
```

- Roles determine what Solar Control can schedule on a host
- Future-proof: a dedicated training box would have `roles: ["training"]` only
- Mac hosts: `roles: ["inference"]` only
- NVIDIA hosts: `roles: ["inference", "training"]` (configurable)

### 6.2 Model Source Abstraction

**Current state:** Models referenced by raw filesystem paths (llama.cpp) or HuggingFace model IDs.

**Target state:** Unified model source resolution.

```
repo://iris-osl:v3              → Resolve via Data Repository, pull from Harbor (ORAS)
huggingface://microsoft/phi-3   → Pull from HuggingFace Hub
local:///path/to/model.gguf     → Use local filesystem path (legacy/fallback)
```

- Solar Control resolves the source and ensures the model is available on the target host
- Enables pulling 3rd party models from HuggingFace Hub directly (already done informally)
- Data Repository is the metadata authority for `repo://` URIs; blobs are pulled directly from Harbor

### 6.3 Model File Management

**Current state:** No upload/download API. Models must be manually placed on host filesystems.

**Target state:**

- Solar Host exposes model file APIs (list, upload, download) backed by a managed models directory
- Solar Control orchestrates model distribution between hosts
- Solar Control resolves models via Data Repository, pulls from Harbor (ORAS via `harbor-oci-client`) or HuggingFace, and pushes to target hosts
- Free space awareness in health reporting

### 6.4 Job Step Execution

**Current state:** Solar Host manages inference processes only.

**Target state:** Solar Host executes job steps - sequential Docker containers that form a training pipeline.

- Step-based execution engine: run a sequence of specialized containers (`download_model`, `download_dataset`, `train`, `convert_model`, `upload_model`)
- Each step is a Docker container with defined inputs/outputs and shared volumes between steps
- Docker container lifecycle: pull image, create, start, stop, remove
- Volume mounting: shared workspace directory across steps (data, models, config, outputs)
- Log streaming: container stdout/stderr → Socket.IO → Solar Control → SuperNova
- Resource isolation: NVIDIA Container Toolkit for GPU/VRAM allocation to containers
- Step status reporting (per-step progress, not just per-job)
- Extensible: new step types can be added as new Docker images without changing Solar Host code

### 6.5 Resource Management

**Current state:** Basic memory usage reporting. No reservation or coordination.

**Target state:**

- Available resource calculation (total VRAM - used by inference instances)
- Resource reservation API ("reserve X GB VRAM for training on this host")
- Inference instance migration (move instances to other hosts to free resources for training)
- Priority system (production inference vs. training jobs)

**Coordination protocol:** Solar handles resource coordination autonomously. SuperNova submits jobs with resource requirements - Solar arranges instances and models across hosts to free the needed resources. If resources cannot be fulfilled, Solar reports back to SuperNova with the reason. SuperNova never directly manages host resources.

### 6.6 Declarative Intent-Based Management

**Current state:** Imperative per-host instance management ("create instance X on host Y").

**Target state:** Submit desired state, Solar Control arranges.

- "I want `iris-osl:110m` with 2 replicas" → Solar Control places one instance per host on two suitable hosts
- "I want to train on a host with 20 GB free VRAM" → Solar Control frees resources and allocates
- Solar WebUI shifts from configuring instances to monitoring how Solar Control arranges them

### 6.7 Data Repository Integration

- Solar Control resolves `repo://` URIs via Data Repository API, obtaining harbor_ref + metadata for direct Harbor (ORAS) pull
- Step containers pull/push blobs directly from/to Harbor; register artifacts with Data Repository after upload
- All Harbor/ORAS operations in Solar Control and step containers use the shared [`harbor-oci-client`](https://github.com/DamitDev/harbor-oci-client) library (same as Data Repository)
- Model catalog awareness in Solar WebUI (via Data Repository catalog API)

---

## 7. Roadmap
Roadmap can be seen [here](ROADMAP.md)

---

## 8. Decisions Log

Architectural decisions made during the planning phase.


| #   | Decision                            | Choice                                                                 | Rationale                                                                                                                                                                                                                                |
| --- | ----------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Project name                        | **SuperNova**                                                          | Fits the Solar celestial theme. Models go supernova in training, their remnants become stars in Solar.                                                                                                                                   |
| 2   | Data Repository architecture        | **Standalone service** (Harbor/ORAS + Postgres)                        | Both Solar and SuperNova consume it. Harbor already deployed for blobs. Postgres for metadata. No new storage infra. No company S3/MinIO available.                                                                                      |
| 3   | Trainer Host agent                  | **Integrated into Solar Host**                                         | Training runs in Docker containers, inference runs natively. Docker provides process isolation. Avoids inter-agent communication overhead. Solar Host already has the infrastructure (Socket.IO, health reporting, resource monitoring). |
| 4   | Communication topology              | **SuperNova orchestrates, Solar executes**                             | SuperNova is the brain (Slurm-like). Solar Control is the muscle. SuperNova talks only to Solar Control API, never directly to Solar Hosts.                                                                                              |
| 5   | Repository awareness                | **Both SuperNova and Solar are repo-aware**                            | Solar needs repo access for model deployment (`repo://` URIs) and HuggingFace Hub pulls. SuperNova needs it for training data and model uploads.                                                                                         |
| 6   | Model management in Solar           | **Model source abstraction** (`repo://`, `huggingface://`, `local://`) | Replaces raw filesystem paths. Enables pulling 3rd party models from HuggingFace Hub. Makes Data Repository the primary model source.                                                                                                    |
| 7   | WebUI separation                    | **Separate SuperNova WebUI and Solar WebUI**                           | Different user stories: operations vs. training. Keeps each UI focused.                                                                                                                                                                  |
| 8   | Host capabilities                   | **Configurable roles** (`inference`, `training`)                       | Not auto-detected. Future-proof for dedicated training or inference-only hosts. Current: Macs = inference, NVIDIA = both.                                                                                                                |
| 9   | Etalon integration                  | **Black box, Docker images per branch**                                | Etalon stays unchanged. Platform mounts input volumes and collects output volumes. Tagged Docker images in Harbor (e.g. `etalon-categorizer:v2`).                                                                                        |
| 10  | Experiment tracking                 | **No W&B for production runs**                                         | Console logs + checkpoint eval metrics are sufficient for mature Etalon branches. SuperNova parses these directly. No external dependency or license needed.                                                                             |
| 11  | Dataset creation                    | **Dual approach**                                                      | Automated/scheduled runs can include pre-configured dataset scripts. User-submitted jobs use bring-your-own-dataset (upload to repo first).                                                                                              |
| 12  | Solar WebUI evolution               | **Declarative intent-based**                                           | Shift from "configure instance X on host Y" to "submit intent, monitor how Solar Control arranges". Training awareness as overlay.                                                                                                       |
| 13  | External model registry (MatrixHub) | **Not now, revisit later**                                             | Evaluated MatrixHub - still in super alpha, project goals far from done. Custom solution is more specialized and simpler for current needs.                                                                                              |
| 14  | Deployment model                    | **Declarative intent, no host specification**                          | Deployment declares target model alias, replica count, and strategy. One replica per host (never duplicate on same hardware). Partial fulfillment with error if not enough hosts. Strategies: `rolling` (default) and `immediate`.       |
| 15  | Training pipeline model             | **Step-based pipeline**                                                | Jobs define a sequence of steps (`download_model`, `download_dataset`, `train`, `convert_model`, `upload_model`). Each step is a specialized Docker container. Composable and extensible.                                                |
| 16  | Job queue backend                   | **PostgreSQL**                                                         | Stores full job history with all parameters. Enables audits and reports. No Redis needed.                                                                                                                                                |
| 17  | Inter-service auth                  | **API keys**                                                           | Consistent with ecosystem. SuperNova ↔ Solar Control via API key.                                                                                                                                                                        |
| 18  | Resource coordination               | **Solar handles autonomously**                                         | SuperNova submits resource requirements. Solar arranges instances/models to free resources. Reports back if impossible.                                                                                                                  |
| 19  | GGUF conversion                     | **Job step (`convert_model`)**                                         | Future-proofed via composable steps. Not a separate system - just another container in the pipeline.                                                                                                                                     |
| 20  | GitOps                              | **Extend `aiops-k8s`**                                                 | SuperNova Control + Data Repository deployed via existing ArgoCD App-of-Apps pattern.                                                                                                                                                    |
| 21  | Etalon image CI/CD                  | **GitHub Actions on local runners**                                    | Same pattern as all other services. Per-branch images.                                                                                                                                                                                   |
| 22  | Artifact retention                  | **SuperNova-managed**                                                  | Retention policy configurable per-job. Cleanup executed as a step-based action.                                                                                                                                                          |
| 23  | Harbor client library               | **Shared package (`harbor-oci-client`)**                               | Data Repository, Solar Control, and step containers all need Harbor API and/or ORAS operations. Extracted into a shared library to avoid maintaining identical code in 3+ repos. Installed via `pip install git+https://github.com/DamitDev/harbor-oci-client.git`. |


---

## 9. Open Questions

- ~~Define custom OCI media types for model artifacts vs training datasets~~ — Resolved in D-003. Four media types defined and validated: `model.config.v1+json`, `model.weights.v1+tar+gzip`, `dataset.config.v1+json`, `dataset.content.v1+tar+gzip`. See `data-repository/docs/harbor.md`.
- Detailed design of step shared volume layout (how steps pass data to each other on host filesystem)
- Model selection strategy implementation details (how SuperNova communicates selection criteria to Etalon via training config extension)

