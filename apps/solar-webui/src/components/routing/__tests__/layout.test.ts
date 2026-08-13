import { describe, expect, it } from 'vitest';
import { buildFlowGraph, FlowGraph, NODE_SIZE } from '../graph';
import { atLeast, boundsOf, layoutGraph } from '../layout';
import { HostWithInstances, Instance } from '@/api/types';

function instance(id: string, alias: string, model: string, status: Instance['status'] = 'running') {
  return {
    id,
    status,
    retry_count: 0,
    created_at: '2026-08-13T10:00:00Z',
    config: { backend_type: 'llamacpp', alias, model, host: 'h' },
  } as unknown as Instance;
}

/** A fleet with uneven fan-out, which is what used to make rows collide. */
function fleet(hostCount: number, instancesPerHost: (index: number) => number): HostWithInstances[] {
  return Array.from({ length: hostCount }, (_, h) => ({
    id: `h${h}`,
    name: `host${String(h).padStart(2, '0')}`,
    url: `http://host${h}:8001`,
    api_key: 'k',
    status: 'online',
    created_at: '2026-08-13T10:00:00Z',
    instances: Array.from({ length: instancesPerHost(h) }, (_, i) =>
      instance(`h${h}-i${i}`, `model-${i}`, `model-${i % 4}:7b`),
    ),
  })) as HostWithInstances[];
}

function graphFor(hosts: HostWithInstances[], expandAll = true): FlowGraph {
  return buildFlowGraph({
    hosts,
    requests: [],
    endpoints: [
      { id: 'e1', name: 'prod' },
      { id: 'e2', name: 'dev' },
    ],
    getInstanceState: () => null,
    expandAll,
    // Past the cap the graph rolls hosts up, and the layout would never see a
    // wide fan-out at all.
    showAllHosts: new Set(Array.from({ length: 30 }, (_, i) => `model-${i}`)),
  });
}

interface Rect {
  id: string;
  left: number;
  top: number;
  right: number;
  bottom: number;
}

function rects(graph: FlowGraph): Rect[] {
  const { positions } = layoutGraph(graph);
  return graph.nodes.map((node) => {
    const position = positions.get(node.id)!;
    return {
      id: node.id,
      left: position.x,
      top: position.y,
      right: position.x + node.width,
      bottom: position.y + node.height,
    };
  });
}

function overlaps(a: Rect, b: Rect): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

function firstOverlap(boxes: Rect[]): [Rect, Rect] | null {
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      if (overlaps(boxes[i], boxes[j])) return [boxes[i], boxes[j]];
    }
  }
  return null;
}

