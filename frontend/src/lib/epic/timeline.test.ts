/**
 * The epic run timeline's model (#2271) — the Gantt's arithmetic, with no DOM
 * anywhere near it.
 *
 * Every claim here is one the renderer cannot make for itself: that zooming
 * keeps the instant under the pointer still, that a fit really covers the
 * span the API reported, that the ruler's step follows the scale, that an
 * unfinished band is drawn to NOW rather than to nothing, and that the rows
 * come out in the order the API sent them — which is first-activity order,
 * pinned here against the committed fixture so a model that quietly re-sorted
 * would be caught.
 */
import { describe, expect, it } from 'vitest';
import type { EpicTimeline, EpicTimelineNode, RunAuditEvent } from '$lib/api';
import timelineJson from '../../../fixtures/factory/epics-launch-fixes-timeline.json';
import { focusedRunEvent } from '$lib/runs/events';
import { LABEL_CHAR_PX, LABEL_GAP_PX } from '$lib/time/axis';
import {
  BAND_EDGE,
  BAND_FILL,
  BAR_COLOR,
  BROKEN_STROKE,
  CANVAS_VIEWS,
  CANVAS_VIEW_LABEL,
  MARK_LEGEND,
  MIN_TICK_PX,
  MIN_WINDOW_MS,
  ROW_H,
  TICK_STEPS_MS,
  VERDICT_COLOR,
  bandBoxes,
  bandKind,
  barBoxes,
  barHref,
  barLabel,
  barRole,
  barTooltip,
  canvasView,
  chartHeight,
  firstActivityMs,
  fitScale,
  fitTimeline,
  mergeMark,
  msAt,
  nowLabelX,
  nowOf,
  otherView,
  panBy,
  roundMarks,
  roundTooltip,
  rowStep,
  rows,
  spanOf,
  tickStep,
  ticks,
  togglesView,
  verdictTone,
  windowMs,
  xOf,
  zoomAt
} from './timeline';

const fixture: EpicTimeline = timelineJson;

const WIDTH = 900;

/** A node with nothing on it, so each test states only what it is about. */
function node(over: Partial<EpicTimelineNode> = {}): EpicTimelineNode {
  return { id: '1', title: 'a node', bands: [], runs: [], rounds: [], merged_at: null, ...over };
}

function run(over: Partial<EpicTimelineNode['runs'][number]> = {}) {
  return {
    run_id: 'r1',
    kind: 'dispatch',
    status: 'success',
    started_at: '2026-09-28T08:00:00+00:00',
    finished_at: '2026-09-28T09:00:00+00:00',
    cost_usd: 1.5,
    attempt_number: 1,
    ...over
  };
}

const T = (iso: string): number => Date.parse(iso);

/** An hour-wide scale over a day the fixture does not use. */
const hour = fitScale({ startMs: T('2026-01-02T00:00:00Z'), endMs: T('2026-01-02T01:00:00Z') }, WIDTH);

describe('the time axis', () => {
  it('maps an instant to a pixel and back', () => {
    const mid = (hour.startMs + hour.endMs) / 2;
    expect(msAt(hour, xOf(hour, mid))).toBeCloseTo(mid, 6);
    expect(xOf(hour, hour.startMs)).toBeCloseTo(0, 6);
    expect(xOf(hour, hour.endMs)).toBeCloseTo(WIDTH, 6);
  });

  it('survives a zero-width canvas instead of dividing by it', () => {
    const flat = fitScale({ startMs: 0, endMs: 1000 }, 0);
    expect(Number.isFinite(xOf(flat, 500))).toBe(true);
    expect(Number.isFinite(msAt(flat, 10))).toBe(true);
  });
});

