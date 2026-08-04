import { useState, useEffect, useCallback, useMemo } from 'react';
import solarClient from '@/api/client';
import { Host, Instance, InstanceStatus } from '@/api/types';
import { useRoutingEventsContext } from '@/context/RoutingEventsContext';
import { InstanceSummary } from '@/hooks/useEventStream';

interface HostWithInstances extends Host {
  instances: Instance[];
}

// --- localStorage helpers for host ordering ---

const HOST_ORDER_KEY = 'solar_host_order';
const INSTANCE_ORDER_KEY_PREFIX = 'solar_instance_order_';

function readHostOrder(): string[] {
  try {
    const raw = localStorage.getItem(HOST_ORDER_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    /* ignore */
  }
  return [];
}

function writeHostOrder(order: string[]) {
  localStorage.setItem(HOST_ORDER_KEY, JSON.stringify(order));
}

function readInstanceOrder(hostId: string): string[] {
  try {
    const raw = localStorage.getItem(INSTANCE_ORDER_KEY_PREFIX + hostId);
    if (raw) return JSON.parse(raw);
  } catch {
    /* ignore */
  }
  return [];
}

function writeInstanceOrder(hostId: string, order: string[]) {
  localStorage.setItem(INSTANCE_ORDER_KEY_PREFIX + hostId, JSON.stringify(order));
}

function applySavedOrder<T extends { id: string }>(items: T[], savedOrder: string[]): T[] {
  if (savedOrder.length === 0) return items;

  const orderMap = new Map(savedOrder.map((id, idx) => [id, idx]));
  const sorted = [...items].sort((a, b) => {
    const ia = orderMap.get(a.id);
    const ib = orderMap.get(b.id);
    if (ia !== undefined && ib !== undefined) return ia - ib;
    if (ia !== undefined) return -1;
    if (ib !== undefined) return 1;
    return 0;
  });
  return sorted;
}

/**
 * Merge Socket.IO InstanceSummary data into the full REST Instance objects.
 * Updates status fields from the socket data; preserves full config from REST.
 * If the socket reports instances that don't exist in REST data, returns
 * their IDs so the caller can lazy-hydrate them.
 */
function mergeInstanceData(
  restInstances: Instance[],
  socketInstances: InstanceSummary[] | undefined,
): { merged: Instance[]; unknownIds: string[] } {
  if (!socketInstances) return { merged: restInstances, unknownIds: [] };

  const restMap = new Map(restInstances.map((i) => [i.id, i]));
  const merged: Instance[] = [];
  const unknownIds: string[] = [];

  for (const si of socketInstances) {
    const existing = restMap.get(si.id);
    if (existing) {
      merged.push({
        ...existing,
        status: (si.status as InstanceStatus) || existing.status,
      });
      restMap.delete(si.id);
    } else {
      unknownIds.push(si.id);
      merged.push({
        id: si.id,
        config: {
          backend_type: (si.backend_type || 'llamacpp') as any,
          alias: si.alias || si.id,
          host: '0.0.0.0',
          port: si.port || 0,
          model: '',
        } as any,
        status: (si.status as InstanceStatus) || 'stopped',
        port: si.port || 0,
        created_at: new Date().toISOString(),
        retry_count: 0,
        supported_endpoints: si.supported_endpoints || [],
      } as Instance);
    }
  }

  return { merged, unknownIds };
}

export function useInstances() {
  const [hosts, setHosts] = useState<HostWithInstances[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { hostStatuses, hostInstances, routingConnected } = useRoutingEventsContext();

  const [hostOrder, setHostOrder] = useState<string[]>(readHostOrder);
  const [instanceOrders, setInstanceOrders] = useState<Record<string, string[]>>(() => ({}));

  const fetchData = useCallback(async () => {
    try {
      setError(null);
      const hostsData = await solarClient.getHosts();

      const hostsWithInstances = await Promise.all(
        hostsData.map(async (host) => {
          try {
            const instances = await solarClient.getHostInstances(host.id);
            return { ...host, instances };
          } catch {
            return { ...host, instances: [] };
          }
        }),
      );

      setHosts(hostsWithInstances);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  }, []);

  // One-time REST fetch on mount
  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Lazy hydration: when Socket.IO reports unknown instance IDs, fetch that
  // host's instances via REST once to get full config data.
  useEffect(() => {
    if (!hostInstances || hostInstances.size === 0) return;

    const hostsToHydrate: string[] = [];
    for (const [hostId, socketInsts] of hostInstances) {
      const host = hosts.find((h) => h.id === hostId);
      if (!host) continue;
      const restIds = new Set(host.instances.map((i) => i.id));
      const hasUnknown = socketInsts.some((si) => !restIds.has(si.id));
      if (hasUnknown) hostsToHydrate.push(hostId);
    }

    if (hostsToHydrate.length === 0) return;

    let cancelled = false;
    (async () => {
      for (const hostId of hostsToHydrate) {
        if (cancelled) break;
        try {
          const instances = await solarClient.getHostInstances(hostId);
          if (cancelled) break;
          setHosts((prev) => prev.map((h) => (h.id === hostId ? { ...h, instances } : h)));
        } catch {
          /* host might be offline, the socket data is enough */
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [hostInstances, hosts]);

  // When a new host appears in hostStatuses but not in our hosts list, add it
  useEffect(() => {
    if (!hostStatuses || hostStatuses.size === 0) return;
    const existingIds = new Set(hosts.map((h) => h.id));
    const newHostIds = [...hostStatuses.keys()].filter((id) => !existingIds.has(id));
    if (newHostIds.length === 0) return;

    let cancelled = false;
    (async () => {
      for (const hostId of newHostIds) {
        if (cancelled) break;
        try {
          const allHosts = await solarClient.getHosts();
          const newHost = allHosts.find((h) => h.id === hostId);
          if (!newHost || cancelled) continue;
          let instances: Instance[] = [];
          try {
            instances = await solarClient.getHostInstances(hostId);
          } catch {
            /* ok */
          }
          if (cancelled) break;
          setHosts((prev) => {
            if (prev.some((h) => h.id === hostId)) return prev;
            return [...prev, { ...newHost, instances }];
          });
        } catch {
          /* ignore */
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [hostStatuses, hosts]);

  const startInstance = useCallback(async (hostId: string, instanceId: string) => {
    await solarClient.startInstance(hostId, instanceId);
  }, []);

  const stopInstance = useCallback(async (hostId: string, instanceId: string) => {
    await solarClient.stopInstance(hostId, instanceId);
  }, []);

  const restartInstance = useCallback(async (hostId: string, instanceId: string) => {
    await solarClient.restartInstance(hostId, instanceId);
  }, []);

  const isHostReachable = useCallback(
    (hostId: string): boolean => {
      const wsStatus = hostStatuses?.get(hostId);
      if (wsStatus?.connected === true) return true;
      const host = hosts.find((h) => h.id === hostId);
      if (host && (host.status as string) === 'online') return true;
      if (wsStatus && (wsStatus.status as string) === 'online') return true;
      return false;
    },
    [hostStatuses, hosts],
  );

  const reorderHost = useCallback(
    (activeId: string, overId: string) => {
      setHostOrder((prev) => {
        const currentIds = hosts.map((h) => h.id);
        const fullOrder = applySavedOrder(
          currentIds.map((id) => ({ id })),
          prev,
        ).map((x) => x.id);

        const oldIdx = fullOrder.indexOf(activeId);
        const newIdx = fullOrder.indexOf(overId);
        if (oldIdx === -1 || newIdx === -1) return prev;

        const updated = [...fullOrder];
        updated.splice(oldIdx, 1);
        updated.splice(newIdx, 0, activeId);
        writeHostOrder(updated);
        return updated;
      });
    },
    [hosts],
  );

  const reorderInstance = useCallback(
    (hostId: string, activeId: string, overId: string) => {
      setInstanceOrders((prev) => {
        const host = hosts.find((h) => h.id === hostId);
        if (!host) return prev;

        const savedOrder = prev[hostId] ?? readInstanceOrder(hostId);
        const currentIds = host.instances.map((i) => i.id);
        const fullOrder = applySavedOrder(
          currentIds.map((id) => ({ id })),
          savedOrder,
        ).map((x) => x.id);

        const oldIdx = fullOrder.indexOf(activeId);
        const newIdx = fullOrder.indexOf(overId);
        if (oldIdx === -1 || newIdx === -1) return prev;

        const updated = [...fullOrder];
        updated.splice(oldIdx, 1);
        updated.splice(newIdx, 0, activeId);
        writeInstanceOrder(hostId, updated);
        return { ...prev, [hostId]: updated };
      });
    },
    [hosts],
  );

  const mergedHosts = useMemo(() => {
    let result = hosts.map((host) => {
      const wsStatus = hostStatuses?.get(host.id);
      let mergedStatus = host.status;
      if (wsStatus) {
        const restIsOnline = (host.status as string) === 'online';
        const wsIsOffline = (wsStatus.status as string) === 'offline';
        mergedStatus = restIsOnline && wsIsOffline ? host.status : (wsStatus.status as any);
      }
      const base = wsStatus
        ? {
            ...host,
            status: mergedStatus,
            memory: wsStatus.memory || host.memory,
            gpu_type: wsStatus.gpu_type || host.gpu_type,
            roles: wsStatus.roles || host.roles,
            disk_total_gb: wsStatus.disk_total_gb ?? host.disk_total_gb,
            disk_used_gb: wsStatus.disk_used_gb ?? host.disk_used_gb,
            disk_available_gb: wsStatus.disk_available_gb ?? host.disk_available_gb,
            memory_available_gb: wsStatus.memory_available_gb ?? host.memory_available_gb,
            version: wsStatus.version || host.version,
          }
        : host;

      // Merge Socket.IO instance data with REST data
      const socketInsts = hostInstances?.get(host.id);
      const { merged: mergedInstances } = mergeInstanceData(base.instances, socketInsts);

      const savedInstanceOrder = instanceOrders[host.id] ?? readInstanceOrder(host.id);
      const sortedInstances = applySavedOrder(mergedInstances, savedInstanceOrder);

      return { ...base, instances: sortedInstances };
    });

    result = applySavedOrder(result, hostOrder);

    return result;
  }, [hosts, hostStatuses, hostInstances, hostOrder, instanceOrders]);

  return {
    hosts: mergedHosts,
    loading,
    error,
    refresh: fetchData,
    startInstance,
    stopInstance,
    restartInstance,
    reorderHost,
    reorderInstance,
    isHostReachable,
    isConnected: routingConnected,
  };
}
