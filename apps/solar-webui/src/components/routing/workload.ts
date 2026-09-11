/**
 * Derives the routing page's view model from live topology and request state.
 *
 * Kept free of React so the load arithmetic can be tested directly -- the
 * previous graph mixed this with absolute node positioning, which is what made
 * it both untestable and prone to overlap.
 */

import {
  HostStatus,
  HostWithInstances,
  Instance,
  InstanceStatus,
  GatewayEventDTO,
  getModelCategory,
  RoutingState,
  RoutingStateAggregates,
} from '@/api/types';
import { InstanceStateData, RequestState } from '@/hooks/eventStream/useEventStream';

/** Statuses that mean a request is still occupying capacity. */
const ACTIVE_STATUSES: ReadonlySet<RequestState['status']> = new Set(['pending', 'routed', 'processing']);

export function isActiveRequest(request: RequestState): boolean {
  return ACTIVE_STATUSES.has(request.status) && !request.removing;
}

export interface InstanceCell {
  key: string;
  instanceId: string;
  hostId: string;
  hostName: string;
  hostStatus: HostStatus;
  alias: string;
  model: string;
  category: string;
  status: InstanceStatus;
  /** Requests routed here and not yet finished. */
  inFlight: number;
  state: InstanceStateData | null;
}

export function instanceAlias(instance: Instance): string {
  return instance.config?.alias || modelOf(instance) || instance.id;
}

/** Best available model identity across the backend-specific config shapes. */
export function modelOf(instance: Instance): string {
  const config = instance.config as { model?: string; model_id?: string } | undefined;
  return config?.model || config?.model_id || '';
}

export function cellKey(hostId: string, instanceId: string): string {
  return `${hostId}:${instanceId}`;
}

interface BuildOptions {
  hosts: HostWithInstances[];
  getInstanceState: (hostId: string, instanceId: string) => InstanceStateData | null | undefined;
  /** Server-computed "host:instance" -> in-flight counts, authoritative for the
   * cell load bars. */
  aggregates: RoutingStateAggregates;
  /** Substring match over instance alias, model, and host name. */
  search?: string;
  /** Drop instances that are not running. */
  runningOnly?: boolean;
}

export const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

/**
 * Every instance the current filters keep, flattened and sorted by name so the
 * picture stays stable as requests come and go -- a view that reorders itself
 * under load is unreadable.
 */
export function buildCells({
  hosts,
  getInstanceState,
  aggregates,
  search = '',
  runningOnly = false,
}: BuildOptions): InstanceCell[] {
  const inFlight = new Map(Object.entries(aggregates.by_instance));
  const terms = search.toLowerCase().split(/\s+/).filter(Boolean);
  const cells: InstanceCell[] = [];

  for (const host of hosts) {
    for (const instance of host.instances ?? []) {
      if (runningOnly && instance.status !== 'running') continue;

      const alias = instanceAlias(instance);
      const model = modelOf(instance) || alias;
      if (terms.length) {
        const haystack = `${alias} ${model} ${host.name}`.toLowerCase();
        if (!terms.every((term) => haystack.includes(term))) continue;
      }

      cells.push({
        key: cellKey(host.id, instance.id),
        instanceId: instance.id,
        hostId: host.id,
        hostName: host.name,
        hostStatus: host.status,
        alias,
        model,
        category: instance.config ? getModelCategory(instance.config) : 'generation',
        status: instance.status,
        inFlight: inFlight.get(cellKey(host.id, instance.id)) ?? 0,
        state: getInstanceState(host.id, instance.id) ?? null,
      });
    }
  }

  return cells.sort((a, b) => collator.compare(a.alias, b.alias) || collator.compare(a.hostName, b.hostName));
}

export interface FlowTotals {
  endpoints: number;
  pending: number;
  processing: number;
  errored: number;
  hostsOnline: number;
  hostsTotal: number;
  instancesRunning: number;
  instancesTotal: number;
}

/** The always-same-size header numbers, independent of fleet size. */
export function summarizeFlow(
  hosts: HostWithInstances[],
  endpointCount: number,
  aggregates: RoutingStateAggregates,
): FlowTotals {
  const { queued, processing, errored } = aggregates;
  let instancesRunning = 0;
  let instancesTotal = 0;
  for (const host of hosts) {
    for (const instance of host.instances ?? []) {
      instancesTotal += 1;
      if (instance.status === 'running') instancesRunning += 1;
    }
  }

  return {
    endpoints: endpointCount,
    pending: queued,
    processing,
    errored,
    hostsOnline: hosts.filter((h) => h.status === 'online').length,
    hostsTotal: hosts.length,
    instancesRunning,
    instancesTotal,
  };
}

