import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import solarClient from '@/api/client';
import type { ApiEndpoint } from '@/api/types';
import { EndpointFormModal } from '@/components/endpoints/EndpointFormModal';

vi.mock('@/api/client', () => ({
  default: {
    previewEndpointModels: vi.fn(),
    createEndpoint: vi.fn(),
    updateEndpoint: vi.fn(),
  },
}));

const mockedClient = vi.mocked(solarClient);

const REGISTRY = ['iris-osl:8b', 'iris-osl:70b', 'qwen-v4-flash:284b'];

const endpoint = (overrides: Partial<ApiEndpoint> = {}): ApiEndpoint => ({
  id: 'ep-1',
  name: 'Primary',
  description: null,
  serve_all_models: true,
  model_patterns: [],
  key_count: 0,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  // Mirror the control plane: `aliases` is the matched subset of `available`.
  mockedClient.previewEndpointModels.mockImplementation(async ({ serve_all_models, model_patterns }) => {
    const matched = serve_all_models
      ? REGISTRY
      : REGISTRY.filter((alias) => model_patterns.some((p) => new RegExp(`^${p.replace(/\*/g, '.*')}$`).test(alias)));
    return { aliases: matched, count: matched.length, available: REGISTRY };
  });
  mockedClient.updateEndpoint.mockImplementation(async (_id, payload) => endpoint(payload as Partial<ApiEndpoint>));
  mockedClient.createEndpoint.mockImplementation(async (payload) => endpoint(payload as Partial<ApiEndpoint>));
});

describe('EndpointFormModal model access', () => {
  it('offers the registry as a pick list instead of requiring glob syntax', async () => {
    render(<EndpointFormModal onClose={vi.fn()} />);
    fireEvent.click(screen.getByText('Selected models only'));
    for (const alias of REGISTRY) {
      expect(await screen.findByText(alias)).toBeTruthy();
    }
  });

  it('saves the models ticked in the pick list', async () => {
    const onCreated = vi.fn();
    render(<EndpointFormModal onClose={vi.fn()} onCreated={onCreated} />);
    fireEvent.change(screen.getByPlaceholderText('Production API'), { target: { value: 'Scoped' } });
    fireEvent.click(screen.getByText('Selected models only'));
    fireEvent.click(await screen.findByText('iris-osl:8b'));
    fireEvent.click(screen.getByText('Create'));

    await waitFor(() => expect(mockedClient.createEndpoint).toHaveBeenCalled());
    expect(mockedClient.createEndpoint.mock.calls[0][0]).toMatchObject({
      name: 'Scoped',
      serve_all_models: false,
      model_patterns: ['iris-osl:8b'],
    });
  });

  it('persists edited patterns on save (regression: PUT used to drop them)', async () => {
    const onSaved = vi.fn();
    render(
      <EndpointFormModal
        endpoint={endpoint({ serve_all_models: false, model_patterns: ['iris-osl:8b'] })}
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );
    fireEvent.click(await screen.findByText('qwen-v4-flash:284b'));
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(mockedClient.updateEndpoint).toHaveBeenCalled());
    expect(mockedClient.updateEndpoint.mock.calls[0][1]).toMatchObject({
      serve_all_models: false,
      model_patterns: ['iris-osl:8b', 'qwen-v4-flash:284b'],
    });
    expect(onSaved).toHaveBeenCalled();
  });

  it('keeps the selection when toggling to all-models and back', async () => {
    render(
      <EndpointFormModal
        endpoint={endpoint({ serve_all_models: false, model_patterns: ['iris-osl:8b'] })}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText('All models'));
    fireEvent.click(screen.getByText('Selected models only'));
    const checkbox = (await screen.findByText('iris-osl:8b')).closest('label')?.querySelector('input');
    expect((checkbox as HTMLInputElement).checked).toBe(true);
  });

  it('shows models covered by a wildcard rule as included and read-only', async () => {
    render(
      <EndpointFormModal
        endpoint={endpoint({ serve_all_models: false, model_patterns: ['iris-osl:*'] })}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getAllByText('via iris-osl:*').length).toBe(2));
    const row = screen.getByText('iris-osl:70b').closest('label');
    expect((row?.querySelector('input') as HTMLInputElement).disabled).toBe(true);
    expect((row?.querySelector('input') as HTMLInputElement).checked).toBe(true);
  });

  it('keeps a ticked model visible after its instance leaves the registry', async () => {
    render(
      <EndpointFormModal
        endpoint={endpoint({ serve_all_models: false, model_patterns: ['unloaded:1b'] })}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );
    expect(await screen.findByText('unloaded:1b')).toBeTruthy();
    expect(await screen.findByText('not loaded')).toBeTruthy();
  });

  it('does not flash a false empty-scope warning before the registry resolves', async () => {
    render(
      <EndpointFormModal
        endpoint={endpoint({ serve_all_models: false, model_patterns: ['iris-osl:*'] })}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );
    expect(screen.queryByText(/Nothing is selected/)).toBeNull();
    expect(await screen.findByText(/of 3 registered model/)).toBeTruthy();
    expect(screen.queryByText(/Nothing is selected/)).toBeNull();
  });

  it('warns when a scoped endpoint would serve nothing', async () => {
    render(<EndpointFormModal onClose={vi.fn()} />);
    fireEvent.click(screen.getByText('Selected models only'));
    expect(await screen.findByText(/Nothing is selected/)).toBeTruthy();
  });
});
