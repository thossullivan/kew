<script lang="ts">
  import {
    GAUGE,
    GAUGE_COMPACT,
    gaugeMoney,
    gaugeState,
    type GaugeTick,
    type GaugeValueMarker,
    type LabelAlign
  } from '$lib/factory/gauge';

  let {
    spent,
    cap,
    marker = null,
    dashed = false,
    fmt = gaugeMoney
  }: {
    spent: number;
    cap: number | null | undefined;
    /** An extra pinned value, e.g. `{ value: 600, label: 'prior cap' }`. */
    marker?: GaugeValueMarker | null;
    /** The compact "no data yet" scale: a dashed track, no fill, no cap pin. */
    dashed?: boolean;
    /** Axis money formatter; defaults to `$0` / `$125` / `cap $1,500`. */
    fmt?: (n: number) => string;
  } = $props();

  /** Keep an end label inside the track instead of hanging off the edge. */
  const SHIFT: Record<LabelAlign, string> = {
    start: 'none',
    center: 'translateX(-50%)',
    end: 'translateX(-100%)'
  };

  /** 2dp is finer than a pixel on a 1440px board; 61.00333333% is just noise. */
  const at = (pct: number) => `${Number(pct.toFixed(2))}%`;

  const LABEL = 'absolute whitespace-nowrap';

  /**
   * The two ends of the axis, at every width (DESIGN.md Responsive). The
   * middle tick keeps its taller mark and loses its words: three money labels
   * do not fit across the Monitor's 2×2 phone grid, where the operator browser
   * check at 390px read "$600 cap $1,200" and "$2,000cap $4,000" — the halfway
   * label printed over the cap's (#2282 D25). The floor and the cap are the
   * reading that cannot be dropped: one names the scale's floor, the other the
   * cap the fill is measured against. `gaugeLabelsFit` in `gauge.ts` is the
   * measurement behind it.
   */
  const printed = (tick: GaugeTick) => tick.label != null && tick.align !== 'center';

  const g = $derived(dashed ? GAUGE_COMPACT : GAUGE);
  const state = $derived(gaugeState(spent, cap, { marker, fmt }));
  // Yellow is the instrument colour; over cap it becomes the violation colour,
  // and the clamped fill stops being the only thing the reader sees.
  const fill = $derived(state.over ? 'var(--danger)' : 'var(--warn)');
  // A marker's label is a line of its own under the axis, so the row grows
  // by that line only when there is a marker to name.
  const height = $derived(g.height + (state.marker ? g.captionLine : 0));
</script>

<!--
  11px mono, but not the `.text-11` utility: that one uppercases and tracks out
  its text, which would print "CAP $250" where the artboard reads "cap $250".
-->
{#if state.track}
  <div class="relative font-mono text-[11px] text-text-3" style:height={`${height}px`} data-gauge>
    {#if g.dashed}
      <div
        class="absolute inset-x-0 border-t border-dashed border-hair-strong"
        style:top={`${g.trackTop}px`}
      ></div>
    {:else}
      <div
        class="absolute inset-x-0 bg-ink-track"
        style:top={`${g.trackTop}px`}
        style:height={`${g.trackHeight}px`}
      ></div>
      <div
        class="absolute left-0"
        style:width={at(state.pct ?? 0)}
        style:top={`${g.trackTop}px`}
        style:height={`${g.trackHeight}px`}
        style:background-color={fill}
      ></div>
      <div
        class="absolute right-0"
        style:top={`${g.capMarker.top}px`}
        style:width={`${g.capMarker.width}px`}
        style:height={`${g.capMarker.height}px`}
        style:background-color={fill}
      ></div>
    {/if}

    {#each state.ticks as tick (tick.pct)}
      <div
        class="absolute w-px"
        style:left={at(tick.pct)}
        style:top={`${g.tickTop}px`}
        style:height={`${tick.major ? g.majorTick : g.minorTick}px`}
        style:background-color={tick.major ? 'var(--text-4)' : 'var(--hair-strong)'}
      ></div>
      {#if printed(tick)}
        <span
          class={LABEL}
          style:left={at(tick.pct)}
          style:top={`${g.labelTop}px`}
          style:transform={SHIFT[tick.align]}>{tick.label}</span
        >
      {/if}
    {/each}

    {#if state.marker}
      <div
        class="absolute w-px bg-text-2"
        style:left={at(state.marker.pct)}
        style:top={`${g.markerTop}px`}
        style:height={`${g.markerHeight}px`}
      ></div>
      <!-- Under the axis, not above the track (#2953): above, it was a third
           money label, and clamped to 100% it painted over the over-cap
           caption. -->
      <span
        class="absolute inset-x-0 truncate"
        style:top={`${g.height}px`}
        style:line-height={`${g.captionLine}px`}
        title={state.marker.label}
        data-gauge-marker-caption>{state.marker.label}</span
      >
    {/if}

    {#if state.over}
      <!-- The fill is clamped at 100%, so the overrun has to be said in words. -->
      <span
        class="absolute right-0 whitespace-nowrap font-medium text-danger"
        style:top={`${g.markerLabelTop}px`}
        title={state.exact}>{state.caption}</span
      >
    {/if}
  </div>
{:else}
  <div class="font-mono text-[11px] text-text-3">{state.caption}</div>
{/if}
