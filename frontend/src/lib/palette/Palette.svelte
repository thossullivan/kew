<script lang="ts">
  // #2516 — the Cmd-K palette: one field and a ranked list of every page, epic,
  // item and recent run.
  //
  // #2517 — and what can be done from it ($lib/palette/actions). The item open
  // on `/work?item=`, or the one picked with → on its row, lists its OFFERED
  // actions and the dispatch command the server composed for it; "Stop the
  // fleet" is always listed. Every write goes through the confirm step below
  // first — a press on an action never writes by itself — and a stale revision
  // re-reads the item instead of retrying blind. A copy writes nothing.
  //
  // `Meta+K` / `Ctrl+K` is the registry's reserved palette press, claimed here
  // and nowhere else ($lib/keys/registry), and holds the registry suspended
  // while open, so no page shortcut answers behind it (#2781). The palette's own keys (↑ ↓ → Enter
  // Esc) are read on the dialog and stopped there, so a surface underneath that
  // answers Esc from a field (the epic page's) does not also close itself.
  //
  // #2518 — and, when the server allows it (`palette_ask` in /api/meta), ask
  // mode ($lib/palette/ask): Enter on a query ending in `?`, or Tab on any
  // query, has a model rank the entries already listed. The answer IS those
  // entries, so selecting one is the same press as selecting it here.
  //
  // No timer of its own: the epics and the work projection are read once when
  // the palette opens and again whenever the shared pulse lands a new reading
  // while it is open; the runs and the dashboard ARE the pulse's.
  import { goto } from '$app/navigation';
  import {
    fetchDispatchPlan,
    fetchMeta,
    fetchPaletteSources,
    type DispatchPlan,
    type EpicSummary,
    type WorkProjection,
    type WorkView
  } from '$lib/api';
  import { claimReserved, suspendShortcuts } from '$lib/keys/registry';
  import { factoryPulse } from '$lib/shell/pulse.svelte';
  import { Button, Input, Title } from '$lib/ui';
  import { contained } from '$lib/ui/contained';
  import { itemRef } from '$lib/utils';
  import { readyQueueIds } from '$lib/work/list';
  import { tick, untrack } from 'svelte';
  import {
    STOP_FLEET,
    commandEntries,
    itemCommands,
    needsConfirm,
    needsReason,
    offersDispatch,
    openItemOf,
    runCommand,
    viewFor,
    type PaletteCommand
  } from './actions';
  import { askPalette, asksOn, isAskQuery, type AskAnswer } from './ask';
  import { KIND_LABEL, paletteEntries, paletteKey, rankEntries } from './entries';

  let open = $state(false);
  let query = $state('');
  let cursor = $state(0);
  let epics = $state<EpicSummary[] | null>(null);
  let projection = $state<WorkProjection | null>(null);
  let field = $state<HTMLInputElement | null>(null);
  /** The item `/work?item=` had open when the palette opened. */
  let openItem = $state<string | null>(null);
  /** The item picked with → — its actions alone are listed while it is. */
  let picked = $state<string | null>(null);
  /** The dispatch plan read for the subject item, for its copy command. */
  let plan = $state<DispatchPlan | null>(null);
  /** Views re-read after a write or a 409 — newer than the projection's. */
  let fresh = $state<Record<string, WorkView>>({});
  /** The write waiting on its confirm step. */
  let pending = $state<PaletteCommand | null>(null);
  let reason = $state('');
  let busy = $state(false);
  let chip = $state<string | null>(null);
  let error = $state<string | null>(null);
  /** Where focus was when the palette opened; it goes back there on close. */
  let opener: HTMLElement | null = null;
  /** One read out at a time; a pulse landing on top of one is skipped. */
  let reading = false;
  /** Whether the server allows ask mode — off until /api/meta says so. */
  let askEnabled = $state(false);
  /** The last answer, shown while the field still holds the query it answered. */
  let asked = $state<AskAnswer | null>(null);
  let asking = $state(false);

  const subject = $derived(picked ?? openItem);
  const subjectView = $derived(viewFor(subject, projection?.views ?? null, fresh));
  const queued = $derived(
    subject !== null && readyQueueIds(factoryPulse.dashboard).has(subject)
  );
  /** Whether dispatch is the subject's next move — else no plan is read (#2785). */
  const dispatchable = $derived(offersDispatch({ itemId: subject, view: subjectView, queued }));
  const forItem = $derived(itemCommands({ itemId: subject, view: subjectView, plan, queued }));
  const entries = $derived(
    picked !== null
      ? commandEntries([...forItem, STOP_FLEET])
      : [
          ...commandEntries(forItem),
          ...paletteEntries({
            epics,
            projection,
            dashboard: factoryPulse.dashboard,
            runs: factoryPulse.runs
          }),
          ...commandEntries([STOP_FLEET])
        ]
  );
  const answered = $derived(asked !== null && asked.query === query ? asked : null);
  const results = $derived(answered?.entries ?? rankEntries(entries, query));
  const askHint = $derived(
    askEnabled && answered === null && !asking && isAskQuery(query)
  );
  const reasonMissing = $derived(pending !== null && needsReason(pending) && reason.trim() === '');

  $effect(() => claimReserved('palette', () => (open ? close() : show())));

  // #2781 — nothing reaches the page behind the box: the confirm step stops
  // only Esc on its way out, so a `j` from its button used to walk the Work
  // list underneath. The key layer drops page shortcuts while this is held.
  $effect(() => {
    if (open) return suspendShortcuts();
  });

  // Read on open, and again on each pulse reading while open — the pulse's
  // `runs` is a new array per successful tick, so this tracks the heartbeat
  // without a clock of its own.
  $effect(() => {
    void factoryPulse.runs;
    if (!open) return;
    untrack(() => void load());
  });

  // The subject's dispatch plan, read when the subject changes. A read only:
  // the command it carries is copied, never run from here. Not read at all
  // where dispatch is not the subject's next move: a `stalled_epic:*` key is
  // not an item, and an item in review has another move (#2785).
  $effect(() => {
    const item = subject;
    plan = null;
    if (!open || item === null || !dispatchable) return;
    void fetchDispatchPlan({ item }).then(
      (next) => {
        if (subject === item) plan = next;
      },
      () => {}
    );
  });

  async function load(): Promise<void> {
    if (reading) return;
    reading = true;
    try {
      const next = await fetchPaletteSources();
      // A failed read keeps what was in hand rather than blanking the list.
      if (next.epics !== null) epics = next.epics;
      if (next.projection !== null) projection = next.projection;
    } finally {
      reading = false;
    }
  }

  async function show(): Promise<void> {
    asked = null;
    void fetchMeta().then(
      (meta) => (askEnabled = meta.palette_ask === true),
      () => (askEnabled = false)
    );
    opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    query = '';
    cursor = 0;
    openItem = openItemOf(new URL(window.location.href));
    picked = null;
    fresh = {};
    clearOutcome();
    open = true;
    await tick();
    field?.focus();
  }

  function close(): void {
    open = false;
    pending = null;
    const back = opener;
    opener = null;
    if (back?.isConnected) back.focus();
  }

  function clearOutcome(): void {
    pending = null;
    reason = '';
    chip = null;
    error = null;
  }

  async function go(href: string): Promise<void> {
    // Focus follows the navigation, not the opener: the page it was on is gone.
    opener = null;
    open = false;
    await goto(href);
  }

  async function backToList(): Promise<void> {
    pending = null;
    reason = '';
    await tick();
    field?.focus();
  }

  /** Ask the model to rank what is listed. Nothing runs: it only reorders. */
  async function ask(): Promise<void> {
    if (asking) return;
    const q = query;
    asking = true;
    error = null;
    try {
      const out = await askPalette(q, entries);
      if (query !== q) return;
      asked = out.answer;
      error = out.error;
      cursor = 0;
    } finally {
      asking = false;
    }
  }

  function pick(itemId: string): void {
    picked = itemId;
    query = '';
    cursor = 0;
    clearOutcome();
  }

  /** A copy runs at once; a write opens its confirm step and waits there. */
  async function choose(command: PaletteCommand): Promise<void> {
    chip = null;
    error = null;
    if (needsConfirm(command)) {
      pending = command;
      reason = '';
      // The field the list kept focus in is gone; the step's first control
      // takes it, so the keys stay inside the dialog.
      await tick();
      document.querySelector<HTMLElement>('#palette-reason, #palette-confirm')?.focus();
      return;
    }
    const out = await runCommand(command, {});
    chip = out.chip;
    error = out.error;
  }

  async function confirm(): Promise<void> {
    const command = pending;
    if (command === null || busy || reasonMissing) return;
    busy = true;
    error = null;
    try {
      const out = await runCommand(command, { reason, confirm: true });
      chip = out.chip;
      error = out.error;
      if (out.view !== null && command.kind === 'offered') {
        fresh = { ...fresh, [command.itemId]: out.view };
      }
      // A refusal the reader can answer (not an approver, a server error)
      // keeps the step open with its reason; a landed write, or a stale
      // revision whose item was just re-read, goes back to the list.
      if (out.error === null || out.reread) await backToList();
    } finally {
      busy = false;
    }
  }

  function onkeydown(event: KeyboardEvent): void {
    if (pending !== null) {
      // The confirm step's own controls answer every key but Esc, which
      // cancels the step without writing.
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      void backToList();
      return;
    }
    if (asksOn(event.key, query, { enabled: askEnabled, asked: answered?.query ?? null })) {
      event.preventDefault();
      event.stopPropagation();
      void ask();
      return;
    }
    const action = paletteKey(event.key, cursor, results);
    if (action === null) return;
    event.preventDefault();
    event.stopPropagation();
    if (action.kind === 'move') {
      cursor = action.cursor;
      // Keep the row under the cursor on screen in a list longer than the box.
      void tick().then(() =>
        document.getElementById(`palette-${cursor}`)?.scrollIntoView?.({ block: 'nearest' })
      );
    }
    else if (action.kind === 'go') void go(action.href);
    else if (action.kind === 'command') void choose(action.command);
    else if (action.kind === 'actions') pick(action.itemId);
    // Esc leaves a picked item's actions first, then the palette.
    else if (picked !== null) unpick();
    else close();
  }

  function unpick(): void {
    picked = null;
    query = '';
    cursor = 0;
    clearOutcome();
  }

  function onpress(entry: (typeof results)[number]): void {
    if (entry.command) void choose(entry.command);
    else void go(entry.href);
  }

  function oninput(): void {
    cursor = 0;
  }
