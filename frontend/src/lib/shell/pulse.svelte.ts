// One poll for the whole factory shell (FUI-09).
//
// Every factory screen shows the same two live numbers in its header, and each
// of them used to be a page's own `setInterval` (routes/+page.svelte,
// routes/epics/+page.svelte, routes/loops/+page.svelte all poll separately). A
// header that lives in the `(factory)` layout would multiply those polls per
// navigation, so the reading is taken here once, shared by reference, and
// reference-counted: the first subscriber starts the timer and the last one
// stops it. Pages that need the same two payloads read them from here instead
// of fetching them again.
import { browser } from '$app/environment';
import {
  fetchBundle,
  fetchDashboard,
  fetchHealth,
  fetchRuns,
  type Bundle,
  type Dashboard,
  type Health,
  type Run
} from '$lib/api';
import { isUnauthorized } from '$lib/epic/live';

/** The factory heartbeat, from the plan: one read of both payloads every 7s. */
export const PULSE_INTERVAL_MS = 7000;

/** The health reading's own, slower beat (#2789, C7). */
export const HEALTH_INTERVAL_MS = 60_000;

class FactoryPulse {
  /** `null` until the first successful read — never a zeroed stand-in. */
  dashboard = $state<Dashboard | null>(null);
  runs = $state<Run[] | null>(null);
  /**
   * Whether a dashboard read has settled at least once, answered or refused.
   * Until then a missing count is "not read yet", not "could not be read", and
   * the header says `reading…` rather than a bare `—` (D35).
   */
  hasRead = $state(false);
  /** Read once per shell mount: a build stamp only changes on redeploy. */
  bundle = $state<Bundle | null>(null);
  /**
   * The factory health reading (#2298), for the header's "something required is
   * down" link. Deliberately NOT on the 7s tick: every fact in it is a cached
   * host probe or a cached tracker reading, so a seven-second poll would ask
   * faster than the server can answer differently. It is re-read on its own
   * `HEALTH_INTERVAL_MS` beat, though: read once per mount, a "2 down" that was
   * fixed a minute later stood in the header for the rest of the session (C7).
   */
  health = $state<Health | null>(null);

  /**
   * Whether the item the current page is showing needs a human — the first half
   * of the owner's cursor rule, which no fleet-wide count can answer. A page
   * that shows one item sets it in an `$effect` and clears it in the teardown;
   * a page that shows none leaves it alone, and the cursor falls back to the
   * fleet reading.
   */
  viewedItemNeedsYou = $state(false);

  private subscribers = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private healthTimer: ReturnType<typeof setInterval> | null = null;
  /** True while a health read is out; a beat on top of it is skipped. */
  private readingHealth = false;
  /** True while a `refresh()` is out. A tick that arrives on top of one is
   * skipped rather than queued: the reply it is waiting for is the reading.
   * Owned by the generation that set it — see the `finally` in `refresh`. */
  private reading = false;
  /**
   * Which refresh is allowed to write. Bumped by `stop`, so a reply still in
   * flight when the last subscriber leaves — or when the shell is remounted
   * behind it — is dropped instead of overwriting a fresher payload with a
   * staler one (C8).
   */
  private generation = 0;
  /** Bound once so `removeEventListener` gets the same reference. */
  private readonly onVisibility = () => {
    if (document.hidden) return;
    // Back in front: take a reading now rather than showing the numbers from
    // before the tab was hidden for the rest of the interval.
    void this.refresh();
  };

