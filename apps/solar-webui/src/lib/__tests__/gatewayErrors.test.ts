import { describe, expect, it } from 'vitest';
import { formatRelativeTime, groupEvents, hostLabel, parseGatewayEvent, statusText } from '../gatewayErrors';
import { GatewayEventDTO } from '@/api/types';

function errorEvent(error_message: string, extra: Record<string, unknown> = {}): GatewayEventDTO {
  return {
    type: 'request_error',
    data: {
      request_id: 'req-1',
      model: 'qwen3.6:35b',
      error_message,
      duration: 1.5,
      timestamp: '2026-08-13T12:00:00Z',
      ...extra,
    },
  };
}

describe('parseGatewayEvent - upstream failures', () => {
  it('unwraps the OpenAI-style error envelope the gateway forwards verbatim', () => {
    const body = JSON.stringify({
      error: { message: 'context length exceeded', type: 'invalid_request_error', code: 'context_length_exceeded' },
    });
    const parsed = parseGatewayEvent(errorEvent(`Request failed: 400 - ${body}`));

    expect(parsed.kind).toBe('upstream_error');
    expect(parsed.status).toBe(400);
    expect(parsed.title).toBe('context length exceeded');
    expect(parsed.code).toBe('context_length_exceeded');
    // The detail carries the status so the title stays the human sentence.
    expect(parsed.detail).toBe('Bad request');
  });

  it('pretty-prints the original body for the expander rather than dropping it', () => {
    const parsed = parseGatewayEvent(errorEvent('Request failed: 500 - {"error":{"message":"boom"}}'));

    expect(parsed.raw).toBe(JSON.stringify({ error: { message: 'boom' } }, null, 2));
    expect(parsed.raw).toContain('\n');
  });

  it('falls back to the status phrase when the body carries no message', () => {
    const parsed = parseGatewayEvent(errorEvent('Request failed: 503 - '));

    expect(parsed.status).toBe(503);
    expect(parsed.title).toBe('Service unavailable');
    expect(parsed.detail).toBeNull();
  });

  it('keeps a non-JSON upstream body as the message', () => {
    const parsed = parseGatewayEvent(errorEvent('Request failed: 502 - upstream closed connection'));

    expect(parsed.status).toBe(502);
    expect(parsed.title).toBe('upstream closed connection');
  });

  it('reads code from `type` when the backend omits `code`', () => {
    const body = JSON.stringify({ error: { message: 'slow down', type: 'rate_limit_error' } });
    expect(parseGatewayEvent(errorEvent(`Request failed: 429 - ${body}`)).code).toBe('rate_limit_error');
  });

  it('understands the FastAPI detail envelope', () => {
    const body = JSON.stringify({ detail: 'model is loading' });
    expect(parseGatewayEvent(errorEvent(`Request failed: 422 - ${body}`)).title).toBe('model is loading');
  });

  it('joins FastAPI validation lists into one line', () => {
    const body = JSON.stringify({ detail: [{ msg: 'field required' }, { msg: 'must be positive' }] });
    expect(parseGatewayEvent(errorEvent(`Request failed: 422 - ${body}`)).title).toBe(
      'field required; must be positive',
    );
  });
});

describe('parseGatewayEvent - gateway-side failures', () => {
  it('recognises the no-capacity message', () => {
    const parsed = parseGatewayEvent(errorEvent("Model 'solver-v4:9b' not found or no instances available"));

    expect(parsed.kind).toBe('no_capacity');
    expect(parsed.title).toBe('No instance available');
    expect(parsed.detail).toContain('solver-v4:9b');
  });

  it('extracts the attempt count and last error from a connect failure', () => {
    const parsed = parseGatewayEvent(
      errorEvent("Failed to connect to model 'qwen3.5:4b' after 3 attempts: Connection refused"),
    );

    expect(parsed.kind).toBe('connect_failed');
    expect(parsed.title).toBe('All 3 routing attempts failed');
    expect(parsed.detail).toBe('qwen3.5:4b — last error: Connection refused');
  });

  it('treats a client disconnect as a warning, not an error', () => {
    const parsed = parseGatewayEvent(errorEvent('Client disconnected'));

    expect(parsed.kind).toBe('client_disconnected');
    expect(parsed.severity).toBe('warning');
  });

  it('keeps an unrecognised message intact', () => {
    const parsed = parseGatewayEvent(errorEvent('something entirely new'));

    expect(parsed.kind).toBe('unknown');
    expect(parsed.title).toBe('something entirely new');
  });

  it('splits a multi-line message into title and detail', () => {
    const parsed = parseGatewayEvent(errorEvent('Traceback\n  File "x.py", line 3'));

    expect(parsed.title).toBe('Traceback');
    expect(parsed.detail).toContain('x.py');
    expect(parsed.raw).toContain('Traceback');
  });

  it('unwraps a bare JSON message with no status prefix', () => {
    const parsed = parseGatewayEvent(errorEvent('{"error":{"message":"no slots available","code":"busy"}}'));

    expect(parsed.title).toBe('no slots available');
    expect(parsed.code).toBe('busy');
  });

  it('does not crash on a missing error message', () => {
    const parsed = parseGatewayEvent({ type: 'request_error', data: { request_id: 'r' } });

    expect(parsed.title).toBe('Request failed');
    expect(parsed.raw).toBeNull();
  });
});

