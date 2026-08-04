import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, AlertTriangle, Gauge, RefreshCw } from 'lucide-react';
import solarClient from '@/api/client';
import { AggregatedResourceResponse } from '@/api/types';
import { useRoutingEventsContext } from '@/context/RoutingEventsContext';
import { cn, formatRelativeTime } from '@/lib/utils';
import { HostResourceCard } from './HostResourceCard';

const fmtGb = (value: number | null | undefined): string => (value == null ? '—' : `${value.toFixed(1)} GB`);

const toGbParam = (s: string): number | undefined =>
  s.trim() !== '' && Number.isFinite(Number(s)) ? Number(s) : undefined;

function StatCard({
  label,
  value,
  sub,
  valueClass,
  title,
}: {
  label: string;
  value: string;
  sub?: React.ReactNode;
  valueClass?: string;
  title?: string;
}) {
  return (
    <div className="bg-nord-1 border border-nord-3 rounded p-4" title={title}>
      <div className="text-nord-4 text-sm">{label}</div>
      <div className={cn('text-nord-6 text-2xl font-semibold mt-0.5', valueClass)}>{value}</div>
      {sub && <div className="text-nord-4 text-xs mt-1">{sub}</div>}
    </div>
  );
}

interface SummaryTotals {
  vram: { total: number; hosts: number };
  ram: { total: number; hosts: number };
  disk: { total: number; hosts: number };
  vramTraining: { total: number; hosts: number };
  maxFreeVramTraining: number | null;
  freeVramByGpuType: Record<string, number>;
}

const EMPTY_SUMMARY: SummaryTotals = {
  vram: { total: 0, hosts: 0 },
  ram: { total: 0, hosts: 0 },
  disk: { total: 0, hosts: 0 },
  vramTraining: { total: 0, hosts: 0 },
  maxFreeVramTraining: null,
  freeVramByGpuType: {},
};

