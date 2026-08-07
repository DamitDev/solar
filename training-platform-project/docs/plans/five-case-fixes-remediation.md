# Remediation plan — audit findings on the five-case-fixes branch

Follow-up to
[five-case-fixes-implementation.md](five-case-fixes-implementation.md).
Branch under review: `fix/five-case-fixes` (three commits on top of `main`).

This document is self-contained: every finding restates its evidence with
file/line references and a concrete fix, so the original plan is background
reading rather than a prerequisite.

---

## 0. Scope and verification baseline

The implementation is high fidelity. All four suites are green and every
number an earlier review reported was reproduced independently:

| Gate | Result |
|---|---|
| `make test-solar-control` | 699 passed |
| `make test-solar-host` | 550 passed |
| `pnpm --filter solar-webui test` | 217 passed, 28 files |
| ruff + black (control, host) | clean |
| eslint + prettier (webui) | clean — 3 pre-existing warnings, 0 errors |
| `make integration` | **not verified** — Docker unavailable in the audit environment |

Green suites are not the same as a working feature. The P0 finding below is a
production-only defect that the unit tests actively mask, which is exactly why
it survived to this point.

### Findings by severity

| # | Severity | Area | Finding |
|---|---|---|---|
| **F1** | **P0 blocker** | C1 / 1.3 | Drift circuit breaker cannot fire: the counter is persisted but never hydrated |
| **F2** | P1 | C3 / 3.2-3.3 | Legacy intents become uneditable — missing grandfathering in the third validator |
| **F3** | P1 | C3 / 3.8 | The 422 for F2 renders as a content-free banner: inline slot / banner-filter mismatch |
| **F4** | P1 | C3 / 3.8 | Advisory warnings never reach the user on the create path |
| **F5** | P2 | C1 / 1.1 | Glob and `endswith` matching applies to every string backend field, not path-like keys |
| **F6** | P2 | C4 / 4.2 | Cold starts still fail hard at 30 min on the inner HTTP pull bound, without `recoverable` |
| **F7** | P2 | C3 / 3.1 | `gpu_type` is normalized on write but not on read, so validation and placement disagree |
| **F8** | P3 | C3 / 3.8 | `resources.vram_gb` / `ram_gb` are dead entries in `INLINE_ERROR_FIELDS` |
| **F9** | P3 | C3 | Four per-intent reconciler dicts are never pruned for deleted intents |
| **F10** | P3 | C2 / 2.4 | `exit_code` is `null` for readiness-timeout start failures |
| **F11** | P3 | C2 / 2.2 | Stale comment: "newest file per alias" now means per `(alias, instance_id)` |
| **F12** | P3 | C5 / 5.4 | `_merge_resource_payload` docstring overclaims `memory_type` coverage |
| **F13** | P3 | C5 / 5.4 | Freshness arithmetic duplicated instead of calling the shared helper |
| **F14** | P3 | C1 / tests | The `_coerce_jsonish` parity test does not actually compare against the host |
| **F15** | P3 | C3 / tests | The ownership pin test is a subset assertion and cannot fail on a host-side addition |

Sequencing: F1 alone gates merge. F2 and F3 are one user-visible defect in two
layers and must land together. Everything else is independent.

---

## 1. F1 — the drift circuit breaker cannot fire (P0)

### Symptom

Plan item 1.3 exists to guarantee that *any future* drift vector degrades into
one actionable error rather than an infinite stop/recreate loop. It never
trips. `BackendDriftUnsettled` and the `Degraded` / `DriftUnsettled` condition
are unreachable in production.

### Root cause

`drift_replace_attempts` and `drift_unsettled_keys` are written to
`status_json` but never read back out of it.

The reconciler persists them:

```python
# apps/solar-control/app/services/reconciliation.py:2650-2651
"drift_replace_attempts": drift_attempts,
"drift_unsettled_keys": sorted(unsettled_keys),
```

It reads them off the hydrated model on the next tick:

