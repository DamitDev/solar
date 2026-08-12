import { describe, expect, it } from 'vitest';
import {
  FORBIDDEN_BACKEND_FIELDS,
  normalizeGpuType,
  unchangedBackendFields,
  validateIntentRequest,
  sanitizeIntentBackend,
} from '@/lib/intentValidation';

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

    it('requires a draft model for dspark speculative decoding', () => {
      expect(fieldNames({ backend: { backend_type: 'llamacpp', spec_type: 'draft-dspark' } })).toContain(
        'backend.spec_draft_model',
      );
      expect(
        fieldNames({
          backend: { backend_type: 'llamacpp', spec_type: 'draft-dspark', spec_draft_model: '*DSpark*.gguf' },
        }),
      ).not.toContain('backend.spec_draft_model');
    });

    it('does not ask draft-mtp for a draft model', () => {
      expect(
        fieldNames({ backend: { backend_type: 'llamacpp', spec_type: 'draft-mtp', spec_draft_n_max: 2 } }),
      ).not.toContain('backend.spec_draft_model');
    });

    it('allows speculative decoding only for llama.cpp', () => {
      expect(fieldNames({ backend: { backend_type: 'huggingface_causal', spec_type: 'draft-mtp' } })).toContain(
        'backend.spec_type',
      );
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

    // Mirrors _validate_device in app/validation.py — every case here is a
    // hard 422 server-side, so the round trip buys the user nothing.
    describe('device', () => {
      const hf = (device: unknown) => ({ backend_type: 'huggingface_causal', device });

      it('rejects a value outside the device vocabulary', () => {
        const errors = errorsFor({ backend: hf('rocm') });
        const err = errors.find((e) => e.field === 'backend.device');
        expect(err).toBeDefined();
        expect(err!.message).toContain('auto, cpu, cuda, mps');
      });

      it('accepts every device the server accepts', () => {
        for (const device of ['auto', 'cuda', 'mps', 'cpu']) {
          expect(fieldNames({ backend: hf(device) })).not.toContain('backend.device');
        }
      });

      it('rejects a device that contradicts the requested accelerator', () => {
        const errors = errorsFor({ backend: hf('cuda'), placement: { gpu_type: 'apple_mps' } });
        const err = errors.find((e) => e.field === 'backend.device');
        expect(err).toBeDefined();
        expect(err!.message).toContain('nvidia_cuda');
      });

      it('sees through an alias spelling, as the canonicalizing server does', () => {
        // gpu_type 'mps' is stored as apple_mps, so comparing the raw token
        // would let the reported mps-plus-NVIDIA case through to the server.
        expect(fieldNames({ backend: hf('cuda'), placement: { gpu_type: 'mps' } })).toContain('backend.device');
        expect(fieldNames({ backend: hf('mps'), placement: { gpu_type: 'NVIDIA' } })).toContain('backend.device');
      });

      it('lets auto and cpu run on any accelerator', () => {
        expect(fieldNames({ backend: hf('auto'), placement: { gpu_type: 'apple_mps' } })).not.toContain(
          'backend.device',
        );
        expect(fieldNames({ backend: hf('cpu'), placement: { gpu_type: 'nvidia_cuda' } })).not.toContain(
          'backend.device',
        );
      });

      it('reports one message per bad device, not one per broken rule', () => {
        const errors = errorsFor({ backend: hf('rocm'), placement: { gpu_type: 'apple_mps' } });
        expect(errors.filter((e) => e.field === 'backend.device')).toHaveLength(1);
      });

      it('is a llama.cpp non-field, and says what to use instead', () => {
        const errors = errorsFor({ backend: { backend_type: 'llamacpp', device: 'cuda' } });
        const err = errors.find((e) => e.field === 'backend.device');
        expect(err).toBeDefined();
        expect(err!.message).toContain('n_gpu_layers');
      });

      it('leaves a stored llama.cpp device alone, as the server does', () => {
        // Editing replays the full spec: rejecting a value the user never
        // touched would make the intent permanently unsaveable here while the
        // server accepted it.
        const request = { ...validRequest, backend: { backend_type: 'llamacpp', device: 'cuda' } };
        expect(validateIntentRequest(request as any, ['device'])).toEqual([]);
      });

      it('ignores an absent device on any backend', () => {
        expect(fieldNames({ backend: { backend_type: 'llamacpp' } })).not.toContain('backend.device');
        expect(fieldNames({ backend: hf(null) })).not.toContain('backend.device');
      });
    });
  });

  it('rejects empty placement roles', () => {
    expect(fieldNames({ placement: { roles: [] } })).toContain('placement.roles');
    expect(fieldNames({ placement: { roles: ['inference'] } })).not.toContain('placement.roles');
  });

  describe('placement.gpu_type', () => {
    it('rejects a token the server would reject too', () => {
      const errors = errorsFor({ placement: { gpu_type: 'quantum' } });
      const gpuError = errors.find((e) => e.field === 'placement.gpu_type');
      expect(gpuError).toBeDefined();
      // Naming the accepted values beats a bare rejection.
      expect(gpuError!.message).toContain('nvidia_cuda');
    });

    it('accepts every canonical token and alias the server accepts', () => {
      for (const token of ['nvidia_cuda', 'apple_mps', 'cpu', 'nvidia', 'cuda', 'mps', 'metal', 'apple', 'none']) {
        expect(fieldNames({ placement: { gpu_type: token } })).not.toContain('placement.gpu_type');
      }
    });

    it('ignores case and the -/_ spelling, as the server does', () => {
      for (const token of ['NVIDIA', 'NVIDIA-CUDA', 'nvidia-cuda', ' Apple-MPS ', 'Metal']) {
        expect(fieldNames({ placement: { gpu_type: token } })).not.toContain('placement.gpu_type');
      }
    });

    it('treats an empty gpu_type as "any" rather than invalid', () => {
      expect(fieldNames({ placement: { gpu_type: '' } })).not.toContain('placement.gpu_type');
      expect(fieldNames({ placement: { gpu_type: null } })).not.toContain('placement.gpu_type');
    });
  });

  describe('placement.host_allow', () => {
    it('rejects a host that is both allowed and denied', () => {
      const errors = errorsFor({ placement: { host_allow: ['h1', 'h2'], host_deny: ['h2'] } });
      const err = errors.find((e) => e.field === 'placement.host_allow');
      expect(err).toBeDefined();
      expect(err!.message).toContain('h2');
    });

    it('accepts disjoint allow and deny lists', () => {
      expect(fieldNames({ placement: { host_allow: ['h1'], host_deny: ['h2'] } })).not.toContain(
        'placement.host_allow',
      );
    });
  });
});