export function ResourcesPage() {
  const [data, setData] = useState<AggregatedResourceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(true);
  const [roleFilter, setRoleFilter] = useState('all'); // all | training | inference
  const [gpuTypeFilter, setGpuTypeFilter] = useState('all'); // all | nvidia_cuda | apple_mps | cpu
  const [minVram, setMinVram] = useState(''); // GB threshold input, '' = none
  const [minRam, setMinRam] = useState(''); // GB threshold input, '' = none
  const [appliedMinVram, setAppliedMinVram] = useState(''); // committed on Enter/blur
  const [appliedMinRam, setAppliedMinRam] = useState(''); // committed on Enter/blur

  const { hostStatuses, hostInstances, routingConnected } = useRoutingEventsContext();

  const fetchResources = useCallback(async () => {
    setLoading(true);
    try {
      const res = await solarClient.getResources({
        role: roleFilter !== 'all' ? roleFilter : undefined,
        gpu_type: gpuTypeFilter !== 'all' ? gpuTypeFilter : undefined,
        min_available_vram_gb: toGbParam(appliedMinVram),
        min_available_ram_gb: toGbParam(appliedMinRam),
      });
      setData(res);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch resources');
    } finally {
      setLoading(false);
    }
  }, [roleFilter, gpuTypeFilter, appliedMinVram, appliedMinRam]);

  // Initial load + refetch when committed filters change
  useEffect(() => {
    fetchResources();
  }, [fetchResources]);

  // Event-driven refresh: refetch (debounced) only when the live stream signature changes
  const streamSignature = useMemo(() => {
    const hostSig = Array.from(hostStatuses.values())
      .map((h) => `${h.host_id}:${h.status}:${h.memory?.used_gb ?? ''}:${h.memory_available_gb ?? ''}`)
      .sort()
      .join('|');
    const instSig = Array.from(hostInstances.entries())
      .map(([id, insts]) => `${id}:${insts.length}`)
      .sort()
      .join('|');
    return `${hostSig}::${instSig}`;
  }, [hostStatuses, hostInstances]);

  useEffect(() => {
    if (!live) return;
    const t = window.setTimeout(() => fetchResources(), 1500);
    return () => window.clearTimeout(t);
  }, [streamSignature, live, fetchResources]);

  // Periodic refresh fallback (tighter than GatewayDashboard — resource data moves with workloads)
  useEffect(() => {
    if (!live) return;
    const interval = routingConnected ? 60000 : 20000;
    const id = window.setInterval(() => fetchResources(), interval);
    return () => window.clearInterval(id);
  }, [live, routingConnected, fetchResources]);

  // Summary: group API-provided available values over reachable snapshots — never re-derive
  const summary: SummaryTotals = useMemo(() => {
    if (!data) return EMPTY_SUMMARY;
    const reachable = data.hosts.filter((h) => h.reachable);
    const sum = (dim: 'vram' | 'ram' | 'disk') => {
      let total = 0;
      let hosts = 0;
      for (const h of reachable) {
        const v = h[`${dim}_available_gb`];
        if (v != null) {
          total += v;
          hosts += 1;
        }
      }
      return { total, hosts };
    };
    const trainingHosts = reachable.filter((h) => h.roles.includes('training'));
    const vramTraining = { total: 0, hosts: 0 };
    let maxFreeVramTraining: number | null = null;
    const freeVramByGpuType: Record<string, number> = {};
    for (const h of trainingHosts) {
      const v = h.vram_available_gb;
      if (v == null) continue;
      vramTraining.total += v;
      vramTraining.hosts += 1;
      if (maxFreeVramTraining == null || v > maxFreeVramTraining) maxFreeVramTraining = v;
      const gpu = h.gpu_type ?? 'unknown';
      freeVramByGpuType[gpu] = (freeVramByGpuType[gpu] ?? 0) + v;
    }
    return {
      vram: sum('vram'),
      ram: sum('ram'),
      disk: sum('disk'),
      vramTraining,
      maxFreeVramTraining,
      freeVramByGpuType,
    };
  }, [data]);

  const lastUpdated = useMemo(() => {
    let max: string | null = null;
    for (const h of data?.hosts ?? []) {
      if (h.snapshot_timestamp && (max == null || h.snapshot_timestamp > max)) max = h.snapshot_timestamp;
    }
    return max ?? undefined;
  }, [data]);

  const gpuBreakdown = Object.entries(summary.freeVramByGpuType)
    .map(([gpu, v]) => `${gpu}: ${v.toFixed(1)} GB`)
    .join(' · ');

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
    <div className="max-w-7xl mx-auto p-6 space-y-8">
      {/* Header: title + filters + Auto/Manual + Refresh + updated-at */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <Gauge className="text-nord-8" />
          <h1 className="text-2xl font-semibold text-nord-6">Resources</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={roleFilter}
            onChange={(e) => setRoleFilter(e.target.value)}
            className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            title="Filter hosts by role"
          >
            <option value="all">All roles</option>
            <option value="training">Training</option>
            <option value="inference">Inference</option>
          </select>
          <select
            value={gpuTypeFilter}
            onChange={(e) => setGpuTypeFilter(e.target.value)}
            className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1"
            title="Filter hosts by GPU type"
          >
            <option value="all">All GPU types</option>
            <option value="nvidia_cuda">NVIDIA CUDA</option>
            <option value="apple_mps">Apple MPS</option>
            <option value="cpu">CPU</option>
          </select>
          <label
            className="flex items-center gap-1 text-xs text-nord-4"
            title="Only show hosts with at least this much free VRAM"
          >
            Min free VRAM
            <input
              type="number"
              min={0}
              value={minVram}
              onChange={(e) => setMinVram(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') setAppliedMinVram(minVram);
              }}
              onBlur={() => setAppliedMinVram(minVram)}
              placeholder="GB"
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1 w-20"
            />
          </label>
          <label
            className="flex items-center gap-1 text-xs text-nord-4"
            title="Only show hosts with at least this much free RAM"
          >
            Min free RAM
            <input
              type="number"
              min={0}
              value={minRam}
              onChange={(e) => setMinRam(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') setAppliedMinRam(minRam);
              }}
              onBlur={() => setAppliedMinRam(minRam)}
              placeholder="GB"
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded px-2 py-1 w-20"
            />
          </label>
          <button
            onClick={() => setLive((v) => !v)}
            className={`px-3 py-2 rounded ${live ? 'bg-nord-10 text-nord-6' : 'bg-nord-3 text-nord-6 hover:bg-nord-2'}`}
            title={live ? 'Auto-refresh enabled' : 'Enable auto-refresh'}
          >
            {live ? 'Auto' : 'Manual'}
          </button>
          <button
            onClick={() => fetchResources()}
            className="px-3 py-2 bg-nord-3 text-nord-6 rounded hover:bg-nord-2 flex items-center gap-2"
            title="Refresh now"
          >
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
          <span className="text-xs text-nord-4">Updated {formatRelativeTime(lastUpdated)}</span>
        </div>
      </div>

      {error && (
        <div className="bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg p-4 flex items-center gap-2 text-nord-6">
          <AlertCircle size={18} className="shrink-0 text-nord-11" />
          <span className="text-sm">{error}</span>
        </div>
      )}

      {data && data.unreachable_hosts > 0 && (
        <div className="flex items-center gap-2 text-sm text-nord-12">
          <AlertTriangle size={16} className="shrink-0" />
          <span>
            {data.unreachable_hosts} host{data.unreachable_hosts === 1 ? '' : 's'} unreachable — excluded from totals
          </span>
        </div>
      )}

      {data && data.hosts.length === 0 ? (
        <div className="text-center py-16 text-nord-4">
          <p className="text-lg">No hosts match the current filters</p>
          <p className="text-sm mt-2">
            Adjust the filters above, or check{' '}
            <Link to="/hosts" className="text-nord-8 hover:underline">
              Hosts &amp; Instances
            </Link>{' '}
            to see configured hosts.
          </p>
        </div>
      ) : (
        <>
          {/* Summary strip */}
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
            <StatCard
              label="Hosts"
              value={`${data?.reachable_hosts ?? 0} / ${data?.total_hosts ?? 0}`}
              sub={
                (data?.unreachable_hosts ?? 0) > 0 ? (
                  <span className="text-nord-11">{data?.unreachable_hosts} unreachable</span>
                ) : (
                  'reachable / total'
                )
              }
            />
            <StatCard
              label="Free VRAM"
              value={summary.vram.hosts > 0 ? fmtGb(summary.vram.total) : '—'}
              sub={
                summary.vram.hosts > 0
                  ? `across ${summary.vram.hosts} host${summary.vram.hosts === 1 ? '' : 's'}`
                  : 'no host reports VRAM'
              }
              title="Sum of available VRAM over reachable hosts in current filter"
            />
            <StatCard
              label="Free VRAM (training)"
              value={summary.vramTraining.hosts > 0 ? fmtGb(summary.vramTraining.total) : '—'}
              sub={
                summary.vramTraining.hosts > 0
                  ? `across ${summary.vramTraining.hosts} training host${summary.vramTraining.hosts === 1 ? '' : 's'}`
                  : 'no training host reports VRAM'
              }
              title="Sum of available VRAM over reachable training-capable hosts"
            />
            <StatCard
              label="Free RAM"
              value={summary.ram.hosts > 0 ? fmtGb(summary.ram.total) : '—'}
              sub={
                summary.ram.hosts > 0
                  ? `across ${summary.ram.hosts} host${summary.ram.hosts === 1 ? '' : 's'}`
                  : 'no host reports RAM'
              }
            />
            <StatCard
              label="Free Disk"
              value={summary.disk.hosts > 0 ? fmtGb(summary.disk.total) : '—'}
              sub={
                summary.disk.hosts > 0
                  ? `across ${summary.disk.hosts} host${summary.disk.hosts === 1 ? '' : 's'}`
                  : 'no host reports disk'
              }
            />
            <StatCard
              label="Fits? (training)"
              value={summary.maxFreeVramTraining != null ? fmtGb(summary.maxFreeVramTraining) : '—'}
              sub={gpuBreakdown || 'no training host reports VRAM'}
              title="Max free VRAM on a single training-capable host — can we fit a training job?"
            />
          </div>

          {/* Host cards */}
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
            {data?.hosts.map((h) => (
              <HostResourceCard key={h.host_id} snapshot={h} />
            ))}
          </div>

          {/* Legend + semantics footnote */}
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-nord-4">
              <span className="flex items-center gap-1.5">
                <span className="inline-block w-3.5 h-2.5 rounded-sm bg-nord-10" />
                Inference
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block w-3.5 h-2.5 rounded-sm bg-nord-15" />
                Training (job steps)
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block w-3.5 h-2.5 rounded-sm bg-nord-13" />
                Reserved (pending)
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block w-3.5 h-2.5 rounded-sm bg-nord-2" />
                Free (available)
              </span>
            </div>
            <p className="text-xs text-nord-4">available = total − Σeffective · effective = max(actual, requested)</p>
          </div>
        </>
      )}
    </div>
  );
}
