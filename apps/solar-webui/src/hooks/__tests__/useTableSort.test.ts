import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SortColumn, useTableSort } from '../useTableSort';

interface Row {
  name: string;
  count: number;
  avg?: number | null;
}

const columns: SortColumn<Row>[] = [
  { key: 'name', value: (r) => r.name },
  { key: 'count', value: (r) => r.count, numeric: true },
  { key: 'avg', value: (r) => r.avg, numeric: true },
];

const rows: Row[] = [
  { name: 'qwen3.6:35b', count: 5, avg: 2 },
  { name: 'iris-bert:110m', count: 50, avg: null },
  { name: 'solver-v4:9b', count: 12, avg: 1 },
];

const names = (result: { current: { rows: Row[] } }) => result.current.rows.map((r) => r.name);

describe('useTableSort', () => {
  it('applies the default column and direction on first render', () => {
    const { result } = renderHook(() => useTableSort(rows, columns, 'name'));

    expect(result.current.sortKey).toBe('name');
    expect(result.current.direction).toBe('asc');
    expect(names(result)).toEqual(['iris-bert:110m', 'qwen3.6:35b', 'solver-v4:9b']);
  });

  it('leaves the source array untouched', () => {
    const input = [...rows];
    renderHook(() => useTableSort(input, columns, 'name'));

    expect(input).toEqual(rows);
  });

  it('flips direction when the active column is clicked again', () => {
    const { result } = renderHook(() => useTableSort(rows, columns, 'name'));

    act(() => result.current.toggle('name'));

    expect(result.current.direction).toBe('desc');
    expect(names(result)).toEqual(['solver-v4:9b', 'qwen3.6:35b', 'iris-bert:110m']);
  });

  it('starts numeric columns descending, since the big values are the point', () => {
    const { result } = renderHook(() => useTableSort(rows, columns, 'name'));

    act(() => result.current.toggle('count'));

    expect(result.current.direction).toBe('desc');
    expect(names(result)).toEqual(['iris-bert:110m', 'solver-v4:9b', 'qwen3.6:35b']);
  });

  it('starts text columns ascending when switched to', () => {
    const { result } = renderHook(() => useTableSort(rows, columns, 'count', 'desc'));

    act(() => result.current.toggle('name'));

    expect(result.current.direction).toBe('asc');
  });

  it('sorts names the way a person reads them, not by code point', () => {
    const hosts = [{ name: 'host10' }, { name: 'host2' }, { name: 'Host1' }].map((h) => ({ ...h, count: 0 }));
    const { result } = renderHook(() => useTableSort(hosts, columns, 'name'));

    expect(result.current.rows.map((r) => r.name)).toEqual(['Host1', 'host2', 'host10']);
  });

  it('keeps missing values last in both directions', () => {
    const { result } = renderHook(() => useTableSort(rows, columns, 'avg'));
    const last = () => names(result)[rows.length - 1];

    expect(last()).toBe('iris-bert:110m');

    act(() => result.current.toggle('avg'));

    expect(last()).toBe('iris-bert:110m');
  });

  it('returns rows untouched when the sort key matches no column', () => {
    const { result } = renderHook(() => useTableSort(rows, columns, 'nope'));

    expect(names(result)).toEqual(rows.map((r) => r.name));
  });

  it('re-sorts when the underlying rows change', () => {
    const { result, rerender } = renderHook(({ data }) => useTableSort(data, columns, 'name'), {
      initialProps: { data: rows },
    });

    rerender({ data: [...rows, { name: 'aardvark:1b', count: 1, avg: 1 }] });

    expect(names(result)[0]).toBe('aardvark:1b');
  });

  it('keeps the chosen sort across row updates', () => {
    const { result, rerender } = renderHook(({ data }) => useTableSort(data, columns, 'name'), {
      initialProps: { data: rows },
    });

    act(() => result.current.toggle('count'));
    rerender({ data: [...rows, { name: 'zeta:1b', count: 999, avg: 1 }] });

    expect(result.current.sortKey).toBe('count');
    expect(names(result)[0]).toBe('zeta:1b');
  });
});
