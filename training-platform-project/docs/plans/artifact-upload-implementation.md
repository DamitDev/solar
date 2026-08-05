# Artifact Upload — Implementation Plan

Companion to [docs/specs/artifact-upload.md](../specs/artifact-upload.md). That
document is the contract; this one is the execution order, the test matrix, and the
PR text.

| Field | Value |
|-------|-------|
| Issues | S-045, S-046, S-047, U-007 |
| Repos | `solar` (monorepo), `aiops-k8s` |
| Created | 2026-08-05 |

## 0. Deliverables

| # | Issue | Repo / app | Branch | Size |
|---|-------|------------|--------|------|
| 1 | S-045 | `apps/supernova-steps` | `feature/S-045` | S |
| 2 | S-046 | `apps/solar-host` | `feature/S-046` | S |
| 3 | S-047 | `apps/solar-control` + `aiops-k8s` | `feature/S-047` | L |
| 4 | U-007 | `apps/solar-webui` | `feature/U-007` | M |

### Sequencing

S-045 and S-046 are independent of each other and of the rest; ship them first as
two small, reviewable PRs. They fix a live defect and are worth merging even if the
upload UI slips.

S-047 must merge before U-007 has anything to call. U-007 can be developed in
parallel against a locally running Solar Control.

The `aiops-k8s` change ships **with** S-047 but must be applied to the cluster
*before* the Solar Control image carrying the relay is rolled out, otherwise the
first upload dies at the 10 MB ingress cap.

```
S-045 ──┐
S-046 ──┼──> (independent, merge first)
        │
S-047 ──┴──> U-007
  └── aiops-k8s PR (apply before image rollout)
```

---

## 1. S-045 — Fix the `upload_model` artifact layout

**Repo:** `apps/supernova-steps` · **Branch:** `feature/S-045`

### Problem

`push_to_harbor` calls `OrasHelper.push_custom`, which tars the content directory
into one `model.tar.gz` layer. Solar Host pulls flat and never untars, so
`_select_gguf_path` finds no `.gguf` and the pull fails with `404 not_found`. Every
model produced by a training pipeline is unusable for llama.cpp inference.

### Changes

`upload-model/entrypoint.py`:

1. Replace the `push_custom` call in `push_to_harbor` with a flat push that emits
   one layer per file plus a SuperNova-typed config blob (spec §2.1). Walk the
   source directory recursively; the layer title is the path relative to the
   artifact root, POSIX-separated.
2. Validate every relative path against spec §2.3 before pushing. Reject absolute
   paths, `..` segments, and duplicates with a `PushError`.
3. Delete `prepare_content_dir` — a single-file source becomes a one-layer artifact
   titled after the file, with no temp directory and no copy.
4. Pass `size_bytes` to `register_version` (sum of file sizes) and update the
   docstring that currently explains why it was omitted. The stored bytes now equal
   the source bytes, so the value is truthful.
5. Skip symlinks rather than following them, matching `compute_dir_size`.

If `OrasHelper.push` (already one-layer-per-file with title annotations) is used
directly, only the config blob needs custom assembly. Prefer that over
reimplementing the blob dance — the step has the files on local disk and does not
need streaming.

### Unit tests (`upload-model/tests/test_upload_model.py`)

Existing `push_custom` assertions must be rewritten, not deleted.

| Test | Asserts |
|------|---------|
| `test_push_emits_one_layer_per_file` | Layer count equals file count; every layer carries a title annotation |
| `test_push_layer_titles_are_relative_posix_paths` | `sub/dir/file.bin` → title `sub/dir/file.bin`, not an absolute path |
| `test_push_single_file_source_titles_the_file` | No temp directory is created; title is the file name |
| `test_push_rejects_path_traversal_title` | A crafted `..` path raises `PushError` |
| `test_push_rejects_duplicate_titles` | Raises `PushError` |
| `test_push_skips_symlinks` | Symlinked entry is absent from the layers |
| `test_config_blob_uses_supernova_media_type` | `application/vnd.supernova.model.config.v1+json` for models, dataset equivalent for datasets |
| `test_register_version_sends_size_bytes` | Payload contains the summed size |
| `test_push_empty_directory_raises` | Existing `require_source_artifact` behaviour still holds |

### Commands

