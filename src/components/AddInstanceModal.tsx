import { useState } from 'react';
import { X, Cpu, Brain, MessageSquare, Binary, Tags, Search } from 'lucide-react';
import {
  InstanceConfig,
  BackendType,
  LlamaCppConfig,
  HuggingFaceCausalConfig,
  HuggingFaceClassificationConfig,
  HuggingFaceEmbeddingConfig,
  getBackendLabel,
} from '@/api/types';

interface AddInstanceModalProps {
  hostId: string;
  hostName: string;
  onClose: () => void;
  onCreate: (hostId: string, config: InstanceConfig) => Promise<void>;
}

// Primary backend type
type PrimaryBackend = 'llamacpp' | 'huggingface';

// Mode types
type LlamaCppMode = 'llm' | 'embedding' | 'reranker';
type HuggingFaceMode = 'causal' | 'classifier' | 'embedding';

type ModeOption = {
  value: string;
  label: string;
  icon: typeof Cpu;
  description: string;
};

const LLAMACPP_MODES: ModeOption[] = [
  { value: 'llm', label: 'Text Generation', icon: MessageSquare, description: 'Chat & text completion' },
  { value: 'embedding', label: 'Embedding', icon: Binary, description: 'Vector embeddings' },
  { value: 'reranker', label: 'Reranker', icon: Search, description: 'Document reranking' },
];

const HUGGINGFACE_MODES: ModeOption[] = [
  { value: 'causal', label: 'Causal LM', icon: MessageSquare, description: 'Text generation models' },
  { value: 'classifier', label: 'Classifier', icon: Tags, description: 'Sequence classification' },
  { value: 'embedding', label: 'Embedding', icon: Binary, description: 'Embedding models' },
];

const DEVICE_OPTIONS = ['auto', 'cuda', 'mps', 'cpu'];
const DTYPE_OPTIONS = ['auto', 'float16', 'bfloat16', 'float32'];

