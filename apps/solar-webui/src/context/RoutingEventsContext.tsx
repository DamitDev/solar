/**
 * RoutingEventsContext - Compatibility wrapper using the new EventStreamContext
 *
 * This context wraps the unified EventStreamContext to maintain backward
 * compatibility with existing components while using the new single
 * WebSocket architecture.
 */

import { createContext, useCallback, useContext, useMemo, useState, ReactNode } from 'react';
import { EventStreamProvider, useEventStreamContext, HostStatusData, RequestState } from './EventStreamContext';
import { InstanceSummary } from '@/hooks/useEventStream';
import { PendingHost } from '@/api/types';

export interface RoutingEvent {
  type: 'request_start' | 'request_routed' | 'request_success' | 'request_error' | 'request_reroute' | 'keepalive';
  data?: {
    request_id: string;
    model: string;
    resolved_model?: string;
    endpoint?: string;
    host_id?: string;
    host_name?: string;
    instance_id?: string;
    instance_url?: string;
    error_message?: string;
    duration?: number;
    timestamp: string;
    stream?: boolean;
    client_ip?: string;
  };
}

// Re-export RequestState from EventStreamContext for compatibility
export type { RequestState };

interface RoutingEventsContextValue {
  requests: Map<string, RequestState>;
  removeRequest: (requestId: string) => void;
  events: RoutingEvent[];
  addRecentEvents: (items: RoutingEvent[]) => void;
  routingConnected: boolean;
  statusConnected: boolean;
  hostStatuses: Map<string, HostStatusData>;
  pendingHosts: Map<string, PendingHost>;
  hostInstances: Map<string, InstanceSummary[]>;
}

const RoutingEventsContext = createContext<RoutingEventsContextValue | undefined>(undefined);

function RoutingEventsInner({ children }: { children: ReactNode }) {
  const eventStream = useEventStreamContext();
  const [events, setEvents] = useState<RoutingEvent[]>([]);
  const EVENTS_MAX = 2000;

  const addRecentEvents = useCallback((items: RoutingEvent[]) => {
    if (!items || items.length === 0) return;
    setEvents((prev) => {
      const existingKeys = new Set(
        prev.map((e) => `${e.type}:${e.data?.request_id ?? ''}:${e.data?.timestamp ?? (e as any).timestamp ?? ''}`),
      );
      const newItems = items.filter((e) => {
        const key = `${e.type}:${e.data?.request_id ?? ''}:${(e as any).data?.timestamp ?? (e as any).timestamp ?? ''}`;
        return !existingKeys.has(key);
      });
      if (newItems.length === 0) return prev;
      const merged = [...prev, ...newItems];
      merged.sort((a, b) => {
        const at = (a.data as any)?.timestamp || (a as any).timestamp || '';
        const bt = (b.data as any)?.timestamp || (b as any).timestamp || '';
        return bt.localeCompare(at);
      });
      return merged.length > EVENTS_MAX ? merged.slice(0, EVENTS_MAX) : merged;
    });
  }, []);

  const value = useMemo<RoutingEventsContextValue>(
    () => ({
      requests: eventStream.requests,
      removeRequest: eventStream.removeRequest,
      events,
      addRecentEvents,
      routingConnected: eventStream.isConnected,
      statusConnected: eventStream.isConnected,
      hostStatuses: eventStream.hosts,
      pendingHosts: eventStream.pendingHosts,
      hostInstances: eventStream.hostInstances,
    }),
    [
      eventStream.requests,
      eventStream.removeRequest,
      eventStream.isConnected,
      eventStream.hosts,
      eventStream.pendingHosts,
      eventStream.hostInstances,
      events,
      addRecentEvents,
    ],
  );

  return <RoutingEventsContext.Provider value={value}>{children}</RoutingEventsContext.Provider>;
}

export function RoutingEventsProvider({ children }: { children: ReactNode }) {
  return (
    <EventStreamProvider>
      <RoutingEventsInner>{children}</RoutingEventsInner>
    </EventStreamProvider>
  );
}

export function useRoutingEventsContext() {
  const ctx = useContext(RoutingEventsContext);
  if (!ctx) throw new Error('useRoutingEventsContext must be used within a RoutingEventsProvider');
  return ctx;
}
