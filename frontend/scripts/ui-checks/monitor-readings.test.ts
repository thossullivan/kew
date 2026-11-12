/**
 * The Monitor stream comparison's own test (#2921): it fails when it should,
 * without a browser. A drawn row is what `readStream` reads — the id its row
 * was drawn for, its time and the instant its title stamps — and an answer is
 * the events of one `/api/audit` read, newest first. A stream drawn in the
 * answer's order passes; one drawn oldest-first, one that draws a row twice,
 * one that drops a row, and two events of one second drawn swapped or one
 * drawn in the other's place each fail.
 */
import { describe, expect, it } from 'vitest';
import { streamOrderFault, summaryFault } from './monitor-readings.mjs';

const T0 = Date.parse('2026-10-15T12:00:00Z');

/** An answer of `n` events, newest first: ids n..1, a minute apart. */
const answer = (n: number) =>
  Array.from({ length: n }, (_, k) => {
    const id = n - k;
    return { id, timestamp: new Date(T0 + id * 60_000).toISOString() };
  });

/** The rows a stream draws for these events, in the order given. */
const rows = (events: { id: number; timestamp: string }[]) =>
  events.map((e) => ({ id: e.id, time: e.timestamp.slice(11, 16), ms: Date.parse(e.timestamp) + 400 }));

describe('streamOrderFault', () => {
  const served = answer(3);

  it('passes a stream drawn in the answer order', () => {
    expect(streamOrderFault(rows(served), [served])).toBeNull();
  });

  it('fails a stream drawn oldest-first', () => {
    expect(streamOrderFault(rows([...served].reverse()), [served])).toBe(
      'the stream draws ids [1,2,3], not the answered [3,2,1]'
    );
  });

  it('fails a stream that draws a row twice', () => {
    expect(streamOrderFault(rows([served[0], ...served]), [served])).toBe(
      'the stream draws ids [3,3,2,1], not the answered [3,2,1]'
    );
  });

  it('fails a stream that drops a row', () => {
    expect(streamOrderFault(rows(served.slice(1)), [served])).toBe(
      'the stream draws ids [2,1], not the answered [3,2,1]'
    );
  });

  it('fails when the page got no answer at all', () => {
    expect(streamOrderFault(rows(served), [])).toMatch(/no \/api\/audit answer/);
  });

  describe('two events stamped in one second', () => {
    const at = new Date(T0).toISOString();
    const same = [
      { id: 102, timestamp: at },
      { id: 101, timestamp: at }
    ];

    it('passes them drawn in the answer order', () => {
      expect(streamOrderFault(rows(same), [same])).toBeNull();
    });

    it('fails them drawn swapped', () => {
      expect(streamOrderFault(rows([same[1], same[0]]), [same])).toBe(
        'the stream draws ids [101,102], not the answered [102,101]'
      );
    });

    it('fails one drawn in the place of the other', () => {
      expect(streamOrderFault(rows([same[0], same[0]]), [same])).toBe(
        'the stream draws ids [102,102], not the answered [102,101]'
      );
    });
  });

  it('passes a stream that matches an earlier answer than the last', () => {
    const later = answer(4);
    expect(streamOrderFault(rows(served), [served, later])).toBeNull();
    expect(streamOrderFault(rows([...served].reverse()), [served, later])).toBe(
      'the stream draws ids [1,2,3], not the answered [4,3,2,1]'
    );
  });
});

/**
 * The Needs you summary's reading (#3052 N3): it passes a count of kinds that
 * adds up to the figure, and fails one that does not, one that names no kind,
 * and a cut sentence.
 */
describe('summaryFault', () => {
  it('passes the queue by kind when it adds up to the figure', () => {
    expect(summaryFault('1 merge decision · 1 hold', '2')).toBeNull();
    expect(summaryFault('13 merge decisions', '13')).toBeNull();
    expect(summaryFault('2 merge decisions · 1 hold · 1 more', '4')).toBeNull();
  });

  it('fails a summary that miscounts the figure', () => {
    expect(summaryFault('1 merge decision · 1 hold', '3')).toMatch(/counts 2 rows under a figure of 3/);
  });

  it('fails one that names no kind, and a cut sentence', () => {
    expect(summaryFault('2 more', '2')).toMatch(/names no kind/);
    expect(summaryFault('PR #2059 is open —…', '2')).toMatch(/not a count of kinds/);
  });
});
