<script lang="ts">
  /**
   * FUI-27 — the runs list, restyled onto the hairline primitives.
   *
   * Three readouts in DMonitor's readout grammar (y ≈88–258) over one table in
   * its live-runs grammar (y ≈294–440): an 11px head row, 56px rows on a quiet
   * rule, the state as a mark and its word. The day groups keep their subtotal,
   * drawn as a section head rather than as a card of their own.
   *
   * No reading changed. The filter, the grouping, the KPIs, the cache and diff
   * stats and the route claims all come from the same modules they did before —
   * in particular the run link, which is resolved by `$lib/run-links` so this
   * page never builds a second path to the run detail (run-links.test.ts).
   */
  import { StatusMark, Readout, SectionHead } from '$lib/ui';
  import { CLAIM_TONE_TEXT, dataRowClasses, TAP_PILL } from '$lib/factory/classes';
  import type { Run } from '$lib/api';
  import { filterRuns, STATUS_LABELS, type RunStatus } from '$lib/runs-filter';
  import { runKpis, displayStatus } from '$lib/kpi';
  import { FAILURE_STATUSES } from '$lib/run-status';
  import { groupRunsByDay, cacheHitPct, diffStat } from '$lib/runs-detail';
  import { CLAIM_TITLE, configuredRoute, routeClaims, runLink } from '$lib/run-links';
  import { timeAgo } from '$lib/utils';
  import { safeHref } from '$lib/safe-href';
  import { errorCategoryLabel, modelName } from '$lib/factory/names';

  let { data }: { data: { runs: Run[] | null } } = $props();

  const liveMode = $derived(data.runs != null);
  const runs = $derived<Run[]>(data.runs ?? []);
  let query = $state('');
  let statusFilter = $state<RunStatus>('all');
  let selectedId = $state<string | null>(null);
  const shown = $derived(filterRuns(runs, query, statusFilter));
  const days = $derived(groupRunsByDay(shown));

  // Terminal failure statuses — the shared set (C3/#2296), not a fourth
  // private copy. The local one omitted `refused` and `budget_stop`, so those
  // runs showed no error line at all.
  const fmtInt = (n: number) => new Intl.NumberFormat('en-US').format(n);
  const fmtDur = (s: number) => {
    if (!s || s < 0) return '—';
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    const rem = Math.round(s % 60);
    return rem ? `${m}m ${rem}s` : `${m}m`;
  };

  const kpis = $derived(runKpis(runs));
  const totalCost = $derived(runs.reduce((a, r) => a + (r.cost_usd || 0), 0));

  const fmtUSD = (n: number) => '$' + n.toFixed(2);

  // 636px of fixed track and gap before the title gets any: the table is
  // six columns from 1024 up and a two-line row below it (DESIGN.md
  // "Responsive": a fold is read off the sum, not off the phone that showed it).
  const COLUMNS = '64px minmax(0, 1fr) 156px 96px 104px 116px';
</script>

