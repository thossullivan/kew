<script lang="ts">
  /**
   * FUI-22 — `/monitor`, the fleet instrument panel (DMonitor).
   *
   * This issue lands the route and the readout row across the top (x 40–1400,
   * y ≈88–258); FUI-23 hung the live-runs table off the same column, where it
   * reads the shell's shared runs poll rather than a load of its own, so the
   * table and the Working readout above it count one payload between them.
   * FUI-24 added the holds column and FUI-25 the event stream, which
   * polls `/api/audit` for itself rather than through `+page.ts` — it is the
   * only reading on this screen that has to keep moving while the page sits
   * open, and FUI-30 will swap that poll for the push feed in one component.
   *
   * The page is a renderer. Every figure, caption and reason in it is built by
   * `$lib/monitor/readouts`, where a vitest holds them to the artboard and to
   * the money rule (N5: the gauge reads `agentic`, the portion the governor
   * enforces, never `spent`).
   *
   * The two counters and the two spend cells all come from the shell's shared
   * poll rather than a fetch of their own, so they tick with the header above
   * them and cost the page nothing (#2847 Q2: the spend cells used to poll
   * `/api/report/spend` beside the pulse's dashboard read of the same caps).
   */
  import { page } from '$app/state';
  import { replaceState } from '$app/navigation';
  import { Gauge, Readout } from '$lib/ui';
  import EventStream from '$lib/monitor/EventStream.svelte';
  import Holds from '$lib/monitor/Holds.svelte';
  import LiveRuns from '$lib/monitor/LiveRuns.svelte';
  import { readoutClasses, TAP_INLINE } from '$lib/factory/classes';
  import { factoryPulse } from '$lib/shell/pulse.svelte';
  import {
    captionText,
    DAILY_CAP_FIELD,
    isUnavailable,
    needsYouReadout,
    priorCap,
    QUEUE_UNREAD,
    RUNS_UNREAD,
    spendFromPulse,
    spendReadout,
    workingReadout,
    WEEKLY_CAP_FIELD,
    type CaptionSegment,
    type Source,
    type SpendReadout
  } from '$lib/monitor/readouts';
  import type { BudgetMap } from '$lib/api';
  import {
    parseStreamQuery,
    serializeStreamQuery,
    STREAM_PARAMS,
    type StreamQuery
  } from '$lib/monitor/stream-query';
  import type { MonitorData } from './+page';

  let { data }: { data: MonitorData } = $props();

  /**
   * The stream's query: the one it last wrote, or the URL's.
   *
   * `replaceState` is shallow routing, and shallow routing does NOT move
   * `page.url` — SvelteKit keeps it at the last real navigation and updates
   * only `page.state`. Reading the query off `page.url` alone read the URL the
   * page was opened on, forever: a preset click rewrote the address bar and the
   * stream snapped straight back, and so did every brush drag, pan and filter
   * (#2248). So the search the stream writes travels in `page.state` as well,
   * and is what this reads while it is there. A real navigation (a link to
   * `/monitor?item=…`, a reload) resets `page.state`, and the reading falls
   * back to `page.url`, which is then current.
   */
  const streamQuery = $derived(
    parseStreamQuery(new URLSearchParams(page.state.streamSearch ?? page.url.search))
  );

  /**
   * The stream's params, replaced wholesale. Only the six names
   * `stream-query.ts` owns are cleared first, so a param another surface put on
   * this URL survives a range click.
   *
   * Built off the address bar as the shallow router keeps it — the search in
   * `page.state.streamSearch`, which this page's writes and a demo tour's
   * (`writeTourLink`) both keep current — and off `page.url` only while the
   * state holds none, since shallow routing leaves `page.url` behind. The state
   * is merged into, not replaced. A tour keeps `?tour=&step=` in the address
   * bar and its own keys in `page.state` (`demo/tours/deep-link.ts`), and a
   * range click built from `page.url` with a fresh state dropped both, closing
   * the tour (#3468 C8).
   */
  function setStreamQuery(next: StreamQuery): void {
    const url = new URL(page.url);
    if (page.state.streamSearch !== undefined) url.search = page.state.streamSearch;
    for (const name of STREAM_PARAMS) url.searchParams.delete(name);
    for (const [name, value] of serializeStreamQuery(next)) url.searchParams.append(name, value);
    replaceState(url, { ...page.state, streamSearch: url.search });
  }

  /**
   * The caps as the holds column last read them. `priorCap` draws the pin if
   * and only if an override is in force, so a map read once by `load()` would
   * go on marking an override another operator reverted while the page sat
   * open — with the holds column below already reporting "none active" off its
   * own poll of the same endpoint. A reading that FAILED is not an answer here:
   * the pin keeps the last good map rather than blinking out on one dropped
   * poll, which is how the shell's pulse treats a transient failure too.
   */
  let polledBudget = $state<BudgetMap | null>(null);
  const budget = $derived<Source<BudgetMap>>(polledBudget ?? data.budget);

  /**
   * The spend as the shell's pulse last read it (#2847 Q2).
   *
   * The Monitor is a screen left open, so the two gauges must move while the
   * runs above them spend the cap down (C9). They used to by polling
   * `/api/report/spend` on the heartbeat — beside the pulse's own dashboard
   * read, whose `glance.caps` is the same two windows. One read now: the
   * pulse's, which keeps the last good dashboard through a dropped poll and
   * already skips a hidden tab.
   */
  const spendSource = $derived(spendFromPulse(factoryPulse.dashboard));

  // The pulse holds `null` until its first tick lands, and after a poll that
  // failed. Neither is a zero, so both become the source's stated reason.
  const working = $derived(
    workingReadout(factoryPulse.runs ?? { available: false, reason: RUNS_UNREAD })
  );
  const needsYou = $derived(
    needsYouReadout(factoryPulse.dashboard ?? { available: false, reason: QUEUE_UNREAD })
  );

  /**
   * Whether the Needs you sentence, "and N more" included, fits on one line
   * of its cell (#3052 N3). Where it does not, the cell says the queue by kind
   * instead ("1 merge decision · 1 hold"): the clamp cut the fixture's sentence
   * to "PR #2059 is open —…" at 1440, which names nothing.
   *
   * Measured off a probe that holds the whole sentence on one line across the
   * caption's full width and stays in the layout whichever reading is drawn,
   * so a window that widens finds the sentence fitting again. The sentence
   * itself is read here so a new one is measured even when the width has not
   * moved. Where there is no `ResizeObserver` (a test DOM) there is no layout
   * to measure either, and the sentence stands.
   */
  let captionProbe = $state<HTMLSpanElement | null>(null);
  let sentenceFits = $state(true);
  /** Room for the gap the drawn count takes beside the sentence over a space. */
  const FIT_SLACK_PX = 4;

  $effect(() => {
    const probe = captionProbe;
    if (!probe || typeof ResizeObserver === 'undefined') return;
    void captionText(needsYou.caption);
    const measure = () => {
      sentenceFits = probe.scrollWidth + FIT_SLACK_PX <= probe.clientWidth;
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(probe);
    return () => observer.disconnect();
  });

  // One failed spend read takes both windows with it (they arrive together),
  // but never the prior-cap pin beside them, which comes from the budget map.
  const spend: SpendReadout[] = $derived([
    spendReadout(
      'Today',
      isUnavailable(spendSource) ? spendSource : spendSource.daily,
      priorCap(budget, DAILY_CAP_FIELD)
    ),
    spendReadout(
      'This week',
      isUnavailable(spendSource) ? spendSource : spendSource.weekly,
      priorCap(budget, WEEKLY_CAP_FIELD)
    )
  ]);

  /** The figure's own classes, borrowed so the "/ $250" sits on its baseline. */
  const moneyFigure = readoutClasses({ tone: 'money' }).figure;
</script>

<!--
  A caption is segmented so the server's own sentence can carry the link to the
  row it describes ("merge decision on [PR #2059]") instead of repeating the
  reference after it.
-->
{#snippet caption(segments: CaptionSegment[])}
  {#if segments.length > 0}
    <div class="text-15 text-text-2">
      {#each segments as segment, i (i)}
        {#if segment.href}
          <!-- A ref inside a sentence takes its press target as padding, which
               an inline box is tapped by without taking it into the line it
               sits in (#2282 D25). -->
          <a
            class={`text-text-1 underline-offset-2 hover:underline ${TAP_INLINE}`}
            href={segment.href}>{segment.text}</a
          >
        {:else}{segment.text}{/if}
      {/each}
    </div>
  {/if}
{/snippet}

<!--
  The Needs you caption, which is one line at a desk and two on a phone
  (#2847 P10). It describes the top row of a queue, so the server's sentence is
  a label here rather than the reading: the whole of it in the `title`, with
  "and N more" after it never clipped — beside it from 1024 up, on the line
  under it below that, where a cell is half of a 390px screen. It ran six lines
  unclamped.

  When the sentence does not fit its line it is not cut to an ellipsis: the
  cell says the queue by kind instead, which wraps only at its spaces (#3052
  N3). The probe is what decides that; it is never seen.
-->
{#snippet clampedCaption(segments: CaptionSegment[], summary: CaptionSegment[])}
  {#if segments.length > 0}
    <div class="relative text-15 text-text-2" title={captionText(segments)} data-needs-you-caption>
      <span
        bind:this={captionProbe}
        class="pointer-events-none invisible absolute inset-x-0 top-0 overflow-hidden whitespace-nowrap"
        aria-hidden="true"
        data-caption-probe>{captionText(segments)}</span
      >
      {#if sentenceFits || summary.length === 0}
        <div class="flex flex-col lg:flex-row lg:items-baseline" data-caption-sentence>
          <!-- The ref's press padding (TAP_INLINE) is room inside the clip, so the
               ellipsis's `overflow: hidden` does not cut the target back to the
               line (#2282 D25). -->
          <span class="-my-2.5 min-w-0 truncate py-2.5 sm:my-0 sm:py-0">
            {#each segments.filter((segment) => !segment.count) as segment, i (i)}
              {#if segment.href}<a
                  class={`text-text-1 underline-offset-2 hover:underline ${TAP_INLINE}`}
                  href={segment.href}>{segment.text}</a
                >{:else}{segment.text}{/if}
            {/each}
          </span>
          {#each segments.filter((segment) => segment.count) as segment, i (i)}
            <span class="shrink-0 whitespace-nowrap lg:ml-[0.3em]">{segment.text.trim()}</span>
          {/each}
        </div>
      {:else}
        <!-- Each kind is one phrase that never breaks inside ("1 merge
             decision"); the line breaks only at the separators between them
             (#3468 D33), where at 390 it read "1 merge decision · 1 / hold". -->
        <div data-caption-summary>
          {#each summary as segment, i (i)}
            {#if segment.href}<a
                class={`whitespace-nowrap text-text-1 underline-offset-2 hover:underline ${TAP_INLINE}`}
                href={segment.href}>{segment.text}</a
              >{:else if segment.text.trim() === '·'}{segment.text}{:else}<span
                class="whitespace-nowrap">{segment.text}</span
              >{/if}
          {/each}
        </div>
      {/if}
    </div>
  {/if}
{/snippet}

<!--
  The page's own gutter is 16px below `sm` (#2282 D3), as the header's is: 40px
  either side of a 390px screen is a fifth of it, and the readouts clipped for
  exactly that much.
-->
<div class="flex min-h-0 flex-1 flex-col gap-9 overflow-y-auto px-4 pt-8 pb-9 sm:px-10">
  <!--
    Four across from 1024 up, 2×2 below it (#2282 D3). The cells' hairlines and
    flush edges move with the grid: below `lg` the divider belongs to the right
    column of each row, the row below gets a rule over it, and the outer
    padding comes off whichever cell is at the screen edge — which at 2×2 is a
    different pair of cells than at 4×1.

    Every track is `minmax(0, …)` and every cell `min-w-0` (#2889): a bare `1fr`
    never shrinks below its content's min-content width, so a long one-line
    Needs you caption widened its own track and pushed Today and This week off
    a 1440 screen. Now the caption stays inside its own cell.
  -->
  <section
    aria-label="Readouts"
    class="grid grid-cols-2 border-y border-hair lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.35fr)_minmax(0,1.35fr)]"
  >
    <Readout
      label={working.label}
      tone={working.tone}
      value={working.figure}
      edge="start"
      class="min-w-0 pr-4 lg:pr-7"
    >
      {@render caption(working.caption)}
    </Readout>

    <Readout
      label={needsYou.label}
      tone={needsYou.tone}
      value={needsYou.figure}
      divided
      class="min-w-0 pr-0 pl-4 lg:pr-7 lg:pl-7"
    >
      {@render clampedCaption(needsYou.caption, needsYou.summary ?? [])}
    </Readout>

    {#each spend as cell, i (cell.label)}
      <Readout
        label={cell.label}
        tone={cell.tone}
        meta={cell.meta}
        metaShort={cell.metaShort}
        metaTitle={cell.metaTitle}
        divided
        edge={i === 1 ? 'end' : undefined}
        class={i === 0
          ? 'min-w-0 border-t border-l-0 border-hair pr-4 pl-0 lg:border-t-0 lg:border-l lg:pr-7 lg:pl-7'
          : 'min-w-0 border-t border-hair pl-4 lg:border-t-0 lg:pl-7'}
      >
        {#snippet figure()}
          <!-- #2545 D21 — the cap only from 1024 up. In the 2×2 fold a cell is
               half of a 390px screen, and `$178.05 / $250` broke over two
               lines there. The meta line (`71.2% of cap`, `no cap`) and the gauge
               ticks under the figure still say what the cap is. -->
          <div class={moneyFigure}>
            {cell.figure}{#if cell.capLabel}<span
                class="ml-2.5 hidden text-15 font-normal text-text-3 lg:inline"
                >/ {cell.capLabel}</span
              >{/if}
          </div>
        {/snippet}
        {@render caption(cell.caption)}
        {#if cell.gauge}
          <!--
            The headroom the artboard gives a marked gauge: 26px against 8px
            unmarked. The marker's label is a caption under the axis now
            (#2953), so that row holds the over-cap caption alone.
          -->
          <div style:margin-top={cell.gauge.marker ? '26px' : '8px'}>
            <Gauge spent={cell.gauge.spent} cap={cell.gauge.cap} marker={cell.gauge.marker} />
          </div>
        {/if}
      </Readout>
    {/each}
  </section>

  <!-- The live runs (DMonitor y ≈294–440). -->
  <LiveRuns />

  <!--
    The lower band (DMonitor y ≈476–924): holds and blocks on 5 columns, the
    event stream on 7.
  -->
  <section
    class="grid min-h-0 grow grid-cols-1 border-t border-hair lg:grid-cols-[5fr_7fr]"
  >
    <div class="min-w-0 pt-[22px] pb-6 lg:pr-10 lg:pb-0">
      <!--
        The budget map is passed as the column's first reading only: an override
        another operator lifts has to leave the holds list, so `Holds` re-reads
        `/api/config/budget` on its own poll rather than living on this load —
        and hands each reading back, so the prior-cap pin above is drawn off the
        same map the rows below are, never off a snapshot they have outlived.
      -->
      <Holds
        budget={data.budget}
        onbudget={(b) => {
          if (!isUnavailable(b)) polledBudget = b;
        }}
      />
    </div>
    <!--
      Below 1024 the two stack and the holds sit above the stream (DESIGN.md,
      Responsive Behavior; #2282 D3). The hairline between them turns with
      them: a left border on a full-width column is a line down one edge of the
      screen, so below `lg` it is the rule over the stream instead.
    -->
    <div class="min-w-0 border-t border-hair pt-[22px] lg:border-t-0 lg:border-l lg:pl-10">
      <!--
        The stream's state lives in the URL, and this page is what owns a URL:
        the range, the brushed window and the three filters are read out of
        the URL and written back with `replaceState`, so a window worth
        showing somebody is a link and a reload opens on what was being read.
        `replaceState` keeps `load()` from re-running — none of these params is
        a server read — and the same shallow-navigation reason `/work` gives.
      -->
      <EventStream query={streamQuery} onquery={setStreamQuery} />
    </div>
  </section>
</div>
