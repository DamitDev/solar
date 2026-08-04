# Model Source URI Specification

| Field       | Value                          |
|-------------|--------------------------------|
| Issue       | S-008                          |
| Status      | Done                           |
| Created     | 2026-03-31                     |
| Depends on  | —                              |
| Depended by | S-009 – S-013, D-014, D-016    |

## 1. Overview

Solar currently references models by raw filesystem paths (`LlamaCppConfig.model` for llama.cpp GGUF files) or HuggingFace model IDs (`HuggingFaceConfig.model_id`). There is no unified addressing scheme, no download orchestration, and no caching.

SuperNova and Solar need a consistent way to specify model sources so that Solar Control can resolve URIs and ensure models are available on target hosts before instance creation. This specification defines three URI schemes — `repo://`, `huggingface://`, and `local://` — along with their syntax, resolution behavior, directory layout, caching semantics, and error handling.

This document is the source of truth for all model source URI behavior across the platform.

## 2. URI Schemes

### 2.1 `repo://` — Data Repository artifacts

**Format:** `repo://{name}:{version}`

| Component | Description | Constraints |
|-----------|-------------|-------------|
| `name`    | Artifact name in Data Repository (`artifacts.name`) | Required. Alphanumeric, hyphens, underscores. Case-sensitive. |
| `version` | Artifact version (`artifact_versions.version`) | Required. No "latest" semantics in v1. |

**Examples:**

```
repo://iris-osl:v3
repo://iris-tickets:2026-03
repo://IRIS-BERT-base:v1
```

**Resolution:** Solar Control queries the Data Repository resolve endpoint to obtain a `harbor_ref` (e.g. `imgrepo.damit.hu/supernova/iris-osl:v3`), then instructs Solar Host to pull the artifact from Harbor via ORAS. Data Repository is metadata-only; blobs live in Harbor.

### 2.2 `huggingface://` — HuggingFace Hub

**Format:** `huggingface://{model_id}`

Where `model_id` is `{org}/{model}` or `{model}` (matching HuggingFace Hub conventions).

| Component  | Description | Constraints |
|------------|-------------|-------------|
| `model_id` | HuggingFace Hub model identifier | Required. Must be a valid Hub ID. |

**Examples:**

```
huggingface://microsoft/phi-3
huggingface://meta-llama/Llama-2-7b-hf
huggingface://phi-3
```

**Resolution:** Solar Control instructs Solar Host to download the model from HuggingFace Hub using the `huggingface_hub` library.

### 2.3 `local://` — Local filesystem

**Format:** `local://{path}`

Follows RFC 8089 conventions: triple slash for absolute paths, double slash for relative paths.

| Variant | Format | Resolution |
|---------|--------|------------|
| Absolute | `local:///absolute/path` | Use path as-is (after validation) |
| Relative | `local://relative/path` | Resolve relative to `MODELS_DIR` |

**Examples:**

```
local:///opt/models/iris.gguf
local:///mnt/nvme/models/custom-model/
local://repo--iris-osl--v3/model.gguf
```

**Resolution:** Solar Host validates the path exists and is within an allowed base directory (`MODELS_DIR` for relative paths, configurable allowlist for absolute paths). No download occurs. This scheme provides backward compatibility with existing raw filesystem paths.

### 2.4 Grammar

```
model_source_uri = repo_uri | huggingface_uri | local_uri

repo_uri         = "repo://" name ":" version
name             = 1*(ALPHA / DIGIT / "-" / "_")
version          = 1*(ALPHA / DIGIT / "-" / "_" / ".")

huggingface_uri  = "huggingface://" model_id
model_id         = [org "/"] model_name
org              = 1*(ALPHA / DIGIT / "-" / "_" / ".")
model_name       = 1*(ALPHA / DIGIT / "-" / "_" / ".")

local_uri        = "local://" path
path             = abs_path | rel_path
abs_path         = "/" 1*(PCHAR / "/")
rel_path         = 1*(PCHAR / "/")
```

## 3. Resolution Architecture

### 3.1 Design principle

Following the Kubernetes model where kubelet pulls container images directly from registries and the control plane only declares _what_ to pull, **Solar Host downloads models directly from their sources** (Harbor, HuggingFace Hub). Solar Control orchestrates by resolving URIs to pull instructions and sending those to the host.

