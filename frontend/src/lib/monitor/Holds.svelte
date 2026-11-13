<script lang="ts">
  /**
   * FUI-24 — "Holds and blocks" (DMonitor x 40–607, y ≈476–924): a 104px kind
   * column, the sentence beside it, and the count in the section head.
   *
   * The component is a renderer, a poll and a writer. Every row, word and count
   * in it is built by `$lib/monitor/holds`, where a vitest holds them to the
   * artboard; what lives here is the timer over that module's `holdsPoll`
   * and the approve/deny writes, which are `verbs.ts`'s `decideHold`, unchanged.
   *
   * A hold row carries its own action in words — the cap key to raise, the
   * `kew epic unhold` to run — and NOT a button (#2208): the one write this
   * column has is the judgment queue's approve/deny, and that releases the
   * GitHub needs-approval label, which no `epic_holds` row has.
   *
   * The blocked rows cost `/api/epics` plus one snapshot per epic that has a
   * blocked node, and the overrides cost `/api/config/budget`; both are
   * re-taken on this component's own poll by `readHoldSources`, because a hold
   * that has been lifted has to leave the column that says it is standing.
   * `budget` is the page's `load()` reading, held only until the first poll
   * answers, so the column is never briefly blank on a cap it already knows;
   * each polled map goes back to the page through `onbudget`, because the
   * prior-cap pin beside the spend gauge is drawn off the same `override` as
   * these rows and the two must not disagree about it.
   *
   * A decided row leaves on the WRITE RESPONSE and stays gone across the poll,
   * exactly as the console's queue does (CS-6) — a read-back that lags the
   * write must never resurrect a row the operator has already actioned. The
   * dismissal ends the moment the read it is bridging lands: a hold re-raised
   * later on the same item is a new hold, and must be drawn.
   *
   * `epics` is the seam the fixture tests and any future push feed use: pass a
   * payload and this component stops fetching for itself — the caps included,
   * so a caller that drives the epics owns keeping `budget` current too.
   */
  import { browser } from '$app/environment';
  import type { BudgetMap, EpicSnapshot } from '$lib/api';
  import { TAP_INLINE } from '$lib/factory/classes';
  import type { Source } from '$lib/monitor/readouts';
  import { factoryPulse } from '$lib/shell/pulse.svelte';
  import { Button, Command, SectionHead, StatusMark, Title } from '$lib/ui';
  import { decideHold, decideRemovesRow } from '$lib/verbs';
  import { untrack } from 'svelte';
  import {
    composeHolds,
    drawnHolds,
    holdSources,
    holdWord,
    HOLDS_EMPTY,
    HOLDS_POLL_MS,
    holdsPoll,
    pruneRowState,
    tourTargets,
    type HoldRow,
    type PolledSources
  } from './holds';

  let {
    budget,
    epics,
    onbudget
  }: {
    /** `/api/config/budget` as the page read it; the seed for the first poll. */
    budget: Source<BudgetMap>;
    /** Snapshots supplied by the caller. Absent: the component polls. */
    epics?: Source<readonly EpicSnapshot[]>;
    /**
     * Every caps reading this column takes, handed back to the page. The
     * Monitor's prior-cap pin is drawn off the same `override` these rows are,
     * so it reads the poll's map rather than going on marking an override that
     * was lifted while the page sat open.
     */
    onbudget?: (budget: Source<BudgetMap>) => void;
  } = $props();

  /** `null` until the first poll answers; `holdSources` owns what that means. */
  let polled = $state<PolledSources | null>(null);

  /**
   * The since-when on each hold row, re-read on the column's own poll (#2208).
   * Seeded so the first paint says how long the hold has stood rather than
   * waiting 30s to say anything, and ticking at the poll's own rate because a
   * hold that has stood for hours does not need a second hand.
   */
  let clock = $state(Date.now());
  $effect(() => {
    if (!browser) return;
    const timer = setInterval(() => {
      clock = Date.now();
    }, HOLDS_POLL_MS);
    return () => clearInterval(timer);
  });

  function land(next: PolledSources): void {
    polled = next;
    onbudget?.(next.budget);
  }

  $effect(() => {
    if (epics !== undefined || !browser) return;
    // One round out at a time, the newest drawn (#3468 C9). A hidden tab reads
    // nothing (#2285, C8), and reads once when it comes back.
    const poll = holdsPoll(land, () => document.hidden);
    poll.show();
    const timer = setInterval(() => poll.beat(), HOLDS_POLL_MS);
    const onVisible = () => poll.show();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      poll.stop();
    };
  });

  /** Rows a successful decide has removed, kept out across the poll (CS-6). */
  let dismissed = $state<Record<string, boolean>>({});
  /** Per-row write state, rendered INLINE on the row and never page-top. */
  const busy = $state<Record<string, boolean>>({});
  let errors = $state<Record<string, string>>({});

  /**
   * Which rows flashed "Copied", keyed like every other per-row state here so
   * `pruneRowState` drops it with the row. The `<code>` block beside the button
   * holds the same text, so a clipboard this page cannot reach — an insecure
   * context, a denied permission — leaves the command selectable rather than
   * lost, and the button simply does not flash.
   */
  let copied = $state<Record<string, boolean>>({});
  const copyFlash: Record<string, ReturnType<typeof setTimeout>> = {};

  async function copy(row: HoldRow): Promise<void> {
    if (row.command === '') return;
    try {
      await navigator.clipboard.writeText(row.command);
    } catch {
      return;
    }
    copied[row.key] = true;
    clearTimeout(copyFlash[row.key]);
    copyFlash[row.key] = setTimeout(() => {
      delete copied[row.key];
      delete copyFlash[row.key];
    }, 1500);
  }


  // Which reading stands for which source is `holdSources`, not a chain of
  // `??` in a component no vitest can mount: the caps come from the poll the
  // moment it answers, and an unread source is named rather than defaulted to
  // an empty list. The judgment queue rides the shell's shared poll, so this
  // column costs it nothing beyond the read the header above it already takes.
  const composed = $derived(
    composeHolds(
      holdSources({ polled, seedBudget: budget, epics, dashboard: factoryPulse.dashboard }),
      clock
    )
  );

  /**
   * A dismissal lasts exactly as long as the sources go on serving the row it
   * hid. Once a read that COVERS the row comes back without it the write has
   * landed, the key is dropped, and the same item raising a fresh hold is drawn
   * like any other. A source that did not answer covers nothing, which is why
   * the prune is given the whole view and not the row list — `pruneRowState`.
   *
   * The per-row maps are read inside `untrack`: the composed view is this
   * effect's only trigger, and letting it depend on the maps it writes would
   * make it re-run on its own writes for nothing. `pruneRowState` returns the
   * map it was given when there is nothing to drop, so the common poll writes
   * no state at all.
   */
  $effect(() => {
    const view = composed;
    untrack(() => {
      const keptDismissed = pruneRowState(view, dismissed);
      if (keptDismissed !== dismissed) dismissed = keptDismissed;
      const keptErrors = pruneRowState(view, errors);
      if (keptErrors !== errors) errors = keptErrors;
      const keptCopied = pruneRowState(view, copied);
      if (keptCopied !== copied) copied = keptCopied;
    });
  });

  /** What is drawn, and the count in the head, read off the same list. */
  const view = $derived(drawnHolds(composed, dismissed));
  const tour = $derived(tourTargets(view.rows));

  async function decide(row: HoldRow, action: 'approve' | 'deny'): Promise<void> {
    if (row.itemId == null || busy[row.key]) return;
    busy[row.key] = true;
    delete errors[row.key];
    const res = await decideHold(row.itemId, action, '');
    busy[row.key] = false;
    if (decideRemovesRow(res)) dismissed[row.key] = true;
    else if (!res.ok) errors[row.key] = res.message;
  }
