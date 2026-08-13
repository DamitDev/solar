import { memo } from 'react';
import { Handle, NodeProps, Position } from 'reactflow';
import { AlertTriangle, ChevronDown, ChevronRight, MoreHorizontal, Server, Share2, Zap } from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  EndpointNodeData,
  FlowNodeData,
  GatewayNodeData,
  HostNodeData,
  ModelNodeData,
  NODE_SIZE,
  OverflowNodeData,
} from './graph';

/**
 * Every node is boxed to the exact size the layout reserved for its kind.
 * The size is not a styling detail: dagre placed the box using these numbers,
 * so letting content grow the node is what reintroduces overlap.
 */
function NodeBox({
  kind,
  dimmed,
  selected,
  className,
  children,
  title,
}: {
  kind: keyof typeof NODE_SIZE;
  dimmed: boolean;
  selected: boolean;
  className?: string;
  children: React.ReactNode;
  title?: string;
}) {
  const { width, height } = NODE_SIZE[kind];
  return (
    <div
      style={{ width, height }}
      title={title}
      className={cn(
        'rounded border overflow-hidden cursor-pointer transition-opacity',
        selected && 'ring-2 ring-nord-8',
        dimmed && 'opacity-20',
        className,
      )}
    >
      {children}
    </div>
  );
}

/** Connection points only; React Flow needs them to anchor edges. */
function Ports({ source = true, target = true }: { source?: boolean; target?: boolean }) {
  return (
    <>
      {target && <Handle type="target" position={Position.Left} isConnectable={false} />}
      {source && <Handle type="source" position={Position.Right} isConnectable={false} />}
    </>
  );
}

export interface NodeExtras {
  dimmed: boolean;
  selected: boolean;
}

type Props<T extends FlowNodeData> = NodeProps<T & NodeExtras>;

export const EndpointNode = memo(({ data }: Props<EndpointNodeData>) => (
  <NodeBox
    kind="endpoint"
    dimmed={data.dimmed}
    selected={data.selected}
    className="bg-nord-1 border-nord-3 px-2.5 py-1.5"
    title={data.label}
  >
    <Ports target={false} />
    <div className="flex items-center gap-1.5 min-w-0">
      <Zap size={12} style={{ color: data.color }} className="shrink-0" />
      <span className="text-xs text-nord-6 truncate flex-1">{data.label}</span>
    </div>
    <div className="mt-0.5 flex items-center gap-2 text-[10px] tabular-nums">
      {data.inFlight > 0 ? (
        <span className="text-nord-8">{data.inFlight} in flight</span>
      ) : (
        <span className="text-nord-4">idle</span>
      )}
      {data.errors > 0 && <span className="text-nord-11">{data.errors} failed</span>}
    </div>
  </NodeBox>
));
EndpointNode.displayName = 'EndpointNode';

export const GatewayNode = memo(({ data }: Props<GatewayNodeData>) => (
  <NodeBox
    kind="gateway"
    dimmed={data.dimmed}
    selected={data.selected}
    className="bg-nord-10/20 border-nord-10 px-3 py-2"
  >
    <Ports />
    <div className="flex items-center gap-1.5">
      <Share2 size={13} className="text-nord-8 shrink-0" />
      <span className="text-sm text-nord-6 font-medium">Gateway</span>
    </div>
    <dl className="mt-1 space-y-0.5 text-[11px] text-nord-4 tabular-nums">
      <Row label="Queued" value={data.queued} tone={data.queued > 0 ? 'text-nord-13' : undefined} />
      <Row label="Processing" value={data.processing} tone={data.processing > 0 ? 'text-nord-8' : undefined} />
      <Row label="Hosts online" value={`${data.hostsOnline}/${data.hostsTotal}`} />
    </dl>
  </NodeBox>
));
GatewayNode.displayName = 'GatewayNode';

function Row({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className="flex justify-between gap-2">
      <dt>{label}</dt>
      <dd className={tone}>{value}</dd>
    </div>
  );
}

export const ModelNode = memo(({ data }: Props<ModelNodeData>) => {
  const degraded = data.running === 0 && data.instances > 0;
  return (
    <NodeBox
      kind="model"
      dimmed={data.dimmed}
      selected={data.selected}
      title={data.model ? `${data.alias} (${data.model})` : data.alias}
      className={cn(
        'px-2.5 py-2',
        data.errors > 0
          ? 'border-nord-11/60 bg-nord-11/10'
          : data.inFlight > 0
            ? 'border-nord-10 bg-nord-10/15'
            : degraded
              ? 'border-nord-3 bg-nord-1'
              : 'border-nord-3 bg-nord-2',
      )}
    >
      <Ports />
      <div className="flex items-center gap-1.5 min-w-0">
        {data.expanded ? (
          <ChevronDown size={13} className="text-nord-8 shrink-0" />
        ) : (
          <ChevronRight size={13} className="text-nord-4 shrink-0" />
        )}
        <span className="text-xs text-nord-6 truncate flex-1">{data.alias}</span>
        {data.inFlight > 0 && (
          <span className="text-[10px] px-1 rounded bg-nord-10 text-nord-6 tabular-nums shrink-0">{data.inFlight}</span>
        )}
        {data.errors > 0 && <AlertTriangle size={11} className="text-nord-11 shrink-0" />}
      </div>

      {data.model && <div className="text-[10px] text-nord-4 truncate mt-0.5 pl-[18px]">{data.model}</div>}

      <div className="mt-1 pl-[18px] flex items-center gap-2 text-[10px] text-nord-4 tabular-nums">
        <span className={degraded ? 'text-nord-12' : ''}>
          {data.running}/{data.instances} running
        </span>
        <span>
          on {data.hosts} {data.hosts === 1 ? 'host' : 'hosts'}
        </span>
      </div>
    </NodeBox>
  );
});
ModelNode.displayName = 'ModelNode';

