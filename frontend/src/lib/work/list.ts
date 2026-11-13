/**
 * FUI-18 — the Work screen's issue list, as a pure model.
 *
 * The list on the left of DWork (x 0–420, y 56–720) is one table over two
 * payloads that do not overlap:
 *
 * * `/api/dashboard/work` — the canonical work projection. It covers every item
 *   kew has actually *touched*: eighteen dimensions, one derived exception and
 *   one safest action per item. It carries no title.
 * * `/api/dashboard`'s `glance.ready_queue` — the open issues kew has NOT
 *   touched. Idle work, with a title.
 *
 * Neither is a superset of the other, so the artboard's ten rows are the two
 * merged, keyed on the durable string `item_id`, projection first because it is
 * the one with facts. `item_id` is opaque here and stays opaque: a Linear key
 * (`SFD-12`) rides through as itself, and nothing in this file parses an id or
 * coerces one to a number.
 *
 * Titles come from whichever payload in hand happens to name the item — the
 * ready queue, the glance's completions and candidates, and `/api/runs`. The
 * projection has no title field, and inventing one ("item 2055") would be a
 * stand-in for a fact; an item nobody named keeps an empty title and the row
 * shows its number alone.
 *
 * `itemState` is the interim state word, and is marked for deletion: #2050
 * gives kew a closed run-state vocabulary, and FUI-29 deletes this helper in
 * favour of the served word. Until then it is the ONE place the Work screen
 * decides a state word, so there is exactly one thing for FUI-29 to remove.
 */
import type {
  Dashboard,
  DashboardRow,
  ReadyIssue,
  Run,
  WorkProjection,
  WorkView
} from '$lib/api';
import { orderRows, rowKey, typeLabel } from '$lib/dashboard';
import { statusWord } from '$lib/epic/render';
import { statusShape } from '$lib/factory/status';
import { JUDGMENT_ROW_SOURCES } from '$lib/shell/header-state';
import { rowSentence } from '$lib/needs-you/summary';
import { itemRef } from '$lib/utils';

/** One row of the list. */
export type WorkRow = {
  /** The durable item id, and the `?item=` value. Opaque — never parsed. */
  itemId: string;
  /** The No. column, as the artboard draws it: `2055` bare, `SFD-12` bare. */
  number: string;
  /** Display reference (`#2055`, `SFD-12`) for prose and accessible names. */
  ref: string;
  /** Empty when no payload in hand names the item — never a stand-in. */
  title: string;
  /** A status word `statusShape()` knows (factory/status.ts). */
  state: string;
  /** True when the needs-you set names this item. */
  needsYou: boolean;
  /** The projection's view, or `null` for an item only the set knows about. */
  view: WorkView | null;
  /**
   * The ready queue's tier label for this item (#2785): `null` when the queue
   * does not hold it, `''` when it does with no tier label.
   */
  tier: string | null;
  /**
   * The needs-you entry this row stands for, when the row EXISTS because of one
   * — a stalled epic, an orphan PR, a hold on an item neither payload listed.
   * `null` for an ordinary row, including one the set also names: there, the
   * projection or the ready queue is what put it in the list.
   *
   * It carries the two things such a row cannot get from anywhere else: the
   * sentence to print where a title would go, and where to send the reader.
   */
  entry: DashboardRow | null;
  /**
   * The needs-you entry that NAMES this item, whichever payload drew the row.
   *
   * `entry` says the row exists because of the set; this says the set names it.
   * They are the same object for an entry row, and for a row the projection
   * composed `entry` is null while this is the set's own row — which carries
   * the three things the projection does not: the sentence every surface
   * prints, the stamp the wait began at, and the command kew computed to end
   * it. The one needs-you block reads them off here (#2280 D9), so a merge
   * decision the projection composed gets the same Why / Since / Do this block
   * an unlisted hold has had since #2246.
   */
  attention: DashboardRow | null;
  /**
   * Where the row's link goes: **always** this row selecting itself (`?item=`).
   *
   * Until #2246 a row standing for a needs-you entry linked OUT, to the epic or
   * the artifact, because `?item=` naming something no payload listed selects
   * nothing (`selectedRow`). That is no longer true of an entry row: its
   * identity is `rowKey(entry)` where it names no item, which `selectedRow`
   * matches, so the URL selects it like any other row. Five of the Work
   * screen's seven rows were entry rows, and clicking any of them left the
   * Work screen (P1 of the UX brief) — so every row now selects in place, and
   * where the entry actually lives is a secondary link in the detail pane
   * (`entryDetail` in `$lib/work/entry-detail`).
   */
  href: string;
  /**
   * True when `href` leaves the app, so the anchor takes `target`/`rel`.
   *
   * Always false since #2246, and kept rather than deleted because it is what
   * a row that DOES link out would set, and `list.test.ts` holds the whole
   * list to false over the live-shaped fixture — an assertion needs a field to
   * read. Nothing on the Work page branches on it any more.
   */
  external: boolean;
};