```python
# apps/solar-control/app/services/reconciliation.py:1386-1387
attempts = getattr(intent.status, "drift_replace_attempts", 0) or 0
if attempts < settings.max_drift_replace_attempts:
```

```python
# apps/solar-control/app/services/reconciliation.py:2465-2471
prev_attempts = getattr(intent.status, "drift_replace_attempts", 0) or 0
if drift_replace:
    drift_attempts = prev_attempts + 1
```

But `_row_to_response`
([apps/solar-control/app/database/intents.py](../../../apps/solar-control/app/database/intents.py)
lines 47-69) builds `IntentStatus` from an explicit key list that omits both
fields, so Pydantic supplies the defaults (`0` and `[]`) on every load. Both
fields are declared on the model
([apps/solar-control/app/models/intent.py](../../../apps/solar-control/app/models/intent.py)
lines 205 and 214, the latter documented as "Persisted rather than…"), and the
update route already resets them on a spec edit
([database/intents.py](../../../apps/solar-control/app/database/intents.py)
lines 186-187) — so persistence was clearly intended and only the read side is
missing.

Round trip, reproduced directly against `_row_to_response`:

```
persisted in status_json  : 2 ['chat_template_kwargs']
hydrated onto IntentStatus: 0 []
```

Every tick reloads the intent through `get_intent`, so `prev_attempts` is
always `0`, `drift_attempts` is written as `1` forever, and the counter can
never reach `max_drift_replace_attempts` (default `3`).

### Why the tests do not catch it

`tests/test_drift_circuit_breaker.py` assigns the counter onto the in-memory
intent between ticks (around line 377) instead of going through a persist and
reload. That models a hydration path that does not exist.

### Fix

**1.1 — Hydrate both fields.** In `_row_to_response`, alongside the
neighbouring `status.get(...)` calls:

```python
drift_replace_attempts=status.get("drift_replace_attempts", 0),
drift_unsettled_keys=status.get("drift_unsettled_keys", []),
```

**1.2 — Make the test exercise the round trip.** Replace the manual counter
sync with a helper that feeds each tick's persisted `status_json` back through
`_row_to_response`, so the assertion "the breaker trips on the Nth consecutive
drift REPLACE" depends on hydration actually working. Add a direct unit test
pinning that every key `_update_status` writes into `status_json` is read back
by `_row_to_response`, so the next status field added cannot repeat this.

**1.3 — Consider an integration guard.** `tests_integration/intent_path/` runs
real reconcile ticks against Postgres. A test that forces an unsettleable drift
vector and asserts the intent reaches `BackendDriftUnsettled` rather than
churning would close the class of bug that unit tests structurally cannot see.
Worthwhile but optional; the pin test in 1.2 is the cheap guard.

---

## 2. F2 + F3 — legacy intents become uneditable behind a content-free error (P1)

These are one defect in two layers. Either fix alone leaves a bad outcome: the
server fix alone still leaves other errors invisible, and the webui fix alone
turns a silent failure into a visible one the user still cannot act on.

### F2 — the server half: missing grandfathering

`_unchanged_backend_fields`
([apps/solar-control/app/validation.py](../../../apps/solar-control/app/validation.py)
lines 399-417) exists specifically so that tightening a static table cannot
strand an already-stored intent — every update replays the full spec, so it
would otherwise fail on a field the user is not touching. Its docstring says so
explicitly.

The exemption reaches two of the three backend validators:

```python
# apps/solar-control/app/validation.py:592-600
errors.extend(
    _validate_backend_field_ownership(
        backend, exempt_fields=ownership_exempt_fields
    )
)
errors.extend(
    _validate_device(backend, placement, exempt_fields=ownership_exempt_fields)
)
errors.extend(_validate_backend_model_selection(backend, model_source))
```

`_validate_backend_model_selection` gets no `exempt_fields` parameter, yet it
owns two rules that can fire on untouched stored values: `model_file` requires
`llamacpp` (lines 167-175) and `mmproj` requires `model_type` in
`{None, "llm"}` (lines 205-217).

