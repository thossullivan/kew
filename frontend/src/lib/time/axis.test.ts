/**
 * #2299 — the shared time axis. What is checked here is the arithmetic every
 * ruler in the console now runs on: which interval a zoom level picks, that
 * the grid lands on round LOCAL times, and that a label nobody could read is
 * dropped rather than drawn.
 *
 * The suite's zone is pinned to UTC (vite.config.ts); the one test that is
 * about a zone sets `TZ` itself and puts it back.
 */
import { describe, expect, it } from 'vitest';
import {
  COARSEST_INTERVAL,
  LABEL_CHAR_PX,
  LABEL_GAP_PX,
  MAX_AXIS_TICKS,
  NOW_LABEL,
  MIN_LABEL_SPACING_PX,
  MIN_MINOR_SPACING_PX,
  NICE_INTERVALS,
  axisIntervals,
  axisLabel,
  axisMsAt,
  axisTicks,
  axisX,
  dayLabel,
  nowLabelBox,
  type AxisWindow
} from './axis';

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const WIDTH = 1000;
const T = (iso: string): number => Date.parse(iso);

/** A window of `spanMs`, `WIDTH` wide, starting on a round hour. */
const windowOf = (spanMs: number, startMs = T('2026-09-28T08:00:00Z')): AxisWindow => ({
  startMs,
  endMs: startMs + spanMs,
  width: WIDTH
});

describe('the interval ladder', () => {
  it('is round clock time, ascending, and every rung divides some coarser one', () => {
    for (let i = 1; i < NICE_INTERVALS.length; i++)
      expect(NICE_INTERVALS[i]).toBeGreaterThan(NICE_INTERVALS[i - 1]);
    expect(NICE_INTERVALS[0]).toBe(SECOND);
    expect(COARSEST_INTERVAL).toBe(DAY);
    // Each rung divides the day evenly, so no grid walks off a day boundary.
    for (const step of NICE_INTERVALS) expect(DAY % step).toBe(0);
  });

  it('takes the first rung at or above the room a label needs, at every zoom', () => {
    const spans = [30 * SECOND, 5 * MINUTE, HOUR, 6 * HOUR, DAY, 7 * DAY];
    const majors = spans.map((span) => axisIntervals(windowOf(span)).majorMs);
    // Monotonic: a wider window never takes a finer step...
    for (let i = 1; i < majors.length; i++)
      expect(majors[i]).toBeGreaterThanOrEqual(majors[i - 1]);
    // ...it moves (a fixed axis would answer the same six times)...
    expect(new Set(majors).size).toBeGreaterThan(3);
    // ...and every answer is on the ladder, spaced for a label.
    for (const [i, major] of majors.entries()) {
      expect(NICE_INTERVALS).toContain(major);
      const spacing = (major / spans[i]) * WIDTH;
      expect(spacing).toBeGreaterThanOrEqual(MIN_LABEL_SPACING_PX);
    }
  });

  it('is the first rung AT or above the wanted spacing, not the next one up', () => {
    // 72px of a 1000px canvas is 7.2% of the span. A span of 1000 * HOUR / 72
    // therefore wants exactly an hour, and an hour is a rung: take it.
    expect(axisIntervals(windowOf((WIDTH * HOUR) / MIN_LABEL_SPACING_PX)).majorMs).toBe(HOUR);
  });

  it('stops at the coarsest rung rather than inventing one', () => {
    expect(axisIntervals(windowOf(200 * DAY)).majorMs).toBe(COARSEST_INTERVAL);
  });

  it('subdivides the major with the densest rung that fits it a whole number of times', () => {
    const { majorMs, minorMs } = axisIntervals(windowOf(6 * HOUR));
    expect(majorMs % minorMs).toBe(0);
    expect(minorMs).toBeLessThan(majorMs);
    const spacing = (minorMs / (6 * HOUR)) * WIDTH;
    expect(spacing).toBeGreaterThanOrEqual(MIN_MINOR_SPACING_PX);
    // Densest: the rung below it would be closer together than the minimum.
    const finer = NICE_INTERVALS.filter((step) => step < minorMs && majorMs % step === 0);
    for (const step of finer)
      expect((step / (6 * HOUR)) * WIDTH).toBeLessThan(MIN_MINOR_SPACING_PX);
  });

  it('makes every line a labelled one where no rung can subdivide the major', () => {
    // A 200-day window: a day is the major, and a minor that fitted inside it
    // would be a row of ticks 0.3px apart.
    const { majorMs, minorMs } = axisIntervals(windowOf(200 * DAY));
    expect(minorMs).toBe(majorMs);
  });
});

