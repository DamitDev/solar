import { AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import { HostResourceSnapshot } from '@/api/types';
import { cn, getGpuTypeLabel } from '@/lib/utils';

interface Props {
  snapshot: HostResourceSnapshot;
  expanded: boolean;
  onToggle: () => void;
}

type Dim = 'vram' | 'ram' | 'disk';

const DIMS: Array<{ key: Dim; label: string }> = [
  { key: 'vram', label: 'VRAM' },
  { key: 'ram', label: 'RAM' },
  { key: 'disk', label: 'Disk' },
];

const STATUS_DOT: Record<string, string> = {
  online: 'bg-nord-14',
  offline: 'bg-nord-3',
  error: 'bg-nord-11',
};

/**
 * One host per line, aligned in a grid so 50 of them can be scanned by eye --
 * the card grid answers "how is this host doing", this answers "which host
 * should I look at".
 */
export function HostResourceRow({ snapshot, expanded, onToggle }: Props) {
  const unreachable = !snapshot.reachable;
  const draining = snapshot.drain_state === 'draining';

  return (
    <button
      type="button"
      onClick={onToggle}
      data-testid="host-row"
      aria-expanded={expanded}
      className={cn(
        'w-full text-left px-3 py-2 grid items-center gap-3 border-b border-nord-3 last:border-b-0 transition-colors',
        // Fixed tracks, not auto/fr: each row is its own grid, so content-sized
        // columns would land at a different x on every line and defeat the
        // point of a scannable list.
        'grid-cols-[16px_minmax(0,1fr)_11rem_9.5rem_9.5rem_9.5rem_5rem]',
        expanded ? 'bg-nord-2/50' : 'hover:bg-nord-2/40',
        unreachable && 'opacity-60',
      )}
      title={snapshot.url}
    >
      <span className="text-nord-4">{expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>

      <span className="flex items-center gap-2 min-w-0">
        <span
          className={cn('w-2 h-2 rounded-full shrink-0', STATUS_DOT[snapshot.status] ?? 'bg-nord-3')}
          title={snapshot.status}
        />
        <span data-testid="host-row-name" className="text-nord-6 text-sm truncate">
          {snapshot.host_name}
        </span>
        {unreachable && <AlertTriangle size={12} className="text-nord-11 shrink-0" />}
        {draining && (
          <span className="text-[10px] px-1 rounded bg-nord-13/25 text-nord-13 shrink-0" title="Draining">
            drain
          </span>
        )}
      </span>

      <span className="flex items-center gap-1 text-[10px] text-nord-4 overflow-hidden">
        {snapshot.roles.map((role) => (
          <span key={role} className="px-1.5 py-0.5 rounded bg-nord-2 shrink-0">
            {role.slice(0, 5)}
          </span>
        ))}
        {snapshot.gpu_type && (
          <span className="px-1.5 py-0.5 rounded bg-nord-2 truncate" title={getGpuTypeLabel(snapshot.gpu_type)}>
            {getGpuTypeLabel(snapshot.gpu_type)}
          </span>
        )}
      </span>

      {DIMS.map(({ key, label }) => (
        <MiniBar
          key={key}
          label={label}
          totalGb={snapshot[`${key}_total_gb`]}
          availableGb={snapshot[`${key}_available_gb`]}
        />
      ))}

      <span className="text-xs text-nord-4 tabular-nums whitespace-nowrap justify-self-end">
        {snapshot.running_instance_count}/{snapshot.instance_count} inst
        {snapshot.active_jobs.length > 0 && ` · ${snapshot.active_jobs.length} job`}
      </span>
    </button>
  );
}

/** Used fraction only: at this size, segment breakdown is noise. */
function MiniBar({
  label,
  totalGb,
  availableGb,
}: {
  label: string;
  totalGb: number | null | undefined;
  availableGb: number | null | undefined;
}) {
  const hasData = totalGb != null && availableGb != null && totalGb > 0;
  const usedPct = hasData ? Math.max(0, Math.min(100, ((totalGb - availableGb) / totalGb) * 100)) : 0;

  return (
    <span className="flex flex-col gap-1 min-w-0" title={hasData ? `${label}: ${availableGb.toFixed(1)} GB free` : ''}>
      <span className="flex items-baseline justify-between gap-1 text-[10px] text-nord-4">
        <span>{label}</span>
        <span className="tabular-nums">{hasData ? `${availableGb.toFixed(0)} GB free` : '—'}</span>
      </span>
      <span className="h-1.5 w-full rounded-full bg-nord-2 overflow-hidden block">
        {hasData && (
          <span
            className={cn('h-full block rounded-full', usedPct > 90 ? 'bg-nord-12' : 'bg-nord-10')}
            style={{ width: `${usedPct}%` }}
          />
        )}
      </span>
    </span>
  );
}
