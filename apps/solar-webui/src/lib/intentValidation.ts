/**
 * Client-side shape validation for intent submission (spec deployment-intent.md §4.7).
 *
 * The server stays authoritative — this mirrors the same rules so the form can
 * surface inline errors before submitting.
 */

import { IntentCreateRequest } from '@/api/types';
import { DEVICE_OPTIONS } from '@/lib/backendConfig';

export interface IntentFieldError {
  field: string;
  message: string;
}

export const INTENT_BACKEND_TYPES = [
  'llamacpp',
  'huggingface_causal',
  'huggingface_classification',
  'huggingface_embedding',
  'huggingface_vision',
  'sglang',
] as const;

export const INTENT_PRIORITIES = ['production', 'staging', 'ephemeral'] as const;
export const INTENT_STRATEGIES = ['rolling', 'immediate'] as const;

/**
 * C3: the canonical accelerator tokens, mirroring the server's
 * VALID_GPU_TYPES. The server stays authoritative and normalizes on save;
 * this exists so the form can reject an unknown token before the round trip.
 */
export const VALID_GPU_TYPES = ['nvidia_cuda', 'apple_mps', 'cpu'] as const;

/**
 * Accepted aliases, mirroring the server's GPU_TYPE_ALIASES. Keys are matched
 * after case-folding and unifying `-`/`_`, so only one spelling is listed.
 */
export const GPU_TYPE_ALIASES: Record<string, string> = {
  nvidia: 'nvidia_cuda',
  cuda: 'nvidia_cuda',
  mps: 'apple_mps',
  metal: 'apple_mps',
  apple: 'apple_mps',
  none: 'cpu',
};

const gpuToken = (value: string) => value.trim().toLowerCase().replace(/-/g, '_');

/** C3: normalize a gpu_type token (alias -> canonical) or return null when unknown. */
export function normalizeGpuType(value: string | null | undefined): string | null {
  if (!value) return null;
  const token = gpuToken(value);
  if (!token) return null;
  if ((VALID_GPU_TYPES as readonly string[]).includes(token)) return token;
  return GPU_TYPE_ALIASES[token] ?? null;
}

/** Fields that must NOT appear inside `backend` — they are server-derived (§4.7). */
export const FORBIDDEN_BACKEND_FIELDS = ['alias', 'model_source', 'host', 'port', 'api_key', 'model_path'];

/** SGLang only runs on CUDA, mirroring the server's SGLANG_GPU_TYPE. */
export const SGLANG_GPU_TYPE = 'nvidia_cuda';

/** CLI flags solar-host derives itself, mirroring RESERVED_SGLANG_ARGS. */
export const RESERVED_SGLANG_ARGS = ['--host', '--port', '--api-key', '--model-path', '--served-model-name'];

/**
 * Backend fields only SGLang accepts, mirroring the server's `_SGLANG_FIELDS`
 * minus `dtype`/`trust_remote_code`, which HuggingFace shares.
 */
export const SGLANG_ONLY_FIELDS = [
  'tp_size',
  'dp_size',
  'context_length',
  'mem_fraction_static',
  'chunked_prefill_size',
  'max_running_requests',
  'cuda_graph_max_bs',
  'cuda_graph_max_bs_decode',
  'swa_full_tokens_ratio',
  'quantization',
  'kv_cache_dtype',
  'moe_runner_backend',
  'speculative_algorithm',
  'enable_hierarchical_cache',
  'hicache_ratio',
  'hicache_mem_layout',
  'hicache_io_backend',
  'hicache_storage_backend',
  'hicache_storage_backend_extra_config',
  'hicache_storage_prefetch_policy',
  'extra_args',
  'extra_env',
];

/**
 * The accelerator each `device` demands, mirroring the server's
 * `_DEVICE_TO_GPU_TYPE`. `auto` and `cpu` are absent because they constrain
 * no placement.
 */
const DEVICE_TO_GPU_TYPE: Record<string, string> = {
  cuda: 'nvidia_cuda',
  mps: 'apple_mps',
};

const MODEL_SOURCE_RE = /^(repo|huggingface|local):\/\//;

