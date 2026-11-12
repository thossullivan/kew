<script lang="ts">
  /**
   * FUI-07 — the pipeline timeline: lanes of stage cells, each a marker plus
   * the segment that leads to the next cell.
   *
   * Every decision lives in `factory/swimlane` and every mark in `StatusMark`,
   * so this file only places them. Two layouts fall out of the one `size` prop
   * because the artboards draw exactly two: 14px with a lane column and a
   * header row of stage names (DWork, x 468–1392, y ≈336–522), and 12px with
   * the stage name inside each cell and no lane column (the DEpic selected
   * rail, x ≈368–832, y ≈780–840). Either half can be overridden.
   *
   * Neither layout reflows on a phone: a column narrower than its own 11px
   * header prints the next header on top of it, so below `laneMinWidth` the
   * lane keeps the desktop's stage order and dot vocabulary and scrolls inside
   * its own box instead (#2372).
   *
   * There is no component-render test infra in this repo, so the layout is
   * checked by rendering server-side in swimlane.test.ts and reading the inline
   * styles back against the artboards' own numbers.
   */
  import type { Snippet } from 'svelte';
  import { cn } from '$lib/utils';
  import StatusMark from './StatusMark.svelte';
  import type { StatusVocabulary } from '$lib/factory/status';
  import {
    captionTone,
    columnCount,
    laneMinWidth,
    laneView,
    segmentLeft,
    segmentTop,
    stubOffset,
    stubSize,
    SWIMLANE_SIZES,
    type SwimlaneCellView,
    type SwimlaneLane,
    type SwimlaneSize
  } from '$lib/factory/swimlane';

  let {
    stages,
    lanes,
    size = 14,
    vocabulary = 'epic',
    bg = 'var(--ink)',
    laneLabels = size === 14,
    stageHeader = size === 14 ? 'row' : 'cell',
    laneHeading = 'Lane',
    placeholder = '—',
    label = 'Pipeline',
    caption,
    class: klass = ''
  }: {
    /** Column names, in order. A lane may carry fewer cells than these. */
    stages: readonly string[];
    lanes: readonly SwimlaneLane[];
    /** 14px for the Work pipeline, 12px for the compact Epic rail. */
    size?: SwimlaneSize;
    /** Which vocabulary the cell states came from; see `statusShape`. */
    vocabulary?: StatusVocabulary;
    /** What a hollow marker is filled with, so it hides the line it sits on. */
    bg?: string;
    /** Draw the lane-name column. Defaults on at 14px, off at 12px. */
    laneLabels?: boolean;
    /** Stage names in a header row, or above each cell. */
    stageHeader?: 'row' | 'cell';
    /** The lane column's own heading. */
    laneHeading?: string;
    /**
     * What an inactive cell with nothing to say reads as. The artboard writes
     * an em dash in the release lane's empty columns; pass `''` for a blank.
     */
    placeholder?: string;
    /** Accessible name for the whole swimlane. */
    label?: string;
    /** Replaces the built-in two-line caption. */
    caption?: Snippet<[SwimlaneCellView]>;
    class?: string;
  } = $props();

  const g = $derived(SWIMLANE_SIZES[size]);
  const columns = $derived(columnCount(stages, lanes));
  const cols = $derived(
    `${laneLabels ? `${g.laneWidth}px ` : ''}repeat(${columns}, minmax(0, 1fr))`
  );
  /**
   * The floor every band of the grid is drawn to, so a narrow viewport scrolls
   * the lane instead of squeezing the columns under their own labels (#2372).
   * It is below the artboards' own track widths, so desktop never sees it.
   */
  const minWidth = $derived(laneMinWidth(size, columns, laneLabels));
  const rows = $derived(
    lanes.map((lane) => ({ lane, cells: laneView(lane, columns, stages, vocabulary) }))
  );
  /** The header row names every column, including any no stage named. */
  const headers = $derived(Array.from({ length: columns }, (_, i) => stages[i] ?? ''));
  const px = (n: number) => `${n}px`;
</script>

