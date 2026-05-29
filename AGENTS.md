# SuperNova - Agent Reference

## Project Overview

SuperNova is a job-based AI training platform that orchestrates the full model lifecycle: data in → training → model out → deployed. It integrates with the existing Solar System (inference layer) and runs on on-prem GPU infrastructure. SuperNova is the brain (Slurm-like orchestrator), Solar is the muscle (execution layer).

## Architecture

| Component | Role | Tech Stack |
|-----------|------|------------|
| **SuperNova Control API** | Job orchestration, scheduling, deployment strategy | FastAPI, PostgreSQL |
| **SuperNova WebUI** | Training job submission, monitoring, catalog browsing | React, TypeScript, Vite, Tailwind |
| **Data Repository** | Centralized model & dataset storage with metadata and versioning | FastAPI, Harbor/ORAS, PostgreSQL |
| **Solar Control API** | Stateless execution layer — host management, model distribution, job step execution, inference routing | Python (FastAPI), PostgreSQL, Redis, Socket.IO |
| **Solar Host** | Process manager for inference instances and job step containers, resource reporting | Python (FastAPI) |
| **Solar WebUI** | Operations monitoring, declarative instance management | React, TypeScript, Vite, Tailwind |

Communication topology: SuperNova → Solar Control API → Solar Hosts. SuperNova never talks directly to Solar Hosts.

## Repositories

