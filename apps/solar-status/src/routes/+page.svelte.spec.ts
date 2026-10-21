import { render, screen, within } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import type { StatusSummary, UptimePoint } from '../lib/status/types';
import Page from './+page.svelte';

afterEach(() => {
  vi.unstubAllGlobals();
});

function day(index: number, uptime: number, samples: number): UptimePoint {
  return { ts: `2026-01-${String(index + 1).padStart(2, '0')}T00:00:00+00:00`, uptime, samples };
}

function summary(overrides: Partial<StatusSummary> = {}): StatusSummary {
  return {
    generated_at: '2026-01-04T12:00:00+00:00',
    window_days: 4,
    bucket: '1d',
    virtual_models: [],
    aliases: [],
    instances: [],
    ...overrides
  };
}

function stubSummary(body: StatusSummary) {
  const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

it('renders the status page heading', () => {
  stubSummary(summary());
  render(Page);

  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Solar status');
});

it('shows a loading state until the summary arrives', async () => {
  stubSummary(summary());
  render(Page);

  expect(screen.getByRole('status')).toHaveTextContent('Loading status');
  expect(await screen.findByText('No components are reported yet.')).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('fetches the public status endpoint once without credentials', async () => {
  const fetchMock = stubSummary(summary());
  render(Page);

  await screen.findByText('No components are reported yet.');
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

  expect(await screen.findByRole('alert')).toHaveTextContent('status API is unreachable');
});

it('treats an HTTP error as unreachable', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: false, status: 500 }))
  );
  render(Page);

  expect(await screen.findByRole('alert')).toBeInTheDocument();
});

it('renders models, aliases and instances with status, phase and uptime', async () => {
  const history = [day(0, 1, 10), day(1, 0.5, 10), day(2, 0, 10), day(3, 0, 0)];
  stubSummary(
    summary({
      virtual_models: [
        {
          name: 'smart',
          targets: ['core', 'wind'],
          target_status: { core: 'satisfied', wind: 'missing' },
          availability: 'degraded',
          uptime_pct: 87.5,
          history
        }
      ],
      aliases: [
        {
          alias: 'core',
          instances_running: 1,
          instances_total: 2,
          intent_phase: 'degraded',
          availability: 'operational',
          uptime_pct: 99.123,
          history
        }
      ],
      instances: [
        {
          instance_id: 'inst-1',
          alias: 'standalone',
          availability: 'down',
          uptime_pct: null,
          history: []
        }
      ]
    })
  );
  render(Page);

  const rows = await screen.findAllByTestId('component-row');
  expect(rows).toHaveLength(3);

  const [model, alias, instance] = rows;
  expect(within(model).getByRole('heading', { level: 3 })).toHaveTextContent('smart');
  expect(model).toHaveTextContent('Targets: core → wind');
  expect(model).toHaveTextContent('Degraded');
  expect(model).toHaveTextContent('87.50% uptime');

  expect(within(alias).getByRole('heading', { level: 3 })).toHaveTextContent('core');
  expect(alias).toHaveTextContent('Operational');
  expect(alias).toHaveTextContent('Intent: degraded');
  expect(alias).toHaveTextContent('1/2 instances running');
  expect(alias).toHaveTextContent('99.12% uptime');

  expect(within(instance).getByRole('heading', { level: 3 })).toHaveTextContent('inst-1');
  expect(instance).toHaveTextContent('Down');
  expect(instance).toHaveTextContent('No data');
  expect(within(instance).queryByText(/Intent:/)).not.toBeInTheDocument();
});

it('draws one bar per day and renders outage days as gaps, not interpolated', async () => {
  stubSummary(
    summary({
      aliases: [
        {
          alias: 'core',
          instances_running: 1,
          instances_total: 1,
          intent_phase: null,
          availability: 'operational',
          uptime_pct: 62.5,
          history: [day(0, 1, 10), day(1, 0.5, 10), day(2, 0, 10), day(3, 0, 0)]
        }
      ]
    })
  );
  render(Page);

  const bars = await screen.findAllByTestId('uptime-day');
  expect(bars.map((bar) => bar.dataset.tone)).toEqual(['up', 'partial', 'down', 'gap']);
  expect(bars[3]).toHaveAttribute('title', '2026-01-04: no data (outage)');
});
