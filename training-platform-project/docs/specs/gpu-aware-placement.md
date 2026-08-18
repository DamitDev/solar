# GPU-Aware Placement Specification

| Field       | Value                          |
|-------------|--------------------------------|
| Issue       | S-058                          |
| Status      | Draft                          |
| Created     | 2026-08-18                     |
| Depends on  | S-034, S-035, S-037, S-038, S-041 |
| Depended by | (none yet)                     |

## 1. Overview

Solar treats each host as one aggregate VRAM pool: the host sums every GPU
into a single used/total number, placement checks that one number, and the
backend process is launched without any device selection. On a multi-GPU
host this is wrong in two directions. A host can look "has VRAM" while the
only free capacity sits on a specific device nobody told the backend about —
the process defaults to GPU 0, contends with whatever is already there, or
OOMs at load. And a host can be genuinely under-utilized — e.g. ai04
(3x RTX PRO 6000) — with no mechanism to land a new instance on the free
card, because "free" is only knowable per device.

This specification makes Solar **GPU-aware**: hosts report per-GPU
telemetry, intents declare *how many* GPUs they need, and solar-control
picks the exact physical devices while solar-host enforces the choice at
launch. The user never writes a physical device id.

Two principles shape the design:

- **Intents declare the what, not the how.** An intent says `gpu_count: 2`,
  never `devices: "0,1"`. Which physical GPUs serve the intent is a
  placement decision owned by solar-control, like host selection already is.
  Backend device flags (`--main-gpu`, `--tensor-split`, `--tp-size`) keep
  their meaning, but as positions *within the scheduler-chosen set*.
- **One placement policy, two enforcement layers.** Placement lives in
  solar-control's shared `placement` module (already the single policy for
  the reconciler and the S-038 reservation coordinator). solar-host enforces
  the chosen devices via `CUDA_VISIBLE_DEVICES` and re-verifies them at
  spawn. The host never decides placement; the control never touches the
  process environment directly.

## 2. Current state

- **Host telemetry is aggregate.** `memory_monitor.py` `_get_nvidia_memory()`
  sums `nvmlDeviceGetMemoryInfo` across every device into `used_gb` /
  `total_gb` / `available_gb`; `detect_gpu_type()` only asks whether any
  device exists. Nothing per-device crosses the wire in `/resources` or the
  WS push.
- **Placement is aggregate.** `placement.fits_resources()` compares
  `resources.vram_gb` against `snapshot.vram_available_gb`; `find_candidates()`
  ranks by most free aggregate VRAM (§8.4 of deployment-intent.md).
- **Backends already carry device knobs, unenforced.** llama.cpp exposes
  `devices`, `split_mode`, `tensor_split`, `main_gpu`
  (`backends/llamacpp.py` `_multi_gpu_args` → `--device`/`--split-mode`/
  `--tensor-split`/`--main-gpu`); sglang exposes `tp_size`; HuggingFace
  exposes `--device`. All of these are raw physical indices or CUDA-visible
  defaults. Nothing sets `CUDA_VISIBLE_DEVICES`, so a process sees every GPU
  and picks device 0 implicitly.
- **Reservations are aggregate.** Both `ReservationRequest` models
  (solar-control `app/models/reservation.py`, solar-host
  `resources/models.py`) carry `vram_gb` as a single number; the host
  `ResourceManager` accounts for one host-wide pool; `CapacityExceededError`
  names only a dimension, not a device.
- **Cold-start reservations are control-side only.** The reconciler
  reserves the intent's `vram_gb` on the selected host before the model pull
  (per-intent Redis hash) and releases it once the instance runs. The host
  never sees this claim; nothing accounts for it per device.

## 3. Design decisions

The following decisions were made up front (Commander, 2026-08-18) and are
not open for renegotiation during implementation:

- **D1 – control picks the physical GPUs.** Placement selects a device set
  per instance; the host enforces it. Intents never contain physical device
  ids.
- **D2 – reservations are GPU-aware too.** Training reservations (S-034
  /S-038/S-035 headroom semantics) and inference cold-start reservations both
  carry a GPU dimension. One resource model, no special cases.
- **D3 – `vram_gb` is a per-GPU footprint.** An intent requesting
  `vram_gb: 30, gpu_count: 2` needs 30 GB free on each of two devices
  (60 GB total). With the default `gpu_count: 1` this is identical to
  today's semantics, so single-GPU intents are untouched.
- **D4 – multi-GPU sets are constrained by capacity fit only.** No
  name/architecture homogeneity requirement. Per-GPU fit naturally forbids
  absurd mixes (a 24 GB card cannot satisfy a 60 GB per-GPU request), and
  capacity-only keeps franken-machine hosts usable. GPU *name* is still
  carried in telemetry for display and future affinity rules.
