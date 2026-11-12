<script lang="ts">
  /**
   * What each version of a role's prompt did, on the Prompts page (#3026):
   * one row per active or retired version (and any other one something ran
   * under), newest first, the built-in prompt last, read from
   * `GET /api/prompts/{role}/outcomes`.
   *
   * An implementer role shows the items its runs worked on, first-pass
   * approval (the first review round after the version's run was ready), mean
   * rounds to ready and the median cost of a merged item; the
   * reviewer shows how many rounds it returned each verdict on, one column per
   * verdict, so the counts line up down the table. A version under the server's
   * `min_items` shows a sentence in place of its rates. Numbers are mono and
   * right-aligned; there is no chart. Only the newest read for the role shown
   * lands, so a slow answer for a role the pane has left is dropped.
   *
   * The section is a size container, and while it is narrower than `@3xl`
   * (48rem) the reviewer's table gives way to one block per version: a
   * column per verdict outgrows the pane once five or more are served, and
   * the pane's width is not the viewport's (from `md` up the page is a 400px
   * aside beside it, so a 768px window leaves the section 288px). The table
   * with seven verdicts needs 739px, so it shows only where it fits; a 1440
   * window gives the section 960px. A block's first line is the version (the table's version cell)
   * with its rounds, labelled as the table's head labels them; under it the
   * verdicts wrap as pairs, word then count, and a pair never breaks inside.
   * Both layouts read their counts from `verdictCells` over `verdictColumns`.
   *
   * `revision` is the page's count of profile decisions: each one (an
   * approval, a decline, a retirement, a proposal, a reload) reads the table
   * again, so a version's state and a newly approved version's row follow the
   * decision. The table on screen stays until the new read lands. An answer
   * with a null `generation` (#3089) is a table a decision interrupted on
   * every server read: its states and counts may come from two decisions, so
   * it is never drawn, and the pane says the profile changed.
   *
   * `historyGeneration` is the generation of the versions the editor shows
   * (undefined while its read is in flight, null when it has none). The table
   * is drawn only when its own `generation` is that one (`tableAgreement`):
   * a decision committed between the two reads (a CLI approval) would
   * otherwise pair one generation's table with another's history. While they
   * differ the pane says so and `onstale` reads the page again, a bounded
   * number of times. The section head's count is the drawn table's, so it
   * never sits beside a sentence saying no version can be compared.
   */
  import { untrack } from 'svelte';
  import { fetchPromptOutcomes, type PromptOutcomes } from '$lib/api';
  import { SectionHead } from '$lib/ui';
  import { readFailure } from '$lib/work/evidence';
  import {
    implementerCells,
    OUTCOMES_DIVERGED,
    OUTCOMES_REREAD,
    tableAgreement,
    tooFew,
    verdictCells,
    verdictColumns,
    verdictLabel
  } from './outcomes';

  let {
    role,
    revision = 0,
    historyGeneration,
    onstale
  }: {
    role: string;
    revision?: number;
    /** The editor's versions generation: undefined while read, null when it has none. */
    historyGeneration: number | null | undefined;
    /** Asks the page to read its versions and this table again. */
    onstale: () => void;
  } = $props();

  let read = $state<PromptOutcomes | null>(null);
  let failure = $state<string | null>(null);
  /** An outcomes read is in flight. */
  let pending = $state(false);
  /** The last table drawn as current: it stays while a read is in flight. */
  let agreed = $state<PromptOutcomes | null>(null);
  /** Re-reads made because the table and the versions named two generations. */
  let attempts = $state(0);

  $effect(() => {
    const of = role;
    void revision;
    // A re-read of the role shown keeps its table until the answer lands;
    // another role starts from nothing.
    if (untrack(() => read?.role) !== of) {
      read = null;
      agreed = null;
      attempts = 0;
    }
    failure = null;
    pending = true;
    let current = true;
    fetchPromptOutcomes(of)
      .then((answer) => {
        if (!current) return;
        if (answer.generation === null) {
          read = null;
          agreed = null;
          failure = `The ${of} profile changed while its outcomes were being read`;
        } else read = answer;
      })
      .catch((err) => {
        if (current) failure = readFailure(err, `The ${of} outcomes`);
      })
      .finally(() => {
        if (current) pending = false;
      });
    return () => {
      current = false;
    };
  });

  const answer = $derived(read !== null && read.role === role ? read : null);
  const agreement = $derived(
    answer === null || answer.generation === null
      ? null
      : tableAgreement({ outcomes: answer.generation, history: historyGeneration, pending, attempts })
  );
  // Runs when the agreement or the answer changes, not when `attempts` alone
  // moves (the agreement it feeds stays `reread`), so each mismatch asks once.
  $effect(() => {
    const next = agreement;
    const landed = answer;
    untrack(() => {
      if (next === 'draw') {
        attempts = 0;
        agreed = landed;
      } else if (next === 'reread') {
        attempts += 1;
        onstale();
      }
    });
  });
  /** A re-read the generations asked for has not agreed yet: no table is current. */
  const rereading = $derived(agreement === 'reread' || (agreement === 'wait' && attempts > 0));
  /**
   * The table drawn: the answer once it agrees, and the last agreed one while
   * an ordinary read (a decision's) is in flight, never during a re-read.
   */
  const shown = $derived(
    failure !== null || rereading
      ? null
      : agreement === 'draw'
        ? answer
        : agreement === 'wait' && agreed !== null && agreed.role === role
          ? agreed
          : null
  );
  /** The reviewer table's verdict columns, one count per cell. */
  const verdicts = $derived(shown?.kind === 'reviewer' ? verdictColumns(shown.versions) : []);

  const TH = 'pb-2 align-bottom text-11 font-normal text-text-3';
  const NUM = 'py-2 pl-3 text-right align-baseline font-mono text-13 whitespace-nowrap text-text-1 tabular-nums';
  const ROW = 'border-t border-hair';
