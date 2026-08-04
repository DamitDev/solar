import { useEffect, useMemo, useRef, useState } from 'react';
import { X, Pause, Play, Download } from 'lucide-react';
import { useEventStreamContext } from '@/context/EventStreamContext';
import solarClient from '@/api/client';
import { LogMessage } from '@/api/types';

interface LogViewerProps {
  hostId: string;
  instanceId: string;
  alias: string;
  onClose: () => void;
}

export function LogViewer({ hostId, instanceId, alias, onClose }: LogViewerProps) {
  const logContainerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [historicalLogs, setHistoricalLogs] = useState<LogMessage[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);

  // Use the unified event stream for new logs
  const { isConnected, getInstanceLogs, clearInstanceLogs } = useEventStreamContext();
  const streamLogs = getInstanceLogs(hostId, instanceId);

  // Merge historical logs with stream logs, dedup by seq+timestamp
  // (seq alone collides across instance restarts). Sort by timestamp
  // so ordering is correct even when seq resets or messages arrive
  // out of order via Socket.IO.
  const messages = useMemo(() => {
    const seen = new Set<string>();
    const merged: LogMessage[] = [];

    for (const log of historicalLogs) {
      const key = `${log.seq}:${log.timestamp}`;
      if (!seen.has(key)) {
        seen.add(key);
        merged.push(log);
      }
    }

    for (const log of streamLogs) {
      const key = `${log.seq}:${log.timestamp}`;
      if (!seen.has(key)) {
        seen.add(key);
        merged.push(log);
      }
    }

    return merged.sort((a, b) => {
      if (a.timestamp < b.timestamp) return -1;
      if (a.timestamp > b.timestamp) return 1;
      return (a.seq ?? 0) - (b.seq ?? 0);
    });
  }, [historicalLogs, streamLogs]);

  // Fetch historical logs on mount
  useEffect(() => {
    let cancelled = false;

    const fetchHistory = async () => {
      setLoadingHistory(true);
      setHistoryError(null);

      try {
        const logs = await solarClient.getInstanceLogs(hostId, instanceId);
        if (!cancelled) {
          setHistoricalLogs(logs);
        }
      } catch (err) {
        if (!cancelled) {
          console.error('Failed to fetch historical logs:', err);
          setHistoryError('Failed to load historical logs');
        }
      } finally {
        if (!cancelled) {
          setLoadingHistory(false);
        }
      }
    };

    fetchHistory();

    return () => {
      cancelled = true;
    };
  }, [hostId, instanceId]);

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    if (autoScroll && logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [messages, autoScroll]);

  const handleClear = () => {
    setHistoricalLogs([]);
    clearInstanceLogs(hostId, instanceId);
  };

  const handleDownload = () => {
    const content = messages.map((m) => `[${m.timestamp}] ${m.line}`).join('\n');
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${alias}-logs.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl w-full max-w-[95vw] max-h-[90vh] flex flex-col border border-nord-3">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <div>
            <h2 className="text-lg font-semibold text-nord-6">{alias} - Logs</h2>
            <p className="text-sm text-nord-4">
              {loadingHistory ? (
                <span className="text-nord-13">Loading history...</span>
              ) : isConnected ? (
                <span className="text-nord-14">● Connected (Event Stream)</span>
              ) : (
                <span className="text-nord-11">● Disconnected</span>
              )}
              {historyError && <span className="text-nord-11 ml-2">({historyError})</span>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownload}
              disabled={messages.length === 0}
              className="px-3 py-1 text-xs bg-nord-3 hover:bg-nord-2 rounded transition-colors text-nord-4 disabled:opacity-50"
              title="Download logs"
            >
              <Download size={14} />
            </button>
            <button
              onClick={handleClear}
              className="px-3 py-1 text-xs bg-nord-3 hover:bg-nord-2 rounded transition-colors text-nord-4"
              title="Clear logs"
            >
              Clear
            </button>
            <button
              onClick={() => setAutoScroll(!autoScroll)}
              className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4"
              title={autoScroll ? 'Pause auto-scroll' : 'Resume auto-scroll'}
            >
              {autoScroll ? <Pause size={18} /> : <Play size={18} />}
            </button>
            <button onClick={onClose} className="p-2 hover:bg-nord-2 rounded transition-colors text-nord-4">
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Log content */}
        <div ref={logContainerRef} className="flex-1 overflow-auto p-4 bg-nord-0 text-nord-6 font-mono text-sm">
          {loadingHistory ? (
            <div className="text-nord-3">Loading historical logs...</div>
          ) : messages.length === 0 ? (
            <div className="text-nord-3">
              {isConnected
                ? 'No logs yet. Logs appear when the instance produces output.'
                : 'Connecting to event stream...'}
            </div>
          ) : (
            messages.map((msg) => (
              <div key={`${msg.seq}:${msg.timestamp}`} className="whitespace-pre">
                <span className="text-nord-3">[{msg.timestamp}]</span> {msg.line}
              </div>
            ))
          )}
        </div>

        {/* Footer */}
        <div className="p-3 border-t border-nord-3 bg-nord-2 text-sm text-nord-4">
          {messages.length} log messages • Scroll horizontally for long lines
        </div>
      </div>
    </div>
  );
}
