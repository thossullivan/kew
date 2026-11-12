<script lang="ts">
  /**
   * FUI-27 — the flight recorder, restyled onto the hairline primitives.
   *
   * #3459 (D1c) — the page reads top-down and fits a phone. The header carries
   * the run's identity on one mono line; the safest action sits right under it,
   * a command-block with Copy when a command performs it, else one quiet line
   * with its link; below the event list, Review and route beside Cost from
   * 1024px and one column under it; then Details, Evidence, Changes and the
   * compiled prompt as disclosures whose closed state still says what is in
   * them. Every word still comes from `buildFlightView`, and the protected
   * prompt is still fetched only by the reveal click.
   *
   * Three claims that used to be pill badges are now marks from the one status
   * table (FUI-04), in the evidence vocabulary they belong to: a verified chain
   * is `attested` and an unverified one keeps the warn dashed ring rather than
   * a green check; a host-attested figure is attested where a runner's own
   * claim is unverified; and an exact check that matched is attested, one that
   * mismatched wears the failure X, and one that could not be read is pending.
   * Everything else here is a fact rather than a status — sealed, deleted,
   * revealed — so it stays a word, coloured only where the fact is an alarm.
   */
  import type { Snippet } from 'svelte';
  import { invalidateAll } from '$app/navigation';
  import { Command, CopyButton, SectionHead, StatusMark } from '$lib/ui';
  import RunEvents from '$lib/runs/RunEvents.svelte';
  import { verdictScopeNote } from '$lib/runs/events';
  import { fetchPrompt, type RunAuditEvent } from '$lib/api';
  import {
    buildFlightView,
    canReveal,
    collapsePrompt,
    eventKindLabel,
    loadingPrompt,
    promptSummary,
    refusedPrompt,
    revealedPrompt,
    type PromptPanel
  } from '$lib/flight-recorder';
  import type { RunDetailData } from './+page';
  import { displayStatus } from '$lib/kpi';
  import { reviewHeading, verdictReading } from '$lib/runs/verdict';
  import { timeAgo } from '$lib/utils';
  import { fmtDateTime, utcTitle } from '$lib/time';
  import { formatDuration } from '$lib/time/duration';
  import { workHref } from '$lib/monitor/live-runs';
  import { modelName, reasonLabel } from '$lib/factory/names';
  import { SEP } from '$lib/utils';

  let { data }: { data: RunDetailData } = $props();

  /** What the event list has read since the loader: further pages, the live
   * tail and the run's re-read status. The safest action reads the same. */
  let read = $state<{ events: RunAuditEvent[]; status: string | null } | null>(null);

  const view = $derived(
    buildFlightView({
      runId: data.runId,
      run: data.run && read?.status ? { ...data.run, status: read.status } : data.run,
      flight: data.flight,
      diff: data.diff,
      events: read?.events ?? data.audit?.events ?? null,
      attempt: data.attempt
    })
  );
  const missing = $derived(data.run === null && data.flight === null);
  const itemId = $derived(data.flight?.item_id || data.run?.item_id || '');
  const duration = $derived(
    data.run && data.run.duration_seconds > 0 ? formatDuration(data.run.duration_seconds * 1000) : ''
  );
  /** "from attempt 2" beside a run-row figure when the page shows another attempt. */
  const fromRow = $derived(view.rowAttempt ? `from ${view.rowAttempt}` : '');
  /** "Review · round 1" for a review run, '' for every other kind (D11). */
  const heading = $derived(reviewHeading(data.run));

  // The prompt panel the operator has acted on, if any. `null` means "whatever
  // the evidence says" — the digest-only description, with no bytes. Every
  // refresh resets it to null, so revealed bytes never survive a reload.
  let acted = $state<PromptPanel | null>(null);
  const prompt = $derived(acted ?? view.prompt);

  async function reveal() {
    acted = loadingPrompt(view.prompt);
    try {
      acted = revealedPrompt(view.prompt, await fetchPrompt(data.runId, view.identity.attempt));
    } catch (e) {
      acted = refusedPrompt(view.prompt, e);
    }
  }

  function hide() {
    acted = collapsePrompt(prompt);
  }

  let refreshing = $state(false);

  async function refresh() {
    refreshing = true;
    acted = null; // drop any revealed bytes before re-reading the evidence
    try {
      await invalidateAll();
    } finally {
      refreshing = false;
    }
  }

  /** Which disclosures are open. They all start shut. */
  let open = $state<Record<string, boolean>>({});

  /** A link to a disclosure on this page opens it, rather than landing on its
   * closed summary line. */
  function openTarget(href: string) {
    const id = href.slice(1);
    if (id in DISCLOSURES) open[id] = true;
  }

  /** Shutting the prompt drops any revealed bytes, as "hide the bytes" does:
   * a closed disclosure never holds a prompt nobody can see. */
  function toggle(id: string) {
    if (id === 'prompt' && open[id] && prompt.status === 'revealed') hide();
    open[id] = !open[id];
  }

  const DISCLOSURES: Record<string, true> = { details: true, evidence: true, changes: true, prompt: true };

  /** An exact check, in the evidence vocabulary. */
  const CHECK_MARK: Record<string, string> = {
    match: 'attested',
    mismatch: 'failure',
    unavailable: 'pending'
  };

  /** The quiet instrument control: a hairline box, no fill, 7px control radius. */
  const CONTROL =
    'inline-flex min-h-8 items-center rounded-[7px] border border-hair-strong px-3.5 py-1.5 ' +
    'font-mono text-13 text-text-2 transition-colors hover:border-text-5 hover:text-text-1 ' +
    'disabled:opacity-50';

  /** The host sequence's columns: two lines of three below `sm`, one row from it. */
  const SEQ_GRID =
    'grid grid-cols-[2rem_minmax(0,1fr)_minmax(0,1fr)] gap-x-4 gap-y-1 ' +
    'sm:grid-cols-[2.5rem_minmax(0,1.2fr)_minmax(0,0.9fr)_minmax(0,1.3fr)_minmax(0,0.6fr)_minmax(0,1.4fr)]';
  const SEQ_WRAP = 'col-start-2 sm:col-start-auto';

  const short = (sha: string) => (sha.length > 12 ? `${sha.slice(0, 12)}…` : sha);
  const fmtBytes = (n: number) => `${new Intl.NumberFormat('en-US').format(n)} bytes`;
  const joined = (parts: string[]) => parts.filter(Boolean).join(' · ');
