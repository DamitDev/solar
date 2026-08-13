/**
 * Turns raw gateway event payloads into something readable.
 *
 * The gateway forwards upstream failures verbatim, so `error_message` is
 * usually a wrapper like `Request failed: 503 - {"error":{...}}` -- a status
 * line glued to a serialized provider error. Showing that string as-is is what
 * made the events panel unreadable, so everything is parsed down to a short
 * title, an optional detail, and the original text kept for the expander.
 */

import { GatewayEventDTO } from '@/api/types';

export type EventKind =
  'upstream_error' | 'no_capacity' | 'connect_failed' | 'client_disconnected' | 'reroute' | 'unknown';

export interface ParsedEvent {
  kind: EventKind;
  severity: 'error' | 'warning';
  /** One line, safe to truncate. */
  title: string;
  /** Supporting text; omitted when it would just repeat the title. */
  detail: string | null;
  status: number | null;
  code: string | null;
  model: string | null;
  hostId: string | null;
  /** Only some event types carry it; fall back to resolving `hostId`. */
  hostName: string | null;
  instanceId: string | null;
  requestId: string | null;
  timestamp: string | null;
  durationS: number | null;
  attempt: number | null;
  /** Pretty-printed original, shown only when the row is expanded. */
  raw: string | null;
  /** Stable across occurrences that differ only in ids/numbers. */
  signature: string;
}

const UPSTREAM_RE = /^Request failed:\s*(\d{3})\s*-\s*([\s\S]*)$/;
const NO_CAPACITY_RE = /^Model '(.+)' not found or no instances available$/;
const CONNECT_FAILED_RE = /^Failed to connect to model '(.+)' after (\d+) attempts?:\s*([\s\S]*)$/;

/** Reason phrases for the statuses an LLM backend realistically returns. */
const STATUS_TEXT: Record<number, string> = {
  400: 'Bad request',
  401: 'Unauthorized',
  403: 'Forbidden',
  404: 'Not found',
  408: 'Request timeout',
  413: 'Payload too large',
  422: 'Unprocessable request',
  429: 'Rate limited',
  500: 'Upstream server error',
  502: 'Bad gateway',
  503: 'Service unavailable',
  504: 'Gateway timeout',
};

export function statusText(status: number): string {
  return STATUS_TEXT[status] ?? `HTTP ${status}`;
}

/** Digs the human message out of the many error envelopes backends use. */
function messageFromBody(body: unknown): { message: string | null; code: string | null } {
  if (typeof body === 'string') return { message: body.trim() || null, code: null };
  if (!body || typeof body !== 'object') return { message: null, code: null };

  const obj = body as Record<string, unknown>;
  const err = obj.error;

  if (typeof err === 'string') return { message: err, code: asString(obj.code) };
  if (err && typeof err === 'object') {
    const e = err as Record<string, unknown>;
    return {
      message: asString(e.message) ?? asString(e.detail) ?? null,
      code: asString(e.code) ?? asString(e.type) ?? null,
    };
  }

  // FastAPI uses `detail`, either a string or a list of validation errors.
  const detail = obj.detail;
  if (typeof detail === 'string') return { message: detail, code: asString(obj.code) };
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => asString((d as Record<string, unknown>)?.msg)).filter(Boolean);
    if (msgs.length) return { message: msgs.join('; '), code: asString(obj.code) };
  }

  return { message: asString(obj.message), code: asString(obj.code) };
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function tryParseJson(text: string): unknown | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

/** Pretty-print JSON bodies so the expander is readable; leave text alone. */
function prettyRaw(text: string): string {
  const parsed = tryParseJson(text);
  return parsed ? JSON.stringify(parsed, null, 2) : text;
}

/**
 * Collapses the parts that vary between otherwise identical failures, so
 * repeats of the same problem group into one row instead of hundreds.
 */
function signatureOf(kind: EventKind, status: number | null, code: string | null, title: string): string {
  const normalized = title
    .toLowerCase()
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, '<id>')
    .replace(/\d+/g, '<n>')
    .slice(0, 120);
  return [kind, status ?? '', code ?? '', normalized].join('|');
}

