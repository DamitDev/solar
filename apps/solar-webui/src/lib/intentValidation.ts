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
export const FORBIDDEN_BACKEND_FIELDS = ['alias', 'model_source', 'host', 'port', 'api_key'];

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
