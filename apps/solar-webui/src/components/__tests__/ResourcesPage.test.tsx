import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ResourcesPage } from '../ResourcesPage';
import solarClient from '@/api/client';
import { HostResourceSnapshot } from '@/api/types';

vi.mock('@/api/client', () => ({
  default: { getResources: vi.fn(), getDrainStatus: vi.fn() },
}));

vi.mock('@/context/RoutingEventsContext', () => ({
  useRoutingEventsContext: () => ({
    hostStatuses: new Map(),
    hostInstances: new Map(),
    routingConnected: true,
  }),
}));

const getResources = vi.mocked(solarClient.getResources);

function host(overrides: Partial<HostResourceSnapshot> & { host_name: string }): HostResourceSnapshot {
  return {
    host_id: overrides.host_name,
    url: `http://${overrides.host_name}:8001`,
    status: 'online',
    version: '0.4.2',
    roles: ['inference'],
    gpu_type: 'nvidia_cuda',
    reachable: true,
    error: null,
    drain_state: null,
    vram_total_gb: 80,
    vram_available_gb: 40,
    vram_system_used_gb: 40,
    vram_training_used_gb: 0,
    vram_reserved_headroom_gb: 0,
    ram_total_gb: 128,
    ram_available_gb: 64,
    ram_system_used_gb: 64,
    ram_training_used_gb: 0,
    ram_reserved_headroom_gb: 0,
    disk_total_gb: 900,
    disk_available_gb: 500,
    disk_system_used_gb: 400,
    disk_training_used_gb: 0,
    disk_reserved_headroom_gb: 0,
    instance_count: 1,
    running_instance_count: 1,
    reservation_count: 0,
    instances: [],
    active_jobs: [],
    reservations: [],
    snapshot_timestamp: '2026-08-13T12:00:00Z',
    ...overrides,
  } as HostResourceSnapshot;
}

function respondWith(hosts: HostResourceSnapshot[]) {
  getResources.mockResolvedValue({
    hosts,
    total_hosts: hosts.length,
    reachable_hosts: hosts.filter((h) => h.reachable).length,
    unreachable_hosts: hosts.filter((h) => !h.reachable).length,
  });
}

/** Names in render order, from the compact rows or the cards, whichever is shown. */
function hostOrder(): string[] {
  return screen
    .getAllByRole('heading', { level: 3 })
    .map((h) => h.textContent?.trim() ?? '')
    .filter(Boolean);
}

function rowNames(): string[] {
  return screen.getAllByTestId('host-row').map((r) => within(r).getByTestId('host-row-name').textContent ?? '');
}

function manyHosts(count: number): HostResourceSnapshot[] {
  return Array.from({ length: count }, (_, i) => host({ host_name: `host${String(i + 1).padStart(2, '0')}` }));
}

