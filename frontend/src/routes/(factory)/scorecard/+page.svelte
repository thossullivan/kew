<script lang="ts">
  /**
   * FUI-28 — `/scorecard` on the factory's hairline primitives (DMonitor
   * readouts y ≈88–258, live-runs table grammar y ≈294–440).
   *
   * The data is unchanged: the same window selector, the same leverage headline
   * and the same per-model rows with their by-complexity breakdown.
   *
   * Every cell's wording comes from `$lib/scorecard-headline`, which the page
   * used for the headline already and now uses for the rows too — the model row
   * and its by-complexity rows print the same figures, and they had two copies
   * of each formula between them. That module is also where "local" is decided,
   * off the served `channel` rather than a model-name prefix (N11).
   */
  import { Button, DataRow, Readout, SectionHead } from '$lib/ui';
  import {
    DEFAULT_SCORECARD_DAYS,
    fetchScorecard,
    type OutcomeReport,
    type Scorecard,
    type ScorecardRow
  } from '$lib/api';
  import OutcomeCharts from '$lib/report/OutcomeCharts.svelte';
  import { gaugeMoney } from '$lib/factory/gauge';
  import {
    avgReviews,
    deriveHeadline,
    isLocalRow,
    lifecycleNote,
    mergeRate,
    perMerged,
    planningRatio,
    windowHasPRs
  } from '$lib/scorecard-headline';

  let { data }: { data: { scorecard: Scorecard | null; outcomes: OutcomeReport | null } } =
    $props();

  const liveMode = data.scorecard != null;
  const WINDOWS = [3, 7, 30, 90];

  let scorecard = $state<Scorecard | null>(data.scorecard);
  let days = $state(data.scorecard?.days ?? DEFAULT_SCORECARD_DAYS);
  let loading = $state(false);

  async function selectWindow(d: number) {
    if (d === days && scorecard) return;
    days = d;
    loading = true;
    try {
      scorecard = await fetchScorecard(d);
    } catch {
      // keep last-good data; the offline word already communicates staleness
    } finally {
      loading = false;
    }
  }

  const rows = $derived<ScorecardRow[]>(scorecard?.rows ?? []);
  const head = $derived(deriveHeadline(rows));
  const hasPRs = $derived(windowHasPRs(rows));

  /** Model · runs · merge rate · reviews · spend · $/merged · planned vs actual. */
  const SCORECARD_COLUMNS = 'minmax(0, 1fr) 64px 148px 84px 176px 116px 132px';
  /** Below 1024 a row wraps its cells as a line of labelled figures; from
   * 1024 up it is the grid the head names (Budget's caps table does the same). */
  const WRAPPED_ROW = 'flex h-auto flex-wrap items-center gap-x-5 gap-y-2 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0';
  /** A figure's own label, drawn where the head is not (below 1024). */
  const CELL_LABEL = 'mr-1.5 text-text-3 lg:hidden';

  // The spend cell's bar is relative to the heaviest model in the window, not to
  // a cap: nothing on this page is enforced, so it carries no cap marker.
  const maxSpend = $derived(Math.max(0, ...rows.map((r) => r.spend)));
  const spendPct = (n: number) => (maxSpend > 0 ? (n / maxSpend) * 100 : 0);

  // The section's own meta line: the window, and the staleness of the one
  // number that is not from the ledger. A clone that could not be read says so
  // here rather than drawing a zero denominator.
  // A zero week count is dropped, not printed (D44: show only non-zero counts).
  const outcomeMeta = $derived.by(() => {
    if (data.outcomes === null) return 'not served';
    const n = data.outcomes.weeks.length;
    const main = data.outcomes.merged_main_unavailable
      ? 'main not readable'
      : data.outcomes.merged_main_head
        ? `main at ${data.outcomes.merged_main_head}`
        : '';
    return [n > 0 ? `${n}w` : '', main].filter((part) => part !== '').join(' · ');
  });

  let expanded = $state(new Set<string>());
  function toggle(model: string) {
    const next = new Set(expanded);
    if (next.has(model)) next.delete(model);
    else next.add(model);
    expanded = next;
  }
</script>

