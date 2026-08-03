/**
 * Intent phase + reconcile badges (U-003, spec deployment-intent.md §7.2).
 */

import { getIntentPhaseColor } from '@/lib/utils';

const PHASE_TOOLTIPS: Record<string, string> = {
  pending: 'Stored and validated — reconciliation not yet running (S-041)',
  reconciling: 'Reconciler is actively working toward the desired state',
  ready: 'All desired replicas ready',
  degraded: 'Partial fulfillment — some but not all replicas ready',
  failed: 'Reconciliation cannot make progress, zero replicas ready',
  deleting: 'Delete received — stopping managed instances',
  deleted: 'All managed instances removed',
};

export function IntentPhaseBadge({ phase, reconcile }: { phase: string; reconcile?: string | null }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        title={PHASE_TOOLTIPS[phase] ?? phase}
        className={`px-2 py-0.5 rounded text-xs font-medium ${getIntentPhaseColor(phase)}`}
      >
        {phase}
      </span>
      {reconcile && <span className="text-xs text-nord-4">· {reconcile}</span>}
    </span>
  );
}
