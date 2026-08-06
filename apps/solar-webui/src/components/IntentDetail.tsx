/**
 * IntentDetail — per-intent status view (U-003, spec deployment-intent.md
 * §10.1–10.3): replica counts, replica_set, conditions, strategy_progress,
 * last_error, the intent spec, plus edit (§12.5) and delete.
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { AlertCircle, ArrowLeft, ChevronDown, FileText, Pencil, Trash2, TriangleAlert } from 'lucide-react';
import solarClient from '@/api/client';
import { Intent, IntentCondition } from '@/api/types';
import { useEventStreamContext } from '@/context/EventStreamContext';
import type { PullProgressEvent } from '@/hooks/useEventStream';
import { useFallbackPolling } from '@/hooks/useFallbackPolling';
import { cn, formatDateTime, formatRelativeTime } from '@/lib/utils';
import { IntentPhaseBadge } from './IntentBadges';
import { DeleteIntentModal } from './DeleteIntentModal';
import { IntentFormModal } from './IntentFormModal';
import { LogViewer } from './LogViewer';

const DETAIL_POLL_INTERVAL_MS = 5_000;

function StatBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-nord-4 uppercase tracking-wide">{label}</dt>
      <dd className="mt-1 text-sm font-medium text-nord-6">{children}</dd>
    </div>
  );
}

/** C4: compact pull-progress row — live bar while downloading, terminal
 * status line once the host finished (or failed) the pull. */
function pullProgressRow(progress: PullProgressEvent | undefined): ReactNode {
  if (!progress) return null;
  const data = progress.data;
  const total = data.bytes_total ?? 0;
  const done = data.bytes_done ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const speed =
    data.speed_bps != null && data.speed_bps > 0 ? `${(data.speed_bps / 1024 / 1024).toFixed(1)} MB/s` : null;
  const host = progress.host_name ? ` · ${progress.host_name}` : '';

  if (data.phase === 'downloading') {
    return (
      <section>
        <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">Model pull</h4>
        <div className="mt-3 rounded-md border border-nord-3 bg-nord-2 p-4 text-sm">
          <div className="flex items-center justify-between text-xs text-nord-4">
            <span>
              {data.source_uri}
              {host}
            </span>
            <span>
              {total > 0
                ? `${(done / 1024 / 1024).toFixed(1)} / ${(total / 1024 / 1024).toFixed(1)} MB (${pct}%)`
                : `${(done / 1024 / 1024).toFixed(1)} MB`}
              {speed ? ` · ${speed}` : ''}
            </span>
          </div>
          <div className="mt-2 h-1.5 rounded-full bg-nord-3 overflow-hidden">
            <div className="h-full rounded-full bg-nord-8 transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      </section>
    );
  }

  if (data.phase === 'completed') {
    return (
      <p className="mt-3 text-xs text-nord-4">
        Model pull completed{host}: {data.source_uri}
      </p>
    );
  }
  if (data.phase === 'failed') {
    return (
      <p className="mt-3 text-xs text-nord-11">
        Model pull failed{host}: {data.source_uri}
        {data.error ? ` — ${data.error}` : ''}
      </p>
    );
  }
  return null;
}

function ConditionChip({ condition }: { condition: IntentCondition }) {
  const active = condition.status;
  const color = active
    ? condition.type === 'Available'
      ? 'bg-nord-14 text-nord-0'
      : condition.type === 'Progressing'
        ? 'bg-nord-10 text-nord-6'
        : condition.type === 'Conflict'
          ? 'bg-nord-11 text-nord-6'
          : 'bg-nord-12 text-nord-6'
    : 'bg-nord-3 text-nord-4';
  return (
    <span
      title={`${condition.reason} — ${condition.message}`}
      className={cn('px-2 py-0.5 rounded text-xs font-medium', color)}
    >
      {CONDITION_LABELS[condition.type] ?? condition.type}
    </span>
  );
}

