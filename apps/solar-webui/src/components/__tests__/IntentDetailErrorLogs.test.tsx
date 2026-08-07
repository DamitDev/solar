/**
 * C2/C3/C4 on the intent detail view: a start failure links to its process
 * logs and shows the log tail, a recoverable failure reads as "still working"
 * rather than an error, and advisory warnings survive the next event and can
 * be dismissed.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { act } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import solarClient from '@/api/client';
import { Intent, IntentLastError } from '@/api/types';

const eventStream = {
  intents: new Map<string, Intent>(),
  isConnected: true,
  getPullProgress: vi.fn().mockReturnValue(undefined),
  // LogViewer subscribes to the stream for live lines.
  getInstanceLogs: vi.fn().mockReturnValue([]),
  clearInstanceLogs: vi.fn(),
  getInstanceState: vi.fn().mockReturnValue(undefined),
};

vi.mock('@/context/EventStreamContext', () => ({
  useEventStreamContext: () => eventStream,
}));

function makeIntent(lastError: IntentLastError | null, override: Partial<Intent> = {}): Intent {
  return {
    id: 'intent-1',
    alias: 'iris',
    model_source: 'repo://iris:v1',
    replicas: 1,
    priority: 'staging',
    strategy: 'immediate',
    backend: { backend_type: 'llamacpp' },
    placement: { roles: [], gpu_type: null, host_allow: [], host_deny: [] },
    resources: { vram_gb: null, ram_gb: null },
    metadata: {},
    status: {
      phase: 'degraded',
      reconcile: 'failed',
      desired_replicas: 1,
      observed_replicas: 0,
      ready_replicas: 0,
      updated_replicas: 0,
      available: false,
      shortfall: 1,
      replica_set: [],
      conditions: [],
      strategy_progress: null,
      last_error: lastError,
    },
    ...override,
  } as Intent;
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

describe('IntentDetail last_error', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    eventStream.intents = new Map();
    eventStream.isConnected = true;
    eventStream.getPullProgress = vi.fn().mockReturnValue(undefined);
    eventStream.getInstanceLogs = vi.fn().mockReturnValue([]);
    vi.spyOn(solarClient, 'getPulls').mockResolvedValue({});
  });

  it('shows the log tail the host returned with the start failure', async () => {
    await renderDetail(
      makeIntent({
        code: 'HostStartFailed',
        message: 'Failed to start instance: exited with code 1',
        host_id: 'host-a',
        instance_id: 'inst-9',
        at: '2026-08-06T10:00:00+00:00',
        log_tail: ['loading model...', 'CUDA error: out of memory'],
      } as IntentLastError),
    );

    expect(screen.getByText('HostStartFailed')).toBeInTheDocument();
    expect(screen.getByText(/CUDA error: out of memory/)).toBeInTheDocument();
  });

  it('opens the process logs for the failed instance', async () => {
    vi.spyOn(solarClient, 'getInstanceLogs').mockResolvedValue([
      { seq: 1, timestamp: '2026-08-06T09:59:59+00:00', line: 'ggml_backend_cuda_init failed' } as any,
    ]);
    await renderDetail(
      makeIntent({
        code: 'HostStartFailed',
        message: 'boom',
        host_id: 'host-a',
        instance_id: 'inst-9',
        at: '2026-08-06T10:00:00+00:00',
      } as IntentLastError),
    );

    await userEvent.click(screen.getByRole('button', { name: /View process logs/i }));

    expect(await screen.findByText(/ggml_backend_cuda_init failed/)).toBeInTheDocument();
    expect(solarClient.getInstanceLogs).toHaveBeenCalledWith('host-a', 'inst-9');
  });

  it('says the logs were not retained when the post-mortem read comes back empty', async () => {
    // The live-view wording ("logs appear when the instance produces output")
    // is wrong for an instance that already died: nothing more is coming.
    vi.spyOn(solarClient, 'getInstanceLogs').mockResolvedValue([]);
    await renderDetail(
      makeIntent({
        code: 'HostStartFailed',
        message: 'boom',
        host_id: 'host-a',
        instance_id: 'inst-9',
        at: '2026-08-06T10:00:00+00:00',
      } as IntentLastError),
    );

    await userEvent.click(screen.getByRole('button', { name: /View process logs/i }));

    expect(await screen.findByText(/No logs retained for this instance/i)).toBeInTheDocument();
    expect(screen.queryByText(/Logs appear when the instance produces output/i)).not.toBeInTheDocument();
  });

  it('has no log link when the host answered without an instance id', async () => {
    await renderDetail(
      makeIntent({
        code: 'HostUnreachable',
        message: 'connection refused',
        host_id: 'host-a',
        at: '2026-08-06T10:00:00+00:00',
      } as IntentLastError),
    );

    expect(screen.queryByRole('button', { name: /View process logs/i })).not.toBeInTheDocument();
  });

  it('renders a recoverable failure as "still working" instead of the error block', async () => {
    // C4: the reconciler gave up on this attempt while the host was still
    // downloading. Showing the red block says "broken" when the answer is
    // "wait", so the amber notice replaces it rather than decorating it.
    await renderDetail(
      makeIntent({
        code: 'TimeoutError',
        message: 'action timed out after 2760s',
        host_id: 'host-a',
        source_uri: 'repo://iris:v1',
        at: '2026-08-06T10:00:00+00:00',
        recoverable: true,
      } as IntentLastError),
    );

    expect(screen.getByText(/Still working/)).toBeInTheDocument();
    expect(screen.getByText(/reconciliation continues automatically/i)).toBeInTheDocument();
    // The raw error message and its red framing are gone.
    expect(screen.queryByText(/action timed out after 2760s/)).not.toBeInTheDocument();
  });

  it('keeps the error block for a non-recoverable failure', async () => {
    await renderDetail(
      makeIntent({
        code: 'HostStartFailed',
        message: 'exited with code 1',
        host_id: 'host-a',
        at: '2026-08-06T10:00:00+00:00',
        recoverable: false,
      } as IntentLastError),
    );

    expect(screen.getByText('HostStartFailed')).toBeInTheDocument();
    expect(screen.queryByText(/Still working/)).not.toBeInTheDocument();
  });
});

describe('IntentDetail advisory warnings', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    eventStream.intents = new Map();
    eventStream.isConnected = true;
    eventStream.getPullProgress = vi.fn().mockReturnValue(undefined);
    vi.spyOn(solarClient, 'getPulls').mockResolvedValue({});
  });

  it('survives an intent_update that carries no warnings', async () => {
    // `intent` resolves to the event copy once one arrives, and warnings are
    // response-only — held on the record they would vanish on the next tick.
    const withWarnings = makeIntent(null, {
      warnings: [{ field: 'resources.vram_gb', message: 'exceeds every host today' }],
    });
    await renderDetail(withWarnings);
    expect(screen.getByText(/exceeds every host today/)).toBeInTheDocument();

    await act(async () => {
      eventStream.intents = new Map([['intent-1', makeIntent(null)]]);
      // Force a re-render through a state change the component owns.
      window.dispatchEvent(new Event('resize'));
    });

    expect(screen.getByText(/exceeds every host today/)).toBeInTheDocument();
  });

  it('can be dismissed', async () => {
    await renderDetail(
      makeIntent(null, { warnings: [{ field: 'placement.gpu_type', message: 'no host has apple_mps' }] }),
    );
    expect(screen.getByText(/no host has apple_mps/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Dismiss warnings/i }));

    await waitFor(() => expect(screen.queryByText(/no host has apple_mps/)).not.toBeInTheDocument());
  });
});
