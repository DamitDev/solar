<script lang="ts">
  import { onDestroy } from 'svelte';
  import { fetchJson } from '../lib/api/client';
  import ComponentRow from '../lib/components/ComponentRow.svelte';
  import MessageItem from '../lib/components/MessageItem.svelte';
  import { hasComponents, splitMessages, type StatusSummary } from '../lib/status/types';

  type Load =
    { kind: 'loading' } | { kind: 'ready'; summary: StatusSummary } | { kind: 'unreachable' };

  // "Current" availability must not go stale on a long-open tab.
  const REFRESH_INTERVAL_MS = 60_000;

  let load = $state<Load>({ kind: 'loading' });
  let cancelled = false;
  let timer: ReturnType<typeof setInterval> | undefined;
  // The interval and the visibility handler can overlap, so a slow older
  // response must never overwrite a newer summary that already landed.
  let latestRequestId = 0;

  async function refresh(): Promise<void> {
    const requestId = ++latestRequestId;
    try {
      const summary = await fetchJson<StatusSummary>('status');
      if (!cancelled && requestId === latestRequestId) load = { kind: 'ready', summary };
    } catch {
      if (!cancelled && requestId === latestRequestId) load = { kind: 'unreachable' };
    }
  }

  function refreshWhenVisible(): void {
    if (document.visibilityState === 'visible') void refresh();
  }

  $effect(() => {
    void refresh();
  });

  timer = setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
  document.addEventListener('visibilitychange', refreshWhenVisible);

  onDestroy(() => {
    cancelled = true;
    clearInterval(timer);
    document.removeEventListener('visibilitychange', refreshWhenVisible);
  });
</script>

<main class="min-h-screen bg-zinc-950 text-zinc-100">
  <div class="mx-auto max-w-5xl px-4 py-10">
    <h1 class="text-3xl font-bold tracking-tight">Solar status</h1>

    {#if load.kind === 'loading'}
      <p class="mt-8 text-zinc-400" role="status">Loading status…</p>
    {:else if load.kind === 'unreachable'}
      <p class="mt-8 rounded border border-red-900 bg-red-950/40 p-4 text-red-300" role="alert">
        The status API is unreachable. Please try again later.
      </p>
    {:else}
      {@const summary = load.summary}
      {@const messages = splitMessages(summary)}

      {#if messages.active.length === 0 && messages.upcoming.length === 0}
        <p class="mt-8 text-zinc-400" data-testid="messages-empty">
          No active incidents or upcoming maintenance.
        </p>
      {/if}

      {#if messages.active.length > 0}
        <section class="mt-8" aria-labelledby="messages-heading">
          <h2 id="messages-heading" class="text-lg font-semibold">Messages</h2>
          <ul class="mt-2 divide-y divide-zinc-800">
            {#each messages.active as message (message.id)}
              <MessageItem {message} />
            {/each}
          </ul>
        </section>
      {/if}

      {#if messages.upcoming.length > 0}
        <section class="mt-8" aria-labelledby="upcoming-heading">
          <h2 id="upcoming-heading" class="text-lg font-semibold">Upcoming maintenance</h2>
          <ul class="mt-2 divide-y divide-zinc-800">
            {#each messages.upcoming as message (message.id)}
              <MessageItem {message} />
            {/each}
          </ul>
        </section>
      {/if}

      {#if !hasComponents(summary)}
        <p class="mt-8 text-zinc-400">No components are reported yet.</p>
      {/if}

      {#if summary.virtual_models.length > 0}
        <!-- Product naming: virtual names (core, wind, ...) are "Aliases"; the API
             field stays `virtual_models`. -->
        <section class="mt-8" aria-labelledby="aliases-heading">
          <h2 id="aliases-heading" class="text-lg font-semibold">Aliases</h2>
          <ul class="mt-2 grid gap-x-8 md:grid-cols-2">
            {#each summary.virtual_models as model (model.name)}
              <ComponentRow
                name={model.name}
                detail={model.targets.length > 0
                  ? `Targets: ${model.targets.join(' → ')}`
                  : undefined}
                windowDays={summary.window_days}
                status={model}
              />
            {/each}
          </ul>
        </section>
      {/if}

      {#if summary.aliases.length > 0}
        <!-- Product naming: concrete names (deepseek-v4.1-flash-1017:552b, ...) are
             "Models"; the API field stays `aliases`. -->
        <section class="mt-8" aria-labelledby="models-heading">
          <h2 id="models-heading" class="text-lg font-semibold">Models</h2>
          <ul class="mt-2 grid gap-x-8 md:grid-cols-2">
            {#each summary.aliases as alias (alias.alias)}
              <ComponentRow name={alias.alias} windowDays={summary.window_days} status={alias} />
            {/each}
          </ul>
        </section>
      {/if}

      {#if summary.instances.length > 0}
        <section class="mt-8" aria-labelledby="instances-heading">
          <h2 id="instances-heading" class="text-lg font-semibold">Instances</h2>
          <ul class="mt-2 grid gap-x-8 md:grid-cols-2">
            {#each summary.instances as instance (instance.instance_id)}
              <ComponentRow
                name={instance.instance_id}
                detail={`Alias: ${instance.alias}`}
                windowDays={summary.window_days}
                status={instance}
              />
            {/each}
          </ul>
        </section>
      {/if}
    {/if}
  </div>
</main>