Reproduced with `validate_intent_update`, changing only `replicas`:

```
mmproj case     -> [{'field': 'backend.mmproj',
                     'message': "mmproj is meaningless for model_type 'embedding' — …"}]
model_file case -> [{'field': 'backend.model_file',
                     'message': 'model_file is only supported for the llamacpp backend'}]
```

Both configurations were legal before this branch — the host silently dropped
the field — so they exist in stored specs. The intent is now permanently
uneditable.

**Fix.** Give `_validate_backend_model_selection` the same
`exempt_fields: frozenset[str] = frozenset()` parameter and skip `model_file`
and `mmproj` when the key is exempt, matching `_validate_device`'s treatment at
line 331. `file_filters` should *not* be exempted: it is validated against
`model_source`, which the update can change independently of the backend, so a
carried-over value can become newly invalid.

While here, fold `model_file`'s wrong-backend check into the ownership table as
plan item 3.2 originally specified — `model_file` is already in
`_LLAMACPP_ONLY_FIELDS`, so the rule is duplicated across two mechanisms today
and only one of them grandfathers. Keep the non-empty-string check where it is.

### F3 — the webui half: the error renders as an empty banner

`INLINE_ERROR_FIELDS`
([apps/solar-webui/src/components/IntentFormModal.tsx](../../../apps/solar-webui/src/components/IntentFormModal.tsx)
lines 64-79) is a hand-maintained list of fields that own an inline slot. It
drives the banner filter:

```tsx
// apps/solar-webui/src/components/IntentFormModal.tsx:260-263
setServerError({
  message: detail.message,
  errors: (detail.errors ?? []).filter((errItem) => !INLINE_ERROR_FIELDS.has(errItem.field)),
});
```

Three of its entries name slots that are conditionally rendered, and in each
case the condition is false in exactly the configuration that produces the
error:

| Field | Slot | Rendered only when | Error fires when |
|---|---|---|---|
| `backend.mmproj` | `BackendConfigFields.tsx:293` | `llamaCppMode === 'llm'` | mode is *not* `llm` |
| `backend.model_file` | `BackendConfigFields.tsx:273` | inside the llama.cpp branch | backend is *not* llamacpp |
| `backend.device` | `BackendConfigFields.tsx:799` | inside the HuggingFace branch | backend *is* llamacpp |

The error is therefore stripped from the banner and has nowhere to render. What
the user sees is the red box with `extractApiError`'s fallback message —
literally the words "Invalid intent" — and no field, no message, no way to
proceed.

`backend.device` is currently protected by F2's grandfathering, so only the
`mmproj` and `model_file` paths are live today. Fixing F2 closes those two as
well, but the mismatch remains a trap for every future rule.

**Fix.** Stop maintaining the set by hand. Track which slots actually mounted
and filter the banner on that, so an error can never fall between the two. The
smallest version that achieves it: have `fieldError(field)` register the field
in a ref on render, and filter the banner against that ref. A field whose slot
did not mount stays in the banner, which is the correct fallback.

Two supporting changes:

- **Auto-open collapsed sections that contain an error.** `placement.*` and
  `resources.*` slots live inside `<details>` elements whose open state is
  computed once at mount (lines 114-117). An error landing in a closed section
  is invisible for the same reason. On submit failure, open any section holding
  a field error.
- **Scroll the first error into view.** The modal is `max-h-[90vh]
  overflow-y-auto`; an inline error below the fold reads as no feedback at all.

---

## 3. F4 — advisory warnings never reach the user on the create path (P1)

### Symptom

Plan §9's C3 manual validation step — "Request more replicas than there are
hosts; confirm the intent saves and shows an advisory warning" — cannot pass.
Creating an intent shows no warning.

### Root cause

