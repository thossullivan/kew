<script lang="ts">
  // #2786 D16 — a lower panel of the Work detail behind a disclosure that names
  // what is inside it. The description and the evidence ledger both used to be
  // open under the pipeline, so the pane read as three long documents at once.
  // Each starts closed, and its head is the panel's own section head: the
  // label, and the count (`214 words`, `2 / 5 attested`) that says what opening
  // it shows.
  //
  // A button with `aria-expanded`, the way every other disclosure here is
  // written (`Why`, the first-run card's deferred checks), and not a
  // `<details>`: that is the app's MENU primitive, which closes on an outside
  // press (`menus.test.ts`) — and a panel being read must not fold up because
  // the reader clicked the pipeline above it. The head keeps
  // `sectionHeadClasses`, so a closed panel reads as the head the open one has
  // always had.
  import type { Snippet } from 'svelte';
  import { sectionHeadClasses } from '$lib/factory/classes';

  let {
    label,
    meta = '',
    open = $bindable(false),
    head,
    children
  }: {
    label: string;
    /** What is inside, quietly, on the right. Ignored when `head` is given. */
    meta?: string;
    /** Closed on load (#2786). */
    open?: boolean;
    /** Replaces `meta` when the right-hand side needs marks, not just words. */
    head?: Snippet;
    children: Snippet;
  } = $props();

  const parts = sectionHeadClasses('mb-0');
  const id = $props.id();
</script>

<h2 class="m-0 min-w-0">
  <button
    type="button"
    aria-expanded={open}
    aria-controls={id}
    data-panel={label}
    onclick={() => (open = !open)}
    class="{parts.root} min-h-8 w-full cursor-pointer text-left transition-colors
           hover:text-text-1 focus-visible:outline-accent-2"
  >
    <span
      aria-hidden="true"
      class="inline-block w-2 font-mono text-11 text-text-3 transition-transform"
      class:rotate-90={open}>›</span
    >
    <span class={parts.label}>{label}</span>
    {#if head}
      <span class={parts.meta}>{@render head()}</span>
    {:else if meta}
      <span class={parts.meta}>{meta}</span>
    {/if}
  </button>
</h2>
<div {id} hidden={!open} class="mt-3 min-w-0 flex-col gap-3" class:flex={open}>
  {@render children()}
</div>
