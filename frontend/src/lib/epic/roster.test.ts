/**
 * #2206 — the Epics list's two shelves.
 *
 * Sixty-six epics, most last ticked two months ago, read as one undifferentiated
 * roster: an epic that finished in July sat above one that ticked a minute ago,
 * because the list was sorted by ref. The split and the order are decided here
 * rather than in the page, because what counts as finished is a *reading* of
 * two facts (the driver's terminal signal and the operator's archive decision)
 * and a reading printed in markup cannot be tested.
 */
import { describe, expect, it } from 'vitest';
import { signalTone } from './progress';
import { staleness } from './staleness';
import {
  epicRoster,
  isRetired,
  needsYouHref,
  NO_HEARTBEAT,
  nonZeroCounts,
  signalReading,
  type HeartbeatEpic,
  type RosterEpic
} from './roster';

const epic = (over: Partial<RosterEpic> & { epic_ref: string }): RosterEpic => ({
  signal: 'progressing',
  last_tick_ts: '2026-09-01T00:00:00Z',
  archived: false,
  ...over
});

describe('isRetired', () => {
  it('reads a complete epic as finished', () => {
    expect(isRetired(epic({ epic_ref: 'a', signal: 'complete' }))).toBe(true);
  });

  it('reads an archived epic as finished whatever its signal says', () => {
    for (const signal of ['progressing', 'awaiting_merges', 'stalled', null])
      expect(isRetired(epic({ epic_ref: 'a', signal, archived: true }))).toBe(true);
  });

  it('leaves a stalled or awaiting epic active — it still needs someone', () => {
    for (const signal of ['progressing', 'awaiting_merges', 'stalled', 'blocked', null])
      expect(isRetired(epic({ epic_ref: 'a', signal }))).toBe(false);
  });

  it('treats a missing `archived` field as not archived', () => {
    // A `kew serve` predating #2206 omits the field entirely.
    expect(isRetired({ epic_ref: 'a', signal: 'progressing', last_tick_ts: null })).toBe(
      false
    );
  });
});

describe('epicRoster', () => {
  it('splits active from finished and archived', () => {
    const { active, finished } = epicRoster([
      epic({ epic_ref: 'live' }),
      epic({ epic_ref: 'done', signal: 'complete' }),
      epic({ epic_ref: 'retired', archived: true })
    ]);
    expect(active.map((e) => e.epic_ref)).toEqual(['live']);
    expect(finished.map((e) => e.epic_ref)).toEqual(['done', 'retired']);
  });

  it('sorts each shelf by last tick, newest first', () => {
    const { active } = epicRoster([
      epic({ epic_ref: 'old', last_tick_ts: '2026-07-01T00:00:00Z' }),
      epic({ epic_ref: 'new', last_tick_ts: '2026-09-30T12:00:00Z' }),
      epic({ epic_ref: 'mid', last_tick_ts: '2026-08-15T00:00:00Z' })
    ]);
    expect(active.map((e) => e.epic_ref)).toEqual(['new', 'mid', 'old']);
  });

  it('sorts the finished shelf the same way', () => {
    const { finished } = epicRoster([
      epic({ epic_ref: 'older', signal: 'complete', last_tick_ts: '2026-01-01T00:00:00Z' }),
      epic({ epic_ref: 'newer', archived: true, last_tick_ts: '2026-06-01T00:00:00Z' })
    ]);
    expect(finished.map((e) => e.epic_ref)).toEqual(['newer', 'older']);
  });

  it('puts an epic with no tick last, not first', () => {
    // `last_tick_ts: null` is "never driven", not "driven at the epoch" — a
    // Date.parse(null) coerced to 0 would have sorted it below nothing at all,
    // and `undefined`/NaN would have made the comparator non-transitive.
    const { active } = epicRoster([
      epic({ epic_ref: 'never', last_tick_ts: null }),
      epic({ epic_ref: 'ticked', last_tick_ts: '2026-01-01T00:00:00Z' }),
      epic({ epic_ref: 'unparseable', last_tick_ts: 'not a date' })
    ]);
    expect(active.map((e) => e.epic_ref)).toEqual([
      'ticked',
      'never',
      'unparseable'
    ]);
  });

  it('breaks a tie by ref so the order is stable across polls', () => {
    const { active } = epicRoster([
      epic({ epic_ref: 'b' }),
      epic({ epic_ref: 'a' }),
      epic({ epic_ref: 'c' })
    ]);
    expect(active.map((e) => e.epic_ref)).toEqual(['a', 'b', 'c']);
  });

  it('never mutates the list it was handed', () => {
    const input = [epic({ epic_ref: 'b' }), epic({ epic_ref: 'a' })];
    epicRoster(input);
    expect(input.map((e) => e.epic_ref)).toEqual(['b', 'a']);
  });

  it('answers an empty roster with two empty shelves', () => {
    expect(epicRoster([])).toEqual({ active: [], finished: [] });
  });
});