describe('fit', () => {
  it('covers the whole span the API reported', () => {
    const scale = fitScale(spanOf(fixture), WIDTH);
    const span = spanOf(fixture);
    expect(xOf(scale, span.startMs)).toBeGreaterThanOrEqual(0);
    expect(xOf(scale, span.endMs)).toBeLessThanOrEqual(WIDTH);
    // And it is not a fit of something much bigger: the span fills most of it.
    expect(span.endMs - span.startMs).toBeGreaterThan(windowMs(scale) * 0.8);
  });

  it('gives an epic with no activity at all a window rather than a point', () => {
    const empty: EpicTimeline = {
      epic_ref: 'e',
      now: '2026-01-02T00:00:00+00:00',
      span: { start: null, end: '2026-01-02T00:00:00+00:00' },
      nodes: []
    };
    expect(windowMs(fitScale(spanOf(empty), WIDTH))).toBeGreaterThanOrEqual(MIN_WINDOW_MS);
  });

  it('carries the window past activity stamped after the server`s own clock', () => {
    // Two hosts, two clocks: `span.end` is the serve host's `now` and the
    // activity inside it was stamped by the driver's, so the start CAN land
    // after the end. Collapsing to the hour before `now` then draws every
    // band and bar off the right edge of a canvas that clips its overflow,
    // and `F` cannot get them back.
    const skewed: EpicTimeline = {
      epic_ref: 'e',
      now: '2026-01-02T00:00:00+00:00',
      span: { start: '2026-01-02T00:02:00+00:00', end: '2026-01-02T00:00:00+00:00' },
      nodes: []
    };
    const span = spanOf(skewed);
    const first = T('2026-01-02T00:02:00Z');
    expect(span.endMs).toBeGreaterThan(first);
    // And `now` is still in the picture, which is the reading that explains it.
    expect(span.startMs).toBeLessThanOrEqual(T('2026-01-02T00:00:00Z'));
    const scale = fitScale(span, WIDTH);
    expect(xOf(scale, first)).toBeGreaterThanOrEqual(0);
    expect(xOf(scale, first)).toBeLessThanOrEqual(WIDTH);
  });

  /** A finished epic, read seventeen days after its last run. */
  const finished = (over: Partial<EpicTimelineNode> = {}): EpicTimeline => ({
    epic_ref: 'e',
    now: '2026-10-15T08:00:00+00:00',
    span: { start: '2026-09-28T08:00:00+00:00', end: '2026-10-15T08:00:00+00:00' },
    nodes: [
      node({
        bands: [{ status: 'done', from: '2026-09-28T09:02:00+00:00', to: null }],
        runs: [run()],
        merged_at: '2026-09-28T09:01:00+00:00',
        ...over
      })
    ]
  });

  it('fits a complete epic to its own span, not out to now', () => {
    expect(spanOf(finished()).endMs).toBe(T('2026-09-28T09:02:00Z'));
    // The desktop lane, and the phone's 170px one where the open-epic pad
    // alone would leave over a third of the chart empty on the right.
    for (const width of [WIDTH, 170]) {
      const scale = fitTimeline(finished(), width);
      const [bar] = barBoxes(finished().nodes[0], scale, nowOf(finished()));
      expect(bar.x).toBeGreaterThanOrEqual(0);
      expect(bar.x).toBeLessThan(width * 0.15);
      expect(bar.x + bar.width).toBeGreaterThan(width * 0.85);
      expect(bar.x + bar.width).toBeLessThanOrEqual(width);
    }
  });

  it('keeps the full right pad for an epic still running, where the now cap sits', () => {
    const open = finished({ runs: [run({ finished_at: null })] });
    expect(fitTimeline(open, 170)).toEqual(fitScale(spanOf(open), 170));
  });

  it('keeps now as the end while any node or run is still open', () => {
    const now = T('2026-10-15T08:00:00Z');
    const working = [{ status: 'in_progress', from: '2026-09-28T08:00:00+00:00', to: null }];
    expect(spanOf(finished({ bands: working })).endMs).toBe(now);
    expect(spanOf(finished({ runs: [run({ finished_at: null })] })).endMs).toBe(now);
  });
});

