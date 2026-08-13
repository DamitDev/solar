import { AlertCircle, Loader2, X } from 'lucide-react';
import { RequestState } from '@/hooks/useEventStream';
import { parseGatewayEvent } from '@/lib/gatewayErrors';
import { cn } from '@/lib/utils';

interface Props {
  requests: RequestState[];
  onDismiss: (requestId: string) => void;
  /** Traces the request's path through the graph. */
  onSelect?: (request: RequestState) => void;
  selectedId?: string;
}

/**
 * In-flight and failed requests as a bounded list.
 *
 * Requests are traffic, not topology: a list that turns over is a better fit
 * than a node per request, which is what made the old graph grow without
 * bound exactly when it was busiest. Selecting one traces it on the canvas.
 */
export function RequestTicker({ requests, onDismiss, onSelect, selectedId }: Props) {
  return (
    <div data-testid="request-ticker" className="bg-nord-1 border border-nord-3 rounded flex flex-col min-h-0">
      <header className="px-3 py-2 border-b border-nord-3 flex items-center justify-between">
        <span className="text-nord-6 text-sm font-medium">Live requests</span>
        <span className="text-xs text-nord-4 tabular-nums">{requests.length}</span>
      </header>

      <div className="overflow-auto">
        {requests.length === 0 ? (
          <p className="px-3 py-8 text-center text-sm text-nord-4">Nothing in flight</p>
        ) : (
          requests.map((request) => (
            <TickerRow
              key={request.request_id}
              request={request}
              onDismiss={onDismiss}
              onSelect={onSelect}
              selected={selectedId === request.request_id}
            />
          ))
        )}
      </div>
    </div>
  );
}

function TickerRow({
  request,
  onDismiss,
  onSelect,
  selected,
}: {
  request: RequestState;
  onDismiss: (id: string) => void;
  onSelect?: (request: RequestState) => void;
  selected: boolean;
}) {
  const failed = request.status === 'error';
  // Reuse the gateway parser so a failure reads the same on both pages.
  const parsed = failed
    ? parseGatewayEvent({ type: 'request_error', data: { error_message: request.error_message } })
    : null;

  return (
    <div
      onClick={() => onSelect?.(request)}
      className={cn(
        'px-3 py-2 border-b border-nord-3 last:border-b-0 flex items-start gap-2 transition-opacity',
        onSelect && 'cursor-pointer hover:bg-nord-2/50',
        selected && 'bg-nord-10/20',
        request.removing && 'opacity-0',
      )}
    >
      <span className="pt-0.5 shrink-0">
        {failed ? (
          <AlertCircle size={13} className="text-nord-12" />
        ) : (
          <Loader2 size={13} className={cn('text-nord-8', request.status !== 'pending' && 'animate-spin')} />
        )}
      </span>

      <div className="min-w-0 flex-1">
        <div className="text-xs text-nord-6 truncate">{request.resolved_model || request.model || 'unknown model'}</div>
        <div className="text-[10px] text-nord-4 truncate">
          {request.status === 'pending' ? 'choosing an instance' : (request.host_name ?? request.host_id ?? 'unrouted')}
          {request.duration != null && ` · ${request.duration.toFixed(2)}s`}
        </div>
        {parsed && <div className="text-[10px] text-nord-12 mt-0.5 line-clamp-2">{parsed.title}</div>}
      </div>

      {failed && (
        <button
          onClick={(event) => {
            event.stopPropagation();
            onDismiss(request.request_id);
          }}
          aria-label="Dismiss request"
          className="text-nord-4 hover:text-nord-6 shrink-0"
        >
          <X size={12} />
        </button>
      )}
    </div>
  );
}
