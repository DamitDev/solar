/**
 * Builds the routing flowchart: endpoint -> gateway -> model -> host.
 *
 * Two things keep it readable at fleet scale. Traffic is aggregated onto edge
 * weights, so load never adds nodes; and the host column is drawn per expanded
 * model, so the picture never contains every model-to-host pair at once --
 * fifty hosts times a handful of models is a hairball no layout can fix.
 *
 * Nodes also carry their own size, because the previous graph derived
 * positions from constants that nothing enforced on the rendered node, and
 * boxes collided as soon as one grew a line of text.
 */

import { HostStatus, HostWithInstances, InstanceStatus } from '@/api/types';
import { InstanceStateData, RequestState } from '@/hooks/useEventStream';
import { seriesColor } from '@/components/charts/chartTheme';
import { InstanceCell, buildCells, collator, isActiveRequest } from './workload';

export const GATEWAY_NODE_ID = 'gateway';

/** Rendered size per node kind. The node components read the same numbers. */
export const NODE_SIZE = {
  endpoint: { width: 168, height: 48 },
  gateway: { width: 196, height: 96 },
  model: { width: 216, height: 72 },
  host: { width: 216, height: 76 },
  overflow: { width: 216, height: 56 },
} as const;

/**
 * Hosts drawn individually before the rest roll up into one box. A model on
 * thirty hosts is not thirty things you want to read; it is a handful that are
 * busy or broken, and a count.
 */
export const MAX_HOSTS_PER_MODEL = 8;

export type FlowNodeKind = keyof typeof NODE_SIZE;

export interface EndpointNodeData {
  kind: 'endpoint';
  label: string;
  color: string;
  inFlight: number;
  errors: number;
}

export interface GatewayNodeData {
  kind: 'gateway';
  queued: number;
  processing: number;
  errored: number;
  hostsOnline: number;
  hostsTotal: number;
}

export interface ModelNodeData {
  kind: 'model';
  alias: string;
  /** Underlying model id, when it says something the alias does not. */
  model: string | null;
  category: string;
  hosts: number;
  instances: number;
  running: number;
  inFlight: number;
  errors: number;
  expanded: boolean;
}

export interface HostNodeData {
  kind: 'host';
  hostName: string;
  hostStatus: HostStatus;
  alias: string;
  /** Worst status among this host's instances of the model. */
  status: InstanceStatus;
  instances: number;
  running: number;
  inFlight: number;
  errors: number;
  state: InstanceStateData | null;
}

export interface OverflowNodeData {
  kind: 'overflow';
  alias: string;
  hosts: number;
  running: number;
  instances: number;
  inFlight: number;
  errors: number;
}

export type FlowNodeData = EndpointNodeData | GatewayNodeData | ModelNodeData | HostNodeData | OverflowNodeData;

export interface FlowGraphNode {
  id: string;
  kind: FlowNodeKind;
  width: number;
  height: number;
  data: FlowNodeData;
}

export type EdgeTone = 'idle' | 'ready' | 'active' | 'error';

export interface FlowGraphEdge {
  id: string;
  source: string;
  target: string;
  /** Unfinished requests currently on this hop. */
  inFlight: number;
  errors: number;
  tone: EdgeTone;
  /** Endpoint colour, so a request keeps its identity along the path. */
  color?: string;
  /** Instances behind a model-to-host hop, when a host runs more than one. */
  multiplicity?: number;
}

export interface FlowGraph {
  nodes: FlowGraphNode[];
  edges: FlowGraphEdge[];
  /** Models present after filtering, in render order. */
  aliases: string[];
  instanceCount: number;
  /** `hostId:instanceId` to alias, so a routed request can find its model. */
  aliasByInstance: Map<string, string>;
}

export interface BuildGraphOptions {
  hosts: HostWithInstances[];
  requests: RequestState[];
  endpoints: { id: string; name: string }[];
  getInstanceState: (hostId: string, instanceId: string) => InstanceStateData | null | undefined;
  search?: string;
  runningOnly?: boolean;
  /** Aliases whose host column is drawn. */
  expanded?: ReadonlySet<string>;
  /** Draw every model's hosts, whatever `expanded` says. */
  expandAll?: boolean;
  /** Aliases drawn host by host, past the usual cap. */
  showAllHosts?: ReadonlySet<string>;
}

export function endpointNodeId(endpointId: string): string {
  return `endpoint:${endpointId}`;
}

export function modelNodeId(alias: string): string {
  return `model:${alias}`;
}

