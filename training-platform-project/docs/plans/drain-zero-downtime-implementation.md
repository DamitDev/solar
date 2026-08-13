# Drain Zero-Downtime Evacuation — Implementation Plan

| Field    | Value                                          |
|----------|------------------------------------------------|
| Issues   | S-057                                          |
| Status   | Draft                                          |
| Created  | 2026-08-13                                     |
| Spec     | [docs/specs/host-draining.md](../specs/host-draining.md) (§4.2 amendment) |

## 0. Deliverables

- S-057 — `execute_evacuation()` in migration.py, EVACUATE executor switch,
  stall/backoff alignment with spec §4.3, spec amendment, unit + integration
  tests.

Land in one monorepo PR on branch `fix/S-057-drain-zero-downtime`
(conventional commit `fix:` per AGENTS.md).

---

## 1. Problem recap

EVACUATE (`app/services/reconciliation.py:1934`) calls `execute_migration`
(`app/services/migration.py:650`), whose ordering is stop-before-create:

1. ensure model on target — the long step; source keeps serving
2. **stop source** — alias goes dark here
3. disown source
4. create target — host-side create leaves the instance `STOPPED`
   (`solar_host/process_manager.py:925`)
5. next reconciler tick RECREATEs the target, `_start_instance` blocks on
   log-gated readiness

Downtime = step 2 → target running. For a cold target that is seconds to
minutes. The integration test only asserts final convergence.

---

## 2. `execute_evacuation()` in migration.py

### New function

`async def execute_evacuation(*, instance_id, source_host_id, target_host_id) -> MigrationResult`

Ordering (mirrors `execute_migration` steps 1-5, then diverges):

1. Validate hosts (exist, distinct), `check_no_active_training(source)`.
2. `capture_instance_config(source_host, instance_id)`; validate alias,
   model_source, priority (same as execute_migration).
3. `_settle_owning_intent(alias)`.
4. `validate_target_fitness(..., allow_production=True)` — a drain is the
   explicit policy decision the safeguard asks for (unchanged).
5. `check_one_replica_per_host(target, alias)`.
6. `ensure_model_on_target(target, model_source, file_filters)` — source
   keeps serving during the pull.
7. **Create target first**: `create_instance_on_host(target_host,
   create_wrapper)` with the same payload construction as execute_migration
   (resolved model path, ownership markers `managed_by`/`intent_id`,
   `priority`).
8. **Refresh the settle** (`_settle_owning_intent(alias)`) — the target's
   WS push must land before the next diff can observe the two-replica
   state.
9. **Start target**: POST `/instances/{id}/start`, timeout
   `settings.host_start_timeout_s` (blocking on log-gated readiness).
   On any start failure: best-effort `DELETE` the target instance, return
   a failed `MigrationResult` (`step="start_target", status="failed"`).
   The source is untouched — the drain stalls, the alias keeps serving.
10. **Stop source** (`stop_source_instance`). On failure: return failed
    result; the target is running, and the surplus logic
    (`reconciliation.py:1577-1582`) prefers the draining-host replica, so
    the reconciler converges on its own.
11. **Disown source** (`disown_source_instance` + Redis marker clearing,
    same as execute_migration).
12. **Delete source instance** — inside `execute_evacuation` now, not the
    executor: the drain contract is that the host ends up genuinely empty
    (host-draining.md §4.2).
13. Refresh the settle once more, so the source's disappearance lands
    before the next diff.

Return `MigrationResult` via `_build_result` with per-step `MigrationStep`
entries (`status="completed"` on the happy path, `"failed"` + `error` on
any step failure) — same shape the executor already understands.

### Implementation notes that are easy to get wrong

- **A module-level start helper is needed.** `_start_instance` is a
  `Reconciler` method (`reconciliation.py:2339`). Add
  `start_instance_on_host(host, instance_id)` in migration.py mirroring
  `create_instance_on_host`: aiohttp POST, timeout
  `settings.host_start_timeout_s`, `HTTPException` on non-200, 502 on
  connection errors. Evacuation does not need the
  `StartOutcomeUnknown`/`InstanceStartFailed` distinction — any failure
  leads to delete-target + failed result.
