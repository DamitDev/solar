import { render, screen, within } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import type { StatusMessage, StatusSummary, UptimePoint } from '../lib/status/types';
import Page from './+page.svelte';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function day(index: number, uptime: number, samples: number): UptimePoint {
  return { ts: `2026-01-${String(index + 1).padStart(2, '0')}T00:00:00+00:00`, uptime, samples };
}

function summary(overrides: Partial<StatusSummary> = {}): StatusSummary {
  return {
    generated_at: '2026-01-04T12:00:00+00:00',
    window_days: 90,
    bucket: '1d',
    virtual_models: [],
    aliases: [],
    instances: [],
    messages: [],
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
  // The window label describes the summary's 90-day window, not the length of
  // this component's (shorter) history.
  expect(alias).toHaveTextContent('90 days ago');

  expect(within(instance).getByRole('heading', { level: 3 })).toHaveTextContent('inst-1');
  expect(instance).toHaveTextContent('Down');
  expect(instance).toHaveTextContent('No data');
  expect(within(instance).queryByText(/Intent:/)).not.toBeInTheDocument();
});

it('labels virtual_models "Aliases" above aliases labelled "Models"', async () => {
  stubSummary(
    summary({
      virtual_models: [
        {
          name: 'core',
          targets: ['deepseek-v4.1-flash-1017:552b'],
          target_status: { 'deepseek-v4.1-flash-1017:552b': 'satisfied' },
          availability: 'operational',
          uptime_pct: 100,
          history: []
        }
      ],
      aliases: [
        {
          alias: 'deepseek-v4.1-flash-1017:552b',
          instances_running: 1,
          instances_total: 1,
          intent_phase: null,
          availability: 'operational',
          uptime_pct: 100,
          history: []
        }
      ]
    })
  );
  render(Page);

  const headings = (await screen.findAllByRole('heading', { level: 2 })).map((heading) =>
    heading.textContent?.trim()
  );
  expect(headings).toEqual(['Aliases', 'Models']);

  const aliases = within(screen.getByRole('heading', { name: 'Aliases' }).closest('section')!);
  expect(aliases.getByTestId('component-row')).toHaveTextContent('core');

  const models = within(screen.getByRole('heading', { name: 'Models' }).closest('section')!);
  expect(models.getByTestId('component-row')).toHaveTextContent('deepseek-v4.1-flash-1017:552b');
});

it('labels the uptime window from window_days, not the history length', async () => {
  stubSummary(
    summary({
      aliases: [
        {
          alias: 'young',
          instances_running: 1,
          instances_total: 1,
          intent_phase: null,
          availability: 'operational',
          uptime_pct: 100,
          history: [day(0, 1, 10)]
        }
      ]
    })
  );
  render(Page);

  const [row] = await screen.findAllByTestId('component-row');
  expect(row).toHaveTextContent('90 days ago');
  expect(row).not.toHaveTextContent('1 days ago');
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
  expect(bars[3]).not.toHaveAttribute('title');
  expect(bars[3]).toHaveTextContent('2026-01-04: no data (outage)');
  expect(bars[3].querySelector('[role="tooltip"]')).not.toBeNull();
  // A gap after the first sample is an outage: failure-colored.
  expect(bars[3]).toHaveClass('bg-red-900');
});

it('renders days before the first sample as empty, never outage red', async () => {
  stubSummary(
    summary({
      aliases: [
        {
          alias: 'young',
          instances_running: 1,
          instances_total: 1,
          intent_phase: null,
          availability: 'operational',
          uptime_pct: 100,
          history: [day(0, 0, 0), day(1, 0, 0), day(2, 1, 10), day(3, 1, 10)]
        }
      ]
    })
  );
  render(Page);

  const bars = await screen.findAllByTestId('uptime-day');
  expect(bars.map((bar) => bar.dataset.tone)).toEqual(['empty', 'empty', 'up', 'up']);
  expect(bars[0]).toHaveTextContent('2026-01-01: no data yet');
  // Pre-existence days are neutral, not outage-colored.
  expect(bars[0]).toHaveClass('bg-zinc-800');
  expect(bars[0].className).not.toContain('bg-red-900');
});

it('renders an unknown availability neutrally instead of down', async () => {
  stubSummary(
    summary({
      aliases: [
        {
          alias: 'core',
          instances_running: 0,
          instances_total: 0,
          intent_phase: null,
          availability: 'unknown',
          uptime_pct: null,
          history: []
        }
      ]
    })
  );
  render(Page);

  const [row] = await screen.findAllByTestId('component-row');
  expect(row).toHaveTextContent('Unknown');
  expect(row).not.toHaveTextContent('Down');
  const badge = row.querySelector('[data-availability]');
  expect(badge).toHaveAttribute('data-availability', 'unknown');
  expect(badge?.className).not.toContain('red');
});

function message(overrides: Partial<StatusMessage> & Pick<StatusMessage, 'id' | 'type'>) {
  return {
    body: `body ${overrides.id}`,
    start_time: '2026-01-04T10:00:00+00:00',
    end_time: '2026-01-04T14:00:00+00:00',
    ...overrides
  } satisfies StatusMessage;
}

