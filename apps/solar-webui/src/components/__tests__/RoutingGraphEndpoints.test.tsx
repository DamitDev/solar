import { render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import solarClient from '@/api/client';
import { ApiEndpoint } from '@/api/types';

const eventStream = {
  getInstanceState: () => null,
  endpoints: [] as ApiEndpoint[],
  isConnected: true,
};

vi.mock('reactflow', () => ({
  __esModule: true,
  default: () => <div data-testid="react-flow" />,
  Controls: () => null,
  MiniMap: () => null,
  useNodesState: () => [[], vi.fn(), vi.fn()],
  useEdgesState: () => [[], vi.fn(), vi.fn()],
  MarkerType: { ArrowClosed: 'arrowclosed' },
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
}));
vi.mock('reactflow/dist/style.css', () => ({}));
vi.mock('@/context/RoutingEventsContext', () => ({
  useRoutingEventsContext: () => ({ requests: new Map(), removeRequest: vi.fn() }),
}));
vi.mock('@/context/EventStreamContext', () => ({
  useEventStreamContext: () => eventStream,
}));
vi.mock('@/hooks/useInstances', () => ({
  useInstances: () => ({ hosts: [], loading: false }),
}));

const endpoint = (id: string): ApiEndpoint =>
  ({ id, path: `/v1/${id}`, name: id, enabled: true }) as unknown as ApiEndpoint;

describe('RoutingGraph endpoint bootstrap', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    eventStream.endpoints = [];
    eventStream.isConnected = true;
  });

  it('fetches endpoints over REST when the socket is connected but has no events', async () => {
    // endpoints_update only fires on endpoint CRUD, so a connected socket with
    // an empty event array must still bootstrap — otherwise the graph stays
    // empty until someone edits an endpoint.
    const getEndpoints = vi.spyOn(solarClient, 'getEndpoints').mockResolvedValue([endpoint('chat')]);

    const { RoutingGraph } = await import('@/components/RoutingGraph');
    render(<RoutingGraph />);

    await waitFor(() => expect(getEndpoints).toHaveBeenCalled());
  });

  it('prefers event endpoints over a REST fetch', async () => {
    eventStream.endpoints = [endpoint('embeddings')];
    const getEndpoints = vi.spyOn(solarClient, 'getEndpoints').mockResolvedValue([]);

    const { RoutingGraph } = await import('@/components/RoutingGraph');
    render(<RoutingGraph />);

    await waitFor(() => expect(getEndpoints).not.toHaveBeenCalled());
  });
});