<div class="flex min-h-0 flex-1 flex-col">
  <header
    class="flex shrink-0 flex-wrap items-baseline gap-4 border-b border-hair px-4 pt-5 pb-[18px] sm:px-10"
  >
    <h1 class="text-26 text-text-1">Runs</h1>
    <span class="text-11 text-text-3">fleet history</span>
    <div class="grow"></div>
    <span class="font-mono text-13 text-text-3">
      {liveMode ? 'kew serve · connected' : 'offline · no data'}
    </span>
  </header>

  <div class="min-h-0 flex-1 overflow-y-auto px-4 pt-7 pb-9 sm:px-10">
    <!-- Three across from 640 up; below it two over one, as Monitor's four
         fold to 2×2 (#3457): at 390 a third of the screen ran "SUCCESS RATE"
         into "TOTAL COST". The third cell spans the row so its rule does. -->
    <section aria-label="Readouts" class="grid grid-cols-2 border-y border-hair sm:grid-cols-3">
      <Readout
        label="Runs"
        value={kpis.total || '—'}
        caption="last 50 runs"
        edge="start"
        class="min-w-0 pr-4 sm:pr-7"
      />
      <Readout
        label="Success rate"
        value={kpis.successRate == null ? '—' : `${kpis.successRate}%`}
        caption="of terminal runs"
        divided
        class="min-w-0 pr-0 pl-4 sm:px-7"
      />
      <Readout
        label="Total cost"
        tone="money"
        value={runs.length ? fmtUSD(totalCost) : '—'}
        caption="last 50 runs"
        divided
        edge="end"
        class="col-span-2 min-w-0 border-t border-l-0 border-hair pl-0 sm:col-span-1 sm:border-t-0
               sm:border-l sm:pl-7"
      />
    </section>

    <div class="mt-9 mb-6 flex flex-wrap items-center gap-5">
      <label class="sr-only" for="runs-search">Filter runs</label>
      <input
        id="runs-search"
        type="search"
        bind:value={query}
        placeholder="Filter by # or title"
        class="h-11 min-w-[260px] flex-1 rounded-[4px] border border-hair-strong bg-transparent
               px-3.5 font-mono text-13 text-text-1 placeholder:text-text-3
               hover:border-text-5 focus:border-text-5 focus:outline-none"
      />
      <!-- A narrowed list is marked in --text-1, the way the header marks its
           active tab: magenta is rationed to a decision a human has to make. -->
      <div class="flex items-center gap-5 font-mono text-[11px]">
        {#each STATUS_LABELS as f (f)}
          <button
            type="button"
            aria-pressed={statusFilter === f}
            onclick={() => (statusFilter = f)}
            class={`inline-flex items-center transition-colors hover:text-text-1 ${TAP_PILL}`}
            class:text-text-1={statusFilter === f}
            class:text-text-3={statusFilter !== f}>{f}</button
          >
        {/each}
      </div>
      <span class="font-mono text-[11px] text-text-3">{shown.length}/{runs.length}</span>
    </div>

    {#if days.length > 0}
      <div
        data-runs-head
        class={dataRowClasses({ head: true, class: 'hidden lg:grid' })}
        style:grid-template-columns={COLUMNS}
      >
        <span>Item</span><span>Run</span><span>Runner</span><span class="text-right">Cost</span>
        <span class="text-right">Started</span><span class="text-right">State</span>
      </div>

      {#each days as g (g.day)}
        <div class="mt-7 first:mt-5">
          <SectionHead label={g.label} meta={fmtUSD(g.subtotal)} as="h2" />
        </div>
        {#each g.runs as r, i (r.run_id)}
          {@const failed = FAILURE_STATUSES.has(r.status)}
          {@const open = selectedId === r.run_id}
          {@const cache = cacheHitPct(r)}
          {@const diff = diffStat(r)}
          {@const ds = displayStatus({ status: r.status, startedAt: r.started_at })}
          {@const link = runLink({ runId: r.run_id, issueNum: r.issue_num })}
          {@const claims = routeClaims(configuredRoute(r.runner, r.model))}
          <!--
            Six columns from 1024 up; two lines below it, as Monitor's live runs
            (#3457): the item, its runner and its cost, then when it started and
            how it ended. The same six cells either way, in the heads' order for
            a screen reader, placed by `order` over a wrapping flex row. The
            title is the one cell that gives way.
          -->
          <button
            type="button"
            aria-expanded={open}
            onclick={() => (selectedId = open ? null : r.run_id)}
            class={dataRowClasses({
              rule: open || i === g.runs.length - 1 ? 'none' : 'quiet',
              class:
                'flex h-auto w-full flex-wrap items-baseline gap-x-3 gap-y-1.5 py-3 text-left ' +
                'transition-colors hover:bg-ink-selected lg:grid lg:h-14 lg:items-center lg:gap-5 lg:py-0'
            })}
            class:bg-ink-selected={open}
            style:grid-template-columns={COLUMNS}
          >
            <span class="order-1 shrink-0 font-mono text-13 text-text-3 lg:order-none"
              >#{r.issue_num}</span
            >
            <span
              class="order-2 min-w-0 flex-1 truncate text-15 lg:order-none"
              class:text-text-1={open}
              class:text-text-2={!open}
              data-run-title>{r.title}</span
            >
            <span
              class="order-3 max-w-[40%] truncate text-13 text-text-2 lg:order-none lg:max-w-none"
              title={r.model || undefined}>{modelName(r.model ?? '')}</span
            >
            {#if r.channel === 'local'}
              <span class="order-4 text-right font-mono text-13 text-text-3 lg:order-none"
                >unpriced</span
              >
            {:else}
              <span class="order-4 text-right font-mono text-13 text-text-2 lg:order-none"
                >{fmtUSD(r.cost_usd)}</span
              >
            {/if}
            <span class="order-6 font-mono text-13 text-text-3 lg:order-none lg:text-right"
              >{timeAgo(r.started_at)}</span
            >
            <span class="order-7 ml-auto flex justify-end font-mono text-13 lg:order-none lg:ml-0">
              <StatusMark kind={ds} vocabulary="run" size={7} word class="gap-2" />
            </span>
            <!-- The line break, below `lg` only. -->
            <span class="order-5 w-full lg:hidden" aria-hidden="true"></span>
          </button>

          {#if open}
            <div
              class="flex flex-col gap-3 bg-ink-selected px-1 pt-1 pb-4 font-mono text-13
                     text-text-3"
              class:border-b={i !== g.runs.length - 1}
              class:border-hair-quiet={i !== g.runs.length - 1}
            >
              <div class="flex flex-wrap items-center gap-x-5 gap-y-2">
                <!-- The one deep view: /runs/[id] owns the flight recorder, so
                     this drawer links to it through the shared resolver instead
                     of growing a second recorder home of its own. -->
                {#if link.available}
                  <a
                    href={link.href}
                    title={link.title}
                    class="text-text-1 underline-offset-4 hover:underline">{link.label} →</a
                  >
                {:else}
                  <span>{link.reason}</span>
                {/if}
                {#if r.pr_url}
                  <!-- A served URL is a link only through safeHref (C10). -->
                  {#if safeHref(r.pr_url)}
                    <a
                      href={safeHref(r.pr_url)}
                      target="_blank"
                      rel="noreferrer"
                      class="text-text-1 underline-offset-4 hover:underline">view PR ↗</a
                    >
                  {:else}
                    <span class="break-all">{r.pr_url}</span>
                  {/if}
                  {#if r.pr_state}<span>{r.pr_state}</span>{/if}
                {/if}
                {#if diff}<span>{diff}</span>{/if}
                {#if r.attempt_number > 1}<span class="text-warn">attempt {r.attempt_number}</span
                  >{/if}
                {#if r.complexity}<span>{r.complexity}</span>{/if}
              </div>

              <div class="flex flex-wrap gap-x-6 gap-y-1.5">
                <span>duration <span class="text-text-2">{fmtDur(r.duration_seconds)}</span></span>
                <span>in <span class="text-text-2">{fmtInt(r.input_tokens)}</span></span>
                <span>out <span class="text-text-2">{fmtInt(r.output_tokens)}</span></span>
                <span
                  >cache read <span class="text-text-2">{cache == null ? '—' : cache + '%'}</span
                  ></span
                >
                {#if r.branch}<span>branch <span class="text-text-2">{r.branch}</span></span>{/if}
              </div>

              {#if failed && r.error_category}
                <!-- The worded category leads; the recorded code stays as
                     secondary detail and wraps rather than clipping at phone
                     width. -->
                <div
                  class="flex min-w-0 flex-wrap gap-x-2 text-danger"
                  title={r.error_category}
                  data-run-error
                >
                  <span>error · {errorCategoryLabel(r.error_category)}</span>
                  <span class="min-w-0 break-all text-text-4" data-run-error-code
                    >{r.error_category}</span
                  >
                </div>
              {/if}

              <!-- Route claims (I25): what the lane was configured to be, what
                   is proven about it right now, and whether a journey
                   certificate stands behind it — three claims, never folded
                   into one another. This list carries configuration only, so
                   the other two read unverified/uncertified rather than
                   borrowing confidence from the run having finished. The claim's
                   own tone colours its label (CLAIM_TONE_TEXT) — the factory has
                   no pill to carry it, but a danger-toned claim may not read as
                   a success-toned one. -->
              <div
                class="flex flex-wrap gap-x-6 gap-y-1.5 border-t border-hair-quiet pt-3
                       text-[11px]"
              >
                {#each claims.all as claim (claim.kind)}
                  <span class="flex items-center gap-1.5" title={claim.detail}>
                    <span>{CLAIM_TITLE[claim.kind]}</span>
                    <span class={CLAIM_TONE_TEXT[claim.tone]}>{claim.label}</span>
                    <span>evidence {claim.evidence}</span>
                  </span>
                {/each}
              </div>
            </div>
          {/if}
        {/each}
      {/each}
    {:else}
      <p class="py-16 text-center text-13 text-text-3">
        {liveMode && runs.length > 0
          ? 'no runs match.'
          : liveMode
            ? 'no runs recorded yet.'
            : 'start kew serve to see runs.'}
      </p>
    {/if}
  </div>
</div>