/** Everything the list is built from. Every field may be missing on its own. */
export type WorkListInput = {
  projection: WorkProjection | null;
  dashboard: Dashboard | null;
  runs: Run[] | null;
};

/** The `?item=` link for a row that selects itself. */
function selfHref(itemId: string): string {
  return '/work?item=' + encodeURIComponent(itemId);
}

/**
 * Own-property lookup, because the tables below are object literals and the
 * state words come off the wire: `EXCEPTION_STATE_WORD['constructor']` would
 * otherwise answer with `Object.prototype`'s, walking straight past the
 * fallback. Same guard, same reason, as `own` in factory/status.ts.
 */
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/**
 * `WorkExecution.state` → the state word, when the exception adds nothing.
 *
 * The four members are the whole vocabulary of the field (`_execution_dto` in
 * kew/src/kew/service/work.py, typed in api.ts).
 */
export const EXECUTION_STATE_WORD: Record<string, string> = {
  not_started: 'idle',
  in_flight: 'working',
  completed: 'success',
  terminal: 'flawed'
};

/**
 * `WorkException.state` → the state word it overrides the execution word with.
 *
 * The exception is the ONE verdict every surface reads, so it outranks the raw
 * execution state: an item whose run completed is not "success" while a human's
 * merge decision is outstanding. The word answers one question — **what is this
 * item waiting for?**
 *
 * * `working` — kew's own machinery is still moving it. `review_remediation_due`
 *   and `review_round_due` are that too (#2328): the last review round returned
 *   NEEDS_CHANGES or NEEDS_FULL, so the next move is kew's own remediation or
 *   its next full round, and the item is cyan rather than magenta until a
 *   verdict ends the cycle.
 * * `in review` — a review verdict on a published artifact is outstanding. Two
 *   exceptions read it: a HUMAN merge decision, and `review_in_flight`, where
 *   the round kew is reading the artifact with has not returned yet (#2279).
 *   Neither is magenta: the verdict is what makes the next move a person's. A
 *   round that STOPPED reporting is `review_abandoned` and reads `manual`
 *   instead — no verdict is coming, so the next move is already a person's.
 *   `run_abandoned` is the same reading for a pending row of any other kind
 *   (#2279): a dispatched attempt whose host died is not kew working, and the
 *   store's own backstop will not finalize a row whose owner it cannot prove
 *   dead, so a person is the only thing that moves it.
 * * `flawed` — the attempt died. `terminal_failure` read `manual` until #2279,
 *   which was the third of three treatments #2053 was drawn with at once
 *   (magenta "manual" here, violet BLOCKED on Monitor, truncated on the DAG).
 *   It takes the failure word and the failure X instead: `kew.waits`'s
 *   `failed-terminal` bucket names no attention kind, the driver's live
 *   attention stream drops terminal nodes, and the needs-you set no longer
 *   counts one (`QUIET_EXCEPTIONS` in kew/src/kew/service/attention.py) — so
 *   this word is the same reading those three take, rather than a fourth.
 * * `pending` — `remote_unavailable` (#2893): the projection could not READ the
 *   tracker item or the pull request. The read is kew's own, retried by the
 *   next poll (`observe_remote` is kew's move in evidence.ts), and the server's
 *   needs-you set no longer counts it (`QUIET_EXCEPTIONS`). As `manual`, live
 *   #2870 wore the magenta diamond with no entry behind it and no reason under
 *   it; the row says what could not be read instead (`rowReason`).
 * * `manual` — any other human move, including "find out": where the exception
 *   is an unreadable fact, the safest action is to go and look, which is a
 *   person.
 * * `held` — a cap, a stop or somebody's decision, which is not a defect.
 *   `terminal_refused` reads as a hold for the same reason `refused` does in
 *   factory/status.ts: `_REFUSAL_TERMINAL` in kew/service/work.py groups
 *   refusals as a decision rather than a fault.
 * * `''` — no override; `none` is the absence of an exception, not a state.
 *
 * `awaiting_publication` is magenta-adjacent for the reason status.ts already
 * gives: a preserved artifact waits for a PERSON to run `kew finalize`.
 *
 * `declined` is `manual` rather than `held`, even though the decline is itself
 * somebody's decision: `_after_decline` in kew/service/work.py answers it with
 * `inspect` and marks it blocking, and `_PR_REVIEW_EXCEPTIONS` in
 * service/dashboard.py puts the row in the judgment queue — so a person IS the
 * next move, and every other exception whose safest action is `inspect` reads
 * as `manual` here. A hold is something that lifts; a decline never does.
 *
 * The keys are the whole exception vocabulary — `list.test.ts` holds this table
 * to `KNOWN_EXCEPTION_STATES`, and `kew/tests/test_factory_exception_parity.py`
 * holds BOTH tables to the verdicts `derive_verdict` can actually return, so a
 * projection that grows an exception fails there rather than quietly painting it
 * as its bare execution state. The anchor has to reach into Python: while both
 * tables lived in TypeScript they could be missing the same state together, and
 * `declined` was, for exactly that reason.
 */
