<script lang="ts">
  import { renderMarkdown } from '../status/markdown';
  import { formatWindow, type StatusMessage } from '../status/types';
  import MessageBadge from './MessageBadge.svelte';

  let { message }: { message: StatusMessage } = $props();

  // Sanitized by renderMarkdown (marked + DOMPurify allow-list).
  const html = $derived(renderMarkdown(message.body));
</script>

<li class="py-4" data-testid="message-item">
  <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
    <MessageBadge type={message.type} />
    <span class="text-xs text-zinc-500">{formatWindow(message)}</span>
  </div>
  <!-- Typography adds literal backticks around inline code via ::before/::after; drop them. -->
  <div
    class="prose mt-2 max-w-none prose-invert prose-code:before:content-none prose-code:after:content-none"
    data-testid="message-body"
  >
    <!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized by renderMarkdown -->
    {@html html}
  </div>
</li>
