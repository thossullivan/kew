/**
 * #2304 — the tick strip's model.
 *
 * What is pinned here is what no screenshot of the strip can catch: that the
 * slots are a fixed width padded on the OLD side (so strips align down a
 * column), that cyan is spent on the live tick and nothing else, that a quiet
 * tick is `--text-5` and not a mark, and that an outcome word this build has
 * never heard of reads quiet rather than colouring a whole strip.
 */
import { describe, expect, it } from 'vitest';
import type { EpicHeartbeat, EpicTickStrip, Iteration } from '$lib/api';
import { staleness } from '$lib/epic/staleness';
import { STATUS_MARKS } from './status';
import { axisX } from '$lib/time/axis';
import { MIN_MARK_W, batchMarks } from '$lib/time/batch';
import {
  COMPACT_SLOTS,
  EMPTY_LANE_MS,
  FULL_STRIP_LANE_W,
  FULL_STRIP_LIMIT,
  FULL_STRIP_RIGHT_PAD,
  FULL_STRIP_WIDTH,
  LIVE_MEANING,
  DRIVER_BUSY,
  DRIVER_LIVE,
  QUIET_COLOR,
  TICK_LIVE_MAX_MS,
  batchFill,
  changeSlots,
  fitStripWidth,
  heartbeatFromTickLog,
  isIterationLive,
  isTickLive,
  iterationCounts,
  iterationSlots,
  iterationWord,
  lastTickTs,
  stripLane,
  stripLimit,
  stripSummary,
  tickCounts,
  tickMarks,
  tickMeaning,
  tickSlots,
  tickWord
} from './tick-strip';

const tick = (over: Partial<EpicTickStrip> = {}): EpicTickStrip => ({
  id: 1,
  ts_started: '2026-10-07T00:00:00+00:00',
  ts_finished: '2026-10-07T00:00:02+00:00',
  outcome: 'nothing',
  signal: 'progressing',
  n_dispatched: 0,
  n_merged: 0,
  n_held: 0,
  n_stuck: 0,
  journal_tick_id: null,
  error_class: null,
  ...over
});

/**
 * An attempt opened a second ago and not closed — a tick genuinely in flight,
 * past `begin_epic_tick` (#2882).
 *
 * Relative to the real clock on purpose: "running" is a BOUNDED claim
 * (`TICK_LIVE_MAX_MS`), so a frozen stamp is a tick that reads live on the day
 * it was written and abandoned forever after.
 */
const liveTick = (over: Partial<EpicTickStrip> = {}): EpicTickStrip =>
  tick({
    ts_started: new Date(Date.now() - 1_000).toISOString(),
    ts_finished: null,
    outcome: null,
    reached_tick: true,
    ...over
  });

/** An attempt opened long enough ago that its driver cannot still be alive. */
const orphanTick = (): EpicTickStrip =>
  liveTick({ ts_started: new Date(Date.now() - TICK_LIVE_MAX_MS - 1_000).toISOString() });

/** `n` attempts a minute apart — a perfectly even run, which is the best case
 * the data can offer and so the one the lane's arithmetic has to survive. */
const spread = (n: number): EpicTickStrip[] =>
  Array.from({ length: n }, (_, i) =>
    tick({ id: i + 1, ts_started: new Date(i * 60_000).toISOString(), ts_finished: null })
  );

const iteration = (over: Partial<Iteration> = {}): Iteration => ({
  iteration_id: 'I0',
  idx: 0,
  kind: 'assistant_turn',
  status: 'success',
  cost_usd: 0,
  tokens: 0,
  run_id: null,
  started_at: '2026-10-07T00:00:00+00:00',
  finished_at: '2026-10-07T00:00:01+00:00',
  ...over
});

