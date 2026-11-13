import { describe, expect, it } from 'vitest';
import {
  accumulatedTicker,
  applyFrame,
  initialLiveState,
  isUnauthorized,
  seededTicker,
  TICKER_CAP
} from './live';
import { railSelected } from './rail';
import { tickerLines } from './ticker';
import type {
  EpicEconomics,
  EpicHeartbeat,
  EpicNode,
  EpicSnapshot,
  EpicTick
} from '$lib/api';

const snap = (tickId: number, overrides: Partial<EpicSnapshot> = {}): EpicSnapshot => ({
  epic_ref: 'epic-1',
  tick_id: tickId,
  tick_ts: `2026-07-02T0${tickId}:00:00.000Z`,
  signal: 'progressing',
  counts: {},
  frontier: [],
  nodes: [],
  edges: [],
  unavailable: [],
  ...overrides
});

const tick = (id: number, actions: EpicTick['actions'] = {}): EpicTick => ({
  id,
  tick_ts: `2026-07-02T0${id}:00:00.000Z`,
  snapshot: snap(id),
  actions,
  economics: null
});

const econ = (label: string): EpicEconomics => ({
  window_start: null,
  drain_total_usd: 1,
  node_drain_usd: {},
  label
});

const hb = (tickCount: number): EpicHeartbeat => ({
  last_tick_started_at: '2026-07-02T09:00:00.000Z',
  last_tick_ts: '2026-07-02T09:00:00.000Z',
  last_tick_duration_s: 1,
  tick_count: tickCount,
  interval_s: 60,
  last_signal: 'progressing',
  change_count: tickCount
});

describe('initialLiveState', () => {
  it('seeds lastTickId from the initial snapshot tick_id', () => {
    const s = initialLiveState({ snapshot: snap(5) });
    expect(s.lastTickId).toBe(5);
    expect(s.snapshot?.tick_id).toBe(5);
    expect(s.ticker).toEqual([]);
  });

  it('defaults to lastTickId 0 with a null snapshot (pre-journal epic)', () => {
    const s = initialLiveState({});
    expect(s.lastTickId).toBe(0);
    expect(s.snapshot).toBeNull();
    expect(s.economics).toBeNull();
    expect(s.heartbeat).toBeNull();
  });
});

describe('applyFrame — snapshot frames', () => {
  it('applies a newer tick: replaces snapshot, bumps lastTickId, prepends ticker lines', () => {
    const start = initialLiveState({ snapshot: snap(1) });
    const next = applyFrame(start, {
      type: 'snapshot',
      data: tick(2, { dispatched: ['514'] })
    });
    expect(next.lastTickId).toBe(2);
    expect(next.snapshot?.tick_id).toBe(2);
    expect(next.ticker).toEqual([{ icon: '▸', text: 'dispatched #514', ts: tick(2).tick_ts }]);
  });

  it('dedups: a frame with id <= lastTickId is ignored entirely (no snapshot/ticker mutation)', () => {
    const start = applyFrame(initialLiveState({ snapshot: snap(1) }), {
      type: 'snapshot',
      data: tick(2, { dispatched: ['514'] })
    });
    const replay = applyFrame(start, { type: 'snapshot', data: tick(2, { dispatched: ['999'] }) });
    expect(replay).toEqual(start);
    const stale = applyFrame(start, { type: 'snapshot', data: tick(1, { dispatched: ['999'] }) });
    expect(stale).toEqual(start);
  });

  it('reconnect never replays history: skipping id 2 straight to id 5 loses no state integrity, just history', () => {
    const start = initialLiveState({ snapshot: snap(1) });
    const next = applyFrame(start, { type: 'snapshot', data: tick(5, { dispatched: ['777'] }) });
    expect(next.lastTickId).toBe(5);
    expect(next.ticker).toHaveLength(1);
  });

  it('ticker is newest-first and capped at 100', () => {
    let state = initialLiveState({ snapshot: snap(0) });
    for (let i = 1; i <= TICKER_CAP + 5; i++) {
      state = applyFrame(state, { type: 'snapshot', data: tick(i, { dispatched: [String(i)] }) });
    }
    expect(state.ticker).toHaveLength(TICKER_CAP);
    // newest first: the very last applied tick's line is at index 0
    expect(state.ticker[0].text).toBe(`dispatched #${TICKER_CAP + 5}`);
    // oldest surviving entry is the (TICKER_CAP+5 - TICKER_CAP + 1)th tick
    expect(state.ticker[TICKER_CAP - 1].text).toBe('dispatched #6');
  });

  it('a single tick can contribute multiple ticker lines, all prepended together', () => {
    const start = initialLiveState({ snapshot: snap(0) });
    const next = applyFrame(start, {
      type: 'snapshot',
      data: tick(1, { dispatched: ['1'], held: ['2'] })
    });
    expect(next.ticker.map((l) => l.icon)).toEqual(['▸', '⏸']);
  });
});

