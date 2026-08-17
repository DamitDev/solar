import { useMemo } from 'react';
import { SortHeader } from '@/components/tables/SortHeader';
import { SortColumn, useTableSort } from '@/hooks/useTableSort';
import { formatTokenCount } from '@/lib/utils';

/** The shape "By Model" and "By Host" share, once the label is resolved. */
export interface BreakdownRow {
  id: string;
  label: string;
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
}

export function BreakdownTable({ title, labelHeading, rows }: Props) {
  const columns = useMemo<SortColumn<BreakdownRow>[]>(
    () => [
      { key: 'label', value: (r) => r.label },
      { key: 'completed', value: (r) => r.completed, numeric: true },
      { key: 'token_in', value: (r) => r.token_in, numeric: true },
      { key: 'token_cached', value: (r) => r.token_cached, numeric: true },
      { key: 'token_out', value: (r) => r.token_out, numeric: true },
      { key: 'avg_duration_s', value: (r) => r.avg_duration_s, numeric: true },
    ],
    [],
  );

  // Alphabetical by default: the list is a reference you scan for a known name,
  // not a leaderboard.
  const { rows: sorted, sortKey, direction, toggle } = useTableSort(rows, columns, 'label');

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
              <SortHeader
                label={labelHeading}
                sortKey="label"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
              />
              <SortHeader
                label="Completed"
                sortKey="completed"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
              />
              <SortHeader
                label="Input tokens"
                sortKey="token_in"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
              />
              <SortHeader
                label="Cached"
                sortKey="token_cached"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
              />
              <SortHeader
                label="Output tokens"
                sortKey="token_out"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
              />
              <SortHeader
                label="Avg Duration"
                sortKey="avg_duration_s"
                activeKey={sortKey}
                direction={direction}
                onSort={toggle}
                align="right"
              />
            </tr>
          </thead>
          <tbody className="text-nord-6">
            {sorted.length ? (
              sorted.map((row) => (
                <tr key={row.id} className="border-t border-nord-3 hover:bg-nord-2/40">
                  <td className="px-3 py-2">{row.label}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{row.completed}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{formatTokenCount(row.token_in)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{formatTokenCount(row.token_cached)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{formatTokenCount(row.token_out)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{row.avg_duration_s.toFixed(2)}s</td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={6} className="px-3 py-4 text-center text-nord-4">
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
