import { useState } from 'react';
import { X } from 'lucide-react';
import solarClient from '@/api/client';

interface AddHostModalProps {
  onClose: () => void;
  onSuccess: () => void;
}

export function AddHostModal({ onClose, onSuccess }: AddHostModalProps) {
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      await solarClient.createHost({ name, url, api_key: apiKey });
      onSuccess();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add host');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
      <div className="bg-nord-1 rounded-lg shadow-2xl w-full max-w-md border border-nord-3">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-semibold text-nord-6">Add Solar Host</h2>
          <button onClick={onClose} className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          {error && (
            <div className="p-3 bg-nord-11 bg-opacity-20 text-nord-11 rounded-md text-sm border border-nord-11">
              {error}
            </div>
          )}

          <div>
            <label className="block text-sm font-medium mb-1 text-nord-4">Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Mac Studio 1"
              required
              className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1 text-nord-4">URL</label>
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://192.168.1.100:8001"
              required
              className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1 text-nord-4">API Key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="host-api-key"
              required
              className="w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10"
            />
          </div>

          <div className="flex gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-10 text-nord-6 rounded-md hover:bg-nord-9 transition-colors disabled:opacity-50 font-medium"
            >
              {loading ? 'Adding...' : 'Add Host'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
