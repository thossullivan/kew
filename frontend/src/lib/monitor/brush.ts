/**
 * #2248 — the arithmetic of the Monitor's overview track: the histogram's bars,
 * pixels to time, and the four moves that change the brushed window.
 *
 * The overview is a histogram of the whole range; the brush is the slice of it
 * the detail strip and the list are drawn from. Everything about that slice
 * that could be got wrong lives here as arithmetic, so a vitest can hold it
 * without a browser: drag to draw, drag to pan, drag an edge to resize, keys to
 * pan and zoom. The component owns pointer events and nothing else.
 *
 * Three rules.
 *
 * **Exact at both edges.** `timeAtPx(range, width, 0)` is the range's start and
 * `timeAtPx(range, width, width)` is its end, to the millisecond, and `pxAtTime`
 * is their inverse. A brush drawn by dragging the full width of the track must
 * select the range it was drawn over — not the range minus a rounding error,
 * which is what makes a "whole range" selection quietly drop the newest event.
 *
 * **A move never leaves the range.** Pan and zoom clamp, and clamping preserves
 * the window's span rather than squashing it: a window pushed off the left edge
 * comes to rest flush against it, the same width it was. The operator who holds
 * the arrow key down ends up at the oldest edge, not at a one-pixel window.
 *
 * **A window is at least one bucket.** The overview cannot resolve finer than
 * its own bucket, so a brush narrower than one bucket says nothing the strip
 * can draw and the list would answer with a near-empty window. Zoom and draw
 * both widen to one bucket, around the centre of what was asked for.
 */
import type { AuditHistogram } from '$lib/api';
import type { KeyDef } from '$lib/keys/registry';
import { fmtDateTime, fmtTime, parseInstant } from '$lib/time';
import type { Span } from './stream-query';

export type { Span };

/** Keyboard pan, as a fraction of the window. `←`/`→` move 10% (2.2 §3). */
export const PAN_FRACTION = 0.1;

/** Keyboard zoom: `+` narrows to 2/3, `−` widens by the reciprocal. */
export const ZOOM_FACTOR = 2 / 3;

/**
 * The bucket widths the overview may ask the histogram for, finest first. Each
 * is a round amount of clock time, so a bar edge falls on a minute, an hour or
 * a day rather than on an arbitrary offset from "now".
 */
export const BUCKET_LADDER_S = [
  10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400
] as const;

/** What the overview aims for: enough bars to read a shape, few enough to see. */
export const TARGET_BUCKETS = 96;

/**
 * What `/api/audit/histogram` will serve in one request — `MAX_BUCKETS` in
 * `service/audit.py`, which turns anything bigger into a 422. The bucket size
 * below is what keeps the overview inside it.
 */
export const MAX_BUCKETS = 500;

/** The unit a bucket is counted in past the ladder's coarsest rung. */
const DAY_S = 86_400;

/**
 * The bucket size for a range, in seconds — the finest ladder step that keeps
 * the bar count near `TARGET_BUCKETS`.
 *
 * The ladder's job is to make one request per range rather than one per event:
 * a 7-day range is 84 two-hour bars, not 1,400 events fetched to be counted in
 * the browser.
 *
 * `TARGET_BUCKETS` holds for EVERY span, including the ones the ladder's
 * coarsest rung is too fine for. The range can be as wide as a link says it is
 * (`from`/`to` arrive from the URL, and `Custom…` zooms out), and falling
 * through to one-day buckets asked for one bucket per day: past 500 days that is
 * more than `MAX_BUCKETS`, the endpoint answers 422, and the overview goes
 * permanently blank. Past the ladder the bucket is counted in whole days
 * instead — the same move `tickStepsFor` makes for the axis, and still a round
 * amount of clock time.
 */
export function bucketSecondsFor(spanMs: number): number {
  const wanted = Math.max(1, spanMs) / 1000 / TARGET_BUCKETS;
  return BUCKET_LADDER_S.find((step) => step >= wanted) ?? Math.ceil(wanted / DAY_S) * DAY_S;
}

const spanOf = (window: Span): number => window.endMs - window.startMs;

