import { describe, expect, it } from 'vitest';
import { GatewayRequestSummary, GatewayStats } from '@/api/types';
import { mergeRealtimeStats } from '../gatewayStats';

function baseline(over: Partial<GatewayStats> = {}): GatewayStats {
  return {
    from: '2026-08-17T00:00:00',
    to: '2026-08-17T01:00:00',
    completed: 50,
    missed: 0,
    error: 0,
    rerouted_requests: 0,
    token_in_total: 5000,
    token_cached_total: 0,
    token_uncached_total: 5000,
    token_in_measured_total: 5000,
    cache_hit_rate: 0,
    token_out_total: 2000,
    avg_tokens_in: 100,
    avg_tokens_out: 40,
    models: [],
    hosts: [],
    ...over,
  };
}

function live(over: Partial<GatewayRequestSummary> = {}): GatewayRequestSummary {
  return {
    request_id: 'req-1',
    status: 'success',
    attempts: 1,
    end_timestamp: '2026-08-17T01:00:01',
    prompt_tokens: 200,
    completion_tokens: 150,
    ...over,
  };
}

describe('mergeRealtimeStats', () => {
  it('grows a cold-start baseline without overstating the rate', () => {
    // Regression for the live-rate bug: a 1h window of 50 cold llama.cpp
    // requests (5000 prompt tokens, zero hits) reports rate 0. The next live
    // request has 200 prompt tokens with 100 cached. The truth is
    // 100 / 5200 ≈ 1.9%, not 50%.
    const stats = mergeRealtimeStats(baseline(), [live({ request_id: 'req-new', cached_tokens: 100 })]);

    expect(stats.token_in_measured_total).toBe(5200);
    expect(stats.token_cached_total).toBe(100);
    expect(stats.cache_hit_rate).toBeCloseTo(100 / 5200, 6);
  });

  it('adds only cache-aware rows to the measured denominator', () => {
    const stats = mergeRealtimeStats(
      baseline({ token_in_measured_total: 700, token_cached_total: 300, cache_hit_rate: 300 / 700 }),
      [
        // Cache-aware: contributes prompt tokens to the denominator.
        live({ request_id: 'req-cached', prompt_tokens: 200, cached_tokens: 50 }),
        // HuggingFace: counts toward totals but must not dilute the rate.
        live({ request_id: 'req-hf', prompt_tokens: 900 }),
      ],
    );

    expect(stats.token_in_total).toBe(5000 + 200 + 900);
    expect(stats.token_in_measured_total).toBe(700 + 200);
    expect(stats.token_cached_total).toBe(300 + 50);
    expect(stats.token_uncached_total).toBe(5000 + 200 + 900 - (300 + 50));
    expect(stats.cache_hit_rate).toBeCloseTo(350 / 900, 6);
  });

  it('keeps the rate at zero while there are still no hits', () => {
    const stats = mergeRealtimeStats(baseline(), [
      live({ request_id: 'req-cold', prompt_tokens: 300, cached_tokens: 0 }),
    ]);

    expect(stats.token_cached_total).toBe(0);
    expect(stats.token_in_measured_total).toBe(5300);
    expect(stats.cache_hit_rate).toBe(0);
  });

  it('counts statuses and rerouted attempts like the old merge', () => {
    const stats = mergeRealtimeStats(baseline({ completed: 5, missed: 1, error: 1, rerouted_requests: 2 }), [
      live({ request_id: 'req-ok' }),
      live({ request_id: 'req-missed', status: 'missed', attempts: 2 }),
      live({ request_id: 'req-err', status: 'error', attempts: 3 }),
    ]);

    expect(stats.completed).toBe(6);
    expect(stats.missed).toBe(2);
    expect(stats.error).toBe(2);
    expect(stats.rerouted_requests).toBe(4);
  });

  it('recomputes the per-request averages over the summed statuses', () => {
    const stats = mergeRealtimeStats(baseline({ completed: 1, token_in_total: 100, token_out_total: 40 }), [
      live({ request_id: 'req-2', prompt_tokens: 300, completion_tokens: 100 }),
    ]);

    expect(stats.avg_tokens_in).toBeCloseTo((100 + 300) / 2, 6);
    expect(stats.avg_tokens_out).toBeCloseTo((40 + 100) / 2, 6);
  });
});
