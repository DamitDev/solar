import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useInstanceState } from '@/hooks/useInstanceState';

let throwOnContext = false;

vi.mock('@/context/EventStreamContext', () => ({
  useEventStreamContext: () => {
    if (throwOnContext) {
      throw new Error('Not inside EventStreamProvider');
    }
    return {
      isConnected: true,
      getInstanceState: () => ({
        busy: true,
        phase: 'running',
        decode_tps: 12.5,
        generated_tokens: 42,
      }),
    };
  },
}));

describe('useInstanceState', () => {
  beforeEach(() => {
    throwOnContext = false;
  });

  it('returns null state when no EventStreamProvider is present (no crash)', () => {
    throwOnContext = true;
    const { result } = renderHook(() => useInstanceState('host-1', 'i1'));
    expect(result.current).toEqual({ state: null, connected: false });
  });

  it('maps event stream state into InstanceRuntimeState', () => {
    const { result } = renderHook(() => useInstanceState('host-1', 'i1'));
    expect(result.current.connected).toBe(true);
    expect(result.current.state).toEqual(
      expect.objectContaining({
        instance_id: 'i1',
        busy: true,
        phase: 'running',
        decode_tps: 12.5,
        generated_tokens: 42,
      }),
    );
  });
});
