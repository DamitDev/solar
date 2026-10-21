<script lang="ts">
  import { barTone, type BarTone, type UptimePoint } from '../status/types';

  let { history }: { history: UptimePoint[] } = $props();

  const colors: Record<BarTone, string> = {
    up: 'bg-emerald-500',
    partial: 'bg-amber-500',
    down: 'bg-red-500',
    // Days without samples after the first sample are outages:
    // failure-colored, never filled in.
    gap: 'bg-red-900',
    // Days before the first sample predate the component: neutral, not an
    // outage (the backend excludes them from the uptime percentage too).
    empty: 'bg-zinc-800'
  };

  // Centre the tooltip under the bar, but anchor it to the container edge for the
  // outermost bars so it is not clipped by the viewport.
  const EDGE_BARS = 4;

  function edge(index: number): string {
    if (index < EDGE_BARS) return 'left-0';
    if (index >= history.length - EDGE_BARS) return 'right-0';
    return 'left-1/2 -translate-x-1/2';
  }

  function describe(point: UptimePoint, tone: BarTone): string {
    const day = point.ts.slice(0, 10);
    if (tone === 'empty') return `${day}: no data yet`;
    if (tone === 'gap') return `${day}: no data (outage)`;
    return `${day}: ${(point.uptime * 100).toFixed(1)}% up`;
  }
</script>

<div class="flex h-8 items-stretch gap-px" role="img" aria-label="Uptime, one bar per day">
  {#each history as point, i (point.ts)}
    {@const tone = barTone(history, i)}
    <!-- The tooltip is positioned relative to the bar (not the cursor) and shows without delay. -->
    <div
      class="group relative min-w-px flex-1 rounded-[1px] {colors[tone]}"
      data-testid="uptime-day"
      data-tone={tone}
    >
      <span
        role="tooltip"
        class="pointer-events-none absolute top-full z-10 mt-1 hidden rounded bg-zinc-800 px-4 py-2 text-xs whitespace-nowrap text-zinc-100 shadow-lg ring-1 ring-zinc-700 group-hover:block {edge(
          i
        )}"
      >
        {describe(point, tone)}
      </span>
    </div>
  {/each}
</div>
