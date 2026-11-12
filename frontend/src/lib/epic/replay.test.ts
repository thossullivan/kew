import { describe, expect, it } from 'vitest';
import {
  dwellMs,
  frameAtT,
  frameClock,
  frameOffsets,
  parseSpeed,
  parseT,
  playbackDwellMs,
  scrubberStops,
  wallClock
} from './replay';

describe('dwellMs — ~30_000/frameCount clamped to [300, 2000]', () => {
  it('clamps to the high bound', () => {
    expect(dwellMs(10)).toBe(2000);
  });
  it('clamps to the low bound', () => {
    expect(dwellMs(300)).toBe(300);
  });
  it('unclamped midpoint', () => {
    expect(dwellMs(60)).toBe(500);
  });
});

describe('wallClock — "HH:MM · +XhYYm"', () => {
  const firstTs = '2026-07-02T05:10:00.000Z';

  it('formats wall time plus offset from the first frame', () => {
    expect(wallClock('2026-07-02T09:12:00.000Z', firstTs)).toBe('09:12 · +4h02m');
  });

  it('+0h00m on the first frame', () => {
    expect(wallClock(firstTs, firstTs)).toBe('05:10 · +0h00m');
  });

  it('invalid tickTs falls back to —', () => {
    expect(wallClock('not-a-date', firstTs)).toBe('—');
  });

  it('invalid firstTs falls back to —', () => {
    expect(wallClock('2026-07-02T09:12:00.000Z', 'not-a-date')).toBe('—');
  });
});

describe('scrubberStops — proportional to real tick_ts spacing', () => {
  it('0h, 1h, 5h -> 0, 0.2, 1.0', () => {
    const base = Date.parse('2026-07-02T00:00:00.000Z');
    const ticks = [0, 1, 5].map((h) => ({ tick_ts: new Date(base + h * 3_600_000).toISOString() }));
    const stops = scrubberStops(ticks);
    expect(stops).toHaveLength(3);
    expect(stops[0]).toBeCloseTo(0);
    expect(stops[1]).toBeCloseTo(0.2);
    expect(stops[2]).toBeCloseTo(1.0);
  });

  it('single tick -> [0]', () => {
    expect(scrubberStops([{ tick_ts: '2026-07-02T00:00:00.000Z' }])).toEqual([0]);
  });

  it('empty -> []', () => {
    expect(scrubberStops([])).toEqual([]);
  });
});

describe('frameClock — which second a frame is a picture of', () => {
  // A live tail that has fallen behind: the last frame is twenty minutes old,
  // and a card in it has been executing for two hours.
  const WALL = Date.parse('2026-10-06T12:00:00.000Z');
  const TICK = '2026-10-06T11:40:00.000Z';
  const SINCE = Date.parse('2026-10-06T10:00:00.000Z');

  it('reads a live frame against the wall clock', () => {
    expect(frameClock(false, TICK, WALL)).toBe(WALL);
  });

  it('reads a replayed frame against the tick’s own instant', () => {
    // Against today's wall clock a week-old tick ages every card to this
    // morning and saturates the running bar it would have drawn.
    expect(frameClock(true, TICK, WALL)).toBe(Date.parse(TICK));
  });

  it('keeps a live frame on the wall clock when the stream has DROPPED', () => {
    // The #2305 review's finding: `streaming` fused "not a replay" with "the
    // channel is up", and the clock read it. A dropped LIVE stream therefore
    // moved every card onto `tick_ts` — the age jumped BACKWARDS at the drop
    // (2h in flight read as 1h40m) and froze there, understating the work
    // unboundedly for as long as the server stayed down. The clock is
    // `replay`'s to choose; a drop stops the MOTION and nothing else.
    const dropped = frameClock(false, TICK, WALL);
    expect(dropped).toBe(WALL);
    expect(dropped - SINCE).toBe(2 * 3_600_000);
    // And what the fused flag used to give instead, for the record.
    expect(frameClock(true, TICK, WALL) - SINCE).toBeLessThan(2 * 3_600_000);
  });

  it('falls back to the wall clock for an unreadable tick_ts', () => {
    expect(frameClock(true, 'not-a-date', WALL)).toBe(WALL);
    expect(frameClock(true, null, WALL)).toBe(WALL);
    expect(frameClock(true, undefined, WALL)).toBe(WALL);
  });
});

describe('a replay link: ?t= and ?speed= (#3142)', () => {
  const ticks = [
    { tick_ts: '2026-10-19T19:00:00+00:00' },
    { tick_ts: '2026-10-19T19:00:10.700+00:00' },
    { tick_ts: '2026-10-19T20:00:00+00:00' }
  ];

  it('reads each frame as whole seconds since the first, floored', () => {
    expect(frameOffsets(ticks)).toEqual([0, 10, 3600]);
  });

  it('reads the frame a t names: the last at or before it', () => {
    expect(frameAtT(ticks, 0)).toBe(0);
    expect(frameAtT(ticks, 9)).toBe(0);
    expect(frameAtT(ticks, 10)).toBe(1);
    expect(frameAtT(ticks, 3599)).toBe(1);
    expect(frameAtT(ticks, 99_999)).toBe(2);
  });

  it('round-trips: the t a frame writes reads back to that frame', () => {
    frameOffsets(ticks).forEach((t, i) => expect(frameAtT(ticks, t)).toBe(i));
  });

  it('keeps an unreadable tick_ts on the moment before it', () => {
    const bad = [ticks[0], { tick_ts: 'garbage' }, ticks[2]];
    expect(frameOffsets(bad)).toEqual([0, 0, 3600]);
  });

  it('takes only whole non-negative seconds as t', () => {
    expect(parseT('0')).toBe(0);
    expect(parseT('3600')).toBe(3600);
    for (const raw of [null, '', '-1', '1.5', 'x', '1e3']) expect(parseT(raw)).toBeNull();
  });

  it('takes only the offered speeds, defaulting to 1x', () => {
    expect(parseSpeed('10')).toBe(10);
    expect(parseSpeed('60')).toBe(60);
    for (const raw of [null, '', '2', '0', 'fast']) expect(parseSpeed(raw)).toBe(1);
  });

  it('plays speed times faster than the 1x pace', () => {
    expect(playbackDwellMs(60, 1)).toBe(500);
    expect(playbackDwellMs(60, 10)).toBe(50);
    expect(playbackDwellMs(60, 60)).toBeCloseTo(500 / 60);
  });
});
