import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, MockInstance, vi } from 'vitest';
import solarClient from '@/api/client';
import { StorageDeleteModal } from '@/components/StorageDeleteModal';
import { HostStorage, StorageDeleteItem } from '@/api/types';

const hosts: HostStorage[] = [
  {
    host_id: 'host-1',
    host_name: 'node-a',
    reachable: true,
    error: null,
    disk_total_gb: 100,
    disk_used_gb: 50,
    disk_available_gb: 50,
    total_size_bytes: 300,
    models: [
      {
        slug: 'hf--org--qwen',
        model_name: null,
        version: null,
        category: null,
        source_uri: 'huggingface://org/qwen',
        origin: 'huggingface',
        harbor_ref: null,
        path: '/opt/solar/models/hf--org--qwen',
        size_bytes: 300,
        downloaded_at: null,
        in_use_by: [],
        files: [
          { name: 'model-Q4_K_M.gguf', size_bytes: 100 },
          { name: 'model-Q8_0.gguf', size_bytes: 101 },
          { name: 'config.json', size_bytes: 99 },
        ],
      },
    ],
  },
];

const singleItem: StorageDeleteItem[] = [{ host_id: 'host-1', slug: 'hf--org--qwen' }];

function renderModal(items = singleItem) {
  return render(<StorageDeleteModal items={items} hosts={hosts} onClose={vi.fn()} onDone={vi.fn()} />);
}

describe('StorageDeleteModal smart delete', () => {
  let deleteSpy: MockInstance<typeof solarClient.deleteStoredModels> = vi.fn() as any;

  beforeEach(() => {
    vi.restoreAllMocks();
    deleteSpy = vi.spyOn(solarClient, 'deleteStoredModels').mockResolvedValue([
      {
        host_id: 'host-1',
        host_name: 'node-a',
        slug: 'hf--org--qwen',
        status: 'deleted',
        detail: null,
        freed_bytes: 100,
      },
    ] as any);
  });

  it('shows per-file checkboxes for a multi-file model, all checked by default', () => {
    renderModal();

    expect(screen.getByText('model-Q4_K_M.gguf')).toBeInTheDocument();
    expect(screen.getByText('model-Q8_0.gguf')).toBeInTheDocument();
    expect(screen.getByText('config.json')).toBeInTheDocument();
    expect(screen.getByText('3/3 files · 300 B')).toBeInTheDocument();
  });

  it('sends the selected file names as filters when a subset is chosen', async () => {
    renderModal();

    // Uncheck the Q8 quant and config.json — only the Q4 quant stays selected.
    const q8 = screen.getByRole('checkbox', { name: /model-Q8_0\.gguf/ });
    await userEvent.click(q8);
    const cfg = screen.getByRole('checkbox', { name: /config\.json/ });
    await userEvent.click(cfg);

    expect(screen.getByText('1/3 files · 100 B')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Delete 1 model/ }));

    await waitFor(() => expect(solarClient.deleteStoredModels).toHaveBeenCalled());
    const [requestItems] = deleteSpy.mock.calls[0];
    expect(requestItems).toEqual([{ host_id: 'host-1', slug: 'hf--org--qwen', filters: ['model-Q4_K_M.gguf'] }]);
  });

  it('sends a plain whole-model delete when every file is selected', async () => {
    renderModal();

    await userEvent.click(screen.getByRole('button', { name: /Delete 1 model/ }));

    await waitFor(() => expect(solarClient.deleteStoredModels).toHaveBeenCalled());
    const [requestItems] = deleteSpy.mock.calls[0];
    expect(requestItems).toEqual([{ host_id: 'host-1', slug: 'hf--org--qwen' }]);
  });

  it('skips the model entirely when no files are selected', async () => {
    renderModal();

    for (const name of ['model-Q4_K_M.gguf', 'model-Q8_0.gguf', 'config.json']) {
      await userEvent.click(screen.getByRole('checkbox', { name: new RegExp(name) }));
    }

    // Delete is disabled with nothing selected.
    expect(screen.getByRole('button', { name: /Delete 1 model/ })).toBeDisabled();
  });

  it('keeps the old whole-model behaviour for models without a file list', async () => {
    const bareHosts: HostStorage[] = [
      {
        ...hosts[0],
        models: [
          {
            slug: 'repo--iris--v3',
            model_name: null,
            version: null,
            category: null,
            source_uri: 'repo://iris:v3',
            origin: 'repository',
            harbor_ref: null,
            path: '/opt/solar/models/repo--iris--v3',
            size_bytes: 500,
            downloaded_at: null,
            in_use_by: [],
            files: [],
          },
        ],
      },
    ];
    const items = [{ host_id: 'host-1', slug: 'repo--iris--v3' }];
    render(<StorageDeleteModal items={items} hosts={bareHosts} onClose={vi.fn()} onDone={vi.fn()} />);

    await userEvent.click(screen.getByRole('button', { name: /Delete 1 model/ }));

    await waitFor(() => expect(solarClient.deleteStoredModels).toHaveBeenCalled());
    const [requestItems] = deleteSpy.mock.calls[0];
    expect(requestItems).toEqual([{ host_id: 'host-1', slug: 'repo--iris--v3' }]);
  });
});
