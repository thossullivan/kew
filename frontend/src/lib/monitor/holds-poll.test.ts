/**
 * #3468 C9 — the Holds column's poll: one round out at a time, newest wins.
 *
 * A round is `/api/epics`, a snapshot per blocked epic and the caps. Fired
 * bare off a 30s interval, a server slower than the beat stacked one more
 * round per tick, and a slow round answering after a newer one drew the older
 * world over it. The fetchers are injected through `readHoldSources`'s
 * `fetchers` argument, the network boundary, and each answers when the test
 * says so.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BudgetMap } from '$lib/api';
import {
  HOLDS_POLL_MS,
  holdsPoll,
  type HoldsFetchers,
  type PolledSources
} from './holds';

/** A caps map that says which round served it. */
const caps = (round: number): BudgetMap => ({
  'budget.weekly_usd': { effective: round, file_value: round, override: null }
});

/** Fetchers whose caps read answers only when the test resolves it. */
function network() {
  const pending: Array<(map: BudgetMap) => void> = [];
  let epicsCalls = 0;
  const fetchers: Partial<HoldsFetchers> = {
    epics: async () => {
      epicsCalls += 1;
      return [];
    },
    budget: () => new Promise<BudgetMap>((resolve) => pending.push(resolve))
  };
  return {
    fetchers,
    pending,
    rounds: () => epicsCalls,
    budgetReads: () => pending.length
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const roundOf = (sources: PolledSources) =>
  'available' in sources.budget ? null : sources.budget['budget.weekly_usd'].effective;

describe('holdsPoll', () => {
  it('starts no second round while one is in flight', async () => {
    const net = network();
    const landed: PolledSources[] = [];
    const poll = holdsPoll((s) => landed.push(s), () => false, net.fetchers);

    poll.beat();
    poll.beat();
    poll.beat();
    await settle();
    expect(net.rounds()).toBe(1);
    expect(net.budgetReads()).toBe(1);

    net.pending[0](caps(1));
    await settle();
    expect(landed.map(roundOf)).toEqual([1]);

    // With the round answered, the next beat reads again.
    poll.beat();
    await settle();
    expect(net.rounds()).toBe(2);
    poll.stop();
  });

  it('never lets a round that answers after a newer one replace it', async () => {
    const net = network();
    const landed: PolledSources[] = [];
    const poll = holdsPoll((s) => landed.push(s), () => false, net.fetchers);

    poll.beat();
    await settle();
    // The tab comes back while round 1 is still out: round 2 is the newer.
    poll.show();
    await settle();
    expect(net.budgetReads()).toBe(2);

    net.pending[1](caps(2));
    await settle();
    net.pending[0](caps(1));
    await settle();
    expect(landed.map(roundOf)).toEqual([2]);
    poll.stop();
  });

  it('reads nothing and draws nothing while the tab is hidden', async () => {
    const net = network();
    const landed: PolledSources[] = [];
    let hidden = false;
    const poll = holdsPoll((s) => landed.push(s), () => hidden, net.fetchers);

    poll.beat();
    await settle();
    hidden = true;
    net.pending[0](caps(1));
    await settle();
    poll.beat();
    await settle();
    expect(landed).toEqual([]);
    expect(net.rounds()).toBe(1);
    poll.stop();
  });
});

/**
 * A server that answers every request `delayMs` after it is made, and ends a
 * request when its `AbortSignal` aborts, as `fetch` does.
 */
function slowServer(delayMs: number) {
  let started = 0;
  let finished = 0;
  let outstandingPeak = 0;
  const unsignalled: string[] = [];
  function request<T>(name: string, value: T, signal?: AbortSignal): Promise<T> {
    if (!signal) unsignalled.push(name);
    started += 1;
    outstandingPeak = Math.max(outstandingPeak, started - finished);
    return new Promise<T>((resolve, reject) => {
      const done = () => {
        finished += 1;
        clearTimeout(timer);
      };
      const timer = setTimeout(() => {
        done();
        resolve(value);
      }, delayMs);
      signal?.addEventListener('abort', () => {
        done();
        reject(new DOMException('aborted', 'AbortError'));
      });
    });
  }
  const fetchers: HoldsFetchers = {
    epics: (_f, signal) =>
      request('epics', [{ epic_ref: 'e1', counts: { blocked: 1 } }] as never, signal),
    epic: (_ref, _f, signal) =>
      request('epic', { epic_ref: 'e1', snapshot: null } as never, signal),
    budget: (_f, signal) => request('budget', caps(1), signal)
  };
  return {
    fetchers,
    unsignalled,
    outstanding: () => started - finished,
    peak: () => outstandingPeak
  };
}

describe('holdsPoll against a server slower than its beat', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** The component's wiring: one read at once, then one beat per interval. */
  function mount(fetchers: HoldsFetchers, landed: PolledSources[]) {
    const poll = holdsPoll((s) => landed.push(s), () => false, fetchers);
    poll.show();
    const timer = setInterval(() => poll.beat(), HOLDS_POLL_MS);
    return () => {
      clearInterval(timer);
      poll.stop();
    };
  }

  it('keeps one round of requests open across beats, however slow the server', async () => {
    const server = slowServer(95_000);
    const unmount = mount(server.fetchers, []);

    await vi.advanceTimersByTimeAsync(HOLDS_POLL_MS * 6);

    // A round is the caps read plus the epics read, then one snapshot: never
    // more than one round's requests at once, and every one abortable.
    expect(server.peak()).toBeLessThanOrEqual(2);
    expect(server.outstanding()).toBeLessThanOrEqual(2);
    expect(server.unsignalled).toEqual([]);
    unmount();
    expect(server.outstanding()).toBe(0);
  });

  it('draws a round that answers after the beat but inside two', async () => {
    const server = slowServer(HOLDS_POLL_MS * 0.8);
    const landed: PolledSources[] = [];
    const unmount = mount(server.fetchers, landed);

    // Epics then snapshot run back to back: the round answers at 1.6 beats.
    await vi.advanceTimersByTimeAsync(HOLDS_POLL_MS * 1.7);

    expect(landed.map(roundOf)).toEqual([1]);
    unmount();
  });
});
