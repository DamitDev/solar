/**
 * StorageByModelView — Tab B of the Storage page: one panel per distinct
 * model, listing every host holding a copy. The view that scales past a
 * handful of hosts, with the aggregated "Delete on all idle hosts" action.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react';
import { StoredModel } from '@/api/types';
import { cn, formatBytes, formatDateTime, formatRelativeTime, getStatusColor } from '@/lib/utils';
import {
  ModelGroup,
  groupHasMixedVersions,
  groupIdleCopies,
  groupInUseHostCount,
  groupSizeBytes,
  isModelInUse,
  originBadgeClass,
  originLabel,
  selectionKey,
  StoredModelCopy,
} from '@/lib/storage';

interface StorageByModelViewProps {
  /** Paginated slice of model groups, sorted by aggregate size descending. */
  groups: ModelGroup[];
  selected: Set<string>;
  onToggle: (key: string) => void;
  onToggleAll: (keys: string[]) => void;
  onDeleteOne: (hostId: string, slug: string) => void;
  /** Opens the delete modal preselected with exactly these copies. */
  onDeleteCopies: (copies: StoredModelCopy[]) => void;
}

function GroupOriginBadge({ origin }: { origin: StoredModel['origin'] }) {
  return (
    <span className={cn('px-2 py-0.5 rounded text-xs font-medium', originBadgeClass(origin))}>
      {originLabel(origin)}
    </span>
  );
}

