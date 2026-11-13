/**
 * #2299 — overlap batching. What is checked here: that marks merge exactly
 * when their drawn extents touch and split when they do not, that a batch
 * wears its worst member's state, and that the popover's order is kew's
 * severity order with the quiet remainder behind a rule.
 */
import { describe, expect, it } from 'vitest';
import {
  BATCH_LIST_CAP,
  BATCH_SEVERITIES,
  MIN_MARK_W,
  batchEntries,
  batchMarks,
  batchSeverity,
  severityRank,
  worseOf,
  type BatchMark,
  type BatchSeverity
} from './batch';
import type { AxisWindow } from './axis';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

/** An hour over 60 pixels: one pixel a minute, so the arithmetic is readable. */
const WINDOW: AxisWindow = { startMs: 0, endMs: HOUR, width: 60 };

const mark = (
  minutes: number,
  severity: BatchSeverity = 'quiet',
  name = `at ${minutes}`,
  lengthMinutes = 0
): BatchMark<string> => ({
  startMs: minutes * MINUTE,
  endMs: (minutes + lengthMinutes) * MINUTE,
  severity,
  item: name
});

describe('batching', () => {
  it('merges marks whose drawn extents overlap', () => {
    // Four minutes apart is four pixels apart, and a mark is MIN_MARK_W wide:
    // the second starts exactly where the first ends.
    const batches = batchMarks([mark(10), mark(14), mark(18)], WINDOW);
    expect(batches).toHaveLength(1);
    expect(batches[0].items.map((m) => m.item)).toEqual(['at 10', 'at 14', 'at 18']);
    expect(batches[0].startMs).toBe(10 * MINUTE);
    expect(batches[0].endMs).toBe(18 * MINUTE);
    expect(batches[0].x).toBe(10);
    // The widened extent: the left edge to the far mark's own minimum width.
    expect(batches[0].width).toBe(12);
  });

  it('splits marks that are drawn clear of each other', () => {
    const batches = batchMarks([mark(10), mark(20), mark(30)], WINDOW);
    expect(batches).toHaveLength(3);
    for (const batch of batches) {
      expect(batch.items).toHaveLength(1);
      expect(batch.width).toBe(MIN_MARK_W);
      expect(batch.startMs).toBe(batch.endMs);
    }
    expect(batches.map((b) => b.x)).toEqual([10, 20, 30]);
  });

  it('splits where one more pixel of zoom pulls them apart', () => {
    const marks = [mark(10), mark(15)];
    // Five pixels apart with a 4px mark: clear of each other.
    expect(batchMarks(marks, WINDOW)).toHaveLength(2);
    // The same marks, half the width: now 2.5px apart, and one mark.
    expect(batchMarks(marks, { ...WINDOW, width: 30 })).toHaveLength(1);
    // And a wider minimum mark closes the gap at the original zoom.
    expect(batchMarks(marks, WINDOW, { minWidthPx: 8 })).toHaveLength(1);
  });

  it('takes the union of the times and the widest extent', () => {
    // A long mark with a short one inside it.
    const batches = batchMarks([mark(10, 'quiet', 'long', 20), mark(12)], WINDOW);
    expect(batches).toHaveLength(1);
    expect(batches[0].startMs).toBe(10 * MINUTE);
    expect(batches[0].endMs).toBe(30 * MINUTE);
    expect(batches[0].x).toBe(10);
    expect(batches[0].width).toBe(20);
  });

  it('sorts by left edge whatever order the marks arrive in', () => {
    const batches = batchMarks([mark(30), mark(10), mark(31), mark(20)], WINDOW);
    expect(batches.map((b) => b.x)).toEqual([10, 20, 30]);
    expect(batches[2].items.map((m) => m.item)).toEqual(['at 30', 'at 31']);
  });

  it('has nothing to draw for no marks', () => {
    expect(batchMarks([], WINDOW)).toEqual([]);
  });

  it('leaves the marks it was handed alone', () => {
    const marks = [mark(30), mark(10)];
    batchMarks(marks, WINDOW);
    expect(marks.map((m) => m.item)).toEqual(['at 30', 'at 10']);
  });
});

