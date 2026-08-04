.PHONY: install dev-control dev-host dev-data-repository dev-webui test lint format integration test-solar-control test-data-repository test-solar-host test-supernova-steps test-solar-webui lint-solar-control lint-data-repository lint-solar-host lint-supernova-steps lint-solar-webui

install:
	pnpm install && uv sync && cd apps/solar-host && uv sync && cd ../supernova-steps && uv sync

dev-control:
	cd apps/solar-control && uv run uvicorn app.main:sio_asgi_app --reload --port 8000

dev-host:
	cd apps/solar-host && uv run uvicorn solar_host.main:app --reload --port 8001

dev-data-repository:
	cd apps/data-repository && uv run uvicorn app.main:app --reload --port 8002

dev-webui:
	pnpm --filter solar-webui dev

# ── Per-app targets (used by CI quality gates) ──────────────────────
# Target names must match the CI matrix app names (apps/<name>).

test-solar-control:
	cd apps/solar-control && uv run pytest -q

test-data-repository:
	cd apps/data-repository && uv run pytest -q

test-solar-host:
	cd apps/solar-host && uv run pytest -q

test-supernova-steps:
	cd apps/supernova-steps && uv run pytest -q

test-solar-webui:
	pnpm --filter solar-webui lint && pnpm --filter solar-webui test

lint-solar-control:
	cd apps/solar-control && uv run ruff check . && uv run black --check .

lint-data-repository:
	cd apps/data-repository && uv run ruff check . && uv run black --check .

lint-solar-host:
	cd apps/solar-host && uv run ruff check . && uv run black --check .

lint-supernova-steps:
	cd apps/supernova-steps && uv run ruff check . && uv run black --check .

lint-solar-webui:
	pnpm --filter solar-webui lint

# ── Aggregates ──────────────────────────────────────────────────────

test:
	cd apps/solar-control && uv run pytest -x -q && cd ../../apps/data-repository && uv run pytest -x -q && cd ../../apps/solar-host && uv run pytest -x -q && cd ../../apps/supernova-steps && uv run pytest -q && pnpm --filter solar-webui lint

lint:
	cd apps/solar-control && uv run ruff check . && cd ../../apps/data-repository && uv run ruff check . && cd ../../apps/solar-host && uv run ruff check . && cd ../../apps/supernova-steps && uv run ruff check . && pnpm --filter solar-webui lint

format:
	cd apps/solar-control && uv run black . && cd ../../apps/data-repository && uv run black . && cd ../../apps/solar-host && uv run black . && cd ../../apps/supernova-steps && uv run black .

# Dockerized cross-service suite (solar-control + solar-host + data-repository).
# Requires Docker; syncs the host venv with the huggingface extra first (torch).
integration:
	cd apps/solar-host && uv sync --extra huggingface
	cd apps/solar-control && uv run --extra integration pytest tests_integration/ -v --tb=short

# Regenerate the Docker requirements.txt files from the uv lockfile.
# Run after dependabot bumps or manual dependency changes.
export-requirements:
	uv export --frozen --no-dev --project apps/solar-control -o apps/solar-control/requirements.txt
	uv export --frozen --no-dev --project apps/data-repository -o apps/data-repository/requirements.txt
