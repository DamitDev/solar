import { useState, useEffect, useCallback } from 'react';
import { Plus, RefreshCw, AlertCircle, Key, Eye, EyeOff, Pencil, Trash2, BarChart3, X } from 'lucide-react';
import solarClient from '@/api/client';
import type { ApiEndpoint, EndpointUsageResponse } from '@/api/types';

function maskApiKey(key: string): string {
  if (!key || key.length <= 8) return '••••••••';
  return key.slice(0, 8) + '...';
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  } catch {
    return iso;
  }
}

interface EndpointCardProps {
  endpoint: ApiEndpoint;
  usage: EndpointUsageResponse | null;
  onEdit: (ep: ApiEndpoint) => void;
  onDelete: (ep: ApiEndpoint) => void;
}

function EndpointCard({ endpoint, usage, onEdit, onDelete }: EndpointCardProps) {
  const [showKey, setShowKey] = useState(false);

  const u = usage?.usage;
  const totalRequests = u?.total_requests ?? 0;
  const totalTokens = u?.total_tokens ?? 0;
  const avgLatency = u?.avg_duration_s != null ? u.avg_duration_s.toFixed(2) : '—';

  return (
    <div className="bg-nord-1 rounded-lg border border-nord-3 p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <h3 className="text-lg font-semibold text-nord-6 truncate">{endpoint.name}</h3>
          {endpoint.description && <p className="text-sm text-nord-4 mt-1 line-clamp-2">{endpoint.description}</p>}
          <div className="mt-3 flex items-center gap-2">
            <code className="flex items-center gap-1.5 px-2 py-1 bg-nord-2 rounded text-sm text-nord-5 font-mono break-all">
              <Key size={14} className="text-nord-4 shrink-0" />
              {showKey ? endpoint.api_key : maskApiKey(endpoint.api_key)}
            </code>
            <button
              onClick={() => setShowKey(!showKey)}
              className="p-1.5 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-6 transition-colors"
              title={showKey ? 'Hide key' : 'Show key'}
            >
              {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          <p className="text-xs text-nord-4 mt-2">Created {formatDate(endpoint.created_at)}</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onEdit(endpoint)}
            className="p-2 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-6 transition-colors"
            title="Edit"
          >
            <Pencil size={18} />
          </button>
          <button
            onClick={() => onDelete(endpoint)}
            className="p-2 rounded hover:bg-nord-11 hover:bg-opacity-20 text-nord-4 hover:text-nord-11 transition-colors"
            title="Delete"
          >
            <Trash2 size={18} />
          </button>
        </div>
      </div>
      <div className="mt-4 pt-4 border-t border-nord-3 flex items-center gap-4 text-sm">
        <div className="flex items-center gap-1.5 text-nord-4">
          <BarChart3 size={16} />
          <span>Requests: {totalRequests.toLocaleString()}</span>
        </div>
        <div className="text-nord-4">Tokens: {totalTokens.toLocaleString()}</div>
        <div className="text-nord-4">Avg latency: {avgLatency}s</div>
      </div>
    </div>
  );
}

interface CreateEndpointModalProps {
  onClose: () => void;
  onSuccess: () => void;
}

