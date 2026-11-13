/**
 * Staleness badges that CARRY THEIR CAUSE (#2303).
 *
 * A badge saying "stale" and nothing else makes the reader go and find out what
 * moved, which is the work the badge was supposed to save. There are exactly two
 * causes, and they are different kinds of thing:
 *
 * * **review stale** — a SELF cause. This branch moved: the head is not the
 *   commit the last verdict was given. kew re-reviews on its own, so this is
 *   information, never a person's move.
 * * **needs rebase** — an UPSTREAM cause. The base moved. `behind` the factory
 *   catches up to by itself; `conflicted` it cannot, and that one — and only
 *   that one — is magenta (DESIGN.md: magenta means a person is the next move).
 *
 * Both badges are drawn off ONE journal row, and the base half of it can be
 * carried forward over ticks GitHub answered UNKNOWN on — so the rebase badge
 * is bounded by the SERVER's own age bound for that reading
 * (`BASE_READING_MAX_AGE_MS`). Two surfaces reaching opposite conclusions from
 * one row is the thing that makes a reader stop trusting either.
 *
 * No fetch of its own and no DOM: `ui/FreshnessBadge.svelte` draws what this
 * returns, and `freshness.test.ts` asserts the mapping per `review_state` and
 * per `base.state`. `readFreshness` does walk the endpoint's pages, but over a
 * reader it is HANDED — so the paging rule (and what it confesses when it runs
 * out of pages) is assertable without a server. The clock is a parameter for
 * `format.ts`'s reason — a duration printed from a clock a test cannot set is a
 * reading nobody can assert.
 */
import type { BaseState, PrFreshness, PrFreshnessRow } from '$lib/api';
import { parseInstant } from '$lib/time';
import { safeHref } from '$lib/safe-href';

/** Which of the two badges this is. A new cause gets a new kind, not a reuse. */
export type BadgeKind = 'review-stale' | 'needs-rebase';

/** One reason a badge is on screen, with the commit it is about. */
export type FreshnessCause = {
  /** `head` when this branch moved, `base` when the thing upstream did. */
  origin: 'head' | 'base';
  /** The sentence the popover prints. Never empty. */
  text: string;
  /** The commit this cause is about, shortened for reading; '' when none. */
  commit: string;
  /** Where that commit lives, or '' — never a URL this file invented. */
  href: string;
  /** When the cause was first observed; null when nothing recorded it. */
  since: string | null;
};

export type StalenessBadge = {
  kind: BadgeKind;
  /** The two words on the badge itself. */
  label: string;
  /**
   * Whether a PERSON is the next move. The ONLY thing that turns the badge
   * magenta, and true for exactly one state: a conflict the factory cannot
   * resolve. A base that is merely behind is kew's own job.
   */
  needsAPerson: boolean;
  /** Every cause behind this badge, in reading order. Never empty. */
  causes: FreshnessCause[];
};

/** `base.state` values that put a badge on screen. */
const REBASE_STATES = new Set(['behind', 'conflicted']);

/**
 * How old the journaled merge state behind `base` may be and still earn a
 * badge — a MIRROR of the server's own bound
 * (`kew.service.work.JOURNALED_MERGE_STATE_MAX_AGE_S`, one hour), which refuses
 * a reading past it rather than raising a blocking `merge_conflict` from a
 * journal nobody has written to since.
 *
 * It is mirrored here because the driver CARRIES a merge state forward over a
 * tick GitHub answered UNKNOWN on (`graph/driver/journal.py`), so `base.state`
 * can be an arbitrarily old reading with `base.at` saying when it was actually
 * taken. Painting the rationed magenta needs-rebase badge off it while
 * `cached_pull_request` refuses the same row is two surfaces reaching opposite
 * conclusions from one journal row — and the magenta one is the one that asks a
 * person to go and rebase something that may have been rebased hours ago.
 */
export const BASE_READING_MAX_AGE_MS = 60 * 60 * 1000;

/** The age phrase in the note below, so the sentence and the bound cannot drift. */
const BASE_READING_MAX_AGE_LABEL = 'an hour';

/**
 * Whether `base` is a reading recent enough to act on.
 *
 * False for a reading with no instant at all — an age nobody can establish is
 * not an age — and for one past `BASE_READING_MAX_AGE_MS`. Either way the badge
 * is withheld and `unreadBaseNote` says why, which is the difference between
 * "nothing moved" and "nobody looked recently".
 */
export function baseReadingIsCurrent(base: BaseState, nowMs: number): boolean {
  const at = parseInstant(base.at);
  return at !== null && nowMs - at <= BASE_READING_MAX_AGE_MS;
}

