<script lang="ts">
  /**
   * FUI-28 — `/budget` on the factory's hairline primitives (DMonitor readouts
   * and gauge grammar, y ≈88–258).
   *
   * No new data and no new capability: the same two spend windows, the same
   * three caps, the same audited write and revert. What changed is the grammar —
   * the cards are gone, the readouts and their ticked `Gauge` replace `CapBar`,
   * and the caps are one hairline table instead of three boxes.
   *
   * Every word in the two spend cells is built by `$lib/monitor/readouts`,
   * the module the Monitor's own readouts are built from, so the two screens
   * cannot disagree about what today has cost — and neither can drift off the
   * money rule it holds (N5: a cap is compared against `agentic`, the portion
   * the governor enforces, never against `spent`). Interactive spend is stated
   * under the gauge, never inside it: the governor does not gate it.
   *
   * `legacy-restyle-b.test.ts` holds this file to the artboard.
   */
  import { fmtDateTime } from '$lib/time';
  import { Button, DataRow, Gauge, Input, Readout, SectionHead } from '$lib/ui';
  import {
    revertBudget,
    setBudget,
    type BudgetField,
    type BudgetMap,
    type SpendCell,
    type SpendWindows
  } from '$lib/api';
  import { readoutClasses } from '$lib/factory/classes';
  import { gaugeMoney } from '$lib/factory/gauge';
  import {
    captionText,
    DAILY_CAP_FIELD,
    isUnavailable,
    priorCap,
    spendReadout,
    unreadable,
    WEEKLY_CAP_FIELD,
    type Source,
    type SpendReadout
  } from '$lib/monitor/readouts';
  import { readBudgetPage } from './reads';

  let { data }: { data: { budget: BudgetMap | null; spend: Source<SpendWindows> } } = $props();

  /**
   * Both readings are page state, and both are replaced together by every
   * write — a gauge is assembled from the two at once, so half a refresh is a
   * gauge that contradicts itself. `reads.ts` holds the reason and the rule.
   *
   * `+page.ts` reports a failed spend read as the reason to print (the read is
   * deliberately non-essential there, so a spend outage must not take the caps
   * with it), said in the same words the Monitor says it in.
   */
  let budget = $state<BudgetMap | null>(data.budget);
  let windows = $state<Source<SpendWindows>>(data.spend);

  const CAPS = [
    { key: 'budget.per_issue_usd', label: 'Per issue', hint: 'max spend per dispatched issue' },
    // The governor's windows (`_spend_windows` in kew/budget.py): today's UTC
    // day, and that day with the six days before it.
    { key: 'budget.daily_usd', label: 'Daily', hint: 'max spend across today (UTC)' },
    { key: 'budget.weekly_usd', label: 'Weekly', hint: 'max spend across today (UTC) and the six days before it' }
  ] as const;

  /**
   * Cap · effective · kew.toml · where it comes from · the two writes. 584px
   * of fixed track, 80 of gap and the gutter leave "in force" nothing to read
   * below 1024, so the row is five columns from there and two lines under it
   * (#3457).
   */
  const CAP_COLUMNS = '140px 110px 110px minmax(0, 1fr) 224px';

  /** The map as the page last read it, for the prior-cap pin on each gauge. */
  const budgetSource = $derived<Source<BudgetMap>>(budget ?? unreadable('budget', null));

  type SpendView = { readout: SpendReadout; observed: SpendCell | null };

  const spend = $derived<SpendView[]>(
    (
      [
        { label: 'Today', window: 'daily', field: DAILY_CAP_FIELD },
        { label: 'This week', window: 'weekly', field: WEEKLY_CAP_FIELD }
      ] as const
    ).map((w) => {
      // One failed spend read takes both windows with it (they arrive
      // together), but never the prior-cap pin beside them.
      const cell = isUnavailable(windows) ? windows : windows[w.window];
      return {
        readout: spendReadout(w.label, cell, priorCap(budgetSource, w.field)),
        observed: isUnavailable(cell) ? null : cell
      };
    })
  );

  /** The figure's own classes, borrowed so the "/ $250" sits on its baseline. */
  const moneyFigure = readoutClasses({ tone: 'money' }).figure;

  type DraftState = { value: string; reason: string; saving: boolean; error: string | null };

  /** The editor's one field: a 44px hairline input on the ink, as DWork's. */
  const FIELD_INPUT =
    'h-11 rounded-[4px] border-hair-strong bg-transparent text-13 text-text-1 placeholder:text-text-3';

  /** Which cap's editor is open. One at a time: a write names one field. */
  let editing = $state<string | null>(null);

  let drafts = $state<Record<string, DraftState>>(
    Object.fromEntries(
      CAPS.map((c) => [c.key, { value: '', reason: '', saving: false, error: null }])
    )
  );

  function toggle(key: string) {
    drafts[key].error = null;
    editing = editing === key ? null : key;
  }

  /**
   * Take both readings again after a write, and move the page to them at once.
   *
   * A cap write moves the cap the gauge is drawn against as well as the pin
   * beside it, and the two are served by different endpoints — so this is the
   * only way either of them is replaced. It throws what the caps read threw,
   * which the caller states on the row it was written from.
   */
  async function reread() {
    const read = await readBudgetPage();
    budget = read.budget;
    windows = read.spend;
  }

  async function save(key: string) {
    const d = drafts[key];
    const num = parseFloat(d.value);
    if (Number.isNaN(num) || num < 0) {
      d.error = 'Enter a non-negative number';
      return;
    }
    d.saving = true;
    d.error = null;
    try {
      await setBudget(key, num, d.reason);
      d.value = '';
      d.reason = '';
      await reread();
      editing = null;
    } catch (e) {
      d.error = e instanceof Error ? e.message : 'Save failed';
    } finally {
      d.saving = false;
    }
  }

  async function revert(key: string) {
    const d = drafts[key];
    d.saving = true;
    d.error = null;
    try {
      await revertBudget(key);
      await reread();
    } catch (e) {
      d.error = e instanceof Error ? e.message : 'Revert failed';
    } finally {
      d.saving = false;
    }
  }

  /**
   * Where the cap in force comes from, in words. An override says who moved it
   * and when, because that is the question a raised cap raises; otherwise the
   * cell states the committed file value's job.
   */
  function sourceNote(field: BudgetField | undefined, hint: string): string {
    if (!field) return 'not served by this kew serve';
    const ov = field.override;
    if (!ov) return `kew.toml · ${hint}`;
    const reason = ov.reason ? ` · ${ov.reason}` : '';
    return `override by ${ov.set_by} · ${fmtDateTime(ov.set_at)}${reason}`;
  }
