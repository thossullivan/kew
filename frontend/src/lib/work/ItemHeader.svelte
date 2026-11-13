<script lang="ts">
  // FUI-19 — the Work item header (DWork x 468–1392, y ≈84–246): breadcrumb and
  // actions on one line, a 26px mono title under it, then the meta row.
  //
  // This file decides nothing. What the crumbs say, which cells the meta row
  // has and — the load-bearing one — WHICH BUTTONS EXIST all come from
  // `$lib/work/header`, whose `actions` are the projection's own offer. There
  // is no literal button in this markup, so no amount of editing here can put
  // back the artboard's Claim / Hold / Flags: a verb kew never offered has no
  // path to the screen. `header.test.ts` holds both halves of that to the
  // committed surface-convergence fixture.
  //
  // The write path is the dashboard's, unchanged: `performWorkAction` against
  // the revision the view was read at, `applyWorkActionResult` to transition
  // from the SERVER's recomposed view, and a 409 that re-reads rather than
  // retrying blind — the world moved, so the decision is made again. The one
  // thing `act` decides for itself is WHOSE outcome it is: the item is read
  // before the await and the readout is only written back if that item is still
  // the one on screen.
  import { performWorkAction } from '$lib/api';
  import { applyWorkActionResult, workActionLabel } from '$lib/dashboard';
  import { Button, Input, StatusMark, Title, Why } from '$lib/ui';
  import { itemHeader, type ItemHeaderInput } from './header';
  import type { WorkView } from '$lib/api';

  let {
    row,
    runs,
    onapplied,
    onstale
  }: ItemHeaderInput & {
    /** The server's post-write view, for the page to fold into its projection. */
    onapplied?: (view: WorkView) => void;
    /** The revision went stale: the caller must re-read before deciding again. */
    onstale?: () => Promise<void> | void;
  } = $props();

  const header = $derived(itemHeader({ row, runs }));

  let busy = $state<string | null>(null);
  let reason = $state('');
  let chip = $state<string | null>(null);
  let error = $state<string | null>(null);
  let whyOpen = $state(false);

  // A new item is a new decision: nothing said about the last one carries
  // over. `lastItem` is a plain `let` for the reason the Work page's `lastPulse`
  // is one — an effect that read the state it writes would re-run itself.
  let lastItem: string | null = null;
  $effect(() => {
    const id = row.itemId;
    if (id === lastItem) return;
    lastItem = id;
    busy = null;
    reason = '';
    chip = null;
    error = null;
    whyOpen = false;
  });

  async function act(action: string): Promise<void> {
    if (header.revision === '' || busy !== null) return;
    // The item and revision this decision was made against, read BEFORE the
    // await: a write takes a moment, and in that moment the reader can select
    // another row. The outcome belongs to `item` and to nothing else.
    const item = header.itemId;
    const revision = header.revision;
    busy = action;
    error = null;
    const out = applyWorkActionResult(await performWorkAction(item, action, revision, reason));
    // The chip, the error and the pressed state are THIS row's readout. If the
    // reader has moved on, the item-change effect has already cleared them, and
    // writing them here would paint one item's outcome onto another's header.
    if (header.itemId === item) {
      busy = null;
      chip = out.chip;
      error = out.error;
    }
    // The server's view and a stale-revision re-read are facts about the item
    // rather than about the selection — the page folds the view in by item id —
    // so they are handed over either way.
    if (out.view) onapplied?.(out.view);
    // Keep the staleness message up: a silent re-read would leave the header
    // looking as though the press had landed.
    if (out.reread) await onstale?.();
  }
</script>

