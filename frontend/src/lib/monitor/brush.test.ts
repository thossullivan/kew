/**
 * #2248 — the brush maths, held to the millisecond.
 *
 * The brush is the only control on the Monitor that converts a gesture into a
 * fetch, so every rounding error in it becomes a window the operator did not
 * ask for. What is pinned here is the four properties that make it trustworthy:
 * exact at both edges, clamped at the range, centred under zoom, and never
 * finer than the overview can resolve.
 */
import { describe, expect, it } from 'vitest';
import {
  BUCKET_LADDER_S,
  MAX_BUCKETS,
  bucketSecondsFor,
  drawWindow,
  PAN_FRACTION,
  pan,
  panBy,
  pctAtTime,
  pxAtTime,
  resizeWindow,
  snapToBucket,
  timeAtPx,
  windowRect,
  zoom,
  ZOOM_FACTOR,
  type Span
} from './brush';
import { DAY_MS, HOUR_MS, MINUTE_MS } from './stream-query';

const END = Date.parse('2026-10-02T10:04:00.000Z');
const RANGE: Span = { startMs: END - DAY_MS, endMs: END };
const WIDTH = 720;
const BUCKET = 15 * MINUTE_MS;

describe('pixel to time, and back', () => {
  it('is exact at both edges', () => {
    expect(timeAtPx(RANGE, WIDTH, 0)).toBe(RANGE.startMs);
    expect(timeAtPx(RANGE, WIDTH, WIDTH)).toBe(RANGE.endMs);
    expect(pxAtTime(RANGE, WIDTH, RANGE.startMs)).toBe(0);
    expect(pxAtTime(RANGE, WIDTH, RANGE.endMs)).toBe(WIDTH);
  });

  it('is its own inverse in the middle', () => {
    for (const px of [1, 180, 359, 360, 541, 719]) {
      expect(pxAtTime(RANGE, WIDTH, timeAtPx(RANGE, WIDTH, px))).toBeCloseTo(px, 9);
    }
  });

  it('clamps a drag that left the track', () => {
    expect(timeAtPx(RANGE, WIDTH, -40)).toBe(RANGE.startMs);
    expect(timeAtPx(RANGE, WIDTH, WIDTH + 40)).toBe(RANGE.endMs);
    expect(pxAtTime(RANGE, WIDTH, RANGE.startMs - DAY_MS)).toBe(0);
    expect(pxAtTime(RANGE, WIDTH, RANGE.endMs + DAY_MS)).toBe(WIDTH);
  });

  it('answers with the start before the track has been measured', () => {
    expect(timeAtPx(RANGE, 0, 50)).toBe(RANGE.startMs);
    expect(pxAtTime(RANGE, 0, RANGE.endMs)).toBe(0);
    expect(Number.isNaN(timeAtPx(RANGE, 0, 50))).toBe(false);
  });

  it('draws the brush box as percentages of the track', () => {
    const half = { startMs: RANGE.startMs, endMs: RANGE.startMs + DAY_MS / 2 };
    expect(windowRect(RANGE, half)).toEqual({ leftPct: 0, widthPct: 50 });
    expect(windowRect(RANGE, RANGE)).toEqual({ leftPct: 0, widthPct: 100 });
    expect(pctAtTime(RANGE, RANGE.endMs)).toBe(100);
  });
});