describe('the grid', () => {
  it('lands on round local times, inside the canvas, in order', () => {
    const ticks = axisTicks(windowOf(6 * HOUR));
    expect(ticks.length).toBeGreaterThan(1);
    const { minorMs } = axisIntervals(windowOf(6 * HOUR));
    for (const tick of ticks) {
      expect(tick.ms % minorMs).toBe(0);
      expect(tick.x).toBeGreaterThanOrEqual(-1);
      expect(tick.x).toBeLessThanOrEqual(WIDTH + 1);
      expect(tick.x).toBeCloseTo(axisX(windowOf(6 * HOUR), tick.ms), 6);
    }
    for (let i = 1; i < ticks.length; i++)
      expect(ticks[i].ms).toBeGreaterThan(ticks[i - 1].ms);
  });

  it('labels its majors and leaves its minors bare', () => {
    const ticks = axisTicks(windowOf(6 * HOUR));
    expect(ticks.some((tick) => !tick.major)).toBe(true);
    for (const tick of ticks.filter((t) => !t.major)) expect(tick.label).toBe('');
    expect(ticks.filter((t) => t.major && t.label !== '').length).toBeGreaterThan(1);
  });

  it('draws no minor lines for a ruler that labels everything it draws', () => {
    const ticks = axisTicks(windowOf(6 * HOUR), { minors: false });
    for (const tick of ticks) expect(tick.major).toBe(true);
  });

  it('dates every local midnight, and draws it once where a major lands there too', () => {
    const window = {
      startMs: T('2026-09-28T20:00:00Z'),
      endMs: T('2026-09-29T04:00:00Z'),
      width: WIDTH
    };
    const days = axisTicks(window).filter((tick) => tick.day);
    expect(days).toHaveLength(1);
    expect(days[0].ms).toBe(T('2026-09-29T00:00:00Z'));
    expect(days[0].label).toBe('2026-09-29');
    expect(days[0].major).toBe(true);
    // One line, not a date drawn on top of an hour.
    expect(axisTicks(window).filter((tick) => tick.ms === days[0].ms)).toHaveLength(1);
  });

  it('can be asked for no day lines at all', () => {
    const window = {
      startMs: T('2026-09-28T20:00:00Z'),
      endMs: T('2026-09-29T04:00:00Z'),
      width: WIDTH
    };
    expect(axisTicks(window, { dayLines: false }).some((tick) => tick.day)).toBe(false);
  });

  it('steps on the local clock in a zone offset by half an hour (#2271 review r7)', () => {
    // St John's is UTC-2:30 in October. Stepped from the UTC epoch, the grid
    // never hit local midnight and the date label lost to the hour beside it.
    const previous = process.env.TZ;
    process.env.TZ = 'America/St_Johns';
    try {
      const window = {
        startMs: T('2026-09-28T18:00:00Z'),
        endMs: T('2026-09-29T10:00:00Z'),
        width: WIDTH
      };
      const ticks = axisTicks(window, { minors: false });
      const days = ticks.filter((tick) => tick.day);
      expect(days).toHaveLength(1);
      expect(days[0].label).toBe('2026-09-29');
      // Every hour tick is on :00 local, which in St John's is :30 UTC.
      for (const tick of ticks.filter((t) => !t.day))
        expect(new Date(tick.ms).getMinutes()).toBe(0);
    } finally {
      if (previous === undefined) delete process.env.TZ;
      else process.env.TZ = previous;
    }
  });

  it('caps its lines however wide the window is', () => {
    // A decade, which no zoom should be able to turn into 3,650 lines.
    const ticks = axisTicks(windowOf(3650 * DAY));
    expect(ticks.length).toBeLessThanOrEqual(MAX_AXIS_TICKS);
    expect(ticks.length).toBeGreaterThan(1);
    const tighter = axisTicks(windowOf(3650 * DAY), { maxTicks: 12 });
    expect(tighter.length).toBeLessThanOrEqual(12);
  });

  it('has no lines to draw on a window of no width', () => {
    expect(axisTicks({ startMs: 0, endMs: 0, width: 0 }, { maxTicks: 0 })).toEqual([]);
  });
});

