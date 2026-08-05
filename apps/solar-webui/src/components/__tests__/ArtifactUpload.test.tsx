import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ArtifactUpload } from '@/components/ArtifactUpload';
import solarClient from '@/api/client';
import { CreateUploadResponse } from '@/api/types';

function fakeFile(name: string, webkitRelativePath: string): File {
  return Object.assign(new File(['x'], name), { webkitRelativePath });
}

async function driveToProgress() {
  // Category: model
  await userEvent.click(screen.getByRole('button', { name: /Model/i }));
  await userEvent.click(screen.getByRole('button', { name: /Next/ }));

  // Folder: one model file
  const input = screen.getByTestId('folder-picker');
  fireEvent.change(input, {
    target: { files: [fakeFile('model.gguf', 'my-model/model.gguf')] },
  });
  await userEvent.click(screen.getByRole('button', { name: /Next/ }));

  // Metadata: name, then start
  await userEvent.type(screen.getByLabelText(/Artifact name/), 'my-model');
  await userEvent.click(screen.getByRole('button', { name: /Start upload/ }));
}

describe('ArtifactUpload', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('aborts the session on cancel', async () => {
    const abortUpload = vi.spyOn(solarClient, 'abortUpload').mockResolvedValue(undefined);
    const uploadFile = vi.spyOn(solarClient, 'uploadFile').mockImplementation(
      () =>
        new Promise(() => {
          /* never resolves — upload in flight */
        }),
    );
    const session: CreateUploadResponse = {
      upload_id: 'up-1',
      harbor_ref: 'harbor.test/supernova/my-model:v1',
      name: 'my-model',
      version: 'v1',
      expires_at: '2026-08-06T12:00:00Z',
    };
    vi.spyOn(solarClient, 'createUpload').mockResolvedValue(session);

    render(
      <MemoryRouter>
        <ArtifactUpload />
      </MemoryRouter>,
    );

    await driveToProgress();
    await userEvent.click(screen.getByRole('button', { name: /Abort upload/ }));

    expect(abortUpload).toHaveBeenCalledWith('up-1');
    expect(await screen.findByText('Upload aborted')).toBeInTheDocument();
    expect(uploadFile).toHaveBeenCalled();
  });

  it('walks through category, folder, metadata, and creates the session', async () => {
    const createUpload = vi.spyOn(solarClient, 'createUpload').mockResolvedValue({
      upload_id: 'up-1',
      harbor_ref: 'harbor.test/supernova/my-model:v1',
      name: 'my-model',
      version: 'v1',
      expires_at: '2026-08-06T12:00:00Z',
    });
    vi.spyOn(solarClient, 'uploadFile').mockResolvedValue({
      path: 'model.gguf',
      digest: 'sha256:abc',
      size: 1,
    });
    vi.spyOn(solarClient, 'completeUpload').mockResolvedValue({
      name: 'my-model',
      version: 'v1',
      category: 'model',
      harbor_ref: 'harbor.test/supernova/my-model:v1',
      size_bytes: 1,
      registration: {},
    });

    render(
      <MemoryRouter>
        <ArtifactUpload />
      </MemoryRouter>,
    );

    await driveToProgress();

    await waitFor(() =>
      expect(createUpload).toHaveBeenCalledWith(
        expect.objectContaining({
          category: 'model',
          name: 'my-model',
          files: [{ path: 'model.gguf', size: 1 }],
        }),
      ),
    );
    expect(await screen.findByText('Upload complete')).toBeInTheDocument();
  });
});