<div class="flex min-h-0 flex-1 flex-col gap-9 overflow-y-auto px-4 pt-8 pb-9 sm:px-10">
  <header class="flex flex-wrap items-end justify-between gap-6">
    <div class="flex flex-col">
      <p class="text-11 text-text-3">Report</p>
      <h1 class="mt-2 text-26 text-text-1">Scorecard</h1>
      <p class="mt-3 text-15 text-text-2">
        Cost per model over the last {days} days · API-equivalent list price.
      </p>
    </div>
    <div class="flex items-center gap-4">
      {#if liveMode}
        <div class="flex items-center gap-2" role="group" aria-label="Time window">
          {#each WINDOWS as w (w)}
            <!-- The window in force is the selected ink, as a selected row is. -->
            <Button
              variant="factory"
              aria-pressed={w === days}
              disabled={loading}
              class={w === days ? 'border-text-5 bg-ink-selected text-text-1' : ''}
              onclick={() => selectWindow(w)}>{w}d</Button
            >
          {/each}
        </div>
      {/if}
      <span class={`text-11 ${liveMode ? 'text-accent-2' : 'text-text-3'}`}>
        {liveMode ? 'kew serve · connected' : 'offline · no data'}
      </span>
    </div>
  </header>

  {#if liveMode && rows.length}
    <!-- Below 640 two cells a row and the third under them, as Runs' readouts
         are: a third of a 390px screen does not hold "$19.22" (#3461). -->
    <section aria-label="Leverage" class="grid grid-cols-2 border-y border-hair sm:grid-cols-3">
      <Readout
        label="Spend"
        tone="money"
        meta={`last ${days}d`}
        stackMeta
        value={gaugeMoney(head.totalSpend)}
        caption="API-equivalent · list price"
        edge="start"
        class="min-w-0 pr-4 sm:pr-7"
      />
      <Readout
        label="Merged"
        value={head.totalMerged}
        caption={hasPRs
          ? 'PRs merged in this window'
          : 'no PRs opened in this window — runs and spend only'}
        divided
        class="min-w-0 pr-0 pl-4 sm:px-7"
      />
      <!-- Cyan, the colour of work in flight, because free inference doing the
           same work is the reading this page exists for. -->
      <Readout
        label="Local models"
        tone="working"
        value={gaugeMoney(head.localSpend)}
        caption="free or local inference, of the spend beside it"
        divided
        edge="end"
        class="col-span-2 min-w-0 border-t border-l-0 border-hair pl-0 sm:col-span-1 sm:border-t-0 sm:border-l sm:pl-7"
      />
    </section>
  {/if}

  <!-- C3/#2301: what the factory delivered, week by week. Above the per-model
       table because the outcome is the reading; the model split is why. -->
  <section aria-label="Weekly outcomes" class="flex flex-col">
    <SectionHead
      label="Outcomes by week"
      meta={outcomeMeta}
    />
    {#if data.outcomes && data.outcomes.weeks.length}
      <OutcomeCharts report={data.outcomes} />
    {:else}
      <!-- A sentence, so data-13 Sans, never the label caps (D44). -->
      <p class="text-13 text-text-3" data-empty-sentence>
        {liveMode
          ? 'The weekly outcome report is not available from this kew serve.'
          : 'Start kew serve to see what the factory delivered week by week.'}
      </p>
    {/if}
  </section>

  <section aria-label="Cost per model" class="flex flex-col">
    <SectionHead
      label="Cost per merged PR"
      meta={rows.length === 0 ? '' : `${rows.length} ${rows.length === 1 ? 'model' : 'models'}`}
    />

    {#if rows.length > 0}
      <!-- Seven columns need 720px of fixed track, so below 1024 the head is
           gone and each row wraps: the model on its own line, then each figure
           under its own label, then the spend bar across the row (#3461). -->
      <div class="hidden lg:contents">
      <DataRow columns={SCORECARD_COLUMNS} head>
        <span>Model</span><span class="text-right">Runs</span><span>Merge rate</span><span
          class="text-right">Reviews</span
        ><span>Spend</span><span class="text-right">$/merged PR</span><span
          class="text-right"
          title="recorded actual ÷ planned bound">Planned vs actual</span
        >
      </DataRow>
      </div>

      {#each rows as r, i (r.model)}
        {@const open = expanded.has(r.model)}
        {@const life = lifecycleNote(r.lifecycle_status)}
        <DataRow
          columns={SCORECARD_COLUMNS}
          rule={i === rows.length - 1 && !open ? 'none' : 'quiet'}
          class={WRAPPED_ROW}
          data-scorecard-row
        >
          <span class="flex min-w-0 basis-full items-center gap-3 lg:basis-auto">
            <!-- A row that expands is a button, not a clickable div: the
                 breakdown has to be reachable from the keyboard. -->
            <button
              type="button"
              class="flex min-w-0 items-center gap-2 text-left"
              aria-expanded={expanded.has(r.model)}
              onclick={() => toggle(r.model)}
            >
              <span class="text-text-5" aria-hidden="true">{open ? '▾' : '▸'}</span>
              <span class="truncate font-mono text-13 text-text-1">{r.model}</span>
            </button>
            {#if isLocalRow(r)}<span class="text-11 text-accent-2">local · unpriced</span>{/if}
            {#if life}<span class={`text-11 ${life.class}`}>{life.label}</span>{/if}
          </span>
          <span class="text-right font-mono text-13 text-text-2"
            ><span class={CELL_LABEL}>runs</span>{r.runs}</span
          >
          <span class="font-mono text-13 text-text-2"
            ><span class={CELL_LABEL}>merge rate</span>{mergeRate(r)}</span
          >
          <span class="text-right font-mono text-13 text-text-2"
            ><span class={CELL_LABEL}>reviews</span>{avgReviews(r)}</span
          >
          <span class="order-last flex basis-full items-center gap-2.5 lg:order-none lg:basis-auto">
            <span class="h-[3px] min-w-0 flex-1 bg-ink-track">
              <span
                class="block h-full"
                style:width={`${spendPct(r.spend)}%`}
                style:background-color={isLocalRow(r) ? 'var(--accent-2)' : 'var(--warn)'}
              ></span>
            </span>
            <span class="font-mono text-13 text-warn">{gaugeMoney(r.spend)}</span>
          </span>
          <span class="text-right font-mono text-13 text-text-2"
            ><span class={CELL_LABEL}>$/merged PR</span>{perMerged(r, gaugeMoney)}</span
          >
          <span class="text-right font-mono text-13 text-text-2"
            ><span class={CELL_LABEL}>planned vs actual</span>{planningRatio(r)}</span
          >
        </DataRow>

        {#if open}
          {#each r.by_complexity as c, j (c.complexity)}
            <DataRow
              columns={SCORECARD_COLUMNS}
              height={50}
              rule={i === rows.length - 1 && j === r.by_complexity.length - 1 ? 'none' : 'quiet'}
              class={`${WRAPPED_ROW} lg:h-[50px]`}
              data-scorecard-row
            >
              <span class="basis-full truncate pl-6 font-mono text-13 text-text-3 lg:basis-auto"
                >· {c.complexity}</span
              >
              <span class="text-right font-mono text-13 text-text-3"
                ><span class={CELL_LABEL}>runs</span>{c.runs}</span
              >
              <span class="font-mono text-13 text-text-3"
                ><span class={CELL_LABEL}>merge rate</span>{mergeRate(c)}</span
              >
              <span class="text-right font-mono text-13 text-text-3"
                ><span class={CELL_LABEL}>reviews</span>{avgReviews(c)}</span
              >
              <span class="hidden lg:block"></span>
              <span class="text-right font-mono text-13 text-text-3"
                ><span class={CELL_LABEL}>$/merged PR</span>{perMerged(c, gaugeMoney)}</span
              >
              <span class="text-right font-mono text-13 text-text-3"
                ><span class={CELL_LABEL}>planned vs actual</span>{planningRatio(c)}</span
              >
            </DataRow>
          {/each}
        {/if}
      {/each}
    {:else}
      <p class="text-13 text-text-3" data-empty-sentence>
        {liveMode ? 'No runs in this window.' : 'Start kew serve to see the scorecard.'}
      </p>
    {/if}
  </section>
</div>
