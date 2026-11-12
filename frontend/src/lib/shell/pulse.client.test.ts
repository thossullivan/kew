/**
 * #2285 (C8) — the factory pulse's three guards.
 *
 * One `setInterval` reads `/api/dashboard` and `/api/runs` every 7s for the
 * whole shell. Unguarded, that is three bugs, and this file is each of them
 * once. They need a clock and a `document`, so this is a `.client.test.ts`:
 * the timer and the visibility listener are the behaviour under test, and
 * neither exists in an SSR render.
 *
 * 1. A backgrounded tab kept asking for numbers nobody could see, for as long
 *    as it stayed open — the acceptance line for this node is literally "a
 *    hidden tab makes no requests".
 * 2. Against a server slower than the interval, every tick added another pair
 *    of reads to the thing that was already slow.
 * 3. Two reads in flight land in either order, and the loser overwrote the
 *    winner — the header showing older counts than it had already received.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Dashboard, Health, Run } from '$lib/api';

const fetchDashboard = vi.fn<() => Promise<Dashboard>>();
const fetchRuns = vi.fn<() => Promise<Run[]>>();
const fetchBundle = vi.fn(() => Promise.resolve({ present: true } as never));
const fetchHealth = vi.fn<() => Promise<Health>>();

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api', () => ({ fetchDashboard, fetchRuns, fetchBundle, fetchHealth }));

const { factoryPulse, HEALTH_INTERVAL_MS, PULSE_INTERVAL_MS } = await import('./pulse.svelte');

/** A dashboard distinguishable by one number. */
const dashboard = (pending: number): Dashboard =>
  ({ banner: {}, rows: [], glance: { fleet: { stopped: false, pending } } }) as unknown as Dashboard;

/** A health reading distinguishable by one word. */
const health = (status: string): Health => ({ status }) as unknown as Health;

/** A promise this test resolves by hand, so a read can be left in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

let hidden = false;

/** Hide or show the tab, and fire the event the browser fires with it. */
function visibility(next: boolean): void {
  hidden = next;
  document.dispatchEvent(new Event('visibilitychange'));
}

/**
 * Let every already-resolved promise settle, without advancing the clock. A
 * refresh is several microtasks deep (`Promise.allSettled` over two reads, then
 * the generation check), so one turn of the queue is not enough.
 */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 8; turn++) await Promise.resolve();
}

/**
 * The teardown for the subscription under test, kept here rather than at the
 * end of each case: `factoryPulse` is a module singleton, and a test that threw
 * before its own `stop()` would leave the next one unable to start the poll.
 */
let stop: (() => void) | null = null;

function subscribe(): void {
  stop = factoryPulse.subscribe();
}

/** Leave the poll, once, from inside a case. */
function unsubscribe(): void {
  stop?.();
  stop = null;
}

beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  fetchDashboard.mockReset().mockImplementation(() => Promise.resolve(dashboard(0)));
  fetchRuns.mockReset().mockImplementation(() => Promise.resolve([]));
  fetchHealth.mockReset().mockImplementation(() => Promise.resolve(health('down')));
  factoryPulse.dashboard = null;
  factoryPulse.health = null;
  factoryPulse.runs = null;
  factoryPulse.hasRead = false;
});

afterEach(() => {
  stop?.();
  stop = null;
  vi.useRealTimers();
});

describe('a hidden tab', () => {
  it('makes no requests at all', async () => {
    subscribe();
    await settle();
    expect(fetchDashboard).toHaveBeenCalledTimes(1); // the read on subscribe

    visibility(true);
    for (let tick = 0; tick < 5; tick++) {
      await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS);
    }

    expect(fetchDashboard).toHaveBeenCalledTimes(1);
    expect(fetchRuns).toHaveBeenCalledTimes(1);
  });

  it('takes a reading the moment it is looked at again', async () => {
    subscribe();
    await settle();
    visibility(true);
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS);
    expect(fetchDashboard).toHaveBeenCalledTimes(1);

    // Back in front: the numbers are refreshed now, not up to 7s from now.
    fetchDashboard.mockImplementation(() => Promise.resolve(dashboard(4)));
    visibility(false);
    await settle();

    expect(fetchDashboard).toHaveBeenCalledTimes(2);
    expect(factoryPulse.dashboard?.glance.fleet?.pending).toBe(4);
  });

  it('stops asking as soon as it is hidden, mid-poll', async () => {
    subscribe();
    await settle();
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS);
    expect(fetchDashboard).toHaveBeenCalledTimes(2);

    visibility(true);
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS * 3);

    expect(fetchDashboard).toHaveBeenCalledTimes(2);
  });

  it('does not publish a reply that lands after the hide, and reads fresh on show', async () => {
    subscribe();
    await settle();
    const published = factoryPulse.dashboard;
    const slow = deferred<Dashboard>();
    fetchDashboard.mockImplementationOnce(() => slow.promise);
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS);
    expect(fetchDashboard).toHaveBeenCalledTimes(2);

    // Hidden with the read still out; the server answers while hidden.
    visibility(true);
    slow.resolve(dashboard(7));
    await settle();
    expect(factoryPulse.dashboard).toBe(published);

    // Shown: one fresh reading, and it is the one published.
    fetchDashboard.mockImplementation(() => Promise.resolve(dashboard(9)));
    visibility(false);
    await settle();
    expect(fetchDashboard).toHaveBeenCalledTimes(3);
    expect(factoryPulse.dashboard?.glance.fleet?.pending).toBe(9);
  });
});

