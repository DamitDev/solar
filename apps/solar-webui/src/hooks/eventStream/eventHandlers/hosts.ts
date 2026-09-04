import type { PendingHost } from '@/api/types';
import type { HostStatusData } from '@/hooks/eventStream/useEventStream';
import type { RegisteredHandler } from './types';

export const handleInitialStatus: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (Array.isArray(event.data)) {
    const hostMap = new Map<string, HostStatusData>();
    event.data.forEach((host: HostStatusData) => {
      hostMap.set(host.host_id, host);
    });
    ctx.setHosts(() => hostMap);
    h.onInitialStatus?.(event.data);
  }
};

export const handleHostStatus: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (event.data) {
    ctx.setHosts((prev) => {
      const newMap = new Map(prev);
      newMap.set(event.data.host_id, event.data);
      return newMap;
    });
    h.onHostStatus?.(event.data);
  }
};

export const handleHostPending: RegisteredHandler = (event, ctx) => {
  if (event.data?.pending_id) {
    ctx.setPendingHosts((prev) => {
      const newMap = new Map(prev);
      newMap.set(event.data.pending_id, event.data as PendingHost);
      return newMap;
    });
  }
};

export const handleHostPendingRemoved: RegisteredHandler = (event, ctx) => {
  if (event.data?.pending_id) {
    ctx.setPendingHosts((prev) => {
      const newMap = new Map(prev);
      newMap.delete(event.data.pending_id);
      return newMap;
    });
  }
};

export const handleInstancesUpdate: RegisteredHandler = (event, ctx) => {
  if (event.data?.host_id && Array.isArray(event.data?.instances)) {
    ctx.setHostInstances((prev) => {
      const newMap = new Map(prev);
      newMap.set(event.data.host_id, event.data.instances);
      return newMap;
    });
  }
};

export const handleHostHealth: RegisteredHandler = (event, ctx) => {
  if (event.host_id && event.data) {
    const hostId = event.host_id;
    ctx.setHosts((prev) => {
      const newMap = new Map(prev);
      const existing = newMap.get(hostId);
      if (existing) {
        newMap.set(hostId, {
          ...existing,
          memory: event.data.memory ?? existing.memory,
          ...(event.data.gpu_type && { gpu_type: event.data.gpu_type }),
          ...(event.data.roles && { roles: event.data.roles }),
          ...(event.data.disk_total_gb != null && { disk_total_gb: event.data.disk_total_gb }),
          ...(event.data.disk_used_gb != null && { disk_used_gb: event.data.disk_used_gb }),
          ...(event.data.disk_available_gb != null && { disk_available_gb: event.data.disk_available_gb }),
          ...(event.data.memory_available_gb != null && {
            memory_available_gb: event.data.memory_available_gb,
          }),
          ...(event.data.version && { version: event.data.version }),
        });
      }
      return newMap;
    });
  }
};
