import type { ApiEndpoint, ApiKey } from '@/api/types';
import type { GatewayRequestSummary } from '@/hooks/useEventStream';
import type { RegisteredHandler } from './types';

export const handleGatewayRequest: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  // Completed request summary (client-side filter by endpoint_id)
  if (event.data) {
    const summary: GatewayRequestSummary = event.data;
    const filterEp = ctx.gatewayFilterRef.current.endpoint_id;
    if (filterEp && summary.endpoint_id !== filterEp) {
      return;
    }
    ctx.setGatewayRequests((prev) => {
      const updated = [summary, ...prev].slice(0, 500);
      return updated;
    });
    h.onGatewayRequest?.(summary);
  }
};

export const handleFilterStatus: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  // Filter configuration acknowledgement
  const filter = event.filter;
  if (filter) {
    ctx.setGatewayFilter(() => filter);
    h.onFilterStatus?.(filter);
  }
};

export const handleEndpointsUpdate: RegisteredHandler = (event, ctx) => {
  // C5: endpoint records change only on edits — event-driven.
  if (Array.isArray(event.data?.endpoints)) {
    ctx.setEndpoints(() => event.data.endpoints as ApiEndpoint[]);
  }
};

export const handleApiKeysUpdate: RegisteredHandler = (event, ctx) => {
  // Key records change only on explicit key CRUD.
  if (Array.isArray(event.data?.api_keys)) {
    ctx.setApiKeys(() => event.data.api_keys as ApiKey[]);
  }
};
