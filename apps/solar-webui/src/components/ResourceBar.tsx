import { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export interface ResourceBarSegment {
  key: string;
  label: string; // tooltip text, e.g. "Inference — 2 instances"
  gb: number | null | undefined;
  className: string; // fill classes, e.g. 'bg-nord-10'
}

interface ResourceBarProps {
  dimLabel: string; // 'VRAM' | 'RAM' | 'Disk'
  icon?: ReactNode;
  totalGb: number | null | undefined;
  segments: ResourceBarSegment[]; // rendered in order; widths proportional to gb/total
  availableGb: number | null | undefined; // authoritative free value from the API (S-034)
  unavailable?: boolean; // true → render dashes instead of a bar
}

export function ResourceBar({ dimLabel, icon, totalGb, segments, availableGb, unavailable }: ResourceBarProps) {
  const total = totalGb ?? 0;
  const pct = (gb: number | null | undefined) =>
    total > 0 ? Math.max(0, Math.min(100, ((gb ?? 0) / total) * 100)) : 0;

  if (unavailable) {
    return (
      <div className="space-y-1">
        <div className="flex items-center justify-between text-xs text-nord-4">
          <span className="font-medium flex items-center gap-1">
            {icon}
            {dimLabel}:
          </span>
          <span>—</span>
        </div>
        <div className="w-full h-2.5 bg-nord-2 rounded-full opacity-40" />
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs text-nord-4">
        <span className="font-medium flex items-center gap-1">
          {icon}
          {dimLabel}:
        </span>
        <span>
          {totalGb != null && availableGb != null
            ? `${(totalGb - availableGb).toFixed(1)} / ${totalGb.toFixed(1)} GB (${
                totalGb > 0 ? (((totalGb - availableGb) / totalGb) * 100).toFixed(1) : '0.0'
              }%)`
            : '—'}
        </span>
      </div>
      <div className="w-full h-2.5 bg-nord-2 rounded-full overflow-hidden flex">
        {segments.map((seg) => (
          <div
            key={seg.key}
            className={cn('h-full transition-all duration-300', seg.className)}
            style={{ width: `${pct(seg.gb)}%` }}
            title={seg.gb != null ? `${seg.label}: ${seg.gb.toFixed(1)} GB` : seg.label}
          />
        ))}
      </div>
    </div>
  );
}