/** The day an instant falls on in the viewer's zone, for comparing two of them. */
const dayOf = (ms: number): string => fmtDateTime(ms).slice(0, 10);

/**
 * "02:45 – 04:30" for a window today, or "09-23 16:00 – 09-24 16:00" for one
 * whose edges fall on different days or on a day that is not today.
 *
 * A 24-hour range read "15:46 – 15:46" without this: both edges are correct and
 * the label says the range is empty. And a window inside yesterday read as two
 * clock times with no day, which every reader takes for today (#3014). The date
 * is added only when it is the thing that distinguishes them, so the common
 * case — a window today — stays the two clock times the artboard prints. One
 * clock, one formatter, same as every other time here.
 *
 * `separator` is what a caller changes: the artboard writes the brush's
 * `aria-valuetext` as "02:45 to 04:30", because a dash read aloud is not a word,
 * and the two readings must still be one string's worth of arithmetic. `nowMs`
 * is the instant "today" is read at.
 */
export function spanLabel(span: Span, separator = ' – ', nowMs = Date.now()): string {
  const day = dayOf(span.startMs);
  const today = day === dayOf(span.endMs) && day === dayOf(nowMs);
  const read = (ms: number) => (today ? fmtTime(ms, false) : fmtDateTime(ms).slice(5, 16));
  return `${read(span.startMs)}${separator}${read(span.endMs)}`;
}

/**
 * The instant a pixel offset along the track names, clamped to the range.
 *
 * `width` of zero is the first frame, before the element has been measured:
 * there is no position to read off a track with no width, so the range's start
 * is the honest answer rather than a NaN that would reach the URL.
 */
export function timeAtPx(range: Span, widthPx: number, px: number): number {
  if (widthPx <= 0) return range.startMs;
  const clamped = Math.min(Math.max(px, 0), widthPx);
  if (clamped === widthPx) return range.endMs;
  return range.startMs + (spanOf(range) * clamped) / widthPx;
}

/** The pixel offset an instant sits at, clamped to the track. */
export function pxAtTime(range: Span, widthPx: number, ms: number): number {
  const span = spanOf(range);
  if (widthPx <= 0 || span <= 0) return 0;
  const clamped = Math.min(Math.max(ms, range.startMs), range.endMs);
  return (widthPx * (clamped - range.startMs)) / span;
}

/** Where along the range an instant sits, 0–100 — the units the strip draws in. */
export function pctAtTime(range: Span, ms: number): number {
  return pxAtTime(range, 100, ms);
}

/** The brush's box over the track, as percentages of it. */
export function windowRect(range: Span, window: Span): { leftPct: number; widthPct: number } {
  const leftPct = pctAtTime(range, window.startMs);
  return { leftPct, widthPct: Math.max(0, pctAtTime(range, window.endMs) - leftPct) };
}

/**
 * The window widened to at least one bucket, about its own centre, then slid
 * back inside the range. A range narrower than one bucket cannot hold one, so
 * it answers with the whole range.
 */
export function snapToBucket(range: Span, window: Span, bucketMs: number): Span {
  const want = Math.max(spanOf(window), Math.max(1, bucketMs));
  if (want >= spanOf(range)) return { ...range };
  const centre = (window.startMs + window.endMs) / 2;
  return slide(range, { startMs: centre - want / 2, endMs: centre + want / 2 });
}

/** A window moved whole until it is inside the range, keeping its span. */
function slide(range: Span, window: Span): Span {
  const span = spanOf(window);
  if (span >= spanOf(range)) return { ...range };
  const startMs = Math.min(Math.max(window.startMs, range.startMs), range.endMs - span);
  return { startMs: Math.round(startMs), endMs: Math.round(startMs + span) };
}

/** The window moved by `deltaMs`, clamped at the range's edges. */
export function pan(range: Span, window: Span, deltaMs: number): Span {
  return slide(range, { startMs: window.startMs + deltaMs, endMs: window.endMs + deltaMs });
}

/** `←`/`→`: a pan of `PAN_FRACTION` of the window, in the sign's direction. */
export function panBy(range: Span, window: Span, fraction: number): Span {
  return pan(range, window, spanOf(window) * fraction);
}

