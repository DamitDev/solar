/**
 * StorageByHostView — Tab A of the Storage page: one panel per host with
 * the local model inventory, guarded by in-use checkboxes.
 */

import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronDown, ChevronRight, ChevronUp, Server, Trash2 } from 'lucide-react';
import { HostStorage, StoredModel } from '@/api/types';
import { cn, formatBytes, formatDateTime, formatRelativeTime, getMemoryColor, getStatusColor } from '@/lib/utils';
import { isModelInUse, originBadgeClass, originLabel, selectionKey } from '@/lib/storage';

interface StorageByHostViewProps {
  /** Filtered hosts, paginated slice, sorted by total size descending. */
  hosts: HostStorage[];
  selected: Set<string>;
  onToggle: (key: string) => void;
  onToggleAll: (keys: string[]) => void;
  onDeleteOne: (hostId: string, slug: string) => void;
}

type SortKey = 'name' | 'size' | 'downloaded';

const SORT_HEADERS: Array<{ key: SortKey; label: string; className?: string }> = [
  { key: 'name', label: 'Model' },
  { key: 'size', label: 'Size', className: 'text-right' },
  { key: 'downloaded', label: 'Downloaded' },
];

function OriginBadge({ model }: { model: StoredModel }) {
  return (
    <span
      className={cn('px-2 py-0.5 rounded text-xs font-medium', originBadgeClass(model.origin))}
      title={model.source_uri ?? undefined}
    >
      {originLabel(model.origin)}
      {model.harbor_ref && (
        <span className="block text-[10px] truncate max-w-[10rem] opacity-80">{model.harbor_ref}</span>
      )}
    </span>
  );
}

function UsedByChips({ model }: { model: StoredModel }) {
  if (model.in_use_by.length === 0) {
    return <span className="text-nord-4">—</span>;
  }
  return (
    <span className="flex flex-wrap gap-1">
      {model.in_use_by.map((ref) => (
        <Link
          key={ref.instance_id}
          to="/hosts"
          title={`${ref.status} · ${ref.instance_id}`}
          className="bg-nord-2 text-nord-4 rounded px-2 py-0.5 text-xs hover:text-nord-6 hover:bg-nord-3 transition-colors"
        >
          {ref.alias || ref.instance_id.slice(0, 8)}
        </Link>
      ))}
    </span>
  );
}

function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
  className,
}: {
  label: string;
  sortKey: SortKey;
  sort: { key: SortKey; dir: 'asc' | 'desc' };
  onSort: (key: SortKey) => void;
  className?: string;
}) {
  const active = sort.key === sortKey;
  return (
    <th className={cn('px-4 py-3 font-medium select-none cursor-pointer', className)}>
      <button
        onClick={() => onSort(sortKey)}
        className={cn(
          'inline-flex items-center gap-1 uppercase tracking-wide hover:text-nord-6 transition-colors',
          active ? 'text-nord-6' : 'text-nord-4',
        )}
      >
        {label}
        {active && (sort.dir === 'asc' ? <ChevronUp size={12} /> : <ChevronDown size={12} />)}
      </button>
    </th>
  );
}