- **Delete the target on start failure.** Without it, one dead instance
  per retry piles up on the target and each still counts as an observed
  replica (the CREATE executor's pattern, `reconciliation.py:1852-1863`).
- **Settle refresh after create AND after start.** The pre-migration
  settle (10 s) expires during a multi-minute pull; the post-create
  refresh covers the created-but-stopped window, the post-start refresh
  covers the running-overlap window. Both are single calls, so the
  uncovered races are milliseconds wide.
- **The overlap is safe by construction.** Gateway registry is
  `alias → list[entries]` from all running instances (`app/gateway.py`) —
  two entries under one alias is the normal load-balanced shape. The
  surplus stop prefers draining-host replicas, so a stray tick retires
  the source, never the target.
- **Source deletion moves into the function.** The executor's current
  post-migration `_delete_instance` (`reconciliation.py:2009-2011`) goes
  away — `execute_evacuation` returns only after the source is gone, so
  `managed_remaining` and the `drained` sweep see consistent state.

---

## 3. EVACUATE executor switch in reconciliation.py

### Changes

- `reconciliation.py:1934-2013`: replace the `execute_migration` call with
  `execute_evacuation`; keep `_reserve_cold_start` and the pre-settle.
- Failure handling aligns with spec §4.3:
  - `result.status == "completed"` → `clear_stall`, return result.
  - `result.status == "failed"` → `record_stall(...)`, return `None`
    (no raise, no backoff — the drain re-evaluates every tick; the stall
    reason is visible in the drain status).
  - Unexpected exception → log + `record_stall` + return `None`
    (currently the code raises, which records exponential backoff and
    contradicts §4.3; this fixes that divergence).
- Delete the now-redundant post-migration source deletion.

### Unit tests

- `tests/test_migration.py` (new `test_evacuation.py` if cleaner):
  - happy path call order: ensure_model → create → start → stop → disown
    → delete (mock order assertion);
  - start failure → target deleted, source untouched, status `failed`;
  - stop failure → target left running, status `failed`;
  - disown failure → failed result, source stop already applied;
  - `start_instance_on_host` timeout/connection error → 502;
  - payload construction matches execute_migration's (ownership markers,
    resolved model path, priority).
- `tests/test_reconciliation.py`:
  - EVACUATE executor calls `execute_evacuation` (patch), not
    `execute_migration`;
  - failed evacuation → stall recorded, no backoff recorded;
  - no-target path unchanged (stall, no backoff);
  - existing drain tests keep passing (`test_drain.py`, `test_strategies.py`).

### Integration tests (`tests_integration/migration_path/test_host_drain.py`)

- Add a continuity assertion to `test_drain_evacuates_replica_and_completes`:
  sample the fleet instance state at a fine interval while the drain runs
  (the test stack can read both hosts' instance lists) and assert the
  alias never has zero running instances at any sample point — the drain
  must not cost serving capacity, not merely converge afterwards.
- Keep the existing final-state assertions (host `drained`, intent ready
  on target, second drain works).

Flakiness note: with a cached model the overlap window can be short; the
assertion is "never zero running", which holds regardless of overlap
length. Do not assert that an overlap was observed.

---

## 4. Spec amendment (`docs/specs/host-draining.md`)

- §4.2 table row: replace "migrate to the best remaining placement
  candidate via S-037, then delete the source" with the create-then-stop
  mechanism: evacuation creates the replacement on the target, starts it
  and waits for log-gated readiness, then stops, disowns and deletes the
  source.
- §4.2 paragraph "Evacuation **deletes the source instance** once the
  migration completes, which is where it departs from S-037": extend to
  note that evacuation also *orders* create-before-stop, and that S-037's
  `execute_migration` remains stop-before-create for the manual API path
  (D-017).
- §1's "A drain never reduces serving capacity on its own" needs no
  change — the amendment makes the implementation match it.

---

## 5. Commands

```bash
PYTHONPATH="" make test-solar-control
PYTHONPATH="" make lint-solar-control
PYTHONPATH="" make integration   # foreground, or PATH="/home/legekka/.conda/bin:$PATH" when backgrounded
```

---

## 6. Acceptance

1. Draining a host with a running managed replica: the alias stays
   registered and serving from drain start to `drained` — no gap.
2. The source host ends up genuinely empty; the drain promotes normally.
3. Target start failure: source keeps serving, drain reports stalled,
   no dead instances accumulate on the target.
4. Manual `POST /api/instances/migrate` behavior is unchanged
   (stop-before-create, source left stopped + disowned for inspection).
