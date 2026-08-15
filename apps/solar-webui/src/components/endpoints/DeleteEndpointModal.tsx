import { useState } from 'react';
import { X, TriangleAlert, Trash2 } from 'lucide-react';
import solarClient from '@/api/client';
import type { ApiEndpoint } from '@/api/types';

interface DeleteEndpointModalProps {
  endpoint: ApiEndpoint;
  keyCount: number;
  onClose: () => void;
  onDeleted: () => void;
}

export function DeleteEndpointModal({ endpoint, keyCount, onClose, onDeleted }: DeleteEndpointModalProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    setLoading(true);
    setError(null);
    try {
      await solarClient.deleteEndpoint(endpoint.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete endpoint');
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
      <div className="bg-nord-1 rounded-lg shadow-2xl w-full max-w-md border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-semibold text-nord-11 flex items-center gap-2">
            <TriangleAlert size={18} />
            Delete Endpoint
          </h2>
          <button onClick={onClose} className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>
        <div className="p-4 space-y-4">
          {error && (
            <div className="p-3 bg-nord-11 bg-opacity-20 text-nord-11 rounded-md text-sm border border-nord-11">
              {error}
            </div>
          )}
          <p className="text-sm text-nord-4">
            Deleting <span className="font-semibold text-nord-6">{endpoint.name}</span> is permanent. {keyCount} API ke
            {keyCount === 1 ? 'y' : 'ys'} are cascade-deleted with the endpoint — every credential stops resolving
            immediately. Telemetry already attributed to the endpoint is kept.
          </p>
          <div className="flex gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleDelete}
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-11 text-nord-6 rounded-md hover:bg-nord-11 hover:bg-opacity-80 transition-colors disabled:opacity-50 font-medium flex items-center justify-center gap-2"
            >
              <Trash2 size={16} />
              {loading ? 'Deleting...' : 'Delete endpoint'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
