<script lang="ts">
  /**
   * #2302 — "Plan a dispatch", on a Work item; #2535 — disclosed progressively.
   *
   * Closed, it is ONE line: the route (`runner / model`), the verdict (ready,
   * or refused and the one phrase that says why), and the primary action —
   * copying the command. Everything else is a press away, in a side drawer:
   * the two inputs first, then the resolved route, then only the rows that are
   * not what the operator would assume (overridden or invalid), with "show all
   * fields" for the rest. It used to open all of that inline — fourteen rows,
   * two refusal paragraphs and a remedy block jammed under the item.
   *
   * Three rules shape it:
   *
   * * **One read per question, never a poll.** The closed line is a route and
   *   a verdict, so it needs a plan: one read when an item is shown, and one
   *   each time a choice changes — and the ITEM is one of those choices, so a
   *   selection that moves drops the plan in hand and re-resolves rather than
   *   leaving the last item's route under this item's header. The host facts
   *   behind it come from the serve sweep's probe cache and a TTL'd resolver,
   *   so a read inspects nothing the sweep has not already (#2285).
   * * **A slow old answer never wins.** Every read carries a monotonic ticket
   *   (`PlanRequests`); an answer the operator has moved past is dropped on
   *   arrival rather than painted over the newer one.
   * * **It never dispatches.** Run is the one verb (#3055): off, it says why
   *   beside it. A dispatch happens when the command runs: Run hands it to the
   *   running `kew drive` as a ledger request (#2537), and the line under the
   *   route says what the drive did; Copy hands it to a terminal.
   *
   * One choice is the operator's own (#2741): a model for this one dispatch,
   * from the catalog, limited to the runner the plan resolved. Untouched, it
   * sends nothing and the route's model runs. Chosen, Run sends it beside the
   * command — the server re-checks it against the catalog and the runner, and
   * the dispatch's `routing_decision` attests it as a "console launchpad"
   * override — and Copy carries it as `--model`. Either way the one row marked
   * "✓ runs now" is the model that will run (#2895).
   *
   * Every word, state and blocker comes from `$lib/work/launchpad` — this file
   * draws them and owns nothing but the controls, the drawer and the read.
   */
  import { onMount, untrack } from 'svelte';
  import { fetchDispatchPlan, fetchModels, type CatalogModel, type DispatchPlan } from '$lib/api';
  import ModelPicker from '$lib/ModelPicker.svelte';
  import { canonicalModel, runnerModels } from '$lib/model-picker';
  import { dataRowClasses, TAP_PILL } from '$lib/factory/classes';
  import RunButton from '$lib/RunButton.svelte';
  import {
    isRunnable,
    pairClear,
    pairHeld,
    pairLedgers,
    pendingRuns,
    watchPair,
    type RunLine
  } from '$lib/run-request';
  import { Button, Command, SectionHead, StatusMark } from '$lib/ui';
  import { contained, onTop } from '$lib/ui/contained';
  import { reservedPress, suspendShortcuts } from '$lib/keys/registry';
  import {
    COMPLEXITY_CONTROL_ID,
    PlanRequests,
    STATE_LABEL,
    STATE_MARK,
    attentionRows,
    commandNote,
    dispatchCommand,
    invalidRows,
    launchBlockers,
    noCommandReason,
    planRows,
    planVerdict,
    routeName,
    routeSummary,
    type PlanRow
  } from './launchpad';

  let {
    itemId,
    read = fetchDispatchPlan,
    readModels = () => fetchModels().then((payload) => payload.models)
  }: {
    /** The item this plan is for: an issue number, or a tracker key. */
    itemId: string;
    /** Injected in tests; the one read this panel takes. */
    read?: typeof fetchDispatchPlan;
    /** Injected in tests; the catalog read, taken once, when the drawer opens. */
    readModels?: () => Promise<CatalogModel[]>;
  } = $props();

  const TIERS = ['simple', 'medium', 'complex'] as const;
  const LANES = ['implement', 'review', 'remediation'] as const;

  /** Whether the drawer is open. The closed line is always on screen. */
  let open = $state(false);
  let plan = $state<DispatchPlan | null>(null);
  let notice = $state('');
  let reading = $state(false);
  /**
   * The operator's tier; `''` is untouched — the plan resolves the item's own
   * (the ready queue's label) or the default lane's, and the select shows it.
   */
  let complexity = $state('');
  /**
   * The tier the plan resolved with no tier sent — the item's own — which the
   * untouched option names; `''` when that read resolved none.
   */
  let itemTier = $state('');
  const untouchedTier = $derived(itemTier === '' ? 'default' : `from item: ${itemTier}`);
  let lane = $state<string>('implement');
  let copied = $state(false);
  let copyFlash: ReturnType<typeof setTimeout> | null = null;
  /** The catalog the model control picks from; null until read. */
  let models = $state<CatalogModel[] | null>(null);
  let modelsNotice = $state('');
  /** The operator's model for this dispatch; `''` is untouched — no override. */
  let model = $state('');
  /** "show all fields": the set and default rows, folded by default. */
  let allFields = $state(false);
  /** Which remedies have their `details` open, by field. */
  let detailed = $state<Record<string, boolean>>({});
  /** The control that opened the drawer, which gets the focus back. A plain
   *  field: nothing renders from it. */
  let opener: HTMLElement | null = null;

  /**
   * The read tickets. A plain field, not `$state`: nothing renders from it, and
   * reactive state an effect both read and wrote would re-run itself.
   */
  const reads = new PlanRequests();

  /**
   * Which item the plan in hand was read for — the work page's `detailItem`,
   * one panel down. A plain field for the same reason `reads` is: nothing
   * renders from it, and reactive state the effect below both read and wrote
   * would re-run itself. It starts as the mounted item — `untrack` because
   * capturing the initial value is the whole point, and because an effect that
   * found it unset on its first run would take a second read on mount.
   */
  let planItem = untrack(() => itemId);

  /**
   * The lane and tier the plan in hand answered. A changed choice whose read
   * failed leaves the last good plan on screen (a dropped read is not evidence
   * the route changed), but that plan answers another choice, and its Run
   * would launch the lane no longer selected (#2975).
   */
  let planChoice = $state<{ lane: string; tier: string | null } | null>(null);

  const rows = $derived(planRows(plan));
  const invalid = $derived(invalidRows(rows));
  const shown = $derived(allFields ? rows : attentionRows(rows));
  const folded = $derived(rows.length - attentionRows(rows).length);
  const command = $derived(dispatchCommand(plan));
  /** The models this dispatch may take: the resolved runner's routable ones. */
  const routable = $derived(
    plan === null || models === null ? [] : runnerModels(models, plan.runner.value)
  );
  /**
   * The route's model as a catalog id: MODEL may name an alias (#2895).
   */
  const routeModel = $derived(
    plan === null || models === null ? '' : canonicalModel(models, plan.model.value)
  );
  /**
   * The rows the control shows: the routable ones, plus the route's model even
   * when the catalog would filter it out — not routable, or retired — so the
   * model that runs always has the "runs now" row (#2895).
   */
  const modelChoices = $derived(
    plan === null || models === null ? [] : runnerModels(models, plan.runner.value, routeModel)
  );
  /**
   * The override in force: the choice, while the plan's runner still offers
   * it. A re-resolve onto another runner drops it rather than sending a model
   * the server would refuse; so does picking a route's model the catalog does
   * not route, which is the model that runs untouched anyway.
   */
  const override = $derived(
    command !== '' && routable.some((m) => m.id === model) ? model : ''
  );
  /** What Copy hands a terminal: the command, and the override as its flag. */
  const copyText = $derived(override === '' ? command : `${command} --model ${override}`);
  const runnable = $derived(isRunnable(command));
  let runLine = $state<RunLine | null>(null);
  /** This panel's Run button reports its POST open. */
  let runPosting = $state(false);
  /** The closed line's Run, which the drawer's Run presses (#3055). */
  let runButton = $state<{ press: () => void } | null>(null);
  /**
   * Run's POST is open: the route it carries is fixed until it answers. The
   * store, not the button, is the record (#2975) — a button that unmounted
   * mid-POST leaves the POST going, so a Run for this item, or for the pair
   * on screen, still holds the lane, the tier and the model.
   */
  const runSending = $derived(
    runPosting ||
      $pendingRuns.some(
        (run) =>
          run.itemId === itemId ||
          (run.command === command && run.model === (override || null))
      )
  );
  /**
   * The plan in hand answers a choice no longer on screen, and no read is
   * resolving the one that is: its route is shown in the drawer, but its Run
   * and Copy are not this choice's dispatch, so neither is offered.
   */
  const stale = $derived(
    plan !== null &&
      !reading &&
      (planChoice?.lane !== lane || planChoice.tier !== (complexity || null))
  );
  /**
   * The ledger answered for the pair on screen and holds nothing of it
   * unsettled (#2975). A command the drive cannot run is never queued, so it
   * has no ledger to wait on. Watched here, not only by Run: the drawer's Copy
   * shows without a Run button, and must not hand over a command already
   * queued.
   */
  const runClear = $derived(!runnable || pairClear($pairLedgers, command, override || null));
  $effect(() => {
    const watched = command;
    const chosen = override || null;
    if (!isRunnable(watched)) return;
    return untrack(() => watchPair(watched, chosen));
  });
  /**
   * The plan's command is this choice's dispatch, and handing it over starts
   * nothing beside a Run already made: it answers the lane and tier on screen,
   * no read is replacing it, no POST of it is open, and the ledger answered
   * with nothing of it unsettled. Every surface that hands the command over —
   * Run, both Copies, `copy()` — asks this one predicate (#2975).
   */
  const actionable = $derived(plan !== null && !reading && !stale && !runSending && runClear);
  const note = $derived(commandNote(plan, override));
  const noCommand = $derived(noCommandReason(plan));
  const verdict = $derived(plan === null ? null : planVerdict(plan));
  const runOffId = $derived(`plan-run-off-${itemId}`);
  /**
   * Why the drawer's Run is off, as a sentence, or `''` while it is on
   * (#3055). The plan is asked first, then this choice's own holds: a read in
   * flight, a Run sending, a request of this pair still open.
   */
  const runOff = $derived.by(() => {
    if (plan === null) {
      return reading
        ? 'Run waits for the route to resolve.'
        : 'Run is off because the plan could not be read.';
    }
    if (command === '') {
      return `Run is off because there is no command to run: ${noCommand || 'this plan has none'}.`;
    }
    if (!runnable) return 'Run is off because the drive does not take this command; copy it instead.';
    if (verdict?.ready !== true) return `Run is off: ${verdict?.reason ?? 'the plan is not ready'}.`;
    if (stale) return "Run is off because this lane's route could not be resolved.";
    if (reading) return 'Run waits for the route to resolve.';
    if (runSending) return 'Run is off while a Run of this item is sending.';
    if (!runClear) {
      return pairHeld($pairLedgers, command, override || null)
        ? 'Run is off while a Run of this command is still open.'
        : "Run waits for the drive's ledger to answer.";
    }
    return '';
  });
  /** The drawer's Run: the closed line's, pressed, with the drawer closed so
   *  the drive's answer under it is in view. */
  function runFromDrawer(): void {
    if (runOff !== '' || runButton === null) return;
    runButton.press();
    closeDrawer();
  }
  const blockers = $derived(
    plan === null && !reading ? [] : launchBlockers(plan, { brief: true })
  );

  /**
   * Take a plan read. The ticket is taken BEFORE the await, so a second change
   * while this one is in flight supersedes it; the answer of a superseded read
   * is dropped rather than believed.
   */
  async function revalidate(): Promise<void> {
    const ticket = reads.begin();
    reading = true;
    const asked = itemId;
    const sentTier = complexity || null;
    const sent = lane;
    try {
      const next = await read({ item: asked, complexity: sentTier, lane: sent });
      if (!reads.owns(ticket)) return;
      if (next.item !== asked) {
        // A plan for another item under this item's header would hand the
        // operator another item's command (#2845 P5), so it is refused, not
        // painted.
        plan = null;
        notice = `the plan read answered for item ${next.item ?? 'none'}, not item ${asked}, so it is not shown`;
        return;
      }
      plan = next;
      planChoice = { lane: sent, tier: sentTier };
      notice = '';
      if (sentTier === null) itemTier = next.complexity.known ? next.complexity.value : '';
    } catch {
      if (!reads.owns(ticket)) return;
      // Said in place, as a sentence: no toast, and the last good plan is kept
      // rather than blanked — a dropped read is not evidence the route changed.
      notice = 'the route could not be resolved — is kew serve still running?';
    } finally {
      if (reads.owns(ticket)) reading = false;
    }
  }

  onMount(() => {
    void revalidate();
  });

  /**
   * The selection moved. Another item's route, command and blockers under this
   * item's header is a lie about this item — the work page drops its pipeline,
   * evidence and description on the same event for the same reason — so the
   * plan is dropped here rather than left on screen until a read lands.
   *
   * The new ticket supersedes a read for the previous item still in flight, so
   * its answer is never written to a panel that is now about something else.
   */
  $effect(() => {
    const next = itemId;
    if (next === planItem) return;
    planItem = next;
    plan = null;
    itemTier = '';
    // The tier picked was a choice about the last item (#2895): kept, the
    // untouched option would name "default" for an item with a tier of its own.
    complexity = '';
    notice = '';
    detailed = {};
    model = '';
    // A Run's line is about its own item (#2975): its submission stays in the
    // store, and the item's button shows its answer when the item is back.
    runLine = null;
    void revalidate();
  });

  function openDrawer(event: MouseEvent): void {
    opener = event.currentTarget as HTMLElement;
    open = true;
    // A press on Details after a dropped read is the operator asking again.
    if (plan === null && !reading) void revalidate();
    if (models === null) void readCatalog();
  }

  /** The catalog, read once. A failure is said where the control would be. */
  async function readCatalog(): Promise<void> {
    modelsNotice = '';
    try {
      models = await readModels();
    } catch {
      modelsNotice = 'the model catalog could not be read — reopen Details to try again';
    }
  }

  function closeDrawer(): void {
    open = false;
    // Back to the control that opened it: the drawer's own controls are gone.
    opener?.focus();
  }

  // No page shortcut answers behind an `aria-modal` box (#2781) — a `j` from a
  // button in here moved the selection the drawer was planning.
  $effect(() => {
    if (open) return suspendShortcuts();
  });

  /**
   * Keys inside the drawer stay inside it, bar the two reserved presses: ⌘K
   * and `?` go on to the key layer, which suspends the page's shortcuts while
   * the drawer is open but still hands those to the palette and help (#2781).
   * Escape closes it. Tab is `use:contained`'s, which listens on the document
   * ahead of this.
   */
  function onDrawerKey(event: KeyboardEvent): void {
    if (reservedPress(event) !== null) return;
    event.stopPropagation();
    // A box opened over the drawer owns Escape (#2841).
    if (event.key !== 'Escape') return;
    if (!(event.currentTarget instanceof Element) || !onTop(event.currentTarget)) return;
    event.preventDefault();
    closeDrawer();
  }

  /** A choice changed: re-resolve. The guard is the ticket, not a debounce. */
  function choose(next: () => void): void {
    // A Run in flight carries this route; re-routing now would hide it (#2975).
    if (runSending) return;
    next();
    void revalidate();
  }

  function focusFix(row: PlanRow): void {
    if (row.fix?.focus === undefined) return;
    document.getElementById(row.fix.focus)?.focus();
  }

  async function copy(): Promise<void> {
    if (command === '' || !actionable) return;
    try {
      await navigator.clipboard.writeText(copyText);
    } catch {
      // Clipboard unreachable. The `<code>` block is still the command.
      return;
    }
    copied = true;
    if (copyFlash !== null) clearTimeout(copyFlash);
    copyFlash = setTimeout(() => {
      copied = false;
      copyFlash = null;
    }, 1500);
  }

  const GRID = 'minmax(0, 7rem) minmax(0, 1fr) 110px';
  /**
   * The table's 50px row is a FLOOR here, not a height (#2522). An invalid row
   * carries the place that fixes it on a second grid line, and a fixed 50px row
   * has no height for one: the link was laid out below the row's own border and
   * on top of the next row's value. Every row without a fix still measures
   * exactly 50px — one 19.5px line inside 2×8px of padding is under the floor —
   * so the rows the artboard draws are unchanged, and only a row carrying a fix
   * grows, by the one line it drew in someone else's row before. `gap-y-1` is
   * the space between those two lines: the table's 20px column gap would be a
   * chasm between a value and the place that fixes it.
   */
  const row = dataRowClasses({ height: 50, class: 'h-auto min-h-[50px] gap-y-1 py-2' });
  const headRow = dataRowClasses({ head: true, height: 50 });
  const SELECT = `rounded-[4px] border border-hair-strong bg-ink px-2 py-1 font-mono text-13
                  text-text-1 ${TAP_PILL}`;
