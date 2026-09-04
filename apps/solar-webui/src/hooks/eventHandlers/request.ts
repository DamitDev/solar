import type { RegisteredHandler } from './types';

export const handleRequestStart: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (ctx.awaitingSnapshotRef.current) return;
  if (event.data?.request_id) {
    ctx.updateRequest(event.data.request_id, {
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
};

export const handleRequestRouted: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (ctx.awaitingSnapshotRef.current) return;
  if (event.data?.request_id) {
    ctx.updateRequest(event.data.request_id, {
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
};

export const handleRequestSuccess: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (ctx.awaitingSnapshotRef.current) return;
  if (event.data?.request_id) {
    ctx.updateRequest(event.data.request_id, {
      status: 'success',
      duration: event.data.duration,
    });
    h.onRoutingEvent?.(event.type, event.data);
    // Auto-remove after 5 seconds
    setTimeout(() => {
      ctx.removeRequest(event.data.request_id);
    }, 5000);
  }
};

export const handleRequestError: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (ctx.awaitingSnapshotRef.current) return;
  if (event.data?.request_id) {
    ctx.updateRequest(event.data.request_id, {
      status: 'error',
      error_message: event.data.error_message,
      duration: event.data.duration,
      host_id: event.data.host_id,
      instance_id: event.data.instance_id,
    });
    h.onRoutingEvent?.(event.type, event.data);
  }
};

export const handleRequestReroute: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  h.onRoutingEvent?.(event.type, event.data);
};
