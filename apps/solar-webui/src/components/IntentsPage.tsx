/**
 * IntentsPage — declarative intent list (U-003, spec deployment-intent.md §12.2).
 *
 * Data source: the global event-stream `intents` map (intent_update /
 * intent_removed events) overlaid on a REST-bootstrapped + 10 s polling copy.
 * Both writes are full records — "latest record wins", events take precedence.
 * The polling fallback keeps the page useful against an S-040-only backend
 * that never emits events.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { AlertCircle, RefreshCw, Target, Trash2, Plus, Pencil } from 'lucide-react';
import solarClient from '@/api/client';
import { Intent, IntentCreateRequest } from '@/api/types';
import { useEventStreamContext } from '@/context/EventStreamContext';
import type { PullProgressEvent } from '@/hooks/useEventStream';
import { useFallbackPolling } from '@/hooks/useFallbackPolling';
import { formatRelativeTime } from '@/lib/utils';
import { sortIntents } from '@/lib/intents';
import { IntentPhaseBadge } from './IntentBadges';
import { IntentFormModal } from './IntentFormModal';
import { DeleteIntentModal } from './DeleteIntentModal';

const POLL_INTERVAL_MS = 10_000;

/** C4: compact live-pull indicator for the intents table. */
function pullProgressRow(intent: Intent, lookup: (sourceUri: string) => PullProgressEvent | undefined): ReactNode {
  const progress = lookup(intent.model_source);
  if (!progress || progress.data.phase !== 'downloading') return null;
  const total = progress.data.bytes_total ?? 0;
  const done = progress.data.bytes_done ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <span className="block text-[11px] text-nord-8">
      ↓ {pct}% · {(done / 1024 / 1024).toFixed(1)} MB
    </span>
  );
}

