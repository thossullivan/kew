import type {
  Dashboard,
  DashboardBanner,
  DashboardRow,
  WorkAction,
  WorkActionResponse,
  WorkException,
  WorkView
} from './api';

// The needs-you row model the factory screens share (#702, Group 3; the legacy
// console it was written for is gone, #3454). Pure and vitest-covered: priority
// ordering, row identity, the dismissal prune and banner extraction live here so
// the pages that draw the rows stay renderers.

// The contract row types. Rows of any other type are ignored by the client
// (additivity — the DTO is defined to grow new row types without a client bump).
// `work_exception` / `work_unreadable` are the canonical projection's own rows
// (I19): an off-normal item the console could not name before, and an item
// whose facts could not be read at all. The second one MUST stay visible —
// dropping it would render a shorter, healthier world than the real one.
// `epic_wait` is an epic node whose journaled state waits on a person — a merge,
// a continuation, a publication, a no-change decision (#2207). It MUST stay
// visible for the same reason: the header counts the served rows, so a type
// `orderRows` quietly filtered out is a count with no row under it.
export const KNOWN_ROW_TYPES = [
  'approval_hold',
  'budget_hold',
  'stalled_epic',
  'merge_queue_suspect',
  'pr_review',
  'work_exception',
  'epic_wait',
  'orphan_pr',
  'work_unreadable',
  'triage_proposal'
] as const;

export type KnownRowType = (typeof KNOWN_ROW_TYPES)[number];

const KNOWN = new Set<string>(KNOWN_ROW_TYPES);

// Consequence classes, most-consequential first: money you cannot get back,
// then a stalled epic burning budget, then a review waiting on you, then
// hygiene. Rows carry `consequence` from the server; when absent we fall back
// to the type's default class so ordering is always deterministic.
const CONSEQUENCE_ORDER = ['money', 'epic', 'review', 'wait', 'hygiene'];

const TYPE_CONSEQUENCE: Record<string, string> = {
  approval_hold: 'money',
  budget_hold: 'money',
  stalled_epic: 'epic',
  pr_review: 'review',
  work_exception: 'review',
  // An epic node whose journaled state waits on a person — a merge, a
  // continuation, a publication, a no-change decision (`epic_wait` in
  // kew/src/kew/service/needs_you.py). Below review and above hygiene: a
  // person's move, but one no artifact is blocked on this second.
  epic_wait: 'wait',
  orphan_pr: 'hygiene',
  work_unreadable: 'hygiene',
  // A triage proposal waiting on a person (`triage_proposal` in
  // kew/src/kew/service/needs_you.py). Ranked with the other review-pressure
  // rows: a judgment somebody owes, and nothing dispatches off it.
  triage_proposal: 'review',
  // A merge queue stopped on an unverified main (`merge_queue_suspect` in
  // kew/src/kew/service/needs_you.py): nothing merges for anybody until a
  // person repairs main and resumes it, so it ranks with a stalled epic.
  merge_queue_suspect: 'epic'
};

// [singular, plural] display labels per type.
const TYPE_LABEL: Record<string, [string, string]> = {
  approval_hold: ['approval', 'approvals'],
  budget_hold: ['budget hold', 'budget holds'],
  stalled_epic: ['stalled epic', 'stalled epics'],
  pr_review: ['PR review', 'PR reviews'],
  work_exception: ['exception', 'exceptions'],
  epic_wait: ['epic wait', 'epic waits'],
  orphan_pr: ['orphan PR', 'orphan PRs'],
  work_unreadable: ['unreadable item', 'unreadable items'],
  triage_proposal: ['triage proposal', 'triage proposals'],
  merge_queue_suspect: ['merge queue', 'merge queues']
};

function consequenceRank(c: string): number {
  const i = CONSEQUENCE_ORDER.indexOf(c);
  return i === -1 ? CONSEQUENCE_ORDER.length : i;
}

function rowConsequence(r: DashboardRow): string {
  return r.consequence || TYPE_CONSEQUENCE[r.type] || '';
}

/** Human label for a row type, pluralised on `count`. Unknown → the raw type. */
export function typeLabel(type: string, count: number): string {
  const pair = TYPE_LABEL[type];
  if (!pair) return type;
  return count === 1 ? pair[0] : pair[1];
}