/**
 * #2283 (D16) — the live claim has to be earned.
 *
 * The screenshot that filed this: three epics with a cyan halo on
 * "progressing" while the Last tick column beside them said "no ticks yet".
 * DESIGN.md: show live (cyan, halo) only with a fresh heartbeat — the epic
 * page's window, max(3x interval, interval + 2x the last tick's duration),
 * since #2844 N18; otherwise the signal goes quiet and the row says
 * "no heartbeat".
 */
describe('signalReading', () => {
  const live = (over: Partial<HeartbeatEpic> = {}): HeartbeatEpic => ({
    signal: 'progressing',
    last_tick_ts: '2026-10-05T12:00:00Z',
    interval_s: 60,
    ...over
  });
  const now = Date.parse('2026-10-05T12:00:30Z');

  it('keeps cyan and the halo while the heartbeat is fresh', () => {
    expect(signalReading(live(), now)).toEqual({
      color: 'var(--accent-2)',
      halo: true,
      note: null
    });
  });

  it('counts the window as three intervals, inclusive', () => {
    const edge = Date.parse('2026-10-05T12:03:00Z');
    expect(signalReading(live(), edge).halo).toBe(true);
    expect(signalReading(live(), edge + 1).halo).toBe(false);
  });

  it('drops the live claim, and says so, past the window', () => {
    expect(signalReading(live(), Date.parse('2026-10-05T12:05:00Z'))).toEqual({
      color: 'var(--text-3)',
      halo: false,
      note: NO_HEARTBEAT
    });
  });

  it('drops it on an epic that has never ticked — the filed screenshot', () => {
    expect(signalReading(live({ last_tick_ts: null }), now).note).toBe(NO_HEARTBEAT);
  });

  it('drops it where no window exists to be fresh in', () => {
    // A one-shot drive (`interval_s: null`) makes no alive-or-gone claim
    // possible, which is not the same as being alive. Same for an interval
    // that is not a duration, and for a timestamp that will not parse.
    for (const over of [{ interval_s: null }, { interval_s: 0 }, { last_tick_ts: 'soon' }])
      expect(signalReading(live(over), now).note).toBe(NO_HEARTBEAT);
  });

  it('reads liveness off the tick START, so a long tick is not a dead driver', () => {
    // `last_tick_ts` only lands when a tick FINISHES, so a long tick ages the
    // finish stamp out of the window: read off the finish stamp, this row said
    // "no heartbeat" for exactly as long as the driver was busy, while
    // `driver_liveness` (off `last_tick_started_at`) called the same driver
    // live. 150s into a 60s cadence is such a tick — the start stamp is 150s
    // old (inside 3x), the finish stamp of the tick before it is 240s old
    // (outside).
    const busy = live({
      last_tick_started_at: '2026-10-05T12:00:00Z',
      last_tick_ts: '2026-10-05T11:56:00Z'
    });
    expect(signalReading(busy, Date.parse('2026-10-05T12:02:30Z'))).toEqual({
      color: 'var(--accent-2)',
      halo: true,
      note: null
    });
    // The finish stamp alone, on the same facts, is the bug being fixed.
    expect(
      signalReading(
        live({ last_tick_ts: busy.last_tick_ts }),
        Date.parse('2026-10-05T12:02:30Z')
      ).note
    ).toBe(NO_HEARTBEAT);
    // And the start stamp is the one that ages: a tick that began outside the
    // window is still gone, however recently an earlier tick finished.
    const hung = live({
      last_tick_started_at: '2026-10-05T11:50:00Z',
      last_tick_ts: '2026-10-05T12:00:00Z'
    });
    expect(signalReading(hung, Date.parse('2026-10-05T12:00:30Z')).note).toBe(NO_HEARTBEAT);
  });

  it('reads a tick the way the epic page does, past 2x its interval and through a long tick', () => {
    // #2844 N18: the row held DESIGN.md's old flat 2x window while the epic
    // page's `staleness()` allowed max(3x interval, interval + 2x duration),
    // so a tick 150s into a 60s cadence read "no heartbeat" here and live on
    // the page. Both now read `freshBeat`; the duration is on the summary.
    const started = '2026-10-05T12:00:00Z';
    const finished = '2026-10-05T11:58:30Z';
    const hb = (duration: number | null, ageS: number) => {
      const at = Date.parse(started) + ageS * 1000;
      const row = live({
        last_tick_started_at: started,
        last_tick_ts: finished,
        last_tick_duration_s: duration
      });
      const page = staleness(
        {
          last_tick_started_at: started,
          last_tick_ts: finished,
          last_tick_duration_s: duration,
          tick_count: 3,
          interval_s: 60,
          last_signal: 'progressing',
          change_count: 0
        },
        at
      );
      return { row: signalReading(row, at).halo, page: page.kind === 'fresh' };
    };
    // 2.5 intervals: live on both.
    expect(hb(null, 150)).toEqual({ row: true, page: true });
    // A long finished tick (100s) widens the window to 60 + 200 = 260s.
    expect(hb(100, 240)).toEqual({ row: true, page: true });
    expect(hb(null, 240)).toEqual({ row: false, page: false });
    expect(hb(100, 261)).toEqual({ row: false, page: false });
  });

  it('falls back to the finish stamp when no start stamp is served', () => {
    // A `kew serve` predating #2283 omits `last_tick_started_at` from the
    // list; so does an epic with journal rows but no driver-state row. The
    // finish stamp is a worse reading of the same thing, not a different
    // question — withdrawing every live claim against an older server would
    // be the louder lie.
    for (const over of [{}, { last_tick_started_at: null }])
      expect(signalReading(live(over), now).halo).toBe(true);
  });

  it('reads a tick from the future as fresh rather than as gone', () => {
    // A skewed clock between the driver's host and this browser is not a dead
    // driver, and "no heartbeat" over a tick that just landed is the lie this
    // reading exists to prevent, pointing the other way.
    expect(signalReading(live(), Date.parse('2026-10-05T11:59:00Z')).halo).toBe(true);
  });

  it('leaves a signal that never claimed live exactly as its tone says', () => {
    // complete, stalled and an unrecognised word draw no halo in the first
    // place (`signalTone`), so there is no claim to withdraw and nothing to
    // confess: "no heartbeat" on a July epic on the Finished shelf would be
    // an alarm about a drain that ended on purpose.
    const old = Date.parse('2026-10-05T12:00:00Z');
    for (const signal of ['complete', 'stalled', 'awaiting_merges', null]) {
      const reading = signalReading(live({ signal, last_tick_ts: '2026-07-01T00:00:00Z' }), old);
      expect(reading).toEqual({ ...signalTone(signal), note: null });
    }
  });
});

