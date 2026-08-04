# Releasing

A Solar monorepo egyetlen release folyamatot használ: a unified `release.yaml`
workflow-t, a `.github/release-manifest.json` manifesttel. Ez leváltja a régi
repo-k egyedi release workflow-jait.

## Harbor image release

1. GitHub → Actions → **Release** → Run workflow.
2. `version`: egyetlen verzió az összes kiválasztott komponensnek (pl. `v0.7.0`).
3. `components`: komponensek és/vagy presetek szóközzel/vesszővel elválasztva.
   - Presetek: `all`, `core` (solar-control, solar-webui, data-repository), `steps`
     (download-model, download-dataset, upload-model).
   - Komponensek: `solar-control`, `solar-webui`, `data-repository`,
     `download-model`, `download-dataset`, `upload-model`.

A workflow a manifest alapján build mátrixot épít, és minden image-et a saját
Harbor projektjébe pushol:

| Komponens | Image |
|---|---|
| solar-control | `imgrepo.damit.hu/aiops/solar-control:<version>` |
| solar-webui | `imgrepo.damit.hu/aiops/solar-webui:<version>` |
| data-repository | `imgrepo.damit.hu/aiops/data-repository:<version>` |
| download-model / download-dataset / upload-model | `imgrepo.damit.hu/supernova/steps/<name>:<version>` |

Az image nevek **változatlanok** a régi repo-khoz képest → az aiops-k8s értékfájlok
(environments/solar*, environments/solar-dev) deploy után csak a tag-et kell
frissíteni.

### Harbor titkok

- `AIOPS_HARBOR_USERNAME` / `AIOPS_HARBOR_PASSWORD` — aiops projekt robot (control,
  webui, data-repository).
- `SUPERNOVA_HARBOR_USERNAME` / `SUPERNOVA_HARBOR_PASSWORD` — supernova projekt
  robot (step image-ek).

## Test image-ek

A **Build test images** workflow (`test-images.yaml`) ugyanezt a mátrixot használja,
de tetszőleges `test-*` taggel és branch kiválasztással (workflow_dispatch):

```
test-<branch>-<identifier>
```

A Python komponensek `APP_VERSION` build arg-ját a workflow PEP 440-kompatibilis
lokális verzióvá alakítja (pl. `test-operator-1` → `0.0.0+test.operator.1`).

## solar-host PyPI publish

A **Publish Host** workflow (`publish-host.yaml`) wheel-t buildel a
`apps/solar-host`-ból és PyPI-re publikál OIDC-vel (trusted publisher, `release`
environment). Ehhez a PyPI-n a `solar-host` projekthez hozzá kell adni a
DamitDev/solar repó `publish-host.yaml` workflow-ját pending publisherként.

## Verzió-illesztés

A komponensek egyetlen közös verziót kapnak release-enként. A lokális fejlesztés
`0.0.0-dev` marad; a Dockerfile-ok `APP_VERSION` build arg-gal kapják meg a release
verziót (sed a pyproject/package.json version mezőjébe).