This avoids the overhead of proxying multi-GB model files through Solar Control (source → control → host would double network transfer). It also simplifies model distribution: to place a model on another host, Solar Control sends the same pull command — both hosts pull from the authoritative source.

### 3.2 Responsibility split

| Concern | Solar Control | Solar Host |
|---------|--------------|------------|
| URI parsing | Parses all three schemes | Parses `local://` only |
| Metadata resolution | Queries Data Repository for `repo://` harbor_ref | — |
| Download execution | — | Pulls from Harbor (ORAS) or HuggingFace Hub |
| Cache management | — | Owns manifest, checks cache before pulling |
| Path validation | — | Validates `local://` paths |
| Instance creation | Sends resolved `local://` path to host | Resolves `local://` to filesystem path |

### 3.3 Resolution flow

```
┌────────┐          ┌──────────────┐       ┌───────────────┐       ┌─────────────────┐
│ Client │          │ Solar Control│       │  Solar Host   │       │ Harbor / HF Hub │
└───┬────┘          └──────┬───────┘       └──────┬────────┘       └────────┬────────┘
    │  create instance     │                      │                         │
    │  (model_source URI)  │                      │                         │
    │─────────────────────>│                      │                         │
    │                      │                      │                         │
    │          ┌───────────┴───────────┐          │                         │
    │          │ Parse URI, resolve    │          │                         │
    │          │ metadata if repo://   │          │                         │
    │          └───────────┬───────────┘          │                         │
    │                      │                      │                         │
    │                      │ POST /models/pull    │                         │
    │                      │ {source, source_uri} │                         │
    │                      │─────────────────────>│                         │
    │                      │                      │                         │
    │                      │              ┌───────┴────────┐                │
    │                      │              │ Check manifest │                │
    │                      │              │ (cache hit?)   │                │
    │                      │              └───────┬────────┘                │
    │                      │                      │                         │
    │                      │                      │  [cache miss]           │
    │                      │                      │  Pull from source       │
    │                      │                      │────────────────────────>│
    │                      │                      │<────────────────────────│
    │                      │                      │  Update manifest        │
    │                      │                      │                         │
    │                      │    {path, cached}    │                         │
    │                      │<─────────────────────│                         │
    │                      │                      │                         │
    │                      │  create instance     │                         │
    │                      │  (local:// path)     │                         │
    │                      │─────────────────────>│                         │
    │                      │                      │                         │
    │   instance created   │                      │                         │
    │<─────────────────────│                      │                         │
```

### 3.4 Resolution by scheme

**`repo://`:**

1. Solar Control parses URI → `{name, version}`.
2. Solar Control queries Data Repository: `GET /api/resolve?uri=repo://{name}:{version}` → receives `{harbor_ref, digest, size_bytes, metadata}`.
3. Solar Control sends `POST /models/pull` to Solar Host with `{source: "harbor", harbor_ref, source_uri, digest}`.
4. Solar Host checks manifest for `source_uri`. If cached, returns existing path immediately.
5. On cache miss: Solar Host uses `OrasHelper.pull(harbor_ref, MODELS_DIR/{slug}/)` to download.
6. Solar Host updates manifest, returns resolved local path.
7. Solar Control creates instance with `local://{path}`.

**`huggingface://`:**

1. Solar Control parses URI → `{model_id}`.
2. Solar Control sends `POST /models/pull` to Solar Host with `{source: "huggingface", model_id, source_uri}`.
3. Solar Host checks manifest for `source_uri`. If cached, returns existing path immediately.
4. On cache miss: Solar Host uses `huggingface_hub` to download to `MODELS_DIR/{slug}/`.
5. Solar Host updates manifest, returns resolved local path.
6. Solar Control creates instance with `local://{path}`.

**`local://`:**

1. Solar Control passes `local://` URI directly to Solar Host in the instance creation request (via `model_source` field).
2. Solar Host parses the path, validates it (see Section 8), resolves to absolute path.
3. No download, no manifest update, no pull command.

### 3.5 Idempotency

Resolution is idempotent. Repeated calls with the same URI produce the same result:

- `repo://` and `huggingface://`: manifest cache hit returns the stored path.
- `local://`: path validation is stateless.