describe('applyFrame — economics/heartbeat frames replace state, never touch snapshot/ticker', () => {
  it('economics frame replaces economics only', () => {
    const start = initialLiveState({ snapshot: snap(3), economics: econ('old') });
    const next = applyFrame(start, { type: 'economics', data: econ('new') });
    expect(next.economics?.label).toBe('new');
    expect(next.snapshot).toBe(start.snapshot);
    expect(next.ticker).toBe(start.ticker);
    expect(next.lastTickId).toBe(start.lastTickId);
  });

  it('heartbeat frame replaces heartbeat only', () => {
    const start = initialLiveState({ snapshot: snap(3) });
    const next = applyFrame(start, { type: 'heartbeat', data: hb(9) });
    expect(next.heartbeat?.tick_count).toBe(9);
    expect(next.snapshot).toBe(start.snapshot);
  });
});

describe('applyFrame — attention frames refresh the join between ticks', () => {
  const node = (id: string): EpicNode => ({
    id,
    title: `issue ${id}`,
    status: 'in_review',
    status_since: null,
    url: null,
    pr_url: null,
    external: false,
    stuck: null,
    attention: null,
    blocked_reason: null
  });
  const waiting = (id: string): EpicNode => ({
    ...node(id),
    attention: { kind: 'merge_decision', reason: `PR open for #${id}`, url: null }
  });

  it('re-annotates the snapshot on screen without touching tick state or ticker', () => {
    const start = initialLiveState({ snapshot: snap(3, { nodes: [node('101')] }) });
    const next = applyFrame(start, {
      type: 'attention',
      data: { by_node: { '101': waiting('101').attention! }, unavailable: [] }
    });
    expect(next.snapshot?.nodes[0].attention?.kind).toBe('merge_decision');
    expect(next.lastTickId).toBe(start.lastTickId);
    expect(next.ticker).toBe(start.ticker);
    expect(next.snapshot?.tick_id).toBe(3);
  });

  it('clears a node the join no longer names — the diamond goes out between ticks', () => {
    const start = initialLiveState({ snapshot: snap(3, { nodes: [waiting('101')] }) });
    const next = applyFrame(start, { type: 'attention', data: { by_node: {}, unavailable: [] } });
    expect(next.snapshot?.nodes[0].attention).toBeNull();
    expect(next.snapshot?.unavailable).toEqual([]);
  });

  it('a join that stopped being readable replaces the reading, never leaves it standing', () => {
    const start = initialLiveState({ snapshot: snap(3, { nodes: [waiting('101')] }) });
    const next = applyFrame(start, {
      type: 'attention',
      data: { by_node: {}, unavailable: ['attention'] }
    });
    expect(next.snapshot?.nodes[0].attention).toBeNull();
    expect(next.snapshot?.unavailable).toEqual(['attention']);
  });

  it('is a no-op with no snapshot yet — the next snapshot frame carries the join', () => {
    const start = initialLiveState({});
    expect(applyFrame(start, { type: 'attention', data: { by_node: {}, unavailable: [] } })).toBe(
      start
    );
  });
});

