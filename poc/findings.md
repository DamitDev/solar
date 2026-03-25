# D-003: ORAS Python Library — Evaluation Findings

## Summary

`oras-py` (v0.2.42) is validated for pushing and pulling OCI artifacts to Harbor. All POC tests pass: authentication, multi-file push, single-file push, pull with checksum verification, HEAD requests for artifact existence, and custom SuperNova media types. The library is suitable for use in step containers (S-028, S-029, S-030) and as the basis for the Data Repository's Harbor integration (D-006).

## Test Results

| Test | Result | Details |
|------|--------|---------|
| Authentication | PASS | Robot account `robot_supernova+aiops`, token-based auth |
| Push multi-file | PASS | 4 files (HuggingFace format), 1 MB total, 1.34s |
| Pull + checksum | PASS | 4/4 files verified, SHA-256 match |
| Push single 10 MB | PASS | 8.2 MB/s push, 26.3 MB/s pull, checksum verified |
| HEAD request | PASS | OCI: 200 existing / 404 missing, Harbor API: 200 with artifact list |
| Custom media types | PASS | `application/vnd.supernova.model.*` round-trips correctly |

## Installation

```bash
pip install oras==0.2.42
```

Dependencies: `jsonschema`, `requests`. No compiled extensions — pure Python.

Note: `login()` writes credentials to `~/.docker/config.json`. The `docker` Python package is optional (oras falls back to its own config writer).

## API Patterns

### Authentication

```python
import oras.client

client = oras.client.OrasClient(hostname="imgrepo.damit.hu", auth_backend="token")
client.login(hostname="imgrepo.damit.hu", username=username, password=password)
```

- `auth_backend="token"` (default) — exchanges basic credentials for a bearer token per the OCI Distribution spec. This is correct for Harbor.
- `login()` persists credentials to `~/.docker/config.json`. For containers, ensure the path is writable or pre-mount the Docker config.
- `OrasClient` IS a `Registry` (inheritance, not composition) — all low-level methods (`upload_blob`, `get_manifest`, etc.) are available directly on the client instance.

### Simple Push (file-per-layer)

```python
import oras.utils

with oras.utils.workdir(model_dir):
    response = client.push(
        files=["config.json", "model.safetensors", "tokenizer.json"],
        target="imgrepo.damit.hu/supernova/iris-osl:v3",
    )
# response.status_code == 201
# response.headers["Docker-Content-Digest"] == "sha256:..."
```

**Critical**: files must be relative to the current working directory. Use `oras.utils.workdir()` to set context before push. Absolute paths raise `ValueError`.

Each file becomes a separate OCI layer with media type `application/vnd.oci.image.layer.v1.tar` and an `org.opencontainers.image.title` annotation set to the filename.

### Simple Pull

```python
pulled_files = client.pull(
    target="imgrepo.damit.hu/supernova/iris-osl:v3",
    outdir="/tmp/pulled-model",
)
# pulled_files == ["/tmp/pulled-model/config.json", "/tmp/pulled-model/model.safetensors", ...]
```

Files are named by the `org.opencontainers.image.title` annotation (basename from push).

### Custom Media Types (SuperNova pattern)

For production use, models and datasets should use SuperNova-specific media types to distinguish artifact types in the registry. This requires the low-level API:

```python
import oras.oci
import oras.defaults
import oras.utils

# 1. Build manifest
manifest = oras.oci.NewManifest()

# 2. Config blob with custom media type
conf, _ = oras.oci.ManifestConfig(config_json_path)
conf["mediaType"] = "application/vnd.supernova.model.config.v1+json"

# 3. Weights layer (tar.gz the model directory)
blob = oras.utils.make_targz(model_dir)
layer = oras.oci.NewLayer(blob, "application/vnd.supernova.model.weights.v1+tar+gzip", is_dir=True)
layer["annotations"] = {oras.defaults.annotation_title: "model.tar.gz"}
manifest["layers"].append(layer)
manifest["config"] = conf

# 4. Upload blobs then manifest
container = client.get_container("imgrepo.damit.hu/supernova/iris-osl:v3")
client.upload_blob(config_json_path, container, conf)
client.upload_blob(blob, container, layer)
client.upload_manifest(manifest, container)
```

`ManifestConfig()` defaults to `application/vnd.unknown.config.v1+json` — override the `mediaType` key before upload. `NewLayer()` accepts `media_type` as a parameter directly.

### Artifact Existence Check (HEAD request)

For the Data Repository's `harbor_ref` validation on registration (D-006):

```python
# OCI Distribution spec — token-scoped HEAD
token = get_token(host, repo, "pull", basic_auth)
resp = requests.head(
    f"https://{host}/v2/{repo}/manifests/{tag}",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.oci.image.manifest.v1+json",
    },
)
# 200 → exists, returns Docker-Content-Digest and Content-Length
# 404 → does not exist
```

