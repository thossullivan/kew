<script lang="ts">
  /**
   * FUI-23 — the Monitor's live-runs table (DMonitor y ≈294–440).
   *
   * The component is a renderer and a clock. Every word in it is built by
   * `$lib/monitor/live-runs`, where a vitest holds the rows to the artboard —
   * which runs are listed, how an elapsed time is formatted, how a posture is
   * worded and what an unreported stage draws.
   *
   * It takes no reading of its own. The runs come from the shell's shared poll,
   * the same payload the header's "2 working" and the Working readout above this
   * table are counted from, so the table costs this page nothing and cannot
   * disagree with the figure over it.
   *
   * The one thing it owns is the second hand. Elapsed is a stopwatch, and a
   * column that only moved on the 7s runs poll would sit visibly still and then
   * jump; so a 1s tick re-derives the view, which is pure and cheap. Nothing
   * else on the row changes between polls.
   *
   * `runs` is the seam the tests (and any future push feed) use: pass a payload
   * and the shared poll is not consulted.
   */
  import { browser } from '$app/environment';
  import type { Run } from '$lib/api';
  import { dataRowClasses, TAP_PILL } from '$lib/factory/classes';
  import { RUNS_UNREAD, type Source } from '$lib/monitor/readouts';
  import { factoryPulse } from '$lib/shell/pulse.svelte';
  import { SectionHead, StatusMark } from '$lib/ui';
  import { ELAPSED_TICK_MS, liveRunsView, LIVE_RUNS_COLUMNS, stageLine } from './live-runs';

  let { runs }: { runs?: Source<readonly Run[]> } = $props();

  /** The Elapsed column's second hand. Seeded so the first paint is not 1s late. */
  let clock = $state(Date.now());
  $effect(() => {
    if (!browser) return;
    const timer = setInterval(() => {
      clock = Date.now();
    }, ELAPSED_TICK_MS);
    return () => clearInterval(timer);
  });

  // The pulse holds `null` until its first tick lands, and after a poll that
  // failed. Neither is an empty fleet, so both become the source's stated
  // reason — the same reading `workingReadout` is given on the page above.
  const source = $derived<Source<readonly Run[]>>(
    runs ?? factoryPulse.runs ?? { available: false, reason: RUNS_UNREAD }
  );
  const view = $derived(liveRunsView(source, clock));
</script>

<section aria-label="Live runs" class="flex min-w-0 flex-col">
  <SectionHead label="Live runs" meta={view.meta} />

  {#if view.rows.length > 0}
    <!--
      The column heads belong to the six-column table, which only exists from
      1024 up: below that a row is two lines and a heading strip over it would
      name columns that are not there (#2282 D3).
    -->
    <div
      class={dataRowClasses({ head: true, class: 'hidden lg:grid' })}
      style:grid-template-columns={LIVE_RUNS_COLUMNS}
    >
      <span>Item</span><span>Stage</span><span>Runner</span><span>Posture</span><span
        class="text-right">Elapsed</span
      ><span class="text-right">State</span>
    </div>

    {#each view.rows as row, i (row.key)}
      <!--
        Six columns from 1024 up; two lines below it (#2282 D3). The six cells
        are the same six DOM nodes either way — the heads above name them in
        this order, and that is the order a screen reader reads at any width —
        so the phone layout is `order` over a wrapping flex row rather than a
        second copy of the row: the run and its clock on the first line, what
        it is doing on the second. The height goes back to `auto` with it,
        because a long runner or posture must wrap rather than be cut off by a
        56px row.
      -->
      <div
        data-live-run={row.itemId || undefined}
        data-run={row.key}
        class={dataRowClasses({
          rule: i === view.rows.length - 1 ? 'none' : 'quiet',
          class: 'flex h-auto flex-wrap gap-2 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0'
        })}
        style:grid-template-columns={LIVE_RUNS_COLUMNS}
      >
        <!-- A run the payload names no item for (the `0` sentinel a non-GitHub
             item is served as) has nowhere on the Work screen to go: the cell
             states the absence instead of linking to an item that is not
             there. -->
        {#if row.href}
          <!--
            The one control in the row, and 20px of it on a phone against
            DESIGN.md's 32px floor: the cell is a box of its own, so it takes
            the height rather than padding, and the row's `h-auto` holds it
            (#2282 D25).
          -->
          <a
            class={`order-1 inline-flex items-center font-mono text-13 text-text-1
                    underline-offset-2 hover:underline lg:order-none ${TAP_PILL}`}
            href={row.href}>{row.ref}</a
          >
        {:else}
          <span class="order-1 font-mono text-13 text-text-3 lg:order-none">{row.ref}</span>
        {/if}
        <span class="order-5 min-w-0 truncate text-15 text-text-2 lg:order-none"
          >{stageLine(row)}</span
        >
        <span
          class="order-6 min-w-0 truncate text-13 text-text-2 lg:order-none"
          data-runner
          title={row.runnerId || undefined}>{row.runner}</span
        >
        <span class="order-7 min-w-0 truncate font-mono text-13 text-text-2 lg:order-none"
          >{row.posture}</span
        >
        <span class="order-2 ml-auto text-right font-mono text-13 text-text-3 lg:order-none lg:ml-0"
          >{row.elapsed}</span
        >
        <span class="order-3 flex justify-end font-mono text-13 lg:order-none">
          <!-- gap-2 (8px) over StatusMark's own 6px, as the DWork state cell
               and the holds column below both draw it. -->
          <StatusMark kind={row.state} vocabulary="run" size={7} word class="gap-2" />
        </span>
        <!--
          The line break, below `lg` only: a flex row wraps where it runs out
          of width, and what goes on the second line here is a decision, not an
          overflow. Removed from the grid above that, where there is one line.
        -->
        <span class="order-4 w-full lg:hidden" aria-hidden="true"></span>
      </div>
    {/each}
  {:else}
    <!-- "nothing running" is a sentence, so it is data-13, not an 11px mono note (#2896 R7). -->
    <p class="text-13 text-text-3">{view.notice}</p>
  {/if}
</section>
