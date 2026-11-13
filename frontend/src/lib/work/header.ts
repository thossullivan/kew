/**
 * FUI-19 — the Work item header, as a pure model (DWork x 468–1392, y ≈84–246).
 *
 * The header is the one place on the Work screen where a button can be
 * pressed, so it is also the one place where an invented affordance would do
 * real damage. D draws "Claim", "Hold" and "Flags"; kew offers none of them.
 * The rule this file exists to keep is the surface-convergence rule the CLI and
 * the TUI already obey: **the buttons are `offeredActions(view)`, in the order
 * the projection returned them, and nothing else**. An empty offer is a real
 * answer and renders as no buttons at all — the deviation from the artboard is
 * intentional and is asserted in `header.test.ts`.
 *
 * Everything else here is a reading, not a derivation:
 *
 * * the breadcrumb links to the epic the projection named (`identity.epic_ref`),
 *   and never parses a branch to find one;
 * * `containment` is the route's *declared* posture, so the cell says so in its
 *   title rather than implying an enforced boundary (`provenance` is
 *   `declared`, never measured — the same claim `run-links.ts` keeps separate);
 * * spend is summed from the runs already in hand, and is absent rather than
 *   `$0.00` when no run in hand belongs to the item;
 * * the exception is the projection's own verdict, labelled and toned by
 *   `$lib/dashboard` — the header prints WHY an item is off-normal, it never
 *   decides that from the dimensions itself.
 *
 * D's fourth meta cell is "risk contained". kew has no risk tiers, so it shows
 * containment instead (Factory UI plan §1), and D's "review gate" crumb — an
 * area label kew does not serve — is dropped rather than invented.
 */
import type { Run, WorkAction, WorkException } from '$lib/api';
import { exceptionLabel, exceptionTone, offeredActions, type PressureTone } from '$lib/dashboard';
import { fmtUSD } from '$lib/epic/format';
import { needsYouBlock } from '$lib/needs-you/block';
import { runLink } from '$lib/run-links';
import { safeHref } from '$lib/safe-href';
import { stateWord, type WorkRow } from './list';
import { epicHref, prRef } from './refs';

/** One breadcrumb segment. `href` is null for the segment you are already on. */
export type Crumb = { key: string; text: string; href: string | null };

/**
 * One cell of the meta row. `status` is drawn as a mark plus its word, `text`
 * as an optional label and a value; `money` is the instrument yellow the spend
 * readouts already use.
 */
export type MetaCell = {
  key: string;
  kind: 'status' | 'text';
  /** Prefix word ("PR", "run"), or '' where the value stands alone. */
  label: string;
  value: string;
  /** Trailing word ("spent"), or ''. */
  suffix: string;
  tone: 'neutral' | 'money';
  href: string | null;
  /**
   * True when `href` leaves the console for the forge. The markup opens those
   * in a new tab with `rel="noreferrer"`, the way every other `pr_url` link in
   * the app does; an in-app route (the run page) navigates in place.
   */
  external: boolean;
  /** Hover text: what the reading is, or what the link opens. Never empty. */
  title: string;
  /**
   * For a `status` cell, the word drawn beside the mark; `''` for a `text`
   * cell, whose `value` already IS its word.
   *
   * Two fields because `value` is what the MARK is decided from — the row's
   * raw state, in the run vocabulary — and the word is the meaning's word
   * (`stateWord`). They were one field until #2281 D13 gave the list row
   * `stateWord`: the header went on printing `StatusMark`'s own echo of the
   * raw state, so one screen said DONE in the list and `success` in the header
   * about the same item. The shape still comes off `value`, so the mark and
   * the word can no more disagree here than they can in the row.
   */
  word: string;
};

/**
 * The projection's exception, named and toned. `null` is the ordinary case: an
 * item with `state === 'none'` is not off-normal, and there is nothing to say.
 */
export type ItemException = {
  state: string;
  label: string;
  /** The projection's own sentence for WHY. `''` where it wrote none. */
  detail: string;
  tone: PressureTone;
  blocking: boolean;
};

/**
 * What the runs in hand cost for one item: `total` over every one of them, and
 * `current` for the run the projection names as the item's own (`null` when
 * that run is not in hand, or there is no view to name it).
 */
export type ItemSpend = { total: number; runs: number; current: number | null };

export type ItemHeaderInput = {
  row: WorkRow;
  /** The runs the shell already polled; `null` when that read has not landed. */
  runs: Run[] | null;
};

