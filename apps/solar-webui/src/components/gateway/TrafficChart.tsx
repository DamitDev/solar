import { useMemo, useState } from 'react';
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { AlertCircle, Activity } from 'lucide-react';
import { GatewayBucket, GatewayGroupBy, GatewayTimeseries } from '@/api/types';
import { useGatewayTimeseries } from '@/hooks/useGatewayTimeseries';
import { cn } from '@/lib/utils';
import {
  BUCKET_LABELS,
  NORD,
  STATUS_COLORS,
  TOKEN_COLORS,
  axisProps,
  durationAxisFormatter,
  formatBucketLabel,
  formatBucketTick,
  formatCompactNumber,
  formatDuration,
  gridProps,
  seriesColor,
} from '@/components/charts/chartTheme';

type Metric = 'requests' | 'tokens' | 'latency';
type Split = 'status' | 'endpoint' | 'model' | 'host';

const METRICS: Array<{ id: Metric; label: string }> = [
  { id: 'requests', label: 'Requests' },
  { id: 'tokens', label: 'Tokens' },
  { id: 'latency', label: 'Latency' },
];

const SPLITS: Array<{ id: Split; label: string }> = [
  { id: 'status', label: 'Status' },
  { id: 'endpoint', label: 'Endpoint' },
  { id: 'model', label: 'Model' },
  { id: 'host', label: 'Host' },
];

interface Band {
  key: string;
  label: string;
  color: string;
  /** Stacked areas for parts of a whole; lines for independent measures. */
  shape: 'area' | 'line';
  stacked: boolean;
}

const Y_AXIS_WIDTH = 52;

/** A chart row: the bucket timestamp plus one numeric field per band. */
type Row = { ts: string } & Record<string, number | string | null>;

interface Props {
  from: string;
  to: string;
  endpointId: string | null;
  requestType: string | null;
  /** Resolves breakdown keys to display names (endpoint ids, host ids). */
  labelFor: (split: Split, key: string) => string;
}