export const EXCEPTION_STATE_WORD: Record<string, string> = {
  none: '',
  in_flight: 'working',
  review_in_flight: 'in review',
  review_remediation_due: 'working',
  review_round_due: 'working',
  not_started: 'idle',
  no_change_authorized: 'working',
  source_desynchronized: 'working',
  awaiting_human_decision: 'in review',
  changes_requested: 'in review',
  checks_failing: 'manual',
  merge_conflict: 'manual',
  declined: 'manual',
  judgment_unknown: 'manual',
  awaiting_publication: 'manual',
  publication_risk_hold: 'manual',
  no_change_pending: 'manual',
  no_change_sync_failed: 'manual',
  readiness_refuted: 'manual',
  route_substituted: 'manual',
  orphan_artifact: 'manual',
  source_state_unknown: 'manual',
  remote_unavailable: 'pending',
  review_abandoned: 'manual',
  run_abandoned: 'manual',
  terminal_failure: 'flawed',
  no_artifact: 'manual',
  fleet_stopped: 'held',
  review_capacity_saturated: 'held',
  terminal_refused: 'held'
};

/**
 * The state word for one item. **Interim: FUI-29 deletes this.**
 *
 * #2050 closes the run-state vocabulary and serves the word; until it lands the
 * Work screen derives one, and derives it here only. An exception state this
 * file has never heard of falls through to the execution state, and an
 * execution state it has never heard of is echoed verbatim — the same
 * forward-compatibility rule as `statusShape`, which draws an unrecognised word
 * as a muted ring beside its own name rather than blanking the cell.
 */
export function itemState(view: WorkView): string {
  const override = own(EXCEPTION_STATE_WORD, view.exception.state);
  if (override) return override;
  return own(EXECUTION_STATE_WORD, view.execution.state) ?? view.execution.state;
}

/**
 * The word the STATE column prints for a row's state (#2281 D13).
 *
 * The three tables above are vocabularies of their own — `success`, `idle`,
 * `manual` — and the DAG, reading the same mark table, said `done` and `todo`
 * about nodes in the same state. Two names for one reading, on two screens an
 * operator moves between. DESIGN.md makes the mark table the single source, so
 * the word is the MEANING's word here: the row's state is resolved to a meaning
 * by `statusShape` — in the run vocabulary, because a row of this list is a run
 * reading — and printed by `statusWord`, which is the function every DAG node
 * already prints. `statusShape` keeps deciding the mark beside it, so the shape
 * and the word can never name two different states.
 *
 * A state the mark table has never heard of is ECHOED, not relabelled: the
 * fallback meaning is `todo`, and printing "todo" over a word a server just
 * grew would be the one thing worse than an unfamiliar word — a confident
 * wrong reading. Same rule as `itemState` and `statusShape` itself.
 */
export function stateWord(state: string): string {
  const mark = statusShape(state, 'run');
  return mark.known ? statusWord(mark.meaning) : mark.word;
}

/**
 * `DashboardRow.attention_kind` → the state word an entry row reads.
 *
 * The third table, for the third kind of row. An entry row has no projection
 * behind it, so neither table above can speak for it — and before #2246 every
 * one of them read `manual`, which on the live screen was five of seven rows
 * saying the same word about five different waits.
 *
 * The kind is the one category every surface reads (`AttentionKind` in
 * `kew/waits.py`, plus the work projection's own `merge_decision`,
 * `work_exception` and `approval`), and the words are the ones the two tables
 * above already use for the same meaning:
 *
 * * `in review` — a human verdict on a published artifact is outstanding, which
 *   is what `awaiting_human_decision` reads above.
 * * `held` — a cap, a refusal or somebody's decision. Every `*_held`-shaped
 *   kind is one: it is a wait that LIFTS, and the yellow ring says so. An
 *   `approval` is here too — a spend decision is a cap, not a defect.
 * * `manual` — any other human move, including "find out".
 *
 * Open vocabulary, as `AttentionKind`'s docstring requires: a kind this table
 * has never heard of, and an absent one (a `kew serve` older than #2244), read
 * as `manual` — the word for "a person is next, and nothing more is known",
 * which is what every entry row said before this table existed.
 */
