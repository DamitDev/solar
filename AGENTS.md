# Solar Monorepo — AGENTS.md

A közös repo a Solar rendszer minden komponensének. Ez a fájl a fejlesztői
ágenseknek (és embereknek) szóló konvenciókat írja le.

## Struktúra

- `apps/solar-control` — FastAPI control plane. Workspace tag (root `.venv`).
- `apps/data-repository` — FastAPI registry. Workspace tag (root `.venv`).
- `apps/solar-host` — FastAPI host agent. **Nem** workspace tag: saját venv,
  PyPI-n publikált csomag.
- `apps/solar-webui` — React/Vite frontend. pnpm workspace tag.
- `apps/supernova-steps` — Supernova lépésképek. **Nem** workspace tag: virtuális
  uv projekt (lint/test), Docker kép requirements.txt-ből épül.
- `training-platform-project` — tervezés (issues, specs, ROADMAP). Kód nincs benne.

## Parancsok

```bash
make install          # pnpm install + uv sync (root + host + steps)
make dev-control      # http://localhost:8000
make dev-host         # http://localhost:8001
make dev-data-repository  # http://localhost:8002
make dev-webui
make test             # minden unit teszt + webui lint
make lint             # ruff check minden Python appon + webui lint
make format           # black minden Python appon
make integration      # solar-control cross-service suite (Docker kell)
```

App-szintű targetek: `make test-<app>` / `make lint-<app>`, ahol az app neve:
`solar-control`, `solar-host`, `solar-webui`, `data-repository`, `supernova-steps`.
Ezeket hívja a CI is.

## Konvenciók

- Conventional commits (`feat:`, `fix:`, `chore:`, `refactor:`, `docs:`).
- PR template: `.github/PULL_REQUEST_TEMPLATE.md` — title + fenced `## Description`,
  `## Changes`, opcionális `## Related Issues`.
- Felhasználó felé néző README/dokumentáció magyarul; AGENTS és kódkommentek angolul.
- A Docker image nevek rögzítettek (`aiops/*`, `supernova/steps/*`) — ne változtasd
  meg őket, az aiops-k8s azokra hivatkozik.
- A training-platform-project issue-k az architektúra forrásai; új funkciónál előbb
  ott nézz körül (S-xxx/D-xxx/N-xxx), mint kódot írnál.

## CI

- `ci.yaml` — dinamikus dispatcher: a változott útvonalak alapján hívja a
  `quality-gates.yaml`-t (unit tesztek + ruff + black check apponként).
- `integration-tests.yaml` — solar-control cross-service suite (solar-control,
  solar-host, data-repository path filter).
- `test-images.yaml` / `release.yaml` — `.github/release-manifest.json` alapján.
- `publish-host.yaml` — solar-host PyPI publish (OIDC, `release` environment).

Ha új appot adsz hozzá: `pnpm-workspace.yaml` / root `pyproject.toml` members,
`Makefile` targetek, `ci.yaml` dispatcher case, szükség esetén
`release-manifest.json`.
