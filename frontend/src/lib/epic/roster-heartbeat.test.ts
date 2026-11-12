/**
 * #2844 N18 — the Epics row reads its heartbeat the way the epic page reads its
 * own: through `heartbeatFromTickLog` and one freshness window, off facts the
 * summary serves on every row.
 */
import { describe, expect, it } from 'vitest';
import type { EpicHeartbeat, EpicTickStrip } from '$lib/api';
import { heartbeatFromTickLog, lastTickTs } from '$lib/factory/tick-strip';
import { staleness } from './staleness';
import {
  NO_HEARTBEAT,
  epicWithTickLog,
  heartbeatFresh,
  heartbeatWord,
  signalReading,
  summaryAttempt
} from './roster';

const epic = {
  epic_ref: 'go-live',
  signal: 'progressing',
  last_tick_ts: null as string | null,
  last_tick_started_at: null as string | null,
  last_tick_duration_s: null as number | null,
  last_attempt_started_at: null as string | null,
  last_attempt_finished_at: null as string | null,
  interval_s: 60
};
const now = Date.parse('2026-10-07T00:01:00Z');

/** The page's word for the same driver: the heartbeat DTO through the tick
 * log it fetched, then `staleness()`. */
function pageWord(
  hb: EpicHeartbeat,
  log: EpicTickStrip[],
  at: number
): ReturnType<typeof heartbeatWord> {
  return heartbeatWord(hb.last_signal, staleness(heartbeatFromTickLog(hb, log), at).kind === 'fresh');
}

/** One attempt; an open one is a tick under way, past `begin_epic_tick`. */
const attempt = (started: string, finished: string | null): EpicTickStrip => ({
  id: 7,
  ts_started: started,
  ts_finished: finished,
  outcome: finished === null ? null : 'nothing',
  signal: 'progressing',
  n_dispatched: 0,
  n_merged: 0,
  n_held: 0,
  n_stuck: 0,
  journal_tick_id: null,
  error_class: null,
  reached_tick: true
});

describe('epicWithTickLog', () => {
  it('turns a journal-quiet row live off the newest attempt the summary serves', () => {
    expect(signalReading(epic, now).note).toBe(NO_HEARTBEAT);
    const read = epicWithTickLog({
      ...epic,
      last_attempt_started_at: '2026-10-07T00:00:30Z',
      last_attempt_finished_at: '2026-10-07T00:00:32Z'
    });
    expect(read.last_tick_started_at).toBe('2026-10-07T00:00:30Z');
    expect(read.last_tick_duration_s).toBe(2);
    expect(signalReading(read, now)).toMatchObject({ halo: true, note: null });
  });

  it('keeps a newer served stamp over an older attempt', () => {
    const served = {
      ...epic,
      last_tick_started_at: '2026-10-07T00:00:50Z',
      last_attempt_started_at: '2026-10-07T00:00:10Z'
    };
    expect(epicWithTickLog(served)).toBe(served);
  });

  it('returns a row from a server that serves no attempt as it came', () => {
    expect(epicWithTickLog(epic)).toBe(epic);
    expect(summaryAttempt(epic)).toEqual([]);
  });

  it('does not invent a heartbeat from an old attempt', () => {
    const read = epicWithTickLog({ ...epic, last_attempt_started_at: '2026-10-06T00:00:00Z' });
    expect(signalReading(read, now).note).toBe(NO_HEARTBEAT);
  });
});

