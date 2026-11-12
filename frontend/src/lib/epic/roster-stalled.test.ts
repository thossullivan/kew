/**
 * #3054 N14 — a stalled epic says why, and magenta is spent only where a
 * person is next.
 */
import { describe, expect, it } from 'vitest';
import { signalTone } from './progress';
import { NO_HEARTBEAT, NOTHING_READY, stalledReason } from './roster';

const now = Date.parse('2026-10-19T12:00:00Z');
const epic = (over: Record<string, unknown> = {}) => ({
  epic_ref: 'test-integrity',
  signal: 'stalled',
  last_tick_ts: '2026-10-19T11:59:30Z',
  interval_s: 60,
  ...over
});

describe('stalledReason', () => {
  it('names the people waiting, and their tone is magenta', () => {
    const reason = stalledReason(epic(), now, 10);
    expect(reason).toMatchObject({ note: '10 need you', personNext: true });
    expect(signalTone('stalled', reason!.personNext).color).toBe('var(--accent)');
  });

  it('says "no heartbeat" in amber when nobody waits and the driver is not beating', () => {
    const reason = stalledReason(epic({ last_tick_ts: '2026-10-18T00:00:00Z' }), now, 0);
    expect(reason).toMatchObject({ note: NO_HEARTBEAT, personNext: false });
    expect(reason!.hint).toContain('kew epic run test-integrity');
    expect(signalTone('stalled', reason!.personNext).color).toBe('var(--warn)');
  });

  it('says nothing is ready when nobody waits and the driver is beating', () => {
    const reason = stalledReason(epic(), now, 0);
    expect(reason).toMatchObject({ note: NOTHING_READY, personNext: false });
    expect(reason!.hint.length).toBeGreaterThan(0);
  });

  it('adds nothing to a signal that is not stalled', () => {
    for (const signal of ['progressing', 'complete', 'awaiting_merges', null]) {
      expect(stalledReason(epic({ signal }), now, 3)).toBeNull();
    }
  });
});
