<script lang="ts">
  /**
   * FUI-25 / #2248 — the Monitor's event stream: a range bar, an overview
   * histogram with a brush, the detail strip for the brushed window, the filter
   * row, and the list under all of it.
   *
   * The component is a renderer, a clock and a fetcher. Every position, word and
   * colour comes from `$lib/monitor/stream` (the strip and the rows),
   * `$lib/monitor/brush` (the bars and the window maths) and
   * `$lib/monitor/stream-query` (the range, the filters and what the URL says);
   * what lives here is the px geometry, the poll, and the pointer and key
   * handlers the brush needs.
   *
   * A poll that fails prints "stream unavailable" where the list would be.
   * Blanking the list instead would say the factory had been quiet, which on
   * this screen is the difference between a calm fleet and a blind one.
   *
   * Five rules, beyond the ones `stream.ts` already carries.
   *
   * **The URL is the state, and the page owns it.** The range, the brushed
   * window and the three filters arrive as `query` and leave through `onquery`.
   * This component imports nothing from `$app` — the Monitor page reads the
   * URL (`page.state`, then `page.url`) and calls `replaceState` — which is
   * what lets a vitest render the whole stream server-side, out of any request.
   *
   * **Live is the only state that polls.** `isLive(query)` is a reading of the
   * query alone, so the 5s timer exists exactly while the right edge follows the
   * clock. Any other range is paused, says so, and offers "Jump to now".
   *
   * **Live keeps the strip it always had.** In live mode the strip is composed
   * with no window, which is the 30 minutes ending at the next 5-minute mark
   * that FUI-25 drew (skew-following axis included), and the overview spans
   * exactly that — so the brush box and the strip beneath it cannot disagree.
   * Brushing pins a window, which pauses, and from then on both are drawn for
   * the operator's own edges.
   *
   * **The overview and the list are filtered together.** One `streamFilters`
   * reading goes to `/api/audit` and to `/api/audit/histogram`, so the bars can
   * never count events the list below them is hiding.
   *
   * **A burst of windows is one read, and the newest read wins.** The brush can
   * change the query far faster than a person can mean to — a held arrow key
   * repeats about thirty times a second, and each repeat is two requests
   * against the store the fleet is writing audit events to — so a read other
   * than the first waits `STREAM_SETTLE_MS` for the query to stop moving. And
   * the reads are generation-guarded: the answer for a window the operator has
   * left never lands on top of the one they are on, which nothing would correct
   * under a paused range.
   */
  import { browser } from '$app/environment';
  import {
    fetchAudit,
    fetchAuditHistogram,
    fetchEpics,
    type AuditEvent,
    type AuditHistogram,
    type AuditList
  } from '$lib/api';
  import { TAP_INLINE, TAP_PILL } from '$lib/factory/classes';
  import { isUnavailable, unreadable, type Source } from '$lib/monitor/readouts';
  import { runHref } from '$lib/flight-recorder';
  import { workHref } from '$lib/monitor/live-runs';
  import { SectionHead } from '$lib/ui';
  import { fmtTime } from '$lib/time';
  import OverviewTrack from './OverviewTrack.svelte';
  import {
    FAMILY_COLOR,
    FAMILY_LABEL,
    OVERVIEW_FAMILIES,
    PAN_FRACTION,
    ZOOM_FACTOR,
    bucketSecondsFor,
    overviewBars,
    panBy,
    spanLabel,
    zoom,
    type Span
  } from './brush';
  import {
    composeStream,
    duration,
    MARK_COLOR,
    ROW,
    STREAM_LIMIT,
    STREAM_POLL_MS,
    STREAM_SETTLE_MS,
    STREAM_UNREAD,
    STRIP,
    fitStrip,
    oldestRead,
    streamWindow,
    type LabelAlign
  } from './stream';
  import {
    LIVE_QUERY,
    PRESETS,
    STREAM_KINDS,
    applyPreset,
    isLive,
    jumpToNow,
    isoOf,
    listFilters,
    moveBounds,
    resolveStreamScope,
    streamFilters,
    streamSearch,
    toggleKind,
    withWindow,
    type PresetKey,
    type StreamKind,
    type StreamQuery
  } from './stream-query';

  let {
    source,
    nowMs,
    query = LIVE_QUERY,
    onquery = () => {}
  }: {
    /** A payload supplied by the caller. Absent: the component polls. */
    source?: Source<AuditList>;
    /** The clock the strip is drawn against. Absent: the poll's own. */
    nowMs?: number;
    /** The range, the brushed window and the filters — the page's URL, read. */
    query?: StreamQuery;
    /** Where a range click, a brush gesture or a filter goes. */
    onquery?: (next: StreamQuery) => void;
  } = $props();

  let polled = $state<Source<AuditList>>({ available: false, reason: STREAM_UNREAD });
  /** The pages "Load older" has fetched, oldest page last. */
  let older = $state<AuditEvent[]>([]);
  /** The cursor the oldest page in hand gave, or `null` at the tail of the log. */
  let cursor = $state<number | null>(null);
  let paging = $state(false);
  let histogram = $state<AuditHistogram | null>(null);
  /** The epics the epic chips offer, read once — the roster, not a poll. */
  let roster = $state<string[]>([]);
  let clock = $state(Date.now());
  /** The strip's measured width: which of its labels fit is a question of px. */
  let stripWidth = $state(0);

  /**
   * Keeps `stripWidth` current. Where there is no `ResizeObserver` (a test DOM)
   * the width stays unmeasured, and `fitStrip` draws every label as composed.
   */
  function measure(node: HTMLElement): { destroy: () => void } | undefined {
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => (stripWidth = node.clientWidth));
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }

  /**
   * The row whose payload is open, or `null` — one at a time (#2210).
   *
   * Keyed by the row's own key, which is the event's id, so the poll replacing
   * the page every 5s leaves the open row open and does not shift the disclosure
   * onto whatever event lands in that position next.
   */
  let openKey = $state<string | null>(null);

  const live = $derived(isLive(query));

  /**
   * The range the overview spans, the window everything below it is drawn for,
   * and what a key's move is clamped to, at one instant.
   *
   * `bounds` is the range for every preset that is a span of the clock, and the
   * window's own reach under `Custom…`, whose range IS its window — see
   * `moveBounds`. Live is the one case that does not come from
   * `resolveStreamScope`: the strip keeps the 5-minute-snapped 30 minutes FUI-25
   * drew, the overview is given exactly those edges so the two agree, and a move
   * that cannot happen inside them writes nothing (`setWindow`).
   */
  function spanFor(at: number): { range: Span; window: Span; bounds: Span } {
    const snapped = streamWindow(at);
    if (live) return { range: snapped, window: snapped, bounds: snapped };
    const scope = resolveStreamScope(query, at);
    // A window brushed out of the live range keeps the range it was drawn on.
    // `resolveStreamScope` would answer the last 30 minutes to the millisecond,
    // which is up to five minutes off the snapped range the brush was dragged
    // over: the box jumped sideways on mouseup and the track under it re-mapped.
    if (scope.preset.live) {
      const range = {
        startMs: Math.min(snapped.startMs, scope.window.startMs),
        endMs: Math.max(snapped.endMs, scope.window.endMs)
      };
      return { range, window: scope.window, bounds: range };
    }
    return { range: scope.range, window: scope.window, bounds: moveBounds(scope) };
  }

  const span = $derived(spanFor(nowMs ?? clock));
  const bucketMs = $derived(bucketSecondsFor(span.range.endMs - span.range.startMs) * 1000);

  /**
   * One page of the window, with the filters the overview is also given —
   * except in live mode, where the list reads the newest page unbounded so an
   * idle fleet still shows its last events (`listFilters`).
   */
  const readPage = (window: Span, after?: number) =>
    fetchAudit({
      ...listFilters(query, window, live),
      limit: STREAM_LIMIT,
      ...(after != null ? { after } : {})
    });

  /**
   * Which read is the current one.
   *
   * Every query change starts `poll` again, and a read for the window the
   * operator has just left can resolve after the read for the one they are on:
   * the awaits give no order back. Without this the late answer landed last and
   * overwrote the current reading — or printed "stream unavailable" over a good
   * one — and under any non-live range there is no timer to correct it, because
   * "paused" means paused. So each run takes a number, and a run that is no
   * longer the newest writes nothing: not the page, not the cursor, not the
   * bars, not the clock. It is also why the histogram is not even asked for
   * once the list read it was paired with has gone stale.
   */
  let generation = 0;

  /**
   * The histogram request the bars on the screen answer. A move that leaves the
   * range and the filters where they were — every brush gesture under a preset
   * — does not ask for the same bars again.
   */
  let barsKey: string | null = null;

  /** The preset the clock was last read for: `null` before the first read. */
  let clockPreset: string | null = null;

  async function poll(readList = true): Promise<void> {
    const mine = ++generation;
    const current = () => mine === generation;
    // A paused range holds its clock. "24h" is the last 24 hours as of the
    // click that chose it, and re-reading the clock on every brush move slid
    // the whole track under the brush between one gesture and the next. The
    // clock moves while live, and when a preset is chosen.
    const at = nowMs ?? (live || query.preset !== clockPreset ? Date.now() : clock);
    clockPreset = query.preset;
    const { range, window } = spanFor(at);
    if (readList) {
      try {
        const read = await readPage(window);
        if (!current()) return;
        polled = read;
        cursor = read.next_after;
        older = [];
        tail = false;
      } catch (err) {
        if (!current()) return;
        polled = unreadable('stream', err);
      }
    }
    // The bars are a read of their own and a failed one is not a reason to drop
    // the list: the overview goes blank, the stream below it does not.
    const filters = streamFilters(query, range);
    const key = JSON.stringify([range.startMs, range.endMs, filters]);
    if (key !== barsKey) {
      try {
        // `until` is the range's own end, not the list's end-plus-a-millisecond:
        // the endpoint tiles dense buckets from `since`, and a millisecond past
        // the last one would buy a 97th bucket holding a sliver of time. The
        // bars are a shape to navigate by; the list below is the record.
        const read = await fetchAuditHistogram(
          {
            since: isoOf(range.startMs),
            until: isoOf(range.endMs),
            bucket_s: bucketSecondsFor(range.endMs - range.startMs)
          },
          filters
        );
        if (!current()) return;
        histogram = read;
        barsKey = read === null ? null : key;
      } catch {
        if (!current()) return;
        histogram = null;
        barsKey = null;
      }
    }
    clock = at;
  }

  /**
   * True once a "Load older" read came back with nothing older behind it: the
   * tail of the log, under these filters. The button goes then, because a
   * control that fetches nothing is a control that lies.
   */
  let tail = $state(false);

  /**
   * The query "Load older" wrote when it widened the window to the page it
   * fetched. The list in hand already IS that window's reading, so the read
   * that query would start leaves the list alone — re-reading it would put the
   * head page back and drop the pages just loaded — and asks only for the bars,
   * whose range the wider window may have stretched.
   */
  let adopted: string | null = null;

  /**
   * The next page back — and past the window's start when that is where the
   * next page is (Tom, 2026-10-02: "Load older" may widen the window).
   *
   * It pages with `after`: the cursor the page in hand ended on, or, where that
   * page reached the window's own start, the oldest event in hand. The read
   * keeps the window's end and its filters and drops its start, and when the
   * page it brings back reaches before that start the brush's left edge is
   * moved to the oldest event in it. So the button is useful even when one page
   * held the whole window, and the strip, the overview and the list go on
   * describing one span of time.
   */
  async function loadOlder(): Promise<void> {
    if (paging || tail || isUnavailable(reading)) return;
    // A page is a page OF a window. If the window moves while this one is in
    // flight the poll has already replaced the list under it, so the page is
    // dropped rather than appended to a list it is no longer part of.
    const mine = generation;
    const { window } = span;
    const after = cursor ?? oldestId(reading.events);
    const { since: _since, ...unbounded } = streamFilters(query, window);
    paging = true;
    try {
      const read = await fetchAudit({
        ...unbounded,
        // Nothing in hand to page from: the newest page before the window.
        ...(after === null ? { until: isoOf(window.startMs) } : { after }),
        limit: STREAM_LIMIT
      });
      if (mine !== generation) return;
      older = [...older, ...read.events];
      cursor = read.next_after;
      tail = read.next_after === null;
      const reached = oldestRead(read.events);
      if (reached !== null && reached < window.startMs) {
        const next = withWindow(query, { startMs: reached, endMs: window.endMs });
        adopted = streamSearch(next);
        onquery(next);
      }
    } catch (err) {
      if (mine !== generation) return;
      polled = unreadable('stream', err);
    } finally {
      paging = false;
    }
  }

  /** The smallest id in a page — what `after` continues from — or `null`. */
  function oldestId(events: AuditEvent[]): number | null {
    return events.length === 0 ? null : Math.min(...events.map((event) => event.id));
  }

  /**
   * Whether anything has been read yet. A plain `let`, deliberately: it decides
   * whether a read waits, and a reactive one would re-run the effect that sets
   * it.
   */
  let opened = false;

  // Keyed on the URL the query writes: a new range, a new window or a new filter
  // starts the reading again, and nothing else does. The timer exists only while
  // live, which is the whole of "any non-live range does not poll".
  //
  // The first read happens at once; every later one waits `STREAM_SETTLE_MS`
  // for the query to stop moving, and the next change cancels the read waiting
  // in front of it. A held arrow key therefore costs the store one read rather
  // than one per repeat — the brush's drags report once each
  // (`OverviewTrack.svelte`), and this is what bounds the keys the same way.
  $effect(() => {
    if (source !== undefined || !browser) return;
    // The window "Load older" widened to is already in hand: only the bars move.
    const readList = streamSearch(query) !== adopted;
    adopted = null;
    let settle: ReturnType<typeof setTimeout> | undefined;
    if (opened) settle = setTimeout(() => void poll(readList), STREAM_SETTLE_MS);
    else void poll();
    opened = true;
    if (!isLive(query)) return () => clearTimeout(settle);
    // A hidden tab reads nothing (#2285, C8): the stream is a picture of now
    // for somebody looking at it, and the next visible tick redraws it whole.
    const timer = setInterval(() => {
      if (!document.hidden) void poll();
    }, STREAM_POLL_MS);
    return () => {
      clearTimeout(settle);
      clearInterval(timer);
    };
  });

  $effect(() => {
    if (!browser) return;
    void (async () => {
      try {
        const epics = await fetchEpics();
        roster = epics.filter((epic) => epic.archived !== true).map((epic) => epic.epic_ref);
      } catch {
        // No roster: the chips still offer the epics this page's events name.
        roster = [];
      }
    })();
  });

  /** The page in hand: the caller's, or the poll's head page plus what paging added. */
  const reading = $derived.by((): Source<AuditList> => {
    if (source !== undefined) return source;
    if (isUnavailable(polled)) return polled;
    return { events: [...polled.events, ...older], next_after: cursor };
  });

  const view = $derived(composeStream(reading, nowMs ?? clock, live ? undefined : span.window));

  const bars = $derived(histogram === null ? null : overviewBars(histogram, span.range));

  /**
   * The oldest instant the page in hand holds — what "Load older" reaches back
   * past, and so what its label states. `null` while nothing in hand carries a
   * readable stamp, and the label then says only "Load older".
   */
  const olderThan = $derived(isUnavailable(reading) ? null : oldestRead(reading.events));

  /** "Load older": a paused window this component reads itself, short of the log's tail. */
  const olderOffered = $derived(!live && source === undefined && !tail);

  /** "02:45 – 04:30 · 1h 45m" — the window, in words, above the strip. */
  const windowLabel = $derived(spanLabel(span.window));
  const windowSpan = $derived(duration(span.window.endMs - span.window.startMs));

  /**
   * The epic chips: the roster, plus every epic this page's events name,
   * plus whatever is being filtered on. The last two matter when the roster read
   * failed or when a link arrives naming an epic that has since been archived —
   * a filter the UI cannot show among its own chips is a filter nobody can clear.
   */
  const epicOptions = $derived.by(() => {
    const seen = new Set(roster);
    if (!isUnavailable(reading))
      for (const event of reading.events) if (event.epic_ref) seen.add(event.epic_ref);
    if (query.epic) seen.add(query.epic);
    return [...seen].sort((a, b) => a.localeCompare(b));
  });

  function setPreset(key: PresetKey): void {
    onquery(applyPreset(query, key, nowMs ?? clock));
  }

  /**
   * The brushed window, pinned — which is also what pauses the poll, so a move
   * that lands on the window it started from must not pin one.
   *
   * In live mode (and under any preset nobody has brushed yet) the range and the
   * window are the same span, so `pan` and a zoom out provably return it
   * unchanged: pinning there would stop the 5s poll with nothing on the screen to
   * show for the key that did it.
   */
  function setWindow(next: Span): void {
    const { window } = span;
    if (next.startMs === window.startMs && next.endMs === window.endMs) return;
    onquery(withWindow(query, next));
  }

  /**
   * The keys the overview track owns, acted on against the current span.
   *
   * A pan and a zoom are clamped to `bounds` rather than to the range, which is
   * the same thing under every preset that has a span of its own. Under
   * `Custom…` it is what makes a zoom-in reversible: the range there follows the
   * window, so clamping the way back to it left the operator with no way back.
   */
  function onkey(named: string): void {
    const { bounds, window } = span;
    if (named === 'live') onquery(jumpToNow(query));
    // A reset puts the brush back around the whole range; clearing the pins
    // outright would be the range itself under `Custom…`, whose range IS its
    // window — a double-click would have thrown the operator's range away and
    // snapped the view back to Live 30m.
    else if (named === 'reset') setPreset(query.preset);
    else if (named === 'pan-older') setWindow(panBy(bounds, window, -PAN_FRACTION));
    else if (named === 'pan-newer') setWindow(panBy(bounds, window, PAN_FRACTION));
    else if (named === 'zoom-in') setWindow(zoom(bounds, window, ZOOM_FACTOR, bucketMs));
    else if (named === 'zoom-out') setWindow(zoom(bounds, window, 1 / ZOOM_FACTOR, bucketMs));
  }

  function setEpic(value: string): void {
    onquery({ ...query, epic: value === '' ? null : value });
  }

  function setItem(value: string | null): void {
    onquery({ ...query, item: value });
  }

  /** 2dp is finer than a pixel at this width; 89.8944% is just noise. */
  const at = (pct: number) => `${Number(pct.toFixed(2))}%`;

  const SHIFT: Record<LabelAlign, string> = {
    start: 'none',
    center: 'translateX(-50%)',
    end: 'translateX(-100%)'
  };

  const grid = `${ROW.timeWidth}px ${ROW.railWidth}px minmax(0, 1fr)`;

  /**
   * What joins the row's lead to the words after it. A literal in the markup
   * would lose its leading space to Svelte's whitespace trimming at the start
   * of an element, and the two halves would run together. The leading space
   * is a no-break space: the browser still dropped an ordinary one at the
   * start of the clipped box, `preserve-spaces` or not, and the row read
   * "Adversarial review verdict· launch-fixes" (#3468 D33).
   */
  const SEPARATOR = '\u00a0· ';

  /** The "Load older" pill — drawn at the tail of the log, or in the sentence
   * an empty window says, which is where the operator most needs to reach
   * further back (#2282 D15). Never both at once. */
  const OLDER =
    'inline-flex cursor-pointer items-center rounded-full border border-hair-strong ' +
    'px-2.5 py-0.5 text-[11px] text-text-1 hover:border-accent-2 hover:text-accent-2 ' +
    // 32px of press on a phone (D25); the artboard's pill from `sm` up — the
    // same gate every other control on this strip takes.
    TAP_PILL;

  /**
   * The range, kind and item pills. 22px is the artboard's height and 10px
   * short of a press target, so below `sm` the pill takes `min-h-8` and keeps
   * its 11px type: the operator browser check at 390px measured these six at
   * 22px against DESIGN.md's 32px floor (#2282 D25).
   */
  const CHIP =
    'inline-flex h-[22px] cursor-pointer items-center gap-1.5 rounded-full border px-2.5 font-mono text-[11px] ' +
    TAP_PILL;
  const CHIP_OFF = 'border-hair-strong text-text-3 hover:text-text-1';
  const CHIP_ON = 'border-accent-2 bg-accent-2-wash text-accent-2';
