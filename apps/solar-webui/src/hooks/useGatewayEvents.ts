import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import solarClient from '@/api/client';
import { ParsedEvent, parseGatewayEvent } from '@/lib/gatewayErrors';

interface Options {
  from: string;
  to: string;
  endpointId?: string | null;
  /** Poll for new events while the page is in live mode. */
  live?: boolean;
  limit?: number;
}

interface Result {
  events: ParsedEvent[];
  loading: boolean;
  error: string | null;
  /** True when the server had more events than `limit` for this range. */
  truncated: boolean;
  refresh: () => void;
}

const POLL_MS = 15_000;

/**
 * Owns the gateway events for the current filters.
 *
 * The panel used to read from the app-wide routing event accumulator, which
 * merged every fetch into one ever-growing list: switching endpoint or time
 * range added rows instead of replacing them, so the panel showed a union of
 * everything ever viewed. Here each fetch replaces the list outright, and the
 * range and endpoint are part of the request rather than applied afterwards.
 */
export function useGatewayEvents({ from, to, endpointId, live = false, limit = 500 }: Options): Result {
  const [events, setEvents] = useState<ParsedEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const tokenRef = useRef(0);

  const fetchEvents = useCallback(async () => {
    const token = ++tokenRef.current;
    setLoading(true);
    try {
      const res = await solarClient.getRecentGatewayEvents({
        from,
        to,
        limit,
        types: 'request_error,request_reroute',
        endpoint_id: endpointId ?? undefined,
      });
      // A newer fetch already won; its results must not be overwritten.
      if (token !== tokenRef.current) return;
      const items = res.items ?? [];
      setEvents(items.map(parseGatewayEvent));
      setTruncated(items.length >= limit);
      setError(null);
    } catch (err) {
      if (token !== tokenRef.current) return;
      setError(err instanceof Error ? err.message : 'Failed to load gateway events');
      setEvents([]);
    } finally {
      if (token === tokenRef.current) setLoading(false);
    }
  }, [from, to, endpointId, limit]);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  useEffect(() => {
    if (!live) return;
    const id = setInterval(fetchEvents, POLL_MS);
    return () => clearInterval(id);
  }, [live, fetchEvents]);

  const sorted = useMemo(
    () => [...events].sort((a, b) => (b.timestamp ?? '').localeCompare(a.timestamp ?? '')),
    [events],
  );

  return { events: sorted, loading, error, truncated, refresh: fetchEvents };
}
