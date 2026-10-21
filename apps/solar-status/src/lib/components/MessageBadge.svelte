<script lang="ts">
  import type { MessageType } from '../status/types';

  let { type }: { type: MessageType } = $props();

  const tones: Record<MessageType, { label: string; classes: string }> = {
    incident: { label: 'Incident', classes: 'bg-red-950 text-red-300 border-red-900' },
    maintenance: { label: 'Maintenance', classes: 'bg-amber-950 text-amber-300 border-amber-900' },
    'planned maintenance': {
      label: 'Planned maintenance',
      classes: 'bg-sky-950 text-sky-300 border-sky-900'
    },
    resolution: {
      label: 'Resolution',
      classes: 'bg-emerald-950 text-emerald-300 border-emerald-900'
    }
  };

  const tone = $derived(tones[type]);
</script>

<span
  class="rounded border px-1.5 py-0.5 text-xs font-medium {tone.classes}"
  data-message-type={type}
>
  {tone.label}
</span>
