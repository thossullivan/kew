<script lang="ts" generics="T">
  // The compact tick strip (#2304): fixed slots, newest at the right edge.
  //
  // Every decision that can be wrong without showing in a screenshot lives in
  // `factory/tick-strip.ts` and is unit-tested there (there is no
  // component-render harness in this repo — see epic/render.ts). This file only
  // turns a slot into a mark: a shape for a tick that did something, a
  // `--text-5` rule for a quiet one, nothing at all for the padding that holds
  // the strip's width open so the strips down a column share an edge.
  //
  // The live tick is the one slot that is cyan and the one slot that grows
  // (DESIGN.md: cyan is the only "live" colour, and it is rationed).
  //
  // No slot is FOCUSABLE, and that is deliberate: the loops roster's row is
  // itself a `<button>`, and interactive content inside a button is not a
  // document. So the strip is one `role="img"` whose label carries the whole
  // reading (`stripSummary`) for anyone who cannot hover, and the popover —
  // in place, no toast and no modal — is the sighted reader's detail.
  import StatusMark from '$lib/ui/StatusMark.svelte';
  import { cn } from '$lib/utils';
  import { CHANGE_COLOR, QUIET_COLOR, stripSummary, type TickSlot } from '$lib/factory/tick-strip';

  let {
    slots,
    size = 7,
    slotPx = 9,
    label,
    popover,
    word,
    class: klass = ''
  }: {
    /** Already padded to its fixed width by `tickSlots`/`iterationSlots`. */
    slots: TickSlot<T>[];
    /** A finished slot's mark size; the live one draws one rung larger. */
    size?: 7 | 10;
    /** The width of one slot, in px. Fixed, so strips align across rows. */
    slotPx?: number;
    /** What the strip as a whole is, before its reading. */
    label: string;
    /** A slot's popover lines. Empty = the word on its own. */
    popover: (item: T) => string[];
    /** The heading line of a slot's popover — its own word, never invented. */
    word: (item: T | null) => string;
    class?: string;
  } = $props();

  let openIndex = $state<number | null>(null);

  const reading = $derived(`${label}: ${stripSummary(slots, (item) => word(item))}`);
</script>

<!--
  A grid and not a flex row: `repeat(n, <slotPx>px)` is what makes slot k of one
  strip sit at the same x as slot k of the strip above it, which is the only
  reason a strip per row is worth drawing. A flex row with `gap` would centre
  the short strips and the column would read as noise.
-->
<div
  role="img"
  aria-label={reading}
  class={cn('grid items-center', klass)}
  style:grid-template-columns={`repeat(${slots.length}, ${slotPx}px)`}
>
  {#each slots as slot, i (i)}
    {@const detail = slot.item === null ? [] : popover(slot.item)}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <span
      class="relative flex h-4 items-center justify-center"
      onmouseenter={() => (openIndex = slot.kind === 'pad' ? null : i)}
      onmouseleave={() => (openIndex = null)}
    >
      {#if slot.kind === 'pad'}
        <!-- Nothing. A tick that never happened is not a tick that did
             nothing, and drawing it as one would invent history. -->
      {:else if slot.kind === 'quiet'}
        <!-- A quiet tick: a `--text-5` rule, no shape. Nine ticks in ten are
             this, and nine marks in ten would be a strip that says nothing. -->
        <span class="h-px w-full" style:background={QUIET_COLOR}></span>
      {:else if slot.kind === 'change'}
        <!-- A change (#2844 D23): a neutral dot, not the done dot's green,
             which means ready, merged or done. Smaller than the slot, so a
             run of changes reads as dots and not as one bar. -->
        <span class="size-[5px] rounded-full" style:background={CHANGE_COLOR}></span>
      {:else}
        <StatusMark kind={slot.meaning} size={slot.live ? 10 : size} />
      {/if}

      {#if openIndex === i}
        <span
          class="pointer-events-none absolute bottom-5 left-1/2 z-20 -translate-x-1/2
                 rounded border border-hair-strong bg-ink-raised px-2 py-1
                 font-mono text-[11px] whitespace-nowrap text-text-2 shadow-lg"
        >
          <span class="text-text-1">{word(slot.item)}</span>
          {#each detail as line (line)}
            <span class="block">{line}</span>
          {/each}
        </span>
      {/if}
    </span>
  {/each}
</div>
