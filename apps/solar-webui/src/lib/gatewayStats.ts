import { GatewayRequestSummary, GatewayStats } from '@/api/types';

/**
 * Fold live gateway_request events into REST stats without a refetch.
 *
 * The arithmetic mirrors the server's `read_stats` exactly:
 * - `token_uncached_total = token_in_total - token_cached_total`
 * - the cache-hit rate divides by the measured denominator, which is the sum
 *   of prompt tokens over cache-aware rows only (the server excludes NULL
 *   `cached_tokens` rows), so non-cache-aware requests never dilute it.
 *
 * The baseline's `token_in_measured_total` comes straight from the API and is
 * grown by each new cache-aware request's prompt tokens. Reverse-deriving it
 * from `cached / rate` would be wrong: a zero rate means "no hits yet", not
 * "no measured traffic", so a cold-start batch would throw away the real
 * denominator and the very first cached row would overstate the rate.
 */
export function mergeRealtimeStats(prev: GatewayStats, newItems: GatewayRequestSummary[]): GatewayStats {
  let {
    completed,
    missed,
    error,
    rerouted_requests,
    token_in_total,
    token_out_total,
    token_cached_total,
    token_in_measured_total,
  } = prev;
  let totalCompleted = completed + missed + error;
  for (const r of newItems) {
    if (r.status === 'success') completed++;
    else if (r.status === 'missed') missed++;
    else if (r.status === 'error') error++;
    if (r.attempts > 1) rerouted_requests++;
    token_in_total += r.prompt_tokens ?? 0;
    token_cached_total += r.cached_tokens ?? 0;
    token_out_total += r.completion_tokens ?? 0;
    // Only cache-aware requests contribute to the measured denominator,
    // mirroring the server's NULL-cached_tokens exclusion.
    if (r.cached_tokens != null && r.prompt_tokens != null) {
      token_in_measured_total += r.prompt_tokens;
    }
  }
  totalCompleted += newItems.length;
  return {
    ...prev,
    completed,
    missed,
    error,
    rerouted_requests,
    token_in_total,
    token_cached_total,
    token_uncached_total: token_in_total - token_cached_total,
    token_in_measured_total,
    cache_hit_rate: token_in_measured_total > 0 ? token_cached_total / token_in_measured_total : 0,
    token_out_total,
    avg_tokens_in: totalCompleted > 0 ? token_in_total / totalCompleted : 0,
    avg_tokens_out: totalCompleted > 0 ? token_out_total / totalCompleted : 0,
  };
}
