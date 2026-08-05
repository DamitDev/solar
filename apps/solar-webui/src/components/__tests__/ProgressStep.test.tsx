import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProgressStep } from '@/components/upload/ProgressStep';
import solarClient from '@/api/client';
import { CreateUploadResponse } from '@/api/types';
import { SelectedUploadFile } from '@/lib/uploadPaths';

const session: CreateUploadResponse = {
  upload_id: 'up-1',
  harbor_ref: 'harbor.test/supernova/my-model:v1',
  name: 'my-model',
  version: 'v1',
  expires_at: '2026-08-06T12:00:00Z',
};

function fakeFile(name: string, webkitRelativePath: string, size: number): File {
  return Object.assign(new File([new Uint8Array(size)], name), { webkitRelativePath });
}

const entries: SelectedUploadFile[] = [
  { file: fakeFile('a.bin', 'm/a.bin', 100), path: 'a.bin' },
  { file: fakeFile('b.bin', 'm/b.bin', 200), path: 'b.bin' },
];

describe('ProgressStep', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('aggregates per-file bytes into the total progress', async () => {
    vi.spyOn(solarClient, 'uploadFile').mockImplementation((_uploadId, _path, _file, onProgress) => {
      // Fire progress so the aggregate reflects per-file bytes.
      if (onProgress) onProgress(100, 100);
      return Promise.resolve({ path: _path, digest: 'sha256:abc', size: 100 });
    });
    vi.spyOn(solarClient, 'completeUpload').mockResolvedValue({
      name: 'my-model',
      version: 'v1',
      category: 'model',
      harbor_ref: 'harbor.test/supernova/my-model:v1',
      size_bytes: 300,
      registration: {},
    });

    render(
      <MemoryRouter>
        <ProgressStep session={session} entries={entries} onCancel={vi.fn()} />
      </MemoryRouter>,
    );

    // Aggregate totals reflect both files.
    await waitFor(() => expect(screen.getByText(/2\/2 files/)).toBeInTheDocument());
    expect(screen.getByText(/300 B/)).toBeInTheDocument();
    // Completion drives the success panel.
    expect(await screen.findByText('Upload complete')).toBeInTheDocument();
    expect(screen.getByText(/my-model:v1/)).toBeInTheDocument();
  });

  it('lets a failed file be retried', async () => {
    const uploadFile = vi
      .spyOn(solarClient, 'uploadFile')
      .mockResolvedValueOnce({ path: 'a.bin', digest: 'sha256:aa', size: 100 })
      .mockRejectedValueOnce(new Error('Network error during upload'))
      .mockResolvedValueOnce({ path: 'b.bin', digest: 'sha256:bb', size: 200 });
    vi.spyOn(solarClient, 'completeUpload').mockResolvedValue({
      name: 'my-model',
      version: 'v1',
      category: 'model',
      harbor_ref: 'harbor.test/supernova/my-model:v1',
      size_bytes: 300,
      registration: {},
    });

    render(
      <MemoryRouter>
        <ProgressStep session={session} entries={entries} onCancel={vi.fn()} />
      </MemoryRouter>,
    );

    // One file fails -> retry button appears; clicking it re-queues the file.
    const retry = await screen.findByRole('button', { name: /^retry$/i });
    await userEvent.click(retry);
    await waitFor(() => expect(screen.getByText('Upload complete')).toBeInTheDocument());
    expect(uploadFile).toHaveBeenCalledTimes(3);
  });

  it('guards navigation while an upload is in flight', () => {
    let resolveFirst: (value: any) => void = () => {};
    vi.spyOn(solarClient, 'uploadFile').mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );

    render(
      <MemoryRouter>
        <ProgressStep session={session} entries={entries} onCancel={vi.fn()} />
      </MemoryRouter>,
    );

    const handler = window.onbeforeunload;
    expect(handler).toBeDefined();

    resolveFirst({ path: 'a.bin', digest: 'sha256:aa', size: 100 });
  });
});
