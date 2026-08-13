import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  MarkerType,
  MiniMap,
  Node,
  ReactFlowProvider,
  type ReactFlowState,
  useReactFlow,
  useStore,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { Activity, Search, X } from 'lucide-react';
import solarClient from '@/api/client';
import { ApiEndpoint } from '@/api/types';
import { useEventStreamContext } from '@/context/EventStreamContext';
import { useRoutingEventsContext } from '@/context/RoutingEventsContext';
import { useFallbackPolling } from '@/hooks/useFallbackPolling';
import { useInstances } from '@/hooks/useInstances';
import { RequestState } from '@/hooks/useEventStream';
import { cn } from '@/lib/utils';
import { RequestTicker } from './routing/RequestTicker';
import { SummaryBar } from './routing/SummaryBar';
import { FlowGraph, ModelNodeData, aliasOfRequest, buildFlowGraph, traceRequest, traceThrough } from './routing/graph';
import { Bounds, LayoutResult, atLeast, boundsOf, layoutGraph } from './routing/layout';
import { EndpointNode, GatewayNode, HostNode, ModelNode, OverflowNode } from './routing/nodes';
import { summarizeFlow, tickerRequests } from './routing/workload';

const EDGE_COLORS = { idle: '#434C5E', ready: '#4C566A', active: '#88C0D0', error: '#BF616A' } as const;

/** Framing a couple of boxes should not blow them up to fill the canvas. */
const MAX_ZOOM = 1;

// Must be stable: React Flow remounts every node when this object changes.
const nodeTypes = {
  endpoint: EndpointNode,
  gateway: GatewayNode,
  model: ModelNode,
  host: HostNode,
  overflow: OverflowNode,
};

const canvasWidth = (state: ReactFlowState) => state.width;
const canvasHeight = (state: ReactFlowState) => state.height;

interface Trace {
  nodes: Set<string>;
  edges: Set<string>;
  /** Node id when a box was clicked, request id when a ticker row was. */
  origin: string;
  from: 'node' | 'request';
}

/**
 * The routing flowchart: endpoint -> gateway -> model -> host.
 *
 * Only the models are drawn by default. Fifty hosts across a handful of models
 * is several hundred edges, which no layout makes readable, so a model's hosts
 * appear when it is opened -- and any request can be clicked to trace the exact
 * path it took.
 */
export function RoutingFlow() {
  return (
    <ReactFlowProvider>
      <RoutingFlowCanvas />
    </ReactFlowProvider>
  );
}

