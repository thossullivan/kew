<script lang="ts">
  /**
   * C3/#2301 — four small multiples on one weekly x-axis, with a synced
   * crosshair: hovering a week lights that week on all four.
   *
   * There is one `active` week index for the whole stack, so the sync is not a
   * mechanism — it is the absence of one. Each row maps the pointer to a band
   * off its own box, and because every row's plot cell is the same width in
   * the same grid, every row maps it identically.
   *
   * Everything drawn here is arithmetic from `./outcomes`: this file holds no
   * formula. No chart library, no animation, no tooltip element — the hovered
   * week's reading is printed in place beside its chart's label (DESIGN: show
   * state in place, no toasts).
   *
   * Colour: amber is money, so only the $/merged-PR series is amber. Cyan is
   * the crosshair and nothing else, because the only thing selected here is a
   * week. A week whose sample is below `min_sample` is drawn in `text-5` and
   * says so when it is read — greyed, never hidden: "we shipped three things
   * that week" is the finding, not an error.
   */
  import type { OutcomeReport } from '$lib/api';
  import { bindShortcuts, registerShortcuts } from '$lib/keys/registry';
  import {
    OUTCOME_KEYS,
    VIEW_H,
    VIEW_W,
    bandWidth,
    bandX,
    barSegments,
    buildCharts,
    crosshairKey,
    crosshairNote,
    plotY,
    polylines,
    stepIndex,
    weekFooter,
    weekIndexAt,
    weekLabel
  } from './outcomes';

  let { report }: { report: OutcomeReport | null } = $props();

  const charts = $derived(buildCharts(report));
  const weeks = $derived(report?.weeks ?? []);
  const count = $derived(weeks.length);

  /** The week every chart is lit on, or null when the pointer is away. */
  let active = $state<number | null>(null);

  /** The marker size, in view units. Rects, not circles: the viewBox is
   *  stretched, so a circle would render as an ellipse. */
  const MARK = 2.6;

  function track(event: PointerEvent): void {
    const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
    if (box.width <= 0) return;
    active = weekIndexAt((event.clientX - box.left) / box.width, count);
  }

  let group = $state<HTMLDivElement | undefined>(undefined);

  /**
   * A press on the stack — its one tab stop. The keys are registered on the
   * console's key layer so `?` lists them (#2781); a press anywhere else on
   * the page, or one carrying ⌘, ^ or ⌥ (⌘← is the browser's Back), is left
   * alone.
   */
  function step(event: KeyboardEvent): void {
    if (!(event.target instanceof Node) || !group?.contains(event.target)) return;
    const named = crosshairKey(event);
    if (named === null) return;
    if (named === 'older' || named === 'newer') {
      active = stepIndex(active, named === 'newer' ? 1 : -1, count);
    } else if (named === 'first') {
      active = count > 0 ? 0 : null;
    } else if (named === 'last') {
      active = count > 0 ? count - 1 : null;
    } else {
      active = null;
    }
    event.preventDefault();
  }

  $effect(() => registerShortcuts(bindShortcuts('outcomes', OUTCOME_KEYS, step)));

  /** The bar chart's own scale: the busiest week's total merges. */
  const mergedMax = $derived(Math.max(1, ...weeks.map((w) => barSegments(w).total)));

  /** One week's two-segment bar, in view units. The bar is 44% of its band so
   *  the weeks read as columns rather than as one filled block. */
  function bar(index: number) {
    const seg = barSegments(weeks[index]);
    const width = bandWidth(count) * 0.44;
    const floor = VIEW_H - 1;
    const height = Math.max(0, floor - plotY(seg.total, mergedMax));
    const mine = seg.total > 0 ? (seg.authored / seg.total) * height : 0;
    return { x: bandX(index, count) - width / 2, width, floor, height, mine };
  }

  /** The ink of a point: greyed when its sample is too small to read. */
  const ink = (money: boolean, thin: boolean) =>
    thin ? 'var(--text-5)' : money ? 'var(--warn)' : 'var(--text-2)';
</script>

<!-- One focusable group for the whole stack: arrow keys step the crosshair,
     Escape drops it, and Tab always leaves. `role="application"` is the
     honest name for a surface whose arrow keys mean something other than
     scrolling, and the same precedent the DAG canvas sets. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="flex flex-col"
  role="application"
  aria-label="Weekly outcomes. Arrow keys move the crosshair between weeks."
  tabindex="0"
  bind:this={group}
  onpointerleave={() => (active = null)}
