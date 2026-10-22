// Mirrors the GET /api/status contract (solar-control app/models/status.py).

// 'unknown' means the backend could not read the live state or the history of
// the component: never render it as a confident 'down'.
export type Availability = 'operational' | 'degraded' | 'down' | 'unknown';

export interface UptimePoint {
  ts: string;
  // Share of sampler ticks the component was up (0..1).
  uptime: number;
  // 0 samples: no sampler ticks that day. Before the component's first
  // sample that is pre-existence, after it an outage gap; never interpolated
  // from neighbours.
  samples: number;
}

export interface ComponentStatus {
  availability: Availability;
  uptime_pct: number | null;
  history: UptimePoint[];
}

export interface VirtualModelStatus extends ComponentStatus {
  name: string;
  description?: string | null;
  targets: string[];
  target_status: Record<string, string>;
}

export interface AliasStatus extends ComponentStatus {
  alias: string;
  instances_running: number;
  instances_total: number;
  intent_phase: string | null;
}

export interface InstanceStatus extends ComponentStatus {
  instance_id: string;
  alias: string;
}

export interface StatusSummary {
  generated_at: string;
  window_days: number;
  bucket: string;
  virtual_models: VirtualModelStatus[];
  aliases: AliasStatus[];
  instances: InstanceStatus[];
  messages: StatusMessage[];
}

export function hasComponents(summary: StatusSummary): boolean {
  return (
    summary.virtual_models.length > 0 || summary.aliases.length > 0 || summary.instances.length > 0
  );
}

export function formatUptime(pct: number | null): string {
  return pct === null ? 'No data' : `${pct.toFixed(2)}% uptime`;
}

export type BarTone = 'empty' | 'gap' | 'up' | 'partial' | 'down';

/**
 * Tone for the bar at `index` in `history`.
 *
 * Days before the component's first sample predate its existence: they get the
 * neutral 'empty' tone, because the backend excludes them from the uptime
 * percentage. A day without samples after the first sample is a real outage gap
 * and stays red.
 */
export function barTone(history: UptimePoint[], index: number): BarTone {
  const point = history[index];
  if (point.samples === 0) {
    return history.slice(0, index).some((earlier) => earlier.samples > 0) ? 'gap' : 'empty';
  }
  if (point.uptime >= 0.999) return 'up';
  if (point.uptime > 0) return 'partial';
  return 'down';
}

export type MessageType = 'incident' | 'maintenance' | 'planned maintenance' | 'resolution';

export interface StatusMessage {
  id: string;
  body: string;
  type: MessageType;
  start_time: string;
  // Null means the window is open-ended.
  end_time: string | null;
  created_at?: string | null;
}

export function isUpcoming(message: StatusMessage, generatedAt: string): boolean {
  return (
    message.type === 'planned maintenance' &&
    new Date(message.start_time).getTime() > new Date(generatedAt).getTime()
  );
}

export function splitMessages(summary: StatusSummary): {
  active: StatusMessage[];
  upcoming: StatusMessage[];
} {
  const active: StatusMessage[] = [];
  const upcoming: StatusMessage[] = [];
  for (const message of summary.messages) {
    (isUpcoming(message, summary.generated_at) ? upcoming : active).push(message);
  }
  return { active, upcoming };
}

function formatInstant(iso: string): string {
  return `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

export function formatWindow(message: StatusMessage): string {
  const start = formatInstant(message.start_time);
  return message.end_time === null
    ? `From ${start}`
    : `${start} – ${formatInstant(message.end_time)}`;
}
