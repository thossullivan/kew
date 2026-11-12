/**
 * #2299 — the one time axis every kew timeline draws its ruler from.
 *
 * Three surfaces showed a time grid and each chose its own steps: the epic run
 * Gantt (#2271) stepped 5 minutes to 1 day, the Monitor's event strip stepped a
 * ladder of its own aimed at eight labels, and the brush's overview counted in
 * buckets. Three ladders put three different readings of the same minute on one
 * screen — which is the same failure `$lib/time` exists to prevent one level
 * down, where three components printed one instant in three zones (#2205).
 *
 * So the ladder, the label and the thinning pass live here, and a surface
 * contributes only its own pixels and its own idea of how much room a label
 * needs. The epic Gantt is moved onto it by this change — its `ticks()` is
 * where this arithmetic comes from, and `timeline.ts` now asks for it rather
 * than keeping a copy; the Monitor's strip and the brush move as each of those
 * surfaces is next opened, which is why the options below carry the knobs a
 * percentage-based, centre-labelled axis needs and not only the Gantt's.
 *
 * The interval ladder, the "first nice interval at or above the wanted
 * spacing" rule and the minor-divides-the-major rule follow the ideas of
 * Dagster's `js_modules/ui-core/src/ui/timeAxis.ts` (Apache-2.0,
 * https://github.com/dagster-io/dagster @ 54577dad, Copyright 2025 Dagster
 * Labs, Inc.). Modified by kew: re-implemented in TypeScript for this console's
 * geometry, and — the one deliberate divergence — the grid is stepped on the
 * VIEWER'S LOCAL CLOCK rather than on multiples of the interval since the UTC
 * epoch. In a zone whose offset is not a whole number of steps (UTC-2:30,
 * +5:30) epoch-stepped ticks never land on local midnight, the thinning pass
 * then drops the dated label in favour of the hour beside it, and the ruler
 * carries no date at all across a day boundary (#2271 review round 7).
 *
 * `$lib/time` here is the module file `src/lib/time.ts` — the formatters — not
 * this directory: a path resolves to the file before the folder, and every
 * clock reading on the axis still goes through the console's one formatter.
 */
import { fmtTime } from '$lib/time';

/** A window of time over a width of pixels: `startMs` sits at x = 0 and
 * `endMs` at x = `width`. Structurally the epic Gantt's `Scale`, so that
 * module's own geometry passes straight through. */
export type AxisWindow = {
  readonly startMs: number;
  readonly endMs: number;
  readonly width: number;
};

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * The only intervals a ruler may step by, finest first.
 *
 * Every rung is a round amount of clock time a person says out loud, and each
 * is divided evenly by some finer rung — which is what lets a minor grid
 * subdivide a major one without walking off it.
 */
export const NICE_INTERVALS: readonly number[] = [
  SECOND,
  5 * SECOND,
  10 * SECOND,
  15 * SECOND,
  30 * SECOND,
  MINUTE,
  2 * MINUTE,
  5 * MINUTE,
  10 * MINUTE,
  15 * MINUTE,
  30 * MINUTE,
  HOUR,
  2 * HOUR,
  3 * HOUR,
  6 * HOUR,
  12 * HOUR,
  DAY
];

/** The coarsest rung: past it the step cannot grow and labels thin instead. */
export const COARSEST_INTERVAL = NICE_INTERVALS[NICE_INTERVALS.length - 1];

/** The closest two labels may sit before one of them is dropped, and the
 * spacing the major interval is chosen for. A `2026-09-28` in 11px mono is
 * about 62px wide. */
export const MIN_LABEL_SPACING_PX = 72;

/** The closest two minor gridlines may sit. Finer than this is a grey wash,
 * not a grid. */
export const MIN_MINOR_SPACING_PX = 8;

/** Hard cap on the lines one ruler draws, so no window — and the window can
 * arrive from a URL — turns into an unbounded loop. */
export const MAX_AXIS_TICKS = 500;

