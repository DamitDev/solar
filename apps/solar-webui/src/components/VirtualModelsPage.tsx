/**
 * VirtualModelsPage — stable model aliases with context/capability contracts (S-060).
 *
 * REST + 10 s polling (no event stream: virtual models are low-churn config).
 * Each row shows the ordered target chain with live per-target health from the
 * control plane's registry view.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, Boxes, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';
import solarClient from '@/api/client';
import { VirtualModel } from '@/api/types';
import { VirtualModelFormModal } from './VirtualModelFormModal';
import { DeleteVirtualModelModal } from './DeleteVirtualModelModal';

const POLL_INTERVAL_MS = 10_000;

function targetChipClass(status: string | undefined): string {
  if (!status) return 'bg-nord-2 text-nord-6';
  if (status === 'satisfied') return 'bg-nord-14 bg-opacity-30 text-nord-14 border border-nord-14';
  if (status === 'missing') return 'bg-nord-2 text-nord-4 border border-nord-3';
  // violating: <reason>
  return 'bg-nord-11 bg-opacity-20 text-nord-11 border border-nord-11';
}

function contractSummary(vm: VirtualModel): string {
  const parts: string[] = [];
  if (vm.contract.context_size) parts.push(`${(vm.contract.context_size / 1000).toFixed(0)}K ctx`);
  if (vm.contract.capabilities?.length) parts.push(vm.contract.capabilities.join(', '));
  return parts.join(' · ') || 'no contract';
}

export function VirtualModelsPage() {
  const [models, setModels] = useState<VirtualModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [formTarget, setFormTarget] = useState<VirtualModel | 'new' | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<VirtualModel | null>(null);

  const fetchList = useCallback(async () => {
    try {
      const list = await solarClient.listVirtualModels();
      setModels(list);
      setError(null);
    } catch (err: any) {
      setError(err?.message || 'Failed to load virtual models');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchList();
    const t = setInterval(fetchList, POLL_INTERVAL_MS);
    return () => clearInterval(t);
  }, [fetchList]);

  const sorted = [...models].sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div className="bg-nord-0 min-h-screen">
      <header className="bg-nord-1 shadow-lg">
        <div className="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-nord-6">Virtual Models</h1>
              <p className="text-sm text-nord-4 mt-1">
                Stable model names with guaranteed context and capability contracts. Clients never see which concrete
                model answers.
              </p>
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
                onClick={() => setFormTarget('new')}
                className="flex items-center gap-2 px-4 py-2 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors font-medium"
              >
                <Plus size={18} /> New Virtual Model
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {loading && models.length === 0 ? (
          <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
            <div className="text-center">
              <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-nord-9 mx-auto mb-4"></div>
              <p className="text-nord-4">Loading...</p>
            </div>
          </div>
        ) : error && models.length === 0 ? (
          <div className="mb-6 p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div>
              <h3 className="font-semibold text-nord-6">Virtual models unavailable</h3>
              <p className="text-sm text-nord-4">{error}</p>
              <button
                onClick={fetchList}
                className="mt-2 text-sm text-nord-8 hover:text-nord-6 flex items-center gap-1"
              >
                <RefreshCw size={14} /> Retry
              </button>
            </div>
          </div>
        ) : models.length === 0 ? (
          <div className="text-center py-16">
            <Boxes size={64} className="mx-auto text-nord-3 mb-4" />
            <h2 className="text-2xl font-semibold text-nord-6 mb-2">No virtual models yet</h2>
            <p className="text-nord-4 mb-6">
              Create a stable name your clients can use regardless of which concrete model serves it.
            </p>
            <button
              onClick={() => setFormTarget('new')}
              className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
            >
              <Plus size={20} /> New Virtual Model
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-nord-3 text-left text-xs text-nord-4">
                  <th className="py-2 pr-4 font-medium">Name</th>
                  <th className="py-2 pr-4 font-medium">Targets (failover order)</th>
                  <th className="py-2 pr-4 font-medium">Contract</th>
                  <th className="py-2 pr-4 font-medium">Updated</th>
                  <th className="py-2 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((vm) => (
                  <tr key={vm.id} className="border-b border-nord-3 hover:bg-nord-1 transition-colors">
                    <td className="py-2.5 pr-4 font-mono text-nord-6">{vm.name}</td>
                    <td className="py-2.5 pr-4">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {vm.targets.map((t, i) => (
                          <span key={t} className="flex items-center gap-1.5">
                            {i > 0 && <span className="text-nord-4 text-xs">→</span>}
                            <span
                              className={`px-2 py-0.5 rounded text-xs font-mono ${targetChipClass(vm.target_status?.[t])}`}
                              title={t + (vm.target_status?.[t] ? `: ${vm.target_status[t]}` : '')}
                            >
                              {t}
                            </span>
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="py-2.5 pr-4 text-nord-4 text-xs">{contractSummary(vm)}</td>
                    <td className="py-2.5 pr-4 text-nord-4 text-xs">
                      {vm.updated_at ? new Date(vm.updated_at).toLocaleString() : '—'}
                    </td>
                    <td className="py-2.5 text-right whitespace-nowrap">
                      <button
                        onClick={() => setFormTarget(vm)}
                        className="p-1.5 text-nord-4 hover:text-nord-8 hover:bg-nord-2 rounded transition-colors"
                        title="Edit"
                      >
                        <Pencil size={16} />
                      </button>
                      <button
                        onClick={() => setDeleteTarget(vm)}
                        className="p-1.5 text-nord-4 hover:text-nord-11 hover:bg-nord-2 rounded transition-colors"
                        title="Delete"
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

      {formTarget && (
        <VirtualModelFormModal
          existing={formTarget === 'new' ? null : formTarget}
          onClose={() => setFormTarget(null)}
          onSaved={() => {
            setFormTarget(null);
            fetchList();
          }}
        />
      )}
      {deleteTarget && (
        <DeleteVirtualModelModal
          vm={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => {
            setDeleteTarget(null);
            fetchList();
          }}
        />
      )}
    </div>
  );
}