/**
 * The window scaled by `factor` about its own centre — `+` narrows, `−` widens
 * — never finer than one bucket and never wider than the range.
 *
 * The centre is what is held fixed: an operator zooming into a burst is asking
 * for more detail on what is already in front of them, and a zoom that drifted
 * would make them chase it.
 */
export function zoom(range: Span, window: Span, factor: number, bucketMs: number): Span {
  const centre = (window.startMs + window.endMs) / 2;
  const want = Math.min(spanOf(window) * factor, spanOf(range));
  const scaled = { startMs: centre - want / 2, endMs: centre + want / 2 };
  return snapToBucket(range, scaled, bucketMs);
}

/** What a key on the brush asks the page for. */
export type BrushKey = 'pan-older' | 'pan-newer' | 'zoom-in' | 'zoom-out' | 'live';

/** The brush keys answer only while the track has focus (#2841, Q8). */
const TRACK = 'on the event track';

/**
 * The track's shortcuts (#2781), on the console's key layer so the `?` overlay
 * lists them: `←`/`→` pan, `+`/`−` zoom and `L` returns to live. `=` and `_`
 * are the same keys unshifted and shifted, so either spelling answers.
 */
export const BRUSH_KEYS: readonly (KeyDef & { readonly action: BrushKey })[] = [
  { key: 'ArrowLeft', description: 'pan the window older', where: TRACK, action: 'pan-older' },
  { key: 'ArrowRight', description: 'pan the window newer', where: TRACK, action: 'pan-newer' },
  { key: '+', description: 'zoom in', where: TRACK, action: 'zoom-in' },
  { key: '=', description: 'zoom in', where: TRACK, action: 'zoom-in' },
  { key: '-', description: 'zoom out', where: TRACK, action: 'zoom-out' },
  { key: '_', description: 'zoom out', where: TRACK, action: 'zoom-out' },
  { key: 'l', description: 'back to live', where: TRACK, action: 'live' },
  { key: 'L', description: 'back to live', where: TRACK, action: 'live' }
];

/**
 * The brush's reading of a press, or `null` when it is not the brush's.
 *
 * A press carrying ⌘, ^ or ⌥ is the browser's — ⌘= and ⌘− zoom the page and
 * ⌘← goes back — so it is never the brush's, whichever surface offers it.
 */
export function brushKey(press: {
  readonly key: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly altKey: boolean;
}): BrushKey | null {
  if (press.metaKey || press.ctrlKey || press.altKey) return null;
  return BRUSH_KEYS.find((def) => def.key === press.key)?.action ?? null;
}

/**
 * A drag across empty track: the window between two pixel offsets, in either
 * drag direction, at least one bucket wide.
 */
export function drawWindow(
  range: Span,
  widthPx: number,
  fromPx: number,
  toPx: number,
  bucketMs: number
): Span {
  const a = timeAtPx(range, widthPx, Math.min(fromPx, toPx));
  const b = timeAtPx(range, widthPx, Math.max(fromPx, toPx));
  return snapToBucket(range, { startMs: a, endMs: b }, bucketMs);
}

/** Which part of the brush a drag has hold of. */
export type BrushGrip = 'start' | 'end' | 'window';

/**
 * One edge dragged to a pixel offset. The edges may cross — dragging the left
 * handle past the right one keeps a window rather than inverting it, because a
 * negative window has no reading and the operator's intent is plain.
 */
export function resizeWindow(
  range: Span,
  widthPx: number,
  window: Span,
  grip: 'start' | 'end',
  px: number,
  bucketMs: number
): Span {
  const at = timeAtPx(range, widthPx, px);
  const fixed = grip === 'start' ? window.endMs : window.startMs;
  const next = { startMs: Math.min(at, fixed), endMs: Math.max(at, fixed) };
  return snapToBucket(range, next, bucketMs);
}

/**
 * The four bands a bar is stacked from, bottom to top, with the colour each is
 * drawn in. These are TOPICS, not verdicts: the overview is cyan where a review
 * or an attestation happened, never green, because the histogram counts event
 * NAMES and a name cannot say whether the check it records passed.
 *
 * That is the one thing this reading must not get wrong. `stream.ts` spends
 * green on an attestation that came back good and reads the verdict out of the
 * payload to decide; a histogram has no payload to read, so a green band here
 * would be a claim bought off an event's name.
 */
