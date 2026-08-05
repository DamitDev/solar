/**
 * StorageDeleteModal — confirm and report a bulk model deletion (storage).
 *
 * One delete code path for both the selection bar and the single-row trash
 * icon: the confirm phase lists what will be deleted (grouped by host),
 * warns that files must be re-downloaded, and the result phase reports
 * per-item outcomes — a bulk operation with mixed results cannot be
 * reported through a native dialog.
 */

import { useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, Info, Loader2, Trash2, WifiOff, X, XCircle } from 'lucide-react';
import solarClient from '@/api/client';
import { HostStorage, StorageDeleteItem, StorageDeleteResult, StorageDeleteStatus } from '@/api/types';
import { formatBytes } from '@/lib/utils';

interface StorageDeleteModalProps {
  items: StorageDeleteItem[];
  /** Raw (unfiltered) storage data, for display names and sizes. */
  hosts: HostStorage[];
  onClose: () => void;
  /** Called when the modal closes after a completed delete — refetch. */
  onDone: () => void;
}

const RESULT_META: Record<StorageDeleteStatus, { icon: typeof CheckCircle2; className: string; label: string }> = {
  deleted: { icon: CheckCircle2, className: 'text-nord-14', label: 'Deleted' },
  in_use: { icon: AlertTriangle, className: 'text-nord-13', label: 'In use — skipped' },
  not_found: { icon: Info, className: 'text-nord-4', label: 'Already removed' },
  unreachable: { icon: WifiOff, className: 'text-nord-3', label: 'Host unreachable' },
  error: { icon: XCircle, className: 'text-nord-11', label: 'Failed' },
};

interface GroupedItem {
  hostId: string;
  hostName: string;
  slug: string;
  name: string;
  size: number;
  inUse: boolean;
}

