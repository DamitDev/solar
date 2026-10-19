import { render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import Page from './+page.svelte';

afterEach(() => {
  vi.unstubAllGlobals();
});

it('renders the status page heading', () => {
  render(Page);

  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Solar status');
});

it('calls the status endpoint once on mount and reports the placeholder response', async () => {
  const fetchMock = vi.fn(async () => ({ ok: false, status: 501 }));
  vi.stubGlobal('fetch', fetchMock);

  render(Page);

  expect(await screen.findByText(/HTTP 501/)).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
    expect.objectContaining({ href: `${location.origin}/api/status` }),
    undefined
  );
});

it('reports an unreachable API without throwing', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new Error('boom');
    })
  );

  render(Page);

  expect(await screen.findByText(/API unreachable: boom/)).toBeInTheDocument();
});
