import { useState } from 'react';
import { X } from 'lucide-react';
import {
  InstanceConfig,
  LlamaCppConfig,
  HuggingFaceCausalConfig,
  HuggingFaceClassificationConfig,
  getBackendLabel,
} from '@/api/types';
import { BackendConfigFields } from './BackendConfigFields';
import { getBackendTypeFromSelection, getDefaultConfig, stripEmptyOptionalFields } from '@/lib/backendConfig';

interface AddInstanceModalProps {
  hostId: string;
  hostName: string;
  onClose: () => void;
  onCreate: (hostId: string, config: InstanceConfig) => Promise<void>;
}

export function AddInstanceModal({ hostId, hostName, onClose, onCreate }: AddInstanceModalProps) {
  const [loading, setLoading] = useState(false);
  const [formData, setFormData] = useState<Partial<InstanceConfig>>(
    getDefaultConfig('llamacpp', 'llm') as Partial<InstanceConfig>,
  );

  const backendType = getBackendTypeFromSelection(
    (formData.backend_type ?? 'llamacpp').startsWith('huggingface') ? 'huggingface' : 'llamacpp',
    formData.backend_type === 'huggingface_causal'
      ? 'causal'
      : formData.backend_type === 'huggingface_classification'
        ? 'classifier'
        : formData.backend_type === 'huggingface_embedding'
          ? 'embedding'
          : ((formData as Partial<LlamaCppConfig>).model_type ?? 'llm'),
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Validate required fields based on backend type
    if (backendType === 'llamacpp') {
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
              {loading ? 'Creating...' : `Create ${getBackendLabel(backendType)} Instance`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
