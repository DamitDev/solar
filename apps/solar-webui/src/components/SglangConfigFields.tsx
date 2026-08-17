/**
 * SGLang field block, shared by the add/intent form and the edit modal so the
 * two cannot offer different flags for the same backend.
 *
 * The host owns `--port`, `--api-key` and `--served-model-name`, and the
 * prompt cache directory comes from host settings, so none of those appear
 * here. Everything SGLang gained since this form was written goes into the
 * "extra arguments" / "extra environment" boxes at the bottom.
 */

import { useState, type ReactNode } from 'react';
import { formatExtraArgs, formatExtraEnv, parseExtraArgs, parseExtraEnv } from '@/lib/backendConfig';

interface SglangConfigFieldsProps {
  value: Record<string, any>;
  onChange: (next: Record<string, any>) => void;
  /** AddInstanceModal: true; intent form: false */
  showAlias?: boolean;
  /** model_path — AddInstanceModal: true; intent resolves it server-side */
  showModelFields?: boolean;
  aliasValue?: string;
  onAliasChange?: (v: string) => void;
  fieldError?: (field: string) => ReactNode;
  /** Prefix for the input ids so two forms can coexist in the DOM. */
  idPrefix?: string;
}

const inputClass =
  'w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent';
const monoInputClass = `${inputClass} font-mono text-sm`;
const labelClass = 'block text-sm font-medium text-nord-4 mb-1';
const hintClass = 'text-xs text-nord-4 mt-1';

