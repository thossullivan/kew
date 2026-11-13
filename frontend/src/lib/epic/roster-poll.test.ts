/**
 * #2900 C3 — the Epics list poll: one read at a time, the newest response
 * wins, and a read when the tab comes back.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { READ_HOLD_MS, RosterPoll } from './roster-poll';

/** A read the test answers by hand, in whatever order it likes. */
function harness() {
  const pending: {
    resolve: (v: string) => void;
    reject: (e: unknown) => void;
    signal: AbortSignal;
  }[] = [];
  const landed: string[] = [];
  const failed: unknown[] = [];
  const stale: number[] = [];
  let hidden = false;
  const poll = new RosterPoll<string>({
    // Like `fetch`, a read rejects the moment its signal aborts.
    read: (signal) =>
      new Promise<string>((resolve, reject) => {
        pending.push({ resolve, reject, signal });
        signal.addEventListener('abort', () =>
          reject(new DOMException('aborted', 'AbortError'))
        );
      }),
    land: (v) => landed.push(v),
    fail: (e) => failed.push(e),
    hidden: () => hidden,
    stale: () => stale.push(pending.length)
  });
  return {
    poll,
    pending,
    landed,
    failed,
    stale,
    /** Reads not aborted — for a server that never answers, the fetches open. */
    openReads: () => pending.filter((p) => !p.signal.aborted).length,
    hide: (h: boolean) => {
      hidden = h;
    }
  };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

describe('RosterPoll', () => {
  it('starts no request on a beat while a read is out', async () => {
    const h = harness();
    h.poll.beat();
    h.poll.beat();
    h.poll.beat();
    expect(h.pending).toHaveLength(1);

    h.pending[0].resolve('first');
    await settle();
    expect(h.landed).toEqual(['first']);

    h.poll.beat();
    expect(h.pending).toHaveLength(2);
  });

  it('a failed read frees the next beat', async () => {
    const h = harness();
    h.poll.beat();
    h.pending[0].reject(new Error('503'));
    await settle();
    expect(h.failed).toHaveLength(1);
    h.poll.beat();
    expect(h.pending).toHaveLength(2);
  });

  it('response 1 delayed past response 2 does not overwrite it', async () => {
    const h = harness();
    h.poll.beat();
    h.poll.show();
    expect(h.pending).toHaveLength(2);

    h.pending[1].resolve('newer');
    await settle();
    h.pending[0].resolve('older');
    await settle();

    expect(h.landed).toEqual(['newer']);
  });

  it('showing aborts the read already out, so only the show read lands', async () => {
    const h = harness();
    h.poll.beat();
    h.poll.show();
    expect(h.pending[0].signal.aborted).toBe(true);
    h.pending[0].resolve('older');
    await settle();
    h.pending[1].resolve('newer');
    await settle();
    expect(h.landed).toEqual(['newer']);
    expect(h.failed).toEqual([]);
  });

  it('show aborts an existing read and never exceeds one open fetch', () => {
    const h = harness();
    h.poll.beat(); // read 1: never answered
    for (let i = 0; i < 3; i += 1) {
      h.hide(true);
      h.poll.beat();
      h.hide(false);
      h.poll.show();
      expect(h.openReads()).toBe(1);
    }
    expect(h.pending).toHaveLength(4);
    expect(h.failed).toEqual([]);
  });

  it('a hidden tab reads nothing; showing it reads once', () => {
    const h = harness();
    h.hide(true);
    h.poll.beat();
    h.poll.show();
    expect(h.pending).toHaveLength(0);

    h.hide(false);
    h.poll.show();
    expect(h.pending).toHaveLength(1);
  });

  it('draws nothing after it is stopped', async () => {
    const h = harness();
    h.poll.beat();
    h.poll.stop();
    h.pending[0].resolve('late');
    await settle();
    expect(h.landed).toEqual([]);
    h.poll.show();
    expect(h.pending).toHaveLength(1);
  });

  it('a superseded read that never resolves does not hold the next beat', async () => {
    const h = harness();
    h.poll.beat(); // read 1: never answered
    h.poll.show(); // read 2
    h.pending[1].resolve('shown');
    await settle();
    expect(h.landed).toEqual(['shown']);

    h.poll.beat();
    expect(h.pending).toHaveLength(3);
  });

  it('a newer answer landing aborts the older read still out', async () => {
    const h = harness();
    h.poll.beat(); // read 1: never answered
    h.poll.show(); // read 2
    h.pending[1].resolve('shown');
    await settle();
    expect(h.pending[0].signal.aborted).toBe(true);
    expect(h.pending[1].signal.aborted).toBe(false);
    expect(h.failed).toEqual([]);
  });

  it('stopping aborts every read still out, and reports none as failed', async () => {
    const h = harness();
    h.poll.beat();
    h.poll.show();
    h.poll.stop();
    await settle();
    expect(h.pending.every((p) => p.signal.aborted)).toBe(true);
    expect(h.failed).toEqual([]);
  });

  describe('with a clock', () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    it('a lone read that never resolves holds the beat only for a bounded time', async () => {
      vi.useFakeTimers();
      const h = harness();
      h.poll.beat(); // never answered
      vi.advanceTimersByTime(READ_HOLD_MS - 1);
      h.poll.beat();
      expect(h.pending).toHaveLength(1);

      vi.advanceTimersByTime(1);
      h.poll.beat();
      expect(h.pending).toHaveLength(2);

      // The let-go read still answers in order: the newer one lands, the
      // older one arriving after it is not drawn.
      h.pending[1].resolve('newer');
      await vi.runAllTimersAsync();
      h.pending[0].resolve('older');
      await vi.runAllTimersAsync();
      expect(h.landed).toEqual(['newer']);
    });

    it('a read that never settles is aborted at READ_HOLD_MS, and not reported', async () => {
      vi.useFakeTimers();
      const h = harness();
      h.poll.beat();
      vi.advanceTimersByTime(READ_HOLD_MS - 1);
      expect(h.pending[0].signal.aborted).toBe(false);
      vi.advanceTimersByTime(1);
      expect(h.pending[0].signal.aborted).toBe(true);
      await vi.runAllTimersAsync();
      expect(h.failed).toEqual([]);
    });

    it('a server that never answers has at most one read open, however long it polls', () => {
      vi.useFakeTimers();
      const h = harness();
      let most = 0;
      for (let beat = 0; beat < 60; beat += 1) {
        h.poll.beat();
        most = Math.max(most, h.openReads());
        vi.advanceTimersByTime(5000);
      }
      expect(most).toBe(1);
      expect(h.openReads()).toBeLessThanOrEqual(1);
      // It kept asking, one bounded read after another.
      expect(h.pending.length).toBeGreaterThan(1);
    });

    it('a read aborted at READ_HOLD_MS says the list is stale, once', async () => {
      vi.useFakeTimers();
      const h = harness();
      h.poll.beat();
      vi.advanceTimersByTime(READ_HOLD_MS - 1);
      expect(h.stale).toEqual([]);
      vi.advanceTimersByTime(1);
      await vi.runAllTimersAsync();
      expect(h.stale).toEqual([1]);
    });

    it('a read that answers in time, or is aborted by a show or a stop, is not stale', async () => {
      vi.useFakeTimers();
      const h = harness();
      h.poll.beat();
      h.pending[0].resolve('in time');
      await vi.advanceTimersByTimeAsync(READ_HOLD_MS);
      h.poll.beat();
      h.poll.show();
      h.poll.stop();
      await vi.advanceTimersByTimeAsync(READ_HOLD_MS * 2);
      expect(h.landed).toEqual(['in time']);
      expect(h.stale).toEqual([]);
    });
  });

  it('an answer that lands after the tab hides is not drawn; the show reads and draws', async () => {
    const h = harness();
    h.poll.beat();
    h.hide(true);
    h.pending[0].resolve('hidden');
    await settle();
    expect(h.landed).toEqual([]);

    h.hide(false);
    h.poll.show();
    expect(h.pending).toHaveLength(2);
    h.pending[1].resolve('shown');
    await settle();
    expect(h.landed).toEqual(['shown']);
  });

  it('an older failure after a newer success is dropped', async () => {
    const h = harness();
    h.poll.beat();
    h.poll.show();
    h.pending[1].resolve('newer');
    await settle();
    h.pending[0].reject(new Error('401'));
    await settle();
    expect(h.landed).toEqual(['newer']);
    expect(h.failed).toEqual([]);
  });

  it('a failure newer than the drawn answer is still reported', async () => {
    const h = harness();
    h.poll.beat();
    h.poll.show();
    h.pending[0].resolve('older');
    await settle();
    h.pending[1].reject(new Error('401'));
    await settle();
    expect(h.failed).toHaveLength(1);
  });
});