describe('a tick slot', () => {
  it('is quiet for the ticks that did nothing, and for a word it cannot read', () => {
    for (const outcome of ['nothing', 'locked_out', 'abandoned', 'teleported', null]) {
      if (outcome === null) {
        // A finish stamp with no outcome is still an UNCLOSED row server-side,
        // so a fresh one reads live — the one state the strip paints cyan.
        const open = liveTick({ ts_finished: '2026-10-07T00:00:02+00:00' });
        expect(tickSlots([open], 1)[0].live).toBe(true);
        continue;
      }
      const slot = tickSlots([tick({ outcome })], 1)[0];
      expect(slot.kind).toBe('quiet');
      expect(slot.meaning).toBeNull();
      expect(slot.severity).toBe('quiet');
      expect(slot.live).toBe(false);
    }
    // The quiet colour is the token DESIGN.md names, not a mark's.
    expect(QUIET_COLOR).toBe('var(--text-5)');
  });

  it('wears the STATUS_MARKS shape and colour for the ticks that did something', () => {
    const marks = {
      dispatched: STATUS_MARKS.done,
      merged: STATUS_MARKS.done,
      held: STATUS_MARKS.held,
      failed: STATUS_MARKS.failed,
      empty_graph: STATUS_MARKS.missing
    };
    for (const [outcome, expected] of Object.entries(marks)) {
      const slot = tickSlots([tick({ outcome })], 1)[0];
      expect(slot.kind).toBe('mark');
      expect(STATUS_MARKS[slot.meaning!]).toEqual(expected);
      expect(slot.live).toBe(false);
    }
    // "Failed is the red ×."
    expect(STATUS_MARKS.failed).toEqual({ shape: 'x', color: 'var(--danger)' });
  });

  it('spends cyan on the in-progress tick and on nothing else', () => {
    const live = tickSlots([liveTick()], 1)[0];
    expect(live.live).toBe(true);
    expect(live.meaning).toBe(LIVE_MEANING);
    expect(STATUS_MARKS[LIVE_MEANING].color).toBe('var(--accent-2)');

    // No finished outcome may claim the live colour.
    for (const outcome of ['dispatched', 'merged', 'held', 'failed', 'empty_graph', 'nothing']) {
      const slot = tickSlots([tick({ outcome })], 1)[0];
      expect(slot.live).toBe(false);
      if (slot.meaning) expect(STATUS_MARKS[slot.meaning].color).not.toBe('var(--accent-2)');
    }
  });

  /**
   * #2882 — the row opens before the lock, the preflight and the graph build.
   * An attempt still in them ran no tick, so it is neither cyan nor "running",
   * on the attempt strip or the change strip, and does not grow the bar.
   */
  it('draws an open attempt that has not reached its tick quietly', () => {
    for (const reached of [false, undefined]) {
      const before = liveTick({ reached_tick: reached });
      expect(isTickLive(before)).toBe(false);
      expect(isTickLive(before, Date.now(), DRIVER_LIVE)).toBe(false);
      expect(tickMeaning(before)).toBeNull();
      const slot = tickSlots([before], 1)[0];
      expect(slot.live).toBe(false);
      expect(slot.kind).toBe('quiet');
      expect(tickWord(before)).toBe('starting');
      expect(changeSlots([before], 1)[0].kind).toBe('pad');
    }
    // Given up on, it reads as the sweep will write it.
    expect(
      tickWord(
        liveTick({
          reached_tick: false,
          ts_started: new Date(Date.now() - TICK_LIVE_MAX_MS - 1_000).toISOString()
        })
      )
    ).toBe('abandoned');
  });

  it('reads an open row as live however the two null halves arrive', () => {
    expect(isTickLive(liveTick({ outcome: 'nothing' }))).toBe(true);
    expect(isTickLive(liveTick({ ts_finished: '2026-10-07T00:00:02+00:00' }))).toBe(true);
    expect(isTickLive(tick())).toBe(false);
    expect(tickMeaning(liveTick())).toBe(LIVE_MEANING);
  });

  /**
   * The reviewer's finding (PR #2438): nothing closes the row of a driver that
   * dies mid-tick — no TaskStop handler, no server-side reaper that can see it
   * — so an unbounded reading paints that one attempt as a tick still running
   * for the rest of the epic's life. The claim expires with the lock's own
   * staleness threshold; the epic's next tick then closes the row for real.
   */
  it('stops calling an open row live once its driver cannot still be alive', () => {
    const stale = orphanTick();
    expect(isTickLive(stale)).toBe(false);
    expect(tickMeaning(stale)).toBeNull();
    const slot = tickSlots([stale], 1)[0];
    expect(slot.kind).toBe('quiet');
    expect(slot.severity).toBe('quiet');
    // The word the epic's next tick writes into that row, said before the
    // sweep as well as after it.
    expect(tickWord(stale)).toBe('abandoned');
    expect(tickWord(tick({ outcome: 'abandoned' }))).toBe('abandoned');
    expect(tickSlots([tick({ outcome: 'abandoned' })], 1)[0].kind).toBe('quiet');

    // Exactly at the bound it is still live — the bound is an age, not a
    // reading of the row, so an off-by-one here is a strip that flickers.
    const now = Date.parse('2026-10-07T01:00:00+00:00');
    const at = liveTick({ ts_started: new Date(now - TICK_LIVE_MAX_MS).toISOString() });
    expect(isTickLive(at, now)).toBe(true);
    expect(isTickLive(at, now + 1)).toBe(false);

    // An unreadable start buys no cyan either: there is no instant to bound.
    expect(isTickLive(liveTick({ ts_started: 'not a time' }))).toBe(false);
  });

  /**
   * The reviewer's finding (PR #2438, round 5): the bound above is an age of
   * the tick's START, not of the driver's last heartbeat, so on its own it
   * cannot tell a 6-minute tick from a dead driver — and it was deciding that
   * question anyway. A tick that legitimately outlasts it is exactly what
   * `_lock_keepalive` (kew/graph/driver/tick.py) exists to support: the merge
   * queue's combined-tree verification blocks longer than
   * `HEARTBEAT_STALE_SECONDS` and beats the lock through it. Served the
   * driver's own liveness reading, the strip keeps that tick running instead of
   * drawing it quiet and labelling it abandoned.
   */
  it('keeps a long tick live while the server can still see its driver beating', () => {
    const long = orphanTick();
    expect(isTickLive(long, Date.now(), DRIVER_LIVE)).toBe(true);
    expect(tickMeaning(long, DRIVER_LIVE)).toBe(LIVE_MEANING);
    expect(tickWord(long, DRIVER_LIVE)).toBe('running');
    // #2534: a driver busy inside one foreground stage holds an open tick too.
    expect(isTickLive(long, Date.now(), DRIVER_BUSY)).toBe(true);

    const slot = tickSlots([long], 1, DRIVER_LIVE)[0];
    expect(slot.kind).toBe('mark');
    expect(slot.live).toBe(true);
    // The full strip reads the same rows through `tickMarks`, so its bar grows
    // and is batched at the live severity rather than smeared into the quiet
    // hairline the roster slot would have been.
    expect(tickMarks([long], DRIVER_LIVE)[0].severity).not.toBe('quiet');

    // The reading only ever EXTENDS the claim. "gone", "unknown" and an older
    // `kew serve` that serves none all leave the floor as the only evidence,
    // which is the bound above and unchanged.
    for (const driver of ['gone', 'unknown', '', null, undefined]) {
      expect(isTickLive(long, Date.now(), driver)).toBe(false);
      expect(tickWord(long, driver)).toBe('abandoned');
    }
    // And it cannot resurrect a row the server itself CLOSED: a finished tick
    // is finished however alive its driver is.
    expect(isTickLive(tick(), Date.now(), DRIVER_LIVE)).toBe(false);
    // Nor buy cyan for a row with no readable start.
    expect(isTickLive(liveTick({ ts_started: 'not a time' }), Date.now(), DRIVER_LIVE)).toBe(
      false
    );
  });
});

