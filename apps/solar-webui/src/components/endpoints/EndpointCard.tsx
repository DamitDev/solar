import { useState } from 'react';
import { Plus, Pencil, Trash2, BarChart3, Key, Eye, EyeOff, Copy, Check, RefreshCw, Globe2 } from 'lucide-react';
import solarClient from '@/api/client';
import type { ApiEndpoint, ApiKey, EndpointUsageResponse } from '@/api/types';
import { ApiKeyFormModal } from './ApiKeyFormModal';

export interface EndpointModels {
  count: number;
  aliases: string[];
}

function maskKey(key: string): string {
  if (!key || key.length <= 8) return '••••••••';
  return key.slice(0, 8) + '…';
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return 'never';
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

function copyToClipboard(value: string): void {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(value).catch(() => {
      // Clipboard API can fail on non-secure contexts; ignore.
    });
  }
}

interface EndpointCardProps {
  endpoint: ApiEndpoint;
  keys: ApiKey[];
  usage: EndpointUsageResponse | null;
  models: EndpointModels | null;
  onEdit: (ep: ApiEndpoint) => void;
  onDelete: (ep: ApiEndpoint) => void;
  onAddKey: (ep: ApiEndpoint) => void;
  /** Refresh keys/endpoints after an in-card mutation (toggle/delete/edit/rotate). */
  onKeysChanged: () => void;
}

function KeyRow({
  apiKey,
  onToggleEnabled,
  onRotate,
  onDelete,
  onEdit,
}: {
  apiKey: ApiKey;
  onToggleEnabled: (k: ApiKey) => void;
  onRotate: (k: ApiKey) => void;
  onDelete: (k: ApiKey) => void;
  onEdit: (k: ApiKey) => void;
}) {
  const [showKey, setShowKey] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    copyToClipboard(apiKey.key);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      className={`flex items-center gap-3 px-3 py-2 rounded-md border ${
        apiKey.enabled ? 'bg-nord-2 border-nord-3' : 'bg-nord-2 bg-opacity-50 border-nord-3 opacity-60'
      }`}
    >
      <button
        onClick={() => onEdit(apiKey)}
        title="Edit key"
        className="p-1 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors shrink-0"
      >
        <Pencil size={14} />
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-nord-6 truncate">{apiKey.name}</span>
          <span className="text-xs text-nord-4">Last used {formatDate(apiKey.last_used_at)}</span>
        </div>
        <code className="flex items-center gap-1.5 text-xs text-nord-5 font-mono break-all">
          <Key size={12} className="text-nord-4 shrink-0" />
          {showKey ? apiKey.key : maskKey(apiKey.key)}
        </code>
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <button
          onClick={() => setShowKey(!showKey)}
          className="p-1.5 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors"
          title={showKey ? 'Hide key' : 'Show key'}
        >
          {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
        <button
          onClick={handleCopy}
          className="p-1.5 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors"
          title="Copy key"
        >
          {copied ? <Check size={14} className="text-nord-13" /> : <Copy size={14} />}
        </button>
        <label className="flex items-center gap-1.5 text-xs text-nord-4 cursor-pointer ml-1">
          <input
            type="checkbox"
            checked={apiKey.enabled}
            onChange={() => onToggleEnabled(apiKey)}
            className="accent-nord-10"
            title="Enable / disable key"
          />
          Enabled
        </label>
        <button
          onClick={() => onRotate(apiKey)}
          className="p-1.5 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-10 transition-colors"
          title="Rotate key"
        >
          <RefreshCw size={14} />
        </button>
        <button
          onClick={() => onDelete(apiKey)}
          className="p-1.5 rounded hover:bg-nord-3 text-nord-4 hover:text-nord-11 transition-colors"
          title="Delete key"
        >
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  );
}