`warnings` is a response-only field, correctly never persisted and never
emitted on the `intent_update` socket event
([apps/solar-control/app/models/intent.py](../../../apps/solar-control/app/models/intent.py)
lines 253-255). The server attaches it to both the 201 and the 200
([routes/management/intents.py](../../../apps/solar-control/app/routes/management/intents.py)
lines 96 and 208), which matches plan §10's "the `warnings` field on create and
update responses".

The webui then drops it on two of the three flows:

```tsx
// apps/solar-webui/src/components/IntentsPage.tsx:118-126
const handleCreated = (intent: Intent) => {
  setShowNewIntent(false);
  navigate(`/intents/${intent.id}`);
};

const handleEdited = (intent: Intent) => {
  setEditTarget(null);
  setRestIntents((prev) => new Map(prev).set(intent.id, intent));
};
```

`handleCreated` discards the response body and navigates. `IntentDetail` then
calls `getIntent(id)`, and since warnings are response-only that record has
none — making the `record.warnings` branch at `IntentDetail.tsx:175-178`
effectively dead. `handleEdited` keeps the object but `IntentsPage` never
renders warnings.

Only editing from inside the detail page works, because that path passes the
save response straight into local state
([IntentDetail.tsx](../../../apps/solar-webui/src/components/IntentDetail.tsx)
lines 656-660).

### Fix

Carry the warnings across the navigation and render them on arrival. Pass them
through `navigate`'s state argument in `handleCreated` and seed `IntentDetail`'s
`warnings` from `location.state` on mount, clearing the state entry afterwards
so a later reload does not resurrect a stale advisory. Drop the dead
`record.warnings` branch in `fetchIntent`, or keep it only if
`GET /api/intents/{id}` is ever made to recompute advisories — which it should
not be, since that would put a fleet scan on every detail poll.

For `handleEdited`, render a brief inline notice on the list row rather than
routing the user elsewhere; the edit did succeed.

---

## 4. F5 — path matching applies to every string backend field (P2)

### Symptom

The risk is the mirror image of C1: instead of drift where there is none, this
can report no drift where there is some, silently keeping a stale instance
alive after a real config change.

### Root cause

The third comparison layer is not scoped to path-like keys:

```python
# apps/solar-control/app/services/reconciliation.py:2957-2965
if isinstance(spec_value, str) and isinstance(inst_value, str):
    if any(c in spec_value for c in "*?["):
        depth = spec_value.replace(os.sep, "/").count("/") + 1
        tail = "/".join(inst_value.replace(os.sep, "/").split("/")[-depth:])
        return fnmatch.fnmatch(tail, spec_value.replace(os.sep, "/"))
    return inst_value.endswith("/" + _strip_relative_prefix(spec_value))
```

Every string field whose spec value contains `*`, `?` or `[` is glob-matched.
`ot` (llama.cpp tensor override) is a **regular expression** that routinely
contains all three — a value like `blk\.[0-9]*\.ffn.*=CPU` enters the glob
branch. A bare `*` matches any single-segment instance value unconditionally.

The plan scoped this layer to "the instance value is an absolute path"; the
implementation applies it to any two strings, and also does not require
`inst_value` to be absolute.

Note the depth-aware tail logic is *better* than the plan's basename proposal
and should be kept — only the key scoping is wrong.

### Fix

Introduce an explicit set of path-resolved keys — `mmproj`,
`chat_template_file`, `model_file` — and gate layer 3 on membership. Require
`inst_value.startswith("/")` (or `os.path.isabs`) as the plan specified, so a
relative instance value cannot false-match through `endswith`.

Add tests for the two masked-drift cases: an `ot` regex change between two
values where the new instance value happens to `fnmatch` the old spec must
report drift, and `mmproj: "*"` must not match an arbitrary resolved path.

Optional, lower value: extend `_strip_relative_prefix` to handle `../`. The
plan's own `lstrip("./")` sketch had the same limitation, and the current
implementation is already strictly better — it correctly avoids
`lstrip`'s character-set trap.