describe('the row and the epic page agree', () => {
  const started = '2026-10-07T00:00:00Z';
  const hb: EpicHeartbeat = {
    last_tick_started_at: '2026-10-06T23:50:00Z',
    last_tick_ts: '2026-10-06T23:50:01Z',
    last_tick_duration_s: 1,
    tick_count: 9,
    interval_s: 60,
    last_signal: 'progressing',
    change_count: 0
  };
  /** The row: the summary's own fields, and no strip at all — the row past
   * the strip cap the review found reading "no heartbeat". */
  const rowWord = (finished: string | null, at: number) => {
    const row = epicWithTickLog({
      ...epic,
      last_tick_started_at: hb.last_tick_started_at,
      last_tick_ts: hb.last_tick_ts,
      last_tick_duration_s: hb.last_tick_duration_s,
      last_attempt_started_at: started,
      last_attempt_finished_at: finished,
      last_attempt_reached_tick: true
    });
    return heartbeatWord(row.signal, heartbeatFresh(row, at));
  };
  const cases: [string, string | null, number][] = [
    ['2.5 intervals after an attempt still running', null, 150],
    ['3 intervals, inclusive', null, 180],
    ['just past 3 intervals', null, 181],
    ['a long finished attempt (100s) widens the window to 260s', '2026-10-07T00:01:40Z', 250],
    ['past the widened window', '2026-10-07T00:01:40Z', 261]
  ];
  for (const [name, finished, ageS] of cases) {
    it(name, () => {
      const at = Date.parse(started) + ageS * 1000;
      const page = pageWord(hb, [attempt(started, finished)], at);
      expect(rowWord(finished, at)).toBe(page);
    });
  }

  it('reads live and no heartbeat where the window says so', () => {
    const at = (s: number) => Date.parse(started) + s * 1000;
    expect(rowWord(null, at(150))).toBe('live');
    expect(rowWord(null, at(181))).toBe(NO_HEARTBEAT);
    expect(rowWord('2026-10-07T00:01:40Z', at(250))).toBe('live');
  });
});

describe('heartbeatWord', () => {
  it('says live or no heartbeat only for a signal that claims live', () => {
    expect(heartbeatWord('progressing', true)).toBe('live');
    expect(heartbeatWord('progressing', false)).toBe(NO_HEARTBEAT);
    expect(heartbeatWord('stalled', true)).toBeNull();
    expect(heartbeatWord('awaiting_merges', false)).toBeNull();
    expect(heartbeatWord(null, true)).toBeNull();
  });
});


describe('a refused attempt is no beat (#2882)', () => {
  // The last real tick ten minutes old at a 60 s cadence — the driver the
  // server reads `gone` — then a fresh attempt the driver was turned away on.
  const lastTick = '2026-10-07T00:00:00Z';
  const refusedAt = '2026-10-07T00:10:00Z';
  const refusedEnd = '2026-10-07T00:10:01Z';
  const at = Date.parse('2026-10-07T00:10:05Z');
  const hb: EpicHeartbeat = {
    last_tick_started_at: lastTick,
    last_tick_ts: '2026-10-07T00:00:01Z',
    last_tick_duration_s: 1,
    tick_count: 9,
    interval_s: 60,
    last_signal: 'progressing',
    change_count: 0
  };

  for (const outcome of ['failed', 'locked_out', 'empty_graph', 'abandoned']) {
    it(`stays stale on the page and the list after a fresh ${outcome} attempt`, () => {
      const refused = { ...attempt(refusedAt, refusedEnd), outcome };
      const page = heartbeatFromTickLog(hb, [refused]);
      expect(page).toBe(hb);
      expect(staleness(page, at).kind).not.toBe('fresh');
      expect(pageWord(hb, [refused], at)).toBe(NO_HEARTBEAT);

      const row = epicWithTickLog({
        ...epic,
        last_tick_started_at: lastTick,
        last_tick_ts: hb.last_tick_ts,
        last_tick_duration_s: 1,
        last_attempt_started_at: refusedAt,
        last_attempt_finished_at: refusedEnd,
        last_attempt_outcome: outcome
      });
      expect(row.last_tick_started_at).toBe(lastTick);
      expect(heartbeatWord(row.signal, heartbeatFresh(row, at))).toBe(NO_HEARTBEAT);
    });
  }

  it('still takes a newer attempt that reached a tick and changed nothing', () => {
    const quiet = attempt(refusedAt, refusedEnd);
    expect(pageWord(hb, [quiet], at)).toBe('live');
    const row = epicWithTickLog({
      ...epic,
      last_tick_started_at: lastTick,
      last_attempt_started_at: refusedAt,
      last_attempt_finished_at: refusedEnd,
      last_attempt_outcome: 'nothing'
    });
    expect(heartbeatWord(row.signal, heartbeatFresh(row, at))).toBe('live');
  });

  it('skips a refused attempt for an older one that reached a tick', () => {
    const reached = attempt('2026-10-07T00:09:30Z', '2026-10-07T00:09:31Z');
    const refused = { ...attempt(refusedAt, refusedEnd), id: 8, outcome: 'locked_out' };
    const page = heartbeatFromTickLog(hb, [reached, refused]);
    expect(page?.last_tick_started_at).toBe('2026-10-07T00:09:30Z');
  });
});