describe('applyFrame — recovery frames refresh the command between ticks', () => {
  // The host died under 1010 twice and its work is preserved (#2960): the
  // page loaded with continue. Another operator then claims or abandons the
  // continuation, and no tick is journaled.
  const PRESERVED = 'failed-terminal (exit 137, host_restart_repeated, preserved work)';
  const TAKEN_UP = 'failed-terminal (exit 137, host_restart_repeated, preserved work taken up)';
  const failed: EpicNode = {
    id: '1010',
    title: 'issue 1010',
    status: 'failed',
    status_since: null,
    url: null,
    pr_url: null,
    external: false,
    stuck: PRESERVED,
    attention: null,
    blocked_reason: PRESERVED,
    recovery_command: 'kew epic continue epic-1 1010'
  };
  const other: EpicNode = { ...failed, id: '1011', recovery_command: 'kew epic retry epic-1 1011' };
  const mounted = () => initialLiveState({ snapshot: snap(7, { nodes: [failed, other] }) });

  it('replaces the stuck reason and command on the snapshot on screen, without a tick', () => {
    const start = mounted();
    const next = applyFrame(start, {
      type: 'recovery',
      data: {
        by_node: {
          '1010': { stuck: TAKEN_UP, blocked_reason: TAKEN_UP, recovery_command: '' }
        }
      }
    });
    const [taken, untouched] = next.snapshot!.nodes;
    expect(taken.recovery_command).toBe('');
    expect(taken.stuck).toBe(TAKEN_UP);
    expect(taken.blocked_reason).toBe(TAKEN_UP);
    expect(taken.status).toBe('failed');
    // A node the frame does not name keeps what it has.
    expect(untouched).toBe(other);
    // Nothing tick-shaped moved.
    expect(next.lastTickId).toBe(7);
    expect(next.ticker).toBe(start.ticker);
    // The rail prints the command the frame served, not the one it loaded with.
    expect(railSelected({ node: taken, epicRef: 'epic-1' })!.retry).toBe('');
  });

  it('turns a stale retry into continue when the work is preserved again', () => {
    const start = initialLiveState({
      snapshot: snap(7, {
        nodes: [
          {
            ...failed,
            stuck: 'failed-terminal (exit 137, host_restart_repeated)',
            recovery_command: 'kew epic retry epic-1 1010'
          }
        ]
      })
    });
    const next = applyFrame(start, {
      type: 'recovery',
      data: {
        by_node: {
          '1010': {
            stuck: PRESERVED,
            blocked_reason: PRESERVED,
            recovery_command: 'kew epic continue epic-1 1010'
          }
        }
      }
    });
    expect(next.snapshot!.nodes[0].recovery_command).toBe('kew epic continue epic-1 1010');
  });

  it('is a no-op with no snapshot yet', () => {
    const start = initialLiveState({});
    expect(applyFrame(start, { type: 'recovery', data: { by_node: {} } })).toBe(start);
  });
});

describe('accumulatedTicker — replay ticker builds across frames', () => {
  it('accumulates lines from frames 0..uptoIndex, newest frame first', () => {
    const frames = [tick(1, { dispatched: ['a'] }), tick(2, { dispatched: ['b'] }), tick(3, { dispatched: ['c'] })];
    expect(accumulatedTicker(frames, 2).map((l) => l.text)).toEqual([
      'dispatched #c',
      'dispatched #b',
      'dispatched #a'
    ]);
  });

  it('seeking BACKWARD shows only lines recorded up to that frame', () => {
    const frames = [tick(1, { dispatched: ['a'] }), tick(2, { dispatched: ['b'] }), tick(3, { dispatched: ['c'] })];
    expect(accumulatedTicker(frames, 0).map((l) => l.text)).toEqual(['dispatched #a']);
    expect(accumulatedTicker(frames, 1).map((l) => l.text)).toEqual([
      'dispatched #b',
      'dispatched #a'
    ]);
  });

  it('caps at TICKER_CAP, keeping the newest lines', () => {
    const frames = Array.from({ length: TICKER_CAP + 20 }, (_, i) =>
      tick(i + 1, { dispatched: [String(i + 1)] })
    );
    const lines = accumulatedTicker(frames, frames.length - 1);
    expect(lines).toHaveLength(TICKER_CAP);
    expect(lines[0].text).toBe(`dispatched #${TICKER_CAP + 20}`);
    expect(lines[TICKER_CAP - 1].text).toBe('dispatched #21');
  });

  it('a frame with multiple lines keeps tickerLines order within the frame', () => {
    const frames = [tick(1, {}), tick(2, { merged: [{ id: 'm', approved_by: null }], dispatched: ['d'] })];
    expect(accumulatedTicker(frames, 1).map((l) => l.icon)).toEqual(['✓', '▸']);
  });

  it('empty frames / uptoIndex beyond the end degrade safely', () => {
    expect(accumulatedTicker([], 5)).toEqual([]);
    const frames = [tick(1, { dispatched: ['a'] })];
    expect(accumulatedTicker(frames, 99).map((l) => l.text)).toEqual(['dispatched #a']);
  });
});

