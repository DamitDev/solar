# Solar Status

A Solar rendszer nyilvános állapotoldala: önálló single-page alkalmazás SvelteKit (Svelte 5) +
TypeScript + Tailwind CSS alapon, a pnpm workspace tagje. A solar-control `GET /api/status`
végpontjának összegzését jeleníti meg: a virtuális nevek (Aliases), a konkrét nevek
(Models) és a nem kezelt instance-ok (Instances) aktuális elérhetőségét, 90 napos
uptime-történetét, valamint az aktív és közelgő állapotüzeneteket.

## Fontos tudnivalók

- A `solar-status` önálló pnpm workspace tag, nem a solar-webui kiegészítése:
  egy app, egy toolchain.
- Minden API-hívás egyetlen kódpályán fut: a fetch kliens induláskor egyszer oldja
  fel az API base-t, a végponti útvonalakat pedig ehhez fűzi hozzá
  (`${apiBase}/status` minta).
- A backend oldalon egyetlen, nyilvános (azonosítás nélküli) végpont tartozik hozzá: a
  solar-control `GET /api/status` összegző útvonala. A válasz virtuális modellenként,
  aliasonként és instance-onként adja az aktuális elérhetőséget (`operational`, `degraded`,
  `down`, `unknown` — utóbbi, ha a forrás adata nem olvasható), a 90 napos napi
  uptime-bontást és uptime százalékot, valamint a megjeleníthető állapotüzeneteket (aktív
  incidens, karbantartás, előre meghirdetett tervezett karbantartás). Az oldal ezt hívja meg,
  és 60 másodpercenként, illetve a fül újra láthatóvá válásakor frissíti. Minden más `/api`
  útvonal továbbra is a menedzsment kulcsot igényli.

## Állapotüzenetek formázása (markdown)

Az állapotüzenet `body` mezője markdown (GFM): `**félkövér**`, listák, linkek, inline
`kód` és táblázatok használhatók, az egyszeres sortörések megmaradnak. A megjelenítés
`marked` (parser) + `DOMPurify` (szanitizálás) párosra épül, mert az oldal nyilvános:

- a nyers HTML és a szkriptek eltűnnek, csak egy szűk tag-allowlist marad meg;
- a képek nem jelennek meg (nem töltünk be harmadik féltől származó tartalmat);
- a címsorok `h3`-má alakulnak, hogy az üzenet ne hozzon létre második `h1`/`h2` elemet;
- a linkek új lapon nyílnak (`rel="noopener noreferrer"`).

A stílust a `@tailwindcss/typography` `prose prose-invert` osztályai adják. A `body` az
adatbázisban sima szöveg, ezért séma- vagy migrációváltozás nem kell.

## Serving módok

### Fejlesztés: külön Vite dev szerver

```bash
make dev-control # solar-control: http://localhost:8000
make dev-status  # SPA: http://localhost:5174
```

A dev szerver a saját portján, gyökér útvonalon (`/`) szolgálja ki az appot, proxy
nélkül: a böngésző közvetlenül, cross-origin hívja a solar-control API-ját (a
control nyitott CORS házirendje fedezi a dev origint). A solar-control ebben a
módban SPA build nélkül fut.

### Beágyazott: a solar-control kiadja a buildelt SPA-t

```bash
pnpm --filter solar-status build # dist/ keletkezik
make dev-control                 # http://localhost:8000/status
```

Ha a beállított dist-könyvtár létezik, a solar-control a `/status` útvonalon
szolgálja ki (a FastAPI beépített frontend mountja, `fallback='auto'` beállítással:
a kliensoldali route-okra index.html-t ad, a hiányzó assetekre 404-et). A
dist-könyvtárat a `STATUS_DIST_DIR` beállítás adja — alapértelmezésben a
`apps/solar-status/dist`, a Docker image-ben `/app/status-ui`. Build nélkül a
`/status` 404-es, minden más változatlan. A `GET /status` a `/status/` útvonalra
írányít át, ezért a buildelt assetek a `/status/` base alatt hivatkoznak egymásra.

## Konfiguráció

A buildhez és az indításhoz kötelező env változó:

| Változó             | Dev (`.env.development`)    | Production (`.env.production`) |
| ------------------- | --------------------------- | ------------------------------ |
| `VITE_API_BASE_URL` | `http://localhost:8000/api` | `../api`                       |

- Ha hiányzik (vagy üres), a build/indítás azonnal elhasal — nincs beépített
  alapértelmezés a kódban. Értéke buildenként felülírható (shell env, Docker
  build arg).
- A mount pontot a `__BASE_PATH__` build-time define szolgáltatja (`/` dev,
  `/status` beágyazott módban). A feloldás erre az útvonalfüggetlen anchorra
  történik: `new URL(VITE_API_BASE_URL, new URL(basePath, location.origin))`.
- A végponti útvonalak a feloldott base-hez kapcsolódnak: a `status` végpont dev
  módban `http://localhost:8000/api/status`, beágyazott módban same-origin
  `/api/status` útvonalat ér el.

## Fejlesztés, tesztelés

```bash
make dev-status         # Vite dev szerver :5174
make test-solar-status  # vitest (unit + komponenstesztek)
make lint-solar-status  # prettier + eslint + svelte-check
```

Vagy közvetlenül az app könyvtárából: `pnpm dev`, `pnpm test`, `pnpm build`,
`pnpm check`. A komponenstesztek vitest + @testing-library/svelte párost használnak
jsdom környezetben.