/** One character of the 11px mono face the rulers label in — #2271's own
 * measurement, where a `2026-09-28` comes to about 62px. It is how much room
 * a label needs, which is how the edge rule knows whether one fits. */
export const LABEL_CHAR_PX = 6.2;

/** The gap a label leaves between itself and its own tick line. */
export const LABEL_GAP_PX = 3;

/** The word a surface prints at its now marker, and so the width the axis
 * keeps clear for it. */
export const NOW_LABEL = 'now';

/** Which side of its tick a label is drawn on: beside it (every ruler here) or
 * centred over it. It decides which edge a label can run off. */
export type LabelAnchor = 'start' | 'center';

export type AxisOptions = {
  /** @see MIN_LABEL_SPACING_PX */
  readonly minLabelSpacingPx?: number;
  /** @see MIN_MINOR_SPACING_PX */
  readonly minMinorSpacingPx?: number;
  /** @see MAX_AXIS_TICKS */
  readonly maxTicks?: number;
  /** @see LabelAnchor */
  readonly labelAnchor?: LabelAnchor;
  /** @see LABEL_CHAR_PX */
  readonly labelCharPx?: number;
  /** Whether unlabelled minor gridlines are emitted. A ruler that labels
   * every line it draws (the epic Gantt) asks for majors only. */
  readonly minors?: boolean;
  /** Whether local midnights get their own dated line. */
  readonly dayLines?: boolean;
  /** The instant the surface draws its now marker at, where it draws one.
   * The word beside that marker is a label on the same ruler, so an hour
   * label whose box overlaps it is dropped — the edge rule, with the now
   * label standing in for the edge (#2385). A DATED label outranks the word
   * instead: ask `nowLabelBox` where the word goes, because the answer may
   * be nowhere. Absent, or outside the window, and no space is kept. */
  readonly nowMs?: number;
  /** @see NOW_LABEL */
  readonly nowLabel?: string;
};

/** The visible duration. Never zero: every reading below divides by it. */
export const axisSpanMs = (window: AxisWindow): number =>
  Math.max(1, window.endMs - window.startMs);

/** Pixels per millisecond — the zoom, as the ladder reads it. */
export const axisPxPerMs = (window: AxisWindow): number =>
  Math.max(1, window.width) / axisSpanMs(window);

/** Where an instant is drawn. */
export const axisX = (window: AxisWindow, ms: number): number =>
  ((ms - window.startMs) / axisSpanMs(window)) * Math.max(1, window.width);

/** Which instant is under a pixel. */
export const axisMsAt = (window: AxisWindow, x: number): number =>
  window.startMs + (x / Math.max(1, window.width)) * axisSpanMs(window);

/**
 * The major and minor interval for a window.
 *
 * The major is the first rung at or above the room a label needs, so labels
 * never overprint at any zoom the ladder can answer for. The minor is the
 * densest rung that divides the major evenly and is still `minMinorSpacingPx`
 * apart; where there is none, the minor IS the major and every line is a
 * labelled one.
 */
export function axisIntervals(
  window: AxisWindow,
  options: AxisOptions = {}
): { majorMs: number; minorMs: number } {
  const msPerPx = 1 / axisPxPerMs(window);
  const wantMajor = (options.minLabelSpacingPx ?? MIN_LABEL_SPACING_PX) * msPerPx;
  const majorMs = NICE_INTERVALS.find((step) => step >= wantMajor) ?? COARSEST_INTERVAL;
  const wantMinor = (options.minMinorSpacingPx ?? MIN_MINOR_SPACING_PX) * msPerPx;
  const minorMs = NICE_INTERVALS.find(
    (step) => step < majorMs && majorMs % step === 0 && step >= wantMinor
  );
  return { majorMs, minorMs: minorMs ?? majorMs };
}