| Repo | Path | Description |
|------|------|-------------|
| `training-platform-project` | `/mnt/nvme/AI/damit-aiops/training-platform-project` | This repo — project management, issues, roadmap |
| `solar-host` | `/mnt/nvme/AI/solar/solar-host` | Solar Host agent (Python/FastAPI) |
| `solar-control` | `/mnt/nvme/AI/solar/solar-control` | Solar Control API (Python/FastAPI) |
| `solar-webui` | `/mnt/nvme/AI/solar/solar-webui` | Solar WebUI (React) |
| `data-repository` | `/mnt/nvme/AI/damit-aiops/data-repository` | Data Repository API (FastAPI) |
| `supernova-control` | `/mnt/nvme/AI/damit-aiops/supernova-control` | SuperNova Control API (FastAPI) |
| `supernova-webui` | `/mnt/nvme/AI/damit-aiops/supernova-webui` | SuperNova WebUI (React) |
| `supernova-steps` | `/mnt/nvme/AI/damit-aiops/supernova-steps` | Step Docker images (download, upload, convert) |
| `harbor-oci-client` | `/mnt/nvme/AI/damit-aiops/harbor-oci-client` | Shared Python library for Harbor OCI/ORAS operations ([PyPI](https://pypi.org/project/harbor-oci-client/), [GitHub](https://github.com/DamitDev/harbor-oci-client)) |
| `etalon` | `/mnt/nvme/AI/etalon` | Training framework (black box — do not modify) |
| `aiops-k8s` | `/mnt/nvme/AI/damit-aiops/aiops-k8s` | GitOps repo (ArgoCD App-of-Apps) |

## Infrastructure

| Host | IP | Hardware | Roles |
|------|----|----------|-------|
| damcpmacstudio01 | 172.16.240.8 | Mac Studio M3 Ultra, 512 GB RAM | inference |
| damcpmacstudio02 | 172.16.240.9 | Mac Studio M3 Ultra, 256 GB RAM | inference |
| damcpaiops01 | 172.16.240.5 | Nvidia RTX 4090, 24 GB VRAM | inference, training |
| damcpaiops02 | 172.16.240.6 | Nvidia RTX 4090, 24 GB VRAM | inference, training |
| damclk8saiops01-03 | 172.16.252.41-43 | K8s cluster | business logic |

- **Harbor**: `imgrepo.damit.hu` — Docker images, Helm charts, OCI artifacts (models & datasets under `supernova/` project)
- **K8s namespace**: `solar` on `damit-prod` cluster
- **PostgreSQL**: shared cluster, used by Solar Control and SuperNova services
- **Redis**: used by Solar Control for shared state and Socket.IO cross-replica broadcasting
- **Inter-service auth**: API keys

## Workflow Rules

- Always understand the issue and its goal before starting. Ask the supervisor for clarification if anything is unclear.
- Do not make architectural decisions on your own. If you reach a decision point, ask the supervisor. You may propose solutions, but defer to the supervisor's preference.
- Avoid assumptions. Verify that both your ideas and the supervisor's align with best practices and standards.
- Work in sync with the supervisor: stop between steps for review and verification.
- Dynamically adjust the plan if necessary, but only after consulting the supervisor.
- Verify consistency, run linting/formatting checks per the repo's rules, and run tests if applicable.
- Update repo documentation if your changes affect it.
- Never commit or push directly — the supervisor handles git operations.

## Branching Convention

- Create a feature branch from `master` named after the issue ID: `feature/{issue-id}` (e.g. `feature/S-014`, `feature/D-007`).
- If an issue touches multiple repos, create the same-named branch in each repo.
- One branch per issue. Do not mix unrelated changes.

## PR Format

When the implementation is complete, provide a PR title and description for the supervisor using this format.

**PR title (Conventional Commits).** Prefer a short [Conventional Commits](https://www.conventionalcommits.org/) style title so history stays scannable, for example `feat(api): add catalog list endpoints`, `fix(db): correct migration order`, or `docs: update schema for D-013`. Use types such as `feat`, `fix`, `docs`, `chore`, `refactor`, or `test` as appropriate. The supervisor may still adjust wording.

Body format:

```
PR Title
```
---
```
## Description
<short description of the PR, what it does, and why>

## Changes
<list of changes>

<optional>## Reproduction Steps
<steps to reproduce if bug fix>

## Related Issues
Fixes/Resolves/Closes #<issue-id>
```

## Key Architectural Constraints

- **SuperNova orchestrates, Solar executes.** SuperNova submits intents/jobs to Solar Control. Solar handles host-level resource management autonomously.
- **Etalon is a black box.** Training containers get input volumes mounted and produce output volumes. The platform never modifies Etalon internals.
- **Step-based pipelines.** Training jobs are sequences of Docker containers (`download_model`, `download_dataset`, `train`, `convert_model`, `upload_model`). Each step is a separate image.
- **One replica per host.** Never run the same model twice on the same hardware.
- **Declarative deployment.** Inference deployments are declared as intents (deployed alias, model source URI, replica count, priority, strategy, placement) submitted to Solar Control, which reconciles instances across hosts. Clients declare desired state and never target a host directly. See [docs/specs/deployment-intent.md](docs/specs/deployment-intent.md).
- **No external experiment tracking.** SuperNova parses console logs and checkpoint eval metrics directly. No W&B or MLflow dependency.
- **Model source URIs.** Models are referenced via `repo://`, `huggingface://`, or `local://` — not raw filesystem paths.
- **Harbor for blobs, Postgres for metadata.** Data Repository uses ORAS to push/pull OCI artifacts to Harbor, with PostgreSQL for metadata and search.
- **Shared Harbor client.** All Python services that interact with Harbor (Data Repository, Solar Control, step containers) use the `harbor-oci-client` library (`pip install harbor-oci-client`). It provides async `HarborClient` (verify, delete, info), sync `OrasHelper` (push, pull), typed exceptions, and SuperNova media type constants. Do not duplicate Harbor/ORAS logic in individual repos.

## References

For deeper context when needed:

- **Full architecture & design**: [README.md](README.md)
- **Roadmap, phases & issue dependencies**: [ROADMAP.md](ROADMAP.md)
- **Implementation workflow details**: [workflows/issue-implementation.md](workflows/issue-implementation.md)
- **Specifications** ([docs/specs/](docs/specs/)):
  - [Model Source URI](docs/specs/model-source-uri.md) — `repo://`/`huggingface://`/`local://` resolution, caching, and distribution
  - [Job Step Workspace](docs/specs/job-step-workspace.md) — training step workspace contract and inter-step communication
  - [Declarative Deployment Intent](docs/specs/deployment-intent.md) — desired-state inference deployment API, reconciliation, and strategies (S-039)