export type ItemHeaderModel = {
  /** The opaque item id an action is sent against. */
  itemId: string;
  crumbs: Crumb[];
  title: string;
  /** Exactly what the projection offered, safest first. Empty is an answer. */
  actions: WorkAction[];
  /** True when any offered action may not be sent without a written reason. */
  needsReason: boolean;
  meta: MetaCell[];
  /**
   * Why this item is off-normal, or `null`. The state word says WHAT the item
   * waits for ("in review"); this says which exception the projection raised
   * and the sentence it wrote about it — the fact the pre-header detail pane
   * printed, kept on screen rather than folded into a hover title, because a
   * blocking reason a reader has to find with a mouse is one they will miss.
   */
  exception: ItemException | null;
  /**
   * The exception the header PRINTS, which is `exception` unless the needs-you
   * block under it is already carrying the sentence (#2280 D10).
   *
   * Two fields because the fact and the drawing are two questions. D10 deleted
   * the line outright, on the premise that the block below always says it. The
   * block often does — but WHETHER it does is a property of the block's own
   * words, not of which payload the row came from, and gating on the payload
   * got it wrong in both directions at once:
   *
   * * an entry whose reason omits the detail is the `awaiting_human_decision`
   *   branch of `work_reason` (kew/service/attention.py), which words itself
   *   `PR open for #2055 — awaiting review`. The block over such a row says
   *   that reason and the band's sentence, and neither of them is the
   *   exception's; so "kew opens the pull request; you decide whether it
   *   lands." was printed NOWHERE on /work for every item the set names that
   *   way — the committed fixture row included;
   * * a row with a band and no entry at all is in no set, so the line printed
   *   over a block that was already heading the same wait — the one-wait,
   *   two-sentences reading D10 exists to remove, surviving exactly there.
   *
   * So the test is the block's text: `needsYouBlock(row)` is the same
   * derivation the renderer below is handed, and the line is drawn unless that
   * block's summary or its `Why` already contains the exception's detail. A
   * row with no block at all — a QUIET exception (`in_flight`,
   * `review_in_flight`, `terminal_failure`: `QUIET_EXCEPTIONS`, where kew is
   * the next move and nobody is waited on), or any row while `/api/dashboard`
   * is unreadable — keeps the line, because there is no other surface to say
   * it. An exception that wrote no detail has no sentence to rescue, which
   * falls out of the containment test rather than being special-cased.
   */
  exceptionLine: ItemException | null;
  /** The revision an action is decided against; '' when there is no view. */
  revision: string;
};

/**
 * The item and its epic: `#2055 / epic launch-fixes`. An item NOTHING in hand
 * puts in an epic gets one crumb — there is no "no epic" to print.
 *
 * The projection's `identity.epic_ref` is read first and the needs-you row's
 * `epic_ref` behind it, because the two disagree by omission: #2219's
 * projection view carries `identity.epic_ref: null` while the dashboard row for
 * the same item says `ui-refine`, so the Work header for an epic node showed no
 * epic crumb at all (P2 of the UX brief). The fallback is also the ONLY source
 * for an entry row, which has no view.
 */
export function headerCrumbs(row: WorkRow): Crumb[] {
  const crumbs: Crumb[] = [{ key: 'item', text: row.ref, href: null }];
  const epic = row.view?.identity.epic_ref ?? row.entry?.epic_ref;
  if (epic != null && epic !== '')
    crumbs.push({ key: 'epic', text: `epic ${epic}`, href: epicHref(epic) });
  return crumbs;
}

/**
 * What the runs in hand cost for this item.
 *
 * Runs are joined to an item on `String(issue_num)`, the same join `list.ts`
 * takes titles over — which is the GitHub id and nothing else. A Linear item
 * therefore matches no run, and gets NO spend cell rather than a confident
 * `$0.00`; so does an item whose runs the shell has not polled yet.
 */
export function itemSpend(
  runs: Run[] | null | undefined,
  itemId: string,
  runId: string | null = null
): ItemSpend | null {
  let total = 0;
  let matched = 0;
  let current: number | null = null;
  for (const run of runs ?? []) {
    if (String(run.issue_num) !== itemId) continue;
    total += run.cost_usd ?? 0;
    matched += 1;
    if (runId !== null && run.run_id === runId) current = run.cost_usd ?? 0;
  }
  return matched === 0 ? null : { total, runs: matched, current };
}

/**
 * The spend cells, each saying which spend it is (#2786 N19).
 *
 * The header used to print one unlabelled "$3.52 spent" — every run in hand
 * summed — beside an epic card and rail that print the implementing run's
 * $3.10, so one item read two prices with nothing to say they were two
 * different sums. Now the sum is `item total`, and the run the projection
 * names is `this run` beside it. Where the two are the same number (one run,
 * or the others cost nothing) only the total is drawn: two labels on one
 * figure would be a second reading of nothing.
 */
function spendCells(spend: ItemSpend): MetaCell[] {
  const total = fmtUSD(spend.total);
  const cells: MetaCell[] = [];
  if (spend.current !== null && fmtUSD(spend.current) !== total) {
    cells.push(
      cell('run-spend', '', fmtUSD(spend.current), 'what the run this item is on cost', {
        suffix: 'this run',
        tone: 'money'
      })
    );
  }
  cells.push(
    cell(
      'spend',
      '',
      total,
      `every run on this item, summed over ${spend.runs} run${spend.runs === 1 ? '' : 's'} in hand`,
      { suffix: 'item total', tone: 'money' }
    )
  );
  return cells;
}

