<script lang="ts">
  /**
   * FUI-27 — the loops page, restyled onto the hairline primitives.
   *
   * Two columns split by a rule, the way DMonitor splits holds from the event
   * stream (y ≈476–924): the objectives on the left as a hairline list, the
   * selected loop on the right as section heads over hairline tables. No
   * reading changed — the poll, its terminal-state rule and the two fetches
   * are the ones this page has always made.
   *
   * A loop status is a StatusMark like every other status in the factory: the
   * six `LoopStatus` words joined `$lib/factory/status` with this issue, so a
   * paused loop and one that ran out of budget no longer share a colour this
   * page chose for them. The verification line reads in the evidence
   * vocabulary — see `loopGate` in $lib/factory/loops, which owns that mapping
   * along with the terminal set that stops the poll.
   */
  import { fmtTime, utcTitle } from '$lib/time';
  import { Gauge, SectionHead, StatusMark } from '$lib/ui';
  import { dataRowClasses } from '$lib/factory/classes';
  import { LOOP_TERMINAL, loopBudget, loopGate } from '$lib/factory/loops';
  import TickStrip from '$lib/factory/TickStrip.svelte';
  import { loopStripRows, stripKeysToFetch } from '$lib/factory/strip-reads';
  import { iterationCounts, iterationSlots, iterationWord } from '$lib/factory/tick-strip';
  import {
    fetchLoop,
    fetchLoopAudit,
    fetchLoopIterations,
    fetchLoops,
    type Iteration,
    type Loop,
    type LoopDetail,
    type LoopAuditEvent
  } from '$lib/api';
  import { PULSE_INTERVAL_MS } from '$lib/shell/pulse.svelte';

  let { data }: { data: { loops: Loop[] | null } } = $props();

  /**
   * The list as the poll last read it (C11). `load()` ran once per navigation,
   * so a loop that finished, paused or was created while this page sat open
   * never appeared — the right-hand pane refreshed every 4s beside a left-hand
   * list frozen at the moment of arrival. `null` is "not read here yet", and
   * then `data.loops` is the reading; this also recovers a page opened while
   * `kew serve` was down (C17), which used to stay offline until a reload.
   */
  let polledLoops = $state<Loop[] | null>(null);

  const liveMode = $derived(polledLoops != null || data.loops != null);
  const loops = $derived<Loop[]>(polledLoops ?? data.loops ?? []);

  let selectedId = $state<string | null>(null);
  let detail = $state<LoopDetail | null>(null);
  let audit = $state<LoopAuditEvent[]>([]);
  let loading = $state(false);

  // The selection follows the list: the first loop of a fresh load(), until the
  // reader picks another. Read in an effect rather than at initialiser time, so
  // a later load() is not left pointing at a loop that is no longer listed.
  $effect(() => {
    if (selectedId === null && loops.length > 0) selectedId = loops[0].loop_id;
  });

  /**
   * The sidebar strip's slot geometry: 12 slots at 7px, which is the 84px cell
   * `LIST_COLUMNS` reserves below. The two have to multiply out.
   */
  const ITERATION_SLOTS = 12;
  const STRIP_SLOT_PX = 7;
  /** What the strip asks for per loop. One strip's worth, never the history. */
  const ITERATION_STRIP_LIMIT = ITERATION_SLOTS;
  /** The detail pane's strip is wider — it has the room the sidebar has not. */
  const DETAIL_ITERATION_SLOTS = 48;

  /**
   * The per-row iteration strip (#2304) — what each loop has been doing, not
   * only what it is doing now.
   *
   * The row's one StatusMark is the loop's CURRENT status, so a loop that
   * failed four iterations and is now pursuing reads exactly like one that has
   * never stumbled. The strip is the history beside it, in fixed slots so the
   * strips down the sidebar share an edge.
   *
   * Bounded by `$lib/factory/strip-reads`: the first N rows, and only the rows
   * whose step/status/updated stamp has moved since their strip was read — the
   * roster poll must not become one request per loop per tick.
   */
  let iterStrips = $state<Record<string, Iteration[]>>({});
  const iterRead = new Map<string, string>();

  $effect(() => {
    if (!liveMode) return;
    const stamped = loopStripRows(loops);
    for (const id of stripKeysToFetch(stamped, iterRead)) {
      const stamp = stamped.find((row) => row.key === id)?.stamp;
      if (stamp === undefined) continue;
      iterRead.set(id, stamp);
      fetchLoopIterations(id, ITERATION_STRIP_LIMIT)
        .then((rows) => {
          iterStrips = { ...iterStrips, [id]: rows };
        })
        .catch(() => {
          // A strip is an extra reading, never the row — release the stamp so
          // the next poll retries and leave the last good strip up.
          iterRead.delete(id);
        });
    }
  });

  const fmtUSD = (n: number) => '$' + n.toFixed(2);
  const fmtTok = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : '' + n);

  /** The verification gate, in the evidence vocabulary (FUI-04). */
  const gate = $derived(loopGate(detail?.status));
  /** The spend figure; the Gauge under it draws the cap axis, not this reading. */
  const budget = $derived(loopBudget(detail?.spent_usd ?? 0, detail?.budget_usd));

  /**
   * The newest read. Bumped per call, and only the holder of the current number
   * writes — the `selectedId === id` check it replaces could not tell two reads
   * of the SAME loop apart, so a slow reply landing behind a fast one put the
   * older iteration table back on screen (C8).
   */
  let detailGen = 0;
  /**
   * How many detail reads are out. The POLL declines a tick while any is, so a
   * server slower than 4s stops stacking reads on itself (C8). A reader's click
   * is never declined — it goes through and takes the newest generation, which
   * is what drops whatever was already in flight.
   */
  let outstanding = 0;

  async function loadDetail(id: string): Promise<void> {
    if (!liveMode) return;
    const gen = ++detailGen;
    outstanding += 1;
    loading = true;
    try {
      const [d, a] = await Promise.all([fetchLoop(id), fetchLoopAudit(id)]);
      if (gen !== detailGen) return;
      detail = d;
      audit = a;
    } catch {
      if (gen !== detailGen) return;
      detail = null;
      audit = [];
    } finally {
      outstanding -= 1;
      if (gen === detailGen) loading = false;
    }
  }

  function selectLoop(id: string): void {
    if (id === selectedId) return;
    selectedId = id;
    detail = null;
    audit = [];
    // No read here: the selection effect below owns the one load per selection
    // (C11). Doing it in both places fetched every selection TWICE — four
    // requests for one click, since each load takes the detail and the audit.
  }

  // We POLL rather than use SSE: the server's SSE channel
  // (kew.serve.sse.event_stream) tails a single run's `.events.jsonl` file, and
  // loops have no such per-loop event file / shared bus — so there is nothing to
  // subscribe to. A loop event stream is deferred to a future plan.
  //
  // Initial load + poll while the selected loop is non-terminal. Re-runs when
  // selectedId OR the observed detail.status changes; the effect reads
  // detail?.status so that when the loop reaches a TRUE terminal state the
  // effect re-runs and returns early WITHOUT arming a new interval — i.e. the
  // poll actually stops instead of ticking forever as a no-op. A resumable
  // status (needs_human, paused, budget_limited — `_RESUMABLE` in
  // kew/loops/delegate.py) is not in LOOP_TERMINAL, so the poll keeps running
  // and catches the operator's resume — and every status that IS in it has a
  // settled gate reading, because both live in $lib/factory/loops.
  $effect(() => {
    const id = selectedId;
    if (!id || !liveMode) return;
    const status = detail?.status;
    // Stop polling once we have observed a true-terminal status for this loop.
    if (status && LOOP_TERMINAL.has(status)) return;
    const timer = setInterval(() => {
      // A hidden tab reads nothing (C8): nobody is looking at these numbers,
      // and the next visible tick is at most 4s after the reader comes back.
      if (document.hidden || outstanding > 0) return;
      if (selectedId === id) void loadDetail(id);
    }, 4000);
    return () => clearInterval(timer);
  });

  // The ONE load per selection (the poll effect above only arms the interval;
  // this is the first paint, without waiting 4s). `selectLoop` deliberately
  // does not read as well — that was C11's double fetch.
  $effect(() => {
    const id = selectedId;
    if (id && liveMode) void loadDetail(id);
  });

  /**
   * The list read's own two guards, the detail poll's (C8) applied to the
   * column: `listOut` declines a tick while a read is still out, so a server
   * slower than the heartbeat stops stacking list reads on itself, and
   * `listGen` means only the newest reply writes — without it two replies that
   * crossed could land in either order and put an older list back on screen.
   */
  let listOut = false;
  let listGen = 0;

  /**
   * The list's own poll, on the factory's heartbeat (C11). Separate from the
   * detail poll because it never stops: a terminal selection has nothing left
   * to re-read, but the list behind it still gains and loses loops. Guarded the
   * way every poll here is — skipped while the tab is hidden, never stacked on
   * a read that is still out, only the newest reply writes, and a reply that
   * fails leaves the last good list rather than blanking the column.
   */
  $effect(() => {
    const timer = setInterval(() => {
      if (document.hidden || listOut) return;
      listOut = true;
      const gen = ++listGen;
      fetchLoops()
        .then((next) => {
          if (gen === listGen) polledLoops = next;
        })
        .catch(() => {
          // Keep the last good list; the next tick tries again.
        })
        .finally(() => {
          listOut = false;
        });
    }, PULSE_INTERVAL_MS);
    return () => clearInterval(timer);
  });

  /**
   * The sidebar row. #2304 adds a fourth cell for the iteration strip, whose
   * width is the slot arithmetic above: a strip padded wider than its cell
   * overflows the row it was padded to align with. The 360px aside can spend
   * it because the goal text beside it already truncates.
   */
  const LIST_COLUMNS = `48px minmax(0, 1fr) ${ITERATION_SLOTS * STRIP_SLOT_PX}px 20px`;
  /** # · kind · cost · tokens · started; below 1024, where the loop's pane is
   * under 400px, the start time gives way. */
  const ITERATION_GRID =
    'grid-cols-[32px_minmax(0,1fr)_72px_72px] gap-3 lg:grid-cols-[32px_minmax(0,1fr)_88px_88px_96px] lg:gap-5';
  const AUDIT_COLUMNS = '200px minmax(0, 1fr) 120px 96px';