>
  {#each charts as chart (chart.key)}
    {@const point = active === null ? null : chart.points[active]}
    <div
      class="grid grid-cols-1 items-center gap-x-6 border-t border-hair-quiet py-3 md:grid-cols-[216px_minmax(0,1fr)]"
    >
      <div class="flex min-w-0 flex-col gap-1">
        <span class="text-11 tracking-[0.14em] text-text-3 uppercase">{chart.label}</span>
        <!-- The hovered week's reading, in place. Without a hover it reads the
             newest week, so the row is never blank. -->
        <span
          class={`font-mono text-13 ${point?.thin ? 'text-text-4' : 'text-text-1'}`}
          aria-live="off">{crosshairNote(chart, active)}</span
        >
      </div>

      {#if chart.empty}
        <p class="font-mono text-11 text-text-3">{chart.empty}</p>
      {:else}
        <div
          class="relative h-[60px] w-full"
          onpointermove={track}
          onpointerdown={track}
          role="img"
          aria-label={`${chart.label}: ${crosshairNote(chart, active)}`}
        >
          <svg
            class="h-full w-full"
            viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <!-- Baseline, the only axis furniture: a hairline floor. -->
            <line
              x1="0"
              y1={VIEW_H - 0.5}
              x2={VIEW_W}
              y2={VIEW_H - 0.5}
              stroke="var(--hair)"
              stroke-width="1"
              vector-effect="non-scaling-stroke"
            />

            {#if chart.key === 'merged'}
              <!-- Two segments per week: kew-authored, and the rest of what
                   landed on main. -->
              {#each chart.points as p, i (p.week)}
                {@const geometry = bar(i)}
                <rect
                  x={geometry.x}
                  y={geometry.floor - geometry.height}
                  width={geometry.width}
                  height={Math.max(0, geometry.height - geometry.mine)}
                  fill="var(--ink-track)"
                />
                <rect
                  x={geometry.x}
                  y={geometry.floor - geometry.mine}
                  width={geometry.width}
                  height={geometry.mine}
                  fill={p.thin ? 'var(--text-5)' : 'var(--text-2)'}
                />
              {/each}
            {:else}
              {#each polylines(chart.points, chart.max) as path, i (i)}
                <path
                  d={path}
                  fill="none"
                  stroke={chart.money ? 'var(--warn)' : 'var(--text-3)'}
                  stroke-width="1"
                  vector-effect="non-scaling-stroke"
                />
              {/each}
              {#each chart.points as p, i (p.week)}
                {#if p.value !== null}
                  <rect
                    x={bandX(i, count) - MARK / 2}
                    y={plotY(p.value, chart.max) - MARK / 2}
                    width={MARK}
                    height={MARK}
                    fill={ink(chart.money, p.thin)}
                  />
                {/if}
              {/each}
            {/if}

            <!-- The crosshair. Cyan, because a selected week is the one thing
                 on this page that is selected. -->
            {#if active !== null && count > 0}
              <line
                x1={bandX(active, count)}
                y1="0"
                x2={bandX(active, count)}
                y2={VIEW_H}
                stroke="var(--accent-2)"
                stroke-width="1"
                vector-effect="non-scaling-stroke"
                opacity="0.55"
              />
              {#if chart.key === 'merged'}
                <!-- On the bar chart the mark sits on top of kew's own
                     segment, which is the quantity the row is reading. -->
                <rect
                  data-active-mark={chart.key}
                  x={bandX(active, count) - MARK}
                  y={bar(active).floor - bar(active).mine - MARK}
                  width={MARK * 2}
                  height={MARK * 2}
                  fill="var(--accent-2)"
                />
              {:else if point && point.value !== null}
                <rect
                  data-active-mark={chart.key}
                  x={bandX(active, count) - MARK}
                  y={plotY(point.value, chart.max) - MARK}
                  width={MARK * 2}
                  height={MARK * 2}
                  fill="var(--accent-2)"
                />
              {/if}
            {/if}
          </svg>
        </div>
      {/if}
    </div>
  {/each}

  <!-- The one x-axis, shared by all four. Equal columns, so a label sits at
       the centre of the same band its marks do. -->
  {#if count > 0}
    <div class="grid grid-cols-1 gap-x-6 border-t border-hair md:grid-cols-[216px_minmax(0,1fr)]">
      <span class="pt-2 text-11 tracking-[0.14em] text-text-4 uppercase">Week of</span>
      <div class="grid pt-2" style:grid-template-columns={`repeat(${count}, minmax(0, 1fr))`}>
        {#each weeks as week, i (week.week_start)}
          <span
            data-week={week.week_start}
            aria-current={i === active ? 'true' : undefined}
            class={`truncate px-1 text-center font-mono text-11 ${
              i === active ? 'text-accent-2' : week.partial ? 'text-text-4' : 'text-text-3'
            }`}>{weekLabel(week.week_start)}</span
          >
        {/each}
      </div>
    </div>

    <!-- The whole bill behind the medians above, for the same week: a
         $/merged-PR reading without the spend no PR explains understates it. -->
    <p data-outcome-footer class="pt-3 font-mono text-11 text-text-3">
      {weekFooter(report, active)}
    </p>
  {/if}
</div>
