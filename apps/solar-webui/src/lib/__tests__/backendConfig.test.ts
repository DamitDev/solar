import { describe, expect, it } from 'vitest';
import {
  DEVICE_OPTIONS,
  DTYPE_OPTIONS,
  SPLIT_MODE_OPTIONS,
  applySpecType,
  getBackendTypeFromSelection,
  getDefaultConfig,
  stripEmptyOptionalFields,
} from '@/lib/backendConfig';

describe('getBackendTypeFromSelection', () => {
  it('maps llamacpp to llamacpp regardless of mode', () => {
    expect(getBackendTypeFromSelection('llamacpp', 'llm')).toBe('llamacpp');
    expect(getBackendTypeFromSelection('llamacpp', 'embedding')).toBe('llamacpp');
  });

  it('maps huggingface modes to their backend types', () => {
    expect(getBackendTypeFromSelection('huggingface', 'causal')).toBe('huggingface_causal');
    expect(getBackendTypeFromSelection('huggingface', 'classifier')).toBe('huggingface_classification');
    expect(getBackendTypeFromSelection('huggingface', 'embedding')).toBe('huggingface_embedding');
  });

  it('defaults unknown huggingface modes to causal', () => {
    expect(getBackendTypeFromSelection('huggingface', 'quantum')).toBe('huggingface_causal');
  });
});

describe('getDefaultConfig', () => {
  it('includes host/api_key for instance configs', () => {
    const cfg = getDefaultConfig('llamacpp', 'llm');
    expect(cfg.host).toBe('0.0.0.0');
    expect(cfg.api_key).toBe('aiops');
    expect(cfg.backend_type).toBe('llamacpp');
  });

  it('drops server-derived fields for intent configs', () => {
    const cfg = getDefaultConfig('llamacpp', 'llm', true);
    expect(cfg.host).toBeUndefined();
    expect(cfg.api_key).toBeUndefined();
    expect(cfg.alias).toBeUndefined();
    expect(cfg.model).toBeUndefined();
    expect(cfg.backend_type).toBe('llamacpp');
  });

  it('offers a model file selector for intents instead of a model path', () => {
    expect(getDefaultConfig('llamacpp', 'llm', true).model_file).toBe('');
    expect(getDefaultConfig('llamacpp', 'llm').model_file).toBeUndefined();
  });

  it('starts the multi-GPU fields empty so llama.cpp keeps its own defaults', () => {
    const cfg = getDefaultConfig('llamacpp', 'llm', true);
    expect(cfg.devices).toBe('');
    expect(cfg.split_mode).toBe('');
    expect(cfg.tensor_split).toBe('');
    expect(cfg.main_gpu).toBeUndefined();
  });

  it('returns the huggingface shape for huggingface backends', () => {
    const cfg = getDefaultConfig('huggingface', 'classifier', true);
    expect(cfg.backend_type).toBe('huggingface_classification');
  });
});

describe('stripEmptyOptionalFields', () => {
  it('drops an empty model file pattern and keeps a real one', () => {
    expect(stripEmptyOptionalFields({ backend_type: 'llamacpp', model_file: '' })).toEqual({
      backend_type: 'llamacpp',
    });
    expect(stripEmptyOptionalFields({ backend_type: 'llamacpp', model_file: '*Q4*.gguf' }).model_file).toBe(
      '*Q4*.gguf',
    );
  });

  it('drops an empty download filter list and keeps a populated one', () => {
    expect(stripEmptyOptionalFields({ backend_type: 'llamacpp', file_filters: [] })).toEqual({
      backend_type: 'llamacpp',
    });
    expect(stripEmptyOptionalFields({ backend_type: 'llamacpp', file_filters: ['*Q4*'] }).file_filters).toEqual([
      '*Q4*',
    ]);
  });

  it('drops blank multi-GPU fields and keeps the filled ones', () => {
    expect(
      stripEmptyOptionalFields({
        backend_type: 'llamacpp',
        devices: '',
        split_mode: '',
        tensor_split: '3,1',
        main_gpu: 0,
      }),
    ).toEqual({ backend_type: 'llamacpp', tensor_split: '3,1', main_gpu: 0 });
  });

  it('drops every speculative field when no implementation is selected', () => {
    expect(
      stripEmptyOptionalFields({
        backend_type: 'llamacpp',
        spec_draft_n_max: 7,
        spec_draft_model: '*DSpark*.gguf',
        spec_draft_conf_min: 0.4,
      }),
    ).toEqual({ backend_type: 'llamacpp' });
  });

  it('drops the dspark-only fields for draft-mtp', () => {
    expect(
      stripEmptyOptionalFields({
        backend_type: 'llamacpp',
        spec_type: 'draft-mtp',
        spec_draft_n_max: 2,
        spec_draft_model: '*DSpark*.gguf',
        spec_draft_conf_min: 0.4,
      }),
    ).toEqual({ backend_type: 'llamacpp', spec_type: 'draft-mtp', spec_draft_n_max: 2 });
  });

  it('keeps the dspark fields and drops a blank confidence threshold', () => {
    expect(
      stripEmptyOptionalFields({
        backend_type: 'llamacpp',
        spec_type: 'draft-dspark',
        spec_draft_model: '*DSpark*.gguf',
        spec_draft_n_max: 7,
        spec_draft_conf_min: '',
      }),
    ).toEqual({
      backend_type: 'llamacpp',
      spec_type: 'draft-dspark',
      spec_draft_model: '*DSpark*.gguf',
      spec_draft_n_max: 7,
    });
  });

  it('drops speculative decoding for non-generation models', () => {
    expect(
      stripEmptyOptionalFields({
        backend_type: 'llamacpp',
        model_type: 'embedding',
        spec_type: 'draft-dspark',
        spec_draft_model: '*DSpark*.gguf',
      }),
    ).toEqual({ backend_type: 'llamacpp', model_type: 'embedding' });
  });
});

describe('applySpecType', () => {
  it('seeds the block size and a draft model slot for dspark', () => {
    expect(applySpecType({ backend_type: 'llamacpp' }, 'draft-dspark')).toEqual({
      backend_type: 'llamacpp',
      spec_type: 'draft-dspark',
      spec_draft_n_max: 7,
      spec_draft_model: '',
    });
  });

  it('drops the draft model fields when switching to mtp', () => {
    expect(
      applySpecType(
        {
          backend_type: 'llamacpp',
          spec_type: 'draft-dspark',
          spec_draft_model: '*DSpark*.gguf',
          spec_draft_n_max: 7,
          spec_draft_conf_min: 0.4,
        },
        'draft-mtp',
      ),
    ).toEqual({ backend_type: 'llamacpp', spec_type: 'draft-mtp', spec_draft_n_max: 2 });
  });

  it('clears everything when disabled', () => {
    expect(applySpecType({ backend_type: 'llamacpp', spec_type: 'draft-mtp', spec_draft_n_max: 2 }, '')).toEqual({
      backend_type: 'llamacpp',
    });
  });
});

describe('option constants', () => {
  it('exposes device and dtype options', () => {
    expect(DEVICE_OPTIONS).toEqual(['auto', 'cuda', 'mps', 'cpu']);
    expect(DTYPE_OPTIONS).toContain('bfloat16');
  });

  it('exposes every llama.cpp split mode', () => {
    expect(SPLIT_MODE_OPTIONS.map((o) => o.value)).toEqual(['none', 'layer', 'row', 'tensor']);
  });
});