/**
 * The durable work-item id a write is addressed to (bare `SFD-12` for a
 * non-GitHub tracker), falling back to the legacy numeric id for older
 * payloads. Opaque throughout — nothing in the UI parses it.
 */
export function itemIdOf(r: DashboardRow): string | null {
  if (r.item_id != null && r.item_id !== '') return r.item_id;
  return r.issue_num != null ? String(r.issue_num) : null;
}

/**
 * A row's identity across polls — the key its per-row UI and its dismissal are
 * tracked under. The durable item id is preferred (matching `itemIdOf`, so a
 * non-GitHub row with the 0-sentinel `issue_num` gets its own identity), with
 * the numeric / PR / epic fallbacks kept for legacy payloads. A row naming
 * none of those is keyed on its repository, then its title: a held merge-queue
 * main names only its repository, and one ledger can hold two (#2883, #2982).
 *
 * Shared rather than re-derived per surface: Work and the Monitor's holds
 * column both key their rows by it, and the holds column drops a decided row on
 * the write response; two surfaces keying the same row differently is how one
 * of them resurrects it.
 */
export function rowKey(r: DashboardRow): string {
  const id =
    r.item_id != null && r.item_id !== ''
      ? r.item_id
      : (r.issue_num ?? r.pr_number ?? r.epic_ref ?? r.repo ?? r.title ?? '');
  return `${r.type}:${id}`;
}

/**
 * Per-row state (a dismissal, a row's inline write feedback) narrowed to the
 * rows the server is still serving.
 *
 * A dismissal is a bridge across the lag between a write and the read that
 * confirms it (CS-6), so it has to END when that read lands. Kept for the life
 * of the page it would hide the NEXT hold raised on the same item — `rowKey`
 * is `type:item_id`, so a re-raised approval carries the identical key — behind
 * a decision made about a hold that is already closed.
 *
 * The input is returned unchanged when there is nothing to drop, so a caller
 * holding this in reactive state can assign the result on every poll without
 * churning it.
 */
export function pruneKeyedState<T>(
  live: Iterable<string>,
  state: Record<string, T>
): Record<string, T> {
  const keep = new Set(live);
  const keys = Object.keys(state);
  if (keys.every((key) => keep.has(key))) return state;
  return Object.fromEntries(keys.filter((key) => keep.has(key)).map((key) => [key, state[key]]));
}

/**
 * The fail-soft reads each row type is served by, in `/api/dashboard`'s own
 * `unavailable` vocabulary (`assemble` in kew/src/kew/service/dashboard.py runs
 * one reader per name and lists the ones that raised).
 *
 * The three work types name `pr_reviews` as well as their reader: when the work
 * projection fails, `_work_rows` succeeds over `None` and serves no rows, so
 * `work_rows` never appears and `pr_reviews` is the only name that says the
 * rows were not read. `header-state.ts`'s `JUDGMENT_ROW_SOURCES` is the same
 * vocabulary — `dashboard.test.ts` holds this table to it, and
 * `kew/tests/test_factory_header_parity.py` holds that list to the backend.
 * Exported for that check alone; nothing renders from it.
 */
export const ROW_TYPE_SOURCES: Record<string, readonly string[]> = {
  approval_hold: ['approval_holds'],
  budget_hold: ['budget_holds'],
  stalled_epic: ['stalled_epics'],
  pr_review: ['work_rows', 'pr_reviews'],
  work_exception: ['work_rows', 'pr_reviews'],
  work_unreadable: ['work_rows', 'pr_reviews'],
  epic_wait: ['epic_waits'],
  orphan_pr: ['orphan_prs'],
  triage_proposal: ['triage_proposals'],
  merge_queue_suspect: ['merge_queue']
};

/**
 * The source name that marks a row type as the work projection's. The types
 * carrying it are the ones `_work_rows` serves, so they are also the only ones
 * an item in `projection.unreadable` can silence by itself (`rowItemUnread`).
 */
const WORK_PROJECTION_SOURCE = 'work_rows';

// `rowKey` is `<type>:<id>`, and an id may itself contain a colon (a Linear
// item id), so the type is everything before the FIRST one and the item is the
// rest.
function keyType(key: string): string {
  const cut = key.indexOf(':');
  return cut === -1 ? key : key.slice(0, cut);
}