```bash
make test-supernova-steps
make lint-supernova-steps
cd apps/supernova-steps && uv run black .
```

### Acceptance

- `make test-supernova-steps` green, `make lint-supernova-steps` clean.
- Manual: push a fixture with a `.gguf` to `supernova/test-s045:v1`, pull it with
  `OrasHelper.pull`, confirm `_select_gguf_path` returns the `.gguf`. Delete the
  test artifact afterwards.

---

## 2. S-046 — Fix nested-path digest verification in Solar Host

**Repo:** `apps/solar-host` · **Branch:** `feature/S-046`

### Problem

`_verify_pulled_digests` iterates `target_dir.iterdir()` non-recursively and keys
`actual` on `path.name`. A manifest layer titled `nested/extra.txt` is restored
correctly by `oras` but is then reported as `missing on disk after pull`, and the
whole pull fails with `ModelPullError`. Verified against real Harbor.

### Changes

`solar_host/models_manager.py`:

1. `_verify_pulled_digests` walks recursively (`rglob("*")`), keying on
   `path.relative_to(target_dir).as_posix()`.
2. Reject manifest titles that violate spec §2.3 before comparing, so a malicious
   `../../etc/passwd` title fails loudly instead of silently verifying a file
   outside `target_dir`.
3. `_verify_cached_digests` already indexes `base / name`, which resolves nested
   names correctly — add a regression test rather than changing it.

Returned `file_digests` keys become relative paths. Manifest entries written before
this change hold bare filenames; for a flat artifact the two are identical, so no
migration is needed. Note this in the docstring.

### Unit tests (`tests/`)

| Test | Asserts |
|------|---------|
| `test_verify_pulled_digests_accepts_nested_paths` | Nested layer verifies OK (fails before the fix) |
| `test_verify_pulled_digests_flat_unchanged` | Flat artifact still returns the same mapping |
| `test_verify_pulled_digests_detects_nested_mismatch` | Corrupted nested file raises `ModelPullError` |
| `test_verify_pulled_digests_detects_missing_nested_file` | Deleted nested file raises `ModelPullError` |
| `test_verify_pulled_digests_rejects_traversal_title` | `../escape` title raises `ModelPullError` |
| `test_verify_cached_digests_nested` | Cached nested artifact verifies |

### Commands

```bash
make test-solar-host
make lint-solar-host
cd apps/solar-host && uv run black .
```

### Acceptance

- `make test-solar-host` green, `make lint-solar-host` clean.
- The spike reproduction (flat artifact containing `nested/extra.txt`) verifies
  instead of raising.

---

## 3. S-047 — Artifact upload relay in Solar Control

**Repo:** `apps/solar-control` + `aiops-k8s` · **Branch:** `feature/S-047`

### New modules

Following existing conventions (`app/routes/management/`, `app/services/`,
`app/models/`, `app/redis_state/`):

| File | Responsibility |
|------|----------------|
| `app/harbor/oci_push.py` | Streaming OCI push client: token, open session, chunked `PATCH`, close blob, push manifest |
| `app/redis_state/uploads.py` | Session hash CRUD, TTL, per-file digest recording |
| `app/services/uploads.py` | Validation, pre-flight conflict check, orchestration, registration, rollback |
| `app/models/uploads.py` | Pydantic request/response models |
| `app/routes/management/uploads.py` | The five endpoints from spec §4.2 |

`app/config.py` gains `harbor_url`, `harbor_username`, `harbor_password`,
`upload_chunk_size_bytes` (default 8 MiB), `upload_session_ttl_s` (default 86400).

### Implementation notes that are easy to get wrong

These four are load-bearing and each one was an observed failure against real
Harbor. Encode each as a unit test so a refactor cannot silently regress it.

1. **No cookies.** Harbor sets `sid` on `/v2/` responses; replaying it fails with
   `403 CSRF token invalid`. Construct the `httpx.AsyncClient` with cookie
   persistence disabled and clear the jar between requests.
2. **Preserve the `_state` query.** Append `&digest=` to the upload `Location`;
   never pass `params=` to `httpx`, which replaces the query and yields
   `404 BLOB_UPLOAD_INVALID`.
3. **8 MiB chunks.** Above the 5 MiB minimum imposed by object-storage registry
   drivers.
