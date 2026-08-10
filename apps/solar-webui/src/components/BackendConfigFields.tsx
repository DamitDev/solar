/**
 * BackendConfigFields — shared backend editor.
 *
 * Extracted from AddInstanceModal (U-003) so the imperative instance modal and
 * the declarative intent form reuse the same backend-tabs → mode-cards →
 * per-backend-field grid logic instead of duplicating it.
 *
 * The component owns the primary-backend / mode selection state and calls
 * `onChange` with the full next value whenever anything changes:
 *   - AddInstanceModal: `value` is the whole instance formData
 *     (showAlias + showModelFields on) — alias/model live inside the object.
 *   - Intent form: `value` is the `backend` object only
 *     (both flags off) — alias/model_source are intent-level fields and must
 *     not appear inside `backend` (spec deployment-intent.md §4.7).
 */

import { useState } from 'react';
import { Cpu, Brain } from 'lucide-react';
import {
  PrimaryBackend,
  LlamaCppMode,
  HuggingFaceMode,
  ModeOption,
  LLAMACPP_MODES,
  HUGGINGFACE_MODES,
  DEVICE_OPTIONS,
  DTYPE_OPTIONS,
  getDefaultConfig,
} from '@/lib/backendConfig';
import { SpeculativeDecodingFields } from './SpeculativeDecodingFields';

export type { PrimaryBackend, LlamaCppMode, HuggingFaceMode, ModeOption };

interface BackendConfigFieldsProps {
  /** Backend object only (no alias/model/host/api_key) for the intent form; full instance formData for AddInstanceModal. */
  value: Record<string, any>;
  onChange: (next: Record<string, any>) => void;
  /** AddInstanceModal: true; intent form: false */
  showAlias?: boolean;
  /** model path / model_id — AddInstanceModal: true; intent: false */
  showModelFields?: boolean;
  /** Intent form: true. Keeps server-derived fields out of the backend object
   *  on backend/mode switches and shows the model file selector instead of an
   *  absolute model path. */
  forIntent?: boolean;
  /** Bound through for AddInstanceModal (alias is part of its formData). */
  aliasValue?: string;
  onAliasChange?: (v: string) => void;
}

/** Derive the initial tab/mode selection from an existing backend object. */
function getInitialSelection(value: Record<string, any>): { primary: PrimaryBackend; mode: string } {
  const backendType = value?.backend_type;
  if (backendType === 'huggingface_causal') return { primary: 'huggingface', mode: 'causal' };
  if (backendType === 'huggingface_classification') return { primary: 'huggingface', mode: 'classifier' };
  if (backendType === 'huggingface_embedding') return { primary: 'huggingface', mode: 'embedding' };
  if (backendType === 'huggingface_vision') return { primary: 'huggingface', mode: 'causal' };
  const modelType = value?.model_type;
  if (modelType === 'embedding' || modelType === 'reranker') return { primary: 'llamacpp', mode: modelType };
  return { primary: 'llamacpp', mode: 'llm' };
}

