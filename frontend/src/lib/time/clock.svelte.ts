// The console's one motion clock: a 1s tick, shared, and armed by what moves.
//
// #2305's motion rules need a clock with four properties no page-local
// `setInterval` had: it ticks once a second (so bars slide on a `width 1s
// linear` transition rather than stepping), it STOPS when nothing is executing
// or the stream has dropped, it can be paused so a mark never slides out from
// under a pointer, and `prefers-reduced-motion` switches it off entirely.
//
// One clock rather than one per surface, for the same reason `pulse.svelte.ts`
// holds one poll for the whole factory shell: two timers drift, and two cards
// animating off two readings of "now" is a picture of the same second twice.
// Arming is the caller's — a surface calls `arm(running)` from an `$effect`
// and returns its teardown, so the timer lives exactly as long as something
// worth animating is on screen.
import { browser } from '$app/environment';

/** The tick. Bars transition over the same interval, so motion is continuous. */
export const MOTION_TICK_MS = 1000;

/**
 * Whether this reader has asked for less motion.
 *
 * The CSS half of the rule lives in the components (`@media
 * (prefers-reduced-motion: reduce)`), which is what actually stops a halo or a
 * transition. This is the JS half, and it is not decoration: without it the
 * clock would keep ticking and re-rendering every executing card once a second
 * for somebody who has asked the browser for exactly the opposite.
 *
 * Read per call rather than cached, so a reader who changes the setting is
 * honoured on the next arm. `matchMedia` is absent on the server and in older
 * test environments; absent means "no preference expressed".
 */
export function prefersReducedMotion(): boolean {
  if (!browser || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches === true;
}

class MotionClock {
  /** The reading every executing item draws against. */
  now = $state(Date.now());

  private timer: ReturnType<typeof setInterval> | null = null;
  private wanted = false;

  /** True while the clock is actually ticking. */
  get running(): boolean {
    return this.timer !== null;
  }

  /**
   * Ask the clock to run, or to stop.
   *
   * Idempotent, so an `$effect` may call it on every re-run. Disarming TAKES A
   * FINAL READING: a surface that stops the clock (nothing executes any more,
   * the stream dropped, a pointer came to rest on a mark) must not leave the
   * last tick's `now` behind for an age line to keep rendering. The paused
   * reading is the moment of the pause, which is what "the clock stops" means.
   *
   * A reader who prefers reduced motion never gets a timer at all; `now` is
   * still refreshed on each arm, so ages stay honest while nothing animates.
   */
  arm(running: boolean): void {
    this.wanted = running;
    const allowed = running && !prefersReducedMotion();
    if (allowed === this.running) {
      if (!allowed) this.now = Date.now();
      return;
    }
    if (allowed) {
      this.now = Date.now();
      this.timer = setInterval(() => {
        this.now = Date.now();
      }, MOTION_TICK_MS);
      return;
    }
    this.stop();
  }

  /** Stop the timer and take the reading the pause happened at. */
  private stop(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.now = Date.now();
  }

  /**
   * Whether this clock is the right reading for an item's motion.
   *
   * Only `executing` items follow the clock. Everything else is drawn against
   * the frame it arrived in — which is the "past windows never redraw" rule:
   * a settled card must not re-render once a second for a second that has
   * nothing to do with it.
   */
  reading(executing: boolean, frameNow: number): number {
    if (!executing) return frameNow;
    // A stopped clock can be older than the frame (reduced motion takes no
    // ticks at all), and a frame is never in the future of a running one.
    return Math.max(frameNow, this.now);
  }

  /** Test seam: whether anything has asked for the clock. */
  get armed(): boolean {
    return this.wanted;
  }
}

/** The one clock. See the header on why it is shared. */
export const motionClock = new MotionClock();
