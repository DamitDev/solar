import { useEffect, useRef } from 'react';

/**
 * C5: shared fallback-polling hook.
 *
 * The webui is event-driven: `endpoints_update` / `intent_update` /
 * `host_health` events update the UI in real time. This hook only polls
 * when the event stream is NOT connected (`enabled: !isConnected`), so a
 * socket outage degrades to REST instead of freezing the view. It skips
 * ticks while the document is hidden (matching the existing inline guards)
 * and does nothing when `enabled` is false.
 *
 * @param callback  REST refresh to run on each poll tick.
 * @param options   `enabled` — whether polling is allowed (gate on
 *                  `!isConnected`); `intervalMs` — poll interval.
 */
export function useFallbackPolling(
  callback: () => void,
  { enabled, intervalMs = 10000 }: { enabled: boolean; intervalMs?: number },
) {
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  useEffect(() => {
    if (!enabled) return;
    const id = window.setInterval(() => {
      if (document.hidden) return;
      callbackRef.current();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [enabled, intervalMs]);
}