describe('zoom', () => {
  it('keeps the instant under the pointer under the pointer', () => {
    for (const x of [0, 137, WIDTH / 2, WIDTH]) {
      const at = msAt(hour, x);
      for (const factor of [1.2, 1 / 1.2, 4, 0.25]) {
        expect(msAt(zoomAt(hour, factor, x), x)).toBeCloseTo(at, 3);
      }
    }
  });

  it('narrows the window when it zooms in and widens it when it zooms out', () => {
    expect(windowMs(zoomAt(hour, 2, WIDTH / 2))).toBeCloseTo(windowMs(hour) / 2, 3);
    expect(windowMs(zoomAt(hour, 0.5, WIDTH / 2))).toBeCloseTo(windowMs(hour) * 2, 3);
  });

  it('never moves the picture the opposite way to the gesture', () => {
    // A fit is unclamped (it must cover the span), so a fitted window can sit
    // outside the zoom range: 40 seconds is under the five-minute floor and
    // 400 days is over the one-year ceiling. A clamp there must hold the
    // window still, never swap a zoom-in for a jump out and back.
    for (const span of [
      { startMs: 0, endMs: 40_000 },
      { startMs: 0, endMs: 400 * 86_400_000 }
    ]) {
      const fitted = fitScale(span, WIDTH);
      const was = windowMs(fitted);
      // Within an ulp, not to the bit: the scale is kept in floats so the
      // window a zoom holds still comes back a rounding apart (the same
      // reason the five-minute clamp below is checked to the millisecond).
      const eps = was * 1e-9;
      expect(windowMs(zoomAt(fitted, 1.2, WIDTH / 2))).toBeLessThanOrEqual(was + eps);
      expect(windowMs(zoomAt(fitted, 1 / 1.2, WIDTH / 2))).toBeGreaterThanOrEqual(was - eps);
    }
  });

  it('stops zooming in at five minutes', () => {
    let scale = hour;
    for (let i = 0; i < 40; i++) scale = zoomAt(scale, 2, WIDTH / 2);
    // Closed to the millisecond, not to the bit: the scale is kept in floats
    // so zooming can hold an instant exactly under the pointer, and a clamp
    // in floats lands within an ulp of the limit rather than on it.
    expect(windowMs(scale)).toBeCloseTo(MIN_WINDOW_MS, 0);
  });
});

describe('pan', () => {
  it('drags the picture with the pointer', () => {
    const at = msAt(hour, 100);
    const moved = panBy(hour, 40);
    // Dragging right moves the content right: the instant that was at 100 is
    // now at 140.
    expect(msAt(moved, 140)).toBeCloseTo(at, 3);
    expect(windowMs(moved)).toBeCloseTo(windowMs(hour), 3);
  });
});

