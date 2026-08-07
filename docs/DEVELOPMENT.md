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

## Hidegindítás és hibakeresés (S-049 – S-052)

Egy olyan modellhez, ami még nincs a hoston, az intent létrehozása annyi ideig tart,
ameddig a letöltés. Több GB esetén ez percek, és ez alatt **nem hiba**, ha az intent
`reconciling` állapotban áll:

- A host `pull_progress` eseményeket küld (fázis, letöltött bájtok, sebesség), amit a
  WebUI folyamatjelzőként mutat. A `GET /api/pulls` ugyanezt adja vissza annak a
  kliensnek, ami menet közben csatlakozott.
- A reconciler hidegindítós műveletei (CREATE / EVACUATE / MIGRATE) a
  `MODEL_PULL_TIMEOUT_S + HOST_START_TIMEOUT_S + 60` másodperces korláton belül
  várnak, nem a rövid 60 másodpercesen. Amíg a letöltés halad, a várakozás
  végigmegy; ha a host abbahagyja a jelentést, a művelet hamarabb feladja.
- Ha egy hidegindítás úgy szakad meg, hogy a host közben még dolgozott, a hiba
  `recoverable` jelölést kap, és a WebUI-n sárga „még dolgozik” üzenetként jelenik
  meg piros hiba helyett. Ilyenkor nincs teendő, a következő kör folytatja.
- Ha egy instance elindulás közben elszáll, a host megőrzi a kimenetét (a logfájl
  neve tartalmazza az instance id-t), így a WebUI-ról a hibaüzenet mellől
  megnyitható akkor is, ha a reconciler már törölte az instance-t.

### Új környezeti változók

Mindkét app `pydantic_settings.BaseSettings`-t használ `env_prefix` nélkül, így a
környezeti változó a mező nagybetűs neve. Minden alapérték úgy van megválasztva, hogy
beállítás nélkül a viselkedés változatlan maradjon.

**solar-control**

| Változó | Alap | Mit állít |
|---|---|---|
| `MAX_DRIFT_REPLACE_ATTEMPTS` | `3` | Hány egymást követő, drift miatti REPLACE után adja fel a reconciler, és rögzít `BackendDriftUnsettled` hibát végtelen ciklus helyett. Csak a ténylegesen lefutott REPLACE számít bele, így egy host-kiesés nem meríti ki a keretet. |
| `HOST_SNAPSHOT_MAX_AGE_S` | `30.0` | Meddig használható a Redisben tárolt host-erőforrás pillanatkép, mielőtt HTTP-re esik vissza. A host 10 másodpercenként küld health-et, tehát ez három tick. |
| `ACTION_PROGRESS_SLICE_S` | `120.0` | Milyen sűrűn nézi meg a reconciler a letöltés állapotát egy hidegindítás alatt. |
| `PULL_PROGRESS_STALE_AFTER_S` | `180.0` | Ennyi néma másodperc után tekinti elakadtnak a futó letöltést. |
| `PULL_PROGRESS_TERMINAL_GRACE_S` | `300.0` | Meddig marad olvasható egy befejezett letöltés a `GET /api/pulls` válaszában. |

**solar-host**

| Változó | Alap | Mit állít |
|---|---|---|
| `RETAINED_LOG_BUFFERS` | `20` | Hány leállt instance logpuffere marad a memóriában. Egyenként `LOG_BUFFER_SIZE` sor, tehát a memóriaigény felülről korlátos. |
| `LOG_FILE_RETENTION_S` | `86400.0` | Meddig maradnak meg a logfájlok a lemezen (24 óra a korábbi 5 perc helyett). Az alias legfrissebb fájlja kortól függetlenül megmarad. |
| `START_FAILURE_LOG_TAIL_LINES` | `20` | Hány sort csatol a host a strukturált indítási hibaválaszhoz. |
| `PULL_PROGRESS_INTERVAL_S` | `5.0` | Milyen sűrűn küld a host letöltési folyamatjelzést. |

## Konvenciók

- Felhasználó felé néző dokumentáció magyarul, kivéve a kódkommenteket és a
  fejlesztői (AGENTS) dokumentációt.
- Conventional commits; PR template a `.github/PULL_REQUEST_TEMPLATE.md`.
- A Docker image nevek és Harbor projektek **nem változnak** a régi repo-khoz képest
  (`aiops/solar-control`, `aiops/solar-webui`, `aiops/data-repository`,
  `supernova/steps/*`) — az aiops-k8s értékfájlok ettől függetlenek maradnak.
