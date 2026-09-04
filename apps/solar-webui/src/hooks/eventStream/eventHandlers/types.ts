import type { MutableRefObject } from 'react';
import type { ApiEndpoint, ApiKey, GatewayRequestSummary, Intent, LogMessage, PendingHost } from '@/api/types';
import type {
  EventHandlers,
  GatewayFilter,
  HostStatusData,
  InstanceStateData,
  InstanceSummary,
  PullProgressEvent,
  RequestState,
  WSEvent,
} from '@/hooks/eventStream/useEventStream';

/** Stable React values a handler needs to apply an event to hook state. */
export interface DispatchContext {
  setHosts: (updater: (prev: Map<string, HostStatusData>) => Map<string, HostStatusData>) => void;
  setPendingHosts: (updater: (prev: Map<string, PendingHost>) => Map<string, PendingHost>) => void;
  setHostInstances: (updater: (prev: Map<string, InstanceSummary[]>) => Map<string, InstanceSummary[]>) => void;
  setRequests: (updater: (prev: Map<string, RequestState>) => Map<string, RequestState>) => void;
  setInstanceStates: (updater: (prev: Map<string, InstanceStateData>) => Map<string, InstanceStateData>) => void;
  setLogs: (updater: (prev: Map<string, LogMessage[]>) => Map<string, LogMessage[]>) => void;
  setGatewayRequests: (updater: (prev: GatewayRequestSummary[]) => GatewayRequestSummary[]) => void;
  setGatewayFilter: (updater: (prev: GatewayFilter) => GatewayFilter) => void;
  setIntents: (updater: (prev: Map<string, Intent>) => Map<string, Intent>) => void;
  setPullProgress: (updater: (prev: Map<string, PullProgressEvent>) => Map<string, PullProgressEvent>) => void;
  setEndpoints: (updater: (prev: ApiEndpoint[]) => ApiEndpoint[]) => void;
  setApiKeys: (updater: (prev: ApiKey[]) => ApiKey[]) => void;
  awaitingSnapshotRef: MutableRefObject<boolean>;
  gatewayFilterRef: MutableRefObject<GatewayFilter>;
  handlersRef: MutableRefObject<EventHandlers>;
  updateRequest: (requestId: string, updates: Partial<RequestState>) => void;
  removeRequest: (requestId: string) => void;
}

export type RegisteredHandler = (event: WSEvent, ctx: DispatchContext) => void;