---

## 5. F6 — cold starts still fail hard at 30 minutes (P2)

### Symptom

The original C4 report — a `TimeoutError` in `last_error` while the host is
visibly still downloading — is fixed for the 60-second case and recurs at 30
minutes.

### Root cause

Fix 4.1 and 4.2 are correctly implemented: the normal reconcile flow now goes
through `_await_action_with_progress`, whose ceiling is `_action_timeout_s`
(`model_pull_timeout_s + host_start_timeout_s + 60` ≈ 46 min), and it marks a
give-up `recoverable` when Redis pull progress is fresh.

But an *inner* bound fires first. `resolve()` runs inside `_act`, and the
control-to-host pull request carries its own client timeout:

```python
# apps/solar-control/app/model_resolvers/repo.py:184
timeout=aiohttp.ClientTimeout(total=settings.model_pull_timeout_s),
```

The same at
[huggingface.py:43](../../../apps/solar-control/app/model_resolvers/huggingface.py),
with `model_pull_timeout_s = 1800.0`
([config.py:89](../../../apps/solar-control/app/config.py)). Neither resolver
was touched by this branch — `git diff main...HEAD -- app/model_resolvers/` is
empty.

At 30 minutes `aiohttp` raises inside the action task. That is a genuine task
result, not the helper's synthetic timeout, so `_await_action_with_progress`
returns it verbatim and `recoverable` is never set. The host is unaffected —
`pull_model` runs via `asyncio.to_thread` and keeps downloading — so the user
again gets a hard error contradicting live progress.

This is the plan's own root cause B. Fix 4.2 as *written* only covers the outer
`asyncio.wait_for`, so the implementation is faithful to the text; the plan
under-specified the remedy.

### Fix

Two independent options, cheapest first:

- **Mark it recoverable.** Where `_act` failures are converted into
  `last_error`, treat an `asyncio.TimeoutError` / `aiohttp.ServerTimeoutError`
  raised while Redis pull progress for `(host_id, model_source)` is fresher
  than `pull_progress_stale_after_s` as `recoverable = True`, reusing the
  freshness check `_await_action_with_progress` already performs. The webui
  then renders the amber "still working" notice instead of the red block, which
  is the correct user-facing outcome.
- **Make the inner bound progress-aware too.** Replace the fixed
  `ClientTimeout(total=...)` with `sock_read`-based timeouts, or raise
  `model_pull_timeout_s` and let the outer progress-aware ceiling be the real
  bound. The former is more correct; the latter is a one-line stopgap.

Recommend doing the first now and tracking the second as a follow-up issue,
since it touches a resolver shared with paths outside this PR's scope.

---

## 6. F7 — `gpu_type` is normalized on write but not on read (P2)

### Symptom

An intent stored with an alias token such as `mps` is reported by validation as
placeable, while the reconciler places nothing and `_shortfall_reason` says
`no host matches gpu_type=mps`.

### Root cause

Normalization happens only in `validate_intent_create`, which mutates the
payload in place before it is persisted
([validation.py:543-557](../../../apps/solar-control/app/validation.py)) — that
part works, and a full PUT does fix a stored alias.

Nothing normalizes on read. Placement still compares raw:

```python
# apps/solar-control/app/services/placement.py:56
if gpu_type is not None and host.gpu_type != gpu_type:
```

Meanwhile the fleet validator *does* normalize before calling the very same
filter chain:

```python
# apps/solar-control/app/services/intent_validation.py:46-48 (and 166-168)
gpu_type = normalize_gpu_type(placement.get("gpu_type")) or placement.get(
    "gpu_type"
)
```

