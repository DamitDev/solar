import { useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronUp, Cpu, HardDrive, Layers, MemoryStick, Server } from 'lucide-react';
import { ActiveJobSummary, HostResourceSnapshot } from '@/api/types';
import { cn, getStatusColor, getGpuTypeLabel, getGpuTypeBadgeClass, getRoleBadgeClass } from '@/lib/utils';
import { ResourceBar, ResourceBarSegment } from './ResourceBar';

const DIM_LABELS: Record<'vram' | 'ram' | 'disk', string> = {
  vram: 'VRAM',
  ram: 'RAM',
  disk: 'Disk',
};

const DIM_ICONS: Record<'vram' | 'ram' | 'disk', React.ReactNode> = {
  vram: <Cpu size={12} />,
  ram: <MemoryStick size={12} />,
  disk: <HardDrive size={12} />,
};

const fmtGb = (value: number | null | undefined): string => (value == null ? '—' : `${value.toFixed(1)} GB`);

/** Build the [inference, training?, reserved?] segments for one dimension. */
function buildSegments(snapshot: HostResourceSnapshot, dim: 'vram' | 'ram' | 'disk'): ResourceBarSegment[] {
  const systemUsed = snapshot[`${dim}_system_used_gb`] ?? 0;
  const training = snapshot[`${dim}_training_used_gb`] ?? 0;
  const reserved = snapshot[`${dim}_reserved_headroom_gb`] ?? 0;

  const segments: ResourceBarSegment[] = [
    {
      key: 'inference',
      label: `Inference (${snapshot.running_instance_count} instance${snapshot.running_instance_count === 1 ? '' : 's'})`,
      gb: Math.max(0, systemUsed - training),
      className: 'bg-nord-10',
    },
  ];
  if (training > 0) {
    segments.push({
      key: 'training',
      label: `Training (${snapshot.active_jobs.length} job step${snapshot.active_jobs.length === 1 ? '' : 's'})`,
      gb: training,
      className: 'bg-nord-15',
    });
  }
  if (reserved > 0) {
    segments.push({
      key: 'reserved',
      label: `Reserved (${snapshot.reservation_count} reservation${snapshot.reservation_count === 1 ? '' : 's'})`,
      gb: reserved,
      className: 'bg-nord-13',
    });
  }
  return segments;
}

/**
 * Active training job steps, rendered as a compact list.
 * Shared rendering path — U-001 reuses this for host-card training indicators.
 */
