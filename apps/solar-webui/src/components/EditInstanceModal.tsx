import { useState } from 'react';
import { X, MessageSquare, Tags, Binary, Search } from 'lucide-react';
import {
  Instance,
  InstanceConfig,
  getBackendType,
  getFullModelLabel,
  isLlamaCppConfig,
  isHuggingFaceCausalConfig,
  isHuggingFaceClassificationConfig,
  isHuggingFaceEmbeddingConfig,
  LlamaCppConfig,
  HuggingFaceCausalConfig,
  HuggingFaceClassificationConfig,
  HuggingFaceEmbeddingConfig,
} from '@/api/types';

interface EditInstanceModalProps {
  instance: Instance;
  hostId: string;
  onClose: () => void;
  onUpdate: (hostId: string, instanceId: string, config: InstanceConfig) => Promise<void>;
}

const DEVICE_OPTIONS = ['auto', 'cuda', 'mps', 'cpu'];
const DTYPE_OPTIONS = ['auto', 'float16', 'bfloat16', 'float32'];

const BackendIcon = ({ config }: { config: InstanceConfig }) => {
  const backendType = getBackendType(config);

  if (isLlamaCppConfig(config)) {
    const llamaConfig = config as LlamaCppConfig;
    switch (llamaConfig.model_type) {
      case 'embedding':
        return <Binary size={18} className="text-nord-15" />;
      case 'reranker':
        return <Search size={18} className="text-nord-12" />;
      default:
        return <MessageSquare size={18} className="text-nord-10" />;
    }
  }

  switch (backendType) {
    case 'huggingface_causal':
      return <MessageSquare size={18} className="text-nord-14" />;
    case 'huggingface_classification':
      return <Tags size={18} className="text-nord-13" />;
    case 'huggingface_embedding':
      return <Binary size={18} className="text-nord-15" />;
    default:
      return <MessageSquare size={18} className="text-nord-4" />;
  }
};