const cell = (
  key: string,
  label: string,
  value: string,
  title: string,
  extra: Partial<MetaCell> = {}
): MetaCell => ({
  key,
  kind: 'text',
  label,
  value,
  suffix: '',
  tone: 'neutral',
  href: null,
  external: false,
  title,
  word: '',
  ...extra
});

/**
 * The meta row, in the artboard's order: state, containment, spend, PR, run.
 *
 * A cell is present only when its fact is. The state word always is — it is
 * the list row's own word, read through the one function the row reads it
 * through (`stateWord`), so the header and the row the reader clicked can
 * never disagree — and the four that follow come off the view, which an idle
 * ready-queue item does not have at all.
 *
 * `value` stays the row's RAW state because it is what the mark is decided
 * from; `word` is what is printed. Before #2281 D13 the two were the same
 * thing and the invariant held by accident — then the row started printing
 * `stateWord` and the header did not, so the same item read DONE in the list
 * and `success` in the header, on one screen, at one width.
 */
export function headerMeta(input: ItemHeaderInput): MetaCell[] {
  const { row, runs } = input;
  const word = stateWord(row.state);
  const cells: MetaCell[] = [
    cell('state', '', row.state, `this item is ${word}`, { kind: 'status', word })
  ];
  const view = row.view;
  if (view) {
    const route = view.route;
    cells.push(
      cell(
        'containment',
        'containment',
        route.containment,
        `the route's ${route.provenance} containment — a declaration, not a measurement`
      )
    );
  }
  const spend = itemSpend(runs, row.itemId, view?.identity.run_id ?? null);
  if (spend) cells.push(...spendCells(spend));
  const prUrl = view?.artifact.pr_url;
  if (prUrl != null && prUrl !== '') {
    // The served URL passes the gate every served link passes (#3470 C10): a
    // `javascript:` or relative `pr_url` prints its reference as text, never
    // as a link.
    const href = safeHref(prUrl);
    cells.push(
      cell(
        'pr',
        'PR',
        prRef(prUrl),
        href ? 'open the pull request on the forge' : 'the pull request, not a link kew can open',
        { href, external: href != null }
      )
    );
  }
  const link = runLink({ runId: view?.identity.run_id, itemId: row.itemId });
  if (link.available) {
    // The short form is what DWork prints; the whole id rides in the title, so
    // nothing here is the only copy of it.
    cells.push(cell('run', 'run', link.runId.slice(0, 8), link.title, { href: link.href }));
  }
  return cells;
}

/**
 * The exception to print, read from the projection and never derived here.
 *
 * `none` is the absence of an exception, so it is `null` rather than a cell
 * saying "none"; the label table and the tone are `$lib/dashboard`'s, which is
 * the same pair the inbox and the epic pages read, and the tone comes off
 * `blocking` rather than off the exception's name.
 */
export function itemException(exception: WorkException | null | undefined): ItemException | null {
  if (exception == null || exception.state === 'none') return null;
  return {
    state: exception.state,
    label: exceptionLabel(exception.state),
    detail: exception.detail ?? '',
    tone: exceptionTone(exception),
    blocking: exception.blocking
  };
}

/**
 * Whether the one needs-you block under the header already says this
 * exception's sentence — see `exceptionLine`, which this answers.
 *
 * The block is DERIVED here rather than passed in, so the header's reading of
 * it and the block the page renders cannot disagree: `needsYouBlock` is a pure
 * function of the row, and this is the same call with the same argument. The
 * clock it defaults to only reaches `since`, which is not read.
 *
 * Containment rather than equality: the entry's own summary is the reason with
 * the item reference on the front of it (`#2055 — in review: <detail>`), so the
 * sentence is a part of it and not the whole.
 */
function blockSaysException(row: WorkRow, exception: ItemException | null): boolean {
  if (exception === null) return false;
  const block = needsYouBlock(row);
  if (block === null) return false;
  return block.summary.includes(exception.detail) || block.why.includes(exception.detail);
}

/** The whole header, from the row the list selected and the runs in hand. */
export function itemHeader(input: ItemHeaderInput): ItemHeaderModel {
  const { row } = input;
  const view = row.view;
  const actions = view ? offeredActions(view) : [];
  const exception = itemException(view?.exception);
  return {
    itemId: row.itemId,
    crumbs: headerCrumbs(row),
    title: row.title || row.ref,
    actions,
    needsReason: actions.some((a) => a.requires_reason),
    meta: headerMeta(input),
    exception,
    // See `exceptionLine`: the block below is asked whether it is already
    // saying this sentence, and the header says it only where it is not.
    exceptionLine: blockSaysException(row, exception) ? null : exception,
    revision: view?.view_revision ?? ''
  };
}
