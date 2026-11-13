<script lang="ts">
  /**
   * The epic run Gantt (#2271): one row per node, the status behind it, the
   * runs on it, the review rounds that ended each one, and where it merged.
   *
   * It decides no geometry of its own — every x, width and step comes from
   * `$lib/epic/timeline`, which is the module with the tests. What lives here
   * is the canvas's grammar, the same one the DAG has: the wheel zooms about
   * the pointer, a drag pans, `F` fits, a click selects, ↑ and ↓ walk the
   * rows. Esc is the PAGE's (it owns the selection and the popover together).
   */
  import { untrack } from 'svelte';
  import type { EpicTimeline } from '$lib/api';
  import { bindShortcuts, registerShortcuts } from '$lib/keys/registry';
  import { displayId } from './render';
  import { plainTitle } from '$lib/factory/names';
  import Title from '$lib/ui/Title.svelte';
  import {
    BAND_EDGE,
    BAND_EDGE_W,
    BAND_FILL,
    BAR_COLOR,
    BAR_LEGEND,
    BAR_H,
    BAR_TOP,
    BROKEN_STROKE,
    DOT_D,
    LABEL_W,
    MARK_LEGEND,
    ROW_H,
    RULER_H,
    VERDICT_COLOR,
    barHref,
    barLabel,
    barTooltip,
    chartHeight,
    fitTimeline,
    nowLabelX,
    nowOf,
    panBy,
    roundTooltip,
    rowStep,
    rows,
    ticks,
    xOf,
    zoomAt,
    type Scale,
    type TimelineBar,
    type TimelineMark,
    type TimelineRow,
    TIMELINE_KEYS
  } from './timeline';

  type Props = {
    timeline: EpicTimeline;
    selected: string | null;
    onselect: (id: string | null) => void;
  };

  let { timeline, selected, onselect }: Props = $props();

  /** The plot's width — the row minus the label column. Measured, because
   * every x in the model is in pixels of it. */
  let chartW = $state(0);
  /** The scrolling rows' viewport, so a keyboard walk can keep the selected
   * row in it. */
  let bodyH = $state(0);
  /** The rows' scroll offset, tracked so a tooltip anchored to a row lands
   * where the row actually is. (`bind:scrollTop` is not a binding Svelte
   * offers, so it is read off the scroll event.) */
  let scrollTop = $state(0);
  let scale = $state<Scale | null>(null);
  /** Which epic the current scale was fitted for: a 15s refresh must not
   * throw away the window the reader zoomed into, but a different epic must
   * not inherit it either. */
  let fittedFor = $state<string | null>(null);
  let bodyEl = $state<HTMLDivElement | null>(null);
  let rulerEl = $state<HTMLDivElement | null>(null);

  type Tip = { lines: string[]; x: number; y: number };
  let tip = $state<Tip | null>(null);

  /** A drag in progress: where it started, and the scale it started from, so
   * every move is one pan off the same base rather than a sum of deltas. */
  let drag: { clientX: number; base: Scale; moved: boolean } | null = null;
  /** Set by a drag that actually moved, so releasing the pointer over a row
   * pans instead of selecting it. */
  let panned = $state(false);

  const nowMs = $derived(nowOf(timeline));
  const ids = $derived(timeline.nodes.map((n) => n.id));
  const rowList = $derived<TimelineRow[]>(scale ? rows(timeline, scale, nowMs) : []);
  const tickList = $derived(scale ? ticks(scale, nowMs) : []);
  const nowX = $derived(scale ? xOf(scale, nowMs) : 0);
  /** Where the word "now" goes, and whether it is on the canvas at all —
   * the axis's reading of it, which is the box the tick labels were thinned
   * against (#2385). */
  const nowTextX = $derived(scale ? nowLabelX(scale, nowMs) : null);
  const height = $derived(chartHeight(rowList.length));

  /**
   * Fit on the first measurement and on a change of epic; otherwise follow
   * the width and leave the window alone. `untrack` because this effect both
   * reads and writes the scale, and a scale that depended on itself would
   * re-fit on every pan.
   */
  $effect(() => {
    const w = chartW;
    const key = timeline.epic_ref;
    if (w <= 0) return;
    const fit = fitTimeline(timeline, w);
    const current = untrack(() => scale);
    if (current === null || untrack(() => fittedFor) !== key) {
      scale = fit;
      fittedFor = key;
    } else if (current.width !== w) {
      scale = { ...current, width: w };
    }
  });

  /** Keep the selected row in the scrolling viewport — the least scroll that
   * brings it in, so a walk does not re-centre on every step. */
  $effect(() => {
    const at = rowList.findIndex((r) => r.id === selected);
    if (at < 0 || !bodyEl || bodyH <= 0) return;
    const top = at * ROW_H;
    const from = untrack(() => scrollTop);
    if (top < from) bodyEl.scrollTop = top;
    else if (top + ROW_H > from + bodyH) bodyEl.scrollTop = top + ROW_H - bodyH;
  });

  function fit(): void {
    if (chartW > 0) scale = fitTimeline(timeline, chartW);
  }

  /** The pointer's x in plot pixels. The ruler spans exactly the plot, and it
   * never scrolls, so its box is the one to measure against. */
  function plotX(clientX: number): number {
    const box = rulerEl?.getBoundingClientRect();
    return box ? clientX - box.left : 0;
  }

  /**
   * The wheel zooms the time axis about the pointer. A trackpad pinch arrives
   * as a wheel with `ctrlKey`, which is the same move with a coarser gain.
   */
  function onwheel(e: WheelEvent): void {
    if (!scale) return;
    e.preventDefault();
    const gain = e.ctrlKey ? 0.01 : 0.0025;
    scale = zoomAt(scale, Math.exp(-e.deltaY * gain), plotX(e.clientX));
  }

  function onpointerdown(e: PointerEvent): void {
    if (!scale || e.button !== 0) return;
    drag = { clientX: e.clientX, base: scale, moved: false };
    panned = false;
  }

  function onpointermove(e: PointerEvent): void {
    if (drag === null) return;
    // A press released outside the chart before it became a drag (so before
    // capture) never reaches onpointerup here. A later move with no button
    // down is the proof that it ended, so the hover never pans (review r3).
    if ((e.buttons & 1) === 0) {
      drag = null;
      return;
    }
    const dx = e.clientX - drag.clientX;
    if (!drag.moved && Math.abs(dx) < 3) return;
    // Capture only once this is a drag. Captured on pointerdown, the click
    // that follows a plain press is retargeted to this element, so no row's
    // onclick ever ran and a click selected nothing (found in the browser
    // before merge; the reviewer cannot see it).
    if (!drag.moved) (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.moved = true;
    panned = true;
    scale = panBy(drag.base, dx);
  }

  function onpointerup(e: PointerEvent): void {
    if (drag === null) return;
    (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
    drag = null;
  }

  /** A click on a row or a bar selects the node — unless the pointer was
   * dragging the axis, in which case the release is the end of a pan. */
  function pick(id: string): void {
    if (panned) {
      panned = false;
      return;
    }
    onselect(id);
  }

  let tlEl = $state<HTMLDivElement | undefined>(undefined);

  /** A press on the chart — its one tab stop, or anything inside it. The key
   * layer (#2512) has already left modifier presses to the browser. */
  function onkeydown(e: KeyboardEvent): void {
    if (!(e.target instanceof Node) || !tlEl?.contains(e.target)) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const to = rowStep(ids, selected, e.key === 'ArrowDown' ? 1 : -1);
      if (to !== null) onselect(to);
    } else if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      fit();
    }
  }

  // The chart's keys on the console's key layer, while the chart is mounted.
  $effect(() => registerShortcuts(bindShortcuts('timeline', TIMELINE_KEYS, onkeydown)));

  const TIP_W = 230;
  /** Three lines of 11px mono, plus its padding. */
  const TIP_H = 52;

  /**
   * Where a tooltip goes: beside its mark and under its row, clamped into the
   * chart — the canvas clips its own overflow, so a reading that ran off the
   * right-hand edge or past the last row would simply not be there.
   */
  function showTip(lines: string[], row: TimelineRow, x: number): void {
    const left = Math.min(Math.max(0, x + 8), Math.max(0, chartW - TIP_W));
    const below = RULER_H + row.y - scrollTop + ROW_H + 2;
    const flip = below + TIP_H > RULER_H + bodyH;
    tip = {
      lines,
      x: LABEL_W + left,
      y: flip ? Math.max(0, below - ROW_H - TIP_H - 4) : below
    };
  }

  const barTip = (row: TimelineRow, bar: TimelineBar): void =>
    showTip(barTooltip(bar), row, bar.x);
  const markTip = (row: TimelineRow, mark: TimelineMark): void =>
    showTip(roundTooltip(mark), row, mark.x);