</script>

<section class="@container mt-6 min-w-0 border-t border-hair pt-5" aria-label="Outcomes per version" data-profile-outcomes>
  <SectionHead label="Outcomes per version" meta={shown ? `${shown.versions.length}` : ''} />

  {#if failure !== null}
    <p class="text-15 text-text-2" data-outcomes-failure>{failure}, so no version can be compared here.</p>
  {:else if rereading}
    <p class="text-15 text-text-3" data-outcomes-reread>{OUTCOMES_REREAD}</p>
  {:else if agreement === 'diverged'}
    <p class="text-15 text-text-2" data-outcomes-diverged>{OUTCOMES_DIVERGED}</p>
  {:else if shown === null}
    <p class="text-15 text-text-3">Reading what each {role} prompt version did…</p>
  {:else if shown.versions.length === 0}
    <p class="text-15 text-text-2" data-outcomes-empty>
      {#if shown.kind === 'reviewer'}
        No review round is recorded yet, so there is nothing to compare.
      {:else}
        No {role} run has worked on an item yet, so there is nothing to compare.
      {/if}
    </p>
  {:else if shown.kind === 'implementer'}
    <table class="w-full border-collapse" data-outcomes-table="implementer">
      <thead>
        <tr>
          <th scope="col" class="{TH} text-left whitespace-nowrap">Version</th>
          <th scope="col" class="{TH} pl-3 text-right">Items</th>
          <th scope="col" class="{TH} pl-3 text-right">First pass</th>
          <th scope="col" class="{TH} pl-3 text-right">Rounds to ready</th>
          <th scope="col" class="{TH} pl-3 text-right">Median cost</th>
        </tr>
      </thead>
      <tbody>
        {#each shown.versions as row (row.label)}
          <tr class={ROW} data-outcome-row={row.label}>
            {@render version(row.label, row.state)}
            <td class={NUM} data-outcome-n>{row.n}</td>
            {#if row.comparable}
              {#each implementerCells(row) as cell, i (i)}
                <td class={NUM} data-outcome-cell>{cell}</td>
              {/each}
            {:else}
              <td colspan="3" class="py-2 pl-3 text-left align-baseline text-13 text-text-3" data-outcome-few
                >{tooFew(row.n)}</td
              >
            {/if}
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <table class="hidden w-full border-collapse @3xl:table" data-outcomes-table="reviewer">
      <thead>
        <tr>
          <th scope="col" class="{TH} text-left whitespace-nowrap">Version</th>
          <th scope="col" class="{TH} pl-3 text-right">Rounds</th>
          {#each verdicts as verdict (verdict)}
            <th scope="col" class="{TH} pl-3 text-right whitespace-nowrap" data-verdict-head={verdict} data-status-word
              >{verdictLabel(verdict)}</th
            >
          {:else}
            <th scope="col" class="{TH} pl-3 text-left">Verdicts</th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each shown.versions as row (row.label)}
          <tr class={ROW} data-outcome-row={row.label}>
            {@render version(row.label, row.state)}
            <td class={NUM} data-outcome-n>{row.n}</td>
            {#if row.comparable}
              {#each verdictCells(row, verdicts) as cell (cell.verdict)}
                <td class={NUM} data-outcome-cell data-verdict={cell.verdict}>{cell.count}</td>
              {/each}
            {:else}
              <td
                colspan={Math.max(verdicts.length, 1)}
                class="py-2 pl-3 text-left align-baseline text-13 text-text-3"
                data-outcome-few>{tooFew(row.n, 'rounds')}</td
              >
            {/if}
          </tr>
        {/each}
      </tbody>
    </table>
    <ol class="@3xl:hidden" data-outcomes-blocks="reviewer">
      {#each shown.versions as row (row.label)}
        <li class="{ROW} py-2" data-outcome-block={row.label}>
          <div class="flex items-baseline justify-between gap-3">
            <div class="min-w-0">{@render versionLines(row.label, row.state)}</div>
            <span class="inline-flex shrink-0 items-baseline gap-1.5 whitespace-nowrap">
              <span class="text-11 text-text-3">Rounds</span>
              <span class="font-mono text-13 text-text-1 tabular-nums" data-block-n>{row.n}</span>
            </span>
          </div>
          {#if row.comparable}
            <ul class="mt-1 flex flex-wrap gap-x-4 gap-y-1" data-verdict-pairs>
              {#each verdictCells(row, verdicts) as cell (cell.verdict)}
                <li class="inline-flex items-baseline gap-1.5 whitespace-nowrap" data-verdict-pair={cell.verdict}>
                  <span class="text-11 whitespace-nowrap text-text-3" data-status-word>{verdictLabel(cell.verdict)}</span>
                  <span class="font-mono text-13 text-text-1 tabular-nums" data-pair-count>{cell.count}</span>
                </li>
              {/each}
            </ul>
          {:else}
            <p class="mt-1 text-13 text-text-3" data-outcome-few>{tooFew(row.n, 'rounds')}</p>
          {/if}
        </li>
      {/each}
    </ol>
  {/if}
</section>

{#snippet version(label: string, state: string | null)}
  <th scope="row" class="py-2 text-left align-baseline font-normal whitespace-nowrap">{@render versionLines(label, state)}</th>
{/snippet}

<!-- A version's id and, under it, its state: the table's version cell and a phone block's first line.
     The id is an id, so it never wraps — not even at a built-in's hyphen. -->
{#snippet versionLines(label: string, state: string | null)}
  <span class="font-mono text-13 whitespace-nowrap text-text-1" data-outcome-label>{label}</span>
  {#if state !== null}
    <span class="block text-11 text-text-3">{state}</span>
  {/if}
{/snippet}
