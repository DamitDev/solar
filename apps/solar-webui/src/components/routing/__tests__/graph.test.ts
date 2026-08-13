import { describe, expect, it } from 'vitest';
import {
  GATEWAY_NODE_ID,
  MAX_HOSTS_PER_MODEL,
  ModelNodeData,
  aliasOfRequest,
  buildFlowGraph,
  endpointNodeId,
  hostNodeId,
  modelNodeId,
  overflowNodeId,
  traceRequest,
  traceThrough,
} from '../graph';
import { HostWithInstances, Instance } from '@/api/types';
import { InstanceStateData, RequestState } from '@/hooks/useEventStream';

function instance(id: string, alias: string, model = alias, status: Instance['status'] = 'running') {
  return {
    id,
    status,
    retry_count: 0,
    created_at: '2026-08-13T10:00:00Z',
    config: { backend_type: 'llamacpp', alias, model, host: 'h' },
  } as unknown as Instance;
}

function host(id: string, name: string, instances: Instance[], status: HostWithInstances['status'] = 'online') {
  return {
    id,
    name,
    url: `http://${name}:8001`,
    api_key: 'k',
    status,
    created_at: '2026-08-13T10:00:00Z',
    instances,
  } as HostWithInstances;
}

function request(overrides: Partial<RequestState> = {}): RequestState {
  return {
    request_id: Math.random().toString(36).slice(2),
    status: 'processing',
    timestamp: '2026-08-13T12:00:00Z',
    ...overrides,
  };
}

// chat runs on both hosts, embed only on alpha.
const hosts = [
  host('h1', 'alpha', [instance('i1', 'chat', 'qwen3.6:35b'), instance('i2', 'embed', 'iris:110m')]),
  host('h2', 'beta', [instance('i3', 'chat', 'qwen3.6:35b')]),
];

const endpoints = [
  { id: 'e1', name: 'prod' },
  { id: 'e2', name: 'dev' },
];

const noState = () => null;

const build = (overrides: Partial<Parameters<typeof buildFlowGraph>[0]> = {}) =>
  buildFlowGraph({ hosts, requests: [], endpoints, getInstanceState: noState, ...overrides });

const kindCounts = (graph: ReturnType<typeof build>) =>
  graph.nodes.reduce<Record<string, number>>((acc, node) => {
    acc[node.kind] = (acc[node.kind] ?? 0) + 1;
    return acc;
  }, {});

const modelData = (graph: ReturnType<typeof build>, alias: string) =>
  graph.nodes.find((node) => node.id === modelNodeId(alias))!.data as ModelNodeData;