### 3.6 Solar Host pull endpoint

`POST /models/pull` on Solar Host replaces the previously planned `POST /models/upload` as the primary model acquisition mechanism.

**Request body:**

```json
{
  "source": "harbor",
  "harbor_ref": "imgrepo.damit.hu/supernova/iris-osl:v3",
  "source_uri": "repo://iris-osl:v3",
  "digest": "sha256:abc123..."
}
```

```json
{
  "source": "huggingface",
  "model_id": "microsoft/phi-3",
  "source_uri": "huggingface://microsoft/phi-3"
}
```

`file_filters` restricts a HuggingFace snapshot to the matching files — a file is downloaded when it matches **any** pattern. Without it the whole repository is downloaded, which is wasteful for GGUF repositories that ship every quantisation:

```json
{
  "source": "huggingface",
  "model_id": "unsloth/Qwen3-VL-235B-Instruct-GGUF",
  "source_uri": "huggingface://unsloth/Qwen3-VL-235B-Instruct-GGUF",
  "file_filters": ["*UD-Q4_K_XL*", "mmproj-BF16.gguf"]
}
```

Filters are ignored for `harbor` pulls: ORAS always pulls a whole artifact.

**Response body:**

```json
{
  "path": "/opt/solar/models/repo--iris-osl--v3",
  "cached": true,
  "source_uri": "repo://iris-osl:v3"
}
```

**Behavior:** Checks manifest → downloads if needed → updates manifest → returns path. The operation is synchronous (the caller blocks until the model is available). For large models, this may take significant time; the HTTP timeout should be set accordingly.

## 4. Directory Layout

### 4.1 Structure

All downloaded models are stored under `MODELS_DIR` (env: `MODELS_DIR`, default: `./models`) in a flat structure with deterministic slugs:

```
MODELS_DIR/
├── repo--iris-osl--v3/
│   └── <model files>
├── repo--iris-tickets--2026-03/
│   └── <model files>
├── hf--microsoft--phi-3/
│   └── <model files>
├── hf--meta-llama--Llama-2-7b-hf/
│   └── <model files>
└── manifest.json
```

### 4.2 Slug derivation

The directory name (slug) is deterministically derived from the source URI:

| Source URI | Slug |
|------------|------|
| `repo://iris-osl:v3` | `repo--iris-osl--v3` |
| `repo://iris-tickets:2026-03` | `repo--iris-tickets--2026-03` |
| `huggingface://microsoft/phi-3` | `hf--microsoft--phi-3` |
| `huggingface://meta-llama/Llama-2-7b-hf` | `hf--meta-llama--Llama-2-7b-hf` |
| `huggingface://phi-3` | `hf--phi-3` |

**Rules:**

- `repo://{name}:{version}` → `repo--{name}--{version}`
- `huggingface://{model_id}` → `hf--{model_id}` with `/` replaced by `--`
- Original casing is preserved.
- `local://` URIs are not stored under `MODELS_DIR`.

The slug is deterministic: given a source URI, the expected directory can always be computed. However, directory existence alone does not constitute a cache hit — only manifest entries count (see Section 5).

### 4.3 Selecting a file inside an artifact

A pull resolves to a **directory**, but `llama-server --model` needs a **file**. There are three ways to name it, in order of precedence:

| Mechanism | Where it lives | Notes |
|-----------|----------------|-------|
| `model_file` pattern | instance config / intent `backend.model_file` | Filename, relative path or `*` glob. Works for every scheme. |
| `repo://name:version/subpath` | the URI itself | Exact path inside a Harbor artifact, resolved at pull time. |
| Largest root GGUF | automatic | Fallback for `repo://` + `llamacpp` when neither of the above is given. |

Solar Host resolves a `model_file` pattern against the artifact directory (the manifest `path`, or the `local://` target) in this order:

1. An absolute path is used as-is.
2. `<artifact dir>/<pattern>` when that exact file exists.
3. A glob at the root of the artifact directory.
4. A recursive glob — this is what makes a bare filename or a broad pattern work when the file sits in a subfolder of a filtered repository.

