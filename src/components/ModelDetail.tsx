import { Fragment, useEffect, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { Rocket, X } from 'lucide-react';
import { CatalogDeployedHost, CatalogModelItem, CatalogSolarStatus } from '@/api/types';
import { cn, formatBytes, formatDateTime, getCatalogStatusColor } from '@/lib/utils';

const STATUS_TOOLTIPS: Record<CatalogSolarStatus, string> = {
  available: 'At least one instance running',
  deployed: 'Files present on hosts, no instance running',
  unavailable: 'Not deployed',
  unknown: 'Availability source unreachable — absence cannot be confirmed',
};

export function StatusBadge({ status }: { status: CatalogSolarStatus }) {
  return (
    <span
      title={STATUS_TOOLTIPS[status] ?? status}
      className={cn('px-2 py-0.5 rounded text-xs font-medium', getCatalogStatusColor(status))}
    >
      {status}
    </span>
  );
}

function StatBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-nord-4 uppercase tracking-wide">{label}</dt>
      <dd className="mt-1 text-sm font-medium text-nord-6">{children}</dd>
    </div>
  );
}

export function ModelDetail({ model }: { model: CatalogModelItem }) {
  // U-003 handoff: catalog → intent form pre-fill
  const navigate = useNavigate();

  const handleDeploy = () => {
    navigate(
      `/intents?alias=${encodeURIComponent(model.name)}&model_source=${encodeURIComponent(
        `repo://${model.name}:${model.latest_version ?? ''}`,
      )}`,
    );
  };

  // D-018 can list the same host once per model version path present on it
  // (verified in live dev data: damcpaiops02 appears for both v1 and v2).
  // Group by host name but keep every path — do not dedupe silently.
  const deployedByHost = new Map<string, CatalogDeployedHost[]>();
  for (const entry of model.solar.deployed_hosts) {
    const entries = deployedByHost.get(entry.host_name) ?? [];
    entries.push(entry);
    deployedByHost.set(entry.host_name, entries);
  }

  return (
    <div className="space-y-6">
      {/* Stat strip — one glance at the essentials, no label/value pair walls */}
      <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-5">
        <StatBlock label="Category">{model.category}</StatBlock>
        <StatBlock label="Versions">{model.versions_count}</StatBlock>
        <StatBlock label="Latest version">{model.latest_version ?? '—'}</StatBlock>
        <StatBlock label="Created">{formatDateTime(model.created_at)}</StatBlock>
        <StatBlock label="Status">
          <StatusBadge status={model.solar.status} />
        </StatBlock>
      </dl>

      {model.description && <p className="text-sm text-nord-4">{model.description}</p>}

      {/* Deployment */}
      <section>
        <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">Deployment</h4>
        <div className="mt-3 space-y-4">
          <div>
            <h5 className="text-xs text-nord-4">Running instances</h5>
            {model.solar.instances.length > 0 ? (
              <ul className="mt-1.5 flex flex-wrap gap-2">
                {model.solar.instances.map((instance) => (
                  <li
                    key={instance.instance_id}
                    className="inline-flex items-center gap-1.5 bg-nord-2 rounded-md px-2 py-1 text-xs"
                  >
                    <span className="font-medium text-nord-6">{instance.host_name}</span>
                    <span className="font-mono text-nord-4">{instance.instance_id}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-sm text-nord-4">No running instances</p>
            )}
          </div>

          <div>
            <h5 className="text-xs text-nord-4">Deployed hosts</h5>
            {deployedByHost.size > 0 ? (
              <div className="mt-1.5 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-nord-3 text-left text-xs text-nord-4">
                      <th className="py-2 pr-4 font-medium">Host</th>
                      <th className="py-2 pr-4 font-medium whitespace-nowrap">Size</th>
                      <th className="py-2 font-medium">Path</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...deployedByHost.entries()].map(([hostName, entries]) => (
                      <Fragment key={hostName}>
                        <tr className="align-top">
                          <td rowSpan={entries.length} className="py-2 pr-4 font-medium text-nord-6">
                            {hostName}
                          </td>
                          <td className="py-2 pr-4 whitespace-nowrap text-nord-4">
                            {formatBytes(entries[0].size_bytes)}
                          </td>
                          <td className="py-2 font-mono text-xs text-nord-4 break-all">{entries[0].path}</td>
                        </tr>
                        {entries.slice(1).map((entry) => (
                          <tr key={entry.path} className="align-top">
                            <td className="py-2 pr-4 whitespace-nowrap text-nord-4">{formatBytes(entry.size_bytes)}</td>
                            <td className="py-2 font-mono text-xs text-nord-4 break-all">{entry.path}</td>
                          </tr>
                        ))}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : model.solar.status === 'unknown' ? (
              <p className="mt-1 text-sm text-nord-13">Availability could not be verified (hosts unreachable)</p>
            ) : (
              <p className="mt-1 text-sm text-nord-4">No deployed hosts</p>
            )}
          </div>
        </div>
      </section>

      {/* Deploy affordance — opens the intent submission form pre-filled (U-003) */}
      <div className="flex justify-end border-t border-nord-3 pt-3">
        <button
          onClick={handleDeploy}
          className="px-3 py-1.5 rounded bg-nord-10 text-nord-6 text-sm hover:bg-nord-9 transition-colors"
        >
          <Rocket size={14} className="inline mr-1" /> Deploy
        </button>
      </div>
    </div>
  );
}

/**
 * Slide-over detail panel used by the cards view. Renders nothing when
 * `model` is null so the parent can keep it mounted and drive open/close
 * purely through state.
 */
export function ModelDrawer({ model, onClose }: { model: CatalogModelItem | null; onClose: () => void }) {
  useEffect(() => {
    if (!model) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [model, onClose]);

  if (!model) return null;

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/70 animate-[fade-in_150ms_ease-out]" onClick={onClose} />
      <aside className="absolute right-0 top-0 h-full w-full max-w-md bg-nord-1 border-l border-nord-3 shadow-2xl flex flex-col animate-[slide-in-right_200ms_ease-out]">
        <header className="flex items-start justify-between gap-4 p-4 sm:p-5 border-b border-nord-3">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-nord-6 break-all">{model.name}</h2>
            <p className="text-xs text-nord-4 mt-0.5">{model.category}</p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-6 transition-colors"
            title="Close"
          >
            <X size={18} />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto p-4 sm:p-6">
          <ModelDetail model={model} />
        </div>
      </aside>
    </div>
  );
}