describe('severity', () => {
  it('runs failed, then needs-you, then did something, then quiet', () => {
    expect([...BATCH_SEVERITIES]).toEqual(['failed', 'attention', 'did', 'quiet']);
    for (let i = 1; i < BATCH_SEVERITIES.length; i++)
      expect(severityRank(BATCH_SEVERITIES[i])).toBeGreaterThan(
        severityRank(BATCH_SEVERITIES[i - 1])
      );
    expect(worseOf('quiet', 'failed')).toBe('failed');
    expect(worseOf('attention', 'did')).toBe('attention');
    expect(worseOf('did', 'did')).toBe('did');
  });

  it('is read off the one status vocabulary the console has', () => {
    expect(batchSeverity('failed')).toBe('failed');
    expect(batchSeverity('flawed')).toBe('failed');
    expect(batchSeverity('held')).toBe('attention');
    expect(batchSeverity('awaiting_publication')).toBe('attention');
    expect(batchSeverity('in_progress')).toBe('did');
    expect(batchSeverity('done')).toBe('did');
    expect(batchSeverity('success', 'run')).toBe('did');
    // Quiet: a step still ahead, a decision, a word nobody has mapped.
    for (const status of ['todo', 'queued', 'cancelled', 'blocked', 'who_knows', '', null])
      expect(batchSeverity(status), String(status)).toBe('quiet');
  });

  it('shows the worst member, wherever it sits in the batch', () => {
    const batch = batchMarks(
      [mark(10, 'quiet'), mark(12, 'failed'), mark(14, 'did')],
      WINDOW
    );
    expect(batch).toHaveLength(1);
    expect(batch[0].severity).toBe('failed');
    // ...and the same marks without the failure drop to the next one down.
    const milder = batchMarks([mark(10, 'quiet'), mark(12, 'did')], WINDOW);
    expect(milder[0].severity).toBe('did');
  });
});

describe('the popover of a batch', () => {
  it('lists failures first, then what kew did, then a rule, then the quiet', () => {
    const entries = batchEntries([
      mark(10, 'quiet', 'queued'),
      mark(11, 'did', 'ran'),
      mark(12, 'failed', 'broke'),
      mark(13, 'attention', 'held'),
      mark(14, 'quiet', 'todo')
    ]);
    expect(entries.map((e) => (e.kind === 'item' ? e.mark.item : e.kind))).toEqual([
      'broke',
      'held',
      'ran',
      'rule',
      'queued',
      'todo'
    ]);
  });

  it('keeps time order inside a severity', () => {
    // The marks arrive from `batchMarks` in left-edge order, and the severity
    // sort is stable, so it does not reshuffle a group.
    const batches = batchMarks([mark(12, 'did', 'second'), mark(10, 'did', 'first')], WINDOW);
    expect(batches).toHaveLength(1);
    const entries = batchEntries(batches[0].items);
    expect(entries.map((entry) => (entry.kind === 'item' ? entry.mark.item : entry.kind))).toEqual([
      'first',
      'second'
    ]);
  });

  it('draws no rule where there is nothing on one side of it', () => {
    const quiet = batchEntries([mark(10, 'quiet'), mark(11, 'quiet')]);
    expect(quiet.every((entry) => entry.kind === 'item')).toBe(true);
    const loud = batchEntries([mark(10, 'failed'), mark(11, 'did')]);
    expect(loud.every((entry) => entry.kind === 'item')).toBe(true);
  });

  it('stops counting at fifty and says how many it did not show', () => {
    const many = Array.from({ length: 63 }, (_, i) => mark(i, 'did', `run ${i}`));
    const entries = batchEntries(many);
    expect(entries.filter((entry) => entry.kind === 'item')).toHaveLength(BATCH_LIST_CAP);
    const last = entries[entries.length - 1];
    expect(last).toEqual({ kind: 'more', hidden: 13, label: '13 more not shown' });
  });

  it('shows the worst fifty, not the first fifty', () => {
    const many = [
      ...Array.from({ length: 60 }, (_, i) => mark(i, 'quiet', `quiet ${i}`)),
      mark(61, 'failed', 'the failure')
    ];
    const entries = batchEntries(many, 3);
    expect(entries[0]).toMatchObject({ kind: 'item', severity: 'failed' });
    expect(entries.at(-1)).toMatchObject({ kind: 'more', hidden: 58 });
  });

  it('counts a mark it cannot show at all', () => {
    expect(batchEntries([mark(10, 'did')], 0)).toEqual([
      { kind: 'more', hidden: 1, label: '1 more not shown' }
    ]);
  });
});