describe('the last tick (#2545 D23)', () => {
  it('reads the strip when the change journal has no tick', () => {
    // The fixture row that printed "no ticks yet" beside a full strip.
    const ticks = [
      tick({ id: 1, ts_started: '2026-10-03T14:24:30+00:00' }),
      tick({ id: 2, ts_started: '2026-10-03T14:47:30+00:00' })
    ];
    expect(lastTickTs(null, ticks)).toBe('2026-10-03T14:47:30+00:00');
  });

  it('takes the newer of the journal and the strip, in either order', () => {
    const old = '2026-10-01T00:00:00+00:00';
    const recent = '2026-10-03T00:00:00+00:00';
    expect(lastTickTs(old, [tick({ ts_started: recent })])).toBe(recent);
    expect(lastTickTs(recent, [tick({ ts_started: old })])).toBe(recent);
    expect(lastTickTs(null, [tick({ ts_started: recent }), tick({ ts_started: old })])).toBe(
      recent
    );
  });

  it('keeps the journal stamp while the strip is unread or empty', () => {
    expect(lastTickTs('2026-10-01T00:00:00+00:00', undefined)).toBe('2026-10-01T00:00:00+00:00');
    expect(lastTickTs(null, [])).toBeNull();
    expect(lastTickTs(null, undefined)).toBeNull();
  });
});

describe('the sparkline is the tick log (#2783 D23)', () => {
  it('draws one point per logged attempt, in the log`s order, newest last', () => {
    // A driver whose change journal moved 862 times but whose last attempts
    // dispatched, held and failed: the strip says so, attempt by attempt,
    // because every point is a row of the log and nothing else.
    const log = [
      tick({ id: 1, ts_started: '2026-10-07T00:00:00+00:00', outcome: 'dispatched' }),
      tick({ id: 2, ts_started: '2026-10-07T00:01:00+00:00', outcome: 'nothing' }),
      tick({ id: 3, ts_started: '2026-10-07T00:02:00+00:00', outcome: 'held' }),
      tick({ id: 4, ts_started: '2026-10-07T00:03:00+00:00', outcome: 'failed' })
    ];
    const points = tickSlots(log, COMPACT_SLOTS).filter((s) => s.kind !== 'pad');
    expect(points.map((s) => s.item)).toEqual(log);
    expect(points.map((s) => s.meaning)).toEqual(['done', null, 'held', 'failed']);
    // An empty log is an empty strip — no point is invented for it.
    expect(tickSlots([], COMPACT_SLOTS).every((s) => s.kind === 'pad')).toBe(true);
  });
});

