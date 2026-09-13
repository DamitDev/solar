/**
 * DeleteVirtualModelModal — delete confirmation (S-060).
 *
 * Deleting a virtual model only removes the name binding: concrete models and
 * instances are untouched, clients using the name start getting 404s.
 */

import { useState } from 'react';
import { X } from 'lucide-react';
import solarClient from '@/api/client';
import { VirtualModel } from '@/api/types';

interface DeleteVirtualModelModalProps {
  vm: VirtualModel;
  onClose: () => void;
  onDeleted: () => void;
}

export function DeleteVirtualModelModal({ vm, onClose, onDeleted }: DeleteVirtualModelModalProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    setLoading(true);
    setError(null);
    try {
      await solarClient.deleteVirtualModel(vm.name);
      onDeleted();
    } catch (err: any) {
      console.error('Failed to delete virtual model:', err);
      setError(err?.response?.data?.detail || err?.message || 'Failed to delete virtual model');
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-md w-full border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-bold text-nord-6">Delete virtual model</h2>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <p className="text-sm text-nord-4">
            Delete <code className="text-nord-6">{vm.name}</code>?
          </p>
          <p className="text-sm text-nord-4">
            The target models and instances are not touched, but clients calling this name will start receiving
            model-not-found errors.
          </p>

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
              {loading ? 'Deleting...' : 'Delete'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