/** The one `base.state` a person has to clear. */
const PERSON_BASE_STATES = new Set(['conflicted']);

/** How much of a sha reads as an identifier without becoming noise. */
const SHORT_SHA = 7;

/** A sha shortened the way a commit is spoken about; '' when there is none. */
export function shortSha(sha: string | null | undefined): string {
  return (sha ?? '').trim().slice(0, SHORT_SHA);
}

/**
 * Where one commit of this pull request lives.
 *
 * A SUFFIX on the URL the server gave us — never a URL composed out of a repo
 * slug this file guessed, which is how a surface sends people to a 404 with
 * kew's name on it (see `epic/node-popover.ts`). Only GitHub's own `/pull/<n>`
 * shape is extended, because `/commits/<sha>` under it is GitHub's route and
 * nobody else's; anything else gets '' and the popover prints the sha alone.
 */
export function commitHref(prUrl: string | null, sha: string): string {
  const short = shortSha(sha);
  // The served URL passes safeHref (C10) before anything is suffixed to it.
  const url = safeHref(prUrl);
  if (short === '' || url === null || !/\/pull\/\d+\/?$/.test(url)) return '';
  return `${url.replace(/\/$/, '')}/commits/${short}`;
}

/** The head cause: this branch moved past the commit that was reviewed. */
function headCause(row: PrFreshnessRow): FreshnessCause | null {
  const verdict = row.last_verdict;
  const head = shortSha(row.latest_head.sha);
  if (verdict === null || head === '') return null;
  const reviewed = shortSha(verdict.head_sha);
  return {
    origin: 'head',
    text:
      `the head moved to ${head} after the round ${verdict.round} ` +
      `${verdict.verdict} verdict on ${reviewed}`,
    commit: head,
    href: commitHref(row.pr_url, head),
    since: row.latest_head.at
  };
}

/** The base cause: the thing upstream moved, conflict or not. */
function baseCause(base: BaseState): FreshnessCause {
  return {
    origin: 'base',
    text: base.reason,
    commit: '',
    href: '',
    since: base.since
  };
}

/**
 * Every badge one row earns, most consequential first.
 *
 * A conflict outranks a moved head: re-reviewing a branch that cannot merge
 * proves nothing, so the rebase is the move either way. Both can be on at once —
 * that is the point of a badge that carries its cause rather than one word that
 * collapses them.
 *
 * `nowMs` is a parameter rather than a `Date.now()` inside, for `format.ts`'s
 * reason: a badge gated on the age of a reading is a conclusion no test could
 * assert from a clock it cannot set.
 */
export function badgesFor(row: PrFreshnessRow, nowMs: number = Date.now()): StalenessBadge[] {
  const out: StalenessBadge[] = [];
  const base = row.base.state;
  // The base reading has to be one the server would still act on: it is a
  // journal row that can be carried forward over ticks, and the rebase badge is
  // the only magenta on this screen.
  if (REBASE_STATES.has(base) && baseReadingIsCurrent(row.base, nowMs)) {
    out.push({
      kind: 'needs-rebase',
      label: 'needs rebase',
      needsAPerson: PERSON_BASE_STATES.has(base),
      causes: [baseCause(row.base)]
    });
  }
  if (row.review_state === 'stale') {
    const cause = headCause(row);
    out.push({
      kind: 'review-stale',
      label: 'review stale',
      // kew starts the next round itself; nobody is being asked for anything.
      needsAPerson: false,
      causes: [
        cause ?? {
          origin: 'head',
          text: 'the head moved after the last review verdict',
          commit: shortSha(row.latest_head.sha),
          href: commitHref(row.pr_url, row.latest_head.sha),
          since: row.latest_head.at
        }
      ]
    });
  }
  return out;
}

/**
 * What the popover says about a row that earns NO badge but is not clean either.
 *
 * `unknown` is the one that matters: an unjournaled merge state is not a clean
 * tree, and a surface that drew nothing for it would be reporting a guarantee
 * nobody read (DESIGN.md: say what an empty or failed read means). '' when the
 * row really is clean, or when a badge already says it.
 */
export function unreadBaseNote(row: PrFreshnessRow, nowMs: number = Date.now()): string {
  if (REBASE_STATES.has(row.base.state)) {
    // A rebase state whose reading is too old to act on earns no badge, so
    // this line is the ONLY thing left that says the row is not clean.
    if (baseReadingIsCurrent(row.base, nowMs)) return '';
    return row.base.at === null
      ? `${row.base.reason}, but nothing records when that was read — ` +
          'staleness here is unknown'
      : `${row.base.reason}, but that reading is over ${BASE_READING_MAX_AGE_LABEL} ` +
          'old — staleness here is unknown until the driver reads it again';
  }
  if (row.base.state === 'clean') return '';
  return row.base.reason;
}