/**
 * Backend keys an edit carries over from the stored spec unchanged, mirroring
 * the server's `_unchanged_backend_fields`.
 *
 * The server exempts these from its field-ownership table so that an intent
 * stored before a rule was tightened stays editable — every update replays the
 * full spec, so it would otherwise fail on a field the user never touched.
 * Values are compared with `===`, which is exact for `device`, the only field
 * the client consults this for.
 */
export function unchangedBackendFields(
  backend: Record<string, any> | null | undefined,
  currentBackend: Record<string, any> | null | undefined,
): string[] {
  if (!backend || !currentBackend) return [];
  // A backend_type change re-homes every field, so nothing is grandfathered.
  if (backend.backend_type !== currentBackend.backend_type) return [];
  return Object.keys(backend).filter((key) => key in currentBackend && currentBackend[key] === backend[key]);
}

/**
 * Mirror the server's `_validate_device` (§4.7).
 *
 * `device` is a HuggingFace-only contract — llama.cpp selects its device
 * through `devices`/`n_gpu_layers`/`ot` — and its value must not contradict an
 * explicitly chosen `placement.gpu_type`. Both are hard 422s, so the reported
 * "mps plus an NVIDIA host" case need not cost a round trip. One bad value
 * yields one message, matching the server's early returns.
 */
/**
 * Mirror the server's multi-GPU rules: the split flags belong to llama.cpp,
 * and `tensor_split` must parse as numbers.
 *
 * llama.cpp reads the list with strtod and silently treats an unparseable
 * entry as 0.0 — the whole model then loads onto one GPU instead of failing,
 * so a typo is worth catching in the form.
 */
function validateMultiGpu(backend: Record<string, any>): IntentFieldError[] {
  const errors: IntentFieldError[] = [];
  const isLlamaCpp = backend.backend_type === 'llamacpp';

  for (const field of ['devices', 'split_mode', 'tensor_split', 'main_gpu']) {
    const value = backend[field];
    if (value === undefined || value === null || value === '') continue;
    if (!isLlamaCpp) {
      errors.push({
        field: `backend.${field}`,
        message: `${field} is only supported for the llama.cpp backend`,
      });
    }
  }

  const tensorSplit = backend.tensor_split;
  if (isLlamaCpp && typeof tensorSplit === 'string' && tensorSplit.trim()) {
    for (const part of tensorSplit.split(',')) {
      const trimmed = part.trim();
      if (!trimmed) continue;
      const proportion = Number(trimmed);
      if (!Number.isFinite(proportion)) {
        errors.push({
          field: 'backend.tensor_split',
          message: `tensor_split must be comma-separated numbers, got '${trimmed}'`,
        });
        break;
      }
      if (proportion < 0) {
        errors.push({
          field: 'backend.tensor_split',
          message: 'tensor_split proportions must not be negative',
        });
        break;
      }
    }
  }

  return errors;
}

/**
 * Mirror the server's `_validate_sglang`: field ownership, the NVIDIA-only
 * placement contract, and the `extra_args`/`extra_env` escape hatches.
 *
 * The form pins `placement.gpu_type` for SGLang, so a contradiction here means
 * the user changed the backend after the fact — worth saying plainly rather
 * than as a 422 from the server.
 */
function validateSglang(
  backend: Record<string, any>,
  placement: IntentCreateRequest['placement'],
  unchanged: readonly string[],
): IntentFieldError[] {
  const errors: IntentFieldError[] = [];
  const isSglang = backend.backend_type === 'sglang';

  if (!isSglang) {
    for (const field of SGLANG_ONLY_FIELDS) {
      const value = backend[field];
      if (value === undefined || value === null || value === '' || value === false) continue;
      if (Array.isArray(value) && value.length === 0) continue;
      if (unchanged.includes(field)) continue;
      errors.push({ field: `backend.${field}`, message: `${field} is only supported for the sglang backend` });
    }
    return errors;
  }

  const gpuType = placement?.gpu_type;
  const canonical = gpuType ? (normalizeGpuType(gpuType) ?? gpuType) : null;
  if (canonical && canonical !== SGLANG_GPU_TYPE) {
    errors.push({
      field: 'placement.gpu_type',
      message: `the sglang backend requires gpu_type '${SGLANG_GPU_TYPE}', but placement.gpu_type is '${canonical}'`,
    });
  }

  const extraArgs = backend.extra_args;
  if (extraArgs !== undefined && extraArgs !== null) {
    if (!Array.isArray(extraArgs) || extraArgs.some((a) => typeof a !== 'string' || !a.trim())) {
      errors.push({ field: 'backend.extra_args', message: 'Every extra argument must be a non-empty string' });
    } else {
      for (const arg of extraArgs) {
        const flag = arg.trim().split('=')[0];
        if (RESERVED_SGLANG_ARGS.includes(flag)) {
          errors.push({
            field: 'backend.extra_args',
            message: `'${flag}' is managed by solar-host and must not be set through extra args`,
          });
        }
      }
    }
  }

  const extraEnv = backend.extra_env;
  if (extraEnv !== undefined && extraEnv !== null) {
    const isFlatMap =
      typeof extraEnv === 'object' &&
      !Array.isArray(extraEnv) &&
      Object.entries(extraEnv).every(([k, v]) => k.trim() && typeof v === 'string');
    if (!isFlatMap) {
      errors.push({ field: 'backend.extra_env', message: 'Every environment entry must be NAME=value with a name' });
    }
  }

  return errors;
}