</script>

<!-- One labelled fact over a quiet rule. The value wraps rather than cuts, so
     it is readable whole on a phone, and its title is always the full value.
     `from` names the attempt a run-row fact speaks for when the page shows
     another one ("attempt 2"), beside the label. -->
{#snippet field(label: string, value: string, available: boolean = true, title: string = '', from: string = '')}
  <div class="flex items-baseline justify-between gap-4 border-b border-hair-quiet py-2.5">
    <dt class="shrink-0 text-11 text-text-3">
      {label}{#if from}<span data-row-attempt>{SEP}{from}</span>{/if}
    </dt>
    <dd
      class="min-w-0 text-right font-mono text-13 [overflow-wrap:anywhere]"
      class:text-text-2={available}
      class:text-text-3={!available}
      title={title || value}
    >
      {value}
    </dd>
  </div>
{/snippet}

<!-- A disclosure: shut, its summary line still says what is inside. A button
     over a region rather than a `<details>`, which is the app's menu primitive
     (menus.test.ts): this is a section of the page, not a menu to dismiss. -->
{#snippet disclosure(id: string, label: string, summary: string, body: Snippet)}
  <div {id} class="border-t border-hair" data-disclosure={id}>
    <button
      type="button"
      aria-expanded={!!open[id]}
      aria-controls={`${id}-body`}
      onclick={() => toggle(id)}
      class="flex min-h-11 w-full cursor-pointer flex-wrap items-baseline gap-x-4 gap-y-1 py-3 text-left"
    >
      <span class="shrink-0 text-11 font-medium whitespace-nowrap text-text-2"
        >{label}
        <span aria-hidden="true" class="inline-block transition-transform" class:rotate-90={open[id]}>▸</span></span
      >
      <span data-disclosure-summary class="min-w-0 font-mono text-13 text-text-3 [overflow-wrap:anywhere]"
        >{summary}</span
      >
    </button>
    {#if open[id]}
      <div id={`${id}-body`} class="pt-2 pb-7">{@render body()}</div>
    {/if}
  </div>
{/snippet}

<div class="flex min-h-0 flex-1 flex-col">
  <!-- 16px gutter below sm, as Work and Monitor (#3057); 40px from sm up. -->
  <header class="flex shrink-0 flex-col gap-2.5 border-b border-hair px-4 pt-4 pb-4 sm:px-10">
    <div class="flex flex-wrap items-center gap-4">
      <a href="/runs" class="inline-flex min-h-8 items-center text-11 text-text-3 transition-colors hover:text-text-1"
        >← Runs</a
      >
      <div class="grow"></div>
      {#if view.identity.deepLink}
        <a
          href={view.identity.deepLink.href}
          target="_blank"
          rel="noreferrer"
          class="inline-flex min-h-8 items-center font-mono text-13 text-text-2 underline-offset-4
                 hover:text-text-1 hover:underline">{view.identity.deepLink.label} ↗</a
        >
      {/if}
      <button type="button" class={CONTROL} onclick={refresh} disabled={refreshing}>
        {refreshing ? 'refreshing…' : 'refresh'}
      </button>
    </div>
    <h1 class="line-clamp-2 text-26 break-words text-text-1">{view.identity.title || 'Run detail'}</h1>
    <div class="flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-13 text-text-3" data-run-identity>
      <span class="text-text-2">{view.identity.runId}</span>
      {#if view.identity.itemRef}
        {#if itemId}
          <a
            href={workHref(itemId)}
            class="inline-flex min-h-8 items-center text-text-2 underline-offset-4 hover:text-text-1 hover:underline"
            title={`${view.identity.tracker} ${view.identity.sourceId || itemId}`}>{view.identity.itemRef}</a
          >
        {:else}
          <span>{view.identity.itemRef}</span>
        {/if}
      {/if}
      {#if heading}
        <!-- What a review run WAS (design review c9 D11): its verdict is the
             Review section's, below; the status beside it is the process's. -->
        <span class="text-text-1" data-run-heading>{heading}</span>
      {/if}
      <span>attempt {view.identity.attempt}</span>
      {#if view.identity.status}
        {@const shown = displayStatus({
          status: view.identity.status,
          startedAt: data.run?.started_at
        })}
        {#if heading && !view.outcome}
          <!-- A review run that refused the change still ran cleanly: the
               mark says how the PROCESS ended, and says so, so "success" never
               reads as the verdict. -->
          <span class="inline-flex items-center gap-2" data-run-process>
            <span>process</span>
            <StatusMark kind={shown} vocabulary="run" size={7} word class="gap-2" />
          </span>
        {:else if view.outcome}
          <!-- The run and its publication disagree: the mark keeps the run's
               shape, and the sentence names both outcomes (#2541, D37). -->
          <span class="inline-flex items-center gap-2" data-run-outcome>
            <StatusMark kind={shown} vocabulary="run" size={7} />
            <span class="text-warn">{view.outcome}</span>
          </span>
        {:else}
          <StatusMark kind={shown} vocabulary="run" size={7} word class="gap-2" />
        {/if}
      {/if}
      {#if data.run}
        <span class="text-warn" data-header-cost>{view.cost.amount}</span>
      {/if}
      {#if duration}<span>{duration}</span>{/if}
      {#if fromRow}
        <!-- Status, cost and duration are the run row's, which is the latest
             attempt's: said, so they never read as the attempt above's. -->
        <span data-row-attempt>· {fromRow}</span>
      {/if}
    </div>
  </header>

  {#if missing}
    <div class="px-4 pt-5 pb-9 sm:px-10">
      <SectionHead label="Flight recorder" meta="no evidence" />
      <p class="text-15 text-text-2">
        This server has no run with the id <span class="font-mono text-text-1">{data.runId}</span>.
        It may belong to another checkout, or
        <span class="font-mono text-text-1">kew serve</span> may not be running.
      </p>
    </div>
  {:else}
    <div class="min-h-0 flex-1 overflow-y-auto px-4 pb-9 sm:px-10" data-run-content>
      <!-- Safest action: the one next step, derived centrally from the evidence
           so this page never invents a verb the facts do not support. Magenta
           only when a person is the next move: a command they must run. -->
      <section
        aria-label="Safest action"
        class="border-b border-hair py-[18px]"
        data-needs-you={view.safestAction.command ? '' : undefined}
      >
        <div class:border-l-2={!!view.safestAction.command} class:border-accent={!!view.safestAction.command} class:pl-4={!!view.safestAction.command}>
          <SectionHead label="Safest action">
            {#if view.safestAction.blockedBy}
              <!-- A block is a warning, and the dimension id alone does not say
                   what it is: the head's meta slot keeps both the words and the
                   tone the pre-factory badge carried. -->
              <span class="text-warn" title={view.safestAction.blockedBy}
                >blocked by · {reasonLabel(view.safestAction.blockedBy)}</span
              >
            {:else if fromRow}
              {fromRow}
            {/if}
          </SectionHead>
          <p class="text-15 text-text-1">
            {view.safestAction.action}
            {#if !view.safestAction.command && view.safestLink}
              {@const link = view.safestLink}
              {#if link.external}
                <a
                  href={link.href}
                  target="_blank"
                  rel="noreferrer"
                  data-safest-link
                  class="ml-2 inline-flex min-h-8 items-center font-mono text-13 text-text-2 underline-offset-4 hover:text-text-1 hover:underline"
                  >{link.label} ↗</a
                >
              {:else}
                <a
                  href={link.href}
                  data-safest-link
                  onclick={() => link.href.startsWith('#') && openTarget(link.href)}
                  class="ml-2 inline-flex min-h-8 items-center font-mono text-13 text-text-2 underline-offset-4 hover:text-text-1 hover:underline"
                  >{link.label} →</a
                >
              {/if}
            {/if}
          </p>
          {#if view.safestAction.reason}
            <p class="mt-1.5 text-13 text-text-3">{view.safestAction.reason}</p>
          {/if}
          {#if view.safestAction.command}
            <!-- The command that performs the action (#3057), computed from the
                 run's own id — never a template the reader has to fill in. The
                 row wraps so a failed copy's line drops under the command. -->
            <div
              class="mt-3 flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-[7px] border
                     border-hair-box bg-ink-raised px-3 py-2"
              data-command-block
            >
              <span aria-hidden="true" class="shrink-0 font-mono text-13 text-text-5">$</span>
              <span class="min-w-0 flex-1 font-mono text-13 text-text-1" data-safest-command
                ><Command command={view.safestAction.command} /></span
              >
              <CopyButton
                text={view.safestAction.command}
                label="Copy the command"
                variant="needs-you"
                data-copy-command
                class="shrink-0 rounded-[7px]"
              />
            </div>
          {/if}
        </div>
      </section>

      <!-- #2300 — the run's own clock and its ledger events, the split this
           page is opened at from a Gantt bar. -->
      <RunEvents
        runId={data.runId}
        run={data.run}
        first={data.audit}
        onread={(next) => (read = next)}
      />

      <div class="mt-9 grid grid-cols-1 gap-10 lg:grid-cols-2" data-run-columns>
        <section aria-label="Review and route" id="review" class="min-w-0">
          <!-- #2300 — the attested verdict standing over this run's work, read
               from the `adversarial_review_verdict` fact. No verdict is an
               answer ("none recorded"), never a blank the reader has to guess
               at; the tone comes from the console's one verdict mapping, so a
               READY_WITH_RESIDUALS round is not painted as a setback. WHOSE
               decision it is is said out loud by the scope note. -->
          <!-- No section-wide attempt note: the verdict and the route claim
               are the run row's and say so, the runner's report is the
               attempt on the page's own evidence and does not. -->
          <SectionHead label="Review and route" />
          {#if data.run?.verdict}
            {@const v = data.run.verdict}
            {@const reading = verdictReading(v.verdict)}
            <!-- `data-tour`: the demo's refused-PR tour points at a send-back
                 and at the READY after it (#3148). Inert in the console. -->
            <!-- Design review c9 D11: the verdict is a status mark from the one
                 table plus plain words ("Changes requested · round 1"); the
                 recorded code follows them as quiet mono detail, never as the
                 headline. -->
            <div class="flex flex-wrap items-center gap-x-5 gap-y-1" data-tour="review-verdict">
              <span class="inline-flex min-w-0 items-center gap-2.5" data-verdict-reading>
                <StatusMark kind={reading.kind} size={10} label={reading.words} />
                <span class="text-15 text-text-1" data-verdict-words>{reading.words}{SEP}round {v.round}</span>
              </span>
              <!-- The code is a status word: one line, an ellipsis if a newer
                   server sends one wider than the row, and the hook the 390
                   sweep and the demo tour read (#2951). -->
              <span
                data-status-word
                class="max-w-full min-w-0 overflow-hidden font-mono text-13 text-ellipsis whitespace-nowrap text-text-3"
                title={v.verdict}>{v.verdict}</span
              >
              <span class="font-mono text-13 text-text-3" title={utcTitle(v.ts)}>{timeAgo(v.ts)}</span>
              {#if fromRow}
                <span class="font-mono text-13 text-text-3" data-row-attempt>{fromRow}</span>
              {/if}
            </div>
            <p class="mt-1.5 text-13 text-text-3">{verdictScopeNote(v.scope)}</p>
            <dl class="mt-3">
              {@render field('reviewed head', v.head_sha ? short(v.head_sha) : 'unavailable', !!v.head_sha, v.head_sha ?? '')}
              {@render field(
                'reviewer model',
                v.reviewer_model ? modelName(v.reviewer_model) : 'unavailable',
                !!v.reviewer_model,
                v.reviewer_model ?? ''
              )}
            </dl>
          {:else}
            <p class="text-13 text-text-3">
              no governed-review verdict is recorded for this work item.
            </p>
          {/if}
          <dl class="mt-5">
            {@render field(
              'route claimed',
              view.route.claimed || 'unavailable',
              !!view.route.claimed,
              view.route.claimed ? `${view.route.claimed} — ${view.route.claimedProvenance}` : '',
              view.route.claimed ? view.rowAttempt : ''
            )}
            <div class="flex items-baseline justify-between gap-4 border-b border-hair-quiet py-2.5">
              <dt class="shrink-0 text-11 text-text-3">runner reported</dt>
              <dd class="flex min-w-0 flex-wrap items-center justify-end gap-x-3 gap-y-1 font-mono text-13">
                <span
                  class="[overflow-wrap:anywhere]"
                  class:text-text-2={view.route.reportedAvailable}
                  class:text-text-3={!view.route.reportedAvailable}
                  >{view.route.reportedAvailable ? view.route.reported : 'unavailable'}</span
                >
                <StatusMark
                  kind={view.route.attested ? 'attested' : 'unverified'}
                  size={7}
                  word
                  label={view.route.attested ? 'host-attested' : 'runner claim'}
                  class="gap-2"
                />
              </dd>
            </div>
          </dl>
          <p class="mt-2 text-13 text-text-3">{view.route.detail}</p>
        </section>

        <!-- `data-tour`: the demo's spend tour starts on this run's list cost
             (#3149), so the amount is the section's first figure. -->
        <section aria-label="Cost" data-tour="run-cost" class="min-w-0">
          <SectionHead label="Cost" meta={view.cost.meta} />
          <!-- The amount is the run row's: with another attempt on the page it
               says which attempt it is from, as each run-row figure below does
               in its source. -->
          <div class="flex flex-wrap items-baseline gap-x-3">
            <div class="font-mono text-26 text-warn" data-cost-amount>{view.cost.amount}</div>
            {#if data.run && fromRow}
              <span class="font-mono text-13 text-text-3" data-row-attempt>{fromRow}</span>
            {/if}
          </div>
          <p class="mt-1.5 text-13 text-text-3" data-cost-pricing>{view.cost.pricing}</p>
          {#if data.run?.accounting_status}
            <p class="mt-1 font-mono text-13 text-text-3">
              accounting · {data.run.accounting_status}{#if fromRow}<span data-row-attempt>{SEP}{view.rowAttempt}</span
                >{/if}
            </p>
          {/if}
          <!-- Every figure names its source. The planned row is the run row's
               `planned_cost_usd`, 'not recorded' when the column is null. A
               value wraps rather than pushing the page sideways at 390. -->
          {#if view.cost.figures.length}
            <table class="mt-3 w-full border-collapse text-left" data-cost-figures>
              <thead>
                <tr class="border-b border-hair text-11 text-text-3">
                  <th scope="col" class="py-1.5 pr-3 font-normal">figure</th>
                  <th scope="col" class="py-1.5 pr-3 text-right font-normal">value</th>
                  <th scope="col" class="py-1.5 font-normal">source</th>
                </tr>
              </thead>
              <tbody>
                {#each view.cost.figures as f (f.figure + f.source)}
                  <tr class="border-b border-hair-quiet align-baseline" data-cost-figure>
                    <td class="py-1.5 pr-3 text-13 text-text-3">{f.figure}</td>
                    <td
                      class="py-1.5 pr-3 text-right font-mono text-13 [overflow-wrap:anywhere]"
                      class:text-text-2={f.available}
                      class:text-text-3={!f.available}>{f.value}</td
                    >
                    <td class="py-1.5 font-mono text-[11px] text-text-3 [overflow-wrap:anywhere]" data-cost-source
                      >{f.source}</td
                    >
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}
          {#if view.cost.runnerSilent}
            <p class="mt-2 text-13 text-text-3" title={view.cost.detail || undefined} data-runner-silent>
              {view.cost.runnerSilent}
            </p>
          {/if}
        </section>
      </div>

      <div class="mt-9 border-b border-hair">
        {#snippet details()}
          <dl class="grid grid-cols-1 gap-x-10 lg:grid-cols-2">
            {@render field(
              'source id',
              view.identity.sourceId ? `${view.identity.tracker} · ${view.identity.sourceId}` : 'unavailable',
              !!view.identity.sourceId
            )}
            {@render field('branch', view.identity.branch || 'unavailable', !!view.identity.branch)}
            {@render field('run kind', data.run?.run_kind || 'not recorded', !!data.run?.run_kind)}
            {@render field('provider', data.run?.provider || 'not recorded', !!data.run?.provider)}
            {@render field('transport', data.run?.transport_mode || 'not recorded', !!data.run?.transport_mode)}
            {@render field(
              'runner reported model',
              data.run?.reported_model || 'not recorded',
              !!data.run?.reported_model
            )}
          </dl>
          {#if fromRow}
            <p class="mt-2 text-13 text-text-3">kind, provider, transport and model are {fromRow}'s run row.</p>
          {/if}
        {/snippet}
        {@render disclosure(
          'details',
          'Details',
          joined([view.identity.branch, data.run?.provider ?? '', data.run?.transport_mode ?? '']) || 'nothing recorded',
          details
        )}

        {#snippet evidence()}
          <section aria-label="Lineage">
            <SectionHead label="Artifact lineage" meta={view.evidence.sealed ? 'sealed' : 'unsealed'} />
            <dl class="grid grid-cols-1 gap-x-10 lg:grid-cols-2">
              {#each view.lineage as row (row.label)}
                {@render field(
                  row.label,
                  row.available ? short(row.value) : 'unavailable',
                  row.available,
                  row.available ? row.value : row.detail
                )}
              {/each}
            </dl>
          </section>

          <section aria-label="Exact checks" class="mt-6 flex flex-col gap-2">
            <div class="text-11 text-text-3">exact checks</div>
            {#each view.checks as c (c.label)}
              <div class="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-13 text-text-3">
                <StatusMark
                  kind={CHECK_MARK[c.status] ?? 'pending'}
                  size={7}
                  word
                  label={c.status}
                  class="gap-2"
                />
                <span class="text-text-2">{c.label}</span>
                {#if c.expected && c.actual}
                  <span class="[overflow-wrap:anywhere]" title={`${c.expected} vs ${c.actual}`}
                    >{short(c.expected)} vs {short(c.actual)}</span
                  >
                {/if}
                {#if c.detail}<span>· {c.detail}</span>{/if}
              </div>
            {/each}
          </section>

          <section aria-label="Host sequence" class="mt-7">
            <SectionHead label="Host sequence">
              <StatusMark
                kind={view.evidence.chainVerified ? 'attested' : 'unverified'}
                size={7}
                word
                label={view.evidence.chainVerified ? 'chain verified' : 'chain unverified'}
                class="gap-2 font-mono text-[11px]"
              />
            </SectionHead>
            {#if view.evidence.events.length}
              <div class={`${SEQ_GRID} border-b border-hair py-2 text-11 text-text-3`} data-sequence-head>
                <span>seq</span>
                <span>kind</span>
                <span>seal</span>
                <span class={SEQ_WRAP}>producer</span>
                <span>by</span>
                <span class={SEQ_WRAP}>time</span>
              </div>
            {/if}
            {#each view.evidence.events as e (e.seq)}
              <div
                class={`${SEQ_GRID} items-baseline border-b border-hair-quiet py-2.5 font-mono text-13 text-text-3`}
                data-sequence-row
              >
                <span>{e.seq}</span>
                <span class="text-text-1 [overflow-wrap:anywhere]" title={e.kind}>{eventKindLabel(e.kind)}</span>
                <span>{e.sealed ? 'sealed' : 'after the seal'}</span>
                <span class={`${SEQ_WRAP} text-text-2 [overflow-wrap:anywhere]`}
                  >{e.producer_name} {e.producer_version}</span
                >
                <span>{e.producer_kind}</span>
                <span class={SEQ_WRAP} title={utcTitle(e.timestamp)}>{fmtDateTime(e.timestamp)}</span>
              </div>
            {:else}
              <p class="text-13 text-text-3">this run recorded no evidence events.</p>
            {/each}
          </section>

          <section aria-label="Gaps" class="mt-7">
            <SectionHead label="Gaps" meta={`${view.gaps.length}`} />
            {#each view.gaps as g (`${g.kind}:${g.seq}:${g.event}`)}
              <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-hair-quiet py-2.5 font-mono text-13">
                <span class:text-danger={g.kind === 'gap'} class:text-text-3={g.kind !== 'gap'}
                  >{g.kind === 'gap' ? `seq ${g.seq}` : g.event}</span
                >
                {#if g.kind === 'gap'}<span class="text-text-2">{g.event}</span>{/if}
                <span class="text-text-3">{g.reason}</span>
              </div>
            {:else}
              <p class="text-13 text-text-3">every dimension of this run's evidence was readable.</p>
            {/each}
          </section>
        {/snippet}
        {@render disclosure('evidence', 'Evidence', view.evidenceSummary, evidence)}

        {#snippet changes()}
          <section aria-label="Sealed change set">
            <SectionHead label="Sealed change set" meta={view.diff.nodeId || 'no node'} />
            {#if view.diff.available}
              <div class="flex flex-col gap-2">
                {#each view.diff.files as f (f.path)}
                  <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-13">
                    <span class:text-danger={f.change === 'deleted'} class:text-text-3={f.change !== 'deleted'}
                      >{f.change}</span
                    >
                    <span class="text-text-1 [overflow-wrap:anywhere]">{f.path}</span>
                    <span class="text-text-3">{fmtBytes(f.byteLength)}</span>
                    {#if f.textAvailability !== 'available'}
                      <span class="text-text-3">· {f.detail || 'text unavailable'}</span>
                    {/if}
                  </div>
                {:else}
                  <p class="text-13 text-text-3">the sealed package accounts for no paths.</p>
                {/each}
              </div>
              <pre
                class="mt-4 max-h-80 overflow-auto border border-hair bg-ink-raised p-4 font-mono
                       text-13 text-text-2">{view.diff.text}</pre>
            {:else}
              <p class="text-13 text-text-3">{view.diff.detail}</p>
            {/if}
          </section>
        {/snippet}
        {@render disclosure('changes', 'Changes', view.diff.summary, changes)}

        <!-- The protected prompt. The bytes are never part of a load, a preload,
             a persisted store or any background reporting: the only call site is
             the reveal click below, and collapsing drops what it returned. -->
        {#snippet compiled()}
          <section aria-label="Compiled prompt">
            <dl class="max-w-[520px]">
              {@render field(
                'digest',
                prompt.digest ? short(prompt.digest) : 'unavailable',
                !!prompt.digest,
                prompt.digest ?? ''
              )}
              {@render field('size', prompt.byteLength ? fmtBytes(prompt.byteLength) : '—', !!prompt.byteLength)}
              {@render field(
                'retention',
                prompt.expiresAt
                  ? prompt.expired
                    ? 'expired'
                    : `until ${fmtDateTime(prompt.expiresAt)}`
                  : 'not set',
                !!prompt.expiresAt,
                prompt.expiresAt ? utcTitle(prompt.expiresAt) : ''
              )}
            </dl>
            {#if prompt.detail}
              <p class="mt-3 text-13 text-text-3">{prompt.detail}</p>
            {/if}
            <div class="mt-4 flex flex-wrap items-center gap-4">
              {#if prompt.status === 'revealed'}
                <button type="button" class={CONTROL} onclick={hide}>hide the bytes</button>
              {:else}
                <button
                  type="button"
                  class={CONTROL}
                  onclick={reveal}
                  disabled={!canReveal(prompt) || prompt.status === 'loading'}
                >
                  {prompt.status === 'loading' ? 'reading…' : 'show the exact prompt'}
                </button>
              {/if}
              <span class="font-mono text-[11px] text-text-3">
                read once, never cached — needs a signed-in session and a local server
              </span>
            </div>
            {#if prompt.status === 'revealed' && prompt.text !== null}
              <pre
                class="mt-4 max-h-96 overflow-auto border border-hair bg-ink-raised p-4 font-mono
                       text-13 whitespace-pre-wrap text-text-2">{prompt.text}</pre>
            {/if}
          </section>
        {/snippet}
        {@render disclosure('prompt', 'Compiled prompt', promptSummary(prompt), compiled)}
      </div>
    </div>
  {/if}
</div>
