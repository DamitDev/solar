<script lang="ts">
  import { formatUptime, type ComponentStatus } from '../status/types';
  import StatusBadge from './StatusBadge.svelte';
  import UptimeBar from './UptimeBar.svelte';

  let {
    name,
    detail,
    windowDays,
    status
  }: {
    name: string;
    detail?: string;
    windowDays: number;
    status: ComponentStatus;
  } = $props();
</script>

<li class="min-w-0 border-b border-zinc-800 py-4" data-testid="component-row">
  <div class="flex items-center justify-between gap-3">
    <div class="flex min-w-0 flex-1 items-baseline gap-x-3">
      <h3 class="min-w-0 truncate font-medium" title={name}>{name}</h3>
      {#if detail}
        <!-- shrink-100: the detail gives up space long before the name does. -->
        <span class="min-w-0 shrink-100 truncate text-xs text-zinc-500" title={detail}
          >{detail}</span
        >
      {/if}
    </div>
    <StatusBadge availability={status.availability} />
  </div>
  <div class="mt-3">
    <UptimeBar history={status.history} />
    <div class="mt-1 flex justify-between text-xs text-zinc-500">
      <span>{windowDays} days ago</span>
      <span>{formatUptime(status.uptime_pct)}</span>
      <span>Today</span>
    </div>
  </div>
</li>
