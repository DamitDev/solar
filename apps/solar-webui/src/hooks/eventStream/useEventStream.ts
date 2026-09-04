/**
 * useEventStream - Socket.IO hook for solar-webui
 *
 * This hook provides a Socket.IO connection to solar-control's /webui namespace,
 * which streams all events:
 * - host_status: Host online/offline status changes
 * - initial_status: Initial status of all hosts on connect
 * - log: Instance log messages from hosts
 * - instance_state: Instance runtime state updates from hosts
 * - request_start, request_routed, request_success, request_error, request_reroute: Routing events
 * - routing_snapshot: Authoritative fleet state on connect (requests, instance
 *   states, endpoints) that resets and seeds the routing view before deltas flow
 * - gateway_request: Completed request summaries (filterable)
 * - filter_status: Current filter configuration acknowledgement
 */

import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { io, Socket } from 'socket.io-client';
import solarClient from '@/api/client';
import { buildRegistry } from '@/hooks/eventStream/eventHandlers/registry';
import type { DispatchContext, RegisteredHandler } from '@/hooks/eventStream/eventHandlers/types';
import {
  ApiEndpoint,
  ApiKey,
  MemoryInfo,
  LogMessage,
  PendingHost,
  Intent,
  ActiveJobSummary,
  DrainState,
  InstanceStateData,
  RoutingState,
  GatewayRequestSummary,
} from '@/api/types';

// Wire shapes describing the shared server contract live once in @/api/types
// (REST client and this WS client code against the same schema, so they cannot
// drift). Re-exported here so existing consumers can keep importing from this
// module unchanged.
export type { InstanceStateData, RoutingState, GatewayRequestSummary } from '@/api/types';

// Event type definitions
export type WSMessageType =
  | 'initial_status'
  | 'routing_snapshot'
  | 'host_status'
  | 'host_pending'
  | 'host_pending_removed'
  | 'instances_update'
  | 'log'
  | 'instance_state'
  | 'host_health'
  | 'request_start'
  | 'request_routed'
  | 'request_success'
  | 'request_error'
  | 'request_reroute'
  | 'gateway_request'
  | 'filter_status'
  | 'intent_update'
  | 'intent_removed'
  | 'pull_progress'
  | 'endpoints_update'
  | 'api_keys_update'
  | 'keepalive';

// Pull-progress utilities/types live in eventHandlers/pullProgress.ts (this
// module re-exports them so consumers keep importing from here).
import type { PullProgressEvent } from '@/hooks/eventStream/eventHandlers/pullProgress';
export type { PullProgressData, PullProgressEvent } from '@/hooks/eventStream/eventHandlers/pullProgress';
export {
  PULL_PHASE_LABELS,
  PULL_PROGRESS_TERMINAL_GRACE_MS,
  PULL_PROGRESS_STALE_MS,
  isTerminalPullPhase,
  prunePullProgress,
} from '@/hooks/eventStream/eventHandlers/pullProgress';

export interface InstanceSummary {
  id: string;
  alias?: string;
  status: string;
  port?: number;
  backend_type?: string;
  supported_endpoints?: string[];
}

export interface HostStatusData {
  host_id: string;
  name?: string;
  status: 'online' | 'offline' | 'error';
  /** Drain lifecycle (S-043) — orthogonal to status, which is reachability. */
  drain_state?: DrainState | null;
  url?: string;
  memory?: MemoryInfo;
  gpu_type?: string;
  roles?: string[];
  disk_total_gb?: number;
  disk_used_gb?: number;
  disk_available_gb?: number;
  memory_available_gb?: number;
  version?: string;
  connected?: boolean;
  last_seen?: string;
  timestamp?: string;
  active_jobs?: ActiveJobSummary[];
}

export interface LogEventData {
  seq: number;
  line: string;
  level?: string;
}

export interface RoutingEventData {
  request_id: string;
  model?: string;
  resolved_model?: string;
  endpoint?: string;
  endpoint_id?: string;
  host_id?: string;
  host_name?: string;
  instance_id?: string;
  instance_url?: string;
  error_message?: string;
  duration?: number;
  timestamp: string;
  stream?: boolean;
  client_ip?: string;
  attempt?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  decode_tps?: number;
}

// Gateway filter configuration
export interface GatewayFilter {
  status: string; // all, success, error, missed
  request_type: string; // all, chat, completion, embedding, classification
  model?: string | null;
  host_id?: string | null;
  endpoint_id?: string | null;
}

export interface WSEvent {
  type: WSMessageType;
  host_id?: string;
  host_name?: string;
  instance_id?: string;
  timestamp?: string;
  data?: any;
  filter?: GatewayFilter;
}

