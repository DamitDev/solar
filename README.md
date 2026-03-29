# Data Repository

Metadata katalógus OCI artifactokhoz (modellek és datasetek), amelyeket a Harbor-ban tárolunk ORAS-on keresztül. Registration, catalog, search és URI resolution endpointokat biztosít. A Data Repository **nem** proxy-zza a blob adatokat — a consumerek közvetlenül push/pull-olnak Harbor-ba/ból.

## Quick Start

### 1. Környezet előkészítése

```bash
cp .env.example .env
nano .env  # Szerkeszd a szükséges értékeket (DB credentials, Harbor credentials)
```

### 2. Docker Compose indítása

```bash
docker compose up -d
```

### 3. API elérhető

- **API**: `http://localhost:8000`
- **Swagger UI**: `http://localhost:8000/docs`
- **Health Check**: `http://localhost:8000/health`

---

## Requirements

### Futtatáshoz

- Docker & Docker Compose

### Fejlesztéshez

- Python 3.12+
- PostgreSQL 16+
- Harbor hozzáférés: `imgrepo.damit.hu` (D-006-tól)

---

## Lokális fejlesztés (Docker nélkül)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

PostgreSQL-t külön kell futtatni (pl. `docker compose up postgres -d`), utána migráció és indítás:

```bash
./migrate.sh
uvicorn app.main:app --reload
```

---

## Környezeti változók

Az `.env.example` fájl tartalmazza az összes beállítást. Másolás és szerkesztés után `.env`-ként használatos.

| Változó | Default | Leírás |
|---------|---------|--------|
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `data_repository` | Database név |
| `POSTGRES_USER` | `datarepo` | Database user |
| `POSTGRES_PASSWORD` | `datarepo` | Database jelszó |
| `HARBOR_URL` | `https://imgrepo.damit.hu` | Harbor registry URL |
| `HARBOR_USERNAME` | (üres) | Harbor robot account username |
| `HARBOR_PASSWORD` | (üres) | Harbor robot account password |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port |
| `LOG_LEVEL` | `INFO` | Logging szint |

---

## Migrációk

