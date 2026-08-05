# Artifact Delete — Implementation Plan

| Field       | Value                                   |
|-------------|-----------------------------------------|
| Issues      | D-019, S-048, U-008                     |
| Status      | Draft                                   |
| Created     | 2026-08-05                              |
| Spec        | [docs/specs/artifact-delete.md](../specs/artifact-delete.md) |

## 0. Deliverables

- D-019 — Data Repository artifact-level delete (pure unregister + cascade).
- S-048 — Solar Control delete relay: `OciPushClient.delete_repository()`,
  `DataRepoClient.delete()`, `CatalogDeleteService`, version-aware guard,
  three catalog routes.
- U-008 — WebUI versions list + `CatalogDeleteModal` + client methods.

### Sequencing

D-019 first (S-048 depends on its endpoints), S-048 second, U-008 third. All three
land in one monorepo PR (`feature/S-048`): Data Repository's new endpoint has no
other consumer, so splitting would ship dead code.

---

## 1. D-019 — Artifact-level delete in the Data Repository

### Problem

No `DELETE /api/{models,datasets}/{name}` exists. `list_artifacts_by_category`
outer-joins the version-count subquery (`app/repositories/artifacts.py`), so an
artifact whose versions were all deleted keeps appearing as a ghost entry with
`versions_count: 0`. The artifact row must be deletable as a unit.

### Changes

- `app/repositories/artifacts.py` — `_delete_artifact(name, category, not_found_exc)`
  mirroring `_delete_artifact_version`: single `delete(Artifact).where(name, category)`,
  `rowcount == 0` → disambiguate via `_fetch_artifact_identity` → raise. Wrappers
  `delete_model(name)` / `delete_dataset(name)`.
- `app/services/models.py` — `delete_model(name)` / `delete_dataset(name)` on the
  deletion services: `_validate_artifact_name`, repo call, `_commit()`.
- `app/routes/models.py` + `app/routes/datasets.py` — `DELETE /{name}` → 204 with
  inline 422/404 mapping, matching the version-delete route.
- Docstrings (`_delete_artifact_version`, `docs/schema.md`) — Harbor deletion now
  happens in Solar Control's relay (S-048); no longer "deferred to N-029".

### Unit tests

- `tests/test_repositories_artifacts.py` — cascade delete removes version rows;
  unknown name → not-found; category mismatch → not-found.
- `tests/test_services_models.py` / `test_services_datasets.py` — success delegates
  to repo and commits; invalid name raises; not-found propagates;
  `test_delete_model_commits_before_returning` (regression style).
- `tests/test_routes_models.py` / `test_routes_datasets.py` — 204 empty body;
  404 for not-found; 422 for invalid name.

### Commands

```bash
PYTHONPATH="" make test-data-repository
PYTHONPATH="" make lint-data-repository
```

### Acceptance

`DELETE /api/models/{name}` returns 204 and the model disappears from
`GET /api/models` (no ghost entry); versions are gone; datasets behave identically.

---

## 2. S-048 — Catalog delete relay in Solar Control

### New modules

- `app/services/catalog_delete.py` — `CatalogDeleteService` (constructor takes
  `OciPushClient` + `DataRepoClient`, mirroring `UploadService`).

### Implementation notes that are easy to get wrong

- **Order matters**: Harbor `delete_tag` first, unregister second. Every step
  tolerates 404: Harbor delete (already gone), unregister (already gone).
- **Per-version results**: a repository delete unregisters each tag whose Harbor
  delete succeeded and keeps the artifact row when any tag failed — the response
  carries `deleted` / `failed` lists so the UI can report partial failure.
- **Guard `latest`**: `repo://{name}:latest` matches the artifact's **newest**
  version (needs the versions fetch; the flow already does it).
- **`delete_repository` is best-effort and runs last** (after the unregister): its
  outcome is reported, never raises for 403/404. 404 → `harbor_repository_removed:
  true` (Harbor auto-removed the empty repo); 403 → `false`.
- **`version == "latest"` → 422 at the solar-control edge** too, mirroring the Data
  Repository reserved-alias rule.
- **Enrichment**: `GET /versions` per-version `solar` block reads `version` from the
  host manifest (host `/models` entries carry it since D-016); hosts without a
  `version` field contribute to the model-level (not version-level) counts.

### Unit tests

- `tests/test_uploads_oci_push.py` style (`httpx.MockTransport`):
  `delete_repository` 200/202 → removed; 404 → removed (already gone); 403 →
  not-removed, no raise; 500 → not-removed, logged.
