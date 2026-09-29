/**
 * RoutingEventsContext - Compatibility wrapper using the new EventStreamContext
 *
 * This context wraps the unified EventStreamContext to maintain backward
 * compatibility with existing components while using the new single
 * WebSocket architecture.
 */

import { createContext, useContext } from 'react';
import { HostStatusData, RequestState } from './EventStreamContext';
import { InstanceSummary, RoutingState, RoutingStateAggregates } from '@/hooks/eventStream/useEventStream';
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

export interface RoutingEventsContextValue {
  requests: Map<string, RequestState>;
  removeRequest: (requestId: string) => void;
  events: RoutingEvent[];
  addRecentEvents: (items: RoutingEvent[]) => void;
  routingConnected: boolean;
  statusConnected: boolean;
  hostStatuses: Map<string, HostStatusData>;
  pendingHosts: Map<string, PendingHost>;
  hostInstances: Map<string, InstanceSummary[]>;
  registerRoutingSnapshotHandler: (listener: (snapshot: RoutingState) => void) => () => void;
  aggregates: RoutingStateAggregates | null;
}

export const RoutingEventsContext = createContext<RoutingEventsContextValue | undefined>(undefined);

export function useRoutingEventsContext() {
  const ctx = useContext(RoutingEventsContext);
  if (!ctx) throw new Error('useRoutingEventsContext must be used within a RoutingEventsProvider');
  return ctx;
}
