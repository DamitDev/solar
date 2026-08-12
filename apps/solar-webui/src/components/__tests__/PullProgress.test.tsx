/**
 * C4 pull progress in the UI: the row belongs to the host that is pulling, it
 * only shows while the deployment is converging, a finished pull ages out, and
 * a pull already running when the page opened is picked up over REST.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import solarClient from '@/api/client';
import { Intent, PullProgressEntry } from '@/api/types';
import { prunePullProgress, type PullProgressEvent } from '@/hooks/useEventStream';

const eventStream = {
  intents: new Map<string, Intent>(),
  isConnected: true,
  getPullProgress: vi.fn<(hostId: string | null | undefined, sourceUri: string) => PullProgressEvent | undefined>(),
};

vi.mock('@/context/EventStreamContext', () => ({
  useEventStreamContext: () => eventStream,
}));

const SOURCE = 'repo://iris:v1';

function makeIntent(override: Partial<Intent> = {}): Intent {
  return {
    id: 'intent-1',
    alias: 'iris',
    model_source: SOURCE,
    replicas: 1,
    priority: 'staging',
    strategy: 'immediate',
    backend: { backend_type: 'llamacpp' },
    placement: { roles: [], gpu_type: null, host_allow: [], host_deny: [] },
    resources: { vram_gb: null, ram_gb: null },
    metadata: {},
    status: {
      phase: 'reconciling',
      reconcile: 'in_progress',
      desired_replicas: 1,
      observed_replicas: 0,
      ready_replicas: 0,
      updated_replicas: 0,
      available: false,
      shortfall: 1,
      replica_set: [],
      conditions: [],
      strategy_progress: null,
      last_error: null,
      ...(override.status ?? {}),
    },
    ...override,
  } as Intent;
}

function progress(hostId: string, phase: string, extra: Partial<PullProgressEvent['data']> = {}): PullProgressEvent {
  return {
    host_id: hostId,
    host_name: hostId,
    timestamp: new Date().toISOString(),
    data: { source_uri: SOURCE, phase, bytes_done: 5 * 1024 * 1024, bytes_total: 10 * 1024 * 1024, ...extra },
  };
}

async function renderDetail(intent: Intent) {
  vi.spyOn(solarClient, 'getIntent').mockResolvedValue(intent);
  const { IntentDetail } = await import('@/components/IntentDetail');
  render(
    <MemoryRouter initialEntries={[`/intents/${intent.id}`]}>
      <Routes>
        <Route path="/intents/:id" element={<IntentDetail />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText('iris');
}

describe('IntentDetail pull progress', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    eventStream.intents = new Map();
    eventStream.isConnected = true;
    eventStream.getPullProgress = vi.fn().mockReturnValue(undefined);
    vi.spyOn(solarClient, 'getPulls').mockResolvedValue({});
  });

  it('asks for progress on the host running the replica, not the source alone', async () => {
    const intent = makeIntent({
      status: {
        ...makeIntent().status,
        replica_set: [{ host_id: 'host-b', instance_id: 'i-1', state: 'creating' } as any],
      },
    });
    eventStream.getPullProgress = vi.fn((hostId: string | null | undefined) =>
      hostId === 'host-b' ? progress('host-b', 'downloading') : undefined,
    );

    await renderDetail(intent);

    expect(eventStream.getPullProgress).toHaveBeenCalledWith('host-b', SOURCE);
    expect(await screen.findByText(/Model pull/)).toBeInTheDocument();
    expect(screen.getByText(/50%/)).toBeInTheDocument();
  });

  it('falls back to any host while no replica has landed yet', async () => {
    eventStream.getPullProgress = vi.fn(() => progress('host-a', 'downloading'));

    await renderDetail(makeIntent());

    expect(eventStream.getPullProgress).toHaveBeenCalledWith(null, SOURCE);
    expect(await screen.findByText(/Model pull/)).toBeInTheDocument();
  });

  it('hides the row once the intent is ready — a pull is only news while converging', async () => {
    eventStream.getPullProgress = vi.fn(() => progress('host-a', 'downloading'));

    await renderDetail(
      makeIntent({ status: { ...makeIntent().status, phase: 'ready', reconcile: 'idle', ready_replicas: 1 } as any }),
    );

    expect(screen.queryByText(/Model pull/)).not.toBeInTheDocument();
  });

  it('names the phases that carry no byte counts instead of showing nothing', async () => {
    eventStream.getPullProgress = vi.fn(() =>
      progress('host-a', 'verifying', { bytes_done: null, bytes_total: null, speed_bps: null }),
    );

    await renderDetail(makeIntent());

    expect(await screen.findByText(/Model pull/)).toBeInTheDocument();
    expect(screen.getByText(/Verifying/)).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('drops the bar when the source reports no total, keeping the byte count', async () => {
    eventStream.getPullProgress = vi.fn(() =>
      progress('host-a', 'downloading', { bytes_total: null, speed_bps: 2 * 1024 * 1024 }),
    );

    await renderDetail(makeIntent());

    expect(await screen.findByText(/Model pull/)).toBeInTheDocument();
    expect(screen.getByText(/5\.0 MB · 2\.0 MB\/s/)).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('shows a finished pull briefly and drops it once stale', async () => {
    const recent = progress('host-a', 'completed');
    eventStream.getPullProgress = vi.fn(() => recent);
    await renderDetail(makeIntent());
    expect(await screen.findByText(/Model pull completed/)).toBeInTheDocument();

    const stale = { ...recent, timestamp: new Date(Date.now() - 10 * 60_000).toISOString() };
    eventStream.getPullProgress = vi.fn(() => stale);
    await renderDetail(makeIntent({ id: 'intent-2' }));
    await waitFor(() => expect(screen.getAllByText(/Model pull completed/)).toHaveLength(1));
  });

  it('bootstraps progress over REST for a pull that started before mount', async () => {
    const entry: PullProgressEntry = {
      at: new Date().toISOString(),
      data: { source_uri: SOURCE, phase: 'downloading', bytes_done: 2 * 1024 * 1024, bytes_total: 8 * 1024 * 1024 },
    };
    const getPulls = vi.spyOn(solarClient, 'getPulls').mockResolvedValue({ [`host-a|${SOURCE}`]: entry });

    await renderDetail(makeIntent());

    await waitFor(() => expect(getPulls).toHaveBeenCalled());
    expect(await screen.findByText(/25%/)).toBeInTheDocument();
  });

  it('ignores a REST entry for a different model', async () => {
    vi.spyOn(solarClient, 'getPulls').mockResolvedValue({
      'host-a|repo://other:v1': {
        at: new Date().toISOString(),
        data: { source_uri: 'repo://other:v1', phase: 'downloading' },
      },
    });

    await renderDetail(makeIntent());

    await waitFor(() => expect(solarClient.getPulls).toHaveBeenCalled());
    expect(screen.queryByText(/Model pull/)).not.toBeInTheDocument();
  });
});

describe('prunePullProgress', () => {
  const now = Date.parse('2026-08-06T12:00:00.000Z');
  const at = (msAgo: number) => new Date(now - msAgo).toISOString();

  const entry = (phase: string, msAgo: number): PullProgressEvent => ({
    host_id: 'host-a',
    timestamp: at(msAgo),
    data: { source_uri: SOURCE, phase },
  });

  it('drops finished pulls past the grace and keeps recent ones', () => {
    const map = new Map<string, PullProgressEvent>([
      ['host-a|old', entry('completed', 10 * 60_000)],
      ['host-a|recent', entry('failed', 1_000)],
      ['host-a|live', entry('downloading', 60_000)],
    ]);

    prunePullProgress(map, undefined, now);

    expect([...map.keys()]).toEqual(['host-a|recent', 'host-a|live']);
  });

  it('drops a download the host stopped reporting', () => {
    // A host that dies mid-pull never sends a terminal event, so silence is
    // the only signal there is. Without this the frozen byte count is shown
    // as live progress for the rest of the session.
    const map = new Map<string, PullProgressEvent>([
      ['host-a|abandoned', entry('downloading', 30 * 60_000)],
      // Silent for a while but inside the margin: a host that skipped a few
      // emissions keeps its bar.
      ['host-a|slow', entry('downloading', 4 * 60_000)],
    ]);

    prunePullProgress(map, undefined, now);

    expect([...map.keys()]).toEqual(['host-a|slow']);
  });

  it('keeps an unstamped in-flight entry, which may simply be new', () => {
    const map = new Map<string, PullProgressEvent>([
      ['host-a|nostamp', { host_id: 'host-a', data: { source_uri: SOURCE, phase: 'downloading' } }],
    ]);

    prunePullProgress(map, undefined, now);

    expect(map.size).toBe(1);
  });

  it('never drops the key just written', () => {
    const map = new Map<string, PullProgressEvent>([['host-a|just-done', entry('completed', 10 * 60_000)]]);

    prunePullProgress(map, 'host-a|just-done', now);

    expect(map.has('host-a|just-done')).toBe(true);
  });

  it('treats an unstamped terminal entry as expired', () => {
    const map = new Map<string, PullProgressEvent>([
      ['host-a|nostamp', { host_id: 'host-a', data: { source_uri: SOURCE, phase: 'completed' } }],
    ]);

    prunePullProgress(map, undefined, now);

    expect(map.size).toBe(0);
  });

  it('caps the map so a long session cannot grow it without bound', () => {
    const map = new Map<string, PullProgressEvent>();
    for (let i = 0; i < 260; i += 1) {
      // In-flight pulls: not eligible for age-based eviction, so only the cap
      // can bound them.
      map.set(`host-${i}|${SOURCE}`, entry('downloading', 260 - i));
    }

    prunePullProgress(map, `host-259|${SOURCE}`, now);

    expect(map.size).toBe(200);
    expect(map.has(`host-259|${SOURCE}`)).toBe(true);
    // Oldest evicted first.
    expect(map.has('host-0|' + SOURCE)).toBe(false);
  });
});

afterEach(() => {
  vi.useRealTimers();
});
