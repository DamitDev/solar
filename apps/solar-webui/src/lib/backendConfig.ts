/**
 * Backend editor constants and helpers shared by AddInstanceModal and the
 * intent form (U-003). Lives outside the component file so react-refresh
 * fast-refresh keeps working for BackendConfigFields.
 */

import { Cpu, MessageSquare, Binary, Tags, Search } from 'lucide-react';
import { BackendType } from '@/api/types';

export type PrimaryBackend = 'llamacpp' | 'huggingface';

export type LlamaCppMode = 'llm' | 'embedding' | 'reranker';
export type HuggingFaceMode = 'causal' | 'classifier' | 'embedding';

export type ModeOption = {
  value: string;
  label: string;
  icon: typeof Cpu;
  description: string;
};

export const LLAMACPP_MODES: ModeOption[] = [
  { value: 'llm', label: 'Text Generation', icon: MessageSquare, description: 'Chat & text completion' },
  { value: 'embedding', label: 'Embedding', icon: Binary, description: 'Vector embeddings' },
  { value: 'reranker', label: 'Reranker', icon: Search, description: 'Document reranking' },
];

export const HUGGINGFACE_MODES: ModeOption[] = [
  { value: 'causal', label: 'Causal LM', icon: MessageSquare, description: 'Text generation models' },
  { value: 'classifier', label: 'Classifier', icon: Tags, description: 'Sequence classification' },
  { value: 'embedding', label: 'Embedding', icon: Binary, description: 'Embedding models' },
];

export const DEVICE_OPTIONS = ['auto', 'cuda', 'mps', 'cpu'];
export const DTYPE_OPTIONS = ['auto', 'float16', 'bfloat16', 'float32'];

// Helper to get BackendType from selections
export const getBackendTypeFromSelection = (primary: PrimaryBackend, mode: string): BackendType => {
  if (primary === 'llamacpp') {
    return 'llamacpp';
  }
  switch (mode) {
    case 'causal':
      return 'huggingface_causal';
    case 'classifier':
      return 'huggingface_classification';
    case 'embedding':
      return 'huggingface_embedding';
    default:
      return 'huggingface_causal';
  }
};

// Default values for each configuration
// `forIntent` drops the fields forbidden in an intent `backend` object
// (alias, model/model_id, host, api_key — spec §4.7 / §6).
export const getDefaultConfig = (primary: PrimaryBackend, mode: string, forIntent = false): Record<string, any> => {
  const base = forIntent ? {} : { host: '0.0.0.0', api_key: 'aiops' };

  if (primary === 'llamacpp') {
    return {
      ...base,
      backend_type: 'llamacpp',
      ...(forIntent ? {} : { model: '' }),
      mmproj: '',
      ...(forIntent ? {} : { alias: '' }),
      threads: 1,
      n_gpu_layers: 999,
      temp: 1,
      top_p: 1,
      top_k: 0,
      min_p: 0,
      ctx_size: 131072,
      chat_template_file: '',
      chat_template_kwargs: '',
      reasoning_budget: undefined,
      cache_type_k: '',
      cache_type_v: '',
      rope_scaling: '',
      rope_scale: undefined,
      yarn_orig_ctx: undefined,
      special: false,
      ot: '',
      model_type: mode as LlamaCppMode,
      pooling: undefined,
    };
  }

  // HuggingFace modes
  switch (mode) {
    case 'causal':
      return {
        ...base,
        backend_type: 'huggingface_causal',
        ...(forIntent ? {} : { model_id: '' }),
        ...(forIntent ? {} : { alias: '' }),
        device: 'auto',
        dtype: 'auto',
        max_length: 4096,
        trust_remote_code: false,
        use_flash_attention: false,
      };
    case 'classifier':
      return {
        ...base,
        backend_type: 'huggingface_classification',
        ...(forIntent ? {} : { model_id: '' }),
        ...(forIntent ? {} : { alias: '' }),
        device: 'auto',
        dtype: 'auto',
        max_length: 512,
        labels: [],
        trust_remote_code: false,
      };
    case 'embedding':
      return {
        ...base,
        backend_type: 'huggingface_embedding',
        ...(forIntent ? {} : { model_id: '' }),
        ...(forIntent ? {} : { alias: '' }),
        device: 'auto',
        dtype: 'auto',
        max_length: 512,
        normalize_embeddings: true,
        trust_remote_code: false,
      };
    default:
      return base;
  }
};

/**
 * Strip empty-string optional llama.cpp fields so the backend receives None
 * and llama-server uses its own defaults (shared by AddInstanceModal and the
 * intent form).
 */
export const stripEmptyOptionalFields = (config: Record<string, any>): Record<string, any> => {
  const next = { ...config };
  for (const field of [
    'cache_type_k',
    'cache_type_v',
    'rope_scaling',
    'chat_template_file',
    'chat_template_kwargs',
    'reasoning',
    'ot',
    'mmproj',
    'pooling',
  ]) {
    if (!next[field]) delete next[field];
  }
  if (!next.spec_type) delete next.spec_draft_n_max;
  return next;
};
