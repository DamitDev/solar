/**
 * Model display helpers for the hosts table.
 *
 * Every backend stores its model reference under a different key: llama.cpp and
 * the two GGML speech runtimes use `model`, SGLang/vLLM use `model_path`, and
 * HuggingFace uses `model_id`. Resolve all of them, and return a string for an
 * unknown backend too, so a new config shape cannot crash the table.
 */

import { InstanceConfig, isLlamaCppConfig, isSglangConfig, isVllmConfig } from '@/api/types';

export function getModelDisplay(config: InstanceConfig): string {
  if (isLlamaCppConfig(config)) {
    return config.model ?? '';
  }
  if (isSglangConfig(config) || isVllmConfig(config)) {
    return config.model_path ?? '';
  }
  const model = (config as { model?: unknown }).model;
  if (typeof model === 'string') return model;
  const modelId = (config as { model_id?: unknown }).model_id;
  return typeof modelId === 'string' ? modelId : '';
}

/** Truncate long model paths. Non-strings are empty, never a crash. */
export function truncateModel(model: unknown, maxLen = 40): string {
  if (typeof model !== 'string' || model.length === 0) return '';
  if (model.length <= maxLen) return model;
  if (model.includes('/')) {
    const parts = model.split('/');
    const filename = parts[parts.length - 1];
    if (filename.length <= maxLen - 4) {
      return '.../' + filename;
    }
    return '...' + filename.slice(-(maxLen - 3));
  }
  return model.slice(0, maxLen - 3) + '...';
}
