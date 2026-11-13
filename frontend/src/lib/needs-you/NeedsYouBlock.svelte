<script lang="ts">
  // #2280 D9 — the ONE needs-you block, for an entry row and a projection row
  // alike (UXWork's `.ny`). It replaces `NeedsYou.svelte`'s band and
  // `EntryDetail.svelte`'s block, which drew the same wait two ways: the band
  // had no Why, no Since, no command and no magenta border, so the row an
  // operator actually merges got the thinner of the two.
  //
  // A renderer, like every other section on this screen. `needsYouBlock`
  // ($lib/needs-you/block) decides every word, every href and whether there is
  // a block at all; nothing is composed here, and no colour is named — the
  // diamond is `StatusMark`'s and the magenta is the token the card's left
  // border and the Copy button share.
  //
  // Three things the markup itself owes the reader, which
  // `needs-you-block.test.ts` holds it to:
  //
  // * the command is a real `<code>` block, so it can be selected and copied by
  //   hand when the Copy button cannot reach the clipboard (an insecure
  //   context, a denied permission) — the button is a convenience over text
  //   that is already there, never the only copy of the command;
  // * where the drive may run the command (#2537), Run is the magenta move and
  //   Copy steps back beside it; the line under the command is what the DRIVE
  //   did with the Run — queued, running, done, or refused and why — so a
  //   refusal lands in the block that issued it;
  // * the age is printed ONCE, under the `Since` label that says which reading
  //   it is: `held 68d6h` and `waiting 1h00m` are the same field, and only the
  //   row says what the duration is OF. It is in the lead, under the summary
  //   (#3467 D24): one short line that answers "since when" without a click;
  // * where no command clears the row but the server served the move as a
  //   link (a merge's "Open PR #2059 to merge"), that link IS Do this, and the
  //   magenta control (#3467 D24);
  // * every anchor that opens a new tab says so in its own text, with the `↗`
  //   the rest of the console uses.
  import { onDestroy } from 'svelte';
  import type { NeedsYouBlock, TextRun } from '$lib/needs-you/block';
  import RunButton from '$lib/RunButton.svelte';
  import { isRunnable, pairClear, pairLedgers, type RunLine } from '$lib/run-request';
  import { Button, Command, StatusMark, Why } from '$lib/ui';

  let {
    block,
    copied = false,
    oncopy = undefined,
    itemId = null,
    compact = false
  }: {
    block: NeedsYouBlock | null;
    /**
     * #3051 — the Epic rail's 260px Needs-you column: no top margin, and the
     * label column and the command row stay stacked at every width, as they
     * do on a phone, so the command is never squeezed beside Run and Copy.
     */
    compact?: boolean;
    /** The item the block is about, recorded on a Run's request. */
    itemId?: string | null;
    /** True for the moment after a copy landed, so the button can say so. */
    copied?: boolean;
    oncopy?: () => void;
  } = $props();

  const uid = $props.id();
  const detailId = `needs-you-why-${uid}`;
  let whyOpen = $state(false);
  let runLine = $state<RunLine | null>(null);
  const runnable = $derived(block !== null && isRunnable(block.command));
  /**
   * Copy hands over the command Run sends, so it asks Run's gate (#2975): a
   * runnable command copies only once the ledger answered with nothing of it
   * unsettled. A POST still open, a lookup out or failed, or a request queued
   * or running would run twice beside a pasted copy. A command the drive
   * cannot run has no ledger to wait on. The page's `c` asks the same.
   */
  const copyClear = $derived(
    block === null || !runnable || pairClear($pairLedgers, block.command, null)
  );

  function copy(): void {
    if (copyClear) oncopy?.();
  }
  const nodeIds = $derived(block?.nodeIds ?? []);
  const moves = $derived(block?.moves ?? []);
  /** The label column, beside the value from `sm` up unless `compact`. */
  const LEDGER = $derived(
    compact
      ? 'm-0 grid grid-cols-1 items-baseline gap-x-0 gap-y-2.5'
      : 'm-0 grid grid-cols-1 items-baseline gap-x-0 gap-y-2.5 sm:grid-cols-[96px_minmax(0,1fr)]'
  );
  /**
   * #3460 — the links row's buttons in the rail's narrow column: no wider than
   * the block, the server's label wrapping inside the button rather than
   * running past the panel's border ("See the failed merge verify" ended 25px
   * outside it at 1440). Still at least 32px tall. Work's wide block keeps the
   * Button's one-line shape.
   */
  const LINK_FIT = $derived(compact ? 'h-auto min-h-8 max-w-full whitespace-normal py-1.5 text-left' : '');

  // #2884 — the ids a declaration hold names, one per line, for the PR body.
  let idsCopied = $state(false);
  let idsFlash: ReturnType<typeof setTimeout> | null = null;

  async function copyIds(): Promise<void> {
    if (nodeIds.length === 0) return;
    try {
      await navigator.clipboard.writeText(nodeIds.join('\n'));
    } catch {
      // Clipboard unreachable: every id is still on screen as selectable text.
      return;
    }
    idsCopied = true;
    if (idsFlash !== null) clearTimeout(idsFlash);
    idsFlash = setTimeout(() => {
      idsCopied = false;
      idsFlash = null;
    }, 1500);
  }

  // #3056 — a block taken down mid-flash leaves no timer behind to write to it.
  onDestroy(() => {
    if (idsFlash !== null) clearTimeout(idsFlash);
    idsFlash = null;
  });