function RoutingFlowCanvas() {
  const { requests, removeRequest } = useRoutingEventsContext();
  const { getInstanceState, endpoints: eventEndpoints, isConnected } = useEventStreamContext();
  const { hosts, loading } = useInstances();
  const { fitBounds } = useReactFlow();
  const canvas = { width: useStore(canvasWidth), height: useStore(canvasHeight) };

  const [endpoints, setEndpoints] = useState<ApiEndpoint[]>([]);
  const [search, setSearch] = useState('');
  const [runningOnly, setRunningOnly] = useState(false);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [showAllHosts, setShowAllHosts] = useState<ReadonlySet<string>>(new Set());
  const [trace, setTrace] = useState<Trace | null>(null);

  // endpoints_update only fires on CRUD, so a fresh socket has no list yet.
  useFallbackPolling(
    () => {
      solarClient
        .getEndpoints()
        .then(setEndpoints)
        .catch((err) => console.error('Failed to fetch endpoints:', err));
    },
    { enabled: !isConnected, intervalMs: 10000 },
  );

  useEffect(() => {
    if (eventEndpoints.length > 0) {
      setEndpoints(eventEndpoints);
      return;
    }
    solarClient
      .getEndpoints()
      .then(setEndpoints)
      .catch((err) => console.error('Failed to fetch endpoints:', err));
  }, [eventEndpoints]);

  const requestList = useMemo(() => Array.from(requests.values()), [requests]);

  // A search is already a narrow answer, so showing its hosts costs nothing
  // and saves opening each match by hand.
  const searching = search.trim().length > 0;

  const graph = useMemo(
    () =>
      buildFlowGraph({
        hosts,
        requests: requestList,
        endpoints,
        getInstanceState,
        search,
        runningOnly,
        expanded,
        expandAll: searching,
        showAllHosts,
      }),
    [hosts, requestList, endpoints, getInstanceState, search, runningOnly, expanded, searching, showAllHosts],
  );

  const layout = useStableLayout(graph);
  const nodes = useMemo(() => toFlowNodes(graph, layout, trace), [graph, layout, trace]);
  const edges = useMemo(() => toFlowEdges(graph, trace), [graph, trace]);

  const ticker = useMemo(() => tickerRequests(requestList), [requestList]);
  const totals = useMemo(
    () => summarizeFlow(hosts, requestList, endpoints.length),
    [hosts, requestList, endpoints.length],
  );

  // A trace frames its own path: fitting the whole fleet around it would zoom
  // out past being able to read a box. The frame comes from the layout rather
  // than from React Flow, which cannot measure a node until it has painted.
  const shape = graph.nodes.length;
  const frame = useRef<Bounds>({ x: 0, y: 0, width: 0, height: 0 });
  frame.current = atLeast(boundsOf(graph, layout, trace?.nodes), canvas, MAX_ZOOM);
  // Reframe when the graph changes shape or the traced path does -- but not as
  // traffic moves, which would yank the viewport several times a second. Keying
  // on the path rather than on the node it started from matters because opening
  // a model extends its own trace, and the new hosts belong in view.
  const framed = trace ? [...trace.nodes].sort().join() : String(shape);
  useEffect(() => {
    fitBounds(frame.current, { padding: 0.15, duration: 300 });
  }, [framed, fitBounds]);

  const toggleModel = useCallback((alias: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(alias)) next.add(alias);
      return next;
    });
  }, []);

  const onNodeClick = useCallback(
    (_: unknown, node: Node) => {
      const data = node.data as { kind?: string; alias?: string };
      if (data.kind === 'overflow' && data.alias) {
        setShowAllHosts((current) => new Set([...current, data.alias!]));
        return;
      }
      if (data.kind === 'model' && data.alias) toggleModel(data.alias);
      setTrace((current) =>
        current?.origin === node.id ? null : { ...traceThrough(graph, node.id), origin: node.id, from: 'node' },
      );
    },
    [graph, toggleModel],
  );

  const onRequestClick = useCallback(
    (request: RequestState) => {
      if (trace?.origin === request.request_id) {
        setTrace(null);
        return;
      }
      // Open the model the request is using, so its host is on screen to trace.
      const alias = aliasOfRequest(graph, request);
      if (alias && request.host_id && !expanded.has(alias)) {
        setExpanded(new Set([...expanded, alias]));
      }
      setTrace({ ...traceRequest(graph, request, alias), origin: request.request_id, from: 'request' });
    },
    [graph, trace, expanded],
  );

  // A trace is computed against the graph that existed when it was made, and
  // that graph then changes underneath it: opening a model adds the hosts the
  // trace should cover, and a request gains a host once it is routed. Redo it
  // whenever the graph moves, and drop it when its subject is gone.
  const origin = trace?.origin ?? null;
  const from = trace?.from ?? null;
  useEffect(() => {
    if (!origin) return;

    if (from === 'node') {
      if (!graph.nodes.some((node) => node.id === origin)) {
        setTrace(null);
        return;
      }
      setTrace({ ...traceThrough(graph, origin), origin, from });
      return;
    }

    const request = requests.get(origin);
    if (!request || request.removing) {
      setTrace(null);
      return;
    }
    setTrace({ ...traceRequest(graph, request, aliasOfRequest(graph, request)), origin, from: 'request' });
  }, [graph, requests, origin, from]);

  const clear = () => {
    setTrace(null);
    setExpanded(new Set());
    setShowAllHosts(new Set());
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
        <div className="text-xl text-nord-4">Loading routing flow...</div>
      </div>
    );
  }

  return (
    <div className="p-4 flex flex-col gap-3" style={{ height: 'calc(100vh - 60px)' }}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Activity className="text-nord-8" />
          <h1 className="text-2xl font-semibold text-nord-6">Routing</h1>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative">
            <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-nord-4" />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search models and hosts"
              aria-label="Search models and hosts"
              className="bg-nord-2 text-nord-6 border border-nord-3 rounded pl-7 pr-7 py-1 w-60 placeholder:text-nord-4"
            />
            {search && (
              <button
                onClick={() => setSearch('')}
                aria-label="Clear search"
                className="absolute right-1.5 top-1/2 -translate-y-1/2 text-nord-4 hover:text-nord-6"
              >
                <X size={14} />
              </button>
            )}
          </div>

          <button
            onClick={() => setRunningOnly((v) => !v)}
            aria-pressed={runningOnly}
            className={cn(
              'px-2 py-1 rounded border text-xs',
              runningOnly
                ? 'bg-nord-10 text-nord-6 border-nord-10'
                : 'bg-nord-2 text-nord-4 border-nord-3 hover:bg-nord-3',
            )}
            title="Hide instances that are not running"
          >
            Running only
          </button>

          <button
            onClick={() => {
              setExpanded(new Set(graph.aliases));
              setTrace(null);
            }}
            className="px-2 py-1 rounded border border-nord-3 bg-nord-2 text-nord-4 text-xs hover:bg-nord-3"
            title="Show the hosts behind every model"
          >
            Expand all
          </button>

          {(expanded.size > 0 || trace) && (
            <button
              onClick={clear}
              className="px-2 py-1 rounded border border-nord-3 bg-nord-2 text-nord-4 text-xs hover:bg-nord-3"
            >
              Collapse
            </button>
          )}
        </div>
      </div>

      <div className="flex items-center justify-between gap-3">
        <SummaryBar totals={totals} shown={graph.instanceCount} total={totals.instancesTotal} />
        <span className="text-xs text-nord-4 shrink-0">
          {expanded.size > 0 ? 'Click a model to close it' : 'Click a model to see its hosts'}
        </span>
      </div>

      <div className="flex-1 min-h-0 grid grid-cols-1 xl:grid-cols-[1fr_18rem] gap-3">
        <div className="bg-nord-1 border border-nord-3 rounded overflow-hidden">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodeClick={onNodeClick}
            onPaneClick={() => setTrace(null)}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            proOptions={{ hideAttribution: true }}
            minZoom={0.1}
            maxZoom={1.5}
            fitView
            fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#3B4252" />
            <Controls showInteractive={false} />
            {/* Only worth the corner it covers once the graph outgrows the screen. */}
            {shape > 24 && (
              <MiniMap
                pannable
                zoomable
                nodeColor={miniMapColor}
                nodeStrokeWidth={0}
                maskColor="rgba(46, 52, 64, 0.6)"
                style={{
                  backgroundColor: 'rgba(59, 66, 82, 0.85)',
                  border: '1px solid #4C566A',
                  width: 160,
                  height: 108,
                }}
              />
            )}
          </ReactFlow>
        </div>

        <RequestTicker
          requests={ticker}
          onDismiss={removeRequest}
          onSelect={onRequestClick}
          selectedId={trace?.origin}
        />
      </div>
    </div>
  );
}

