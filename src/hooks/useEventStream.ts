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
 * - gateway_request: Completed request summaries (filterable)
 * - filter_status: Current filter configuration acknowledgement
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import { io, Socket } from 'socket.io-client';
import solarClient from '@/api/client';
import { MemoryInfo, LogMessage, PendingHost, Intent, ActiveJobSummary } from '@/api/types';

// Event type definitions
export type WSMessageType =
  | 'initial_status'
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
  | 'keepalive';

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

export interface InstanceStateData {
  busy: boolean;
  phase?: string | null;
  prefill_progress?: number | null;
  active_slots: number;
  slot_id?: number | null;
  task_id?: number | null;
  prefill_prompt_tokens?: number | null;
  generated_tokens?: number | null;
  decode_tps?: number | null;
  decode_ms_per_token?: number | null;
  checkpoint_index?: number | null;
  checkpoint_total?: number | null;
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

// Gateway request summary (completed request)
export interface GatewayRequestSummary {
  request_id: string;
  request_type?: string; // chat, completion, embedding, classification, etc.
  status: 'success' | 'error' | 'missed';
  model?: string;
  resolved_model?: string;
  endpoint?: string;
  endpoint_id?: string;
  client_ip?: string;
  stream?: boolean;
  attempts: number;
  start_timestamp?: string;
  end_timestamp: string;
  duration_s?: number;
  host_id?: string;
  host_name?: string;
  instance_id?: string;
  instance_url?: string;
  error_message?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  decode_tps?: number;
  decode_ms_per_token?: number;
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

  const socketRef = useRef<Socket | null>(null);
  const handlersRef = useRef(handlers);
  const gatewayFilterRef = useRef(gatewayFilter);

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

  const handleEvent = useCallback(
    (event: WSEvent) => {
      const h = handlersRef.current;

      switch (event.type) {
        case 'initial_status':
          if (Array.isArray(event.data)) {
            const hostMap = new Map<string, HostStatusData>();
            event.data.forEach((host: HostStatusData) => {
              hostMap.set(host.host_id, host);
            });
            setHosts(hostMap);
            h.onInitialStatus?.(event.data);
          }
          break;

        case 'host_status':
          if (event.data) {
            setHosts((prev) => {
              const newMap = new Map(prev);
              newMap.set(event.data.host_id, event.data);
              return newMap;
            });
            h.onHostStatus?.(event.data);
          }
          break;

        case 'host_pending':
          if (event.data?.pending_id) {
            setPendingHosts((prev) => {
              const newMap = new Map(prev);
              newMap.set(event.data.pending_id, event.data as PendingHost);
              return newMap;
            });
          }
          break;

        case 'host_pending_removed':
          if (event.data?.pending_id) {
            setPendingHosts((prev) => {
              const newMap = new Map(prev);
              newMap.delete(event.data.pending_id);
              return newMap;
            });
          }
          break;

        case 'instances_update':
          if (event.data?.host_id && Array.isArray(event.data?.instances)) {
            setHostInstances((prev) => {
              const newMap = new Map(prev);
              newMap.set(event.data.host_id, event.data.instances);
              return newMap;
            });
          }
          break;

        case 'log':
          if (event.host_id && event.instance_id && event.data) {
            const key = `${event.host_id}:${event.instance_id}`;
            const logMsg: LogMessage = {
              seq: event.data.seq,
              timestamp: event.timestamp || new Date().toISOString(),
              line: event.data.line,
            };
            setLogs((prev) => {
              const newMap = new Map(prev);
              const existing = newMap.get(key) || [];
              // Keep last 1000 logs
              const updated = [...existing, logMsg].slice(-1000);
              newMap.set(key, updated);
              return newMap;
            });
            h.onLog?.(event.host_id, event.instance_id, event.data);
          }
          break;

        case 'instance_state':
          if (event.host_id && event.instance_id && event.data) {
            const key = `${event.host_id}:${event.instance_id}`;
            setInstanceStates((prev) => {
              const newMap = new Map(prev);
              newMap.set(key, event.data);
              return newMap;
            });
            h.onInstanceState?.(event.host_id, event.instance_id, event.data);
          }
          break;

        case 'host_health':
          if (event.host_id && event.data) {
            const hostId = event.host_id;
            setHosts((prev) => {
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
          break;

        case 'request_start':
          if (event.data?.request_id) {
            updateRequest(event.data.request_id, {
              model: event.data.model,
              endpoint: event.data.endpoint,
              endpoint_id: event.data.endpoint_id,
              status: 'pending',
              timestamp: event.data.timestamp,
              stream: event.data.stream,
              client_ip: event.data.client_ip,
            });
            h.onRoutingEvent?.(event.type, event.data);
          }
          break;

        case 'request_routed':
          if (event.data?.request_id) {
            updateRequest(event.data.request_id, {
              host_id: event.data.host_id,
              host_name: event.data.host_name,
              instance_id: event.data.instance_id,
              instance_url: event.data.instance_url,
              resolved_model: event.data.resolved_model,
              endpoint_id: event.data.endpoint_id,
              status: 'processing',
            });
            h.onRoutingEvent?.(event.type, event.data);
          }
          break;

        case 'request_success':
          if (event.data?.request_id) {
            updateRequest(event.data.request_id, {
              status: 'success',
              duration: event.data.duration,
            });
            h.onRoutingEvent?.(event.type, event.data);
            // Auto-remove after 5 seconds
            setTimeout(() => {
              removeRequest(event.data.request_id);
            }, 5000);
          }
          break;

        case 'request_error':
          if (event.data?.request_id) {
            updateRequest(event.data.request_id, {
              status: 'error',
              error_message: event.data.error_message,
              duration: event.data.duration,
              host_id: event.data.host_id,
              instance_id: event.data.instance_id,
            });
            h.onRoutingEvent?.(event.type, event.data);
          }
          break;

        case 'request_reroute':
          h.onRoutingEvent?.(event.type, event.data);
          break;

        case 'gateway_request':
          // Completed request summary (client-side filter by endpoint_id)
          if (event.data) {
            const summary: GatewayRequestSummary = event.data;
            const filterEp = gatewayFilterRef.current.endpoint_id;
            if (filterEp && summary.endpoint_id !== filterEp) {
              break;
            }
            setGatewayRequests((prev) => {
              const updated = [summary, ...prev].slice(0, 500);
              return updated;
            });
            h.onGatewayRequest?.(summary);
          }
          break;

        case 'filter_status':
          // Filter configuration acknowledgement
          if (event.filter) {
            setGatewayFilter(event.filter);
            h.onFilterStatus?.(event.filter);
          }
          break;

        case 'intent_update':
          // Full intent record (reconciler emits the bare record — bindEvent wraps it in data)
          if (event.data?.id) {
            setIntents((prev) => {
              const m = new Map(prev);
              m.set(event.data.id, event.data as Intent);
              return m;
            });
            h.onIntentUpdate?.(event.data as Intent);
          }
          break;

        case 'intent_removed':
          if (event.data?.id) {
            setIntents((prev) => {
              const m = new Map(prev);
              m.delete(event.data.id);
              return m;
            });
            h.onIntentRemoved?.(event.data.id, event.data.alias);
          }
          break;

        case 'keepalive':
          // Ignore keepalives
          break;
      }
    },
    [updateRequest, removeRequest],
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
    getInstanceLogs,
    getInstanceState,
    clearInstanceLogs,
    removeRequest,
    setFilter,
    clearGatewayRequests,
  };
}