</script>

<!-- A sentence as printed: the runs a filler cut it into (a config name in
     mono, #3075), else the plain string. -->
{#snippet runs(cut: TextRun[] | undefined, plain: string)}
  {#if cut === undefined}{plain}{:else}{#each cut as run, i (i)}{#if run.mono}<span class="font-mono"
          >{run.text}</span
        >{:else}{run.text}{/if}{/each}{/if}
{/snippet}

{#snippet doThis(block: NeedsYouBlock)}
  <!-- #2536 — the block leads with what to do; the why and what the
       command does are one click away. With no command, the note IS the
       move ("what has to happen first"), so it stays in the lead, and so
       does a served link that is the move (#3467 D24). -->
  {#if block.command !== '' || block.note !== '' || moves.length > 0}
    <!-- The artboard's 96px label column (UXWork `.ledger`). Below 640 the
         column would leave the command a word per line (#2788 N1), so the
         label stacks over a full-width command and Run and Copy go under it. -->
    <dl class={LEDGER}>
      <dt class="text-11 text-text-3">Do this</dt>
      <dd class="m-0 flex min-w-0 flex-col gap-2">
        {#if block.command !== ''}
          <div
            class="flex min-w-0 flex-wrap items-center gap-2.5 rounded-[7px] border
                   border-hair-box bg-ink px-3 py-2 {compact ? '' : 'sm:flex-nowrap'}"
          >
            <span
              class="flex min-w-0 basis-full items-baseline gap-2.5 {compact
                ? ''
                : 'sm:basis-0 sm:flex-1'}"
            >
              <span aria-hidden="true" class="shrink-0 font-mono text-13 text-text-5">$</span>
              <Command
                command={block.command}
                class="min-w-0 flex-1 font-mono text-13 text-text-1"
              />
            </span>
            <!-- The one magenta button on the pane: running the command IS
                 the decision this row is waiting for, so the accent is spent
                 on the thing that carries it away — Run where the drive may
                 run it, else Copy. -->
            <RunButton command={block.command} {itemId} bind:line={runLine} />
            <Button
              variant={runnable ? 'factory' : 'needs-you'}
              size="sm"
              aria-label="Copy command (c)"
              disabled={!copyClear}
              onclick={copy}
              class="shrink-0">{copied ? 'Copied' : 'Copy'}</Button
            >
          </div>
          {#if runLine !== null}
            <!-- What the drive did with the Run, in its own words. -->
            <p
              role="status"
              data-run-line={runLine.tone}
              class="m-0 font-mono text-13 {runLine.tone === 'warn'
                ? 'text-warn'
                : runLine.tone === 'ok'
                  ? 'text-success'
                  : 'text-text-3'}"
            >
              {runLine.text}
            </p>
          {/if}
        {:else}
          {#if moves.length > 0}
            <!-- The server's own words and href (#3467 D24): the move is the
                 one magenta control; any other link beside it is quiet. -->
            <div data-needs-you-moves class="flex min-w-0 flex-wrap items-center gap-2">
              {#each moves as link (link.key)}
                <Button
                  variant={link.move ? 'needs-you' : 'factory'}
                  size="sm"
                  href={link.href}
                  target={link.external ? '_blank' : undefined}
                  rel={link.external ? 'noreferrer' : undefined}
                  data-needs-you-move={link.move ? '' : undefined}
                  class={LINK_FIT}
                  >{link.label}{#if link.external}{' '}<span aria-hidden="true">↗</span>{/if}</Button
                >
              {/each}
            </div>
          {/if}
          {#if block.note !== ''}
            <span data-needs-you-note class="text-13 text-text-2">{@render runs(block.noteRuns, block.note)}</span>
          {/if}
        {/if}
      </dd>
    </dl>
  {/if}
{/snippet}

{#if block}
  <!-- `aria-label` names the wait — its kind and its sentence — so a screen
       reader hears WHICH decision is outstanding rather than just "needs you". -->
  <!-- The artboard's card: a hairline box on the raised ink with a 3px magenta
       left edge — the one solid accent on the pane, spent on the one thing
       that is a person's next move. -->
  <section
    aria-label={`Needs you: ${[block.label.toLowerCase(), block.summary].filter((w) => w !== '').join(' — ')}`}
    class="flex min-w-0 flex-col gap-3 rounded-[4px] border border-hair-box border-l-[3px]
           border-l-accent bg-ink-raised {compact ? 'px-4 py-3.5' : 'mt-[18px] px-5 py-[18px]'}"
  >
    <div class="flex items-center gap-2.5">
      <StatusMark kind="needs_you" size={12} label="needs you" />
      <!-- A status word does not wrap (#3050 N9): at 390 this broke into
           NEEDS / YOU beside a long wait. The word holds its line and the
           wait beside it gives way; `data-status-word` is the hook the 390
           sweep reads. -->
      <span data-status-word class="shrink-0 whitespace-nowrap text-11 text-accent">Needs you</span>
      {#if block.label !== ''}
        <span aria-hidden="true" class="text-11 text-text-5">·</span>
        <!-- The KIND of wait ("Held", "Merge decision"), never its reason:
             the sentence below says the reason, once (#3051). data-13, as
             label-11 is for the status word before it (#2787 D4). -->
        <span data-needs-you-kind class="min-w-0 text-13 text-text-2">{block.label}</span>
      {/if}
    </div>

    <!-- #3469 (D35) — in the Epic rail the command comes first, under the
         label: the rail scrolls inside the window's foot at 1440x900, and a
         command below a four-line sentence was below the fold, where DESIGN.md
         puts it "one click away". -->
    {#if compact}{@render doThis(block)}{/if}

    {#if block.summary !== ''}
      <!-- The one sentence every surface prints for this row, in the prose
           size the design system gives a needs-you summary. -->
      <p data-needs-you-summary class="m-0 max-w-[840px] text-15 font-medium text-text-1"
        >{@render runs(block.summaryRuns, block.summary)}</p
      >
    {/if}

    {#if block.since !== ''}
      <!-- #3467 D24 — since when, in the lead: DESIGN.md's needs-you-block
           reads summary, Why, Since, Do this, and the age was folded away
           behind why ▸ where nobody scanning the pane would open it. -->
      <dl data-needs-you-since class={LEDGER}>
        <dt class="text-11 text-text-3">Since</dt>
        <dd class="m-0 font-mono text-13 text-text-2">{block.since}</dd>
      </dl>
    {/if}

    {#if nodeIds.length > 0}
      <!-- #2884 — every id the hold asks to be declared, each whole: the
           reason the summary comes from names one and counts the rest. A long
           id wraps anywhere rather than scrolling the pane sideways. -->
      <dl data-declare-ids class={LEDGER}>
        <dt class="text-11 text-text-3">Declare</dt>
        <dd class="m-0 flex min-w-0 flex-col items-start gap-2">
          <ul class="m-0 flex w-full min-w-0 list-none flex-col gap-1.5 p-0">
            {#each nodeIds as id (id)}
              <li class="min-w-0">
                <code data-node-id class="font-mono text-13 text-text-1 [overflow-wrap:anywhere]"
                  >{id}</code
                >
              </li>
            {/each}
          </ul>
          <Button
            variant="factory"
            size="sm"
            aria-label={`Copy ${nodeIds.length} node id${nodeIds.length === 1 ? '' : 's'}`}
            onclick={copyIds}>{idsCopied ? 'Copied' : 'Copy ids'}</Button
          >
        </dd>
      </dl>
    {/if}

    {#if !compact}{@render doThis(block)}{/if}

    {#if block.why !== '' || (block.command !== '' && block.note !== '')}
      <Why bind:open={whyOpen} controls={detailId} class="self-start" />
      <dl
        id={detailId}
        hidden={!whyOpen}
        class="m-0 grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-x-0 gap-y-2.5"
      >
        {#if block.why !== ''}
          <dt class="text-11 text-text-3">Why</dt>
          <dd class="m-0 text-15 text-text-2">{block.why}</dd>
        {/if}
        {#if block.command !== '' && block.note !== ''}
          <dt class="text-11 text-text-3">It does</dt>
          <dd class="m-0 text-13 text-text-3">{block.note}</dd>
        {/if}
      </dl>
    {/if}

    {#if block.links.length > 0 || block.external.length > 0}
      <!-- Quiet, in one row, under the block: these are places to go and look,
           not the move the row is waiting for. -->
      <div class="flex flex-wrap items-center gap-2 border-t border-hair-quiet pt-3.5">
        {#each block.links as link (link.key)}
          <Button variant="factory" size="sm" href={link.href} class={LINK_FIT}>{link.label}</Button>
        {/each}
        {#each block.external as link (link.key)}
          <Button
            variant="factory"
            size="sm"
            href={link.href}
            target="_blank"
            rel="noreferrer"
            class={`text-text-2 ${LINK_FIT}`}>{link.label} <span aria-hidden="true">↗</span></Button
          >
        {/each}
      </div>
    {/if}
  </section>
{/if}
