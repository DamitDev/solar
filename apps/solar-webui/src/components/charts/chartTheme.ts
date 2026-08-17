/**
 * Shared Nord styling for recharts, so charts look native next to the rest of
 * the dashboard instead of like an embedded widget.
 */

import { GatewayBucket } from '@/api/types';

export const NORD = {
  bg: '#2E3440',
  surface: '#3B4252',
  surfaceRaised: '#434C5E',
  line: '#4C566A',
  text: '#D8DEE9',
  textBright: '#ECEFF4',
  teal: '#8FBCBB',
  cyan: '#88C0D0',
  blue: '#81A1C1',
  deepBlue: '#5E81AC',
  red: '#BF616A',
  orange: '#D08770',
  yellow: '#EBCB8B',
  green: '#A3BE8C',
  purple: '#B48EAD',
} as const;

/** Status colors match the stat cards above the chart. */
export const STATUS_COLORS = {
  success: NORD.green,
  error: NORD.orange,
  missed: NORD.red,
} as const;

export const TOKEN_COLORS = {
  token_in: NORD.blue,
  // Cached input reads as a dimmer sibling of the input band it is part of.
  token_cached: NORD.deepBlue,
  token_out: NORD.cyan,
} as const;

/** Cycled for endpoint/model/host breakdowns; ordered for adjacent contrast. */
export const SERIES_PALETTE = [
  NORD.cyan,
  NORD.green,
  NORD.yellow,
  NORD.purple,
  NORD.orange,
  NORD.teal,
  NORD.blue,
  NORD.red,
];

export const seriesColor = (index: number): string => SERIES_PALETTE[index % SERIES_PALETTE.length];

export const axisProps = {
  stroke: NORD.line,
  tick: { fill: NORD.text, fontSize: 11 },
  tickLine: false,
  axisLine: { stroke: NORD.line },
} as const;

export const gridProps = {
  stroke: NORD.line,
  strokeOpacity: 0.35,
  vertical: false,
} as const;

/** Buckets of a day or more only need a date; finer ones need the clock. */
const DATE_ONLY_BUCKETS: GatewayBucket[] = ['1d', '7d'];

export function formatBucketTick(ts: string, bucket: GatewayBucket): string {
  const d = new Date(ts);
  if (DATE_ONLY_BUCKETS.includes(bucket)) {
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  if (bucket === '6h') {
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit' });
  }
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** Full timestamp for tooltips, where there is room to be unambiguous. */
export function formatBucketLabel(ts: string, bucket: GatewayBucket): string {
  const d = new Date(ts);
  if (DATE_ONLY_BUCKETS.includes(bucket)) {
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatCompactNumber(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(2)}s`;
  const mins = Math.floor(seconds / 60);
  return `${mins}m ${Math.round(seconds % 60)}s`;
}

/**
 * Axis ticks have to share one unit and one precision, which formatDuration
 * cannot do -- it would print "0ms" right below "6.00s" on the same scale.
 */
export function durationAxisFormatter(peakSeconds: number): (value: number) => string {
  if (peakSeconds >= 60) return (v) => `${(v / 60).toFixed(1)}m`;
  if (peakSeconds >= 1) {
    const digits = peakSeconds >= 10 ? 0 : 1;
    return (v) => `${v.toFixed(digits)}s`;
  }
  return (v) => `${Math.round(v * 1000)}ms`;
}

export const BUCKET_LABELS: Record<GatewayBucket, string> = {
  '1m': '1 minute',
  '5m': '5 minutes',
  '15m': '15 minutes',
  '1h': '1 hour',
  '6h': '6 hours',
  '1d': '1 day',
  '7d': '1 week',
};
