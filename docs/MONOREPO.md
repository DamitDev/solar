# Solar Monorepo

A közös ház a Solar rendszer összes komponensének: a control plane, a host agent, a webui,
a Data Repository, a Supernova lépésképek és a teljes tervezési dokumentáció. Egy branch,
egy CI, ugyanaz a verziókezelés.

## Layout

```
solar/
├── apps/
│   ├── solar-control/      # FastAPI control plane (multi-tenant OpenAI gateway, reconciler)
│   ├── solar-host/         # FastAPI host agent (model inference backend process manager)
│   ├── solar-webui/        # React/TypeScript frontend (Vite + express middleware server)
│   ├── data-repository/    # FastAPI modell/dataset registry (Harbor OCI artifact metadata)
│   └── supernova-steps/    # Supernova pipeline step képek (download-model, download-dataset, upload-model)
├── training-platform-project/  # Tervezés: issues/, docs/specs/, ROADMAP.md (S-xxx / D-xxx / N-xxx)
├── docs/                   # Monorepo-szintű dokumentáció (ez a fájl, DEVELOPMENT.md, RELEASING.md)
├── .mise.toml              # Toolchain pin (python 3.12, node 24 LTS, pnpm, uv)
├── pnpm-workspace.yaml     # JS workspace root (apps/solar-webui)
├── pyproject.toml          # Python (uv) workspace root
└── package.json            # Root package (privát, shortcut scriptek)
```

## Előfeltételek

A toolchain [mise](https://mise.jdx.dev/)-en keresztül van pin-elve:

```bash
mise install          # python 3.12, node 24, pnpm, uv
pnpm install          # JS deps (solar-webui)
uv sync               # Python deps (solar-control + data-repository)
```

Vagy egyszerre: `make install` (a solar-host és supernova-steps saját venv-jét is
létrehozza — lásd alább).

## Lokális futtatás

```bash
make dev-control      # control plane  → http://localhost:8000
make dev-host         # host agent     → http://localhost:8001
make dev-data-repository  # data repo → http://localhost:8002
make dev-webui        # webui dev szerver (Vite)
```

A Docker Compose-ok (apps/*/docker-compose*.yml) továbbra is az adott app saját
könyvtárából működnek.

## Toolchain határok

- **uv workspace**: `apps/solar-control` + `apps/data-repository` osztozik a root
  `.venv`-n. A `solar-host` **nem** tag: külön PyPI csomag, nehéz extra függőségekkel
  (torch/transformers), saját venv + lockfile. A `supernova-steps` szintén nem tag:
  a lépésképek saját requirements.txt-ből épülnek Dockerben; a gyökér pyproject-je
  virtuális projekt (lint/test célra).
- **JS**: pnpm workspace, egyetlen tag: `apps/solar-webui`.
- **Verzió pin**: `.mise.toml` — python 3.12, node 24 (LTS), pnpm latest, uv latest.
- **Lint/Format**: `.pre-commit-config.yaml` — ruff + ruff-format + black a Pythonra,
  eslint + prettier a webui-ra.

## Tesztek

```bash
# Unit tesztek apponként (CI quality gate is ezt hívja)
make test-control
make test-data-repository
make test-solar-host
make test-supernova-steps
make test-solar-webui    # eslint + prettier check

# Minden egyben
make test
```

A solar-control teljes cross-service integration suite-ja (testcontainers + valódi
service stack: data-repository + control + 2× host) Docker-t igényel:

```bash
make integration
```

CI-ben ez külön workflow: `.github/workflows/integration-tests.yaml`.

## CI/CD

| Workflow | Trigger | Mit csinál |
|---|---|---|
| `ci.yaml` | push/PR (`main`) | Dinamikus dispatcher: a változott fájlok alapján mátrixot épít, és a `quality-gates.yaml`-t hívja apponként (unit tesztek + ruff + black check) |
| `integration-tests.yaml` | push/PR (`main`, solar-control/host/data-repository path filter) | Dockerizált cross-service suite |
| `test-images.yaml` | `workflow_dispatch` (`image_tag` + komponensek) | Test image-ek build + push Harborba (`test-*` tag) |
| `release.yaml` | `workflow_dispatch` (`version` + komponensek) | Unified Docker build matrix → Harbor (`.github/release-manifest.json`) |
| `publish-host.yaml` | `workflow_dispatch` (`version`) | solar-host wheel build + PyPI OIDC publish |

Teljes release folyamat: [`RELEASING.md`](RELEASING.md).

## Új komponens hozzáadása

1. Hozd létre a könyvtárat `apps/` alá.
2. Python: vedd fel a root `pyproject.toml` `[tool.uv.workspace] members` listájába —
   kivéve, ha indokolt a standalone venv (mint solar-host / supernova-steps).
3. JS: add hozzá a `pnpm-workspace.yaml`-hoz.
4. Írj `AGENTS.md`-t a subtree-be, ha komoly méretű.
5. Vedd fel a `.github/release-manifest.json`-ba (image esetén) — a unified
   `release.yaml` és a `test-images.yaml` automatikusan viszi.
6. A dinamikus CI a `ci.yaml` dispatcher case-éhez add hozzá az app elérési útját,
   a `Makefile`-ba pedig a `test-<app>` / `lint-<app>` targeteket.

## Cross-package változások

Minden egy repóban van → a contract változások (API route, WS event, env var)
ugyanabban a PR-ben mennek minden fogyasztónak. **Nincs sibling PR.**

A training-platform-project issue spec-jei tartalmazzák az architektúra döntéseket
(S-xxx), a D-xxx issue-k a Data Repository/Solar API-kat, az N-xxx issue-k a
Supernova oldalt — a monorepo `apps/` struktúrájára hivatkoznak, ne régi elérési
utakra.