/**
 * The step a grid really takes: the interval it asked for, or a whole multiple
 * of it where the window holds more lines than the cap allows.
 *
 * The window can arrive from a URL, so its span is whatever an operator or a
 * link says it is; a decade at a day's step is 3,650 lines to lay out. A whole
 * multiple is still a round amount of clock time, and — because every rung
 * divides a day — a multiple of a day still lands on local midnights.
 */
function effectiveStep(window: AxisWindow, step: number, maxTicks: number): number {
  const wanted = Math.floor(axisSpanMs(window) / step) + 2;
  return wanted <= maxTicks ? step : step * Math.ceil(wanted / maxTicks);
}

/** The multiples of `step` inside the window, on the viewer's clock — which
 * is why a label lands on a round LOCAL time rather than a round UTC one. */
function multiples(window: AxisWindow, step: number): number[] {
  const out: number[] = [];
  if (step <= 0) return out;
  const offset = -new Date(window.startMs).getTimezoneOffset() * MINUTE;
  const first = Math.ceil((window.startMs + offset) / step) * step - offset;
  const count = Math.floor((window.endMs - first) / step);
  for (let i = 0; i <= count; i++) out.push(first + i * step);
  return out;
}

/** "2026-09-28" — ISO, and therefore the same for every viewer, in their own
 * zone (the same contract as `fmtDateTime`). */
export function dayLabel(ms: number): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).formatToParts(ms);
  const at = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? '';
  return `${at('year')}-${at('month')}-${at('day')}`;
}

/**
 * Every local midnight inside the window.
 *
 * Stepped with `setDate` rather than by adding 86.4e6, so a DST change does
 * not slide the boundary off noon — which is also why this is its own pass and
 * not left to the grid: the grid's offset is read once, at the window's start,
 * and an hour-stepped grid drifts off local midnight after a DST change.
 */
function dayLines(window: AxisWindow): number[] {
  const out: number[] = [];
  const at = new Date(window.startMs);
  at.setHours(0, 0, 0, 0);
  if (at.getTime() < window.startMs) at.setDate(at.getDate() + 1);
  while (at.getTime() <= window.endMs) {
    out.push(at.getTime());
    at.setDate(at.getDate() + 1);
  }
  return out;
}

/**
 * A major tick's reading: enough of the clock to tell it from its neighbours
 * and no more. Seconds only on a grid fine enough to need them, and the date
 * once the grid steps by whole days — "00:00" twice over is two labels nobody
 * can tell apart.
 */
export const axisLabel = (ms: number, majorMs: number): string =>
  majorMs >= DAY ? dayLabel(ms) : fmtTime(ms, majorMs < MINUTE);

export type AxisTick = {
  ms: number;
  x: number;
  /** A labelled line (a day line is one too), rather than a minor gridline. */
  major: boolean;
  /** A local midnight: a stronger line, and the date rather than the time. */
  day: boolean;
  /** The reading, or `''` where it was dropped to keep the ruler legible. The
   * line is still drawn — a tick is a fact, its label is a courtesy. */
  label: string;
};

/** The pixels a label covers: the gap off its own line, then the text. */
export type LabelBox = { readonly from: number; readonly to: number };

/** How much room a reading needs, measured the way the rulers draw it. */
const labelRoom = (label: string, options: AxisOptions): number =>
  LABEL_GAP_PX + label.length * (options.labelCharPx ?? LABEL_CHAR_PX);

/** The box a label drawn for a tick at `x` covers. */
const labelBox = (x: number, room: number, options: AxisOptions): LabelBox => {
  const from = (options.labelAnchor ?? 'start') === 'center' ? x - room / 2 : x;
  return { from, to: from + room };
};

/** Whether two boxes share a pixel — one label drawn over another. */
const overlaps = (a: LabelBox, b: LabelBox): boolean => a.from < b.to && a.to > b.from;

