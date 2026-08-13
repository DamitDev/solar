import { FlowTotals } from './workload';

/**
 * The numbers you still need when the canvas is zoomed out far enough that
 * node labels are unreadable. Fixed size, whatever the fleet looks like.
 */
export function SummaryBar({ totals, shown, total }: { totals: FlowTotals; shown: number; total: number }) {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-nord-4 tabular-nums">
      <Stat label="Endpoints" value={totals.endpoints} />
      <Stat label="Queued" value={totals.pending} tone={totals.pending > 0 ? 'warn' : undefined} />
      <Stat label="Processing" value={totals.processing} tone={totals.processing > 0 ? 'accent' : undefined} />
      <Stat label="Failed" value={totals.errored} tone={totals.errored > 0 ? 'error' : undefined} />
      <Stat label="Hosts online" value={`${totals.hostsOnline}/${totals.hostsTotal}`} />
      <Stat label="Instances running" value={`${totals.instancesRunning}/${totals.instancesTotal}`} />
      {shown !== total && <Stat label="Shown" value={`${shown}/${total}`} tone="accent" />}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number | string; tone?: 'accent' | 'warn' | 'error' }) {
  const color = tone === 'error' ? 'text-nord-11' : tone === 'warn' ? 'text-nord-13' : tone ? 'text-nord-8' : '';
  return (
    <span>
      {label} <span className={color || 'text-nord-6'}>{value}</span>
    </span>
  );
}
