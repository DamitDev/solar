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

PostgreSQL-t külön kell futtatni (pl. `docker compose up postgres -d`), utána:

```bash
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

## Projekt struktúra

```
app/
├── main.py            # FastAPI app, lifespan
├── config.py          # Settings (pydantic-settings)
├── database/
│   ├── connection.py  # asyncpg connection pool
│   └── schema.py      # placeholder (D-005)
├── harbor/
│   └── __init__.py    # placeholder (D-006)
└── routes/
    └── health.py      # GET /health
```

---

## Dokumentáció

- **[docs/schema.md](docs/schema.md)** — PostgreSQL metadata schema, JSONB konvenciók, example query-k
- **[docs/harbor.md](docs/harbor.md)** — Harbor integráció, OCI media type-ok, auth flow, push/pull
- **[poc/findings.md](poc/findings.md)** — ORAS Python library értékelés (D-003)

---

## Roadmap

- **D-005** — Schema provisioning (Alembic initial migration)
- **D-006** — Harbor API client integráció
- **D-007+** — API route-ok (registration, catalog, search, URI resolution)