describe('the ruler', () => {
  it('changes its step with the scale', () => {
    const steps = [1, 6, 60, 600, 6000].map((minutes) =>
      tickStep(
        fitScale({ startMs: 0, endMs: minutes * 60_000 }, WIDTH)
      )
    );
    // Monotonic: a wider window never takes a finer step.
    for (let i = 1; i < steps.length; i++) expect(steps[i]).toBeGreaterThanOrEqual(steps[i - 1]);
    // And it actually moves — a fixed axis would answer the same five times.
    expect(new Set(steps).size).toBeGreaterThan(1);
    for (const s of steps) expect(TICK_STEPS_MS).toContain(s);
  });

  it('never packs its ticks closer than a label', () => {
    for (const minutes of [10, 90, 600, 20_000]) {
      const scale = fitScale({ startMs: 0, endMs: minutes * 60_000 }, WIDTH);
      const step = tickStep(scale);
      const spacing = (step / windowMs(scale)) * WIDTH;
      // The top of the ladder is a day; past that the step cannot grow, and
      // the labels thin out instead (checked below).
      if (step !== TICK_STEPS_MS[TICK_STEPS_MS.length - 1]) {
        expect(spacing).toBeGreaterThanOrEqual(MIN_TICK_PX);
      }
    }
  });

  it('is inside the canvas, in order, and spaced', () => {
    const list = ticks(hour);
    expect(list.length).toBeGreaterThan(1);
    for (const t of list) {
      expect(t.x).toBeGreaterThanOrEqual(-1);
      expect(t.x).toBeLessThanOrEqual(WIDTH + 1);
    }
    for (let i = 1; i < list.length; i++) expect(list[i].ms).toBeGreaterThan(list[i - 1].ms);
  });

  it('marks a day boundary and dates it', () => {
    const scale = fitScale(
      { startMs: T('2026-09-28T20:00:00Z'), endMs: T('2026-09-29T04:00:00Z') },
      WIDTH
    );
    const day = ticks(scale).filter((t) => t.day);
    expect(day).toHaveLength(1);
    // TZ is pinned to UTC for this suite (vite.config.ts), so local midnight
    // is the UTC one.
    expect(day[0].ms).toBe(T('2026-09-29T00:00:00Z'));
    expect(day[0].label).toBe('2026-09-29');
  });

  it('keeps the date in a zone whose offset is not a multiple of the step (review r7)', () => {
    // St John's is UTC-2:30 in October. Stepped from the UTC epoch, the grid
    // never hit local midnight and the date label lost to the hour beside it.
    const prev = process.env.TZ;
    process.env.TZ = 'America/St_Johns';
    try {
      const scale = fitScale(
        { startMs: T('2026-09-28T18:00:00Z'), endMs: T('2026-09-29T10:00:00Z') },
        WIDTH
      );
      const list = ticks(scale);
      const day = list.filter((t) => t.day);
      expect(day).toHaveLength(1);
      expect(day[0].label).toBe('2026-09-29');
      // The hour grid sits on the local clock: every hour tick lands on :00
      // local, which in St John's is :30 UTC.
      for (const t of list.filter((x) => !x.day)) expect(new Date(t.ms).getMinutes()).toBe(0);
    } finally {
      if (prev === undefined) delete process.env.TZ;
      else process.env.TZ = prev;
    }
  });

  it('drops labels rather than overprinting them when the step cannot grow', () => {
    // 200 days: a day step is the widest there is, and 200 dates do not fit.
    const scale = fitScale({ startMs: 0, endMs: 200 * 86_400_000 }, WIDTH);
    const list = ticks(scale);
    expect(list.length).toBeGreaterThan(100);
    const labelled = list.filter((t) => t.label !== '');
    expect(labelled.length).toBeLessThan(list.length);
    for (let i = 1; i < labelled.length; i++) {
      expect(labelled[i].x - labelled[i - 1].x).toBeGreaterThanOrEqual(MIN_TICK_PX - 1);
    }
  });

  it('reads on the gallery view exactly as it did before the shared axis (#2299)', () => {
    // The committed fixture at about the gallery's plot width, pinned so the
    // move onto `$lib/time/axis` cannot change the picture an operator signed
    // off: two-hour steps on the local clock, one dated line at midnight, and
    // every label kept — including the last one, which fits in the 52 pixels
    // the fit leaves to the right of it (a date in that space would not, and
    // would be dropped rather than clipped).
    const scale = fitScale(spanOf(fixture), 1220);
    expect(tickStep(scale)).toBe(2 * 3_600_000);
    expect(ticks(scale).map((tick) => tick.label)).toEqual([
      '08:00',
      '10:00',
      '12:00',
      '14:00',
      '16:00',
      '18:00',
      '20:00',
      '22:00',
      '2026-09-29',
      '02:00',
      '04:00',
      '06:00',
      '08:00',
      '10:00',
      '12:00',
      '14:00',
      '16:00'
    ]);
    expect(ticks(scale).filter((tick) => tick.day).map((tick) => tick.ms)).toEqual([
      Date.parse('2026-09-29T00:00:00Z')
    ]);
  });

  it('gives the now cap its own pixels rather than printing a reading under it (#2385)', () => {
    // The same fixture and width as the gallery reading above, now told where
    // the cap is: the fit leaves 64px to the right of `now`, and the next
    // two-hour tick lands inside the word. It read "16:00" over "now".
    const scale = fitScale(spanOf(fixture), 1220);
    const at = nowOf(fixture);
    const textX = nowLabelX(scale, at);
    expect(textX).not.toBeNull();
    // Beside the cap's line, like every other label on this ruler.
    expect(textX).toBeCloseTo(xOf(scale, at) + LABEL_GAP_PX, 6);

    const before = ticks(scale);
    const after = ticks(scale, at);
    expect(after.map((tick) => tick.ms)).toEqual(before.map((tick) => tick.ms));
    // One reading goes, and it is the one the word was sitting on.
    const lost = before.filter((tick, i) => tick.label !== after[i].label);
    expect(lost.map((tick) => tick.label)).toEqual(['16:00']);

    // And nothing that stayed shares a pixel with the word.
    const from = (textX ?? 0) - LABEL_GAP_PX;
    const to = from + LABEL_GAP_PX + 'now'.length * LABEL_CHAR_PX;
    for (const tick of after.filter((t) => t.label !== '')) {
      const end = tick.x + LABEL_GAP_PX + tick.label.length * LABEL_CHAR_PX;
      expect(tick.x >= to || end <= from).toBe(true);
    }
  });

  it('keeps the date across a day boundary and drops the cap instead (#2385 review)', () => {
    // A live epic opened four minutes after local midnight, fitted: the fit
    // puts `now` 64px off the right edge and midnight six pixels short of it,
    // inside the date's own 65px box. This window holds ONE midnight, so the
    // date losing its reading to the word left the ruler carrying no date at
    // all across the day boundary — #2271 round 7's failure. The word goes;
    // the cap's own full-height line is drawn either way.
    const midnight = T('2026-09-29T00:00:00Z');
    const at = midnight + 4 * 60_000;
    const scale = fitScale({ startMs: T('2026-09-28T12:00:00Z'), endMs: at }, 1220);
    const dated = ticks(scale).filter((tick) => tick.day);
    expect(dated.map((tick) => tick.label)).toEqual(['2026-09-29']);
    // The word wants the date's pixels: it starts inside that box.
    const room = LABEL_GAP_PX + '2026-09-29'.length * LABEL_CHAR_PX;
    expect(xOf(scale, at)).toBeGreaterThan(dated[0].x);
    expect(xOf(scale, at)).toBeLessThan(dated[0].x + room);
    // It does not get them, and it is not drawn: nothing on this ruler moved.
    expect(nowLabelX(scale, at)).toBeNull();
    expect(ticks(scale, at).map((tick) => tick.label)).toEqual(
      ticks(scale).map((tick) => tick.label)
    );
  });

  it('has no cap to clear once the reader has panned now off the canvas', () => {
    const scale = fitScale(spanOf(fixture), 1220);
    const at = nowOf(fixture);
    // A pan that takes the cap off the right-hand edge, and one that takes it
    // off the left: no word is drawn, so no reading is dropped for it.
    for (const away of [panBy(scale, 400), panBy(scale, -1600)]) {
      expect(nowLabelX(away, at)).toBeNull();
      expect(ticks(away, at).map((tick) => tick.label)).toEqual(
        ticks(away).map((tick) => tick.label)
      );
    }
  });
});