export function hostNodeId(alias: string, hostId: string): string {
  return `host:${alias}:${hostId}`;
}

export function overflowNodeId(alias: string): string {
  return `more:${alias}`;
}

function toneOf(inFlight: number, errors: number, running: number): EdgeTone {
  if (errors > 0) return 'error';
  if (inFlight > 0) return 'active';
  return running > 0 ? 'ready' : 'idle';
}

/** Aggregate of one alias on one host, which is what a model-to-host hop means. */
interface HostBinding {
  hostId: string;
  hostName: string;
  hostStatus: HostStatus;
  cells: InstanceCell[];
}

export function buildFlowGraph({
  hosts,
  requests,
  endpoints,
  getInstanceState,
  search = '',
  runningOnly = false,
  expanded,
  expandAll = false,
  showAllHosts,
}: BuildGraphOptions): FlowGraph {
  const cells = buildCells({ hosts, requests, getInstanceState, search, runningOnly });
  const models = groupByAlias(cells);
  const aliasOfCell = new Map(cells.map((cell) => [cell.key, cell.alias]));
  const traffic = tallyTraffic(requests, aliasOfCell, new Set(models.keys()));

  const nodes: FlowGraphNode[] = [];
  const edges: FlowGraphEdge[] = [];

  endpoints.forEach((endpoint, index) => {
    const stats = traffic.byEndpoint.get(endpoint.id) ?? { inFlight: 0, errors: 0 };
    const color = seriesColor(index);
    nodes.push({
      id: endpointNodeId(endpoint.id),
      kind: 'endpoint',
      ...NODE_SIZE.endpoint,
      data: { kind: 'endpoint', label: endpoint.name, color, inFlight: stats.inFlight, errors: stats.errors },
    });
    edges.push({
      id: `${endpointNodeId(endpoint.id)}->${GATEWAY_NODE_ID}`,
      source: endpointNodeId(endpoint.id),
      target: GATEWAY_NODE_ID,
      inFlight: stats.inFlight,
      errors: stats.errors,
      tone: toneOf(stats.inFlight, stats.errors, 1),
      color,
    });
  });

  nodes.push({
    id: GATEWAY_NODE_ID,
    kind: 'gateway',
    ...NODE_SIZE.gateway,
    data: {
      kind: 'gateway',
      queued: traffic.queued,
      processing: traffic.processing,
      errored: traffic.errored,
      hostsOnline: hosts.filter((host) => host.status === 'online').length,
      hostsTotal: hosts.length,
    },
  });

  const aliases = [...models.keys()].sort(collator.compare);

  for (const alias of aliases) {
    const bindings = models.get(alias)!;
    const modelCells = [...bindings.values()].flatMap((binding) => binding.cells);
    const stats = traffic.byModel.get(alias) ?? { inFlight: 0, errors: 0 };
    const isExpanded = expandAll || (expanded?.has(alias) ?? false);
    const running = modelCells.filter((cell) => cell.status === 'running').length;
    const underlying = modelCells.find((cell) => cell.model && cell.model !== alias)?.model ?? null;

    nodes.push({
      id: modelNodeId(alias),
      kind: 'model',
      ...NODE_SIZE.model,
      data: {
        kind: 'model',
        alias,
        model: underlying,
        category: modelCells[0]?.category ?? 'generation',
        hosts: bindings.size,
        instances: modelCells.length,
        running,
        inFlight: stats.inFlight,
        errors: stats.errors,
        expanded: isExpanded,
      },
    });

    edges.push({
      id: `${GATEWAY_NODE_ID}->${modelNodeId(alias)}`,
      source: GATEWAY_NODE_ID,
      target: modelNodeId(alias),
      inFlight: stats.inFlight,
      errors: stats.errors,
      tone: toneOf(stats.inFlight, stats.errors, running),
    });

    if (!isExpanded) continue;

    const all = [...bindings.values()];
    const errorsOf = (binding: HostBinding) => traffic.byBinding.get(bindingKey(alias, binding.hostId))?.errors ?? 0;
    const shown = showAllHosts?.has(alias) ? all : pickInteresting(all, errorsOf);
    const hidden = all.filter((binding) => !shown.includes(binding));

    for (const binding of [...shown].sort((a, b) => collator.compare(a.hostName, b.hostName))) {
      const bindingStats = traffic.byBinding.get(bindingKey(alias, binding.hostId)) ?? { inFlight: 0, errors: 0 };
      const bindingRunning = binding.cells.filter((cell) => cell.status === 'running').length;

      nodes.push({
        id: hostNodeId(alias, binding.hostId),
        kind: 'host',
        ...NODE_SIZE.host,
        data: {
          kind: 'host',
          hostName: binding.hostName,
          hostStatus: binding.hostStatus,
          alias,
          status: worstStatus(binding.cells),
          instances: binding.cells.length,
          running: bindingRunning,
          inFlight: binding.cells.reduce((sum, cell) => sum + cell.inFlight, 0),
          errors: bindingStats.errors,
          state: busiestState(binding.cells),
        },
      });

      edges.push({
        id: `${modelNodeId(alias)}->${hostNodeId(alias, binding.hostId)}`,
        source: modelNodeId(alias),
        target: hostNodeId(alias, binding.hostId),
        inFlight: bindingStats.inFlight,
        errors: bindingStats.errors,
        tone: toneOf(bindingStats.inFlight, bindingStats.errors, bindingRunning),
        multiplicity: binding.cells.length,
      });
    }

    if (hidden.length === 0) continue;

    const hiddenCells = hidden.flatMap((binding) => binding.cells);
    const hiddenTally = hidden.reduce(
      (acc, binding) => {
        const stats = traffic.byBinding.get(bindingKey(alias, binding.hostId));
        return { inFlight: acc.inFlight + (stats?.inFlight ?? 0), errors: acc.errors + (stats?.errors ?? 0) };
      },
      { inFlight: 0, errors: 0 },
    );

    nodes.push({
      id: overflowNodeId(alias),
      kind: 'overflow',
      ...NODE_SIZE.overflow,
      data: {
        kind: 'overflow',
        alias,
        hosts: hidden.length,
        instances: hiddenCells.length,
        running: hiddenCells.filter((cell) => cell.status === 'running').length,
        inFlight: hiddenTally.inFlight,
        errors: hiddenTally.errors,
      },
    });

    edges.push({
      id: `${modelNodeId(alias)}->${overflowNodeId(alias)}`,
      source: modelNodeId(alias),
      target: overflowNodeId(alias),
      inFlight: hiddenTally.inFlight,
      errors: hiddenTally.errors,
      tone: toneOf(hiddenTally.inFlight, hiddenTally.errors, 0),
      multiplicity: hidden.length,
    });
  }

  return { nodes, edges, aliases, instanceCount: cells.length, aliasByInstance: aliasOfCell };
}