describe('the heartbeat read against the tick log (#2783 N18)', () => {
  const beat = (over: Partial<EpicHeartbeat> = {}): EpicHeartbeat => ({
    last_tick_started_at: null,
    last_tick_ts: null,
    last_tick_duration_s: null,
    tick_count: 412,
    interval_s: 60,
    last_signal: 'progressing',
    change_count: 0,
    ...over
  });

  it('takes its last tick from the log when the journal has none', () => {
    // The fixture `launch-fixes` heartbeat: no stamps, a strip full of
    // attempts. Read alone it said "no heartbeat yet" above them.
    const log = [
      tick({ id: 1, ts_started: '2026-10-07T00:00:00+00:00' }),
      tick({
        id: 2,
        ts_started: '2026-10-07T00:01:00+00:00',
        ts_finished: '2026-10-07T00:01:03+00:00'
      })
    ];
    const read = heartbeatFromTickLog(beat(), log);
    expect(read?.last_tick_started_at).toBe('2026-10-07T00:01:00+00:00');
    expect(read?.last_tick_ts).toBe('2026-10-07T00:01:00+00:00');
    expect(read?.last_tick_duration_s).toBe(3);
    expect(read?.tick_count).toBe(412);
    expect(staleness(read, Date.parse('2026-10-07T00:01:30+00:00')).kind).toBe('fresh');
  });

  it('keeps a heartbeat that is already newer than the log, and keeps no heartbeat as none', () => {
    const hb = beat({
      last_tick_started_at: '2026-10-07T00:05:00+00:00',
      last_tick_ts: '2026-10-07T00:05:02+00:00'
    });
    expect(heartbeatFromTickLog(hb, [tick()])).toBe(hb);
    expect(heartbeatFromTickLog(hb, [])).toBe(hb);
    expect(heartbeatFromTickLog(null, [tick()])).toBeNull();
  });

  it('leaves a complete epic`s finish stamp where the journal put it', () => {
    const hb = beat({
      last_signal: 'complete',
      last_tick_started_at: '2026-10-01T00:00:00+00:00',
      last_tick_ts: '2026-10-01T00:00:02+00:00'
    });
    const read = heartbeatFromTickLog(hb, [tick()]);
    expect(read?.last_tick_ts).toBe('2026-10-01T00:00:02+00:00');
    expect(staleness(read, Date.now())).toEqual({
      kind: 'complete',
      finishedAt: '2026-10-01T00:00:02+00:00'
    });
  });
});

describe('fixed slots', () => {
  it('pads the OLD side so the newest tick is the rightmost slot', () => {
    const slots = tickSlots([tick({ id: 7, outcome: 'failed' })], 5);
    expect(slots).toHaveLength(5);
    expect(slots.slice(0, 4).map((s) => s.kind)).toEqual(['pad', 'pad', 'pad', 'pad']);
    expect(slots[4].item?.id).toBe(7);
  });

  it('is a pad and not a quiet tick where no tick ran', () => {
    // The distinction the whole padding rule exists for: an epic that ticked
    // once has one slot of history, not N-1 ticks that did nothing.
    const pads = tickSlots([], 3);
    expect(pads.map((s) => s.kind)).toEqual(['pad', 'pad', 'pad']);
    expect(pads.every((s) => s.item === null)).toBe(true);
  });

  it('keeps the NEWEST attempts when there are more than slots', () => {
    const many = [1, 2, 3, 4].map((id) => tick({ id }));
    expect(tickSlots(many, 2).map((s) => s.item?.id)).toEqual([3, 4]);
    expect(tickSlots(many, 0)).toEqual([]);
    expect(tickSlots(many, -3)).toEqual([]);
  });

  it('defaults to one compact width for every row on the page', () => {
    expect(tickSlots([tick()])).toHaveLength(COMPACT_SLOTS);
    expect(iterationSlots([iteration()])).toHaveLength(COMPACT_SLOTS);
  });
});

