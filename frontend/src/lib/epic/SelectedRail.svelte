<script lang="ts">
  /**
   * FUI-17 — the epic page's selected-node rail (DEpic x 40–1400, y ≈742–960):
   * four hairline-separated columns on the raised ink, replacing the old node
   * detail Card.
   *
   * A renderer, like `Pipeline` and `Holds`. Every word, dash, reason and link
   * below is built by `$lib/epic/rail`, where a vitest holds the columns to
   * their degradation rules; the reads themselves belong to the page, which
   * owns the selection and therefore owns the fetches made for it. Nothing in
   * this file decides a colour: the marks are `StatusMark`'s over
   * `$lib/factory/status`, the swimlane is FUI-07's at its 12px compact size,
   * and the Needs-you column is the shared needs-you block (#3051), whose
   * magenta is spent on its left edge and the button that clears the wait.
   *
   * The columns are 300 / 1fr / 280 / 260 as the artboard draws them, minus
   * the "Needs you" column when nobody is waiting — a hidden column is the
   * rail's way of saying "nothing here", and the grid closes over it rather
   * than leaving 260px of ruled emptiness.
   *
   * Two readers the artboard does not draw (#2333):
   *
   * * Any window narrower than the artboard. Three of those four widths are
   *   fixed — 840px of track — so a viewport that cannot hold them squeezed
   *   Pipeline, Evidence and Needs you into what was left and drew their words
   *   on top of each other: at 390px first, and (the first fix for this folded
   *   at `sm` and stopped there) everywhere up to ~1230px after. The rail
   *   therefore has three tiers, the numbers for all of them in
   *   `RAIL_TRACKS`: ONE column below `lg`, the artboard's four from `xl`, and
   *   2×2 in between. The widths travel as a custom property rather than as a
   *   `grid-template-columns` no media query could reach; each column carries
   *   what it does when it is a row instead (`STACKED`), and the 2×2 tier's
   *   rules are positional, in the stylesheet, because which sections exist is
   *   the model's business.
   * * A short window. The rail is a fixed band under the page's own scroller
   *   (#2278 C4), so on a 600px-tall window it took the bottom third and left
   *   the canvas a slot of graph. Under `SHORT_VIEWPORT` it collapses to the
   *   one line that says WHICH node is selected, with a disclosure that opens
   *   the four sections again; the canvas keeps the height until the reader
   *   asks for the detail. The reading is the window's height, not a pointer
   *   type: a 1440x600 desktop has the same problem a phone does.
   */
  import { Command, StatusMark, Swimlane, Title } from '$lib/ui';
  import NeedsYouBlockView from '$lib/needs-you/NeedsYouBlock.svelte';
  import { plainTitle } from '$lib/factory/names';
  import { PIPELINE_VOCABULARY } from '$lib/work/pipeline';
  import { railModel, RAIL_TRACKS, SHORT_VIEWPORT, type RailInput } from './rail';

  let { input, class: klass = '' }: { input: RailInput; class?: string } = $props();

  const model = $derived(railModel(input));
  /** The artboard's four widths — the last one only when it is drawn. */
  const cols = $derived(
    `${RAIL_TRACKS.selected}px minmax(0, 1fr)` +
      (model.replay ? '' : ` ${RAIL_TRACKS.evidence}px`) +
      (model.attention ? ` ${RAIL_TRACKS.attention}px` : '')
  );

  /**
   * The window's height, bound rather than measured: `0` until the browser
   * says otherwise, which is what a server render and the first frame report.
   * A rail that collapsed on that `0` would flash shut on every load.
   */
  let viewportH = $state(0);
  let expanded = $state(false);
  /** The block's Copy, said for a moment after it lands, and for one node. */
  let copied = $state(false);
  let copyFlash: ReturnType<typeof setTimeout> | null = null;
  let lastRef: string | null = null;
  $effect(() => {
    const ref = model.selected?.ref ?? null;
    if (ref === lastRef) return;
    lastRef = ref;
    copied = false;
  });

  async function copyCommand(): Promise<void> {
    const command = model.attention?.block?.command ?? '';
    if (command === '') return;
    try {
      await navigator.clipboard.writeText(command);
    } catch {
      // Clipboard unreachable: the command is on screen as selectable text.
      return;
    }
    copied = true;
    if (copyFlash !== null) clearTimeout(copyFlash);
    copyFlash = setTimeout(() => {
      copied = false;
      copyFlash = null;
    }, 1500);
  }
  const short = $derived(viewportH > 0 && viewportH < SHORT_VIEWPORT);
  const collapsed = $derived(short && !expanded);
  let viewportW = $state(0);
  /** The artboard's four columns, in a window tall enough to hold them beside
   * the canvas: the rail scrolls inside itself there (D35). Read off the
   * window rather than a variant class, so the one fold the markup carries is
   * still the stacking one (`rail-fold.test.ts`). */
  const fits = $derived(Boolean(model.selected) && !short && viewportW >= RAIL_TRACKS.fourUp);

  /**
   * What a column does when the grid stacks it below `lg`: the rule it carries
   * on its left at desktop widths becomes the rule above it, and the gutter
   * that rule sat in comes off — a full-width section indented 28px from a page
   * that is only 390px wide reads as a mistake.
   */
  const STACKED = 'max-lg:border-l-0 max-lg:border-t max-lg:px-0 max-lg:pt-4';

  /** Same pill as the canvas's Graph / Timeline chips, grown to the 32px a
   *  phone is tapped by below `sm` (D25, `TAP_PILL`). */
  const DISCLOSURE =
    'inline-flex min-h-8 shrink-0 cursor-pointer items-center rounded-full border ' +
    'border-hair-strong px-2.5 font-mono text-[11px] text-text-3 hover:text-text-1 sm:min-h-[22px]';

  /**
   * The lane column appears only when there is more than one lane to tell
   * apart. kew serves exactly one (`main`, named in the heading), and the
   * artboard's compact swimlane has no lane column at all.
   */
  const laneLabels = $derived((model.pipeline?.lanes.length ?? 0) > 1);