4. **Refresh the token per chunk when near expiry.** Verified that a fresh token is
   accepted mid-session, including on the closing `PUT`. This is what makes upload
   duration unbounded.

Also: read the request body with `Request.stream()` so FastAPI never materialises
it; compute sha256 incrementally; never call `await request.body()`.

### Unit tests (`tests/`)

Harbor is mocked with `respx` or an `httpx.MockTransport`.

**`test_uploads_oci_push.py`**

| Test | Asserts |
|------|---------|
| `test_blob_upload_streams_in_chunks` | A 20 MiB body produces 3 `PATCH` calls with correct `Content-Range` |
| `test_blob_upload_computes_digest_while_streaming` | Returned digest equals sha256 of the input |
| `test_blob_upload_never_sends_cookies` | No `Cookie` header on any request after Harbor sets `sid` |
| `test_blob_close_preserves_state_query` | Closing URL retains `_state` and appends `digest` |
| `test_token_refreshed_when_near_expiry` | A second token is minted mid-upload and used |
| `test_patch_failure_raises_and_aborts_session` | Non-202 surfaces a typed error |
| `test_manifest_layers_use_flat_layout` | One layer per file, title annotations, correct config media type |
| `test_peak_memory_is_bounded` | Streaming a large body never buffers more than one chunk |

**`test_uploads_service.py`**

| Test | Asserts |
|------|---------|
| `test_create_rejects_invalid_name` | Mirrors `_NAME_RE`; 422 |
| `test_create_rejects_reserved_latest_version` | 422 |
| `test_create_rejects_traversal_path` | `../x`, `/abs`, duplicates → 422 |
| `test_create_rejects_empty_file_list` | 422 |
| `test_create_rejects_existing_version` | Pre-flight conflict → 409, no Harbor call |
| `test_create_rejects_category_mismatch` | Existing model name uploaded as dataset → 409 |
| `test_create_assigns_next_version_when_omitted` | Returns `v{n+1}` |
| `test_complete_requires_all_files_uploaded` | Missing file → 409 |
| `test_complete_registers_with_summed_size_bytes` | Registration payload carries the true total, not the manifest size |
| `test_complete_rolls_back_harbor_tag_on_registration_failure` | Harbor delete issued; error propagated |
| `test_abort_marks_session_aborted` | Subsequent file upload → 409 |
| `test_session_survives_replica_change` | Session read back from Redis by a second service instance |

**`test_uploads_routes.py`**

| Test | Asserts |
|------|---------|
| `test_upload_endpoints_require_management_key` | 401 without the key |
| `test_put_file_unknown_session_404` | |
| `test_put_file_undeclared_path_422` | |
| `test_get_status_reports_progress` | `bytes_done` / `bytes_total` |
| `test_complete_returns_harbor_ref_and_version` | |

### Integration tests (`tests_integration/`)

The suite already runs a stub Harbor speaking OCI Distribution, but read-only
(`do_GET`, `do_HEAD`, `do_POST` for tokens). Extend
`tests_integration/fixtures/stub_harbor.py` with the write side:

- `POST /v2/<repo>/blobs/uploads/` → `202` + `Location` carrying a `_state` param
- `PATCH <location>` honouring `Content-Range`, returning `202` + a new `Location`
- `PUT <location>?digest=` → `201`, verifying the digest matches the assembled bytes
- `PUT /v2/<repo>/manifests/<tag>` → `201` + `Docker-Content-Digest`

Reproduce the real registry's two traps so the tests catch regressions:

- return `403 CSRF token invalid` if a request carries the `sid` cookie;
- return `404 BLOB_UPLOAD_INVALID` if the `_state` param is missing on close.

New tests in `tests_integration/repo_path/test_upload_path.py`:

| Test | Asserts |
|------|---------|
| `test_upload_multi_file_artifact_end_to_end` | Session → 3 file streams → complete → version visible via `GET /api/catalog/models` |
| `test_uploaded_artifact_is_pullable_by_host` | Solar Host pulls the uploaded artifact and passes digest verification — closes the loop with S-045/S-046 |
| `test_upload_nested_paths_round_trip` | Nested layout survives upload → pull → verify |
| `test_upload_conflicting_version_rejected` | Pre-flight 409 |
| `test_upload_registration_failure_rolls_back_tag` | Data Repository forced to fail; stub Harbor records the DELETE |
| `test_upload_large_file_multi_chunk` | ~24 MiB file produces multiple `PATCH` calls |

