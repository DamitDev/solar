import { describe, expect, it } from 'vitest';
import { Intent } from '@/api/types';
import { sortIntents } from '@/lib/intents';

function intent(alias: string, phase: string, updatedAt = '2026-08-05T00:00:00Z'): Intent {
  return {
    id: `intent-${alias}`,
    alias,
    model_source: 'huggingface://org/model',
    replicas: 1,
    priority: 'production',
    strategy: 'rolling',
    backend: { backend_type: 'llamacpp' },
    placement: { roles: ['inference'], gpu_type: null, host_allow: [], host_deny: [] },
    resources: { vram_gb: null, ram_gb: null },
    metadata: {},
    status: {
      phase,
      reconcile: 'idle',
      desired_replicas: 1,
      observed_replicas: 0,
      ready_replicas: 0,
      updated_replicas: 0,
      available: false,
      shortfall: 0,
      replica_set: [],
      conditions: [],
      strategy_progress: null,
      last_error: null,
      updated_at: updatedAt,
    },
  } as Intent;
}

describe('sortIntents', () => {
  it('orders by phase first', () => {
    const ready = intent('beta', 'ready');
    const reconciling = intent('alpha', 'reconciling');
    const failed = intent('gamma', 'failed');

    expect(sortIntents([ready, reconciling, failed]).map((i) => i.alias)).toEqual([
      'alpha', // reconciling
      'gamma', // failed
      'beta', // ready
    ]);
  });

  it('orders by alias within the same phase', () => {
    const a = intent('zulu', 'ready');
    const b = intent('alpha', 'ready');
    const c = intent('mid', 'ready');

    expect(sortIntents([a, b, c]).map((i) => i.alias)).toEqual(['alpha', 'mid', 'zulu']);
  });

  it('is stable across updates — a status touch does not move the row', () => {
    const a = intent('alpha', 'ready', '2026-08-05T00:00:00Z');
    const b = intent('beta', 'ready', '2026-08-05T00:00:00Z');
    // beta gets updated — with updated_at sorting it would jump to the top.
    const bUpdated = { ...b, status: { ...b.status, updated_at: '2026-08-05T01:00:00Z' } };

    expect(sortIntents([a, bUpdated]).map((i) => i.alias)).toEqual(['alpha', 'beta']);
  });

  it('puts unknown phases last', () => {
    const known = intent('alpha', 'ready');
    const unknown = intent('zzz', 'mystery-phase');

    expect(sortIntents([unknown, known]).map((i) => i.alias)).toEqual(['alpha', 'zzz']);
  });

  it('does not mutate the input array', () => {
    const input = [intent('b', 'ready'), intent('a', 'ready')];
    const original = [...input];
    sortIntents(input);
    expect(input).toEqual(original);
  });
});