export function EditInstanceModal({ instance, hostId, onClose, onUpdate }: EditInstanceModalProps) {
  const [loading, setLoading] = useState(false);
  const [formData, setFormData] = useState<InstanceConfig>(() => {
    // Initialize with correct backend_type and special handling
    if (isLlamaCppConfig(instance.config)) {
      return {
        ...instance.config,
        backend_type: 'llamacpp',
        special: (instance.config as LlamaCppConfig).special ?? false,
        model_type: (instance.config as LlamaCppConfig).model_type || 'llm',
        pooling: (instance.config as LlamaCppConfig).pooling,
      } as LlamaCppConfig;
    }
    return { ...instance.config } as InstanceConfig;
  });

  // Handle labels as comma-separated string
  const [labelsInput, setLabelsInput] = useState(() => {
    if (isHuggingFaceClassificationConfig(instance.config)) {
      return (instance.config as HuggingFaceClassificationConfig).labels?.join(', ') || '';
    }
    return '';
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Validate required fields based on backend type
    if (isLlamaCppConfig(formData)) {
      if (!(formData as LlamaCppConfig).model || !formData.alias) {
        alert('Model Path and Alias are required');
        return;
      }
    } else {
      if (!(formData as HuggingFaceCausalConfig).model_id || !formData.alias) {
        alert('Model ID and Alias are required');
        return;
      }
    }

    // Parse labels for classification models
    const finalConfig = { ...formData };
    if (isHuggingFaceClassificationConfig(formData) && labelsInput.trim()) {
      (finalConfig as HuggingFaceClassificationConfig).labels = labelsInput
        .split(',')
        .map((l) => l.trim())
        .filter((l) => l);
    }

    // Strip empty strings from optional fields so the backend receives None
    // and llama.cpp uses its own defaults
    if (isLlamaCppConfig(formData)) {
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
      if (!c.spec_type || c.model_type !== 'llm') {
        delete c.spec_type;
        delete c.spec_draft_n_max;
      }
    }

    setLoading(true);
    try {
      await onUpdate(hostId, instance.id, finalConfig);
      onClose();
    } catch (error: any) {
      console.error('Failed to update instance:', error);
      alert(`Failed to update instance: ${error.response?.data?.detail || error.message}`);
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

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto border border-nord-3">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-nord-3 sticky top-0 bg-nord-1 z-10">
          <div className="flex items-center gap-2">
            <BackendIcon config={instance.config} />
            <h2 className="text-xl font-bold text-nord-6">Edit {getFullModelLabel(instance.config)} Instance</h2>
          </div>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={20} />
          </button>
        </div>

        {/* Info Banner */}
        <div className="p-4 bg-nord-13 bg-opacity-20 border-b border-nord-13">
          <p className="text-sm text-nord-13">
            ⚠️ Instance must be stopped to edit configuration. Changes take effect on next start.
          </p>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Backend-specific fields */}
            {isLlamaCppConfig(formData) ? (
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
                    value={(formData as LlamaCppConfig).model}
                    onChange={handleChange}
                    placeholder="/path/to/model.gguf"
                    required
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>

                {/* Multimodal projector (vision) */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Multimodal projector (Optional)</label>
                  <input
                    type="text"
                    name="mmproj"
                    value={(formData as LlamaCppConfig).mmproj || ''}
                    onChange={handleChange}
                    placeholder="/path/to/mmproj.gguf"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    Passed to llama-server as <code>--mmproj</code> for vision-capable models.
                  </p>
                </div>

                {/* Multimodal projector GPU offload — only when mmproj is set */}
                {(formData as LlamaCppConfig).mmproj && (
                  <div className="md:col-span-2">
                    <label className="block text-sm font-medium text-nord-4 mb-1">Projector GPU Offload</label>
                    <select
                      name="mmproj_offload"
                      value={(formData as LlamaCppConfig).mmproj_offload === false ? 'false' : 'true'}
                      onChange={(e) => setFormData((prev) => ({ ...prev, mmproj_offload: e.target.value === 'true' }))}
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

                {/* Special Flag */}
                <div className="md:col-span-2 flex items-start gap-3">
                  <div className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      id="special"
                      name="special"
                      checked={!!(formData as LlamaCppConfig).special}
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

                {/* Override Tensor (ot) */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Override Tensor (ot) (Optional)</label>
                  <input
                    type="text"
                    name="ot"
                    value={(formData as LlamaCppConfig).ot || ''}
                    onChange={handleChange}
                    placeholder="Override tensor string"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    Override tensor string passed to llama-server as <code>-ot</code> flag.
                  </p>
                </div>

                {/* Model Type */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Model Type</label>
                  <select
                    name="model_type"
                    value={(formData as LlamaCppConfig).model_type || 'llm'}
                    onChange={handleChange}
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  >
                    <option value="llm">LLM</option>
                    <option value="embedding">Embedding</option>
                    <option value="reranker">Reranker</option>
                  </select>
                  <p className="text-xs text-nord-4 mt-1">
                    Select the model type. Embedding and Reranker models will add the respective flags to llama-server.
                  </p>
                </div>

                {/* Pooling */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Pooling (Optional)</label>
                  <select
                    name="pooling"
                    value={(formData as LlamaCppConfig).pooling || ''}
                    onChange={handleChange}
                    disabled={(formData as LlamaCppConfig).model_type !== 'embedding'}
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <option value="">Default - Unspecified</option>
                    <option value="none">None</option>
                    <option value="mean">Mean</option>
                    <option value="cls">CLS</option>
                    <option value="last">Last</option>
                    <option value="rank">Rank</option>
                  </select>
                  <p className="text-xs text-nord-4 mt-1">
                    Pooling strategy for embedding models. Only valid when Model Type is set to Embedding.
                  </p>
                </div>

                {/* Alias */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">
                    Alias <span className="text-nord-11">*</span>
                  </label>
                  <input
                    type="text"
                    name="alias"
                    value={formData.alias}
                    onChange={handleChange}
                    placeholder="model-name:size"
                    required
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>

                {/* Chat Template File */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Chat Template File (Optional)</label>
                  <input
                    type="text"
                    name="chat_template_file"
                    value={(formData as LlamaCppConfig).chat_template_file || ''}
                    onChange={handleChange}
                    placeholder="/path/to/template.jinja"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>

                {/* Chat Template Kwargs */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Chat Template Kwargs (Optional)</label>
                  <input
                    type="text"
                    name="chat_template_kwargs"
                    value={(formData as LlamaCppConfig).chat_template_kwargs || ''}
                    onChange={handleChange}
                    placeholder='{"enable_thinking":true}'
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent font-mono text-sm"
                  />
                  <p className="text-xs text-nord-4 mt-1">
                    JSON string passed to llama-server as <code>--chat-template-kwargs</code>.
                  </p>
                </div>

                {/* Reasoning */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Reasoning (Optional)</label>
                  <select
                    name="reasoning"
                    value={(formData as LlamaCppConfig).reasoning || ''}
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

                {/* Reasoning Budget */}
                <div className="md:col-span-2">
                  <label className="block text-sm font-medium text-nord-4 mb-1">Reasoning Budget (Optional)</label>
                  <input
                    type="number"
                    name="reasoning_budget"
                    value={(formData as LlamaCppConfig).reasoning_budget ?? ''}
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

                {/* Draft MTP speculative decoding - LLM only */}
                {(formData as LlamaCppConfig).model_type === 'llm' && (
                  <div className="md:col-span-2 rounded-md border border-nord-3 bg-nord-2 p-3">
                    <div className="flex items-start gap-3">
                      <input
                        type="checkbox"
                        id="edit-spec-draft-mtp"
                        checked={(formData as LlamaCppConfig).spec_type === 'draft-mtp'}
                        onChange={(e) =>
                          setFormData((prev) => {
                            if (e.target.checked) {
                              return { ...prev, spec_type: 'draft-mtp', spec_draft_n_max: 2 };
                            }
                            const next = { ...prev } as LlamaCppConfig;
                            delete next.spec_type;
                            delete next.spec_draft_n_max;
                            return next;
                          })
                        }
                        className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
                      />
                      <div className="flex-1">
                        <label htmlFor="edit-spec-draft-mtp" className="block text-sm font-medium text-nord-4">
                          Draft MTP speculative decoding
                        </label>
                        <p className="text-xs text-nord-4 mt-1">
                          Enable faster generation for compatible MTP models. Disabled by default.
                        </p>
                      </div>
                    </div>
                    {(formData as LlamaCppConfig).spec_type === 'draft-mtp' && (
                      <div className="mt-3 pl-7">
                        <label className="block text-sm font-medium text-nord-4 mb-1" htmlFor="edit-spec-draft-n-max">
                          Maximum draft tokens
                        </label>
                        <input
                          type="number"
                          id="edit-spec-draft-n-max"
                          name="spec_draft_n_max"
                          value={(formData as LlamaCppConfig).spec_draft_n_max ?? 2}
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

                {/* Threads */}
                <div>
                  <label className="block text-sm font-medium text-nord-4 mb-1">Threads</label>
                  <input
                    type="number"
                    name="threads"
                    value={(formData as LlamaCppConfig).threads}
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
                    value={(formData as LlamaCppConfig).n_gpu_layers}
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
                    value={(formData as LlamaCppConfig).ctx_size}
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
                    value={(formData as LlamaCppConfig).cache_type_k || ''}
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
                    value={(formData as LlamaCppConfig).cache_type_v || ''}
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
                    value={(formData as LlamaCppConfig).rope_scaling || ''}
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
                    value={(formData as LlamaCppConfig).rope_scale ?? ''}
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
                    value={(formData as LlamaCppConfig).yarn_orig_ctx ?? ''}
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

                {/* Temperature */}
                <div>
                  <label className="block text-sm font-medium text-nord-4 mb-1">Temperature</label>
                  <input
                    type="number"
                    name="temp"
                    value={(formData as LlamaCppConfig).temp}
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
                    value={(formData as LlamaCppConfig).top_p}
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
                    value={(formData as LlamaCppConfig).top_k}
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
                    value={(formData as LlamaCppConfig).min_p}
                    onChange={handleChange}
                    min="0"
                    max="1"
                    step="0.01"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>
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
                    value={(formData as HuggingFaceCausalConfig).model_id || ''}
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
                    value={formData.alias}
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
                    value={(formData as HuggingFaceCausalConfig).device || 'auto'}
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
                    value={(formData as HuggingFaceCausalConfig).dtype || 'auto'}
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
                    value={(formData as HuggingFaceCausalConfig).max_length}
                    onChange={handleChange}
                    min="1"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
                  />
                </div>

                {/* Classification-specific: Labels */}
                {isHuggingFaceClassificationConfig(formData) && (
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
                {isHuggingFaceEmbeddingConfig(formData) && (
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      id="normalize_embeddings"
                      name="normalize_embeddings"
                      checked={!!(formData as HuggingFaceEmbeddingConfig).normalize_embeddings}
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
                    checked={!!(formData as HuggingFaceCausalConfig).trust_remote_code}
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
                {isHuggingFaceCausalConfig(formData) && (
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      id="use_flash_attention"
                      name="use_flash_attention"
                      checked={!!(formData as HuggingFaceCausalConfig).use_flash_attention}
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
              {loading ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