describe('the status band', () => {
  it('is cyan while kew is working and magenta when a person is next', () => {
    for (const s of ['in_progress', 'in_review', 'adversarial_review', 'remediating'])
      expect(bandKind(s)).toBe('flight');
    for (const s of ['held', 'awaiting_publication', 'awaiting_continuation', 'awaiting_no_change'])
      expect(bandKind(s)).toBe('attention');
    // The quiet states draw nothing: a band is a claim that the clock is
    // running on somebody.
    for (const s of ['queued', 'ready', 'todo', 'done', 'blocked', 'failed', 'cancelled', ''])
      expect(bandKind(s)).toBeNull();
    expect(BAND_FILL.flight).toBe('var(--accent-2-wash)');
    expect(BAND_FILL.attention).toBe('var(--accent-wash)');
  });

  it('ends an open band at now, not at the epoch', () => {
    const nowMs = T('2026-09-28T10:00:00Z');
    const [band] = bandBoxes(
      node({ bands: [{ status: 'in_progress', from: '2026-09-28T08:00:00+00:00', to: null }] }),
      hourOf(T('2026-09-28T08:00:00Z'), nowMs),
      nowMs
    );
    expect(band.toMs).toBe(nowMs);
    expect(band.width).toBeGreaterThan(0);
  });

  it('skips a band whose start the server could not state', () => {
    expect(
      bandBoxes(
        node({ bands: [{ status: 'in_progress', from: 'not a time', to: null }] }),
        hour,
        hour.endMs
      )
    ).toEqual([]);
  });
});

