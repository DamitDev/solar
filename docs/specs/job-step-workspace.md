# Job Step Workspace Specification

| Field       | Value                                    |
|-------------|------------------------------------------|
| Issue       | S-021                                    |
| Status      | Draft                                    |
| Created     | 2026-05-07                               |
| Depends on  | —                                        |
| Depended by | S-023, S-028, S-029, S-030, S-031, N-004 |

## 1. Overview

Solar Host executes training jobs as sequences of opaque Docker containers (steps). Each step reads and writes files through mounted directories. The platform must not depend on container internals beyond this workspace contract.

This document defines the canonical workspace layout, host-side conventions, environment variable contract, inter-step communication mechanisms, lifecycle rules, and safety invariants. Every step image (both platform-owned `supernova-steps` images and Etalon training images) must conform to this contract.

### 1.1 Design Principles

- **Opaque containers.** The platform never peeks inside step images. It communicates through mounted directories, environment variables, exit codes (0 = success, non-zero = failure), and stdout/stderr streams.
- **Shared workspace, sequential steps.** One workspace per job, created before the first step and mounted identically into every step. Steps communicate by writing to well-known locations within the workspace.
- **Workspace is ephemeral.** Workspace is per-job, separate from the managed inference `MODELS_DIR`, and cleaned up after a configurable retention period.
- **Minimal prescribed structure.** The platform prescribes only the top-level directories. Sub-structures are defined by the steps themselves (e.g. the train step decides its checkpoint layout).
- **Config directory is shared state.** Steps that need to communicate metadata to downstream steps do so by writing into `/workspace/config/`.

---

## 2. Host-Side Layout

### 2.1 Root Path

The host-side root for all job workspaces is configured via the `JOBS_DIR` environment variable on Solar Host.

| Setting    | Env Var    | Default  | Description                              |
|------------|------------|----------|------------------------------------------|
| Jobs root  | `JOBS_DIR` | `./jobs` | Absolute or relative path to workspace root |

The default `./jobs` is resolved relative to Solar Host's working directory at startup. As with `MODELS_DIR`, the path is resolved to an absolute path before use.

### 2.2 Per-Job Directory

Each job gets a directory named by its job ID under `JOBS_DIR`:

```
JOBS_DIR/
└── <job-id>/
    ├── models/
    ├── data/
    ├── output/
    ├── config/
    │   └── job.json
    └── logs/
```

| Directory        | Purpose                                                                 |
|------------------|-------------------------------------------------------------------------|
| `models/`        | Base model files (pulled by `download_model` step)                      |
| `data/`          | Training dataset files (pulled by `download_dataset` step)              |
| `output/`        | Training outputs: checkpoints, converted models, artifacts for upload   |
| `config/`        | Shared configuration: job manifest, step metadata, inter-step state     |
| `logs/`          | Per-step captured stdout/stderr (written by Solar Host step executor)   |

### 2.3 Relationship to MODELS_DIR

`JOBS_DIR` and `MODELS_DIR` are separate namespaces with different lifecycles:

|                    | JOBS_DIR                     | MODELS_DIR                       |
|--------------------|------------------------------|----------------------------------|
| Purpose            | Ephemeral training workspace | Managed inference model cache    |
| Lifetime           | Per-job, cleaned up after TTL| Persistent, manifest-tracked     |
| Content            | Step input/output artifacts  | Deployed inference models        |
| Managed by         | Solar Host step executor     | Solar Host models manager        |

---

## 3. Inside-Container Layout

### 3.1 Canonical Mount Points

All step containers receive the same four canonical mount points under `/workspace/`. These are the **only** paths step images may read from or write to (excluding stdout/stderr).

```
/workspace/
├── models/     → host: JOBS_DIR/<job-id>/models
├── data/       → host: JOBS_DIR/<job-id>/data
├── output/     → host: JOBS_DIR/<job-id>/output
├── config/     → host: JOBS_DIR/<job-id>/config
```

| Mount point           | Read/Write | Purpose                                           |
|-----------------------|------------|---------------------------------------------------|
| `/workspace/models/`  | RO or RW   | Base model files for training                     |
| `/workspace/data/`    | RO or RW   | Training datasets                                 |
| `/workspace/output/`  | RW         | All step outputs: checkpoints, conversions, etc.  |
| `/workspace/config/`  | RW         | Shared state: job manifest, step metadata         |

The `/workspace/logs/` directory is **not mounted** into step containers. Logs are captured by the Solar Host step executor from container stdout/stderr and written to `JOBS_DIR/<job-id>/logs/` on the host side.