describe('iteration slots', () => {
  it('reads a loop iteration through the run vocabulary', () => {
    const failed = iterationSlots([iteration({ status: 'failed' })], 1)[0];
    expect(failed.kind).toBe('mark');
    expect(STATUS_MARKS[failed.meaning!]).toEqual(STATUS_MARKS.failed);

    const running = iterationSlots([iteration({ finished_at: null })], 1)[0];
    expect(running.live).toBe(true);
    expect(running.meaning).toBe(LIVE_MEANING);
    expect(isIterationLive(iteration({ finished_at: null }))).toBe(true);
  });

  it('draws an unreadable status quiet rather than ringing it', () => {
    const slot = iterationSlots([iteration({ status: 'enlightened' })], 1)[0];
    expect(slot.kind).toBe('quiet');
    expect(slot.meaning).toBeNull();
  });
});

describe('the popover', () => {
  it('shows only the counts that happened, plus the error class', () => {
    expect(tickCounts(tick())).toEqual([]);
    expect(tickCounts(tick({ n_dispatched: 2, n_held: 1 }))).toEqual(['2 dispatched', '1 held']);
    expect(tickCounts(tick({ outcome: 'failed', error_class: 'HTTPError' }))).toEqual([
      'HTTPError'
    ]);
    expect(
      tickCounts(tick({ outcome: 'failed', n_stuck: 3, error_class: 'TimeoutError' }))
    ).toEqual(['3 stuck', 'TimeoutError']);
  });

  it('prints the server\'s own outcome word, even one it has no mark for', () => {
    expect(tickWord(tick({ outcome: 'teleported' }))).toBe('teleported');
    expect(tickWord(liveTick())).toBe('running');
    expect(tickWord(null)).toBe('no tick');
  });
});

describe('the batched full strip', () => {
  it('carries each tick\'s span and severity into the batcher', () => {
    const marks = tickMarks([
      tick({ id: 1, ts_started: '2026-10-07T00:00:00Z', ts_finished: '2026-10-07T00:00:02Z' }),
      tick({
        id: 2,
        outcome: 'failed',
        error_class: 'OSError',
        ts_started: '2026-10-07T00:00:10Z',
        ts_finished: '2026-10-07T00:00:11Z'
      })
    ]);
    expect(marks.map((m) => m.severity)).toEqual(['quiet', 'failed']);
    expect((marks[0]?.endMs ?? 0) - (marks[0]?.startMs ?? 0)).toBe(2000);
  });

  it('gives a running tick no end, and drops a row whose stamp cannot be read', () => {
    const live = tickMarks([liveTick()]);
    expect(live[0]?.endMs).toBe(live[0]?.startMs);
    // A bad historical row is tolerated by the reader, never rewritten — and
    // never drawn at the epoch either.
    expect(tickMarks([tick({ ts_started: 'not a time' })])).toEqual([]);
    const unreadableEnd = tickMarks([tick({ ts_finished: 'not a time' })]);
    expect(unreadableEnd[0]?.endMs).toBe(Date.parse('2026-10-07T00:00:00+00:00'));
  });

  /**
   * The reviewer's finding (PR #2438): the strip asked for 200 attempts and
   * drew them on a 760px lane, which is below the batcher's 4px floor — so the
   * full strip collapsed to ONE bar at anything over ~190 attempts however the
   * driver was paced, and the limit, not the data, was what made it useless.
   */
  it('asks for no more attempts than its own lane can draw apart', () => {
    expect(FULL_STRIP_LANE_W).toBe(FULL_STRIP_WIDTH - FULL_STRIP_RIGHT_PAD);
    expect(FULL_STRIP_LIMIT).toBe(Math.floor(FULL_STRIP_LANE_W / MIN_MARK_W));

    // A perfectly even run of exactly the limit, spread over the whole lane:
    // the best case the data can offer, which is the one the limit has to
    // survive. Every attempt keeps its own bar.
    const atLimit = spread(FULL_STRIP_LIMIT);
    expect(batchMarks(tickMarks(atLimit), stripLane(tickMarks(atLimit)))).toHaveLength(
      FULL_STRIP_LIMIT
    );

    // One more and the floor starts eating bars — which is why the limit is
    // read off the lane rather than chosen. At the old 200 the whole strip was
    // a single bar.
    const over = spread(FULL_STRIP_LIMIT + 1);
    expect(batchMarks(tickMarks(over), stripLane(tickMarks(over))).length).toBeLessThan(
      FULL_STRIP_LIMIT + 1
    );
    const old = spread(200);
    expect(batchMarks(tickMarks(old), stripLane(tickMarks(old)))).toHaveLength(1);
  });
});

