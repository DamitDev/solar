<script lang="ts">
  import { formatWindow, type StatusMessage } from '../status/types';
  import MessageBadge from './MessageBadge.svelte';

  let { message }: { message: StatusMessage } = $props();
</script>

<li class="py-4" data-testid="message-item">
  <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
    <MessageBadge type={message.type} />
    <span class="text-xs text-zinc-500">{formatWindow(message)}</span>
  </div>
  <p class="mt-2 whitespace-pre-line text-zinc-200">{message.body}</p>
</li>
