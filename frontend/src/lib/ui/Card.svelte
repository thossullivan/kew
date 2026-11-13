<script lang="ts">
  import type { Snippet } from 'svelte';
  import { cn } from '$lib/utils';

  let {
    eyebrow = '',
    title = '',
    accent = false,
    pad = true,
    class: klass = '',
    header,
    children,
    ...rest
  }: {
    eyebrow?: string;
    title?: string;
    accent?: boolean;
    pad?: boolean;
    class?: string;
    header?: Snippet;
    children: Snippet;
    [key: string]: unknown;
  } = $props();
</script>

<section
  class={cn(
    'relative overflow-hidden rounded-[var(--radius-lg)] border bg-surface',
    'shadow-[var(--shadow)]',
    accent && 'before:absolute before:inset-x-0 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-accent before:to-transparent',
    pad && 'p-5',
    klass
  )}
  {...rest}
>
  {#if eyebrow || title || header}
    <div class={cn('flex items-baseline justify-between gap-3', pad ? 'mb-4' : 'p-5 pb-0')}>
      <div class="min-w-0">
        {#if eyebrow}<div class="eyebrow">{eyebrow}</div>{/if}
        {#if title}<h2 class="mt-1 truncate text-lg text-fg">{title}</h2>{/if}
      </div>
      {#if header}<div class="shrink-0">{@render header()}</div>{/if}
    </div>
  {/if}
  {@render children()}
</section>
