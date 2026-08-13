import { useMemo, useState } from 'react';
import { AlertCircle, ChevronDown, ChevronRight, RefreshCw, TriangleAlert } from 'lucide-react';
import { useGatewayEvents } from '@/hooks/useGatewayEvents';
import { EventGroup, ParsedEvent, formatRelativeTime, groupEvents, hostLabel, statusText } from '@/lib/gatewayErrors';
import { cn } from '@/lib/utils';

interface Props {
  from: string;
  to: string;
  endpointId: string | null;
  live: boolean;
  /** Names hosts that events reference only by id. */
  hostNameFor?: (hostId: string) => string | undefined;
}

/** Muted for reroutes: they are recoveries, not failures. */
const SEVERITY_STYLES = {
  error: { icon: AlertCircle, color: 'text-nord-12' },
  warning: { icon: TriangleAlert, color: 'text-nord-13' },
} as const;

export function EventsPanel({ from, to, endpointId, live, hostNameFor }: Props) {
  const { events, loading, error, truncated, refresh } = useGatewayEvents({ from, to, endpointId, live });
  const [grouped, setGrouped] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const groups = useMemo(() => groupEvents(events), [events]);
  const errorCount = events.filter((e) => e.severity === 'error').length;
  const rerouteCount = events.length - errorCount;

  const toggle = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <div className="bg-nord-1 border border-nord-3 rounded">
      <div className="p-4 flex flex-wrap items-center justify-between gap-3 border-b border-nord-3">
        <div className="flex items-baseline gap-3">
          <span className="text-nord-6 font-medium">Errors & Reroutes</span>
          <span className="text-xs text-nord-4">
            {events.length === 0
              ? 'nothing in this range'
              : [
                  errorCount > 0 && `${errorCount} error${errorCount === 1 ? '' : 's'}`,
                  rerouteCount > 0 && `${rerouteCount} reroute${rerouteCount === 1 ? '' : 's'}`,
                  grouped && groups.length !== events.length && `${groups.length} distinct`,
                ]
                  .filter(Boolean)
                  .join(' • ')}
            {truncated && ' • showing the most recent only'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setGrouped((g) => !g)}
            className={cn(
              'px-2 py-1 text-xs rounded transition-colors',
              grouped ? 'bg-nord-3 text-nord-6' : 'text-nord-4 hover:text-nord-6 hover:bg-nord-2',
            )}
            title="Collapse repeats of the same failure into one row"
          >
            Group similar
          </button>
          <button
            onClick={refresh}
            className="px-3 py-1.5 bg-nord-3 text-nord-6 rounded hover:bg-nord-2"
            title="Refresh events"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      <div className="max-h-96 overflow-auto">
        {error ? (
          <div className="p-6 text-center text-nord-11 text-sm flex items-center justify-center gap-2">
            <AlertCircle size={16} /> {error}
          </div>
        ) : events.length === 0 ? (
          <div className="p-6 text-center text-nord-4 text-sm">
            {loading ? 'Loading events…' : 'No errors or reroutes in this range'}
          </div>
        ) : grouped ? (
          groups.map((group) => (
            <GroupRow
              key={group.signature}
              group={group}
              hostNameFor={hostNameFor}
              expanded={expanded.has(group.signature)}
              onToggle={() => toggle(group.signature)}
            />
          ))
        ) : (
          events.map((event, i) => {
            const key = `${event.requestId ?? 'e'}-${event.timestamp ?? i}-${i}`;
            return (
              <EventRow
                key={key}
                event={event}
                hostNameFor={hostNameFor}
                expanded={expanded.has(key)}
                onToggle={() => toggle(key)}
              />
            );
          })
        )}
      </div>
    </div>
  );
}