/**
 * Newest first, capped: under load the request map turns over faster than
 * anyone can read, so an unbounded list is just a scroll bar. The trailing
 * terminal-history entries (from `terminalRequests`) are passed in the same
 * iterable the caller builds, so the finished-and-failed rows show next to the
 * snapshot's live active requests.
 */
export function tickerRequests(requests: Iterable<RequestState>, limit = 40): RequestState[] {
  return [...requests]
    .filter((r) => isActiveRequest(r) || r.status === 'error')
    .sort((a, b) => (b.timestamp ?? '').localeCompare(a.timestamp ?? ''))
    .slice(0, limit);
}

/**
 * Maps recent gateway terminal events onto the ticker's request shape.
 *
 * The authoritative snapshot only carries in-flight requests, so right after a
 * (re)connect the ticker would be blank until the first new event flows. Recent
 * `request_error` events backfill that terminal history so recent failures show
 * immediately. Only terminal error events are kept; active work comes from the
 * snapshot's active-request aggregates via `tickerRequests`.
 */
export function terminalRequests(events: GatewayEventDTO[], limit = 40): RequestState[] {
  const terminal: RequestState[] = [];
  for (const event of events) {
    if (event.type !== 'request_error') continue;
    const data = (event.data ?? {}) as Record<string, unknown>;
    const timestamp = (typeof data.timestamp === 'string' ? data.timestamp : event.timestamp) ?? '';
    const requestId = typeof data.request_id === 'string' ? data.request_id : undefined;
    if (!requestId) continue;
    terminal.push({
      request_id: requestId,
      model: typeof data.model === 'string' ? data.model : undefined,
      resolved_model: typeof data.resolved_model === 'string' ? data.resolved_model : undefined,
      host_id: typeof data.host_id === 'string' ? data.host_id : undefined,
      host_name: typeof data.host_name === 'string' ? data.host_name : undefined,
      instance_id: typeof data.instance_id === 'string' ? data.instance_id : undefined,
      error_message: typeof data.error_message === 'string' ? data.error_message : undefined,
      duration: typeof data.duration === 'number' ? data.duration : undefined,
      status: 'error',
      timestamp,
    });
    if (terminal.length >= limit) break;
  }
  return terminal;
}

/**
 * Maps a server routing snapshot's active requests onto the client request Map.
 *
 * The REST fallback reuses the exact same mapping the WS `routing_snapshot`
 * handler applies, so a disconnected client sees the same view it
 * would over a healthy socket. `queued` becomes `pending` (not yet routed);
 * `processing` is kept as-is.
 */
export function snapshotRequests(snapshot: RoutingState | null): Map<string, RequestState> {
  if (!snapshot) return new Map();
  return (snapshot.active_requests ?? []).reduce((acc, r) => {
    acc.set(r.request_id, {
      request_id: r.request_id,
      model: r.model ?? undefined,
      resolved_model: r.resolved_model ?? undefined,
      host_id: r.host_id ?? undefined,
      host_name: r.host_name ?? undefined,
      instance_id: r.instance_id ?? undefined,
      timestamp: r.timestamp ?? new Date().toISOString(),
      status: r.status === 'queued' ? 'pending' : 'processing',
    });
    return acc;
  }, new Map<string, RequestState>());
}

/** Maps a server routing snapshot's instance states onto the `host:instance` Map. */
export function snapshotInstanceStates(snapshot: RoutingState | null): Map<string, InstanceStateData> {
  if (!snapshot) return new Map();
  return (snapshot.instance_states ?? []).reduce((acc, s) => {
    acc.set(`${s.host_id}:${s.instance_id}`, s.data);
    return acc;
  }, new Map<string, InstanceStateData>());
}

/** Fraction of the instance's slots in use, for the cell's load bar. */
export function loadFraction(cell: InstanceCell): number {
  if (cell.status !== 'running') return 0;
  const slots = cell.state?.active_slots ?? 0;
  const load = Math.max(slots, cell.inFlight);
  if (load === 0) return cell.state?.busy ? 0.15 : 0;
  return Math.min(1, load / 4);
}

export function phaseLabel(cell: InstanceCell): string | null {
  const state = cell.state;
  if (!state || cell.status !== 'running') return null;
  if (state.phase === 'prefill' && state.prefill_progress != null) {
    return `prefill ${Math.round(state.prefill_progress * 100)}%`;
  }
  if (state.decode_tps != null) return `${state.decode_tps.toFixed(0)} tok/s`;
  if (state.phase) return state.phase;
  return state.busy ? 'busy' : null;
}