export function BackendConfigFields({
  value,
  onChange,
  showAlias = false,
  showModelFields = false,
  forIntent = false,
  aliasValue,
  onAliasChange,
}: BackendConfigFieldsProps) {
  const initial = getInitialSelection(value);
  const [primaryBackend, setPrimaryBackend] = useState<PrimaryBackend>(initial.primary);
  const [llamaCppMode, setLlamaCppMode] = useState<LlamaCppMode>((initial.mode as LlamaCppMode) || 'llm');
  const [huggingFaceMode, setHuggingFaceMode] = useState<HuggingFaceMode>(
    (initial.mode as HuggingFaceMode) || 'causal',
  );
  const [labelsInput, setLabelsInput] = useState(Array.isArray(value?.labels) ? value.labels.join(', ') : '');

  const currentMode = primaryBackend === 'llamacpp' ? llamaCppMode : huggingFaceMode;

  const handlePrimaryBackendChange = (newBackend: PrimaryBackend) => {
    setPrimaryBackend(newBackend);
    const mode = newBackend === 'llamacpp' ? llamaCppMode : huggingFaceMode;
    onChange(getDefaultConfig(newBackend, mode, forIntent));
    setLabelsInput('');
  };

  const handleModeChange = (mode: string) => {
    if (primaryBackend === 'llamacpp') {
      setLlamaCppMode(mode as LlamaCppMode);
    } else {
      setHuggingFaceMode(mode as HuggingFaceMode);
    }
    onChange(getDefaultConfig(primaryBackend, mode, forIntent));
    setLabelsInput('');
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, type } = e.target;
    const checked = (e.target as HTMLInputElement).checked;
    onChange({
      ...value,
      [name]:
        type === 'number'
          ? e.target.value === ''
            ? undefined
            : parseFloat(e.target.value)
          : type === 'checkbox'
            ? checked
            : e.target.value,
    });
  };

  const modeOptions = primaryBackend === 'llamacpp' ? LLAMACPP_MODES : HUGGINGFACE_MODES;

  return (
    <div className="space-y-6">
      {/* Step 1: Primary Backend Selection */}
      <div>
        <label className="block text-sm font-medium text-nord-4 mb-3">Backend</label>
        <div className="grid grid-cols-2 gap-3">
          {/* Llama.cpp */}
          <button
            type="button"
            onClick={() => handlePrimaryBackendChange('llamacpp')}
            className={`p-4 rounded-lg border-2 transition-all text-left ${
              primaryBackend === 'llamacpp'
                ? 'border-nord-10 bg-nord-10 bg-opacity-15'
                : 'border-nord-3 hover:border-nord-4 bg-nord-2'
            }`}
          >
            <div className="flex items-center gap-3">
              <div
                className={`p-2 rounded-lg ${primaryBackend === 'llamacpp' ? 'bg-nord-10 bg-opacity-20' : 'bg-nord-3'}`}
              >
                <Cpu size={24} className={primaryBackend === 'llamacpp' ? 'text-nord-10' : 'text-nord-4'} />
              </div>
              <div>
                <div
                  className={`text-base font-semibold ${primaryBackend === 'llamacpp' ? 'text-nord-10' : 'text-nord-6'}`}
                >
                  llama.cpp
                </div>
                <div className="text-xs text-nord-4">GGUF models with llama-server</div>
              </div>
            </div>
          </button>

          {/* HuggingFace */}
          <button
            type="button"
            onClick={() => handlePrimaryBackendChange('huggingface')}
            className={`p-4 rounded-lg border-2 transition-all text-left ${
              primaryBackend === 'huggingface'
                ? 'border-nord-14 bg-nord-14 bg-opacity-15'
                : 'border-nord-3 hover:border-nord-4 bg-nord-2'
            }`}
          >
            <div className="flex items-center gap-3">
              <div
                className={`p-2 rounded-lg ${primaryBackend === 'huggingface' ? 'bg-nord-14 bg-opacity-20' : 'bg-nord-3'}`}
              >
                <Brain size={24} className={primaryBackend === 'huggingface' ? 'text-nord-14' : 'text-nord-4'} />
              </div>
              <div>
                <div
                  className={`text-base font-semibold ${primaryBackend === 'huggingface' ? 'text-nord-14' : 'text-nord-6'}`}
                >
                  HuggingFace
                </div>
                <div className="text-xs text-nord-4">Transformers models</div>
              </div>
            </div>
          </button>
        </div>
      </div>

      {/* Step 2: Mode Selection */}
      <div>
        <label className="block text-sm font-medium text-nord-4 mb-3">Mode</label>
        <div className="grid grid-cols-3 gap-3">
          {modeOptions.map((option) => {
            const Icon = option.icon;
            const isSelected = currentMode === option.value;
            const accentColor = primaryBackend === 'llamacpp' ? 'nord-10' : 'nord-14';
            return (
              <button
                key={option.value}
                type="button"
                onClick={() => handleModeChange(option.value)}
                className={`p-3 rounded-lg border-2 transition-all text-center ${
                  isSelected
                    ? `border-${accentColor} bg-${accentColor} bg-opacity-10`
                    : 'border-nord-3 hover:border-nord-4 bg-nord-2'
                }`}
                style={
                  isSelected
                    ? {
                        borderColor: primaryBackend === 'llamacpp' ? '#81A1C1' : '#A3BE8C',
                        backgroundColor:
                          primaryBackend === 'llamacpp' ? 'rgba(129, 161, 193, 0.1)' : 'rgba(163, 190, 140, 0.1)',
                      }
                    : {}
                }
              >
                <Icon
                  size={22}
                  className={`mx-auto ${
                    isSelected ? (primaryBackend === 'llamacpp' ? 'text-nord-10' : 'text-nord-14') : 'text-nord-4'
                  }`}
                />
                <div
                  className={`mt-2 text-sm font-medium ${
                    isSelected ? (primaryBackend === 'llamacpp' ? 'text-nord-10' : 'text-nord-14') : 'text-nord-6'
                  }`}
                >
                  {option.label}
                </div>
                <div className="text-xs text-nord-4 mt-1">{option.description}</div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Configuration Fields */}
      <div className="border-t border-nord-3 pt-4">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {primaryBackend === 'llamacpp' ? (
            /* llama.cpp specific fields */
            <>
              {/* Model Path */}
              {showModelFields && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">
                    Model Path <span className="text-nord-11">*</span>
                  </label>
                  <input
                    type="text"
                    name="model"
                    value={value.model || ''}
                    onChange={handleChange}
                    placeholder="/path/to/model.gguf"
                    required
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>
              )}

              {/* Model file selector — intents point at a directory, not a file */}
              {forIntent && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Model file</label>
                  <input
                    type="text"
                    name="model_file"
                    value={value.model_file || ''}
                    onChange={handleChange}
                    placeholder="*UD-Q4_K_XL*.gguf"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent font-mono text-sm"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    Which GGUF to serve. A filename, relative path or <code>*</code> glob, searched recursively in the
                    downloaded model directory. Leave empty to use the largest GGUF found.
                  </p>
                </div>
              )}

              {/* Multimodal projector (vision) — LLM only */}
              {llamaCppMode === 'llm' && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Multimodal projector (Optional)</label>
                  <input
                    type="text"
                    name="mmproj"
                    value={value.mmproj || ''}
                    onChange={handleChange}
                    placeholder={forIntent ? 'mmproj-BF16.gguf' : '/path/to/mmproj.gguf'}
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    Passed to llama-server as <code>--mmproj</code> for vision-capable models. A bare filename or glob
                    is resolved in the model directory, same as the model file.
                  </p>
                </div>
              )}

              {/* Multimodal projector GPU offload — only when mmproj is set */}
              {llamaCppMode === 'llm' && value.mmproj && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Projector GPU Offload</label>
                  <select
                    name="mmproj_offload"
                    value={value.mmproj_offload === false ? 'false' : 'true'}
                    onChange={(e) => onChange({ ...value, mmproj_offload: e.target.value === 'true' })}
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  >
                    <option value="true">GPU (default)</option>
                    <option value="false">CPU</option>
                  </select>
                  <p className="text-xs text-nord-4 mt-1">
                    Where to run the multimodal projector. Disabling passes <code>--no-mmproj-offload</code>.
                  </p>
                </div>
              )}

              {/* Alias */}
              {showAlias && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">
                    Alias <span className="text-nord-11">*</span>
                  </label>
                  <input
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
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>
              )}

              {/* Chat Template File - only for LLM mode */}
              {llamaCppMode === 'llm' && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Chat Template File (Optional)</label>
                  <input
                    type="text"
                    name="chat_template_file"
                    value={value.chat_template_file || ''}
                    onChange={handleChange}
                    placeholder="/path/to/template.jinja"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>
              )}

              {/* Chat Template Kwargs - only for LLM mode */}
              {llamaCppMode === 'llm' && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Chat Template Kwargs (Optional)</label>
                  <input
                    type="text"
                    name="chat_template_kwargs"
                    value={value.chat_template_kwargs || ''}
                    onChange={handleChange}
                    placeholder='{"enable_thinking":true}'
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent font-mono text-sm"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    JSON string passed to llama-server as <code>--chat-template-kwargs</code>.
                  </p>
                </div>
              )}

              {/* Reasoning - only for LLM mode */}
              {llamaCppMode === 'llm' && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Reasoning (Optional)</label>
                  <select
                    name="reasoning"
                    value={value.reasoning || ''}
                    onChange={handleChange}
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  >
                    <option value="">Default (not set)</option>
                    <option value="on">On</option>
                    <option value="off">Off</option>
                    <option value="auto">Auto (detect from template)</option>
                  </select>
                  <p className="text-xs text-nord-4 mt-1">
                    Passed as <code>--reasoning</code>. Controls thinking/reasoning mode for models that support it.
                  </p>
                </div>
              )}

              {/* Reasoning Budget - only for LLM mode */}
              {llamaCppMode === 'llm' && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Reasoning Budget (Optional)</label>
                  <input
                    type="number"
                    name="reasoning_budget"
                    value={value.reasoning_budget ?? ''}
                    onChange={handleChange}
                    placeholder="-1 = unrestricted, 0 = disable, blank = omit"
                    min="-1"
                    step="1"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    Passed as <code>--reasoning-budget</code>. Use <code>-1</code> for unrestricted, <code>0</code> to
                    disable thinking. Leave blank to omit.
                  </p>
                </div>
              )}

              {/* Speculative decoding - only for LLM mode */}
              {llamaCppMode === 'llm' && (
                <SpeculativeDecodingFields value={value} onChange={onChange} idPrefix="add" forIntent={forIntent} />
              )}

              {/* Special Flag - only for LLM mode */}
              {llamaCppMode === 'llm' && (
                <div className="md:col-span-2 flex items-start gap-3">
                  <div className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      id="special"
                      name="special"
                      checked={!!value.special}
                      onChange={handleChange}
                      className="h-4 w-4 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
                    />
                  </div>
                  <div>
                    <label htmlFor="special" className="block text-sm font-medium text-nord-4 mb-1">
                      Enable --special flag
                    </label>
                    <p className="text-xs text-nord-4">
                      When enabled, llama-server will be started with the <code>--special</code> flag.
                    </p>
                  </div>
                </div>
              )}

              {/* Override Tensor (ot) */}
              <div className="md:col-span-2">
                <label className="block text-sm font-medium text-nord-4 mb-1">Override Tensor (ot) (Optional)</label>
                <input
                  type="text"
                  name="ot"
                  value={value.ot || ''}
                  onChange={handleChange}
                  placeholder="Override tensor string"
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                />
                <p className="text-xs text-nord-4 mt-1">
                  Override tensor string passed to llama-server as <code>-ot</code> flag.
                </p>
              </div>

              {/* Pooling - only for embedding mode */}
              {llamaCppMode === 'embedding' && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Pooling</label>
                  <select
                    name="pooling"
                    value={value.pooling || ''}
                    onChange={handleChange}
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  >
                    <option value="">Default - Unspecified</option>
                    <option value="none">None</option>
                    <option value="mean">Mean</option>
                    <option value="cls">CLS</option>
                    <option value="last">Last</option>
                    <option value="rank">Rank</option>
                  </select>
                  <p className="text-xs text-nord-4 mt-1">Pooling strategy for embedding models.</p>
                </div>
              )}

              {/* Threads */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Threads</label>
                <input
                  type="number"
                  name="threads"
                  value={value.threads}
                  onChange={handleChange}
                  min="1"
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                />
              </div>

              {/* GPU Layers */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">GPU Layers</label>
                <input
                  type="number"
                  name="n_gpu_layers"
                  value={value.n_gpu_layers}
                  onChange={handleChange}
                  min="0"
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                />
              </div>

              {/* Context Size */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Context Size</label>
                <input
                  type="number"
                  name="ctx_size"
                  value={value.ctx_size}
                  onChange={handleChange}
                  min="512"
                  step="512"
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                />
              </div>

              {/* KV Cache Type K */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Cache Type K</label>
                <select
                  name="cache_type_k"
                  value={value.cache_type_k || ''}
                  onChange={handleChange}
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                >
                  <option value="">Default</option>
                  <option value="f32">f32</option>
                  <option value="f16">f16</option>
                  <option value="bf16">bf16</option>
                  <option value="q8_0">q8_0</option>
                  <option value="q4_0">q4_0</option>
                  <option value="q4_1">q4_1</option>
                  <option value="iq4_nl">iq4_nl</option>
                  <option value="q5_0">q5_0</option>
                  <option value="q5_1">q5_1</option>
                </select>
                <p className="text-xs text-nord-4 mt-1">
                  KV cache quantization for keys (<code>-ctk</code>).
                </p>
              </div>

              {/* KV Cache Type V */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Cache Type V</label>
                <select
                  name="cache_type_v"
                  value={value.cache_type_v || ''}
                  onChange={handleChange}
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                >
                  <option value="">Default</option>
                  <option value="f32">f32</option>
                  <option value="f16">f16</option>
                  <option value="bf16">bf16</option>
                  <option value="q8_0">q8_0</option>
                  <option value="q4_0">q4_0</option>
                  <option value="q4_1">q4_1</option>
                  <option value="iq4_nl">iq4_nl</option>
                  <option value="q5_0">q5_0</option>
                  <option value="q5_1">q5_1</option>
                </select>
                <p className="text-xs text-nord-4 mt-1">
                  KV cache quantization for values (<code>-ctv</code>).
                </p>
              </div>

              {/* RoPE Scaling */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">RoPE Scaling</label>
                <select
                  name="rope_scaling"
                  value={value.rope_scaling || ''}
                  onChange={handleChange}
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                >
                  <option value="">Default</option>
                  <option value="none">none</option>
                  <option value="linear">linear</option>
                  <option value="yarn">yarn</option>
                </select>
                <p className="text-xs text-nord-4 mt-1">
                  RoPE scaling method (<code>--rope-scaling</code>).
                </p>
              </div>

              {/* RoPE Scale */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">RoPE Scale</label>
                <input
                  type="number"
                  name="rope_scale"
                  value={value.rope_scale ?? ''}
                  onChange={handleChange}
                  placeholder="Blank = omit"
                  min="1"
                  step="0.1"
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                />
                <p className="text-xs text-nord-4 mt-1">
                  RoPE context scaling factor (<code>--rope-scale</code>).
                </p>
              </div>

              {/* YaRN Original Context */}
              <div className="md:col-span-2">
                <label className="block text-sm font-medium text-nord-4 mb-1">YaRN Original Context</label>
                <input
                  type="number"
                  name="yarn_orig_ctx"
                  value={value.yarn_orig_ctx ?? ''}
                  onChange={handleChange}
                  placeholder="Blank = omit"
                  min="1"
                  step="1"
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                />
                <p className="text-xs text-nord-4 mt-1">
                  Original context size for YaRN (<code>--yarn-orig-ctx</code>).
                </p>
              </div>

              {/* LLM-specific sampling parameters */}
              {llamaCppMode === 'llm' && (
                <>
                  {/* Temperature */}
                  <div>
                    <label className="block text-sm font-medium text-nord-4 mb-1">Temperature</label>
                    <input
                      type="number"
                      name="temp"
                      value={value.temp}
                      onChange={handleChange}
                      min="0"
                      max="2"
                      step="0.01"
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                  </div>

                  {/* Top P */}
                  <div>
                    <label className="block text-sm font-medium text-nord-4 mb-1">Top P</label>
                    <input
                      type="number"
                      name="top_p"
                      value={value.top_p}
                      onChange={handleChange}
                      min="0"
                      max="1"
                      step="0.01"
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                  </div>

                  {/* Top K */}
                  <div>
                    <label className="block text-sm font-medium text-nord-4 mb-1">Top K</label>
                    <input
                      type="number"
                      name="top_k"
                      value={value.top_k}
                      onChange={handleChange}
                      min="0"
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                  </div>

                  {/* Min P */}
                  <div>
                    <label className="block text-sm font-medium text-nord-4 mb-1">Min P</label>
                    <input
                      type="number"
                      name="min_p"
                      value={value.min_p}
                      onChange={handleChange}
                      min="0"
                      max="1"
                      step="0.01"
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                  </div>
                </>
              )}
            </>
          ) : (
            /* HuggingFace specific fields */
            <>
              {/* Model ID */}
              {showModelFields && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">
                    Model ID <span className="text-nord-11">*</span>
                  </label>
                  <input
                    type="text"
                    name="model_id"
                    value={value.model_id || ''}
                    onChange={handleChange}
                    placeholder="microsoft/deberta-v3-base or /local/path"
                    required
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                  <p className="text-xs text-nord-4 mt-1">HuggingFace model ID or local path</p>
                </div>
              )}

              {/* Alias */}
              {showAlias && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">
                    Alias <span className="text-nord-11">*</span>
                  </label>
                  <input
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
                    placeholder="classifier:deberta"
                    required
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>
              )}

              {/* Device */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Device</label>
                <select
                  name="device"
                  value={value.device || 'auto'}
                  onChange={handleChange}
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                >
                  {DEVICE_OPTIONS.map((d) => (
                    <option key={d} value={d}>
                      {d === 'auto' ? 'auto (detect)' : d}
                    </option>
                  ))}
                </select>
              </div>

              {/* Dtype */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Data Type</label>
                <select
                  name="dtype"
                  value={value.dtype || 'auto'}
                  onChange={handleChange}
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                >
                  {DTYPE_OPTIONS.map((d) => (
                    <option key={d} value={d}>
                      {d === 'auto' ? 'auto (detect)' : d}
                    </option>
                  ))}
                </select>
              </div>

              {/* Max Length */}
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Max Length</label>
                <input
                  type="number"
                  name="max_length"
                  value={value.max_length}
                  onChange={handleChange}
                  min="1"
                  className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                />
              </div>

              {/* Classification-specific: Labels */}
              {huggingFaceMode === 'classifier' && (
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Labels (Optional)</label>
                  <input
                    type="text"
                    value={labelsInput}
                    onChange={(e) => {
                      setLabelsInput(e.target.value);
                      onChange({
                        ...value,
                        labels: e.target.value
                          .split(',')
                          .map((l) => l.trim())
                          .filter((l) => l),
                      });
                    }}
                    placeholder="positive, negative, neutral"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    Comma-separated list of label names. Leave empty to use model defaults.
                  </p>
                </div>
              )}

              {/* Embedding-specific: Normalize Embeddings */}
              {huggingFaceMode === 'embedding' && (
                <div className="flex items-start gap-3">
                  <input
                    type="checkbox"
                    id="normalize_embeddings"
                    name="normalize_embeddings"
                    checked={!!value.normalize_embeddings}
                    onChange={handleChange}
                    className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
                  />
                  <div>
                    <label htmlFor="normalize_embeddings" className="block text-sm font-medium text-nord-4">
                      Normalize Embeddings
                    </label>
                    <p className="text-xs text-nord-4">L2 normalize output embedding vectors</p>
                  </div>
                </div>
              )}

              {/* Trust Remote Code */}
              <div className="flex items-start gap-3">
                <input
                  type="checkbox"
                  id="trust_remote_code"
                  name="trust_remote_code"
                  checked={!!value.trust_remote_code}
                  onChange={handleChange}
                  className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
                />
                <div>
                  <label htmlFor="trust_remote_code" className="block text-sm font-medium text-nord-4">
                    Trust Remote Code
                  </label>
                  <p className="text-xs text-nord-4">Allow running custom model code from HuggingFace</p>
                </div>
              </div>

              {/* Causal-specific: Flash Attention */}
              {huggingFaceMode === 'causal' && (
                <div className="flex items-start gap-3">
                  <input
                    type="checkbox"
                    id="use_flash_attention"
                    name="use_flash_attention"
                    checked={!!value.use_flash_attention}
                    onChange={handleChange}
                    className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
                  />
                  <div>
                    <label htmlFor="use_flash_attention" className="block text-sm font-medium text-nord-4">
                      Use Flash Attention 2
                    </label>
                    <p className="text-xs text-nord-4">
                      Enable Flash Attention for faster inference (requires compatible GPU)
                    </p>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
