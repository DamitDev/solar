import { describe, expect, it } from 'vitest';
import {
  InstanceCell,
  buildCells,
  cellKey,
  countInFlightByInstance,
  isActiveRequest,
  loadFraction,
  phaseLabel,
  summarizeFlow,
  tickerRequests,
} from '../workload';
import { HostWithInstances, Instance } from '@/api/types';
import { InstanceStateData, RequestState } from '@/hooks/useEventStream';

function instance(id: string, alias: string, model = 'qwen3.6:35b', status: Instance['status'] = 'running') {
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

const noState = () => null;

describe('isActiveRequest', () => {
  it.each([
    ['pending', true],
    ['routed', true],
    ['processing', true],
    ['success', false],
    ['error', false],
  ] as const)('treats %s as active=%s', (status, expected) => {
    expect(isActiveRequest(request({ status }))).toBe(expected);
  });

  it('ignores a request already fading out', () => {
    expect(isActiveRequest(request({ status: 'processing', removing: true }))).toBe(false);
  });
});

describe('countInFlightByInstance', () => {
  it('counts only unfinished requests, per instance', () => {
    const counts = countInFlightByInstance([
      request({ host_id: 'h1', instance_id: 'i1' }),
      request({ host_id: 'h1', instance_id: 'i1' }),
      request({ host_id: 'h1', instance_id: 'i2' }),
      request({ host_id: 'h1', instance_id: 'i1', status: 'success' }),
    ]);

    expect(counts.get(cellKey('h1', 'i1'))).toBe(2);
    expect(counts.get(cellKey('h1', 'i2'))).toBe(1);
  });

  it('keeps same-named instances on different hosts apart', () => {
    const counts = countInFlightByInstance([
      request({ host_id: 'h1', instance_id: 'i1' }),
      request({ host_id: 'h2', instance_id: 'i1' }),
    ]);

    expect(counts.get(cellKey('h1', 'i1'))).toBe(1);
    expect(counts.get(cellKey('h2', 'i1'))).toBe(1);
  });

  it('skips requests that have not been routed yet', () => {
    expect(countInFlightByInstance([request({ status: 'pending' })]).size).toBe(0);
  });
});

describe('buildCells', () => {
  const hosts = [
    host('h2', 'beta', [instance('i3', 'solver-v4:9b', 'solver-v4:9b')]),
    host('h1', 'alpha', [instance('i1', 'qwen-a'), instance('i2', 'solver-v4:9b', 'solver-v4:9b')]),
  ];

  it('sorts by alias then host, regardless of API order', () => {
    const cells = buildCells({ hosts, requests: [], getInstanceState: noState });

    expect(cells.map((c) => [c.alias, c.hostName])).toEqual([
      ['qwen-a', 'alpha'],
      ['solver-v4:9b', 'alpha'],
      ['solver-v4:9b', 'beta'],
    ]);
  });

  it('keeps the alias and the underlying model apart', () => {
    const cells = buildCells({
      hosts: [host('h1', 'alpha', [instance('i1', 'fast-chat', 'qwen3.6:35b')])],
      requests: [],
      getInstanceState: noState,
    });

    expect(cells[0]).toMatchObject({ alias: 'fast-chat', model: 'qwen3.6:35b' });
  });

  it('attaches in-flight counts to the instance holding the request', () => {
    const cells = buildCells({
      hosts,
      requests: [request({ host_id: 'h1', instance_id: 'i1' }), request({ host_id: 'h1', instance_id: 'i1' })],
      getInstanceState: noState,
    });

    expect(cells.find((c) => c.instanceId === 'i1')!.inFlight).toBe(2);
    expect(cells.find((c) => c.instanceId === 'i3')!.inFlight).toBe(0);
  });

  it('carries live instance state through', () => {
    const cells = buildCells({
      hosts: [host('h1', 'alpha', [instance('i1', 'a')])],
      requests: [],
      getInstanceState: () => ({ busy: true, active_slots: 2 }) as InstanceStateData,
    });

    expect(cells[0].state).toMatchObject({ busy: true, active_slots: 2 });
  });

  it('can hide everything that is not running', () => {
    const cells = buildCells({
      hosts: [host('h1', 'alpha', [instance('i1', 'a'), instance('i2', 'b', 'm', 'stopped')])],
      requests: [],
      getInstanceState: noState,
      runningOnly: true,
    });

    expect(cells.map((c) => c.alias)).toEqual(['a']);
  });

  it('searches across alias, model and host name', () => {
    const search = (q: string) =>
      buildCells({ hosts, requests: [], getInstanceState: noState, search: q }).map((c) => c.alias);

    expect(search('qwen')).toEqual(['qwen-a']);
    expect(search('beta')).toEqual(['solver-v4:9b']);
    expect(search('solver alpha')).toEqual(['solver-v4:9b']);
  });

  it('handles a host reporting no instances', () => {
    expect(buildCells({ hosts: [host('h1', 'a', [])], requests: [], getInstanceState: noState })).toEqual([]);
  });

  it('falls back to the instance id when config carries no name', () => {
    const bare = { id: 'i7', status: 'running', retry_count: 0, created_at: '', config: {} } as unknown as Instance;
    const cells = buildCells({ hosts: [host('h1', 'alpha', [bare])], requests: [], getInstanceState: noState });

    expect(cells[0].alias).toBe('i7');
  });
});

describe('summarizeFlow', () => {
  const hosts = [
    host('h1', 'alpha', [instance('i1', 'a'), instance('i2', 'b', 'm', 'stopped')]),
    host('h2', 'beta', [instance('i3', 'c')], 'offline'),
  ];

  it('separates pending from processing and errored', () => {
    const totals = summarizeFlow(
      hosts,
      [
        request({ status: 'pending' }),
        request({ status: 'processing' }),
        request({ status: 'routed' }),
        request({ status: 'error' }),
        request({ status: 'success' }),
      ],
      3,
    );

    expect(totals).toMatchObject({ pending: 1, processing: 2, errored: 1, endpoints: 3 });
  });

  it('counts hosts and instances against their totals', () => {
    const totals = summarizeFlow(hosts, [], 0);

    expect(totals).toMatchObject({
      hostsOnline: 1,
      hostsTotal: 2,
      instancesRunning: 2,
      instancesTotal: 3,
    });
  });

  it('ignores requests that are fading out', () => {
    expect(summarizeFlow(hosts, [request({ status: 'processing', removing: true })], 0).processing).toBe(0);
  });
});

describe('tickerRequests', () => {
  it('shows active and errored requests, newest first', () => {
    const items = tickerRequests([
      request({ request_id: 'old', timestamp: '2026-08-13T11:00:00Z' }),
      request({ request_id: 'new', timestamp: '2026-08-13T12:00:00Z' }),
      request({ request_id: 'failed', status: 'error', timestamp: '2026-08-13T11:30:00Z' }),
      request({ request_id: 'done', status: 'success', timestamp: '2026-08-13T12:30:00Z' }),
    ]);

    expect(items.map((r) => r.request_id)).toEqual(['new', 'failed', 'old']);
  });

  it('caps the list', () => {
    const many = Array.from({ length: 100 }, (_, i) =>
      request({ request_id: `r${i}`, timestamp: `2026-08-13T12:00:${String(i % 60).padStart(2, '0')}Z` }),
    );

    expect(tickerRequests(many, 10)).toHaveLength(10);
  });
});

describe('loadFraction', () => {
  const cell = (over: Partial<InstanceCell> = {}): InstanceCell => ({
    key: 'h:i',
    instanceId: 'i',
    hostId: 'h',
    hostName: 'alpha',
    hostStatus: 'online',
    alias: 'a',
    model: 'm',
    category: 'generation',
    status: 'running',
    inFlight: 0,
    state: null,
    ...over,
  });

  it('is zero for an idle instance', () => {
    expect(loadFraction(cell())).toBe(0);
  });

  it('is zero for an instance that is not running, whatever it reports', () => {
    expect(loadFraction(cell({ status: 'stopped', inFlight: 3 }))).toBe(0);
  });

  it('shows a sliver for busy-with-no-slot-count, so the cell is not blank', () => {
    expect(loadFraction(cell({ state: { busy: true, active_slots: 0 } as InstanceStateData }))).toBeCloseTo(0.15);
  });

  it('grows with load and saturates at full', () => {
    expect(loadFraction(cell({ inFlight: 2 }))).toBeCloseTo(0.5);
    expect(loadFraction(cell({ inFlight: 9 }))).toBe(1);
  });

  it('takes whichever of slots or in-flight is higher', () => {
    const slots = { busy: true, active_slots: 4 } as InstanceStateData;
    expect(loadFraction(cell({ inFlight: 1, state: slots }))).toBe(1);
  });
});

describe('phaseLabel', () => {
  const base: InstanceCell = {
    key: 'h:i',
    instanceId: 'i',
    hostId: 'h',
    hostName: 'alpha',
    hostStatus: 'online',
    alias: 'a',
    model: 'm',
    category: 'generation',
    status: 'running',
    inFlight: 0,
    state: null,
  };

  it('reports prefill as a percentage', () => {
    const state = { busy: true, active_slots: 1, phase: 'prefill', prefill_progress: 0.42 } as InstanceStateData;
    expect(phaseLabel({ ...base, state })).toBe('prefill 42%');
  });

  it('reports decode as tokens per second', () => {
    const state = { busy: true, active_slots: 1, phase: 'decode', decode_tps: 63.7 } as InstanceStateData;
    expect(phaseLabel({ ...base, state })).toBe('64 tok/s');
  });

  it('falls back to the raw phase, then to busy', () => {
    expect(phaseLabel({ ...base, state: { busy: true, active_slots: 1, phase: 'loading' } as InstanceStateData })).toBe(
      'loading',
    );
    expect(phaseLabel({ ...base, state: { busy: true, active_slots: 1 } as InstanceStateData })).toBe('busy');
  });

  it('says nothing for an idle or stopped instance', () => {
    expect(phaseLabel({ ...base, state: { busy: false, active_slots: 0 } as InstanceStateData })).toBeNull();
    expect(
      phaseLabel({ ...base, status: 'stopped', state: { busy: true, active_slots: 1 } as InstanceStateData }),
    ).toBeNull();
  });
});