function GroupPanel({
  group,
  selected,
  onToggle,
  onToggleAll,
  onDeleteOne,
  onDeleteCopies,
}: {
  group: ModelGroup;
  selected: Set<string>;
  onToggle: (key: string) => void;
  onToggleAll: (keys: string[]) => void;
  onDeleteOne: (hostId: string, slug: string) => void;
  onDeleteCopies: (copies: StoredModelCopy[]) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const idleCopies = useMemo(() => groupIdleCopies(group), [group]);
  const idleKeys = idleCopies.map((c) => selectionKey(c.hostId, c.model.slug));
  const allSelected = idleKeys.length > 0 && idleKeys.every((k) => selected.has(k));
  const someSelected = idleKeys.some((k) => selected.has(k));

  const checkboxRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (checkboxRef.current) {
      checkboxRef.current.indeterminate = someSelected && !allSelected;
    }
  }, [someSelected, allSelected]);

  const inUseHosts = groupInUseHostCount(group);
  const mixed = groupHasMixedVersions(group);

  return (
    <div>
      <div
        className="bg-nord-1 border border-nord-3 rounded px-4 py-3 flex items-center gap-3 cursor-pointer hover:bg-nord-2/50 transition-colors"
        onClick={() => setExpanded((e) => !e)}
      >
        {expanded ? (
          <ChevronDown size={16} className="text-nord-4 flex-shrink-0" />
        ) : (
          <ChevronRight size={16} className="text-nord-4 flex-shrink-0" />
        )}

        <input
          type="checkbox"
          ref={checkboxRef}
          checked={allSelected}
          disabled={idleKeys.length === 0}
          onChange={() => onToggleAll(idleKeys)}
          onClick={(e) => e.stopPropagation()}
          className="accent-nord-10 flex-shrink-0 disabled:opacity-40"
          title={idleKeys.length === 0 ? 'Every copy is in use' : 'Select all idle copies'}
        />

        <span className="font-medium text-nord-6 truncate">{group.modelName}</span>
        <GroupOriginBadge origin={group.origin} />
        <span className="bg-nord-2 text-nord-4 rounded px-2 py-0.5 text-xs flex-shrink-0">
          {group.copies[0]?.model.version ?? '—'}
        </span>
        {mixed && <span className="text-xs text-nord-13 flex-shrink-0">mixed versions</span>}

        <span className="text-sm text-nord-4 ml-auto flex-shrink-0">
          On {group.copies.length} host{group.copies.length === 1 ? '' : 's'} · {formatBytes(groupSizeBytes(group))}
        </span>
        {inUseHosts > 0 && (
          <span className="bg-nord-13/20 text-nord-13 rounded px-2 py-0.5 text-xs flex-shrink-0">
            in use on {inUseHosts} host{inUseHosts === 1 ? '' : 's'}
          </span>
        )}
      </div>

      {expanded && (
        <div className="border border-t-0 border-nord-3 rounded-b overflow-x-auto bg-nord-1">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-nord-3 text-left text-xs text-nord-4 uppercase tracking-wide">
                <th className="px-4 py-3 w-8"></th>
                <th className="px-4 py-3 font-medium">Host</th>
                <th className="px-4 py-3 font-medium">Version</th>
                <th className="px-4 py-3 font-medium text-right">Size</th>
                <th className="px-4 py-3 font-medium">Downloaded</th>
                <th className="px-4 py-3 font-medium">Used by</th>
                <th className="px-4 py-3 font-medium">Path</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {group.copies.map((copy) => {
                const inUse = isModelInUse(copy.model);
                const key = selectionKey(copy.hostId, copy.model.slug);
                const usedByTitle =
                  copy.model.in_use_by.length > 0
                    ? `In use by ${copy.model.in_use_by.map((r) => r.alias).join(', ')}`
                    : undefined;
                return (
                  <tr key={`${copy.hostId}:${copy.model.slug}`} className="border-b border-nord-3 hover:bg-nord-2/50">
                    <td className="px-4 py-3">
                      <input
                        type="checkbox"
                        checked={selected.has(key)}
                        disabled={inUse}
                        onChange={() => onToggle(key)}
                        className="accent-nord-10 disabled:opacity-40"
                        title={usedByTitle}
                      />
                    </td>
                    <td className="px-4 py-3">
                      <span className="flex items-center gap-1.5 text-nord-6">
                        <span
                          className={cn(
                            'inline-block w-2 h-2 rounded-full flex-shrink-0',
                            getStatusColor('online').split(' ')[0],
                          )}
                          title="online"
                        />
                        {copy.hostName}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-nord-4">{copy.model.version ?? '—'}</td>
                    <td className="px-4 py-3 text-right tabular-nums text-nord-6">
                      {formatBytes(copy.model.size_bytes)}
                    </td>
                    <td className="px-4 py-3 text-nord-4" title={formatDateTime(copy.model.downloaded_at ?? undefined)}>
                      {copy.model.downloaded_at ? formatRelativeTime(copy.model.downloaded_at) : '—'}
                    </td>
                    <td className="px-4 py-3 text-nord-4">
                      {inUse ? (
                        <span className="flex flex-wrap gap-1">
                          {copy.model.in_use_by.map((ref) => (
                            <span
                              key={ref.instance_id}
                              className="bg-nord-2 text-nord-4 rounded px-2 py-0.5 text-xs"
                              title={`${ref.status} · ${ref.instance_id}`}
                            >
                              {ref.alias}
                            </span>
                          ))}
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className="font-mono text-xs text-nord-4 truncate max-w-[16rem] block"
                        title={copy.model.path}
                      >
                        {copy.model.path}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => onDeleteOne(copy.hostId, copy.model.slug)}
                        disabled={inUse}
                        className="text-nord-11 hover:bg-nord-11/15 rounded p-1.5 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                        title={
                          inUse
                            ? `In use by ${copy.model.in_use_by.map((r) => r.alias).join(', ')}`
                            : 'Delete this copy'
                        }
                      >
                        <Trash2 size={16} />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {idleCopies.length > 0 && (
            <div className="flex justify-end p-3">
              <button
                onClick={() => onDeleteCopies(idleCopies)}
                className="bg-nord-11/15 border border-nord-11 text-nord-11 rounded px-3 py-1.5 text-sm hover:bg-nord-11/25 transition-colors"
              >
                Delete on all idle hosts ({idleCopies.length} · {formatBytes(groupSizeBytes(group))})
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function StorageByModelView({
  groups,
  selected,
  onToggle,
  onToggleAll,
  onDeleteOne,
  onDeleteCopies,
}: StorageByModelViewProps) {
  return (
    <div className="space-y-3">
      {groups.map((group) => (
        <GroupPanel
          key={group.key}
          group={group}
          selected={selected}
          onToggle={onToggle}
          onToggleAll={onToggleAll}
          onDeleteOne={onDeleteOne}
          onDeleteCopies={onDeleteCopies}
        />
      ))}
    </div>
  );
}
