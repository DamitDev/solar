import { prunePullProgress } from '@/hooks/eventHandlers/pullProgress';
import type { RegisteredHandler } from './types';

export const handlePullProgress: RegisteredHandler = (event, ctx) => {
  // C4: latest pull progress per host|source_uri, as rebroadcast
  // by control ({host_id, host_name, timestamp, data}).
  if (event.host_id && event.data?.source_uri) {
    const key = `${event.host_id}|${event.data.source_uri}`;
    ctx.setPullProgress((prev) => {
      const m = new Map(prev);
      m.set(key, {
        host_id: event.host_id!,
        host_name: event.host_name ?? null,
        timestamp: event.timestamp,
        data: event.data,
      });
      return prunePullProgress(m, key);
    });
  }
};
