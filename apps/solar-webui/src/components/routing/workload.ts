/**
 * Derives the routing page's view model from live topology and request state.
 *
 * Kept free of React so the load arithmetic can be tested directly -- the
 * previous graph mixed this with absolute node positioning, which is what made
 * it both untestable and prone to overlap.
 */

import { HostStatus, HostWithInstances, Instance, InstanceStatus, getModelCategory } from '@/api/types';
import { InstanceStateData, RequestState } from '@/hooks/useEventStream';

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

/** How many unfinished requests each instance is currently holding. */
export function countInFlightByInstance(requests: Iterable<RequestState>): Map<string, number> {
  const counts = new Map<string, number>();
  for (const request of requests) {
    if (!isActiveRequest(request) || !request.instance_id) continue;
    const key = cellKey(request.host_id ?? '', request.instance_id);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

export function cellKey(hostId: string, instanceId: string): string {
  return `${hostId}:${instanceId}`;
}

interface BuildOptions {
  hosts: HostWithInstances[];
  requests: Iterable<RequestState>;
  getInstanceState: (hostId: string, instanceId: string) => InstanceStateData | null | undefined;
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
  requests,
  getInstanceState,
  search = '',
  runningOnly = false,
}: BuildOptions): InstanceCell[] {
  const inFlight = countInFlightByInstance(requests);
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
  requests: Iterable<RequestState>,
  endpointCount: number,
): FlowTotals {
  let pending = 0;
  let processing = 0;
  let errored = 0;
  for (const request of requests) {
    if (request.removing) continue;
    if (request.status === 'pending') pending += 1;
    else if (request.status === 'processing' || request.status === 'routed') processing += 1;
    else if (request.status === 'error') errored += 1;
  }

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
    pending,
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
 * anyone can read, so an unbounded list is just a scroll bar.
 */
export function tickerRequests(requests: Iterable<RequestState>, limit = 40): RequestState[] {
  return [...requests]
    .filter((r) => isActiveRequest(r) || r.status === 'error')
    .sort((a, b) => (b.timestamp ?? '').localeCompare(a.timestamp ?? ''))
    .slice(0, limit);
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