{#snippet marker(v: SwimlaneCellView)}
  <span class="relative block" style:height={px(g.size)}>
    {#if v.segment.width > 0}
      <span
        aria-hidden="true"
        class="absolute right-0 h-0"
        style:left={px(segmentLeft(g.size))}
        style:top={px(segmentTop(g.size, v.segment.width))}
        style:border-top={`${v.segment.width}px ${v.segment.dashed ? 'dashed' : 'solid'} ` +
          v.segment.color}
      ></span>
    {/if}
    {#if v.marker.stub}
      <span
        aria-hidden="true"
        class="absolute rounded-full"
        style:left={px(stubOffset(g.size))}
        style:top={px(stubOffset(g.size))}
        style:width={px(stubSize(g.size))}
        style:height={px(stubSize(g.size))}
        style:background-color={v.marker.mark.color}
      ></span>
    {:else}
      <span class="absolute top-0 left-0">
        <StatusMark
          kind={v.marker.status}
          {vocabulary}
          {bg}
          size={g.size}
          label={v.marker.word}
        />
      </span>
    {/if}
  </span>
{/snippet}

{#snippet twoLine(v: SwimlaneCellView)}
  <span class="font-mono text-13 leading-[1.4]" style:padding-right={px(g.captionGutter)}>
    {#if v.marker.stub && !v.cell.primary && !v.cell.secondary}
      <span class="text-text-3">{placeholder}</span>
    {/if}
    {#if v.cell.primary}
      <span
        data-caption="primary"
        style:color={captionTone(v.marker, 'primary', v.cell.statusLine ?? 'secondary')}
        >{v.cell.primary}</span
      >
    {/if}
    {#if v.cell.secondary}
      {#if v.cell.primary}<br />{/if}
      <span
        data-caption="secondary"
        style:color={captionTone(v.marker, 'secondary', v.cell.statusLine ?? 'secondary')}
        >{v.cell.secondary}</span
      >
    {/if}
  </span>
{/snippet}

<!-- The scroll box, which is the whole phone fix (#2372). Once the viewport is
     narrower than `laneMinWidth`, the lane scrolls inside this element instead
     of overflowing the page — a scroll container's automatic minimum size is
     zero, so a 390px page stays 390px wide. `overscroll-x-contain` keeps a
     sideways swipe off the browser's back gesture. Wider than that, there is
     nothing to scroll and the artboard's grid is untouched. -->
<div
  class={cn('w-full overflow-x-auto overscroll-x-contain', klass)}
  role="group"
  aria-label={label}
>
  {#if stageHeader === 'row'}
    <div
      class="grid border-b border-hair text-11 text-text-3"
      style:grid-template-columns={cols}
      style:min-width={px(minWidth)}
    >
      {#if laneLabels}<div class="pb-[10px]">{laneHeading}</div>{/if}
      {#each headers as head, i (i)}<div class="pb-[10px]">{head}</div>{/each}
    </div>
    <!-- The 6px tick that drops each column's rule into the first lane. -->
    <div
      aria-hidden="true"
      class="grid h-[6px]"
      style:grid-template-columns={cols}
      style:min-width={px(minWidth)}
    >
      {#if laneLabels}<div></div>{/if}
      {#each headers as _, i (i)}<div class="border-l border-hair-strong"></div>{/each}
    </div>
  {/if}

  {#each rows as row, r (r)}
    <div
      class={cn(
        'grid',
        g.rowRule && 'border-b',
        g.rowRule && (r === rows.length - 1 ? 'border-hair' : 'border-hair-quiet')
      )}
      style:grid-template-columns={cols}
      style:min-width={px(minWidth)}
      style:padding-top={px(g.padTop)}
      style:padding-bottom={px(g.padBottom)}
    >
      {#if laneLabels}
        <div class={cn('pt-px font-mono text-13', row.lane.muted ? 'text-text-2' : 'text-text-1')}>
          {row.lane.label}
        </div>
      {/if}
      {#each row.cells as v (v.column)}
        <!-- `data-stage` and `data-caption` name a cell's own words, so a check
             can read the one it means and not its neighbours' (#2916). -->
        <div
          class="flex min-w-0 flex-col"
          style:gap={px(g.gap)}
          data-stage={v.stage ?? undefined}
        >
          {#if stageHeader === 'cell'}
            <!-- Only the first lane names the stages; the rest sit under them. -->
            <span class="text-11 text-text-3">{r === 0 ? (v.stage ?? '') : ''}</span>
          {/if}
          {@render marker(v)}
          {@render (caption ?? twoLine)(v)}
        </div>
      {/each}
    </div>
  {/each}
</div>