describe('buildFlowGraph', () => {
  it('draws endpoints, the gateway and one node per model', () => {
    expect(kindCounts(build())).toEqual({ endpoint: 2, gateway: 1, model: 2 });
  });

  it('leaves the hosts out until a model is opened', () => {
    // 50 hosts across a few models is a hairball; the whole point of the
    // collapsed default is that the picture never contains every pairing.
    expect(kindCounts(build({ expanded: new Set(['chat']) }))).toEqual({
      endpoint: 2,
      gateway: 1,
      model: 2,
      host: 2,
    });
  });

  it('fans an opened model out to exactly its own hosts', () => {
    const graph = build({ expanded: new Set(['embed']) });

    expect(graph.nodes.filter((node) => node.kind === 'host').map((node) => node.id)).toEqual([
      hostNodeId('embed', 'h1'),
    ]);
  });

  it('chains endpoint to gateway to model to host', () => {
    const ids = build({ expanded: new Set(['chat']) }).edges.map((edge) => edge.id);

    expect(ids).toContain(`${endpointNodeId('e1')}->${GATEWAY_NODE_ID}`);
    expect(ids).toContain(`${GATEWAY_NODE_ID}->${modelNodeId('chat')}`);
    expect(ids).toContain(`${modelNodeId('chat')}->${hostNodeId('chat', 'h2')}`);
  });

  it('opens every model when asked', () => {
    expect(kindCounts(build({ expandAll: true }))).toMatchObject({ host: 3 });
  });

  it('sorts models by alias and hosts by name', () => {
    expect(build().aliases).toEqual(['chat', 'embed']);
    expect(
      build({ expanded: new Set(['chat']) })
        .nodes.filter((node) => node.kind === 'host')
        .map((node) => (node.data as { hostName: string }).hostName),
    ).toEqual(['alpha', 'beta']);
  });

  it('summarises a model over the hosts serving it', () => {
    expect(modelData(build(), 'chat')).toMatchObject({
      alias: 'chat',
      model: 'qwen3.6:35b',
      hosts: 2,
      instances: 2,
      running: 2,
    });
  });

  it('drops the underlying model when the alias already says it', () => {
    const graph = buildFlowGraph({
      hosts: [host('h1', 'alpha', [instance('i1', 'qwen3.6:35b')])],
      requests: [],
      endpoints,
      getInstanceState: noState,
    });

    expect(modelData(graph, 'qwen3.6:35b').model).toBeNull();
  });

  it('gives every node the size the layout will reserve for it', () => {
    expect(build({ expandAll: true }).nodes.every((node) => node.width > 0 && node.height > 0)).toBe(true);
  });

  it('keeps the node count flat as traffic arrives, and moves it onto the edges', () => {
    const idle = build({ expanded: new Set(['chat']) });
    const busy = build({
      expanded: new Set(['chat']),
      requests: [
        request({ endpoint_id: 'e1', host_id: 'h1', instance_id: 'i1' }),
        request({ endpoint_id: 'e1', host_id: 'h1', instance_id: 'i1' }),
        request({ endpoint_id: 'e2', host_id: 'h2', instance_id: 'i3' }),
      ],
    });

    expect(busy.nodes).toHaveLength(idle.nodes.length);
    expect(busy.edges).toHaveLength(idle.edges.length);

    expect(busy.edges.find((edge) => edge.target === hostNodeId('chat', 'h1'))!.inFlight).toBe(2);
    expect(busy.edges.find((edge) => edge.source === endpointNodeId('e1'))!.inFlight).toBe(2);
    expect(busy.edges.find((edge) => edge.target === modelNodeId('chat'))!.inFlight).toBe(3);
  });

  it('places a queued request on its model before it has a host', () => {
    const graph = build({
      requests: [request({ status: 'pending', endpoint_id: 'e1', model: 'chat' })],
    });

    expect(graph.nodes.find((node) => node.id === GATEWAY_NODE_ID)!.data).toMatchObject({ queued: 1, processing: 0 });
    expect(graph.edges.find((edge) => edge.target === modelNodeId('chat'))!.inFlight).toBe(1);
  });

  it('marks a hop carrying failures as an error, not as traffic', () => {
    const graph = build({
      expanded: new Set(['chat']),
      requests: [request({ status: 'error', endpoint_id: 'e1', host_id: 'h1', instance_id: 'i1' })],
    });

    const edge = graph.edges.find((candidate) => candidate.target === hostNodeId('chat', 'h1'))!;
    expect(edge).toMatchObject({ errors: 1, inFlight: 0, tone: 'error' });
  });

  it('separates a model with no running instance from one that is simply quiet', () => {
    const graph = buildFlowGraph({
      hosts: [host('h1', 'alpha', [instance('i1', 'chat'), instance('i2', 'dead', 'dead', 'failed')])],
      requests: [],
      endpoints,
      getInstanceState: noState,
    });

    expect(graph.edges.find((edge) => edge.target === modelNodeId('chat'))!.tone).toBe('ready');
    expect(graph.edges.find((edge) => edge.target === modelNodeId('dead'))!.tone).toBe('idle');
  });

  it('collapses several instances of one model on one host into a single hop', () => {
    const graph = buildFlowGraph({
      hosts: [host('h1', 'alpha', [instance('i1', 'chat'), instance('i2', 'chat')])],
      requests: [],
      endpoints,
      getInstanceState: noState,
      expandAll: true,
    });

    expect(graph.nodes.filter((node) => node.kind === 'host')).toHaveLength(1);
    expect(graph.edges.find((edge) => edge.target === hostNodeId('chat', 'h1'))!.multiplicity).toBe(2);
  });

  it('reports the worst instance status on the host, so a failure is visible', () => {
    const graph = buildFlowGraph({
      hosts: [host('h1', 'alpha', [instance('i1', 'chat'), instance('i2', 'chat', 'chat', 'failed')])],
      requests: [],
      endpoints,
      getInstanceState: noState,
      expandAll: true,
    });

    expect(graph.nodes.find((node) => node.kind === 'host')!.data).toMatchObject({ status: 'failed', running: 1 });
  });

  it('prefers a busy instance when reporting host state', () => {
    const graph = buildFlowGraph({
      hosts: [host('h1', 'alpha', [instance('i1', 'chat'), instance('i2', 'chat')])],
      requests: [],
      endpoints,
      getInstanceState: (_h, id) => ({ busy: id === 'i2', decode_tps: id === 'i2' ? 30 : 0 }) as InstanceStateData,
      expandAll: true,
    });

    expect(graph.nodes.find((node) => node.kind === 'host')!.data).toMatchObject({
      state: { busy: true, decode_tps: 30 },
    });
  });

  it('drops models that filtering emptied', () => {
    const graph = build({ search: 'embed' });

    expect(graph.aliases).toEqual(['embed']);
    expect(graph.instanceCount).toBe(1);
  });
});

