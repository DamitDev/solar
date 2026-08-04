import { useEffect, useMemo, useState, useCallback, useRef } from 'react';
import { Activity, AlertTriangle, CheckCircle2, RefreshCw, RotateCcw, TriangleAlert } from 'lucide-react';
import solarClient from '@/api/client';
import { ApiEndpoint, GatewayStats, GatewayRequestSummary } from '@/api/types';
import { useEventStreamContext } from '@/context/EventStreamContext';
import { useRoutingEventsContext } from '@/context/RoutingEventsContext';
import { formatTokenCount } from '@/lib/utils';

function isoInput(dt: Date) {
  const pad = (n: number) => String(n).padStart(2, '0');
  const yyyy = dt.getFullYear();
  const mm = pad(dt.getMonth() + 1);
  const dd = pad(dt.getDate());
  const hh = pad(dt.getHours());
  const mi = pad(dt.getMinutes());
  return `${yyyy}-${mm}-${dd}T${hh}:${mi}`;
}

// Format date as YYYY-MM-DD HH:MM:SS (24-hour)
function formatDateTime(dateStr: string): string {
  const d = new Date(dateStr);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

type TimePreset = '1h' | '12h' | '1d' | '7d' | '1m' | '1y' | 'custom';

function calculatePresetDates(preset: TimePreset): { from: string; to: string } {
  const now = new Date();
  const to = isoInput(now);
  let fromDate: Date;

  switch (preset) {
    case '1h':
      fromDate = new Date(now.getTime() - 60 * 60 * 1000);
      break;
    case '12h':
      fromDate = new Date(now.getTime() - 12 * 60 * 60 * 1000);
      break;
    case '1d':
      fromDate = new Date(now.getTime() - 24 * 60 * 60 * 1000);
      break;
    case '7d':
      fromDate = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
      break;
    case '1m':
      fromDate = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
      break;
    case '1y':
      fromDate = new Date(now.getTime() - 365 * 24 * 60 * 60 * 1000);
      break;
    case 'custom':
    default:
      const nowUtc = new Date();
      const startOfToday = new Date(
        Date.UTC(nowUtc.getUTCFullYear(), nowUtc.getUTCMonth(), nowUtc.getUTCDate(), 0, 0, 0),
      );
      return { from: isoInput(startOfToday), to };
  }

  return { from: isoInput(fromDate), to };
}

export function GatewayDashboard() {
  const { events, addRecentEvents } = useRoutingEventsContext();
  const { gatewayRequests, setFilter, clearGatewayRequests, isConnected } = useEventStreamContext();
  const countedIdsRef = useRef<Set<string>>(new Set());
  const gatewayRequestsRef = useRef(gatewayRequests);
  gatewayRequestsRef.current = gatewayRequests;

  // Time range - initialize with 1d preset
  const initialPreset: TimePreset = '1d';
  const initialDates = calculatePresetDates(initialPreset);
  const [preset, setPreset] = useState<TimePreset>(initialPreset);
  const [from, setFrom] = useState<string>(initialDates.from);
  const [to, setTo] = useState<string>(initialDates.to);

  // Stats (always fetched via REST for aggregates)
  const [stats, setStats] = useState<GatewayStats | null>(null);
  const [loadingStats, setLoadingStats] = useState(false);

  // Filters
  const [statusFilter, setStatusFilter] = useState<'all' | 'success' | 'error' | 'missed'>('all');
  const [requestTypeFilter, setRequestTypeFilter] = useState<string>('all');
  const [hostFilter, setHostFilter] = useState<string>('all');
  const [modelFilter, setModelFilter] = useState<string>('all');
  const [endpointFilter, setEndpointFilter] = useState<string>('all');

  // Endpoints list (for filter dropdown and per-endpoint cards)
  const [endpoints, setEndpoints] = useState<ApiEndpoint[]>([]);
  const [endpointStats, setEndpointStats] = useState<Record<string, GatewayStats>>({});

  // Historical requests (REST fallback when not live or for pagination)
  const [historicalRequests, setHistoricalRequests] = useState<GatewayRequestSummary[]>([]);
  const [loadingReqs, setLoadingReqs] = useState(false);
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(15);
  const [totalRequests, setTotalRequests] = useState(0);
  const [live, setLive] = useState(true);

  const fromIso = useMemo(() => new Date(from).toISOString(), [from]);
  const toIso = useMemo(() => new Date(to).toISOString(), [to]);

  // Available hosts and models from stats
  const availableHosts = useMemo(
    () => stats?.hosts?.map((h) => ({ id: h.host_id, name: h.host_name || h.host_id })) || [],
    [stats],
  );
  const availableModels = useMemo(() => stats?.models?.map((m) => m.model) || [], [stats]);
  const endpointNameById = useMemo(() => new Map(endpoints.map((ep) => [ep.id, ep.name])), [endpoints]);

  // Update dates when preset changes
  useEffect(() => {
    if (preset !== 'custom') {
      const { from: newFrom, to: newTo } = calculatePresetDates(preset);
      setFrom(newFrom);
      setTo(newTo);
    }
  }, [preset]);

  // Update WebSocket filter when local filter changes
  useEffect(() => {
    setFilter({
      status: statusFilter,
      request_type: requestTypeFilter,
      host_id: hostFilter !== 'all' ? hostFilter : null,
      model: modelFilter !== 'all' ? modelFilter : null,
      endpoint_id: endpointFilter !== 'all' ? endpointFilter : null,
    });
    clearGatewayRequests();
  }, [statusFilter, requestTypeFilter, hostFilter, modelFilter, endpointFilter, setFilter, clearGatewayRequests]);

  // Fetch endpoints on mount
  useEffect(() => {
    solarClient
      .getEndpoints()
      .then(setEndpoints)
      .catch(() => setEndpoints([]));
  }, []);

  // Fetch data functions
  const fetchStats = useCallback(async () => {
    setLoadingStats(true);
    try {
      const s = await solarClient.getGatewayStats({
        from: fromIso,
        to: toIso,
        request_type: requestTypeFilter !== 'all' ? requestTypeFilter : undefined,
        endpoint_id: endpointFilter !== 'all' ? endpointFilter : undefined,
      });
      setStats(s);
      countedIdsRef.current = new Set(gatewayRequestsRef.current.map((r) => r.request_id));
    } catch (err) {
      console.error('Failed to fetch gateway stats:', err);
    } finally {
      setLoadingStats(false);
    }
  }, [fromIso, toIso, requestTypeFilter, endpointFilter]);

  const fetchEndpointStats = useCallback(async () => {
    try {
      const baseParams = {
        from: fromIso,
        to: toIso,
        request_type: requestTypeFilter !== 'all' ? requestTypeFilter : undefined,
      };
      const results: Record<string, GatewayStats> = {};
      const allStats = await solarClient.getGatewayStats(baseParams);
      results['all'] = allStats;
      await Promise.all(
        endpoints.map(async (ep) => {
          try {
            const s = await solarClient.getGatewayStats({ ...baseParams, endpoint_id: ep.id });
            results[ep.id] = s;
          } catch {
            /* individual endpoint stat failure is non-fatal */
          }
        }),
      );
      setEndpointStats(results);
    } catch (err) {
      console.error('Failed to fetch endpoint stats:', err);
    }
  }, [fromIso, toIso, requestTypeFilter, endpoints]);

  const fetchRequests = useCallback(async () => {
    setLoadingReqs(true);
    try {
      const res = await solarClient.listGatewayRequests({
        from: fromIso,
        to: toIso,
        status: statusFilter !== 'all' ? statusFilter : undefined,
        request_type: requestTypeFilter !== 'all' ? requestTypeFilter : undefined,
        host_id: hostFilter !== 'all' ? hostFilter : undefined,
        model: modelFilter !== 'all' ? modelFilter : undefined,
        endpoint_id: endpointFilter !== 'all' ? endpointFilter : undefined,
        page,
        limit,
      });
      setHistoricalRequests(res.items);
      setTotalRequests(res.total);
    } catch (err) {
      console.error('Failed to fetch gateway requests:', err);
    } finally {
      setLoadingReqs(false);
    }
  }, [fromIso, toIso, statusFilter, requestTypeFilter, hostFilter, modelFilter, endpointFilter, page, limit]);

  const refreshAll = useCallback(() => {
    fetchStats();
    fetchRequests();
    fetchEndpointStats();
  }, [fetchStats, fetchRequests, fetchEndpointStats]);

  // Load data when filters, time range, or pagination change
  useEffect(() => {
    fetchStats();
    fetchRequests();
  }, [fetchStats, fetchRequests]);

  // Fetch per-endpoint stats when endpoints or time range change
  useEffect(() => {
    fetchEndpointStats();
  }, [fetchEndpointStats]);

  // Periodic refresh when live -- only poll when Socket.IO is not connected
  useEffect(() => {
    if (!live) return;
    const interval = isConnected ? 120000 : 30000;
    const id = window.setInterval(() => {
      if (preset !== 'custom') {
        const { from: newFrom, to: newTo } = calculatePresetDates(preset);
        setFrom(newFrom);
        setTo(newTo);
      }
    }, interval);
    return () => window.clearInterval(id);
  }, [live, preset, isConnected]);

  // Realtime stats update from Socket.IO gateway_request events
  useEffect(() => {
    if (!stats || !live) return;

    const seen = countedIdsRef.current;
    const newItems = gatewayRequests.filter((r) => !seen.has(r.request_id));
    if (newItems.length === 0) return;

    for (const r of newItems) seen.add(r.request_id);

    setStats((prev) => {
      if (!prev) return prev;
      let { completed, missed, error, rerouted_requests, token_in_total, token_out_total } = prev;
      let totalCompleted = completed + missed + error;
      for (const r of newItems) {
        if (r.status === 'success') completed++;
        else if (r.status === 'missed') missed++;
        else if (r.status === 'error') error++;
        if (r.attempts > 1) rerouted_requests++;
        token_in_total += r.prompt_tokens ?? 0;
        token_out_total += r.completion_tokens ?? 0;
      }
      totalCompleted += newItems.length;
      return {
        ...prev,
        completed,
        missed,
        error,
        rerouted_requests,
        token_in_total,
        token_out_total,
        avg_tokens_in: totalCompleted > 0 ? token_in_total / totalCompleted : 0,
        avg_tokens_out: totalCompleted > 0 ? token_out_total / totalCompleted : 0,
      };
    });
  }, [gatewayRequests, live, stats]);

  // Backfill recent events on mount
  useEffect(() => {
    const loadEvents = async () => {
      const res = await solarClient.getRecentGatewayEvents({
        from: fromIso,
        to: toIso,
        limit: 1000,
        types: 'request_error,request_reroute',
        endpoint_id: endpointFilter !== 'all' ? endpointFilter : undefined,
      });
      addRecentEvents(res.items as unknown as any);
    };
    loadEvents();
  }, [fromIso, toIso, endpointFilter, addRecentEvents]);

  // Combine requests for display
  const displayRequests = useMemo(() => {
    if (!live || page > 1) {
      return historicalRequests.slice(0, limit);
    } else {
      let wsFiltered = gatewayRequests;
      if (statusFilter !== 'all') {
        wsFiltered = wsFiltered.filter((r) => r.status === statusFilter);
      }
      if (requestTypeFilter !== 'all') {
        wsFiltered = wsFiltered.filter((r) => r.request_type === requestTypeFilter);
      }
      if (hostFilter !== 'all') {
        wsFiltered = wsFiltered.filter((r) => r.host_id === hostFilter);
      }
      if (modelFilter !== 'all') {
        wsFiltered = wsFiltered.filter((r) => (r.resolved_model || r.model) === modelFilter);
      }
      const wsIds = new Set(wsFiltered.map((r) => r.request_id));
      const filteredHistorical = historicalRequests.filter((r) => !wsIds.has(r.request_id));
      const merged = [...wsFiltered, ...filteredHistorical];
      return merged.slice(0, limit);
    }
  }, [
    live,
    page,
    gatewayRequests,
    historicalRequests,
    limit,
    statusFilter,
    requestTypeFilter,
    hostFilter,
    modelFilter,
  ]);

  return (
    <div className="max-w-7xl mx-auto p-6 space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Activity className="text-nord-8" />
          <h1 className="text-2xl font-semibold text-nord-6">Gateway Monitoring</h1>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setLive((v) => !v)}
            className={`px-3 py-2 rounded ${live ? 'bg-nord-10 text-nord-6' : 'bg-nord-3 text-nord-6 hover:bg-nord-2'}`}
            title={live ? 'Auto-refresh enabled' : 'Enable auto-refresh'}
          >
            {live ? 'Auto' : 'Manual'}
          </button>
          <div className="flex items-center gap-1">
            {(['1h', '12h', '1d', '7d', '1m', '1y', 'custom'] as TimePreset[]).map((p) => (
              <button
                key={p}
                onClick={() => setPreset(p)}
                className={`px-3 py-1 text-sm rounded ${
                  preset === p ? 'bg-nord-10 text-nord-6 font-medium' : 'bg-nord-2 text-nord-6 hover:bg-nord-3'
                }`}
              >
                {p === 'custom' ? 'Custom' : p}
              </button>
            ))}
          </div>
          {preset === 'custom' && (
            <>
              <input
                type="datetime-local"
                value={from}
                onChange={(e) => {
                  setFrom(e.target.value);
                  setPreset('custom');
                }}
                className="bg-nord-1 text-nord-6 border border-nord-3 rounded px-2 py-1"
              />
              <span className="text-nord-4">to</span>
              <input
                type="datetime-local"
                value={to}
                onChange={(e) => {
                  setTo(e.target.value);
                  setPreset('custom');
                }}
                className="bg-nord-1 text-nord-6 border border-nord-3 rounded px-2 py-1"
              />
            </>
          )}
          <select
            value={endpointFilter}
            onChange={(e) => {
              setPage(1);
              setEndpointFilter(e.target.value);
            }}
            className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            title="Filter by endpoint"
          >
            <option value="all">All Endpoints</option>
            {endpoints.map((ep) => (
              <option key={ep.id} value={ep.id}>
                {ep.name}
              </option>
            ))}
          </select>
          <button onClick={refreshAll} className="px-3 py-2 bg-nord-3 text-nord-6 rounded hover:bg-nord-2">
            <RefreshCw size={16} className={loadingStats || loadingReqs ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
        <div className="bg-nord-1 border border-nord-3 rounded p-4">
          <div className="text-nord-4 text-sm">Completed</div>
          <div className="text-nord-14 text-2xl font-semibold">{stats?.completed ?? '—'}</div>
        </div>
        <div className="bg-nord-1 border border-nord-3 rounded p-4">
          <div className="text-nord-4 text-sm">Missed</div>
          <div className="text-nord-11 text-2xl font-semibold">{stats?.missed ?? '—'}</div>
        </div>
        <div className="bg-nord-1 border border-nord-3 rounded p-4">
          <div className="text-nord-4 text-sm">Errors</div>
          <div className="text-nord-12 text-2xl font-semibold">{stats?.error ?? '—'}</div>
        </div>
        <div className="bg-nord-1 border border-nord-3 rounded p-4">
          <div className="text-nord-4 text-sm">Rerouted Requests</div>
          <div className="text-nord-13 text-2xl font-semibold">{stats?.rerouted_requests ?? '—'}</div>
        </div>
        <div className="bg-nord-1 border border-nord-3 rounded p-4">
          <div className="text-nord-4 text-sm">Input tokens</div>
          <div className="text-nord-6 text-lg">{formatTokenCount(stats?.token_in_total)}</div>
          <div className="text-nord-4 text-xs">avg {formatTokenCount(stats?.avg_tokens_in)}</div>
        </div>
        <div className="bg-nord-1 border border-nord-3 rounded p-4">
          <div className="text-nord-4 text-sm">Output tokens</div>
          <div className="text-nord-6 text-lg">{formatTokenCount(stats?.token_out_total)}</div>
          <div className="text-nord-4 text-xs">avg {formatTokenCount(stats?.avg_tokens_out)}</div>
        </div>
      </div>

      {/* Per-endpoint usage cards */}
      {endpoints.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6 gap-3">
          <div
            className={`border rounded p-3 text-sm cursor-pointer transition-colors ${
              endpointFilter === 'all' ? 'bg-nord-10/20 border-nord-10' : 'bg-nord-1 border-nord-3 hover:border-nord-4'
            }`}
            onClick={() => {
              setPage(1);
              setEndpointFilter('all');
            }}
            title="Show all endpoints"
          >
            <div className="text-nord-6 font-medium">All</div>
            <div className="text-nord-4 text-xs mt-1">
              {(() => {
                const all = endpointStats['all'];
                const totalReqs = all ? all.completed + all.missed + all.error : 0;
                const totalTokens = all ? all.token_in_total + all.token_out_total : 0;
                return `${totalReqs} reqs • ${formatTokenCount(totalTokens)} tokens`;
              })()}
            </div>
          </div>
          {endpoints.map((ep) => {
            const epStats = endpointStats[ep.id];
            const totalReqs = (epStats?.completed ?? 0) + (epStats?.missed ?? 0) + (epStats?.error ?? 0);
            const totalTokens = (epStats?.token_in_total ?? 0) + (epStats?.token_out_total ?? 0);
            const avgLatency = epStats?.models?.length
              ? epStats.models.reduce((sum, m) => sum + m.avg_duration_s * m.completed, 0) / (epStats.completed || 1)
              : null;
            const isSelected = endpointFilter === ep.id;
            return (
              <div
                key={ep.id}
                className={`border rounded p-3 text-sm cursor-pointer transition-colors ${
                  isSelected ? 'bg-nord-10/20 border-nord-10' : 'bg-nord-1 border-nord-3 hover:border-nord-4'
                }`}
                onClick={() => {
                  setPage(1);
                  setEndpointFilter(ep.id);
                }}
                title={`Filter to ${ep.name}`}
              >
                <div className="text-nord-6 font-medium truncate">{ep.name}</div>
                <div className="text-nord-4 text-xs mt-1">
                  {totalReqs} reqs • {formatTokenCount(totalTokens)} tokens
                  {avgLatency != null && ` • ${avgLatency.toFixed(2)}s avg`}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Live Events */}
      <div className="bg-nord-1 border border-nord-3 rounded">
        <div className="p-4 flex items-center justify-between border-b border-nord-3">
          <div className="text-nord-6 font-medium">Events (Errors & Reroutes)</div>
          <div className="flex items-center gap-2">
            <button
              onClick={async () => {
                const res = await solarClient.getRecentGatewayEvents({
                  from: fromIso,
                  to: toIso,
                  limit: 1000,
                  types: 'request_error,request_reroute',
                });
                addRecentEvents(res.items as unknown as any);
              }}
              className="px-3 py-2 bg-nord-3 text-nord-6 rounded hover:bg-nord-2 flex items-center gap-2"
            >
              <RotateCcw size={16} /> Load recent
            </button>
            <div className="text-sm text-nord-4">{events.length} events</div>
          </div>
        </div>
        <div className="max-h-72 overflow-auto p-3 text-sm">
          {events.length === 0 ? (
            <div className="text-nord-4">No events yet</div>
          ) : (
            events.map((e, idx) => (
              <div key={idx} className="flex items-start gap-2 py-1">
                {e.type === 'request_reroute' ? (
                  <AlertTriangle className="text-nord-13" size={16} />
                ) : (
                  <TriangleAlert className="text-nord-12" size={16} />
                )}
                <div className="text-nord-6">
                  <span className="text-nord-4">
                    [{formatDateTime((e as any).data?.timestamp || (e as any).timestamp)}]
                  </span>{' '}
                  <span className="uppercase text-xs px-2 py-0.5 rounded bg-nord-2 text-nord-4 mr-2">{e.type}</span>
                  <span>{(e as any).data?.model}</span>
                  {e.type === 'request_reroute' && (
                    <span className="text-nord-4"> → attempt {(e as any).data?.attempt}</span>
                  )}
                  {e.type === 'request_error' && (
                    <span className="text-nord-11"> {(e as any).data?.error_message}</span>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Requests table */}
      <div className="bg-nord-1 border border-nord-3 rounded">
        <div className="p-4 flex items-center justify-between border-b border-nord-3">
          <div className="text-nord-6 font-medium">Requests</div>
          <div className="flex items-center gap-2 flex-wrap">
            <select
              value={statusFilter}
              onChange={(e) => {
                setPage(1);
                setStatusFilter(e.target.value as any);
              }}
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            >
              <option value="all">All Status</option>
              <option value="success">Success</option>
              <option value="error">Error</option>
              <option value="missed">Missed</option>
            </select>
            <select
              value={requestTypeFilter}
              onChange={(e) => {
                setPage(1);
                setRequestTypeFilter(e.target.value);
              }}
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            >
              <option value="all">All Types</option>
              <option value="chat">Chat</option>
              <option value="completion">Completion</option>
              <option value="embedding">Embedding</option>
              <option value="classification">Classification</option>
              <option value="rerank">Rerank</option>
            </select>
            <select
              value={hostFilter}
              onChange={(e) => {
                setPage(1);
                setHostFilter(e.target.value);
              }}
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            >
              <option value="all">All Hosts</option>
              {availableHosts.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}
                </option>
              ))}
            </select>
            <select
              value={modelFilter}
              onChange={(e) => {
                setPage(1);
                setModelFilter(e.target.value);
              }}
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            >
              <option value="all">All Models</option>
              {availableModels.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <button onClick={fetchRequests} className="px-3 py-2 bg-nord-3 text-nord-6 rounded hover:bg-nord-2">
              <RefreshCw size={16} className={loadingReqs ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>
        <div className="overflow-auto">
          <table className="min-w-full text-sm">
            <thead className="bg-nord-2 text-nord-4">
              <tr>
                <th className="text-left px-3 py-2">Time</th>
                <th className="text-left px-3 py-2">Endpoint</th>
                <th className="text-left px-3 py-2">Type</th>
                <th className="text-left px-3 py-2">Model</th>
                <th className="text-left px-3 py-2">Status</th>
                <th className="text-left px-3 py-2">Host</th>
                <th className="text-left px-3 py-2">Input</th>
                <th className="text-left px-3 py-2">Output</th>
                <th className="text-left px-3 py-2">Duration</th>
                <th className="text-left px-3 py-2">Attempts</th>
              </tr>
            </thead>
            <tbody className="text-nord-6">
              {displayRequests.length ? (
                displayRequests.map((r: GatewayRequestSummary) => (
                  <tr key={r.request_id} className="border-t border-nord-3">
                    <td className="px-3 py-2 whitespace-nowrap">{formatDateTime(r.end_timestamp)}</td>
                    <td className="px-3 py-2">
                      <span className="text-xs px-2 py-0.5 rounded bg-nord-2 text-nord-4">
                        {r.endpoint_id ? (endpointNameById.get(r.endpoint_id) ?? r.endpoint_id) : '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className="text-xs px-2 py-0.5 rounded bg-nord-2 text-nord-4">
                        {r.request_type || 'unknown'}
                      </span>
                    </td>
                    <td className="px-3 py-2">{r.resolved_model || r.model}</td>
                    <td className="px-3 py-2">
                      {r.status === 'success' ? (
                        <span className="text-nord-14 flex items-center gap-1">
                          <CheckCircle2 size={14} /> success
                        </span>
                      ) : r.status === 'missed' ? (
                        <span className="text-nord-11">missed</span>
                      ) : (
                        <span className="text-nord-12">error</span>
                      )}
                    </td>
                    <td className="px-3 py-2">{r.host_name || r.host_id || '—'}</td>
                    <td className="px-3 py-2">{formatTokenCount(r.prompt_tokens)}</td>
                    <td className="px-3 py-2">{formatTokenCount(r.completion_tokens)}</td>
                    <td className="px-3 py-2">{r.duration_s?.toFixed(2)}s</td>
                    <td className="px-3 py-2">{r.attempts}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={10} className="px-3 py-6 text-center text-nord-4">
                    No data
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="p-3 border-t border-nord-3 flex items-center justify-between text-sm text-nord-4">
          <div>
            Page {page} • {totalRequests} total
          </div>
          <div className="flex items-center gap-2">
            <button
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="px-3 py-1 bg-nord-2 text-nord-6 rounded disabled:opacity-50"
            >
              Prev
            </button>
            <button
              disabled={displayRequests.length < limit}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1 bg-nord-2 text-nord-6 rounded disabled:opacity-50"
            >
              Next
            </button>
            <select
              value={limit}
              onChange={(e) => {
                setPage(1);
                setLimit(parseInt(e.target.value, 10));
              }}
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            >
              <option value={15}>15</option>
              <option value={25}>25</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
            </select>
          </div>
        </div>
      </div>

      {/* Breakdown tables */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-nord-1 border border-nord-3 rounded">
          <div className="p-4 border-b border-nord-3 text-nord-6 font-medium">By Model</div>
          <div className="overflow-auto">
            <table className="min-w-full text-sm">
              <thead className="bg-nord-2 text-nord-4">
                <tr>
                  <th className="text-left px-3 py-2">Model</th>
                  <th className="text-left px-3 py-2">Completed</th>
                  <th className="text-left px-3 py-2">Input tokens</th>
                  <th className="text-left px-3 py-2">Output tokens</th>
                  <th className="text-left px-3 py-2">Avg Duration</th>
                </tr>
              </thead>
              <tbody className="text-nord-6">
                {stats?.models?.length ? (
                  stats.models.map((m) => (
                    <tr key={m.model} className="border-t border-nord-3">
                      <td className="px-3 py-2">{m.model}</td>
                      <td className="px-3 py-2">{m.completed}</td>
                      <td className="px-3 py-2">{formatTokenCount(m.token_in)}</td>
                      <td className="px-3 py-2">{formatTokenCount(m.token_out)}</td>
                      <td className="px-3 py-2">{m.avg_duration_s.toFixed(2)}s</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={5} className="px-3 py-4 text-center text-nord-4">
                      No data
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
        <div className="bg-nord-1 border border-nord-3 rounded">
          <div className="p-4 border-b border-nord-3 text-nord-6 font-medium">By Host</div>
          <div className="overflow-auto">
            <table className="min-w-full text-sm">
              <thead className="bg-nord-2 text-nord-4">
                <tr>
                  <th className="text-left px-3 py-2">Host</th>
                  <th className="text-left px-3 py-2">Completed</th>
                  <th className="text-left px-3 py-2">Input tokens</th>
                  <th className="text-left px-3 py-2">Output tokens</th>
                  <th className="text-left px-3 py-2">Avg Duration</th>
                </tr>
              </thead>
              <tbody className="text-nord-6">
                {stats?.hosts?.length ? (
                  stats.hosts.map((h) => (
                    <tr key={h.host_id} className="border-t border-nord-3">
                      <td className="px-3 py-2">{h.host_name || h.host_id}</td>
                      <td className="px-3 py-2">{h.completed}</td>
                      <td className="px-3 py-2">{formatTokenCount(h.token_in)}</td>
                      <td className="px-3 py-2">{formatTokenCount(h.token_out)}</td>
                      <td className="px-3 py-2">{h.avg_duration_s.toFixed(2)}s</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={5} className="px-3 py-4 text-center text-nord-4">
                      No data
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
