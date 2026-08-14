import { describe, expect, it } from 'vitest';
import {
  DEVICE_OPTIONS,
  DTYPE_OPTIONS,
  SPLIT_MODE_OPTIONS,
  applySpecType,
  formatExtraArgs,
  formatExtraEnv,
  getBackendTypeFromSelection,
  getDefaultConfig,
  parseExtraArgs,
  parseExtraEnv,
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

  it('maps sglang to sglang, which has no modes', () => {
    expect(getBackendTypeFromSelection('sglang', '')).toBe('sglang');
    expect(getBackendTypeFromSelection('sglang', 'causal')).toBe('sglang');
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

  it('asks an sglang instance for a model path, and an intent for none', () => {
    expect(getDefaultConfig('sglang', '').model_path).toBe('');
    expect(getDefaultConfig('sglang', '', true).model_path).toBeUndefined();
    expect(getDefaultConfig('sglang', '', true).backend_type).toBe('sglang');
  });

  it('leaves the sglang flags it does not seed empty so SGLang keeps its defaults', () => {
    const cfg = getDefaultConfig('sglang', '', true);
    expect(cfg.tp_size).toBe(1);
    expect(cfg.mem_fraction_static).toBe(0.9);
    expect(cfg.context_length).toBeUndefined();
    expect(cfg.quantization).toBe('');
    expect(cfg.extra_args).toEqual([]);
    expect(cfg.extra_env).toEqual({});
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

  it('drops the blank sglang flags and keeps the filled ones', () => {
    expect(
      stripEmptyOptionalFields({
        backend_type: 'sglang',
        quantization: '',
        kv_cache_dtype: 'fp8_e4m3',
        hicache_mem_layout: '',
        hicache_storage_backend: 'file',
      }),
    ).toEqual({ backend_type: 'sglang', kv_cache_dtype: 'fp8_e4m3', hicache_storage_backend: 'file' });
  });

  it("drops an empty sglang dtype but keeps huggingface's 'auto'", () => {
    expect(stripEmptyOptionalFields({ backend_type: 'sglang', dtype: '' })).toEqual({ backend_type: 'sglang' });
    expect(stripEmptyOptionalFields({ backend_type: 'huggingface_causal', dtype: 'auto' }).dtype).toBe('auto');
  });

  it('drops the empty escape hatches so the host sees no override at all', () => {
    expect(stripEmptyOptionalFields({ backend_type: 'sglang', extra_args: [], extra_env: {} })).toEqual({
      backend_type: 'sglang',
    });
    const kept = stripEmptyOptionalFields({
      backend_type: 'sglang',
      extra_args: ['--dist-init-addr', '10.0.0.1:5000'],
      extra_env: { SGLANG_DSV4_COMPRESS_STATE_DTYPE: 'bf16' },
    });
    expect(kept.extra_args).toEqual(['--dist-init-addr', '10.0.0.1:5000']);
    expect(kept.extra_env).toEqual({ SGLANG_DSV4_COMPRESS_STATE_DTYPE: 'bf16' });
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

describe('sglang extra args and env editors', () => {
  it('splits a flag and its value into separate argv entries', () => {
    expect(parseExtraArgs('--dist-init-addr 10.0.0.1:5000\n--enable-metrics')).toEqual([
      '--dist-init-addr',
      '10.0.0.1:5000',
      '--enable-metrics',
    ]);
  });

  it('ignores blank lines and stray whitespace', () => {
    expect(parseExtraArgs('\n  --enable-metrics   \n\n')).toEqual(['--enable-metrics']);
  });

  it('round-trips an argv list through the textarea form', () => {
    const args = ['--dist-init-addr', '10.0.0.1:5000'];
    expect(parseExtraArgs(formatExtraArgs(args))).toEqual(args);
    expect(formatExtraArgs(undefined)).toBe('');
  });

  it('parses NAME=value lines and keeps values containing an equals sign', () => {
    expect(parseExtraEnv('A=1\nB=x=y')).toEqual({ A: '1', B: 'x=y' });
  });

  it('skips comments, blanks and lines with no name', () => {
    expect(parseExtraEnv('# a comment\n\n=orphan\nA=1')).toEqual({ A: '1' });
  });

  it('round-trips an environment map through the textarea form', () => {
    const env = { SGLANG_DSV4_COMPRESS_STATE_DTYPE: 'bf16', OTHER: '2' };
    expect(parseExtraEnv(formatExtraEnv(env))).toEqual(env);
    expect(formatExtraEnv(undefined)).toBe('');
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
