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

  // Centre the tooltip under the bar, but anchor it to the container edge for the
  // outermost bars so it is not clipped by the viewport.
  const EDGE_BARS = 4;

  function edge(index: number): string {
    if (index < EDGE_BARS) return 'left-0';
    if (index >= history.length - EDGE_BARS) return 'right-0';
    return 'left-1/2 -translate-x-1/2';
  }

  function describe(point: UptimePoint): string {
    const day = point.ts.slice(0, 10);
    return point.samples === 0
      ? `${day}: no data (outage)`
      : `${day}: ${(point.uptime * 100).toFixed(1)}% up`;
  }
</script>

<div class="flex h-8 items-stretch gap-px" role="img" aria-label="Uptime, one bar per day">
  {#each history as point, i (point.ts)}
    <!-- The tooltip is positioned relative to the bar (not the cursor) and shows without delay. -->
    <div
      class="group relative min-w-px flex-1 rounded-[1px] {colors[barTone(point)]}"
      data-testid="uptime-day"
      data-tone={barTone(point)}
    >
      <span
        role="tooltip"
        class="pointer-events-none absolute top-full z-10 mt-1 hidden rounded bg-zinc-800 px-4 py-2 text-xs whitespace-nowrap text-zinc-100 shadow-lg ring-1 ring-zinc-700 group-hover:block {edge(
          i
        )}"
      >
        {describe(point)}
      </span>
    </div>
  {/each}
</div>
