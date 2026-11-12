<script lang="ts">
  /**
   * #2248 — the Monitor event stream's overview track: a histogram of the whole
   * range, with the brushed window drawn over it.
   *
   * About 40px of bars, one per bucket, stacked by family, and the brush on top.
   * It is a renderer: every position, height and colour comes from
   * `$lib/monitor/brush`, which a vitest holds without a browser. What lives
   * here is the track's own geometry, the pointer and key handlers, and one
   * measurement — the track's width in pixels, which is the only thing the
   * brush maths cannot know for itself.
   *
   * A 7-day range costs one small request because of this component: the server
   * counts the buckets (`/api/audit/histogram`) and the browser draws them. The
   * alternative — fetching the events to tally them here — is 1,400 rows for a
   * shape 84 bars wide.
   *
   * The track is the keyboard's seat for the whole brush: it takes focus, and
   * `←`/`→` pan, `+`/`−` zoom and `L` returns to live. That is why it is a
   * `slider` with the window's own reading as its value text — the brush is a
   * drawn box, and a screen reader gets the two instants instead.
   *
   * A DRAG IS ONE GESTURE, measured in the geometry it started in and reported
   * once. While the pointer is down the box is drawn from the gesture's own
   * window — local state, percentages, no URL and no fetch — and `onwindow` is
   * called when the hand comes up. Reporting every frame cost a history entry
   * and two audit requests per frame, and it fed the window back in as the
   * range: under `Custom…` the range IS the window (`stream-query.ts`) and in
   * live mode its right edge follows the clock, so a gesture measured against
   * the live range re-mapped the track between two frames of one drag — nine
   * minutes drawn over a 30-minute range collapsed to the bucket floor in four
   * of them.
   */
  import { bindShortcuts, registerShortcuts } from '$lib/keys/registry';
  import {
    BRUSH_KEYS,
    FAMILY_COLOR,
    brushKey,
    drawWindow,
    pan,
    resizeWindow,
    spanLabel,
    timeAtPx,
    windowRect,
    type OverviewReading,
    type Span
  } from './brush';

  let {
    /** The bars, already read — `null` while the histogram is unavailable. */
    reading,
    range,
    window: brushed,
    bucketMs,
    onwindow,
    onkey
  }: {
    reading: OverviewReading | null;
    range: Span;
    window: Span;
    bucketMs: number;
    /**
     * One completed gesture: the window the operator drew, panned or resized,
     * reported when the pointer comes up — never per frame.
     */
    onwindow: (next: Span) => void;
    /** A key the brush owns, by name, for the page to act on. */
    onkey: (key: string) => void;
  } = $props();

  /** The track's own geometry (UXMonitor: 40px of bars over a hairline axis). */
  const TRACK_HEIGHT = 40;
  /** A bucket with anything in it is never invisible, however short its bar. */
  const MIN_BAR = 3;

  /** Which part of the brush a drag has hold of — the three grips, plus a draw. */
  type Grip = 'draw' | 'start' | 'end' | 'window';

  /**
   * A drag in progress.
   *
   * It carries the geometry it is being measured in: `range` is the range the
   * track was drawn against when the pointer went down, and `from` the window
   * it went down on. Both are PINNED, because both can move under a gesture —
   * the range follows the window under `Custom…` and follows the clock while
   * live — and a gesture re-measured mid-drag is a gesture that collapses.
   *
   * `window` is what it has drawn so far, or `null` before it has moved, which
   * is how a plain click is told from a drag and reports nothing.
   */
  type Gesture = { grip: Grip; fromPx: number; range: Span; from: Span; window: Span | null };

  let track = $state<HTMLDivElement | null>(null);
  let drag = $state<Gesture | null>(null);

  /** The box the committed window draws — which gesture a press starts is its call. */
  const committed = $derived(windowRect(range, brushed));
  /** The box on the screen: the live gesture's own, or the committed one. */
  const rect = $derived(drag?.window == null ? committed : windowRect(drag.range, drag.window));
  /** The window the track is reading right now, drawn or committed. */
  const shown = $derived(drag?.window ?? brushed);
  const widthOf = () => track?.getBoundingClientRect().width ?? 0;
  const pxIn = (event: PointerEvent) =>
    event.clientX - (track?.getBoundingClientRect().left ?? 0);

  /**
   * How much of the track has to lie outside the brush for a drag of its body to
   * pan it. Half a percent is about 4px of the artboard's 793px track: with less
   * than that the pan is shorter than the box's own border.
   */
  const PAN_ROOM_PCT = 0.5;

  /**
   * Whether a drag of the brush's body pans it — which it does only while there
   * is range outside the brush to pan into.
   *
   * Every range opens with the brush around the whole of it (`stream-query.ts`:
   * the two start out equal), so the box covers the track from edge to edge. A
   * body drag there is `pan` of a window as wide as its range, which the brush
   * maths rightly answer with the window unchanged — so the box swallowed the
   * pointerdown and the drag did nothing at all, with drag-to-draw sitting
   * underneath it unreachable. With no room to pan, the body IS the track: the
   * drag draws, which is the gesture that gives the operator a narrower window
   * to pan in the first place.
   */
  const pannable = $derived(committed.widthPct <= 100 - PAN_ROOM_PCT);

  /** The slider's value text: the window, said rather than drawn. */
  const windowText = $derived(spanLabel(shown, ' to '));

  function start(grip: Grip, event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    track?.focus();
    // The track captures the pointer, so a drag that leaves it keeps moving the
    // window instead of stopping where the operator's hand crossed the edge —
    // which is the gesture that pans to the oldest end of the range.
    track?.setPointerCapture?.(event.pointerId);
    drag = { grip, fromPx: pxIn(event), range, from: brushed, window: null };
  }

  /**
   * The window a gesture has drawn, as a function of where the pointer is NOW:
   * the pinned range, the pinned window it started on, and the two offsets. One
   * drag is therefore one answer, however many frames it took and whatever the
   * range did underneath it, and a pan dragged to the range's edge and back
   * comes back to where it started instead of leaving the pixels behind.
   */
  function drawn(held: Gesture, at: number, width: number): Span {
    const { range: frame, from, fromPx, grip } = held;
    if (grip === 'draw') return drawWindow(frame, width, fromPx, at, bucketMs);
    if (grip === 'window')
      return pan(frame, from, timeAtPx(frame, width, at) - timeAtPx(frame, width, fromPx));
    return resizeWindow(frame, width, from, grip, at, bucketMs);
  }

  function move(event: PointerEvent): void {
    const held = drag;
    if (held === null) return;
    drag = { ...held, window: drawn(held, pxIn(event), widthOf()) };
  }

  /** The hand comes up: the one report this gesture makes. */
  function end(event: PointerEvent): void {
    const held = drag;
    track?.releasePointerCapture?.(event.pointerId);
    drag = null;
    // A press with no move is a click, not a drag, and selects nothing.
    if (held?.window) onwindow(held.window);
  }

  /**
   * A gesture the browser took away — a scroll, a second finger. It was drawn
   * and never finished, so it is dropped rather than reported: an interrupted
   * drag must not pin a window the operator did not choose.
   */
  function cancel(event: PointerEvent): void {
    track?.releasePointerCapture?.(event.pointerId);
    drag = null;
  }

  /**
   * A press on the track — its one tab stop. The keys are the brief's (pan,
   * zoom, live), registered on the console's key layer so `?` lists them
   * (#2781); a press anywhere else on the page, or one carrying ⌘, ^ or ⌥,
   * is left alone.
   */
  function key(event: KeyboardEvent): void {
    if (!(event.target instanceof Node) || !track?.contains(event.target)) return;
    const named = brushKey(event);
    if (named === null) return;
    event.preventDefault();
    onkey(named);
  }

  $effect(() => registerShortcuts(bindShortcuts('brush', BRUSH_KEYS, key)));
