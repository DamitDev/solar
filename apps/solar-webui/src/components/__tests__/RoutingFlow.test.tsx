import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import solarClient from '@/api/client';
import { ApiEndpoint, HostWithInstances, Instance } from '@/api/types';
import { RequestState } from '@/hooks/useEventStream';

/**
 * React Flow needs a measured container, which jsdom cannot give it, so the
 * canvas is stubbed and the assertions run against the node and edge lists the
 * page hands it. Layout itself is covered in routing/__tests__/layout.test.ts.
 */
vi.mock('reactflow', () => ({
  __esModule: true,
  default: ({
    nodes,
    edges,
    onNodeClick,
  }: {
    nodes: { id: string; data: Record<string, unknown> }[];
    edges: unknown[];
    onNodeClick: (event: unknown, node: unknown) => void;
  }) => (
    <div
      data-testid="react-flow"
      data-nodes={nodes.map((node) => node.id).join(',')}
      data-edges={String(edges.length)}
      data-dimmed={nodes
        .filter((node) => node.data.dimmed)
        .map((node) => node.id)
        .join(',')}
    >
      {nodes.map((node) => (
        <button key={node.id} data-testid={`node-${node.id}`} onClick={() => onNodeClick(null, node)}>
          {node.id}
        </button>
      ))}
    </div>
  ),
  Background: () => null,
  BackgroundVariant: { Dots: 'dots' },
  Controls: () => null,
  MiniMap: () => null,
  Handle: () => null,
  Position: { Left: 'left', Right: 'right' },
  MarkerType: { ArrowClosed: 'arrowclosed' },
  ReactFlowProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReactFlow: () => ({ fitBounds: vi.fn() }),
  useStore: (selector: (state: { width: number; height: number }) => number) => selector({ width: 1000, height: 700 }),
}));
vi.mock('reactflow/dist/style.css', () => ({}));

const eventStream = {
  getInstanceState: () => null,
  endpoints: [] as ApiEndpoint[],
  isConnected: true,
};

const routingEvents = {
  requests: new Map<string, RequestState>(),
  removeRequest: vi.fn(),
};

const instancesHook = {
  hosts: [] as HostWithInstances[],
  loading: false,
};

vi.mock('@/context/EventStreamContext', () => ({
  useEventStreamContext: () => eventStream,
}));
vi.mock('@/context/RoutingEventsContext', () => ({
  useRoutingEventsContext: () => routingEvents,
}));
vi.mock('@/hooks/useInstances', () => ({
  useInstances: () => instancesHook,
}));

const endpoint = (id: string, name = id): ApiEndpoint =>
  ({ id, path: `/v1/${id}`, name, enabled: true }) as unknown as ApiEndpoint;

function instance(id: string, alias: string, model = alias, status: Instance['status'] = 'running') {
  return {
    id,
    status,
    retry_count: 0,
    created_at: '2026-08-13T10:00:00Z',
    config: { backend_type: 'llamacpp', alias, model, host: 'h' },
  } as unknown as Instance;
}

function host(id: string, name: string, instances: Instance[]): HostWithInstances {
  return {
    id,
    name,
    url: `http://${name}:8001`,
    api_key: 'k',
    status: 'online',
    created_at: '2026-08-13T10:00:00Z',
    instances,
  } as HostWithInstances;
}

function liveRequest(overrides: Partial<RequestState> = {}): RequestState {
  return {
    request_id: 'r1',
    status: 'processing',
    model: 'chat',
    endpoint_id: 'e1',
    timestamp: '2026-08-13T12:00:00Z',
    ...overrides,
  };
}

async function renderPage() {
  const { RoutingFlow } = await import('@/components/RoutingFlow');
  return render(<RoutingFlow />);
}

const canvas = () => screen.getByTestId('react-flow');
const nodeIds = () => (canvas().getAttribute('data-nodes') ?? '').split(',').filter(Boolean);
const dimmedIds = () => (canvas().getAttribute('data-dimmed') ?? '').split(',').filter(Boolean);
const clickNode = (id: string) => fireEvent.click(screen.getByTestId(`node-${id}`));

