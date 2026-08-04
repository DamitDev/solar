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
