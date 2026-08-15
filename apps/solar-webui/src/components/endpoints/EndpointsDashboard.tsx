import { useState, useCallback, useEffect } from 'react';
import { Plus, RefreshCw, AlertCircle, KeyRound } from 'lucide-react';
import solarClient from '@/api/client';
import type { ApiEndpoint, ApiKey, EndpointUsageResponse } from '@/api/types';
import { useEventStreamContext } from '@/context/EventStreamContext';
import { useFallbackPolling } from '@/hooks/useFallbackPolling';
import { EndpointCard } from './EndpointCard';
import { EndpointFormModal } from './EndpointFormModal';
import { ApiKeyFormModal } from './ApiKeyFormModal';
import { DeleteEndpointModal } from './DeleteEndpointModal';

export function EndpointsDashboard() {
  const { endpoints: eventEndpoints, apiKeys: eventApiKeys, isConnected } = useEventStreamContext();
  const [endpoints, setEndpoints] = useState<ApiEndpoint[]>([]);
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [usageMap, setUsageMap] = useState<Record<string, EndpointUsageResponse>>({});
  const [modelsMap, setModelsMap] = useState<Record<string, { count: number; aliases: string[] }>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState<ApiEndpoint | null>(null);
  const [deleting, setDeleting] = useState<ApiEndpoint | null>(null);
  const [addingKeyFor, setAddingKeyFor] = useState<ApiEndpoint | null>(null);

  const fetchAll = useCallback(async () => {
    try {
      const [eps, keys] = await Promise.all([solarClient.getEndpoints(), solarClient.getApiKeys()]);
      setEndpoints(eps);
      setApiKeys(keys);
      setError(null);
      return eps;
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

  const fetchModels = useCallback(async (id: string) => {
    try {
      const data = await solarClient.getEndpointModels(id);
      setModelsMap((prev) => ({ ...prev, [id]: { count: data.count, aliases: data.aliases } }));
    } catch {
      // ignore per-endpoint model-list errors
    }
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const eps = await fetchAll();
      setUsageMap({});
      setModelsMap({});
      await Promise.all(eps.flatMap((ep) => [fetchUsage(ep.id), fetchModels(ep.id)]));
    } finally {
      setRefreshing(false);
    }
  }, [fetchAll, fetchUsage, fetchModels]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      const eps = await fetchAll();
      if (cancelled) return;
      setLoading(false);
      eps.forEach((ep) => {
        fetchUsage(ep.id);
        fetchModels(ep.id);
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchAll, fetchUsage, fetchModels]);

  // Event-driven updates: the socket only fires on CRUD, so a fresh page
  // still needs the REST load above; afterwards live edits arrive here.
  useEffect(() => {
    if (eventEndpoints.length > 0) setEndpoints(eventEndpoints);
  }, [eventEndpoints]);

  useEffect(() => {
    if (eventApiKeys.length > 0) setApiKeys(eventApiKeys);
  }, [eventApiKeys]);

  // Disconnected fallback: degraded to polling.
  useFallbackPolling(fetchAll, { enabled: !isConnected });

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
              <p className="text-sm text-nord-4 mt-1">Endpoints own keys and declare which models they serve</p>
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

        {endpoints.length === 0 && !loading ? (
          <div className="text-center py-16">
            <KeyRound size={64} className="mx-auto text-nord-3 mb-4" />
            <h2 className="text-2xl font-semibold text-nord-6 mb-2">No API endpoints</h2>
            <p className="text-nord-4 mb-6">Create an endpoint, then attach API keys to it</p>
            <button
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
            >
              <Plus size={20} />
              Create Endpoint
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            {endpoints.map((ep) => (
              <EndpointCard
                key={ep.id}
                endpoint={ep}
                keys={apiKeys.filter((k) => k.endpoint_id === ep.id)}
                usage={usageMap[ep.id] ?? null}
                models={modelsMap[ep.id] ?? null}
                onEdit={setEditing}
                onDelete={setDeleting}
                onAddKey={() => setAddingKeyFor(ep)}
                onKeysChanged={refresh}
              />
            ))}
          </div>
        )}
      </main>

      {showCreate && (
        <EndpointFormModal
          onClose={() => setShowCreate(false)}
          onCreated={(ep) => {
            setEndpoints((prev) => [ep, ...prev]);
            setShowCreate(false);
          }}
        />
      )}
      {editing && (
        <EndpointFormModal
          endpoint={editing}
          onClose={() => setEditing(null)}
          onSaved={(ep) => {
            setEndpoints((prev) => prev.map((e) => (e.id === ep.id ? ep : e)));
            setEditing(null);
          }}
        />
      )}
      {deleting && (
        <DeleteEndpointModal
          endpoint={deleting}
          keyCount={apiKeys.filter((k) => k.endpoint_id === deleting.id).length}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            setEndpoints((prev) => prev.filter((e) => e.id !== deleting.id));
            setApiKeys((prev) => prev.filter((k) => k.endpoint_id !== deleting.id));
            setDeleting(null);
          }}
        />
      )}
      {addingKeyFor && (
        <ApiKeyFormModal
          endpoints={endpoints}
          defaultEndpointId={addingKeyFor.id}
          onClose={() => setAddingKeyFor(null)}
          onDone={() => {
            setAddingKeyFor(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}