describe('normalizeGpuType', () => {
  it('mirrors the control-side normalization', () => {
    // Same canonical tokens and aliases as app/validation.py; the previous
    // table invented amd_rocm/auto and missed metal/apple/none.
    expect(normalizeGpuType('nvidia_cuda')).toBe('nvidia_cuda');
    expect(normalizeGpuType('nvidia-cuda')).toBe('nvidia_cuda');
    expect(normalizeGpuType('NVIDIA')).toBe('nvidia_cuda');
    expect(normalizeGpuType('cuda')).toBe('nvidia_cuda');
    expect(normalizeGpuType('apple_mps')).toBe('apple_mps');
    expect(normalizeGpuType('apple-mps')).toBe('apple_mps');
    expect(normalizeGpuType('Metal')).toBe('apple_mps');
    expect(normalizeGpuType('mps')).toBe('apple_mps');
    expect(normalizeGpuType('none')).toBe('cpu');
    expect(normalizeGpuType('cpu')).toBe('cpu');
  });

  it('returns null for unknown, empty and non-values', () => {
    expect(normalizeGpuType('rocm')).toBeNull();
    expect(normalizeGpuType('auto')).toBeNull();
    expect(normalizeGpuType('')).toBeNull();
    expect(normalizeGpuType('   ')).toBeNull();
    expect(normalizeGpuType(null)).toBeNull();
    expect(normalizeGpuType(undefined)).toBeNull();
  });
});

describe('unchangedBackendFields', () => {
  it('reports the keys an edit carries over untouched', () => {
    const stored = { backend_type: 'llamacpp', device: 'cuda', threads: 4 };
    const submitted = { backend_type: 'llamacpp', device: 'cuda', threads: 8 };
    expect(unchangedBackendFields(submitted, stored).sort()).toEqual(['backend_type', 'device']);
  });

  it('grandfathers nothing once the backend type changes', () => {
    // A different backend re-homes every field, so no value is "the one that
    // was already stored" any more.
    const stored = { backend_type: 'llamacpp', device: 'cuda' };
    const submitted = { backend_type: 'huggingface_causal', device: 'cuda' };
    expect(unchangedBackendFields(submitted, stored)).toEqual([]);
  });

  it('grandfathers nothing when creating, since there is no stored spec', () => {
    expect(unchangedBackendFields({ backend_type: 'llamacpp', device: 'cuda' }, undefined)).toEqual([]);
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