<header class="flex flex-col">
  <div class="flex items-center gap-2">
    <p class="font-mono text-11 tracking-[0.14em] text-text-3 uppercase">
      {#each header.crumbs as crumb, i (crumb.key)}
        {#if i > 0}<span class="text-text-5" aria-hidden="true">/</span>{/if}
        {#if crumb.href}<a href={crumb.href} class="text-text-2 hover:text-text-1">{crumb.text}</a
          >{:else}<span>{crumb.text}</span>{/if}
      {/each}
    </p>
    <div class="flex-1"></div>
    {#each header.actions as action (action.action)}
      <Button
        variant="factory"
        title={action.intent}
        disabled={busy !== null || (action.requires_reason && reason.trim() === '')}
        onclick={() => act(action.action)}
      >
        {workActionLabel(action.action)}
      </Button>
    {/each}
  </div>

  {#if header.needsReason}
    <!-- A refusal has to be written down before it can be sent, so the field
         sits where the button it unlocks is, not in a dialog over the page.
         The label is real and sr-only, the way the list's search input's is: a
         placeholder is not an accessible name — it disappears on the first
         keystroke, and some screen readers never announce it at all. -->
    <label class="sr-only" for="work-action-reason">Reason for this decision</label>
    <Input
      id="work-action-reason"
      bind:value={reason}
      placeholder="reason (required to refuse)"
      class="mt-3 max-w-[22rem]"
    />
  {/if}

  <h1
    class="mt-[18px] max-w-[840px] font-mono text-26 font-medium tracking-[-0.015em] text-text-1"
  >
    <Title text={header.title} />
  </h1>

  <div class="mt-3.5 flex flex-wrap items-center gap-x-4 gap-y-2 font-mono text-13 text-text-2">
    {#each header.meta as meta, i (meta.key)}
      {#if i > 0}
        <span class="h-3.5 w-px bg-hair-strong" aria-hidden="true"></span>
      {/if}
      {#if meta.kind === 'status'}
        <!-- The word is the model's (`MetaCell.word`), not StatusMark's own
             echo of `kind`: the list row prints `stateWord` (#2281 D13), and
             the mark's echo is the RAW state, so leaving it to the component
             had one screen saying DONE in the list and `success` here. -->
        <StatusMark
          kind={meta.value}
          vocabulary="run"
          size={7}
          word
          label={meta.word}
          class="gap-2"
        />
      {:else}
        <span title={meta.title}>
          {#if meta.label}{meta.label}{' '}{/if}{#if meta.href}<a
              href={meta.href}
              target={meta.external ? '_blank' : null}
              rel={meta.external ? 'noreferrer' : null}
              class="text-text-1 hover:text-accent-2">{meta.value}</a
            >{:else}<span class:text-text-1={meta.tone === 'neutral'} class:text-warn={meta.tone === 'money'}
              >{meta.value}</span
            >{/if}{#if meta.suffix}{' '}{meta.suffix}{/if}
        </span>
      {/if}
    {/each}
  </div>

  {#if header.exceptionLine}
    <!-- #2280 D10 — this line used to print for EVERY off-normal item, in mono
         and in a tone a reader reads as the accent, directly above a needs-you
         block saying something else about the same wait: "kew opens the pull
         request; you decide whether it lands." over "the merge decision is
         yours". One wait, two sentences, neither of them the other's summary.
         So where the block below carries the sentence, the block is the only
         surface that says it — and where it does not, or where there is no
         block at all (a quiet exception: a run in flight, an attempt that
         ended killed), this line is the only surface there is and the fact is
         said here rather than nowhere. Which of the two it is is decided by
         reading the block's own words, not by which payload the row came from:
         `exceptionLine` draws that line; see `$lib/work/header`.
         Its own line rather than a meta cell — a detail runs to a clause, and
         the artboard's meta row is one line of short readouts. Tone comes off
         `blocking`, never off the name. -->
    <!-- #2536 — the label is the line a reader acts on; the detail behind it
         runs to a clause or three, so it sits one click away behind `why ▸`. -->
    {@const why = header.exceptionLine}
    <p
      class="mt-2.5 flex max-w-[840px] items-baseline gap-3 font-mono text-13"
      class:text-danger={why.tone === 'danger'}
      class:text-warn={why.tone === 'warn'}
      class:text-text-2={why.tone === 'neutral'}
    >
      <span>{why.label}</span>
      {#if why.detail !== ''}<Why bind:open={whyOpen} controls="item-exception-why" />{/if}
    </p>
    {#if why.detail !== ''}
      <p id="item-exception-why" hidden={!whyOpen} class="mt-1.5 max-w-[840px] text-13 text-text-2">
        {why.detail}
      </p>
    {/if}
  {/if}

  {#if chip || error}
    <p class="mt-3 font-mono text-13" class:text-warn={error} class:text-text-3={!error}>
      {error ?? chip}
    </p>
  {/if}
</header>