/**
 * Where the now label WOULD go, before the ruler decides whether the word is
 * drawn at all: `null` where there is no now marker on this ruler — no
 * instant given, or one outside the window.
 *
 * Measured like any other label, and then clamped into the canvas: a marker
 * at the right-hand edge draws its word to the LEFT of where it would
 * otherwise go rather than half off the canvas, which is where it lands on
 * top of the last tick's reading (#2385). Surfaces draw the word at
 * `from + LABEL_GAP_PX`, so the pixels they use and the pixels this axis
 * keeps clear are the same pixels.
 */
function wantedNowBox(window: AxisWindow, options: AxisOptions): LabelBox | null {
  const { nowMs } = options;
  if (nowMs === undefined) return null;
  const width = Math.max(1, window.width);
  const x = axisX(window, nowMs);
  if (x < 0 || x > width) return null;
  const room = labelRoom(options.nowLabel ?? NOW_LABEL, options);
  const { from } = labelBox(x, room, options);
  const at = Math.max(0, Math.min(from, width - room));
  return { from: at, to: at + room };
}

/**
 * Drop the labels that cannot be read: the ones that would run off an edge of
 * the canvas, the ones the now label is already sitting on, and then the ones
 * that would overprint a label already kept. Answers with the box the now
 * label really occupies, which is `null` when the word is not drawn.
 *
 * The edge rule is the label's OWN width, not a fixed margin: an `08:00` that
 * fits in the last 40 pixels keeps its label where a `2026-09-28` in the same
 * place would have been drawn half off the canvas. Clipped text is the failure
 * this replaces — the browser draws it, the reader cannot read it, and no
 * screenshot shows which digits went missing.
 *
 * Dated lines claim their label first and hour labels fill the space around
 * them, so a ruler can lose an hour label but never the date across a day
 * boundary (#2271 review round 7). An edge-dropped label leaves its space to
 * its neighbour: it was never drawn, so nothing can collide with it.
 *
 * The now label is an edge of the same kind for an HOUR reading: a tick
 * reading under it is not a crowded pair that thinning can choose between —
 * it is two words printed in one place, "10:00now" (#2385). The tick's line
 * stays; its reading goes.
 *
 * It is NOT an edge for a dated one. The date outranks every other reading on
 * this ruler, and that rule cannot be bought off with three letters: a live
 * epic opened in the hour after local midnight puts the marker a few pixels
 * off the day line, and letting the word win there leaves the whole ruler
 * carrying no date at all — #2271 round 7's failure, re-entered through the
 * now cap, and traded for an overlap that can be a single trailing character
 * (#2385 review). So the word yields: the date keeps its reading and the
 * surface draws no "now", which costs the reader nothing it cannot see —
 * the now marker is its own full-height accent line, drawn whatever this
 * pass decides.
 */
function thinLabels(ticks: AxisTick[], window: AxisWindow, options: AxisOptions): LabelBox | null {
  const minGap = options.minLabelSpacingPx ?? MIN_LABEL_SPACING_PX;
  const width = Math.max(1, window.width);
  const kept: number[] = [];
  const crowded = (x: number): boolean => kept.some((at) => Math.abs(at - x) < minGap);
  /** Whether the label's own box stays inside the canvas. */
  const fits = (box: LabelBox): boolean => box.from >= 0 && box.to <= width;
  const boxOf = (tick: AxisTick): LabelBox =>
    labelBox(tick.x, labelRoom(tick.label, options), options);
  // The dated pass, against the edge and its neighbours only — the now label
  // has no say over a date, so it is not consulted yet.
  const dates: LabelBox[] = [];
  for (const tick of ticks) {
    if (tick.label === '' || !tick.day) continue;
    const box = boxOf(tick);
    if (!fits(box) || crowded(tick.x)) tick.label = '';
    else {
      kept.push(tick.x);
      dates.push(box);
    }
  }
  const wanted = wantedNowBox(window, options);
  const now = wanted !== null && !dates.some((date) => overlaps(date, wanted)) ? wanted : null;
  // The hour pass, which fills the space the dates and the word leave.
  for (const tick of ticks) {
    if (tick.label === '' || tick.day) continue;
    const box = boxOf(tick);
    if (!fits(box) || (now !== null && overlaps(box, now)) || crowded(tick.x)) tick.label = '';
    else kept.push(tick.x);
  }
  return now;
}