function keyItem(key: string): string {
  const cut = key.indexOf(':');
  return cut === -1 ? '' : key.slice(cut + 1);
}

/**
 * True when the read that would have served this key's row did not happen, so
 * the key's absence from `rows` is not the fact "the row is gone".
 *
 * A key of a type this table does not know is held too: an unrecognised type
 * cannot be shown to have been read, and holding a dismissal one poll too long
 * is a hidden row, where dropping it one poll too early is a decided row back
 * under the operator's cursor.
 */
function rowSourceUnread(key: string, unavailable: ReadonlySet<string>): boolean {
  const names = ROW_TYPE_SOURCES[keyType(key)];
  return names === undefined || names.some((name) => unavailable.has(name));
}

/**
 * The items the work projection could not read, in the keys' own vocabulary.
 *
 * The projection fails per ITEM as well as per source: `assemble` serves an
 * item in `projection.unreadable` as a `work_unreadable` row while `work_rows`
 * and `pr_reviews` both answer, so the item's REAL row — the `pr_review` the
 * operator just decided — is absent from a read that never looked at it. Those
 * served rows are the only place the payload names which items those were.
 *
 * The row TYPE is not enough on its own. The needs-you set collapses to one
 * entry per item (`_collapse` in kew/src/kew/service/needs_you.py) and
 * `work_unreadable` is the lowest consequence there is, so an unreadable item
 * that is also held is served as the HOLD, carrying `work_unread` — the same
 * fact under a different row. Reading only the type here went blind on exactly
 * the items that had something else wrong with them too.
 */
function unreadItems(rows: readonly DashboardRow[]): Set<string> {
  const items = new Set<string>();
  for (const r of rows) {
    if (r.type === 'work_unreadable' || r.work_unread === true) items.add(keyItem(rowKey(r)));
  }
  return items;
}

/**
 * The per-item twin of `rowSourceUnread`: true when this key's row is one the
 * work projection serves and the projection could not read this key's item.
 *
 * Only the projection's own row types, because only they are missing for that
 * reason — an approval hold on the same item is read by `approval_holds`, and
 * holding its dismissal on a fact about a different reader would hide a
 * re-raised hold for nothing.
 */
function rowItemUnread(key: string, unread: ReadonlySet<string>): boolean {
  const names = ROW_TYPE_SOURCES[keyType(key)];
  return names !== undefined && names.includes(WORK_PROJECTION_SOURCE) && unread.has(keyItem(key));
}

/**
 * The reading the prune is made by, as a predicate over one key: true when this
 * read either served the key's row or is not a read of that row at all.
 */
function keptByRead(dash: Dashboard): (key: string) => boolean {
  const served = new Set(dash.rows.map(rowKey));
  const unavailable = new Set(dash.unavailable ?? []);
  const unread = unreadItems(dash.rows);
  return (key) =>
    served.has(key) || rowSourceUnread(key, unavailable) || rowItemUnread(key, unread);
}

/**
 * `pruneKeyedState` against a dashboard READ — the rows it serves, plus the
 * keys whose row source, or whose item, it did not read at all.
 *
 * `/api/dashboard` fail-softs per source: a 200 carrying
 * `unavailable: ['approval_holds']` has no approval rows in it, and that is a
 * different fact from "the approval the operator just decided is closed".
 * Pruning on it drops the dismissal bridging the write, and the decided row is
 * drawn again the moment the source recovers while the read-back still lags —
 * the CS-6 resurrection the dismissal exists to prevent. The same reading runs
 * per ITEM: an item served as a `work_unreadable` row is one whose work facts
 * were not read either (`rowItemUnread`). The guard the caller can still only
 * make for itself is `null`, which is no read at all.
 */
export function pruneDashboardState<T>(
  dash: Dashboard,
  state: Record<string, T>
): Record<string, T> {
  const keep = keptByRead(dash);
  return pruneKeyedState(Object.keys(state).filter(keep), state);
}

/**
 * Priority-ordered rows: consequence class first (money → epic → review →
 * hygiene), then oldest-first within a class. Unknown row types are dropped.
 * Never mutates the input.
 */
