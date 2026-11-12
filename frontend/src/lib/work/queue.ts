/**
 * #2210 — the needs-you queue, as the Work detail pane shows it before an
 * issue is chosen.
 *
 * Work is the tab an operator opens to find out what is waiting for them, and
 * two thirds of it was one sentence telling them to pick something first
 * (owner review, 2026-09-29). The empty pane now answers the question the tab
 * is for: how many things need a person, and which.
 *
 * It composes NOTHING of its own. The count is `headerCounts`' — one
 * needs-you set, one reading (#2207, #2229), so the pane cannot print a number
 * the header contradicts — and the rows are the merged list's own, which is
 * that set's one complete rendering (`entryRows` in `list.ts`). The pane is a
 * second view of the list beside it, never a second derivation of it.
 *
 * Null versus zero is the rule it inherits: a queue that could not be read
 * says so, and a partially read one says `at least N`. "Nothing needs you" is
 * a claim, and this surface only makes it when the whole set answered.
 */
import type { Dashboard } from '$lib/api';
import { rowSentence } from '$lib/needs-you/summary';
import { headerCounts } from '$lib/shell/header-state';
import { rowSince } from '$lib/work/entry-detail';
import type { WorkRow } from '$lib/work/list';

/** How many entries the pane lists before it starts counting the rest. */
export const QUEUE_CAP = 6;

/** What the pane says when the whole set answered and it is empty. */
export const QUEUE_CLEAR = 'nothing needs you';

/** … and when nothing has been read at all. Never the sentence above. */
export const QUEUE_UNREAD = 'needs-you queue not read';

/** One listed wait: what it is, and where the reader goes to act on it. */
export type QueueEntry = {
  /** The row's own identity, which is its `?item=` value or the entry's key. */
  key: string;
  /** "#2055", "go-live", "PR 77" — the No. column's reading of the row. */
  ref: string;
  /** The item's title, or the entry's own sentence; never both, never blank. */
  text: string;
  /**
   * What the row is waiting for, in the one sentence every surface prints —
   * less the leading reference, which the row's own No. column has already
   * said (`rowSentence`). '' for a row the needs-you set does not name — the
   * list also flags a row whose projection says a person is next — and never
   * the title again: a line that repeats `text` is noise under it (#2280 D11).
   */
  summary: string;
  /**
   * How long it has stood, from the set row's own stamp: `held 68d6h`,
   * `waiting 1h00m`. '' when there is no readable stamp to say it from.
   */
  since: string;
  /** A status word `statusShape()` knows, so the mark is the shared one. */
  state: string;
  href: string;
  /** True when `href` leaves the app, so the anchor takes `target`/`rel`. */
  external: boolean;
  /**
   * True when the entry's link is the row selecting itself (`?item=`), which
   * is the one href the PAGE can write better than this module can: it keeps
   * the query the reader is already on (`?filter=needs-you`), where a bare
   * `/work?item=` would drop it and reset the list behind the pane.
   *
   * True for every entry since #2246: every row in the merged list selects in
   * place, entry rows included. It is still read off `href` rather than
   * hard-coded, because the condition is what makes the page's rewrite safe —
   * a row that one day links out must not have its destination replaced with a
   * `?item=` for an id the list does not hold.
   */
  selects: boolean;
};

export type QueueSummary = {
  /** "4 needs you", "at least 2 needs you", or the two sentences above. */
  headline: string;
  /** False when the set was not read, or only partly: the headline says which. */
  read: boolean;
  entries: QueueEntry[];
  /** How many flagged rows the cap left off the list. Never a silent 0. */
  more: number;
  /**
   * The ONE row that needs a person, when exactly one does — the pane expands
   * its block under the heading rather than making the reader click a list of
   * one (#2280 D11). `null` for none and for more than one.
   */
  sole: WorkRow | null;
};

function headlineFor(dashboard: Dashboard | null): { headline: string; read: boolean } {
  const counts = headerCounts(dashboard, null);
  if (counts.needsYou === null) {
    // A partial payload can say how many rows it held, never how many there
    // are; an unread one cannot even say that.
    const floor = counts.needsYouFloor;
    return floor > 0
      ? { headline: `at least ${floor} needs you`, read: false }
      : { headline: QUEUE_UNREAD, read: false };
  }
  return {
    headline: counts.needsYou === 0 ? QUEUE_CLEAR : `${counts.needsYou} needs you`,
    read: true
  };
}

function entryOf(row: WorkRow, nowMs: number): QueueEntry {
  const entry = row.attention;
  // `rowSentence`, not `rowSummary`: the needs-you set writes the item's own
  // reference onto every `epic_wait` reason, and this row prints that reference
  // in a column of its own — so the summary line under the title said `#2055`
  // a second time.
  const said = entry === null ? '' : rowSentence(entry);
  // The entry's sentence is already in `title` for a row the set put in the
  // list; an item whose title no payload named falls back to its ref, which
  // is a reading rather than a blank.
  const text = row.title || row.ref;
  return {
    key: row.itemId,
    ref: row.ref,
    // Never the title again, and the model decides that rather than the
    // markup: an entry row the set composed carries this very sentence as its
    // title — see `entryRows` — so there is nothing to add under it. The test
    // is on the tail, so a title that ENDS in the sentence, with a reference
    // in front of it, is caught as the repeat it is.
    summary: text.endsWith(said) ? '' : said,
    since: entry === null ? '' : rowSince(entry, nowMs),
    text,
    state: row.state,
    href: row.href,
    external: row.external,
    // Every row in the merged list selects itself (`WorkRow.href`, #2246), so
    // this is the row's own answer and not a second rule about entries.
    selects: !row.external
  };
}

/**
 * The pane's reading: the queue's headline, and the rows behind it.
 *
 * `rows` is the merged list in the order it is drawn (newest first), so the
 * pane lists the same rows in the same order as the list beside it.
 */
export function needsYouSummary(
  rows: WorkRow[],
  dashboard: Dashboard | null,
  nowMs: number = Date.now()
): QueueSummary {
  const flagged = rows.filter((row) => row.needsYou);
  const { headline, read } = headlineFor(dashboard);
  return {
    headline,
    read,
    entries: flagged.slice(0, QUEUE_CAP).map((row) => entryOf(row, nowMs)),
    more: Math.max(0, flagged.length - QUEUE_CAP),
    sole: flagged.length === 1 ? flagged[0] : null
  };
}