describe('pan', () => {
  const window: Span = { startMs: END - 6 * HOUR_MS, endMs: END - 5 * HOUR_MS };

  it('moves the window and keeps its span', () => {
    const moved = pan(RANGE, window, -HOUR_MS);
    expect(moved).toEqual({ startMs: END - 7 * HOUR_MS, endMs: END - 6 * HOUR_MS });
  });

  it('clamps at the range, flush and the same width', () => {
    const atStart = pan(RANGE, window, -2 * DAY_MS);
    expect(atStart.startMs).toBe(RANGE.startMs);
    expect(atStart.endMs - atStart.startMs).toBe(HOUR_MS);
    const atEnd = pan(RANGE, window, 2 * DAY_MS);
    expect(atEnd.endMs).toBe(RANGE.endMs);
    expect(atEnd.endMs - atEnd.startMs).toBe(HOUR_MS);
  });

  it('moves 10% of the window per arrow key', () => {
    expect(PAN_FRACTION).toBe(0.1);
    const right = panBy(RANGE, window, PAN_FRACTION);
    expect(right.startMs - window.startMs).toBe(HOUR_MS / 10);
    const left = panBy(RANGE, window, -PAN_FRACTION);
    expect(window.startMs - left.startMs).toBe(HOUR_MS / 10);
  });

  it('a window as wide as the range has nowhere to go', () => {
    expect(pan(RANGE, RANGE, -DAY_MS)).toEqual(RANGE);
  });
});

describe('zoom', () => {
  const window: Span = { startMs: END - 6 * HOUR_MS, endMs: END - 4 * HOUR_MS };
  const centre = (span: Span) => (span.startMs + span.endMs) / 2;

  it('keeps the centre', () => {
    const inward = zoom(RANGE, window, ZOOM_FACTOR, BUCKET);
    expect(centre(inward)).toBe(centre(window));
    expect(inward.endMs - inward.startMs).toBe(2 * HOUR_MS * ZOOM_FACTOR);
    const outward = zoom(RANGE, inward, 1 / ZOOM_FACTOR, BUCKET);
    expect(centre(outward)).toBe(centre(window));
    expect(outward.endMs - outward.startMs).toBe(2 * HOUR_MS);
  });

  it('never widens past the range', () => {
    expect(zoom(RANGE, window, 100, BUCKET)).toEqual(RANGE);
  });

  it('snaps a zoom finer than one bucket to one bucket', () => {
    const narrow = zoom(RANGE, { startMs: END - BUCKET / 4, endMs: END }, ZOOM_FACTOR, BUCKET);
    expect(narrow.endMs - narrow.startMs).toBe(BUCKET);
    // And it stays inside the range: the snap slid it off the right edge back in.
    expect(narrow.endMs).toBeLessThanOrEqual(RANGE.endMs);
  });

  it('zooming in at the range edge stays inside the range', () => {
    const atEdge = zoom(RANGE, { startMs: RANGE.startMs, endMs: RANGE.startMs + HOUR_MS }, 0.1, BUCKET);
    expect(atEdge.startMs).toBeGreaterThanOrEqual(RANGE.startMs);
    expect(atEdge.endMs - atEdge.startMs).toBe(BUCKET);
  });
});

describe('snapToBucket', () => {
  it('leaves a window wider than one bucket alone', () => {
    const window = { startMs: END - 3 * BUCKET, endMs: END - BUCKET };
    expect(snapToBucket(RANGE, window, BUCKET)).toEqual(window);
  });

  it('widens a narrower one about its centre', () => {
    const window = { startMs: END - 12 * MINUTE_MS, endMs: END - 10 * MINUTE_MS };
    const snapped = snapToBucket(RANGE, window, BUCKET);
    expect(snapped.endMs - snapped.startMs).toBe(BUCKET);
    expect((snapped.startMs + snapped.endMs) / 2).toBe((window.startMs + window.endMs) / 2);
  });

  it('answers with the whole range when the range is narrower than a bucket', () => {
    const tiny = { startMs: END - MINUTE_MS, endMs: END };
    expect(snapToBucket(tiny, { startMs: END - 1, endMs: END }, BUCKET)).toEqual(tiny);
  });
});