export const OVERVIEW_FAMILIES = ['other', 'review', 'money', 'needs-you'] as const;

export type OverviewFamily = (typeof OVERVIEW_FAMILIES)[number];

export const FAMILY_COLOR: Record<OverviewFamily, string> = {
  other: 'var(--text-4)',
  review: 'var(--accent-2)',
  money: 'var(--warn)',
  'needs-you': 'var(--accent)'
};

/** The legend above the track, left to right, in the stack's own order. */
export const FAMILY_LABEL: Record<OverviewFamily, string> = {
  other: 'other',
  review: 'review / evidence',
  money: 'money',
  'needs-you': 'needs you'
};

const MONEY_NAME = /^(budget|spend)|^runner_usage/;
const NEEDS_YOU_NAME = /^approval_hold|needs_human|^merge_queue_attention$/;
const REVIEW_NAME =
  /^(review|adversarial_review|publication_output_scan|sandbox|egress|journey|certificate)/;

/**
 * The band an event name belongs to. A name this does not recognise is `other`
 * — the neutral band — for the same reason an unrecognised event is neutral on
 * the strip: a new event must never arrive already painted as money or as a
 * request for a person.
 */
export function overviewFamily(name: string): OverviewFamily {
  if (MONEY_NAME.test(name)) return 'money';
  if (NEEDS_YOU_NAME.test(name)) return 'needs-you';
  if (REVIEW_NAME.test(name)) return 'review';
  return 'other';
}

/** One band of one bar: how many events, in which colour. */
export type OverviewBand = { family: OverviewFamily; count: number };

/** One bucket of the histogram, placed on the track. */
export type OverviewBar = {
  /** The bucket's own ISO start, which is its identity across a refetch. */
  key: string;
  startMs: number;
  leftPct: number;
  widthPct: number;
  total: number;
  /** The bar's height as a share of the tallest bucket, 0–100. */
  heightPct: number;
  /** The bands with something in them, bottom to top. Empty for an empty bucket. */
  bands: OverviewBand[];
};

/**
 * The whole overview: a bar per bucket — including the empty ones, which keep
 * their place on the track and draw nothing — and the two totals the head reads.
 */
export type OverviewReading = {
  bars: OverviewBar[];
  /** The tallest bucket's count, which every height is a share of. */
  peak: number;
  total: number;
};

/**
 * The histogram, placed against the range the overview spans.
 *
 * Buckets are positioned by their OWN start instants rather than by their index,
 * so a response for a window that no longer matches the range — the clock moved
 * between the two reads, or a preset changed while the request was in flight —
 * draws where it belongs instead of being stretched to fit. A bucket whose start
 * will not parse is dropped: there is nowhere honest to put it.
 */
export function overviewBars(histogram: AuditHistogram, range: Span): OverviewReading {
  const bucketMs = Math.max(1, histogram.bucket_s * 1000);
  const span = Math.max(1, range.endMs - range.startMs);
  const widthPct = (bucketMs / span) * 100;
  const placed = histogram.buckets.flatMap((bucket) => {
    const startMs = parseInstant(bucket.start);
    if (startMs === null) return [];
    const bands = OVERVIEW_FAMILIES.map((family) => ({
      family,
      count: Object.entries(bucket.counts).reduce(
        (sum, [name, count]) => (overviewFamily(name) === family ? sum + count : sum),
        0
      )
    })).filter((band) => band.count > 0);
    const total = bands.reduce((sum, band) => sum + band.count, 0);
    return [{ key: bucket.start, startMs, total, bands }];
  });
  const peak = placed.reduce((max, bar) => Math.max(max, bar.total), 0);
  return {
    peak,
    total: placed.reduce((sum, bar) => sum + bar.total, 0),
    bars: placed.map((bar) => ({
      ...bar,
      leftPct: ((bar.startMs - range.startMs) / span) * 100,
      widthPct,
      heightPct: peak === 0 ? 0 : (bar.total / peak) * 100
    }))
  };
}