/** The rows of one freshness page, keyed by item id. */
export function byItem(page: PrFreshness | null): Map<string, PrFreshnessRow> {
  return new Map((page?.rows ?? []).map((row) => [row.item_id, row]));
}

/** Rows asked for per page. The endpoint's own ceiling, so one epic is almost
 * always ONE request. */
export const FRESHNESS_PAGE = 200;

/** How many pages one read follows before it stops and SAYS it stopped. A
 * bound rather than a while-loop: a server that answered the same cursor
 * forever must not be able to spin a poll that re-runs every few seconds. */
export const FRESHNESS_MAX_PAGES = 5;

/**
 * One epic's freshness, read across as many pages as it takes — and honest
 * about the rows it did not reach.
 *
 * The endpoint is bounded and cursored, and the first page is NOT the epic:
 * before #2303's review this read one global page of 50 and ignored both
 * `next_cursor` and `unavailable`, so an epic past the page filled drew no
 * badge on any node — indistinguishable, on screen, from nothing being stale.
 * Three things fix that and all three are here: the ref goes into the query
 * (`fetchPrFreshness`), the cursor is followed, and whatever is still missing
 * — a page cap hit, a sub-read the server could not do — comes back in
 * `truncated`/`unavailable` for `freshnessNotice` to say out loud.
 */
export type FreshnessReading = {
  /** Every row read, keyed by item id — the map the DAG cards join onto. */
  rows: Map<string, PrFreshnessRow>;
  /** Reads the server named as not having happened, in page order. */
  unavailable: string[];
  /** True when the walk stopped with a cursor still outstanding: there are
   * rows this reading does not cover, and nothing may be concluded of them. */
  truncated: boolean;
};

export async function readFreshness(
  read: (cursor: string | null) => Promise<PrFreshness>
): Promise<FreshnessReading> {
  const rows = new Map<string, PrFreshnessRow>();
  const unavailable: string[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < FRESHNESS_MAX_PAGES; page += 1) {
    const next: PrFreshness = await read(cursor);
    for (const [id, row] of byItem(next)) rows.set(id, row);
    unavailable.push(...(next.unavailable ?? []));
    cursor = next.next_cursor;
    if (cursor === null) return { rows, unavailable, truncated: false };
  }
  return { rows, unavailable, truncated: true };
}

/**
 * The reading a read that THREW leaves behind.
 *
 * A failed read used to leave `freshness` null, and `freshnessNotice(null)` is
 * null — so a persistently failing `/api/prs/freshness` drew no badge on any
 * node and no confession either: on screen, exactly a clean epic (reviewer
 * finding, #2303). The failure goes into the same `unavailable` channel a
 * server-reported one does, so one line says it for all three causes.
 */
export function failedReading(
  previous: FreshnessReading | null,
  error: unknown
): FreshnessReading {
  return {
    // The rows that WERE read stand: an unread refresh is not evidence that
    // nothing moved, and dropping them would blank every badge on the page.
    rows: previous?.rows ?? new Map(),
    // Replaced, not appended: a poll that keeps failing must not grow one line
    // per tick on the strip.
    unavailable: [`the freshness read failed: ${readFailureText(error)}`],
    truncated: previous?.truncated ?? false
  };
}

/** The sentence a thrown read contributes — never an empty one. */
function readFailureText(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error ?? '');
  return text.trim() === '' ? 'the reason was not reported' : text.trim();
}

/**
 * The one line that says a badge's ABSENCE proves nothing. Null in the
 * ordinary case, where every row was read and every card can be trusted.
 *
 * Silence on failure is not silence (DESIGN.md): a node with no badge reads as
 * "nothing moved here", and that is only true when the row behind it was
 * actually read. Every way it can be untrue gets a sentence — a read the server
 * could not do, rows this client never reached, and the read itself throwing
 * (`failedReading`, which is how a persistently failing endpoint says so
 * instead of drawing a page that looks clean).
 */
export function freshnessNotice(reading: FreshnessReading | null): string | null {
  if (reading === null) return null;
  if (reading.unavailable.length > 0) {
    return `staleness unknown — could not read ${reading.unavailable.join(', ')}`;
  }
  if (reading.truncated) {
    return `staleness unknown past the first ${reading.rows.size} artifacts of this epic`;
  }
  return null;
}