</script>

<!--
  A row's words: the label first, in the reading colour, and the names and
  values the log recorded after it (#2282 D15). Neither is painted by category
  any more — the mark on the rail says that, and a column of green and yellow
  sentences says it four times over.
  The label is never cut (#2982): it is what happened and the move, so one too
  long for a phone wraps and the row grows, while the rest truncates after it.
  The rest is its own box, and its separator leads with a no-break space so the
  space before the dot is kept, not collapsed away at the start of the box.
-->
{#snippet words(row: { lead: string; rest: string; code: string })}
  <span class="max-w-full shrink-0 break-words text-text-1" data-lead>{row.lead}</span
  >{#if row.rest}<span
      class="min-w-0 flex-1 overflow-hidden text-ellipsis text-nowrap text-text-3"
      title={row.code || undefined}>{`${SEPARATOR}${row.rest}`}</span
    >{/if}
{/snippet}

<section aria-label="Event stream" class="flex min-w-0 flex-col">
  <SectionHead label="Event stream" meta={view.available ? view.meta : ''} />

  <!-- The feed's own state: following the clock, or held where it was put. -->
  <div class="mt-2 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 font-mono text-[11px]">
    <span role="status" class="flex items-center gap-2">
      {#if live}
        <span class="h-1.5 w-1.5 rounded-full bg-accent-2" aria-hidden="true"></span>
        <span class="text-text-2">Live</span>
      {:else}
        <span class="text-text-2">Paused</span>
        <button
          type="button"
          class={`cursor-pointer text-accent-2 underline-offset-4 hover:underline ${TAP_INLINE}`}
          onclick={() => onquery(jumpToNow(query))}>Jump to now</button
        >
      {/if}
    </span>
    <span class="flex flex-wrap items-center gap-3 text-text-3" aria-label="Overview colours">
      {#each OVERVIEW_FAMILIES as family (family)}
        <span class="flex items-center gap-1.5">
          <i
            class="block h-1.5 w-2.5"
            aria-hidden="true"
            style:background-color={FAMILY_COLOR[family]}
          ></i>{FAMILY_LABEL[family]}
        </span>
      {/each}
    </span>
  </div>

  <div class="mt-2.5 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
    <div class="flex flex-wrap gap-1" role="group" aria-label="Range">
      {#each PRESETS as preset (preset.key)}
        <button
          type="button"
          class={`${CHIP} ${query.preset === preset.key ? CHIP_ON : CHIP_OFF}`}
          aria-pressed={query.preset === preset.key}
          onclick={() => setPreset(preset.key)}>{preset.label}</button
        >
      {/each}
    </div>
    <span class="font-mono text-[11px] text-text-3">{spanLabel(span.range)}</span>
  </div>

  <div class="mt-2.5">
    <OverviewTrack
      reading={bars}
      range={span.range}
      window={span.window}
      {bucketMs}
      onwindow={setWindow}
      {onkey}
    />
  </div>

  <div
    class="mt-1.5 flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1 font-mono text-[11px] text-text-3"
  >
    <span class="text-text-1">{windowLabel}</span>
    <span>{windowSpan}</span>
    <span class="ml-auto">← → pan · + − zoom · L live</span>
  </div>

  {#if !view.available}
    <p class="mt-3 font-mono text-[11px] text-text-3">{view.notice}</p>
  {:else}
    <!--
      Decorative: every mark, gap and burst below is also a row in the list,
      which is the accessible reading of the same facts.
    -->
    {@const strip = fitStrip(view.strip, stripWidth)}
    <div
      aria-hidden="true"
      class="relative mt-[26px] mb-3.5 font-mono text-[11px] text-text-3"
      style:height={`${STRIP.height}px`}
      use:measure
    >
      <div class="absolute inset-x-0 h-px bg-hair-strong" style:top={`${STRIP.axisTop}px`}></div>

      {#each strip.ticks as tick (tick.pct)}
        <div
          class="absolute w-px"
          style:left={at(tick.pct)}
          style:top={`${STRIP.axisTop}px`}
          style:height={`${tick.major ? STRIP.majorTick : STRIP.minorTick}px`}
          style:background-color={tick.major ? 'var(--text-4)' : 'var(--hair-strong)'}
        ></div>
        {#if tick.label}
          <span
            class="absolute whitespace-nowrap"
            style:left={at(tick.pct)}
            style:top={`${STRIP.tickLabelTop}px`}
            style:transform={SHIFT[tick.align]}>{tick.label}</span
          >
        {/if}
      {/each}

      <!--
        The unread run, where the page ran out before the window did. Dotted
        rather than dashed, and named for what it is: a quiet gap and a
        stretch nobody read are opposite readings and must not share a mark.
      -->
      {#if strip.unseen}
        <div
          class="absolute border-t border-dotted border-text-4"
          style:left="0"
          style:width={`calc(${at(strip.unseen.toPct)} - ${STRIP.gapInset}px)`}
          style:top={`${STRIP.gapTop}px`}
        ></div>
        {#if strip.unseen.label}
          <span
            class="absolute bg-ink px-2.5 whitespace-nowrap text-text-2"
            style:left={at(strip.unseen.midPct)}
            style:top={`${STRIP.gapLabelTop}px`}
            style:transform={SHIFT.center}>{strip.unseen.label}</span
          >
        {/if}
      {/if}

      {#each strip.gaps as gap (gap.fromPct)}
        <div
          class="absolute border-t border-dashed border-hair-strong"
          style:left={`calc(${at(gap.fromPct)} + ${STRIP.gapInset}px)`}
          style:width={`calc(${at(gap.toPct - gap.fromPct)} - ${2 * STRIP.gapInset}px)`}
          style:top={`${STRIP.gapTop}px`}
        ></div>
        <!-- The label sits on the ink ground so the dashes stop behind it. -->
        {#if gap.label}
          <span
            class="absolute bg-ink px-2.5 whitespace-nowrap"
            style:left={at(gap.midPct)}
            style:top={`${STRIP.gapLabelTop}px`}
            style:transform={SHIFT.center}>{gap.label}</span
          >
        {/if}
      {/each}

      {#each strip.marks as mark (mark.id)}
        <div
          class="absolute"
          style:left={at(mark.pct)}
          style:top={`${STRIP.markTop}px`}
          style:width={`${STRIP.markWidth}px`}
          style:height={`${STRIP.markHeight}px`}
          style:margin-left={`${-STRIP.markWidth / 2}px`}
          style:background-color={MARK_COLOR[mark.category]}
        ></div>
        {#if mark.annotation}
          <span
            class="absolute whitespace-nowrap"
            style:left={at(mark.pct)}
            style:top={`${STRIP.annotationTop}px`}
            style:margin-left={`${-STRIP.markWidth / 2}px`}
            style:color={MARK_COLOR[mark.category]}>{mark.annotation}</span
          >
        {/if}
      {/each}

      {#each strip.bursts as burst (burst.pct)}
        <span
          class="absolute whitespace-nowrap text-text-2"
          style:left={at(burst.pct)}
          style:top={`${STRIP.annotationTop}px`}
          style:transform={SHIFT.end}>{burst.label}</span
        >
      {/each}
    </div>

    <!--
      What an empty window says of itself (#2282 D15). The charts above are
      drawn for the window; the list below is not clipped to it, so a blank
      strip over a list of yesterday's events is an honest reading that looked
      exactly like a broken feed. The sentence names the window AND the last
      event there was, and carries the one control that reaches past it —
      which is why the tail's copy of it stands down while this is up.
    -->
    {#if view.emptyWindow}
      <p class="mb-3.5 flex min-w-0 flex-wrap items-center gap-3 font-mono text-13 text-text-3">
        <span>{view.emptyWindow}</span>
        {#if olderOffered}
          <button type="button" class={OLDER} disabled={paging} onclick={() => void loadOlder()}
            >Load older {#if olderThan !== null}<span class="text-text-3"
                >before {fmtTime(olderThan, false)}</span
              >{/if}</button
          >
        {/if}
      </p>
    {/if}

    <!--
      The filters, one row above the list. All three are in the URL and all
      three narrow the bars above as well as the rows below.
    -->
    <div class="mb-2.5 flex min-w-0 flex-wrap items-center gap-2" aria-label="Filters">
      <!-- The epic filter is chips like the kinds beside it, not a native
           select among pills (#3468 D33); it still writes `epic=`. -->
      <span class="flex flex-wrap items-center gap-1" role="group" aria-label="Epic">
        <span class="mr-0.5 font-mono text-[11px] text-text-3">epic</span>
        <button
          type="button"
          class={`${CHIP} ${query.epic == null ? CHIP_ON : CHIP_OFF}`}
          aria-pressed={query.epic == null}
          onclick={() => setEpic('')}>All</button
        >
        {#each epicOptions as ref (ref)}
          <button
            type="button"
            class={`${CHIP} ${query.epic === ref ? CHIP_ON : CHIP_OFF}`}
            aria-pressed={query.epic === ref}
            onclick={() => setEpic(query.epic === ref ? '' : ref)}>{ref}</button
          >
        {/each}
      </span>

      {#if query.item}
        <span class={`${CHIP} ${CHIP_ON} cursor-default`}>
          <span class="text-text-3">item</span>{query.item}
          <button
            type="button"
            class={`inline-flex cursor-pointer items-center ${TAP_PILL}`}
            aria-label="Remove item filter"
            onclick={() => setItem(null)}>×</button
          >
        </span>
      {/if}

      <span class="flex flex-wrap gap-1" role="group" aria-label="Event kinds">
        {#each STREAM_KINDS as kind (kind)}
          <button
            type="button"
            class={`${CHIP} ${query.kinds.includes(kind) ? CHIP_ON : CHIP_OFF}`}
            aria-pressed={query.kinds.includes(kind)}
            onclick={() => onquery(toggleKind(query, kind as StreamKind))}>{kind}</button
          >
        {/each}
      </span>
    </div>

    {#if view.rows.length > 0}
      <ol class="min-w-0 list-none overflow-y-auto">
        {#each view.rows as row, i (row.key)}
          <li
            class="min-w-0"
            aria-label={row.kind === 'event' ? undefined : row.aria}
            data-event-id={row.kind === 'event' ? row.id : undefined}
          >
            <div
              class="grid items-center"
              style:grid-template-columns={grid}
              style:height={row.kind === 'event' ? undefined : `${ROW.gapHeight}px`}
              style:min-height={row.kind === 'event' ? `${ROW.height}px` : undefined}
            >
              {#if row.kind !== 'event'}
                <span></span>
                <span class="relative block h-full">
                  <span
                    class="absolute inset-y-0 w-0 border-l border-hair-strong"
                    class:border-dashed={row.kind === 'gap'}
                    class:border-dotted={row.kind === 'unseen'}
                    style:left={`${ROW.railLeft}px`}
                  ></span>
                </span>
                <!-- A quiet row is a measurement in the strip's own words
                     ("13m 26s quiet", #3052 N15), so data-13-mono, never the
                     uppercase 11px label that printed "13M 26S". The unseen
                     row keeps its label face: it names a cut, not a time. -->
                <span
                  class={row.kind === 'gap'
                    ? 'font-mono text-13 text-text-4'
                    : 'text-11 text-text-3'}
                  data-quiet-gap={row.kind === 'gap' ? '' : undefined}>{row.label}</span
                >
              {:else}
                <!--
                  The local reading, with the full UTC instant in the title:
                  the one thing two operators in two zones can compare, and the
                  only way to tell apart the hour a DST fall-back repeats.
                -->
                <span class="font-mono text-13 text-text-3" title={row.title}>{row.time}</span>
                <span class="relative block h-full">
                  <!--
                    The rail is one line through the whole list, cut back to the
                    dot at the first and last event so it does not run off it.
                  -->
                  <span
                    class="absolute w-px bg-hair-strong"
                    style:left={`${ROW.railLeft}px`}
                    style:top={i === 0 ? `${ROW.height / 2}px` : '0'}
                    style:bottom={i === view.rows.length - 1 ? `${ROW.height / 2}px` : '0'}
                  ></span>
                  <span
                    class="absolute box-border rounded-full border-2 border-ink"
                    style:left={`${ROW.dotLeft}px`}
                    style:top={`${ROW.dotTop}px`}
                    style:width={`${ROW.dotSize}px`}
                    style:height={`${ROW.dotSize}px`}
                    style:background-color={MARK_COLOR[row.category]}
                  ></span>
                </span>
                <!--
                    Where the log recorded a payload, the line is a control:
                    the row says what happened in words and the excerpt itself
                    is one click away. A row with nothing more to show gets no
                    control, so an expand never promises more than it has.
                  -->
                {#if row.detail}
                  <button
                    type="button"
                    class={`flex min-w-0 cursor-pointer items-baseline text-left font-mono
                             text-13 leading-4 underline-offset-4 hover:underline ${TAP_INLINE}`}
                    aria-expanded={openKey === row.key}
                    aria-controls={`stream-payload-${row.key}`}
                    onclick={() => (openKey = openKey === row.key ? null : row.key)}
                    >{@render words(row)}</button
                  >
                {:else}
                  <span class="flex min-w-0 items-baseline font-mono text-13 leading-4"
                    >{@render words(row)}</span
                  >
                {/if}
              {/if}
            </div>

            {#if row.kind === 'event' && row.detail && openKey === row.key}
              <!-- What the log recorded, verbatim, under the row that read it. -->
              <pre
                id={`stream-payload-${row.key}`}
                class="mb-2.5 overflow-x-auto border-l border-hair-strong bg-ink-raised px-3.5
                       py-2.5 font-mono text-[11px] whitespace-pre text-text-2"
                style:margin-left={`${ROW.timeWidth + ROW.railWidth}px`}>{row.detail}</pre>
              <!--
                In-app, both of them: the stream is where an operator notices
                something, and the two places they go next are the item's own
                page and the run that did it.
              -->
              <div
                class="mb-2.5 flex flex-wrap items-center gap-3 font-mono text-[11px]"
                style:margin-left={`${ROW.timeWidth + ROW.railWidth}px`}
              >
                {#if row.itemId}
                  <!--
                    Both hrefs come from the resolvers every other surface uses:
                    an id is opaque — encoded, not parsed — and one carrying a
                    `+`, an `&` or a `#` would otherwise select a different
                    item, or a truncated one.
                  -->
                  <a
                    class={`text-accent-2 hover:underline ${TAP_INLINE}`}
                    href={workHref(row.itemId)}>Open in Work</a
                  >
                  <button
                    type="button"
                    class={`cursor-pointer text-text-3 hover:text-text-1 ${TAP_INLINE}`}
                    onclick={() => setItem(row.itemId)}>Filter to {row.ref}</button
                  >
                {/if}
                {#if row.runId}
                  <a class={`text-accent-2 hover:underline ${TAP_INLINE}`} href={runHref(row.runId)}
                    >Open run</a
                  >
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ol>
    {/if}

    <!--
      Newest first, and the only way past one page — offered under an empty
      window too, which is where the operator most needs to reach further back.
      The button goes at the tail of the log: a control that fetches nothing is
      one that promises there is more to see.
    -->
    {@const tailOlder = olderOffered && !view.emptyWindow}
    {#if view.rows.length > 0 || tailOlder}
      <div
        class="mt-2.5 flex min-w-0 flex-wrap items-center gap-3 font-mono text-[11px] text-text-3"
      >
        {#if view.rows.length > 0}<span>Newest first · {view.meta}</span>{/if}
        {#if tailOlder}
          <button
            type="button"
            class={`ml-auto ${OLDER}`}
            disabled={paging}
            onclick={() => void loadOlder()}
            >Load older {#if olderThan !== null}<span class="text-text-3"
                >before {fmtTime(olderThan, false)}</span
              >{/if}</button
          >
        {/if}
      </div>
    {/if}
  {/if}
</section>