it('renders active messages with body, type and window', async () => {
  stubSummary(
    summary({
      messages: [
        message({ id: 'a', type: 'incident', body: 'Core is slow' }),
        message({ id: 'b', type: 'resolution', end_time: null })
      ]
    })
  );
  render(Page);

  const items = await screen.findAllByTestId('message-item');
  expect(items).toHaveLength(2);
  expect(items[0]).toHaveTextContent('Incident');
  expect(items[0]).toHaveTextContent('Core is slow');
  expect(items[0]).toHaveTextContent('2026-01-04 10:00 UTC – 2026-01-04 14:00 UTC');
  expect(items[1]).toHaveTextContent('Resolution');
  expect(items[1]).toHaveTextContent('From 2026-01-04 10:00 UTC');
  expect(screen.getByRole('heading', { name: 'Messages' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Upcoming maintenance' })).not.toBeInTheDocument();
  expect(screen.queryByTestId('messages-empty')).not.toBeInTheDocument();
});

it('puts planned maintenance after generated_at in the upcoming section', async () => {
  stubSummary(
    summary({
      messages: [
        message({ id: 'now', type: 'maintenance' }),
        message({
          id: 'later',
          type: 'planned maintenance',
          start_time: '2026-01-10T00:00:00+00:00',
          end_time: '2026-01-10T06:00:00+00:00'
        }),
        message({ id: 'ongoing', type: 'planned maintenance' })
      ]
    })
  );
  render(Page);

  const upcoming = within(
    (await screen.findByRole('heading', { name: 'Upcoming maintenance' })).closest('section')!
  );
  expect(upcoming.getAllByTestId('message-item')).toHaveLength(1);
  expect(upcoming.getByTestId('message-item')).toHaveTextContent('body later');

  const active = within(screen.getByRole('heading', { name: 'Messages' }).closest('section')!);
  expect(active.getAllByTestId('message-item').map((item) => item.textContent)).toEqual([
    expect.stringContaining('body now'),
    expect.stringContaining('body ongoing')
  ]);
});

it('styles each message type with its own badge', async () => {
  stubSummary(
    summary({
      messages: [
        message({ id: '1', type: 'incident' }),
        message({ id: '2', type: 'maintenance' }),
        message({ id: '3', type: 'planned maintenance' }),
        message({ id: '4', type: 'resolution' })
      ]
    })
  );
  const { container } = render(Page);

  await screen.findAllByTestId('message-item');
  const badges = [...container.querySelectorAll<HTMLElement>('[data-message-type]')];
  expect(badges.map((badge) => badge.dataset.messageType)).toEqual([
    'incident',
    'maintenance',
    'planned maintenance',
    'resolution'
  ]);
  expect(badges.map((badge) => badge.textContent?.trim())).toEqual([
    'Incident',
    'Maintenance',
    'Planned maintenance',
    'Resolution'
  ]);
  expect(new Set(badges.map((badge) => badge.className)).size).toBe(4);
});

it('shows an empty state when there are no messages', async () => {
  stubSummary(summary());
  render(Page);

  expect(await screen.findByTestId('messages-empty')).toHaveTextContent(
    'No active incidents or upcoming maintenance.'
  );
  expect(screen.queryByTestId('message-item')).not.toBeInTheDocument();
});

it('still renders messages when no components are reported', async () => {
  stubSummary(summary({ messages: [message({ id: 'a', type: 'incident' })] }));
  render(Page);

  expect(await screen.findByTestId('message-item')).toBeInTheDocument();
  expect(screen.getByText('No components are reported yet.')).toBeInTheDocument();
});

it('polls the summary and refreshes when the tab becomes visible', async () => {
  vi.useFakeTimers();
  const fetchMock = stubSummary(summary());
  render(Page);

  await vi.advanceTimersByTimeAsync(0);
  expect(fetchMock).toHaveBeenCalledTimes(1);

  // A long-open tab must not keep showing stale "current" availability.
  await vi.advanceTimersByTimeAsync(60_000);
  expect(fetchMock).toHaveBeenCalledTimes(2);

  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(0);
  expect(fetchMock).toHaveBeenCalledTimes(3);
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function responseWith(body: StatusSummary) {
  return { ok: true, status: 200, json: async () => body };
}

it('keeps the newest summary when an older request resolves last', async () => {
  vi.useFakeTimers();
  const slowFirst = deferred<ReturnType<typeof responseWith>>();
  const fastSecond = deferred<ReturnType<typeof responseWith>>();
  const responses = [slowFirst.promise, fastSecond.promise];
  const fetchMock = vi.fn(() => responses.shift()!);
  vi.stubGlobal('fetch', fetchMock);

  render(Page);
  await vi.advanceTimersByTimeAsync(0);
  expect(fetchMock).toHaveBeenCalledTimes(1);

  // The 60 s poll starts a second request while the first is still in flight.
  await vi.advanceTimersByTimeAsync(60_000);
  expect(fetchMock).toHaveBeenCalledTimes(2);

  // The newer response lands first.
  fastSecond.resolve(
    responseWith(
      summary({ messages: [message({ id: 'fresh', type: 'incident', body: 'fresh summary' })] })
    )
  );
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.getByText('fresh summary')).toBeInTheDocument();

  // The stale one must not overwrite it when it finally arrives.
  slowFirst.resolve(
    responseWith(
      summary({ messages: [message({ id: 'stale', type: 'incident', body: 'stale summary' })] })
    )
  );
  await vi.advanceTimersByTimeAsync(0);
  expect(screen.queryByText('stale summary')).not.toBeInTheDocument();
  expect(screen.getByText('fresh summary')).toBeInTheDocument();
});
