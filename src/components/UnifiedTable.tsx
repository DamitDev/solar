import { useState, useMemo } from 'react';
import { Play, Square, RotateCw, FileText, Trash2, Edit } from 'lucide-react';
import {
  Instance,
  InstanceConfig,
  getFullModelLabel,
  getFullModelColor,
  getModelCategory,
  ModelCategory,
  isLlamaCppConfig,
  LlamaCppConfig,
  HuggingFaceCausalConfig,
  HuggingFaceClassificationConfig,
  HuggingFaceEmbeddingConfig,
} from '@/api/types';
import { cn, getStatusColor, formatUptime } from '@/lib/utils';
import { LogViewer } from './LogViewer';
import { EditInstanceModal } from './EditInstanceModal';
import { IntentManagedBadge } from './IntentManagedBadge';

interface HostInfo {
  id: string;
  name: string;
}

interface UnifiedRow {
  instance: Instance;
  host: HostInfo;
}

interface UnifiedTableProps {
  hosts: Array<{
    id: string;
    name: string;
    instances: Instance[];
  }>;
  isHostReachable: (hostId: string) => boolean;
  onStartInstance: (hostId: string, instanceId: string) => Promise<void>;
  onStopInstance: (hostId: string, instanceId: string) => Promise<void>;
  onRestartInstance: (hostId: string, instanceId: string) => Promise<void>;
  onUpdateInstance: (hostId: string, instanceId: string, config: InstanceConfig) => Promise<void>;
  onDeleteInstance: (hostId: string, instanceId: string) => Promise<void>;
}

/** Category sort order */
const CATEGORY_ORDER: Record<ModelCategory, number> = {
  generation: 0,
  embedding: 1,
  classification: 2,
  reranker: 3,
};

/** Get model display name */
function getModelDisplay(config: InstanceConfig): string {
  if (isLlamaCppConfig(config)) {
    return (config as LlamaCppConfig).model;
  }
  return (config as HuggingFaceCausalConfig | HuggingFaceClassificationConfig | HuggingFaceEmbeddingConfig).model_id;
}

/** Truncate long model paths */
function truncateModel(model: string, maxLen = 40): string {
  if (model.length <= maxLen) return model;
  if (model.includes('/')) {
    const parts = model.split('/');
    const filename = parts[parts.length - 1];
    if (filename.length <= maxLen - 4) {
      return '.../' + filename;
    }
    return '...' + filename.slice(-(maxLen - 3));
  }
  return model.slice(0, maxLen - 3) + '...';
}

