<script lang="ts">
  import { fetchJson } from '../lib/api/client';
  import ComponentRow from '../lib/components/ComponentRow.svelte';
  import MessageItem from '../lib/components/MessageItem.svelte';
  import { hasComponents, splitMessages, type StatusSummary } from '../lib/status/types';

  type Load =
    { kind: 'loading' } | { kind: 'ready'; summary: StatusSummary } | { kind: 'unreachable' };

  let load = $state<Load>({ kind: 'loading' });

  $effect(() => {
    let cancelled = false;

    fetchJson<StatusSummary>('status').then(
      (summary) => {
        if (!cancelled) load = { kind: 'ready', summary };
      },
      () => {
        if (!cancelled) load = { kind: 'unreachable' };
      }
    );

    return () => {
      cancelled = true;
    };
  });
</script>

<main class="min-h-screen bg-zinc-950 text-zinc-100">
  <div class="mx-auto max-w-3xl px-4 py-10">
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
        <section class="mt-8" aria-labelledby="models-heading">
          <h2 id="models-heading" class="text-lg font-semibold">Models</h2>
          <ul class="mt-2 divide-y divide-zinc-800">
            {#each summary.virtual_models as model (model.name)}
              <ComponentRow
                name={model.name}
                detail={model.targets.length > 0
                  ? `Targets: ${model.targets.join(' → ')}`
                  : undefined}
                status={model}
              />
            {/each}
          </ul>
        </section>
      {/if}

      {#if summary.aliases.length > 0}
        <section class="mt-8" aria-labelledby="aliases-heading">
          <h2 id="aliases-heading" class="text-lg font-semibold">Aliases</h2>
          <ul class="mt-2 divide-y divide-zinc-800">
            {#each summary.aliases as alias (alias.alias)}
              <ComponentRow
                name={alias.alias}
                detail={`${alias.instances_running}/${alias.instances_total} instances running`}
                phase={alias.intent_phase}
                status={alias}
              />
            {/each}
          </ul>
        </section>
      {/if}

      {#if summary.instances.length > 0}
        <section class="mt-8" aria-labelledby="instances-heading">
          <h2 id="instances-heading" class="text-lg font-semibold">Instances</h2>
          <ul class="mt-2 divide-y divide-zinc-800">
            {#each summary.instances as instance (instance.instance_id)}
              <ComponentRow
                name={instance.instance_id}
                detail={`Alias: ${instance.alias}`}
                status={instance}
              />
            {/each}
          </ul>
        </section>
      {/if}
    {/if}
  </div>
</main>
