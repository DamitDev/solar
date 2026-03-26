# Harbor — OCI Artifact Storage

## Overview

Harbor (`imgrepo.damit.hu`) is the artifact registry for the AIOps platform. The Data Repository uses it as blob storage — models and datasets are stored as OCI artifacts via the OCI Distribution Spec. Consumers push/pull directly to/from Harbor.

The `supernova` project (project ID `39541`) is the dedicated namespace for all Data Repository artifacts. Repositories are created automatically on first push.

## Access

| Domain | TLS | Notes |
|--------|-----|-------|
| `imgrepo.damit.hu` | Valid cert | Primary, use for production |
| `imgrepo.damit.cloud` | Self-signed cert | Alternative, requires TLS verification disabled (`-k` in curl, `verify=False` in Python) |

### Robot Account

- **Username:** `robot_supernova+aiops`
- **Credentials location:** `temp/harbor-credentials.yaml` (gitignored, local dev only)
- **K8s secret:** TBD — must be created in the `solar` namespace for production (see D-004)

### Permissions

| Operation | Supported |
|-----------|-----------|
| Push blobs and manifests | Yes |
| Pull blobs and manifests | Yes |
| Delete artifacts | Yes |
| Delete repositories | No |
| List repos via Harbor API | Yes |
| List/manage project settings | No |

## Artifact Naming Convention

Each artifact (model or dataset) becomes a repository directly under `supernova/`:

```
imgrepo.damit.hu/supernova/<artifact-name>:<version>
```

Examples:
- `imgrepo.damit.hu/supernova/iris-osl:v3` — a model
- `imgrepo.damit.hu/supernova/iris-tickets:2026-03` — a dataset

The category (model vs dataset) is tracked in Data Repository metadata, not in the Harbor path.

## OCI Media Types

Harbor accepts arbitrary OCI media types. The following custom types are used by SuperNova:

| Media Type | Purpose |
|------------|---------|
| `application/vnd.supernova.model.config.v1+json` | Model artifact config |
| `application/vnd.supernova.model.weights.v1+tar+gzip` | Model weights layer |
| `application/vnd.supernova.dataset.config.v1+json` | Dataset artifact config |
| `application/vnd.supernova.dataset.content.v1+tar+gzip` | Dataset content layer |

Manifests use the standard OCI wrapper: `application/vnd.oci.image.manifest.v1+json`.

## Authentication Flow

The OCI Distribution v2 API uses token-based auth. Direct basic auth against `/v2/` endpoints returns 401 — you must first obtain a JWT.

### Step 1 — Get a bearer token

```
GET /service/token?service=harbor-registry&scope=repository:supernova/<name>:pull,push
Authorization: Basic <robot credentials>
```

Returns:
```json
{
  "token": "<JWT>",
  "expires_in": 1800,
  "issued_at": "..."
}
```

### Step 2 — Use the token for registry operations

```
Authorization: Bearer <token>
```

The token is scoped to the specific repository and actions requested. Request a new token when switching repositories or when the token expires (30 min TTL).

### Harbor v2.0 API (metadata operations)

The Harbor-specific API at `/api/v2.0/` accepts basic auth directly:

```
GET /api/v2.0/projects/supernova/repositories
Authorization: Basic <robot credentials>
```

Use this for listing repositories, artifacts, tags — but use the token flow above for push/pull blob operations.

## Push Flow (OCI Distribution Spec)

### 1. Initiate blob upload

```
POST /v2/supernova/<name>/blobs/uploads/
Authorization: Bearer <token>
→ 202 Accepted, Location header contains upload URL
```

### 2. Upload blob content

```
PUT <upload-url>&digest=sha256:<hash>
Content-Type: application/octet-stream
Authorization: Bearer <token>
Body: <raw bytes>
→ 201 Created
```

### 3. Push manifest

```
PUT /v2/supernova/<name>/manifests/<tag>
Content-Type: application/vnd.oci.image.manifest.v1+json
Authorization: Bearer <token>
Body: <manifest JSON>
→ 201 Created
```

### Manifest structure

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "config": {
    "mediaType": "application/vnd.supernova.model.config.v1+json",
    "digest": "sha256:<config-digest>",
    "size": <config-size>
  },
  "layers": [
    {
      "mediaType": "application/vnd.supernova.model.weights.v1+tar+gzip",
      "digest": "sha256:<layer-digest>",
      "size": <layer-size>,
      "annotations": {
        "org.opencontainers.image.title": "<filename>"
      }
    }
  ],
  "annotations": {
    "org.opencontainers.image.created": "<ISO 8601 timestamp>"
  }
}
```

## Pull Flow

### 1. Fetch manifest

```
GET /v2/supernova/<name>/manifests/<tag>
Accept: application/vnd.oci.image.manifest.v1+json
Authorization: Bearer <token>
```

### 2. Fetch blobs by digest

```
GET /v2/supernova/<name>/blobs/sha256:<digest>
Authorization: Bearer <token>
```

## Shared Library: `harbor-oci-client`

All Python services that interact with Harbor use the [`harbor-oci-client`](https://github.com/DamitDev/harbor-oci-client) shared library. It provides:

- **`HarborClient`** (async) — Harbor REST API wrapper for artifact verification, deletion, and metadata retrieval. Handles OCI Distribution token management with per-repository caching and automatic retries.
- **`OrasHelper`** (sync) — Convenience wrappers around `oras-py` for push/pull with SuperNova media types.
- **Typed exceptions** — `HarborError`, `ArtifactNotFoundError`, `HarborAuthError`, `HarborConnectionError`, `HarborAPIError`.
- **Media type constants** — All custom SuperNova OCI media types (see table above).
- **`parse_ref()`** — Utility to decompose `harbor_ref` strings into host, project, repo name, and reference.

Install:

```bash
pip install git+https://github.com/DamitDev/harbor-oci-client.git
```

See the [harbor-oci-client README](https://github.com/DamitDev/harbor-oci-client) for full API reference.

## Data Repository Module API (`app/harbor`)

The `app.harbor` module is a thin wrapper over `harbor-oci-client` that adds app-level singleton lifecycle (`init_harbor` / `close_harbor` / `harbor_client()`). It is initialized at startup and available via the `harbor_client()` accessor. Auth, token caching, and retries are handled internally by the library.

### Quick Start (for D-007, D-009, N-029 implementers)

```python
from app.harbor import harbor_client, ArtifactNotFoundError