export function EndpointCard({
  endpoint,
  keys,
  usage,
  models,
  onEdit,
  onDelete,
  onAddKey,
  onKeysChanged,
}: EndpointCardProps) {
  const [rotatingKey, setRotatingKey] = useState<ApiKey | null>(null);
  const [editingKey, setEditingKey] = useState<ApiKey | null>(null);

  const u = usage?.usage;
  const totalRequests = u?.total_requests ?? 0;
  const totalTokens = u?.total_tokens ?? 0;
  const avgLatency = u?.avg_duration_s != null ? u.avg_duration_s.toFixed(2) : '—';

  const handleToggleEnabled = async (apiKey: ApiKey) => {
    try {
      await solarClient.updateApiKey(apiKey.id, { enabled: !apiKey.enabled });
      onKeysChanged();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to update key');
    }
  };

  const handleDeleteKey = async (apiKey: ApiKey) => {
    if (!confirm(`Delete API key "${apiKey.name}"? Requests using the credential will stop resolving.`)) return;
    try {
      await solarClient.deleteApiKey(apiKey.id);
      onKeysChanged();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete key');
    }
  };

  return (
    <div className="bg-nord-1 rounded-lg border border-nord-3 p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <h3 className="text-lg font-semibold text-nord-6 truncate">{endpoint.name}</h3>
          {endpoint.description && <p className="text-sm text-nord-4 mt-1 line-clamp-2">{endpoint.description}</p>}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {endpoint.serve_all_models ? (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-nord-10 bg-opacity-20 text-nord-10 rounded text-xs font-medium">
                <Globe2 size={12} />
                All models
              </span>
            ) : (
              <>
                <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-nord-14 bg-opacity-20 text-nord-14 text-xs font-medium">
                  {models?.count ?? 0} model{models?.count === 1 ? '' : 's'}
                </span>
                {endpoint.model_patterns.map((p) => (
                  <code
                    key={p}
                    className="px-1.5 py-0.5 bg-nord-2 border border-nord-3 rounded text-xs text-nord-5 font-mono"
                  >
                    {p}
                  </code>
                ))}
              </>
            )}
            <span className="text-xs text-nord-4">
              {endpoint.key_count} key{endpoint.key_count === 1 ? '' : 's'}
            </span>
          </div>
          <p className="text-xs text-nord-4 mt-1">Created {formatDate(endpoint.created_at)}</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onEdit(endpoint)}
            className="p-2 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-6 transition-colors"
            title="Edit endpoint"
          >
            <Pencil size={18} />
          </button>
          <button
            onClick={() => onDelete(endpoint)}
            className="p-2 rounded hover:bg-nord-11 hover:bg-opacity-20 text-nord-4 hover:text-nord-11 transition-colors"
            title="Delete endpoint"
          >
            <Trash2 size={18} />
          </button>
        </div>
      </div>

      <div className="mt-4 pt-4 border-t border-nord-3 flex items-center gap-4 text-sm">
        <div className="flex items-center gap-1.5 text-nord-4">
          <BarChart3 size={16} />
          <span>24h requests: {totalRequests.toLocaleString()}</span>
        </div>
        <div className="text-nord-4">Tokens: {totalTokens.toLocaleString()}</div>
        <div className="text-nord-4">Avg latency: {avgLatency}s</div>
      </div>

      <div className="mt-4 space-y-2">
        {keys.map((k) => (
          <KeyRow
            key={k.id}
            apiKey={k}
            onEdit={setEditingKey}
            onToggleEnabled={handleToggleEnabled}
            onRotate={setRotatingKey}
            onDelete={handleDeleteKey}
          />
        ))}
        <button
          onClick={() => onAddKey(endpoint)}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg border border-dashed border-nord-3 text-nord-4 hover:text-nord-6 hover:border-nord-10 transition-colors text-sm"
        >
          <Plus size={16} />
          Add API key
        </button>
      </div>

      {editingKey && (
        <ApiKeyFormModal
          endpoints={[]}
          endpoint={endpoint}
          editingKey={editingKey}
          onClose={() => setEditingKey(null)}
          onDone={() => {
            setEditingKey(null);
            onKeysChanged();
          }}
        />
      )}
      {rotatingKey && (
        <ApiKeyFormModal
          endpoints={[]}
          endpoint={endpoint}
          rotatingKey={rotatingKey}
          onClose={() => setRotatingKey(null)}
          onDone={() => {
            setRotatingKey(null);
            onKeysChanged();
          }}
        />
      )}
    </div>
  );
}