### 3.2 Mount Permissions

| Mount point           | Default permissions | Step controls via                   |
|-----------------------|---------------------|-------------------------------------|
| `/workspace/models/`  | Read-write          | Step image's Dockerfile / entrypoint |
| `/workspace/data/`    | Read-only           | Step image's Dockerfile / entrypoint |
| `/workspace/output/`  | Read-write          | Step image's Dockerfile / entrypoint |
| `/workspace/config/`  | Read-write          | Step image's Dockerfile / entrypoint |

The step executor (S-023) may set `models/` and `data/` to read-only for steps that should only consume them (e.g. the `train` step should not modify the base model). This is a safety measure, not a functional requirement. Steps must not assume write access to `models/` or `data/` unless they are download/population steps.

---

## 4. Environment Variable Contract

Step containers receive environment variables from Solar Host's step executor. Variables fall into three categories: workspace paths, infrastructure credentials, and step-specific configuration.

### 4.1 Workspace Paths (all steps)

| Variable             | Value                   | Description                              |
|----------------------|-------------------------|------------------------------------------|
| `JOB_ID`             | `<job-id>`              | Unique job identifier                    |
| `WORKSPACE_MODELS`   | `/workspace/models`     | Canonical path to models directory       |
| `WORKSPACE_DATA`     | `/workspace/data`       | Canonical path to datasets directory     |
| `WORKSPACE_OUTPUT`   | `/workspace/output`     | Canonical path to outputs directory      |
| `WORKSPACE_CONFIG`   | `/workspace/config`     | Canonical path to config directory       |
| `JOB_CONFIG`         | `/workspace/config/job.json` | Path to the job manifest file       |
| `STEP_NAME`          | `download_model` etc.   | Name of the current step                 |
| `STEP_INDEX`         | `0`, `1`, `2`, ...      | Zero-based index of current step in pipeline |

### 4.2 Infrastructure Credentials (all steps)

| Variable              | Description                                      |
|-----------------------|--------------------------------------------------|
| `HARBOR_URL`          | Harbor registry base URL (`https://imgrepo.damit.hu`) |
| `HARBOR_USERNAME`     | Harbor robot account username                    |
| `HARBOR_PASSWORD`     | Harbor robot account password                    |
| `DATA_REPOSITORY_URL` | Data Repository API base URL                     |
| `HF_TOKEN`            | HuggingFace Hub token (optional, for gated models) |
| `HF_HOME`             | HuggingFace cache directory. Mounted from host-global `HF_CACHE_DIR` (like `MODELS_DIR`), not per-workspace. |
| `WANDB_API_KEY`       | Weights & Biases API key (optional, for experiment tracking) |

### 4.3 Step-Specific Variables

Each step receives additional variables defined by the job's step configuration. Solar Host's step executor (S-023) passes these through from the job definition it receives from Solar Control.

#### `download_model`

| Variable           | Example                                  | Description                      |
|--------------------|------------------------------------------|----------------------------------|
| `MODEL_URI`        | `repo://IRIS-BERT-base:v1`              | Source URI of the base model     |
| `MODEL_OUTPUT_DIR` | `/workspace/models/IRIS-BERT-base`      | Target directory inside workspace|

#### `download_dataset`

| Variable              | Example                                | Description                         |
|-----------------------|----------------------------------------|-------------------------------------|
| `DATASET_URI`         | `repo://iris-tickets:2026-03`         | Source URI of the dataset           |
| `DATASET_OUTPUT_DIR`  | `/workspace/data/tickets-dataset`     | Target directory inside workspace   |

#### `train`

| Variable           | Example                                  | Description                            |
|--------------------|------------------------------------------|----------------------------------------|
| `TRAINING_CONFIG`  | `/workspace/config/training.json`        | Path to the Etalon training config JSON |
| `MODEL_DIR`        | `/workspace/models/IRIS-BERT-base`       | Path to base model directory           |
| `DATASET_DIR`      | `/workspace/data/tickets-dataset`        | Path to dataset directory              |
| `OUTPUT_DIR`       | `/workspace/output/base_osl`             | Path to write checkpoints              |
| `WANDB`            | `false`                                  | Enable/disable W&B logging             |
| `RESUME`           | (optional)                               | Checkpoint path to resume from         |

