/**
 * FUI-31 — which description read the Work page is still waiting for.
 *
 * The page takes one description read per selection, and the shell's tick takes
 * it again while FUI-31 is refusing it. `rereadsDescription` decides which
 * ANSWERS are worth taking again; this decides whether a tick may take one at
 * all, and both of its rules are about the reads the page has OUT rather than
 * about any payload:
 *
 * 1. **Never a second read of the item being read.** The endpoint refuses a
 *    read of an item whose read is already in flight (`SAME_ITEM_REASON`), so a
 *    tick that fired one would spend a retry on a refusal of its own making —
 *    and that refusal, landing after the read it duplicated, would replace the
 *    body that had just arrived with a reason and no link to leave by.
 * 2. **Only the newest read owns the screen.** A selection of A -> B -> A has
 *    two reads of A in flight at once, which is why "a read is out" cannot be
 *    keyed on the item id: the earlier A read's completion would hand the flag
 *    back while the later one is still out, and rule 1 would be lost for
 *    exactly the sequence that needs it. So each read is handed a TICKET — the
 *    selection it was taken for — and a read the selection has moved past
 *    neither writes to the screen nor clears the flag its successor holds.
 *
 * A `fetch` already sent cannot be unsent, so nothing here cancels: what a
 * superseded ticket changes is that no answer it brings back is believed. The
 * page holds this as a plain, non-reactive field the way it holds `detailItem`:
 * nothing renders from it, and reactive state an effect both read and wrote
 * would re-run itself.
 */
import type { ItemDescription } from '$lib/api';
import { rereadsDescription } from './evidence';

export class DescriptionReads {
  /** The selection on screen. Each read carries the value it was taken at. */
  private selection = 0;
  /** Whether a read of THAT selection is out. */
  private out = false;
  /** How many times the tick has re-read this selection's description. */
  private tries = 0;

  /**
   * The selection moved. Reads still in flight belong to the item just left —
   * or to the last time this item was selected, which is the same thing here.
   */
  select(): void {
    this.selection += 1;
    this.out = false;
    this.tries = 0;
  }

  /** Take a read for the selection on screen, handing back its ticket. */
  begin(): number {
    this.out = true;
    return this.selection;
  }

  /** Whether `ticket`'s answer is still the one the screen is waiting for. */
  owns(ticket: number): boolean {
    return ticket === this.selection;
  }

  /**
   * Retire `ticket`, whatever it answered. A read the selection has moved past
   * leaves the flag alone: it is not the read that is out.
   */
  end(ticket: number): void {
    if (this.owns(ticket)) this.out = false;
  }

  /**
   * Whether this tick takes the read again — counting the retry when it does,
   * so the bound is spent by the tick that fires rather than by whatever answer
   * that read happens to come back with.
   */
  claimReread(description: ItemDescription | null | undefined): boolean {
    if (this.out) return false;
    if (!rereadsDescription(description, this.tries)) return false;
    this.tries += 1;
    return true;
  }
}

/**
 * #2933 — which answer to a re-read the shell's pulse fires is newer than the
 * one on screen.
 *
 * The pulse re-reads the Work list, the selected item's pipeline and its
 * evidence every tick, and nothing holds those reads to arrive in the order
 * they were sent: pulse N's read can be slow and pulse N+1's answer first. The
 * page would draw N+1, then N's late answer over it — older data under a
 * newer reading. So each read is handed a GENERATION when it is sent, and an
 * answer is drawn only if no later read's answer has been drawn already.
 *
 * Nothing is cancelled and nothing is waited for: an older answer that lands
 * before any newer one is still drawn — it is the newest reading in hand — so a
 * pulse whose reads answer in order behaves exactly as it did before. The page
 * holds one of these per reader as a plain, non-reactive field, as it holds
 * `DescriptionReads`.
 *
 * A per-item reader also has a SELECTION EPOCH. Only drawn answers advance the
 * newest-drawn mark, so after A -> B -> A the first A selection's read — still
 * out, never superseded by a drawn answer — would pass as the newest in hand
 * and draw into a pane that was cleared for the second. `select()` starts an
 * epoch, and no read sent before it is drawn after it.
 */
export class NewestReads {
  /** The generation the last read sent was handed. */
  private sent = 0;
  /** The generation of the newest answer drawn. */
  private drawn = 0;
  /** The first generation of the current selection epoch. */
  private epoch = 0;

  /** The selection moved: every read already sent belongs to the one left. */
  select(): void {
    this.epoch = this.sent + 1;
  }

  /** Send a read, handing back its generation. */
  begin(): number {
    this.sent += 1;
    return this.sent;
  }

  /**
   * Whether `generation`'s answer may be drawn — marking it drawn when it may.
   * An answer older than one already drawn may not, nor one sent before the
   * current selection epoch began.
   */
  take(generation: number): boolean {
    if (generation < this.epoch || generation < this.drawn) return false;
    this.drawn = generation;
    return true;
  }
}

/**
 * #3055 (C7-3) — one pulse read out at a time.
 *
 * `NewestReads` orders the answers but sends every read it is asked for, so a
 * server slower than the pulse stacked one more open request per tick. A
 * reader that takes a `SingleRead` asks `begin()` first: while a read is out
 * it answers `null` and the pulse skips, and the read in flight is the newest
 * the page has asked for. `abort()` drops the read out — superseded by a new
 * load, or by the page going away — so it does not hold a request open for an
 * answer nobody will draw.
 */
export class SingleRead {
  private out: AbortController | null = null;

  /** The signal for a new read, or `null` while one is still out. */
  begin(): AbortSignal | null {
    if (this.out !== null) return null;
    this.out = new AbortController();
    return this.out.signal;
  }

  /** The read `signal` belongs to has settled; a later one may be sent. */
  end(signal: AbortSignal): void {
    if (this.out?.signal === signal) this.out = null;
  }

  /** Abort the read out, if any, and let the next be sent. */
  abort(): void {
    this.out?.abort();
    this.out = null;
  }
}