describe('seededTicker — the journal seed merges under what SSE already showed', () => {
  // The seed is paid once per epic, on the first expand of the ticker (C10).
  // It used to stand down entirely whenever a line was already on screen, so a
  // live epic that ticked once before the reader expanded lost its whole
  // journal tail for good — the seed never came back for it.
  it('seeds the whole tail when nothing has been shown yet', () => {
    const journal = [tick(1, { dispatched: ['a'] }), tick(2, { dispatched: ['b'] })];
    expect(seededTicker([], journal).map((l) => l.text)).toEqual([
      'dispatched #b',
      'dispatched #a'
    ]);
  });

  it('still seeds the history under a line SSE delivered first', () => {
    const journal = [tick(1, { dispatched: ['a'] }), tick(2, { dispatched: ['b'] })];
    const shown = tickerLines(tick(3, { dispatched: ['c'] }));
    expect(seededTicker(shown, journal).map((l) => l.text)).toEqual([
      'dispatched #c',
      'dispatched #b',
      'dispatched #a'
    ]);
  });

  it('prints the overlap once — the drain reads the tick SSE already showed', () => {
    const newest = tick(2, { dispatched: ['b'] });
    const journal = [tick(1, { dispatched: ['a'] }), newest];
    expect(seededTicker(tickerLines(newest), journal).map((l) => l.text)).toEqual([
      'dispatched #b',
      'dispatched #a'
    ]);
  });

  it('keeps a shown line the drain missed on top rather than dropping it', () => {
    // A tick recorded while `fetchAllTicks` was still paging: SSE has it, the
    // journal read does not, and the seed only ever ADDS history below.
    const journal = [tick(1, { dispatched: ['a'] })];
    const shown = tickerLines(tick(9, { stuck: [{ id: 's', reason: 'blocked' }] }));
    const lines = seededTicker(shown, journal);
    expect(lines.map((l) => l.icon)).toEqual(['✗', '▸']);
  });

  it('caps the merged list at TICKER_CAP, newest first', () => {
    const journal = Array.from({ length: TICKER_CAP + 20 }, (_, i) =>
      tick(i + 1, { dispatched: [String(i + 1)] })
    );
    const shown = tickerLines(tick(999, { dispatched: ['live'] }));
    const lines = seededTicker(shown, journal);
    expect(lines).toHaveLength(TICKER_CAP);
    expect(lines[0].text).toBe('dispatched #live');
    expect(lines[1].text).toBe(`dispatched #${TICKER_CAP + 20}`);
  });

  it('a tick seeded from the drain is not printed again when SSE delivers it', () => {
    // The race the id watermark cannot see: the SSE hub polls the journal at
    // POLL_S = 1s (kew/serve/watch.py) while this drain reads the store
    // directly, so tick 7 is committed between the hub's last poll and the
    // seed — seeded here, then delivered as a frame whose id IS newer than
    // lastTickId. Without the line check it printed twice, the second copy
    // stacked above the older lines.
    const seven = tick(7, { dispatched: ['514'] });
    const seeded = initialLiveState({ snapshot: snap(6) });
    const after = { ...seeded, ticker: seededTicker(seeded.ticker, [tick(6, {}), seven]) };
    expect(after.ticker.map((l) => l.text)).toEqual(['dispatched #514']);

    const live = applyFrame(after, { type: 'snapshot', data: seven });

    expect(live.ticker.map((l) => l.text)).toEqual(['dispatched #514']);
    // The frame is still APPLIED — the graph and the watermark move, only the
    // duplicate line is withheld.
    expect(live.snapshot?.tick_id).toBe(7);
    expect(live.lastTickId).toBe(7);
  });

  it('withholds only the lines already shown, keeping the rest of the tick', () => {
    const mixed = tick(7, { dispatched: ['514'], held: ['88'] });
    const seeded = initialLiveState({ snapshot: snap(6) });
    // Only the `dispatched` half made it on screen before the frame arrived.
    const partial = { ...seeded, ticker: [tickerLines(mixed)[0]] };

    const live = applyFrame(partial, { type: 'snapshot', data: mixed });

    expect(live.ticker.map((l) => l.icon)).toEqual(['⏸', '▸']);
  });

  it('an empty journal leaves what is on screen alone', () => {
    const shown = tickerLines(tick(1, { dispatched: ['a'] }));
    expect(seededTicker(shown, [])).toEqual(shown);
  });
});

describe('isUnauthorized', () => {
  it('recognizes the api.ts get() 401 error message shape', () => {
    expect(isUnauthorized(new Error('/api/epics/foo -> 401'))).toBe(true);
  });
  it('rejects other statuses / non-Error values', () => {
    expect(isUnauthorized(new Error('/api/epics/foo -> 404'))).toBe(false);
    expect(isUnauthorized('nope')).toBe(false);
    expect(isUnauthorized(null)).toBe(false);
  });
});