beforeEach(() => {
  vi.restoreAllMocks();
  eventStream.endpoints = [];
  eventStream.isConnected = true;
  routingEvents.requests = new Map();
  instancesHook.hosts = [
    host('h1', 'alpha', [instance('i1', 'chat', 'qwen3.6:35b'), instance('i2', 'embed', 'iris:110m')]),
    host('h2', 'beta', [instance('i3', 'chat', 'qwen3.6:35b')]),
  ];
  instancesHook.loading = false;
  vi.spyOn(solarClient, 'getEndpoints').mockResolvedValue([endpoint('e1', 'prod'), endpoint('e2', 'dev')]);
});

describe('RoutingFlow endpoints', () => {
  it('fetches endpoints over REST when the socket is connected but has no events', async () => {
    // endpoints_update only fires on endpoint CRUD, so a connected socket with
    // no event data must still bootstrap or the graph has no entry points.
    await renderPage();

    await waitFor(() => expect(solarClient.getEndpoints).toHaveBeenCalled());
    await waitFor(() => expect(nodeIds()).toContain('endpoint:e1'));
  });

  it('prefers event endpoints over a REST fetch', async () => {
    eventStream.endpoints = [endpoint('e9', 'stream')];
    await renderPage();

    await waitFor(() => expect(nodeIds()).toContain('endpoint:e9'));
    expect(solarClient.getEndpoints).not.toHaveBeenCalled();
  });
});

describe('RoutingFlow shape', () => {
  it('shows endpoints, the gateway and the models, and no hosts yet', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('endpoint:e1'));

    expect(nodeIds()).toEqual(['endpoint:e1', 'endpoint:e2', 'gateway', 'model:chat', 'model:embed']);
  });

  it('fans a model out to its hosts when it is opened, and back when closed', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    clickNode('model:chat');
    expect(nodeIds()).toEqual(expect.arrayContaining(['model:chat', 'host:chat:h1', 'host:chat:h2', 'model:embed']));
    // Opening one model must not drag the rest of the fleet on screen.
    expect(nodeIds()).not.toContain('host:embed:h1');

    clickNode('model:chat');
    expect(nodeIds().some((id) => id.startsWith('host:'))).toBe(false);
  });

  it('opens everything on request', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));

    expect(nodeIds()).toEqual(expect.arrayContaining(['host:chat:h1', 'host:chat:h2', 'host:embed:h1']));
  });

  it('collapses back to the models', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));

    fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));

    expect(nodeIds().some((id) => id.startsWith('host:'))).toBe(false);
  });

  it('opens matching models while a search is active, since the answer is already narrow', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    fireEvent.change(screen.getByLabelText('Search models and hosts'), { target: { value: 'embed' } });

    expect(nodeIds()).toEqual(['endpoint:e1', 'endpoint:e2', 'gateway', 'model:embed', 'host:embed:h1']);
  });

  it('searches by host name too', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    fireEvent.change(screen.getByLabelText('Search models and hosts'), { target: { value: 'beta' } });

    expect(nodeIds()).toContain('host:chat:h2');
    expect(nodeIds()).not.toContain('host:chat:h1');
  });

  it('hides instances that are not running when asked', async () => {
    instancesHook.hosts = [host('h1', 'alpha', [instance('i1', 'chat'), instance('i2', 'dead', 'dead', 'stopped')])];
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:dead'));

    fireEvent.click(screen.getByRole('button', { name: 'Running only' }));

    expect(nodeIds()).not.toContain('model:dead');
    expect(nodeIds()).toContain('model:chat');
  });

  it('reports how much of the fleet a filter is hiding', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    fireEvent.change(screen.getByLabelText('Search models and hosts'), { target: { value: 'embed' } });

    expect(screen.getByText('Shown').parentElement).toHaveTextContent('1/3');
  });

  it('adds no nodes when traffic arrives', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('gateway'));
    const before = nodeIds().length;
    const edges = canvas().getAttribute('data-edges');

    routingEvents.requests = new Map([
      ['r1', liveRequest({ host_id: 'h1', instance_id: 'i1' })],
      ['r2', liveRequest({ request_id: 'r2', host_id: 'h1', instance_id: 'i1' })],
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Running only' }));
    fireEvent.click(screen.getByRole('button', { name: 'Running only' }));

    expect(nodeIds()).toHaveLength(before);
    expect(canvas().getAttribute('data-edges')).toBe(edges);
  });
});