const renderPage = () =>
  render(
    <MemoryRouter>
      <ResourcesPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  getResources.mockReset();
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe('ResourcesPage ordering', () => {
  it('lists hosts alphabetically rather than in API order', async () => {
    respondWith([host({ host_name: 'zulu' }), host({ host_name: 'alpha' }), host({ host_name: 'mike' })]);

    renderPage();

    await waitFor(() => expect(hostOrder()).toEqual(['alpha', 'mike', 'zulu']));
  });

  it('orders host names the way a person reads them', async () => {
    respondWith([host({ host_name: 'host10' }), host({ host_name: 'host2' }), host({ host_name: 'host1' })]);

    renderPage();

    await waitFor(() => expect(hostOrder()).toEqual(['host1', 'host2', 'host10']));
  });

  it('puts the roomiest host first when sorting by free VRAM', async () => {
    respondWith([
      host({ host_name: 'small', vram_available_gb: 4 }),
      host({ host_name: 'big', vram_available_gb: 120 }),
      host({ host_name: 'mid', vram_available_gb: 40 }),
    ]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('small');

    await user.selectOptions(screen.getByLabelText('Sort hosts by'), 'vram_available_gb');

    expect(hostOrder()).toEqual(['big', 'mid', 'small']);
  });

  it('surfaces unhealthy hosts first when sorting by status', async () => {
    respondWith([
      host({ host_name: 'healthy' }),
      host({ host_name: 'broken', status: 'error', reachable: false }),
      host({ host_name: 'down', status: 'offline', reachable: false }),
    ]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('healthy');

    await user.selectOptions(screen.getByLabelText('Sort hosts by'), 'status');

    expect(hostOrder()).toEqual(['broken', 'down', 'healthy']);
  });

  it('reverses the order when the direction button is pressed', async () => {
    respondWith([host({ host_name: 'alpha' }), host({ host_name: 'zulu' })]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('alpha');

    await user.click(screen.getByRole('button', { name: /sort direction/i }));

    expect(hostOrder()).toEqual(['zulu', 'alpha']);
  });
});

describe('ResourcesPage search', () => {
  it('filters by host name', async () => {
    respondWith([host({ host_name: 'edge01' }), host({ host_name: 'core01' }), host({ host_name: 'edge02' })]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('core01');

    await user.type(screen.getByLabelText('Search hosts'), 'edge');

    expect(hostOrder()).toEqual(['edge01', 'edge02']);
  });

  it('matches on role and GPU type, not just the name', async () => {
    respondWith([
      host({ host_name: 'alpha', roles: ['inference'], gpu_type: 'apple_mps' }),
      host({ host_name: 'bravo', roles: ['inference', 'training'], gpu_type: 'nvidia_cuda' }),
    ]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('alpha');

    await user.type(screen.getByLabelText('Search hosts'), 'training');

    expect(hostOrder()).toEqual(['bravo']);
  });

  it('requires every term to match', async () => {
    respondWith([
      host({ host_name: 'alpha', roles: ['training'], gpu_type: 'apple_mps' }),
      host({ host_name: 'bravo', roles: ['training'], gpu_type: 'nvidia_cuda' }),
    ]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('alpha');

    await user.type(screen.getByLabelText('Search hosts'), 'training nvidia');

    expect(hostOrder()).toEqual(['bravo']);
  });

  it('reports how many of the loaded hosts are showing', async () => {
    respondWith([host({ host_name: 'edge01' }), host({ host_name: 'core01' })]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2 hosts');

    await user.type(screen.getByLabelText('Search hosts'), 'edge');

    expect(screen.getByText('1 of 2')).toBeInTheDocument();
  });

  it('explains an empty result and recovers when cleared', async () => {
    respondWith([host({ host_name: 'edge01' })]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('edge01');

    await user.type(screen.getByLabelText('Search hosts'), 'nothing');
    expect(screen.getByText(/No host matches/)).toBeInTheDocument();

    await user.click(screen.getByLabelText('Clear search'));
    expect(hostOrder()).toEqual(['edge01']);
  });
});

describe('ResourcesPage density', () => {
  it('keeps full cards for a small fleet', async () => {
    respondWith(manyHosts(3));

    renderPage();

    await screen.findByText('host01');
    expect(screen.queryAllByTestId('host-row')).toHaveLength(0);
  });

  it('switches to the compact list once the card grid stops being scannable', async () => {
    respondWith(manyHosts(20));

    renderPage();

    await waitFor(() => expect(screen.getAllByTestId('host-row')).toHaveLength(20));
  });

  it('honours an explicit choice over the automatic one', async () => {
    respondWith(manyHosts(20));
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('host-row')).toHaveLength(20));

    await user.click(screen.getByRole('button', { name: /Cards/ }));

    expect(screen.queryAllByTestId('host-row')).toHaveLength(0);
    expect(localStorage.getItem('solar_resources_density')).toBe('cards');
  });

  it('restores the stored density on the next visit', async () => {
    localStorage.setItem('solar_resources_density', 'compact');
    respondWith(manyHosts(3));

    renderPage();

    await waitFor(() => expect(screen.getAllByTestId('host-row')).toHaveLength(3));
  });

  it('restores the stored sort on the next visit', async () => {
    localStorage.setItem('solar_resources_sort', 'name');
    localStorage.setItem('solar_resources_sort_dir', 'desc');
    respondWith([host({ host_name: 'alpha' }), host({ host_name: 'zulu' })]);

    renderPage();

    await waitFor(() => expect(hostOrder()).toEqual(['zulu', 'alpha']));
  });

  it('expands a compact row into the full card', async () => {
    respondWith(manyHosts(20));
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('host-row')).toHaveLength(20));

    expect(screen.queryByRole('heading', { level: 3 })).not.toBeInTheDocument();

    await user.click(screen.getAllByTestId('host-row')[0]);

    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('host01');
  });

  it('shows one expanded host at a time', async () => {
    respondWith(manyHosts(20));
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('host-row')).toHaveLength(20));

    await user.click(screen.getAllByTestId('host-row')[0]);
    await user.click(screen.getAllByTestId('host-row')[1]);

    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(1);
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('host02');
  });

  it('renders every host of a large fleet in the compact list', async () => {
    respondWith(manyHosts(50));

    renderPage();

    await waitFor(() => expect(rowNames()).toHaveLength(50));
    expect(rowNames()[0]).toBe('host01');
    expect(rowNames()[49]).toBe('host50');
  });
});