</script>

<!--
  The bars are decorative: every event in them is a row in the list below, which
  is the accessible reading of the same facts. The track itself is not — it is
  the brush's control, and it carries the window as its value.
-->
<div
  bind:this={track}
  role="slider"
  tabindex="0"
  aria-label="Event overview. Arrow keys pan the window, plus and minus zoom, L returns to live"
  aria-valuemin={range.startMs}
  aria-valuemax={range.endMs}
  aria-valuenow={shown.startMs}
  aria-valuetext={windowText}
  class="relative cursor-crosshair border-b border-hair-strong bg-ink-raised focus-visible:outline-accent-2"
  style:height={`${TRACK_HEIGHT}px`}
  onpointerdown={(event) => start('draw', event)}
  onpointermove={move}
  onpointerup={end}
  onpointercancel={cancel}
  ondblclick={() => onkey('reset')}
>
  {#if reading && reading.total === 0}
    <!--
      A track with no bars is a reading, not a fault: said in words, so an idle
      range does not look like an overview that failed to draw.
    -->
    <span
      class="pointer-events-none absolute inset-0 flex items-center justify-center font-mono
             text-[11px] text-text-4"
      aria-hidden="true">no events in this range</span
    >
  {/if}
  {#if reading}
    <div class="absolute inset-0" aria-hidden="true">
      <!--
        One slot per bucket, empty ones included: the track's horizontal scale is
        the range, so a quiet stretch has to keep its width or every bar after it
        would sit at the wrong time.
      -->
      {#each reading.bars as bar (bar.key)}
        <div
          class="absolute bottom-0 flex flex-col-reverse"
          data-bucket={bar.key}
          style:left={`${Number(bar.leftPct.toFixed(3))}%`}
          style:width={`${Number(bar.widthPct.toFixed(3))}%`}
          style:height={bar.total === 0
            ? '0'
            : `max(${MIN_BAR}px, ${Number(bar.heightPct.toFixed(2))}%)`}
        >
          {#each bar.bands as band (band.family)}
            <i
              class="block w-full"
              data-family={band.family}
              style:flex={`${band.count} 0 0`}
              style:background-color={FAMILY_COLOR[band.family]}
            ></i>
          {/each}
        </div>
      {/each}
    </div>
  {/if}

  <!-- What is outside the brush, shaded back, so the window reads as the subject. -->
  <div
    aria-hidden="true"
    class="absolute inset-y-0 left-0 bg-ink/60"
    style:width={`${Number(rect.leftPct.toFixed(3))}%`}
  ></div>
  <div
    aria-hidden="true"
    class="absolute inset-y-0 right-0 bg-ink/60"
    style:left={`${Number((rect.leftPct + rect.widthPct).toFixed(3))}%`}
  ></div>

  <div
    class={`absolute inset-y-0 rounded-[2px] border border-text-1 bg-text-1/5 ${
      pannable ? 'cursor-grab' : 'cursor-crosshair'
    }`}
    style:left={`${Number(rect.leftPct.toFixed(3))}%`}
    style:width={`${Number(rect.widthPct.toFixed(3))}%`}
    data-grip={pannable ? 'window' : 'draw'}
    onpointerdown={(event) => {
      event.stopPropagation();
      start(pannable ? 'window' : 'draw', event);
    }}
    role="presentation"
  >
    <!-- The two handles, drawn as the artboard draws them, 10px wide. -->
    <span
      class="absolute top-1/2 -left-[5px] h-6 w-[10px] -translate-y-1/2 cursor-ew-resize
             rounded-[2px] bg-text-1"
      data-grip="start"
      role="presentation"
      onpointerdown={(event) => {
        event.stopPropagation();
        start('start', event);
      }}
    ></span>
    <span
      class="absolute top-1/2 -right-[5px] h-6 w-[10px] -translate-y-1/2 cursor-ew-resize
             rounded-[2px] bg-text-1"
      data-grip="end"
      role="presentation"
      onpointerdown={(event) => {
        event.stopPropagation();
        start('end', event);
      }}
    ></span>
  </div>
</div>