So for a legacy row the two disagree — precisely what plan §3.5 set out to make
impossible ("Eligibility reuses the real filter chain … so validation and
placement cannot disagree").

The underlying non-matching behaviour predates this branch and is not a
regression; the *disagreement* is new.

### Fix

Normalize at the point of use in the reconciler, so validation and placement
read the same token: apply `normalize_gpu_type` where `placement.gpu_type` is
read for `find_candidates` (around `reconciliation.py:1195`) and in
`_shortfall_reason` (around line 2744), falling back to the raw value when
normalization returns `None`, exactly as `intent_validation.py` does.

Optionally add a one-shot backfill that canonicalizes stored `placement.gpu_type`
values, so old rows stop needing a PUT to become placeable. This is a data
migration and can ship separately.

---

## 7. P3 — nits, batched

None of these change behaviour a user can observe. Group them into one
`chore:` commit.

**F8 — dead entries in `INLINE_ERROR_FIELDS`.** `resources.vram_gb` and
`resources.ram_gb` are in the set
([IntentFormModal.tsx:77-78](../../../apps/solar-webui/src/components/IntentFormModal.tsx))
but the VRAM and RAM inputs (lines 632-655) render no `fieldError(...)` slot.
Currently harmless: those two fields are only ever emitted as *warnings* by
`validate_intent_fleet`
([intent_validation.py:218,228](../../../apps/solar-control/app/services/intent_validation.py)),
never as hard errors. F3's render-tracking fix removes the whole class; until
then either add the two slots or drop the two entries.

**F9 — unpruned per-intent reconciler state.** `_fleet_violations_logged`
([reconciliation.py:416](../../../apps/solar-control/app/services/reconciliation.py))
keeps one entry per intent id forever. So do the three pre-existing siblings
declared beside it: `_backoff` (line 395), `_settle_until` (line 400) and
`_displace_cooldown` (line 406). Fix all four together with a single prune step
in the reconcile loop that drops keys for intent ids absent from the current
listing — fixing only the new one leaves the same leak in place.

**F10 — `exit_code` is `null` on readiness timeouts.** The start-failure body
reads `process_manager.get_last_exit_code(instance_id)`
([routes/instances.py:198](../../../apps/solar-host/solar_host/routes/instances.py)),
but `last_exit_codes` is only written in `_handle_child_exit`
([process_manager.py:234](../../../apps/solar-host/solar_host/process_manager.py)).
A readiness-timeout failure therefore reports `exit_code: null`. Either record
the exit code on the timeout path once the process is killed, or document that
`null` means "timed out, process did not exit on its own" — the `log_tail` is
the useful signal there anyway.

**F11 — stale comment.** `solar_host/config.py:53` says "the newest file per
alias is always kept"; `_cleanup_old_logs` keeps the newest per
`(alias, instance_id)` and its own docstring says so
([process_manager.py:785-792](../../../apps/solar-host/solar_host/process_manager.py)).
The code is right and better than the plan; the config comment is stale.

**F12 — docstring overclaims.** `_merge_resource_payload`
([resources.py:36-42](../../../apps/solar-control/app/routes/management/resources.py))
lists `memory_type` among the merged fields; the loop only handles
`vram`/`ram`/`disk` and `reservations`. `memory_type` is absent from
`HostResourceSnapshot` on `main` too, so this is a doc fix, not a behaviour
change — unless surfacing `memory_type` is wanted, which is its own issue.

**F13 — duplicated freshness arithmetic.** `_read_fresh_ws_snapshot`
([resources.py:137-145](../../../apps/solar-control/app/routes/management/resources.py))
reimplements the naive-datetime handling that `entry_age_s`
([redis_state/freshness.py:13-31](../../../apps/solar-control/app/redis_state/freshness.py))
already encapsulates and documents. Call the helper. The logic is currently
correct in both places — control writes `at`, it is timezone-aware, and naive
values are assumed UTC — so this is drift prevention only.

**F14 — parity test does not test parity.** `test_coerce_jsonish_matches_host_semantics`
in `tests/test_reconciliation.py` describes the host's
`_coerce_template_kwargs` in a comment and then asserts only against control's
copy. Plan §1.1 asked for a test that "pins the two behaviours together". The
host package is not importable from control's test env, so pin it by asserting
against a small table of input/output pairs that is also asserted in
`apps/solar-host/tests/`, keeping the table identical in both files.

**F15 — ownership pin test is a subset assertion.** `test_intents.py:1289-1319`
asserts `{...} <= llamacpp_fields`, which passes when the host adds a field the
table omits. That omission is safe today (unknown fields fall through by
design), but the test claims to catch exactly that case. Either assert equality
against the documented list or reword the test name and docstring to say what
it really checks.

---

## 8. Explicitly not changing

Recorded so these are not re-raised:

- **`warnings` on the PUT response is correct.** Plan §10 asks for the field on
  "create and update responses". The code comment saying warnings are "never
  emitted on `intent_update`" refers to the Socket.IO event, which carries DB
  records and has none — not the HTTP response.
- **Reservations reading a ≤30 s WS snapshot is plan-aligned.** Plan 5.4 made
  `_fetch_host_resource_snapshot` cache-first for all call sites, and the host
  still enforces capacity on `POST /resources/reservations`. Worth knowing
  operationally; not a defect.
- **`memory_type` never reaching `HostResourceSnapshot`** predates this branch.
- **`_continue_strategy` leaving CREATE unbounded** is what plan §4 specifies.
- **`huggingface_vision` missing from the webui mode picker** is the recorded
  non-goal, already tracked as U-010.

---

## 9. Tests

**solar-control**

- `tests/test_drift_circuit_breaker.py` — rewrite the tick loop to round-trip
  `status_json` through `_row_to_response` instead of assigning the counter
  (F1). Without this the suite still passes against the broken code.
- `tests/test_intents.py` — new status-field pin test: every key
  `_update_status` writes is hydrated by `_row_to_response` (F1). Update the
  ownership pin test to a real equality assertion (F15).
- `tests/test_intents.py` — update-grandfathering cases for `mmproj` on a
  non-`llm` `model_type` and `model_file` on a HuggingFace backend, both
  asserting no error when the value is carried over unchanged and an error when
  it is newly introduced (F2). Assert `file_filters` is *not* grandfathered.
- `tests/test_reconciliation.py` — masked-drift cases for `ot` and for
  `mmproj: "*"`; a relative `inst_value` must not match via `endswith` (F5).
  Make the `_coerce_jsonish` parity table shared with the host suite (F14).
- `tests/test_action_timeouts.py` — an `aiohttp` timeout raised from inside
  `_act` while Redis progress is fresh yields `last_error.recoverable is True`
  (F6).
- `tests/test_shortfall_reason.py` / `test_intent_validation_fleet.py` — a
  stored `gpu_type: "mps"` produces the same candidate set in validation and in
  the reconcile read path (F7).

**solar-host**

- `tests/test_start_failure_payload.py` — pin whatever F10 decides: either a
  recorded exit code on the readiness-timeout path or an explicit assertion
  that `exit_code` is `null` there with a non-empty `log_tail`.

**solar-webui**

- `src/components/__tests__/IntentFormModal.test.tsx` — a 422 on a field whose
  inline slot is not mounted must appear in the banner; a 422 on a field inside
  a collapsed section must open that section (F3). Add a case per row of the F3
  table.
- New `src/components/__tests__/IntentWarnings.test.tsx` — warnings from a
  create response survive the navigation to the detail page and are dismissible
  (F4).

---

## 10. Lint, format and running

Unchanged from the original plan §8. Format first, then per-app gates:

```bash
make format
make lint-solar-control && make test-solar-control
make lint-solar-host    && make test-solar-host
make lint-solar-webui   && make test-solar-webui
```

`make test` only runs webui lint, so use `make test-solar-webui` to execute
vitest. Full sweep plus `make integration` before pushing — the integration
suite is the one gate this audit could not run, and F1's fix is exactly the
kind of change it should cover.

---

## 11. Manual validation

**F1.** Create a llama.cpp intent, then hand-edit its stored backend so a field
cannot settle (or point `mmproj` at a value the host resolves differently).
Confirm the intent reaches `BackendDriftUnsettled` with the mismatching keys
named after `max_drift_replace_attempts` rounds, instead of churning. Check
`GET /api/intents/{id}` shows `status.drift_replace_attempts` climbing across
ticks rather than sticking at 1.

**F2 + F3.** Insert an intent with `backend_type: llamacpp`,
`model_type: embedding` and a non-empty `mmproj`. Edit only the replica count in
the webui and confirm it saves. Then deliberately introduce a new invalid field
and confirm the error appears — inline where a slot exists, in the banner where
none does, with the containing section opened and scrolled to.

**F4.** Create an intent requesting more replicas than there are eligible
hosts. Confirm the amber advisory appears on the detail page after the
redirect, and that dismissing it and reloading does not bring it back.

**F5.** Set `ot` to a regex containing `*` and `[`, let the intent settle, then
change `ot` to a different regex. Confirm the change is detected as drift and
the replica is replaced exactly once.

**F6.** Start a cold pull large enough to exceed 30 minutes. Confirm the error,
if any, renders as the amber "still working" notice while pull progress keeps
updating, not the red block.

**F7.** Store an intent with `gpu_type: "mps"` via a direct API call against a
pre-fix record, then confirm validation and the reconciler agree on placement.

---

## 12. Documentation

- [deployment-intent.md](../specs/deployment-intent.md) §8.2.2 — record that the
  circuit-breaker counter is persisted in `status_json` **and** hydrated on
  read, since the invariant is the whole point of the feature.
- Same spec, §4.7.2 — document that field-ownership and modality rules
  grandfather values an update carries over unchanged, and that `file_filters`
  is deliberately excluded.
- §8.2.1 — narrow the documented scope of path/glob comparison to the resolved
  path keys once F5 lands.
- [DEVELOPMENT.md](../../../docs/DEVELOPMENT.md) — note the 30-minute inner pull
  bound and what `recoverable` means to an operator watching a cold start
  (Hungarian, per `AGENTS.md`).

New issue files under `issues/Phase 0/Milestone 0.5/` and `issues/Phase 4/`,
continuing from S-052 / U-010, plus ROADMAP rows:

- `S-053` — drift circuit-breaker persistence and the status-hydration pin (F1).
- `S-054` — update grandfathering for modality rules; path-scoped drift
  comparison; `gpu_type` normalization on the read path (F2, F5, F7).
- `S-055` — recoverable cold-start failures on the inner pull bound (F6).
- `U-011` — intent form error routing and advisory warnings on create (F3, F4).

Use plain markdown per `templates/issue-template.md`; ROADMAP tables take
`ID | Issue | Repo | Size | Depends on`.

---

## 13. Commit plan

Conventional commits, ordered so the merge-blocking fix is first and
independently cherry-pickable:

1. `fix(solar-control): hydrate drift circuit-breaker state from status_json` —
   F1 plus the round-trip and status-pin tests.
2. `fix(solar-control): grandfather unchanged backend fields in modality validation` —
   F2, folding `model_file` into the ownership mechanism.
3. `fix(solar-webui): route intent form errors to a mounted slot or the banner` —
   F3, including section auto-open and scroll-into-view.
4. `fix(solar-webui): keep advisory warnings across the create redirect` — F4.
5. `fix(solar-control): scope drift path matching to resolved path fields` — F5.
6. `fix(solar-control): mark cold-start pull timeouts recoverable while progress is fresh` —
   F6.
7. `fix(solar-control): normalize gpu_type on the placement read path` — F7.
8. `chore: prune per-intent reconciler state and correct stale comments` —
   F8-F15.
9. `docs: specs, issues and ROADMAP for the remediation findings` — §12.

Commits 1-4 are the merge gate. 5-9 can follow in the same PR or a second one,
depending on how urgently the branch needs to land.
