import { useMemo } from 'react';
import { SortHeader } from '@/components/tables/SortHeader';
import { SortColumn, useTableSort } from '@/hooks/useTableSort';
import { formatTokenCount } from '@/lib/utils';

/** The shape "By Model" and "By Host" share, once the label is resolved. */
export interface BreakdownRow {
  id: string;
  label: string;
  /** Optional: the endpoint the credential belongs to (By User only). */
  endpoint_label?: string;
  completed: number;
  token_in: number;
  token_cached: number;
  token_out: number;
  avg_duration_s: number;
}

interface Props {
  title: string;
  /** Heading for the label column, e.g. "Model" or "Host". */
  labelHeading: string;
  rows: BreakdownRow[];
  /** Tighter paddings and narrower labels, for tables in dense multi-column grids. */
  compact?: boolean;
  /** Prepend an Endpoint column (By User: the key's owning endpoint). */
  showEndpoint?: boolean;
  /** Append a Tokens column: input + output combined. */
  showTokens?: boolean;
  /** Override the default label-ascending initial sort. */
  defaultSort?: { key: string; direction: 'asc' | 'desc' };
}

export function BreakdownTable({
  title,
  labelHeading,
  rows,
  compact = false,
  showEndpoint = false,
  showTokens = false,
  defaultSort,
}: Props) {
  const pad = compact ? 'px-1.5 py-2' : 'px-2 py-2';
  const labelMax = compact ? 'max-w-[90px]' : 'max-w-[180px]';
  const columns = useMemo<SortColumn<BreakdownRow>[]>(
    () => [
      ...(showEndpoint ? [{ key: 'endpoint', value: (r: BreakdownRow) => r.endpoint_label ?? '' }] : []),
      { key: 'label', value: (r) => r.label },
      { key: 'completed', value: (r) => r.completed, numeric: true },
      { key: 'token_miss', value: (r) => r.token_in - r.token_cached, numeric: true },
      { key: 'token_cached', value: (r) => r.token_cached, numeric: true },
      { key: 'token_out', value: (r) => r.token_out, numeric: true },
      ...(showTokens ? [{ key: 'tokens', value: (r: BreakdownRow) => r.token_in + r.token_out, numeric: true }] : []),
      { key: 'avg_duration_s', value: (r) => r.avg_duration_s, numeric: true },
    ],
    [showEndpoint, showTokens],
  );

  // Alphabetical by default: the list is a reference you scan for a known name,
  // not a leaderboard. Tables can override (By User opens on Tokens desc).
  const {
    rows: sorted,
    sortKey,
    direction,
    toggle,
  } = useTableSort(rows, columns, defaultSort?.key ?? 'label', defaultSort?.direction ?? 'asc');

  const totals = useMemo(
    () =>
      rows.reduce(
        (acc, r) => ({
          completed: acc.completed + r.completed,
          token_in: acc.token_in + r.token_in,
          token_cached: acc.token_cached + r.token_cached,
          token_out: acc.token_out + r.token_out,
        }),
        { completed: 0, token_in: 0, token_cached: 0, token_out: 0 },
      ),
    [rows],
  );

  return (
    <div className="bg-nord-1 border border-nord-3 rounded">
      <div className="p-4 border-b border-nord-3 flex items-baseline justify-between gap-3">
        <span className="text-nord-6 font-medium">{title}</span>
        {rows.length > 0 && (
          <span className="text-xs text-nord-4">
            {rows.length} {rows.length === 1 ? 'row' : 'rows'} • {totals.completed} completed
          </span>
        )}
      </div>
      <div className="overflow-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-nord-2 text-nord-4">
            <tr>
              {showEndpoint && (
                <SortHeader
                  label="Endpoint"
                  sortKey="endpoint"
                  activeKey={sortKey}
                  direction={direction}
                  onSort={toggle}
                  compact={compact}
                />
              )}
              <SortHeader
                label={labelHeading}
                sortKey="label"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                compact={compact}
              />
              <SortHeader
                label="Completed"
                sortKey="completed"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
                compact={compact}
              />
              <SortHeader
                label="Miss"
                sortKey="token_miss"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
                compact={compact}
              />
              <SortHeader
                label="Hit"
                sortKey="token_cached"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
                compact={compact}
              />
              <SortHeader
                label="Output"
                sortKey="token_out"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
                compact={compact}
              />
              {showTokens && (
                <SortHeader
                  label="Tokens"
                  sortKey="tokens"
                  activeKey={sortKey}
                  direction={direction}
                  onSort={toggle}
                  align="right"
                  compact={compact}
                />
              )}
              <SortHeader
                label="Latency"
                sortKey="avg_duration_s"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
                compact={compact}
              />
            </tr>
          </thead>
          <tbody className="text-nord-6">
            {sorted.length ? (
              sorted.map((row) => (
                <tr key={row.id} className="border-t border-nord-3 hover:bg-nord-2/40">
                  {showEndpoint && (
                    <td className={`${pad} max-w-[140px] truncate`} title={row.endpoint_label}>
                      {row.endpoint_label || '—'}
                    </td>
                  )}
                  <td className={`${pad} ${labelMax} truncate`} title={row.label}>
                    {row.label}
                  </td>
                  <td className={`${pad} text-right tabular-nums`}>{row.completed}</td>
                  <td className={`${pad} text-right tabular-nums`}>
                    {formatTokenCount(row.token_in - row.token_cached)}
                  </td>
                  <td className={`${pad} text-right tabular-nums`}>{formatTokenCount(row.token_cached)}</td>
                  <td className={`${pad} text-right tabular-nums`}>{formatTokenCount(row.token_out)}</td>
                  {showTokens && (
                    <td className={`${pad} text-right tabular-nums`}>
                      {formatTokenCount(row.token_in + row.token_out)}
                    </td>
                  )}
                  <td className={`${pad} text-right tabular-nums`}>{row.avg_duration_s.toFixed(2)}s</td>
                </tr>
              ))
            ) : (
              <tr>
                <td
                  colSpan={6 + (showEndpoint ? 1 : 0) + (showTokens ? 1 : 0)}
                  className="px-3 py-4 text-center text-nord-4"
                >
                  No data
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