describe('an open attempt is a beat only once it reached its tick (#2882)', () => {
  // The real heartbeat at 00:00 on a 60 s cadence; an attempt opened at 00:10
  // that is still blocked in preflight — no tick ran, so the driver is gone.
  const lastTick = '2026-10-07T00:00:00Z';
  const openedAt = '2026-10-07T00:10:00Z';
  const at = Date.parse('2026-10-07T00:10:05Z');
  const hb: EpicHeartbeat = {
    last_tick_started_at: lastTick,
    last_tick_ts: '2026-10-07T00:00:01Z',
    last_tick_duration_s: 1,
    tick_count: 9,
    interval_s: 60,
    last_signal: 'progressing',
    change_count: 0
  };
  const row = (reached: boolean | undefined) =>
    epicWithTickLog({
      ...epic,
      last_tick_started_at: lastTick,
      last_tick_ts: hb.last_tick_ts,
      last_tick_duration_s: 1,
      last_attempt_started_at: openedAt,
      last_attempt_finished_at: null,
      last_attempt_outcome: null,
      ...(reached === undefined ? {} : { last_attempt_reached_tick: reached })
    });

  for (const [name, reached] of [
    ['still before its tick', false],
    ['from a server that does not say', undefined]
  ] as const) {
    it(`stays stale on the page and the list for an open attempt ${name}`, () => {
      const open = { ...attempt(openedAt, null), reached_tick: reached };
      const page = heartbeatFromTickLog(hb, [open]);
      expect(page).toBe(hb);
      expect(pageWord(hb, [open], at)).toBe(NO_HEARTBEAT);

      const listed = row(reached);
      expect(listed.last_tick_started_at).toBe(lastTick);
      expect(heartbeatWord(listed.signal, heartbeatFresh(listed, at))).toBe(NO_HEARTBEAT);
    });
  }

  it('reads live on both once the open attempt reached its tick', () => {
    expect(pageWord(hb, [attempt(openedAt, null)], at)).toBe('live');
    const listed = row(true);
    expect(listed.last_tick_started_at).toBe(openedAt);
    expect(heartbeatWord(listed.signal, heartbeatFresh(listed, at))).toBe('live');
  });
});

describe('the last tick is the last beat (#2882)', () => {
  // A real tick ten minutes old, then a fresh attempt that ran no tick: the
  // "last tick" both surfaces print must not read 10s ago beside a heartbeat
  // reading none.
  const journalTs = '2026-10-06T23:50:00Z';
  const tickAt = '2026-10-07T00:00:00Z';
  const freshAt = '2026-10-07T00:09:50Z';
  const reached = attempt(tickAt, '2026-10-07T00:00:01Z');

  const refusals: [string, EpicTickStrip][] = [
    ...['failed', 'locked_out', 'empty_graph', 'abandoned'].map(
      (outcome): [string, EpicTickStrip] => [
        `a ${outcome} attempt`,
        { ...attempt(freshAt, '2026-10-07T00:09:51Z'), id: 8, outcome }
      ]
    ),
    ['an open attempt before its tick', { ...attempt(freshAt, null), id: 8, reached_tick: false }]
  ];
  for (const [name, fresh] of refusals) {
    it(`keeps the older tick's time over ${name}, on the page and the list`, () => {
      // The page: the journal's stamp and the strip it fetched.
      expect(lastTickTs(journalTs, [reached, fresh])).toBe(tickAt);
      expect(lastTickTs(journalTs, [fresh])).toBe(journalTs);
      // The list: the summary's newest attempt ahead of the row's strip.
      const listed = summaryAttempt({
        ...epic,
        last_attempt_started_at: fresh.ts_started,
        last_attempt_finished_at: fresh.ts_finished,
        last_attempt_outcome: fresh.outcome,
        last_attempt_reached_tick: fresh.reached_tick
      });
      expect(lastTickTs(journalTs, [...listed, reached])).toBe(tickAt);
    });
  }

  it('takes a fresh attempt that reached its tick', () => {
    expect(lastTickTs(journalTs, [reached, attempt(freshAt, null)])).toBe(freshAt);
    expect(lastTickTs(journalTs, [reached, attempt(freshAt, '2026-10-07T00:09:51Z')])).toBe(freshAt);
  });
});
