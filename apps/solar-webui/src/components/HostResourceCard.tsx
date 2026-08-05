import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Cpu,
  HardDrive,
  Layers,
  MemoryStick,
  MoveRight,
  PlayCircle,
  PowerOff,
  Server,
} from 'lucide-react';
import solarClient from '@/api/client';
import { ActiveJobSummary, HostDrainStatus, HostResourceSnapshot } from '@/api/types';
import { cn, getStatusColor, getGpuTypeLabel, getGpuTypeBadgeClass, getRoleBadgeClass } from '@/lib/utils';
import { DrainHostModal } from './DrainHostModal';
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

/**
 * Drain progress for a host being emptied (U-005, host-draining.md §4.3–§4.4).
 * A stalled drain is called out separately: it looks identical to a progressing
 * one in replica counts, but only one of them needs an operator.
 */
function DrainPanel({ status }: { status: HostDrainStatus }) {
  const drained = status.drain_state === 'drained';
  const blocked = status.replicas.filter((r) => r.blocked_reason);

  return (
    <div
      className={cn(
        'rounded-md border p-3 space-y-2 text-sm',
        status.stalled ? 'border-nord-11 bg-nord-11 bg-opacity-10' : 'border-nord-12/50 bg-nord-12 bg-opacity-5',
      )}
    >
      <p className="flex items-center gap-2 text-nord-6">
        {status.stalled ? (
          <AlertTriangle size={14} className="shrink-0 text-nord-11" />
        ) : (
          <MoveRight size={14} className="shrink-0 text-nord-13" />
        )}
        {drained ? (
          <span>Drained — nothing is running here. Safe to take offline.</span>
        ) : status.stalled ? (
          <span>
            Drain blocked — {status.managed_remaining} managed instance{status.managed_remaining === 1 ? '' : 's'}{' '}
            cannot be moved
          </span>
        ) : (
          <span>
            Draining — {status.managed_remaining} managed instance{status.managed_remaining === 1 ? '' : 's'} left to
            move
          </span>
        )}
      </p>

      {blocked.length > 0 && (
        <ul className="space-y-1">
          {blocked.map((r) => (
            <li key={r.instance_id} className="text-xs text-nord-4">
              <span className="font-medium text-nord-6">{r.alias || r.instance_id}</span>: {r.blocked_reason}
            </li>
          ))}
        </ul>
      )}

      {status.manual_running > 0 && (
        <p className="text-xs text-nord-13">
          {status.manual_running} manual instance{status.manual_running === 1 ? '' : 's'} still running — draining
          leaves those to you.
        </p>
      )}
    </div>
  );
}

export function HostResourceCard({
  snapshot,
  onDrainChanged,
}: {
  snapshot: HostResourceSnapshot;
  onDrainChanged?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [drainStatus, setDrainStatus] = useState<HostDrainStatus | null>(null);
  const [showDrainModal, setShowDrainModal] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [drainError, setDrainError] = useState<string | null>(null);
  const unreachable = !snapshot.reachable;
  const draining = snapshot.drain_state != null;

  // Progress (and stall reasons) only exist while a drain is running, so the
  // extra request is scoped to that. Re-runs when the page refetches with a
  // changed instance count, i.e. when a replica has actually moved.
  const refreshDrainStatus = useCallback(async () => {
    try {
      setDrainStatus(await solarClient.getDrainStatus(snapshot.host_id));
    } catch (err) {
      console.error('Failed to read drain status:', err);
    }
  }, [snapshot.host_id]);

  useEffect(() => {
    if (!draining) {
      setDrainStatus(null);
      return;
    }
    refreshDrainStatus();
  }, [draining, snapshot.instance_count, snapshot.running_instance_count, refreshDrainStatus]);

  const handleResume = async () => {
    setResuming(true);
    setDrainError(null);
    try {
      await solarClient.resumeHost(snapshot.host_id);
      setDrainStatus(null);
      onDrainChanged?.();
    } catch (err: any) {
      console.error('Failed to resume host:', err);
      setDrainError(err?.response?.data?.detail || err?.message || 'Failed to resume host');
    } finally {
      setResuming(false);
    }
  };

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
          {/* Separate from the status badge on purpose: a draining host is
              still online, and the two answer different questions. */}
          {snapshot.drain_state === 'draining' && (
            <span
              className={cn(
                'px-2 py-1 rounded-full text-xs font-medium flex items-center gap-1',
                drainStatus?.stalled
                  ? 'bg-nord-11 bg-opacity-30 text-nord-11'
                  : 'bg-nord-13 bg-opacity-30 text-nord-13',
              )}
              title={drainStatus?.stalled ? 'No host has room for the remaining instances' : 'Moving managed instances'}
            >
              {drainStatus?.stalled && <AlertTriangle size={12} />}
              {drainStatus?.stalled ? 'draining (blocked)' : 'draining'}
            </span>
          )}
          {snapshot.drain_state === 'drained' && (
            <span
              className="px-2 py-1 rounded-full text-xs font-medium bg-nord-3 text-nord-4"
              title="Evacuated — safe to take offline"
            >
              drained
            </span>
          )}
          {unreachable && (
            <span
              className="px-2 py-1 rounded-full text-xs font-medium bg-nord-11 bg-opacity-30 text-nord-11 flex items-center gap-1"
              title={snapshot.error ?? undefined}
            >
              <AlertTriangle size={12} />
              unreachable
            </span>
          )}
          {draining ? (
            <button
              onClick={handleResume}
              disabled={resuming}
              className="px-2 py-1 rounded text-xs font-medium bg-nord-3 text-nord-6 hover:bg-nord-2 transition-colors disabled:opacity-50 flex items-center gap-1"
              title="Return this host to service"
            >
              <PlayCircle size={12} />
              {resuming ? 'Resuming...' : 'Resume'}
            </button>
          ) : (
            <button
              onClick={() => setShowDrainModal(true)}
              className="px-2 py-1 rounded text-xs font-medium bg-nord-3 text-nord-6 hover:bg-nord-2 transition-colors flex items-center gap-1"
              title="Move managed replicas away and stop new placement (maintenance)"
            >
              <PowerOff size={12} />
              Drain
            </button>
          )}
        </div>
      </div>

      {drainError && <p className="text-sm text-nord-11">{drainError}</p>}
      {draining && drainStatus && <DrainPanel status={drainStatus} />}

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
                          {/* Whether an instance is managed decides
                              whether a drain can move it (S-043 §3). */}
                          <span
                            className={cn(
                              'px-2 py-0.5 rounded-full text-xs',
                              inst.managed_by === 'intent' ? 'bg-nord-10/30 text-nord-8' : 'bg-nord-3 text-nord-4',
                            )}
                            title={
                              inst.managed_by === 'intent'
                                ? 'Managed automatically — moved to another host when this one is drained.'
                                : 'Created manually — never moved by a drain'
                            }
                          >
                            {inst.managed_by === 'intent' ? 'managed' : 'manual'}
                          </span>
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

      {showDrainModal && (
        <DrainHostModal
          hostId={snapshot.host_id}
          hostName={snapshot.host_name}
          onClose={() => setShowDrainModal(false)}
          onDraining={() => {
            setShowDrainModal(false);
            refreshDrainStatus();
            onDrainChanged?.();
          }}
        />
      )}
    </div>
  );
}