export const HostNode = memo(({ data }: Props<HostNodeData>) => {
  const failed = data.status === 'failed';
  const running = data.status === 'running';
  const load = hostLoad(data);
  const phase = hostPhase(data);

  return (
    <NodeBox
      kind="host"
      dimmed={data.dimmed}
      selected={data.selected}
      title={`${data.alias} on ${data.hostName} — ${data.status}`}
      className={cn(
        'px-2.5 py-2',
        failed
          ? 'border-nord-11/50 bg-nord-11/10'
          : data.inFlight > 0
            ? 'border-nord-10 bg-nord-10/10'
            : running
              ? 'border-nord-3 bg-nord-1'
              : 'border-nord-3 bg-nord-1 opacity-70',
      )}
    >
      <Ports source={false} />
      <div className="flex items-center gap-1.5 min-w-0">
        <Server size={11} className={cn('shrink-0', failed ? 'text-nord-11' : 'text-nord-8')} />
        <span className="text-xs text-nord-6 truncate flex-1">{data.hostName}</span>
        {data.instances > 1 && <span className="text-[10px] text-nord-4 shrink-0">×{data.instances}</span>}
        {data.inFlight > 0 && (
          <span className="text-[10px] px-1 rounded bg-nord-10 text-nord-6 tabular-nums shrink-0">{data.inFlight}</span>
        )}
        {failed && <AlertTriangle size={11} className="text-nord-11 shrink-0" />}
      </div>

      {data.hostStatus !== 'online' && <div className="text-[10px] text-nord-11 mt-0.5">host {data.hostStatus}</div>}

      <div className="mt-1.5 h-1 rounded-full bg-nord-2 overflow-hidden">
        <div
          className={cn('h-full rounded-full transition-all duration-300', load >= 1 ? 'bg-nord-12' : 'bg-nord-8')}
          style={{ width: `${load * 100}%` }}
        />
      </div>

      <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-nord-4 tabular-nums">
        <span className="truncate">{running ? (phase ?? 'idle') : data.status}</span>
        {data.instances > 1 && (
          <span className="shrink-0">
            {data.running}/{data.instances}
          </span>
        )}
      </div>
    </NodeBox>
  );
});
HostNode.displayName = 'HostNode';

/** Slots in use across the host's instances of this model. */
function hostLoad(data: HostNodeData): number {
  if (data.running === 0) return 0;
  const slots = data.state?.active_slots ?? 0;
  const load = Math.max(slots, data.inFlight);
  if (load === 0) return data.state?.busy ? 0.15 : 0;
  return Math.min(1, load / (4 * data.instances));
}

function hostPhase(data: HostNodeData): string | null {
  const state = data.state;
  if (!state) return null;
  if (state.phase === 'prefill' && state.prefill_progress != null) {
    return `prefill ${Math.round(state.prefill_progress * 100)}%`;
  }
  if (state.decode_tps != null) return `${state.decode_tps.toFixed(0)} tok/s`;
  if (state.phase) return state.phase;
  return state.busy ? 'busy' : null;
}

export const OverflowNode = memo(({ data }: Props<OverflowNodeData>) => (
  <NodeBox
    kind="overflow"
    dimmed={data.dimmed}
    selected={data.selected}
    title={`Show the remaining ${data.hosts} hosts serving ${data.alias}`}
    className="px-2.5 py-2 bg-nord-1 border-nord-3 border-dashed"
  >
    <Ports source={false} />
    <div className="flex items-center gap-1.5">
      <MoreHorizontal size={12} className="text-nord-4 shrink-0" />
      <span className="text-xs text-nord-4 truncate flex-1">{data.hosts} more hosts</span>
      {data.inFlight > 0 && (
        <span className="text-[10px] px-1 rounded bg-nord-10 text-nord-6 tabular-nums shrink-0">{data.inFlight}</span>
      )}
    </div>
    <div className="mt-1 pl-[18px] text-[10px] text-nord-4 tabular-nums">
      {data.running}/{data.instances} running · click to show
    </div>
  </NodeBox>
));
OverflowNode.displayName = 'OverflowNode';
