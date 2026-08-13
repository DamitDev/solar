import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EventsPanel } from '../gateway/EventsPanel';
import solarClient from '@/api/client';
import { GatewayEventDTO } from '@/api/types';

vi.mock('@/api/client', () => ({
  default: { getRecentGatewayEvents: vi.fn() },
}));

const getEvents = vi.mocked(solarClient.getRecentGatewayEvents);

function upstreamError(message: string, timestamp: string, model = 'qwen3.6:35b'): GatewayEventDTO {
  return {
    type: 'request_error',
    data: {
      request_id: `req-${timestamp}`,
      model,
      error_message: message,
      duration: 1.25,
      timestamp,
    },
  };
}

function respondWith(items: GatewayEventDTO[]) {
  getEvents.mockResolvedValue({ from: 'x', to: 'y', types: [], items });
}

const RANGE = { from: '2026-08-13T00:00:00Z', to: '2026-08-13T12:00:00Z' };

beforeEach(() => {
  getEvents.mockReset();
});

describe('EventsPanel', () => {
  it('renders the upstream message instead of the raw JSON envelope', async () => {
    respondWith([
      upstreamError(
        'Request failed: 400 - {"error":{"message":"context length exceeded","code":"context_length_exceeded"}}',
        '2026-08-13T11:00:00Z',
      ),
    ]);

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);

    expect(await screen.findByText('context length exceeded')).toBeInTheDocument();
    expect(screen.queryByText(/"error":/)).not.toBeInTheDocument();
    expect(screen.getByText('400')).toBeInTheDocument();
  });

  it('reveals the original payload only when the row is expanded', async () => {
    const user = userEvent.setup();
    respondWith([upstreamError('Request failed: 500 - {"error":{"message":"boom"}}', '2026-08-13T11:00:00Z')]);

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);
    await screen.findByText('boom');

    expect(screen.queryByText(/"message": "boom"/)).not.toBeInTheDocument();

    await user.click(screen.getByText('boom'));

    expect(screen.getByText(/"message": "boom"/)).toBeInTheDocument();
  });

  it('passes the endpoint and range to the API rather than filtering afterwards', async () => {
    respondWith([]);

    render(<EventsPanel {...RANGE} endpointId="ep-1" live={false} />);

    await waitFor(() =>
      expect(getEvents).toHaveBeenCalledWith(
        expect.objectContaining({ ...RANGE, endpoint_id: 'ep-1', types: 'request_error,request_reroute' }),
      ),
    );
  });

  it('replaces the list when the endpoint changes instead of accumulating', async () => {
    respondWith([upstreamError('Request failed: 500 - first endpoint', '2026-08-13T11:00:00Z')]);
    const { rerender } = render(<EventsPanel {...RANGE} endpointId="ep-1" live={false} />);
    await screen.findByText('first endpoint');

    respondWith([upstreamError('Request failed: 500 - second endpoint', '2026-08-13T11:30:00Z')]);
    rerender(<EventsPanel {...RANGE} endpointId="ep-2" live={false} />);

    expect(await screen.findByText('second endpoint')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('first endpoint')).not.toBeInTheDocument());
  });

  it('resets the list when the time range changes', async () => {
    respondWith([upstreamError('Request failed: 500 - old range', '2026-08-13T11:00:00Z')]);
    const { rerender } = render(<EventsPanel {...RANGE} endpointId={null} live={false} />);
    await screen.findByText('old range');

    respondWith([]);
    rerender(<EventsPanel from="2026-08-13T06:00:00Z" to={RANGE.to} endpointId={null} live={false} />);

    expect(await screen.findByText('No errors or reroutes in this range')).toBeInTheDocument();
  });

  it('collapses repeats into a single counted row', async () => {
    respondWith([
      upstreamError('Request failed: 503 - {"error":{"message":"no slot 3 free"}}', '2026-08-13T11:00:00Z'),
      upstreamError('Request failed: 503 - {"error":{"message":"no slot 9 free"}}', '2026-08-13T11:05:00Z'),
    ]);

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);

    expect(await screen.findByText('×2')).toBeInTheDocument();
    expect(screen.getByText(/2 errors/)).toBeInTheDocument();
  });

  it('lists every occurrence separately once grouping is switched off', async () => {
    const user = userEvent.setup();
    respondWith([
      upstreamError('Request failed: 503 - {"error":{"message":"no slot 3 free"}}', '2026-08-13T11:00:00Z'),
      upstreamError('Request failed: 503 - {"error":{"message":"no slot 9 free"}}', '2026-08-13T11:05:00Z'),
    ]);

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);
    await screen.findByText('×2');

    await user.click(screen.getByRole('button', { name: 'Group similar' }));

    expect(screen.getByText('no slot 3 free')).toBeInTheDocument();
    expect(screen.getByText('no slot 9 free')).toBeInTheDocument();
    expect(screen.queryByText('×2')).not.toBeInTheDocument();
  });

  it('orders newest first', async () => {
    respondWith([
      upstreamError('Request failed: 500 - older', '2026-08-13T09:00:00Z'),
      upstreamError('Request failed: 500 - newer', '2026-08-13T11:00:00Z'),
    ]);

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);
    await screen.findByText('newer');

    const rendered = screen.getAllByText(/older|newer/).map((n) => n.textContent);
    expect(rendered).toEqual(['newer', 'older']);
  });

  it('separates reroutes from errors in the summary', async () => {
    respondWith([
      upstreamError('Request failed: 500 - boom', '2026-08-13T11:00:00Z'),
      { type: 'request_reroute', data: { attempt: 2, reason: 'connect_error', timestamp: '2026-08-13T11:01:00Z' } },
    ]);

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);

    expect(await screen.findByText(/1 error • 1 reroute/)).toBeInTheDocument();
    expect(screen.getByText('Instance unreachable, rerouted')).toBeInTheDocument();
  });

  it('surfaces a fetch failure without wiping the panel chrome', async () => {
    getEvents.mockRejectedValue(new Error('network down'));

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);

    expect(await screen.findByText('network down')).toBeInTheDocument();
    expect(screen.getByText('Errors & Reroutes')).toBeInTheDocument();
  });

  it('refetches on demand', async () => {
    const user = userEvent.setup();
    respondWith([]);
    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);
    await waitFor(() => expect(getEvents).toHaveBeenCalledTimes(1));

    await user.click(screen.getByTitle('Refresh events'));

    await waitFor(() => expect(getEvents).toHaveBeenCalledTimes(2));
  });

  it('expands a group to list its occurrences', async () => {
    const user = userEvent.setup();
    respondWith([
      upstreamError('Request failed: 503 - {"error":{"message":"busy 1"}}', '2026-08-13T11:00:00Z', 'model-a'),
      upstreamError('Request failed: 503 - {"error":{"message":"busy 2"}}', '2026-08-13T11:05:00Z', 'model-b'),
    ]);

    render(<EventsPanel {...RANGE} endpointId={null} live={false} />);
    const row = await screen.findByText(/busy \d/);

    await user.click(row);

    const occurrences = screen.getByText('Occurrences').parentElement as HTMLElement;
    expect(within(occurrences).getByText('model-a')).toBeInTheDocument();
    expect(within(occurrences).getByText('model-b')).toBeInTheDocument();
  });
});
