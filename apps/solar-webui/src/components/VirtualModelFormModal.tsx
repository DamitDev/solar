/**
 * VirtualModelFormModal — create/edit a virtual model (S-060).
 *
 * Ordered target list with add/remove/reorder (up/down buttons — the order is
 * the failover order, no drag-and-drop dependency), plus the declared
 * contract: minimum context size and required capabilities.
 */

import { useState } from 'react';
import { ArrowDown, ArrowUp, Plus, X } from 'lucide-react';
import solarClient from '@/api/client';
import { VirtualModel, VirtualModelContract } from '@/api/types';

const KNOWN_CAPABILITIES = ['completion', 'multimodal', 'embedding', 'classification'];

interface VirtualModelFormModalProps {
  /** null = create mode. */
  existing: VirtualModel | null;
  onClose: () => void;
  onSaved: () => void;
}

export function VirtualModelFormModal({ existing, onClose, onSaved }: VirtualModelFormModalProps) {
  const [name, setName] = useState(existing?.name ?? '');
  const [targets, setTargets] = useState<string[]>(existing?.targets ?? []);
  const [newTarget, setNewTarget] = useState('');
  const [description, setDescription] = useState(existing?.description ?? '');
  const [contextSize, setContextSize] = useState<string>(
    existing?.contract?.context_size ? String(existing.contract.context_size) : '',
  );
  const [capabilities, setCapabilities] = useState<string[]>(existing?.contract?.capabilities ?? []);
  const [customCapability, setCustomCapability] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isEdit = existing !== null;

  const addTarget = () => {
    const t = newTarget.trim();
    if (!t) return;
    if (targets.includes(t)) {
      setError('Target already in the list');
      return;
    }
    setTargets([...targets, t]);
    setNewTarget('');
  };

  const removeTarget = (idx: number) => setTargets(targets.filter((_, i) => i !== idx));

  const move = (idx: number, dir: -1 | 1) => {
    const next = [...targets];
    const j = idx + dir;
    if (j < 0 || j >= next.length) return;
    [next[idx], next[j]] = [next[j], next[idx]];
    setTargets(next);
  };

  const toggleCapability = (cap: string) => {
    setCapabilities(capabilities.includes(cap) ? capabilities.filter((c) => c !== cap) : [...capabilities, cap]);
  };

  const addCustomCapability = () => {
    const cap = customCapability.trim();
    if (cap && !capabilities.includes(cap)) setCapabilities([...capabilities, cap]);
    setCustomCapability('');
  };

  const handleSubmit = async () => {
    setLoading(true);
    setError(null);
    setWarnings([]);
    try {
      const contract: VirtualModelContract = {
        context_size: contextSize ? parseInt(contextSize, 10) : null,
        capabilities: capabilities.length > 0 ? capabilities : null,
      };
      if (isEdit) {
        const saved = await solarClient.updateVirtualModel(existing!.name, {
          targets,
          description: description || null,
          contract,
        });
        setWarnings(saved.warnings ?? []);
        if (!saved.warnings?.length) onSaved();
      } else {
        const saved = await solarClient.createVirtualModel({
          name: name.trim(),
          targets,
          description: description || null,
          contract,
        });
        setWarnings(saved.warnings ?? []);
        if (!saved.warnings?.length) onSaved();
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      if (detail?.errors && Array.isArray(detail.errors)) {
        setError(detail.errors.join('; '));
      } else {
        setError(detail?.detail || detail || err?.message || 'Failed to save virtual model');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-lg w-full border border-nord-3 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-bold text-nord-6">{isEdit ? 'Edit virtual model' : 'New virtual model'}</h2>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={18} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-nord-4 mb-1">Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={isEdit}
              placeholder="team-chat"
              className="w-full px-3 py-2 bg-nord-0 border border-nord-3 rounded-md text-nord-6 font-mono text-sm disabled:opacity-50 focus:outline-none focus:border-nord-8"
            />
            <p className="text-xs text-nord-4 mt-1">
              Clients use this name exactly. Must not collide with a real model name.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-nord-4 mb-1">Targets (failover order)</label>
            <div className="space-y-1.5 mb-2">
              {targets.length === 0 && <p className="text-xs text-nord-4">No targets yet — add at least one.</p>}
              {targets.map((t, idx) => (
                <div key={t} className="flex items-center gap-2">
                  <span className="text-xs text-nord-4 w-5 text-right">{idx + 1}.</span>
                  <span className="flex-1 px-2 py-1 bg-nord-0 border border-nord-3 rounded font-mono text-xs text-nord-6">
                    {t}
                  </span>
                  <button
                    onClick={() => move(idx, -1)}
                    disabled={idx === 0}
                    className="p-1 text-nord-4 hover:text-nord-6 disabled:opacity-30"
                    title="Move up"
                  >
                    <ArrowUp size={14} />
                  </button>
                  <button
                    onClick={() => move(idx, 1)}
                    disabled={idx === targets.length - 1}
                    className="p-1 text-nord-4 hover:text-nord-6 disabled:opacity-30"
                    title="Move down"
                  >
                    <ArrowDown size={14} />
                  </button>
                  <button
                    onClick={() => removeTarget(idx)}
                    className="p-1 text-nord-4 hover:text-nord-11"
                    title="Remove"
                  >
                    <X size={14} />
                  </button>
                </div>
              ))}
            </div>
            <div className="flex gap-2">
              <input
                type="text"
                value={newTarget}
                onChange={(e) => setNewTarget(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addTarget())}
                placeholder="deepseek-v4-flash:284b"
                className="flex-1 px-3 py-1.5 bg-nord-0 border border-nord-3 rounded-md text-nord-6 font-mono text-sm focus:outline-none focus:border-nord-8"
              />
              <button
                onClick={addTarget}
                className="px-3 py-1.5 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors flex items-center gap-1"
              >
                <Plus size={14} /> Add
              </button>
            </div>
            <p className="text-xs text-nord-4 mt-1">
              Tried in order; the first target that is up and satisfies the contract serves the request.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-nord-4 mb-1">Description (optional)</label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this name is for"
              className="w-full px-3 py-2 bg-nord-0 border border-nord-3 rounded-md text-nord-6 text-sm focus:outline-none focus:border-nord-8"
            />
          </div>

          <div className="border border-nord-3 rounded-md p-3 space-y-3">
            <p className="text-sm font-medium text-nord-6">Contract</p>
            <p className="text-xs text-nord-4">
              The guarantee every target must satisfy. Requests fail over to the next target when one cannot.
            </p>
            <div>
              <label className="block text-xs text-nord-4 mb-1">Minimum context size (tokens, optional)</label>
              <input
                type="number"
                min="1"
                value={contextSize}
                onChange={(e) => setContextSize(e.target.value)}
                placeholder="200000"
                className="w-full px-3 py-1.5 bg-nord-0 border border-nord-3 rounded-md text-nord-6 text-sm focus:outline-none focus:border-nord-8"
              />
            </div>
            <div>
              <label className="block text-xs text-nord-4 mb-1">Required capabilities (optional)</label>
              <div className="flex flex-wrap gap-2 mb-2">
                {KNOWN_CAPABILITIES.map((cap) => (
                  <label key={cap} className="flex items-center gap-1.5 text-xs text-nord-4 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={capabilities.includes(cap)}
                      onChange={() => toggleCapability(cap)}
                      className="h-3.5 w-3.5 rounded border-nord-3 bg-nord-1 text-nord-10 focus:ring-nord-10"
                    />
                    {cap}
                  </label>
                ))}
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={customCapability}
                  onChange={(e) => setCustomCapability(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addCustomCapability())}
                  placeholder="custom capability"
                  className="flex-1 px-3 py-1 bg-nord-0 border border-nord-3 rounded-md text-nord-6 text-xs focus:outline-none focus:border-nord-8"
                />
                <button
                  onClick={addCustomCapability}
                  className="px-2 py-1 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 text-xs"
                >
                  Add
                </button>
              </div>
            </div>
          </div>

          {warnings.length > 0 && (
            <div className="p-3 bg-nord-11 bg-opacity-10 border border-nord-11 rounded-md">
              <p className="text-xs font-medium text-nord-11 mb-1">Saved with warnings</p>
              <ul className="text-xs text-nord-4 list-disc list-inside">
                {warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </div>
          )}

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
              onClick={handleSubmit}
              disabled={loading || targets.length === 0 || (!isEdit && !name.trim())}
              className="flex-1 px-4 py-2 bg-nord-10 text-nord-6 rounded-md hover:bg-nord-9 transition-colors disabled:opacity-50 font-medium"
            >
              {loading ? 'Saving...' : isEdit ? 'Save changes' : 'Create'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
