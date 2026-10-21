<script lang="ts">
  import { barTone, type BarTone, type UptimePoint } from '../status/types';

  let { history }: { history: UptimePoint[] } = $props();

  const colors: Record<BarTone, string> = {
    up: 'bg-emerald-500',
    partial: 'bg-amber-500',
    down: 'bg-red-500',
    // Days without samples are outages: failure-colored, never filled in.
    gap: 'bg-red-900'
  };

  function describe(point: UptimePoint): string {
    const day = point.ts.slice(0, 10);
    return point.samples === 0
      ? `${day}: no data (outage)`
      : `${day}: ${(point.uptime * 100).toFixed(1)}% up`;
  }
</script>

<div class="flex h-8 items-stretch gap-px" role="img" aria-label="Uptime, one bar per day">
  {#each history as point (point.ts)}
    <div
      class="min-w-px flex-1 rounded-[1px] {colors[barTone(point)]}"
      data-testid="uptime-day"
      data-tone={barTone(point)}
      title={describe(point)}
    ></div>
  {/each}
</div>