client = harbor_client()

# Verify an artifact exists before accepting a registration
try:
    info = await client.verify_artifact("imgrepo.damit.hu/supernova/iris-osl:v3")
    # info.digest  -> "sha256:abc123..."   (store in artifact_versions.digest)
    # info.content_length -> 10485760      (store in artifact_versions.size_bytes)
except ArtifactNotFoundError:
    # Reject registration — artifact not in Harbor
    raise

# Delete an artifact (retention cleanup, N-029)
await client.delete_artifact("imgrepo.damit.hu/supernova/iris-osl:v3")

# Get detailed artifact metadata (optional cross-validation)
detail = await client.get_artifact_info("imgrepo.damit.hu/supernova/iris-osl:v3")
# detail.digest, detail.size, detail.media_type, detail.push_time, detail.tags
```

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `verify_artifact(harbor_ref)` | `ArtifactInfo` | OCI HEAD request. Returns `digest` and `content_length`. Raises `ArtifactNotFoundError` on 404. |
| `delete_artifact(harbor_ref)` | `None` | Harbor v2.0 API DELETE. Raises `ArtifactNotFoundError` on 404. |
| `get_artifact_info(harbor_ref)` | `ArtifactDetail` | Harbor v2.0 API GET. Returns `digest`, `size`, `media_type`, `push_time`, `tags`. |

### Exceptions

All exceptions inherit from `HarborError` and carry `detail` (message) and `status_code` (int or None).

| Exception | When | Suggested HTTP mapping |
|-----------|------|----------------------|
| `ArtifactNotFoundError` | 404 from Harbor | 404 |
| `HarborAuthError` | 401/403 from Harbor | 502 |
| `HarborConnectionError` | Network/timeout | 502 |
| `HarborAPIError` | Other unexpected status | 502 |

### Media Type Constants

Available in `app.harbor.media_types`:

```python
from app.harbor.media_types import MODEL_CONFIG, MODEL_WEIGHTS, DATASET_CONFIG, DATASET_CONTENT
```

### Configuration

The client reads from environment variables via `app.config.Settings`:

| Env Var | Setting | Default |
|---------|---------|---------|
| `HARBOR_URL` | `harbor_url` | `https://imgrepo.damit.hu` |
| `HARBOR_USERNAME` | `harbor_username` | (empty) |
| `HARBOR_PASSWORD` | `harbor_password` | (empty) |

### Internals

- **Verification** uses OCI Distribution HEAD at `/v2/<repo>/manifests/<ref>` with a scoped bearer token (not Harbor v2.0 API — see `poc/findings.md` for rationale).
- **Deletion and info** use Harbor v2.0 API at `/api/v2.0/` with basic auth.
- **Tokens** are cached per-repository with a 60-second safety margin on the 30-minute TTL.
- **Retries**: 3 attempts with exponential backoff on connection errors and 5xx. A 401 on OCI requests triggers one automatic token refresh.

## Consumers

All Python consumers use the `harbor-oci-client` library for Harbor interactions.

| Consumer | Usage | Library API | Auth Source |
|----------|-------|-------------|-------------|
| Data Repository (D-006) | Artifact verification, deletion, metadata retrieval | `HarborClient` (verify, delete, get_info) | K8s secret (TBD) |
| Solar Control (D-016) | `repo://` resolver — pull models from Harbor to hosts | `HarborClient` (verify) + `OrasHelper` (pull) | K8s secret (TBD) |
| Step containers — S-028, S-029, S-030 | Direct ORAS push/pull of models and datasets | `OrasHelper` (push, pull, push_custom) | Injected env vars / mounted secret |

## Related Issues

- **D-001** — Create `supernova` project in Harbor (this setup)
- **D-003** — ORAS push/pull POC (depends on D-001)
- **D-004** — K8s secrets and credential distribution
- **D-006** — Harbor API integration in Data Repository
- **D-016** — Complete `repo://` resolver in Solar Control (uses `harbor-oci-client`)
- **S-028, S-029, S-030** — Step container images (use `harbor-oci-client` for ORAS)
