import { describe, expect, it } from 'vitest';
import type { EpicSummary, Loop } from '$lib/api';
import {
  STRIP_ROW_CAP,
  epicStripRows,
  loopStripRows,
  readStrips,
  stripKeysToFetch
} from './strip-reads';

const epic = (over: Partial<EpicSummary> = {}): EpicSummary => ({
  epic_ref: 'a',
  signal: 'progressing',
  counts: {},
  drain_total_usd: 0,
  last_tick_ts: null,
  interval_s: 30,
  change_count: 0,
  label: 'drain spend',
  ...over
});

const loop = (over: Partial<Loop> = {}): Loop => ({
  loop_id: 'L1',
  source: 'issue',
  issue_num: 1,
  repo: null,
  goal_text: 'make CI green',
  runner_name: 'claude-code',
  model: '',
  drive_mode: 'delegated',
  status: 'pursuing',
  status_reason: '',
  needs_human: false,
  stale: false,
  budget_usd: null,
  spent_usd: 0,
  wall_clock_secs: 0,
  max_steps: 200,
  step_count: 3,
  created_at: null,
  updated_at: '2026-10-07T00:00:00+00:00',
  finished_at: null,
  ...over
});

describe('which roster rows get a strip read', () => {
  it('asks once for a quiet row and never again', () => {
    const rows = epicStripRows([epic({ last_tick_ts: '2026-10-07T00:00:00Z' })]);
    const read = new Map<string, string>();
    expect(stripKeysToFetch(rows, read)).toEqual(['a']);
    read.set('a', rows[0].stamp);
    expect(stripKeysToFetch(rows, read)).toEqual([]);
  });

  it('asks again as soon as the epic ticks', () => {
    const read = new Map([['a', '2026-10-07T00:00:00Z']]);
    const ticked = epicStripRows([epic({ last_tick_ts: '2026-10-07T00:00:30Z' })]);
    expect(stripKeysToFetch(ticked, read)).toEqual(['a']);
  });

  it('prefers an epic START stamp, so a tick in flight redraws', () => {
    const rows = epicStripRows([
      epic({ last_tick_ts: '2026-10-07T00:00:00Z', last_tick_started_at: '2026-10-07T00:00:30Z' })
    ]);
    expect(rows[0].stamp).toContain('2026-10-07T00:00:30Z');
    expect(rows[0].stamp).not.toContain('2026-10-07T00:00:00Z');
    expect(stripKeysToFetch(rows, new Map([['a', ':2026-10-07T00:00:00Z']]))).toEqual(['a']);
  });

  it('re-reads an epic whose driver ATTEMPTED a tick and got no further', () => {
    // Reviewer finding (PR #2438, round 3). Both instants are written by the
    // driver's `begin_epic_tick`, which `advance` reaches only past the
    // single-driver lock, the tool preflight and the non-empty-graph guard. A
    // row whose driver is locked out, failing preflight or finding an empty
    // graph therefore moves neither — and its strip, which exists to show
    // exactly those attempts, was never re-read again.
    const ticked = epic({
      last_tick_ts: '2026-10-07T00:00:00Z',
      last_tick_started_at: '2026-10-07T00:00:00Z',
      tick_attempt_stamp: '7.7'
    });
    const read = new Map([['a', epicStripRows([ticked])[0].stamp]]);
    expect(stripKeysToFetch(epicStripRows([ticked]), read)).toEqual([]);

    // One locked-out attempt: the attempt stamp is the only thing that moved.
    const attempted = { ...ticked, tick_attempt_stamp: '8.7' };
    expect(stripKeysToFetch(epicStripRows([attempted]), read)).toEqual(['a']);
    // And closing that attempt moves it again — the live slot gets an outcome.
    const closed = epicStripRows([{ ...ticked, tick_attempt_stamp: '8.8' }]);
    expect(stripKeysToFetch(closed, read)).toEqual(['a']);
  });

  it('still re-reads on a tick when the server sends no attempt stamp', () => {
    // A `kew serve` predating #2304 omits the field; the instants it does send
    // are a worse signal than the stamp but a better one than nothing.
    const read = new Map([['a', ':2026-10-07T00:00:00Z']]);
    const ticked = epicStripRows([epic({ last_tick_ts: '2026-10-07T00:00:30Z' })]);
    expect(stripKeysToFetch(ticked, read)).toEqual(['a']);
  });

  it('moves a loop stamp on a step, a status change or an update', () => {
    const base = loopStripRows([loop()])[0].stamp;
    expect(loopStripRows([loop({ step_count: 4 })])[0].stamp).not.toBe(base);
    expect(loopStripRows([loop({ status: 'achieved' })])[0].stamp).not.toBe(base);
    expect(loopStripRows([loop({ updated_at: '2026-10-07T00:01:00+00:00' })])[0].stamp).not.toBe(
      base
    );
    // A finished, untouched loop is read once and then left alone.
    expect(stripKeysToFetch(loopStripRows([loop()]), new Map([['L1', base]]))).toEqual([]);
  });

  it('is capped, so a seventy-row roster is not seventy requests a poll', () => {
    const rows = epicStripRows(
      Array.from({ length: 70 }, (_, i) => epic({ epic_ref: `e${i}` }))
    );
    expect(stripKeysToFetch(rows, new Map())).toHaveLength(STRIP_ROW_CAP);
    expect(stripKeysToFetch(rows, new Map(), 3)).toEqual(['e0', 'e1', 'e2']);
    expect(stripKeysToFetch(rows, new Map(), 0)).toEqual([]);
  });

  it('keys a never-driven epic on a stable empty stamp', () => {
    const rows = epicStripRows([epic()]);
    expect(stripKeysToFetch(rows, new Map([['a', rows[0].stamp]]))).toEqual([]);
    // Stable across calls, which is what stops the poll re-reading it.
    expect(epicStripRows([epic()])[0].stamp).toBe(rows[0].stamp);
  });
});

