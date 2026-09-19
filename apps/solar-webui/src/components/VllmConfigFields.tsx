/**
 * vLLM field block, shared by the add/intent form and the edit modal so the
 * two cannot offer different flags for the same backend.
 *
 * The host owns `--host`, `--port`, `--api-key`, `--model`,
 * `--served-model-name`, `--enable-prompt-tokens-details` and
 * `--disable-log-stats`, so none of those appear here. Everything vLLM gained
 * since this form was written goes into the "extra arguments" / "extra
 * environment" boxes at the bottom.
 */

import { useState, type ReactNode } from 'react';
import { formatExtraArgs, formatExtraEnv, parseExtraArgs, parseExtraEnv } from '@/lib/backendConfig';

interface VllmConfigFieldsProps {
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

export function VllmConfigFields({
  value,
  onChange,
  showAlias = false,
  showModelFields = false,
  aliasValue,
  onAliasChange,
  fieldError = () => null,
  idPrefix = 'vllm',
}: VllmConfigFieldsProps) {
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
        placeholder="Blank = vLLM default"
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

  const checkboxField = (name: string, label: string, hint: ReactNode) => (
    <div className="flex items-start gap-3">
      <input
        type="checkbox"
        id={`${idPrefix}-${name}`}
        name={name}
        checked={!!value[name]}
        onChange={handleChange}
        className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
      />
      <div>
        <label htmlFor={`${idPrefix}-${name}`} className="block text-sm font-medium text-nord-4">
          {label}
        </label>
        <p className="text-xs text-nord-4">{hint}</p>
      </div>
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
            Local directory of the model weights, passed as the positional model argument of <code>vllm serve</code>.
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
            Requests use this name. vLLM performs no colon parsing, so the alias — colon included — is served verbatim.
          </p>
        </div>
      )}

      {/* Parallelism and memory */}
      <div className="md:col-span-2 pt-2">
        <h4 className="text-sm font-semibold text-nord-6">Parallelism &amp; memory</h4>
      </div>

      {numberField(
        'tensor_parallel_size',
        'Tensor Parallel Size',
        <>Number of GPUs the model is sharded across (--tensor-parallel-size).</>,
        { min: 1, step: 1 },
      )}
      {numberField(
        'pipeline_parallel_size',
        'Pipeline Parallel Size',
        <>Number of pipeline stages (--pipeline-parallel-size).</>,
        { min: 1, step: 1 },
      )}
      {numberField(
        'max_model_len',
        'Max Model Length',
        <>Maximum sequence length; the engine's KV pool is sized from it (--max-model-len).</>,
        // HTML uses `min` as the step base: (value - min) must be a multiple of
        // `step`, so 1024 keeps 262144 and the other aligned windows legal.
        { min: 1024, step: 1024 },
      )}
      {numberField(
        'gpu_memory_utilization',
        'GPU Memory Utilization',
        <>Share of GPU memory reserved for the engine (--gpu-memory-utilization).</>,
        { min: 0.1, max: 1, step: 0.01 },
      )}
      {numberField('max_num_seqs', 'Max Num Seqs', <>Concurrency cap for the scheduler (--max-num-seqs).</>, {
        min: 1,
        step: 1,
      })}
      {numberField(
        'max_num_batched_tokens',
        'Max Num Batched Tokens',
        <>Maximum tokens per scheduler step (--max-num-batched-tokens).</>,
        { min: 1, step: 1 },
      )}

      {/* Model and kernels */}
      <div className="md:col-span-2 pt-2">
        <h4 className="text-sm font-semibold text-nord-6">Model &amp; kernels</h4>
      </div>

      {textField('dtype', 'Data Type', 'bfloat16', <>Weight dtype (--dtype). Blank lets vLLM decide.</>)}
      {textField('quantization', 'Quantization', 'fp8', <>Quantization method (--quantization).</>)}
      {textField('kv_cache_dtype', 'KV Cache Type', 'fp8', <>KV cache dtype (--kv-cache-dtype).</>)}
      {textField('moe_backend', 'MoE Backend', 'cutlass', <>MoE kernel backend (--moe-backend).</>)}