describe('RoutingFlow tracing', () => {
  it('dims everything off the path when a box is picked', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    clickNode('model:chat');

    expect(dimmedIds()).toEqual(['model:embed']);
  });

  it('clears the trace from the pane', async () => {
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));
    clickNode('model:chat');

    fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));

    expect(dimmedIds()).toEqual([]);
  });

  it('follows a live request down to the host it landed on', async () => {
    routingEvents.requests = new Map([['r1', liveRequest({ host_id: 'h1', instance_id: 'i1' })]]);
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    fireEvent.click(within(screen.getByTestId('request-ticker')).getByText('chat'));

    // Selecting a request opens the model it is using, so the last hop exists.
    expect(nodeIds()).toContain('host:chat:h1');
    const dimmed = dimmedIds();
    expect(dimmed).not.toContain('endpoint:e1');
    expect(dimmed).not.toContain('gateway');
    expect(dimmed).not.toContain('model:chat');
    expect(dimmed).not.toContain('host:chat:h1');
    expect(dimmed).toContain('host:chat:h2');
    expect(dimmed).toContain('endpoint:e2');
  });

  it('stops at the model for a request that is still queued', async () => {
    routingEvents.requests = new Map([['r1', liveRequest({ status: 'pending' })]]);
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));

    fireEvent.click(within(screen.getByTestId('request-ticker')).getByText('chat'));

    expect(nodeIds().some((id) => id.startsWith('host:'))).toBe(false);
    expect(dimmedIds()).toContain('model:embed');
  });

  it('drops the trace when the request it followed disappears', async () => {
    routingEvents.requests = new Map([['r1', liveRequest({ host_id: 'h1', instance_id: 'i1' })]]);
    const { rerender } = await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:chat'));
    fireEvent.click(within(screen.getByTestId('request-ticker')).getByText('chat'));
    expect(dimmedIds().length).toBeGreaterThan(0);

    routingEvents.requests = new Map();
    const { RoutingFlow } = await import('@/components/RoutingFlow');
    rerender(<RoutingFlow />);

    await waitFor(() => expect(dimmedIds()).toEqual([]));
  });
});

describe('RoutingFlow at fleet scale', () => {
  const fleet = () =>
    Array.from({ length: 50 }, (_, h) =>
      host(
        `h${h}`,
        `host${String(h).padStart(2, '0')}`,
        Array.from({ length: 4 }, (_, i) => instance(`h${h}-i${i}`, `model-${i}`)),
      ),
    );

  it('stays at one box per model until a model is opened', async () => {
    instancesHook.hosts = fleet();

    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('gateway'));

    // 2 endpoints + gateway + 4 models, out of 200 instances.
    expect(nodeIds()).toHaveLength(7);
  });

  it('caps a fan-out and rolls the rest into one box', async () => {
    instancesHook.hosts = fleet();
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:model-0'));

    clickNode('model:model-0');

    // 8 hosts and a rollup, not fifty boxes.
    expect(nodeIds().filter((id) => id.startsWith('host:'))).toHaveLength(8);
    expect(nodeIds()).toContain('more:model-0');
  });

  it('shows the rest of the hosts when the rollup is picked', async () => {
    instancesHook.hosts = fleet();
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:model-0'));
    clickNode('model:model-0');

    clickNode('more:model-0');

    expect(nodeIds().filter((id) => id.startsWith('host:'))).toHaveLength(50);
    expect(nodeIds()).not.toContain('more:model-0');
  });

  it('keeps a busy host out of the rollup', async () => {
    instancesHook.hosts = fleet();
    // host49 sorts last, so it is only drawn because it is working.
    routingEvents.requests = new Map([
      ['r1', liveRequest({ model: 'model-0', host_id: 'h49', instance_id: 'h49-i0' })],
    ]);
    await renderPage();
    await waitFor(() => expect(nodeIds()).toContain('model:model-0'));

    clickNode('model:model-0');

    expect(nodeIds()).toContain('host:model-0:h49');
  });

  it('shows a loading state until the topology arrives', async () => {
    instancesHook.loading = true;
    await renderPage();

    expect(screen.getByText('Loading routing flow...')).toBeInTheDocument();
    expect(screen.queryByTestId('react-flow')).not.toBeInTheDocument();
  });
});