describe('landing a strip read', () => {
  /** A request whose answer the test hands over when it chooses. */
  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }
  const flush = () => new Promise((res) => setTimeout(res, 0));

  it('never lets an older response land over a newer one', async () => {
    const read = new Map<string, string>();
    const landed: Record<string, string> = {};
    const answers = [deferred<string>(), deferred<string>()];
    let asked = 0;
    const fetchStrip = () => answers[asked++].promise;
    const land = (key: string, strip: string) => {
      landed[key] = strip;
    };

    readStrips([{ key: 'a', stamp: 'A' }], read, fetchStrip, land);
    readStrips([{ key: 'a', stamp: 'B' }], read, fetchStrip, land);
    expect(asked).toBe(2);

    answers[1].resolve('strip at B');
    await flush();
    answers[0].resolve('strip at A');
    await flush();

    expect(landed).toEqual({ a: 'strip at B' });
    expect(read.get('a')).toBe('B');
    // Nothing to re-read: the strip held is the one read at the row's stamp.
    expect(stripKeysToFetch([{ key: 'a', stamp: 'B' }], read)).toEqual([]);
  });

  it('lets an older failure leave the newer claim alone', async () => {
    const read = new Map<string, string>();
    const answers = [deferred<string>(), deferred<string>()];
    let asked = 0;
    const fetchStrip = () => answers[asked++].promise;

    readStrips([{ key: 'a', stamp: 'A' }], read, fetchStrip, () => {});
    readStrips([{ key: 'a', stamp: 'B' }], read, fetchStrip, () => {});
    answers[0].reject(new Error('timed out'));
    await flush();

    expect(read.get('a')).toBe('B');
  });

  it('releases its own claim on failure so the next poll retries', async () => {
    const read = new Map<string, string>();
    const rows = [{ key: 'a', stamp: 'A' }];
    readStrips(
      rows,
      read,
      () => Promise.reject(new Error('down')),
      () => {}
    );
    await flush();

    expect(stripKeysToFetch(rows, read)).toEqual(['a']);
  });
});