describe('drawing and resizing', () => {
  it('draws the window between two pixels, in either drag direction', () => {
    const forward = drawWindow(RANGE, WIDTH, 180, 360, BUCKET);
    const backward = drawWindow(RANGE, WIDTH, 360, 180, BUCKET);
    expect(forward).toEqual(backward);
    expect(forward.startMs).toBe(RANGE.startMs + DAY_MS / 4);
    expect(forward.endMs).toBe(RANGE.startMs + DAY_MS / 2);
  });

  it('a click rather than a drag still draws one bucket', () => {
    const tap = drawWindow(RANGE, WIDTH, 360, 361, BUCKET);
    expect(tap.endMs - tap.startMs).toBe(BUCKET);
  });

  it('a drag across the whole track selects the whole range', () => {
    expect(drawWindow(RANGE, WIDTH, 0, WIDTH, BUCKET)).toEqual(RANGE);
  });

  it('resizes an edge and keeps the other fixed', () => {
    const window = { startMs: RANGE.startMs + DAY_MS / 4, endMs: RANGE.startMs + DAY_MS / 2 };
    const wider = resizeWindow(RANGE, WIDTH, window, 'start', 90, BUCKET);
    expect(wider.endMs).toBe(window.endMs);
    expect(wider.startMs).toBe(RANGE.startMs + DAY_MS / 8);
  });

  it('keeps a window when an edge is dragged past the other', () => {
    const window = { startMs: RANGE.startMs + DAY_MS / 4, endMs: RANGE.startMs + DAY_MS / 2 };
    const crossed = resizeWindow(RANGE, WIDTH, window, 'start', WIDTH - 1, BUCKET);
    expect(crossed.endMs).toBeGreaterThan(crossed.startMs);
    expect(crossed.startMs).toBeGreaterThanOrEqual(window.endMs - BUCKET);
  });
});

describe('bucketSecondsFor', () => {
  it('keeps every preset range to one small request', () => {
    const buckets = (spanMs: number) => spanMs / 1000 / bucketSecondsFor(spanMs);
    for (const span of [30 * MINUTE_MS, HOUR_MS, 6 * HOUR_MS, DAY_MS, 7 * DAY_MS]) {
      expect(buckets(span)).toBeLessThanOrEqual(500);
      expect(buckets(span)).toBeGreaterThanOrEqual(24);
    }
  });

  it('reads 24 hours in 15-minute buckets, as the artboard draws it', () => {
    expect(bucketSecondsFor(DAY_MS)).toBe(900);
  });

  it('is a round amount of clock time, always', () => {
    for (const span of [1, MINUTE_MS, DAY_MS, 7 * DAY_MS])
      expect(BUCKET_LADDER_S).toContain(bucketSecondsFor(span));
    // Past the ladder it is a whole number of days, which is round too.
    for (const span of [365 * DAY_MS, 10 * 365 * DAY_MS])
      expect(bucketSecondsFor(span) % 86_400, `${span / DAY_MS}d`).toBe(0);
  });

  /**
   * The range can be as wide as a link says it is — `from`/`to` come off the
   * URL, and a zoom-out under `Custom…` grows the range it is clamped by — so
   * the cap has to hold for every span and not just for the presets above.
   *
   * It did not. Falling through to the ladder's coarsest rung asked for one
   * bucket per day, so a range over `MAX_BUCKETS` days asked for more buckets
   * than the endpoint will serve: `/api/audit/histogram` answered 422, the read
   * was swallowed (the bars are not a reason to drop the list), and the overview
   * stayed blank for as long as the range did.
   */
  it('never asks for more buckets than the endpoint will serve', () => {
    const buckets = (spanMs: number) => Math.ceil(spanMs / 1000 / bucketSecondsFor(spanMs));
    for (let days = 1; days <= 3650; days += 1) {
      const span = days * DAY_MS;
      expect(buckets(span), `${days}d`).toBeLessThanOrEqual(MAX_BUCKETS);
      // And still a shape worth reading: one bar per day over a decade is not.
      expect(buckets(span), `${days}d`).toBeGreaterThanOrEqual(24);
    }
    expect(MAX_BUCKETS).toBe(500);
  });
});