describe('buildFlowGraph fan-out cap', () => {
  const wide = Array.from({ length: 30 }, (_, i) =>
    host(`h${i}`, `host${String(i).padStart(2, '0')}`, [instance(`i${i}`, 'chat')]),
  );

  const buildWide = (overrides: Partial<Parameters<typeof buildFlowGraph>[0]> = {}) =>
    buildFlowGraph({
      hosts: wide,
      requests: [],
      endpoints,
      getInstanceState: noState,
      expandAll: true,
      ...overrides,
    });

  it('draws a capped number of hosts and rolls the rest into one node', () => {
    const graph = buildWide();

    expect(graph.nodes.filter((node) => node.kind === 'host')).toHaveLength(MAX_HOSTS_PER_MODEL);
    expect(graph.nodes.find((node) => node.id === overflowNodeId('chat'))!.data).toMatchObject({
      hosts: 30 - MAX_HOSTS_PER_MODEL,
      running: 30 - MAX_HOSTS_PER_MODEL,
    });
  });

  it('does not roll up a fan-out that already fits', () => {
    expect(build({ expandAll: true }).nodes.some((node) => node.kind === 'overflow')).toBe(false);
  });

  it('keeps the hosts that are failing or working, whatever their name', () => {
    // host29 sorts last, so it is only drawn because something is happening.
    const graph = buildWide({
      hosts: [...wide.slice(0, 29), host('h29', 'host29', [instance('i29', 'chat', 'chat', 'failed')])],
      requests: [request({ host_id: 'h28', instance_id: 'i28' })],
    });
    const drawn = graph.nodes.filter((node) => node.kind === 'host').map((node) => node.id);

    expect(drawn).toContain(hostNodeId('chat', 'h29'));
    expect(drawn).toContain(hostNodeId('chat', 'h28'));
  });

  it('still reads alphabetically once the interesting hosts are chosen', () => {
    const names = buildWide()
      .nodes.filter((node) => node.kind === 'host')
      .map((node) => (node.data as { hostName: string }).hostName);

    expect(names).toEqual([...names].sort());
  });

  it('carries the traffic of hosts that did not fit onto the rollup', () => {
    // More busy hosts than slots, so some genuinely busy ones roll up and
    // their load has to survive the summarising.
    const busy = Array.from({ length: MAX_HOSTS_PER_MODEL + 4 }, (_, i) =>
      request({ host_id: `h${i}`, instance_id: `i${i}` }),
    );
    const graph = buildWide({ requests: busy });
    const edge = graph.edges.find((candidate) => candidate.target === overflowNodeId('chat'))!;

    expect(edge).toMatchObject({ inFlight: 4, errors: 0, tone: 'active' });
    expect(graph.nodes.find((node) => node.id === overflowNodeId('chat'))!.data).toMatchObject({ inFlight: 4 });
  });

  it('shows every host when asked for one model only', () => {
    const graph = buildFlowGraph({
      hosts: [...wide, host('hx', 'other', [instance('ix', 'embed')])],
      requests: [],
      endpoints,
      getInstanceState: noState,
      expandAll: true,
      showAllHosts: new Set(['chat']),
    });

    expect(graph.nodes.filter((node) => node.kind === 'host')).toHaveLength(31);
    expect(graph.nodes.some((node) => node.kind === 'overflow')).toBe(false);
  });
});

