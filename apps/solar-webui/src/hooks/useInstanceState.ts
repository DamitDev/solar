/**
 * useInstanceState - Hook for getting instance runtime state
 *
 * Pure Socket.IO consumer: reads instance_state events from EventStreamContext.
 * No REST polling -- all runtime state comes through the event stream.
 */

import { useMemo } from 'react';
import { InstanceRuntimeState } from '@/api/types';
import { useEventStreamContext } from '@/context/EventStreamContext';

export function useInstanceState(hostId: string, instanceId: string) {
  let eventStreamState: any = undefined;
  let eventStreamConnected = false;

  try {
    const ctx = useEventStreamContext();
    eventStreamState = ctx.getInstanceState(hostId, instanceId);
    eventStreamConnected = ctx.isConnected;
  } catch {
    // Not inside EventStreamProvider
  }

  const state = useMemo<InstanceRuntimeState | null>(() => {
    if (!eventStreamState) return null;
    return {
      instance_id: instanceId,
      busy: eventStreamState.busy || false,
      phase: eventStreamState.phase || 'idle',
      prefill_progress: eventStreamState.prefill_progress,
      active_slots: eventStreamState.active_slots || 0,
      slot_id: eventStreamState.slot_id,
      task_id: eventStreamState.task_id,
      prefill_prompt_tokens: eventStreamState.prefill_prompt_tokens,
      generated_tokens: eventStreamState.generated_tokens,
      decode_tps: eventStreamState.decode_tps,
      decode_ms_per_token: eventStreamState.decode_ms_per_token,
      checkpoint_index: eventStreamState.checkpoint_index,
      checkpoint_total: eventStreamState.checkpoint_total,
      timestamp: new Date().toISOString(),
    };
  }, [eventStreamState, instanceId]);

  return {
    state,
    connected: eventStreamConnected,
  };
}
