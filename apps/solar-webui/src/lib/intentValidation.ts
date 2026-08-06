/**
 * Client-side shape validation for intent submission (spec deployment-intent.md §4.7).
 *
 * The server stays authoritative — this mirrors the same rules so the form can
 * surface inline errors before submitting.
 */

import { IntentCreateRequest } from '@/api/types';

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
 * C3: accelerator vocabulary accepted on intent placement, with aliases.
 * Mirrors the server's VALID_GPU_TYPES table — the server stays
 * authoritative and normalizes to the canonical token on save.
 */
export const GPU_TYPES: Record<string, string> = {
  auto: 'auto',
  cpu: 'cpu',
  mps: 'apple_mps',
  apple_mps: 'apple_mps',
  cuda: 'nvidia_cuda',
  nvidia_cuda: 'nvidia_cuda',
  NVIDIA: 'nvidia_cuda',
  'NVIDIA-CUDA': 'nvidia_cuda',
  rocm: 'amd_rocm',
  amd_rocm: 'amd_rocm',
};

/** C3: normalize a gpu_type token (alias -> canonical) or return null when unknown. */
export function normalizeGpuType(value: string | null | undefined): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  return GPU_TYPES[trimmed] ?? null;
}

/** Fields that must NOT appear inside `backend` — they are server-derived (§4.7). */
export const FORBIDDEN_BACKEND_FIELDS = ['alias', 'model_source', 'host', 'port', 'api_key'];

const MODEL_SOURCE_RE = /^(repo|huggingface|local):\/\//;

export function validateIntentRequest(req: IntentCreateRequest): IntentFieldError[] {
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
  }

  if (req.placement?.roles !== undefined && req.placement.roles.length === 0) {
    errors.push({ field: 'placement.roles', message: 'Placement roles must not be empty' });
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
