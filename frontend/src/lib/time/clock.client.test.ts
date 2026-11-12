/**
 * #2305 — the motion clock's four rules.
 *
 * A `.client.test.ts` because the behaviour under test IS a timer and a media
 * query: neither exists in an SSR render.
 *
 *  * it ticks once a second while something is executing;
 *  * it stops when nothing is, or when the stream has dropped (both reach it as
 *    `arm(false)`), and takes a final reading as it stops — a paused clock must
 *    not leave the previous tick's "now" behind for an age line to render;
 *  * only executing items read it (`reading`), which is how a settled card
 *    avoids re-rendering once a second for a second with nothing in it;
 *  * `prefers-reduced-motion` means no timer at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$app/environment', () => ({ browser: true }));

const { motionClock, MOTION_TICK_MS, prefersReducedMotion } = await import('./clock.svelte');

/** Stub `matchMedia` so the reduced-motion branch is reachable. */
function prefersReduce(reduce: boolean): void {
  window.matchMedia = ((query: string) =>
    ({ matches: reduce && query.includes('prefers-reduced-motion'), media: query })) as never;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-06T12:00:00Z'));
  prefersReduce(false);
});

afterEach(() => {
  motionClock.arm(false);
  vi.useRealTimers();
});

describe('the motion clock', () => {
  it('ticks once a second while it is armed', () => {
    motionClock.arm(true);
    const start = motionClock.now;
    vi.advanceTimersByTime(MOTION_TICK_MS);
    expect(motionClock.now).toBe(start + MOTION_TICK_MS);
    vi.advanceTimersByTime(MOTION_TICK_MS * 3);
    expect(motionClock.now).toBe(start + MOTION_TICK_MS * 4);
    expect(MOTION_TICK_MS).toBe(1000);
  });

  it('stops dead when nothing executes any more', () => {
    motionClock.arm(true);
    vi.advanceTimersByTime(MOTION_TICK_MS);
    motionClock.arm(false);
    const paused = motionClock.now;
    vi.advanceTimersByTime(MOTION_TICK_MS * 10);
    expect(motionClock.now).toBe(paused);
    expect(motionClock.running).toBe(false);
  });

  it('takes its reading at the moment of the pause, not at the last tick', () => {
    // Hovering a mark pauses the clock mid-second. If the pause kept the
    // previous tick's reading, every age on screen would jump backwards.
    motionClock.arm(true);
    vi.advanceTimersByTime(MOTION_TICK_MS + 400);
    motionClock.arm(false);
    expect(motionClock.now).toBe(Date.now());
  });

  it('is idempotent, so an effect may re-run as often as it likes', () => {
    motionClock.arm(true);
    const timers = vi.getTimerCount();
    motionClock.arm(true);
    motionClock.arm(true);
    expect(vi.getTimerCount()).toBe(timers);
    vi.advanceTimersByTime(MOTION_TICK_MS);
    expect(motionClock.now).toBe(Date.now());
  });

  it('runs no timer at all for a reader who prefers reduced motion', () => {
    prefersReduce(true);
    expect(prefersReducedMotion()).toBe(true);
    motionClock.arm(true);
    expect(motionClock.running).toBe(false);
    const still = motionClock.now;
    vi.advanceTimersByTime(MOTION_TICK_MS * 10);
    expect(motionClock.now).toBe(still);
  });

  it('still refreshes its reading under reduced motion, so ages stay honest', () => {
    prefersReduce(true);
    motionClock.arm(true);
    vi.advanceTimersByTime(MOTION_TICK_MS * 30);
    motionClock.arm(true);
    expect(motionClock.now).toBe(Date.now());
  });

  it('is read only by what is executing — past windows never redraw', () => {
    motionClock.arm(true);
    vi.advanceTimersByTime(MOTION_TICK_MS * 5);
    const frame = Date.now() - MOTION_TICK_MS * 5;
    expect(motionClock.reading(false, frame)).toBe(frame);
    expect(motionClock.reading(true, frame)).toBe(motionClock.now);
  });

  it('never reads older than the frame it is drawn with', () => {
    // A stopped clock (reduced motion, or a stream that dropped) can be older
    // than the newest frame; an age must not run backwards because of it.
    motionClock.arm(false);
    const stopped = motionClock.now;
    vi.setSystemTime(new Date('2026-10-06T12:10:00Z'));
    expect(motionClock.reading(true, Date.now())).toBe(Date.now());
    expect(Date.now()).toBeGreaterThan(stopped);
  });
});
