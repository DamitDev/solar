import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { StoragePage } from '@/components/StoragePage';
import solarClient from '@/api/client';
import { HostStorage, StorageResponse, StoredModel } from '@/api/types';
import { formatBytes } from '@/lib/utils';

function model(slug: string, overrides: Partial<StoredModel> = {}): StoredModel {
  return {
    slug,
    model_name: slug,
    version: 'v1',
    category: null,
    source_uri: null,
    origin: 'repository',
    harbor_ref: null,
    path: `/opt/solar/models/${slug}`,
    size_bytes: 1000,
    downloaded_at: null,
    in_use_by: [],
    files: [],
    ...overrides,
  };
}

function host(id: string, models: StoredModel[], overrides: Partial<HostStorage> = {}): HostStorage {
  return {
    host_id: id,
    host_name: id,
    reachable: true,
    error: null,
    disk_total_gb: 100,
    disk_used_gb: 50,
    disk_available_gb: 50,
    total_size_bytes: models.reduce((s, m) => s + m.size_bytes, 0),
    models,
    ...overrides,
  };
}

function storage(hosts: HostStorage[]): StorageResponse {
  return {
    hosts,
    unreachable_hosts: hosts.filter((h) => !h.reachable).map((h) => h.host_name),
    generated_at: '2026-08-05T00:00:00Z',
  };
}

const baseHosts = () => [
  host('node-a', [
    model('iris', { size_bytes: 1000 }),
    model('phi', {
      size_bytes: 200,
      in_use_by: [{ instance_id: 'i1', alias: 'phi-alias', status: 'running' }],
    }),
  ]),
  host('node-b', [model('iris', { size_bytes: 300 })]),
];

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/storage']}>
      <StoragePage />
    </MemoryRouter>,
  );
}

describe('StoragePage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('renders one panel per host with model counts and total size', async () => {
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(baseHosts()));

    renderPage();

    expect(await screen.findByText('node-a')).toBeInTheDocument();
    expect(screen.getByText('node-b')).toBeInTheDocument();
    expect(screen.getByText(/2 models ·/)).toBeInTheDocument();
    expect(screen.getByText(/1 model ·/)).toBeInTheDocument();
  });

  it('expands a host and disables the checkbox of an in-use model', async () => {
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(baseHosts()));

    renderPage();

    await userEvent.click(await screen.findByText('node-a'));

    const table = screen.getAllByRole('table')[0];
    expect(within(table).getByText('iris')).toBeInTheDocument();

    const phiRow = within(table).getByText('phi').closest('tr')!;
    const phiCheckbox = within(phiRow).getByRole('checkbox');
    expect(phiCheckbox).toBeDisabled();
    expect(phiCheckbox).toHaveAttribute('title', 'In use by phi-alias');
  });

  it('shows the selection bar with correct counts when selecting across hosts', async () => {
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(baseHosts()));

    renderPage();

    await userEvent.click(await screen.findByText('node-a'));
    let table = screen.getAllByRole('table')[0];
    await userEvent.click(within(within(table).getByText('iris').closest('tr')!).getByRole('checkbox'));

    await userEvent.click(screen.getByText('node-b'));
    table = screen.getAllByRole('table')[1];
    await userEvent.click(within(within(table).getByText('iris').closest('tr')!).getByRole('checkbox'));

    expect(await screen.findByText('2 models selected on 2 hosts')).toBeInTheDocument();
    expect(screen.getByText(`frees ~${formatBytes(1300)}`)).toBeInTheDocument();
  });

  it('switches to By model and merges copies from two hosts into one panel', async () => {
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(baseHosts()));

    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /By model/ }));

    expect(await screen.findByText(/On 2 hosts ·/)).toBeInTheDocument();
    expect(screen.getByText('iris')).toBeInTheDocument();
    expect(screen.getByText('phi')).toBeInTheDocument();
  });

  it('delete on all idle hosts preselects only idle copies and opens the modal', async () => {
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(baseHosts()));
    const deleteStoredModels = vi.spyOn(solarClient, 'deleteStoredModels').mockResolvedValue([]);

    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /By model/ }));
    await userEvent.click(screen.getByText('iris'));
    await userEvent.click(await screen.findByRole('button', { name: /Delete on all idle hosts/ }));

    const modal = screen.getByRole('heading', { name: 'Delete stored models' }).closest('.fixed') as HTMLElement;
    expect(within(modal).getAllByText('iris')).toHaveLength(2);
    // phi is in use, so it is not part of the confirm list
    expect(within(modal).queryByText('phi')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Delete 2 models/ }));
    await waitFor(() =>
      expect(deleteStoredModels).toHaveBeenCalledWith([
        { host_id: 'node-a', slug: 'iris' },
        { host_id: 'node-b', slug: 'iris' },
      ]),
    );
  });

  it('renders mixed per-item results after confirming a bulk delete', async () => {
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(baseHosts()));
    vi.spyOn(solarClient, 'deleteStoredModels').mockResolvedValue([
      { host_id: 'node-a', host_name: 'node-a', slug: 'iris', status: 'deleted', detail: null, freed_bytes: 1000 },
      { host_id: 'node-b', host_name: 'node-b', slug: 'iris', status: 'unreachable', detail: 'down', freed_bytes: 0 },
    ]);

    renderPage();

    await userEvent.click(await screen.findByText('node-a'));
    await userEvent.click(within(screen.getByText('iris').closest('tr')!).getByRole('checkbox'));
    await userEvent.click(await screen.findByRole('button', { name: /Delete selected/ }));
    await userEvent.click(screen.getByRole('button', { name: /Delete 1 model/ }));

    expect(await screen.findByText(/Deleted · 1000 B freed/)).toBeInTheDocument();
    expect(screen.getByText(/Host unreachable/)).toBeInTheDocument();
    expect(screen.getByText(/1 deleted · 1 skipped · 0 failed/)).toBeInTheDocument();
  });

  it('shows the degraded banner and keeps the unreachable panel collapsed', async () => {
    const hosts = [...baseHosts(), host('node-dead', [], { reachable: false, error: 'Host status is offline' })];
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(hosts));

    renderPage();

    expect(await screen.findByText(/Some hosts could not be reached/)).toBeInTheDocument();
    expect(screen.getByText('unreachable')).toBeInTheDocument();

    // clicking the dead panel does not expand it
    await userEvent.click(screen.getByText('node-dead'));
    expect(screen.queryByText('No models stored on this host.')).not.toBeInTheDocument();
  });

  it('narrows rows with the Not in use filter and the search box', async () => {
    vi.spyOn(solarClient, 'getStorage').mockResolvedValue(storage(baseHosts()));

    renderPage();

    await userEvent.click(await screen.findByText('node-a'));
    expect(screen.getByText('phi')).toBeInTheDocument();

    // Not in use filter hides the in-use phi model
    await userEvent.click(screen.getByRole('button', { name: 'Not in use' }));
    expect(screen.queryByText('phi')).not.toBeInTheDocument();

    // search narrows to iris only
    await userEvent.type(screen.getByPlaceholderText('Search models or hosts…'), 'iris');
    await waitFor(() => expect(screen.queryByText('phi')).not.toBeInTheDocument());
    expect(screen.getByText('iris')).toBeInTheDocument();
  });
});