### `aiops-k8s` changes

Separate PR in `/mnt/nvme/AI/damit-aiops/aiops-k8s`.

`environments/solar/values/solar.yaml` and `environments/solar-dev/values/solar.yaml`:

```yaml
solar-control:
  ingress:
    annotations:
      nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
      nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
      nginx.ingress.kubernetes.io/proxy-body-size: "0"
      nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
  resources:
    requests: {cpu: "50m", memory: "128Mi"}
    limits:   {cpu: "1000m", memory: "512Mi"}
  config:
    HARBOR_URL: "https://imgrepo.damit.hu"

solar-webui:
  ingress:
    annotations:
      nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
      nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
      nginx.ingress.kubernetes.io/proxy-body-size: "0"
      nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
  resources:
    limits: {cpu: "500m", memory: "128Mi"}
  config:
    PROXY_TIMEOUT_MS: "3600000"
```

Also:

- `charts/solar/charts/solar-control/values.yaml` — add the `HARBOR_URL` config key
  and the `HARBOR_USERNAME` / `HARBOR_PASSWORD` secret keys to the templates.
- `environments/*/secrets.yaml.example` — document the two new secret keys.
- Regenerate both sealed secrets with `scripts/generate-sealed-secrets.sh` (prod
  targets `damit-prod`, dev targets `damit-tst`).
- Bump `charts/solar/Chart.yaml` and the `solar-control` subchart version; update
  the ArgoCD `targetRevision` after publishing.

The `solar-webui` ingress annotations are the part most likely to be forgotten. The
upload request crosses that ingress first, and it currently sets none, inheriting
the 1 MB nginx default.

### Commands

```bash
make test-solar-control
make lint-solar-control
cd apps/solar-control && uv run black .
make integration                       # requires Docker
make export-requirements               # if dependencies changed
```

### Acceptance

- Unit and integration suites green; ruff and black clean.
- A `helm template` of the modified chart renders both ingresses with all four
  annotations.
- Manual end-to-end against real Harbor (§6).

---

## 4. U-007 — Artifact upload UI in Solar WebUI

**Repo:** `apps/solar-webui` · **Branch:** `feature/U-007`

### Changes

| File | Change |
|------|--------|
| `src/App.tsx` | New `/upload` route |
| `src/components/Navigation.tsx` | Nav entry |
| `src/api/client.ts` | `createUpload`, `uploadFile`, `getUploadStatus`, `completeUpload`, `abortUpload` |
| `src/api/types.ts` | Upload session, file entry, progress types |
| `src/components/ArtifactUpload.tsx` | Wizard shell and step state |
| `src/components/upload/CategoryStep.tsx` | Model / Dataset choice plus the per-category requirements panel |
| `src/components/upload/FolderStep.tsx` | Directory picker, exclusion filter, file review table |
| `src/components/upload/MetadataStep.tsx` | Category-specific metadata form |
| `src/components/upload/ProgressStep.tsx` | Per-file and aggregate progress |
| `src/lib/uploadPaths.ts` | Relative-path derivation, exclusion rules, path validation |

Existing conventions apply: Nord theme classes, `lucide-react` icons, `cn()` from
`src/lib/utils.ts`, loading/error patterns from `Dashboard.tsx`.

### Behaviour

- Category is chosen explicitly; it drives the endpoint, the requirements panel, and
  the metadata form (spec §5.1). Dataset `format` is a required select constrained
  to `parquet` / `hdf5` / `json`, because the Data Repository rejects anything else.
- Folder selection uses `<input type="file" webkitdirectory directory multiple>`.
  A browser cannot read a typed path, so there is no text input for one. The first
  segment of `webkitRelativePath` is stripped so the artifact root is the folder's
  contents. Directories are never in a `FileList`, so "only files are uploaded"
  holds by construction.
- The exclusion list from spec §5.2 is applied automatically and shown to the user;
  every remaining file is individually deselectable.
- `XMLHttpRequest` is used for uploads, because `upload.onprogress` is the only
  reliable progress source. Concurrency limited to 2–3 files.