- `tests/test_catalog_delete_service.py` (fake `DataRepoClient` + fake OCI client):
  - version happy path: Harbor DELETE precedes unregister;
  - Harbor 404 tolerated; Harbor 403/500 → 502, unregister not called;
  - unregister 404 tolerated; other unregister errors surfaced;
  - 409 when an instance runs the version, incl. `repo://name:latest` → newest;
  - `version == "latest"` → 422; unknown name/version → 404;
  - repository delete: partial failure → `artifact_removed: false`, `failed`
    populated, artifact row kept; all clean → artifact row removed + best-effort
    repo delete reported;
  - guard uses only running instances; host-cached copies do not block.
- `tests/test_catalog.py` — routes: proxy mapping (204/404/422/502), 409 with
  blocking instance list and no upstream call, `DATA_REPOSITORY_URL` unset → 500,
  versions-list enrichment shape and degraded-host behaviour.

### Integration tests (`tests_integration/`)

- Extend `fixtures/stub_harbor.py` with repository-level DELETE (200 when the repo
  still has manifests; 404 when absent/empty — mirroring real Harbor's auto-removal).
- New `tests_integration/repo_path/test_delete_path.py`:
  1. upload artifact → in catalog + `repo://` resolves → delete version → Harbor
     got the DELETE (assert via `count_requests`), catalog no longer lists it,
     resolve 404s;
  2. upload two versions → delete repository → artifact gone from catalog and
     Harbor;
  3. stub rejects the Harbor delete → metadata survives.

### Commands

```bash
PYTHONPATH="" make test-solar-control
PYTHONPATH="" make lint-solar-control
PYTHONPATH="" make integration
```

### Acceptance

Deleting a version with a running instance → 409 listing it; without → 204 and the
version is gone from catalog and Harbor. Deleting a repository with one failing tag
→ 200 with `failed` populated and the artifact row kept.

---

## 3. U-008 — Catalog delete UI in Solar WebUI

### Changes

- `src/api/types.ts` — `CatalogModelVersion`, `CatalogVersionsResponse`,
  `CatalogDeleteResult`.
- `src/api/client.ts` — `getCatalogModelVersions`, `deleteCatalogModelVersion`,
  `deleteCatalogModel`.
- `src/components/ModelDetail.tsx` — Versions section (lazy fetch on open, one row
  per version with trash action), "Delete repository" action, `onDeleted` prop.
- New `src/components/CatalogDeleteModal.tsx` — two-phase modal modelled on
  `StorageDeleteModal`: confirm (blocked state with 409 instance list + disabled
  confirm, host-cache warning, typed name confirmation for repo delete) → results
  (per-version outcomes).
- `src/components/ModelCatalog.tsx` — `onDeleted` → `fetchCatalog()`.

### Behaviour

- Version rows show version, size, created, deployment badge, trash action.
- Version delete confirm: "Version v1 of model X will be removed from the catalog
  and deleted from Harbor."
- Repo delete confirm: typed model name required; warning text mentions Harbor
  artifact removal and that host copies remain until evicted.
- 409 → blocked state; 502 → error phase with the upstream detail.

### Unit tests (Vitest, `src/**/__tests__`)

- `client.test.ts` — three new methods hit the right method/URL/params.
- `ModelDetail.test.tsx` — versions render from the client response; delete-version
  flow calls the client and refetches; delete-repo flow calls the client and
  invokes `onDeleted`; 409 renders blocking instances with confirm disabled;
  typed confirmation gates repo delete; cancel does not call the client.
- Use `vi.spyOn(solarClient, ...)` and a MemoryRouter wrapper (ModelDetail uses
  `useNavigate`); scope repeated strings with `within(...)`.

### Commands

```bash
PYTHONPATH="" make test-solar-webui
pnpm --filter solar-webui build
```

### Acceptance

The catalog detail view lists versions with per-row delete; repository delete
requires typing the model name; running instances block with a rendered list;
successful deletes refresh the catalog.

---

## 4. Cross-cutting quality gates

```bash
PYTHONPATH="" make lint
PYTHONPATH="" make format
PYTHONPATH="" make test
PYTHONPATH="" make integration
```

Then review every touched file for introduced issues (Hermes equivalent of the
Cursor "ReadLints" step), and do a manual dev check per the S-045 convention:
`test-` prefixed repository in the `supernova` Harbor project, including the
`harbor_repository_removed` flag outcome (expected: `true` via 404 after a clean
artifact delete).