export const ATTENTION_STATE_WORD: Record<string, string> = {
  merge_ready: 'in review',
  merge_decision: 'in review',
  held: 'held',
  budget_exhausted: 'held',
  publication_held: 'held',
  sandbox_refused: 'held',
  approval: 'held',
  epic_complete: 'manual',
  // A node the host restarted under twice (#2971): it FAILED, nothing holds
  // it, and a person finds out why the host keeps going down before retrying
  // it — the needs-you diamond, not the yellow ring of a wait that lifts.
  host_restart_repeated: 'manual',
  // An agent token near a provider usage limit (#3329): nothing is held yet,
  // and whether to replace the token before it is refused is a person's call.
  usage_limit_warning: 'manual',
  work_exception: 'manual',
  // A triage proposal (#2293): a classifier said what the item is, and the
  // decision is a person's. Named here rather than left to the fallback, so
  // the kind is a decision rather than an omission. The word is `manual` —
  // `needs_you` in `MEANING_OF` — because the status vocabulary is a closed set
  // held to its pinned marks (`factory/status.ts`, `status.test.ts`), and the
  // specific wait is already on the row: `label` carries what triage proposed
  // and `command` carries the `kew triage decide` that ends it.
  triage_proposal: 'manual',
  // A merge-queue main held red or unverified (#2982). `manual` for the same
  // reason as a triage proposal: the fix is a person's, the sentence on the row
  // says which main, and `command` is what re-verifies it.
  merge_queue_suspect: 'manual'
};

/** The word for one attention kind. See `ATTENTION_STATE_WORD`. */
export function attentionState(kind: string | null | undefined): string {
  return own(ATTENTION_STATE_WORD, kind ?? '') ?? 'manual';
}

/**
 * Whether dispatch is this item's next move (#2785): the projection's safest
 * action is `dispatch`, or the ready queue holds it. "Plan a dispatch" and the
 * palette's dispatch copy are offered on that and nothing else — an item in
 * review or held has another next move, and a needs-you row keyed on its entry
 * (`stalled_epic:go-live`) is not an item at all, so neither payload names it.
 */
export function dispatchIsNext(view: WorkView | null, queued: boolean): boolean {
  return queued || view?.safest_action?.action === 'dispatch';
}

/**
 * Whether the row stands for a work item kew can be asked about by id.
 *
 * False only for an entry row whose needs-you entry names no item — a stalled
 * epic, an orphan PR — which `entryRows` keys on the entry's own `rowKey`
 * (`orphan_pr:77`) for want of an item id. That key selects the row, but it is
 * not an item: `/api/work/{id}/…` knows nothing by it, so the pane takes no
 * per-item read for it (#3453). An entry row that does name an item, and every
 * row either payload drew, is one.
 */
export function rowNamesItem(row: Pick<WorkRow, 'entry'>): boolean {
  return row.entry === null || rowItemId(row.entry) !== '';
}

/** `dispatchIsNext` for a row of the list. */
export function rowDispatchIsNext(row: Pick<WorkRow, 'view' | 'tier'>): boolean {
  return dispatchIsNext(row.view, row.tier !== null);
}

/** The ready queue's item ids, from whatever dashboard reading is in hand. */
export function readyQueueIds(dashboard: Dashboard | null): Set<string> {
  return new Set((dashboard?.glance.ready_queue ?? []).map(queueItemId));
}

/** The durable id of a ready-queue entry: the string one, else the number. */
function queueItemId(issue: ReadyIssue): string {
  const id = issue.item_id;
  return id != null && id !== '' ? id : String(issue.issue_num);
}

/**
 * The durable item id a judgment row names, or `''` when it names none.
 *
 * The string id wins over the legacy number, and the `0` sentinel names nothing
 * (see `DashboardRowDTO` in kew/service/models.py).
 */
function rowItemId(row: DashboardRow): string {
  const id = row.item_id != null && row.item_id !== '' ? row.item_id : row.issue_num;
  return id == null || id === '' || id === 0 ? '' : String(id);
}

/**
 * Item ids the needs-you set names — the definition of "needs you", which
 * Factory UI plan §1 pins in one helper. These are the same entries `headerCounts`
 * counts for the header's "1 needs you" and Monitor's column lists; this reads
 * *which* items they are, so the list's filter and the header's count can never
 * disagree about what the word means.
 *
 * Not every entry names an item — a stalled epic names an epic and an orphan PR
 * names a pull request — so this is not the whole set. `entryRows` draws a row
 * for each of those too, which is what retired the banner that used to apologise
 * for them (#2207).
 */