When several files match, the trailing shards of a split GGUF (`...-00002-of-00003.gguf`) are dropped because `llama-server` loads them itself from the first shard, and the largest remaining file wins. A tie between equally sized candidates is an error, not a guess.

The llama.cpp `mmproj` field accepts the same patterns, resolved against the same directory. Note that filters and selectors are independent: `file_filters` decides what lands on disk, `model_file` / `mmproj` decide which of those files each llama-server flag points at.

## 5. Caching

### 5.1 Manifest as single source of truth

The manifest file (`MODELS_DIR/manifest.json`) is the **single source of truth** for cache detection. A model is considered cached if and only if it has an entry in the manifest. Directory existence without a manifest entry does not count as cached — this avoids ambiguity from partial downloads, manually placed files, or stale state.

### 5.2 Manifest schema

```json
{
  "models": [
    {
      "slug": "repo--iris-osl--v3",
      "source_uri": "repo://iris-osl:v3",
      "path": "/opt/solar/models/repo--iris-osl--v3",
      "size_bytes": 4815162342,
      "digest": "sha256:abc123...",
      "downloaded_at": "2026-03-31T14:22:00Z"
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | Directory name under `MODELS_DIR` |
| `source_uri` | string | Original model source URI (cache key) |
| `path` | string | Absolute path to model directory |
| `size_bytes` | integer | Total size of model files in bytes |
| `digest` | string, optional | Content digest (from Harbor or computed) |
| `downloaded_at` | string | ISO 8601 timestamp of download completion |
| `file_filters` | list of strings, optional | HuggingFace patterns the snapshot was downloaded with. Absent/`null` means the full repository is present. |

### 5.3 Cache behavior per scheme

**`repo://`**: Cache key is the full `source_uri` (e.g. `repo://iris-osl:v3`). Versions are immutable in Data Repository — same version always refers to the same artifact. Skip download if manifest entry exists with matching `source_uri`. No TTL-based invalidation.

**`huggingface://`**: Cache key is the full `source_uri` (e.g. `huggingface://microsoft/phi-3`). Skip download if manifest entry exists with matching `source_uri`. HuggingFace models can change upstream — the cached version is treated as valid unless explicit re-download is requested (future: force-refresh flag).

A filtered snapshot narrows what "cached" means, so the recorded `file_filters` participate in the check. One repository always keeps one directory:

- Recorded `null` (full repository) satisfies every request.
- A recorded pattern set satisfies a request whose patterns it already covers.
- Otherwise the snapshot is **topped up in place** with the union of both pattern sets (or unfiltered, when the new request wants everything). The directory is kept — its files are valid and `snapshot_download` only fetches what is missing.

**`local://`**: No caching. Path is resolved on every use. The file or directory must exist at resolution time.

### 5.4 Atomic manifest updates

To prevent corruption from interrupted writes:

1. Download model files to `MODELS_DIR/{slug}/`.
2. Write updated manifest to a temporary file (`manifest.json.tmp`).
3. Rename temporary file to `manifest.json` (atomic on POSIX).

If the download fails or is interrupted, no manifest entry is written. The incomplete directory is not treated as cached. Cleanup of orphaned directories (directories without manifest entries) is a future concern.

### 5.5 Model distribution

To place a model on a different host, Solar Control sends the same pull command (with the same source reference) to that host. Both hosts pull from the authoritative source (Harbor or HuggingFace Hub). No host-to-host byte transfer is needed.

## 6. Error Handling

### 6.1 Error categories

| Phase | Error | HTTP Status | Responsibility |
|-------|-------|-------------|----------------|
| URI parsing | Invalid scheme | 400 | Solar Control |
| URI parsing | Missing required component (e.g. no version in `repo://`) | 400 | Solar Control |
| Metadata resolution | Artifact not found in Data Repository | 404 | Solar Control |
| Metadata resolution | Data Repository unreachable | 502 | Solar Control |
| Pull | Source unreachable (Harbor or HuggingFace) | 502 | Solar Host |
| Pull | Authentication failed | 401 | Solar Host |
| Pull | Artifact not found at source | 404 | Solar Host |
| Pull | Insufficient disk space | 507 | Solar Host |
| Pull | Permission denied (filesystem) | 403 | Solar Host |
| Pull | Missing credentials (`HARBOR_*`, `HF_TOKEN`) | 500 | Solar Host |
| Path validation | `local://` path not found | 404 | Solar Host |
| Path validation | `local://` path outside allowed base | 400 | Solar Host |

