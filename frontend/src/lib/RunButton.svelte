<script lang="ts">
  // #2537 — Run: hand a printed command to the running `kew drive`.
  //
  // The console launches nothing. Pressing Run queues a request in the ledger
  // (`startRun`); the drive runs it as the operator and the request row says
  // what happened. This button only shows the command is runnable, sends it,
  // and hands the drive's reading back through `line` — the block that owns the
  // command prints that line under it, so a refusal (fleet stopped, drive held,
  // budget spent, no drive running) lands in the block that issued the Run.
  //
  // A command outside the drive's runnable set renders nothing: Copy is that
  // block's only move, as before.
  import { onDestroy, untrack } from 'svelte';
  import { Button } from '$lib/ui';
  import {
    followRun,
    isRunnable,
    isSubmitting,
    isTerminal,
    pairClear,
    pairLedgers,
    resumeRun,
    runButtonLabel,
    runButtonName,
    runLine,
    startRun,
    watchPair,
    type RunLine
  } from '$lib/run-request';
  import type { RunRequest } from '$lib/verbs';

  let {
    command,
    itemId = null,
    model = null,
    variant = 'needs-you',
    held = false,
    line = $bindable(null),
    sending = $bindable(false)
  }: {
    command: string;
    itemId?: string | null;
    /** A model for this one run (#2741), sent beside the command; null sends none. */
    model?: string | null;
    variant?: 'needs-you' | 'factory';
    /** True while the block may not hand its command over (#2975): the
     * launchpad re-resolving its route, or sharing one gate with its Copies. */
    held?: boolean;
    /** What the drive did with the last Run of this command, for the block. */
    line?: RunLine | null;
    /** True from the press until the POST answers (#2878): the block holds
     * its model picker still, since a new model would drop this submission
     * while the POST can still queue it. */
    sending?: boolean;
  } = $props();

  /** Leaves the submission or follow in flight; the POST itself goes on (#2975). */
  let stop: (() => void) | null = null;
  let busy = $state(false);
  /** The ledger answered for this pair and holds nothing of it unsettled
   * (#2975). Until then — a lookup out, or failed and asked again — this
   * button cannot know no request of it is already queued (one a button that
   * left saw accepted), so it may not send another. */
  const clear = $derived(pairClear($pairLedgers, command, model || null));
  /** Where the followed request stands: "Sending…" until the POST answers,
   * "Queued…" until the drive starts it (#2878). */
  let phase = $state<RunRequest['state'] | 'sending' | null>(null);
  const runnable = $derived(isRunnable(command));
  // Magenta is a person's next move (DESIGN.md); a request in flight is not
  // one, so the button goes neutral until it is actionable again (#2878).
  const shown = $derived(busy && variant === 'needs-you' ? 'factory' : variant);
  // A disabled button fades to 45%, under AA (3.8:1 dark, 3.0:1 light). The
  // in-flight label is a reading the operator needs — "Queued…" can stand for
  // an hour — so it stays opaque, in the quieter text-2 (#2878).
  const BUSY = 'disabled:opacity-100 text-text-2';

  function show(state: { request: RunRequest | null; error: string }) {
    sending = false;
    if (state.request === null) {
      busy = false;
      phase = null;
      line = { tone: 'warn', text: state.error };
      return;
    }
    busy = !isTerminal(state.request);
    phase = busy ? state.request.state : null;
    line = runLine(state.request);
  }

  /** Press this Run from elsewhere on its block (#3055: the drawer's Run). */
  export function press(): void {
    run();
  }

  function run(): void {
    if (busy || held || !clear || !runnable) return;
    stop?.();
    busy = true;
    phase = 'sending';
    sending = true;
    line = { tone: 'quiet', text: 'sending to the drive…' };
    stop = startRun(command, itemId, show, { model });
  }

  // A new command (another row selected) or model (another pick, #2878) drops
  // the old reading, then picks up the newest request for this pair: one still
  // in flight is followed, a settled one is shown (#2537: a refusal stays in
  // its block). The drive holds one active request per (command, model), so a
  // request queued on one model says nothing about another's Run.
  //
  // Keyed on the STRINGS (#2789, K3). The caller hands in a field of a block it
  // re-derives on its own clock, and an effect reading the prop directly woke
  // on every new block — re-reading the ledger and restarting the follow every
  // few seconds, hidden tab or not. A derived only wakes its readers when the
  // value itself changes.
  //
  // Only the two keys wake it: the rest runs `untrack`ed, because an answer a
  // gone button left behind is handed over synchronously, and its `line`
  // write would otherwise wake this effect again and drop it (#2975).
  const commandKey = $derived(command);
  const modelKey = $derived(model || null);
  $effect(() => {
    const current = commandKey;
    const chosen = modelKey;
    return untrack(() => pickUp(current, chosen));
  });

  function pickUp(current: string, chosen: string | null): (() => void) | undefined {
    stop?.();
    stop = null;
    busy = false;
    phase = null;
    sending = false;
    line = null;
    if (!isRunnable(current)) return;
    // A Run of this pair a button that is gone pressed (#2975): its POST still
    // commits, so this button reads "Sending…" until it answers, then shows the
    // answer — or the answer that landed while no button was here to hear it.
    if (isSubmitting(current, chosen)) {
      busy = true;
      phase = 'sending';
      sending = true;
      line = { tone: 'quiet', text: 'sending to the drive…' };
    }
    stop = resumeRun(current, chosen, show);
    let live = true;
    // The pair's ledger, read until it answers (#2975): with no submission to
    // resume, the request it finds is this button's to show.
    const found =
      stop !== null
        ? null
        : (request: RunRequest | null) => {
            if (!live || request === null) return;
            // A settled request still reads in the block (refused, failed,
            // done): the drive's answer must outlive a re-render or a reload.
            if (isTerminal(request)) show({ request, error: '' });
            else stop = followRun(request, show);
          };
    const unwatch = watchPair(current, chosen, found);
    return () => {
      live = false;
      unwatch();
    };
  }

  // Unmounting leaves the submission, never cancels it (#2975): the POST is
  // the store's to finish, and the hold on the block's route is the store's
  // too (`pendingRuns`), so this button's own `sending` goes with it.
  onDestroy(() => {
    stop?.();
    sending = false;
  });
</script>

{#if runnable}
  <Button
    variant={shown}
    size="sm"
    aria-label={runButtonName(busy ? phase : null, command)}
    data-run-button
    disabled={busy || held || !clear}
    onclick={run}
    class={busy ? `shrink-0 ${BUSY}` : 'shrink-0'}>{runButtonLabel(busy ? phase : null)}</Button
  >
{/if}