export function judgmentItemIds(dashboard: Dashboard | null): Set<string> {
  return new Set(judgmentEntries(dashboard).keys());
}

/**
 * The set's entries by the item id each names, for the rows that name one.
 *
 * The first entry for an id wins, in the server's own consequence order
 * (`orderRows` is not applied here: `dashboard.rows` is already served in it,
 * and the LIST re-orders its own rows anyway). Two entries on one item is a
 * shape the set can serve — a hold and a merge decision on the same node — and
 * the block reads the most consequential of them, which is the first.
 */
export function judgmentEntries(dashboard: Dashboard | null): Map<string, DashboardRow> {
  const byItem = new Map<string, DashboardRow>();
  for (const row of dashboard?.rows ?? []) {
    const id = rowItemId(row);
    if (id !== '' && !byItem.has(id)) byItem.set(id, row);
  }
  return byItem;
}

/**
 * The needs-you entries the merged list has no row for, in the order the server
 * serves them.
 *
 * Two ways an entry has no row of its own, and the reader cannot act on the
 * difference:
 *
 * * **It names no item.** A `stalled_epic` entry names an epic and an
 *   `orphan_pr` entry names a pull request; both are built with `issue_num` and
 *   `item_id` at their `DashboardRowDTO` defaults, so a list keyed on `item_id`
 *   has nowhere to put them.
 * * **It names an item the list does not hold.** An entry can carry a real
 *   `item_id` the projection never composed and the ready queue never offered —
 *   a `budget_hold` on an epic node with no delivery PR and no `ready` label, an
 *   `epic_wait` on a node kew has not dispatched yet, or any entry at all while
 *   `/api/dashboard/work` is unreadable.
 *
 * Either way `headerCounts` counts it and `?filter=needs-you` used to render
 * nothing, so the list read as an empty queue directly under a header saying "16
 * needs you" — and the only thing the screen could do was print a banner saying
 * so. `entryRows` draws them instead: a row per entry, which is what the banner
 * was standing in for (#2207).
 */
export function judgmentRowsNotListed(
  dashboard: Dashboard | null,
  rows: WorkRow[]
): DashboardRow[] {
  // An entry with no item of its own is listed under its `rowKey`, which is
  // what `entryRows` keys such a row on — so both identities count as drawn.
  const listed = new Set(rows.map((r) => r.itemId));
  return (dashboard?.rows ?? []).filter((row) => {
    const id = rowItemId(row);
    return id === '' ? !listed.has(rowKey(row)) : !listed.has(id);
  });
}

/**
 * One row per needs-you entry the merged list does not already hold.
 *
 * The row's identity is the entry's own `rowKey` (`stalled_epic:go-live`,
 * `orphan_pr:77`) when it names no item, so two entries of one type cannot
 * collide and the key is the same one the console and Monitor track the entry
 * under. Where the entry DOES name an item, that id is the row's — a later
 * projection poll that starts composing the item merges onto the same row rather
 * than drawing a second one beside it.
 *
 * `state` is `attentionState(entry.attention_kind)` — the row's own category,
 * read and not derived. Every entry row used to read `manual`, which made the
 * one magenta-adjacent word on the screen say nothing: a round-cap hold, a
 * merge decision and a continuation are three different waits and the State
 * column said the same thing about all three (#2246).
 */
export function entryRows(dashboard: Dashboard | null, rows: WorkRow[]): WorkRow[] {
  return orderRows(judgmentRowsNotListed(dashboard, rows)).map((entry) => {
    const id = rowItemId(entry);
    const itemId = id === '' ? rowKey(entry) : id;
    return {
      itemId,
      number: id === '' ? entryNumber(entry) : id,
      // The ref stays the epic's own: the No. column's short mark is not a name.
      ref: id === '' ? entry.epic_ref || entryNumber(entry) : itemRef(id, null),
      // The ITEM's title where the server served one (#2786 D14): titled by
      // its wait, SFD-229 read "the review round cap was reached" on Work and
      // "budget caps per epic…" on its epic card. The wait moves to the quiet
      // line under it (`rowReason`). An epic-level entry is titled by its
      // epic's ref, which the No. column no longer prints in full. Only an
      // entry with neither falls back to the sentence — the ONE reading of a
      // needs-you row (#2245), and `rowSentence` rather than `entry.reason`,
      // which put `review_rounds_exhausted` in the Issue column.
      title: entryTitle(entry, id),
      state: attentionState(entry.attention_kind),
      needsYou: true,
      view: null,
      tier: null,
      entry,
      attention: entry,
      href: selfHref(itemId),
      external: false
    };
  });
}