function validateDevice(
  backend: Record<string, any>,
  placement: IntentCreateRequest['placement'],
  unchanged: readonly string[],
): IntentFieldError[] {
  const device = backend.device;
  if (device === undefined || device === null) return [];

  if (backend.backend_type === 'llamacpp') {
    // The server grandfathers a device an edit carried over untouched, so
    // flagging it here would block a save the server would have accepted.
    if (unchanged.includes('device')) return [];
    return [
      {
        field: 'backend.device',
        message:
          'device is only supported for huggingface_* backends; llama.cpp device selection is devices/n_gpu_layers/ot',
      },
    ];
  }

  if (!DEVICE_OPTIONS.includes(device)) {
    return [
      {
        field: 'backend.device',
        message: `'${device}' is not a valid device. Must be one of: ${[...DEVICE_OPTIONS].sort().join(', ')}`,
      },
    ];
  }

  // The server canonicalizes placement before it runs this check, so an alias
  // spelling such as gpu_type 'mps' has to be resolved here too — comparing
  // the raw token would let the contradiction through.
  const gpuType = placement?.gpu_type;
  const canonical = gpuType ? (normalizeGpuType(gpuType) ?? gpuType) : null;
  const required = DEVICE_TO_GPU_TYPE[device];
  if (required && canonical && canonical !== required) {
    return [
      {
        field: 'backend.device',
        message: `device '${device}' requires gpu_type '${required}', but placement.gpu_type is '${canonical}'`,
      },
    ];
  }
  return [];
}

/**
 * S-058: mirror the server's `derive_gpu_count` — the GPU count the
 * backend implies: sglang `tp_size`; llama.cpp `devices` list length or
 * the `tensor_split` count when devices is absent.
 */
function deriveGpuCount(backend: Record<string, any>): number | null {
  if (!backend || typeof backend !== 'object') return null;
  const backendType = backend.backend_type;
  if (backendType === 'sglang') {
    const tpSize = backend.tp_size;
    if (Number.isInteger(tpSize) && tpSize >= 1) return tpSize;
    return null;
  }
  if (backendType === 'llamacpp') {
    const devices = backend.devices;
    if (typeof devices === 'string' && devices.trim()) {
      const parts = devices.split(',').filter((p) => p.trim());
      if (parts.length > 0) return parts.length;
    }
    const tensorSplit = backend.tensor_split;
    if (typeof tensorSplit === 'string' && tensorSplit.trim()) {
      const parts = tensorSplit.split(',').filter((p) => p.trim());
      if (parts.length > 0) return parts.length;
    }
  }
  return null;
}

/**
 * Device-name suffix of a llama.cpp `devices` entry ('CUDA0' → suffix 0).
 * Entries without a numeric suffix ('none', and anything non-CUDA) do not
 * match and stay legal — they never index a visible device.
 */
const DEVICE_NAME_RE = /^([A-Za-z]+)(\d+)$/;

/**
 * S-058: mirror the server's `_validate_gpu_device_positions`.
 *
 * After `CUDA_VISIBLE_DEVICES` the child process only ever sees
 * CUDA0..CUDA(n-1), so a `devices` entry or `main_gpu` at or beyond the
 * resolved `gpu_count` names a device that does not exist in the process.
 * Fields an edit carries over unchanged are skipped via `unchanged`,
 * matching the server's grandfathering.
 */