  /**
   * Join the poll. Call from an `$effect` and return the teardown, so the timer
   * lives exactly as long as some factory surface is mounted.
   */
  subscribe(): () => void {
    if (!browser) return () => {};
    this.subscribers += 1;
    if (this.subscribers === 1) this.start();
    // Idempotent: a teardown run twice used to take the count BELOW zero, and
    // then the next subscriber could never bring it back to one — the shell
    // mounted with the poll permanently stopped and no way to tell.
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.subscribers -= 1;
      if (this.subscribers === 0) this.stop();
    };
  }

  private start() {
    void this.refresh();
    if (this.bundle === null) void this.readBundle();
    if (this.health === null) void this.readHealth();
    this.timer = setInterval(() => void this.refresh(), PULSE_INTERVAL_MS);
    this.healthTimer = setInterval(() => void this.readHealth(), HEALTH_INTERVAL_MS);
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  private stop() {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    if (this.healthTimer !== null) clearInterval(this.healthTimer);
    this.healthTimer = null;
    document.removeEventListener('visibilitychange', this.onVisibility);
    // Nothing in flight may write after the last subscriber has gone, and the
    // next subscriber starts with no read out — the abandoned one can no longer
    // release this flag, because the `finally` below checks whose it is.
    this.generation += 1;
    this.reading = false;
  }

  /**
   * Take both readings. A transient failure keeps the last good payload — the
   * header would otherwise blink to em dashes on one dropped poll — while a
   * reading never taken at all stays `null`, which is what renders the dash.
   *
   * Three guards, all from C8, because an unguarded 7s poll is three bugs:
   *
   * 1. **A hidden tab reads nothing.** A backgrounded tab kept asking the
   *    server for numbers nobody could see, for as long as it stayed open.
   * 2. **A tick on top of a slow read is skipped.** Under a server slower than
   *    the interval the polls piled up, each one adding load to the thing that
   *    was already slow.
   * 3. **Only the newest read writes.** Two reads in flight can land in either
   *    order, and the loser used to overwrite the winner — the header silently
   *    showing older counts than it had already received.
   *
   * Guard 1 is checked twice: a read that set out visible and lands hidden is
   * dropped, not published (#2911).
   */
  async refresh(): Promise<void> {
    if (browser && document.hidden) return;
    if (this.reading) return;
    this.reading = true;
    const generation = this.generation;
    let settled: [PromiseSettledResult<Dashboard>, PromiseSettledResult<Run[]>];
    try {
      settled = await Promise.allSettled([fetchDashboard(), fetchRuns()]);
    } finally {
      // Only the read that still owns the current generation may release the
      // flag. `stop()` cleared it on this read's behalf already; clearing it
      // again here would release the flag belonging to the NEWER read a
      // remount started behind this one, and then a tick would open a second
      // concurrent read under the same generation — whichever answered last
      // writing, which is guard 3 undone by guard 2.
      if (generation === this.generation) this.reading = false;
    }
    const [dashboard, runs] = settled;
    if (generation !== this.generation) return; // a newer reading owns the state
    // A reply that lands after the tab was hidden is not published: a page
    // reads more on every new dashboard (Work re-reads its list, pipeline and
    // evidence), so publishing it is a hidden tab asking (#2911). The show
    // takes a fresh reading anyway, which is the one worth drawing.
    if (browser && document.hidden) return;
    if (dashboard.status === 'fulfilled') this.dashboard = dashboard.value;
    if (runs.status === 'fulfilled') this.runs = runs.value;
    this.hasRead = true;
    const rejection = [dashboard, runs].find((r) => r.status === 'rejected');
    if (rejection?.status === 'rejected') this.signOutIfUnauthorized(rejection.reason);
  }

  private async readBundle(): Promise<void> {
    try {
      this.bundle = await fetchBundle();
    } catch {
      // Non-fatal: no stamp, and no warning about not having one.
      this.bundle = null;
    }
  }

  /** Same guards as `refresh`: a hidden tab reads nothing, and a beat on top
   * of a read still out is skipped. */
  private async readHealth(): Promise<void> {
    if (document.hidden || this.readingHealth) return;
    this.readingHealth = true;
    try {
      this.health = await fetchHealth();
    } catch {
      // Non-fatal, and silent: an unread health payload is not a broken factory,
      // so the header shows nothing rather than inventing an alarm.
      this.health = null;
    } finally {
      this.readingHealth = false;
    }
  }

  /** A 401 means the session lapsed; the sign-in page is the only way back. */
  private signOutIfUnauthorized(reason: unknown) {
    if (browser && isUnauthorized(reason)) window.location.href = '/inbox';
  }
}

export const factoryPulse = new FactoryPulse();