- **D5 – `CUDA_VISIBLE_DEVICES` is the enforcement lever.** solar-host sets
  it in the child environment for every backend. CUDA renumbers devices in
  the order given, so backend flags keep working unchanged as positions in
  the visible set. One lever, three backends.
- **D6 – graceful degradation.** Hosts without per-GPU telemetry (old
  agents, Mac unified-memory hosts, CPU boxes) report an empty `gpus` list
  and everything falls back to today's aggregate behavior. The fleet keeps
  working during rollout.

## 4. Host telemetry

`memory_monitor.py` gains `get_gpu_list() -> list[GpuInfo]`, the per-device
version of the existing pynvml loop:

```
GpuInfo {
  index: int          # physical device index as reported by pynvml
  name: str           # e.g. "NVIDIA RTX PRO 6000 Blackwell"
  total_gb: float
  used_gb: float      # live nvidia-smi usage (any process)
  available_gb: float # total - used
}
```

The list is included in the `/resources` response and the WS resource push
(`ws_client.py` health/resource payload). Aggregate fields (`vram_total_gb`,
`vram_available_gb`, ...) stay for backward compatibility and for hosts
without a GPU list. The host advertises `gpus: []` when pynvml reports no
devices or the host is Apple/CPU.

## 5. Intent contract

`ResourceRequirements` gains:

```
gpu_count: int = 1   # how many GPUs this intent needs
```

- `vram_gb` is per-GPU (D3).
- Derived counts: when `gpu_count` is not set, validation derives it from
  backend fields: sglang `tp_size`, llama.cpp `devices` list length (and
  `tensor_split` count when present — must match `devices`). When both
  explicit and derived values exist they must agree, else 422.
- Backend device fields become positions in the visible set (D5):
  llama.cpp `devices: "0,1"` on a two-GPU assignment means "both chosen
  GPUs", `main_gpu: 0` means the first chosen GPU; sglang `tp_size` must
  equal `gpu_count`. Physical pinning of a specific device id is no longer
  expressible — that is deliberate and documented in the webui form tooltip.
- HuggingFace backends stay single-GPU (`gpu_count` must be 1).
- Unified-memory hosts (Mac/CPU): `gpu_count` is ignored, the VRAM
  estimate folds into RAM as today.

## 6. Placement

`solar-control/app/services/placement.py` gains per-GPU selection while
keeping the existing signature shape so both callers (reconciler and S-038
reservation coordinator) inherit it:

```
find_gpu_assignment(
    snapshot: HostResourceSnapshot,
    gpu_count: int,
    vram_gb: float,
) -> list[int] | None          # physical device indices, or None
```

- A host fits when `gpu_count` distinct devices each have
  `available_gb >= vram_gb`, where per-device availability is:

```
device_available = device.available_gb          # live nvidia-smi free
                 - reserved_headroom(device)    # host-side reservations (S-034)
                 - claims(device)               # control-side cold-start reservations
```

  The two ledgers (host-side reservation state, control-side Redis claim
  hashes) both count, so concurrent intents cannot double-book one device.
- Assignment preference: devices with the most free memory first (least
  fragmentation); tie-break by lowest physical index for determinism.
- `fits_resources` and `find_candidates` keep their aggregate fast-path for
  hosts without a `gpus` list (D6). Hosts *with* a list use the per-GPU
  path exclusively.
- The reconciler's per-host one-replica rule is unchanged.

## 7. Reservation and lifecycle

### 7.1 Cold-start reservation (inference)

The reconciler's per-intent Redis reservation gains `gpu_ids`:
`{host_id, gpu_ids, vram_gb, ram_gb, ...}`. Placement picks host + device
set in one step; the claim holds the specific devices through the model
pull. On instance create the same `gpu_ids` travel to the host; on release
(stop/delete/scale-to-zero) the claim clears and the devices return to the
pool.

### 7.2 Host-side reservations (training, S-034)

Both `ReservationRequest` models gain optional `gpu_count` (default 1) and
the host's `ResourceManager` accounts per device:

- The S-038 coordinator passes `gpu_ids` chosen by `find_gpu_assignment` in
  the reservation payload.
- `CapacityExceededError` gains the failing device index in its message.
- Pending reservations hold per-device headroom; running ones report
  `actual_vram_gb` per device.
- Existing callers that omit `gpu_count` behave exactly as today
  (one-device aggregate reservation), preserving backward compatibility
  with older SuperNova/step callers.

### 7.3 Migration, evacuation, displacement

Migration targets re-run `find_gpu_assignment` on the target host and carry
the new `gpu_ids` through `execute_migration` / `execute_evacuation`
(S-037/S-057 paths). A target that cannot fit the device set is treated
exactly like a target that cannot fit VRAM today — the migration stalls and
reports why.

