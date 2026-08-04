# Development Guide

Rövid útmutató azoknak, akik a monorepóban dolgoznak.

## uv alapok (conda-soknak)

A repo `uv`-t használ a Python függőségekhez. A legfontosabb parancsok:

```bash
uv sync              # lockfile alapján szinkronizálja a root .venv-t (workspace tagok)
uv run <cmd>         # parancs futtatása a workspace venv-jében
uv add <pkg>         # függőség hozzáadása az aktuális projekt pyproject-jéhez
uv lock              # lockfile újragenerálás
```

A workspace tagok (`apps/solar-control`, `apps/data-repository`) egy közös `.venv`-t
használnak a repo gyökerében. A `solar-host` és a `supernova-steps` saját venv-vel
dolgozik:

```bash
cd apps/solar-host && uv sync          # saját .venv (host)
cd apps/supernova-steps && uv sync     # saját .venv (steps, virtuális projekt)
```

## Quality gate-ek

Minden PR-nak zölden kell átmennie a saját app-ja quality gate-jén:

- **Python appok**: `ruff check` + `black --check` + `pytest` (a `make lint-<app>` /
  `make test-<app>` targetekkel).
- **solar-webui**: `eslint` + `prettier --check` (a `pnpm --filter solar-webui lint`
  script).
- **solar-control** érintő változásoknál a cross-service integration suite is fut
  (`make integration`, CI-ben az `integration-tests.yaml`).

## Konvenciók

- Felhasználó felé néző dokumentáció magyarul, kivéve a kódkommenteket és a
  fejlesztői (AGENTS) dokumentációt.
- Conventional commits; PR template a `.github/PULL_REQUEST_TEMPLATE.md`.
- A Docker image nevek és Harbor projektek **nem változnak** a régi repo-khoz képest
  (`aiops/solar-control`, `aiops/solar-webui`, `aiops/data-repository`,
  `supernova/steps/*`) — az aiops-k8s értékfájlok ettől függetlenek maradnak.
