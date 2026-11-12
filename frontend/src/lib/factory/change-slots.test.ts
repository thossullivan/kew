/**
 * #2844 D23 — the Epics list's sparkline draws one mark per change.
 *
 * On live data 196-199 of an epic's last 200 ticks did nothing, while reviews
 * and merges land as snapshot changes on ticks whose own outcome is `nothing`.
 * A strip of attempts was flat beside a rising CHANGE; these pin that the marks
 * are the changes, drawn as a neutral dot and never the done dot's green, that
 * a failed tick keeps its red ×, and that no-op ticks draw nothing at all.
 */
import { describe, expect, it } from 'vitest';
import { fetchEpicChangeStrip, type EpicTickStrip } from '$lib/api';
import { STATUS_MARKS } from './status';
import { CHANGE_COLOR, CHANGE_WINDOW, COMPACT_SLOTS, changeSlots, changeWord } from './tick-strip';

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

const noOps = (n: number) => Array.from({ length: n }, () => tick());
const marks = (ticks: EpicTickStrip[]) => changeSlots(ticks, 16).filter((s) => s.kind !== 'pad');

describe('changeSlots', () => {
  it('draws one mark per change across a run of no-op ticks', () => {
    const changes = [
      tick({ journal_tick_id: 11 }),
      tick({ journal_tick_id: 12 }),
      tick({ journal_tick_id: 13 })
    ];
    const ticks = [...noOps(60), changes[0], ...noOps(70), changes[1], ...noOps(60), changes[2], ...noOps(5)];
    const drawn = marks(ticks);
    expect(drawn.map((s) => s.item?.journal_tick_id)).toEqual([11, 12, 13]);
    expect(drawn.every((s) => s.kind === 'change' && s.meaning === null)).toBe(true);
  });

  it('draws a change as a neutral dot, never the done dot\'s green', () => {
    // Live, every slot of three epics whose CHANGE read 2735, 560 and 209 was
    // a green dot: green means ready, merged or done (DESIGN.md).
    expect(CHANGE_COLOR).toBe('var(--text-2)');
    expect(CHANGE_COLOR).not.toBe(STATUS_MARKS.done.color);
    const merged = marks([tick({ outcome: 'merged', n_merged: 1, journal_tick_id: 5 })]);
    expect(merged.map((s) => [s.kind, s.meaning])).toEqual([['change', null]]);
  });

  it('draws nothing for no-op ticks that changed nothing', () => {
    const slots = changeSlots(noOps(200), 16);
    expect(slots).toHaveLength(16);
    expect(slots.every((s) => s.kind === 'pad')).toBe(true);
  });

  it('gains a mark when a change lands, so it moves when CHANGE moves', () => {
    const before = [...noOps(100), tick({ journal_tick_id: 1 }), ...noOps(50)];
    const after = [...before, ...noOps(20), tick({ journal_tick_id: 2 })];
    expect(marks(before)).toHaveLength(1);
    expect(marks(after)).toHaveLength(2);
  });

  it('keeps a failed tick as the red ×, and leaves a held repeat out', () => {
    const ticks = [tick({ outcome: 'held' }), tick({ outcome: 'failed' }), ...noOps(3)];
    const drawn = marks(ticks);
    expect(drawn).toHaveLength(1);
    expect(drawn[0]!.meaning).toBe('failed');
  });

  it('draws every change the same, whatever its outcome, but a failed one red', () => {
    const drawn = marks([
      tick({ outcome: 'held', journal_tick_id: 4 }),
      tick({ outcome: 'failed', journal_tick_id: 5 })
    ]);
    expect(drawn.map((s) => [s.kind, s.meaning])).toEqual([
      ['change', null],
      ['mark', 'failed']
    ]);
  });

  it('keeps the live tick cyan', () => {
    const open = tick({
      ts_started: new Date().toISOString(),
      ts_finished: null,
      outcome: null,
      reached_tick: true
    });
    const drawn = marks([tick({ journal_tick_id: 6 }), open]);
    expect(drawn.map((s) => s.kind)).toEqual(['change', 'mark']);
    expect(drawn[1]!.live).toBe(true);
  });

  it('draws one mark per change when no-op ticks repeat its journal id', () => {
    // An unchanged snapshot's attempt carries the previous change's id: the
    // journal's dedup returns the row it already has. One change followed by
    // a hundred no-ops is one mark, not sixteen.
    const repeat = (id: number, n: number) =>
      Array.from({ length: n }, () => tick({ journal_tick_id: id }));
    const ticks = [tick({ journal_tick_id: 42 }), ...repeat(42, 100)];
    expect(marks(ticks).map((s) => s.item?.journal_tick_id)).toEqual([42]);
    const more = [...ticks, tick({ journal_tick_id: 43 }), ...repeat(43, 30)];
    expect(marks(more).map((s) => s.item?.id)).toEqual([ticks[0]!.id, more[101]!.id]);
  });

  it('keeps the newest changes, newest at the right edge', () => {
    const ticks = Array.from({ length: 20 }, (_, i) => tick({ journal_tick_id: 100 + i }));
    const slots = changeSlots(ticks, 16);
    expect(slots).toHaveLength(16);
    expect(slots[15]!.item?.journal_tick_id).toBe(119);
    expect(slots[0]!.item?.journal_tick_id).toBe(104);
  });
});

describe('changeWord', () => {
  it('reads a change whose outcome is nothing as changed, and leaves other words alone', () => {
    expect(changeWord(tick({ journal_tick_id: 9 }))).toBe('changed');
    expect(changeWord(tick({ outcome: 'merged', journal_tick_id: 9 }))).toBe('merged');
    expect(changeWord(tick({ outcome: 'failed' }))).toBe('failed');
    expect(changeWord(null)).toBe('no tick');
  });
});

describe('the change read', () => {
  it('asks the server for the marks themselves, one strip of them', async () => {
    // Reading the last 200 attempts and filtering them here drew no mark for a
    // change followed by 201 no-op ticks; the server filters the whole kept
    // log instead (`?changes=true`, kew/src/kew/service/epics.py tick_strip).
    const asked: string[] = [];
    const fake = (async (url: string) => {
      asked.push(url);
      return new Response('[]', { headers: { 'Content-Type': 'application/json' } });
    }) as unknown as typeof fetch;
    expect(await fetchEpicChangeStrip('go-live', CHANGE_WINDOW, fake)).toEqual([]);
    expect(asked).toEqual([`/api/epics/go-live/tick-strip?changes=true&limit=${COMPACT_SLOTS}`]);
  });

  it('draws a change the read returns behind 201 no-op attempts', () => {
    // The page draws the read's rows through `changeSlots`; the fixture server
    // ignores the filter and serves every attempt, so the client filter has to
    // hold the change too.
    const drawn = marks([tick({ journal_tick_id: 1 }), ...noOps(201)]);
    expect(drawn.map((s) => s.item?.journal_tick_id)).toEqual([1]);
  });
});