/**
 * The No. column for an entry naming no work item: a short epic mark, its PR,
 * or the type itself. Never a number it does not have.
 *
 * The epic's REF used to go here, and a live ref like `supervised-daemon` ran
 * out of the 56px column into the title beside it (#2786 N3). The ref is the
 * row's title instead (`entryTitle`).
 */
export const EPIC_MARK = 'epic';

function entryNumber(entry: DashboardRow): string {
  if (entry.epic_ref) return EPIC_MARK;
  if (entry.pr_number != null) return `PR ${entry.pr_number}`;
  return typeLabel(entry.type, 1);
}

/**
 * An entry row's title: the item's own, as the server served it; else, for an
 * entry naming no item, its epic's ref; else the entry's sentence.
 */
function entryTitle(entry: DashboardRow, itemId: string): string {
  const own = (entry.title ?? '').trim();
  if (own !== '') return own;
  if (itemId === '' && entry.epic_ref) return entry.epic_ref;
  return rowSentence(entry);
}

/**
 * The quiet line under a row's title (#2786 D14): why the row waits on a
 * person, in the server's one sentence — or `''` for a row nothing waits on,
 * and for one whose title already IS that sentence.
 *
 * A row whose remote state could not be read waits on nobody, but it is not
 * fine either, so it says what could not be read (#2893): the exception's own
 * detail, the sentence the item's view leads with.
 */
export function rowReason(
  row: Pick<WorkRow, 'title' | 'attention' | 'state' | 'needsYou' | 'view'>
): string {
  if (row.attention === null) return readFailure(row.view);
  if (saysMergeDecision(row)) return '';
  const said = rowSentence(row.attention);
  return said === row.title ? '' : said;
}

/** The notice for a read failure the server served no detail for. */
export const READ_FAILURE_FALLBACK = 'its remote state could not be read';

/** What a `remote_unavailable` view could not read, or `''` for any other. */
function readFailure(view: WorkView | null): string {
  if (view?.exception.state !== 'remote_unavailable') return '';
  return view.exception.detail.trim() || READ_FAILURE_FALLBACK;
}

/**
 * Whether the State cell already says "a merge decision on an open PR is
 * yours" (#2848): IN REVIEW beside the needs-you diamond. The server's
 * sentence for that wait is the generic "PR #N is open — the merge decision is
 * yours", and as the reason line it only repeated the cell and took the line
 * the title's second line needs.
 *
 * The PR is read off `pr_number` or the row's PR URL (#2893): a live merge row
 * carries its PR as a URL with `pr_number: null`, and the drop never fired.
 *
 * A merge over a verify that failed at the PR's head is not that sentence: it
 * leads with the failure (#3460), which the State cell does not say, so it is
 * never dropped.
 */
function saysMergeDecision(row: Pick<WorkRow, 'attention' | 'state' | 'needsYou'>): boolean {
  const kind = row.attention?.attention_kind ?? '';
  return (
    (kind === 'merge_decision' || kind === 'merge_ready') &&
    row.attention !== null &&
    row.attention.verify_link == null &&
    namesPr(row.attention) &&
    row.needsYou &&
    stateWord(row.state) === 'in review'
  );
}

/** A pull request's URL — `_PR_URL` in kew/src/kew/service/attention.py, which
 * writes the sentence: an issue URL is not a pull request, so it names none. */
const PR_URL = /\/(?:pull|pulls|pr|merge_requests)\/\d+\/?$/;

/** Whether an entry names the pull request its merge is made on. */
function namesPr(entry: Pick<DashboardRow, 'pr_number' | 'url'>): boolean {
  return entry.pr_number != null || PR_URL.test((entry.url ?? '').trim());
}

/**
 * Item id → title, from every payload in hand that names one.
 *
 * `/api/runs` rows carry no `item_id` (the DTO predates it), so a run only
 * names a GitHub item, and the `0` sentinel issue number names nothing.
 */
function titleIndex(dashboard: Dashboard | null, runs: Run[] | null): Map<string, string> {
  const titles = new Map<string, string>();
  const put = (id: string | null | undefined, title: string) => {
    if (id == null || id === '' || id === '0' || !title) return;
    if (!titles.has(id)) titles.set(id, title);
  };
  const glance = dashboard?.glance;
  for (const issue of glance?.ready_queue ?? []) put(queueItemId(issue), issue.title);
  for (const done of glance?.recent_completions ?? [])
    put(done.item_id ?? String(done.issue_num), done.title);
  for (const cand of glance?.queue_candidates ?? [])
    put(cand.item_id ?? String(cand.issue_num), cand.title);
  for (const run of runs ?? []) put(String(run.issue_num), run.title);
  return titles;
}