</script>

{#if open}
  <!-- The backdrop dismisses on a press of its own, not of the box; the keys
       are the dialog's (Esc included), so the backdrop needs none. -->
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div
    class="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-4 pt-[12vh]"
    onclick={(e) => e.target === e.currentTarget && close()}
  >
    <div
      class="flex max-h-[70vh] w-full max-w-xl flex-col overflow-hidden rounded-[11px] border border-hair-strong bg-ink-raised shadow-[var(--shadow)]"
      role="dialog"
      aria-modal="true"
      aria-label="Go to"
      tabindex="-1"
      use:contained
      {onkeydown}
    >
      {#if pending !== null}
        <!-- The confirm step: what will happen, the reason when one is owed,
             and an explicit confirm. Nothing is sent before the press. -->
        <form
          class="flex flex-col gap-3 p-4"
          aria-label="Confirm {pending.label}"
          onsubmit={(e) => {
            e.preventDefault();
            void confirm();
          }}
        >
          <p class="text-13 text-text-1">
            {pending.label}{#if pending.kind === 'offered'}<span class="text-text-3"
                >{' — '}{itemRef(pending.itemId, null)}</span
              >{/if}
          </p>
          <p class="text-13 text-text-3">{pending.intent}</p>
          {#if needsReason(pending)}
            <label class="sr-only" for="palette-reason">Reason</label>
            <Input
              id="palette-reason"
              bind:value={reason}
              placeholder="reason (required)"
              autocomplete="off"
            />
          {/if}
          {#if error}<p role="alert" class="text-13 text-danger"><Title text={error} /></p>{/if}
          <div class="flex gap-2">
            <Button
              id="palette-confirm"
              type="submit"
              variant={pending.kind === 'stop_fleet' ? 'danger' : 'factory'}
              disabled={busy || reasonMissing}
            >
              Confirm — {pending.label}
            </Button>
            <Button variant="ghost" onclick={() => void backToList()}>Cancel</Button>
          </div>
        </form>
      {:else}
        <!-- `shrink-0`: in the capped column the results list squeezed the
             field to a 22px line, and with no ring it did not read as focused
             when the palette opened (#2850 P18). Cyan is focus (DESIGN.md). -->
        <input
          bind:this={field}
          bind:value={query}
          {oninput}
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-results"
          aria-activedescendant={results[cursor] ? `palette-${cursor}` : undefined}
          aria-label="Go to a page, epic, item or run"
          placeholder={picked !== null
            ? `Actions for ${itemRef(picked, null)} — Esc to go back`
            : askEnabled
              ? 'Go to a page, epic, item or run — or end with ? to ask'
              : 'Go to a page, epic, item or run'}
          autocomplete="off"
          spellcheck="false"
          class="h-12 w-full shrink-0 border-b border-hair-strong bg-transparent px-4 text-13 text-text-1
                 caret-accent-2 placeholder:text-text-3 focus:outline-2 focus:-outline-offset-2 focus:outline-accent-2"
        />
        {#if asking}
          <p role="status" class="px-4 pt-2 text-13 text-text-3">Asking…</p>
        {:else if askHint}
          <p class="px-4 pt-2 text-13 text-text-3">Enter to ask — a model ranks these entries; nothing runs until you pick one.</p>
        {/if}
        {#if chip || error}
          <p role="status" class={['px-4 pt-2 text-13', error ? 'text-danger' : 'text-text-2']}>
            {error ?? chip}
          </p>
        {/if}
        <ul id="palette-results" role="listbox" aria-label="Results" class="min-h-0 overflow-y-auto py-1">
          {#each results as entry, i (entry.key)}
            <!-- The field holds focus and answers the keys for the list
                 (`aria-activedescendant`); a press on a row is the pointer's. -->
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <li
              id={`palette-${i}`}
              role="option"
              aria-selected={i === cursor}
              class={[
                'flex min-w-0 items-baseline gap-3 px-4 py-2 text-13',
                i === cursor ? 'bg-ink-selected text-text-1' : 'text-text-2'
              ]}
              onmousemove={() => (cursor = i)}
              onclick={() => onpress(entry)}
            >
              <span class="w-10 shrink-0 text-11 text-text-3">{KIND_LABEL[entry.kind]}</span>
              <span class={['max-w-[60%] shrink-0 truncate', entry.kind !== 'action' && 'font-mono']}
                >{entry.id}</span
              >
              {#if answered?.reasons[entry.key]}<span class="min-w-0 truncate text-text-3"
                  >{answered.reasons[entry.key]}</span
                >{:else if entry.title}<span class="min-w-0 truncate text-text-3"><Title text={entry.title} /></span>{/if}
            </li>
          {:else}
            <li class="px-4 py-2 text-13 text-text-3">
              {#if answered?.refusal}<Title text={answered.refusal} />{:else}{answered ? 'Nothing listed answers' : 'Nothing matches'} “{query}”.{/if}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </div>
{/if}