export function orderRows(rows: DashboardRow[]): DashboardRow[] {
  return rows
    .filter((r) => KNOWN.has(r.type))
    .slice()
    .sort((a, b) => {
      const cr = consequenceRank(rowConsequence(a)) - consequenceRank(rowConsequence(b));
      if (cr !== 0) return cr;
      // Age is an ISO timestamp; the lexicographically smaller string is older.
      return a.age < b.age ? -1 : a.age > b.age ? 1 : 0;
    });
}

export type BannerTone = 'danger' | 'warn';
export type BannerNotice = {
  kind: 'fleet_stopped' | 'no_live_driver';
  message: string;
  tone: BannerTone;
};

/** The active fleet alarms, most-severe first. */
export function bannerNotices(b: DashboardBanner): BannerNotice[] {
  const out: BannerNotice[] = [];
  if (b.fleet_stopped) {
    out.push({
      kind: 'fleet_stopped',
      message: 'Fleet stopped — no new work will dispatch until it is resumed.',
      tone: 'danger'
    });
  }
  if (b.no_live_driver) {
    out.push({
      kind: 'no_live_driver',
      message: 'No live driver — the queue is not being worked.',
      tone: 'warn'
    });
  }
  return out;
}

// --- Canonical exception workspace (I19 / I20) ------------------------------
//
// Everything below reads the projection and formats it. There is deliberately
// no rule here that turns a run status, a PR state or an array length into a
// customer-facing claim: the exception, the safest action, the offered actions
// and the review-capacity reading are all decided in `kew.service.work` and
// arrive already decided. A second resolver on this side is exactly how two
// surfaces start disagreeing about the same work.

export type PressureTone = 'danger' | 'warn' | 'neutral';

// Display names for the exceptions the projection raises. A state missing here
// still renders (spaced, verbatim) — an unnamed future exception must stay
// visible rather than vanish behind a label table.
const EXCEPTION_LABEL: Record<string, string> = {
  publication_risk_hold: 'publication risk held',
  awaiting_publication: 'awaiting publication',
  no_change_pending: 'no-change awaiting judgment',
  no_change_authorized: 'no-change sync outstanding',
  no_change_sync_failed: 'no-change sync failed',
  source_desynchronized: 'tracker sync pending',
  source_state_unknown: 'tracker state unknown',
  checks_failing: 'checks failing',
  merge_conflict: 'merge conflict',
  changes_requested: 'changes requested',
  awaiting_human_decision: 'awaiting review',
  declined: 'declined without merge',
  judgment_unknown: 'decision unknown',
  orphan_artifact: 'orphan artifact',
  route_substituted: 'route substituted',
  readiness_refuted: 'route boundary refuted',
  review_capacity_saturated: 'review capacity saturated',
  remote_unavailable: 'remote unavailable',
  terminal_failure: 'attempt failed',
  terminal_refused: 'refused',
  no_artifact: 'no artifact',
  not_started: 'not started',
  in_flight: 'in flight',
  review_in_flight: 'governed review running',
  review_remediation_due: 'kew is revising',
  review_round_due: 'kew re-reviews next',
  review_abandoned: 'review round stopped reporting',
  run_abandoned: 'run stopped reporting',
  fleet_stopped: 'fleet stopped',
  none: 'none'
};

/**
 * Every exception state the projection raises, as named above. Exported so a
 * surface that must decide something *per* exception — the Work list's state
 * word (FUI-18) — can be held to the whole vocabulary by its own test instead
 * of transcribing it a second time.
 *
 * This table is itself a transcription, so it is not the authority: it is held
 * to `derive_verdict` in kew/src/kew/service/work.py by
 * `kew/tests/test_factory_exception_parity.py`. Nothing else could catch a state
 * missing from BOTH this table and a surface that reads it — `declined` was
 * missing from both, and the TypeScript guard compared one copy to the other.
 */
export const KNOWN_EXCEPTION_STATES: readonly string[] = Object.keys(EXCEPTION_LABEL);

/** Human name for an exception state; unknown states render verbatim. */
export function exceptionLabel(state: string): string {
  return EXCEPTION_LABEL[state] ?? state.replace(/_/g, ' ');
}