export interface RequestState {
  request_id: string;
  model?: string;
  resolved_model?: string;
  endpoint?: string;
  endpoint_id?: string;
  host_id?: string;
  host_name?: string;
  instance_id?: string;
  instance_url?: string;
  status: 'pending' | 'routed' | 'processing' | 'success' | 'error';
  error_message?: string;
  duration?: number;
  timestamp: string;
  stream?: boolean;
  client_ip?: string;
  removing?: boolean;
}

// Event handlers interface
export interface EventHandlers {
  onHostStatus?: (data: HostStatusData) => void;
  onInitialStatus?: (hosts: HostStatusData[]) => void;
  onLog?: (hostId: string, instanceId: string, data: LogEventData) => void;
  onInstanceState?: (hostId: string, instanceId: string, data: InstanceStateData) => void;
  onRoutingEvent?: (type: WSMessageType, data: RoutingEventData) => void;
  onRoutingSnapshot?: (snapshot: RoutingState) => void;
  onGatewayRequest?: (data: GatewayRequestSummary) => void;
  onFilterStatus?: (filter: GatewayFilter) => void;
  onIntentUpdate?: (intent: Intent) => void;
  onIntentRemoved?: (id: string, alias: string) => void;
}

const DEFAULT_FILTER: GatewayFilter = {
  status: 'all',
  request_type: 'all',
  model: null,
  host_id: null,
  endpoint_id: null,
};

