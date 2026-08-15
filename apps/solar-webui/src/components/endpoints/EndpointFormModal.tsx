import { useState, useEffect, useCallback } from 'react';
import { X, Plus, Globe, Filter } from 'lucide-react';
import solarClient from '@/api/client';
import type { ApiEndpoint } from '@/api/types';

interface EndpointFormModalProps {
  /** Present when editing an existing endpoint; absent for creation. */
  endpoint?: ApiEndpoint;
  onClose: () => void;
  onCreated?: (ep: ApiEndpoint) => void;
  onSaved?: (ep: ApiEndpoint) => void;
}

export function EndpointFormModal({ endpoint, onClose, onCreated, onSaved }: EndpointFormModalProps) {
  const [name, setName] = useState(endpoint?.name ?? '');
  const [description, setDescription] = useState(endpoint?.description ?? '');
  const [serveAll, setServeAll] = useState(endpoint?.serve_all_models ?? true);
  const [patterns, setPatterns] = useState<string[]>(endpoint?.model_patterns ?? []);
  const [preview, setPreview] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isEdit = !!endpoint;

  // Live preview whenever the scope inputs change — the value is advisory
  // and refreshed on every keystroke, so failures are silently ignored.
  const refreshPreview = useCallback(async (all: boolean, pats: string[]) => {
    try {
      const data = await solarClient.previewEndpointModels({
        serve_all_models: all,
        model_patterns: pats.filter((p) => p.trim() !== ''),
      });
      setPreview(data.aliases);
    } catch {
      setPreview([]);
    }
  }, []);

  // Debounce the preview so rapid pattern typing does not fire a POST on
  // every keystroke; the value is advisory so a failed/raced response is
  // silently ignored (a newer keystroke supersedes it).
  useEffect(() => {
    const timer = setTimeout(() => {
      refreshPreview(serveAll, patterns);
    }, 300);
    return () => clearTimeout(timer);
  }, [serveAll, patterns, refreshPreview]);

  const updatePattern = (index: number, value: string) => {
    setPatterns((prev) => prev.map((p, i) => (i === index ? value : p)));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const payload = {
        name,
        description: description || null,
        serve_all_models: serveAll,
        model_patterns: serveAll ? [] : patterns.filter((p) => p.trim() !== ''),
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

          <div>
            <label className="flex items-center gap-2 text-sm font-medium text-nord-4 cursor-pointer">
              <input
                type="checkbox"
                checked={serveAll}
                onChange={(e) => setServeAll(e.target.checked)}
                className="accent-nord-10"
              />
              <Globe size={16} className="text-nord-4" />
              Serve all registered models
            </label>
            {!serveAll && (
              <div className="mt-3 space-y-2">
                <label className="flex items-center gap-2 text-sm font-medium text-nord-4">
                  <Filter size={16} className="text-nord-4" />
                  Model patterns (fnmatch globs over registry aliases)
                </label>
                {patterns.map((p, i) => (
                  <div key={i} className="flex gap-2">
                    <input
                      type="text"
                      value={p}
                      onChange={(e) => updatePattern(i, e.target.value)}
                      placeholder="e.g. iris-osl:*"
                      className="flex-1 px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10 font-mono text-sm"
                    />
                    <button
                      type="button"
                      onClick={() => setPatterns((prev) => prev.filter((_, j) => j !== i))}
                      className="px-3 py-2 bg-nord-3 text-nord-4 rounded-md hover:bg-nord-11 hover:bg-opacity-20 hover:text-nord-11 transition-colors"
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() => setPatterns((prev) => [...prev, ''])}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-dashed border-nord-3 text-nord-4 hover:text-nord-6 hover:border-nord-10 transition-colors text-sm"
                >
                  <Plus size={14} />
                  Add pattern
                </button>

                <div className="mt-2 p-3 bg-nord-2 border border-nord-3 rounded-md">
                  <p className="text-xs text-nord-4 mb-2">
                    Matched aliases: <span className="text-nord-13">{preview.length}</span>
                  </p>
                  {preview.length === 0 ? (
                    <p className="text-xs text-nord-5">No models match these patterns.</p>
                  ) : (
                    <div className="flex flex-wrap gap-1.5 max-h-32 overflow-y-auto">
                      {preview.map((alias) => (
                        <code key={alias} className="px-1.5 py-0.5 bg-nord-3 rounded text-xs text-nord-5 font-mono">
                          {alias}
                        </code>
                      ))}
                    </div>
                  )}
                </div>
                <p className="text-xs text-nord-5">
                  A request for any model outside this scope returns an OpenAI-shaped <code>model_not_found</code>.
                </p>
              </div>
            )}
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