function ActiveJobsList({ jobs }: { jobs: ActiveJobSummary[] }) {
  if (jobs.length === 0) {
    return <p className="text-sm text-nord-4">No active job steps</p>;
  }
  return (
    <ul className="space-y-1.5">
      {jobs.map((job) => {
        const terminal = job.status === 'completed' || job.status === 'failed';
        const stepName = terminal ? job.last_step_name : job.current_step_name;
        return (
          <li key={job.job_id} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
            <span
              className="font-mono text-xs text-nord-6 break-all"
              title={job.submission_id ? `submission: ${job.submission_id}` : undefined}
            >
              {job.job_id}
            </span>
            {job.name && <span className="font-medium text-nord-6">{job.name}</span>}
            {stepName && (
              <span className="text-xs text-nord-4">
                step: {stepName}
                {job.current_step_index != null ? ` (${job.current_step_index})` : ''}
              </span>
            )}
            <span className={cn('px-2 py-0.5 rounded-full text-xs font-medium', getStatusColor(job.status))}>
              {job.status}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function HostResourceCard({ snapshot }: { snapshot: HostResourceSnapshot }) {
  const [expanded, setExpanded] = useState(false);
  const unreachable = !snapshot.reachable;

  return (
    <div
      className={cn(
        'bg-nord-1 border rounded-lg p-4 space-y-3',
        unreachable ? 'border-nord-11/40 opacity-70' : 'border-nord-3',
      )}
    >
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Server size={18} className="text-nord-8 shrink-0" />
          <h3 className="text-nord-6 font-semibold truncate" title={snapshot.url}>
            {snapshot.host_name}
          </h3>
          {snapshot.version && <span className="text-xs text-nord-4 shrink-0">v{snapshot.version}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {snapshot.roles.map((role) => (
            <span key={role} className={cn('px-2 py-1 rounded-full text-xs font-medium', getRoleBadgeClass(role))}>
              {role}
            </span>
          ))}
          {snapshot.gpu_type && (
            <span
              className={cn('px-2 py-1 rounded-full text-xs font-medium', getGpuTypeBadgeClass(snapshot.gpu_type))}
              title={`Acceleration: ${getGpuTypeLabel(snapshot.gpu_type)}`}
            >
              {getGpuTypeLabel(snapshot.gpu_type)}
            </span>
          )}
          <span className={cn('px-2 py-1 rounded-full text-xs font-medium', getStatusColor(snapshot.status))}>
            {snapshot.status}
          </span>
          {unreachable && (
            <span
              className="px-2 py-1 rounded-full text-xs font-medium bg-nord-11 bg-opacity-30 text-nord-11 flex items-center gap-1"
              title={snapshot.error ?? undefined}
            >
              <AlertTriangle size={12} />
              unreachable
            </span>
          )}
        </div>
      </div>

      {/* Per-dimension segmented bars */}
      {(['vram', 'ram', 'disk'] as const).map((dim) => {
        const total = snapshot[`${dim}_total_gb`] ?? null;
        const available = snapshot[`${dim}_available_gb`] ?? null;
        const noData = total == null;
        return (
          <div key={dim}>
            <ResourceBar
              dimLabel={DIM_LABELS[dim]}
              icon={DIM_ICONS[dim]}
              totalGb={total}
              segments={buildSegments(snapshot, dim)}
              availableGb={available}
              unavailable={unreachable || noData}
            />
            {noData && !unreachable && (
              <div className="mt-1">
                <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-nord-2 text-nord-4">
                  no live data
                </span>
              </div>
            )}
          </div>
        );
      })}

      {/* Workloads panel */}
      <div className="border-t border-nord-3 pt-3">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-center justify-between gap-2 text-sm text-nord-6 font-medium"
        >
          <span className="flex items-center gap-2">
            <Layers size={14} className="text-nord-4" />
            Workloads
          </span>
          <span className="flex items-center gap-2">
            <span className="text-xs font-normal text-nord-4">
              {snapshot.running_instance_count} instance{snapshot.running_instance_count === 1 ? '' : 's'} ·{' '}
              {snapshot.active_jobs.length} job{snapshot.active_jobs.length === 1 ? '' : 's'} ·{' '}
              {snapshot.reservation_count} reservation{snapshot.reservation_count === 1 ? '' : 's'}
            </span>
            {expanded ? <ChevronUp size={16} className="shrink-0" /> : <ChevronDown size={16} className="shrink-0" />}
          </span>
        </button>

        {expanded && (
          <div className="mt-3 space-y-4">
            {unreachable ? (
              <p className="flex items-center gap-2 text-sm text-nord-11">
                <AlertTriangle size={14} />
                {snapshot.error ?? 'Host unreachable'}
              </p>
            ) : (
              <>
                {/* Instances */}
                <div>
                  <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-nord-4">
                    Instances ({snapshot.instance_count})
                  </h4>
                  {snapshot.instances.length > 0 ? (
                    <ul className="space-y-1.5">
                      {snapshot.instances.map((inst) => (
                        <li key={inst.id} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                          <span className="min-w-0 font-medium text-nord-6">{inst.alias || inst.id}</span>
                          {inst.status && (
                            <span
                              className={cn(
                                'px-2 py-0.5 rounded-full text-xs font-medium',
                                getStatusColor(inst.status),
                              )}
                            >
                              {inst.status}
                            </span>
                          )}
                          {inst.backend_type && <span className="text-xs text-nord-4">{inst.backend_type}</span>}
                          {inst.port != null && <span className="font-mono text-xs text-nord-4">:{inst.port}</span>}
                        </li>
                      ))}
                    </ul>
                  ) : snapshot.instance_count > 0 ? (
                    <p className="text-sm text-nord-4">
                      {snapshot.instance_count} instance{snapshot.instance_count === 1 ? '' : 's'} — details unavailable
                    </p>
                  ) : (
                    <p className="text-sm text-nord-4">No instances</p>
                  )}
                </div>

                {/* Training jobs */}
                <div>
                  <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-nord-4">
                    Training Jobs ({snapshot.active_jobs.length})
                  </h4>
                  <ActiveJobsList jobs={snapshot.active_jobs} />
                </div>

                {/* Reservations */}
                <div>
                  <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-nord-4">
                    Reservations ({snapshot.reservation_count})
                  </h4>
                  {snapshot.reservations.length > 0 ? (
                    <ul className="space-y-1.5">
                      {snapshot.reservations.map((res) => (
                        <li key={res.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                          <span className="font-mono text-xs text-nord-6 break-all" title={res.id}>
                            {res.job_id}
                          </span>
                          <span
                            className={cn(
                              'px-2 py-0.5 rounded-full text-xs font-medium',
                              res.status === 'pending' ? 'text-nord-0 bg-nord-13' : getStatusColor(res.status),
                            )}
                          >
                            {res.status}
                          </span>
                          <span className="text-xs text-nord-4">
                            req {fmtGb(res.vram_gb)} VRAM · {fmtGb(res.ram_gb)} RAM · {fmtGb(res.disk_gb)} disk
                          </span>
                          {res.status === 'running' && (
                            <span className="text-xs text-nord-4">
                              actual {fmtGb(res.actual_vram_gb)} VRAM · {fmtGb(res.actual_ram_gb)} RAM ·{' '}
                              {fmtGb(res.actual_disk_gb)} disk
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-sm text-nord-4">No reservations</p>
                  )}
                  {snapshot.reservation_count > 0 && (
                    <p className="mt-2 text-xs text-nord-4">
                      Totals: {snapshot.reservation_vram_total_gb.toFixed(1)} VRAM ·{' '}
                      {snapshot.reservation_ram_total_gb.toFixed(1)} RAM ·{' '}
                      {snapshot.reservation_disk_total_gb.toFixed(1)} disk
                    </p>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
