import { useState } from 'react';
import { ShieldQuestion, Check, X, ChevronDown, ChevronUp, Clock } from 'lucide-react';
import { PendingHost } from '@/api/types';
import solarClient from '@/api/client';

interface PendingHostBannerProps {
  pendingHosts: Map<string, PendingHost>;
  onApproved: () => void;
}

interface ApproveFormState {
  pendingId: string;
  name: string;
  url: string;
  loading: boolean;
  error: string | null;
}

export function PendingHostBanner({ pendingHosts, onApproved }: PendingHostBannerProps) {
  const [expanded, setExpanded] = useState(true);
  const [approveForm, setApproveForm] = useState<ApproveFormState | null>(null);

  const pending = Array.from(pendingHosts.values());

  const handleApprove = async (form: ApproveFormState) => {
    setApproveForm({ ...form, loading: true, error: null });
    try {
      await solarClient.approveHost(form.pendingId, { name: form.name, url: form.url });
      setApproveForm(null);
      onApproved();
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || 'Approval failed';
      setApproveForm({ ...form, loading: false, error: msg });
    }
  };

  const handleReject = async (pendingId: string) => {
    if (!confirm('Reject this host? It will be disconnected.')) return;
    try {
      await solarClient.rejectHost(pendingId);
    } catch (err) {
      console.error('Failed to reject host:', err);
    }
  };

  const startApprove = (p: PendingHost) => {
    setApproveForm({
      pendingId: p.pending_id,
      name: p.host_name || '',
      url: '',
      loading: false,
      error: null,
    });
  };

  return (
    <div className="mb-6 rounded-lg border border-nord-13 border-opacity-50 bg-nord-13 bg-opacity-10 overflow-hidden">
      {/* Header */}
      <button
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-nord-13 hover:bg-opacity-5 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          <ShieldQuestion size={20} className="text-nord-13" />
          <span className="font-medium text-nord-6">
            {pending.length} pending host{pending.length !== 1 ? 's' : ''} awaiting approval
          </span>
        </div>
        {expanded ? <ChevronUp size={18} className="text-nord-4" /> : <ChevronDown size={18} className="text-nord-4" />}
      </button>

      {/* Body */}
      {expanded && (
        <div className="px-4 pb-4 space-y-3">
          {pending.map((p) => (
            <div key={p.pending_id} className="flex items-start gap-4 bg-nord-1 rounded-lg p-4 border border-nord-3">
              {approveForm?.pendingId === p.pending_id ? (
                /* Approve form inline */
                <div className="flex-1 space-y-3">
                  {approveForm.error && (
                    <div className="text-sm text-nord-11 bg-nord-11 bg-opacity-10 p-2 rounded">{approveForm.error}</div>
                  )}
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs font-medium text-nord-4 mb-1">Name</label>
                      <input
                        type="text"
                        value={approveForm.name}
                        onChange={(e) => setApproveForm({ ...approveForm, name: e.target.value })}
                        placeholder="e.g. Mac Studio 1"
                        className="w-full px-3 py-1.5 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded text-sm focus:outline-none focus:ring-2 focus:ring-nord-10"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-nord-4 mb-1">URL</label>
                      <input
                        type="text"
                        value={approveForm.url}
                        onChange={(e) => setApproveForm({ ...approveForm, url: e.target.value })}
                        placeholder="http://192.168.1.100:8001"
                        className="w-full px-3 py-1.5 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded text-sm focus:outline-none focus:ring-2 focus:ring-nord-10"
                      />
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-nord-4 mr-auto">
                      Key: <code className="text-nord-13">{p.api_key_preview}</code>
                      {p.instance_count != null && (
                        <>
                          {' '}
                          &middot; {p.instance_count} instance{p.instance_count !== 1 ? 's' : ''}
                        </>
                      )}
                    </span>
                    <button
                      onClick={() => setApproveForm(null)}
                      disabled={approveForm.loading}
                      className="px-3 py-1 text-sm bg-nord-3 text-nord-4 rounded hover:bg-nord-2 transition-colors"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => handleApprove(approveForm)}
                      disabled={approveForm.loading || !approveForm.name || !approveForm.url}
                      className="flex items-center gap-1 px-3 py-1 text-sm bg-nord-14 text-nord-0 font-medium rounded hover:opacity-90 transition-colors disabled:opacity-50"
                    >
                      <Check size={14} />
                      {approveForm.loading ? 'Approving...' : 'Approve'}
                    </button>
                  </div>
                </div>
              ) : (
                /* Default row */
                <>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <Clock size={14} className="text-nord-13 flex-shrink-0" />
                      <span className="font-medium text-nord-6 truncate">{p.host_name || 'Unknown host'}</span>
                      <code className="text-xs text-nord-4 bg-nord-2 px-1.5 py-0.5 rounded">{p.api_key_preview}</code>
                    </div>
                    <div className="text-xs text-nord-4 mt-1">
                      Connected {new Date(p.connected_at).toLocaleTimeString()}
                      {p.instance_count != null && (
                        <>
                          {' '}
                          &middot; {p.instance_count} instance{p.instance_count !== 1 ? 's' : ''}
                        </>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <button
                      onClick={() => startApprove(p)}
                      className="flex items-center gap-1 px-3 py-1.5 text-sm bg-nord-14 text-nord-0 font-medium rounded hover:opacity-90 transition-colors"
                    >
                      <Check size={14} />
                      Approve
                    </button>
                    <button
                      onClick={() => handleReject(p.pending_id)}
                      className="flex items-center gap-1 px-3 py-1.5 text-sm bg-nord-11 bg-opacity-80 text-nord-6 rounded hover:bg-opacity-100 transition-colors"
                    >
                      <X size={14} />
                      Reject
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
