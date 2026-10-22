import { createApiUrl } from './base';

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, url: URL) {
    super(`API request to ${url.href} failed with status ${status}`);
    this.name = 'ApiError';
    this.status = status;
  }
}

export async function fetchApi(endpointPath: string, init?: RequestInit): Promise<Response> {
  const url = createApiUrl(endpointPath);
  const response = await fetch(url, init);
  if (!response.ok) {
    throw new ApiError(response.status, url);
  }
  return response;
}

export async function fetchJson<T>(endpointPath: string, init?: RequestInit): Promise<T> {
  const response = await fetchApi(endpointPath, init);
  return response.json() as Promise<T>;
}