</script>

<div class="flex min-h-0 flex-1 flex-col gap-9 overflow-y-auto px-4 pt-8 pb-9 sm:px-10">
  <header class="flex flex-col">
    <p class="text-11 text-text-3">Settings</p>
    <h1 class="mt-2 text-26 text-text-1">Budget</h1>
    <p class="mt-3 max-w-[840px] text-15 text-text-2">
      A cap is enforced against the cost of every run kew records, reviews included, over the
      governor's window: today (UTC) for the daily cap, and today with the six days before it
      for the weekly cap. A live override beats committed
      <span class="font-mono text-text-1">kew.toml</span>; every write is approver-gated and
      audited.
    </p>
  </header>

  <!-- `data-tour`: the demo's spend tour shows today's and the week's spend
       against their caps (#3149). The wrapper draws no box (`contents`) and
       is there so the highlighted section's parent is not this scrolling
       column: driver.js sets `overflow: hidden` on a highlighted element's
       scrolling parent, which would freeze the page mid-tour. -->
  <div class="contents">
    <section aria-label="Spend against cap" data-tour="budget-spend" class="grid grid-cols-2 border-y border-hair">
      {#each spend as view, i (view.readout.label)}
        {@const cell = view.readout}
        <!-- Below 640 a cell is half of a 390px screen: the percent goes under
             its label, and the cap leaves the figure for the meta and the
             gauge's ticks to say, as Monitor's spend cells do (#3457). -->
        <Readout
          label={cell.label}
          tone={cell.tone}
          meta={cell.meta}
          metaTitle={cell.metaTitle}
          stackMeta
          divided={i === 1}
          edge={i === 0 ? 'start' : 'end'}
          class={i === 0 ? 'min-w-0 pr-4 sm:pr-7' : 'min-w-0 pl-4 sm:pl-7'}
        >
          {#snippet figure()}
            <div class={moneyFigure}>
              {cell.figure}{#if cell.capLabel}<span
                  class="ml-2.5 hidden text-15 font-normal text-text-3 sm:inline"
                  >/ {cell.capLabel}</span
                >{/if}
            </div>
          {/snippet}
          {#if cell.caption.length > 0}
            <!-- A spend cell's only caption is the reason it could not be read. -->
            <div class="text-15 text-text-2">{captionText(cell.caption)}</div>
          {/if}
          {#if cell.gauge}
            <!--
              A marker labels itself above the track, so a marked gauge needs the
              headroom the artboard gives it: 26px against 8px unmarked.
            -->
            <div style:margin-top={cell.gauge.marker ? '26px' : '8px'}>
              <Gauge spent={cell.gauge.spent} cap={cell.gauge.cap} marker={cell.gauge.marker} />
            </div>
          {/if}
          {#if view.observed && view.observed.interactive > 0}
            <!--
              Interactive (Claude Code OTel) spend is observed over the same
              window, and the governor does NOT gate it — so it is said beneath
              the gauge in words, never drawn against the cap.
            -->
            <div class="mt-2.5 flex items-center gap-2 font-mono text-[11px] text-text-3">
              <span class="inline-block h-1.5 w-1.5 rounded-full bg-accent-2"></span>
              <span>interactive {gaugeMoney(view.observed.interactive)}</span>
              <span>· observed · not enforced</span>
            </div>
          {/if}
        </Readout>
      {/each}
    </section>
  </div>

  <section aria-label="Budget caps" class="flex flex-col">
    <SectionHead label="Budget caps" meta="API-equivalent list price" />

    {#if !budget}
      <p class="font-mono text-[11px] text-text-3">
        caps not read — is kew serve running?
      </p>
    {:else}
      <DataRow columns={CAP_COLUMNS} head class="hidden lg:grid" data-cap-head>
        <span>Cap</span><span class="text-right">Effective</span><span class="text-right"
          >kew.toml</span
        ><span>In force</span><span></span>
      </DataRow>

      {#each CAPS as cap, i (cap.key)}
        {@const field = budget[cap.key]}
        {@const d = drafts[cap.key]}
        <!-- Below 1024 the cap and its two figures, then where it comes from
             and the writes; the head is gone there, so the file value says
             what it is. -->
        <DataRow
          columns={CAP_COLUMNS}
          rule={i === CAPS.length - 1 && editing !== cap.key ? 'none' : 'quiet'}
          class="flex h-auto flex-wrap items-center gap-x-5 gap-y-2 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0"
          data-cap-row
        >
          <span class="text-15 text-text-1">{cap.label}</span>
          <span class="ml-auto text-right font-mono text-13 text-warn lg:ml-0"
            >{field ? gaugeMoney(field.effective) : '—'}</span
          >
          <span class="text-right font-mono text-13 text-text-3"
            ><span class="mr-2 lg:hidden">kew.toml</span>{field ? gaugeMoney(field.file_value) : '—'}</span
          >
          <!-- The line break, below `lg` only; no cell of the grid above it. -->
          <span class="basis-full lg:hidden" aria-hidden="true"></span>
          <span class="min-w-0 flex-1 text-15 wrap-anywhere text-text-2 lg:truncate"
            >{sourceNote(field, cap.hint)}</span
          >
          <span class="ml-auto flex shrink-0 justify-end gap-2.5 lg:ml-0">
            <Button
              variant="factory"
              aria-expanded={editing === cap.key}
              disabled={d.saving}
              onclick={() => toggle(cap.key)}>Override…</Button
            >
            {#if field?.override}
              <Button variant="factory" disabled={d.saving} onclick={() => revert(cap.key)}
                >Revert</Button
              >
            {/if}
          </span>
        </DataRow>

        {#if editing === cap.key}
          <!-- The editor opens under the row it writes, rather than over the
               page: a cap is read against the two gauges above it, and a dialog
               would cover the reading the operator is deciding from. -->
          <div
            role="group"
            aria-label={`Override the ${cap.label} cap`}
            class="flex flex-wrap items-end gap-3 pt-4 pb-5"
          >
            <div class="flex flex-col gap-1.5">
              <label class="sr-only" for={`budget-value-${cap.key}`}
                >New {cap.label} cap in USD</label
              >
              <Input
                id={`budget-value-${cap.key}`}
                type="number"
                placeholder={field ? gaugeMoney(field.effective).replace('$', '') : '0.00'}
                bind:value={d.value}
                disabled={d.saving}
                class={`${FIELD_INPUT} w-[9rem] font-mono`}
              />
            </div>
            <div class="flex min-w-[14rem] flex-1 flex-col gap-1.5">
              <label class="sr-only" for={`budget-reason-${cap.key}`}
                >Reason for the {cap.label} override</label
              >
              <Input
                id={`budget-reason-${cap.key}`}
                type="text"
                placeholder="reason (optional, audited)"
                bind:value={d.reason}
                disabled={d.saving}
                class={`${FIELD_INPUT} w-full`}
              />
            </div>
            <Button variant="factory" disabled={d.saving || !d.value} onclick={() => save(cap.key)}
              >{d.saving ? 'Saving…' : 'Save'}</Button
            >
            <Button variant="factory" disabled={d.saving} onclick={() => toggle(cap.key)}
              >Cancel</Button
            >
            {#if d.error}
              <p class="w-full text-13 text-danger">{d.error}</p>
            {/if}
          </div>
        {:else if d.error}
          <p class="pb-4 text-13 text-danger">{d.error}</p>
        {/if}
      {/each}
    {/if}
  </section>
</div>
