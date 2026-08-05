/**
 * StoragePage — per-host and per-model view of downloaded models with
 * guarded bulk deletion.
 *
 * One endpoint (GET /api/storage/hosts) feeds both tabs, so selection
 * state is shared and there is no second source of truth. Deletion is
 * gated client-side by in_use_by (advisory, drives the disabled
 * checkboxes) and authoritatively by the host's own 409.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  AlertCircle,
  Boxes,
  ChevronLeft,
  ChevronRight,
  HardDrive,
  RefreshCw,
  Search,
  Server,
  TriangleAlert,
} from 'lucide-react';
import solarClient from '@/api/client';
import { HostStorage, StorageDeleteItem, StorageResponse } from '@/api/types';
import { cn, formatBytes } from '@/lib/utils';
import {
  applyStorageFilters,
  DEFAULT_STORAGE_FILTERS,
  distinctModelCount,
  groupByModel,
  groupSizeBytes,
  isModelInUse,
  MIN_SIZE_OPTIONS,
  ModelGroup,
  originLabel,
  reachableHostCount,
  reclaimableBytes,
  selectionKey,
  storageSearchMatches,
  StorageFilters,
  StoredModelCopy,
  totalStoredBytes,
} from '@/lib/storage';
import { StorageByHostView } from './StorageByHostView';
import { StorageByModelView } from './StorageByModelView';
import { StorageDeleteModal } from './StorageDeleteModal';
import { StorageSelectionBar } from './StorageSelectionBar';

type ViewMode = 'host' | 'model';

const VIEW_KEY = 'solar-storage-view';

function selectionParts(key: string): { hostId: string; slug: string } {
  const idx = key.indexOf('::');
  return { hostId: key.slice(0, idx), slug: key.slice(idx + 2) };
}

export function StoragePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [data, setData] = useState<StorageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [view, setView] = useState<ViewMode>(() => {
    const qs = searchParams.get('view');
    if (qs === 'host' || qs === 'model') return qs;
    try {
      const raw = localStorage.getItem(VIEW_KEY);
      if (raw === 'host' || raw === 'model') return raw;
    } catch {
      /* ignore */
    }
    return 'host';
  });
  const [filters, setFilters] = useState<StorageFilters>(DEFAULT_STORAGE_FILTERS);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [prunedNote, setPrunedNote] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [deleteItems, setDeleteItems] = useState<StorageDeleteItem[] | null>(null);
  const requestSeq = useRef(0);

  const fetchStorage = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const res = await solarClient.getStorage();
      if (seq !== requestSeq.current) return; // stale response guard
      setData(res);

      // Prune selection: keys that vanished or became in use are dropped.
      const valid = new Set<string>();
      for (const host of res.hosts) {
        for (const model of host.models) {
          if (!isModelInUse(model)) valid.add(selectionKey(host.host_id, model.slug));
        }
      }
      setSelected((prev) => {
        const pruned = [...prev].filter((k) => !valid.has(k));
        if (pruned.length > 0) {
          setPrunedNote(
            `${pruned.length} selected model${pruned.length === 1 ? '' : 's'} ${
              pruned.length === 1 ? 'is' : 'are'
            } now in use and were deselected.`,
          );
        } else {
          setPrunedNote(null);
        }
        return new Set([...prev].filter((k) => valid.has(k)));
      });
    } catch (err: any) {
      if (seq !== requestSeq.current) return;
      setData(null);
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Failed to load storage data');
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStorage();
  }, [fetchStorage]);

  // Debounce the search box; a new search resets to the first page.
  useEffect(() => {
    const id = window.setTimeout(() => {
      setDebouncedSearch(search);
      setPage(0);
    }, 300);
    return () => window.clearTimeout(id);
  }, [search]);

  const handleViewChange = (mode: ViewMode) => {
    setView(mode);
    setPage(0);
    try {
      localStorage.setItem(VIEW_KEY, mode);
    } catch {
      /* ignore */
    }
    setSearchParams(mode === 'model' ? { view: 'model' } : {}, { replace: true });
  };

  // ── Derived data ──

  const filteredHosts = useMemo(() => {
    if (!data) return [];
    let hosts = applyStorageFilters(data.hosts, filters);
    if (debouncedSearch) hosts = hosts.filter((h) => storageSearchMatches(h, debouncedSearch));
    return hosts;
  }, [data, filters, debouncedSearch]);

  const sortedHosts = useMemo(
    () => [...filteredHosts].sort((a, b) => b.total_size_bytes - a.total_size_bytes),
    [filteredHosts],
  );

  const groups = useMemo(() => {
    const g = groupByModel(filteredHosts);
    g.sort((a, b) => groupSizeBytes(b) - groupSizeBytes(a));
    return g;
  }, [filteredHosts]);

  const outer: Array<HostStorage | ModelGroup> = view === 'host' ? sortedHosts : groups;
  const pageCount = Math.max(1, Math.ceil(outer.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const slice = outer.slice(currentPage * pageSize, currentPage * pageSize + pageSize);

  /** Visible model copies after filters + search (the "Showing N of M" count). */
  const visibleModelCount = useMemo(() => filteredHosts.reduce((s, h) => s + h.models.length, 0), [filteredHosts]);

  const totalModels = useMemo(() => (data ? data.hosts.reduce((s, h) => s + h.models.length, 0) : 0), [data]);
  const reclaimable = useMemo(() => (data ? reclaimableBytes(data.hosts) : 0), [data]);
  const unusedModelCount = useMemo(
    () => (data ? data.hosts.reduce((s, h) => s + h.models.filter((m) => !isModelInUse(m)).length, 0) : 0),
    [data],
  );
  const hostTotal = data?.hosts.length ?? 0;
  const hostReachable = data ? reachableHostCount(data.hosts) : 0;

  const filtersActive = filters.usage !== 'all' || filters.origin !== 'all' || filters.minSizeBytes > 0;

  // ── Selection ──

  const toggleSelection = useCallback((key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const toggleAll = useCallback((keys: string[]) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const allSelected = keys.length > 0 && keys.every((k) => next.has(k));
      if (allSelected) for (const k of keys) next.delete(k);
      else for (const k of keys) next.add(k);
      return next;
    });
  }, []);

  const handleSelectUnused = useCallback(() => {
    const keys: string[] = [];
    for (const host of filteredHosts) {
      for (const model of host.models) {
        if (!isModelInUse(model)) keys.push(selectionKey(host.host_id, model.slug));
      }
    }
    setSelected(new Set(keys));
  }, [filteredHosts]);

  const clearSelection = useCallback(() => {
    setSelected(new Set());
    setPrunedNote(null);
  }, []);

  const selectionStats = useMemo(() => {
    const hostIds = new Set<string>();
    let freed = 0;
    for (const key of selected) {
      const { hostId, slug } = selectionParts(key);
      hostIds.add(hostId);
      const host = data?.hosts.find((h) => h.host_id === hostId);
      const model = host?.models.find((m) => m.slug === slug);
      if (model && !isModelInUse(model)) freed += model.size_bytes;
    }
    return { modelCount: selected.size, hostCount: hostIds.size, freed };
  }, [selected, data]);

  // ── Delete flows ──

  const openDeleteItems = useCallback((items: StorageDeleteItem[]) => {
    setDeleteItems(items);
  }, []);

  const handleDeleteOne = useCallback(
    (hostId: string, slug: string) => openDeleteItems([{ host_id: hostId, slug }]),
    [openDeleteItems],
  );

  const handleDeleteCopies = useCallback(
    (copies: StoredModelCopy[]) => openDeleteItems(copies.map((c) => ({ host_id: c.hostId, slug: c.model.slug }))),
    [openDeleteItems],
  );

  const handleDeleteSelected = useCallback(() => {
    const items: StorageDeleteItem[] = [];
    for (const key of selected) {
      const { hostId, slug } = selectionParts(key);
      const host = data?.hosts.find((h) => h.host_id === hostId);
      const model = host?.models.find((m) => m.slug === slug);
      if (model && !isModelInUse(model)) items.push({ host_id: hostId, slug });
    }
    if (items.length > 0) openDeleteItems(items);
  }, [selected, data, openDeleteItems]);

  const handleDeleteDone = useCallback(() => {
    clearSelection();
    fetchStorage();
  }, [clearSelection, fetchStorage]);

  // ── Render ──

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-nord-9 mx-auto mb-4"></div>
          <p className="text-nord-4">Loading…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-nord-0">
      {/* Header */}
      <header className="bg-nord-1 shadow-lg">
        <div className="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-nord-6">Storage</h1>
              <p className="text-sm text-nord-4 mt-1">
                Downloaded models on each host. Review what is taking space and remove what is not in use.
              </p>
            </div>
            <div className="flex gap-2 items-center">
              {/* Search */}
              <div className="relative">
                <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-nord-4" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search models or hosts…"
                  className="bg-nord-1 text-nord-6 border border-nord-3 rounded pl-9 pr-3 py-2 w-64 focus:border-nord-8 outline-none"
                />
              </div>
              {/* View toggle */}
              <div className="flex bg-nord-3 rounded-lg p-0.5">
                <button
                  onClick={() => handleViewChange('host')}
                  className={cn(
                    'flex items-center gap-1 px-3 py-1.5 rounded-md text-sm transition-colors',
                    view === 'host' ? 'bg-nord-10 text-nord-6' : 'text-nord-4 hover:text-nord-6',
                  )}
                  title="By host"
                >
                  <Server size={16} /> By host
                </button>
                <button
                  onClick={() => handleViewChange('model')}
                  className={cn(
                    'flex items-center gap-1 px-3 py-1.5 rounded-md text-sm transition-colors',
                    view === 'model' ? 'bg-nord-10 text-nord-6' : 'text-nord-4 hover:text-nord-6',
                  )}
                  title="By model"
                >
                  <Boxes size={16} /> By model
                </button>
              </div>
              {/* Refresh */}
              <button
                onClick={fetchStorage}
                disabled={loading}
                className="bg-nord-3 text-nord-6 rounded-lg px-4 py-2 hover:bg-nord-2 transition-colors disabled:opacity-50"
              >
                <RefreshCw size={18} className={loading ? 'animate-spin' : ''} />
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {error ? (
          <div className="mb-6 p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div>
              <h3 className="font-semibold text-nord-6">Storage unavailable</h3>
              <p className="text-sm text-nord-4">{error}</p>
              <button
                onClick={fetchStorage}
                className="mt-2 text-sm text-nord-8 hover:text-nord-6 flex items-center gap-1"
              >
                <RefreshCw size={14} /> Retry
              </button>
            </div>
          </div>
        ) : (
          data && (
            <>
              {/* Summary strip */}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                <div className="bg-nord-1 border border-nord-3 rounded p-4">
                  <div className="text-xs text-nord-4 uppercase tracking-wide">Models</div>
                  <div className="text-2xl font-semibold text-nord-6">{distinctModelCount(data.hosts)}</div>
                  <div className="text-xs text-nord-4 mt-1">
                    {totalModels} cop{totalModels === 1 ? 'y' : 'ies'} across {hostTotal} host
                    {hostTotal === 1 ? '' : 's'}
                  </div>
                </div>
                <div className="bg-nord-1 border border-nord-3 rounded p-4">
                  <div className="text-xs text-nord-4 uppercase tracking-wide">On disk</div>
                  <div className="text-2xl font-semibold text-nord-6">{formatBytes(totalStoredBytes(data.hosts))}</div>
                  <div className="text-xs text-nord-4 mt-1">
                    largest:{' '}
                    {(() => {
                      const all = data.hosts.flatMap((h) => h.models);
                      const largest = all.reduce<{ name: string; size: number } | null>(
                        (acc, m) =>
                          !acc || m.size_bytes > acc.size ? { name: m.model_name ?? m.slug, size: m.size_bytes } : acc,
                        null,
                      );
                      return largest ? `${largest.name} (${formatBytes(largest.size)})` : '—';
                    })()}
                  </div>
                </div>
                <div className="bg-nord-1 border border-nord-3 rounded p-4">
                  <div className="text-xs text-nord-4 uppercase tracking-wide">Reclaimable</div>
                  <div className="text-2xl font-semibold text-nord-14">{formatBytes(reclaimable)}</div>
                  <div className="text-xs text-nord-4 mt-1">
                    {unusedModelCount} model{unusedModelCount === 1 ? '' : 's'} not in use ·{' '}
                    <button onClick={handleSelectUnused} className="text-nord-8 hover:text-nord-6 transition-colors">
                      Select all unused
                    </button>
                  </div>
                </div>
                <div className="bg-nord-1 border border-nord-3 rounded p-4">
                  <div className="text-xs text-nord-4 uppercase tracking-wide">Hosts</div>
                  <div className="text-2xl font-semibold text-nord-6">
                    {hostReachable} of {hostTotal} reachable
                  </div>
                  {data.unreachable_hosts.length > 0 ? (
                    <div className="text-xs text-nord-13 mt-1">
                      {data.unreachable_hosts.length} unreachable: {data.unreachable_hosts.join(', ')}
                    </div>
                  ) : (
                    <div className="text-xs text-nord-4 mt-1">all hosts answering</div>
                  )}
                </div>
              </div>

              {/* Filter row */}
              <div className="flex flex-wrap items-center gap-2 mb-4">
                {(
                  [
                    { key: 'all', label: 'All' },
                    { key: 'unused', label: 'Not in use' },
                    { key: 'in_use', label: 'In use' },
                  ] as Array<{ key: StorageFilters['usage']; label: string }>
                ).map((opt) => (
                  <button
                    key={opt.key}
                    onClick={() => setFilters((f) => ({ ...f, usage: opt.key }))}
                    className={cn(
                      'rounded-full border px-3 py-1 text-xs transition-colors',
                      filters.usage === opt.key
                        ? 'border-nord-10 bg-nord-10/20 text-nord-6'
                        : 'border-nord-3 text-nord-4 hover:text-nord-6',
                    )}
                  >
                    {opt.label}
                  </button>
                ))}

                <select
                  value={filters.origin}
                  onChange={(e) => setFilters((f) => ({ ...f, origin: e.target.value as StorageFilters['origin'] }))}
                  className="bg-nord-2 border-nord-3 rounded px-2 py-1 text-sm text-nord-6"
                >
                  <option value="all">All origins</option>
                  <option value="repository">{originLabel('repository')}</option>
                  <option value="huggingface">{originLabel('huggingface')}</option>
                  <option value="local">{originLabel('local')}</option>
                </select>

                <select
                  value={filters.minSizeBytes}
                  onChange={(e) => setFilters((f) => ({ ...f, minSizeBytes: Number(e.target.value) }))}
                  className="bg-nord-2 border-nord-3 rounded px-2 py-1 text-sm text-nord-6"
                >
                  {MIN_SIZE_OPTIONS.map((opt) => (
                    <option key={opt.bytes} value={opt.bytes}>
                      {opt.label}
                    </option>
                  ))}
                </select>

                <span className="ml-auto text-xs text-nord-4">
                  Showing {visibleModelCount} of {totalModels} model{totalModels === 1 ? '' : 's'}
                  {filtersActive && (
                    <button
                      onClick={() => setFilters(DEFAULT_STORAGE_FILTERS)}
                      className="ml-2 text-nord-8 hover:text-nord-6 transition-colors"
                    >
                      Clear filters
                    </button>
                  )}
                </span>
              </div>

              {/* Degraded banner */}
              {data.unreachable_hosts.length > 0 && (
                <div className="bg-nord-13 bg-opacity-15 border border-nord-13 rounded p-3 mb-6 flex items-start gap-2">
                  <TriangleAlert size={16} className="text-nord-13 flex-shrink-0 mt-0.5" />
                  <span className="text-sm text-nord-6">
                    Some hosts could not be reached. Their stored models are not listed and the totals above are
                    partial.
                  </span>
                </div>
              )}

              {/* Content / empty states */}
              {data.hosts.length === 0 ? (
                <div className="text-center py-16">
                  <Server size={64} className="mx-auto text-nord-3 mb-4" />
                  <h2 className="text-2xl font-semibold text-nord-6 mb-2">No hosts connected</h2>
                  <p className="text-nord-4 mb-6">Storage is collected from each connected host.</p>
                  <Link
                    to="/hosts"
                    className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
                  >
                    <Server size={20} /> Go to Hosts
                  </Link>
                </div>
              ) : totalModels === 0 && !debouncedSearch ? (
                <div className="text-center py-16">
                  <HardDrive size={64} className="mx-auto text-nord-3 mb-4" />
                  <h2 className="text-2xl font-semibold text-nord-6 mb-2">No models are stored yet</h2>
                  <p className="text-nord-4 mb-6">Deploy a model from the catalog to download it onto a host.</p>
                  <Link
                    to="/catalog"
                    className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
                  >
                    <HardDrive size={20} /> Go to Catalog
                  </Link>
                </div>
              ) : outer.length === 0 ? (
                <div className="text-center py-16">
                  <Search size={64} className="mx-auto text-nord-3 mb-4" />
                  <h2 className="text-2xl font-semibold text-nord-6 mb-2">
                    No models match &quot;{debouncedSearch || 'the current filters'}&quot;
                  </h2>
                  <button
                    onClick={() => {
                      setSearch('');
                      setFilters(DEFAULT_STORAGE_FILTERS);
                    }}
                    className="mt-2 inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
                  >
                    <Search size={20} /> Clear search and filters
                  </button>
                </div>
              ) : (
                <>
                  {view === 'host' ? (
                    <StorageByHostView
                      hosts={slice as HostStorage[]}
                      selected={selected}
                      onToggle={toggleSelection}
                      onToggleAll={toggleAll}
                      onDeleteOne={handleDeleteOne}
                    />
                  ) : (
                    <StorageByModelView
                      groups={slice as ModelGroup[]}
                      selected={selected}
                      onToggle={toggleSelection}
                      onToggleAll={toggleAll}
                      onDeleteOne={handleDeleteOne}
                      onDeleteCopies={handleDeleteCopies}
                    />
                  )}

                  {/* Pagination (outer list only) */}
                  {outer.length > pageSize && (
                    <div className="mt-4 flex items-center justify-between text-sm text-nord-4">
                      <span>
                        {outer.length === 0 ? 0 : currentPage * pageSize + 1}–
                        {Math.min((currentPage + 1) * pageSize, outer.length)} of {outer.length}
                      </span>
                      <div className="flex gap-2 items-center">
                        <span className="mr-1 text-xs">Show</span>
                        <select
                          value={pageSize}
                          onChange={(e) => {
                            setPageSize(Number(e.target.value));
                            setPage(0);
                          }}
                          className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
                        >
                          {[10, 25, 50, 100].map((n) => (
                            <option key={n} value={n}>
                              {n}
                            </option>
                          ))}
                        </select>
                        <button
                          onClick={() => setPage((p) => Math.max(0, p - 1))}
                          disabled={currentPage === 0}
                          className="px-3 py-1 rounded bg-nord-3 text-nord-6 hover:bg-nord-2 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                        >
                          <ChevronLeft size={16} /> Prev
                        </button>
                        <button
                          onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                          disabled={outer.length === 0 || currentPage >= pageCount - 1}
                          className="px-3 py-1 rounded bg-nord-3 text-nord-6 hover:bg-nord-2 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                        >
                          Next <ChevronRight size={16} />
                        </button>
                      </div>
                    </div>
                  )}
                </>
              )}
            </>
          )
        )}
      </main>

      {/* Sticky selection bar */}
      {selected.size > 0 && (
        <StorageSelectionBar
          modelCount={selectionStats.modelCount}
          hostCount={selectionStats.hostCount}
          freedBytes={selectionStats.freed}
          prunedNote={prunedNote}
          onClear={clearSelection}
          onDelete={handleDeleteSelected}
        />
      )}

      {/* Delete modal */}
      {deleteItems && (
        <StorageDeleteModal
          items={deleteItems}
          hosts={data?.hosts ?? []}
          onClose={() => setDeleteItems(null)}
          onDone={handleDeleteDone}
        />
      )}
    </div>
  );
}
