/**
 * DrainHostModal — confirm taking a host out of service (U-005, spec
 * host-draining.md §3, §5.1).
 *
 * Draining moves the host's intent-managed replicas to other hosts and stops
 * new work landing there. It cannot start while manually created instances are
 * running or job steps are active, so the modal lists those blockers and keeps
 * the confirm button disabled until they are gone. The state can change between
 * opening and confirming, so a server-side 409 is rendered inline too.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, RefreshCw, X } from 'lucide-react';
import solarClient from '@/api/client';
import { DrainBlocker } from '@/api/types';

interface DrainHostModalProps {
  hostId: string;
  hostName: string;
  onClose: () => void;
  onDraining: () => void;
}

const BLOCKER_LABELS: Record<string, string> = {
  manual_instance: 'Manual instance',
  active_job: 'Job step',
};

export function DrainHostModal({ hostId, hostName, onClose, onDraining }: DrainHostModalProps) {
  const [blockers, setBlockers] = useState<DrainBlocker[] | null>(null);
  const [checking, setChecking] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const check = useCallback(async () => {
    setChecking(true);
    setError(null);
    try {
      const status = await solarClient.getDrainStatus(hostId);
      setBlockers(status.blockers);
    } catch (err: any) {
      console.error('Failed to read drain status:', err);
      setError(err?.response?.data?.detail || err?.message || 'Failed to read drain status');
    } finally {
      setChecking(false);
    }
  }, [hostId]);

  useEffect(() => {
    check();
  }, [check]);

  const handleDrain = async () => {
    setLoading(true);
    setError(null);
    try {
      await solarClient.drainHost(hostId);
      onDraining();
    } catch (err: any) {
      console.error('Failed to drain host:', err);
      const detail = err?.response?.data?.detail;
      // 409 carries the blockers the preflight rejected on — show them rather
      // than a bare message, since they are what the operator has to act on.
      if (detail?.blockers) {
        setBlockers(detail.blockers as DrainBlocker[]);
        setError(detail.detail || 'Host cannot be drained yet');
      } else {
        setError(typeof detail === 'string' ? detail : err?.message || 'Failed to drain host');
      }
      setLoading(false);
    }
  };

  const blocked = (blockers?.length ?? 0) > 0;

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-lg w-full border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-bold text-nord-6">Drain host</h2>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-nord-4">
            Drain <code className="text-nord-6">{hostName}</code> for maintenance?
          </p>
          <ul className="space-y-1.5 text-sm text-nord-4 list-disc pl-5">
            <li>Managed instances are moved to other hosts, one at a time.</li>
            <li>The host accepts no new instances until you resume it.</li>
            <li>
              If an instance has nowhere to go it keeps serving here and the drain waits — Solar never drops serving
              capacity to finish a drain.
            </li>
          </ul>

          {checking && (
            <p className="flex items-center gap-2 text-sm text-nord-4">
              <RefreshCw size={14} className="animate-spin" />
              Checking what is running on this host...
            </p>
          )}

          {!checking && blocked && (
            <div className="bg-nord-12 bg-opacity-10 border border-nord-12 rounded-md p-3 space-y-2">
              <p className="flex items-center gap-2 text-sm font-medium text-nord-13">
                <AlertTriangle size={14} className="shrink-0" />
                Stop these first — draining leaves them alone
              </p>
              <ul className="space-y-1.5">
                {blockers!.map((b) => (
                  <li key={`${b.kind}:${b.id}`} className="text-sm text-nord-4">
                    <span className="text-xs uppercase tracking-wide text-nord-13">
                      {BLOCKER_LABELS[b.kind] ?? b.kind}
                    </span>{' '}
                    <span className="font-medium text-nord-6">{b.name || b.id}</span>
                    <span className="block text-xs">{b.detail}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {!checking && !blocked && blockers != null && (
            <p className="text-sm text-nord-14">Nothing blocks the drain.</p>
          )}

          {error && <p className="text-sm text-nord-11">{error}</p>}

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => check()}
              disabled={loading || checking}
              className="px-4 py-2 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors disabled:opacity-50"
              title="Re-check blockers"
            >
              <RefreshCw size={16} className={checking ? 'animate-spin' : ''} />
            </button>
            <button
              type="button"
              onClick={handleDrain}
              disabled={loading || checking || blocked}
              className="flex-1 px-4 py-2 bg-nord-12 text-nord-0 rounded-md hover:bg-nord-13 transition-colors disabled:opacity-50 font-medium"
              title={blocked ? 'Stop the listed workloads first' : undefined}
            >
              {loading ? 'Draining...' : 'Drain'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
