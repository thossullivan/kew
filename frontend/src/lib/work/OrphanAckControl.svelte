<script lang="ts">
  // #3453 — the orphan-PR acknowledge on Work, which only `/console` and the TUI
  // had. A renderer over `orphanAck` ($lib/work/orphan-ack): that model decides
  // whether this row presses Acknowledge itself or points at the item whose
  // header already offers it, so an orphan is never given two controls.
  //
  // The press is the console's own: `ackOrphan`, ledger-only, never closing the
  // PR, with no confirm step because the console has none — it records that a
  // person has seen the PR and nothing more. The row leaves on the WRITE
  // RESPONSE (`ackRemovesRow`, through `onacked`); a refusal stays here, in the
  // server's words, under the button that asked.
  import type { OrphanAck } from '$lib/work/orphan-ack';
  import { ackOrphan, ackRemovesRow } from '$lib/verbs';
  import { TAP_INLINE } from '$lib/factory/classes';
  import { Button } from '$lib/ui';

  let {
    ack,
    href,
    onacked = undefined
  }: {
    ack: OrphanAck | null;
    /** The list's own link to an item, so a pointer keeps the reader's filter. */
    href: (itemId: string) => string;
    onacked?: (pr: number) => void;
  } = $props();

  let busy = $state(false);
  let error = $state<string | null>(null);

  // A new orphan under the same control starts clean: a refusal is about the
  // PR it was for, never the next one.
  let lastPr: number | null = null;
  $effect(() => {
    const pr = ack?.pr ?? null;
    if (pr === lastPr) return;
    lastPr = pr;
    busy = false;
    error = null;
  });

  async function acknowledge(pr: number): Promise<void> {
    if (busy) return;
    busy = true;
    error = null;
    const res = await ackOrphan(pr);
    // The answer belongs to the PR it was sent for; a reader who moved on
    // does not get it painted onto the next one.
    if (ack?.pr !== pr) return;
    busy = false;
    if (ackRemovesRow(res)) onacked?.(pr);
    else if (!res.ok) error = res.message;
  }
</script>

{#if ack?.kind === 'ack'}
  {@const pr = ack.pr}
  <section
    aria-label={`Acknowledge orphan PR #${pr}`}
    data-orphan-ack={pr}
    class="flex min-w-0 flex-col gap-2"
  >
    <div class="flex min-w-0 flex-wrap items-center gap-2.5">
      <Button
        variant="needs-you"
        size="sm"
        disabled={busy}
        onclick={() => acknowledge(pr)}>{busy ? 'Acknowledging…' : 'Acknowledge'}</Button
      >
      {#if ack.url}
        <Button variant="factory" size="sm" href={ack.url} target="_blank" rel="noreferrer"
          >Open PR #{pr} <span aria-hidden="true">↗</span></Button
        >
      {/if}
    </div>
    <p class="m-0 text-13 text-text-3">
      Records that you have seen PR #{pr}. The pull request stays open.
    </p>
    {#if error !== null}
      <p role="alert" data-orphan-ack-error class="m-0 font-mono text-13 text-warn">{error}</p>
    {/if}
  </section>
{:else if ack?.kind === 'linked'}
  <p data-orphan-ack-linked={ack.pr} class="m-0 text-13 text-text-3">
    PR #{ack.pr} is acknowledged from
    <a href={href(ack.itemId)} class={`text-text-2 underline hover:text-text-1 ${TAP_INLINE}`}>{ack.ref}</a>, the item
    whose run left it.
  </p>
{/if}