function bindingKey(alias: string, hostId: string): string {
  return `${alias}|${hostId}`;
}

/**
 * The hosts worth a box of their own: anything failing or working, then the
 * rest by name. Whatever does not make the cut is still counted, just rolled
 * into one node -- twenty idle hosts say the same thing twenty times.
 */
function pickInteresting(bindings: HostBinding[], errorsOf: (binding: HostBinding) => number): HostBinding[] {
  if (bindings.length <= MAX_HOSTS_PER_MODEL) return bindings;

  const score = (binding: HostBinding) => {
    if (errorsOf(binding) > 0) return 0;
    if (binding.cells.some((cell) => cell.inFlight > 0)) return 1;
    if (binding.cells.some((cell) => cell.status === 'failed')) return 2;
    if (binding.cells.some((cell) => cell.state?.busy)) return 3;
    if (binding.cells.some((cell) => cell.status === 'running')) return 4;
    return 5;
  };

  return [...bindings]
    .sort((a, b) => score(a) - score(b) || collator.compare(a.hostName, b.hostName))
    .slice(0, MAX_HOSTS_PER_MODEL);
}

function groupByAlias(cells: InstanceCell[]): Map<string, Map<string, HostBinding>> {
  const models = new Map<string, Map<string, HostBinding>>();
  for (const cell of cells) {
    let bindings = models.get(cell.alias);
    if (!bindings) {
      bindings = new Map();
      models.set(cell.alias, bindings);
    }
    let binding = bindings.get(cell.hostId);
    if (!binding) {
      binding = { hostId: cell.hostId, hostName: cell.hostName, hostStatus: cell.hostStatus, cells: [] };
      bindings.set(cell.hostId, binding);
    }
    binding.cells.push(cell);
  }
  return models;
}

/** Failures first: a host with one broken instance is worth looking at. */
function worstStatus(cells: InstanceCell[]): InstanceStatus {
  if (cells.some((cell) => cell.status === 'failed')) return 'failed';
  if (cells.some((cell) => cell.status === 'running')) return 'running';
  return cells[0]?.status ?? 'stopped';
}