function HostPanel({
  host,
  selected,
  onToggle,
  onToggleAll,
  onDeleteOne,
}: {
  host: HostStorage;
  selected: Set<string>;
  onToggle: (key: string) => void;
  onToggleAll: (keys: string[]) => void;
  onDeleteOne: (hostId: string, slug: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({
    key: 'size',
    dir: 'desc',
  });

  const deletable = host.models.filter((m) => !isModelInUse(m));
  const deletableKeys = deletable.map((m) => selectionKey(host.host_id, m.slug));
  const allSelected = deletableKeys.length > 0 && deletableKeys.every((k) => selected.has(k));
  const someSelected = deletableKeys.some((k) => selected.has(k));

  const checkboxRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (checkboxRef.current) {
      checkboxRef.current.indeterminate = someSelected && !allSelected;
    }
  }, [someSelected, allSelected]);

  const sortedModels = useMemo(() => {
    const models = [...host.models];
    const dir = sort.dir === 'asc' ? 1 : -1;
    models.sort((a, b) => {
      switch (sort.key) {
        case 'name':
          return (a.model_name ?? a.slug).localeCompare(b.model_name ?? b.slug) * dir;
        case 'size':
          return (a.size_bytes - b.size_bytes) * dir;
        case 'downloaded':
          return (a.downloaded_at ?? '').localeCompare(b.downloaded_at ?? '') * dir;
      }
    });
    return models;
  }, [host.models, sort]);

  const handleSort = (key: SortKey) => {
    setSort((prev) => (prev.key === key ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }));
  };

  const diskPercent =
    host.disk_total_gb != null && host.disk_total_gb > 0 && host.disk_used_gb != null
      ? (host.disk_used_gb / host.disk_total_gb) * 100
      : null;

  return (
    <div className={cn(host.reachable ? '' : 'opacity-60')}>
      <div
        className={cn(
          'bg-nord-1 border border-nord-3 rounded px-4 py-3 flex items-center gap-3',
          host.reachable && 'cursor-pointer hover:bg-nord-2/50 transition-colors',
        )}
        onClick={host.reachable ? () => setExpanded((e) => !e) : undefined}
      >
        {host.reachable ? (
          expanded ? (
            <ChevronDown size={16} className="text-nord-4 flex-shrink-0" />
          ) : (
            <ChevronRight size={16} className="text-nord-4 flex-shrink-0" />
          )
        ) : (
          <ChevronRight size={16} className="text-nord-3 flex-shrink-0" />
        )}

        <input
          type="checkbox"
          ref={checkboxRef}
          checked={allSelected}
          disabled={!host.reachable || deletableKeys.length === 0}
          onChange={() => onToggleAll(deletableKeys)}
          onClick={(e) => e.stopPropagation()}
          className="accent-nord-10 flex-shrink-0 disabled:opacity-40"
          title={
            deletableKeys.length === 0 ? 'No deletable models on this host' : 'Select all unused models on this host'
          }
        />

        <Server size={16} className="text-nord-4 flex-shrink-0" />
        <span className="font-medium text-nord-6 truncate">{host.host_name}</span>
        {!host.reachable && (
          <span
            className="bg-nord-3 text-nord-4 rounded px-2 py-0.5 text-xs flex-shrink-0"
            title={host.error ?? 'Host unreachable'}
          >
            unreachable
          </span>
        )}
        <span
          className={cn(
            'px-2 py-0.5 rounded-full text-xs font-medium flex-shrink-0',
            getStatusColor(host.reachable ? 'online' : 'offline'),
          )}
        >
          {host.reachable ? 'online' : 'offline'}
        </span>

        <span className="text-sm text-nord-4 ml-auto flex-shrink-0">
          {host.models.length} model{host.models.length === 1 ? '' : 's'} · {formatBytes(host.total_size_bytes)}
        </span>

        {diskPercent != null && (
          <span className="hidden md:flex items-center gap-2 flex-shrink-0" title={host.error ?? undefined}>
            <span className="w-40 h-2 bg-nord-2 rounded-full overflow-hidden">
              <span
                className={cn('h-full block rounded-full', getMemoryColor(diskPercent))}
                style={{ width: `${Math.min(diskPercent, 100)}%` }}
              />
            </span>
            <span className="text-xs text-nord-4">
              {host.disk_used_gb?.toFixed(1)} / {host.disk_total_gb?.toFixed(1)} GB ·{' '}
              {host.disk_available_gb?.toFixed(1)} free
            </span>
          </span>
        )}
      </div>

      {host.reachable && expanded && (
        <div className="border border-t-0 border-nord-3 rounded-b overflow-x-auto bg-nord-1">
          {host.models.length === 0 ? (
            <div className="py-8 text-sm text-nord-4 text-center">No models stored on this host.</div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-nord-3 text-left text-xs text-nord-4">
                  <th className="px-4 py-3 w-8"></th>
                  {SORT_HEADERS.map((h) => (
                    <SortHeader
                      key={h.key}
                      label={h.label}
                      sortKey={h.key}
                      sort={sort}
                      onSort={handleSort}
                      className={h.className}
                    />
                  ))}
                  <th className="px-4 py-3 font-medium uppercase tracking-wide">Origin</th>
                  <th className="px-4 py-3 font-medium uppercase tracking-wide">Version</th>
                  <th className="px-4 py-3 font-medium uppercase tracking-wide">Used by</th>
                  <th className="px-4 py-3 font-medium uppercase tracking-wide">Path</th>
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody>
                {sortedModels.map((model) => {
                  const inUse = isModelInUse(model);
                  const key = selectionKey(host.host_id, model.slug);
                  const usedByTitle =
                    model.in_use_by.length > 0
                      ? `In use by ${model.in_use_by.map((r) => r.alias).join(', ')}`
                      : undefined;
                  return (
                    <Fragment key={model.slug}>
                      <tr className="border-b border-nord-3 hover:bg-nord-2/50">
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
                          <div className="font-medium text-nord-6 break-all">{model.model_name ?? model.slug}</div>
                          {model.model_name && model.model_name !== model.slug && (
                            <div className="text-xs text-nord-4 break-all">{model.slug}</div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-nord-6">
                          {formatBytes(model.size_bytes)}
                        </td>
                        <td className="px-4 py-3 text-nord-4" title={formatDateTime(model.downloaded_at ?? undefined)}>
                          {model.downloaded_at ? formatRelativeTime(model.downloaded_at) : '—'}
                        </td>
                        <td className="px-4 py-3">
                          <OriginBadge model={model} />
                        </td>
                        <td className="px-4 py-3 text-nord-4">{model.version ?? '—'}</td>
                        <td className="px-4 py-3">
                          <UsedByChips model={model} />
                        </td>
                        <td className="px-4 py-3">
                          <span
                            className="font-mono text-xs text-nord-4 truncate max-w-[16rem] block"
                            title={model.path}
                          >
                            {model.path}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            onClick={() => onDeleteOne(host.host_id, model.slug)}
                            disabled={inUse}
                            className="text-nord-11 hover:bg-nord-11/15 rounded p-1.5 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                            title={
                              inUse
                                ? `In use by ${model.in_use_by.map((r) => r.alias).join(', ')}`
                                : 'Delete this model'
                            }
                          >
                            <Trash2 size={16} />
                          </button>
                        </td>
                      </tr>
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

export function StorageByHostView({ hosts, selected, onToggle, onToggleAll, onDeleteOne }: StorageByHostViewProps) {
  return (
    <div className="space-y-3">
      {hosts.map((host) => (
        <HostPanel
          key={host.host_id}
          host={host}
          selected={selected}
          onToggle={onToggle}
          onToggleAll={onToggleAll}
          onDeleteOne={onDeleteOne}
        />
      ))}
    </div>
  );
}