export function IntentsPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { intents: eventIntents, getPullProgress, isConnected } = useEventStreamContext();

  const [restIntents, setRestIntents] = useState<Map<string, Intent>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showNewIntent, setShowNewIntent] = useState(false);
  const [intentInitial, setIntentInitial] = useState<Partial<IntentCreateRequest> | undefined>(undefined);
  const [editTarget, setEditTarget] = useState<Intent | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Intent | null>(null);

  // Events overwrite REST records (both are full records)
  const intents = useMemo(() => {
    const merged = new Map(restIntents);
    for (const [id, intent] of eventIntents) merged.set(id, intent);
    return merged;
  }, [restIntents, eventIntents]);

  const fetchList = useCallback(async () => {
    try {
      const records = await solarClient.getIntents();
      setRestIntents((prev) => {
        const next = new Map(prev);
        for (const record of records) next.set(record.id, record);
        return next;
      });
      setError(null);
    } catch (err: any) {
      // Keep showing data we already have; surface the banner only when empty
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : (err?.message ?? 'Failed to load intents'));
    } finally {
      setLoading(false);
    }
  }, []);

  // Bootstrap on mount
  useEffect(() => {
    fetchList();
  }, [fetchList]);

  // Polling fallback (C5): the event stream is the source while connected;
  // REST polls only when the socket is down.
  useFallbackPolling(fetchList, { enabled: !isConnected, intervalMs: POLL_INTERVAL_MS });

  // U-002 handoff: /intents?alias=...&model_source=... auto-opens the pre-filled modal
  const aliasParam = searchParams.get('alias');
  const modelSourceParam = searchParams.get('model_source');
  useEffect(() => {
    if (aliasParam) {
      setIntentInitial({ alias: aliasParam, model_source: modelSourceParam ?? undefined });
      setShowNewIntent(true);
      setSearchParams({}, { replace: true });
    }
  }, [aliasParam, modelSourceParam, setSearchParams]);

  const openNewIntent = () => {
    setIntentInitial(undefined);
    setShowNewIntent(true);
  };

  const handleCreated = (intent: Intent) => {
    setShowNewIntent(false);
    navigate(`/intents/${intent.id}`);
  };

  const handleEdited = (intent: Intent) => {
    setEditTarget(null);
    setRestIntents((prev) => new Map(prev).set(intent.id, intent));
  };

  const handleDeleted = () => {
    if (deleteTarget) {
      setRestIntents((prev) => {
        const next = new Map(prev);
        next.delete(deleteTarget.id);
        return next;
      });
    }
    setDeleteTarget(null);
  };

  // Stable order: phase, then alias. Sorting by updated_at (the previous
  // behaviour) reordered rows on every status update and caused misclicks.
  const sorted = useMemo(() => sortIntents([...intents.values()]), [intents]);

  return (
    <div className="bg-nord-0">
      {/* Header */}
      <header className="bg-nord-1 shadow-lg">
        <div className="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-nord-6">Intents</h1>
              <p className="text-sm text-nord-4 mt-1">Deployments — Solar Control chooses which hosts run them.</p>
            </div>
            <div className="flex gap-2 items-center">
              <button
                onClick={fetchList}
                disabled={loading}
                className="flex items-center gap-2 px-4 py-2 bg-nord-3 text-nord-6 rounded-lg hover:bg-nord-2 transition-colors disabled:opacity-50"
              >
                <RefreshCw size={18} className={loading ? 'animate-spin' : ''} />
                Refresh
              </button>
              <button
                onClick={openNewIntent}
                className="flex items-center gap-2 px-4 py-2 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors font-medium"
              >
                <Plus size={18} /> New Intent
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {loading && intents.size === 0 ? (
          <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
            <div className="text-center">
              <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-nord-9 mx-auto mb-4"></div>
              <p className="text-nord-4">Loading...</p>
            </div>
          </div>
        ) : error && intents.size === 0 ? (
          <div className="mb-6 p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div>
              <h3 className="font-semibold text-nord-6">Intents unavailable</h3>
              <p className="text-sm text-nord-4">{error}</p>
              <button
                onClick={fetchList}
                className="mt-2 text-sm text-nord-8 hover:text-nord-6 flex items-center gap-1"
              >
                <RefreshCw size={14} /> Retry
              </button>
            </div>
          </div>
        ) : intents.size === 0 ? (
          <div className="text-center py-16">
            <Target size={64} className="mx-auto text-nord-3 mb-4" />
            <h2 className="text-2xl font-semibold text-nord-6 mb-2">No intents yet</h2>
            <p className="text-nord-4 mb-6">Describe a deployment and Solar Control will run it.</p>
            <button
              onClick={openNewIntent}
              className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
            >
              <Plus size={20} /> New Intent
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-nord-3 text-left text-xs text-nord-4">
                  <th className="py-2 pr-4 font-medium">Phase</th>
                  <th className="py-2 pr-4 font-medium">Alias</th>
                  <th className="py-2 pr-4 font-medium">Model source</th>
                  <th className="py-2 pr-4 font-medium">Replicas</th>
                  <th className="py-2 pr-4 font-medium">Priority</th>
                  <th className="py-2 pr-4 font-medium">Strategy</th>
                  <th className="py-2 pr-4 font-medium">Updated</th>
                  <th className="py-2 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((intent) => (
                  <tr
                    key={intent.id}
                    onClick={() => navigate(`/intents/${intent.id}`)}
                    className="border-b border-nord-3 hover:bg-nord-2 cursor-pointer transition-colors"
                  >
                    <td className="py-2.5 pr-4">
                      <IntentPhaseBadge phase={intent.status?.phase} reconcile={intent.status?.reconcile} />
                    </td>
                    <td className="py-2.5 pr-4 font-mono text-nord-6">
                      {intent.alias}
                      {intent.status?.strategy_progress?.message && (
                        <span
                          className="block text-[11px] font-normal text-nord-4 truncate max-w-[280px]"
                          title={intent.status.strategy_progress.message}
                        >
                          {intent.status.strategy_progress.message}
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 pr-4">
                      <span
                        className="font-mono text-xs text-nord-4 block max-w-[280px] truncate"
                        title={intent.model_source}
                      >
                        {intent.model_source}
                      </span>
                      {/* C4: compact live-pull indicator */}
                      {pullProgressRow(intent, getPullProgress)}
                    </td>
                    <td className="py-2.5 pr-4 text-nord-6">
                      {intent.status?.ready_replicas ?? 0}/{intent.replicas}
                    </td>
                    <td className="py-2.5 pr-4">
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-nord-3 text-nord-4">
                        {intent.priority}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-nord-4">{intent.strategy}</td>
                    <td className="py-2.5 pr-4 text-nord-4 whitespace-nowrap">
                      {formatRelativeTime(intent.status?.updated_at ?? intent.status?.created_at ?? undefined)}
                    </td>
                    <td className="py-2.5 text-right whitespace-nowrap">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setEditTarget(intent);
                        }}
                        className="p-1.5 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors"
                        title="Edit intent"
                      >
                        <Pencil size={16} />
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setDeleteTarget(intent);
                        }}
                        className="p-1.5 rounded hover:bg-nord-11 hover:bg-opacity-20 text-nord-4 hover:text-nord-11 transition-colors"
                        title="Delete intent"
                      >
                        <Trash2 size={16} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>

      {showNewIntent && (
        <IntentFormModal initial={intentInitial} onClose={() => setShowNewIntent(false)} onSaved={handleCreated} />
      )}
      {editTarget && <IntentFormModal intent={editTarget} onClose={() => setEditTarget(null)} onSaved={handleEdited} />}
      {deleteTarget && (
        <DeleteIntentModal intent={deleteTarget} onClose={() => setDeleteTarget(null)} onDeleted={handleDeleted} />
      )}
    </div>
  );
}