describe('the run bars', () => {
  it('colours by what the run was', () => {
    expect(barRole('dispatch')).toBe('dispatch');
    expect(barRole('adversarial_review')).toBe('review');
    expect(barRole('review_remediation')).toBe('remediation');
    for (const k of ['ci_fix', 'resolve', 'loop', 'planning', 'a kind from a newer host'])
      expect(barRole(k)).toBe('other');
    expect(BAR_COLOR.dispatch).toBe('var(--text-2)');
    expect(BAR_COLOR.review).toBe('var(--accent-2)');
    expect(BAR_COLOR.remediation).toBe('var(--text-1)');
    expect(BAR_COLOR.other).toBe('var(--text-3)');
  });

  it('never draws a run in the violet DESIGN.md keeps for "blocked" (#2843 P16)', () => {
    const drawn = [...Object.values(BAR_COLOR), ...MARK_LEGEND.map((e) => e.color)];
    expect(drawn).not.toContain('var(--status-blocked)');
  });

  it('keys every colour the lanes paint beyond a bar fill (#2843 P16)', () => {
    const keyed = MARK_LEGEND.flatMap((e) => [e.color, e.edge]);
    for (const colour of [
      BROKEN_STROKE,
      ...Object.values(BAND_FILL),
      ...Object.values(BAND_EDGE).filter((c) => c !== null),
      ...Object.values(VERDICT_COLOR)
    ])
      expect(keyed).toContain(colour);
  });

  it('runs an unfinished bar out to now and says it is open', () => {
    const nowMs = T('2026-09-28T09:30:00Z');
    const scale = hourOf(T('2026-09-28T08:00:00Z'), nowMs);
    const [bar] = barBoxes(node({ runs: [run({ finished_at: null })] }), scale, nowMs);
    expect(bar.open).toBe(true);
    expect(bar.endMs).toBe(nowMs);
  });

  it('outlines a failed or killed run in danger', () => {
    for (const status of ['failure', 'failed', 'killed']) {
      const [bar] = barBoxes(node({ runs: [run({ status })] }), hour, hour.endMs);
      expect(bar.broken).toBe(true);
    }
    expect(barBoxes(node({ runs: [run()] }), hour, hour.endMs)[0].broken).toBe(false);
  });

  it('draws a refused or zero-length run as a tick, not as a bar', () => {
    const scale = hourOf(T('2026-09-28T08:00:00Z'), T('2026-09-28T09:00:00Z'));
    const refused = barBoxes(node({ runs: [run({ status: 'refused' })] }), scale, scale.endMs)[0];
    expect(refused.tick).toBe(true);
    const zero = barBoxes(
      node({ runs: [run({ finished_at: '2026-09-28T08:00:00+00:00' })] }),
      scale,
      scale.endMs
    )[0];
    expect(zero.tick).toBe(true);
    expect(zero.width).toBe(2);
    expect(barBoxes(node({ runs: [run()] }), scale, scale.endMs)[0].tick).toBe(false);
  });

  it('skips a run that never started', () => {
    expect(barBoxes(node({ runs: [run({ started_at: null })] }), hour, hour.endMs)).toEqual([]);
  });

  it('tells the reader kind, status, span, duration and cost', () => {
    const scale = hourOf(T('2026-09-28T08:00:00Z'), T('2026-09-28T09:00:00Z'));
    const [bar] = barBoxes(node({ runs: [run()] }), scale, scale.endMs);
    const tip = barTooltip(bar);
    expect(tip[0]).toBe('dispatch · success');
    expect(tip[1]).toContain('2026-09-28 08:00:00');
    expect(tip[2]).toBe('1h00m · $1.50');
  });
});

