import { describe, expect, it } from 'vitest';
import {
  DEVICE_OPTIONS,
  DTYPE_OPTIONS,
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
});

describe('option constants', () => {
  it('exposes device and dtype options', () => {
    expect(DEVICE_OPTIONS).toEqual(['auto', 'cuda', 'mps', 'cpu']);
    expect(DTYPE_OPTIONS).toContain('bfloat16');
  });
});
