import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Inbox,
  LayoutGrid,
  RefreshCw,
  Search,
  Table2,
  TriangleAlert,
} from 'lucide-react';
import solarClient from '@/api/client';
import { CatalogModelItem, CatalogResponse } from '@/api/types';
import { cn, formatDateTime } from '@/lib/utils';
import { ModelDetail, ModelDrawer, StatusBadge } from './ModelDetail';

type ViewMode = 'cards' | 'table';

const VIEW_MODE_KEY = 'solar-catalog-view-mode';

function readViewMode(): ViewMode {
  try {
    const raw = localStorage.getItem(VIEW_MODE_KEY);
    if (raw === 'cards' || raw === 'table') return raw;
  } catch {
    /* ignore */
  }
  return 'cards';
}

export function ModelCatalog() {
  const [data, setData] = useState<CatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [limit, setLimit] = useState(25);
  const [offset, setOffset] = useState(0);
  const [viewMode, setViewMode] = useState<ViewMode>(readViewMode);
  const [expandedName, setExpandedName] = useState<string | null>(null);
  const [detailModel, setDetailModel] = useState<CatalogModelItem | null>(null);
  const requestSeq = useRef(0);

  const fetchCatalog = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const res = await solarClient.getCatalogModels({
        search: debouncedSearch || undefined,
        limit,
        offset,
      });
      if (seq !== requestSeq.current) return; // stale response guard
      setData(res);
    } catch (err: any) {
      if (seq !== requestSeq.current) return;
      setData(null);
      // D-018 maps Data Repository down to 502 with an "unreachable" detail
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Failed to load model catalog');
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [debouncedSearch, limit, offset]);

  useEffect(() => {
    fetchCatalog();
  }, [fetchCatalog]);

  // Debounce the search input; a new search resets to the first page.
  useEffect(() => {
    const id = window.setTimeout(() => {
      setDebouncedSearch(search);
      setOffset(0);
    }, 300);
    return () => window.clearTimeout(id);
  }, [search]);

  const handleViewModeToggle = (mode: ViewMode) => {
    setViewMode(mode);
    localStorage.setItem(VIEW_MODE_KEY, mode);
  };

  const handleLimitChange = (value: number) => {
    setLimit(value);
    setOffset(0);
  };

  const toggleExpand = (name: string) => {
    setExpandedName((prev) => (prev === name ? null : name));
  };

  const rangeStart = data && data.total > 0 ? offset + 1 : 0;
  const rangeEnd = data ? Math.min(offset + limit, data.total) : 0;

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-nord-9 mx-auto mb-4"></div>
          <p className="text-nord-4">Loading...</p>
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
              <h1 className="text-3xl font-bold text-nord-6">Model Catalog</h1>
              <p className="text-sm text-nord-4 mt-1">Models in the Data Repository with Solar runtime status</p>
            </div>
            <div className="flex gap-2 items-center">
              {/* Search */}
              <div className="relative">
                <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-nord-4" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search models…"
                  className="bg-nord-1 text-nord-6 border border-nord-3 rounded pl-9 pr-3 py-2 w-64 focus:border-nord-8 outline-none"
                />
              </div>
              {/* View mode toggle */}
              <div className="flex bg-nord-3 rounded-lg p-0.5">
                <button
                  onClick={() => handleViewModeToggle('cards')}
                  className={cn(
                    'flex items-center gap-1 px-3 py-1.5 rounded-md text-sm transition-colors',
                    viewMode === 'cards' ? 'bg-nord-10 text-nord-6' : 'text-nord-4 hover:text-nord-6',
                  )}
                  title="Card view"
                >
                  <LayoutGrid size={16} />
                </button>
                <button
                  onClick={() => handleViewModeToggle('table')}
                  className={cn(
                    'flex items-center gap-1 px-3 py-1.5 rounded-md text-sm transition-colors',
                    viewMode === 'table' ? 'bg-nord-10 text-nord-6' : 'text-nord-4 hover:text-nord-6',
                  )}
                  title="Table view"
                >
                  <Table2 size={16} />
                </button>
              </div>
              <button
                onClick={fetchCatalog}
                disabled={loading}
                className="flex items-center gap-2 px-4 py-2 bg-nord-3 text-nord-6 rounded-lg hover:bg-nord-2 transition-colors disabled:opacity-50"
              >
                <RefreshCw size={18} className={loading ? 'animate-spin' : ''} />
                Refresh
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {error ? (
          // Degraded state: Data Repository unreachable (502) — never render as empty catalog.
          <div className="mb-6 p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div>
              <h3 className="font-semibold text-nord-6">Catalog unavailable</h3>
              <p className="text-sm text-nord-4">{error}</p>
              <button
                onClick={fetchCatalog}
                className="mt-2 text-sm text-nord-8 hover:text-nord-6 flex items-center gap-1"
              >
                <RefreshCw size={14} /> Retry
              </button>
            </div>
          </div>
        ) : (
          data && (
            <>
              {/* Enrichment banner: availability source degraded, never blocks the list */}
              {data.meta.enrichment !== 'ok' && (
                <div className="mb-6 p-3 bg-nord-13 bg-opacity-15 border border-nord-13 rounded text-sm text-nord-6 flex items-start gap-2">
                  <TriangleAlert size={16} className="flex-shrink-0 mt-0.5" />
                  <span>
                    {data.meta.enrichment === 'partial'
                      ? 'Availability data is partial (some hosts unreachable). Models that could not be checked show as unknown.'
                      : 'Host availability could not be checked. Models without running instances show as unknown.'}
                  </span>
                </div>
              )}

              {data.total === 0 ? (
                /* Distinct empty states: whole catalog vs. no search match */
                <div className="text-center py-16">
                  {debouncedSearch ? (
                    <>
                      <Search size={64} className="mx-auto text-nord-3 mb-4" />
                      <h2 className="text-2xl font-semibold text-nord-6 mb-2">
                        No models match &quot;{debouncedSearch}&quot;
                      </h2>
                      <p className="text-nord-4 mb-6">Try a different search term</p>
                      <button
                        onClick={() => setSearch('')}
                        className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
                      >
                        <Search size={20} /> Clear search
                      </button>
                    </>
                  ) : (
                    <>
                      <Inbox size={64} className="mx-auto text-nord-3 mb-4" />
                      <h2 className="text-2xl font-semibold text-nord-6 mb-2">The catalog is empty</h2>
                      <p className="text-nord-4">No models are registered in the Data Repository</p>
                    </>
                  )}
                </div>
              ) : viewMode === 'table' ? (
                /* Table view */
                <div className="bg-nord-1 border border-nord-3 rounded overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-nord-3 text-left text-xs text-nord-4">
                        <th className="px-4 py-3 font-medium">Status</th>
                        <th className="px-4 py-3 font-medium">Model</th>
                        <th className="px-4 py-3 font-medium">Versions</th>
                        <th className="px-4 py-3 font-medium">Latest</th>
                        <th className="px-4 py-3 font-medium">Running</th>
                        <th className="px-4 py-3 font-medium">Deployed hosts</th>
                        <th className="px-4 py-3 font-medium">Created</th>
                        <th className="px-4 py-3"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((model) => {
                        const expanded = expandedName === model.name;
                        return (
                          <Fragment key={model.name}>
                            <tr
                              onClick={() => toggleExpand(model.name)}
                              className="border-b border-nord-3 hover:bg-nord-2/50 cursor-pointer"
                            >
                              <td className="px-4 py-3">
                                <StatusBadge status={model.solar.status} />
                              </td>
                              <td className="px-4 py-3">
                                <div className="font-medium text-nord-6 break-all">{model.name}</div>
                                <div className="text-xs text-nord-4">{model.category}</div>
                              </td>
                              <td className="px-4 py-3 text-nord-6">{model.versions_count}</td>
                              <td className="px-4 py-3 text-nord-6">{model.latest_version ?? '—'}</td>
                              <td className="px-4 py-3 text-nord-6">{model.solar.running_instances}</td>
                              <td className="px-4 py-3 text-nord-6">{model.solar.deployed_hosts.length}</td>
                              <td className="px-4 py-3 text-nord-4">{formatDateTime(model.created_at)}</td>
                              <td className="px-4 py-3 text-nord-4">
                                {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                              </td>
                            </tr>
                            {expanded && (
                              <tr className="border-b border-nord-3">
                                <td colSpan={8} className="p-0">
                                  <div className="bg-nord-2/50 p-4 sm:p-6">
                                    <ModelDetail model={model} onDeleted={fetchCatalog} />
                                  </div>
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                /* Cards view */
                <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                  {data.items.map((model) => (
                    <div
                      key={model.name}
                      onClick={() => setDetailModel(model)}
                      className="bg-nord-1 border border-nord-3 rounded p-4 cursor-pointer hover:bg-nord-2/50 hover:border-nord-8/60 transition-colors"
                    >
                      <div className="flex items-center justify-between mb-2">
                        <StatusBadge status={model.solar.status} />
                        <div className="flex items-center gap-2">
                          {model.latest_version && (
                            <span className="bg-nord-2 text-nord-4 rounded px-2 py-0.5 text-xs">
                              {model.latest_version}
                            </span>
                          )}
                          <ChevronRight size={16} className="text-nord-4" />
                        </div>
                      </div>
                      <h3 className="font-medium text-nord-6 break-all">{model.name}</h3>
                      <p className="text-xs text-nord-4 mt-0.5">{model.category}</p>
                      {model.description && (
                        <p className="text-sm text-nord-4 mt-2 line-clamp-2">{model.description}</p>
                      )}
                      <p className="text-xs text-nord-4 mt-2">
                        {model.versions_count} version{model.versions_count === 1 ? '' : 's'} •{' '}
                        {model.solar.running_instances} running • {model.solar.deployed_hosts.length} host
                        {model.solar.deployed_hosts.length === 1 ? '' : 's'}
                      </p>
                    </div>
                  ))}
                </div>
              )}

              {/* Pagination + page size */}
              <div className="mt-4 flex items-center justify-between text-sm text-nord-4">
                <span>
                  {rangeStart}–{rangeEnd} of {data.total}
                </span>
                <div className="flex gap-2 items-center">
                  <span className="mr-1 text-xs">Show</span>
                  <select
                    value={limit}
                    onChange={(e) => handleLimitChange(Number(e.target.value))}
                    className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
                  >
                    {[10, 25, 50, 100].map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={() => setOffset((prev) => Math.max(0, prev - limit))}
                    disabled={offset === 0}
                    className="px-3 py-1 rounded bg-nord-3 text-nord-6 hover:bg-nord-2 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                  >
                    <ChevronLeft size={16} /> Prev
                  </button>
                  <button
                    onClick={() => setOffset((prev) => prev + limit)}
                    disabled={data.total === 0 || offset + limit >= data.total}
                    className="px-3 py-1 rounded bg-nord-3 text-nord-6 hover:bg-nord-2 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                  >
                    Next <ChevronRight size={16} />
                  </button>
                </div>
              </div>
            </>
          )
        )}
      </main>

      {/* Detail drawer — cards view opens this instead of expanding in place */}
      <ModelDrawer model={detailModel} onClose={() => setDetailModel(null)} onDeleted={fetchCatalog} />
    </div>
  );
}