describe('the verdict marks', () => {
  it('is green for a ready round and amber for anything else', () => {
    expect(verdictTone('READY')).toBe('success');
    expect(verdictTone('READY_WITH_RESIDUALS')).toBe('success');
    for (const v of ['NEEDS_CHANGES', 'REJECTED', '', 'something new'])
      expect(verdictTone(v)).toBe('warn');
  });

  it('sits at recorded_at and names its round', () => {
    const scale = hourOf(T('2026-09-28T08:00:00Z'), T('2026-09-28T09:00:00Z'));
    const [mark] = roundMarks(
      node({ rounds: [{ round: 6, verdict: 'NEEDS_CHANGES', recorded_at: '2026-09-28T08:30:00+00:00' }] }),
      scale
    );
    expect(mark.ms).toBe(T('2026-09-28T08:30:00Z'));
    expect(mark.x).toBeCloseTo(xOf(scale, mark.ms), 6);
    expect(roundTooltip(mark)[0]).toBe('round 6 · NEEDS_CHANGES');
  });

  it('keys every round on a lane apart, including a retry`s second round 1', () => {
    // `record_review_round` restarts the count at 1 for each review cycle, so
    // a retried node's rounds arrive as [1, 2, 1, 2]. Keying the dots on the
    // round number alone loses half of them.
    const scale = hourOf(T('2026-09-28T08:00:00Z'), T('2026-09-28T09:00:00Z'));
    const at = (m: number) => `2026-09-28T08:${String(m).padStart(2, '0')}:00+00:00`;
    const marks = roundMarks(
      node({
        rounds: [
          { round: 1, verdict: 'NEEDS_CHANGES', recorded_at: at(10) },
          { round: 2, verdict: 'READY', recorded_at: at(20) },
          { round: 1, verdict: 'NEEDS_CHANGES', recorded_at: at(30) },
          { round: 2, verdict: 'READY', recorded_at: at(40) }
        ]
      }),
      scale
    );
    expect(marks).toHaveLength(4);
    expect(new Set(marks.map((m) => m.key)).size).toBe(4);
    // Even two rounds the store wrote at the same instant stay apart.
    const same = roundMarks(
      node({
        rounds: [
          { round: 1, verdict: 'READY', recorded_at: at(10) },
          { round: 1, verdict: 'READY', recorded_at: at(10) }
        ]
      }),
      scale
    );
    expect(new Set(same.map((m) => m.key)).size).toBe(2);
  });

  it('places the merge, and has none to place when nothing merged', () => {
    const scale = hourOf(T('2026-09-28T08:00:00Z'), T('2026-09-28T09:00:00Z'));
    expect(mergeMark(node({ merged_at: '2026-09-28T08:45:00+00:00' }), scale)?.ms).toBe(
      T('2026-09-28T08:45:00Z')
    );
    expect(mergeMark(node(), scale)).toBeNull();
  });
});

describe('the rows', () => {
  it('is one 28px row per node, in the API`s order', () => {
    const scale = fitScale(spanOf(fixture), WIDTH);
    const list = rows(fixture, scale, nowOf(fixture));
    expect(list).toHaveLength(fixture.nodes.length);
    expect(list.map((r) => r.id)).toEqual(fixture.nodes.map((n) => n.id));
    list.forEach((r, i) => expect(r.y).toBe(i * ROW_H));
    expect(chartHeight(list.length)).toBe(fixture.nodes.length * ROW_H);
  });

  it('and that order is first activity, the nodes with none last', () => {
    // The API owns the ordering (`EpicTimelineDTO`); the model must not
    // re-sort, so this is the contract both ends hold, read off the fixture.
    const firsts = fixture.nodes.map((n) => firstActivityMs(n));
    let seenNull = false;
    let last = -Infinity;
    for (const ms of firsts) {
      if (ms === null) {
        seenNull = true;
        continue;
      }
      expect(seenNull).toBe(false);
      expect(ms).toBeGreaterThanOrEqual(last);
      last = ms;
    }
    expect(firstActivityMs(node())).toBeNull();
    expect(firstActivityMs(node({ runs: [run()] }))).toBe(T('2026-09-28T08:00:00Z'));
  });

  it('draws something for every node the fixture has anything for', () => {
    const scale = fitScale(spanOf(fixture), WIDTH);
    const list = rows(fixture, scale, nowOf(fixture));
    const drawn = list.filter((r) => r.bars.length > 0 || r.bands.length > 0);
    expect(drawn.length).toBeGreaterThan(0);
    for (const r of list) {
      for (const b of [...r.bars, ...r.bands]) {
        expect(b.width).toBeGreaterThan(0);
        expect(Number.isFinite(b.x)).toBe(true);
      }
    }
  });
});

describe('the selection walk', () => {
  const ids = ['a', 'b', 'c'];
  it('moves down and up without wrapping', () => {
    expect(rowStep(ids, 'a', 1)).toBe('b');
    expect(rowStep(ids, 'c', 1)).toBeNull();
    expect(rowStep(ids, 'b', -1)).toBe('a');
    expect(rowStep(ids, 'a', -1)).toBeNull();
  });

  it('starts at an end when nothing is selected', () => {
    expect(rowStep(ids, null, 1)).toBe('a');
    expect(rowStep(ids, null, -1)).toBe('c');
    expect(rowStep([], null, 1)).toBeNull();
    // An id this timeline does not have is the same as no selection.
    expect(rowStep(ids, 'zz', 1)).toBe('a');
  });
});

