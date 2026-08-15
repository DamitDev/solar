/**
 * EventStreamContext - Global context for the unified WebSocket event stream
 *
 * This context provides access to the event stream throughout the application,
 * ensuring a single WebSocket connection is shared across all components.
 */

import { createContext, useContext, ReactNode } from 'react';
import {
  useEventStream,
  HostStatusData,
  InstanceStateData,
  InstanceSummary,
  RequestState,
  WSMessageType,
  RoutingEventData,
  LogEventData,
  EventHandlers,
  GatewayRequestSummary,
  GatewayFilter,
  PullProgressEvent,
} from '@/hooks/useEventStream';
import { LogMessage, PendingHost, Intent, ApiEndpoint, ApiKey } from '@/api/types';

interface EventStreamContextValue {
  isConnected: boolean;
  hosts: Map<string, HostStatusData>;
  pendingHosts: Map<string, PendingHost>;
  hostInstances: Map<string, InstanceSummary[]>;
  requests: Map<string, RequestState>;
  instanceStates: Map<string, InstanceStateData>;
  logs: Map<string, LogMessage[]>;
  gatewayRequests: GatewayRequestSummary[];
  gatewayFilter: GatewayFilter;
  intents: Map<string, Intent>;
  // C4: latest pull progress per "{host_id}|{source_uri}".
  pullProgress: Map<string, PullProgressEvent>;
  // C5: endpoint records, event-driven (endpoints_update).
  endpoints: ApiEndpoint[];
  // S-045: API key records, event-driven (api_keys_update).
  apiKeys: ApiKey[];
  getInstanceLogs: (hostId: string, instanceId: string) => LogMessage[];
  getInstanceState: (hostId: string, instanceId: string) => InstanceStateData | undefined;
  getPullProgress: (hostId: string | null | undefined, sourceUri: string) => PullProgressEvent | undefined;
  clearInstanceLogs: (hostId: string, instanceId: string) => void;
  removeRequest: (requestId: string) => void;
  setFilter: (filter: Partial<GatewayFilter>) => void;
  clearGatewayRequests: () => void;
}

const EventStreamContext = createContext<EventStreamContextValue | null>(null);

interface EventStreamProviderProps {
  children: ReactNode;
  handlers?: EventHandlers;
}

export function EventStreamProvider({ children, handlers }: EventStreamProviderProps) {
  const eventStream = useEventStream(handlers);

  return <EventStreamContext.Provider value={eventStream}>{children}</EventStreamContext.Provider>;
}

export function useEventStreamContext(): EventStreamContextValue {
  const context = useContext(EventStreamContext);
  if (!context) {
    throw new Error('useEventStreamContext must be used within an EventStreamProvider');
  }
  return context;
}

// Re-export types for convenience
export type {
  HostStatusData,
  InstanceStateData,
  InstanceSummary,
  RequestState,
  WSMessageType,
  RoutingEventData,
  LogEventData,
  EventHandlers,
  GatewayRequestSummary,
  GatewayFilter,
  Intent,
};
