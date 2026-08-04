import { useState } from 'react';
import { Play, Square, RotateCw, FileText, Trash2, Edit, ChevronUp, ChevronDown } from 'lucide-react';
import {
  Instance,
  InstanceConfig,
  getFullModelLabel,
  getFullModelColor,
  isLlamaCppConfig,
  LlamaCppConfig,
  HuggingFaceCausalConfig,
  HuggingFaceClassificationConfig,
  HuggingFaceEmbeddingConfig,
} from '@/api/types';
import { cn, getStatusColor, formatUptime } from '@/lib/utils';
import { LogViewer } from './LogViewer';
import { EditInstanceModal } from './EditInstanceModal';

interface InstanceTableProps {
  instances: Instance[];
  hostId: string;
  onStart: (hostId: string, instanceId: string) => Promise<void>;
  onStop: (hostId: string, instanceId: string) => Promise<void>;
  onRestart: (hostId: string, instanceId: string) => Promise<void>;
  onUpdate: (hostId: string, instanceId: string, config: InstanceConfig) => Promise<void>;
  onDelete: (hostId: string, instanceId: string) => Promise<void>;
  onMoveUp?: (instanceId: string) => void;
  onMoveDown?: (instanceId: string) => void;
}

/** Get model path/id for display */
function getModelDisplay(config: InstanceConfig): string {
  if (isLlamaCppConfig(config)) {
    return (config as LlamaCppConfig).model;
  }
  return (config as HuggingFaceCausalConfig | HuggingFaceClassificationConfig | HuggingFaceEmbeddingConfig).model_id;
}