export function SglangConfigFields({
  value,
  onChange,
  showAlias = false,
  showModelFields = false,
  aliasValue,
  onAliasChange,
  fieldError = () => null,
  idPrefix = 'sglang',
}: SglangConfigFieldsProps) {
  const [extraArgsText, setExtraArgsText] = useState(() => formatExtraArgs(value.extra_args));
  const [extraEnvText, setExtraEnvText] = useState(() => formatExtraEnv(value.extra_env));

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, type } = e.target;
    onChange({
      ...value,
      [name]:
        type === 'number'
          ? e.target.value === ''
            ? undefined
            : parseFloat(e.target.value)
          : type === 'checkbox'
            ? e.target.checked
            : e.target.value,
    });
  };

  const numberField = (name: string, label: string, hint: ReactNode, extra: Record<string, any> = {}) => (
    <div>
      <label className={labelClass} htmlFor={`${idPrefix}-${name}`}>
        {label}
      </label>
      <input
        id={`${idPrefix}-${name}`}
        type="number"
        name={name}
        value={value[name] ?? ''}
        onChange={handleChange}
        placeholder="Blank = SGLang default"
        className={inputClass}
        {...extra}
      />
      <p className={hintClass}>{hint}</p>
      {fieldError(`backend.${name}`)}
    </div>
  );

  const textField = (name: string, label: string, placeholder: string, hint: ReactNode) => (
    <div>
      <label className={labelClass} htmlFor={`${idPrefix}-${name}`}>
        {label}
      </label>
      <input
        id={`${idPrefix}-${name}`}
        type="text"
        name={name}
        value={value[name] || ''}
        onChange={handleChange}
        placeholder={placeholder}
        className={monoInputClass}
      />
      <p className={hintClass}>{hint}</p>
      {fieldError(`backend.${name}`)}
    </div>
  );

  return (
    <>
      {showModelFields && (
        <div className="md:col-span-2">
          <label className={labelClass} htmlFor={`${idPrefix}-model_path`}>
            Model Path <span className="text-nord-11">*</span>
          </label>
          <input
            id={`${idPrefix}-model_path`}
            type="text"
            name="model_path"
            value={value.model_path || ''}
            onChange={handleChange}
            placeholder="/path/to/model-directory"
            required
            className={inputClass}
          />
          <p className={hintClass}>
            Local directory of the model weights, passed as <code>--model-path</code>.
          </p>
          {fieldError('backend.model_path')}
        </div>
      )}

      {showAlias && (
        <div className="md:col-span-2">
          <label className={labelClass} htmlFor={`${idPrefix}-alias`}>
            Alias <span className="text-nord-11">*</span>
          </label>
          <input
            id={`${idPrefix}-alias`}
            type="text"
            name="alias"
            value={aliasValue ?? value.alias ?? ''}
            onChange={(e) => {
              if (onAliasChange) {
                onAliasChange(e.target.value);
              } else {
                onChange({ ...value, alias: e.target.value });
              }
            }}
            placeholder="model-name:size"
            required
            className={inputClass}
          />
          <p className={hintClass}>
            Requests use this name. SGLang reads <code>:</code> as its LoRA separator, so it is served with the colon
            replaced by <code>-</code> and solar-control translates each request.
          </p>
        </div>
      )}

      {/* Parallelism and memory */}
      <div className="md:col-span-2 pt-2">
        <h4 className="text-sm font-semibold text-nord-6">Parallelism &amp; memory</h4>
      </div>

      {numberField('tp_size', 'Tensor Parallel Size', <>Number of GPUs the model is sharded across (--tp-size).</>, {
        min: 1,
        step: 1,
      })}
      {numberField('dp_size', 'Data Parallel Size', <>Independent replicas inside one server (--dp-size).</>, {
        min: 1,
        step: 1,
      })}
      {numberField('context_length', 'Context Length', <>Maximum sequence length (--context-length).</>, {
        // HTML uses `min` as the step base: (value - min) must be a multiple of
        // `step`. min=1 therefore rejects 262144 (256 Ki tokens) and every other
        // 1024-aligned window. Keep min equal to the step, matching ctx_size.
        min: 1024,
        step: 1024,
      })}
      {numberField(
        'mem_fraction_static',
        'Static Memory Fraction',
        <>Share of GPU memory for weights and the KV pool (--mem-fraction-static).</>,
        { min: 0.1, max: 1, step: 0.01 },
      )}
      {numberField(
        'chunked_prefill_size',
        'Chunked Prefill Size',
        <>Prefill chunk in tokens (--chunked-prefill-size).</>,
        {
          step: 1024,
        },
      )}
      {numberField(
        'max_running_requests',
        'Max Running Requests',
        <>Concurrency cap for the scheduler (--max-running-requests).</>,
        { min: 1, step: 1 },
      )}
      {numberField(
        'cuda_graph_max_bs',
        'CUDA Graph Max Batch',
        <>Largest captured batch size (--cuda-graph-max-bs).</>,
        {
          min: 1,
          step: 1,
        },
      )}
      {numberField(
        'cuda_graph_max_bs_decode',
        'CUDA Graph Max Batch (decode)',
        <>Largest captured decode batch (--cuda-graph-max-bs-decode).</>,
        { min: 1, step: 1 },
      )}
      {numberField(
        'swa_full_tokens_ratio',
        'SWA Full Tokens Ratio',
        <>Full-attention share for hybrid sliding-window models (--swa-full-tokens-ratio).</>,
        { min: 0, max: 1, step: 0.01 },
      )}

      {/* Model and kernels */}
      <div className="md:col-span-2 pt-2">
        <h4 className="text-sm font-semibold text-nord-6">Model &amp; kernels</h4>
      </div>

      {textField('dtype', 'Data Type', 'bfloat16', <>Weight dtype (--dtype). Blank lets SGLang decide.</>)}
      {textField('quantization', 'Quantization', 'fp8', <>Quantization method (--quantization).</>)}
      {textField('kv_cache_dtype', 'KV Cache Type', 'fp8_e4m3', <>KV cache dtype (--kv-cache-dtype).</>)}
      {textField(
        'moe_runner_backend',
        'MoE Runner Backend',
        'flashinfer_mxfp4',
        <>MoE kernel (--moe-runner-backend).</>,
      )}
      {textField(
        'speculative_algorithm',
        'Speculative Algorithm',
        'EAGLE',
        <>Speculative decoding algorithm (--speculative-algorithm).</>,
      )}

      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          id={`${idPrefix}-trust_remote_code`}
          name="trust_remote_code"
          checked={!!value.trust_remote_code}
          onChange={handleChange}
          className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
        />
        <div>
          <label htmlFor={`${idPrefix}-trust_remote_code`} className="block text-sm font-medium text-nord-4">
            Trust Remote Code
          </label>
          <p className="text-xs text-nord-4">Allow the model repo&apos;s own modelling code (--trust-remote-code)</p>
        </div>
      </div>

      {/* Hierarchical cache */}
      <div className="md:col-span-2 pt-2">
        <h4 className="text-sm font-semibold text-nord-6">Prompt cache</h4>
        <p className={hintClass}>
          The file-backed cache directory comes from the host&apos;s <code>SGLANG_PROMPT_CACHE_DIR</code>; the host
          drops the storage flags when it is not configured.
        </p>
      </div>

      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          id={`${idPrefix}-enable_hierarchical_cache`}
          name="enable_hierarchical_cache"
          checked={!!value.enable_hierarchical_cache}
          onChange={handleChange}
          className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
        />
        <div>
          <label htmlFor={`${idPrefix}-enable_hierarchical_cache`} className="block text-sm font-medium text-nord-4">
            Hierarchical Cache
          </label>
          <p className="text-xs text-nord-4">Keep evicted prefixes in host memory (--enable-hierarchical-cache)</p>
        </div>
      </div>

      {numberField('hicache_ratio', 'Host Cache Ratio', <>Host-to-device cache size ratio (--hicache-ratio).</>, {
        min: 0,
        step: 1,
      })}
      {textField(
        'hicache_mem_layout',
        'Host Cache Layout',
        'page_first_direct',
        <>Memory layout (--hicache-mem-layout).</>,
      )}
      {textField(
        'hicache_io_backend',
        'Host Cache IO Backend',
        'direct',
        <>Transfer backend (--hicache-io-backend).</>,
      )}
      {textField(
        'hicache_storage_backend',
        'Storage Backend',
        'file',
        <>Persistent backend (--hicache-storage-backend).</>,
      )}
      {textField(
        'hicache_storage_prefetch_policy',
        'Prefetch Policy',
        'wait_complete',
        <>Prefetch policy (--hicache-storage-prefetch-policy).</>,
      )}

      <div className="md:col-span-2">
        <label className={labelClass} htmlFor={`${idPrefix}-hicache_storage_backend_extra_config`}>
          Storage Backend Options
        </label>
        <input
          id={`${idPrefix}-hicache_storage_backend_extra_config`}
          type="text"
          name="hicache_storage_backend_extra_config"
          value={value.hicache_storage_backend_extra_config || ''}
          onChange={handleChange}
          placeholder='{"max_size":"256G","eviction_ratio":0.9}'
          className={monoInputClass}
        />
        <p className={hintClass}>
          JSON object passed as <code>--hicache-storage-backend-extra-config</code>.
        </p>
        {fieldError('backend.hicache_storage_backend_extra_config')}
      </div>

      {/* Advanced */}
      <div className="md:col-span-2 pt-2">
        <h4 className="text-sm font-semibold text-nord-6">Advanced</h4>
      </div>

      <div className="md:col-span-2">
        <label className={labelClass} htmlFor={`${idPrefix}-extra_args`}>
          Extra Arguments
        </label>
        <textarea
          id={`${idPrefix}-extra_args`}
          value={extraArgsText}
          onChange={(e) => {
            setExtraArgsText(e.target.value);
            onChange({ ...value, extra_args: parseExtraArgs(e.target.value) });
          }}
          rows={3}
          placeholder={'--dist-init-addr 10.0.0.1:5000\n--enable-metrics'}
          className={`${monoInputClass} resize-y`}
        />
        <p className={hintClass}>
          One flag per line, value after a space. Quote a value that contains spaces, e.g.{' '}
          <code>--preferred-sampling-params &apos;{'{"temperature": 1}'}&apos;</code>. Passed to SGLang after the fields
          above, so an entry here wins. <code>--port</code>, <code>--api-key</code>, <code>--model-path</code> and{' '}
          <code>--served-model-name</code> are managed by the host and rejected.
        </p>
        {fieldError('backend.extra_args')}
      </div>

      <div className="md:col-span-2">
        <label className={labelClass} htmlFor={`${idPrefix}-extra_env`}>
          Extra Environment
        </label>
        <textarea
          id={`${idPrefix}-extra_env`}
          value={extraEnvText}
          onChange={(e) => {
            setExtraEnvText(e.target.value);
            onChange({ ...value, extra_env: parseExtraEnv(e.target.value) });
          }}
          rows={3}
          placeholder={'SGLANG_DSV4_COMPRESS_STATE_DTYPE=bf16\nSGLANG_ENABLE_JIT_DEEPGEMM=1'}
          className={`${monoInputClass} resize-y`}
        />
        <p className={hintClass}>One NAME=value per line, set on the SGLang process.</p>
        {fieldError('backend.extra_env')}
      </div>
    </>
  );
}
