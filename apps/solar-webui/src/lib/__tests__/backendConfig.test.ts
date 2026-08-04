import { describe, expect, it } from 'vitest';
import { DEVICE_OPTIONS, DTYPE_OPTIONS, getBackendTypeFromSelection, getDefaultConfig } from '@/lib/backendConfig';

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

  it('returns the huggingface shape for huggingface backends', () => {
    const cfg = getDefaultConfig('huggingface', 'classifier', true);
    expect(cfg.backend_type).toBe('huggingface_classification');
  });
});

describe('option constants', () => {
  it('exposes device and dtype options', () => {
    expect(DEVICE_OPTIONS).toEqual(['auto', 'cuda', 'mps', 'cpu']);
    expect(DTYPE_OPTIONS).toContain('bfloat16');
  });
});