describe('a label', () => {
  it('reads the clock, with seconds only on a grid fine enough to need them', () => {
    const at = T('2026-09-28T08:00:30Z');
    expect(axisLabel(at, 30 * SECOND)).toBe('08:00:30');
    expect(axisLabel(at, MINUTE)).toBe('08:00');
    // Past a day's step the clock reads 00:00 on every tick: the date is the
    // only part of the reading that tells two of them apart.
    expect(axisLabel(T('2026-09-28T00:00:00Z'), 2 * DAY)).toBe('2026-09-28');
    expect(dayLabel(at)).toBe('2026-09-28');
  });

  it('is dropped at an edge it would run off, rather than clipped', () => {
    // A ruler whose last tick sits a few pixels from the right edge: the line
    // is drawn, the text that would hang off the canvas is not.
    const window = windowOf(HOUR);
    const ticks = axisTicks(window, { minors: false });
    const last = ticks[ticks.length - 1];
    expect(last.x).toBeCloseTo(WIDTH, 6);
    expect(last.label).toBe('');
    // Every label that IS drawn fits inside the canvas, text and all.
    for (const tick of ticks.filter((t) => t.label !== '')) {
      const room = LABEL_GAP_PX + tick.label.length * LABEL_CHAR_PX;
      expect(tick.x).toBeGreaterThanOrEqual(0);
      expect(tick.x + room).toBeLessThanOrEqual(WIDTH);
    }
  });

  it('measures the room by the label, not by a fixed margin', () => {
    // Two windows whose last labelled tick sits at the same x, 40px from the
    // right edge of a 500px canvas. A five-character clock reading fits
    // there...
    const clock = axisTicks(
      { startMs: T('2026-09-28T06:10:00Z'), endMs: T('2026-09-28T08:09:36Z'), width: 500 },
      { minors: false }
    );
    const last = clock[clock.length - 1];
    expect(last.x).toBeCloseTo(460, 0);
    expect(last.label).toBe('08:00');
    // ...and a ten-character date in the same place does not.
    const dated = axisTicks(
      { startMs: T('2026-09-27T01:00:00Z'), endMs: T('2026-09-28T02:00:00Z'), width: 500 },
      { minors: false }
    );
    const midnight = dated.filter((tick) => tick.day);
    expect(midnight).toHaveLength(1);
    expect(midnight[0].x).toBeCloseTo(460, 0);
    expect(midnight[0].label).toBe('');
  });

  it('drops a centred label at the left edge, where a label beside its tick fits', () => {
    const window = windowOf(HOUR);
    const beside = axisTicks(window, { minors: false, labelAnchor: 'start' });
    const centred = axisTicks(window, { minors: false, labelAnchor: 'center' });
    expect(beside[0].x).toBe(0);
    expect(beside[0].label).not.toBe('');
    expect(centred[0].label).toBe('');
    // Same lines either way: it is the text that is dropped, never the tick.
    expect(centred.map((tick) => tick.ms)).toEqual(beside.map((tick) => tick.ms));
  });

  it('leaves the room of an edge-dropped label to its neighbour', () => {
    // Dropping a label must not reserve the space it would have taken: the
    // tick before it is then free to keep its own.
    const window = windowOf(HOUR);
    const ticks = axisTicks(window, { minors: false });
    const last = ticks[ticks.length - 1];
    expect(last.label).toBe('');
    expect(ticks[ticks.length - 2].label).not.toBe('');
  });

  it('is thinned rather than overprinted once the step cannot grow', () => {
    // 200 days: a day is the widest step there is, and 200 dates do not fit.
    const window = windowOf(200 * DAY);
    const ticks = axisTicks(window);
    expect(ticks.length).toBeGreaterThan(100);
    const labelled = ticks.filter((tick) => tick.label !== '');
    expect(labelled.length).toBeLessThan(ticks.length);
    for (let i = 1; i < labelled.length; i++)
      expect(labelled[i].x - labelled[i - 1].x).toBeGreaterThanOrEqual(MIN_LABEL_SPACING_PX - 1);
  });

  it('gives the date the label when an hour tick wants the same space', () => {
    // A window where midnight and the hour grid collide: the ruler may lose an
    // hour, never the date (review r7).
    const window = {
      startMs: T('2026-09-27T12:00:00Z'),
      endMs: T('2026-09-30T12:00:00Z'),
      width: WIDTH
    };
    const ticks = axisTicks(window, { minors: false });
    for (const day of ticks.filter((tick) => tick.day)) expect(day.label).not.toBe('');
  });
});

