import { describe, expect, it } from 'vitest';
import { staleness } from './staleness';
import type { EpicHeartbeat } from '$lib/api';

const NOW = Date.parse('2026-07-02T12:00:00.000Z');
const ago = (ms: number) => new Date(NOW - ms).toISOString();

const hb = (overrides: Partial<EpicHeartbeat>): EpicHeartbeat => ({
  last_tick_started_at: ago(0),
  last_tick_ts: ago(0),
  last_tick_duration_s: 5,
  tick_count: 1,
  interval_s: 60,
  last_signal: 'progressing',
  change_count: 1,
  ...overrides
});

describe('staleness — failure-modes table', () => {
  it('fresh: within threshold, signal progressing', () => {
    // threshold = max(3*60, 60+2*5) = max(180, 70) = 180; age = 30 <= 180
    const s = staleness(hb({ last_tick_started_at: ago(30_000) }), NOW);
    expect(s.kind).toBe('fresh');
  });

  it('stale: exceeded threshold, last_signal progressing -> "driver last seen"', () => {
    // age = 200 > 180
    const s = staleness(hb({ last_tick_started_at: ago(200_000) }), NOW);
    expect(s).toMatchObject({ kind: 'stale' });
    if (s.kind === 'stale') {
      expect(s.lastSeen).toBe(ago(200_000));
      expect(s.ageS).toBeCloseTo(200, 0);
    }
  });

  it('complete: last_signal complete -> NEVER stale, even 10-day-old heartbeat', () => {
    const tenDaysMs = 10 * 24 * 60 * 60 * 1000;
    const s = staleness(
      hb({ last_signal: 'complete', last_tick_started_at: ago(tenDaysMs), last_tick_ts: ago(tenDaysMs) }),
      NOW
    );
    expect(s.kind).toBe('complete');
    if (s.kind === 'complete') {
      expect(s.finishedAt).toBe(ago(tenDaysMs));
    }
  });

  it('stalled: last_signal stalled -> "by design", not "driver gone"', () => {
    const s = staleness(hb({ last_signal: 'stalled', last_tick_started_at: ago(999_999_000) }), NOW);
    expect(s.kind).toBe('stalled');
  });

  it('one-shot: interval_s null -> no alive/gone claim', () => {
    const s = staleness(hb({ interval_s: null, last_tick_started_at: ago(500_000) }), NOW);
    expect(s).toMatchObject({ kind: 'oneshot' });
    if (s.kind === 'oneshot') expect(s.ageS).toBeCloseTo(500, 0);
  });

  it('long tick: past 3*interval but within interval+2*duration -> still fresh (max clause)', () => {
    // threshold = max(3*60, 60+2*100) = max(180, 260) = 260; age = 200 <= 260
    const s = staleness(
      hb({ last_tick_duration_s: 100, last_tick_started_at: ago(200_000) }),
      NOW
    );
    expect(s.kind).toBe('fresh');
  });

  it('null heartbeat -> unknown', () => {
    expect(staleness(null, NOW)).toEqual({ kind: 'unknown' });
  });

  it('heartbeat with no last_tick_started_at -> unknown', () => {
    expect(staleness(hb({ last_tick_started_at: null }), NOW)).toEqual({ kind: 'unknown' });
  });
});
