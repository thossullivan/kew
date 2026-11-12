<script lang="ts">
  /**
   * #2300 (C3) — the run page's split: the phase timeline over the event list.
   *
   * Every reading here comes from `$lib/runs/events` and every pixel of the
   * ruler from `$lib/time/axis` — the one time axis the whole console steps by.
   * What lives in this file is the grammar: hairline rules where a card used to
   * draw a box, the five factory sizes, cyan only for what is live and selected,
   * amber only for money and what is held, red only for what failed, and state
   * shown in place with no toast and no modal. Each row reads as a sentence
   * (`./event-words`); its seq and raw payload sit behind its disclosure.
   *
   * Three behaviours this component owns, each of which was invisible to a
   * payload test and is driven in `run-events.client.test.ts`:
   *
   *   * the filter is URL state, read through `page.state` FIRST — `replaceState`
   *     is shallow routing, so it moves the address bar and `page.state` and
   *     leaves `page.url` at the last real navigation (the trap #2248, #2250,
   *     #2271 and #2285 each hit on their own page);
   *   * hovering a row marks its time on the ruler, debounced 100ms, so a
   *     pointer crossing six rows draws one marker rather than six;
   *   * the live tail follows only while the reader is at the bottom, and it
   *     stops when the RUN does rather than when the prop does — the `run`
   *     prop is loader data nothing on this page invalidates, so the page
   *     re-reads the row for itself (`readRunStatus`).
   */
  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';
  import { onDestroy } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import type { Run, RunAuditEvent } from '$lib/api';
  import { RUN_AUDIT_PAGE, fetchRun, fetchRunAudit } from '$lib/api';
  import { LIVE_STATUSES } from '$lib/run-status';
  import { SectionHead } from '$lib/ui';
  import { fmtTime, fmtDateTime, utcTitle } from '$lib/time';
  import { eventLabel } from '$lib/factory/names';
  import { eventTone, runEventLine } from './event-words';
  import { chipClasses } from '$lib/factory/classes';
  import { formatDuration } from '$lib/time/duration';
  import { axisTicks, axisX, type AxisWindow } from '$lib/time/axis';
  import {
    FOCUSED_TIME_PARAM,
    HOVER_DEBOUNCE_MS,
    RUN_FILTER_PARAM,
    STATUS_POLL_MS,
    TAIL_BATCH_MS,
    TAIL_POLL_MS,
    bandPresses,
    canClearRunFilter,
    clearRunFilter,
    debounce,
    filterRunEvents,
    focusedRunEvent,
    followsTail,
    kindChips,
    mayHoldMoreRunEvents,
    mergeRunEvents,
    parseRunFilter,
    phaseDurationMs,
    phaseSpan,
    placeLaneLabels,
    runEventsCount,
    runEventsEmptyState,
    runFilterQuery,
    runPhases,
    runPresets,
    runStages,
    stageSelected,
    stillLive,
    tailCursor,
    toggleKind,
    toggleStage,
    type RunFilter,
    type RunStage
  } from './events';

  type Props = {
    runId: string;
    run: Run | null;
    /** The first page the loader read, or null when that read failed. */
    first: { events: RunAuditEvent[]; next_seq: number } | null;
    /**
     * Every reading this list takes — the events it holds and the run status
     * it last read for itself — handed back to the page, so the safest action
     * is drawn off the same ledger as the rows above it (#3057).
     */
    onread?: (read: { events: RunAuditEvent[]; status: string | null }) => void;
  };

  let { runId, run, first, onread }: Props = $props();

  /** The ruler's height, and the lane the phase bars are drawn in: a bar,
   * then one or more lines of stage names under it (`placeLaneLabels`). */
  const RULER_H = 22;
  const BAR_Y = 4;
  const BAR_H = 10;
  const LABEL_LINE = 15;
  /** Where the names' lines start: the bar's press band is everything above. */
  const LABEL_TOP = BAR_Y + BAR_H + 2;
  /** A name's width in the lane's 11px mono face: 0.6em a character, and a
   * little over for the glyphs' own bearings, so a name never touches the next. */
  const LABEL_CHAR_W = 7;
  /** The smallest press a control offers. */
  const MIN_PRESS = 32;

  /** Every event held, oldest first. Grows as the tail is read.
   *
   * Seeded from the loader's page and then owned here: a reload or a refresh
   * re-runs the loader, which is the only moment `first` should replace what
   * the tail has since read (see `seededFrom`). */
  // svelte-ignore state_referenced_locally
  let events = $state<RunAuditEvent[]>(first?.events ?? []);
  // svelte-ignore state_referenced_locally
  let cursor = $state(first?.next_seq ?? 0);
  const loading = $derived(first === null && events.length === 0);

  /** Whether a page already read came back at the server's cap, and so may
   * have unread ledger behind the rows this page holds. */
  // svelte-ignore state_referenced_locally
  let more = $state(mayHoldMoreRunEvents(first));
  /** True while a further page is out, so the control is pressed once. */
  let paging = $state(false);

  /**
   * The run's status as this page last read it for ITSELF, or null before any
   * such read. The `run` prop is loader data and nothing on this page
   * invalidates it (see `STATUS_POLL_MS`), so a run that finished while the
   * page was open would otherwise be tailed for the rest of the session.
   */
  let readStatus = $state<string | null>(null);
  /** The finish this page read for itself, which the prop will not learn. */
  let readFinishedAt = $state<string | null>(null);

  /**
   * A refresh (`invalidateAll`) re-runs the loader WITHOUT remounting this
   * list, so new loader data is the moment to drop what the list read for
   * itself: the fresh row outranks a status read before it, and a reading
   * frozen once the run left a live status would otherwise outvote it for the
   * page's safest action — `kew finalize` offered after the run was finalized
   * (#3057). A refresh whose ledger read failed keeps the rows already held.
   * `generation` drops a status read that was out across the refresh.
   */
  // svelte-ignore state_referenced_locally
  let seededFrom: [Run | null, Props['first']] = [run, first];
  let generation = 0;

  $effect.pre(() => {
    if (run === seededFrom[0] && first === seededFrom[1]) return;
    seededFrom = [run, first];
    generation += 1;
    readStatus = null;
    readFinishedAt = null;
    if (first === null) return;
    events = first.events;
    cursor = first.next_seq;
    more = mayHoldMoreRunEvents(first);
    pending = [];
  });

  $effect(() => {
    onread?.({ events, status: readStatus });
  });

  /** The live run's reader. Only a run still executing is tailed. */
  const live = $derived(stillLive(LIVE_STATUSES, run?.status, readStatus));

  // --- the filter, in the URL ---------------------------------------------

  // Read through `page.state` first (see the header): `page.url` is the
  // reading only until this page's first write.
  const search = $derived(new URLSearchParams(page.state.runSearch ?? page.url.search));
  const filter = $derived<RunFilter>(parseRunFilter(search.get(RUN_FILTER_PARAM)));
  const focusedTime = $derived(search.get(FOCUSED_TIME_PARAM));

  /** The page's URL as this page last wrote it. */
  function here(): URL {
    const url = new URL(page.url);
    url.search = page.state.runSearch ?? page.url.search;
    return url;
  }

  /** The one write: the address bar and `page.state` move together. */
  function write(url: URL): void {
    replaceState(url, { ...page.state, runSearch: url.search });
  }

  function setFilter(next: RunFilter): void {
    const url = here();
    const query = runFilterQuery(next);
    if (query === '') url.searchParams.delete(RUN_FILTER_PARAM);
    else url.searchParams.set(RUN_FILTER_PARAM, query);
    write(url);
  }

  /** A timestamp permalink: this page, at that instant. */
  function focusHref(event: RunAuditEvent): string {
    const url = here();
    url.searchParams.set(FOCUSED_TIME_PARAM, event.ts);
    return `${url.pathname}${url.search}`;
  }

  // --- the readings --------------------------------------------------------

  /**
   * Where the run's clock ends: its recorded finish, or now for a live one.
   *
   * `Date.now()` is read once, at mount — deliberately, because a clock in a
   * `$derived` would re-run the whole projection on every tick for a lane that
   * `phaseSpan` already extends to the newest event that arrived. A timestamp
   * that cannot be parsed falls through to the next reading rather than being
   * drawn at the epoch.
   */
  const endMs = $derived(
    Date.parse(readFinishedAt ?? run?.finished_at ?? '') ||
      (live ? Date.now() : Date.parse(run?.started_at ?? '') || 0)
  );
  const phases = $derived(runPhases(events, endMs));
  const span = $derived(phaseSpan(phases, endMs));
  const stages = $derived(runStages(phases));
  const shown = $derived(filterRunEvents(events, filter, phases));
  const chips = $derived(kindChips(events, filter));
  /** Only the presets that apply: one with nothing to select is not drawn,
   * where it used to sit at 40% and read as broken (#3458). */
  const presets = $derived(runPresets(events, phases).filter((preset) => preset.available));
  const focused = $derived(focusedRunEvent(events, focusedTime));
  const emptyState = $derived(runEventsEmptyState({ loading, events, shown, filter }));

  /** The plot's measured width — every x in the ruler is in pixels of it. */
  let plotW = $state(0);
  const window_ = $derived<AxisWindow>({ startMs: span.startMs, endMs: span.endMs, width: plotW });
  // Major ticks only: ten-second minors under a lane of named stages were
  // noise the names now carry (#3458).
  const ticks = $derived(plotW > 0 ? axisTicks(window_, { minors: false, dayLines: true }) : []);

  const xOf = (ms: number): number => axisX(window_, ms);
  const widthOf = (stage: RunStage): number =>
    Math.max(2, xOf(stage.endMs) - xOf(stage.startMs));
  /** Where a stage's bar starts: an instant at the run's last moment would
   * otherwise draw its 2px past the plot's edge, where nothing can press it. */
  const barX = (stage: RunStage): number =>
    Math.max(0, Math.min(xOf(stage.startMs), plotW - widthOf(stage)));

  /** Every stage's name, placed so none overlaps and none leaves the plot. */
  const labelPlaces = $derived(
    placeLaneLabels(
      stages.map((stage) => ({ x: xOf(stage.startMs), width: stage.label.length * LABEL_CHAR_W })),
      plotW
    )
  );
  const labelRows = $derived(Math.max(1, ...labelPlaces.map((place) => place.row + 1)));
  const laneH = $derived(BAR_Y + BAR_H + 4 + labelRows * LABEL_LINE);

  /** The held and failed instants, marked on the lane in amber and red. */
  const laneMarks = $derived(
    events.flatMap((event) => {
      const tone = eventTone(event);
      const ms = Date.parse(event.ts);
      return tone === 'plain' || Number.isNaN(ms) ? [] : [{ seq: event.seq, ms, tone }];
    })
  );

  /** A stage's press is two: its bar, in the bar's band, and its name, in its
   * line's band. Each band is shared out so no press covers another stage's
   * bar or name (`bandPresses`), and each is widened toward the console's 32px. */
  const barPresses = $derived(
    bandPresses(
      stages.map((stage) => ({ x: barX(stage), width: widthOf(stage) })),
      plotW,
      MIN_PRESS
    )
  );
  const labelPresses = $derived.by(() => {
    const presses: { x: number; width: number }[] = new Array(stages.length);
    for (let row = 0; row < labelRows; row += 1) {
      const onRow = labelPlaces.flatMap((place, i) => (place.row === row ? [i] : []));
      const shared = bandPresses(
        onRow.map((i) => ({ x: labelPlaces[i].x, width: stages[i].label.length * LABEL_CHAR_W })),
        plotW,
        MIN_PRESS
      );
      onRow.forEach((i, k) => (presses[i] = shared[k]));
    }
    return presses;
  });

  function pressStage(stage: RunStage, additive: boolean): void {
    setFilter(toggleStage(filter, stage, additive));
  }

  // --- the row disclosure --------------------------------------------------

  /** The rows whose recorded payload and seq are open, by seq. */
  const opened = new SvelteSet<number>();

  function toggleOpen(seq: number): void {
    if (opened.has(seq)) opened.delete(seq);
    else opened.add(seq);
  }

  // --- the hover marker ----------------------------------------------------

  /** The instant the pointer is resting on, or null. Debounced (see header). */
  let markMs = $state<number | null>(null);
  const mark = debounce<number | null>((ms) => (markMs = ms), HOVER_DEBOUNCE_MS);
  const markX = $derived(markMs === null ? null : xOf(markMs));
  onDestroy(() => mark.cancel());

  // --- the live tail -------------------------------------------------------

  /** The scrolling list, so the tail follows only while it is at the bottom. */
  let listEl = $state<HTMLDivElement | null>(null);
  let atBottom = $state(true);

  function onscroll(): void {
    if (listEl) atBottom = followsTail(listEl);
  }

  // --- the chip row --------------------------------------------------------

  /** Below 640 the chips scroll in one row; its right edge fades while more
   * chips lie past it, and not once the row is scrolled to its end (#2897). */
  let chipRowEl = $state<HTMLDivElement | null>(null);
  let chipsPastEdge = $state(false);

  function measureChipRow(): void {
    if (chipRowEl) chipsPastEdge = chipRowEl.scrollLeft + chipRowEl.clientWidth < chipRowEl.scrollWidth - 1;
  }

  $effect(() => {
    const el = chipRowEl;
    if (!el) return;
    const ro = new ResizeObserver(measureChipRow);
    ro.observe(el);
    return () => ro.disconnect();
  });

  // The chips, presets and clear change the row's width without resizing it.
  $effect(() => {
    void [chips, presets, filter];
    measureChipRow();
  });

  /**
   * Read forward from the cursor, and apply what arrives in batches.
   *
   * One read per `TAIL_POLL_MS` beat, resumed from `next_seq`, with a read
   * still in flight never doubled up — so a live run costs one bounded request
   * a second however long it runs. What comes back is buffered and folded in on
   * the `TAIL_BATCH_MS` beat, so a page of thirty rows is one update.
   *
   * Three poll-hygiene rules (#2285 C7): the timer is armed only while the run
   * is live and dropped the moment it is not — which is a reading of the ROW,
   * taken here, not of a prop nothing refreshes — a hidden tab reads nothing,
   * and coming back to the front takes a reading at once rather than waiting
   * out the interval.
   */
  let reading = false;
  let pending: RunAuditEvent[] = [];

  const applyPending = debounce<null>(() => {
    if (pending.length === 0) return;
    events = mergeRunEvents(events, pending);
    pending = [];
    // The tail is followed only while the reader is at the bottom: a live run
    // that scrolled someone off the line they were reading would be unusable
    // exactly when it matters most.
    if (atBottom && listEl) listEl.scrollTop = listEl.scrollHeight;
  }, TAIL_BATCH_MS);

  async function readTail(): Promise<void> {
    if (reading || (typeof document !== 'undefined' && document.hidden)) return;
    reading = true;
    try {
      const next = await fetchRunAudit(runId, cursor);
      cursor = Math.max(cursor, next.next_seq, tailCursor(next.events));
      more = mayHoldMoreRunEvents(next);
      if (next.events.length > 0) {
        pending = [...pending, ...next.events];
        applyPending(null);
      }
    } catch {
      // A failed read is not an error dialog: the rows already held stay on
      // screen and the next beat tries again (DESIGN.md — no modal errors).
    } finally {
      reading = false;
    }
  }

  /**
   * Read the run's own row, so the tail stops when the run does.
   *
   * `live` derives from the `run` prop, which only a manual refresh of the
   * whole page updates — so without this the timer above outlived the run it
   * was following and a finished run was polled once a second for as long as
   * the tab stayed open. The reading is monotone (`stillLive`): it can only
   * ever end the tail, never start one.
   */
  let readingStatus = false;

  async function readRunStatus(): Promise<void> {
    if (readingStatus || (typeof document !== 'undefined' && document.hidden)) return;
    readingStatus = true;
    const asked = generation;
    try {
      const row = await fetchRun(runId);
      if (asked !== generation) return;
      readFinishedAt = row.finished_at ?? null;
      readStatus = row.status;
    } catch {
      // Same rule as the tail: no dialog, and the next beat asks again. The
      // status stays the one the loader read.
    } finally {
      // A beat landing on a read still out is skipped, not queued — so two
      // replies can never land out of order and a stale "pending" can never
      // overwrite the finish that stopped the tail.
      readingStatus = false;
    }
  }

  /**
   * The next page of a finished run's ledger, on the reader's own press.
   *
   * A live run needs no control — its tail already walks forward from the
   * cursor — but a finished run longer than one page would otherwise stop at
   * the cap with nothing to press and a count that read as the whole run.
   */
  async function readMore(): Promise<void> {
    if (paging) return;
    paging = true;
    try {
      const next = await fetchRunAudit(runId, cursor);
      cursor = Math.max(cursor, next.next_seq, tailCursor(next.events));
      more = mayHoldMoreRunEvents(next);
      if (next.events.length > 0) events = mergeRunEvents(events, next.events);
    } catch {
      // The rows held stay on screen and the control stays pressable.
    } finally {
      paging = false;
    }
  }

  $effect(() => {
    if (!live) return;
    const timer = setInterval(readTail, TAIL_POLL_MS);
    // The row's own beat, a tenth as often: a status changes once per run.
    const statusTimer = setInterval(readRunStatus, STATUS_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void readTail();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      clearInterval(statusTimer);
      document.removeEventListener('visibilitychange', onVisible);
      applyPending.cancel();
    };
  });

  // The shared pill: 32px of press below 640, where these were 22px tags
  // with 3px corners (#2788 N15). It keeps its width and its one line, so the
  // row can scroll on a phone instead of squeezing the pills (#2850 P20).
  const chipClass = (on: boolean) => chipClasses(on, 'shrink-0 whitespace-nowrap transition-colors');