describe('the pixel readings', () => {
  it('put the ends of the window at 0 and the width, and invert each other', () => {
    const window = windowOf(HOUR);
    expect(axisX(window, window.startMs)).toBe(0);
    expect(axisX(window, window.endMs)).toBeCloseTo(WIDTH, 6);
    expect(axisMsAt(window, 250)).toBe(window.startMs + 15 * MINUTE);
    expect(axisX(window, axisMsAt(window, 317))).toBeCloseTo(317, 6);
  });

  it('never divide by a zero span', () => {
    const flat = { startMs: 5, endMs: 5, width: 0 };
    expect(Number.isFinite(axisX(flat, 5))).toBe(true);
    expect(Number.isFinite(axisMsAt(flat, 10))).toBe(true);
  });
});

describe('the now label', () => {
  /** The room the word "now" takes, measured the way the rulers draw it. */
  const NOW_ROOM = LABEL_GAP_PX + NOW_LABEL.length * LABEL_CHAR_PX;

  it('takes a tick\'s reading with it when the two boxes overlap', () => {
    // A five-minute grid over an hour: 08:20 is labelled, and a now marker
    // standing on it leaves no room for the reading.
    const window = windowOf(HOUR);
    const at = T('2026-09-28T08:21:00Z');
    const plain = axisTicks(window, { minors: false });
    const labelled = plain.find((tick) => tick.ms === T('2026-09-28T08:20:00Z'));
    expect(labelled?.label).toBe('08:20');

    const ticks = axisTicks(window, { minors: false, nowMs: at });
    const under = ticks.find((tick) => tick.ms === T('2026-09-28T08:20:00Z'));
    // The line is a fact, the label a courtesy: the tick survives, its
    // reading does not.
    expect(under?.major).toBe(true);
    expect(under?.label).toBe('');
    // Its neighbours, a whole label away, keep theirs.
    expect(ticks.find((tick) => tick.ms === T('2026-09-28T08:15:00Z'))?.label).toBe('08:15');
    expect(ticks.find((tick) => tick.ms === T('2026-09-28T08:25:00Z'))?.label).toBe('08:25');
    // Nothing else moved: the same lines, and the same readings otherwise.
    expect(ticks.map((tick) => tick.ms)).toEqual(plain.map((tick) => tick.ms));
  });

  it('is the right-hand edge for the last tick, which is #2385 itself', () => {
    // The fixture's picture: now at the end of the window, where its word is
    // pulled back inside the canvas and lands on a 10:00 that does fit. It
    // read "10:00now".
    const endMs = T('2026-09-28T10:02:24Z');
    const window = { startMs: endMs - HOUR, endMs, width: WIDTH };
    const tenAm = T('2026-09-28T10:00:00Z');
    const before = axisTicks(window, { minors: false }).find((tick) => tick.ms === tenAm);
    expect(before?.x).toBeCloseTo(960, 6);
    expect(before?.label).toBe('10:00');

    const box = nowLabelBox(window, { nowMs: endMs });
    expect(box).toEqual({ from: WIDTH - NOW_ROOM, to: WIDTH });
    const after = axisTicks(window, { minors: false, nowMs: endMs }).find(
      (tick) => tick.ms === tenAm
    );
    expect(after?.label).toBe('');
  });

  it('keeps every reading it is nowhere near', () => {
    const window = windowOf(HOUR);
    const at = T('2026-09-28T08:20:00Z');
    const kept = (nowMs?: number): string[] =>
      axisTicks(window, { minors: false, nowMs }).map((tick) => tick.label);
    // Before the window, after it, and not asked for at all: all the same
    // ruler. (`axisX` is unbounded, so an instant off the canvas has an x —
    // it is the box that has to be absent, not merely off to one side.)
    expect(nowLabelBox(window, { nowMs: window.startMs - HOUR })).toBeNull();
    expect(nowLabelBox(window, { nowMs: window.endMs + HOUR })).toBeNull();
    expect(nowLabelBox(window, {})).toBeNull();
    expect(kept(window.startMs - HOUR)).toEqual(kept());
    expect(kept(window.endMs + HOUR)).toEqual(kept());
    // ...and the one that IS in the window is the only one that costs a
    // label, so the test above is measuring the rule and not the window.
    expect(kept(at)).not.toEqual(kept());
  });

  it('measures the word it is given, and a centred ruler from its middle', () => {
    const window = windowOf(HOUR);
    const at = T('2026-09-28T08:20:00Z');
    const x = axisX(window, at);
    expect(nowLabelBox(window, { nowMs: at })).toEqual({ from: x, to: x + NOW_ROOM });
    // A longer word takes more room; a centred ruler hangs it off both sides.
    const long = LABEL_GAP_PX + 'right now'.length * LABEL_CHAR_PX;
    expect(nowLabelBox(window, { nowMs: at, nowLabel: 'right now' })).toEqual({
      from: x,
      to: x + long
    });
    expect(nowLabelBox(window, { nowMs: at, labelAnchor: 'center' })).toEqual({
      from: x - NOW_ROOM / 2,
      to: x + NOW_ROOM / 2
    });
  });

  it('yields to a dated reading rather than taking it (#2385 review)', () => {
    // A live epic opened in the first hour after local midnight: a fourteen-
    // hour window puts the marker five pixels off the day line, inside the
    // date's own box. The word used to win and the ruler was left carrying no
    // date at all across the day boundary — #2271 round 7's failure, bought
    // with three letters. Now the date keeps its reading and the word is not
    // drawn.
    const midnight = T('2026-09-29T00:00:00Z');
    const at = midnight + 4 * MINUTE;
    const window = {
      startMs: midnight - 12 * HOUR,
      endMs: midnight + 108 * MINUTE,
      width: WIDTH
    };
    const dated = (nowMs?: number): string | undefined =>
      axisTicks(window, { minors: false, nowMs }).find((tick) => tick.ms === midnight)?.label;
    expect(dated()).toBe('2026-09-29');
    // The word WANTS those pixels: it lands inside the date's box.
    const date = { from: axisX(window, midnight), to: 0 };
    date.to = date.from + LABEL_GAP_PX + '2026-09-29'.length * LABEL_CHAR_PX;
    const wanted = axisX(window, at);
    expect(wanted).toBeGreaterThan(date.from);
    expect(wanted).toBeLessThan(date.to);
    // It does not get them, and it is not drawn anywhere else either: the
    // surface asks this axis where the word goes, and the answer is nowhere.
    expect(dated(at)).toBe('2026-09-29');
    expect(nowLabelBox(window, { minors: false, nowMs: at })).toBeNull();
    // And a word that is not drawn costs no OTHER reading: the ruler is the
    // one it would have been without a marker at all.
    expect(axisTicks(window, { minors: false, nowMs: at }).map((tick) => tick.label)).toEqual(
      axisTicks(window, { minors: false }).map((tick) => tick.label)
    );
  });

  it('still takes an hour reading when the date it would cost is elsewhere', () => {
    // The same window, with the marker a long way short of midnight: the date
    // is nowhere near the word, so the rule above does not quietly disarm the
    // #2385 one — the hour under the word still loses its reading.
    const midnight = T('2026-09-29T00:00:00Z');
    const window = {
      startMs: midnight - 12 * HOUR,
      endMs: midnight + 108 * MINUTE,
      width: WIDTH
    };
    const hour = T('2026-09-28T18:00:00Z');
    const labelOf = (nowMs?: number): string | undefined =>
      axisTicks(window, { minors: false, nowMs }).find((tick) => tick.ms === hour)?.label;
    expect(labelOf()).toBe('18:00');
    expect(nowLabelBox(window, { minors: false, nowMs: hour })).not.toBeNull();
    expect(labelOf(hour)).toBe('');
    // The date is untouched by that: it was never in the way.
    expect(
      axisTicks(window, { minors: false, nowMs: hour }).find(
        (tick) => tick.ms === T('2026-09-29T00:00:00Z')
      )?.label
    ).toBe('2026-09-29');
  });

  it('is not consulted by a date that the edge rule drops anyway', () => {
    // A date half off the right-hand edge is dropped by the edge rule, so it
    // claims nothing — and the word, which fits, is still drawn. (The date
    // yielding to the word and the word yielding to the date would otherwise
    // both be reachable in one window.)
    const at = T('2026-09-29T00:02:00Z');
    const window = { startMs: at - 12 * HOUR, endMs: at, width: WIDTH };
    const midnight = T('2026-09-29T00:00:00Z');
    const dated = axisTicks(window, { minors: false }).find((tick) => tick.ms === midnight);
    expect(dated?.day).toBe(true);
    expect(dated?.label).toBe('');
    expect(nowLabelBox(window, { minors: false, nowMs: at })).toEqual({
      from: WIDTH - NOW_ROOM,
      to: WIDTH
    });
  });
});