/**
 * Whether a row's State cell draws the needs-you diamond BESIDE its state mark.
 *
 * Every member of the set the header counts wears the diamond (#2539, D15): a
 * merge decision's state is `in review`, a cyan ring, and #2055 read IN REVIEW
 * while the header counted it in "2 needs you" — the one thing that said a
 * person was next was not on its row. A state that already IS the diamond
 * (`manual`) is not given a second one.
 */
export function needsYouDiamond(row: Pick<WorkRow, 'needsYou' | 'state'>): boolean {
  return row.needsYou && statusShape(row.state, 'run').meaning !== 'needs_you';
}

/**
 * The set's members first (#2539, D15) — the rows the header counts are the
 * ones a reader came for, and they were scattered through the list by number.
 * Then newest first, which for a tracker that counts is the number descending —
 * the artboard's order (2060 … 1803). A non-numeric key cannot be compared to
 * a number, so Linear keys sort among themselves, alphabetically, after the
 * numbered ones rather than being wedged into a sequence they are not part of.
 */
const NUMERIC_ID = /^[0-9]+$/;

function compareRows(a: WorkRow, b: WorkRow): number {
  if (a.needsYou !== b.needsYou) return a.needsYou ? -1 : 1;
  const an = NUMERIC_ID.test(a.itemId);
  const bn = NUMERIC_ID.test(b.itemId);
  if (an && bn) return Number(b.itemId) - Number(a.itemId);
  if (an !== bn) return an ? -1 : 1;
  return a.itemId.localeCompare(b.itemId);
}

/**
 * A row's state when the needs-you set holds its item: `held`, whatever the
 * row's own payload says (#2842).
 *
 * The projection does not read the driver's hold table, so a node held between
 * two review rounds composes as `review_round_due` — "kew re-reviews next",
 * WORKING — while the set serves the hold that stops the re-review. Live #2814
 * read WORKING beside the diamond for exactly that reason: the round was kew's
 * move only once a person lifted the hold. A hold is the row's reading because
 * nothing else moves until it lifts. Any other entry leaves the word alone — a
 * merge decision stays IN REVIEW with the diamond beside it (#2539).
 */
function heldOver(state: string, entry: DashboardRow | undefined): string {
  return entry !== undefined && attentionState(entry.attention_kind) === 'held' ? 'held' : state;
}

/**
 * The list: the projection's views, then the ready queue's idle issues that the
 * projection does not already cover.
 *
 * Duplicates are dropped by `item_id`, keeping the projection's row — it is the
 * one with an execution, an exception and a safest action. A view with no
 * `item_id` at all is dropped, because a row the URL cannot address is a row
 * nothing can select.
 *
 * An idle issue's state word is `idle` and nothing more. DWork draws `3 tasks`
 * on #2050, from a multi-step count no payload carries (Factory UI plan: "defer it and
 * render it only if a field appears"), so that one row is an intentional
 * deviation from the artboard.
 */
export function mergeWorkList(input: WorkListInput): WorkRow[] {
  const { projection, dashboard, runs } = input;
  const titles = titleIndex(dashboard, runs);
  const entries = judgmentEntries(dashboard);
  const rows: WorkRow[] = [];
  const seen = new Set<string>();
  const tiers = new Map(
    (dashboard?.glance.ready_queue ?? []).map((issue) => [queueItemId(issue), issue.complexity])
  );

  const push = (itemId: string, state: string, view: WorkView | null) => {
    if (itemId === '' || seen.has(itemId)) return;
    seen.add(itemId);
    rows.push({
      itemId,
      number: itemId,
      ref: itemRef(itemId, null),
      title: titles.get(itemId) ?? '',
      state: heldOver(state, entries.get(itemId)),
      needsYou: entries.has(itemId),
      view,
      tier: tiers.get(itemId) ?? null,
      entry: null,
      attention: entries.get(itemId) ?? null,
      href: selfHref(itemId),
      external: false
    });
  };

  for (const view of projection?.views ?? []) {
    push(view.identity.item_id ?? '', itemState(view), view);
  }
  for (const issue of dashboard?.glance.ready_queue ?? []) {
    push(queueItemId(issue), 'idle', null);
  }
  // Then the needs-you set's own: every entry the two payloads left undrawn.
  // The list is the set's one complete rendering, so `?filter=needs-you` can
  // never show fewer rows than the header counted.
  rows.push(...entryRows(dashboard, rows));
  return rows.sort(compareRows);
}

