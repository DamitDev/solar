/**
 * StorageSelectionBar — sticky footer shown while the storage selection is
 * non-empty. Aggregates the selection (model + host counts, reclaimable
 * bytes) and hosts the Clear / Delete selected actions.
 */

import { Trash2, X } from 'lucide-react';
import { formatBytes } from '@/lib/utils';

interface StorageSelectionBarProps {
  modelCount: number;
  hostCount: number;
  freedBytes: number;
  /** Note shown when the refetch deselected some entries (now in use). */
  prunedNote: string | null;
  onClear: () => void;
  onDelete: () => void;
}

export function StorageSelectionBar({
  modelCount,
  hostCount,
  freedBytes,
  prunedNote,
  onClear,
  onDelete,
}: StorageSelectionBarProps) {
  return (
    <div className="sticky bottom-0 z-40 bg-nord-1 border-t border-nord-3 shadow-2xl">
      <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
        <div className="min-w-0">
          <p className="text-sm text-nord-6">
            {modelCount} model{modelCount === 1 ? '' : 's'} selected on {hostCount} host
            {hostCount === 1 ? '' : 's'}
          </p>
          <p className="text-xs text-nord-4">frees ~{formatBytes(freedBytes)}</p>
          {prunedNote && <p className="text-xs text-nord-13 mt-0.5">{prunedNote}</p>}
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            onClick={onClear}
            className="bg-nord-3 text-nord-6 rounded-md px-4 py-2 hover:bg-nord-2 transition-colors flex items-center gap-1.5"
          >
            <X size={14} /> Clear
          </button>
          <button
            onClick={onDelete}
            className="bg-nord-11 text-nord-6 rounded-md px-4 py-2 flex items-center gap-2 hover:bg-opacity-90 transition-colors"
          >
            <Trash2 size={16} /> Delete selected
          </button>
        </div>
      </div>
    </div>
  );
}
