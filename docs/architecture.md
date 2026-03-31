# Data Repository — Architecture

## Table of Contents

1. [Overview](#overview)
2. [System Context](#system-context)
3. [Technology Stack](#technology-stack)
4. [Project Structure](#project-structure)
5. [Layered Architecture](#layered-architecture)
6. [Application Lifecycle](#application-lifecycle)
7. [Dependency Injection](#dependency-injection)
8. [API Design Conventions](#api-design-conventions)
9. [Error Handling](#error-handling)
10. [Database Conventions](#database-conventions)
11. [Data Modelling Principles](#data-modelling-principles)
12. [Harbor Integration Pattern](#harbor-integration-pattern)
13. [Configuration](#configuration)
14. [Infrastructure Conventions](#infrastructure-conventions)
15. [Testing Conventions](#testing-conventions)
16. [Extending the Service](#extending-the-service)

---

## Overview

Data Repository is a lightweight **metadata catalog** for OCI artifacts (models and datasets) stored in Harbor. It acts as the single source of truth for artifact identity, versioning, lineage, and evaluation metrics across the SuperNova platform.

**What it stores:** Metadata only — names, versions, Harbor references, digests, sizes, training configs, evaluation metrics, and lineage links. Blob data (model weights, datasets) lives exclusively in Harbor.

**Separation of concerns:** Harbor is the blob store; Data Repository is the index. Consumers query this service for catalog, search, and URI resolution — they never hit Harbor directly for these purposes.

---

## System Context

```
┌──────────────────────────────────────────────────────────┐
│                    SuperNova Platform                    │
│                                                          │
│  ┌──────────────────┐                                    │
│  │ SuperNova Control│ ──► register artifacts/versions    │
│  └──────────────────┘         │                          │
│                               ▼                          │
│  ┌──────────────────┐  ┌─────────────────────┐           │
│  │  Solar Control   │◄─│   Data Repository   │           │
│  │ (repo:// URIs)   │  │                     │           │
│  └──────────────────┘  │  FastAPI + asyncpg  │           │
│                        │  PostgreSQL metadata│           │
│  ┌──────────────────┐  │  Harbor verification│           │
│  │  SuperNova WebUI │◄─│                     │           │
│  │  (catalog/search)│  └──────────┬──────────┘           │
│  └──────────────────┘             │                      │
│                                   ▼                      │
│                          ┌─────────────────┐             │
│                          │ Harbor Registry │             │
│                          │ (blob storage)  │             │
│                          └─────────────────┘             │
└──────────────────────────────────────────────────────────┘
```

The service registers artifacts on behalf of producers (SuperNova Control) and resolves references for consumers (Solar Control, WebUI). It never moves blob data — only metadata.

---

## Technology Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12 |
| Web framework | FastAPI |
| ASGI server | Uvicorn |
| Data validation / settings | Pydantic v2 + pydantic-settings |
| Database | PostgreSQL |
| ORM / async driver | SQLAlchemy 2 (async) + asyncpg |
| Migrations | Alembic |
| Harbor client | harbor-oci-client ([PyPI](https://pypi.org/project/harbor-oci-client/)) |
| Linting / formatting | Ruff + Black |
| Testing | pytest + pytest-asyncio |

For pinned versions see `requirements.txt` and `requirements-dev.txt`.

---

## Project Structure

```
data-repository/
├── app/
│   ├── main.py               # FastAPI app factory, lifespan, router registration
│   ├── config.py             # Centralised settings via pydantic-settings
│   ├── dependencies.py       # Application-level FastAPI Depends() providers
│   ├── exceptions.py         # Domain exceptions (HTTP-agnostic)
│   │
│   ├── routes/               # HTTP boundary: one file per domain area
│   ├── schemas/              # Pydantic request/response models: one file per domain area
│   ├── services/             # Business logic: one file per domain area
│   ├── repositories/         # SQL access layer: one file per domain area
│   │
│   ├── database/
│   │   ├── connection.py     # Async engine + session factory singleton
│   │   ├── dependencies.py   # get_db_session — per-request session provider
│   │   ├── models.py         # SQLAlchemy ORM models (schema source of truth for Alembic)
│   │   └── migrations/       # Alembic env and versioned migration scripts
│   │
│   └── harbor/
│       └── __init__.py       # HarborClient singleton lifecycle + re-exports
│
├── tests/
│   ├── conftest.py           # Shared fixtures
│   └── test_<layer>_<domain>.py  # Tests named by layer and domain area
│
├── docs/
│   ├── architecture.md       # This file — principles and conventions
│   ├── harbor.md             # Harbor protocol reference (auth, push/pull, media types)
│   └── schema.md             # PostgreSQL schema reference (DDL, indexes, example queries)
│
├── Dockerfile                # Multi-stage build
├── docker-compose.yaml       # Local dev environment
├── alembic.ini               # Alembic configuration
├── entrypoint.sh             # Container startup: migrate → serve
├── requirements.txt          # Production dependencies
└── requirements-dev.txt      # Dev/test dependencies
```

**Convention:** Code is organised by **layer first, domain area second**. A new domain (e.g. `datasets`) adds one file in each relevant layer rather than a self-contained feature folder.

---

## Layered Architecture

The codebase enforces a strict four-layer dependency order. Each layer has one responsibility and may only depend on the layer(s) below it.

```
HTTP Request
     │
     ▼
┌─────────────────────────────────────┐
│  Routes  (app/routes/)              │  HTTP boundary only.
│                                     │  Parses inputs, calls the service,
│                                     │  maps domain exceptions → HTTPException.
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  Services  (app/services/)          │  Business logic and orchestration.
│                                     │  Validates domain rules, calls external
│                                     │  systems, delegates writes to repositories,
│                                     │  raises domain exceptions only.
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  Repositories  (app/repositories/)  │  SQL access only.
│                                     │  Executes queries, maps DB constraint
│                                     │  violations to domain exceptions.
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│  Database  (app/database/)          │  Infrastructure.
│                                     │  Engine, session factory, ORM models,
│                                     │  migration scripts.
└─────────────────────────────────────┘

  +  app/harbor/    Cross-cutting singleton.
                    Injected into services only. Never referenced by routes
                    or repositories.
```

### Layer Boundary Rules

These rules are invariants — violating them breaks the ability to test and reason about each layer independently.

**Routes must not:**
- Contain business logic or validation beyond what Pydantic already enforces on the schema
- Call repositories directly, bypassing the service
- Import `AsyncSession`, `HarborClient`, or any infrastructure type directly

**Services must not:**
- Import `HTTPException` or any symbol from `fastapi`
- Build SQL queries or use `AsyncSession` directly (always delegate to a repository)
- Access the Harbor singleton via the module-level accessor — receive it as a constructor argument

**Repositories must not:**
- Import Harbor or any symbol from `fastapi`
- Contain business logic — no name validation, no version resolution, no decisions about what to write
- Import from `app/services/` or `app/routes/`

---

## Application Lifecycle

All external connections are owned by the FastAPI `lifespan` context manager in `app/main.py`. This is the only place that initialises and tears down module-level singletons.

```
Container starts
      │
      ▼
entrypoint.sh
      ├─► alembic upgrade head      (blocks; container exits on failure)
      └─► uvicorn app.main:app
               │
               ▼
         lifespan (startup)
               ├─► init_db(...)      → creates engine + session factory
               ├─► init_<client>(…)  → creates any other singletons
               └─► yield             ← app serves requests
               │
         lifespan (shutdown)
               ├─► close_<client>()
               └─► close_db()        → disposes engine, drains pool
```

### Singleton Pattern

Each external dependency follows the same three-function module API:

```python
async def init_<client>(...) -> None:  # called in lifespan startup
async def close_<client>() -> None:    # called in lifespan shutdown
def <client>() -> ClientType:          # accessor; raises RuntimeError if not initialised
```

The accessor is intentionally a plain function (not a property or global), so it can be wrapped by a FastAPI `Depends()` provider without special handling.

**Why module-level singletons, not class attributes?** FastAPI does not have a canonical application-state container, and module-level variables are simpler to test and reason about than `app.state`. The pattern trades a little purity for zero boilerplate at call sites.

---

## Dependency Injection

FastAPI's `Depends()` system wires the layers together per request. The general shape of the DI graph for any write operation is:

```
route handler
    └── Depends(get_<domain>_service)         [app/dependencies.py]
              ├── Depends(get_db_session)      [app/database/dependencies.py]
              │       └── get_session_factory() → engine singleton
              └── Depends(get_<external_client>)
                      └── <client>() accessor  → client singleton
```

All application-level provider functions live in `app/dependencies.py`. Database session provisioning lives in `app/database/dependencies.py` because it is infrastructure, not domain wiring.

### Transaction Boundary

`get_db_session` owns the transaction for each request:

```python
session = factory()
try:
    yield session       # route handler runs here
    await session.commit()
except Exception:
    await session.rollback()
    raise
finally:
    await session.close()
```

**Consequence:** Services and repositories never call `commit()` or `rollback()`. A repository can call `flush()` to materialise writes and detect constraint violations before the commit, but the decision to commit belongs exclusively to `get_db_session`. This makes the transaction boundary easy to find and impossible to accidentally bypass.

### The Stable Override Point

Every service provider in `app/dependencies.py` is the single override point for testing that service's routes. Swapping a mock in `app.dependency_overrides` replaces the entire service without involving the database or any external system:

```python
app.dependency_overrides[get_<domain>_service] = lambda: mock_service
```

---

## API Design Conventions

### URL Structure

Routes are namespaced by domain area under `/api/`:

```
/api/<domain-plural>/{identifier}/...
```

Sub-resources are nested one level deep. Avoid deeper nesting.

### HTTP Methods and Status Codes

| Operation | Method | Success code |
|-----------|--------|-------------|
| Create a resource | `POST` | `201 Created` |
| Read a resource or list | `GET` | `200 OK` |
| Full replace | `PUT` | `200 OK` |
| Partial update | `PATCH` | `200 OK` |
| Delete | `DELETE` | `204 No Content` |

### Request and Response Schemas

Each domain area has a dedicated file in `app/schemas/`. Request and response types are separate Pydantic models even when they look similar — they evolve independently.

Optional fields that the service can resolve from an external source (e.g. Harbor) follow the convention: accept `None` in the request body and fall back to the resolved value. This keeps the API ergonomic for callers that already have the value while avoiding a mandatory round-trip for those that don't.

### Health Endpoint

Every instance of this service exposes `GET /health`. It performs a lightweight database liveness probe (`SELECT 1`) and returns:
- `200 {"status": "healthy", ...}` when the DB is reachable
- `503 {"status": "unhealthy", ...}` with error detail when it is not

This endpoint is used by container orchestrators and load balancers. It must not require authentication and must not perform any writes.

---

## Error Handling

### Domain Exceptions

`app/exceptions.py` defines all application-specific exception types. They are:
- **HTTP-agnostic** — no status codes, no FastAPI imports
- **Carried with a `.detail` string** suitable for returning as an error message
- **Named after the domain condition**, not the HTTP outcome (e.g. `ArtifactNotFoundInHarborError`, not `NotFoundException`)

### Exception Flow

```
Repository  ──raises──►  domain exception (DB constraint violated)
                                │
Service     ──raises──►  domain exception (validation failed, external call failed)
                                │
Route       ──catches──► raises HTTPException(status_code=..., detail=exc.detail)
```

The route handler is the **only** place in the codebase that catches domain exceptions and converts them to `HTTPException`. This makes the mapping easy to audit in one place and keeps services and repositories fully decoupled from the HTTP layer.

### External Client Exceptions

Exceptions from external clients (e.g. `HarborAuthError`, `HarborConnectionError`) must be caught in the **service layer** and re-raised as domain exceptions before propagating further. Routes must never catch library-specific exceptions.

---

## Database Conventions

### Async-first

All database access is async via SQLAlchemy's async extension (`AsyncSession`, `create_async_engine`, `asyncpg` driver). Synchronous SQLAlchemy APIs must not be used in request handlers.

### ORM Models as Schema Source of Truth

`app/database/models.py` is the authoritative definition of the database schema. Alembic uses autogenerate against these models to produce migration scripts. The ORM models therefore must always reflect the current desired state of the schema.

**Note on naming:** When an ORM column name collides with a SQLAlchemy reserved name (e.g. `metadata`), use a trailing underscore for the Python attribute (`metadata_`) while keeping the actual column name unchanged via the `Column("metadata", ...)` declaration.

### Session Per Request

Each HTTP request receives exactly one `AsyncSession`. The session is never shared between requests or stored outside the request context. It is always closed in a `finally` block — see [Dependency Injection](#dependency-injection) for the full lifecycle.

### Repository Pattern

All SQL is written in repository classes, not in services or route handlers. A repository:
- Accepts a session in its constructor and stores it as `self._session`
- Contains no business logic — only query construction and execution
- Converts `IntegrityError` and similar database exceptions into domain exceptions before letting them propagate

### Migrations

Alembic manages all schema changes. **The container entry point always runs `alembic upgrade head` before starting the API server.** This means the schema is always up to date when the application starts and removes the need for any runtime schema bootstrapping.

New migrations are generated from ORM model changes:

```bash
python -m alembic revision --autogenerate -m "short description"
```

Always review the generated file before committing — autogenerate is not perfect and may miss certain changes (e.g. custom SQL types, server defaults). Migration filenames follow the pattern `NNNN_<description>.py` with zero-padded sequential IDs.

---

## Data Modelling Principles

See [`docs/schema.md`](schema.md) for full DDL, indexes, and example queries. The following principles govern how the schema is designed.

**Artifact names are globally unique.** A name identifies an artifact across the entire catalog and maps 1:1 to a Harbor repository path. There is no namespacing by category.

**Versions are immutable.** Once an artifact version is registered it is sealed. There is no `UPDATE` on version rows. This mirrors OCI content-addressable semantics — a digest uniquely identifies a manifest.

**`updated_at` only on the parent artifact row.** It is bumped whenever a new version is added or the artifact description changes. Version rows carry only `created_at`.

**JSONB for metadata, not wide tables.** Version-level attributes (training config, evaluation metrics, lineage) that vary by artifact type are stored in a single `metadata JSONB` column with a GIN index. This avoids nullable-column sprawl and keeps the schema stable as new artifact types are introduced. Structured fields can be promoted to first-class columns when they need FK constraints or are queried under a join.

**UUID primary keys.** All primary keys are `gen_random_uuid()` UUIDs. Sequential integers are avoided to prevent ID leakage and to ease future sharding or federation.

**`ON DELETE CASCADE` on version FK.** Deleting an artifact removes all its versions. An artifact without versions is meaningless.

**Lineage as soft references.** Lineage fields in metadata (e.g. `parent_model`, `source_dataset`) use `<name>:<version>` strings that reference other artifacts by name and version. These are not FK constraints — they are queryable via the GIN index, and the loose coupling avoids complex FK graphs across artifact types.

---

## Harbor Integration Pattern

### Singleton Lifecycle

The `app/harbor/` module owns the `HarborClient` singleton. It follows the [standard singleton pattern](#singleton-pattern) with `init_harbor()`, `close_harbor()`, and `harbor_client()` functions. The module also re-exports all types and exceptions from `harbor-oci-client` so that the rest of the codebase imports from `app.harbor` rather than directly from the library.

This indirection means the integration point is isolated to one module. If the library's import paths change, only `app/harbor/__init__.py` needs updating.

### Injection, Not Access

Services receive the `HarborClient` as a constructor argument. They must never call `harbor_client()` directly. This makes services trivially testable with a mock and prevents hidden coupling to the singleton lifecycle.

### Exception Translation

The service layer is responsible for translating library-specific Harbor exceptions into domain exceptions before they leave the service. Routes must never catch `HarborError` or its subclasses.

The translation convention:

| Library exception | Suggested domain exception |
|------------------|---------------------------|
| `ArtifactNotFoundError` | `ArtifactNotFoundInHarborError` |
| `HarborAuthError`, `HarborConnectionError`, `HarborAPIError` | `HarborVerificationError` |

For full Harbor protocol documentation, OCI media types, robot account details, and available client methods, see [`docs/harbor.md`](harbor.md).

---

## Configuration

All configuration is centralised in `app/config.py` as a `pydantic-settings` `BaseSettings` subclass. A module-level `settings = Settings()` singleton is imported wherever configuration is needed.

**Convention:** No code outside `app/config.py` reads environment variables directly (no bare `os.getenv` calls for application settings). All values go through `Settings` so they are validated, typed, and documented in one place.

Configuration is provided via environment variables in all environments. For local development, a `.env` file is loaded automatically by pydantic-settings. For production, values are injected by the container runtime or Kubernetes secrets. The `.env` file is gitignored — `.env.example` documents all required variables and their defaults.

Computed properties on `Settings` (e.g. `database_url`, `async_database_url`) derive structured values from raw fields. They live in `Settings` rather than scattered across the codebase.

---

## Infrastructure Conventions

### Container Image

The `Dockerfile` uses a multi-stage build:
1. **Builder stage** — installs dependencies into an isolated prefix
2. **Runtime stage** — copies only installed packages and source; no build tooling in the final image

The application runs as an unprivileged user (`appuser`, UID 1000). The image exposes a single port driven by the `PORT` environment variable.

### Startup Sequence

`entrypoint.sh` always runs migrations before starting the API server:

```bash
python -m alembic upgrade head   # exits non-zero on failure → container restart
exec uvicorn ...                  # starts only after schema is current
```

This means there is no separate migration job or init-container — the application is self-migrating on every deploy. The trade-off is a small startup delay; the benefit is that the schema is always correct when the server becomes ready.

### Local Development

`docker-compose.yaml` provides a self-contained local environment with the API service and a PostgreSQL instance. The API service waits for a passing `pg_isready` healthcheck before starting, which prevents race conditions during `alembic upgrade head`.

### CI/CD

Releases are published by tagging a GitHub Release. The CI workflow builds a `linux/amd64` image, pushes it to the internal Harbor registry, and tags it with the release version. Non-pre-release tags additionally receive a `latest` tag. Harbor credentials are stored as GitHub Actions secrets and never appear in configuration files.

---

## Testing Conventions

### No External Dependencies in Tests

Tests never connect to a real database or Harbor instance. All I/O boundaries are mocked. This keeps the test suite fast and deterministic.

### Two-Tier Unit Testing

Testing is split into two complementary layers that each test exactly one concern:

**Route tests** (`tests/test_routes_<domain>.py`) verify the HTTP-to-domain-exception mapping. They use `TestClient` with a minimal `FastAPI` app and override the service dependency with an `AsyncMock`. The only question these tests answer is: *given this domain exception, does the route return the correct HTTP status code?*

```python
app.dependency_overrides[get_<domain>_service] = lambda: mock_service
```

**Service tests** (`tests/test_services_<domain>.py`) verify business logic and orchestration. They construct the service class directly with mock constructor arguments (Harbor client, repository). No FastAPI machinery is involved. These tests answer: *given these inputs and dependency behaviours, does the service produce the correct output or raise the correct domain exception?*

### Fixture Conventions

Shared fixtures live in `tests/conftest.py`. Fixtures are kept minimal — they provide a typed object (e.g. a UUID, a request schema instance, a mock session) without configuring return values. Return values are set per-test so each test is self-documenting.

### Test Naming

Test functions are named `test_<condition>_<expected_outcome>`, e.g.:
- `test_harbor_not_found_returns_404`
- `test_version_already_exists_raises_conflict`
- `test_omitted_version_is_auto_incremented`

---

## Extending the Service

### Adding a New Domain Operation (endpoint)

Follow this checklist to stay consistent with the conventions above:

1. **`app/exceptions.py`** — add any new domain exception classes; keep them HTTP-agnostic with a `.detail` attribute
2. **`app/schemas/<domain>.py`** — add Pydantic request and response models as separate classes
3. **`app/repositories/<domain>.py`** — add repository methods; SQL only, no business logic; convert DB exceptions to domain exceptions
4. **`app/services/<domain>.py`** — add a service class; receive dependencies via constructor; raise domain exceptions only; never import from `fastapi`
5. **`app/dependencies.py`** — add a `get_<domain>_service` provider that wires `get_db_session` and any singleton clients into the service constructor
6. **`app/routes/<domain>.py`** — add route handlers; call the service; catch domain exceptions and raise `HTTPException`; register on an `APIRouter`
7. **`app/main.py`** — include the new router
8. **`tests/`** — add route-level tests (DI override) and service-level tests (constructor injection); do not skip either layer

### Adding a New Singleton Dependency

When a new external client needs app-level lifecycle, follow the singleton pattern already established by `app/harbor/`:

1. Create `app/<client>/__init__.py` with `init_<client>()`, `close_<client>()`, and `<client>()` accessor
2. Call `init_<client>()` in the `lifespan` startup block and `close_<client>()` in the shutdown block in `app/main.py`
3. Add a `get_<client>` provider in `app/dependencies.py`
4. Re-export all library types and exceptions from `app/<client>/` so the rest of the codebase never imports directly from the library

### Adding a Database Migration

1. Modify the ORM models in `app/database/models.py` to reflect the desired schema state
2. Generate a migration: `python -m alembic revision --autogenerate -m "short description"`
3. Review the generated file — verify it is correct and complete
4. Apply locally: `python -m alembic upgrade head`

The migration will run automatically on the next container startup via `entrypoint.sh`.
