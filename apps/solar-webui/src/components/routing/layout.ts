/**
 * Places the flowchart: four columns, one block per model.
 *
 * The graph is a forest of stars -- every host hangs off exactly one model --
 * so ordering is a non-problem and the layout is arithmetic. What matters is
 * that the arithmetic uses the sizes the nodes actually render at, which is
 * precisely what the previous graph got wrong: it advanced a y cursor by a
 * guessed row height and boxes collided the moment one grew a line of text.
 * layout.test.ts asserts no two boxes overlap across a range of fleet shapes.
 */

import { FlowGraph, FlowGraphNode, GATEWAY_NODE_ID, NODE_SIZE } from './graph';

export interface NodePosition {
  x: number;
  y: number;
}

export interface LayoutResult {
  positions: Map<string, NodePosition>;
  width: number;
  height: number;
}

/** Gap between columns. */
const RANK_SEP = 88;
/** Gap between two boxes in the same column. */
const NODE_SEP = 14;
/** Gap between two models and everything hanging off them. */
const BLOCK_SEP = 34;
/** A fan-out taller than this wraps into further columns. */
const GRID_ROWS = 9;
const MARGIN = 24;

export function layoutGraph(graph: FlowGraph): LayoutResult {
  const positions = new Map<string, NodePosition>();
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));

  const endpoints = graph.nodes.filter((node) => node.kind === 'endpoint');
  const models = graph.nodes.filter((node) => node.kind === 'model');
  const childrenOf = groupChildren(graph, byId);

  const xEndpoint = MARGIN;
  const xGateway = xEndpoint + NODE_SIZE.endpoint.width + RANK_SEP;
  const xModel = xGateway + NODE_SIZE.gateway.width + RANK_SEP;
  const xFanOut = xModel + NODE_SIZE.model.width + RANK_SEP;

  // Each model owns a horizontal band containing its own fan-out, so blocks
  // can never reach into one another.
  let cursor = MARGIN;
  let widest = xModel + NODE_SIZE.model.width;

  for (const model of models) {
    const children = childrenOf.get(model.id) ?? [];
    const rows = Math.min(Math.max(children.length, 1), GRID_ROWS);
    const rowHeight = NODE_SIZE.host.height + NODE_SEP;
    const blockHeight = children.length === 0 ? model.height : rows * rowHeight - NODE_SEP;

    children.forEach((child, index) => {
      // Column-major, so the names read down the page and the nearest column
      // fills first, which keeps the edges short.
      const column = Math.floor(index / GRID_ROWS);
      const row = index % GRID_ROWS;
      const x = xFanOut + column * (NODE_SIZE.host.width + NODE_SEP);
      positions.set(child.id, { x, y: cursor + row * rowHeight });
      widest = Math.max(widest, x + child.width);
    });

    positions.set(model.id, { x: xModel, y: cursor + (blockHeight - model.height) / 2 });
    cursor += blockHeight + BLOCK_SEP;
  }

  const contentHeight = models.length > 0 ? cursor - BLOCK_SEP - MARGIN : 0;

  // The gateway and the endpoints face the middle of everything they feed.
  const middle = MARGIN + contentHeight / 2;
  positions.set(GATEWAY_NODE_ID, { x: xGateway, y: middle - NODE_SIZE.gateway.height / 2 });

  const endpointsHeight = endpoints.length * (NODE_SIZE.endpoint.height + NODE_SEP) - NODE_SEP;
  let endpointY = Math.max(MARGIN, middle - endpointsHeight / 2);
  for (const endpoint of endpoints) {
    positions.set(endpoint.id, { x: xEndpoint, y: endpointY });
    endpointY += NODE_SIZE.endpoint.height + NODE_SEP;
  }

  let height = 0;
  for (const node of graph.nodes) {
    const position = positions.get(node.id);
    if (!position) continue;
    height = Math.max(height, position.y + node.height);
  }

  return { positions, width: widest, height };
}

export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * The box around a set of nodes, for framing the view. Taking it from the
 * layout rather than from React Flow's measurements means framing works on the
 * frame the nodes appear, before anything has been measured.
 */
export function boundsOf(graph: FlowGraph, layout: LayoutResult, ids?: Iterable<string>): Bounds {
  const wanted = ids ? new Set(ids) : null;
  let left = Infinity;
  let top = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;

  for (const node of graph.nodes) {
    if (wanted && !wanted.has(node.id)) continue;
    const position = layout.positions.get(node.id);
    if (!position) continue;
    left = Math.min(left, position.x);
    top = Math.min(top, position.y);
    right = Math.max(right, position.x + node.width);
    bottom = Math.max(bottom, position.y + node.height);
  }

  if (left === Infinity) return { x: 0, y: 0, width: layout.width, height: layout.height };
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/** Grows a box around its centre so framing it cannot zoom in past `maxZoom`. */
export function atLeast(bounds: Bounds, viewport: { width: number; height: number }, maxZoom: number): Bounds {
  const width = Math.max(bounds.width, viewport.width / maxZoom);
  const height = Math.max(bounds.height, viewport.height / maxZoom);
  return {
    x: bounds.x + bounds.width / 2 - width / 2,
    y: bounds.y + bounds.height / 2 - height / 2,
    width,
    height,
  };
}

/** Fan-out nodes per model, in graph order. */
function groupChildren(graph: FlowGraph, byId: Map<string, FlowGraphNode>): Map<string, FlowGraphNode[]> {
  const children = new Map<string, FlowGraphNode[]>();
  for (const edge of graph.edges) {
    if (edge.source === GATEWAY_NODE_ID) continue;
    const child = byId.get(edge.target);
    if (!child || (child.kind !== 'host' && child.kind !== 'overflow')) continue;
    const list = children.get(edge.source) ?? [];
    list.push(child);
    children.set(edge.source, list);
  }
  return children;
}
