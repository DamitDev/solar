import type { LogMessage, RoutingState } from '@/api/types';
import type { InstanceStateData, RequestState } from '@/hooks/eventStream/useEventStream';
import type { RegisteredHandler } from './types';

export const handleLog: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (event.host_id && event.instance_id && event.data) {
    const key = `${event.host_id}:${event.instance_id}`;
    const logMsg: LogMessage = {
      seq: event.data.seq,
      timestamp: event.timestamp || new Date().toISOString(),
      line: event.data.line,
    };
    ctx.setLogs((prev) => {
      const newMap = new Map(prev);
      const existing = newMap.get(key) || [];
      // Keep last 1000 logs
      const updated = [...existing, logMsg].slice(-1000);
      newMap.set(key, updated);
      return newMap;
    });
    h.onLog?.(event.host_id, event.instance_id, event.data);
  }
};

export const handleInstanceState: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  // Dropped while awaiting the authoritative snapshot; the snapshot
  // provides the reset base and raced deltas would corrupt it.
  if (ctx.awaitingSnapshotRef.current) return;
  if (event.host_id && event.instance_id && event.data) {
    const key = `${event.host_id}:${event.instance_id}`;
    ctx.setInstanceStates((prev) => {
      const newMap = new Map(prev);
      newMap.set(key, event.data);
      return newMap;
    });
    h.onInstanceState?.(event.host_id, event.instance_id, event.data);
  }
};

export const handleRoutingSnapshot: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (event.data) {
    // Reset the routing view to the authoritative server snapshot.
    const snap = event.data as RoutingState;
    const requestMap: Map<string, RequestState> = (snap.active_requests ?? []).reduce((acc, r) => {
      acc.set(r.request_id, {
        request_id: r.request_id,
        model: r.model ?? undefined,
        resolved_model: r.resolved_model ?? undefined,
        host_id: r.host_id ?? undefined,
        host_name: r.host_name ?? undefined,
        instance_id: r.instance_id ?? undefined,
        timestamp: r.timestamp ?? new Date().toISOString(),
        status: r.status === 'queued' ? 'pending' : 'processing',
      });
      return acc;
    }, new Map());
    ctx.setRequests(() => requestMap);
    const states: Map<string, InstanceStateData> = (snap.instance_states ?? []).reduce((acc, s) => {
      acc.set(`${s.host_id}:${s.instance_id}`, s.data);
      return acc;
    }, new Map());
    ctx.setInstanceStates(() => states);
    if (Array.isArray(snap.endpoints)) {
      ctx.setEndpoints(() => snap.endpoints);
    }
    ctx.awaitingSnapshotRef.current = false;
    h.onRoutingSnapshot?.(snap);
  }
};