describe("the full strip's lane", () => {
  /**
   * The slack a pixel comparison here allows.
   *
   * The lane is kept in floating point on purpose (`epic/timeline.ts`), and
   * solving for a pad against instants around 1.8e12 ms leaves a few parts in
   * 10^11 of a pixel on the far edge. That is nanometres of a bar, not a bar in
   * the clip; a test that demanded exactness would be pinning the float, not
   * the geometry.
   */
  const PX_EPS = 1e-6;

  /** Every bar a lane produces, with where it was actually placed. */
  const barsOn = (ticks: EpicTickStrip[], width = FULL_STRIP_WIDTH) => {
    const marks = tickMarks(ticks);
    return batchMarks(marks, stripLane(marks, width));
  };

  /**
   * The reviewer's finding (PR #2438): the lane's right edge WAS the newest
   * attempt's instant, and a running tick's mark has `endMs === startMs` — so
   * the live bar was placed at x === width, outside a width-px SVG that clips
   * its own content. The one bar a reader is looking for drew zero visible
   * pixels for exactly as long as a tick was in flight.
   */
  it('holds a pad open so the running tick draws inside the clipped lane', () => {
    const bars = barsOn([
      tick({ id: 1, ts_started: '2026-10-07T00:00:00Z', ts_finished: '2026-10-07T00:02:00Z' }),
      liveTick({ id: 2 })
    ]);
    // Two bars: the live attempt is its own, not swallowed by the older one.
    expect(bars).toHaveLength(2);
    const live = bars[bars.length - 1];
    expect(live.items.map((m) => m.item.id)).toEqual([2]);
    // Wholly inside the lane, and no narrower than the batcher's floor — the
    // two halves of "a reader can see it".
    expect(live.width).toBeGreaterThanOrEqual(MIN_MARK_W);
    expect(live.x).toBeGreaterThanOrEqual(0);
    expect(live.x + live.width).toBeLessThanOrEqual(FULL_STRIP_WIDTH + PX_EPS);
    // The whole of the batcher's minimum mark fits, which is the finding: it
    // used to be placed AT x = width, so none of it did.
    expect(FULL_STRIP_WIDTH - live.x).toBeGreaterThanOrEqual(MIN_MARK_W - PX_EPS);
  });

  it('keeps every bar inside the lane, at any width and any pacing', () => {
    const cases: EpicTickStrip[][] = [
      [liveTick({ id: 1 })],
      [tick({ id: 1, ts_finished: '2026-10-07T00:00:00+00:00' })],
      [tick({ id: 1 }), liveTick({ id: 2 })],
      spread(FULL_STRIP_LIMIT),
      [...spread(4), liveTick({ id: 99 })]
    ];
    for (const width of [40, 220, FULL_STRIP_WIDTH]) {
      for (const ticks of cases) {
        for (const bar of barsOn(ticks, width)) {
          expect(bar.x).toBeGreaterThanOrEqual(0);
          expect(bar.width).toBeGreaterThanOrEqual(MIN_MARK_W - PX_EPS);
          expect(bar.x + bar.width).toBeLessThanOrEqual(width + PX_EPS);
        }
      }
    }
  });

  it('opens a span with nothing to measure to a minute, and covers no more', () => {
    const now = Date.parse('2026-10-07T12:00:00Z');
    // No attempts at all: a minute ending at now, so the ruler has something
    // to divide rather than a zero-span window every reading divides by.
    const empty = stripLane([], FULL_STRIP_WIDTH, now);
    expect(empty.startMs).toBe(now - EMPTY_LANE_MS);
    expect(axisX(empty, now)).toBeCloseTo(FULL_STRIP_LANE_W, 3);

    // One attempt, and two instantaneous attempts stamped at the same instant:
    // the same degenerate span — a clock that did not move — opened the same
    // way and starting at the attempt.
    const still = { ts_started: '2026-10-07T00:00:00Z', ts_finished: '2026-10-07T00:00:00Z' };
    for (const ticks of [
      [liveTick({ id: 1 })],
      [tick({ id: 1, ...still }), tick({ id: 2, ...still })]
    ]) {
      const marks = tickMarks(ticks);
      const lane = stripLane(marks, FULL_STRIP_WIDTH, now);
      expect(lane.startMs).toBe(Math.min(...marks.map((m) => m.startMs)));
      expect(axisX(lane, lane.startMs + EMPTY_LANE_MS)).toBeCloseTo(FULL_STRIP_LANE_W, 3);
    }

    // And a real span is still the attempts' own, nothing wider: the newest
    // instant lands on the pad's edge, not past it.
    const marks = tickMarks([
      tick({ id: 1, ts_started: '2026-10-07T00:00:00Z', ts_finished: '2026-10-07T00:00:02Z' }),
      tick({ id: 2, ts_started: '2026-10-07T01:00:00Z', ts_finished: '2026-10-07T01:00:30Z' })
    ]);
    const lane = stripLane(marks, FULL_STRIP_WIDTH, now);
    expect(lane.startMs).toBe(Date.parse('2026-10-07T00:00:00Z'));
    expect(axisX(lane, Date.parse('2026-10-07T01:00:30Z'))).toBeCloseTo(FULL_STRIP_LANE_W, 3);
  });
});