/** Tone for an exception — read from `blocking`, never guessed from the name. */
export function exceptionTone(e: WorkException): PressureTone {
  if (e.state === 'none') return 'neutral';
  return e.blocking ? 'danger' : 'warn';
}

/**
 * Every action `kew.service.work` is able to offer, in the order the factory
 * presents them. This list exists to be LABELLED, not to be chosen from: a
 * button appears only when a view actually offered the action.
 */
export const CANONICAL_WORK_ACTIONS = [
  'finalize_publication',
  'confirm_publication_risk',
  'confirm_no_change',
  'reject_no_change',
  'retry_no_change_sync',
  'acknowledge_orphan',
  'dispatch',
  'resume_fleet'
] as const;

const ACTION_LABEL: Record<string, string> = {
  finalize_publication: 'Finalize publication',
  confirm_publication_risk: 'Confirm publication risk',
  confirm_no_change: 'Confirm no-change',
  reject_no_change: 'Reject no-change',
  retry_no_change_sync: 'Retry source sync',
  acknowledge_orphan: 'Acknowledge orphan',
  dispatch: 'Dispatch',
  resume_fleet: 'Resume fleet'
};

/** Button text for one offered action; an unknown one renders verbatim. */
export function workActionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action.replace(/_/g, ' ');
}

/**
 * The actions this view offered, safest first — exactly as received. Empty is
 * a real answer and is rendered as one: resolving a conflict, fixing a build
 * and deciding on a pull request are steps kew does not perform, and offering
 * a redispatch in their place is the invented action this surface must never
 * make up.
 */
export function offeredActions(v: WorkView): WorkAction[] {
  return v.actions ?? [];
}

export type WorkActionErrorKind =
  | 'stale' // 409: the world moved, or that is not on offer here
  | 'not_approver'
  | 'unauthenticated'
  | 'invalid'
  | 'network'
  | 'server';

export type WorkActionErrorInfo = { kind: WorkActionErrorKind; message: string };

const ACTION_ERROR_COPY: Record<WorkActionErrorKind, string> = {
  stale: 'The work moved since you read it — re-read it and decide again.',
  not_approver: "You're not an approver — this action is reserved for approvers.",
  unauthenticated: 'Sign in to act on this work.',
  invalid: 'The server rejected this request.',
  network: 'Network error — check the connection and retry.',
  server: 'The server could not perform this action.'
};

function actionErrorKind(status: number): WorkActionErrorKind {
  if (status === 409) return 'stale';
  if (status === 403) return 'not_approver';
  if (status === 401) return 'unauthenticated';
  if (status === 400) return 'invalid';
  if (status === 0) return 'network';
  return 'server';
}

/**
 * Classify a failed action. The approver and session explanations stay the
 * friendly defaults — the server's own detail there is a technical allowlist
 * string — while every other kind prefers the server's detail, which is the
 * only thing that can say WHICH fact went stale.
 */
export function workActionError(status: number, detail: string): WorkActionErrorInfo {
  const kind = actionErrorKind(status);
  const friendlyOnly = kind === 'not_approver' || kind === 'unauthenticated';
  return { kind, message: friendlyOnly ? ACTION_ERROR_COPY[kind] : detail || ACTION_ERROR_COPY[kind] };
}

export type WorkActionOutcome = {
  /** The post-write projection to render against; null when nothing landed. */
  view: WorkView | null;
  chip: string | null;
  error: string | null;
  /** The caller must re-read the view before it may decide again. */
  reread: boolean;
  /** The returned view says this work is no longer off-normal. */
  resolved: boolean;
};

/**
 * Turn one action response into the row's next state. The transition comes
 * from the WRITE RESPONSE — the server recomposes the view after the effect —
 * so a surface never advances a work item on its own belief about what the
 * action must have done, and a full page reload lands on the same truth.
 */
export function applyWorkActionResult(res: WorkActionResponse): WorkActionOutcome {
  if (!res.ok) {
    const info = workActionError(res.status, res.detail);
    return { view: null, chip: null, error: info.message, reread: info.kind === 'stale', resolved: false };
  }
  const chip = res.data.idempotent ? 'already applied' : res.data.applied ? 'applied' : 'no change';
  return {
    view: res.data.view,
    chip,
    error: null,
    reread: false,
    resolved: res.data.view.exception.state === 'none'
  };
}