/**
 * Case-insensitive substring search over the two columns a reader can see: the
 * number and the title. The number is matched case-insensitively too — it is a
 * digit string only for GitHub, and `sfd-12` must find `SFD-12`.
 */
export function searchWorkRows(rows: WorkRow[], query: string): WorkRow[] {
  const q = query.trim().toLowerCase();
  if (q === '') return rows;
  return rows.filter(
    (r) => r.number.toLowerCase().includes(q) || r.title.toLowerCase().includes(q)
  );
}

export const WORK_FILTERS = ['all', 'needs-you'] as const;
export type WorkFilter = (typeof WORK_FILTERS)[number];

/** Label per filter, as the button shows it. */
export const WORK_FILTER_LABEL: Record<WorkFilter, string> = {
  all: 'All',
  'needs-you': 'Needs you'
};

/** `?filter=` is user input: anything unrecognised means the unfiltered list. */
export function parseWorkFilter(value: string | null | undefined): WorkFilter {
  return (WORK_FILTERS as readonly string[]).includes(value ?? '')
    ? (value as WorkFilter)
    : 'all';
}

export function filterWorkRows(rows: WorkRow[], filter: WorkFilter): WorkRow[] {
  return filter === 'needs-you' ? rows.filter((r) => r.needsYou) : rows;
}

/** A named thing the list is not showing. Never a silent shortening. */
export type WorkNotice = {
  key: string;
  text: string;
  /**
   * The accessible name of the mark beside the text. Not every notice is about
   * a failed read — announcing the needs-you one as "unreadable" would tell a
   * screen reader the opposite of what it says — so each notice names its own.
   */
  label: string;
};

/** The mark's name for the notices that ARE about a payload that failed. */
const UNREADABLE = 'unreadable';

/**
 * What the list is not showing, named.
 *
 * A payload that failed is never rendered as an absence of work: an unreadable
 * projection with a readable ready queue still lists the idle issues, and says
 * out loud that the touched ones are missing. `projection.unreadable` names the
 * items whose facts the server itself could not compose — they have no row to
 * draw, so the notice is the only place they exist, and dropping it would
 * render a shorter, healthier world than the real one.
 *
 * There is no longer a notice for a needs-you entry with no row: `entryRows`
 * draws one for every entry, so the list IS the set's complete rendering and
 * has nothing left to apologise for (#2207). The banner it replaced said "1
 * stalled epic in the needs-you queue has no row here — the header counts it,
 * and this list cannot show it", which was true and useless: the reader wanted
 * the row, not the admission. The `rows` parameter stays because the caller
 * passes the list it is about to render and a future notice may be about it.
 */
export function listNotices(
  input: WorkListInput,
  rows: WorkRow[] = mergeWorkList(input)
): WorkNotice[] {
  const notices: WorkNotice[] = [];
  const { projection, dashboard } = input;
  if (projection === null) {
    notices.push({
      key: 'projection',
      label: UNREADABLE,
      text: 'The work projection could not be read — items kew has already touched are missing from this list.'
    });
  } else if (projection.unreadable.length > 0) {
    const named = projection.unreadable.map((id) => itemRef(id, null)).join(', ');
    const plural = projection.unreadable.length === 1 ? 'item' : 'items';
    notices.push({
      key: 'unreadable',
      label: UNREADABLE,
      text: `${projection.unreadable.length} ${plural} could not be read: ${named}.`
    });
  }
  if (dashboard === null) {
    notices.push({
      key: 'dashboard',
      label: UNREADABLE,
      text: 'The dashboard could not be read — idle issues and the needs-you filter are missing.'
    });
    return notices;
  }
  const out = new Set(dashboard.unavailable ?? []);
  if (out.has('ready_queue')) {
    notices.push({
      key: 'ready_queue',
      label: UNREADABLE,
      text: 'The ready queue could not be read — idle issues are missing from this list.'
    });
  }
  const judgmentOut = JUDGMENT_ROW_SOURCES.filter((source) => out.has(source));
  if (judgmentOut.length > 0) {
    notices.push({
      key: 'judgment',
      label: UNREADABLE,
      text: `The needs-you filter is incomplete: ${judgmentOut.join(', ')} could not be read.`
    });
  }
  return notices;
}

/**
 * The row `?item=` selects: the named one if it is in the list, else none.
 *
 * A `?item=` naming an item this list does not hold selects nothing rather than
 * silently sliding the selection onto the first row — a link to an item that
 * has since left the list must not read as a link to a different item.
 */
export function selectedRow(rows: WorkRow[], item: string | null | undefined): WorkRow | null {
  if (item == null || item === '') return null;
  return rows.find((r) => r.itemId === item) ?? null;
}
