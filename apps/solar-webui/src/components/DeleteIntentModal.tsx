/**
 * DeleteIntentModal — confirmation with consequence + orphan option
 * (U-003, spec deployment-intent.md §12.4).
 *
 * Default deletes stop and remove the intent's managed instances. The
 * advanced "orphan" checkbox clears ownership markers instead, leaving the
 * instances running as manual instances.
 */

import { useState } from 'react';
import { X } from 'lucide-react';
import solarClient from '@/api/client';
import { Intent } from '@/api/types';

interface DeleteIntentModalProps {
  intent: Intent;
  onClose: () => void;
  onDeleted: () => void;
}

export function DeleteIntentModal({ intent, onClose, onDeleted }: DeleteIntentModalProps) {
  const [orphan, setOrphan] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    setLoading(true);
    setError(null);
    try {
      await solarClient.deleteIntent(intent.id, orphan);
      onDeleted();
    } catch (err: any) {
      console.error('Failed to delete intent:', err);
      setError(err?.response?.data?.detail || err?.message || 'Failed to delete intent');
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-md w-full border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-bold text-nord-6">Delete intent</h2>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-nord-4">
            Delete <code className="text-nord-6">{intent.alias}</code>?
          </p>
          <p className="text-sm text-nord-4">
            All instances managed by this intent will be stopped and the alias leaves the gateway registry.
          </p>

          <label className="flex items-start gap-3 text-sm text-nord-4 cursor-pointer">
            <input
              type="checkbox"
              checked={orphan}
              onChange={(e) => setOrphan(e.target.checked)}
              className="h-4 w-4 mt-0.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
            />
            <span>
              <span className="font-medium text-nord-6">Orphan instances</span> — keep them running, unmanaged (clears{' '}
              <code>managed_by</code>/<code>intent_id</code>).
            </span>
          </label>

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
              onClick={handleDelete}
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-11 text-nord-6 rounded-md hover:bg-nord-10 transition-colors disabled:opacity-50 font-medium"
            >
              {loading ? 'Deleting...' : orphan ? 'Delete (orphan)' : 'Delete'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
