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

## Consumers

| Consumer | Usage | Auth Source |
|----------|-------|-------------|
| Data Repository (D-006) | Artifact verification, metadata sync via Harbor API | K8s secret (TBD) |
| Step containers — S-028, S-029, S-030 | Direct ORAS push/pull of models and datasets | Injected env vars / mounted secret |
| Solar Control | `repo://` resolver, ORAS pulls for model distribution | K8s secret (TBD) |

## Related Issues

- **D-001** — Create `supernova` project in Harbor (this setup)
- **D-003** — ORAS push/pull POC (depends on D-001)
- **D-004** — K8s secrets and credential distribution
- **D-006** — Harbor API integration in Data Repository
- **S-028, S-029, S-030** — Step container images with ORAS support