function validateGpuDevicePositions(
  backend: Record<string, any>,
  resolvedGpuCount: number,
  unchanged: readonly string[],
): IntentFieldError[] {
  const errors: IntentFieldError[] = [];
  if (backend?.backend_type !== 'llamacpp' || resolvedGpuCount < 1) return errors;
  const upper = resolvedGpuCount - 1;

  const devices = backend.devices;
  if (typeof devices === 'string' && devices.trim() && !unchanged.includes('devices')) {
    for (const part of devices.split(',')) {
      const trimmed = part.trim();
      if (!trimmed) continue;
      const m = DEVICE_NAME_RE.exec(trimmed);
      if (!m) continue; // 'none' and other non-CUDA tokens stay legal
      if (Number(m[2]) > upper) {
        errors.push({
          field: 'backend.devices',
          message: `devices entry '${trimmed}' is out of range — device flags are positions within the chosen set (0..${upper}); physical ids are not selectable`,
        });
        break;
      }
    }
  }

  const mainGpu = backend.main_gpu;
  if (Number.isInteger(mainGpu) && mainGpu > upper && !unchanged.includes('main_gpu')) {
    errors.push({
      field: 'backend.main_gpu',
      message: `main_gpu ${mainGpu} is out of range — device flags are positions within the chosen set (0..${upper}); physical ids are not selectable`,
    });
  }

  return errors;
}

