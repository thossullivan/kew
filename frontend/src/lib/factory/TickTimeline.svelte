<script lang="ts">
  // The epic page's FULL tick strip (#2304): the same attempts as the compact
  // row strip, but placed by their instants on the console's one shared time
  // axis (`$lib/time/axis`) and batched where they overlap (`$lib/time/batch`).
  //
  // Placement rather than fixed slots, because this strip sits under a Gantt
  // already drawn on that axis: a slot strip beside a time-placed one would put
  // two readings of the same minute on one screen, which is exactly what #2299
  // pulled the axis out to prevent. The SVG lane, the hairline ruler and the
  // bar geometry are `epic/EpicTimeline.svelte`'s, so the two read as one
  // picture of the same epic.
  //
  // A dense epic puts dozens of ticks on one pixel. Drawn as they are that is a
  // smear whose topmost mark is whichever the `#each` reached last — the screen
  // reporting array order as severity. So overlapping ticks become ONE bar
  // wearing its worst member's colour, with the detail in the popover.
  //
  // A quiet batch is a `--text-5` HAIRLINE, not a filled block: nine ticks in
  // ten did nothing, and a lane of blocks would read as nine tenths activity.
  // Only a live batch is cyan and only it grows (DESIGN.md: cyan is the only
  // "live" colour, and it is rationed) — and not even a live batch is cyan
  // when something in it broke: see `batchFill` in `factory/tick-strip.ts`,
  // where the colour decision lives so it can be unit-tested.
  import { fmtTime } from '$lib/time';
  import { axisTicks, type AxisWindow } from '$lib/time/axis';
  import { batchEntries, batchMarks, type BatchSeverity } from '$lib/time/batch';
  import type { EpicTickStrip } from '$lib/api';
  import {
    FULL_STRIP_WIDTH,
    batchFill,
    isTickLive,
    stripLane,
    tickCounts,
    tickMarks,
    tickWord
  } from '$lib/factory/tick-strip';

  // `driverStatus` is the epic heartbeat's served liveness reading, handed
  // straight to the strip's liveness rule: without it the only evidence an open
  // row is still running is its own age, and a tick that legitimately outlasts
  // `TICK_LIVE_MAX_MS` (the merge queue beats its lock through exactly such a
  // step) would stop growing its bar and read "abandoned" in the popover while
  // the server still calls the driver live.
  let {
    ticks,
    width = FULL_STRIP_WIDTH,
    label = 'Driver ticks',
    driverStatus = null
  }: {
    ticks: EpicTickStrip[];
    width?: number;
    label?: string;
    driverStatus?: string | null;
  } = $props();

  /** The lane's height, and the three bar heights in it: a hairline for a
   * quiet batch, a short bar for one that did something, the tall one for the
   * live tick. */
  const LANE_H = 22;
  const QUIET_H = 1;
  const BAR_H = 8;
  const LIVE_H = 14;

  const marks = $derived(tickMarks(ticks, driverStatus));

  /** The window the strip covers — `stripLane`, so the pad that keeps a
   * zero-duration mark (a tick still RUNNING has no end) off the clipped right
   * edge is pinned by a unit test rather than by this file. */
  // Named `lane`, not `window`: a component-scope `const window` shadows the
  // global one for the whole module, which is a trap for the next edit here.
  const lane = $derived<AxisWindow>(stripLane(marks, width));

  const batches = $derived(batchMarks(marks, lane));
  const rule = $derived(axisTicks(lane, { minors: false }));

  /** A batch is live when any attempt in it is still running — the strip's one
   * liveness rule (`isTickLive`), bound included, so a row left open by a
   * driver that died mid-tick stops growing a bar here too. */
  const isLive = (items: readonly { item: EpicTickStrip }[]): boolean =>
    items.some(({ item }) => isTickLive(item, Date.now(), driverStatus));

  const barHeight = (live: boolean, severity: BatchSeverity): number =>
    live ? LIVE_H : severity === 'quiet' ? QUIET_H : BAR_H;

  let open = $state<number | null>(null);
  const openBatch = $derived(batches.find((b) => b.items[0].item.id === open) ?? null);
</script>

<div class="relative" style:width={`${width}px`}>
  <svg
    {width}
    height={LANE_H}
    role="img"
    aria-label={`${label}: ${ticks.length} attempts`}
    style="display: block"
  >
    <!-- The ruler, from the shared axis. A line is a fact; its label is a
         courtesy, and the axis drops the ones that cannot be read. -->
    {#each rule as t (t.ms)}
      <line
        x1={t.x}
        x2={t.x}
        y1="0"
        y2={LANE_H}
        stroke={t.day ? 'var(--hair-strong)' : 'var(--hair-quiet)'}
      />
    {/each}

    {#each batches as batch (batch.items[0].item.id)}
      {@const live = isLive(batch.items)}
      {@const h = barHeight(live, batch.severity)}
      <rect
        x={batch.x}
        y={(LANE_H - h) / 2}
        width={batch.width}
        height={h}
        rx={h > QUIET_H ? 1 : 0}
        fill={batchFill(live, batch.severity)}
        tabindex="0"
        role="button"
        aria-label={`${batch.items.length} tick${batch.items.length === 1 ? '' : 's'} at ${fmtTime(batch.startMs)}`}
        onmouseenter={() => (open = batch.items[0].item.id)}
        onmouseleave={() => (open = null)}
        onfocus={() => (open = batch.items[0].item.id)}
        onblur={() => (open = null)}
      />
    {/each}
  </svg>

  {#if openBatch}
    <!-- In place, over the lane: no toast and no modal (DESIGN.md). The counts
         and the error class, which is all the server records about a failure. -->
    <span
      class="pointer-events-none absolute bottom-6 z-20 flex max-w-xs flex-col gap-0.5
             rounded border border-hair-strong bg-ink-raised px-2 py-1.5
             font-mono text-[11px] text-text-2 shadow-lg"
      style:left={`${Math.min(Math.max(0, openBatch.x - 8), Math.max(0, width - 220))}px`}
    >
      <span class="text-text-1">
        {fmtTime(openBatch.startMs)} · {openBatch.items.length} tick{openBatch.items.length === 1
          ? ''
          : 's'}
      </span>
      {#each batchEntries(openBatch.items, 8) as entry, i (i)}
        {#if entry.kind === 'item'}
          {@const detail = tickCounts(entry.mark.item)}
          <span class="truncate">
            {tickWord(entry.mark.item, driverStatus)}{detail.length ? ` · ${detail.join(' · ')}` : ''}
          </span>
        {:else if entry.kind === 'rule'}
          <span class="my-0.5 h-px bg-hair"></span>
        {:else}
          <span class="text-text-3">{entry.label}</span>
        {/if}
      {/each}
    </span>
  {/if}
</div>