</script>

<svelte:window bind:innerHeight={viewportH} bind:innerWidth={viewportW} />

<!-- `data-rail-scroll` (D35, #3469): with all four columns drawn in a window
     that is not short, the rail is sized by its content and capped at 218px,
     the artboard's band (DEpic y 742-960); above the cap it scrolls inside
     itself, and the canvas takes the rest of the window. At 1440x900 the
     NEEDS YOU column outgrew the 196px band and its command and links were
     below the window's edge, reachable only by scrolling the canvas away. The
     block puts the command first in that column (`NeedsYouBlock`'s
     `compact`), so it is on screen at the rail's top. Where the canvas is at
     its floor the rail also gives way under its content, down to 140px (the
     NEEDS YOU column's head and the first line of its command), so a window
     900px tall or more scrolls no page: go-live at 1440x900 leaves the rail
     146px. A shorter window scrolls the page the difference (1280x800 leaves
     ~45px), rather than the rail shrinking to its headings. Folded below
     `xl`, the page's scroller keeps the height instead (DESIGN.md Responsive
     Behavior), and the 196px band is a floor. -->
<section
  aria-label={model.selected ? `Selected node ${model.selected.ref}` : 'Selected node'}
  data-rail-scroll={fits ? '' : undefined}
  class={`border-t border-hair bg-ink-raised px-5 sm:px-10 ${
    model.selected && !collapsed ? 'py-5' : 'py-3'
  } ${
    fits
      ? 'max-h-[218px] min-h-[140px] shrink overflow-y-auto overscroll-contain'
      : `shrink-0 ${model.selected && !collapsed ? 'min-h-[196px]' : ''}`
  } ${klass}`}
>
  {#if !model.selected}
    <!-- Nothing selected, which is now only true once the reader has cleared
         the opening selection the page makes for them (D20). An empty rail
         collapses to ITS ONE LINE rather than holding 196px of ruled
         emptiness open for a sentence: the canvas above gets the height. -->
    <p class="text-13 text-text-3">{model.hint}</p>
  {:else}
    {#if short}
      <!-- The one line a short window keeps: which node is selected, and the
           way back to the rest of it. It stays above the sections when they
           are open, so the control that closed them is never below the fold
           the page had to grow to show them. -->
      <div class={`flex items-center gap-3 ${expanded ? 'mb-4' : ''}`}>
        <span class="shrink-0 text-11 text-text-3">Selected</span>
        <span class="shrink-0 font-mono text-13 text-text-1">{model.selected.ref}</span>
        <span class="min-w-0 flex-1 truncate text-13 text-text-1" title={plainTitle(model.selected.title)}
          ><Title text={model.selected.title} /></span
        >
        <StatusMark kind={model.selected.status} size={10} bg="var(--ink-raised)" />
        {#if model.attention}
          <StatusMark kind="needs_you" size={10} bg="var(--ink-raised)" label="needs you" />
        {/if}
        <button
          type="button"
          class={DISCLOSURE}
          aria-expanded={expanded}
          aria-controls="selected-rail-detail"
          onclick={() => (expanded = !expanded)}>{expanded ? 'Hide detail' : 'Detail'}</button
        >
      </div>
    {/if}
    {#if !collapsed}
      <div
        id="selected-rail-detail"
        class="rail-grid grid gap-0 max-lg:gap-4"
        style:--rail-cols={cols}
      >
        <!-- Selected (DEpic x 40–340) -->
        <div class="flex min-w-0 flex-col gap-2 pr-7 max-lg:pr-0">
          <span class="text-11 text-text-3">Selected</span>
          <span class="flex min-w-0 items-baseline gap-2.5">
            <span class="shrink-0 font-mono text-15 text-text-1">{model.selected.ref}</span>
            <span class="truncate text-15 font-medium text-text-1" title={plainTitle(model.selected.title)}
              ><Title text={model.selected.title} /></span
            >
          </span>
          <span class="flex flex-wrap items-center gap-2 font-mono text-13 text-text-2">
            <StatusMark kind={model.selected.status} size={10} bg="var(--ink-raised)" word />
            <span class="text-text-5">·</span>
            <span title={model.selected.containment.reason || 'the route’s declared containment'}
              >{model.selected.containment.value}</span
            >
            <span class="text-text-5">·</span>
            <!-- The node's LIFETIME issue spend, which is the figure the node
                 pane printed and the one DEpic draws. -->
            <span
              class="text-warn"
              title={model.selected.spend.reason || 'lifetime issue spend for this node'}
              >{model.selected.spend.value}</span
            >
          </span>
          {#if model.selected.relations}
            <span class="truncate font-mono text-13 text-text-3" title={model.selected.relations}
              >{model.selected.relations}</span
            >
          {/if}

          {#if model.selected.retry}
            <!-- Kept from the node pane: a failed node's one recorded next move. -->
            <div class="flex flex-col gap-1">
              <span class="text-13 text-danger">{model.selected.retryReason}</span>
              <Command
                command={model.selected.retry}
                class="block rounded-[4px] bg-ink-selected px-2 py-1 font-mono text-13 text-text-1"
              />
            </div>
          {/if}

          <a
            href={model.selected.workHref}
            class="mt-auto flex h-11 items-center gap-2 font-mono text-13 text-text-1 hover:text-accent-2"
            >{model.selected.workLabel} <span aria-hidden="true">→</span></a
          >
        </div>

        {#if model.replay}
          <!-- Design review c9 D13: a replayed frame has no pipeline and no
               evidence of its own, so the two columns give way to one
               sentence, in sans, said once — not the same mono line under
               each heading and a bare dash beside one of them. The grid
               closes over the Evidence track. -->
          <div class={`flex min-w-0 flex-col justify-center border-l border-hair px-7 ${STACKED}`}>
            <p data-rail-replay class="m-0 max-w-[60ch] text-13 text-text-3">{model.replay}</p>
          </div>
        {/if}

        <!-- Pipeline · main (DEpic x 368–832): FUI-11's projection, compacted -->
        {#if model.pipeline && !model.replay}
          <!-- `overflow-hidden`: a lane with more stages than the four
               `PIPELINE_STAGES` composes can still want more track than any
               tier gives it, and a word cut off at the column's own rule is a
               loss the reader can see — an overprinted neighbour is not. -->
          <div
            class={`flex min-w-0 flex-col gap-[14px] overflow-hidden border-l border-hair px-7 ${STACKED}`}
          >
            <span class="flex items-baseline">
              <span class="text-11 text-text-2">{model.pipeline.heading}</span>
            </span>
            {#if model.pipeline.placeholder}
              <span class="font-mono text-13 text-text-3"
                >{model.pipeline.placeholder} <span class="ml-2">{model.pipeline.reason}</span></span
              >
            {:else}
              <Swimlane
                stages={model.pipeline.stages}
                lanes={model.pipeline.lanes}
                size={12}
                vocabulary={PIPELINE_VOCABULARY}
                bg="var(--ink-raised)"
                {laneLabels}
                label="Pipeline stages"
              />
              {#if model.pipeline.notice}
                <p role="status" class="flex items-start gap-2 text-13 text-text-3">
                  <StatusMark
                    kind="unverified"
                    size={7}
                    bg="var(--ink-raised)"
                    label="unreadable"
                    class="mt-1 shrink-0"
                  />
                  <span>{model.pipeline.notice}</span>
                </p>
              {/if}
            {/if}
          </div>
        {/if}

        <!-- Evidence (DEpic x 860–1112): FUI-12's chain, four rows at 24px -->
        {#if model.evidence && !model.replay}
          <div class={`flex min-w-0 flex-col gap-2 border-l border-hair px-7 ${STACKED}`}>
            <span class="flex items-baseline">
              <span class="text-11 text-text-2">Evidence</span>
              <!-- The artboard greens the numerator alone: "3 / 4 attested"
                   counts what kew vouched for, not what it recorded. -->
              <span class="ml-auto font-mono text-13 text-text-3"
                >{#if model.evidence.rows.length}<span class="text-success"
                    >{model.evidence.attested}</span
                  > / {model.evidence.total} attested{:else}{model.evidence.count}{/if}</span
              >
            </span>
            {#if model.evidence.rows.length}
              <ol class="m-0 list-none p-0">
                {#each model.evidence.rows as row (row.key)}
                  <li
                    class="grid h-6 grid-cols-[26px_minmax(0,1fr)_70px_14px] items-center font-mono text-13"
                  >
                    <span class="text-text-3">{row.n}</span>
                    <span class="truncate text-text-2" title={row.label}>{row.label}</span>
                    <span class="text-right text-text-3" title={row.rightTitle}>{row.right}</span>
                    <span class="justify-self-end">
                      <StatusMark
                        kind={row.state}
                        size={10}
                        bg="var(--ink-raised)"
                        label={row.state}
                      />
                    </span>
                  </li>
                {/each}
              </ol>
            {:else}
              <span class="font-mono text-13 text-text-3">{model.evidence.reason}</span>
            {/if}
            {#if model.evidence.notice}
              <p class="text-13 text-text-3">{model.evidence.notice}</p>
            {/if}
          </div>
        {/if}

        <!-- Needs you (DEpic x 1140–1400). Absent when nobody is waiting. -->
        {#if model.attention}
          <div class={`flex min-w-0 flex-col gap-2 border-l border-hair pl-7 ${STACKED}`}>
            {#if model.attention.block}
              <!-- #3051 — the shared needs-you block: the kind, the server's
                   sentence, and "Do this" with the computed command — the line
                   Monitor prints for the same wait, never a bare "Held why ▸". -->
              <NeedsYouBlockView
                block={model.attention.block}
                compact
                itemId={input.node?.id ?? null}
                {copied}
                oncopy={copyCommand}
              />
            {:else}
              <span class="flex items-center gap-2.5 text-11 text-accent">
                <StatusMark kind="needs_you" size={10} bg="var(--ink-raised)" label="needs you" />
                Needs you
              </span>
              <!-- The join failed: the column cannot say "clear", so it says why. -->
              <p role="status" class="text-13 text-text-3">{model.attention.notice}</p>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  {/if}
</section>

<style>
  /* The artboard's own tracks, handed in by the component. A media query
     cannot reach an inline `grid-template-columns`, and the folds below are
     exactly media queries — so the widths travel as a property and the rules
     that replace them live here. The two breakpoints are `RAIL_TRACKS`'s
     `fourUp` and `twoUp` in rem: Tailwind's `xl` and `lg`, the second of which
     is what the column children's `max-lg:` classes fold on. Narrowest query
     last — these have the same specificity, so the order IS the tiers. */
  .rail-grid {
    grid-template-columns: var(--rail-cols);
  }

  /* 2×2 (`lg` to `xl`): two equal tracks, which at 1024px is 472px of column
     each — more than the 320px the Pipeline track needs, where four tracks
     leave it 104px.

     The rules are positional rather than per-column, because WHICH sections
     are drawn is the model's business: a pipeline read that 404s costs its
     own column and moves every section after it into a different cell. So the
     first column of a row carries no left rule (there is nothing left of it),
     and every row after the first carries a rule above it instead. */
  @media (width >= 64rem) and (width < 80rem) {
    .rail-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      row-gap: 1rem;
    }

    .rail-grid > :nth-child(odd) {
      border-left-width: 0;
      padding-left: 0;
    }

    .rail-grid > :nth-child(n + 3) {
      border-top: 1px solid var(--hair);
      padding-top: 1rem;
    }
  }

  @media (width < 64rem) {
    .rail-grid {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
