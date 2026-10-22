<script lang="ts">
  import type { Availability } from '../status/types';

  let { availability }: { availability: Availability } = $props();

  const tones: Record<Availability, { label: string; dot: string; text: string }> = {
    operational: { label: 'Operational', dot: 'bg-emerald-500', text: 'text-emerald-400' },
    degraded: { label: 'Degraded', dot: 'bg-amber-500', text: 'text-amber-400' },
    down: { label: 'Down', dot: 'bg-red-500', text: 'text-red-400' },
    // A source the backend could not read: neutral, never outage-colored.
    unknown: { label: 'Unknown', dot: 'bg-zinc-500', text: 'text-zinc-400' }
  };

  const tone = $derived(tones[availability]);
</script>

<span
  class="inline-flex items-center gap-2 text-sm font-medium {tone.text}"
  data-availability={availability}
>
  <span class="size-2.5 rounded-full {tone.dot}" aria-hidden="true"></span>
  {tone.label}
</span>