export function TrafficChart({ from, to, endpointId, requestType, labelFor }: Props) {
  const [metric, setMetric] = useState<Metric>('requests');
  const [split, setSplit] = useState<Split>('status');

  // Only the requests metric is broken down; tokens and latency have their own
  // fixed shape, so asking the API to group them would just waste work.
  const groupBy: GatewayGroupBy = metric === 'requests' && split !== 'status' ? split : 'none';

  const { data, loading, error } = useGatewayTimeseries({ from, to, groupBy, endpointId, requestType });

  const { rows, bands, summary, axisTick } = useMemo(
    () => buildChartData(data, metric, split, labelFor),
    [data, metric, split, labelFor],
  );

  const bucket = (data?.bucket ?? '1h') as GatewayBucket;
  const hasData = rows.some((r) => bands.some((b) => Number(r[b.key]) > 0));

  return (
    <div className="bg-nord-1 border border-nord-3 rounded">
      <div className="p-4 flex flex-wrap items-center justify-between gap-3 border-b border-nord-3">
        <div className="flex items-baseline gap-3">
          <span className="text-nord-6 font-medium">Traffic</span>
          <span className="text-xs text-nord-4">
            {BUCKET_LABELS[bucket]} buckets
            {summary && ` • ${summary}`}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {metric === 'requests' && (
            <div className="flex items-center gap-1 mr-1">
              <span className="text-xs text-nord-4 mr-1">Split by</span>
              {SPLITS.map((s) => (
                <button
                  key={s.id}
                  onClick={() => setSplit(s.id)}
                  className={cn(
                    'px-2 py-1 text-xs rounded transition-colors',
                    split === s.id ? 'bg-nord-3 text-nord-6' : 'text-nord-4 hover:text-nord-6 hover:bg-nord-2',
                  )}
                >
                  {s.label}
                </button>
              ))}
            </div>
          )}
          <div className="flex items-center gap-1">
            {METRICS.map((m) => (
              <button
                key={m.id}
                onClick={() => setMetric(m.id)}
                className={cn(
                  'px-3 py-1 text-sm rounded transition-colors',
                  metric === m.id ? 'bg-nord-10 text-nord-6 font-medium' : 'bg-nord-2 text-nord-6 hover:bg-nord-3',
                )}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="relative p-4">
        {loading && (
          <div className="absolute top-2 right-4 text-xs text-nord-4 flex items-center gap-1.5">
            <Activity size={12} className="animate-pulse" /> updating
          </div>
        )}

        {error ? (
          <div className="h-[240px] flex items-center justify-center gap-2 text-nord-11 text-sm">
            <AlertCircle size={16} /> {error}
          </div>
        ) : !hasData ? (
          <div className="h-[240px] flex items-center justify-center text-nord-4 text-sm">No traffic in this range</div>
        ) : (
          <>
            <div style={{ height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid {...gridProps} />
                  <XAxis
                    dataKey="ts"
                    tickFormatter={(ts) => formatBucketTick(ts, bucket)}
                    minTickGap={32}
                    {...axisProps}
                  />
                  <YAxis tickFormatter={axisTick} width={Y_AXIS_WIDTH} {...axisProps} />
                  <Tooltip content={<ChartTooltip bucket={bucket} bands={bands} />} cursor={{ stroke: NORD.line }} />
                  {bands.map((band) =>
                    band.shape === 'area' ? (
                      <Area
                        key={band.key}
                        type="monotone"
                        dataKey={band.key}
                        stackId={band.stacked ? 'stack' : undefined}
                        stroke={band.color}
                        fill={band.color}
                        fillOpacity={0.28}
                        strokeWidth={1.5}
                        isAnimationActive={false}
                      />
                    ) : (
                      <Line
                        key={band.key}
                        type="monotone"
                        dataKey={band.key}
                        stroke={band.color}
                        strokeWidth={2}
                        dot={false}
                        // A bucket with no successful request has no latency to
                        // report, so bridge it rather than dropping to zero.
                        connectNulls
                        isAnimationActive={false}
                      />
                    ),
                  )}
                </ComposedChart>
              </ResponsiveContainer>
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-3 text-xs text-nord-4">
              {bands.map((band) => (
                <span key={band.key} className="flex items-center gap-1.5">
                  <span
                    className={cn('inline-block', band.shape === 'area' ? 'w-3 h-2 rounded-sm' : 'w-3 h-0.5')}
                    style={{ backgroundColor: band.color }}
                  />
                  {band.label}
                </span>
              ))}
              {data?.series_truncated && <span className="italic">longer tail not shown</span>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function ChartTooltip({
  active,
  payload,
  label,
  bucket,
  bands,
}: {
  active?: boolean;
  payload?: Array<{ dataKey?: string | number; value?: number | string }>;
  label?: string;
  bucket: GatewayBucket;
  bands: Band[];
}) {
  if (!active || !payload?.length) return null;

  const raw = (key: string) => payload.find((p) => p.dataKey === key)?.value;
  const stacked = bands.filter((b) => b.stacked);
  const total = stacked.reduce((sum, b) => sum + Number(raw(b.key) ?? 0), 0);

  return (
    <div className="bg-nord-0 border border-nord-3 rounded px-3 py-2 text-xs shadow-lg">
      <div className="text-nord-4 mb-1.5">{label ? formatBucketLabel(label, bucket) : ''}</div>
      {bands.map((band) => {
        const value = raw(band.key);
        return (
          <div key={band.key} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 text-nord-4">
              <span
                className={cn('inline-block', band.shape === 'area' ? 'w-2 h-2 rounded-sm' : 'w-2 h-0.5')}
                style={{ backgroundColor: band.color }}
              />
              {band.label}
            </span>
            <span className="text-nord-6 tabular-nums">
              {band.key === 'avg_duration_s'
                ? formatDuration(value == null ? null : Number(value))
                : formatCompactNumber(Number(value ?? 0))}
            </span>
          </div>
        );
      })}
      {stacked.length > 1 && (
        <div className="flex items-center justify-between gap-4 mt-1 pt-1 border-t border-nord-3">
          <span className="text-nord-4">Total</span>
          <span className="text-nord-6 tabular-nums">{formatCompactNumber(total)}</span>
        </div>
      )}
    </div>
  );
}

function buildChartData(
  data: GatewayTimeseries | null,
  metric: Metric,
  split: Split,
  labelFor: (split: Split, key: string) => string,
): { rows: Row[]; bands: Band[]; summary: string | null; axisTick: (value: number) => string } {
  if (!data) return { rows: [], bands: [], summary: null, axisTick: formatCompactNumber };

  if (metric === 'latency') {
    const measured = data.points.filter((p) => p.avg_duration_s != null);
    const peak = measured.reduce((max, p) => Math.max(max, p.avg_duration_s ?? 0), 0);
    return {
      rows: data.points.map((p) => ({ ts: p.ts, avg_duration_s: p.avg_duration_s })),
      bands: [{ key: 'avg_duration_s', label: 'Avg duration', color: NORD.yellow, shape: 'line', stacked: false }],
      summary: measured.length ? `peak ${formatDuration(peak)}` : null,
      axisTick: durationAxisFormatter(peak),
    };
  }

  if (metric === 'tokens') {
    const tin = data.points.reduce((s, p) => s + p.token_in, 0);
    const tout = data.points.reduce((s, p) => s + p.token_out, 0);
    // The input band splits into its cached and truly-evaluated portions;
    // cached + uncached == token_in, so the total stays comparable.
    return {
      rows: data.points.map((p) => ({
        ts: p.ts,
        token_cached: p.token_cached,
        token_uncached: p.token_in - p.token_cached,
        token_out: p.token_out,
      })),
      bands: [
        {
          key: 'token_cached',
          label: 'Hit',
          color: TOKEN_COLORS.token_cached,
          shape: 'area',
          stacked: true,
        },
        {
          key: 'token_uncached',
          label: 'Miss',
          color: TOKEN_COLORS.token_in,
          shape: 'area',
          stacked: true,
        },
        { key: 'token_out', label: 'Output', color: TOKEN_COLORS.token_out, shape: 'area', stacked: true },
      ],
      summary: `${formatCompactNumber(tin + tout)} tokens`,
      axisTick: formatCompactNumber,
    };
  }

  const requestTotal = data.points.reduce((s, p) => s + p.success + p.error + p.missed, 0);
  const summary = `${formatCompactNumber(requestTotal)} requests`;

  if (split === 'status' || data.series.length === 0) {
    const failures = data.points.reduce((s, p) => s + p.error + p.missed, 0);
    return {
      rows: data.points.map((p) => ({ ts: p.ts, success: p.success, error: p.error, missed: p.missed })),
      // Unstacked: each status is drawn from zero. Stacking failures onto
      // successes both hides them and misreports the success line.
      bands: [
        { key: 'success', label: 'Success', color: STATUS_COLORS.success, shape: 'area', stacked: false },
        { key: 'error', label: 'Error', color: STATUS_COLORS.error, shape: 'line', stacked: false },
        { key: 'missed', label: 'Missed', color: STATUS_COLORS.missed, shape: 'line', stacked: false },
      ],
      summary: failures > 0 ? `${summary} • ${formatCompactNumber(failures)} failed` : summary,
      axisTick: formatCompactNumber,
    };
  }

  // Every series shares the combined bucket grid, so index alignment is safe.
  const bands: Band[] = data.series.map((s, i) => ({
    key: `s${i}`,
    label: labelFor(split, s.key),
    color: seriesColor(i),
    shape: 'area',
    stacked: true,
  }));

  const rows: Row[] = data.points.map((p, idx) => {
    const row: Row = { ts: p.ts };
    data.series.forEach((s, i) => {
      const point = s.points[idx];
      row[`s${i}`] = point ? point.success + point.error + point.missed : 0;
    });
    return row;
  });

  return { rows, bands, summary, axisTick: formatCompactNumber };
}
