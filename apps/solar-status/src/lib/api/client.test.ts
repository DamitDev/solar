import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiUrl } from './base';
import { ApiError, fetchJson } from './client';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('fetchJson', () => {
  it('fetches the endpoint appended to the API base and returns the parsed payload', async () => {
    const payload = { ready: true };
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => payload }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await fetchJson<{ ready: boolean }>('status');

    expect(result).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(apiUrl('status'), undefined);
  });

  it('throws an ApiError carrying the response status when the response is not ok', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 501 }))
    );

    await expect(fetchJson('status')).rejects.toMatchObject({ name: 'ApiError', status: 501 });
  });

  it('never parses the body of a failed response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 501 }))
    );

    const error = await fetchJson('status').catch((error: unknown) => error);

    expect(error).toBeInstanceOf(ApiError);
  });
});
