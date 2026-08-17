import { useState } from 'react';
import { X } from 'lucide-react';
import solarClient from '@/api/client';
import type { ApiEndpoint } from '@/api/types';
import { ModelScopeEditor } from './ModelScopeEditor';
import { combinePatterns, splitPatterns, useModelScopePreview } from './modelScope';

interface EndpointFormModalProps {
  /** Present when editing an existing endpoint; absent for creation. */
  endpoint?: ApiEndpoint;
  onClose: () => void;
  onCreated?: (ep: ApiEndpoint) => void;
  onSaved?: (ep: ApiEndpoint) => void;
}

export function EndpointFormModal({ endpoint, onClose, onCreated, onSaved }: EndpointFormModalProps) {
  const initialScope = splitPatterns(endpoint?.model_patterns);
  const [name, setName] = useState(endpoint?.name ?? '');
  const [description, setDescription] = useState(endpoint?.description ?? '');
  const [serveAll, setServeAll] = useState(endpoint?.serve_all_models ?? true);
  const [selected, setSelected] = useState<string[]>(initialScope.selected);
  const [globs, setGlobs] = useState<string[]>(initialScope.globs);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isEdit = !!endpoint;
  const patterns = combinePatterns({ selected, globs });
  const { matched, available, ready } = useModelScopePreview(serveAll, patterns);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      // The scope is sent whichever mode is active: keeping the patterns while
      // "All models" is on makes the toggle non-destructive, and the gateway
      // ignores them as long as serve_all_models is set.
      const payload = {
        name,
        description: description || null,
        serve_all_models: serveAll,
        model_patterns: patterns,
      };
      const saved = endpoint
        ? await solarClient.updateEndpoint(endpoint.id, payload)
        : await solarClient.createEndpoint(payload);
      if (endpoint) onSaved?.(saved);
      else onCreated?.(saved);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save endpoint');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
      <div className="bg-nord-1 rounded-lg shadow-2xl w-full max-w-2xl border border-nord-3 max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-semibold text-nord-6">{isEdit ? 'Edit Endpoint' : 'Create Endpoint'}</h2>
          <button onClick={onClose} className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4 overflow-y-auto">
          {error && (
            <div className="p-3 bg-nord-11 bg-opacity-20 text-nord-11 rounded-md text-sm border border-nord-11">
              {error}
            </div>
          )}
          <div>
            <label className="block text-sm font-medium mb-1 text-nord-4">Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Production API"
              required
              className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1 text-nord-4">Description (optional)</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="e.g. Production environment endpoint"
              rows={2}
              className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10 resize-none"
            />
          </div>

          <div className="pt-2 border-t border-nord-3">
            <ModelScopeEditor
              serveAll={serveAll}
              onServeAllChange={setServeAll}
              selected={selected}
              onSelectedChange={setSelected}
              globs={globs}
              onGlobsChange={setGlobs}
              matched={matched}
              available={available}
              ready={ready}
            />
          </div>

          <div className="flex gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-10 text-nord-6 rounded-md hover:bg-nord-9 transition-colors disabled:opacity-50 font-medium"
            >
              {loading ? 'Saving...' : isEdit ? 'Save' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
