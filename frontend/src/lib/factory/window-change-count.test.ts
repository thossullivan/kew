/**
 * #3054 N1 — the Epics list's CHANGE counts the changes inside the change
 * strip's window, off the strip's own read, not the journal's row count.
 *
 * Live, CHANGE read 4554 and 2725: the journal wrote a row on every tick
 * before #2879, and the served `change_count` counts rows.
 */
import { describe, expect, it } from 'vitest';
import type { EpicTickStrip } from '$lib/api';
import { CHANGE_WINDOW, changeSlots, windowChangeCount } from './tick-strip';

let nextId = 1;
const tick = (over: Partial<EpicTickStrip> = {}): EpicTickStrip => {
  const id = nextId++;
  const started = new Date(Date.parse('2026-10-07T00:00:00Z') + id * 30_000).toISOString();
  return {
    id,
    ts_started: started,
    ts_finished: started,
    outcome: 'nothing',
    signal: 'progressing',
    n_dispatched: 0,
    n_merged: 0,
    n_held: 0,
    n_stuck: 0,
    journal_tick_id: null,
    error_class: null,
    ...over
  };
};

/** `n` per-tick rows that changed nothing: each carries the id of the change
 * before it, as the journal's dedup hands back the row it already has. */
const unchanged = (n: number, journalId: number | null) =>
  Array.from({ length: n }, () => tick({ journal_tick_id: journalId }));

describe('windowChangeCount', () => {
  it('reports CHANGE 3 for 400 unchanged per-tick rows and 3 real changes inside the window', () => {
    const ticks = [
      ...unchanged(100, null),
      tick({ journal_tick_id: 7 }),
      ...unchanged(150, 7),
      tick({ journal_tick_id: 8 }),
      ...unchanged(100, 8),
      tick({ journal_tick_id: 9 }),
      ...unchanged(50, 9)
    ];
    expect(ticks).toHaveLength(403);
    expect(windowChangeCount(ticks)).toBe(3);
  });

  it('counts only the changes inside the strip window, the dots the row draws', () => {
    const ticks = Array.from({ length: CHANGE_WINDOW + 30 }, (_, i) => tick({ journal_tick_id: 100 + i }));
    const dots = changeSlots(ticks, CHANGE_WINDOW).filter((s) => s.kind === 'change');
    expect(windowChangeCount(ticks)).toBe(CHANGE_WINDOW);
    expect(windowChangeCount(ticks)).toBe(dots.length);
  });

  it('leaves out the failed ticks the window also holds', () => {
    const ticks = [tick({ journal_tick_id: 1 }), tick({ outcome: 'failed' }), tick({ journal_tick_id: 2 })];
    expect(windowChangeCount(ticks)).toBe(2);
  });

  it('is no reading while the strip is unread', () => {
    expect(windowChangeCount(undefined)).toBeNull();
    expect(windowChangeCount([])).toBe(0);
  });
});

describe('changeSlots alignment', () => {
  it('draws the marks from the left edge, oldest first, with the empty slots after them', () => {
    const slots = changeSlots([tick({ journal_tick_id: 1 }), tick({ journal_tick_id: 2 })], 16);
    expect(slots).toHaveLength(16);
    expect(slots.slice(0, 2).map((s) => s.item?.journal_tick_id)).toEqual([1, 2]);
    expect(slots.slice(2).every((s) => s.kind === 'pad')).toBe(true);
  });
});
