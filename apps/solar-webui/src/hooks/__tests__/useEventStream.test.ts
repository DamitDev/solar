import { renderHook, act } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// ---- mocks ---------------------------------------------------------------

let socketHandlers: Record<string, ((...args: any[]) => void)[]> = {};
let connectCallback: (() => void) | null = null;

const emit = (eventName: string, payload: any) => {
  for (const cb of socketHandlers[eventName] ?? []) cb(payload);
};

const triggerConnect = () => {
  connectCallback?.();
};

vi.mock('socket.io-client', () => ({
  io: vi.fn(
    () =>
      ({
        on: vi.fn((event: string, cb: any) => {
          if (event === 'connect') connectCallback = cb;
          else (socketHandlers[event] ??= []).push(cb);
        }),
        emit: vi.fn(),
        connected: false,
        disconnect: vi.fn(() => (socketHandlers = {})),
        removeAllListeners: vi.fn(() => (socketHandlers = {})),
      }) as any,
  ),
}));

vi.mock('@/api/client', () => ({
  default: {
    getControlSocketIOUrl: () => 'http://control',
    getSocketIOPath: () => '/socket.io',
    getManagementApiKey: () => 'key',
  },
}));

// ---- the unit under test -------------------------------------------------

import { useEventStream } from '@/hooks/eventStream/useEventStream';

function snapshotPayload(overrides: Partial<any> = {}) {
  return {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    active_requests: [
      { request_id: 'req-running', host_id: 'h1', instance_id: 'i1', status: 'processing' },
      { request_id: 'req-queued', status: 'queued' },
    ],
    instance_states: [{ host_id: 'h1', instance_id: 'i1', data: { busy: true, phase: 'running', active_slots: 1 } }],
    endpoints: [{ id: 'ep1', name: 'Default' }],
    ...overrides,
  };
}

describe('useEventStream snapshot consumer (US-006)', () => {
  beforeEach(() => {
    socketHandlers = {};
    connectCallback = null;
    vi.clearAllMocks();
  });

  it('resets the routing maps on connect and gates deltas until the snapshot lands', () => {
    const { result } = renderHook(() => useEventStream());
    // Deltas after connect but before snapshot must be dropped.
    act(() => {
      triggerConnect();
    });
    // A request delta that races the connect while awaiting the snapshot.
    act(() => {
      emit('request_start', { request_id: 'stale', timestamp: new Date().toISOString() });
      emit('instance_state', {
        host_id: 'h1',
        instance_id: 'i1',
        data: { busy: true, phase: 'running', active_slots: 1 },
      });
    });
    expect(result.current.requests.get('stale')).toBeUndefined();
    expect(result.current.instanceStates.get('h1:i1')).toBeUndefined();
  });

  it('applies the snapshot wholesale and then resumes processing deltas', () => {
    const { result } = renderHook(() => useEventStream());
    act(() => {
      triggerConnect();
    });
    act(() => {
      emit('routing_snapshot', snapshotPayload());
    });
    expect(result.current.requests.get('req-running')).toEqual(
      expect.objectContaining({ request_id: 'req-running', status: 'processing' }),
    );
    expect(result.current.requests.get('req-queued')).toEqual(
      expect.objectContaining({ request_id: 'req-queued', status: 'pending' }),
    );
    expect(result.current.instanceStates.get('h1:i1')).toEqual(
      expect.objectContaining({ busy: true, phase: 'running' }),
    );
    expect(result.current.endpoints).toEqual([{ id: 'ep1', name: 'Default' }]);

    // A delta arriving after the snapshot is applied (gating cleared).
    act(() => {
      emit('request_start', { request_id: 'fresh', timestamp: new Date().toISOString() });
      emit('request_routed', {
        request_id: 'fresh',
        host_id: 'h2',
        instance_id: 'i2',
        resolved_model: 'm',
      });
    });
    expect(result.current.requests.get('fresh')?.status).toBe('processing');
    expect(result.current.requests.get('fresh')?.host_id).toBe('h2');
  });
});

describe('useEventStream handler registry', () => {
  beforeEach(() => {
    socketHandlers = {};
    connectCallback = null;
    vi.clearAllMocks();
  });

  it('routes events through a consumer-registered handler via registerHandler', () => {
    const custom = vi.fn();
    const { result } = renderHook(() => useEventStream());
    act(() => {
      triggerConnect();
      // Override a bound built-in event with a consumer handler.
      result.current.registerHandler('host_status', custom);
    });
    act(() => {
      emit('host_status', { host_id: 'h1', status: 'online' });
    });
    expect(custom).toHaveBeenCalledTimes(1);
    expect(custom.mock.calls[0][0]).toMatchObject({ type: 'host_status', data: { host_id: 'h1' } });
  });

  it('no-ops without crashing for an event type with no registered handler', () => {
    const { result } = renderHook(() => useEventStream());
    act(() => {
      triggerConnect();
    });
    expect(() => {
      act(() => {
        emit('request_start', { request_id: 'never-gated' });
      });
    }).not.toThrow();
    expect(result.current.requests.get('never-gated')).toBeUndefined();
  });
});