function CreateEndpointModal({ onClose, onSuccess }: CreateEndpointModalProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await solarClient.createEndpoint({ name, description: description || undefined });
      onSuccess();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create endpoint');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
      <div className="bg-nord-1 rounded-lg shadow-2xl w-full max-w-md border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-semibold text-nord-6">Create API Endpoint</h2>
          <button onClick={onClose} className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
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
          <p className="text-xs text-nord-4">An API key will be generated automatically.</p>
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
              {loading ? 'Creating...' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface EditEndpointModalProps {
  endpoint: ApiEndpoint;
  onClose: () => void;
  onSuccess: () => void;
}

function EditEndpointModal({ endpoint, onClose, onSuccess }: EditEndpointModalProps) {
  const [name, setName] = useState(endpoint.name);
  const [description, setDescription] = useState(endpoint.description ?? '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await solarClient.updateEndpoint(endpoint.id, {
        name,
        description: description || null,
      });
      onSuccess();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update endpoint');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
      <div className="bg-nord-1 rounded-lg shadow-2xl w-full max-w-md border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-semibold text-nord-6">Edit Endpoint</h2>
          <button onClick={onClose} className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
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
              {loading ? 'Saving...' : 'Save'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function EndpointsDashboard() {
  const [endpoints, setEndpoints] = useState<ApiEndpoint[]>([]);
  const [usageMap, setUsageMap] = useState<Record<string, EndpointUsageResponse>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState<ApiEndpoint | null>(null);

  const fetchEndpoints = useCallback(async () => {
    try {
      const data = await solarClient.getEndpoints();
      setEndpoints(data);
      setError(null);
      return data;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load endpoints');
      setEndpoints([]);
      return [];
    }
  }, []);

  const fetchUsage = useCallback(async (id: string) => {
    try {
      const data = await solarClient.getEndpointUsage(id, 24);
      setUsageMap((prev) => ({ ...prev, [id]: data }));
    } catch {
      // ignore per-endpoint usage errors
    }
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    const list = await fetchEndpoints();
    setUsageMap({});
    await Promise.all(list.map((ep) => fetchUsage(ep.id)));
    setRefreshing(false);
  }, [fetchEndpoints, fetchUsage]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      const list = await fetchEndpoints();
      if (cancelled) return;
      setLoading(false);
      list.forEach((ep) => fetchUsage(ep.id));
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchEndpoints, fetchUsage]);

  const handleDelete = async (ep: ApiEndpoint) => {
    if (!confirm(`Delete endpoint "${ep.name}"? This cannot be undone.`)) return;
    try {
      await solarClient.deleteEndpoint(ep.id);
      await refresh();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete endpoint');
    }
  };

  if (loading && endpoints.length === 0) {
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-nord-9 mx-auto mb-4"></div>
          <p className="text-nord-4">Loading...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-nord-0">
      <header className="bg-nord-1 shadow-lg">
        <div className="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-nord-6">API Endpoints</h1>
              <p className="text-sm text-nord-4 mt-1">Manage multi-tenant API keys and usage</p>
            </div>
            <div className="flex gap-2 items-center">
              <button
                onClick={refresh}
                disabled={refreshing}
                className="flex items-center gap-2 px-4 py-2 bg-nord-3 text-nord-6 rounded-lg hover:bg-nord-2 transition-colors disabled:opacity-50"
              >
                <RefreshCw size={18} className={refreshing ? 'animate-spin' : ''} />
                Refresh
              </button>
              <button
                onClick={() => setShowCreate(true)}
                className="flex items-center gap-2 px-4 py-2 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
              >
                <Plus size={18} />
                New Endpoint
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {error && (
          <div className="mb-6 p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div>
              <h3 className="font-semibold text-nord-6">Error</h3>
              <p className="text-sm text-nord-4">{error}</p>
            </div>
          </div>
        )}

        {endpoints.length === 0 ? (
          <div className="text-center py-16">
            <Key size={64} className="mx-auto text-nord-3 mb-4" />
            <h2 className="text-2xl font-semibold text-nord-6 mb-2">No API endpoints</h2>
            <p className="text-nord-4 mb-6">Create an endpoint to get an API key for your applications</p>
            <button
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
            >
              <Plus size={20} />
              Create Endpoint
            </button>
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {endpoints.map((ep) => (
              <EndpointCard
                key={ep.id}
                endpoint={ep}
                usage={usageMap[ep.id] ?? null}
                onEdit={setEditing}
                onDelete={handleDelete}
              />
            ))}
          </div>
        )}
      </main>

      {showCreate && <CreateEndpointModal onClose={() => setShowCreate(false)} onSuccess={refresh} />}
      {editing && (
        <EditEndpointModal
          endpoint={editing}
          onClose={() => setEditing(null)}
          onSuccess={() => {
            refresh();
            setEditing(null);
          }}
        />
      )}
    </div>
  );
}
