import type { Intent } from '@/api/types';
import type { RegisteredHandler } from './types';

export const handleIntentUpdate: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  // Full intent record (reconciler emits the bare record — bindEvent wraps it in data)
  if (event.data?.id) {
    ctx.setIntents((prev) => {
      const m = new Map(prev);
      m.set(event.data.id, event.data as Intent);
      return m;
    });
    h.onIntentUpdate?.(event.data as Intent);
  }
};

export const handleIntentRemoved: RegisteredHandler = (event, ctx) => {
  const h = ctx.handlersRef.current;
  if (event.data?.id) {
    ctx.setIntents((prev) => {
      const m = new Map(prev);
      m.delete(event.data.id);
      return m;
    });
    h.onIntentRemoved?.(event.data.id, event.data.alias);
  }
};