</script>

<section aria-label="Run events" class="mt-9">
  <!-- The count rides in the snippet, not in `meta`: a `SectionHead` given
       children renders those INSTEAD of `meta`, so the count passed as `meta`
       next to a live mark was never drawn at all. Sized like the live mark
       beside it and NOT with `text-11`: that utility carries uppercase and
       tracking of its own, which the head's `meta` wrapper neutralises for
       exactly this reason — only the label shouts. -->
  <SectionHead label="Run events">
    <span class="mr-4 font-mono text-[11px] text-text-3"
      >{runEventsCount(shown.length, events.length, more)}</span
    >
    {#if live}
      <!-- Cyan is the only "live" colour (DESIGN.md). -->
      <span class="font-mono text-[11px] text-accent-2">streaming</span>
    {/if}
  </SectionHead>

  <!-- The phase timeline: the shared axis over one lane of phase bars. A
       click filters the list to that phase; shift-click adds another. -->
  <div class="border-b border-hair pb-3" bind:clientWidth={plotW}>
    <svg width={plotW} height={RULER_H} aria-hidden="true">
      {#each ticks as t (t.ms)}
        <line
          x1={t.x}
          x2={t.x}
          y1={t.major ? RULER_H - 7 : RULER_H - 4}
          y2={RULER_H}
          stroke={t.day ? 'var(--hair-strong)' : 'var(--hair)'}
        />
        {#if t.label !== ''}
          <text x={t.x + 3} y={11} fill="var(--text-3)" font-size="11">{t.label}</text>
        {/if}
      {/each}
    </svg>
    <svg width={plotW} height={laneH} role="list" aria-label="Run phases">
      {#each stages as stage, i (stage.seq)}
        {@const on = stageSelected(filter, stage)}
        {@const place = labelPlaces[i]}
        {@const x = barX(stage)}
        {@const w = widthOf(stage)}
        {@const barHit = barPresses[i]}
        {@const labelHit = labelPresses[i]}
        <g role="listitem">
          <!-- One stage is one control: its bar and its name, so a one-second
               stage is still a name to press. Neither press reaches over
               another stage's bar or name (`bandPresses`).
               Space is taken here, or it would also scroll the page.
               The global focus ring does not draw on an SVG group, so a
               keyboard focus strokes the bar cyan instead (`.stage` below). -->
          <g
            role="button"
            tabindex="0"
            aria-pressed={on}
            aria-label={`filter to ${stage.label}`}
            data-phases={stage.phases.join(' ')}
            data-stage={stage.label}
            class="stage cursor-pointer outline-none"
            onclick={(e) => pressStage(stage, e.shiftKey)}
            onkeydown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                pressStage(stage, e.shiftKey);
              }
            }}
          >
            <title>{`${stage.label} · ${formatDuration(phaseDurationMs(stage))} · ${stage.count} event${stage.count === 1 ? '' : 's'}`}</title>
            <rect x={barHit.x} y="0" width={barHit.width} height={LABEL_TOP} fill="transparent" data-stage-press />
            <rect
              x={labelHit.x}
              y={LABEL_TOP + place.row * LABEL_LINE}
              width={labelHit.width}
              height={place.row === labelRows - 1 ? laneH - LABEL_TOP - place.row * LABEL_LINE : LABEL_LINE}
              fill="transparent"
              data-stage-press
            />
            <rect
              x={x}
              y={BAR_Y}
              width={w}
              height={BAR_H}
              rx="1"
              fill={on ? 'var(--text-2)' : 'var(--text-4)'}
              stroke={on ? 'var(--text-1)' : 'none'}
              stroke-width={on ? 1 : 0}
              class="stage-bar"
              data-stage-bar
            />
            <text
              x={place.x}
              y={BAR_Y + BAR_H + 4 + place.row * LABEL_LINE + 11}
              fill={on ? 'var(--text-1)' : 'var(--text-3)'}
              font-size="11"
              class="font-mono"
              data-stage-label>{stage.label}</text
            >
          </g>
        </g>
      {/each}
      <!-- A deferral or hold in amber, a failure in red, at its instant on
           the lane (DESIGN.md: amber means held, red means failed). -->
      {#each laneMarks as m (m.seq)}
        <rect
          x={xOf(m.ms) - 1}
          y={BAR_Y - 3}
          width="2"
          height={BAR_H + 6}
          fill={m.tone === 'held' ? 'var(--warn)' : 'var(--danger)'}
          data-lane-mark={m.tone}
          pointer-events="none"
        />
      {/each}
      <!-- The hovered row's time, marked on the ruler. Decoration only: it
           never takes a click meant for a phase under it. -->
      {#if markX !== null}
        <line
          x1={markX}
          x2={markX}
          y1="0"
          y2={laneH}
          stroke="var(--accent-2)"
          stroke-width="1"
          data-testid="hover-mark"
          pointer-events="none"
        />
      {/if}
      {#if focused}
        {@const fx = xOf(Date.parse(focused.ts))}
        <line x1={fx} x2={fx} y1="0" y2={laneH} stroke="var(--text-2)" stroke-dasharray="2 2" pointer-events="none" />
      {/if}
    </svg>
  </div>

  <!-- Presets built from this run's own events, then the kind chips with
       their counts, then the one clear. Below 640 they scroll in one row:
       wrapped, eleven kinds took about 400px before the first event at 390
       (#2850 P20). The right edge fades while chips lie past it (#2897),
       but not while a chip holds keyboard focus: the mask would dim its
       label and its focus ring. -->
  <div
    bind:this={chipRowEl}
    data-more-right={chipsPastEdge}
    class="flex flex-nowrap items-center gap-x-2 gap-y-2 overflow-x-auto border-b border-hair-quiet py-3
           sm:flex-wrap sm:overflow-x-visible {chipsPastEdge
      ? '[mask-image:linear-gradient(to_right,black_calc(100%-2rem),transparent)] sm:[mask-image:none] has-[:focus-visible]:[mask-image:none]'
      : ''}"
    onscroll={measureChipRow}
  >
    {#each presets as preset (preset.id)}
      <button
        type="button"
        class={chipClass(false)}
        data-preset={preset.id}
        title={`shows ${preset.count} event${preset.count === 1 ? '' : 's'}`}
        onclick={() => setFilter(preset.filter)}>{preset.label} {preset.count}</button
      >
    {/each}
    {#if presets.length > 0}<span class="w-3 shrink-0" aria-hidden="true"></span>{/if}
    {#each chips as chip (chip.kind)}
      <button
        type="button"
        class={chipClass(chip.selected)}
        aria-pressed={chip.selected}
        title={chip.kind}
        onclick={() => setFilter(toggleKind(filter, chip.kind))}>{eventLabel(chip.kind)} {chip.count}</button
      >
    {/each}
    {#if canClearRunFilter(filter)}
      <!-- Clearing drops what was typed and keeps what was clicked. -->
      <button type="button" class={chipClass(false)} onclick={() => setFilter(clearRunFilter(filter))}
        >clear filter</button
      >
    {/if}
  </div>

  <!-- Below 640 the list grows with the page: an inner scroller inside the
       page's own captured phone swipes (#3458). -->
  <div
    class="min-h-[120px] sm:max-h-[420px] sm:overflow-y-auto"
    role="list"
    aria-label="Ledger events"
    bind:this={listEl}
    {onscroll}
    onpointerleave={() => mark(null)}
  >
    {#each shown as event (event.seq)}
      {@const ms = Date.parse(event.ts)}
      {@const line = runEventLine(event)}
      {@const open = opened.has(event.seq)}
      {@const isFocused = focused?.seq === event.seq}
      <!-- time · what happened · who · what it says. The seq and the raw
           payload are evidence, one press away behind the row's disclosure. -->
      <div
        class="border-b border-hair-quiet {isFocused ? 'bg-ink-selected shadow-[inset_2px_0_0_var(--accent-2)]' : ''}"
        role="listitem"
        data-seq={event.seq}
        data-tone={line.tone}
        data-focused={isFocused}
        onpointerenter={() => mark(Number.isNaN(ms) ? null : ms)}
      >
        <div class="flex flex-wrap items-center gap-x-3 py-0.5 pr-2 sm:flex-nowrap">
          <button
            type="button"
            class="inline-flex h-8 w-8 shrink-0 items-center justify-center font-mono text-13 text-text-4 hover:text-text-1"
            aria-expanded={open}
            aria-label={open ? 'hide what was recorded' : 'show what was recorded'}
            onclick={() => toggleOpen(event.seq)}>{open ? '▾' : '▸'}</button
          >
          <a
            href={focusHref(event)}
            class="shrink-0 font-mono text-13 text-text-2 underline-offset-4 hover:text-text-1 hover:underline"
            title={utcTitle(event.ts)}>{fmtTime(ms, true)}</a
          >
          <span
            class="shrink-0 font-sans text-13 {line.tone === 'failed'
              ? 'text-danger'
              : line.tone === 'held'
                ? 'text-warn'
                : 'text-text-1'}"
            data-event-label
            title={event.event}>{line.label}</span
          >
          {#if event.actor_type}<span class="shrink-0 font-mono text-[11px] text-text-4">{event.actor_type}</span>{/if}
          {#if line.summary !== ''}
            <span
              class="min-w-0 basis-full truncate pb-1 pl-11 text-13 text-text-3 sm:flex-1 sm:basis-auto sm:pb-0 sm:pl-0"
              data-event-summary
              title={line.summary}>{line.summary}</span
            >
          {/if}
        </div>
        {#if open}
          <div class="pb-2 pl-11 pr-2 font-mono text-[11px] text-text-3" data-event-detail>
            <p>ledger seq {event.seq}</p>
            <pre class="mt-1 whitespace-pre-wrap break-all">{event.payload ? JSON.stringify(event.payload, null, 2) : 'no payload'}</pre>
          </div>
        {/if}
      </div>
    {/each}
    {#if emptyState !== null}
      <p class="py-6 text-13 text-text-3">{emptyState}</p>
    {/if}
  </div>

  <!-- A run longer than one page says so, and the next page is one press
       away. Offered only for a run that has stopped: a live one's tail is
       already walking the cursor forward. -->
  {#if more && !live}
    <button type="button" class="mt-2 {chipClass(false)}" disabled={paging} onclick={() => void readMore()}
      >{paging ? 'reading…' : `read the next ${RUN_AUDIT_PAGE}`}</button
    >
  {/if}

  {#if focused}
    <p class="mt-2 font-mono text-[11px] text-text-4">
      focused on {fmtDateTime(focused.ts)}
    </p>
  {/if}
</section>

<style>
  /* A stage focused from the keyboard: cyan, the console's focus colour, on
     the bar a reader is pressing — never on a pointer press. CSS outranks
     the bar's own `stroke` attribute, so a selected bar shows it too. */
  .stage:focus-visible .stage-bar {
    stroke: var(--accent-2);
    stroke-width: 2px;
  }
</style>
