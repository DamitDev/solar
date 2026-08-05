import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CatalogDeleteModal } from '@/components/CatalogDeleteModal';
import solarClient from '@/api/client';
import { CatalogDeleteResult } from '@/api/types';

const repoResult: CatalogDeleteResult = {
  name: 'mymodel',
  deleted: ['v2', 'v1'],
  failed: [],
  artifact_removed: true,
  harbor_repository_removed: true,
};

describe('CatalogDeleteModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('deletes a version after confirmation', async () => {
    const deleteVersion = vi.spyOn(solarClient, 'deleteCatalogModelVersion').mockResolvedValue();
    const onDone = vi.fn();
    const onClose = vi.fn();

    render(
      <CatalogDeleteModal
        modelName="mymodel"
        target={{ kind: 'version', version: 'v1' }}
        onClose={onClose}
        onDone={onDone}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /Delete version/ }));

    expect(deleteVersion).toHaveBeenCalledWith('mymodel', 'v1');
    expect(await screen.findByText(/Version v1 deleted/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalled();
    expect(onDone).toHaveBeenCalled();
  });

  it('requires typed name confirmation for repository delete', async () => {
    const deleteModel = vi.spyOn(solarClient, 'deleteCatalogModel').mockResolvedValue(repoResult);

    render(
      <CatalogDeleteModal modelName="mymodel" target={{ kind: 'repository' }} onClose={vi.fn()} onDone={vi.fn()} />,
    );

    const confirm = screen.getByRole('button', { name: /Delete repository/ });
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByPlaceholderText('mymodel'), 'wrong');
    expect(confirm).toBeDisabled();

    await userEvent.clear(screen.getByPlaceholderText('mymodel'));
    await userEvent.type(screen.getByPlaceholderText('mymodel'), 'mymodel');
    expect(confirm).toBeEnabled();

    await userEvent.click(confirm);

    expect(deleteModel).toHaveBeenCalledWith('mymodel');
    expect(await screen.findByText(/Deleted 2 versions/)).toBeInTheDocument();
  });

  it('blocks the confirm while running instances serve the target', async () => {
    const deleteModel = vi.spyOn(solarClient, 'deleteCatalogModel');

    render(
      <CatalogDeleteModal
        modelName="mymodel"
        target={{ kind: 'repository', blockedByRunning: 2 }}
        onClose={vi.fn()}
        onDone={vi.fn()}
      />,
    );

    expect(screen.getByText(/2 running instances serve this model/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Delete repository/ })).toBeDisabled();
    expect(deleteModel).not.toHaveBeenCalled();
  });

  it('surfaces the 409 detail from the server and allows going back', async () => {
    vi.spyOn(solarClient, 'deleteCatalogModelVersion').mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'Cannot delete version v1: served by running instances (i1@host-1).' },
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <CatalogDeleteModal
        modelName="mymodel"
        target={{ kind: 'version', version: 'v1' }}
        onClose={vi.fn()}
        onDone={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /Delete version/ }));

    expect(await screen.findByText(/Cannot delete version v1: served by running instances/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByRole('button', { name: /Delete version/ })).toBeInTheDocument();
  });

  it('renders partial failures and keeps the entry note', async () => {
    vi.spyOn(solarClient, 'deleteCatalogModel').mockResolvedValue({
      name: 'mymodel',
      deleted: ['v2'],
      failed: [{ version: 'v1', detail: 'Harbor delete failed: boom' }],
      artifact_removed: false,
      harbor_repository_removed: false,
    });

    render(
      <CatalogDeleteModal modelName="mymodel" target={{ kind: 'repository' }} onClose={vi.fn()} onDone={vi.fn()} />,
    );

    await userEvent.type(screen.getByPlaceholderText('mymodel'), 'mymodel');
    await userEvent.click(screen.getByRole('button', { name: /Delete repository/ }));

    expect(await screen.findByText(/Some versions could not be deleted/)).toBeInTheDocument();
    expect(screen.getByText('Harbor delete failed: boom')).toBeInTheDocument();
    expect(screen.getByText(/fix the failures and try again/)).toBeInTheDocument();
  });

  it('cancels without calling the client', async () => {
    const deleteModel = vi.spyOn(solarClient, 'deleteCatalogModel');
    const onClose = vi.fn();

    render(
      <CatalogDeleteModal modelName="mymodel" target={{ kind: 'repository' }} onClose={onClose} onDone={vi.fn()} />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(deleteModel).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