function GroupRow({
  group,
  hostNameFor,
  expanded,
  onToggle,
}: {
  group: EventGroup;
  hostNameFor?: (hostId: string) => string | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { latest, count } = group;

  return (
    <div className="border-t border-nord-3 first:border-t-0">
      <EventSummary
        event={latest}
        hostNameFor={hostNameFor}
        count={count}
        expanded={expanded}
        onToggle={onToggle}
        expandable={count > 1 || Boolean(latest.raw)}
      />
      {expanded && (
        <div className="px-4 pb-3 pl-11 space-y-2">
          {latest.raw && <RawBlock raw={latest.raw} />}
          {count > 1 && (
            <div className="text-xs text-nord-4">
              <div className="mb-1">Occurrences</div>
              <div className="space-y-0.5">
                {group.occurrences.slice(0, 20).map((occurrence, i) => (
                  <div key={i} className="flex items-center gap-3 tabular-nums">
                    <span className="text-nord-4">{formatRelativeTime(occurrence.timestamp)}</span>
                    {occurrence.model && <span className="text-nord-6">{occurrence.model}</span>}
                    {hostLabel(occurrence, hostNameFor) && <span>{hostLabel(occurrence, hostNameFor)}</span>}
                  </div>
                ))}
                {count > 20 && <div className="italic">…and {count - 20} more</div>}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function EventRow({
  event,
  hostNameFor,
  expanded,
  onToggle,
}: {
  event: ParsedEvent;
  hostNameFor?: (hostId: string) => string | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="border-t border-nord-3 first:border-t-0">
      <EventSummary
        event={event}
        hostNameFor={hostNameFor}
        expanded={expanded}
        onToggle={onToggle}
        expandable={Boolean(event.raw)}
      />
      {expanded && event.raw && (
        <div className="px-4 pb-3 pl-11">
          <RawBlock raw={event.raw} />
        </div>
      )}
    </div>
  );
}

function EventSummary({
  event,
  hostNameFor,
  count,
  expanded,
  onToggle,
  expandable,
}: {
  event: ParsedEvent;
  hostNameFor?: (hostId: string) => string | undefined;
  count?: number;
  expanded: boolean;
  onToggle: () => void;
  expandable: boolean;
}) {
  const { icon: Icon, color } = SEVERITY_STYLES[event.severity];
  const host = hostLabel(event, hostNameFor);

  return (
    <div
      className={cn('flex items-start gap-2 px-4 py-2.5', expandable && 'cursor-pointer hover:bg-nord-2/40')}
      onClick={expandable ? onToggle : undefined}
    >
      <span className="w-4 pt-0.5 shrink-0 text-nord-4">
        {expandable ? expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} /> : null}
      </span>
      <Icon size={16} className={cn(color, 'shrink-0 mt-0.5')} />

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 flex-wrap">
          {event.status != null && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-nord-2 text-nord-4 tabular-nums shrink-0">
              {event.status}
            </span>
          )}
          <span className="text-nord-6 text-sm break-words">{event.title}</span>
          {count != null && count > 1 && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-nord-3 text-nord-6 tabular-nums shrink-0">×{count}</span>
          )}
        </div>

        <div className="mt-0.5 flex items-center gap-2 flex-wrap text-xs text-nord-4">
          <span title={event.timestamp ?? undefined}>{formatRelativeTime(event.timestamp)}</span>
          {event.model && <Chip>{event.model}</Chip>}
          {host && <Chip>{host}</Chip>}
          {event.code && <Chip>{event.code}</Chip>}
          {event.durationS != null && <span className="tabular-nums">{event.durationS.toFixed(2)}s</span>}
          {event.detail && event.detail !== statusText(event.status ?? 0) && (
            <span className="text-nord-4">{event.detail}</span>
          )}
        </div>
      </div>
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return <span className="px-1.5 py-0.5 rounded bg-nord-2 text-nord-4">{children}</span>;
}

function RawBlock({ raw }: { raw: string }) {
  return (
    <pre className="text-xs bg-nord-0 border border-nord-3 rounded p-2 overflow-auto max-h-56 text-nord-4 whitespace-pre-wrap break-words">
      {raw}
    </pre>
  );
}