/**
 * #2473 — found browser-checking the strip at 390x844: the full strip drew at
 * its design 760px inside a box the epic page clips at the viewport, so roughly
 * half of it was cut off — and since the strip puts its newest attempt LAST, the
 * half taken was the recent ticks, the reading a person opens the page for.
 */
describe("the full strip's width inside its box", () => {
  /** The epic page's header strip at 390px: the window less its `px-10`
   *  gutters. The exact number is the page's, not this test's — what is pinned
   *  is that a box narrower than the design width wins. */
  const PHONE_BOX = 390 - 80;

  it('draws at the design width wherever that fits', () => {
    expect(fitStripWidth(FULL_STRIP_WIDTH)).toBe(FULL_STRIP_WIDTH);
    expect(fitStripWidth(1360)).toBe(FULL_STRIP_WIDTH);
  });

  it('fits a box too narrow for the design width, which is the bug', () => {
    expect(fitStripWidth(PHONE_BOX)).toBe(PHONE_BOX);
    expect(fitStripWidth(PHONE_BOX)).toBeLessThan(FULL_STRIP_WIDTH);
  });

  it('never returns a fractional width, which an SVG attribute cannot clip to', () => {
    expect(fitStripWidth(310.6)).toBe(310);
  });

  it('reads an unmeasured box as the design width rather than as 0px', () => {
    // `bind:clientWidth` is 0 until the box is laid out, and a 0px strip is an
    // invisible one — on a desktop viewport that frame would be a strip that
    // vanished rather than one frame of the clip it is on a phone.
    expect(fitStripWidth(0)).toBe(FULL_STRIP_WIDTH);
    expect(fitStripWidth(-40)).toBe(FULL_STRIP_WIDTH);
    expect(fitStripWidth(Number.NaN)).toBe(FULL_STRIP_WIDTH);
    expect(fitStripWidth(Number.POSITIVE_INFINITY)).toBe(FULL_STRIP_WIDTH);
  });

  /** Every bar a given set of attempts draws at a given width. */
  const barsAt = (ticks: EpicTickStrip[], width: number) => {
    const marks = tickMarks(ticks);
    return batchMarks(marks, stripLane(marks, width));
  };

  /**
   * The point of fitting rather than anchoring: the newest attempt still lands
   * against the fitted strip's own right edge, with the batcher's whole minimum
   * mark inside it — so the slot that used to be outside the clip is now the
   * last slot a reader can see, and every attempt the fitted width can hold is
   * still drawn.
   *
   * At exactly `stripLimit(width)` attempts, not at a comfortable handful: a
   * count the fitted lane cannot resolve would pass this assertion trivially
   * by collapsing into one batch that still contains everything.
   */
  it('keeps the newest attempt inside the fitted lane, every attempt drawn', () => {
    // `spread` leaves every attempt open, so the newest mark is the
    // zero-duration one the right pad exists for — the hardest placement.
    const width = fitStripWidth(PHONE_BOX);
    const n = stripLimit(width);
    const ticks = spread(n);
    const bars = barsAt(ticks, width);
    // One bar per attempt: the resolution the limit is there to guarantee.
    expect(bars).toHaveLength(n);
    for (const bar of bars) {
      expect(bar.x).toBeGreaterThanOrEqual(0);
      expect(bar.x + bar.width).toBeLessThanOrEqual(width + 1e-6);
    }
    // Nothing was dropped: every attempt is in some batch, newest last.
    expect(bars.flatMap((b) => b.items).length).toBe(n);
    const newest = bars[bars.length - 1];
    expect(newest.items[newest.items.length - 1].item.id).toBe(n);
    expect(width - newest.x).toBeGreaterThanOrEqual(MIN_MARK_W - 1e-6);
  });

  /**
   * The reviewer's finding (round 1 of #2473): fitting the WIDTH was half the
   * fix. The page asks for `FULL_STRIP_LIMIT` attempts, and that number is read
   * off the 760px lane — so the fitted 310px strip was handed 189 of them, and
   * the batcher's 4px floor is not a gradual squeeze but a cliff: at that width
   * 77 evenly paced attempts keep 77 bars and 78 collapse into ONE. The fitted
   * strip degenerated into exactly the single "189 ticks" bar `FULL_STRIP_LIMIT`
   * was introduced (PR #2438) to prevent.
   *
   * So the attempt count is read off the width the strip actually draws at,
   * which is the same rule `FULL_STRIP_LIMIT` already is — it IS that rule at
   * the design width.
   */
  it('counts attempts off the width it draws at, not off the design width', () => {
    const width = fitStripWidth(PHONE_BOX);
    expect(stripLimit(FULL_STRIP_WIDTH)).toBe(FULL_STRIP_LIMIT);
    expect(stripLimit(width)).toBeLessThan(FULL_STRIP_LIMIT);
    // Read off the fitted lane the same way, and never zero attempts however
    // narrow the box measures.
    expect(stripLimit(width)).toBe(Math.floor((width - FULL_STRIP_RIGHT_PAD) / MIN_MARK_W));
    expect(stripLimit(1)).toBeGreaterThanOrEqual(1);
    // An unmeasured box reads as the design width, like `fitStripWidth`.
    expect(stripLimit(0)).toBe(FULL_STRIP_LIMIT);

    // The cliff itself: the design limit at the phone width is ONE bar for 189
    // attempts, which is what the fitted strip drew before the limit was
    // fitted too.
    expect(barsAt(spread(FULL_STRIP_LIMIT), width)).toHaveLength(1);

    // And the trim that follows from the limit keeps the NEWEST attempts, each
    // with a bar of its own.
    const trimmed = spread(FULL_STRIP_LIMIT).slice(-stripLimit(width));
    expect(trimmed[trimmed.length - 1].id).toBe(FULL_STRIP_LIMIT);
    expect(barsAt(trimmed, width)).toHaveLength(stripLimit(width));
  });
});

