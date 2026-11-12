<script lang="ts">
  /**
   * FUI-05 — an instrument readout: 11px label, 44px figure, 15px caption
   * (DMonitor y ≈88–258). The figure's colour says what the number means, so
   * a cyan 2 is "working" and a magenta 1 is "needs you".
   */
  import type { Snippet } from 'svelte';
  import { readoutClasses, type ReadoutTone } from '$lib/factory/classes';

  let {
    label,
    value = undefined,
    tone = 'neutral',
    meta = '',
    metaShort = '',
    metaTitle = '',
    stackMeta = false,
    caption = '',
    divided = false,
    edge = undefined,
    class: klass = '',
    figure,
    children
  }: {
    label: string;
    /** The figure. Omit it and pass a `figure` snippet for a gauge instead. */
    value?: string | number;
    tone?: ReadoutTone;
    /** A 13px measurement on the label's right, e.g. "71.2% of cap". */
    meta?: string;
    /**
     * `meta` as it reads below 1024, where a readout is half of a 390px screen
     * and the whole reading broke over two lines (#2896 R12): "31.5% cap".
     * Omit it and `meta` is drawn at every width.
     */
    metaShort?: string;
    /**
     * The exact reading behind a bounded one — "×1000000.0 over cap" behind
     * "×99+ over cap" (#2896) — as a title only, never drawn. Defaults to `meta`.
     */
    metaTitle?: string;
    /**
     * Below 640 the meta takes a line of its own under the label. Where a
     * readout is half a 390px screen and carries no `metaShort`, "TODAY" and
     * "71.2% of cap" ran into each other with no gap (#3457).
     */
    stackMeta?: boolean;
    caption?: string;
    /** Draws the hairline that separates this cell from the one on its left. */
    divided?: boolean;
    /** Pulls the outer padding off the first and last cells of a row. */
    edge?: 'start' | 'end';
    class?: string;
    figure?: Snippet;
    /** Anything below the caption, such as a gauge's tick labels. */
    children?: Snippet;
  } = $props();

  const parts = $derived(readoutClasses({ tone, divided, edge, class: klass }));
  const metaClass = $derived(stackMeta ? `${parts.meta} max-sm:ml-0` : parts.meta);
</script>

<div class={parts.root}>
  <div class={stackMeta ? 'flex items-baseline max-sm:flex-col max-sm:gap-1' : 'flex items-baseline'}>
    <span class={parts.label}>{label}</span>
    {#if meta && metaShort}
      <span class={`${metaClass} hidden lg:inline`} title={metaTitle || meta}>{meta}</span>
      <span class={`${metaClass} lg:hidden`} title={metaTitle || meta}>{metaShort}</span>
    {:else if meta}<span class={metaClass} title={metaTitle || undefined}>{meta}</span>{/if}
  </div>
  {#if figure}
    {@render figure()}
  {:else if value !== undefined}
    <div class={parts.figure}>{value}</div>
  {/if}
  {#if caption}<div class={parts.caption}>{caption}</div>{/if}
  {#if children}{@render children()}{/if}
</div>