</script>

<section aria-label="Holds and blocks" class="flex min-w-0 flex-col">
  <SectionHead label="Holds and blocks" meta={view.meta} />

  {#if view.rows.length > 0}
    <ul class="m-0 list-none p-0">
      {#each view.rows as row (row.key)}
        <li
          data-hold-item={row.itemId ?? undefined}
          data-tour={tour[row.key]}
          class="grid grid-cols-[104px_minmax(0,1fr)] items-baseline gap-4 border-b border-hair-quiet py-4 last:border-b-0"
        >
          <!--
            The mark is the ROW's, not a lookup on its kind: `merge` is one
            word over two facts — a person's merge decision and a base-branch
            conflict the merge queue is holding the node on — and the model is
            where that is decided (`HoldRow.mark`).
          -->
          <!-- The word follows the mark (#2539, D19): HELD in amber over a hold,
               the move's own word in magenta over a person's. -->
          <StatusMark
            kind={row.mark}
            size={10}
            word
            label={holdWord(row)}
            class="text-11"
          />
          <!-- A hold reason can carry a path with no break in it
               (`src/kew/graph/driver/merge_queue.py`), which outran a 390px
               column and scrolled the Monitor sideways (#2889), so a long word
               breaks. -->
          <div class="min-w-0 text-15 leading-normal break-words text-text-2">
            {#each row.detail as segment, i (i)}
              {#if segment.href}
                <!--
                  A ref inside a sentence, 20px tall on a phone: the press
                  target is vertical padding, which an inline box is tapped by
                  but does not take into the line it sits in, so the sentence
                  reads exactly where it did (#2282 D25).
                -->
                <a
                  class={`text-text-1 underline-offset-2 hover:underline ${TAP_INLINE}`}
                  href={segment.href}>{segment.text}</a
                >
              {:else}{segment.text}{/if}
            {/each}
            {#if row.actions.length > 0 && row.itemId != null}
              <div class="mt-2.5 flex items-center gap-3">
                {#each row.actions as act (act.action)}
                  <Button
                    variant={act.primary ? 'primary' : 'ghost'}
                    size="sm"
                    disabled={busy[row.key]}
                    onclick={() => decide(row, act.action)}>{act.label}</Button
                  >
                {/each}
              </div>
            {/if}
            <!--
              What actually clears the row (#2208, #2280 D14): the command the
              server computed, in a block with a magenta Copy beside it. A row
              with a block carries no action line under it (#2896 R7); a row
              without one says its action in words instead. Both are absent on
              a hold nobody can act on — the CI gate clears itself.
            -->
            {#if row.command !== ''}
              <div
                class="mt-2.5 flex min-w-0 items-center gap-2.5 rounded-[7px] border
                       border-hair-box bg-ink px-3 py-2"
              >
                <span aria-hidden="true" class="shrink-0 font-mono text-13 text-text-5">$</span>
                <Command
                  command={row.command}
                  class="min-w-0 flex-1 font-mono text-13 text-text-1"
                />
                <Button
                  variant="needs-you"
                  size="sm"
                  aria-label={`Copy command for ${row.kind}`}
                  onclick={() => copy(row)}
                  class="shrink-0">{copied[row.key] ? 'Copied' : 'Copy'}</Button
                >
              </div>
            {/if}
            {#if row.command === '' && row.action !== ''}
              <p data-hold-action class="mt-2 font-mono text-[11px] text-text-3">{row.action}</p>
            {/if}
            {#if row.verifyLink || row.link}
              <!--
                The move when it is a link (#2539, D20): a merge is made on the
                forge, so its Do this line is the PR, not a command, in the
                same words Work's block uses ("Open PR #2059 to merge"). Over a
                verify that failed at this head the failure is the first move
                and the PR the second (#3460). Quiet text with ↗, never cyan:
                cyan does not mark a link (DESIGN.md Signal, `link-external`).
              -->
              <div class="mt-2.5 flex min-w-0 items-baseline gap-3" data-hold-do-this>
                <span class="shrink-0 text-11 text-text-3">Do this</span>
                <div class="flex min-w-0 flex-col gap-1">
                  {#each [{ link: row.verifyLink, verify: true }, { link: row.link, verify: false }] as move (move.verify)}
                    {#if move.link}
                      <p
                        class="m-0 text-13"
                        data-hold-verify-link={move.verify ? '' : undefined}
                        data-hold-link={move.verify ? undefined : ''}
                      >
                        <a
                          class={`text-text-2 underline-offset-2 hover:underline ${TAP_INLINE}`}
                          href={move.link.href}
                          target={move.link.external ? '_blank' : null}
                          rel={move.link.external ? 'noreferrer' : null}
                          >{move.link.label}{#if move.link.external}{' '}↗{/if}</a
                        >
                      </p>
                    {/if}
                  {/each}
                </div>
              </div>
            {/if}
            {#if errors[row.key]}
              <p class="mt-2 text-13 text-danger"><Title text={errors[row.key]} /></p>
            {/if}
          </div>
        </li>
      {/each}
    </ul>
  {:else if view.complete}
    <!-- An empty state is a sentence, so it is data-13, not an 11px mono note (#2896 R7). -->
    <p class="text-13 text-text-3">{HOLDS_EMPTY}</p>
  {/if}

  <!--
    Named sources, not a blanket apology: an operator who can see that only the
    epics went missing knows which part of this column to distrust.
  -->
  {#each view.notices as notice (notice)}
    <p class="mt-2 font-mono text-[11px] text-text-3">{notice}</p>
  {/each}
</section>