export function StorageDeleteModal({ items, hosts, onClose, onDone }: StorageDeleteModalProps) {
  const [results, setResults] = useState<StorageDeleteResult[] | null>(null);
  const [deleting, setDeleting] = useState(false);

  const grouped: Array<{ hostName: string; models: GroupedItem[] }> = useMemo(() => {
    const byHost = new Map<string, GroupedItem[]>();
    for (const item of items) {
      const host = hosts.find((h) => h.host_id === item.host_id);
      const model = host?.models.find((m) => m.slug === item.slug);
      const hostName = host?.host_name ?? item.host_id;
      if (!byHost.has(hostName)) byHost.set(hostName, []);
      byHost.get(hostName)!.push({
        hostId: item.host_id,
        hostName,
        slug: item.slug,
        name: model?.model_name ?? model?.slug ?? item.slug,
        size: model?.size_bytes ?? 0,
        inUse: (model?.in_use_by.length ?? 0) > 0,
      });
    }
    return [...byHost.entries()].map(([hostName, models]) => ({ hostName, models }));
  }, [items, hosts]);

  const totalBytes = useMemo(
    () => grouped.reduce((s, g) => s + g.models.reduce((x, m) => x + m.size, 0), 0),
    [grouped],
  );
  const inUseCount = useMemo(() => grouped.reduce((s, g) => s + g.models.filter((m) => m.inUse).length, 0), [grouped]);

  const handleDelete = async () => {
    setDeleting(true);
    try {
      const res = await solarClient.deleteStoredModels(items);
      setResults(res);
    } catch (err: any) {
      console.error('Bulk delete failed:', err);
      setResults([
        {
          host_id: '',
          host_name: '',
          slug: '',
          status: 'error',
          detail: err?.response?.data?.detail || err?.message || 'Delete request failed',
          freed_bytes: 0,
        },
      ]);
    } finally {
      setDeleting(false);
    }
  };

  const summary = useMemo(() => {
    if (!results) return null;
    let deleted = 0;
    let skipped = 0;
    let failed = 0;
    let freed = 0;
    for (const r of results) {
      if (r.status === 'deleted') {
        deleted += 1;
        freed += r.freed_bytes;
      } else if (r.status === 'error') {
        failed += 1;
      } else {
        skipped += 1;
      }
    }
    return { deleted, skipped, failed, freed };
  }, [results]);

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-2xl w-full border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-bold text-nord-6">Delete stored models</h2>
          <button
            onClick={onClose}
            disabled={deleting}
            className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4 disabled:opacity-50"
          >
            <X size={18} />
          </button>
        </div>

        {results === null ? (
          /* ── Confirm phase ── */
          <div className="p-4 space-y-4">
            <p className="text-sm text-nord-6">
              {items.length} model{items.length === 1 ? '' : 's'} on {grouped.length} host
              {grouped.length === 1 ? '' : 's'} · frees ~{formatBytes(totalBytes)}
            </p>

            <div className="max-h-64 overflow-y-auto divide-y divide-nord-3 border border-nord-3 rounded">
              {grouped.map((g) => (
                <div key={g.hostName}>
                  <div className="bg-nord-2 px-3 py-1.5 text-xs text-nord-4">{g.hostName}</div>
                  {g.models.map((m) => (
                    <div key={m.slug} className="flex items-center justify-between px-3 py-2 text-sm">
                      <span className="text-nord-6 break-all pr-4">{m.name}</span>
                      <span className="text-nord-4 tabular-nums flex-shrink-0">{formatBytes(m.size)}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>

            <div className="bg-nord-13 bg-opacity-15 border border-nord-13 rounded p-3 text-sm text-nord-6">
              These files must be downloaded again before the models can be served.
            </div>

            {inUseCount > 0 && (
              <div className="bg-nord-12 bg-opacity-15 border border-nord-12 rounded p-3 text-sm text-nord-6">
                {inUseCount} model{inUseCount === 1 ? ' is' : 's are'} in use and will be skipped.
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2 border-t border-nord-3">
              <button
                onClick={onClose}
                disabled={deleting}
                className="bg-nord-3 text-nord-6 rounded-md px-4 py-2 hover:bg-nord-2 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="bg-nord-11 text-nord-6 rounded-md px-4 py-2 flex items-center gap-2 hover:bg-opacity-90 transition-colors disabled:opacity-60"
              >
                {deleting ? (
                  <>
                    <Loader2 size={16} className="animate-spin" /> Deleting…
                  </>
                ) : (
                  <>
                    <Trash2 size={16} /> Delete {items.length} model{items.length === 1 ? '' : 's'}
                  </>
                )}
              </button>
            </div>
          </div>
        ) : (
          /* ── Result phase ── */
          <div className="p-4 space-y-3">
            <div className="max-h-64 overflow-y-auto divide-y divide-nord-3 border border-nord-3 rounded">
              {results.map((r, i) => {
                const meta = RESULT_META[r.status];
                const Icon = meta.icon;
                return (
                  <div key={`${r.host_id}:${r.slug}:${i}`} className="flex items-center gap-2 px-3 py-2 text-sm">
                    <Icon size={16} className={`flex-shrink-0 ${meta.className}`} />
                    <span className="text-nord-6 break-all flex-1">
                      {r.host_name ? `${r.host_name} / ` : ''}
                      {r.slug || 'request'}
                    </span>
                    <span className={`text-xs flex-shrink-0 ${meta.className}`}>
                      {r.status === 'deleted'
                        ? `${meta.label} · ${formatBytes(r.freed_bytes)} freed`
                        : r.status === 'error'
                          ? `${meta.label} — ${r.detail ?? ''}`
                          : meta.label}
                    </span>
                  </div>
                );
              })}
            </div>

            {summary && (
              <p className="text-sm text-nord-4">
                {summary.deleted} deleted · {summary.skipped} skipped · {summary.failed} failed ·{' '}
                {formatBytes(summary.freed)} freed
              </p>
            )}

            <div className="flex justify-end pt-2 border-t border-nord-3">
              <button
                onClick={() => {
                  onClose();
                  onDone();
                }}
                className="bg-nord-3 text-nord-6 rounded-md px-4 py-2 hover:bg-nord-2 transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
