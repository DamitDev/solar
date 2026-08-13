import { useCallback, useEffect, useRef, useState } from 'react';
import solarClient from '@/api/client';
import { GatewayGroupBy, GatewayTimeseries } from '@/api/types';

interface Options {
  from: string;
  to: string;
  groupBy?: GatewayGroupBy;
  endpointId?: string | null;
  requestType?: string | null;
}

interface Result {
  data: GatewayTimeseries | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

/**
 * Fetch the bucketed gateway series for the current filters.
 *
 * Every fetch is guarded by a token: changing the time range or endpoint while
 * a slower request is still in flight must not let the stale response win, or
 * the chart ends up showing a range the user already moved away from.
 */
export function useGatewayTimeseries({ from, to, groupBy = 'none', endpointId, requestType }: Options): Result {
  const [data, setData] = useState<GatewayTimeseries | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef(0);

  const fetchSeries = useCallback(async () => {
    const token = ++tokenRef.current;
    setLoading(true);
    try {
      const res = await solarClient.getGatewayTimeseries({
        from,
        to,
        group_by: groupBy,
        endpoint_id: endpointId ?? undefined,
        request_type: requestType && requestType !== 'all' ? requestType : undefined,
      });
      if (token !== tokenRef.current) return;
      setData(res);
      setError(null);
    } catch (err) {
      if (token !== tokenRef.current) return;
      setError(err instanceof Error ? err.message : 'Failed to load gateway traffic');
    } finally {
      if (token === tokenRef.current) setLoading(false);
    }
  }, [from, to, groupBy, endpointId, requestType]);

  useEffect(() => {
    fetchSeries();
  }, [fetchSeries]);

  return { data, loading, error, refresh: fetchSeries };
}