/** Truncate string with ellipsis in the middle for long paths */
function truncateModel(model: string, maxLen = 40): string {
  if (model.length <= maxLen) return model;
  // Show last portion for file paths, first portion for model IDs
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

export function InstanceTable({
  instances,
  hostId,
  onStart,
  onStop,
  onRestart,
  onUpdate,
  onDelete,
  onMoveUp,
  onMoveDown,
}: InstanceTableProps) {
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [showLogsFor, setShowLogsFor] = useState<Instance | null>(null);
  const [showEditFor, setShowEditFor] = useState<Instance | null>(null);

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

  return (
    <>
      <div className="overflow-x-auto rounded-lg border border-nord-3">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-nord-2 text-nord-4 text-xs uppercase tracking-wider">
              {(onMoveUp || onMoveDown) && <th className="px-2 py-2 text-center w-12">Order</th>}
              <th className="px-2 py-2 text-left">Alias</th>
              <th className="px-2 py-2 text-left">Model</th>
              <th className="px-2 py-2 text-center">Backend</th>
              <th className="px-2 py-2 text-center">Status</th>
              <th className="px-2 py-2 text-center">Port</th>
              <th className="px-2 py-2 text-left">Uptime</th>
              <th className="px-2 py-2 text-center">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-nord-3">
            {instances.map((instance, idx) => {
              const isLoading = loadingId === instance.id;
              const model = getModelDisplay(instance.config);

              return (
                <>
                  <tr key={instance.id} className="bg-nord-1 hover:bg-nord-2 transition-colors">
                    {/* Order arrows */}
                    {(onMoveUp || onMoveDown) && (
                      <td className="px-2 py-1 text-center">
                        <div className="flex flex-col items-center gap-0.5">
                          <button
                            onClick={() => onMoveUp?.(instance.id)}
                            disabled={idx === 0}
                            className="p-0.5 rounded hover:bg-nord-3 transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-nord-4 hover:text-nord-6"
                            title="Move up"
                          >
                            <ChevronUp size={14} />
                          </button>
                          <button
                            onClick={() => onMoveDown?.(instance.id)}
                            disabled={idx === instances.length - 1}
                            className="p-0.5 rounded hover:bg-nord-3 transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-nord-4 hover:text-nord-6"
                            title="Move down"
                          >
                            <ChevronDown size={14} />
                          </button>
                        </div>
                      </td>
                    )}

                    {/* Alias */}
                    <td className="px-2 py-1 font-medium text-nord-6 whitespace-nowrap">{instance.config.alias}</td>

                    {/* Model */}
                    <td className="px-2 py-1 text-nord-4 font-mono text-xs" title={model}>
                      {truncateModel(model)}
                    </td>

                    {/* Backend badge */}
                    <td className="px-2 py-1 text-center">
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
                    <td className="px-2 py-1 text-center">
                      <span
                        className={cn(
                          'inline-block px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap',
                          getStatusColor(instance.status),
                        )}
                      >
                        {instance.status}
                      </span>
                    </td>

                    {/* Port */}
                    <td className="px-2 py-1 text-center font-mono text-nord-8 text-xs">{instance.port ?? '—'}</td>

                    {/* Uptime */}
                    <td className="px-2 py-1 font-mono text-xs text-nord-8 whitespace-nowrap">
                      {instance.started_at ? formatUptime(instance.started_at) : '—'}
                    </td>

                    {/* Actions */}
                    <td className="px-2 py-1">
                      <div className="flex items-center justify-center gap-1">
                        {/* Start (only when stopped/failed) */}
                        {(instance.status === 'stopped' || instance.status === 'failed') && (
                          <>
                            <button
                              onClick={() => handleAction(instance.id, () => onStart(hostId, instance.id))}
                              disabled={isLoading}
                              className="p-1 rounded hover:bg-nord-14 hover:bg-opacity-20 text-nord-14 transition-colors disabled:opacity-50"
                              title="Start"
                            >
                              <Play size={14} />
                            </button>
                            <button
                              onClick={() => setShowEditFor(instance)}
                              disabled={isLoading}
                              className="p-1 rounded hover:bg-nord-10 hover:bg-opacity-20 text-nord-10 transition-colors disabled:opacity-50"
                              title="Edit"
                            >
                              <Edit size={14} />
                            </button>
                            <button
                              onClick={() => handleAction(instance.id, () => onDelete(hostId, instance.id))}
                              disabled={isLoading}
                              className="p-1 rounded hover:bg-nord-11 hover:bg-opacity-20 text-nord-11 transition-colors disabled:opacity-50"
                              title="Delete"
                            >
                              <Trash2 size={14} />
                            </button>
                          </>
                        )}

                        {/* Stop/Restart (only when running) */}
                        {instance.status === 'running' && (
                          <>
                            <button
                              onClick={() => handleAction(instance.id, () => onStop(hostId, instance.id))}
                              disabled={isLoading}
                              className="p-1 rounded hover:bg-nord-11 hover:bg-opacity-20 text-nord-11 transition-colors disabled:opacity-50"
                              title="Stop"
                            >
                              <Square size={14} />
                            </button>
                            <button
                              onClick={() => handleAction(instance.id, () => onRestart(hostId, instance.id))}
                              disabled={isLoading}
                              className="p-1 rounded hover:bg-nord-10 hover:bg-opacity-20 text-nord-10 transition-colors disabled:opacity-50"
                              title="Restart"
                            >
                              <RotateCw size={14} />
                            </button>
                          </>
                        )}

                        {/* Starting/Stopping state */}
                        {(instance.status === 'starting' || instance.status === 'stopping') && (
                          <span className="text-xs text-nord-4 px-1">{instance.status}...</span>
                        )}

                        {/* Logs (always available) */}
                        <button
                          onClick={() => setShowLogsFor(instance)}
                          className="p-1 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors"
                          title="View logs"
                        >
                          <FileText size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>

                  {/* Error row */}
                  {instance.error_message && (
                    <tr key={`${instance.id}-error`} className="bg-nord-11 bg-opacity-10">
                      <td
                        colSpan={onMoveUp || onMoveDown ? 8 : 7}
                        className="px-3 py-1 text-xs text-nord-11 border-l-2 border-nord-11"
                      >
                        {instance.error_message}
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Log Viewer Modal */}
      {showLogsFor && (
        <LogViewer
          hostId={hostId}
          instanceId={showLogsFor.id}
          alias={showLogsFor.config.alias}
          onClose={() => setShowLogsFor(null)}
        />
      )}

      {/* Edit Instance Modal */}
      {showEditFor && (
        <EditInstanceModal
          instance={showEditFor}
          hostId={hostId}
          onClose={() => setShowEditFor(null)}
          onUpdate={onUpdate}
        />
      )}
    </>
  );
}
