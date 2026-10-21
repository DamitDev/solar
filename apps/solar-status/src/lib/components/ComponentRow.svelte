<script lang="ts">
  import { formatUptime, type ComponentStatus } from '../status/types';
  import StatusBadge from './StatusBadge.svelte';
  import UptimeBar from './UptimeBar.svelte';

  let {
    name,
    detail,
    phase,
    status
  }: {
    name: string;
    detail?: string;
    phase?: string | null;
    status: ComponentStatus;
  } = $props();
</script>

<li class="border-b border-zinc-800 py-4" data-testid="component-row">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <div class="flex flex-wrap items-baseline gap-x-3">
      <h3 class="font-medium">{name}</h3>
      {#if detail}
        <span class="text-xs text-zinc-500">{detail}</span>
      {/if}
      {#if phase}
        <span class="rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-300">
          Intent: {phase}
        </span>
      {/if}
    </div>
    <StatusBadge availability={status.availability} />
  </div>
  <div class="mt-3">
    <UptimeBar history={status.history} />
    <div class="mt-1 flex justify-between text-xs text-zinc-500">
      <span>{status.history.length} days ago</span>
      <span>{formatUptime(status.uptime_pct)}</span>
      <span>Today</span>
    </div>
  </div>
</li>