### 7.4 Instance state

The host stores `gpu_ids` on the instance record, exposes them in
`HostInstanceSummary` and the WS `instances_update` payload, and includes
them in the storage manifest per-instance rows.

## 8. Enforcement and races

- **Environment injection.** Each backend runner's `build_env` sets
  `CUDA_VISIBLE_DEVICES` to the comma-joined physical indices in the chosen
  order (sglang: `backends/sglang.py::build_env`; llama.cpp and HuggingFace:
  their existing `build_env` equivalents). The env-dict pattern already used
  for `VIRTUAL_ENV`/`PATH`/`HICACHE_STORAGE_DIR_ENV` is reused verbatim.
- **Spawn-time re-verification.** Immediately before starting the child
  process, solar-host re-checks `nvmlDeviceGetMemoryInfo` on each chosen
  device against `vram_gb`. If a foreign process took the memory in the
  window between placement and launch, the start fails fast with a clear
  error naming the device; the reconciler's existing retry/backoff re-places
  on the next tick. This is the last-line guard against TOCTOU and against
  anything that bypassed the ledgers (manual processes, stray CUDA jobs).
- **Ledger integrity.** Control-side claims are the booking authority for
  intents; host-side reservations for training. Both are released
  idempotently (existing release sweeps cover control; host TTL/cleanup
  covers the rest).

## 9. API and contract changes

- solar-host `GET /resources`: add `gpus: [...]`.
- WS health/resource push: add `gpus: [...]`.
- solar-control `HostResourceSnapshot`: add `gpus: list[GpuInfo]`.
- Intent `ResourceRequirements`: add `gpu_count` (validation + webui mirror).
- `ReservationRequest` (both sides): add `gpu_count`; reservation responses
  (`ReservationView`, `HostReservationSummary`) carry `gpu_ids` when set.
- Instance create payload control → host: carry `gpu_ids`.
- `HostInstanceSummary` + storage manifest: carry `gpu_ids`.

## 10. WebUI (minimal)

- Resources page: per-GPU cards showing name, total/used/free per device
  (from `HostResourceSnapshot.gpus`).
- Intent form: `gpu_count` number input next to `vram_gb`; tooltip noting
  that physical device selection is automatic; llama.cpp multi-GPU fields
  remain and are interpreted as positions within the chosen set.
- Instance/storage rows: display assigned devices (`GPU 0, GPU 2`).

## 11. Amendments to existing docs

- `docs/specs/deployment-intent.md` §4.6 (ResourceRequirements) gains
  `gpu_count` and the per-GPU `vram_gb` semantics; §8.4 (placement policy)
  gains the per-GPU selection and ranking rules.
- S-034/S-035 resource accounting docs gain the per-device `available`
  formula (Section 6 above).
- `AGENTS.md` references list gains this spec.

## 12. Testing and verification

- **Unit (solar-control):** `find_gpu_assignment` bin-packing (fits,
  fragmentation ranking, count unmet, empty gpus fallback); validation of
  derived `gpu_count` vs explicit (sglang `tp_size`, llama.cpp `devices`);
  unified-memory folding; double-booking prevention across two concurrent
  claims.
- **Unit (solar-host):** `CUDA_VISIBLE_DEVICES` injection in all three
  backend envs; spawn-time re-verify fail-fast when a device went busy;
  reservation per-device headroom math; `CapacityExceededError` naming the
  device.
- **Integration:** extend the stub host to report a 3-GPU list with device 2
  free; assert an intent with `gpu_count: 1` lands on device 2, spawns with
  `CUDA_VISIBLE_DEVICES=2`, and reports `gpu_ids` to the WS payload; a
  training reservation through the S-038 coordinator holds the chosen
  devices; migration carries `gpu_ids` to the target.
- **Spike (before implementation):** on ai04 (3x RTX PRO 6000), confirm
  llama.cpp honors `CUDA_VISIBLE_DEVICES` for `--device`/`--main-gpu`
  ordering and sglang TP serves correctly from a reordered subset
  (`CUDA_VISIBLE_DEVICES=2,0` with `tp_size=2`). This validates D5 on real
  hardware before any code is written around it.

## 13. Rollout

solar-host and solar-control ship together. During the transition, hosts
without a `gpus` list use the aggregate path (D6), so nothing regresses.
The intent `gpu_count` field is optional; existing intents default to 1 and
behave identically.

## 14. Out of scope / future

- GPU *name*/architecture affinity constraints (D4 defers these; telemetry
  carries the data).
- MIG slicing / per-GPU quotas.
- NUMA or PCIe topology awareness.
- Explicit device pinning by the user (deliberately removed by D1).