function busiestState(cells: InstanceCell[]): InstanceStateData | null {
  const withState = cells.filter((cell) => cell.state);
  if (withState.length === 0) return null;
  return (withState.find((cell) => cell.state?.busy) ?? withState[0]).state;
}

interface Tally {
  inFlight: number;
  errors: number;
}

interface Traffic {
  byEndpoint: Map<string, Tally>;
  byModel: Map<string, Tally>;
  byBinding: Map<string, Tally>;
  queued: number;
  processing: number;
  errored: number;
}

/**
 * Rolls live requests onto the hops they occupy. A request names its instance
 * once routed; before that it can still be placed on a model by name, which is
 * what makes a queued request visible where it is actually waiting.
 */
function tallyTraffic(
  requests: Iterable<RequestState>,
  aliasOfCell: Map<string, string>,
  knownAliases: ReadonlySet<string>,
): Traffic {
  const traffic: Traffic = {
    byEndpoint: new Map(),
    byModel: new Map(),
    byBinding: new Map(),
    queued: 0,
    processing: 0,
    errored: 0,
  };

  const bump = (map: Map<string, Tally>, key: string, field: keyof Tally) => {
    const current = map.get(key) ?? { inFlight: 0, errors: 0 };
    current[field] += 1;
    map.set(key, current);
  };

  for (const request of requests) {
    if (request.removing) continue;
    const active = isActiveRequest(request);
    const failed = request.status === 'error';
    if (!active && !failed) continue;

    const field: keyof Tally = failed ? 'errors' : 'inFlight';
    if (failed) traffic.errored += 1;
    else if (request.status === 'pending') traffic.queued += 1;
    else traffic.processing += 1;

    if (request.endpoint_id) bump(traffic.byEndpoint, request.endpoint_id, field);

    const alias = aliasFor(request, aliasOfCell, knownAliases);
    if (!alias) continue;
    bump(traffic.byModel, alias, field);
    if (request.host_id) bump(traffic.byBinding, bindingKey(alias, request.host_id), field);
  }

  return traffic;
}

function aliasFor(
  request: RequestState,
  aliasOfCell: Map<string, string>,
  knownAliases: ReadonlySet<string>,
): string | null {
  if (request.host_id && request.instance_id) {
    const alias = aliasOfCell.get(`${request.host_id}:${request.instance_id}`);
    if (alias) return alias;
  }
  for (const name of [request.resolved_model, request.model]) {
    if (name && knownAliases.has(name)) return name;
  }
  return null;
}

/**
 * Every node and edge on a path through `nodeId`, in both directions, so
 * clicking any box answers "what feeds this, and where does it go".
 */
export function traceThrough(graph: FlowGraph, nodeId: string): { nodes: Set<string>; edges: Set<string> } {
  const nodes = new Set<string>([nodeId]);
  const edges = new Set<string>();

  const walk = (direction: 'up' | 'down') => {
    const seen = new Set([nodeId]);
    const frontier = [nodeId];
    while (frontier.length > 0) {
      const current = frontier.pop()!;
      for (const edge of graph.edges) {
        const from = direction === 'down' ? edge.source : edge.target;
        const to = direction === 'down' ? edge.target : edge.source;
        if (from !== current) continue;
        edges.add(edge.id);
        if (seen.has(to)) continue;
        seen.add(to);
        nodes.add(to);
        frontier.push(to);
      }
    }
  };

  walk('down');
  walk('up');
  return { nodes, edges };
}

/** The hops one request occupies, for highlighting a ticker entry. */
export function traceRequest(
  graph: FlowGraph,
  request: RequestState,
  alias: string | null,
): { nodes: Set<string>; edges: Set<string> } {
  const path: string[] = [];
  if (request.endpoint_id) path.push(endpointNodeId(request.endpoint_id));
  path.push(GATEWAY_NODE_ID);
  if (alias) {
    path.push(modelNodeId(alias));
    if (request.host_id) path.push(hostNodeId(alias, request.host_id));
  }

  const known = new Set(graph.nodes.map((node) => node.id));
  const nodes = new Set(path.filter((id) => known.has(id)));
  const edges = new Set<string>();
  for (let i = 0; i < path.length - 1; i += 1) {
    const edge = graph.edges.find((candidate) => candidate.source === path[i] && candidate.target === path[i + 1]);
    if (edge) edges.add(edge.id);
  }
  return { nodes, edges };
}

/** Which model a live request belongs to, for tracing and auto-expansion. */
export function aliasOfRequest(graph: FlowGraph, request: RequestState): string | null {
  return aliasFor(request, graph.aliasByInstance, new Set(graph.aliases));
}