      {checkboxField(
        'trust_remote_code',
        'Trust Remote Code',
        <>Allow the model repo&apos;s own modelling code (--trust-remote-code)</>,
      )}
      {checkboxField('enforce_eager', 'Enforce Eager', <>Skip CUDA graph capture (--enforce-eager)</>)}

      <div className="md:col-span-2">
        <label className={labelClass} htmlFor={`${idPrefix}-enable_prefix_caching`}>
          Prefix Caching
        </label>
        <select
          id={`${idPrefix}-enable_prefix_caching`}
          name="enable_prefix_caching"
          value={value.enable_prefix_caching === true ? 'on' : value.enable_prefix_caching === false ? 'off' : ''}
          onChange={(e) => {
            const raw = e.target.value;
            const next = { ...value };
            if (raw === '') {
              // Tri-state: omitted means the engine's own default.
              delete next.enable_prefix_caching;
            } else {
              next.enable_prefix_caching = raw === 'on';
            }
            onChange(next);
          }}
          className={inputClass}
        >
          <option value="">Engine default</option>
          <option value="on">Enabled (--enable-prefix-caching)</option>
          <option value="off">Disabled (--no-enable-prefix-caching)</option>
        </select>
        <p className={hintClass}>Automatic prefix caching; left alone the engine default applies.</p>
        {fieldError('backend.enable_prefix_caching')}
      </div>

      {/* Speculative decoding */}
      <div className="md:col-span-2 pt-2">
        <h4 className="text-sm font-semibold text-nord-6">Speculative decoding</h4>
      </div>

      <div className="md:col-span-2">
        <label className={labelClass} htmlFor={`${idPrefix}-speculative_config`}>
          Speculative Config
        </label>
        <input
          id={`${idPrefix}-speculative_config`}
          type="text"
          name="speculative_config"
          value={value.speculative_config || ''}
          onChange={handleChange}
          placeholder='{"method":"mtp","num_speculative_tokens":5}'
          className={monoInputClass}
        />
        <p className={hintClass}>
          JSON object passed as <code>--speculative-config</code>.
        </p>
        {fieldError('backend.speculative_config')}
      </div>

      {textField(
        'tool_call_parser',
        'Tool Call Parser',
        'hermes',
        <>Parser that turns model output into tool calls (--tool-call-parser).</>,
      )}
      {textField(
        'reasoning_parser',
        'Reasoning Parser',
        'deepseek_r1',
        <>Parser that splits thinking from the answer (--reasoning-parser).</>,
      )}

      {checkboxField(
        'enable_auto_tool_choice',
        'Auto Tool Choice',
        <>Let the model choose tool calls (--enable-auto-tool-choice)</>,
      )}

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
          placeholder={'--max-num-partial-prefills 4\n--enable-chunked-prefill'}
          className={`${monoInputClass} resize-y`}
        />
        <p className={hintClass}>
          One flag per line, value after a space. Quote a value that contains spaces, e.g.{' '}
          <code>--override-generation-config &apos;{'{...}'}&apos;</code>. Passed to vLLM after the fields above, so an
          entry here wins. <code>--host</code>, <code>--port</code>, <code>--api-key</code>, <code>--model</code>,{' '}
          <code>--served-model-name</code>, <code>--enable-prompt-tokens-details</code>,{' '}
          <code>--no-enable-prompt-tokens-details</code> and <code>--disable-log-stats</code> are managed by the host
          and rejected.
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
          placeholder={'VLLM_USE_FLASHINFER_MOE_FP4=1\nVLLM_ATTENTION_BACKEND=FLASHINFER'}
          className={`${monoInputClass} resize-y`}
        />
        <p className={hintClass}>One NAME=value per line, set on the vLLM process.</p>
        {fieldError('backend.extra_env')}
      </div>
    </>
  );
}