### 6.2 Error response format

Errors should use a consistent JSON structure:

```json
{
  "error": "model_pull_failed",
  "detail": "Artifact not found in Harbor: imgrepo.damit.hu/supernova/iris-osl:v99",
  "source_uri": "repo://iris-osl:v99",
  "status_code": 404
}
```

### 6.3 Propagation

Solar Host pull errors are returned to Solar Control, which propagates them to the original caller with context about which host and which URI failed. Solar Control does not retry on behalf of the caller — retries are the caller's responsibility.

## 7. Backward Compatibility

- Existing `model` (llama.cpp GGUF path) and `model_id` (HuggingFace Hub ID or path) fields continue to work unchanged.
- A new optional field `model_source` is added to instance config schemas.
- Resolution order: if `model_source` is present, it takes precedence. Otherwise, `model` / `model_id` are used as before.
- Solar Host rejects `repo://` and `huggingface://` in the `model_source` field at instance creation time with a clear error: these must be resolved via `POST /models/pull` first. Only `local://` URIs (or legacy fields) are accepted for instance creation.

## 8. Security

- **Path traversal prevention:** `local://` paths are normalized and validated. Relative paths must resolve within `MODELS_DIR`. Absolute paths must be within an allowed base directory. Sequences like `../` are rejected after normalization.
- **Harbor credentials:** Configured via environment variables on Solar Host: `HARBOR_URL`, `HARBOR_USERNAME`, `HARBOR_PASSWORD`. Required for `repo://` pulls.
- **HuggingFace credentials:** Configured via `HF_TOKEN` environment variable on Solar Host. Required for gated models; optional for public models.
- **Pull endpoint authentication:** `POST /models/pull` requires the same `X-API-Key` authentication as other Solar Host management endpoints.

## 9. Impact on Existing Issues

This specification changes the resolution architecture from "Solar Control proxies model bytes" to "Solar Host pulls directly from source." The following issues are affected:

| Issue | Original Design | Updated Design |
|-------|----------------|----------------|
| S-009 | Managed models directory + manifest | Unchanged. Spec defines directory layout (Section 4) and manifest schema (Section 5). |
| S-010 | Accept `model_source` URI on solar-host | Unchanged. `local://` resolution and backward compatibility as specified. |
| S-011 | URI parser + resolver dispatcher in solar-control | Parser unchanged. Dispatcher sends pull command to Solar Host instead of downloading itself. |
| S-012 | HF resolver: Solar Control downloads + uploads to host | Reworked. Solar Control sends pull command; Solar Host downloads from HuggingFace Hub directly. |
| S-013 | `repo://` resolver stub | Unchanged. Stub returns error until Data Repository is available. |
| S-015 | `POST /models/upload` on solar-host | Replaced by `POST /models/pull`. Host pulls from source directly instead of receiving uploads. |
| S-016 | `GET /models/{name}/download` on solar-host | Deprioritized. Not needed for model distribution since hosts pull from authoritative sources. |
| S-019 | Model distribution: download from host A, upload to host B | Simplified. Solar Control tells target host to pull from the same source (Harbor/HuggingFace). |

### New solar-host dependencies

- `harbor-oci-client` — for ORAS pull from Harbor (`repo://` scheme)
- `huggingface_hub` — for downloading from HuggingFace Hub (`huggingface://` scheme)

These are direct dependencies, configured via environment variables (see Section 8).

## 10. Future Extensions

- **`huggingface://` revision pinning:** Support `huggingface://microsoft/phi-3@main` or `huggingface://microsoft/phi-3@sha256:...` to pin specific revisions.
- **`repo://` latest version:** Allow `repo://iris-osl` (without version) to resolve to the latest version in Data Repository.
- **Force-refresh:** A flag on the pull request to bypass cache and re-download, replacing the existing manifest entry.
- **Digest verification:** For `repo://` pulls, verify downloaded content digest against the digest from Data Repository.
- **`POST /models/upload`:** For edge cases where a model is not in any registry (e.g. locally trained model not yet pushed to Harbor). Lower priority.