describe('the health reading (#2789, C7)', () => {
  it('is read again on its own slow beat, so a fixed outage leaves the header', async () => {
    subscribe();
    await settle();
    expect(factoryPulse.health).toEqual(health('down'));

    fetchHealth.mockImplementation(() => Promise.resolve(health('ok')));
    await vi.advanceTimersByTimeAsync(HEALTH_INTERVAL_MS - 1);
    expect(factoryPulse.health).toEqual(health('down')); // never on the 7s tick
    await vi.advanceTimersByTimeAsync(1);

    expect(factoryPulse.health).toEqual(health('ok'));
  });

  it('is not read while the tab is hidden', async () => {
    subscribe();
    await settle();
    expect(fetchHealth).toHaveBeenCalledTimes(1);

    visibility(true);
    await vi.advanceTimersByTimeAsync(HEALTH_INTERVAL_MS * 3);

    expect(fetchHealth).toHaveBeenCalledTimes(1);
  });
});

describe('the first read (D35)', () => {
  it('has not read while the first read is still out, and has once it settles', async () => {
    const first = deferred<Dashboard>();
    fetchDashboard.mockImplementation(() => first.promise);
    subscribe();
    await settle();
    expect(factoryPulse.hasRead).toBe(false);

    first.resolve(dashboard(0));
    await settle();
    expect(factoryPulse.hasRead).toBe(true);
  });

  it('has read after a refused read too, which the header then calls unreadable', async () => {
    fetchDashboard.mockImplementation(() => Promise.reject(new Error('503')));
    fetchRuns.mockImplementation(() => Promise.reject(new Error('503')));
    subscribe();
    await settle();
    expect(factoryPulse.hasRead).toBe(true);
    expect(factoryPulse.dashboard).toBeNull();
  });
});

describe('a read that is still out', () => {
  it('skips the tick on top of it rather than queueing another pair', async () => {
    const slow = deferred<Dashboard>();
    fetchDashboard.mockImplementationOnce(() => slow.promise);
    subscribe();
    await settle();
    expect(fetchDashboard).toHaveBeenCalledTimes(1);

    // Three intervals pass while the server has not answered the first read.
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS * 3);
    expect(fetchDashboard).toHaveBeenCalledTimes(1);

    slow.resolve(dashboard(1));
    await settle();
    // ...and the poll picks straight back up on the next tick.
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS);
    expect(fetchDashboard).toHaveBeenCalledTimes(2);
  });
});

describe('a reply that lands after the shell has gone', () => {
  it('does not write the state a newer reading owns', async () => {
    const slow = deferred<Dashboard>();
    fetchDashboard.mockImplementationOnce(() => slow.promise);
    subscribe();
    await settle();

    // The last factory surface unmounts while the read is in flight.
    unsubscribe();
    slow.resolve(dashboard(9));
    await settle();

    expect(factoryPulse.dashboard).toBeNull();
  });

  it('leaves the timer stopped, so nothing is read after the last subscriber', async () => {
    subscribe();
    await settle();
    unsubscribe();
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS * 3);
    expect(fetchDashboard).toHaveBeenCalledTimes(1);
  });

  it('does not hand the in-flight flag to the read that remounting started', async () => {
    // `stop()` clears `reading` for whoever comes next, so the read it
    // abandoned must not clear it a SECOND time when it finally answers: that
    // released the remount's own read, and the next tick then opened a
    // concurrent pair under the same generation — guard 2 defeated, and with it
    // guard 3, since either of the two could answer last.
    const stale = deferred<Dashboard>();
    fetchDashboard.mockImplementationOnce(() => stale.promise);
    subscribe();
    await settle();
    expect(fetchDashboard).toHaveBeenCalledTimes(1);

    // The shell unmounts and remounts behind the read that is still out.
    unsubscribe();
    const fresh = deferred<Dashboard>();
    fetchDashboard.mockImplementationOnce(() => fresh.promise);
    subscribe();
    await settle();
    expect(fetchDashboard).toHaveBeenCalledTimes(2);

    // The abandoned read answers. It writes nothing (older generation)...
    stale.resolve(dashboard(1));
    await settle();
    expect(factoryPulse.dashboard).toBeNull();

    // ...and the ticks on top of the remount's read are still skipped.
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS * 3);
    expect(fetchDashboard).toHaveBeenCalledTimes(2);

    // So the only reading that lands is the one the live subscriber asked for.
    fresh.resolve(dashboard(2));
    await settle();
    expect(factoryPulse.dashboard?.glance.fleet?.pending).toBe(2);
  });

  it('survives a teardown run twice, and polls for the next subscriber', async () => {
    subscribe();
    await settle();
    const teardown = stop!;
    teardown();
    teardown(); // the count must not go below zero
    stop = null;

    subscribe();
    await settle();
    expect(fetchDashboard).toHaveBeenCalledTimes(2);
  });
});
