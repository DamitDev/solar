/**
 * C3: advisory warnings are attached to the create and update responses only —
 * never persisted, never emitted on intent_update. The create flow redirects to
 * the detail page, which re-fetches the record, so the warnings only reach the
 * user if the redirect carries them. These tests cover that handoff end to end
 * and the in-place edit that stays on the list.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import solarClient from '@/api/client';
import { Intent } from '@/api/types';

const eventStream = {
  intents: new Map<string, Intent>(),
  isConnected: true,
  getPullProgress: vi.fn().mockReturnValue(undefined),
  getInstanceLogs: vi.fn().mockReturnValue([]),
  clearInstanceLogs: vi.fn(),
  getInstanceState: vi.fn().mockReturnValue(undefined),
};

vi.mock('@/context/EventStreamContext', () => ({
  useEventStreamContext: () => eventStream,
}));

const intent: Intent = {
  id: 'intent-1',
  alias: 'iris',
  model_source: 'repo://iris:v1',
  replicas: 4,
  priority: 'staging',
  strategy: 'immediate',
  backend: { backend_type: 'llamacpp', model_type: 'llm' },
  placement: { roles: ['inference'], gpu_type: null, host_allow: [], host_deny: [] },
  resources: { vram_gb: null, ram_gb: null },
  metadata: {},
  status: {
    phase: 'pending',
    reconcile: 'idle',
    desired_replicas: 4,
    observed_replicas: 0,
    ready_replicas: 0,
    updated_replicas: 0,
    available: false,
    shortfall: 4,
    replica_set: [],
    conditions: [],
    strategy_progress: null,
    last_error: null,
  },
} as Intent;

const SHORTFALL_WARNING = {
  field: 'replicas',
  message: '4 replicas requested but only 2 hosts are eligible',
};

/** The list page and the detail page behind the same router, as in the app. */
async function renderApp() {
  const { IntentsPage } = await import('@/components/IntentsPage');
  const { IntentDetail } = await import('@/components/IntentDetail');
  render(
    <MemoryRouter initialEntries={['/intents']}>
      <Routes>
        <Route path="/intents" element={<IntentsPage />} />
        <Route path="/intents/:id" element={<IntentDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('advisory warnings on the create path', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    eventStream.intents = new Map();
    eventStream.isConnected = true;
    vi.spyOn(solarClient, 'getPulls').mockResolvedValue({});
    vi.spyOn(solarClient, 'getHosts').mockResolvedValue([]);
    // The record the detail page re-fetches has no warnings: they are
    // response-only, which is exactly why the redirect has to carry them.
    vi.spyOn(solarClient, 'getIntent').mockResolvedValue(intent);
  });

  it('survives the redirect to the detail page and can be dismissed', async () => {
    vi.spyOn(solarClient, 'getIntents').mockResolvedValue([]);
    vi.spyOn(solarClient, 'createIntent').mockResolvedValue({
      ...intent,
      warnings: [SHORTFALL_WARNING],
    } as Intent);

    await renderApp();

    await userEvent.click(await screen.findByRole('button', { name: /New Intent/i }));
    await userEvent.type(screen.getByPlaceholderText('model-name:size'), 'iris');
    await userEvent.type(screen.getByPlaceholderText(/repo:\/\//), 'repo://iris:v1');
    await userEvent.click(screen.getByRole('button', { name: 'Submit Intent' }));

    // Arrived on the detail page with the advisory intact.
    expect(await screen.findByText(/only 2 hosts are eligible/)).toBeInTheDocument();
    expect(screen.getByText('Saved with warnings')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Dismiss warnings/i }));
    await waitFor(() => expect(screen.queryByText(/only 2 hosts are eligible/)).not.toBeInTheDocument());
  });

  it('does not resurrect a dismissed advisory when the page reloads', async () => {
    // The navigation entry is cleared on arrival, so remounting the same URL
    // (a reload, or a back/forward) starts clean.
    const { IntentDetail } = await import('@/components/IntentDetail');
    const { unmount } = render(
      <MemoryRouter initialEntries={[{ pathname: '/intents/intent-1', state: { warnings: [SHORTFALL_WARNING] } }]}>
        <Routes>
          <Route path="/intents/:id" element={<IntentDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/only 2 hosts are eligible/)).toBeInTheDocument();
    unmount();

    render(
      <MemoryRouter initialEntries={['/intents/intent-1']}>
        <Routes>
          <Route path="/intents/:id" element={<IntentDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    await screen.findByText('iris');
    expect(screen.queryByText(/only 2 hosts are eligible/)).not.toBeInTheDocument();
  });
});

describe('advisory warnings on the in-place edit path', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    eventStream.intents = new Map();
    eventStream.isConnected = true;
    vi.spyOn(solarClient, 'getPulls').mockResolvedValue({});
    vi.spyOn(solarClient, 'getHosts').mockResolvedValue([]);
  });

  it('reports them on the row instead of routing the user away', async () => {
    vi.spyOn(solarClient, 'getIntents').mockResolvedValue([intent]);
    vi.spyOn(solarClient, 'updateIntent').mockResolvedValue({
      ...intent,
      warnings: [SHORTFALL_WARNING],
    } as Intent);

    await renderApp();

    await userEvent.click(await screen.findByTitle('Edit intent'));
    await userEvent.click(await screen.findByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText(/only 2 hosts are eligible/)).toBeInTheDocument();
    // Still on the list — the edit succeeded, so there is nowhere to go.
    expect(screen.getByText('repo://iris:v1')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Dismiss warnings/i }));
    await waitFor(() => expect(screen.queryByText(/only 2 hosts are eligible/)).not.toBeInTheDocument());
  });
});