The step executor derives `TRAINING_CONFIG`, `MODEL_DIR`, `DATASET_DIR`, and `OUTPUT_DIR` from the job definition. See [Section 7.4](#74-train-step) for how the Training JSON uses these paths.

#### `convert_model`

| Variable           | Example                                  | Description                            |
|--------------------|------------------------------------------|----------------------------------------|
| `MODEL_INPUT`      | `/workspace/output/base_osl/best`       | Path to HF model to convert            |
| `MODEL_OUTPUT`     | `/workspace/output/base_osl.gguf`       | Path for the GGUF output file          |
| `QUANTIZATION`     | `Q4_K_M`                                 | Quantization level (llama.cpp format)  |

#### `upload_model`

| Variable              | Example                                           | Description                          |
|-----------------------|---------------------------------------------------|--------------------------------------|
| `MODEL_SOURCE_PATH`   | `/workspace/output/base_osl.gguf`                 | Path to the model artifact to upload |
| `HARBOR_TARGET_REF`   | `imgrepo.damit.hu/supernova/iris-osl:v4`          | Target Harbor OCI reference          |
| `ARTIFACT_NAME`       | `iris-osl`                                        | Data Repository artifact name        |
| `VERSION`             | `v4`                                              | Version string for registration      |
| `ARTIFACT_CATEGORY`   | `model`                                           | `model` or `dataset`                 |
| `METADATA_PATH`       | `/workspace/config/upload-metadata.json`          | Path to metadata JSON for registration |

---

## 5. Inter-Step Communication

### 5.1 Config Directory as Shared State

Steps communicate structured data to downstream steps by reading and writing files in `/workspace/config/`. The directory is mounted read-write into every step container, making it the sole channel for step-to-step metadata.

### 5.2 Job Manifest (`job.json`)

Solar Host writes `job.json` into the config directory before the first step runs. This file is the job-level source of truth. All steps may read it. Some steps extend it.

**Schema:**

```json
{
  "job_id": "job-a1b2c3d4",
  "name": "iris-osl-retrain-2026-03",
  "created_at": "2026-05-07T10:00:00Z",
  "host": "damcpaiops01",
  "pipeline": [
    "download_model",
    "download_dataset",
    "train",
    "upload_model"
  ],
  "base_model_uri": "repo://IRIS-BERT-base:v1",
  "training_data_uri": "repo://iris-tickets:2026-03",
  "training_config_path": "/workspace/config/training.json",
  "model_selection": {
    "strategy": "best_metric",
    "metric": "f1",
    "direction": "max"
  },
  "deployment": {
    "target": "iris-osl:110m",
    "replicas": 2,
    "strategy": "rolling"
  },
  "retention_hours": 24,
  "steps": {}
}
```

The `steps` object (initially empty) accumulates per-step results. Each step that produces metadata for downstream consumption writes into `steps.<step_name>`.

### 5.3 Step Results Convention

Steps write their output metadata to `job.json` under `steps.<step_name>`. The step executor (S-023) does NOT modify `job.json` — only step containers do.

#### `download_model` result

```json
{
  "steps": {
    "download_model": {
      "status": "completed",
      "model_dir": "/workspace/models/IRIS-BERT-base",
      "source_uri": "repo://IRIS-BERT-base:v1",
      "harbor_ref": "imgrepo.damit.hu/supernova/IRIS-BERT-base:v1",
      "digest": "sha256:abc123...",
      "size_bytes": 4815162342
    }
  }
}
```

#### `download_dataset` result

```json
{
  "steps": {
    "download_dataset": {
      "status": "completed",
      "dataset_dir": "/workspace/data/tickets-dataset",
      "source_uri": "repo://iris-tickets:2026-03",
      "harbor_ref": "imgrepo.damit.hu/supernova/iris-tickets:2026-03",
      "record_count": 15000,
      "format": "parquet"
    }
  }
}
```

#### `train` result (including best checkpoint)

```json
{
  "steps": {
    "train": {
      "status": "completed",
      "output_dir": "/workspace/output/base_osl",
      "best_checkpoint": "checkpoint-12000",
      "best_checkpoint_path": "/workspace/output/base_osl/checkpoint-12000",
      "total_steps": 16000,
      "eval_metrics": {
        "eval_loss": 0.42,
        "eval_f1": 0.947,
        "eval_accuracy": 0.95
      },
      "duration_seconds": 1847
    }
  }
}
```

The `train` step (Etalon image) is responsible for determining the best checkpoint and writing the path into `job.json`. The `upload_model` step reads `steps.train.best_checkpoint_path` to know what to upload.

### 5.4 Upload Metadata

The `upload_model` step may also receive a dedicated metadata JSON file (`upload-metadata.json` in config/) produced by the train step or by SuperNova Control. This file contains the artifact metadata to be registered with Data Repository (see [schema.md](../../data-repository/docs/schema.md) for the metadata conventions).

---

## 6. Lifecycle

### 6.1 Creation

The Solar Host step executor (S-023) creates the workspace before the first step:

1. Resolve `JOBS_DIR` to absolute path.
2. Check available disk space on the `JOBS_DIR` partition. Reject if below `min_free_disk_gb` (default: 2 GB, configurable per-job).
3. Create `JOBS_DIR/<job-id>/{models,data,output,config,logs}`.
4. Set ownership to the container runtime UID (typically `1000:1000`, configurable).
5. Set permissions: `0755` on directories, `0644` on files.
6. Write `job.json` into `config/`.
7. Optionally write `training.json` into `config/` if the training config was supplied inline in the job definition.

### 6.2 Per-Step Execution

For each step in the pipeline:

1. **Pre-step disk check.** Verify free space on `JOBS_DIR` partition meets minimum.
2. **Mount workspace.** Bind-mount `JOBS_DIR/<job-id>/models` → `/workspace/models/`, etc.
3. **Inject environment.** Pass all variables from Section 4.
4. **Run container.** Stream stdout/stderr → `JOBS_DIR/<job-id>/logs/<step_name>.log`.
5. **Wait for exit.** Capture exit code.
6. **Report status.** Emit Socket.IO events: `step_started`, `step_completed`, `step_failed`.

If a step fails (non-zero exit code):
- All subsequent steps are skipped (fail-fast).
- The workspace is preserved for debugging.
- Cleanup follows the same retention policy as successful jobs.

### 6.3 Cleanup

After job completion (success or failure):

1. The workspace is preserved for `retention_hours` (configured in `job.json`, default 24 hours).
2. A background task on Solar Host deletes `JOBS_DIR/<job-id>/` after the retention period expires.
3. Cancellation (`DELETE /jobs/{id}`) triggers immediate cleanup, bypassing retention.

Cleanup removes the entire job directory tree:

```
rm -rf JOBS_DIR/<job-id>
```

---

## 7. Step-by-Step Examples

Each example shows the workspace state before and after the step runs, the environment injected, and the expected side effects.

### 7.1 `download_model` Step

**Pre-step workspace:**
```
JOBS_DIR/job-a1b2/
├── models/         (empty)
├── data/           (empty)
├── output/         (empty)
├── config/
│   └── job.json
└── logs/           (empty)
```

**Environment injected:**
```
JOB_ID=job-a1b2
WORKSPACE_MODELS=/workspace/models
MODEL_URI=repo://IRIS-BERT-base:v1
MODEL_OUTPUT_DIR=/workspace/models/IRIS-BERT-base
HARBOR_URL=https://imgrepo.damit.hu
HARBOR_USERNAME=robot_supernova+aiops
HARBOR_PASSWORD=...
```

**Step behavior:**
1. Read `MODEL_URI` and `MODEL_OUTPUT_DIR`.
2. Resolve `repo://IRIS-BERT-base:v1` via Data Repository API to get Harbor reference.
3. Pull artifact from Harbor via ORAS into `MODEL_OUTPUT_DIR`.
4. Update `job.json`: add `steps.download_model` with status, paths, and digest.

**Post-step workspace:**
```
JOBS_DIR/job-a1b2/
├── models/
│   └── IRIS-BERT-base/
│       ├── config.json
│       ├── model.safetensors
│       └── tokenizer.json
├── data/           (empty)
├── output/         (empty)
├── config/
│   └── job.json    (updated)
└── logs/
    └── download_model.log
```

### 7.2 `download_dataset` Step

**Pre-step workspace:** (inherits from download_model)
```
Same as above, models/ populated.
```

**Environment injected:**
```
JOB_ID=job-a1b2
WORKSPACE_DATA=/workspace/data
DATASET_URI=repo://iris-tickets:2026-03
DATASET_OUTPUT_DIR=/workspace/data/tickets-dataset
HARBOR_URL=...
```

**Step behavior:**
1. Read `DATASET_URI` and `DATASET_OUTPUT_DIR`.
2. Resolve via Data Repository, pull from Harbor via ORAS.
3. Dump files to `DATASET_OUTPUT_DIR/`.
4. Update `job.json`: add `steps.download_dataset`.

**Post-step workspace:**
```
JOBS_DIR/job-a1b2/
├── models/
│   └── IRIS-BERT-base/
├── data/
│   └── tickets-dataset/
│       ├── train.parquet
│       └── test.parquet
├── output/         (empty)
├── config/
│   └── job.json    (updated)
└── logs/
```

### 7.3 Config Preparation (between download and train)

Before the `train` step, Solar Host writes the training config JSON into the workspace. This file is derived from the job definition submitted to Solar Control (which in turn came from SuperNova).

**`/workspace/config/training.json`** (written by Solar Host):

```json
{
  "name": "base-osl-2026-05",
  "model": "/workspace/models/IRIS-BERT-base",
  "tokenizer": "/workspace/models/IRIS-BERT-base",
  "output_dir": "/workspace/output/base_osl",
  "train_dataset": "/workspace/data/tickets-dataset",
  "class_column": "osl",
  "format_path": "formats/default.txt",
  "eval_dataset_split_size": 1000,
  "max_seq_length": 512,
  "batch_size": 32,
  "gradient_accumulation_steps": 1,
  "gradient_checkpointing": true,
  "max_steps": 16000,
  "num_workers": 4,
  "learning_rate": 5e-5,
  "eta_min": 1e-7,
  "warmup_steps": 200,
  "optimizer": "adamw_8bit",
  "scheduler": "cosine_with_min_lr",
  "logging_steps": 10,
  "save_steps": 2000,
  "wandb": {
    "project": "aiops-categorizer",
    "name": "base-osl-2026-05",
    "tags": ["finetune", "osl"]
  }
}
```

Key mappings from workspace paths to Etalon config fields:

| Etalon config field | Workspace path                                |
|---------------------|-----------------------------------------------|
| `model`             | `/workspace/models/<model-name>`              |
| `tokenizer`         | `/workspace/models/<model-name>` (usually same as model) |
| `output_dir`        | `/workspace/output/<run-name>`                |
| `train_dataset`     | `/workspace/data/<dataset-name>`              |

### 7.4 `train` Step

**Environment injected:**
```
JOB_ID=job-a1b2
TRAINING_CONFIG=/workspace/config/training.json
MODEL_DIR=/workspace/models/IRIS-BERT-base
DATASET_DIR=/workspace/data/tickets-dataset
OUTPUT_DIR=/workspace/output/base_osl
WANDB=false
HF_HOME=/workspace/.cache/huggingface
HF_TOKEN=...        (optional)
```

**Step behavior (Etalon container):**
1. Read `TRAINING_CONFIG` → parse Etalon config JSON.
2. Load model from `/workspace/models/IRIS-BERT-base`.
3. Load dataset from `/workspace/data/tickets-dataset`.
4. Run training loop; write checkpoints to `/workspace/output/base_osl/checkpoint-NNNNN/`.
5. Determine best checkpoint (by eval metric per `model_selection` in `job.json`).
6. Write results to `job.json`: `steps.train` with `best_checkpoint_path`, `eval_metrics`, etc.
7. Optionally write `upload-metadata.json` for the Data Repository registration.
8. Exit 0 on success.

**Post-step workspace:**
```
JOBS_DIR/job-a1b2/
├── models/
│   └── IRIS-BERT-base/
├── data/
│   └── tickets-dataset/
├── output/
│   └── base_osl/
│       ├── checkpoint-2000/
│       ├── checkpoint-4000/
│       ├── ...
│       ├── checkpoint-12000/    (best)
│       │   ├── config.json
│       │   ├── model.safetensors
│       │   ├── trainer_state.json
│       │   └── tokenizer.json
│       └── checkpoint-16000/
├── config/
│   ├── job.json                (updated with steps.train)
│   ├── training.json
│   └── upload-metadata.json    (optional, for Data Repository)
├── logs/
│   └── train.log
└── .cache/
    └── huggingface/            (model cache, reused across jobs)
```

### 7.5 `convert_model` Step

**Pre-step:** Reads `job.json` → `steps.train.best_checkpoint_path` for the input.

**Environment injected:**
```
JOB_ID=job-a1b2
MODEL_INPUT=/workspace/output/base_osl/checkpoint-12000
MODEL_OUTPUT=/workspace/output/base_osl.gguf
QUANTIZATION=Q4_K_M
```

**Step behavior:**
1. Read `MODEL_INPUT` (HF model directory).
2. Convert to GGUF using llama.cpp conversion tools.
3. Write GGUF file to `MODEL_OUTPUT`.
4. Update `job.json`: add `steps.convert_model`.

### 7.6 `upload_model` Step

**Pre-step:** Reads `job.json` → `steps.train.best_checkpoint_path` (or `steps.convert_model.*`) for the upload source.

**Environment injected:**
```
JOB_ID=job-a1b2
MODEL_SOURCE_PATH=/workspace/output/base_osl/checkpoint-12000
HARBOR_TARGET_REF=imgrepo.damit.hu/supernova/iris-osl:v4
ARTIFACT_NAME=iris-osl
VERSION=v4
ARTIFACT_CATEGORY=model
METADATA_PATH=/workspace/config/upload-metadata.json
DATA_REPOSITORY_URL=http://data-repository.solar.svc.cluster.local
```

**Step behavior:**
1. Push `MODEL_SOURCE_PATH` to Harbor at `HARBOR_TARGET_REF` via ORAS.
2. Read `METADATA_PATH` for artifact metadata.
3. Register artifact version with Data Repository: `POST /api/models/iris-osl/versions`.
4. Update `job.json`: add `steps.upload_model` with the Harbor reference.

---

## 8. Safety & Security

### 8.1 Path Containment

Step containers are confined to the workspace through Docker bind mounts. Each bind mount maps a specific host directory to a specific container path. There is no mechanism for a step to access files outside `/workspace/` because no other paths are mounted.

- `local://` URIs are not resolved inside step containers. Steps only operate on workspace paths.
- Step containers run without privileged mode.
- The `--network` flag may be set to restrict or disable network access for steps that should not access external services.

### 8.2 Host-Side Path Safety

When creating the workspace, Solar Host:
- Resolves `JOBS_DIR` to an absolute path.
- Rejects `JOBS_DIR` values that resolve outside an allowed base (e.g. not under `/opt/solar/` or configured allowlist).
- Rejects `job-id` values containing `/`, `..`, or non-filesystem-safe characters.
- Uses `Path.resolve()` and validates the resolved path is a child of the resolved `JOBS_DIR`.

### 8.3 Credential Isolation

Harbor and HuggingFace credentials are injected as environment variables with Docker's `--env` flag. They are:
- Not written to any file inside the workspace.
- Not persisted in the `job.json` manifest.
- Scoped to the container's lifetime (cleared when the container exits).
- Masked in Solar Host logs.

### 8.4 Disk Space Guards

| Checkpoint                          | Action on failure                                    |
|-------------------------------------|------------------------------------------------------|
| Before workspace creation           | Reject job, return 507 to Solar Control              |
| Before each step                    | Abort step, preserve workspace, emit `step_failed`   |
| During download (streaming pull)    | Cancel download, clean partial files, emit failure   |

The minimum free disk threshold is `min_free_disk_gb` (Solar Host env, default 2 GB), overridable per-job via `job.json` (`min_free_disk_gb` field).

### 8.5 Ownership & Permissions

All workspace directories and files are owned by the container runtime UID (default `1000:1000`, configurable via `CONTAINER_UID` / `CONTAINER_GID` on Solar Host). The step executor creates directories with `0755` and files with `0644`.

Containers run as `CONTAINER_UID:CONTAINER_GID` so they can read and write the workspace. No `root`-owned files should appear in the workspace. The step executor verifies at workspace creation time that the UID/GID is valid and that `JOBS_DIR` is writable.

---

## 9. Docker Reference

### 9.1 Step Image Contract

Every step image must:
- Accept the environment variables listed in Section 4.
- Read inputs from `/workspace/models/`, `/workspace/data/`, `/workspace/config/`.
- Write outputs to `/workspace/output/`.
- Write shared state to `/workspace/config/job.json` (merge, don't overwrite).
- Exit 0 on success, non-zero on failure.
- Log progress to stdout/stderr (timestamps optional, text format).

Step images must NOT:
- Assume write access to `/workspace/models/` or `/workspace/data/` (only download steps get write access).
- Hardcode paths — always use environment variables or `job.json`.
- Access the network unless required for their function (download/upload steps need it; train typically does too for HF Hub cache).
- Write outside `/workspace/`.

### 9.2 Example Dockerfile (Etalon Training Image)

A platform-compatible Etalon image that conforms to the workspace contract:

```dockerfile
FROM pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel

RUN apt-get update && apt-get install -y git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/etalon

# Upgrade pip and install dependencies
RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy training code
COPY . /opt/etalon

COPY entrypoint.sh /opt/etalon/entrypoint.sh
RUN chmod +x /opt/etalon/entrypoint.sh

# The image accepts configuration via environment variables only.
# The training config JSON path comes from TRAINING_CONFIG env var.
# Paths inside the config (model, dataset, output_dir) use /workspace/ paths.

ENV HF_HOME=/workspace/.cache/huggingface

ENTRYPOINT ["/opt/etalon/entrypoint.sh"]
```

### 9.3 Example entrypoint.sh (Etalon)

```bash
#!/bin/bash
set -euo pipefail

# Read training config path from environment
TRAINING_CONFIG="${TRAINING_CONFIG:-/workspace/config/training.json}"
JOB_CONFIG="${JOB_CONFIG:-/workspace/config/job.json}"

# Ensure HF cache directory exists
mkdir -p "${HF_HOME:-/workspace/.cache/huggingface}"

# Log in to wandb if token is provided
if [ "${WANDB:-false}" = "true" ] && [ -n "${WANDB_API_KEY:-}" ]; then
    wandb login "$WANDB_API_KEY"
fi

# Update accelerate config if needed
if [ -n "${ACCELERATE_CONFIG:-}" ]; then
    python utils/update_accelerate_config.py \
        --training_config "$TRAINING_CONFIG" \
        --accelerate_config "$ACCELERATE_CONFIG"
fi

# Build and execute the training command
CMD="accelerate launch --config_file ${ACCELERATE_CONFIG:-configs/accelerate_deepspeed_config.json} trainer.py -c $TRAINING_CONFIG"

if [ "${WANDB:-false}" = "true" ]; then
    CMD="$CMD -w"
fi
if [ -n "${RESUME:-}" ]; then
    CMD="$CMD -r $RESUME"
fi

exec $CMD
```

**Post-training hook (conceptual):** After training completes, the Etalon image must determine the best checkpoint and update `job.json`. This logic can be part of a `TrainingStatusCallback` (as in the aiops-categorizer branch) that writes to `job.json` on `on_train_end`. A simpler approach: a post-train script that parses `trainer_state.json` and writes the result.

```bash
# post-train.sh — called after trainer.py exits successfully
#!/bin/bash
JOB_CONFIG="${JOB_CONFIG:-/workspace/config/job.json}"
OUTPUT_DIR="${OUTPUT_DIR:-/workspace/output}"

# Find the best checkpoint from trainer_state.json in the output dir
BEST_CKPT=$(python -c "
import json, os, glob
trainer_files = glob.glob(os.path.join('$OUTPUT_DIR', '**', 'trainer_state.json'), recursive=True)
if trainer_files:
    with open(trainer_files[0]) as f:
        state = json.load(f)
    best = state.get('best_model_checkpoint', '')
    print(os.path.basename(best) if best else '')
")

# Merge into job.json
python -c "
import json
with open('$JOB_CONFIG', 'r') as f:
    job = json.load(f)
job.setdefault('steps', {})['train'] = {
    'status': 'completed',
    'output_dir': '$OUTPUT_DIR',
    'best_checkpoint': '$BEST_CKPT',
    'best_checkpoint_path': '$OUTPUT_DIR/$BEST_CKPT'
}
with open('$JOB_CONFIG', 'w') as f:
    json.dump(job, f, indent=2)
"
```

### 9.4 Conceptual docker-compose.yaml (Local Development)

For testing step images locally against the workspace contract:

```yaml
services:
  train:
    image: etalon-aiops-categorizer:v3
    volumes:
      - ./workspace/models:/workspace/models:ro
      - ./workspace/data:/workspace/data:ro
      - ./workspace/output:/workspace/output:rw
      - ./workspace/config:/workspace/config:rw
      - ./workspace/.cache/huggingface:/workspace/.cache/huggingface:rw
    environment:
      - JOB_ID=local-test
      - TRAINING_CONFIG=/workspace/config/training.json
      - JOB_CONFIG=/workspace/config/job.json
      - MODEL_DIR=/workspace/models/IRIS-BERT-base
      - DATASET_DIR=/workspace/data/tickets-dataset
      - OUTPUT_DIR=/workspace/output/test-run
      - WANDB=false
      - HF_HOME=/workspace/.cache/huggingface
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
```

---

## 10. Etalon Migration Notes

Current Etalon branches expect relative paths resolved against `/opt/etalon/`. To comply with the workspace contract, Etalon images must be adapted:

### 10.1 Changes Required

| Concern                        | Current State                                 | Target State                                       |
|--------------------------------|-----------------------------------------------|----------------------------------------------------|
| Model path                     | `models/IRIS-BERT-base` (relative to WORKDIR) | `/workspace/models/IRIS-BERT-base` (absolute)      |
| Dataset path                   | `data/tickets-dataset` (relative to WORKDIR)  | `/workspace/data/tickets-dataset` (absolute)       |
| Output path                    | `outputs/base_osl` (relative to WORKDIR)      | `/workspace/output/base_osl` (absolute)            |
| Config source                  | `-c configs/base_osl.json` (CLI arg)          | `-c "${TRAINING_CONFIG}"` (env var → path)         |
| Best checkpoint communication  | Manual inspection or W&B                      | Write to `/workspace/config/job.json`              |
| HuggingFace cache              | Default `~/.cache/huggingface`                | Explicit `HF_HOME=/workspace/.cache/huggingface`   |
| W&B configuration              | In config JSON, always present                | Gated behind `WANDB` env var; WANDB_API_KEY optional |

### 10.2 Minimal Changes to Etalon Code

The training config JSON already drives all paths. The only code changes needed are:

1. **`trainer.py` or entrypoint.sh**: Add a post-training hook that writes `steps.train` to `$JOB_CONFIG`.
2. **`modules/config.py`**: Accept absolute paths (already works — `Config` stores paths as-is).
3. **`modules/utils.py` — `load_dataset()`**: Already checks if `data_dir` exists locally (absolute paths work).
4. **Dockerfile**: Set `HF_HOME=/workspace/.cache/huggingface`.
5. **docker-compose.yaml**: Update mounts to `/workspace/...` paths.

### 10.3 Etalon Branch Considerations

| Branch             | Dataset Format  | Notes                                                    |
|--------------------|-----------------|----------------------------------------------------------|
| `aiops-categorizer`| Parquet         | Has `TrainingStatusCallback`; best candidate for adapter |
| `icinga-classifier`| HDF5            | Uses custom model architecture (`modeling_alert_classifier.py`) |
| `worksheet` (wp)   | JSON            | Quantized (4-bit) training, small batches                |
| `T12`              | Image files     | ViT-based image classification                           |
| `icinga`           | Parquet/HDF5    | Time-series forecasting (Informer/PatchTST)              |

All branches use the same `Config` class and the same `model`/`train_dataset`/`output_dir` pattern. The workspace contract applies uniformly.

---

## 11. Error Handling

### 11.1 Workspace Creation Failures

| Condition                          | Error Code | HTTP Status |
|------------------------------------|------------|-------------|
| `JOBS_DIR` not configured/writable | `workspace_unavailable` | 500 |
| Insufficient disk space            | `insufficient_storage`  | 507 |
| Invalid `job-id` (path traversal)  | `invalid_job_id`       | 400 |
| Directory creation failed (perms)  | `workspace_create_failed` | 500 |

### 11.2 Step Failures

When a step fails (non-zero exit code):
- The step executor captures the exit code and last N lines of stderr.
- A `step_failed` Socket.IO event is emitted with `{step_name, exit_code, error_summary}`.
- Subsequent steps are skipped.
- The workspace is preserved for `retention_hours`.
- Solar Control forwards the failure to SuperNova Control.

---

## 12. Future Considerations

- **Concurrent steps within a job.** Currently steps run sequentially. If parallel step execution is added, the config directory serves as the synchronization point — steps must use atomic writes (write tmp, then rename) when updating shared state.
- **Workspace snapshot/checkpoint.** Long-running training jobs may benefit from workspace snapshots for crash recovery. The workspace layout is self-contained (all state under `JOBS_DIR/<job-id>/`), making snapshot straightforward.
- **Multi-GPU jobs.** Workspace layout is unchanged. Additional GPU allocation is handled by the step executor (S-024), not by the workspace spec.
- **Step caching.** If the same `download_model` step runs with the same `MODEL_URI`, results could be cached. The workspace layout does not address this — caching is a step executor concern.

---

## 13. Related Specifications

- [Model Source URI Specification](model-source-uri.md) — `repo://`, `huggingface://`, `local://` URI schemes
- [Data Repository Schema](../../data-repository/docs/schema.md) — artifact metadata conventions
- [Data Repository Harbor Docs](../../data-repository/docs/harbor.md) — OCI artifact layout and media types
- S-023 — Step executor (consumes this spec)
- S-028, S-029, S-030, S-031 — Step container images (must conform to this spec)
- N-004 — Job submission (generates job definitions that reference workspace paths)
- N-011 — Step pipeline orchestration (translates job config to step env vars)