/** A ruler, as the thinning pass left it: its lines, and where the now label
 * ended up — `null` when the word is not drawn. */
type AxisRuler = {
  readonly ticks: AxisTick[];
  readonly nowLabel: LabelBox | null;
};

/**
 * The ruler: minor gridlines, labelled majors on round local times, and a
 * dated line at every local midnight, in time order and deduplicated — a
 * midnight that is also a major tick is ONE line, not a date drawn on top of
 * an hour.
 *
 * The three passes share one budget, dated lines first: a line the cap cannot
 * afford is a line that is not drawn, and the first thing a ruler can do
 * without is a hairline between two labels.
 *
 * The lines and the now label come back together because the thinning pass
 * decides them together — whether the word is drawn depends on which dates
 * survived, and which hour readings survive depends on the word. This is the
 * one place that is worked out; `axisTicks` and `nowLabelBox` below are two
 * views of it, not two rules, which is what stops a ruler clearing space for
 * a word it is not printing.
 */
function axisRuler(window: AxisWindow, options: AxisOptions = {}): AxisRuler {
  const { majorMs, minorMs } = axisIntervals(window, options);
  const maxTicks = options.maxTicks ?? MAX_AXIS_TICKS;
  const majorStep = effectiveStep(window, majorMs, maxTicks);
  const wantMinor = effectiveStep(window, minorMs, maxTicks);
  // A minor that no longer subdivides the widened major would be a row of
  // hairlines its own labels never land on; at those spans every line is a
  // major one.
  const minorStep = majorStep % wantMinor === 0 ? wantMinor : majorStep;
  const by = new Map<number, AxisTick>();
  /** Fold one line into the grid. False once the budget is spent. */
  const mark = (ms: number, kind: 'minor' | 'major' | 'day'): boolean => {
    let tick = by.get(ms);
    if (tick === undefined) {
      if (by.size >= maxTicks) return false;
      tick = { ms, x: axisX(window, ms), major: false, day: false, label: '' };
      by.set(ms, tick);
    }
    if (kind !== 'minor') tick.major = true;
    if (kind === 'major' && !tick.day) tick.label = axisLabel(ms, majorStep);
    if (kind === 'day') {
      tick.day = true;
      tick.label = dayLabel(ms);
    }
    return true;
  };
  // A grid stepping by more than a day visits midnights of its own and labels
  // them with their date; below that, midnight needs its own dated line.
  if ((options.dayLines ?? true) && majorStep <= DAY) {
    for (const ms of dayLines(window)) if (!mark(ms, 'day')) break;
  }
  for (const ms of multiples(window, majorStep)) if (!mark(ms, 'major')) break;
  if (options.minors ?? true) {
    for (const ms of multiples(window, minorStep)) if (!mark(ms, 'minor')) break;
  }
  const ticks = [...by.values()].sort((a, b) => a.ms - b.ms);
  return { ticks, nowLabel: thinLabels(ticks, window, options) };
}

/** The ruler's lines and their readings. @see axisRuler */
export const axisTicks = (window: AxisWindow, options: AxisOptions = {}): AxisTick[] =>
  axisRuler(window, options).ticks;

/**
 * The box the now label covers on this ruler, or `null` where the word is not
 * drawn: no instant given, one outside the window, or one whose word would
 * have been printed over a dated reading, which outranks it.
 *
 * A surface that draws a now marker asks BOTH this and `axisTicks`, with the
 * same options: the answers are two views of one ruler, so the pixels the
 * surface writes the word into are the pixels the readings were thinned
 * against.
 */
export const nowLabelBox = (window: AxisWindow, options: AxisOptions = {}): LabelBox | null =>
  axisRuler(window, options).nowLabel;