</script>

<section aria-label="Plan a dispatch" class="mt-[18px] flex min-w-0 flex-col">
  <SectionHead label="Plan a dispatch" />

  <!-- Closed: ONE line (#2535), held to it by `nowrap` — the route and the
       verdict give way (truncate) before the actions do, and the full route is
       in the drawer and on the hover. -->
  <div data-plan-summary class="flex min-w-0 items-center gap-3 whitespace-nowrap py-3">
    {#if plan !== null && verdict !== null && !stale}
      <!-- The model by name; the id the resolver sent is the hover (#2785). -->
      {@const summary = routeName(plan, override)}
      {@const ids =
        override === '' ? routeSummary(plan) : `${plan.runner.value} / ${override} (override)`}
      <span class="min-w-0 truncate font-mono text-13 text-text-1" title={ids}>{summary}</span>
      <!-- The verdict word never gives way; the reason after it does. The
           word is a label (`text-11`), the reason a phrase (`text-13`). -->
      <span class="flex min-w-0 items-center gap-1.5 text-text-3">
        <StatusMark
          kind={verdict.ready ? STATE_MARK.set : STATE_MARK.invalid}
          vocabulary="run"
          size={12}
          class="shrink-0"
        />
        <span class="shrink-0 text-11">{verdict.ready ? 'ready' : 'refused'}</span>
        {#if !verdict.ready}
          <span class="min-w-0 truncate text-13" title={verdict.reason}>— {verdict.reason}</span>
        {/if}
      </span>
    {:else}
      <span class="min-w-0 truncate text-13 text-text-3" aria-live="polite">
        {reading ? 'resolving the route…' : notice !== '' ? 'the route could not be resolved' : ''}
      </span>
    {/if}
    <span class="ml-auto flex shrink-0 items-center gap-2">
      {#if command !== '' && verdict?.ready === true && !stale}
        <!-- The primary action: running the command is the move this panel
             exists to hand over, so copying it needs no drawer. A refused plan's
             primary action is Details — what is in the way — and the line's
             room goes to the reason instead. Quiet, never magenta (#3467 D29):
             an idle item is not a needs-you, and DESIGN.md `button-primary`
             spends the fill only on the action that clears one. -->
        <RunButton
          {command}
          {itemId}
          model={override || null}
          variant="factory"
          held={!actionable}
          bind:this={runButton}
          bind:line={runLine}
          bind:sending={runPosting}
        />
        <Button
          variant="factory"
          size="sm"
          aria-label="Copy command"
          disabled={!actionable}
          onclick={copy}>{copied ? 'Copied' : 'Copy'}</Button
        >
      {/if}
      <Button
        variant="factory"
        size="sm"
        class={TAP_PILL}
        aria-haspopup="dialog"
        aria-expanded={open}
        onclick={openDrawer}>Details</Button
      >
    </span>
  </div>
  {#if runLine !== null}
    <!-- What the drive did with the Run (#2537), in its own words: a refusal
         lands here, under the line that issued it. -->
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

  {#if open}
    <!-- The drawer: a side sheet over the page, full width on a phone. The box
         is the console's dialog grammar (Config's): a hairline on the raised
         ink, `aria-modal` kept by `use:contained`. A press on the scrim closes
         it. -->
    <div
      role="presentation"
      class="fixed inset-0 z-50 flex justify-end bg-black/60"
      onclick={(e) => e.target === e.currentTarget && closeDrawer()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Plan a dispatch · item ${itemId}`}
        tabindex="-1"
        use:contained
        onkeydown={onDrawerKey}
        class="h-full w-full max-w-[520px] min-w-0 overflow-x-hidden overflow-y-auto border-l
               border-hair-box bg-ink-raised px-5 py-4 shadow-[var(--shadow)]"
      >
        <!-- A block, not a flex column: in a column that scrolls, every row
             would shrink to its 50px floor and the #2522 fix line would be
             drawn over the next row again. -->
        <div class="flex items-center gap-3">
          <h2 class="min-w-0 truncate text-13 text-text-1">Plan a dispatch</h2>
          <span class="truncate text-11 text-text-4">item {itemId}</span>
          <Button variant="ghost" size="sm" class={`ml-auto ${TAP_PILL}`} onclick={closeDrawer}
            >Close</Button
          >
        </div>

        <!-- The inputs first. Untouched, `complexity` sends nothing, so the
             server keeps deciding it, and its option names the tier the plan
             resolved — "from item: medium" for the ready queue's label, or
             `default` (#2785). It is an option of its own, so picking it back
             stays on it rather than snapping to the tier it names (#2845 Q5). -->
        <div class="flex flex-wrap items-center gap-5 py-3">
          <label class="flex items-center gap-2 text-11 text-text-3" for={COMPLEXITY_CONTROL_ID}>
            complexity
            <select
              id={COMPLEXITY_CONTROL_ID}
              class={SELECT}
              disabled={runSending}
              value={complexity}
              onchange={(event) =>
                choose(() => (complexity = (event.currentTarget as HTMLSelectElement).value))}
            >
              <option value="">{untouchedTier}</option>
              {#each TIERS as tier (tier)}
                <option value={tier}>{tier}</option>
              {/each}
            </select>
          </label>
          <label class="flex items-center gap-2 text-11 text-text-3" for="plan-lane">
            lane
            <select
              id="plan-lane"
              class={SELECT}
              disabled={runSending}
              value={lane}
              onchange={(event) =>
                choose(() => (lane = (event.currentTarget as HTMLSelectElement).value))}
            >
              {#each LANES as entry (entry)}
                <option value={entry}>{entry}</option>
              {/each}
            </select>
          </label>
          <span class="text-11 text-text-4" aria-live="polite" data-route-note
            >{reading ? 'resolving…' : runSending ? 'held while Run is sending' : ''}</span
          >
        </div>

        <!-- Then the resolved route, whole: the model id exactly as resolved,
             wrapped rather than cut, because `claude-opus-5-5` cut short is
             `claude-opus-5`, a different model. -->
        {#if plan !== null}
          <dl class="m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 border-t border-hair py-3">
            <dt class="text-11 text-text-3">runner</dt>
            <dd class="m-0 min-w-0 break-all font-mono text-13 text-text-1" data-route="runner">
              {plan.runner.value || '—'}
            </dd>
            <dt class="text-11 text-text-3">model</dt>
            <dd class="m-0 min-w-0 break-all font-mono text-13 text-text-1" data-route="model">
              {plan.model.value || '—'}
            </dd>
          </dl>
        {/if}

        {#if plan !== null && command !== ''}
          <!-- The one choice that is the operator's (#2741): a model for this
               dispatch only, from the resolved runner's catalog rows. -->
          <section
            aria-label="Model for this dispatch"
            class="flex min-w-0 flex-col gap-1.5 border-t border-hair py-3"
          >
            <div class="flex min-h-8 items-center gap-3">
              <span class="text-11 text-text-3">model for this dispatch</span>
              {#if override !== ''}
                <Button
                  variant="ghost"
                  size="sm"
                  class={`ml-auto ${TAP_PILL}`}
                  disabled={runSending}
                  onclick={() => (model = '')}>Use the route's model</Button
                >
              {/if}
            </div>
            <!-- A sentence, so data-13 text-3, not a label in capitals (#2845). -->
            <p class="m-0 text-13 text-text-3" data-model-note>
              {runSending
                ? 'held while Run is sending: the model goes with the request'
                : override === ''
                ? "untouched: the route's model runs, and no override is sent"
                : `sent with Run as this dispatch's model; Copy adds --model ${override}`}
            </p>
            {#if models === null}
              <p class="m-0 text-13 text-text-3">{modelsNotice || 'reading the model catalog…'}</p>
            {:else if modelChoices.length === 0}
              <p class="m-0 text-13 text-text-3">
                the catalog lists no routable {plan.runner.value || 'runner'} model
              </p>
            {:else}
              <ModelPicker
                models={modelChoices}
                value={override}
                current={override || routeModel}
                currentLabel="runs now"
                label="Model for this dispatch"
                disabled={runSending}
                onpick={(id) => (model = id)}
              />
            {/if}
          </section>
        {/if}

        {#if notice !== ''}
          <p class="py-2 text-13 text-danger">{notice}</p>
        {/if}

        {#if invalid.length > 0}
          <!-- One short line per remedy: the row that is wrong, in a phrase,
               and the place that fixes it. The full reason — a refusal can be
               a paragraph — is behind `details`. -->
          <section aria-label="What needs fixing" class="flex flex-col border-y border-hair py-2">
            {#each invalid as bad (bad.field)}
              <div class="flex flex-col py-1.5">
                <div class="flex items-center gap-2.5 text-13 text-text-1">
                  <StatusMark
                    kind={STATE_MARK.invalid}
                    vocabulary="run"
                    size={12}
                    class="shrink-0"
                  />
                  <!-- The link is INSIDE the sentence, not a flex sibling of it
                       (#2522), so it flows after the last word and `nowrap`
                       keeps it whole. -->
                  <span class="min-w-0"
                    >{bad.field} — {bad.brief ?? bad.value}{' '}{#if bad.fix?.href}<a
                        class="whitespace-nowrap text-text-3 hover:text-text-1 hover:underline"
                        href={bad.fix.href}>{bad.fix.label} →</a
                      >{:else if bad.fix?.focus}<button
                        type="button"
                        class="whitespace-nowrap text-text-3 hover:text-text-1 hover:underline"
                        onclick={() => focusFix(bad)}>{bad.fix.label} →</button
                      >{/if}{' '}<button
                      type="button"
                      class="whitespace-nowrap text-text-4 hover:text-text-1 hover:underline"
                      aria-expanded={detailed[bad.field] === true}
                      onclick={() => (detailed[bad.field] = !detailed[bad.field])}>details</button
                    ></span
                  >
                </div>
                {#if detailed[bad.field]}
                  <p class="m-0 mt-1 pl-[22px] text-13 break-words text-text-3">{bad.source}</p>
                {/if}
              </div>
            {/each}
          </section>
        {/if}

        {#if rows.length === 0}
          <p class="py-4 text-13 text-text-3">
            {reading ? 'resolving the route…' : 'no plan has been read yet.'}
          </p>
        {:else}
          {#if shown.length > 0}
            <div class={`${headRow} mt-3`} style:grid-template-columns={GRID}>
              <span>field</span><span>resolved · source</span><span class="text-right">state</span>
            </div>
          {/if}
          {#each shown as entry (entry.field)}
            <div class={row} style:grid-template-columns={GRID}>
              <span class="truncate text-11 text-text-3">{entry.field}</span>
              <!-- One line, not a wrapping one: the drawer's value column is
                   narrower than the page's was, and a wrapped source grew rows
                   that carry no fix past the 50px the table draws. The source
                   gives way first; its whole text is on the hover. -->
              <span class="flex min-w-0 items-baseline gap-2">
                <span class="max-w-full shrink-0 truncate font-mono text-13 text-text-1"
                  >{entry.value}</span
                >
                <!-- The arrow IS the provenance (the artboard's own row shape):
                     `runner codex ← kew.toml [routing].simple`. -->
                <!-- data-13, not label-11: a source is often a phrase
                     ("the ready queue's `simple` label") (#2845). -->
                <span class="min-w-0 truncate text-13 text-text-4" title={entry.source}
                  >← {entry.source}</span
                >
              </span>
              <span class="flex justify-end">
                <StatusMark
                  kind={STATE_MARK[entry.state]}
                  vocabulary="run"
                  word
                  label={STATE_LABEL[entry.state]}
                  size={12}
                  class="text-11"
                />
              </span>
              <!-- On the row itself, not only in the band above: an invalid
                   row is the thing the reader is looking at, so what fixes it
                   is one click from there. A SECOND GRID LINE of this row,
                   under the source and starting in its column (#2522), so the
                   row grows by it instead of drawing it over the next row;
                   `truncate` is the floor under a label that outgrows it. -->
              {#if entry.fix?.href}
                <a
                  class="col-span-2 col-start-2 w-fit max-w-full truncate text-11 text-text-3
                         hover:text-text-1 hover:underline"
                  href={entry.fix.href}>{entry.fix.label} →</a
                >
              {:else if entry.fix?.focus}
                <button
                  type="button"
                  class="col-span-2 col-start-2 w-fit max-w-full truncate text-left text-11
                         text-text-3 hover:text-text-1 hover:underline"
                  onclick={() => focusFix(entry)}>{entry.fix.label} →</button
                >
              {/if}
            </div>
          {/each}
          {#if folded > 0}
            <!-- A control, so a quiet button in data type — not a label. -->
            <Button
              variant="factory"
              size="sm"
              class={`mt-2 w-fit ${TAP_PILL}`}
              aria-expanded={allFields}
              onclick={() => (allFields = !allFields)}
              >{allFields ? 'fewer fields ▴' : `${folded} more fields ▸`}</Button
            >
          {/if}
        {/if}

        <!-- The command block (DESIGN.md `command-block`): the command as
             typed, in a real `<code>` so it can be selected by hand when the
             clipboard is unreachable. -->
        <div class="mt-[18px] flex flex-col gap-2">
          {#if command !== '' && stale}
            <p class="m-0 text-13 text-text-3" data-command-stale>
              this lane's route could not be resolved, so the {planChoice?.lane} command is not
              offered
            </p>
          {:else if command !== ''}
            <div
              class="flex min-w-0 items-center gap-2.5 rounded-[7px] border border-hair-box bg-ink px-3 py-2"
            >
              <span aria-hidden="true" class="shrink-0 font-mono text-13 text-text-5">$</span>
              <Command
                command={copyText}
                class="min-w-0 flex-1 font-mono text-13 text-text-1"
              />
              <Button
                variant="factory"
                size="sm"
                aria-label="Copy command"
                disabled={!actionable}
                onclick={copy}
                class="shrink-0">{copied ? 'Copied' : 'Copy'}</Button
              >
            </div>
            {#if note !== ''}
              <span class="text-13 text-text-4">{note}</span>
            {/if}
          {:else if noCommand !== ''}
            <p class="text-13 text-text-3">{noCommand}</p>
          {/if}

          <div class="flex flex-wrap items-center gap-4">
            <!-- One verb for one action (#3055): the button is Run, the word
                 the note under it uses. When it is off, the sentence beside it
                 says why, and the button names that sentence. It presses the
                 closed line's Run, so there is one Run and one line saying
                 what the drive did. Quiet (DESIGN.md `button-quiet`), as every
                 control on this panel is (#3467 D29). -->
            <Button
              variant="factory"
              size="sm"
              class={TAP_PILL}
              disabled={runOff !== ''}
              aria-describedby={runOff === '' ? undefined : runOffId}
              data-drawer-run
              onclick={runFromDrawer}>Run</Button
            >
            {#if runOff !== ''}
              <p id={runOffId} class="m-0 min-w-0 text-13 text-text-3" data-run-off>{runOff}</p>
            {/if}
            <ul class="m-0 flex w-full min-w-0 list-none flex-col gap-1 p-0" data-launch-note>
              {#each blockers as blocker (blocker)}
                <li class="text-13 text-text-4">{blocker}</li>
              {/each}
            </ul>
          </div>
        </div>
      </div>
    </div>
  {/if}
</section>