describe('buildFlowGraph filtering', () => {
  it('narrows to the searched model', () => {
    const graph = build({ search: 'embed' });

    expect(graph.aliases).toEqual(['embed']);
    expect(graph.instanceCount).toBe(1);
  });
});

describe('traceThrough', () => {
  it('follows a path in both directions from a host', () => {
    const graph = build({ expanded: new Set(['chat']) });
    const trace = traceThrough(graph, hostNodeId('chat', 'h1'));

    expect(trace.nodes).toContain(modelNodeId('chat'));
    expect(trace.nodes).toContain(GATEWAY_NODE_ID);
    expect(trace.nodes).toContain(endpointNodeId('e1'));
    expect(trace.nodes.has(hostNodeId('chat', 'h2'))).toBe(false);
    expect(trace.nodes.has(modelNodeId('embed'))).toBe(false);
  });

  it('expands to everything downstream when the gateway is picked', () => {
    const graph = build({ expandAll: true });

    expect(traceThrough(graph, GATEWAY_NODE_ID).nodes.size).toBe(graph.nodes.length);
  });
});

describe('traceRequest', () => {
  it('highlights only the hops one request occupies', () => {
    const graph = build({ expanded: new Set(['chat']) });
    const trace = traceRequest(graph, request({ endpoint_id: 'e2', host_id: 'h2', instance_id: 'i3' }), 'chat');

    expect([...trace.nodes].sort()).toEqual(
      [endpointNodeId('e2'), GATEWAY_NODE_ID, modelNodeId('chat'), hostNodeId('chat', 'h2')].sort(),
    );
    expect(trace.edges.size).toBe(3);
  });

  it('stops at the model while the request is still queued', () => {
    const graph = build();
    const trace = traceRequest(graph, request({ status: 'pending', endpoint_id: 'e1', model: 'chat' }), 'chat');

    expect([...trace.nodes].sort()).toEqual([endpointNodeId('e1'), GATEWAY_NODE_ID, modelNodeId('chat')].sort());
  });

  it('skips the host hop while the model is closed', () => {
    const graph = build();
    const trace = traceRequest(graph, request({ endpoint_id: 'e1', host_id: 'h1', instance_id: 'i1' }), 'chat');

    expect(trace.nodes.has(hostNodeId('chat', 'h1'))).toBe(false);
    expect(trace.nodes.has(modelNodeId('chat'))).toBe(true);
  });

  it('ignores an endpoint the graph does not know', () => {
    const graph = build();
    const trace = traceRequest(graph, request({ endpoint_id: 'gone', host_id: 'h1', instance_id: 'i1' }), 'chat');

    expect(trace.nodes.has(GATEWAY_NODE_ID)).toBe(true);
    expect([...trace.nodes].some((id) => id.startsWith('endpoint:'))).toBe(false);
  });
});

describe('aliasOfRequest', () => {
  it('resolves a routed request through the instance it landed on', () => {
    const graph = build();

    expect(aliasOfRequest(graph, request({ host_id: 'h1', instance_id: 'i2' }))).toBe('embed');
  });

  it('falls back to the model name for a request that is not routed yet', () => {
    const graph = build();

    expect(aliasOfRequest(graph, request({ status: 'pending', resolved_model: 'chat' }))).toBe('chat');
  });

  it('returns nothing for a model the fleet no longer serves', () => {
    const graph = build();

    expect(aliasOfRequest(graph, request({ model: 'retired' }))).toBeNull();
  });
});
