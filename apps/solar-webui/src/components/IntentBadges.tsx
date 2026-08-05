/**
 * Intent phase + reconcile badges (U-003, spec deployment-intent.md §7.2).
 */

import { getIntentPhaseColor } from '@/lib/utils';

const PHASE_TOOLTIPS: Record<string, string> = {
  pending: 'Stored and validated. No instances have been created yet.',
  reconciling: 'Solar Control is actively working toward the requested configuration',
  ready: 'All requested instances are ready',
  degraded: 'Running with fewer instances than requested',
  failed: 'Cannot continue, zero instances ready',
  deleting: 'Delete received — stopping managed instances',
  deleted: 'All managed instances removed',
};

// Display-only labels; the API contract is untouched.
const PHASE_LABELS: Record<string, string> = {
  pending: 'queued',
  reconciling: 'updating',
  ready: 'ready',
  degraded: 'degraded',
  failed: 'failed',
};

export function IntentPhaseBadge({ phase, reconcile }: { phase: string; reconcile?: string | null }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        title={PHASE_TOOLTIPS[phase] ?? phase}
        className={`px-2 py-0.5 rounded text-xs font-medium ${getIntentPhaseColor(phase)}`}
      >
        {PHASE_LABELS[phase] ?? phase}
      </span>
      {reconcile && reconcile !== 'idle' && <span className="text-xs text-nord-4">· updating</span>}
    </span>
  );
}
