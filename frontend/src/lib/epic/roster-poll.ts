/**
 * #2900 C3 — the Epics list's `/api/epics` poll: one read at a time, the newest
 * response wins, and a read when the tab comes back.
 *
 * The page polls every 5 s and each read got heavier in c6. Three rules, all
 * about the reads OUT rather than about any payload:
 *
 * 1. **A beat while a read is out starts nothing.** A read slower than the
 *    interval used to stack the next one behind it, and the next, so a slow
 *    server was asked more often the slower it got.
 * 2. **Only the newest response lands.** Each read is handed a ticket; an
 *    answer whose ticket is older than one already drawn is dropped. Without
 *    it an older roster landing after a newer one flipped heartbeat words and
 *    change marks back.
 * 3. **The tab coming back reads once.** A hidden tab reads nothing (#2285),
 *    so on return the page held a roster up to a full interval old. `show()`
 *    reads at once, even past a read already out — that read began before the
 *    tab was hidden, so `show()` aborts it first and only one read is ever
 *    open, however often the tab is shown.
 *
 * Every read carries an `AbortSignal`, and a read that can no longer matter is
 * aborted, not just ignored: a read older than the newest answer drawn can
 * never land, so it is aborted the moment that answer lands, and any read out
 * longer than `READ_HOLD_MS` is aborted then. So no read holds rule 1's guard
 * forever, and a server that never answers costs one open fetch at a time,
 * never a growing pile of them. No answer a superseded or aborted read brings
 * back is believed — a success or a failure (an older 401 after a newer
 * roster landed must not send the page away, and an abort is not a failure to
 * report).
 *
 * #3054 C7-2 — `hidden()` is asked again when an answer comes back, as the
 * shell pulse does (#2911): a read that set out visible and lands after the
 * tab hid is not drawn, because drawing it starts the rows' change-strip reads
 * from a hidden tab. Nothing is lost: `show()` reads again on return. A read
 * the hold aborts is reported to `stale`, so the page can say its list is old
 * rather than leave an aborted read looking like a current one.
 */

/** How long one read may stay open — a few beats, well past any honest
 * read, so only a hung one is aborted. */
export const READ_HOLD_MS = 20_000;

export interface RosterPollHooks<T> {
  /** Start one read; it must stop when `signal` aborts. */
  read: (signal: AbortSignal) => Promise<T>;
  /** Draw an answer — only ever one newer than the last drawn. */
  land: (value: T) => void;
  /** A read failed. */
  fail: (error: unknown) => void;
  /** Whether the tab is hidden; a hidden tab reads nothing and draws nothing. */
  hidden: () => boolean;
  /** A read was aborted for staying open past the hold. */
  stale?: () => void;
  /**
   * How long one read may stay open; `READ_HOLD_MS` when absent. A caller
   * whose beat is longer than that passes a hold past its own beat, or every
   * read slower than the hold is aborted before it can land.
   */
  holdMs?: number;
}

interface OpenRead {
  controller: AbortController;
  timer: ReturnType<typeof setTimeout>;
}

export class RosterPoll<T> {
  /** The ticket the last read sent was handed. */
  private sent = 0;
  /** The ticket of the newest answer drawn. */
  private drawn = 0;
  /** The reads still open, by ticket — each one holds rule 1's guard. */
  private open = new Map<number, OpenRead>();
  private stopped = false;

  constructor(private readonly hooks: RosterPollHooks<T>) {}

  /** The interval's beat: reads only when no read is out (rule 1). */
  beat(): void {
    if (this.open.size > 0) return;
    this.start();
  }

  /** The tab came back: abort whatever is out, then one read (rule 3). */
  show(): void {
    if (this.stopped || this.hooks.hidden()) return;
    for (const ticket of [...this.open.keys()]) this.abort(ticket);
    this.start();
  }

  /** The page is gone: every open read is aborted, nothing is drawn. */
  stop(): void {
    this.stopped = true;
    for (const ticket of [...this.open.keys()]) this.abort(ticket);
  }

  private start(): void {
    if (this.stopped || this.hooks.hidden()) return;
    this.sent += 1;
    const ticket = this.sent;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      this.abort(ticket);
      if (!this.stopped) this.hooks.stale?.();
    }, this.hooks.holdMs ?? READ_HOLD_MS);
    this.open.set(ticket, { controller, timer });
    const believed = () => !this.stopped && !controller.signal.aborted && ticket >= this.drawn;
    this.hooks.read(controller.signal).then(
      (value) => {
        this.close(ticket);
        // Landed after the tab hid: not drawn and not counted as drawn, so the
        // read `show()` makes on return is the one that lands.
        if (!believed() || this.hooks.hidden()) return;
        this.drawn = ticket;
        // Every read older than this one can never land now.
        for (const older of [...this.open.keys()]) {
          if (older < ticket) this.abort(older);
        }
        this.hooks.land(value);
      },
      (error: unknown) => {
        this.close(ticket);
        if (!believed()) return;
        this.hooks.fail(error);
      }
    );
  }

  /** Abort the read and stop it holding rule 1's guard. */
  private abort(ticket: number): void {
    this.open.get(ticket)?.controller.abort();
    this.close(ticket);
  }

  /** The read is no longer open. */
  private close(ticket: number): void {
    clearTimeout(this.open.get(ticket)?.timer);
    this.open.delete(ticket);
  }
}