describe('a bar on the batched full strip', () => {
  /**
   * The reviewer's finding (PR #2438): a live member of a batch overrode the
   * batch's colour outright, so a failure that landed on the same pixels as
   * the running tick drew cyan. At this strip's own width and limit that is
   * every failure on the lane.
   */
  it('wears its worst member\'s colour, and cyan only with nothing to act on', () => {
    for (const severity of ['failed', 'attention'] as const) {
      expect(batchFill(true, severity)).toBe(batchFill(false, severity));
      expect(batchFill(true, severity)).not.toBe(STATUS_MARKS[LIVE_MEANING].color);
    }
    expect(batchFill(false, 'failed')).toBe(STATUS_MARKS.failed.color);
    expect(batchFill(false, 'attention')).toBe(STATUS_MARKS.held.color);

    // Cyan is still the live reading where nothing in the batch broke or is
    // waiting on a person — it is rationed, not withdrawn.
    expect(batchFill(true, 'did')).toBe(STATUS_MARKS[LIVE_MEANING].color);
    expect(batchFill(true, 'quiet')).toBe(STATUS_MARKS[LIVE_MEANING].color);
    expect(batchFill(false, 'did')).toBe(STATUS_MARKS.done.color);
    expect(batchFill(false, 'quiet')).toBe(QUIET_COLOR);
  });
});

describe('a loop iteration slot', () => {
  it('names itself by index and status, and says running with no finish', () => {
    expect(iterationWord(iteration({ idx: 4, status: 'success' }))).toBe('#4 success');
    expect(iterationWord(iteration({ idx: 4, finished_at: null }))).toBe('#4 running');
    expect(iterationWord(iteration({ idx: 0, status: '' }))).toBe('#0 unknown');
    expect(iterationWord(null)).toBe('no iteration');
  });

  it('shows only the readings it has', () => {
    expect(iterationCounts(iteration({ kind: '', cost_usd: 0, tokens: 0 }))).toEqual([]);
    expect(iterationCounts(iteration({ kind: 'turn', cost_usd: 0.42, tokens: 900 }))).toEqual([
      'turn',
      '$0.42',
      '900 tokens'
    ]);
  });
});

describe('the strip summary (its whole accessible reading)', () => {
  it('counts the attempts by word and ignores the padding', () => {
    const slots = tickSlots([tick({ outcome: 'failed' }), tick({ outcome: 'nothing' })], 6);
    expect(stripSummary(slots, tickWord)).toBe('1 failed, 1 nothing');
  });

  it('says so when a strip is all padding', () => {
    expect(stripSummary(tickSlots([], 6), tickWord)).toBe('no ticks yet');
  });
});
