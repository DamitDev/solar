import { useEffect, useRef, useState } from 'react';
import {
  Play,
  Square,
  RotateCw,
  FileText,
  Trash2,
  Edit,
  MessageSquare,
  Tags,
  Binary,
  Search,
  GripVertical,
  Loader2,
} from 'lucide-react';
import {
  Instance,
  InstanceConfig,
  getBackendType,
  getFullModelLabel,
  getFullModelColor,
  isLlamaCppConfig,
  isHuggingFaceCausalConfig,
  isHuggingFaceClassificationConfig,
  isHuggingFaceEmbeddingConfig,
  LlamaCppConfig,
  HuggingFaceCausalConfig,
  HuggingFaceClassificationConfig,
  HuggingFaceEmbeddingConfig,
} from '@/api/types';
import { cn, getStatusColor, formatUptime } from '@/lib/utils';
import { LogViewer } from './LogViewer';
import { EditInstanceModal } from './EditInstanceModal';
import { useInstanceState } from '@/hooks/useInstanceState';
import { IntentManagedBadge } from './IntentManagedBadge';

interface InstanceCardProps {
  instance: Instance;
  hostId: string;
  hostReachable?: boolean;
  onStart: (hostId: string, instanceId: string) => Promise<void>;
  onStop: (hostId: string, instanceId: string) => Promise<void>;
  onRestart: (hostId: string, instanceId: string) => Promise<void>;
  onUpdate: (hostId: string, instanceId: string, config: InstanceConfig) => Promise<void>;
  onDelete: (hostId: string, instanceId: string) => Promise<void>;
  dragListeners?: Record<string, Function>;
}

const BackendIcon = ({ config }: { config: InstanceConfig }) => {
  const backendType = getBackendType(config);

  if (isLlamaCppConfig(config)) {
    const llamaConfig = config as LlamaCppConfig;
    switch (llamaConfig.model_type) {
      case 'embedding':
        return <Binary size={14} />;
      case 'reranker':
        return <Search size={14} />;
      default:
        return <MessageSquare size={14} />;
    }
  }

  switch (backendType) {
    case 'huggingface_causal':
      return <MessageSquare size={14} />;
    case 'huggingface_classification':
      return <Tags size={14} />;
    case 'huggingface_embedding':
      return <Binary size={14} />;
    default:
      return <MessageSquare size={14} />;
  }
};