export function useEventStream(handlers: EventHandlers = {}) {
  const [isConnected, setIsConnected] = useState(false);
  const [hosts, setHosts] = useState<Map<string, HostStatusData>>(new Map());
  const [pendingHosts, setPendingHosts] = useState<Map<string, PendingHost>>(new Map());
  const [hostInstances, setHostInstances] = useState<Map<string, InstanceSummary[]>>(new Map());
  const [requests, setRequests] = useState<Map<string, RequestState>>(new Map());
  const [instanceStates, setInstanceStates] = useState<Map<string, InstanceStateData>>(new Map());
  const [logs, setLogs] = useState<Map<string, LogMessage[]>>(new Map());
  const [gatewayRequests, setGatewayRequests] = useState<GatewayRequestSummary[]>([]);
  const [gatewayFilter, setGatewayFilter] = useState<GatewayFilter>(DEFAULT_FILTER);
  const [intents, setIntents] = useState<Map<string, Intent>>(new Map());
  // C4: latest pull progress per "{host_id}|{source_uri}".
  const [pullProgress, setPullProgress] = useState<Map<string, PullProgressEvent>>(new Map());
  // C5: multi-tenant API endpoint records, event-driven (endpoints_update).
  const [endpoints, setEndpoints] = useState<ApiEndpoint[]>([]);
  // API key records, event-driven (api_keys_update; cascades on
  // endpoint delete arrive as a list refresh from the same endpoint).
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);

  const socketRef = useRef<Socket | null>(null);
  const handlersRef = useRef(handlers);
  const gatewayFilterRef = useRef(gatewayFilter);
  // While true, request_*/instance_state deltas are gated until the
  // authoritative routing_snapshot arrives (a delta that races the connect can
  // otherwise land on a stale base and be lost or double-applied).
  const awaitingSnapshotRef = useRef(false);

  // Keep refs updated
  useEffect(() => {
    handlersRef.current = handlers;
  }, [handlers]);
  useEffect(() => {
    gatewayFilterRef.current = gatewayFilter;
  }, [gatewayFilter]);

  const updateRequest = useCallback((requestId: string, updates: Partial<RequestState>) => {
    setRequests((prev) => {
      const newMap = new Map(prev);
      const existing = newMap.get(requestId);
      if (existing) {
        newMap.set(requestId, { ...existing, ...updates });
      } else {
        newMap.set(requestId, {
          request_id: requestId,
          status: 'pending',
          timestamp: new Date().toISOString(),
          ...updates,
        } as RequestState);
      }
      return newMap;
    });
  }, []);

  const removeRequest = useCallback((requestId: string) => {
    setRequests((prev) => {
      const newMap = new Map(prev);
      const existing = newMap.get(requestId);
      if (existing) {
        newMap.set(requestId, { ...existing, removing: true });
      }
      return newMap;
    });

    setTimeout(() => {
      setRequests((prev) => {
        const newMap = new Map(prev);
        newMap.delete(requestId);
        return newMap;
      });
    }, 350);
  }, []);

  // ---- event handler registry -------------------------------------------------
  // Each WS event type is a single registered handler instead of a growing
  // switch branch. Built-ins live in this module's sibling eventHandlers/ dir
  // and are seeded once into the registry; consumers can extend or override it
  // via the public registerHandler without editing the dispatch.
  const registryRef = useRef<Partial<Record<WSMessageType, RegisteredHandler>>>({});

  const registerHandler = useCallback((type: WSMessageType, handler: (event: WSEvent) => void) => {
    registryRef.current[type] = (event) => handler(event);
  }, []);

  // A context bundling every stable React value the handlers touch (state
  // setters, refs, and the stable updateRequest/removeRequest callbacks), all
  // of which keep the same identity across renders. Building it once means the
  // built-in registry never needs rebuilding.
  const ctx: DispatchContext = useMemo(
    () => ({
      setHosts,
      setPendingHosts,
      setHostInstances,
      setRequests,
      setInstanceStates,
      setLogs,
      setGatewayRequests,
      setGatewayFilter,
      setIntents,
      setPullProgress,
      setEndpoints,
      setApiKeys,
      awaitingSnapshotRef,
      gatewayFilterRef,
      handlersRef,
      updateRequest,
      removeRequest,
    }),
    [updateRequest, removeRequest],
  );

  // Seed the built-in handlers once on mount. Mutation (Object.assign) keeps
  // the same ref object alive so later registerHandler overrides persist.
  useEffect(() => {
    Object.assign(registryRef.current, buildRegistry());
  }, []);

  const handleEvent = useCallback(
    (event: WSEvent) => {
      // Dispatch through the registry; unregistered types fall back to a no-op.
      registryRef.current[event.type]?.(event, ctx);
    },
    [ctx],
  );
  const setFilter = useCallback((filter: Partial<GatewayFilter>) => {
    setGatewayFilter((prevFilter) => {
      return { ...prevFilter, ...filter };
    });

    const merged = { ...gatewayFilterRef.current, ...filter };
    const socket = socketRef.current;
    if (socket?.connected) {
      socket.emit('set_filter', merged);
    }
  }, []);

  // Clear gateway requests (when filter changes)
  const clearGatewayRequests = useCallback(() => {
    setGatewayRequests([]);
  }, []);

  useEffect(() => {
    let socket: Socket | null = null;

    const connect = () => {
      const baseUrl = solarClient.getControlSocketIOUrl();
      const path = solarClient.getSocketIOPath();
      const apiKey = solarClient.getManagementApiKey();

      if (!baseUrl) {
        console.warn('EventStream: No base URL for Socket.IO');
        return;
      }

      const urlWithNamespace = baseUrl.replace(/\/$/, '') + '/webui';
      console.log('EventStream: Connecting to', urlWithNamespace, 'path:', path, 'hasAuth:', !!apiKey);

      const opts: any = {
        path,
        transports: ['websocket'],
        autoConnect: true,
      };
      if (apiKey) {
        opts.auth = { api_key: apiKey };
      }

      socket = io(urlWithNamespace, opts);

      socketRef.current = socket;
      const webuiSocket = socket;

      webuiSocket.on('connect', () => {
        console.log('EventStream: Connected');
        setIsConnected(true);
        // Reset the routing view before the authoritative snapshot lands so any
        // delta that races the connect has no stale base to corrupt.
        awaitingSnapshotRef.current = true;
        setRequests(new Map());
        setInstanceStates(new Map());
        setEndpoints([]);
      });

      webuiSocket.on('disconnect', (reason) => {
        console.log('EventStream: Disconnected', reason);
        setIsConnected(false);
      });

      webuiSocket.on('connect_error', (err) => {
        console.error('EventStream: Connection error', err.message);
        setIsConnected(false);
      });

      // Map Socket.IO events (emitted by event name) to WSEvent format for handleEvent
      const bindEvent = (eventName: string, toWSEvent: (payload: any) => WSEvent) => {
        webuiSocket.on(eventName, (payload: any) => {
          handleEvent(toWSEvent(payload));
        });
      };

      bindEvent('initial_status', (payload) => ({ type: 'initial_status', data: payload }));
      bindEvent('routing_snapshot', (payload) => ({ type: 'routing_snapshot', data: payload }));
      bindEvent('host_status', (payload) => ({ type: 'host_status', data: payload }));
      bindEvent('host_pending', (payload) => ({ type: 'host_pending', data: payload }));
      bindEvent('host_pending_removed', (payload) => ({ type: 'host_pending_removed', data: payload }));
      bindEvent('instances_update', (payload) => ({ type: 'instances_update', data: payload }));
      bindEvent('host_health', (payload) => {
        const raw = payload?.data ?? payload;
        return {
          type: 'host_health',
          host_id: payload?.host_id,
          data: {
            ...raw,
            ...(payload?.memory && { memory: payload.memory }),
            ...(payload?.disk_total_gb != null && { disk_total_gb: payload.disk_total_gb }),
            ...(payload?.disk_used_gb != null && { disk_used_gb: payload.disk_used_gb }),
            ...(payload?.disk_available_gb != null && { disk_available_gb: payload.disk_available_gb }),
            ...(payload?.memory_available_gb != null && { memory_available_gb: payload.memory_available_gb }),
          },
        };
      });
      bindEvent('instance_state', (payload) => ({
        type: 'instance_state',
        host_id: payload?.host_id,
        instance_id: payload?.instance_id,
        timestamp: payload?.timestamp,
        data: payload?.data ?? payload,
      }));
      bindEvent('log', (payload) => ({
        type: 'log',
        host_id: payload?.host_id,
        instance_id: payload?.instance_id,
        timestamp: payload?.timestamp,
        data: payload?.data ?? payload,
      }));
      bindEvent('request_start', (payload) => ({ type: 'request_start', data: payload }));
      bindEvent('request_routed', (payload) => ({ type: 'request_routed', data: payload }));
      bindEvent('request_success', (payload) => ({ type: 'request_success', data: payload }));
      bindEvent('request_error', (payload) => ({ type: 'request_error', data: payload }));
      bindEvent('request_reroute', (payload) => ({ type: 'request_reroute', data: payload }));
      bindEvent('gateway_request', (payload) => ({ type: 'gateway_request', data: payload }));
      bindEvent('filter_status', (payload) => ({
        type: 'filter_status',
        filter: payload?.filter ?? payload,
      }));
      bindEvent('intent_update', (payload) => ({ type: 'intent_update', data: payload }));
      bindEvent('intent_removed', (payload) => ({ type: 'intent_removed', data: payload }));
      bindEvent('pull_progress', (payload) => ({
        type: 'pull_progress',
        host_id: payload?.host_id,
        host_name: payload?.host_name,
        timestamp: payload?.timestamp,
        data: payload?.data ?? payload,
      }));
      bindEvent('endpoints_update', (payload) => ({ type: 'endpoints_update', data: payload }));
      bindEvent('api_keys_update', (payload) => ({ type: 'api_keys_update', data: payload }));
    };

    connect();

    return () => {
      if (socket) {
        socket.disconnect();
        socket.removeAllListeners();
      }
      socketRef.current = null;
    };
  }, [handleEvent]);

  // Helper to get logs for a specific instance
  const getInstanceLogs = useCallback(
    (hostId: string, instanceId: string): LogMessage[] => {
      return logs.get(`${hostId}:${instanceId}`) || [];
    },
    [logs],
  );

  // Helper to get state for a specific instance
  const getInstanceState = useCallback(
    (hostId: string, instanceId: string): InstanceStateData | undefined => {
      return instanceStates.get(`${hostId}:${instanceId}`);
    },
    [instanceStates],
  );

  // Helper to clear logs for an instance
  const clearInstanceLogs = useCallback((hostId: string, instanceId: string) => {
    setLogs((prev) => {
      const newMap = new Map(prev);
      newMap.delete(`${hostId}:${instanceId}`);
      return newMap;
    });
  }, []);

  /**
   * C4: pull progress for one host's copy of a model.
   *
   * Keyed by host, not just source: two hosts pulling the same model produce
   * two independent progressions, and matching on the source alone showed
   * whichever arrived last — potentially a different host's download.
   *
   * `hostId` may be null when the caller does not know which host is pulling
   * yet (a shortfall CREATE has not landed a replica), in which case the
   * newest entry for the source is the best available answer.
   */
  const getPullProgress = useCallback(
    (hostId: string | null | undefined, sourceUri: string): PullProgressEvent | undefined => {
      if (!sourceUri) return undefined;
      if (hostId) return pullProgress.get(`${hostId}|${sourceUri}`);
      let latest: PullProgressEvent | undefined;
      for (const [key, entry] of pullProgress) {
        if (key.endsWith(`|${sourceUri}`)) {
          if (!latest || (entry.timestamp ?? '') > (latest.timestamp ?? '')) {
            latest = entry;
          }
        }
      }
      return latest;
    },
    [pullProgress],
  );

  return {
    isConnected,
    hosts,
    pendingHosts,
    hostInstances,
    requests,
    instanceStates,
    logs,
    gatewayRequests,
    gatewayFilter,
    intents,
    pullProgress,
    endpoints,
    apiKeys,
    getInstanceLogs,
    getInstanceState,
    getPullProgress,
    clearInstanceLogs,
    removeRequest,
    setFilter,
    clearGatewayRequests,
    registerHandler,
  };
}
