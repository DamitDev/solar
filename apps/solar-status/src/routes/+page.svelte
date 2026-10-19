<script lang="ts">
  import { apiBase, createApiUrl } from '../lib/api/base';
  import { ApiError, fetchApi } from '../lib/api/client';

  type Probe =
    | { kind: 'pending' }
    | { kind: 'responded'; status: number }
    | { kind: 'unreachable'; reason: string };

  let probe = $state<Probe>({ kind: 'pending' });

  const statusUrl = createApiUrl('status');

  $effect(() => {
    let cancelled = false;

    fetchApi('status').then(
      (response) => {
        if (!cancelled) probe = { kind: 'responded', status: response.status };
      },
      (error: unknown) => {
        if (cancelled) return;
        probe =
          error instanceof ApiError
            ? { kind: 'responded', status: error.status }
            : {
                kind: 'unreachable',
                reason: error instanceof Error ? error.message : String(error)
              };
      }
    );

    return () => {
      cancelled = true;
    };
  });
</script>

<main class="flex min-h-screen items-center justify-center bg-zinc-950 text-zinc-100">
  <section class="text-center">
    <h1 class="text-3xl font-bold tracking-tight">Solar status</h1>
    <p class="mt-2 text-zinc-400">Status page foundation</p>
    <p class="mt-6 font-mono text-sm text-zinc-500">API base: {apiBase.href}</p>
    {#if probe.kind === 'pending'}
      <p class="mt-1 font-mono text-sm text-zinc-500">Calling {statusUrl.href}…</p>
    {:else if probe.kind === 'responded'}
      <p class="mt-1 font-mono text-sm text-zinc-500">
        GET {statusUrl.pathname} → HTTP {probe.status}
      </p>
    {:else}
      <p class="mt-1 font-mono text-sm text-red-400">API unreachable: {probe.reason}</p>
    {/if}
  </section>
</main>
