import { useState } from 'react';
import { AlertCircle, AlertTriangle, Check, Copy, Globe2, KeyRound, ListChecks, RefreshCw, X } from 'lucide-react';
import solarClient from '@/api/client';
import type { ApiEndpoint, ApiKey } from '@/api/types';
import { useModelScopePreview } from './modelScope';

/** Keep the inherited-scope hint from growing taller than the form. */
const ALIAS_PREVIEW_LIMIT = 8;

interface ApiKeyFormModalProps {
  /** Selectable endpoints (create/reassign). Omit in fixed-endpoint contexts. */
  endpoints?: ApiEndpoint[];
  /** Preselected endpoint for creation calls. */
  defaultEndpointId?: string;
  /** Fixed endpoint — used by key-row actions inside a card. */
  endpoint?: ApiEndpoint;
  /** Present when renaming / toggling / reassigning an existing key. */
  editingKey?: ApiKey;
  /** Present when rotating: the modal confirms and reveals the new key once. */
  rotatingKey?: ApiKey;
  onClose: () => void;
  onDone: () => void;
}

export function ApiKeyFormModal({
  endpoints,
  defaultEndpointId,
  endpoint,
  editingKey,
  rotatingKey,
  onClose,
  onDone,
}: ApiKeyFormModalProps) {
  const targets = endpoints && endpoints.length > 0 ? endpoints : endpoint ? [endpoint] : [];
  const fixedTarget = targets.length <= 1;

  const [endpointId, setEndpointId] = useState(
    endpoint?.id ?? defaultEndpointId ?? editingKey?.endpoint_id ?? targets[0]?.id ?? '',
  );
  const [name, setName] = useState(editingKey?.name ?? rotatingKey?.name ?? '');
  const [description, setDescription] = useState(editingKey?.description ?? rotatingKey?.description ?? '');
  const [generated, setGenerated] = useState<ApiKey | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const isRotating = !!rotatingKey;

  // A key has no scope of its own: it inherits whatever its endpoint serves.
  // Showing that here is what makes reassignment (and the absence of per-key
  // globs) understandable without leaving the modal.
  const selectedEndpoint = targets.find((ep) => ep.id === endpointId);
  const serveAll = selectedEndpoint?.serve_all_models ?? true;
  const { matched, available, ready } = useModelScopePreview(serveAll, selectedEndpoint?.model_patterns ?? []);
  const granted = serveAll ? available : matched;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (
      isRotating &&
      !generated &&
      !confirm(`Rotate key "${rotatingKey?.name}"? The old credential stops working immediately.`)
    ) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      if (isRotating) {
        const newKey = await solarClient.rotateApiKey(rotatingKey!.id);
        setGenerated(newKey);
      } else if (editingKey) {
        await solarClient.updateApiKey(editingKey.id, {
          name,
          description: description || null,
          endpoint_id: endpointId,
        });
        onDone();
      } else {
        const newKey = await solarClient.createApiKey({
          endpoint_id: endpointId,
          name,
          description: description || null,
        });
        setGenerated(newKey);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save API key');
    } finally {
      setLoading(false);
    }
  };

  const copyGenerated = () => {
    if (!generated) return;
    navigator.clipboard?.writeText(generated.key).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
      <div className="bg-nord-1 rounded-lg shadow-2xl w-full max-w-md border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-semibold text-nord-6 flex items-center gap-2">
            {isRotating ? (
              <RefreshCw size={18} className="text-nord-10" />
            ) : (
              <KeyRound size={18} className="text-nord-10" />
            )}
            {generated
              ? isRotating
                ? 'Key Rotated'
                : 'API Key Created'
              : isRotating
                ? 'Rotate API Key'
                : editingKey
                  ? 'Edit API Key'
                  : 'Add API Key'}
          </h2>
          <button onClick={onClose} className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>

        {generated ? (
          <div className="p-4 space-y-4">
            <div className="p-3 bg-nord-14 bg-opacity-15 text-nord-14 rounded-md text-sm">
              {isRotating
                ? 'The old key no longer works. The new one is shown here and also visible in the list.'
                : 'Your new key is shown here and visible in the list at any time.'}
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 px-3 py-2 bg-nord-2 border border-nord-3 rounded-md text-sm text-nord-5 font-mono break-all">
                {generated.key}
              </code>
              <button
                onClick={copyGenerated}
                className="p-2 rounded bg-nord-3 text-nord-6 hover:bg-nord-2 transition-colors"
                title="Copy key"
              >
                {copied ? <Check size={16} className="text-nord-13" /> : <Copy size={16} />}
              </button>
            </div>
            <button
              onClick={onDone}
              className="w-full px-4 py-2 bg-nord-10 text-nord-6 rounded-md hover:bg-nord-9 transition-colors font-medium"
            >
              Done
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="p-4 space-y-4">
            {error && (
              <div className="p-3 bg-nord-11 bg-opacity-20 text-nord-11 rounded-md text-sm border border-nord-11 flex items-start gap-2">
                <AlertCircle className="flex-shrink-0" size={16} />
                {error}
              </div>
            )}
            <div>
              <label className="block text-sm font-medium mb-1 text-nord-4">Endpoint</label>
              <select
                value={endpointId}
                onChange={(e) => setEndpointId(e.target.value)}
                disabled={fixedTarget}
                required
                className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10 disabled:opacity-60"
              >
                {targets.map((ep) => (
                  <option key={ep.id} value={ep.id}>
                    {ep.name}
                  </option>
                ))}
              </select>
              {fixedTarget && <p className="text-xs text-nord-5 mt-1">Key binds to this endpoint.</p>}
            </div>

            <div className="p-3 rounded-md bg-nord-2 border border-nord-3 space-y-2">
              <p className="flex items-center gap-1.5 text-xs font-medium text-nord-4">
                {serveAll ? <Globe2 size={13} /> : <ListChecks size={13} />}
                {serveAll
                  ? 'Reaches all registered models'
                  : ready
                    ? `Reaches ${granted.length} model${granted.length === 1 ? '' : 's'}`
                    : 'Resolving model access…'}
              </p>
              {ready && !serveAll && granted.length === 0 ? (
                <p className="flex items-start gap-2 text-xs text-nord-12">
                  <AlertTriangle size={13} className="shrink-0 mt-0.5" />
                  This endpoint serves no models right now, so the key cannot resolve any request.
                </p>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {granted.slice(0, ALIAS_PREVIEW_LIMIT).map((alias) => (
                    <code
                      key={alias}
                      className="px-1.5 py-0.5 bg-nord-1 border border-nord-3 rounded text-xs text-nord-5 font-mono"
                    >
                      {alias}
                    </code>
                  ))}
                  {granted.length > ALIAS_PREVIEW_LIMIT && (
                    <span className="text-xs text-nord-4">+{granted.length - ALIAS_PREVIEW_LIMIT} more</span>
                  )}
                </div>
              )}
              <p className="text-xs text-nord-5">
                Model access is a property of the endpoint — edit the endpoint to change it.
              </p>
            </div>
            {!isRotating && (
              <>
                <div>
                  <label className="block text-sm font-medium mb-1 text-nord-4">Name</label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. CI runner"
                    required
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1 text-nord-4">Description (optional)</label>
                  <input
                    type="text"
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="What uses this key?"
                    className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10"
                  />
                </div>
              </>
            )}
            {isRotating && (
              <p className="text-sm text-nord-5 flex items-center gap-2">
                <RefreshCw size={14} />
                Rotating replaces the credential for "{rotatingKey?.name}". The old key stops working immediately, new
                one is shown after the rotation.
              </p>
            )}
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
                {loading ? 'Working...' : isRotating ? 'Rotate' : editingKey ? 'Save' : 'Create'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
