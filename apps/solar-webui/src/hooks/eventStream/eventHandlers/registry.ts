import type { WSMessageType } from '@/hooks/eventStream/useEventStream';
import type { RegisteredHandler } from './types';
import { handleApiKeysUpdate, handleEndpointsUpdate, handleGatewayRequest, handleFilterStatus } from './gateway';
import {
  handleHostHealth,
  handleHostPending,
  handleHostPendingRemoved,
  handleHostStatus,
  handleInitialStatus,
  handleInstancesUpdate,
} from './hosts';
import { handleInstanceState, handleLog, handleRoutingSnapshot } from './instanceState';
import { handleIntentRemoved, handleIntentUpdate } from './intents';
import { handlePullProgress } from './pulls';
import {
  handleRequestError,
  handleRequestReroute,
  handleRequestRouted,
  handleRequestStart,
  handleRequestSuccess,
} from './request';

/**
 * The complete set of built-in WS event handlers keyed by event type.
 *
 * keepalive intentionally maps to no handler (a registered no-op).
 */
export function buildRegistry(): Partial<Record<WSMessageType, RegisteredHandler>> {
  return {
    initial_status: handleInitialStatus,
    routing_snapshot: handleRoutingSnapshot,
    host_status: handleHostStatus,
    host_pending: handleHostPending,
    host_pending_removed: handleHostPendingRemoved,
    instances_update: handleInstancesUpdate,
    log: handleLog,
    instance_state: handleInstanceState,
    host_health: handleHostHealth,
    request_start: handleRequestStart,
    request_routed: handleRequestRouted,
    request_success: handleRequestSuccess,
    request_error: handleRequestError,
    request_reroute: handleRequestReroute,
    gateway_request: handleGatewayRequest,
    filter_status: handleFilterStatus,
    endpoints_update: handleEndpointsUpdate,
    api_keys_update: handleApiKeysUpdate,
    intent_update: handleIntentUpdate,
    intent_removed: handleIntentRemoved,
    pull_progress: handlePullProgress,
  };
}