/**
 * dagre only needs to run when the graph's shape changes. Traffic updates
 * arrive several times a second and must not move any box.
 */
function useStableLayout(graph: FlowGraph): LayoutResult {
  const cache = useRef<{ signature: string; result: LayoutResult } | null>(null);
  const signature = graph.nodes.map((node) => node.id).join('|');
  if (!cache.current || cache.current.signature !== signature) {
    cache.current = { signature, result: layoutGraph(graph) };
  }
  return cache.current.result;
}

function toFlowNodes(graph: FlowGraph, layout: LayoutResult, trace: Trace | null): Node[] {
  return graph.nodes.map((node) => ({
    id: node.id,
    type: node.kind,
    position: layout.positions.get(node.id) ?? { x: 0, y: 0 },
    // Size lives on the node component. Setting it here too makes React Flow
    // draw its own wrapper box behind the node.
    data: { ...node.data, dimmed: trace ? !trace.nodes.has(node.id) : false, selected: trace?.origin === node.id },
  }));
}

function toFlowEdges(graph: FlowGraph, trace: Trace | null): Edge[] {
  return graph.edges.map((edge) => {
    const traced = trace ? trace.edges.has(edge.id) : true;
    // Quiet hops keep the endpoint's colour so a lane stays identifiable; busy
    // and broken ones switch to the status colour, which matters more.
    const stroke =
      edge.tone === 'active' || edge.tone === 'error' ? EDGE_COLORS[edge.tone] : (edge.color ?? EDGE_COLORS[edge.tone]);
    const label =
      edge.inFlight > 0
        ? String(edge.inFlight)
        : edge.multiplicity && edge.multiplicity > 1
          ? `×${edge.multiplicity}`
          : undefined;

    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      animated: edge.tone === 'active' && traced,
      label,
      labelStyle: { fill: '#D8DEE9', fontSize: 10 },
      labelBgStyle: { fill: '#2E3440' },
      labelBgPadding: [3, 1] as [number, number],
      style: {
        stroke,
        strokeWidth: edge.tone === 'active' ? Math.min(4, 1.5 + edge.inFlight * 0.5) : 1,
        strokeDasharray: edge.tone === 'idle' ? '4 4' : undefined,
        opacity: traced ? 1 : 0.1,
      },
      markerEnd:
        edge.tone === 'active' || edge.tone === 'error'
          ? { type: MarkerType.ArrowClosed, color: stroke, width: 14, height: 14 }
          : undefined,
    };
  });
}

function miniMapColor(node: Node): string {
  const data = node.data as (ModelNodeData | { kind?: string; color?: string; status?: string }) & {
    kind?: string;
  };
  if (data.kind === 'endpoint') return (data as { color?: string }).color ?? '#5E81AC';
  if (data.kind === 'gateway') return '#5E81AC';
  if (data.kind === 'model') return (data as ModelNodeData).inFlight > 0 ? '#88C0D0' : '#4C566A';
  const status = (data as { status?: string }).status;
  if (status === 'failed') return '#BF616A';
  return status === 'running' ? '#A3BE8C' : '#434C5E';
}