- `beforeunload` guard while uploading; individual file retry on failure.

### Unit tests (Vitest, `src/**/__tests__`)

| Test | Asserts |
|------|---------|
| `uploadPaths.relativePath strips the root folder segment` | `my-model/config.json` → `config.json` |
| `uploadPaths excludes junk files` | `.git/`, `.DS_Store`, `__pycache__/`, `*.pyc` filtered |
| `uploadPaths rejects traversal and absolute paths` | Validation error |
| `uploadPaths preserves nested paths` | `weights/shard-1.bin` kept intact |
| `CategoryStep renders model requirements` | `config.json` / `.gguf` guidance shown |
| `CategoryStep renders dataset requirements` | Format select present and required |
| `FolderStep warns on a model with no config.json and no gguf` | Warning, not a hard block |
| `FolderStep warns when no file matches the declared dataset format` | Warning shown |
| `MetadataStep validates artifact name against the repo pattern` | Rejects uppercase and leading `-` |
| `MetadataStep rejects the reserved version "latest"` | |
| `ProgressStep aggregates per-file bytes` | Total progress correct |
| `ArtifactUpload aborts the session on cancel` | `abortUpload` called |

### Commands

```bash
make test-solar-webui                  # eslint + prettier + vitest
pnpm --filter solar-webui lint:fix
pnpm --filter solar-webui build        # tsc must pass
```

### Acceptance

- Lint, format, type-check, and Vitest green.
- Manual run against a local Solar Control with real Harbor credentials: upload a
  multi-file model folder, confirm it appears in `/catalog` and is deployable.

---

## 5. Cross-cutting quality gates

Run before opening each PR:

```bash
make lint          # ruff across all Python apps + webui lint
make format        # black across all Python apps
make test          # all unit suites + webui lint
make integration   # solar-control cross-service suite (Docker required)
```

CI mirrors this: `ci.yaml` dispatches `quality-gates.yaml` per changed path, and
`integration-tests.yaml` runs on `solar-control` / `solar-host` / `data-repository`
paths. No workflow changes are needed — every touched app already has a matrix
entry.

Conventions to honour:

- Conventional commit titles (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
- PR body uses `.github/PULL_REQUEST_TEMPLATE.md`: `## Description`, `## Changes`,
  optional `## Related Issues`.
- Code comments and this plan in English; user-facing README changes in Hungarian.
- Docker image names are fixed — `aiops/*` and `supernova/steps/*` are referenced by
  `aiops-k8s` and must not change.
- Never commit or push directly; the supervisor handles git operations.

### Release

`.github/release-manifest.json` needs no structural change. The release cuts:

- `upload-model` (S-045)
- `solar-control` (S-047)
- `solar-webui` (U-007)

`solar-host` (S-046) publishes to PyPI via `publish-host.yaml` and needs a version
bump in its `pyproject.toml`.

---

## 6. Manual validation against real Harbor

Credentials live in `apps/data-repository/.env`. Use the `supernova` project and
prefix every test repository with `test-`.

```bash
cd /mnt/nvme/AI/damit-aiops/solar
set -a && . apps/data-repository/.env && set +a
```

| Step | Check |
|------|-------|
| 1 | Start Data Repository (`make dev-data-repository`) and Solar Control (`make dev-control`) with Harbor env set |
| 2 | `POST /api/uploads` for `test-upload-manual` as a model, 3 files including one >16 MiB |
| 3 | Stream each file; confirm multi-chunk `PATCH` in the Solar Control log |
| 4 | `POST .../complete`; confirm `201` manifest push and Data Repository registration |
| 5 | `GET /api/resolve?uri=repo://test-upload-manual:v1` returns the Harbor ref and the **true** `size_bytes` |
| 6 | `OrasHelper.pull` the artifact; confirm flat file layout and byte-identical files |
| 7 | Run Solar Host's `_verify_pulled_digests` against it; expect OK |
| 8 | Repeat with a nested subdirectory; expect OK after S-046 |
| 9 | Upload the same version again; expect a pre-flight 409 |
| 10 | Force a registration failure; confirm the Harbor tag is deleted |

Cleanup:

```bash
curl -X DELETE -u "$HARBOR_USERNAME:$HARBOR_PASSWORD" \
  "https://imgrepo.damit.hu/api/v2.0/projects/supernova/repositories/test-upload-manual/artifacts/v1"
```

The robot account may delete artifacts but not repositories, so empty `test-*`
repositories will remain. That is expected and harmless.

Post-deploy, repeat steps 2–5 against `solar-dev` to confirm the ingress
annotations took effect — a 413 means `proxy-body-size` did not apply, and an
upload that stalls before any Solar Control log line means
`proxy-request-buffering` is still on.

---

## 7. Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Harbor GC reclaims blobs between file upload and `complete` | Low | Sessions are short-lived; GC only targets unreferenced blobs after a grace period |
| Ingress annotations not honoured by `nginx-external` | Medium | Validate on `solar-dev` first; a 413 is the tell |
| Very slow client holds a Solar Control worker for an hour | Medium | 2 replicas, async I/O; revisit if uploads become frequent |
| Browser memory on very large folders | Low | Only `File` handles are held; bytes stream from disk |
| Existing manifest entries keyed by bare filename | Low | Identical to relative paths for flat artifacts; noted in the S-046 docstring |

---

## 8. PR titles and descriptions

### S-045

```
fix(upload-model): push flat file-per-layer artifacts instead of tar.gz
```

---

```
## Description

The `upload_model` step pushed model artifacts as a single `model.tar.gz` layer via
`OrasHelper.push_custom`. Solar Host pulls artifacts flat and never untars them, so
`_select_gguf_path` found no `.gguf` file and every pipeline-produced model failed
to deploy with `404 not_found`. Verified against the live registry: a tar.gz
artifact pulls back as `['model.tar.gz']` and GGUF selection returns `None`, while
the same fixture pushed flat selects the `.gguf` correctly.

This switches the step to the canonical flat file-per-layer layout defined in
`docs/specs/artifact-upload.md`: one OCI layer per file, the layer digest being the
sha256 of the raw file bytes and the file name carried in
`org.opencontainers.image.title`. That is exactly what Solar Host's post-pull
digest verification already assumes.

## Changes

- Push one layer per file instead of tarring the content directory, keeping the
  SuperNova config media type on the config blob.
- Validate layer titles: relative POSIX paths only, no `..` segments, no duplicates.
- Remove `prepare_content_dir`; a single-file source is now a one-layer artifact
  with no temp copy.
- Skip symlinks rather than following them.
- Send a truthful `size_bytes` on registration now that stored bytes equal source
  bytes.
- Rewrite the push unit tests for the flat layout and add traversal, duplicate,
  symlink, and media-type cases.

## Related Issues

Closes #S-045
```

### S-046

```
fix(models-manager): verify nested artifact paths after a Harbor pull
```

---

```
## Description

`_verify_pulled_digests` iterated the pull target non-recursively and keyed files by
bare name, so any artifact containing a subdirectory failed verification with
`<path>: missing on disk after pull` even though `oras` had restored it correctly.
Verified against the live registry with an artifact containing `nested/extra.txt`.

This makes verification walk recursively and key on the path relative to the target
directory, matching the `org.opencontainers.image.title` values in the manifest.

## Changes

- Walk the pull target recursively and key digests on the relative POSIX path.
- Reject manifest titles that are absolute or contain `..`, so a crafted artifact
  cannot verify a file written outside the target directory.
- Add regression tests for nested verification, nested corruption, nested deletion,
  and traversal titles.

## Related Issues

Closes #S-046
```

### S-047

```
feat(uploads): stream artifact uploads through Solar Control into Harbor
```

---

```
## Description

There was no way to get a locally held model or dataset into the platform: the Data
Repository is metadata-only, and Harbor does not support CORS, so a browser cannot
push to it directly (verified — no `Access-Control-Allow-Origin` on any response and
`OPTIONS` is not a recognised route).

This adds an upload relay to Solar Control. The OCI blob upload protocol is chunked,
so the relay forwards the request body to Harbor in 8 MiB chunks while computing the
sha256 as the bytes pass through. Nothing is staged to disk and peak memory is one
chunk, which is what makes this viable inside the current pod limits — no PVC and no
object storage are required. Upload sessions live in Redis so either replica can
serve any request.

Four registry behaviours are encoded as tests because each one was an observed
failure against real Harbor: the `sid` cookie must never be replayed (403 CSRF), the
`_state` query parameter must be preserved when appending `digest` (404
BLOB_UPLOAD_INVALID), chunks must be at least 5 MiB for object-storage backends, and
a refreshed bearer token is accepted mid-session — which is what removes the
30-minute ceiling on upload duration.

See `docs/specs/artifact-upload.md` for the full contract.

## Changes

- Add `POST /api/uploads`, `PUT /api/uploads/{id}/files`, `GET /api/uploads/{id}`,
  `POST /api/uploads/{id}/complete`, and `DELETE /api/uploads/{id}`.
- Add a streaming OCI push client, a Redis-backed session store, and the upload
  service that validates, orchestrates, registers, and rolls back.
- Validate artifact name, version, category, and file paths up front, including a
  pre-flight version conflict check against the Data Repository so a conflict fails
  in the form rather than after a long upload.
- Register with an explicit `size_bytes`; the Data Repository's fallback derives it
  from the manifest HEAD, which reports the manifest size, not the artifact size.
- Delete the pushed Harbor tag when registration fails, so a retry is not blocked.
- Add `HARBOR_URL` / `HARBOR_USERNAME` / `HARBOR_PASSWORD` settings.
- Extend the integration stub Harbor with the OCI write path, including the CSRF and
  `_state` traps, and add end-to-end upload tests that finish with a Solar Host pull.

## Related Issues

Closes #S-047
```

### aiops-k8s

```
chore(solar): allow streamed artifact uploads through the ingress
```

---

```
## Description

Solar Control gains an artifact upload relay (S-047). The current ingress caps
request bodies at 10 MB on Solar Control and at the 1 MB nginx default on Solar
WebUI, so uploads would be rejected before reaching the service.

`proxy-request-buffering: "off"` matters as much as the size cap: without it the
ingress controller buffers the entire request body to its own local disk before
forwarding anything, which would fill node ephemeral storage on a large upload.

## Changes

- Remove the body-size cap and disable request buffering on the solar-control and
  solar-webui ingresses in both environments.
- Add `HARBOR_URL` to the solar-control ConfigMap and `HARBOR_USERNAME` /
  `HARBOR_PASSWORD` to `solar-control-secret`; update `secrets.yaml.example` and
  regenerate both sealed secrets.
- Raise the solar-control CPU limit to 1000m and solar-webui to 500m so a single
  upload does not throttle inference routing.
- Raise the WebUI `PROXY_TIMEOUT_MS` to match the 3600s ingress timeouts.
- Bump the solar chart and solar-control subchart versions.

Apply before rolling out the Solar Control image that carries the relay.
```

### U-007

```
feat(webui): add model and dataset upload
```

---

```
## Description

Operators had no way to publish a model they hold locally — some of our models
cannot go to HuggingFace, and until now the only ingestion path was a full SuperNova
training pipeline. This adds an upload wizard that pushes a folder from the
operator's machine into Harbor via the Solar Control relay (S-047) and registers it
in the Data Repository.

Model and dataset uploads are deliberately distinct choices rather than inferred
from folder contents: the category determines the registration endpoint, the OCI
config media type, and the metadata form. Each shows its own requirements, and
dataset uploads require a `format` constrained to the three values the Data
Repository accepts.

The folder is chosen with a directory picker rather than a typed path, since a
browser cannot read an arbitrary filesystem path. A `FileList` contains only files,
so directories never reach the artifact; common junk (`.git/`, `__pycache__/`,
`.DS_Store`) is filtered automatically and every remaining file is shown with its
size and can be deselected before upload.

## Changes

- Add an `/upload` route and navigation entry.
- Add a four-step wizard: category, folder selection and review, metadata, progress.
- Add typed API client methods for the five upload endpoints.
- Derive artifact-relative paths from `webkitRelativePath`, stripping the chosen
  folder's own name, and validate them against the layout contract.
- Warn when a model has neither `config.json` nor a `.gguf`, and when no file
  matches the declared dataset format.
- Track per-file and aggregate progress via `XMLHttpRequest`, cap concurrency, allow
  per-file retry, and guard navigation while an upload is in flight.
- Link to the new version in the catalog on success.

## Related Issues

Closes #U-007
```
