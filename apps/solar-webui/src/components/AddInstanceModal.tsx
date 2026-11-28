import { useState } from 'react';
import { X } from 'lucide-react';
import {
  InstanceConfig,
  LlamaCppConfig,
  HuggingFaceCausalConfig,
  HuggingFaceClassificationConfig,
  HuggingFaceAudioConfig,
  WhisperCppConfig,
  NemoSpeechConfig,
  SglangConfig,
  VllmConfig,
  getBackendLabel,
} from '@/api/types';
import { BackendConfigFields } from './BackendConfigFields';
import { getBackendTypeFromSelection, getDefaultConfig, stripEmptyOptionalFields } from '@/lib/backendConfig';

interface AddInstanceModalProps {
  hostId: string;
  hostName: string;
  /** Accelerator the host reports; SGLang needs 'nvidia_cuda'. */
  hostGpuType?: string;
  /** Backends the host advertises; empty means it has not said (no opinion). */
  hostSupportedBackends?: string[];
  onClose: () => void;
  onCreate: (hostId: string, config: InstanceConfig) => Promise<void>;
}

export function AddInstanceModal({
  hostId,
  hostName,
  hostGpuType,
  hostSupportedBackends,
  onClose,
  onCreate,
}: AddInstanceModalProps) {
  const [loading, setLoading] = useState(false);
  const [formData, setFormData] = useState<Partial<InstanceConfig>>(
    getDefaultConfig('llamacpp', 'llm') as Partial<InstanceConfig>,
  );

  const backendType = getBackendTypeFromSelection(
    formData.backend_type === 'sglang'
      ? 'sglang'
      : formData.backend_type === 'vllm'
        ? 'vllm'
        : formData.backend_type === 'whispercpp'
          ? 'whispercpp'
          : formData.backend_type === 'nemospeech'
            ? 'nemospeech'
            : (formData.backend_type ?? 'llamacpp').startsWith('huggingface')
              ? 'huggingface'
              : 'llamacpp',
    formData.backend_type === 'huggingface_causal'
      ? 'causal'
      : formData.backend_type === 'huggingface_classification'
        ? 'classifier'
        : formData.backend_type === 'huggingface_embedding'
          ? 'embedding'
          : formData.backend_type === 'huggingface_audio'
            ? 'audio'
            : ((formData as Partial<LlamaCppConfig>).model_type ?? 'llm'),
  );

  // SGLang and vLLM run on CUDA only, and each is a separate install the
  // host has to advertise — the server rejects the mismatch, so the button is
  // disabled rather than offered and refused.
  const sglangUnavailable =
    hostGpuType !== 'nvidia_cuda' ||
    (hostSupportedBackends !== undefined &&
      hostSupportedBackends.length > 0 &&
      !hostSupportedBackends.includes('sglang'));
  const sglangReason =
    hostGpuType !== 'nvidia_cuda' ? 'Requires an NVIDIA host' : `SGLang is not installed on ${hostName}`;
  const vllmUnavailable =
    hostGpuType !== 'nvidia_cuda' ||
    (hostSupportedBackends !== undefined &&
      hostSupportedBackends.length > 0 &&
      !hostSupportedBackends.includes('vllm'));
  const vllmReason = hostGpuType !== 'nvidia_cuda' ? 'Requires an NVIDIA host' : `vLLM is not installed on ${hostName}`;
  // The GGML speech runtimes are separate installs too, but they run on the
  // CPU, so only the advertised-backend check applies to them.
  const hostAdvertises = (name: string) =>
    hostSupportedBackends === undefined || hostSupportedBackends.length === 0 || hostSupportedBackends.includes(name);
  const whisperUnavailable = !hostAdvertises('whispercpp');
  const whisperReason = `whisper-server is not installed on ${hostName}`;
  const nemoUnavailable = !hostAdvertises('nemospeech');
  const nemoReason = `nemo-speech is not installed on ${hostName}`;

  // Selecting HuggingFace before choosing a mode leaves the form without a
  // backend_type, so naming llama.cpp in the submit button was a lie. Stay
  // neutral until a mode supplies the backend type.
  const hasBackend = formData.backend_type !== undefined && String(formData.backend_type) !== 'huggingface';
  const submitLabel = hasBackend ? `Create ${getBackendLabel(backendType)} Instance` : 'Create Instance';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Validate required fields based on backend type
    if (backendType === 'llamacpp') {
      const config = formData as Partial<LlamaCppConfig>;
      if (!config.model || !config.alias) {
        alert('Model Path and Alias are required');
        return;
      }
    } else if (backendType === 'sglang') {
      const config = formData as Partial<SglangConfig>;
      if (!config.model_path || !config.alias) {
        alert('Model Path and Alias are required');
        return;
      }
    } else if (backendType === 'vllm') {
      const config = formData as Partial<VllmConfig>;
      if (!config.model_path || !config.alias) {
        alert('Model Path and Alias are required');
        return;
      }
    } else if (backendType === 'whispercpp') {
      const config = formData as Partial<WhisperCppConfig>;
      if (!config.model || !config.alias) {
        alert('Model Path and Alias are required');
        return;
      }
    } else if (backendType === 'nemospeech') {
      const config = formData as Partial<NemoSpeechConfig>;
      if (!config.model || !config.alias) {
        alert('Decoder GGUF and Alias are required');
        return;
      }
    } else {
      const config = formData as Partial<
        HuggingFaceCausalConfig | HuggingFaceClassificationConfig | HuggingFaceAudioConfig
      >;
      if (!config.model_id || !config.alias) {
        alert('Model ID and Alias are required');
        return;
      }
    }

    // Strip empty strings from optional fields so the backend receives None
    // and llama.cpp uses its own defaults
    const finalConfig = stripEmptyOptionalFields({ ...formData }) as Partial<InstanceConfig>;

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
          <BackendConfigFields
            value={formData as Record<string, any>}
            onChange={(next) => setFormData(next as Partial<InstanceConfig>)}
            showAlias
            showModelFields
            aliasValue={formData.alias}
            onAliasChange={(v) => setFormData((prev) => ({ ...prev, alias: v }))}
            disabledBackends={[
              ...(sglangUnavailable ? (['sglang'] as const) : []),
              ...(vllmUnavailable ? (['vllm'] as const) : []),
              ...(whisperUnavailable ? (['whispercpp'] as const) : []),
              ...(nemoUnavailable ? (['nemospeech'] as const) : []),
            ]}
            disabledReasons={{
              ...(sglangUnavailable ? { sglang: sglangReason } : {}),
              ...(vllmUnavailable ? { vllm: vllmReason } : {}),
              ...(whisperUnavailable ? { whispercpp: whisperReason } : {}),
              ...(nemoUnavailable ? { nemospeech: nemoReason } : {}),
            }}
          />

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
              {loading ? 'Creating...' : submitLabel}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
