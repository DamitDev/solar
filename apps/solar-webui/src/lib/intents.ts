/**
 * Pure intent list helpers (unit-testable without React).
 */

import { Intent } from '@/api/types';

/**
 * Display order for intent phases. Deterministic — the list is sorted by
 * (phase, alias) so updates never reshuffle rows under the cursor.
 * Attention-forward: things that need watching (pending/reconciling/degraded/
 * failed) come before the healthy and finished ones.
 */
export const PHASE_ORDER: Record<string, number> = {
  pending: 0,
  reconciling: 1,
  degraded: 2,
  failed: 3,
  ready: 4,
  deleting: 5,
  deleted: 6,
};

/**
 * Stable intent ordering: phase first, then alias. Sorting by updated_at
 * reorders rows on every status update and causes misclicks; phase+alias is
 * a total order (alias is unique per intent), so rows never jump.
 */
export function sortIntents(intents: Intent[]): Intent[] {
  return [...intents].sort((a, b) => {
    const pa = PHASE_ORDER[a.status?.phase ?? ''] ?? 99;
    const pb = PHASE_ORDER[b.status?.phase ?? ''] ?? 99;
    if (pa !== pb) return pa - pb;
    return a.alias.localeCompare(b.alias);
  });
}
