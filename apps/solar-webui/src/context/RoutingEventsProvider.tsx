import { useCallback, useMemo, useState, ReactNode } from 'react';
import { EventStreamProvider } from './EventStreamProvider';
import { useEventStreamContext } from './EventStreamContext';
import { RoutingEvent, RoutingEventsContext, RoutingEventsContextValue } from './RoutingEventsContext';

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
      registerRoutingSnapshotHandler: eventStream.registerRoutingSnapshotHandler,
      aggregates: eventStream.aggregates,
    }),
    [
      eventStream.requests,
      eventStream.removeRequest,
      eventStream.isConnected,
      eventStream.hosts,
      eventStream.pendingHosts,
      eventStream.hostInstances,
      eventStream.registerRoutingSnapshotHandler,
      eventStream.aggregates,
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