</script>

<!-- One tab stop for the whole chart, like the DAG canvas: the rows are not
     themselves focusable (↑ and ↓ move the SELECTION, not the DOM focus), and
     `role="application"` is the honest name for a surface whose arrow keys
     mean something other than scrolling. -->
<!-- Below 640 the chart is not drawn (D19). It fits the epic's whole window
     into the plot, and at 390 that plot is ~150px: every bar was a 2-6px dot
     and nothing said why. A sentence that says so, and where the epic does
     read at this width, is a reading; a row of dots is not. -->
<p data-timeline-narrow class="narrow">
  The timeline needs a wider window; Graph shows this epic at any width.
</p>
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="tl"
  role="application"
  aria-label="Epic run timeline. Drag to pan, the wheel zooms, F fits, arrow keys walk the rows."
  tabindex="0"
  bind:this={tlEl}
  {onwheel}
  {onpointerdown}
  {onpointermove}
  {onpointerup}
  onpointercancel={onpointerup}
  onpointerleave={() => (tip = null)}
>
  <div class="head">
    <div class="corner" style:flex-basis={`${LABEL_W}px`} style:width={`${LABEL_W}px`}>
      <span>node</span>
    </div>
    <div class="ruler" bind:this={rulerEl} bind:clientWidth={chartW}>
      <svg width={chartW} height={RULER_H} aria-hidden="true">
        {#each tickList as t (t.ms)}
          <line
            x1={t.x}
            x2={t.x}
            y1={RULER_H - 5}
            y2={RULER_H}
            stroke={t.day ? 'var(--hair-strong)' : 'var(--hair)'}
          />
          {#if t.label !== ''}
            <text
              x={t.x + 3}
              y={12}
              fill={t.day ? 'var(--text-2)' : 'var(--text-3)'}
              font-size="11">{t.label}</text
            >
          {/if}
        {/each}
        {#if nowTextX !== null}
          <text x={nowTextX} y={RULER_H - 7} fill="var(--accent-2)" font-size="11">now</text>
        {/if}
      </svg>
    </div>
  </div>

  <div
    class="body"
    bind:this={bodyEl}
    bind:clientHeight={bodyH}
    onscroll={(e) => (scrollTop = e.currentTarget.scrollTop)}
  >
    <div class="rows" style:height={`${height}px`}>
      {#each rowList as row (row.id)}
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
        <div
          class="row"
          class:sel={row.id === selected}
          style:height={`${ROW_H}px`}
          onclick={() => pick(row.id)}
        >
          <div
            class="label"
            style:flex-basis={`${LABEL_W}px`}
            style:width={`${LABEL_W}px`}
            title={`${displayId(row.id)} ${plainTitle(row.title)}`}
          >
            <span class="nid">{displayId(row.id)}</span>
            <span class="name"><Title text={row.title} /></span>
          </div>
          <div class="lane">
            <svg width={chartW} height={ROW_H}>
              {#each tickList as t (t.ms)}
                <line
                  x1={t.x}
                  x2={t.x}
                  y1="0"
                  y2={ROW_H}
                  stroke={t.day ? 'var(--hair-strong)' : 'var(--hair-quiet)'}
                />
              {/each}
              {#each row.bands as band (band.fromMs + band.status)}
                <rect x={band.x} y="0" width={band.width} height={ROW_H} fill={BAND_FILL[band.kind]} />
                {#if BAND_EDGE[band.kind]}
                  <rect
                    x={band.x}
                    y="0"
                    width={BAND_EDGE_W}
                    height={ROW_H}
                    fill={BAND_EDGE[band.kind]}
                  />
                {/if}
              {/each}
              {#each row.bars as bar (bar.run.run_id)}
                <!-- #2300 — the bar IS the link to the run page, at its own
                     attempt and focused on the instant it started. A release
                     that ended a pan must not navigate, which is the same rule
                     `pick` holds for selecting a row. -->
                <a
                  href={barHref(bar)}
                  aria-label={barLabel(bar)}
                  onclick={(e) => {
                    if (panned) e.preventDefault();
                  }}
                >
                  <rect
                    x={bar.x}
                    y={BAR_TOP}
                    width={bar.width}
                    height={BAR_H}
                    rx={bar.tick ? 0 : 1}
                    fill={BAR_COLOR[bar.role]}
                    stroke={bar.broken ? BROKEN_STROKE : 'none'}
                    stroke-width={bar.broken ? 1 : 0}
                    onpointerenter={() => barTip(row, bar)}
                    onpointerleave={() => (tip = null)}
                  />
                </a>
              {/each}
              {#each row.marks as mark (mark.key)}
                <circle
                  cx={mark.x}
                  cy={ROW_H / 2}
                  r={DOT_D / 2}
                  fill={VERDICT_COLOR[mark.tone]}
                  onpointerenter={() => markTip(row, mark)}
                  onpointerleave={() => (tip = null)}
                />
              {/each}
              {#if row.merge}
                <circle cx={row.merge.x} cy={ROW_H / 2} r={DOT_D / 2} fill="var(--success)" />
                <text x={row.merge.x + 6} y={ROW_H / 2 + 3} fill="var(--success)" font-size="11"
                  >merged</text
                >
              {/if}
            </svg>
          </div>
        </div>
      {/each}
    </div>

    <!-- The now line runs the height of every row. Decoration only: it
         must never take the click meant for the row under it. -->
    {#if nowX >= 0 && nowX <= chartW}
      <div class="now" style:left={`${LABEL_W + nowX}px`} style:height={`${height}px`}></div>
    {/if}
  </div>

  <!-- The key to the bar colours (#2783, D33): four colours on a chart with
       no key were four guesses. Drawn off `BAR_LEGEND`, which reads
       `BAR_COLOR`, so a swatch here is the colour a bar above really wears. -->
  <div class="legend" data-timeline-legend>
    {#each BAR_LEGEND as entry (entry.role)}
      <span class="key" data-key={entry.role}>
        <span class="swatch" style:background={entry.color} data-key-colors={entry.color} aria-hidden="true"
        ></span>
        {entry.label}
      </span>
    {/each}
    <!-- Everything else the chart draws (#2843 P16): the failed run's red
         outline, the needs-you and in-flight washes, and the verdict dots.
         `data-key-colors` is every colour the swatch stands for, `|`-separated,
         which the ui-check reads against what the lanes really paint. -->
    {#each MARK_LEGEND as entry (entry.key)}
      <span class="key" data-key={entry.key}>
        <span
          class={`swatch ${entry.shape}`}
          style:background={entry.shape === 'outline' ? undefined : entry.color}
          style:border-color={entry.shape === 'outline' ? entry.color : undefined}
          style:box-shadow={entry.edge ? `inset 2px 0 0 0 ${entry.edge}` : undefined}
          data-key-colors={[entry.color, entry.edge].filter((c) => c !== null).join('|')}
          aria-hidden="true"
        ></span>
        {entry.label}
      </span>
    {/each}
  </div>

  {#if timeline.nodes.length === 0}
    <p class="empty">no runs recorded for this epic yet.</p>
  {/if}

  {#if tip}
    <div class="tip" style:left={`${tip.x}px`} style:top={`${tip.y}px`} style:width={`${TIP_W}px`}>
      {#each tip.lines as line, i (i)}
        <span class:first={i === 0}>{line}</span>
      {/each}
    </div>
  {/if}
</div>

<style>
  /* The chart grows into the page's column, exactly as the DAG canvas does,
     with a floor so it can never collapse: a percentage height of an
     indefinite parent is what once rendered the graph at 0px tall. Horizontal
     overflow is clipped here — the time axis is panned and zoomed, never
     scrolled, and the page must not grow a sideways scrollbar. */
  .tl {
    position: relative;
    display: flex;
    flex: 1 1 0;
    min-height: 280px;
    flex-direction: column;
    overflow: hidden;
    background: var(--ink);
    /* Vertical scrolling stays the browser's; the horizontal axis is ours. */
    touch-action: pan-y;
  }

  .narrow {
    display: none;
    padding: 2.5rem 0.75rem;
    text-align: center;
    font-size: 13px;
    color: var(--text-3);
  }

  @media (max-width: 639.98px) {
    .narrow {
      display: block;
    }

    .tl {
      display: none;
    }
  }

  .tl:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: -2px;
  }

  .empty {
    padding: 2.5rem 0.75rem;
    text-align: center;
    font-size: 13px;
    color: var(--text-3);
  }

  .head {
    display: flex;
    flex: 0 0 auto;
    border-bottom: 1px solid var(--hair);
  }

  /* The label column's width comes from `LABEL_W`, inline, so the model and
     the markup cannot disagree about it. Sticky, so it stays put if this ever
     gains a horizontal scroll (it must not: see `.tl`). */
  .corner,
  .label {
    flex-grow: 0;
    flex-shrink: 0;
    position: sticky;
    left: 0;
    z-index: 1;
    background: var(--ink);
  }

  .corner {
    display: flex;
    align-items: center;
    padding: 0 12px;
    font-family: var(--font-display);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--text-3);
  }

  .ruler {
    position: relative;
    min-width: 0;
    flex: 1 1 0;
    overflow: hidden;
  }

  .legend {
    display: flex;
    flex: 0 0 auto;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 16px;
    padding: 6px 12px;
    border-top: 1px solid var(--hair);
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-3);
  }

  .key {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .swatch {
    display: inline-block;
    width: 14px;
    height: 6px;
    border-radius: 1px;
  }

  /* A failed run: the bar's 1px --danger outline, around no fill of its own. */
  .swatch.outline {
    border: 1px solid;
  }

  /* A band is the row's height, so its key is taller than a bar's. */
  .swatch.band {
    height: 12px;
    border-radius: 0;
  }

  .swatch.dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }

  .body {
    position: relative;
    flex: 1 1 0;
    min-height: 0;
    overflow-x: hidden;
    overflow-y: auto;
  }

  .rows {
    position: relative;
  }

  .row {
    display: flex;
    cursor: pointer;
    border-bottom: 1px solid var(--hair-quiet);
  }

  /* The selected row, as the brief draws it: the selected fill and a 2px cyan
     edge that starts at the very left of the row, inside the label column. */
  .row.sel {
    background: var(--ink-selected);
    box-shadow: inset 2px 0 0 0 var(--accent-2);
  }

  .row.sel .label {
    background: var(--ink-selected);
  }

  .label {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    min-width: 0;
  }

  .nid {
    flex: 0 0 auto;
    font-family: var(--font-display);
    font-size: 11px;
    color: var(--text-3);
  }

  /* Clipped with an ellipsis, never mid-glyph. */
  .name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 13px;
    color: var(--text-2);
  }

  .lane {
    position: relative;
    min-width: 0;
    flex: 1 1 0;
  }

  .now {
    position: absolute;
    top: 0;
    width: 1px;
    background: var(--accent-2);
    pointer-events: none;
  }

  .tip {
    position: absolute;
    z-index: 5;
    display: flex;
    flex-direction: column;
    gap: 2px;
    border: 1px solid var(--hair-strong);
    background: var(--ink-raised);
    padding: 6px 8px;
    font-family: var(--font-display);
    font-size: 11px;
    color: var(--text-3);
    pointer-events: none;
  }

  .tip .first {
    color: var(--text-1);
  }
</style>