export function validateIntentRequest(
  req: IntentCreateRequest,
  unchangedFields: readonly string[] = [],
): IntentFieldError[] {
  const errors: IntentFieldError[] = [];

  if (!req.alias || !req.alias.trim()) {
    errors.push({ field: 'alias', message: 'Alias is required' });
  }

  const source = req.model_source || '';
  if (!source) {
    errors.push({ field: 'model_source', message: 'Model source is required' });
  } else if (/^https?:\/\//i.test(source)) {
    errors.push({ field: 'model_source', message: 'Unsupported scheme — use repo://, huggingface:// or local://' });
  } else if (!MODEL_SOURCE_RE.test(source)) {
    errors.push({
      field: 'model_source',
      message: 'Model source must be a repo://, huggingface:// or local:// URI',
    });
  }

  if (req.replicas !== undefined) {
    if (!Number.isInteger(req.replicas) || req.replicas < 0) {
      errors.push({ field: 'replicas', message: 'Replicas must be an integer >= 0' });
    }
  }

  if (req.priority !== undefined && !(INTENT_PRIORITIES as readonly string[]).includes(req.priority)) {
    errors.push({ field: 'priority', message: 'Priority must be production, staging or ephemeral' });
  }

  if (req.strategy !== undefined && !(INTENT_STRATEGIES as readonly string[]).includes(req.strategy)) {
    errors.push({ field: 'strategy', message: 'Strategy must be rolling or immediate' });
  }

  if (!req.backend || typeof req.backend !== 'object') {
    errors.push({ field: 'backend', message: 'Backend configuration is required' });
  } else {
    const backendType = req.backend.backend_type;
    if (!(INTENT_BACKEND_TYPES as readonly string[]).includes(backendType)) {
      errors.push({ field: 'backend', message: `Unsupported backend type '${backendType ?? ''}'` });
    }
    for (const forbidden of FORBIDDEN_BACKEND_FIELDS) {
      if (forbidden in req.backend) {
        errors.push({
          field: 'backend',
          message: `Field '${forbidden}' is not allowed in backend — it is derived from the intent`,
        });
      }
    }

    const modelFile = req.backend.model_file;
    if (modelFile && backendType !== 'llamacpp') {
      errors.push({
        field: 'backend.model_file',
        message: 'Model file selection is only available for the llama.cpp backend',
      });
    }

    const specType = req.backend.spec_type;
    const draftModel = req.backend.spec_draft_model;
    if (specType && backendType !== 'llamacpp') {
      errors.push({
        field: 'backend.spec_type',
        message: 'Speculative decoding is only available for the llama.cpp backend',
      });
    } else if (specType === 'draft-dspark' && (typeof draftModel !== 'string' || !draftModel.trim())) {
      errors.push({
        field: 'backend.spec_draft_model',
        message: 'DSpark speculative decoding needs a draft model — give a filename, path or glob',
      });
    }

    const filters = req.backend.file_filters;
    if (filters !== undefined && filters !== null) {
      if (!Array.isArray(filters) || filters.some((f) => typeof f !== 'string' || !f.trim())) {
        errors.push({ field: 'backend.file_filters', message: 'Every download filter must be a non-empty pattern' });
      } else if (filters.length > 0 && !source.startsWith('huggingface://')) {
        errors.push({
          field: 'backend.file_filters',
          message: 'Download filters only apply to huggingface:// model sources',
        });
      }
    }

    errors.push(...validateMultiGpu(req.backend));
    errors.push(...validateDevice(req.backend, req.placement, unchangedFields));
    errors.push(...validateSglang(req.backend, req.placement, unchangedFields));
  }

  if (req.placement?.roles !== undefined && req.placement.roles.length === 0) {
    errors.push({ field: 'placement.roles', message: 'Placement roles must not be empty' });
  }

  // C3: a gpu_type the server does not know is a 422; catching it here names
  // the accepted values instead of leaving the user to guess from a rejection.
  const gpuType = req.placement?.gpu_type;
  if (gpuType && normalizeGpuType(gpuType) === null) {
    errors.push({
      field: 'placement.gpu_type',
      message: `Unknown GPU type '${gpuType}' — use one of: ${VALID_GPU_TYPES.join(', ')}`,
    });
  }

  const allow = req.placement?.host_allow ?? [];
  const deny = req.placement?.host_deny ?? [];
  const contradictory = allow.filter((h) => deny.includes(h));
  if (contradictory.length > 0) {
    errors.push({
      field: 'placement.host_allow',
      message: `Host${contradictory.length > 1 ? 's' : ''} both allowed and denied: ${contradictory.join(', ')}`,
    });
  }

  // S-058: resources.gpu_count mirrors the server's `_validate_gpu_count`:
  // an integer >= 1 that must agree with the backend-derived count;
  // HuggingFace backends stay single-GPU.
  const resources = req.resources ?? {};
  const explicitGpuCount = resources.gpu_count;
  let gpuCountError = false;
  if (explicitGpuCount !== undefined && explicitGpuCount !== null) {
    if (!Number.isInteger(explicitGpuCount) || explicitGpuCount < 1) {
      errors.push({ field: 'resources.gpu_count', message: 'gpu_count must be an integer >= 1' });
      gpuCountError = true;
    } else {
      const derived = deriveGpuCount(req.backend);
      if (derived !== null && explicitGpuCount !== derived) {
        const backendType = req.backend?.backend_type;
        const hint =
          backendType === 'sglang' ? 'backend.tp_size' : backendType === 'llamacpp' ? 'backend.devices' : 'backend';
        errors.push({
          field: 'resources.gpu_count',
          message: `resources.gpu_count is ${explicitGpuCount} but ${hint} implies ${derived} — they must agree`,
        });
        // The server skips the position check on any gpu_count error —
        // an unresolved/contested range makes the check meaningless.
        gpuCountError = true;
      }
      if (String(req.backend?.backend_type ?? '').startsWith('huggingface') && explicitGpuCount > 1) {
        errors.push({
          field: 'resources.gpu_count',
          message: 'HuggingFace backends stay single-GPU — gpu_count must be 1',
        });
      }
    }
  }

  // S-058: llama.cpp device flags are positions within the chosen set —
  // mirror the server's `_validate_gpu_device_positions`, which skips the
  // check when gpu_count validation already failed (unknown range).
  if (!gpuCountError && req.backend) {
    errors.push(
      ...validateGpuDevicePositions(req.backend, explicitGpuCount ?? deriveGpuCount(req.backend) ?? 1, unchangedFields),
    );
  }

  return errors;
}

/** Defensively strip forbidden server-derived fields before submit (§4.7). */
export function sanitizeIntentBackend(backend: Record<string, any>): Record<string, any> {
  const cleaned: Record<string, any> = { ...backend };
  for (const forbidden of FORBIDDEN_BACKEND_FIELDS) {
    delete cleaned[forbidden];
  }
  return cleaned;
}
