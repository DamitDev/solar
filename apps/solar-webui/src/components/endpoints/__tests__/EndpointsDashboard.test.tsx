import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import solarClient from '@/api/client';
import type { ApiEndpoint, ApiKey, EndpointUsageResponse } from '@/api/types';
import { EndpointsDashboard } from '@/components/endpoints/EndpointsDashboard';

vi.mock('@/api/client', () => ({
  default: {
    getEndpoints: vi.fn(),
    getApiKeys: vi.fn(),
    getEndpointUsage: vi.fn(),
    getEndpointModels: vi.fn(),
    previewEndpointModels: vi.fn(),
    createEndpoint: vi.fn(),
    updateEndpoint: vi.fn(),
    deleteEndpoint: vi.fn(),
    createApiKey: vi.fn(),
    updateApiKey: vi.fn(),
    deleteApiKey: vi.fn(),
    rotateApiKey: vi.fn(),
  },
}));

const mockedClient = vi.mocked(solarClient);

vi.mock('@/context/EventStreamContext', () => ({
  useEventStreamContext: () => ({
    endpoints: [] as ApiEndpoint[],
    apiKeys: [] as ApiKey[],
    isConnected: false,
  }),
}));

const endpoint = (id: string, overrides: Partial<ApiEndpoint> = {}): ApiEndpoint => ({
  id,
  name: id,
  description: null,
  serve_all_models: true,
  model_patterns: [],
  key_count: 0,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  ...overrides,
});

const apiKey = (overrides: Partial<ApiKey> = {}): ApiKey => ({
  id: 'key-1',
  endpoint_id: 'ep-1',
  name: 'default',
  key: 'sk-abcdefgh12345678',
  description: null,
  enabled: true,
  last_used_at: null,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  ...overrides,
});

const emptyUsage = (ep: ApiEndpoint): EndpointUsageResponse => ({
  endpoint: ep,
  hours: 24,
  usage: {
    total_requests: 0,
    successful_requests: 0,
    error_requests: 0,
    missed_requests: 0,
    total_prompt_tokens: 0,
    total_completion_tokens: 0,
    total_tokens: 0,
    avg_duration_s: null,
    avg_decode_tps: null,
  },
});

const emptyModels = (ep: ApiEndpoint = endpoint('ep-1')) => ({
  endpoint: ep,
  aliases: [] as string[],
  count: 0,
});

beforeEach(() => {
  vi.clearAllMocks();
  mockedClient.getEndpoints.mockResolvedValue([]);
  mockedClient.getApiKeys.mockResolvedValue([]);
  mockedClient.getEndpointUsage.mockResolvedValue(emptyUsage(endpoint('ep-1')));
  mockedClient.getEndpointModels.mockResolvedValue(emptyModels());
  mockedClient.previewEndpointModels.mockResolvedValue({ aliases: [], count: 0 });
});

describe('EndpointsDashboard', () => {
  it('renders endpoints with the all-models badge by default', async () => {
    mockedClient.getEndpoints.mockResolvedValue([endpoint('ep-1', { name: 'Primary' })]);
    render(<EndpointsDashboard />);
    expect(await screen.findByText('Primary')).toBeTruthy();
    expect(screen.getByText('All models')).toBeTruthy();
  });

  it('renders model pattern chips and the matched-alias count for scoped endpoints', async () => {
    mockedClient.getEndpoints.mockResolvedValue([
      endpoint('ep-scoped', {
        name: 'Scoped',
        serve_all_models: false,
        model_patterns: ['iris-*', 'qwen-v4*'],
        key_count: 1,
      }),
    ]);
    mockedClient.getEndpointModels.mockResolvedValue({
      endpoint: endpoint('ep-scoped'),
      count: 2,
      aliases: ['iris-osl:8b', 'qwen-v4-flash:284b'],
    });
    mockedClient.getApiKeys.mockResolvedValue([apiKey({ endpoint_id: 'ep-scoped' })]);
    render(<EndpointsDashboard />);
    expect(await screen.findByText('Scoped')).toBeTruthy();
    expect(screen.getByText('iris-*')).toBeTruthy();
    expect(screen.getByText('qwen-v4*')).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/2 models/)).toBeTruthy());
  });

  it('lists keys with masked values', async () => {
    mockedClient.getEndpoints.mockResolvedValue([endpoint('ep-1')]);
    mockedClient.getApiKeys.mockResolvedValue([apiKey({ id: 'k1', endpoint_id: 'ep-1', name: 'ci-runner' })]);
    render(<EndpointsDashboard />);
    expect(await screen.findByText('ci-runner')).toBeTruthy();
    expect(screen.getByText('sk-abcde…')).toBeTruthy();
    expect(screen.queryByText('sk-abcdefgh12345678')).toBeNull();
  });

  it('opens the delete modal with the cascade key count', async () => {
    mockedClient.getEndpoints.mockResolvedValue([endpoint('ep-1', { name: 'doomed' })]);
    mockedClient.getApiKeys.mockResolvedValue([
      apiKey({ endpoint_id: 'ep-1' }),
      apiKey({ id: 'key-2', endpoint_id: 'ep-1', name: 'second' }),
    ]);
    mockedClient.deleteEndpoint.mockResolvedValue({ message: 'deleted', id: 'ep-1' });
    render(<EndpointsDashboard />);
    await screen.findByText('doomed');
    fireEvent.click(screen.getByTitle('Delete endpoint'));
    expect(await screen.findByText('Delete Endpoint')).toBeTruthy();
    expect(screen.getByText(/2 API keys/)).toBeTruthy();
    fireEvent.click(screen.getByText('Delete endpoint'));
    await waitFor(() => expect(mockedClient.deleteEndpoint).toHaveBeenCalledWith('ep-1'));
  });

  it('opens the create modal from the empty state', async () => {
    render(<EndpointsDashboard />);
    fireEvent.click(await screen.findByText('Create Endpoint'));
    expect(await screen.findByText(/Name/)).toBeTruthy();
  });
});
