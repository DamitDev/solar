import { ChevronDown, ChevronUp, ChevronsUpDown } from 'lucide-react';
import { SortDirection } from '@/hooks/useTableSort';
import { cn } from '@/lib/utils';

interface Props {
  label: string;
  sortKey: string;
  activeKey: string;
  direction: SortDirection;
  onSort: (key: string) => void;
  align?: 'left' | 'right';
}

/** Table heading that doubles as the sort control for its column. */
export function SortHeader({ label, sortKey, activeKey, direction, onSort, align = 'left' }: Props) {
  const active = sortKey === activeKey;
  const Icon = !active ? ChevronsUpDown : direction === 'asc' ? ChevronUp : ChevronDown;

  return (
    <th
      className={cn('px-3 py-2 font-medium whitespace-nowrap', align === 'right' ? 'text-right' : 'text-left')}
      aria-sort={active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          'inline-flex items-center gap-1 transition-colors hover:text-nord-6',
          align === 'right' && 'flex-row-reverse',
          active ? 'text-nord-6' : 'text-nord-4',
        )}
      >
        {label}
        <Icon size={12} className={cn(!active && 'opacity-40')} />
      </button>
    </th>
  );
}