/** A scale over `[from, to]` at the test width, without fit's padding. */
function hourOf(from: number, to: number) {
  return { startMs: from, endMs: to, width: WIDTH };
}

describe('the Graph / Timeline switch', () => {
  it('reads ?view= and falls back to the graph', () => {
    expect(canvasView('timeline')).toBe('timeline');
    expect(canvasView('graph')).toBe('graph');
    // A stale or hand-typed link must land on a view this page has.
    for (const raw of [null, undefined, '', 'gantt', 'TIMELINE'])
      expect(canvasView(raw)).toBe('graph');
    expect(otherView('graph')).toBe('timeline');
    expect(otherView('timeline')).toBe('graph');
    expect(CANVAS_VIEWS).toEqual(['graph', 'timeline']);
    expect(CANVAS_VIEW_LABEL.timeline).toBe('Timeline');
  });

  it('is T, and is not a modifier combination or a T typed into a field', () => {
    const press = (over: Record<string, unknown> = {}) => ({
      key: 't',
      metaKey: false,
      ctrlKey: false,
      altKey: false,
      typing: false,
      ...over
    });
    expect(togglesView(press())).toBe(true);
    expect(togglesView(press({ key: 'T' }))).toBe(true);
    expect(togglesView(press({ key: 'g' }))).toBe(false);
    expect(togglesView(press({ typing: true }))).toBe(false);
    for (const mod of ['metaKey', 'ctrlKey', 'altKey'])
      expect(togglesView(press({ [mod]: true }))).toBe(false);
  });
});

describe('a bar as a link into the run page (#2300)', () => {
  it('names the run, its attempt, and the instant it started', () => {
    const bar = barBoxes(node({ runs: [run({ run_id: 'r9', attempt_number: 3 })] }), hour, T('2026-09-28T09:00:00Z'))[0];

    expect(barHref(bar)).toBe('/runs/r9?attempt=3&focusedTime=2026-09-28T08%3A00%3A00%2B00%3A00');
  });

  it('encodes a run id that is not URL-safe', () => {
    const bar = barBoxes(node({ runs: [run({ run_id: 'a/b?c' })] }), hour, T('2026-09-28T09:00:00Z'))[0];

    expect(barHref(bar).startsWith('/runs/a%2Fb%3Fc?')).toBe(true);
  });

  it('omits the focused time for a run with no recorded start', () => {
    // `barBoxes` leaves such a run out of the picture, so the href is built
    // from the bar of a run that DID start — the guard is on the builder.
    const bar = barBoxes(node({ runs: [run()] }), hour, T('2026-09-28T09:00:00Z'))[0];
    expect(barHref({ ...bar, run: { ...bar.run, started_at: null } })).toBe('/runs/r1?attempt=1');
  });

  it('says in one sentence what the picture shows', () => {
    const bar = barBoxes(node({ runs: [run({ attempt_number: 2 })] }), hour, T('2026-09-28T09:00:00Z'))[0];

    expect(barLabel(bar)).toBe('dispatch run r1, attempt 2, success');
  });

  it('resolves to a real event on the run page it opens', () => {
    // The link is only worth anything if the run page can RESOLVE the instant
    // it carries. In production the row is written before anything is appended
    // under its id, so every one of a run's ledger events is recorded after
    // the `started_at` this href names — and a nearest-BEFORE lookup answered
    // nothing, leaving the marker, the row highlight and the caption silently
    // doing nothing on every bar link.
    const started = '2026-09-28T08:00:00+00:00';
    const bar = barBoxes(node({ runs: [run({ run_id: 'r9', started_at: started })] }), hour, T('2026-09-28T09:00:00Z'))[0];
    const focusedTime = new URL(barHref(bar), 'http://localhost').searchParams.get('focusedTime');

    const ledger: RunAuditEvent[] = [
      { seq: 10, ts: '2026-09-28T08:00:02+00:00', event: 'dispatch_started', actor_type: 'policy', item_id: '1', payload: null },
      { seq: 11, ts: '2026-09-28T08:00:04+00:00', event: 'routing_decision', actor_type: 'policy', item_id: '1', payload: null }
    ];

    expect(focusedTime).toBe(started);
    expect(focusedRunEvent(ledger, focusedTime)?.seq).toBe(10);
  });
});