export function InstanceCard({
  instance,
  hostId,
  hostReachable = true,
  onStart,
  onStop,
  onRestart,
  onUpdate,
  onDelete,
  dragListeners,
}: InstanceCardProps) {
  const [loading, setLoading] = useState(false);
  // Start/restart block until the backend reports readiness, which can
  // take minutes — the button must show the request is in flight instead
  // of letting a re-click launch a second process.
  const [pendingAction, setPendingAction] = useState<'start' | 'restart' | null>(null);
  const actionsDisabled = loading || !hostReachable;
  const [showLogs, setShowLogs] = useState(false);
  const [showEdit, setShowEdit] = useState(false);

  const { state: runtimeState } = useInstanceState(hostId, instance.id);

  // Smooth prefill appearance/disappearance (only for llama.cpp)
  const [prefillVisible, setPrefillVisible] = useState(false);
  const hideTimerRef = useRef<number | null>(null);
  const backendType = getBackendType(instance.config);
  const prefillActive =
    backendType === 'llamacpp' &&
    typeof runtimeState?.prefill_progress === 'number' &&
    runtimeState.prefill_progress < 1;

  useEffect(() => {
    if (prefillActive) {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
      setPrefillVisible(true);
    } else {
      // Graceful fade-out to avoid flicker when prefill ends quickly
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
      }
      hideTimerRef.current = window.setTimeout(() => {
        setPrefillVisible(false);
        hideTimerRef.current = null;
      }, 400);
    }

    return () => {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
    };
  }, [prefillActive]);

  const handleAction = async (kind: 'start' | 'restart' | 'stop' | 'delete', action: () => Promise<void>) => {
    setLoading(true);
    setPendingAction(kind === 'start' || kind === 'restart' ? kind : null);
    try {
      await action();
    } catch (error) {
      console.error('Action failed:', error);
    } finally {
      setLoading(false);
      setPendingAction(null);
    }
  };

  const getModelDisplay = () => {
    if (isLlamaCppConfig(instance.config)) {
      return (instance.config as LlamaCppConfig).model || instance.config.alias || instance.id;
    }
    return (
      (instance.config as HuggingFaceCausalConfig | HuggingFaceClassificationConfig | HuggingFaceEmbeddingConfig)
        .model_id ||
      instance.config.alias ||
      instance.id
    );
  };

  // Get backend-specific details (guards for incomplete config from Socket.IO placeholders)
  const renderBackendDetails = () => {
    if (isLlamaCppConfig(instance.config)) {
      const config = instance.config as LlamaCppConfig;
      if (config.ctx_size == null) return null;
      return (
        <>
          <div className="flex justify-between">
            <span>Context:</span>
            <span className="font-mono text-nord-8">{config.ctx_size.toLocaleString()}</span>
          </div>
          <div className="flex justify-between">
            <span>GPU Layers:</span>
            <span className="font-mono text-nord-8">{config.n_gpu_layers}</span>
          </div>
        </>
      );
    }

    if (isHuggingFaceCausalConfig(instance.config)) {
      const config = instance.config as HuggingFaceCausalConfig;
      if (config.max_length == null) return null;
      return (
        <>
          <div className="flex justify-between">
            <span>Max Length:</span>
            <span className="font-mono text-nord-8">{config.max_length.toLocaleString()}</span>
          </div>
          <div className="flex justify-between">
            <span>Device:</span>
            <span className="font-mono text-nord-8">{config.device}</span>
          </div>
          <div className="flex justify-between">
            <span>Dtype:</span>
            <span className="font-mono text-nord-8">{config.dtype}</span>
          </div>
        </>
      );
    }

    if (isHuggingFaceClassificationConfig(instance.config)) {
      const config = instance.config as HuggingFaceClassificationConfig;
      if (config.max_length == null) return null;
      return (
        <>
          <div className="flex justify-between">
            <span>Max Length:</span>
            <span className="font-mono text-nord-8">{config.max_length.toLocaleString()}</span>
          </div>
          <div className="flex justify-between">
            <span>Device:</span>
            <span className="font-mono text-nord-8">{config.device}</span>
          </div>
          {config.labels && config.labels.length > 0 && (
            <div className="flex justify-between">
              <span>Labels:</span>
              <span
                className="font-mono text-nord-8 text-right truncate max-w-[140px]"
                title={config.labels.join(', ')}
              >
                {config.labels.length} defined
              </span>
            </div>
          )}
        </>
      );
    }

    if (isHuggingFaceEmbeddingConfig(instance.config)) {
      const config = instance.config as HuggingFaceEmbeddingConfig;
      if (config.max_length == null) return null;
      return (
        <>
          <div className="flex justify-between">
            <span>Max Length:</span>
            <span className="font-mono text-nord-8">{config.max_length.toLocaleString()}</span>
          </div>
          <div className="flex justify-between">
            <span>Device:</span>
            <span className="font-mono text-nord-8">{config.device}</span>
          </div>
          <div className="flex justify-between">
            <span>Dtype:</span>
            <span className="font-mono text-nord-8">{config.dtype}</span>
          </div>
          <div className="flex justify-between">
            <span>Normalize:</span>
            <span className="font-mono text-nord-8">{config.normalize_embeddings ? 'Yes' : 'No'}</span>
          </div>
        </>
      );
    }

    return null;
  };

  return (
    <>
      <div className="bg-nord-2 rounded-lg shadow-lg p-4 hover:shadow-xl transition-shadow border border-nord-3">
        {/* Header */}
        <div className="flex items-start justify-between gap-2 mb-3">
          <div className="flex items-start gap-2 flex-1 min-w-0">
            {/* Drag handle */}
            {dragListeners && (
              <button
                {...dragListeners}
                className="p-0.5 mt-1 rounded cursor-grab active:cursor-grabbing hover:bg-nord-3 transition-colors text-nord-4 hover:text-nord-6 touch-none flex-shrink-0"
                title="Drag to reorder"
              >
                <GripVertical size={16} />
              </button>
            )}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <h3 className="font-semibold text-lg truncate text-nord-6">{instance.config.alias}</h3>
                <IntentManagedBadge instance={instance} />
              </div>
              <p className="text-sm text-nord-4 truncate" title={getModelDisplay()}>
                {getModelDisplay()}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-1 flex-shrink-0 flex-wrap justify-end">
            {/* Backend Type Badge */}
            <span
              className={cn(
                'px-2 py-1 rounded-full text-xs font-medium whitespace-nowrap flex items-center gap-1',
                getFullModelColor(instance.config),
              )}
            >
              <BackendIcon config={instance.config} />
              {getFullModelLabel(instance.config)}
            </span>
            {/* Status Badge */}
            <span
              className={cn(
                'px-2 py-1 rounded-full text-xs font-medium whitespace-nowrap',
                getStatusColor(instance.status),
              )}
            >
              {instance.status}
            </span>
            {runtimeState?.busy && (
              <span
                className="px-2 py-1 rounded-full text-xs font-medium whitespace-nowrap bg-nord-13 text-nord-0 animate-pulse"
                title={`Active slots: ${runtimeState.active_slots}`}
              >
                busy
              </span>
            )}
            {(instance.status === 'stopped' || instance.status === 'failed') && (
              <>
                <button
                  onClick={() => setShowEdit(true)}
                  disabled={actionsDisabled}
                  className="p-1 hover:bg-nord-10 hover:bg-opacity-20 text-nord-10 rounded transition-colors disabled:opacity-50"
                  title={hostReachable ? 'Edit instance' : 'Host is offline'}
                >
                  <Edit size={16} />
                </button>
                <button
                  onClick={() => handleAction('delete', () => onDelete(hostId, instance.id))}
                  disabled={actionsDisabled}
                  className="p-1 hover:bg-nord-11 hover:bg-opacity-20 text-nord-11 rounded transition-colors disabled:opacity-50"
                  title={hostReachable ? 'Delete instance' : 'Host is offline'}
                >
                  <Trash2 size={16} />
                </button>
              </>
            )}
          </div>
        </div>

        {/* Details */}
        <div className="space-y-1 text-sm text-nord-4 mb-3">
          {/* Prefill progress (llama.cpp only) */}
          {backendType === 'llamacpp' && (
            <div
              className="mb-2 overflow-hidden"
              style={{
                opacity: prefillVisible ? 1 : 0,
                transform: prefillVisible ? 'scaleY(1)' : 'scaleY(0.95)',
                transformOrigin: 'top',
                maxHeight: prefillVisible ? 28 : 0,
                transition: 'opacity 200ms ease, transform 200ms ease, max-height 250ms ease',
              }}
            >
              <div className="flex justify-between items-center mb-1">
                <span>Prefill</span>
                <span className="font-mono text-nord-8">
                  {typeof runtimeState?.prefill_progress === 'number'
                    ? Math.round(runtimeState.prefill_progress * 100)
                    : 0}
                  %
                </span>
              </div>
              <div className="w-full h-1.5 bg-nord-3 rounded">
                <div
                  className="h-1.5 bg-nord-10 rounded"
                  style={{
                    width: `${Math.max(0, Math.min(100, (runtimeState?.prefill_progress || 0) * 100))}%`,
                    transition: 'width 200ms ease',
                  }}
                />
              </div>
            </div>
          )}
          {instance.port && (
            <div className="flex justify-between">
              <span>Port:</span>
              <span className="font-mono text-nord-8">{instance.port}</span>
            </div>
          )}
          {instance.pid && (
            <div className="flex justify-between">
              <span>PID:</span>
              <span className="font-mono text-nord-8">{instance.pid}</span>
            </div>
          )}
          {instance.started_at && (
            <div className="flex justify-between">
              <span>Uptime:</span>
              <span className="font-mono text-nord-8">{formatUptime(instance.started_at)}</span>
            </div>
          )}
          {/* Backend-specific details */}
          {renderBackendDetails()}

          {/* Supported endpoints (if available) */}
          {instance.supported_endpoints && instance.supported_endpoints.length > 0 && (
            <div className="flex justify-between">
              <span>Endpoints:</span>
              <span className="font-mono text-nord-8 text-xs">{instance.supported_endpoints.length}</span>
            </div>
          )}
        </div>

        {/* Error message */}
        {instance.error_message && (
          <div className="mb-3 p-2 bg-nord-11 bg-opacity-20 text-nord-11 text-xs rounded border border-nord-11">
            {instance.error_message}
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-2">
          {pendingAction === 'start' || pendingAction === 'restart' ? (
            <button
              disabled
              className="flex-1 flex items-center justify-center gap-1 px-3 py-2 bg-nord-3 text-nord-4 rounded cursor-not-allowed font-medium"
              title="Waiting for the backend to report readiness"
            >
              <Loader2 size={16} className="animate-spin" />
              {pendingAction === 'start' ? 'Starting…' : 'Restarting…'}
            </button>
          ) : instance.status === 'stopped' || instance.status === 'failed' ? (
            <button
              onClick={() => handleAction('start', () => onStart(hostId, instance.id))}
              disabled={actionsDisabled}
              className="flex-1 flex items-center justify-center gap-1 px-3 py-2 bg-nord-14 text-nord-0 rounded hover:bg-opacity-90 transition-colors disabled:opacity-50 font-medium"
              title={hostReachable ? 'Start instance' : 'Host is offline'}
            >
              <Play size={16} />
              Start
            </button>
          ) : instance.status === 'running' ? (
            <>
              <button
                onClick={() => handleAction('stop', () => onStop(hostId, instance.id))}
                disabled={actionsDisabled}
                className="flex-1 flex items-center justify-center gap-1 px-3 py-2 bg-nord-11 text-nord-6 rounded hover:bg-opacity-90 transition-colors disabled:opacity-50 font-medium"
                title={hostReachable ? 'Stop instance' : 'Host is offline'}
              >
                <Square size={16} />
                Stop
              </button>
              <button
                onClick={() => handleAction('restart', () => onRestart(hostId, instance.id))}
                disabled={actionsDisabled}
                className="flex-1 flex items-center justify-center gap-1 px-3 py-2 bg-nord-10 text-nord-6 rounded hover:bg-nord-9 transition-colors disabled:opacity-50 font-medium"
                title={hostReachable ? 'Restart instance' : 'Host is offline'}
              >
                <RotateCw size={16} />
                Restart
              </button>
            </>
          ) : (
            <button disabled className="flex-1 px-3 py-2 bg-nord-3 text-nord-4 rounded cursor-not-allowed">
              {instance.status}...
            </button>
          )}
          <button
            onClick={() => setShowLogs(true)}
            className="px-3 py-2 border border-nord-3 text-nord-4 rounded hover:bg-nord-3 transition-colors"
            title="View logs"
          >
            <FileText size={16} />
          </button>
        </div>
      </div>

      {/* Log Viewer Modal */}
      {showLogs && (
        <LogViewer
          hostId={hostId}
          instanceId={instance.id}
          alias={instance.config.alias}
          onClose={() => setShowLogs(false)}
        />
      )}

      {/* Edit Instance Modal */}
      {showEdit && (
        <EditInstanceModal instance={instance} hostId={hostId} onClose={() => setShowEdit(false)} onUpdate={onUpdate} />
      )}
    </>
  );
}