Harbor's token endpoint grants tokens even for non-existent repositories — the 404 comes from the HEAD itself, not from token acquisition.

**Harbor v2.0 API alternative** (basic auth, no token needed):

```
GET /api/v2.0/projects/supernova/repositories/<name>/artifacts
```

Returns 200 with an empty array for non-existent repos (not 404). Less useful for existence checks — use the OCI Distribution HEAD instead.

## Performance

| Operation | Size | Time | Throughput |
|-----------|------|------|------------|
| Push 4 files | 1 MB | 1.34s | ~0.7 MB/s |
| Pull 4 files | 1 MB | 0.29s | ~3.4 MB/s |
| Push single file | 10 MB | 1.22s | 8.2 MB/s |
| Pull single file | 10 MB | 0.38s | 26.3 MB/s |

Throughput improves with larger payloads (less per-request overhead). Push is slower than pull due to blob upload initiation (POST + PUT per blob) and manifest upload. These numbers are for the local network — production performance on the K8s cluster (same datacenter as Harbor) should be similar or better.

Multi-file push creates one layer per file, meaning N+1 HTTP requests (N blob uploads + 1 manifest). For models with many small files, consider tar.gz into a single layer (as the custom media type pattern does).

## Findings & Recommendations

### For Step Containers (S-028, S-029, S-030)

1. **Use the simple `push()`/`pull()` API** for straightforward file transfer. It handles layer creation, blob upload, and manifest assembly automatically.

2. **Use `oras.utils.workdir()`** to set the file context before push. Never pass absolute paths to `push()`.

3. **For model directories**: tar.gz into a single layer with `oras.utils.make_targz()` before push. This reduces HTTP round-trips and is more efficient for directories with many small files (tokenizer configs, vocab files, etc.).

4. **Credentials**: `login()` persists to `~/.docker/config.json`. In containers, either:
   - Mount the Docker config as a volume/secret
   - Call `login()` at container startup with credentials from env vars

### For Data Repository (D-006)

1. **Artifact existence validation**: Use OCI Distribution HEAD (`/v2/<repo>/manifests/<tag>`) with a scoped bearer token. Returns 200 + digest for existing artifacts, 404 for missing ones.

2. **Digest extraction**: The `Docker-Content-Digest` header from HEAD (or push response) gives the manifest digest for storage in `artifact_versions.digest`.

3. **Do NOT use Harbor v2.0 API for existence checks** — it returns 200 with empty arrays for non-existent repos, making it unreliable for validation. Use it only for listing/browsing.

4. **Token management**: Tokens are scoped to a single repository and expire in 30 minutes. Cache and reuse within that window; request new tokens when switching repositories.

### Limitations

- **No streaming upload**: oras-py loads entire blobs into memory for upload. For very large files (>1 GB GGUF models), monitor memory usage. The library does use chunked reads for download (`download_blob`).

- **No progress callbacks**: Neither `push()` nor `pull()` provide progress reporting. For large transfers in step containers, consider wrapping with custom progress tracking at the HTTP layer.

- **Working directory requirement**: `push()` mandates files be in or relative to `cwd`. This is enforced at the library level. Always use `oras.utils.workdir()`.

- **`login()` side effect**: Writes to `~/.docker/config.json`. In multi-tenant or CI environments, ensure isolation (use separate home directories or temp Docker configs).

## Custom Media Types Reference

| Media Type | Purpose |
|------------|---------|
| `application/vnd.supernova.model.config.v1+json` | Model artifact config (metadata JSON) |
| `application/vnd.supernova.model.weights.v1+tar+gzip` | Model weights layer (tar.gz) |
| `application/vnd.supernova.dataset.config.v1+json` | Dataset artifact config (metadata JSON) |
| `application/vnd.supernova.dataset.content.v1+tar+gzip` | Dataset content layer (tar.gz) |

All custom types are preserved by Harbor — verified in the POC. Manifests use the standard OCI wrapper `application/vnd.oci.image.manifest.v1+json`.

## Files

| File | Purpose |
|------|---------|
| `poc/oras_poc.py` | POC script — all tests |
| `poc/findings.md` | This document |
| `requirements.txt` | Python dependencies (pinned) |
| `temp/harbor-credentials.yaml` | Robot account credentials (gitignored) |

## Related Issues

- **D-001** — Harbor `supernova` project (prerequisite, done)
- **D-004** — K8s secrets for Harbor credentials
- **D-006** — Data Repository Harbor integration (uses HEAD pattern from this POC)
- **S-028, S-029, S-030** — Step containers (use push/pull patterns from this POC)
