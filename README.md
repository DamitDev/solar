# Solar

A Solar rendszer monorepója: control plane, host agentek, webui, Data Repository és
a Supernova pipeline lépésképei — egy repóban.

## Komponensek

| App | Leírás | Deploy |
|---|---|---|
| `apps/solar-control` | Stateless koordinátor, multi-tenant OpenAI-kompatibilis gateway | Harbor: `aiops/solar-control` |
| `apps/solar-host` | Model inference backend process manager (llama.cpp, HF) | PyPI: `solar-host` |
| `apps/solar-webui` | React/TypeScript dashboard | Harbor: `aiops/solar-webui` |
| `apps/data-repository` | Modell/dataset registry (Harbor OCI metadata) | Harbor: `aiops/data-repository` |
| `apps/supernova-steps` | Supernova lépésképek (download/upload) | Harbor: `supernova/steps/*` |
| `training-platform-project` | Tervezés: issues, specs, ROADMAP | — |

## Telepítés

```bash
mise install     # python 3.12, node 24, pnpm, uv
make install     # pnpm install + uv sync (root + host + steps)
```

## Gyors indítás

```bash
make dev-control          # http://localhost:8000
make dev-webui            # Vite dev szerver
make test                 # minden teszt
make integration          # cross-service suite (Docker kell)
```

Részletek: [`docs/MONOREPO.md`](docs/MONOREPO.md), [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md),
[`docs/RELEASING.md`](docs/RELEASING.md).