describe('layoutGraph', () => {
  it('places every node', () => {
    const graph = graphFor(fleet(3, () => 2));
    const { positions } = layoutGraph(graph);

    expect(positions.size).toBe(graph.nodes.length);
  });

  // The regression that motivated the rewrite: the old graph advanced a y
  // cursor by a guessed row height, so any host with more instances than the
  // guess allowed for pushed its neighbours' boxes into each other.
  it.each([
    ['a uniform fleet', fleet(6, () => 3)],
    ['uneven fan-out', fleet(8, (i) => (i % 3 === 0 ? 7 : 1))],
    ['a single crowded host', fleet(4, (i) => (i === 0 ? 24 : 1))],
    ['a fleet-sized graph', fleet(50, (i) => (i % 5) + 1)],
  ])('never overlaps two nodes with %s', (_label, hosts) => {
    expect(firstOverlap(rects(graphFor(hosts)))).toBeNull();
  });

  it('never overlaps with every model collapsed either', () => {
    expect(
      firstOverlap(
        rects(
          graphFor(
            fleet(20, () => 4),
            false,
          ),
        ),
      ),
    ).toBeNull();
  });

  it('orders the columns endpoints, gateway, model, host', () => {
    const graph = graphFor(fleet(3, () => 2));
    const { positions } = layoutGraph(graph);
    const columnOf = (kind: string) =>
      Math.min(...graph.nodes.filter((node) => node.kind === kind).map((node) => positions.get(node.id)!.x));

    expect(columnOf('endpoint')).toBeLessThan(columnOf('gateway'));
    expect(columnOf('gateway')).toBeLessThan(columnOf('model'));
    expect(columnOf('model')).toBeLessThan(columnOf('host'));
  });

  it('keeps a small fan-out in a single column', () => {
    const graph = graphFor(fleet(6, () => 3));
    const { positions } = layoutGraph(graph);
    const xs = new Set(graph.nodes.filter((node) => node.kind === 'host').map((node) => positions.get(node.id)!.x));

    expect(xs.size).toBe(1);
  });

  // One model on fifty hosts is a 4000px column; wrapping keeps the block
  // roughly screen-shaped, which is the difference between readable and not.
  it('wraps a wide fan-out into further columns', () => {
    const graph = graphFor(fleet(50, () => 1));
    const { positions, height } = layoutGraph(graph);
    const hosts = graph.nodes.filter((node) => node.kind === 'host');
    const xs = new Set(hosts.map((node) => positions.get(node.id)!.x));

    expect(xs.size).toBeGreaterThan(1);
    expect(height).toBeLessThan(hosts.length * NODE_SIZE.host.height);
  });

  it('gives each model its own band, so fan-outs never interleave', () => {
    const graph = graphFor(fleet(4, () => 3));
    const { positions } = layoutGraph(graph);
    const bandOf = (alias: string) => {
      const own = graph.nodes.filter((node) => node.id.startsWith(`host:${alias}:`));
      const tops = own.map((node) => positions.get(node.id)!.y);
      return [Math.min(...tops), Math.max(...tops)];
    };

    const [firstTop, firstBottom] = bandOf('model-0');
    const [secondTop] = bandOf('model-1');
    expect(firstBottom).toBeLessThan(secondTop);
    expect(firstTop).toBeLessThan(firstBottom);
  });

  it('centres a model against the hosts it feeds', () => {
    const graph = graphFor(fleet(5, () => 1));
    const { positions } = layoutGraph(graph);
    const model = graph.nodes.find((node) => node.kind === 'model')!;
    const hosts = graph.nodes.filter((node) => node.kind === 'host');
    const centre = (id: string, height: number) => positions.get(id)!.y + height / 2;
    const hostCentres = hosts.map((node) => centre(node.id, node.height));

    expect(centre(model.id, model.height)).toBeCloseTo((Math.min(...hostCentres) + Math.max(...hostCentres)) / 2, 5);
  });

  it('reports bounds that contain every node', () => {
    const graph = graphFor(fleet(12, () => 3));
    const { positions, width, height } = layoutGraph(graph);

    for (const node of graph.nodes) {
      const position = positions.get(node.id)!;
      expect(position.x + node.width).toBeLessThanOrEqual(width);
      expect(position.y + node.height).toBeLessThanOrEqual(height);
    }
  });

  it('frames the whole graph by default', () => {
    const graph = graphFor(fleet(4, () => 2));
    const layout = layoutGraph(graph);
    const bounds = boundsOf(graph, layout);

    for (const node of graph.nodes) {
      const position = layout.positions.get(node.id)!;
      expect(position.x).toBeGreaterThanOrEqual(bounds.x);
      expect(position.y).toBeGreaterThanOrEqual(bounds.y);
      expect(position.x + node.width).toBeLessThanOrEqual(bounds.x + bounds.width);
      expect(position.y + node.height).toBeLessThanOrEqual(bounds.y + bounds.height);
    }
  });

  it('frames a subset tightly, so tracing can zoom to the path', () => {
    const graph = graphFor(fleet(20, () => 2));
    const layout = layoutGraph(graph);
    const gateway = graph.nodes.find((node) => node.kind === 'gateway')!;
    const bounds = boundsOf(graph, layout, [gateway.id]);

    expect(bounds).toMatchObject({ width: gateway.width, height: gateway.height });
    expect(bounds.height).toBeLessThan(boundsOf(graph, layout).height);
  });

  it('grows a small frame rather than zooming past life size', () => {
    const tight = { x: 100, y: 100, width: 200, height: 100 };
    const grown = atLeast(tight, { width: 1000, height: 700 }, 1);

    expect(grown).toMatchObject({ width: 1000, height: 700 });
    // Grown around the middle, so the subject stays where the eye expects it.
    expect(grown.x + grown.width / 2).toBe(tight.x + tight.width / 2);
    expect(grown.y + grown.height / 2).toBe(tight.y + tight.height / 2);
  });

  it('leaves a frame that is already large alone', () => {
    const wide = { x: 0, y: 0, width: 4000, height: 3000 };

    expect(atLeast(wide, { width: 1000, height: 700 }, 1)).toEqual(wide);
  });

  it('keeps nodes at the size the renderer will draw', () => {
    const graph = graphFor(fleet(2, () => 2));

    for (const node of graph.nodes) {
      expect({ width: node.width, height: node.height }).toEqual(NODE_SIZE[node.kind]);
    }
  });
});
