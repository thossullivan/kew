<script lang="ts">
  // #3453 — "what can I start": the open issues kew has not touched yet
  // (`glance.queue_candidates`), queued through an explicit confirm. Until now
  // only `/console`'s ready-queue card offered this; Work read the candidates
  // for their titles and nothing else.
  //
  // The flow is the old console card's: pick, confirm, and the
  // write's own answer — `queueOutcome` over `queueIssue` — shown as the
  // result line. A queued issue leaves the options at once, on the write
  // response; it joins the list as a ready row on the next read. A refusal
  // stays under the picker in the server's words. With no candidate and no
  // result there is nothing here at all: no control, no empty card.
  import type { QueueCandidate } from '$lib/api';
  import { queueOutcome, type QueueOutcome } from '$lib/glance';
  import { queueIssue } from '$lib/verbs';
  import { Button } from '$lib/ui';
  import { itemRef } from '$lib/utils';

  let { candidates = [] }: { candidates?: QueueCandidate[] } = $props();

  let selected = $state<number | ''>('');
  let confirming = $state(false);
  let busy = $state(false);
  let error = $state<string | null>(null);
  const outcomes = $state<Record<number, QueueOutcome & { ok: true }>>({});

  const options = $derived(candidates.filter((c) => outcomes[c.issue_num] == null));
  const chosen = $derived(options.find((c) => c.issue_num === selected) ?? null);
  const done = $derived(Object.values(outcomes));

  // A poll that stops offering the chosen issue takes the choice with it: Queue
  // would otherwise stay pressable over an option that is gone, and the confirm
  // would come back unasked if the issue were offered again.
  $effect(() => {
    if (selected === '' || chosen !== null) return;
    selected = '';
    confirming = false;
    error = null;
  });

  function cancel(): void {
    confirming = false;
    error = null;
  }

  async function confirmQueue(): Promise<void> {
    if (chosen === null || busy) return;
    const issue = chosen.issue_num;
    busy = true;
    error = null;
    const res = await queueIssue(issue, true); // confirm: true — the ask ran first
    busy = false;
    const out = queueOutcome(issue, res);
    if (out.ok) {
      outcomes[issue] = out;
      selected = '';
      confirming = false;
    } else {
      error = out.message;
    }
  }
</script>

{#if options.length > 0 || done.length > 0}
  <section
    aria-label="Queue an issue"
    data-queue-picker
    class="flex min-w-0 flex-col gap-3 border-t border-hair px-7 py-4"
  >
    <div class="flex items-baseline gap-2">
      <h2 class="text-11 text-text-3">Not started</h2>
      <span class="text-13 text-text-3">open issues kew has not touched</span>
    </div>

    {#if options.length > 0}
      {#if confirming && chosen !== null}
        <div data-queue-confirm class="flex min-w-0 flex-col gap-2">
          <p class="m-0 min-w-0 text-13 text-text-2">
            Queue <span class="font-mono text-text-1">{itemRef(chosen.item_id, chosen.issue_num)}</span>
            {chosen.title} for the next dispatch?
          </p>
          <div class="flex flex-wrap items-center gap-2">
            <Button variant="factory" size="sm" disabled={busy} onclick={cancel}>Cancel</Button>
            <Button variant="factory" size="sm" disabled={busy} onclick={confirmQueue}
              >{busy ? 'Queueing…' : 'Confirm queue'}</Button
            >
          </div>
        </div>
      {:else}
        <div class="flex min-w-0 items-center gap-2">
          <label class="sr-only" for="work-queue-pick">Issue to queue</label>
          <select
            id="work-queue-pick"
            bind:value={selected}
            class="h-8 min-w-0 flex-1 rounded-[4px] border border-hair-strong bg-ink px-2 font-mono
                   text-13 text-text-1 hover:border-text-5 focus:border-text-5 focus:outline-none"
          >
            <option value="">Pick an issue…</option>
            {#each options as c (c.issue_num)}
              <option value={c.issue_num}>{itemRef(c.item_id, c.issue_num)} — {c.title}</option>
            {/each}
          </select>
          <Button
            variant="factory"
            size="sm"
            disabled={selected === ''}
            onclick={() => {
              error = null;
              confirming = true;
            }}>Queue</Button
          >
        </div>
      {/if}
      {#if error !== null}
        <p role="alert" data-queue-error class="m-0 font-mono text-13 text-warn">{error}</p>
      {/if}
    {/if}

    <!-- What each queue write answered, kept after the picker resets. -->
    {#each done as out (out.issue)}
      <p role="status" data-queue-result={out.issue} class="m-0 flex min-w-0 flex-wrap gap-x-2.5 text-13">
        <span class="font-mono text-text-3">#{out.issue}</span>
        <span class="font-mono {out.chip === 'queued' ? 'text-success' : 'text-text-2'}">{out.chip}</span>
        {#if out.notice}<span class="text-warn">{out.notice}</span>{/if}
      </p>
    {/each}
  </section>
{/if}
