import { useCallback, useMemo, useState } from 'react';

export type SortDirection = 'asc' | 'desc';

export interface SortColumn<T> {
  key: string;
  /** Sort value; null and undefined always sort last, in either direction. */
  value: (row: T) => string | number | null | undefined;
  /**
   * Numeric columns start descending, because "who used the most" is the
   * question people actually have; text columns start ascending.
   */
  numeric?: boolean;
}

interface Result<T> {
  rows: T[];
  sortKey: string;
  direction: SortDirection;
  toggle: (key: string) => void;
}

/**
 * Sorts table rows client-side, keeping the comparison natural: names sort the
 * way a person reads them ("host2" before "host10") rather than by code point.
 */
export function useTableSort<T>(
  rows: T[],
  columns: SortColumn<T>[],
  defaultKey: string,
  defaultDirection: SortDirection = 'asc',
): Result<T> {
  const [sortKey, setSortKey] = useState(defaultKey);
  const [direction, setDirection] = useState<SortDirection>(defaultDirection);

  const columnsByKey = useMemo(() => new Map(columns.map((c) => [c.key, c])), [columns]);

  const toggle = useCallback(
    (key: string) => {
      if (key === sortKey) {
        setDirection((d) => (d === 'asc' ? 'desc' : 'asc'));
        return;
      }
      setSortKey(key);
      setDirection(columnsByKey.get(key)?.numeric ? 'desc' : 'asc');
    },
    [sortKey, columnsByKey],
  );

  const sorted = useMemo(() => {
    const column = columnsByKey.get(sortKey);
    if (!column) return rows;

    const factor = direction === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const left = column.value(a);
      const right = column.value(b);
      // Applied before the direction factor, so flipping the sort never fills
      // the first screen with blanks.
      const missing = compareMissing(left, right);
      return missing !== null ? missing : factor * compareValues(left, right);
    });
  }, [rows, columnsByKey, sortKey, direction]);

  return { rows: sorted, sortKey, direction, toggle };
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

type SortValue = string | number | null | undefined;

/** Returns null when both values are present and should be compared normally. */
function compareMissing(a: SortValue, b: SortValue): number | null {
  const aMissing = a == null || a === '';
  const bMissing = b == null || b === '';
  if (!aMissing && !bMissing) return null;
  if (aMissing && bMissing) return 0;
  return aMissing ? 1 : -1;
}

function compareValues(a: SortValue, b: SortValue): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return collator.compare(String(a), String(b));
}