// Display-only condition labels; the API contract is untouched.
const CONDITION_LABELS: Record<string, string> = {
  Available: 'Serving',
  Progressing: 'Updating',
  Conflict: 'Conflict',
};

export function IntentDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { intents, getPullProgress, isConnected } = useEventStreamContext();

  const [fetched, setFetched] = useState<Intent | null | undefined>(undefined); // null = 404
  const [error, setError] = useState<string | null>(null);
  const [showDelete, setShowDelete] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  // C2: process-log viewer for a failed instance (from last_error).
  const [logViewer, setLogViewer] = useState<{ hostId: string; instanceId: string } | null>(null);

  const fetchIntent = useCallback(async () => {
    if (!id) return;
    try {
      const record = await solarClient.getIntent(id);
      setFetched(record);
      setError(null);
    } catch (err: any) {
      if (err?.response?.status === 404) {
        setFetched(null);
        setError(null);
      } else {
        setError(err?.response?.data?.detail || err?.message || 'Failed to load intent');
      }
    }
  }, [id]);

  useEffect(() => {
    setFetched(undefined);
    fetchIntent();
  }, [fetchIntent]);

  // C5: fallback polling — REST refreshes only while the socket is down.
  useFallbackPolling(fetchIntent, { enabled: !isConnected, intervalMs: DETAIL_POLL_INTERVAL_MS });

  const intent = (id ? intents.get(id) : undefined) ?? fetched;

  if (!id) return null;

  if (fetched === null) {
    return (
      <div className="bg-nord-0 min-h-screen">
        <main className="max-w-7xl mx-auto px-4 py-16 sm:px-6 lg:px-8 text-center">
          <h1 className="text-2xl font-semibold text-nord-6 mb-2">Intent not found</h1>
          <p className="text-nord-4 mb-6">It may have been deleted.</p>
          <Link
            to="/intents"
            className="inline-flex items-center gap-2 px-4 py-2 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
          >
            <ArrowLeft size={16} /> Back to Intents
          </Link>
        </main>
      </div>
    );
  }

  if (!intent) {
    if (error) {
      return (
        <div className="bg-nord-0 min-h-screen">
          <main className="max-w-7xl mx-auto px-4 py-16 sm:px-6 lg:px-8">
            <div className="p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
              <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
              <div>
                <h3 className="font-semibold text-nord-6">Intent unavailable</h3>
                <p className="text-sm text-nord-4">{error}</p>
                <button
                  onClick={fetchIntent}
                  className="mt-2 text-sm text-nord-8 hover:text-nord-6 flex items-center gap-1"
                >
                  Retry
                </button>
              </div>
            </div>
            <Link
              to="/intents"
              className="mt-6 inline-flex items-center gap-2 px-4 py-2 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
            >
              <ArrowLeft size={16} /> Back to Intents
            </Link>
          </main>
        </div>
      );
    }
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-nord-9 mx-auto mb-4"></div>
          <p className="text-nord-4">Loading...</p>
        </div>
      </div>
    );
  }

  const status = intent.status;
  const pendingStored = status.phase === 'pending' && status.reconcile === 'idle';
  const partialFulfillment = status.phase === 'degraded' && status.shortfall > 0;
  const hasConflict = status.conditions.some((c) => c.type === 'Conflict');
  // C3: advisory warnings returned with the last create/update save.
  const warnings = intent.warnings ?? [];

  const metadataEntries = Object.entries(intent.metadata ?? {});

  return (
    <div className="bg-nord-0">
      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8 space-y-6">
        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Link
                to="/intents"
                className="p-1.5 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-6 transition-colors"
                title="Back to Intents"
              >
                <ArrowLeft size={18} />
              </Link>
              <h1 className="text-2xl font-bold text-nord-6 font-mono break-all">{intent.alias}</h1>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-nord-4">
              <IntentPhaseBadge phase={status.phase} reconcile={status.reconcile} />
              <span>
                Created {formatRelativeTime(status.created_at ?? undefined)} · Updated{' '}
                {formatRelativeTime(status.updated_at ?? undefined)}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowEdit(true)}
              className="flex items-center gap-2 px-4 py-2 bg-nord-3 text-nord-6 rounded-lg hover:bg-nord-2 transition-colors"
            >
              <Pencil size={16} /> Edit
            </button>
            <button
              onClick={() => setShowDelete(true)}
              className="flex items-center gap-2 px-4 py-2 bg-nord-3 text-nord-6 rounded-lg hover:bg-nord-11 hover:bg-opacity-20 hover:text-nord-11 transition-colors"
            >
              <Trash2 size={16} /> Delete
            </button>
          </div>
        </div>

        {error && (
          <div className="p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div>
              <p className="text-sm text-nord-4">{error}</p>
              <button
                onClick={fetchIntent}
                className="mt-2 text-sm text-nord-8 hover:text-nord-6 flex items-center gap-1"
              >
                Retry
              </button>
            </div>
          </div>
        )}

        {/* Stored ≠ running (spec §7.3) */}
        {pendingStored && (
          <div className="p-3 bg-nord-13 bg-opacity-15 border border-nord-13 rounded text-sm text-nord-6 flex items-start gap-2">
            <TriangleAlert size={16} className="flex-shrink-0 mt-0.5" />
            <span>Stored and validated. No instances have been created yet.</span>
          </div>
        )}

        {/* Partial fulfillment is a stable state, not an error (spec §8.6) */}
        {partialFulfillment && (
          <div className="p-3 bg-nord-13 bg-opacity-15 border border-nord-13 rounded text-sm text-nord-6 flex items-start gap-2">
            <TriangleAlert size={16} className="flex-shrink-0 mt-0.5" />
            <span>
              Serving with {status.ready_replicas} of {status.desired_replicas} requested instances — not enough
              capacity right now. Missing instances start automatically as capacity frees up.
              {status.shortfall_reason && (
                <span className="block mt-1 text-xs text-nord-4">{status.shortfall_reason}</span>
              )}
            </span>
          </div>
        )}

        {/* C3: advisory warnings returned with the last save */}
        {warnings.length > 0 && (
          <div className="p-3 bg-nord-13 bg-opacity-15 border border-nord-13 rounded text-sm text-nord-6 flex items-start gap-2">
            <TriangleAlert size={16} className="flex-shrink-0 mt-0.5" />
            <div>
              <p className="font-medium">Saved with warnings</p>
              <ul className="mt-1 space-y-0.5 text-xs text-nord-4">
                {warnings.map((w, i) => (
                  <li key={i}>
                    {w.field}: {w.message}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {/* Stat strip */}
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-6">
          <StatBlock label="Requested">{status.desired_replicas}</StatBlock>
          <StatBlock label="Running">{status.observed_replicas}</StatBlock>
          <StatBlock label="Ready">{status.ready_replicas}</StatBlock>
          <StatBlock label="Up to date">{status.updated_replicas}</StatBlock>
          <StatBlock label="Available">{status.available ? 'yes' : 'no'}</StatBlock>
          <StatBlock label="Missing">{status.shortfall}</StatBlock>
        </dl>

        {/* Replica set */}
        <section>
          <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">Replicas</h4>
          {status.replica_set.length === 0 ? (
            <p className="mt-3 text-sm text-nord-4">No replicas yet</p>
          ) : (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-nord-3 text-left text-xs text-nord-4">
                    <th className="py-2 pr-4 font-medium">Host</th>
                    <th className="py-2 pr-4 font-medium">Instance</th>
                    <th className="py-2 pr-4 font-medium">State</th>
                    <th className="py-2 pr-4 font-medium">Model source</th>
                    <th className="py-2 pr-4 font-medium">Healthy</th>
                    <th className="py-2 pr-4 font-medium">Message</th>
                    <th className="py-2 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {status.replica_set.map((replica, index) => (
                    <tr key={replica.instance_id ?? index} className="border-b border-nord-3">
                      <td className="py-2 pr-4 text-nord-6">{replica.host_name ?? replica.host_id ?? '—'}</td>
                      <td className="py-2 pr-4 font-mono text-xs text-nord-4">{replica.instance_id ?? '—'}</td>
                      <td className="py-2 pr-4 text-nord-4">{replica.state ?? '—'}</td>
                      <td className="py-2 pr-4">
                        <span
                          className="font-mono text-xs text-nord-4 block max-w-[240px] truncate"
                          title={replica.model_source ?? undefined}
                        >
                          {replica.model_source ?? '—'}
                        </span>
                      </td>
                      <td className="py-2 pr-4">
                        {replica.healthy ? (
                          <span className="text-nord-14">✓</span>
                        ) : (
                          <span className="text-nord-11">✗</span>
                        )}
                      </td>
                      <td className="py-2 pr-4">
                        {replica.message ? (
                          <span className="block max-w-[280px] truncate text-xs text-nord-4" title={replica.message}>
                            {replica.message}
                          </span>
                        ) : (
                          <span className="text-nord-4">—</span>
                        )}
                      </td>
                      <td className="py-2 text-nord-4 whitespace-nowrap">
                        {formatRelativeTime(replica.updated_at ?? undefined)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* Conditions */}
        {status.conditions.length > 0 && (
          <section>
            <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">Conditions</h4>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {status.conditions.map((condition) => (
                <ConditionChip key={condition.type} condition={condition} />
              ))}
            </div>
            {hasConflict && (
              <p className="mt-2 text-xs text-nord-4">
                A manual instance is using this name on a candidate host — stop it on the Hosts page to continue.
              </p>
            )}
          </section>
        )}

        {/* Strategy progress */}
        {status.strategy_progress && (
          <section>
            <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">Update progress</h4>
            <div className="mt-3 rounded-md border border-nord-3 bg-nord-2 p-4 text-sm">
              <div className="flex flex-wrap gap-x-6 gap-y-2 text-nord-4">
                <span>
                  Strategy: <span className="text-nord-6 font-medium">{status.strategy_progress.strategy}</span>
                </span>
                {status.strategy_progress.step && (
                  <span>
                    Step: <span className="text-nord-6 font-medium">{status.strategy_progress.step}</span>
                  </span>
                )}
                <span>
                  Updated: <span className="text-nord-6 font-medium">{status.strategy_progress.updated}</span>
                </span>
                <span>
                  In progress: <span className="text-nord-6 font-medium">{status.strategy_progress.in_progress}</span>
                </span>
                <span>
                  Failed: <span className="text-nord-6 font-medium">{status.strategy_progress.failed}</span>
                </span>
              </div>
              {status.strategy_progress.message && (
                <p className="mt-2 text-xs text-nord-4">{status.strategy_progress.message}</p>
              )}
            </div>
          </section>
        )}

        {/* C4: live model pull progress for the intent's model source */}
        {intent.model_source && pullProgressRow(getPullProgress(intent.model_source))}

        {/* Last error — C2: links to the failed instance's process logs */}
        {status.last_error && (
          <div className="p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div className="text-sm flex-1 min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <p className="font-semibold text-nord-11">
                  {status.last_error.code}
                  {status.last_error.host_id && (
                    <span className="font-normal text-nord-4"> · host {status.last_error.host_id}</span>
                  )}
                  {status.last_error.source_uri && (
                    <span className="font-normal text-nord-4"> · {status.last_error.source_uri}</span>
                  )}
                </p>
                {status.last_error.recoverable && (
                  <span className="px-1.5 py-0.5 rounded bg-nord-13 bg-opacity-30 text-nord-6 text-[10px] uppercase tracking-wide">
                    host still working
                  </span>
                )}
              </div>
              <p className="text-nord-4 mt-1">{status.last_error.message}</p>
              {status.last_error.log_tail && status.last_error.log_tail.length > 0 && (
                <pre className="mt-2 rounded-md bg-nord-1 border border-nord-11 border-opacity-40 p-2 text-[11px] font-mono text-nord-4 overflow-x-auto max-h-40 overflow-y-auto">
                  {status.last_error.log_tail.join('\n')}
                </pre>
              )}
              {status.last_error.host_id && status.last_error.instance_id && (
                <button
                  onClick={() =>
                    setLogViewer({
                      hostId: status.last_error!.host_id!,
                      instanceId: status.last_error!.instance_id!,
                    })
                  }
                  className="mt-2 text-xs text-nord-8 hover:text-nord-6 flex items-center gap-1"
                >
                  <FileText size={14} /> View process logs
                </button>
              )}
              <p className="text-xs text-nord-4 mt-1">{formatDateTime(status.last_error.at)}</p>
            </div>
          </div>
        )}

        {/* Intent spec */}
        <details className="group border border-nord-3 rounded-md">
          <summary className="flex items-center justify-between px-4 py-3 cursor-pointer select-none list-none">
            <span className="text-sm font-medium text-nord-4">Configuration</span>
            <ChevronDown size={16} className="text-nord-4 transition-transform group-open:rotate-180" />
          </summary>
          <div className="px-4 pb-4 space-y-4">
            <div>
              <h5 className="text-xs text-nord-4 mb-2">Backend</h5>
              <pre className="rounded-md bg-nord-2 border border-nord-3 p-3 text-xs font-mono text-nord-4 overflow-x-auto">
                {JSON.stringify(intent.backend ?? {}, null, 2)}
              </pre>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <h5 className="text-xs text-nord-4 mb-2">Placement</h5>
                <pre className="rounded-md bg-nord-2 border border-nord-3 p-3 text-xs font-mono text-nord-4 overflow-x-auto">
                  {JSON.stringify(intent.placement ?? {}, null, 2)}
                </pre>
              </div>
              <div>
                <h5 className="text-xs text-nord-4 mb-2">Resources</h5>
                <pre className="rounded-md bg-nord-2 border border-nord-3 p-3 text-xs font-mono text-nord-4 overflow-x-auto">
                  {JSON.stringify(intent.resources ?? {}, null, 2)}
                </pre>
              </div>
              <div>
                <h5 className="text-xs text-nord-4 mb-2">Metadata</h5>
                {metadataEntries.length === 0 ? (
                  <p className="text-sm text-nord-4">—</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {metadataEntries.map(([key, value]) => (
                      <span
                        key={key}
                        className="px-2 py-1 rounded bg-nord-2 border border-nord-3 text-xs font-mono text-nord-4"
                      >
                        {key}={value}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        </details>

        {/* Footer timestamps */}
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs text-nord-4 sm:grid-cols-4">
          <div>
            <dt className="uppercase tracking-wide">Last checked</dt>
            <dd className="mt-0.5">{status.last_reconciled_at ? formatDateTime(status.last_reconciled_at) : '—'}</dd>
          </div>
          <div>
            <dt className="uppercase tracking-wide">Ready at</dt>
            <dd className="mt-0.5">{status.ready_at ? formatDateTime(status.ready_at) : '—'}</dd>
          </div>
        </dl>
      </main>

      {showEdit && (
        <IntentFormModal
          intent={intent}
          onClose={() => setShowEdit(false)}
          onSaved={(updated) => {
            setShowEdit(false);
            setFetched(updated);
          }}
        />
      )}
      {showDelete && (
        <DeleteIntentModal
          intent={intent}
          onClose={() => setShowDelete(false)}
          onDeleted={() => navigate('/intents')}
        />
      )}
      {logViewer && (
        <LogViewer
          hostId={logViewer.hostId}
          instanceId={logViewer.instanceId}
          alias={intent.alias}
          onClose={() => setLogViewer(null)}
        />
      )}
    </div>
  );
}
