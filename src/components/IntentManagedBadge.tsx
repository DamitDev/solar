/**
 * IntentManagedBadge — ownership chip for intent-managed instances
 * (U-003, spec deployment-intent.md §5.1).
 */

import { Link } from 'react-router-dom';
import { getIntentOwnership } from '@/lib/utils';

export function IntentManagedBadge({ instance }: { instance: any }) {
  const { managed, intentId } = getIntentOwnership(instance);
  if (!managed || !intentId) return null;

  return (
    <Link
      to={`/intents/${intentId}`}
      title="Managed by an intent — the reconciler owns this instance"
      className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-nord-15/20 text-nord-15 hover:underline whitespace-nowrap"
    >
      intent-managed
    </Link>
  );
}