/**
 * #2283 (D17) — the needs-you column leads with a person's next move, and a
 * mark that says somebody is waiting has to go where the waiting is.
 */
describe('needsYouHref', () => {
  const row = (item: string | null, epic = 'launch-fixes') => ({ item_id: item, epic_ref: epic });

  it('links to the one node that is waited on', () => {
    expect(needsYouHref('launch-fixes', [row('2055')])).toBe('/epics/launch-fixes?node=2055');
  });

  it('reads two waits on one node as that node', () => {
    expect(needsYouHref('launch-fixes', [row('2055'), row('2055')])).toBe(
      '/epics/launch-fixes?node=2055'
    );
  });

  it('sends a bucket naming several nodes to the epic, which shows them all', () => {
    // `?node=` selects one node. Picking the first of three would be an
    // arbitrary answer to "which one needs me"; the graph answers all of it.
    expect(needsYouHref('launch-fixes', [row('2055'), row('2219')])).toBe('/epics/launch-fixes');
  });

  it('sends an epic-wide wait to the epic, because it is no node', () => {
    // A stalled driver, an orphan PR and an epic CI hold are facts about the
    // epic (`_stalled_rows` in kew/src/kew/service/needs_you.py), and the set
    // serves them with no item at all.
    for (const item of [null, '', '  '])
      expect(needsYouHref('launch-fixes', [row(item)])).toBe('/epics/launch-fixes');
  });

  it('ignores another epic’s waits entirely', () => {
    expect(needsYouHref('launch-fixes', [row('9', 'go-live'), row('2055')])).toBe(
      '/epics/launch-fixes?node=2055'
    );
  });

  it('answers a payload nobody has loaded with the epic’s own page', () => {
    expect(needsYouHref('launch-fixes', undefined)).toBe('/epics/launch-fixes');
    expect(needsYouHref('launch-fixes', [])).toBe('/epics/launch-fixes');
  });

  it('encodes both halves, because a ref and an item id are opaque', () => {
    expect(needsYouHref('a/b', [{ item_id: 'c d', epic_ref: 'a/b' }])).toBe(
      '/epics/a%2Fb?node=c%20d'
    );
  });
});

/** #2283 (D17) — "Show only non-zero counts; a zero is noise in a list". */
describe('nonZeroCounts', () => {
  it('drops the zeros and keeps the order of what is left', () => {
    expect(
      nonZeroCounts([
        { status: 'done', count: 13 },
        { status: 'failed', count: 0 },
        { status: 'todo', count: 4 }
      ])
    ).toEqual([
      { status: 'done', count: 13 },
      { status: 'todo', count: 4 }
    ]);
  });

  it('answers an epic with nothing in any status with no marks at all', () => {
    expect(nonZeroCounts([{ status: 'done', count: 0 }])).toEqual([]);
  });
});
