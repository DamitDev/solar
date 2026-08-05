import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelDetail } from '@/components/ModelDetail';
import solarClient from '@/api/client';
import { CatalogModelItem } from '@/api/types';

const model: CatalogModelItem = {
  name: 'mymodel',
  category: 'model',
  description: 'A test model',
  versions_count: 2,
  latest_version: 'v2',
  created_at: '2026-07-01T00:00:00Z',
  solar: {
    status: 'unavailable',
    running_instances: 0,
    deployed_hosts: [],
    instances: [],
  },
};

const versions = {
  versions: [
    {
      version: 'v2',
      harbor_ref: 'imgrepo.damit.hu/supernova/mymodel:v2',
      created_at: '2026-08-01T00:00:00Z',
      size_bytes: 2048,
      checksum: 'sha256:b',
      solar: { running_instances: 0, deployed_hosts: [] },
    },
    {
      version: 'v1',
      harbor_ref: 'imgrepo.damit.hu/supernova/mymodel:v1',
      created_at: '2026-07-01T00:00:00Z',
      size_bytes: 1024,
      checksum: 'sha256:a',
      solar: { running_instances: 1, deployed_hosts: [] },
    },
  ],
};

function renderDetail(overrides: Partial<Parameters<typeof ModelDetail>[0]> = {}) {
  return render(
    <MemoryRouter>
      <ModelDetail model={model} {...overrides} />
    </MemoryRouter>,
  );
}

describe('ModelDetail versions', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(solarClient, 'getCatalogModelVersions').mockResolvedValue(versions as any);
  });

  it('renders versions from the client with running badges', async () => {
    renderDetail();

    expect(await screen.findByText('v2')).toBeInTheDocument();
    expect(screen.getByText('v1')).toBeInTheDocument();
    expect(screen.getByText('1 running')).toBeInTheDocument();
    expect(solarClient.getCatalogModelVersions).toHaveBeenCalledWith('mymodel');
  });

  it('deletes a version through the modal and refetches versions', async () => {
    const deleteVersion = vi.spyOn(solarClient, 'deleteCatalogModelVersion').mockResolvedValue();
    renderDetail();

    await screen.findByText('v1');
    // v2 is the unblocked row (v1 has a running instance in the fixture).
    await userEvent.click(screen.getAllByTitle(/Delete version/)[0]);

    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /Delete version/ }));

    expect(deleteVersion).toHaveBeenCalledWith('mymodel', 'v2');
    expect(await screen.findByText(/Version v2 deleted/)).toBeInTheDocument();
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(solarClient.getCatalogModelVersions).toHaveBeenCalledTimes(2));
  });

  it('opens the repository delete blocked when instances are running', async () => {
    const runningModel: CatalogModelItem = {
      ...model,
      solar: {
        status: 'available',
        running_instances: 2,
        deployed_hosts: [],
        instances: [],
      },
    };
    render(
      <MemoryRouter>
        <ModelDetail model={runningModel} />
      </MemoryRouter>,
    );

    await userEvent.click(screen.getByRole('button', { name: /Delete repository/ }));

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/2 running instances serve this model/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /Delete repository/ })).toBeDisabled();
  });

  it('deletes the repository and triggers the catalog refetch', async () => {
    const deleteModel = vi.spyOn(solarClient, 'deleteCatalogModel').mockResolvedValue({
      name: 'mymodel',
      deleted: ['v2', 'v1'],
      failed: [],
      artifact_removed: true,
      harbor_repository_removed: true,
    });
    const onDeleted = vi.fn();
    renderDetail({ onDeleted });

    await userEvent.click(screen.getByRole('button', { name: /Delete repository/ }));

    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByPlaceholderText('mymodel'), 'mymodel');
    expect(within(dialog).getByPlaceholderText('mymodel')).toHaveValue('mymodel');
    const confirm = within(dialog).getByRole('button', { name: /Delete repository/ });
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);

    expect(await screen.findByText(/Deleted 2 versions/)).toBeInTheDocument();
    await waitFor(() => expect(deleteModel).toHaveBeenCalledWith('mymodel'));
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });
});