// Helper to get BackendType from selections
const getBackendTypeFromSelection = (primary: PrimaryBackend, mode: string): BackendType => {
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
const getDefaultConfig = (primary: PrimaryBackend, mode: string): Partial<InstanceConfig> => {
  const base = {
    host: '0.0.0.0',
    api_key: 'aiops',
  };

  if (primary === 'llamacpp') {
    return {
      ...base,
      backend_type: 'llamacpp',
      model: '',
      mmproj: '',
      alias: '',
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
    } as Partial<LlamaCppConfig>;
  }

  // HuggingFace modes
  switch (mode) {
    case 'causal':
      return {
        ...base,
        backend_type: 'huggingface_causal',
        model_id: '',
        alias: '',
        device: 'auto',
        dtype: 'auto',
        max_length: 4096,
        trust_remote_code: false,
        use_flash_attention: false,
      } as Partial<HuggingFaceCausalConfig>;
    case 'classifier':
      return {
        ...base,
        backend_type: 'huggingface_classification',
        model_id: '',
        alias: '',
        device: 'auto',
        dtype: 'auto',
        max_length: 512,
        labels: [],
        trust_remote_code: false,
      } as Partial<HuggingFaceClassificationConfig>;
    case 'embedding':
      return {
        ...base,
        backend_type: 'huggingface_embedding',
        model_id: '',
        alias: '',
        device: 'auto',
        dtype: 'auto',
        max_length: 512,
        normalize_embeddings: true,
        trust_remote_code: false,
      } as Partial<HuggingFaceEmbeddingConfig>;
    default:
      return base;
  }
};

export function AddInstanceModal({ hostId, hostName, onClose, onCreate }: AddInstanceModalProps) {
  const [loading, setLoading] = useState(false);
  const [primaryBackend, setPrimaryBackend] = useState<PrimaryBackend>('llamacpp');
  const [llamaCppMode, setLlamaCppMode] = useState<LlamaCppMode>('llm');
  const [huggingFaceMode, setHuggingFaceMode] = useState<HuggingFaceMode>('causal');
  const [formData, setFormData] = useState<Partial<InstanceConfig>>(getDefaultConfig('llamacpp', 'llm'));
  const [labelsInput, setLabelsInput] = useState('');

  const currentMode = primaryBackend === 'llamacpp' ? llamaCppMode : huggingFaceMode;
  const backendType = getBackendTypeFromSelection(primaryBackend, currentMode);

  const handlePrimaryBackendChange = (newBackend: PrimaryBackend) => {
    setPrimaryBackend(newBackend);
    const mode = newBackend === 'llamacpp' ? llamaCppMode : huggingFaceMode;
    setFormData(getDefaultConfig(newBackend, mode));
    setLabelsInput('');
  };

  const handleModeChange = (mode: string) => {
    if (primaryBackend === 'llamacpp') {
      setLlamaCppMode(mode as LlamaCppMode);
    } else {
      setHuggingFaceMode(mode as HuggingFaceMode);
    }
    setFormData(getDefaultConfig(primaryBackend, mode));
    setLabelsInput('');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Validate required fields based on backend type
    if (primaryBackend === 'llamacpp') {
      const config = formData as Partial<LlamaCppConfig>;
      if (!config.model || !config.alias) {
        alert('Model Path and Alias are required');
        return;
      }
    } else {
      const config = formData as Partial<HuggingFaceCausalConfig | HuggingFaceClassificationConfig>;
      if (!config.model_id || !config.alias) {
        alert('Model ID and Alias are required');
        return;
      }
    }

    // Parse labels for classification models
    const finalConfig = { ...formData };
    if (backendType === 'huggingface_classification' && labelsInput.trim()) {
      (finalConfig as Partial<HuggingFaceClassificationConfig>).labels = labelsInput
        .split(',')
        .map((l) => l.trim())
        .filter((l) => l);
    }

    // Strip empty strings from optional fields so the backend receives None
    // and llama.cpp uses its own defaults
    if (primaryBackend === 'llamacpp') {
      const c = finalConfig as Partial<LlamaCppConfig>;
      if (!c.cache_type_k) delete c.cache_type_k;
      if (!c.cache_type_v) delete c.cache_type_v;
      if (!c.rope_scaling) delete c.rope_scaling;
      if (!c.chat_template_file) delete c.chat_template_file;
      if (!c.chat_template_kwargs) delete c.chat_template_kwargs;
      if (!c.reasoning) delete c.reasoning;
      if (!c.ot) delete c.ot;
      if (!c.mmproj) delete c.mmproj;
      if (!c.pooling) delete c.pooling;
      if (!c.spec_type) delete c.spec_draft_n_max;
    }

    setLoading(true);
    try {
      await onCreate(hostId, finalConfig as InstanceConfig);
      onClose();
    } catch (error: any) {
      console.error('Failed to create instance:', error);
      alert(`Failed to create instance: ${error.response?.data?.detail || error.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value, type } = e.target;
    const checked = (e.target as HTMLInputElement).checked;

    setFormData((prev) => ({
      ...prev,
      [name]:
        type === 'number' ? (value === '' ? undefined : parseFloat(value)) : type === 'checkbox' ? checked : value,
    }));
  };

  const modeOptions = primaryBackend === 'llamacpp' ? LLAMACPP_MODES : HUGGINGFACE_MODES;

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto border border-nord-3">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-nord-3 sticky top-0 bg-nord-1 z-10">
          <h2 className="text-xl font-bold text-nord-6">Add Instance to {hostName}</h2>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={20} />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-6 space-y-6">
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
                  <div className="md:col-span-2">
                    <label className="block text-sm font-medium text-nord-4 mb-1">
                      Model Path <span className="text-nord-11">*</span>
                    </label>
                    <input
                      type="text"
                      name="model"
                      value={(formData as Partial<LlamaCppConfig>).model || ''}
                      onChange={handleChange}
                      placeholder="/path/to/model.gguf"
                      required
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                  </div>

                  {/* Multimodal projector (vision) — LLM only */}
                  {llamaCppMode === 'llm' && (
                    <div className="md:col-span-2">
                      <label className="block text-sm font-medium text-nord-4 mb-1">
                        Multimodal projector (Optional)
                      </label>
                      <input
                        type="text"
                        name="mmproj"
                        value={(formData as Partial<LlamaCppConfig>).mmproj || ''}
                        onChange={handleChange}
                        placeholder="/path/to/mmproj.gguf"
                        className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                      />
                      <p className="text-xs text-nord-4 mt-1">
                        Passed to llama-server as <code>--mmproj</code> for vision-capable models.
                      </p>
                    </div>
                  )}

                  {/* Multimodal projector GPU offload — only when mmproj is set */}
                  {llamaCppMode === 'llm' && (formData as Partial<LlamaCppConfig>).mmproj && (
                    <div className="md:col-span-2">
                      <label className="block text-sm font-medium text-nord-4 mb-1">Projector GPU Offload</label>
                      <select
                        name="mmproj_offload"
                        value={(formData as Partial<LlamaCppConfig>).mmproj_offload === false ? 'false' : 'true'}
                        onChange={(e) =>
                          setFormData((prev) => ({ ...prev, mmproj_offload: e.target.value === 'true' }))
                        }
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
                  <div className="md:col-span-2">
                    <label className="block text-sm font-medium text-nord-4 mb-1">
                      Alias <span className="text-nord-11">*</span>
                    </label>
                    <input
                      type="text"
                      name="alias"
                      value={formData.alias || ''}
                      onChange={handleChange}
                      placeholder="model-name:size"
                      required
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                  </div>

                  {/* Chat Template File - only for LLM mode */}
                  {llamaCppMode === 'llm' && (
                    <div className="md:col-span-2">
                      <label className="block text-sm font-medium text-nord-4 mb-1">
                        Chat Template File (Optional)
                      </label>
                      <input
                        type="text"
                        name="chat_template_file"
                        value={(formData as Partial<LlamaCppConfig>).chat_template_file || ''}
                        onChange={handleChange}
                        placeholder="/path/to/template.jinja"
                        className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                      />
                    </div>
                  )}

                  {/* Chat Template Kwargs - only for LLM mode */}
                  {llamaCppMode === 'llm' && (
                    <div className="md:col-span-2">
                      <label className="block text-sm font-medium text-nord-4 mb-1">
                        Chat Template Kwargs (Optional)
                      </label>
                      <input
                        type="text"
                        name="chat_template_kwargs"
                        value={(formData as Partial<LlamaCppConfig>).chat_template_kwargs || ''}
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
                        value={(formData as Partial<LlamaCppConfig>).reasoning || ''}
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
                        value={(formData as Partial<LlamaCppConfig>).reasoning_budget ?? ''}
                        onChange={handleChange}
                        placeholder="-1 = unrestricted, 0 = disable, blank = omit"
                        min="-1"
                        step="1"
                        className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                      />
                      <p className="text-xs text-nord-4 mt-1">
                        Passed as <code>--reasoning-budget</code>. Use <code>-1</code> for unrestricted, <code>0</code>{' '}
                        to disable thinking. Leave blank to omit.
                      </p>
                    </div>
                  )}

                  {/* Draft MTP speculative decoding - only for LLM mode */}
                  {llamaCppMode === 'llm' && (
                    <div className="md:col-span-2 rounded-md border border-nord-3 bg-nord-2 p-3">
                      <div className="flex items-start gap-3">
                        <input
                          type="checkbox"
                          id="add-spec-draft-mtp"
                          checked={(formData as Partial<LlamaCppConfig>).spec_type === 'draft-mtp'}
                          onChange={(e) =>
                            setFormData((prev) => {
                              if (e.target.checked) {
                                return { ...prev, spec_type: 'draft-mtp', spec_draft_n_max: 2 };
                              }
                              const next = { ...prev } as Partial<LlamaCppConfig>;
                              delete next.spec_type;
                              delete next.spec_draft_n_max;
                              return next;
                            })
                          }
                          className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
                        />
                        <div className="flex-1">
                          <label htmlFor="add-spec-draft-mtp" className="block text-sm font-medium text-nord-4">
                            Draft MTP speculative decoding
                          </label>
                          <p className="text-xs text-nord-4 mt-1">
                            Enable faster generation for compatible MTP models. Disabled by default.
                          </p>
                        </div>
                      </div>
                      {(formData as Partial<LlamaCppConfig>).spec_type === 'draft-mtp' && (
                        <div className="mt-3 pl-7">
                          <label className="block text-sm font-medium text-nord-4 mb-1" htmlFor="add-spec-draft-n-max">
                            Maximum draft tokens
                          </label>
                          <input
                            type="number"
                            id="add-spec-draft-n-max"
                            name="spec_draft_n_max"
                            value={(formData as Partial<LlamaCppConfig>).spec_draft_n_max ?? 2}
                            onChange={handleChange}
                            min="1"
                            step="1"
                            required
                            className="w-full px-3 py-2 bg-nord-1 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                          />
                          <p className="text-xs text-nord-4 mt-1">
                            Launches with <code>--spec-type draft-mtp --spec-draft-n-max 2</code> by default.
                          </p>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Special Flag - only for LLM mode */}
                  {llamaCppMode === 'llm' && (
                    <div className="md:col-span-2 flex items-start gap-3">
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          id="special"
                          name="special"
                          checked={!!(formData as Partial<LlamaCppConfig>).special}
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
                    <label className="block text-sm font-medium text-nord-4 mb-1">
                      Override Tensor (ot) (Optional)
                    </label>
                    <input
                      type="text"
                      name="ot"
                      value={(formData as Partial<LlamaCppConfig>).ot || ''}
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
                        value={(formData as Partial<LlamaCppConfig>).pooling || ''}
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
                      value={(formData as Partial<LlamaCppConfig>).threads}
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
                      value={(formData as Partial<LlamaCppConfig>).n_gpu_layers}
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
                      value={(formData as Partial<LlamaCppConfig>).ctx_size}
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
                      value={(formData as Partial<LlamaCppConfig>).cache_type_k || ''}
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
                      value={(formData as Partial<LlamaCppConfig>).cache_type_v || ''}
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
                      value={(formData as Partial<LlamaCppConfig>).rope_scaling || ''}
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
                      value={(formData as Partial<LlamaCppConfig>).rope_scale ?? ''}
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
                      value={(formData as Partial<LlamaCppConfig>).yarn_orig_ctx ?? ''}
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
                          value={(formData as Partial<LlamaCppConfig>).temp}
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
                          value={(formData as Partial<LlamaCppConfig>).top_p}
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
                          value={(formData as Partial<LlamaCppConfig>).top_k}
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
                          value={(formData as Partial<LlamaCppConfig>).min_p}
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
                  <div className="md:col-span-2">
                    <label className="block text-sm font-medium text-nord-4 mb-1">
                      Model ID <span className="text-nord-11">*</span>
                    </label>
                    <input
                      type="text"
                      name="model_id"
                      value={(formData as Partial<HuggingFaceCausalConfig>).model_id || ''}
                      onChange={handleChange}
                      placeholder="microsoft/deberta-v3-base or /local/path"
                      required
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                    <p className="text-xs text-nord-4 mt-1">HuggingFace model ID or local path</p>
                  </div>

                  {/* Alias */}
                  <div className="md:col-span-2">
                    <label className="block text-sm font-medium text-nord-4 mb-1">
                      Alias <span className="text-nord-11">*</span>
                    </label>
                    <input
                      type="text"
                      name="alias"
                      value={formData.alias || ''}
                      onChange={handleChange}
                      placeholder="classifier:deberta"
                      required
                      className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                    />
                  </div>

                  {/* Device */}
                  <div>
                    <label className="block text-sm font-medium text-nord-4 mb-1">Device</label>
                    <select
                      name="device"
                      value={(formData as Partial<HuggingFaceCausalConfig>).device || 'auto'}
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
                      value={(formData as Partial<HuggingFaceCausalConfig>).dtype || 'auto'}
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
                      value={(formData as Partial<HuggingFaceCausalConfig>).max_length}
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
                        onChange={(e) => setLabelsInput(e.target.value)}
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
                        checked={!!(formData as Partial<HuggingFaceEmbeddingConfig>).normalize_embeddings}
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
                      checked={!!(formData as Partial<HuggingFaceCausalConfig>).trust_remote_code}
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
                        checked={!!(formData as Partial<HuggingFaceCausalConfig>).use_flash_attention}
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

          {/* Actions */}
          <div className="flex gap-3 pt-4 border-t border-nord-3">
            <button
              type="button"
              onClick={onClose}
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-10 text-nord-6 rounded-md hover:bg-nord-9 transition-colors disabled:opacity-50 font-medium"
            >
              {loading ? 'Creating...' : `Create ${getBackendLabel(backendType)} Instance`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
