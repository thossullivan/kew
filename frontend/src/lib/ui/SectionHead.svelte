<script lang="ts">
  /**
   * FUI-05 — "LIVE RUNS … 2 working": an 11px uppercase label with
   * right-aligned meta (DMonitor y ≈294).
   */
  import type { Snippet } from 'svelte';
  import { sectionHeadClasses } from '$lib/factory/classes';

  let {
    label,
    meta = '',
    note = '',
    as = 'h2',
    class: klass = '',
    children
  }: {
    label: string;
    /** The quiet right-hand count. Sentence case — only the label shouts. */
    meta?: string;
    /** A right-hand caption that is a sentence, in data-13 (#2787 D4). */
    note?: string;
    as?: 'h1' | 'h2' | 'h3';
    class?: string;
    /** Replaces `meta` when the right-hand side needs marks, not just words. */
    children?: Snippet;
  } = $props();

  const parts = $derived(sectionHeadClasses(klass));
</script>

<div class={parts.root}>
  <svelte:element this={as} class={parts.label}>{label}</svelte:element>
  {#if children}
    <span class={parts.meta}>{@render children()}</span>
  {:else if note}
    <span class={parts.note}>{note}</span>
  {:else if meta}
    <span class={parts.meta}>{meta}</span>
  {/if}
</div>
