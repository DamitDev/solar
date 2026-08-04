import { describe, expect, it } from 'vitest';
import { FORBIDDEN_BACKEND_FIELDS, validateIntentRequest, sanitizeIntentBackend } from '@/lib/intentValidation';

const validRequest = {
  alias: 'my-model',
  model_source: 'repo://my-model:v1',
  replicas: 1,
  priority: 'production',
  strategy: 'rolling',
  backend: { backend_type: 'huggingface_classification' },
};

function errorsFor(overrides: Record<string, unknown>) {
  return validateIntentRequest({ ...validRequest, ...overrides } as any);
}

function fieldNames(overrides: Record<string, unknown>) {
  return errorsFor(overrides).map((e) => e.field);
}

describe('validateIntentRequest', () => {
  it('accepts a fully valid request', () => {
    expect(errorsFor({})).toEqual([]);
  });

  it('requires an alias', () => {
    expect(fieldNames({ alias: '' })).toContain('alias');
    expect(fieldNames({ alias: '   ' })).toContain('alias');
    expect(fieldNames({ alias: undefined })).toContain('alias');
  });

  describe('model_source', () => {
    it('requires a source', () => {
      expect(fieldNames({ model_source: '' })).toContain('model_source');
      expect(fieldNames({ model_source: undefined })).toContain('model_source');
    });

    it('rejects http(s) sources explicitly', () => {
      expect(fieldNames({ model_source: 'https://example.com/model' })).toContain('model_source');
    });

    it('rejects non-URI sources', () => {
      expect(fieldNames({ model_source: 'repo:my-model' })).toContain('model_source');
      expect(fieldNames({ model_source: 'my-model' })).toContain('model_source');
    });

    it('accepts repo://, huggingface:// and local://', () => {
      for (const source of ['repo://m:v1', 'huggingface://org/model', 'local:///tmp/model']) {
        expect(fieldNames({ model_source: source })).not.toContain('model_source');
      }
    });
  });

  it('validates replicas as a non-negative integer', () => {
    expect(fieldNames({ replicas: -1 })).toContain('replicas');
    expect(fieldNames({ replicas: 1.5 })).toContain('replicas');
    expect(fieldNames({ replicas: 2 })).not.toContain('replicas');
    expect(fieldNames({ replicas: 0 })).not.toContain('replicas');
  });

  it('validates priority against the enum', () => {
    expect(fieldNames({ priority: 'banana' })).toContain('priority');
    expect(fieldNames({ priority: 'staging' })).not.toContain('priority');
  });

  it('validates strategy against the enum', () => {
    expect(fieldNames({ strategy: 'banana' })).toContain('strategy');
    expect(fieldNames({ strategy: 'immediate' })).not.toContain('strategy');
  });

  describe('backend', () => {
    it('requires a backend object', () => {
      expect(fieldNames({ backend: undefined })).toContain('backend');
      expect(fieldNames({ backend: 'llamacpp' })).toContain('backend');
    });

    it('rejects unsupported backend types', () => {
      expect(fieldNames({ backend: { backend_type: 'quantum' } })).toContain('backend');
    });

    it('rejects server-derived fields inside backend', () => {
      const withForbidden = {
        backend: {
          backend_type: 'llamacpp',
          alias: 'x',
          model_source: 'repo://m:v1',
          host: 'h',
          port: 1,
          api_key: 'k',
        },
      };
      const messages = errorsFor(withForbidden).map((e) => e.message);
      for (const forbidden of FORBIDDEN_BACKEND_FIELDS) {
        expect(messages.some((m) => m.includes(forbidden))).toBe(true);
      }
    });

    it('allows a model file pattern only for llama.cpp', () => {
      expect(fieldNames({ backend: { backend_type: 'llamacpp', model_file: '*Q4*.gguf' } })).not.toContain(
        'backend.model_file',
      );
      expect(fieldNames({ backend: { backend_type: 'huggingface_causal', model_file: '*Q4*.gguf' } })).toContain(
        'backend.model_file',
      );
    });

    it('allows download filters only for huggingface:// sources', () => {
      const backend = { backend_type: 'llamacpp', file_filters: ['*UD-Q4_K_XL*'] };
      expect(fieldNames({ backend, model_source: 'huggingface://unsloth/Model-GGUF' })).not.toContain(
        'backend.file_filters',
      );
      expect(fieldNames({ backend, model_source: 'repo://my-model:v1' })).toContain('backend.file_filters');
      expect(
        fieldNames({ backend: { backend_type: 'llamacpp', file_filters: [] }, model_source: 'repo://my-model:v1' }),
      ).not.toContain('backend.file_filters');
    });

    it('rejects download filters that are not patterns', () => {
      const source = 'huggingface://unsloth/Model-GGUF';
      expect(
        fieldNames({ backend: { backend_type: 'llamacpp', file_filters: '*Q4*' }, model_source: source }),
      ).toContain('backend.file_filters');
      expect(
        fieldNames({ backend: { backend_type: 'llamacpp', file_filters: ['  '] }, model_source: source }),
      ).toContain('backend.file_filters');
    });
  });

  it('rejects empty placement roles', () => {
    expect(fieldNames({ placement: { roles: [] } })).toContain('placement.roles');
    expect(fieldNames({ placement: { roles: ['inference'] } })).not.toContain('placement.roles');
  });
});

describe('sanitizeIntentBackend', () => {
  it('strips every server-derived field and keeps the rest', () => {
    const dirty = {
      backend_type: 'llamacpp',
      alias: 'x',
      model_source: 'repo://m:v1',
      host: '0.0.0.0',
      port: 8080,
      api_key: 'secret',
      threads: 4,
    };
    const cleaned = sanitizeIntentBackend(dirty);
    expect(cleaned).toEqual({ backend_type: 'llamacpp', threads: 4 });
  });
});
