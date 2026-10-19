import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApiUrl } from './base';
import { ApiError, fetchApi, fetchJson } from './client';

afterEach(() => {
  vi.unstubAllGlobals();
});

function okResponse(json: unknown) {
  return { ok: true, status: 200, json: vi.fn(async () => json) };
}

function errorResponse(status: number) {
  return { ok: false, status, json: vi.fn(async () => undefined) };
}

describe('fetchApi', () => {
  it('fetches the endpoint appended to the API base and returns the raw response', async () => {
    const response = okResponse({ ready: true });
    const fetchMock = vi.fn(async () => response);
    vi.stubGlobal('fetch', fetchMock);

    const result = await fetchApi('status');

    expect(result).toBe(response);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(createApiUrl('status'), undefined);
  });

  it('passes the init argument through to fetch', async () => {
    const fetchMock = vi.fn(async () => okResponse(null));
    vi.stubGlobal('fetch', fetchMock);
    const init: RequestInit = { method: 'POST', headers: { 'Content-Type': 'application/json' } };

    await fetchApi('status', init);

    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(createApiUrl('status'), init);
  });

  it('throws an ApiError carrying the response status and never touches the body on failure', async () => {
    const response = errorResponse(501);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response)
    );

    await expect(fetchApi('status')).rejects.toMatchObject({ name: 'ApiError', status: 501 });
    expect(response.json).not.toHaveBeenCalled();
  });
});

describe('fetchJson', () => {
  it('returns the parsed payload of a successful response', async () => {
    const payload = { ready: true };
    const json = vi.fn(async () => payload);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json }))
    );

    const result = await fetchJson<{ ready: boolean }>('status');

    expect(result).toEqual(payload);
    expect(json).toHaveBeenCalledExactlyOnceWith();
  });

  it('propagates the ApiError of a failed response without parsing the body', async () => {
    const response = errorResponse(501);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response)
    );

    await expect(fetchJson('status')).rejects.toBeInstanceOf(ApiError);
    expect(response.json).not.toHaveBeenCalled();
  });
});