Az adatbázis séma kezelése [Alembic](https://alembic.sqlalchemy.org/) segítségével történik. A migráció szkriptek az `app/database/migrations/versions/` mappában vannak.

### Migráció futtatása (lokális fejlesztés)

```bash
./migrate.sh
```

Ez betölti a `.env` fájlból a környezeti változókat és futtatja az `alembic upgrade head` parancsot.

Manuálisan is futtatható:

```bash
source .env
python -m alembic upgrade head
```

### Új migráció létrehozása

```bash
source .env
python -m alembic revision --autogenerate -m "leírás"
```

### Migráció állapot ellenőrzése

```bash
source .env
python -m alembic current   # aktuális revízió
python -m alembic history   # migráció történet
```

### Konténerben

A `docker compose up` automatikusan futtatja a migrációkat az `entrypoint.sh`-n keresztül, mielőtt az API elindul.

---

## Projekt struktúra

```
├── Dockerfile
├── alembic.ini                  # Alembic konfiguráció
├── docker-compose.yaml
├── entrypoint.sh                # Konténer indítás (migráció + API)
├── migrate.sh                   # Lokális migráció script
├── requirements.txt
├── requirements-dev.txt
├── app/
│   ├── main.py                  # FastAPI app, lifespan
│   ├── config.py                # Settings (pydantic-settings)
│   ├── dependencies.py          # App-szintű DI providerek
│   ├── exceptions.py            # Saját kivételek
│   ├── database/
│   │   ├── connection.py        # SQLAlchemy async connection pool
│   │   ├── dependencies.py      # get_db_session provider
│   │   ├── models.py            # SQLAlchemy ORM modellek (Alembic-hez)
│   │   └── migrations/          # Alembic migrációk
│   │       ├── env.py
│   │       ├── script.py.mako
│   │       └── versions/
│   ├── harbor/
│   │   └── __init__.py          # HarborClient
│   ├── repositories/
│   │   └── models.py            # ModelArtifactRepository
│   ├── routes/
│   │   ├── health.py            # GET /health
│   │   └── models.py            # /api/models/* route-ok
│   ├── schemas/
│   │   └── models.py            # Pydantic request/response sémák
│   └── services/
│       └── models.py            # ModelRegistrationService
├── docs/
│   ├── harbor.md
│   └── schema.md
├── poc/
│   ├── findings.md
│   └── oras_poc.py
└── tests/
    ├── conftest.py
    ├── test_routes_models.py
    └── test_services_models.py
```

---

## Dependency Injection architektúra

A service réteg konstruktor-injekciós mintát követ: minden komponens explicit kapja meg a függőségeit, nem importál globális singletonokat.

### Providerek

Az app-szintű providerek az `app/dependencies.py`-ban, az adatbázis-provider az `app/database/dependencies.py`-ban találhatók.

| Provider | Visszatérési érték | Megjegyzés |
|---|---|---|
| `get_db_session` | `AsyncSession` | Per-request session; sikeres válasz esetén automatikus commit, kivétel esetén rollback |
| `get_harbor_client` | `HarborClient` | App-szintű singleton; `RuntimeError`-t dob, ha a lifespan inicializálása előtt hívják |
| `get_model_registration_service` | `ModelRegistrationService` | Per-request példány, a fenti kettőből összerakva |

### Service felépítési szabály

A `ModelRegistrationService(harbor=…, session=…)` belül hozza létre a `ModelArtifactRepository`-t a session alapján.  
Az egyértelmű szabály: **session érkezik → repository belül épül fel**; a hívó fél soha nem példányosítja a repository-t közvetlenül.

### Használat route-okban

```python
from app.dependencies import get_model_registration_service
from app.services.models import ModelRegistrationService
from typing import Annotated
from fastapi import Depends

@router.post("/api/models/{name}/versions", status_code=201)
async def register_model_version(
    name: str,
    request: RegisterModelVersionRequest,
    service: Annotated[ModelRegistrationService, Depends(get_model_registration_service)],
):
    result = await service.register_model_version(name, request)
    ...
```

### Tesztelési minta

Tesztekben a legfelső szintű dependency felülírható — nem szükséges belső szimbólumokat patchelni:

```python
app.dependency_overrides[get_model_registration_service] = lambda: mock_service
```

Service unit teszteknél a mockokat közvetlenül a konstruktoron keresztül lehet injektálni, a `ModelArtifactRepository`-t pedig az `app.services.models.ModelArtifactRepository` útvonalon kell patchelni:

```python
with patch("app.services.models.ModelArtifactRepository", return_value=mock_repo):
    svc = ModelRegistrationService(harbor=mock_harbor, session=AsyncMock())
    result = await svc.register_model_version(name, request)
```

---

## Tesztelés

### Eszközök

| Csomag | Verzió | Szerepe |
|---|---|---|
| `pytest` | 9.0.2 | Teszt runner |
| `pytest-asyncio` | 1.3.0 | Async tesztfüggvények támogatása |
| `unittest.mock` | stdlib | `AsyncMock`, `MagicMock`, `patch` |

Telepítés:

```bash
pip install -r requirements-dev.txt
```

### Tesztek futtatása

```bash
pytest
```

### Tesztszintek

A tesztek két szinten szerveződnek, mindkettő valódi adatbázis és Harbor példány nélkül fut.

#### Route tesztek (`tests/test_routes_models.py`)

A FastAPI `TestClient`-et használják, szinkron módban. A `get_model_registration_service` dependency-t `app.dependency_overrides`-on keresztül cserélik le egy kontrollált mock-ra — így a HTTP státuszkód-leképezés minden doméin kivételre ellenőrizhető infrastruktúra nélkül:

```python
app.dependency_overrides[get_model_registration_service] = lambda: mock_service
```

A `TestClient`-et `raise_server_exceptions=False`-szal hozzák létre, hogy a válasz státuszkódját lehessen assertálni a kivétel helyett. Az érintett esetek:

| Kivétel | HTTP státusz |
|---|---|
| – (sikeres) | 201 |
| `InvalidArtifactNameError` | 422 |
| `ArtifactNotFoundInHarborError` | 404 |
| `HarborVerificationError` | 502 |
| `ArtifactCategoryConflictError` | 409 |
| `VersionAlreadyExistsError` | 409 |
| Pydantic validációs hiba | 422 |

#### Service unit tesztek (`tests/test_services_models.py`)

Minden tesztfüggvény aszinkron (`pytestmark = pytest.mark.asyncio`). A `ModelArtifactRepository`-t az `app.services.models.ModelArtifactRepository` útvonalon patchelik, így a service belső repo-példányosítása a mock-ra cserélődik. A `HarborClient` közvetlenül a konstruktorba kerül.

A tesztek egy `_svc()` aszinkron context manager helpert használnak, amely egységes módon állítja fel a service-t és a szükséges mock-okat:

```python
async with _svc(existing_versions=["v1", "v2"]) as (svc, mock_harbor, mock_repo):
    result = await svc.register_model_version("mymodel", request)
```

Az érintett területek:

- **Névvalidáció** — érvénytelen és érvényes nevek `@pytest.mark.parametrize`-zal
- **Harbor hibaleképezés** — `ArtifactNotFoundError` → `ArtifactNotFoundInHarborError`; `HarborAuthError` / `HarborConnectionError` / `HarborAPIError` → `HarborVerificationError`
- **Auto-verziózás** — első verzió `v1`, növekményes számozás, nem folytonos sorozat kezelése, explicit verzió átadása
- **Digest/checksum feloldás** — request-beli checksum felülírja a Harbor-tól érkezőt; hiány esetén fallback Harbor digestre
- **Doméin kivételek propagálása** — `ArtifactCategoryConflictError`, `VersionAlreadyExistsError`
- **Válasz alakja** — `name`, `version`, `harbor_ref`, `category` mezők ellenőrzése

### Megosztott fixture-ök (`tests/conftest.py`)

| Fixture | Típus | Leírás |
|---|---|---|
| `artifact_id` | `uuid.UUID` | Fix UUID teszteléshez |
| `mock_session` | `AsyncMock` | Minimális `AsyncSession` mock |
| `basic_request` | `RegisterModelVersionRequest` | Alap regisztrációs kérés |

---

## Dokumentáció

- **[docs/schema.md](docs/schema.md)** — PostgreSQL metadata schema, JSONB konvenciók, example query-k
- **[docs/harbor.md](docs/harbor.md)** — Harbor integráció, OCI media type-ok, auth flow, push/pull
- **[poc/findings.md](poc/findings.md)** — ORAS Python library értékelés (D-003)

---

## Roadmap

- ~~**D-005** — Schema provisioning (Alembic initial migration)~~
- **D-006** — Harbor API client integráció
- **D-007+** — API route-ok (registration, catalog, search, URI resolution)