</script>

<!-- Below 768 the objectives sit above the selected loop and the page scrolls
     as one, as Prompts does: two panes side by side left the loop 30px (#3461). -->
<div class="flex min-h-0 flex-1 flex-col overflow-y-auto md:overflow-y-hidden">
  <header
    class="flex shrink-0 flex-wrap items-baseline gap-4 border-b border-hair px-5 pt-5 pb-[18px] md:px-10"
  >
    <h1 class="text-26 text-text-1">Loops</h1>
    <span class="text-11 text-text-3">objectives and their flight recorder</span>
    <div class="grow"></div>
    <span class="font-mono text-13 text-text-3">
      {liveMode ? 'kew serve · connected' : 'offline · no data'}
    </span>
  </header>

  <div class="flex flex-col md:grid md:min-h-0 md:flex-1 md:grid-cols-[360px_minmax(0,1fr)]">
    <!-- The objectives. -->
    <aside
      aria-label="Loops"
      class="min-w-0 border-b border-hair px-5 pt-[22px] pb-4 md:overflow-y-auto md:border-r md:border-b-0 md:px-7 md:pb-0"
    >
      <SectionHead label="Objectives" meta={liveMode ? `${loops.length}` : 'not read'} />

      {#each loops as loop, i (loop.loop_id)}
        <button
          type="button"
          aria-current={loop.loop_id === selectedId ? 'true' : undefined}
          onclick={() => selectLoop(loop.loop_id)}
          class={dataRowClasses({
            rule: i === loops.length - 1 ? 'none' : 'quiet',
            class: 'w-full gap-3 text-left transition-colors hover:bg-ink-selected'
          })}
          class:bg-ink-selected={loop.loop_id === selectedId}
          style:grid-template-columns={LIST_COLUMNS}
        >
          <span class="font-mono text-13 text-text-3"
            >{loop.issue_num ? `#${loop.issue_num}` : '—'}</span
          >
          <span
            class="truncate text-15"
            class:text-text-1={loop.loop_id === selectedId}
            class:text-text-2={loop.loop_id !== selectedId}>{loop.goal_text}</span
          >
          <!-- The history, then the current status: the mark says where the
               loop IS and the strip says how it got there. -->
          <span class="flex justify-end">
            {#if iterStrips[loop.loop_id]}
              <TickStrip
                slots={iterationSlots(iterStrips[loop.loop_id], ITERATION_SLOTS)}
                slotPx={STRIP_SLOT_PX}
                label={`${loop.goal_text} iterations`}
                popover={iterationCounts}
                word={iterationWord}
              />
            {/if}
          </span>
          <span class="flex justify-end">
            <StatusMark kind={loop.needs_human ? 'needs_human' : loop.status} size={7} />
          </span>
        </button>
      {:else}
        <p class="py-10 text-center text-13 text-text-3">
          {liveMode ? 'no loops yet.' : 'start kew serve to see loops.'}
        </p>
      {/each}
    </aside>

    <!-- The selected loop. -->
    <div class="min-w-0 px-5 pt-[22px] pb-9 md:overflow-y-auto md:px-10">
      {#if detail}
        <div class="flex flex-col gap-2.5">
          <span class="text-11 text-text-3">Objective{loading ? ' · refreshing' : ''}</span>
          <h2 class="truncate text-26 text-text-1">{detail.goal_text}</h2>
          <div class="flex flex-wrap items-center gap-x-5 gap-y-2 font-mono text-13 text-text-3">
            <StatusMark kind={detail.status} size={7} word class="gap-2" />
            <span>{detail.drive_mode}</span>
            <span>{detail.runner_name}</span>
            {#if detail.model}<span>{detail.model}</span>{/if}
            <span>steps <span class="text-text-2">{detail.step_count}</span> / {detail.max_steps}</span>
            <StatusMark kind={gate.kind} size={7} word label={gate.word} class="gap-2" />
          </div>
        </div>

        {#if detail.needs_human}
          <!-- The one magenta band: a loop that is waiting on a person. -->
          <div class="mt-6 border-l-2 border-accent bg-ink-raised py-3 pr-4 pl-4">
            <div class="flex items-center gap-2.5 font-mono text-13">
              <StatusMark kind="needs_human" size={10} word label="needs a human" />
              <span class="text-text-2">awaiting review</span>
            </div>
            {#if detail.status_reason}
              <p class="mt-1.5 text-15 text-text-2">{detail.status_reason}</p>
            {/if}
          </div>
        {:else if detail.status_reason}
          <p class="mt-6 text-15 text-text-2">{detail.status_reason}</p>
        {/if}

        <!-- The Gauge captions itself only over cap, and its axis is derived from
             the cap alone, so the spend figure has to be printed here or it is
             printed nowhere (#2157 review). Same pairing as the DMonitor spend
             readouts: the figure over its cap, the gauge under both. -->
        <div class="mt-8 flex flex-col gap-3">
          <div class="flex items-baseline gap-2.5">
            <span class="text-11 text-text-3">Budget</span>
            <span
              class="font-mono text-13"
              class:text-danger={budget.over}
              class:text-text-2={!budget.over}
              >{budget.figure}<span class="text-text-3"> / {budget.capLabel}</span></span
            >
          </div>
          <Gauge spent={detail.spent_usd} cap={detail.budget_usd} />
        </div>

        <!-- The iterations.
             NOTE: the one timestamp per row is it.started_at.
             Plan 2 stamps real per-step started_at/finished_at, but if an
             iteration still has started_at == finished_at we just show the one
             time and do NOT compute/assert a duration. -->
        <div class="mt-9">
          <SectionHead label="Iterations" meta={`${detail.iterations.length}`} />
          <!-- The same strip the roster row carries, at the width this pane
               has (#2304): the table below it is one row per iteration, and a
               loop with two hundred of them is a scroll rather than a shape.
               Read off the detail's own iterations, so it needs no extra
               request on a pane that already made two. -->
          <div class="pb-3">
            <TickStrip
              slots={iterationSlots(detail.iterations, DETAIL_ITERATION_SLOTS)}
              slotPx={STRIP_SLOT_PX}
              label="Iterations"
              popover={iterationCounts}
              word={iterationWord}
            />
          </div>
          <div class={dataRowClasses({ head: true, class: ITERATION_GRID })}>
            <span>#</span><span>Kind</span><span class="text-right">Cost</span>
            <span class="text-right">Tokens</span><span class="hidden text-right lg:block">Started</span>
          </div>
          {#each detail.iterations as it, i (it.iteration_id)}
            <div
              class={dataRowClasses({
                height: 50,
                rule: i === detail.iterations.length - 1 ? 'none' : 'quiet',
                class: ITERATION_GRID
              })}
            >
              <span class="font-mono text-13 text-text-3">{it.idx}</span>
              <span class="truncate text-15 text-text-2">{it.kind || '—'}</span>
              <span class="text-right font-mono text-13 text-text-2">{fmtUSD(it.cost_usd)}</span>
              <span class="text-right font-mono text-13 text-text-2">{fmtTok(it.tokens)}</span>
              <span
                class="hidden text-right font-mono text-13 text-text-3 lg:block"
                title={utcTitle(it.started_at)}>{fmtTime(it.started_at)}</span
              >
            </div>
          {:else}
            <p class="py-8 text-center text-13 text-text-3">no iterations recorded yet.</p>
          {/each}
        </div>

        <!-- The governance envelope, read-only. -->
        <div class="mt-9">
          <SectionHead label="Audit" meta={`${audit.length}`} />
          <div class={dataRowClasses({ head: true, class: 'hidden lg:grid' })} style:grid-template-columns={AUDIT_COLUMNS}>
            <span>Event</span><span>Summary</span><span>Actor</span>
            <span class="text-right">Recorded</span>
          </div>
          {#each audit as ev, i (ev.id)}
            <!-- Below 1024: the event and when, then its summary on a line of its own. -->
            <div
              class={dataRowClasses({
                height: 50,
                rule: i === audit.length - 1 ? 'none' : 'quiet',
                class: 'flex h-auto flex-wrap items-baseline gap-x-3 gap-y-1 py-2.5 lg:grid lg:h-[50px] lg:items-center lg:gap-5 lg:py-0'
              })}
              style:grid-template-columns={AUDIT_COLUMNS}
            >
              <span class="min-w-0 flex-1 truncate font-mono text-13 text-text-1">{ev.event}</span>
              <span class="order-last basis-full truncate text-15 text-text-2 lg:order-none lg:basis-auto"
                >{ev.summary}</span
              >
              <span class="hidden truncate font-mono text-13 text-text-3 lg:block">{ev.actor_type ?? ''}</span>
              <span class="text-right font-mono text-13 text-text-3" title={utcTitle(ev.timestamp)}
                >{fmtTime(ev.timestamp)}</span
              >
            </div>
          {:else}
            <p class="py-8 text-center text-13 text-text-3">no audit events yet.</p>
          {/each}
        </div>
      {:else}
        <p class="py-16 text-center text-13 text-text-3">
          {loading ? 'loading loop…' : liveMode ? 'select a loop to inspect.' : 'start kew serve to see loops.'}
        </p>
      {/if}
    </div>
  </div>
</div>