export function parseGatewayEvent(event: GatewayEventDTO): ParsedEvent {
  const data = (event.data ?? {}) as Record<string, unknown>;
  const base = {
    model: asString(data.resolved_model) ?? asString(data.model),
    hostId: asString(data.host_id),
    hostName: asString(data.host_name),
    instanceId: asString(data.instance_id),
    requestId: asString(data.request_id),
    timestamp: asString(data.timestamp) ?? asString(event.timestamp),
    durationS: typeof data.duration === 'number' ? data.duration : null,
  };

  if (event.type === 'request_reroute') {
    const attempt = typeof data.attempt === 'number' ? data.attempt : null;
    const reason = asString(data.reason) ?? 'connect_error';
    const title = reason === 'connect_error' ? 'Instance unreachable, rerouted' : `Rerouted (${reason})`;
    return {
      ...base,
      kind: 'reroute',
      severity: 'warning',
      title,
      detail: attempt ? `Retry attempt ${attempt} on another instance.` : null,
      status: null,
      code: reason,
      attempt,
      raw: null,
      signature: signatureOf('reroute', null, reason, title),
    };
  }

  const message = asString(data.error_message) ?? '';
  const parsed = classify(message);

  return {
    ...base,
    ...parsed,
    attempt: typeof data.attempt === 'number' ? data.attempt : null,
    signature: signatureOf(parsed.kind, parsed.status, parsed.code, parsed.title),
  };
}

type Classified = Pick<ParsedEvent, 'kind' | 'severity' | 'title' | 'detail' | 'status' | 'code' | 'raw'>;

function classify(message: string): Classified {
  if (!message) {
    return {
      kind: 'unknown',
      severity: 'error',
      title: 'Request failed',
      detail: null,
      status: null,
      code: null,
      raw: null,
    };
  }

  const upstream = message.match(UPSTREAM_RE);
  if (upstream) {
    const status = Number(upstream[1]);
    const body = upstream[2];
    const json = tryParseJson(body);
    const { message: inner, code } = messageFromBody(json ?? body);
    return {
      kind: 'upstream_error',
      severity: 'error',
      title: inner ?? statusText(status),
      detail: inner ? statusText(status) : null,
      status,
      code,
      raw: prettyRaw(body),
    };
  }

  const noCapacity = message.match(NO_CAPACITY_RE);
  if (noCapacity) {
    return {
      kind: 'no_capacity',
      severity: 'error',
      title: 'No instance available',
      detail: `Nothing is currently serving ${noCapacity[1]}.`,
      status: null,
      code: null,
      raw: null,
    };
  }

  const connectFailed = message.match(CONNECT_FAILED_RE);
  if (connectFailed) {
    const [, model, attempts, lastError] = connectFailed;
    return {
      kind: 'connect_failed',
      severity: 'error',
      title: `All ${attempts} routing attempts failed`,
      detail: `${model} — last error: ${lastError.trim() || 'unknown'}`,
      status: null,
      code: null,
      raw: null,
    };
  }

  if (message === 'Client disconnected') {
    return {
      kind: 'client_disconnected',
      severity: 'warning',
      title: 'Client disconnected',
      detail: 'The caller closed the connection before the response finished.',
      status: null,
      code: null,
      raw: null,
    };
  }

  // Bare JSON happens when an exception carries a serialized body.
  const json = tryParseJson(message);
  if (json) {
    const { message: inner, code } = messageFromBody(json);
    return {
      kind: 'unknown',
      severity: 'error',
      title: inner ?? 'Request failed',
      detail: null,
      status: null,
      code,
      raw: prettyRaw(message),
    };
  }

  const [first, ...rest] = message.split('\n');
  return {
    kind: 'unknown',
    severity: 'error',
    title: first.trim(),
    detail: rest.join('\n').trim() || null,
    status: null,
    code: null,
    raw: message.includes('\n') ? message : null,
  };
}

export interface EventGroup {
  signature: string;
  /** Most recent occurrence; what the collapsed row displays. */
  latest: ParsedEvent;
  occurrences: ParsedEvent[];
  count: number;
}

/** Groups by signature, newest group first. */
export function groupEvents(events: ParsedEvent[]): EventGroup[] {
  const groups = new Map<string, ParsedEvent[]>();
  for (const event of events) {
    const bucket = groups.get(event.signature);
    if (bucket) bucket.push(event);
    else groups.set(event.signature, [event]);
  }

  return [...groups.entries()]
    .map(([signature, occurrences]) => {
      const sorted = [...occurrences].sort((a, b) => (b.timestamp ?? '').localeCompare(a.timestamp ?? ''));
      return { signature, latest: sorted[0], occurrences: sorted, count: sorted.length };
    })
    .sort((a, b) => (b.latest.timestamp ?? '').localeCompare(a.latest.timestamp ?? ''));
}

/**
 * Reroute events carry only a host id, so the page's host lookup fills the gap
 * rather than showing an opaque id next to named rows.
 */
export function hostLabel(event: ParsedEvent, resolve?: (hostId: string) => string | undefined): string | null {
  if (event.hostName) return event.hostName;
  if (!event.hostId) return null;
  return resolve?.(event.hostId) ?? event.hostId;
}

export function formatRelativeTime(iso: string | null, now: number = Date.now()): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return '—';

  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