export function UnifiedTable({
  hosts,
  isHostReachable,
  onStartInstance,
  onStopInstance,
  onRestartInstance,
  onUpdateInstance,
  onDeleteInstance,
}: UnifiedTableProps) {
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [showLogsFor, setShowLogsFor] = useState<{ instance: Instance; hostId: string } | null>(null);
  const [showEditFor, setShowEditFor] = useState<{ instance: Instance; hostId: string } | null>(null);

  // Flatten all instances from all hosts, sorted by category then alias
  const rows = useMemo<UnifiedRow[]>(() => {
    const all: UnifiedRow[] = [];
    for (const host of hosts) {
      for (const instance of host.instances) {
        all.push({ instance, host: { id: host.id, name: host.name } });
      }
    }

    all.sort((a, b) => {
      const catA = CATEGORY_ORDER[getModelCategory(a.instance.config)] ?? 99;
      const catB = CATEGORY_ORDER[getModelCategory(b.instance.config)] ?? 99;
      if (catA !== catB) return catA - catB;
      // Then alphabetically by alias
      return a.instance.config.alias.localeCompare(b.instance.config.alias);
    });

    return all;
  }, [hosts]);

  const handleAction = async (instanceId: string, action: () => Promise<void>) => {
    setLoadingId(instanceId);
    try {
      await action();
    } catch (error) {
      console.error('Action failed:', error);
    } finally {
      setLoadingId(null);
    }
  };

  if (rows.length === 0) {
    return (
      <div className="text-center py-8 text-nord-4">
        <p>No instances across any host</p>
      </div>
    );
  }

  return (
    <>
      <div className="overflow-x-auto rounded-lg border border-nord-3">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-nord-2 text-nord-4 text-xs uppercase tracking-wider">
              <th className="px-3 py-2 text-left">Alias</th>
              <th className="px-3 py-2 text-left">Model</th>
              <th className="px-3 py-2 text-center">Type</th>
              <th className="px-3 py-2 text-center">Status</th>
              <th className="px-3 py-2 text-left">Host</th>
              <th className="px-3 py-2 text-center">Port</th>
              <th className="px-3 py-2 text-left">Uptime</th>
              <th className="px-3 py-2 text-center">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-nord-3">
            {rows.map(({ instance, host }) => {
              const isLoading = loadingId === instance.id;
              const reachable = isHostReachable(host.id);
              const disabled = isLoading || !reachable;
              const offlineTitle = reachable ? undefined : 'Host is offline';
              const model = getModelDisplay(instance.config);

              return (
                <tr
                  key={`${host.id}-${instance.id}`}
                  className={cn('bg-nord-1 hover:bg-nord-2 transition-colors', !reachable && 'opacity-70')}
                >
                  {/* Alias */}
                  <td className="px-3 py-1.5 font-medium text-nord-6 whitespace-nowrap">
                    <span className="flex items-center gap-2">
                      {instance.config.alias}
                      <IntentManagedBadge instance={instance} />
                    </span>
                  </td>

                  {/* Model */}
                  <td className="px-3 py-1.5 text-nord-4 font-mono text-xs" title={model}>
                    {truncateModel(model)}
                  </td>

                  {/* Type badge */}
                  <td className="px-3 py-1.5 text-center">
                    <span
                      className={cn(
                        'inline-block px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap',
                        getFullModelColor(instance.config),
                      )}
                    >
                      {getFullModelLabel(instance.config)}
                    </span>
                  </td>

                  {/* Status badge */}
                  <td className="px-3 py-1.5 text-center">
                    <span
                      className={cn(
                        'inline-block px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap',
                        getStatusColor(instance.status),
                      )}
                    >
                      {instance.status}
                    </span>
                  </td>

                  {/* Host */}
                  <td className="px-3 py-1.5 text-nord-4 whitespace-nowrap text-xs">
                    <span className="flex items-center gap-1">
                      {host.name}
                      {!reachable && (
                        <span
                          className="inline-block w-2 h-2 rounded-full bg-nord-11 flex-shrink-0"
                          title="Disconnected"
                        />
                      )}
                    </span>
                  </td>

                  {/* Port */}
                  <td className="px-3 py-1.5 text-center font-mono text-nord-8 text-xs">{instance.port ?? '—'}</td>

                  {/* Uptime */}
                  <td className="px-3 py-1.5 font-mono text-xs text-nord-8 whitespace-nowrap">
                    {instance.started_at ? formatUptime(instance.started_at) : '—'}
                  </td>

                  {/* Actions */}
                  <td className="px-3 py-1.5">
                    <div className="flex items-center justify-center gap-1">
                      {(instance.status === 'stopped' || instance.status === 'failed') && (
                        <>
                          <button
                            onClick={() => handleAction(instance.id, () => onStartInstance(host.id, instance.id))}
                            disabled={disabled}
                            className="p-1 rounded hover:bg-nord-14 hover:bg-opacity-20 text-nord-14 transition-colors disabled:opacity-50"
                            title={offlineTitle || 'Start'}
                          >
                            <Play size={14} />
                          </button>
                          <button
                            onClick={() => setShowEditFor({ instance, hostId: host.id })}
                            disabled={disabled}
                            className="p-1 rounded hover:bg-nord-10 hover:bg-opacity-20 text-nord-10 transition-colors disabled:opacity-50"
                            title={offlineTitle || 'Edit'}
                          >
                            <Edit size={14} />
                          </button>
                          <button
                            onClick={() => handleAction(instance.id, () => onDeleteInstance(host.id, instance.id))}
                            disabled={disabled}
                            className="p-1 rounded hover:bg-nord-11 hover:bg-opacity-20 text-nord-11 transition-colors disabled:opacity-50"
                            title={offlineTitle || 'Delete'}
                          >
                            <Trash2 size={14} />
                          </button>
                        </>
                      )}

                      {instance.status === 'running' && (
                        <>
                          <button
                            onClick={() => handleAction(instance.id, () => onStopInstance(host.id, instance.id))}
                            disabled={disabled}
                            className="p-1 rounded hover:bg-nord-11 hover:bg-opacity-20 text-nord-11 transition-colors disabled:opacity-50"
                            title={offlineTitle || 'Stop'}
                          >
                            <Square size={14} />
                          </button>
                          <button
                            onClick={() => handleAction(instance.id, () => onRestartInstance(host.id, instance.id))}
                            disabled={disabled}
                            className="p-1 rounded hover:bg-nord-10 hover:bg-opacity-20 text-nord-10 transition-colors disabled:opacity-50"
                            title={offlineTitle || 'Restart'}
                          >
                            <RotateCw size={14} />
                          </button>
                        </>
                      )}

                      {(instance.status === 'starting' || instance.status === 'stopping') && (
                        <span className="text-xs text-nord-4 px-1">{instance.status}...</span>
                      )}

                      <button
                        onClick={() => setShowLogsFor({ instance, hostId: host.id })}
                        className="p-1 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors"
                        title="View logs"
                      >
                        <FileText size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Log Viewer Modal */}
      {showLogsFor && (
        <LogViewer
          hostId={showLogsFor.hostId}
          instanceId={showLogsFor.instance.id}
          alias={showLogsFor.instance.config.alias}
          onClose={() => setShowLogsFor(null)}
        />
      )}

      {/* Edit Instance Modal */}
      {showEditFor && (
        <EditInstanceModal
          instance={showEditFor.instance}
          hostId={showEditFor.hostId}
          onClose={() => setShowEditFor(null)}
          onUpdate={onUpdateInstance}
        />
      )}
    </>
  );
}