describe('parseGatewayEvent - reroutes', () => {
  it('describes a connect_error reroute in plain words', () => {
    const parsed = parseGatewayEvent({
      type: 'request_reroute',
      data: { request_id: 'r', model: 'm', reason: 'connect_error', attempt: 2, timestamp: '2026-08-13T12:00:00Z' },
    });

    expect(parsed.kind).toBe('reroute');
    expect(parsed.severity).toBe('warning');
    expect(parsed.title).toBe('Instance unreachable, rerouted');
    expect(parsed.detail).toBe('Retry attempt 2 on another instance.');
  });

  it('keeps the host id and name as separate fields', () => {
    const parsed = parseGatewayEvent({
      type: 'request_reroute',
      data: { host_id: 'host-abc', host_name: 'damcpaiops02', attempt: 1 },
    });

    expect(parsed.hostId).toBe('host-abc');
    expect(parsed.hostName).toBe('damcpaiops02');
  });
});

describe('hostLabel', () => {
  const rerouteWithoutName = parseGatewayEvent({
    type: 'request_reroute',
    data: { host_id: 'host-a', attempt: 1 },
  });

  it('resolves an id-only event through the lookup', () => {
    expect(hostLabel(rerouteWithoutName, (id) => (id === 'host-a' ? 'damcpaiops02' : undefined))).toBe('damcpaiops02');
  });

  it('falls back to the raw id when the lookup misses', () => {
    expect(hostLabel(rerouteWithoutName, () => undefined)).toBe('host-a');
    expect(hostLabel(rerouteWithoutName)).toBe('host-a');
  });

  it('prefers a name the event already carries', () => {
    const parsed = parseGatewayEvent(errorEvent('boom', { host_id: 'host-a', host_name: 'real-name' }));

    expect(hostLabel(parsed, () => 'looked-up')).toBe('real-name');
  });

  it('returns null when there is no host at all', () => {
    expect(hostLabel(parseGatewayEvent(errorEvent('boom')))).toBeNull();
  });
});

describe('groupEvents', () => {
  it('collapses repeats that differ only in ids and numbers', () => {
    const events = [
      parseGatewayEvent(errorEvent('Request failed: 503 - {"error":{"message":"no slot 7 free"}}')),
      parseGatewayEvent(errorEvent('Request failed: 503 - {"error":{"message":"no slot 12 free"}}')),
    ];

    const groups = groupEvents(events);

    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(2);
  });

  it('keeps different statuses apart', () => {
    const events = [
      parseGatewayEvent(errorEvent('Request failed: 503 - {"error":{"message":"busy"}}')),
      parseGatewayEvent(errorEvent('Request failed: 500 - {"error":{"message":"busy"}}')),
    ];

    expect(groupEvents(events)).toHaveLength(2);
  });

  it('orders groups by most recent occurrence and surfaces it as latest', () => {
    const events = [
      parseGatewayEvent(errorEvent('Client disconnected', { timestamp: '2026-08-13T10:00:00Z' })),
      parseGatewayEvent(errorEvent('Request failed: 500 - boom', { timestamp: '2026-08-13T12:00:00Z' })),
      parseGatewayEvent(errorEvent('Request failed: 500 - boom', { timestamp: '2026-08-13T11:00:00Z' })),
    ];

    const groups = groupEvents(events);

    expect(groups[0].latest.timestamp).toBe('2026-08-13T12:00:00Z');
    expect(groups[0].count).toBe(2);
    expect(groups[1].latest.kind).toBe('client_disconnected');
  });

  it('returns nothing for no events', () => {
    expect(groupEvents([])).toEqual([]);
  });
});

describe('formatRelativeTime', () => {
  const now = new Date('2026-08-13T12:00:00Z').getTime();

  it.each([
    ['2026-08-13T11:59:30Z', '30s ago'],
    ['2026-08-13T11:45:00Z', '15m ago'],
    ['2026-08-13T09:00:00Z', '3h ago'],
    ['2026-08-11T12:00:00Z', '2d ago'],
  ])('formats %s as %s', (iso, expected) => {
    expect(formatRelativeTime(iso, now)).toBe(expected);
  });

  it('handles missing and unparseable timestamps', () => {
    expect(formatRelativeTime(null, now)).toBe('—');
    expect(formatRelativeTime('not a date', now)).toBe('—');
  });

  it('never reports a negative age for slight clock skew', () => {
    expect(formatRelativeTime('2026-08-13T12:00:05Z', now)).toBe('0s ago');
  });
});

describe('statusText', () => {
  it('names the statuses an LLM backend actually returns', () => {
    expect(statusText(429)).toBe('Rate limited');
    expect(statusText(504)).toBe('Gateway timeout');
  });

  it('falls back for anything unmapped', () => {
    expect(statusText(418)).toBe('HTTP 418');
  });
});
