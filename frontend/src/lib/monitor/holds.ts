/**
 * FUI-24 — "Holds and blocks", the Monitor's left column (DMonitor x 40–607,
 * y ≈476–924), decided in one pure place.
 *
 * The list answers one question: what is standing still, and who has to move
 * it? Four readings reach it, drawn in the order the artboard draws them —
 * **blocked** (an epic node the driver could not advance), **budget** (a cap
 * that was overridden), the epic HOLDS under the category their reason earns
 * (`monitor/hold-reasons`, mirroring `kew/src/kew/epic_holds.py`), and
 * **approval** / **needs** (the judgment queue's own entries). The kind column
 * is 104px, its mark comes from `factory/status` like every other mark in the
 * app, and the count sits in the section head.
 *
 * Four rules run through the file.
 *
 * **Only rows kew has the facts for.** The artboard's third row says
 * "self-hosted CI paused until #2033 merges". kew records no such fact, so
 * there is no runners row here and no place to put one — a hold list that
 * invents a hold is worse than a short one, because the reader cannot tell
 * which entries are real.
 *
 * **A source that did not answer is not an empty list.** This column is read
 * as "nothing is stuck". Each of its three sources fails independently, so a
 * view built on a partial read says `at least 2 active` rather than `2 active`
 * and names what it could not read. Reporting a confident count over a
 * half-read world is the one mistake this surface must not make.
 *
 * **The decidable rows decide the same way the dashboard does.** Approve and
 * deny are `verbs.ts`'s `decideHold`, the row keys are `dashboard.ts`'s own
 * `rowKey`, and a decided row leaves on the write response — the console's
 * CS-6 discipline, so a lagging poll cannot resurrect a row the operator has
 * already actioned. That dismissal is a bridge to the read that confirms it and
 * is dropped once the read lands — `pruneRowState`, so the next hold raised on
 * the same item is drawn — and the head counts the rows that were drawn, with
 * the dismissed ones taken out: `drawnHolds`.
 *
 * **A hold row names what clears it.** The epic, the item, the reason in words,
 * how long it has stood — and the action that actually ends it: a cap key to
 * raise, a `kew epic unhold`, or nothing at all for the CI gate, which clears
 * itself. The first-pass column read the `budget_hold` row TYPE as the
 * category and offered the queue's approve on it, so a round-cap hold was
 * `BUDGET · review_rounds_exhausted · Approve this hold` — three wrong
 * statements in one row (#2208).
 *
 * `composeHolds` and everything above it is pure. The two readings the page's
 * shared poll does not supply are taken at the foot of the file, where the
 * component's timer calls `readHoldSources`. The caps are among them because an
 * override that has been lifted has to leave a list that says it is standing.
 */
import {
  fetchBudget,
  fetchEpic,
  fetchEpics,
  type BudgetMap,
  type Dashboard,
  type DashboardRow,
  type EpicSnapshot
} from '$lib/api';
import { itemIdOf, orderRows, pruneKeyedState, rowKey, typeLabel } from '$lib/dashboard';
import { elapsed } from '$lib/epic/format';
import { RosterPoll } from '$lib/epic/roster-poll';
import { gaugeMoney } from '$lib/factory/gauge';
import {
  commandNote,
  describeHold,
  type HoldCategory,
  type HoldPlace
} from '$lib/monitor/hold-reasons';
import { workHref } from '$lib/monitor/live-runs';
import {
  isUnavailable,
  QUEUE_UNREAD,
  rowCaption,
  unreadable,
  type CaptionSegment,
  type Source
} from '$lib/monitor/readouts';
import { parseInstant } from '$lib/time';
import { safeAppHref, safeHref } from '$lib/safe-href';
import { reasonBody, rowSentence, rowSummary, rowWords } from '$lib/needs-you/summary';
import { itemRef } from '$lib/utils';
import { epicHref } from '$lib/work/refs';
import { attentionState } from '$lib/work/list';

/** The poll behind the blocked rows. Epic ticks are minutes apart, not seconds. */
export const HOLDS_POLL_MS = 30_000;

/** How long one round may stay open: past the beat, so a beat never finds a
 * round still running that the poll has already let go of. */
export const HOLDS_READ_HOLD_MS = HOLDS_POLL_MS * 2;

/** What the list says when every source answered and nothing is held. */
export const HOLDS_EMPTY = 'nothing held or blocked';

/** What the blocked rows say before their first read lands, or after it failed. */
export const EPICS_UNREAD = 'epics not read';

/**
 * The kind column's word.
 *
 * `blocked` is this column's own reading of an epic node, `approval` and
 * `needs` are the judgment queue's, and everything between them is a
 * `HoldCategory` — read from the hold REASON rather than from the
 * `budget_hold` row type, which every `epic_holds` row wears whatever held the
 * node (#2208: a round-cap hold is not a budget hold).
 */
export type HoldKind = 'blocked' | HoldCategory | 'approval' | 'needs';

/**
 * The artboard's order, top to bottom. Nothing sorts across it.
 *
 * Blocked first and the caps second, as drawn; the other hold categories land
 * where the single `budget` group used to sit, so a column that was read as
 * "blocks, then money, then people" still reads that way.
 */
export const HOLD_ORDER: readonly HoldKind[] = [
  'blocked',
  'budget',
  'review',
  'sandbox',
  'dispatch',
  'merge',
  'ci',
  'other',
  'approval',
  'needs'
];

/**
 * The status word a kind's mark is drawn from on a row that is NOT an epic
 * hold, so the shapes stay `factory/status`'s to decide: `blocked` is the
 * lavender square, `held` the yellow ring a cap already wears everywhere else,
 * and `needs_you` the magenta diamond the colour ration reserves for a human
 * decision.
 *
 * Not the whole answer, which is why `HoldRow.mark` is a field and not a lookup
 * at the point of drawing: `merge` is a category TWO tables reach. On a
 * needs-you entry it is `merge_ready` / `merge_decision` — a person's next move
 * and nothing else's, which is why the amber ring was wrong there (#2280 D14) —
 * and on an `epic_holds` row it is `base_integration_conflict`,
 * `base_integration_failed` or `merge_gate_touched`, where the merge queue IS
 * holding the node. One word read off the kind alone put the needs-you diamond
 * on all three of those.
 */
export const HOLD_MARK: Record<HoldKind, string> = {
  blocked: 'blocked',
  budget: 'held',
  review: 'held',
  sandbox: 'held',
  dispatch: 'held',
  merge: 'needs_you',
  ci: 'held',
  other: 'held',
  approval: 'needs_you',
  needs: 'needs_you'
};

/** The mark every `epic_holds` row wears, whatever its category: it is held. */
export const HELD_MARK = 'held';

/** A row's move as a link. `external` leaves kew, and the markup adds `↗`. */
export type HoldLink = { label: string; href: string; external: boolean };

/**
 * The word a row's mark prints, which follows the row's STATE, not its
 * category (#2539, D19): a held row says HELD in the amber the ring is drawn
 * in, whatever held it — a round-cap hold read REVIEW in amber, which is the
 * colour of a hold and the word of a review. A needs-you row keeps its
 * category, because there the category IS the person's move: MERGE, APPROVAL.
 */
export function holdWord(row: Pick<HoldRow, 'kind' | 'mark'>): string {
  if (row.mark === HELD_MARK) return 'held';
  if (row.mark === HOLD_MARK.blocked) return 'blocked';
  return row.kind;
}

/** An approve/deny offered on a row, labelled as the dashboard labels it. */
export type HoldAction = {
  label: string;
  action: 'approve' | 'deny';
  /** True for the affirmative button the dashboard draws as primary. */
  primary: boolean;
};

export type HoldRow = {
  /** Stable across polls; the key a decided row is dismissed under. */
  key: string;
  kind: HoldKind;
  /**
   * The `factory/status` word this row's mark is drawn from — decided where the
   * row is built, because the kind alone does not answer it: see `HOLD_MARK`,
   * and `HELD_MARK` for the table whose every row is a hold.
   */
  mark: string;
  /** The sentence right of the kind column, with its own reference linked. */
  detail: CaptionSegment[];
  /** The durable work-item id an approve/deny is POSTed for; null when none. */
  itemId: string | null;
  /**
   * The approve/deny this screen can WRITE. Empty for every row it cannot:
   * a blocked node, a cap override — and every epic hold, whose `epic_holds`
   * row `/api/approvals` has never been able to touch (#2208).
   */
  actions: HoldAction[];
  /**
   * What actually clears this row, in words: the cap key to raise, the
   * `kew epic unhold` to run. '' when nobody can act on it (the CI gate) or
   * when the row's own buttons are the action.
   */
  action: string;
  /**
   * The command that ends this wait, runnable as printed — the server's own
   * (`kew.waits.fill_command`), never parsed out of a reason string (#2280
   * D14). '' where kew has no command for it: a blocked node, a cap override,
   * a merge the forge performs.
   */
  command: string;
  /**
   * Whether this row came from the needs-you SET — the one the factory header
   * counts and the Work screen filters on (`needs_you_set` in
   * kew/src/kew/service/needs_you.py).
   *
   * The two readings this column takes for itself are not in that set: a node
   * blocked on an upstream node is nobody's move (`kew.waits` classifies
   * `blocked` to no attention kind — the upstream node is the row to act on),
   * and a cap override in force is a standing policy rather than a wait. They
   * stay drawn, because this column is named for them, and `HOLDS_EMPTY` and
   * the head's count stay over the DRAWN list so the head can never contradict
   * the rows beneath it.
   *
   * So the head's "N active" is holds-and-blocks and the header's "N needs you"
   * is the set — two words, two numbers, deliberately. What #2207 fixed is that
   * this column used to draw only the two set entries it could DECIDE and leave
   * the other twelve to the console, so a reader looking for a counted hold
   * could not find it anywhere.
   */
  counted: boolean;
  /**
   * The row's move when it is a link rather than a command: the server's
   * `Open PR #2059 to merge` for a merge (#2539), or the item's Work page for a
   * needs-you row nothing else on it acts on — so every magenta row carries an
   * action line (D20). Absent on every row that has a command, buttons or a
   * hold action instead.
   */
  link?: HoldLink | null;
  /**
   * A merge over a merge verify that failed at the PR's current head: the
   * item's MERGE VERIFY section, drawn before `link` because the failure is the
   * first thing to read (#3460). Absent on every other row.
   */
  verifyLink?: HoldLink | null;
};

export type HoldsSources = {
  /** Snapshots of the epics that have a blocked node; the blocked rows. */
  epics: Source<readonly EpicSnapshot[]>;
  /** `/api/config/budget` — the overridden caps. */
  budget: Source<BudgetMap>;
  /** The judgment queue — its budget-hold and approval-hold rows. */
  dashboard: Source<Dashboard>;
};

export type HoldsView = {
  rows: HoldRow[];
  /** The section head's right-hand reading: "3 active", "at least 2 active". */
  meta: string;
  /** Every source that did not answer, said in words; [] when all did. */
  notices: string[];
  /** False when a source is missing, which is what hedges `meta`. */
  complete: boolean;
  /**
   * The row-key families (`unreadFamilies`) whose source did not answer. A
   * dismissal under one of them survives the prune: its row is missing from
   * this read, which is not the same fact as its write having landed.
   */
  unread: string[];
};

// --- blocked ----------------------------------------------------------------

/**
 * The epic nodes the driver could not advance.
 *
 * A blocked node with no recorded reason is still listed and says so. FUI-10's
 * `blocked_reason` is null whenever the driver wrote nothing down, and
 * dropping the row on that basis would hide a real block behind a missing
 * annotation — the opposite of what this column is for.
 *
 * **One row per blocked node, not per epic that can see it.** `build_graph`
 * records a resolved out-of-epic dependency as an external boundary node
 * inside EVERY epic that depends on it, so a single blocked dependency shared
 * by three epics would otherwise be drawn three times and counted three times
 * in the head. The de-duplication is by node id, and the epic that OWNS the
 * node — the one carrying it as a member — wins the row, because that is where
 * the reader can act on it.
 *
 * The boundary copy is yielded to the owner only when the owner's copy is
 * ITSELF blocked, which is the only case where the owner draws a row to yield
 * to. Every epic is a separate snapshot read, so the two copies of one node can
 * disagree — a member copy read a moment earlier, or read while the tracker
 * still called it ready — and suppressing the blocked boundary node against a
 * member copy that draws nothing is how the block vanishes from the column
 * altogether, with `complete: true` behind the count and no notice.
 *
 * A boundary node no snapshot owns is still drawn. `build_graph` resolves any
 * dependency the tracker can answer for, including an item that belongs to no
 * epic at all, so "somebody else lists it" is not a fact this column has:
 * dropping every external node is how two epics report `counts.blocked: 1`
 * each while this column says "nothing held or blocked" with a confident
 * count behind it. `epics.py:149-152` tells callers wanting member-only
 * figures to filter `nodes` on `external=False`; the head count here is the
 * rows drawn, which is the stricter reading that filter is for.
 */
function blockedRows(snapshots: readonly EpicSnapshot[]): HoldRow[] {
  const ownedBlocked = new Set<string>();
  for (const snap of snapshots) {
    for (const node of snap.nodes) {
      if (!node.external && node.status === 'blocked') ownedBlocked.add(node.id);
    }
  }

  const rows: HoldRow[] = [];
  const drawn = new Set<string>();
  for (const snap of snapshots) {
    for (const node of snap.nodes) {
      if (node.status !== 'blocked') continue;
      // The owning epic lists it as a member AND reads it as blocked, so it is
      // drawing this same block; this is it seen from a dependent's boundary.
      if (node.external && ownedBlocked.has(node.id)) continue;
      if (drawn.has(node.id)) continue;
      drawn.add(node.id);
      const ref = itemRef(node.id, null);
      const reason = node.blocked_reason
        ? ` ${node.blocked_reason}`
        : ' blocked, with no reason recorded';
      rows.push({
        // An unowned boundary node is keyed by the node alone: it is one block
        // however many epics are waiting on it, and the epic it was first seen
        // through must not become part of its identity across polls.
        key: node.external ? `blocked:${node.id}` : `blocked:${snap.epic_ref}:${node.id}`,
        kind: 'blocked',
        mark: HOLD_MARK.blocked,
        counted: false,
        detail: [
          { text: ref, href: epicHref(snap.epic_ref) },
          { text: reason, href: null }
        ],
        itemId: null,
        actions: [],
        action: '',
        command: ''
      });
    }
  }
  return rows;
}

// --- budget -----------------------------------------------------------------

/**
 * The window a cap field governs, said as the artboard says it:
 * `budget.weekly_usd` → "weekly", `budget.per_issue_usd` → "per-issue". A
 * field this rule has not seen still reads as its own name rather than being
 * dropped, so a new cap appears the day the server starts serving it.
 */
export function capWindow(field: string): string {
  return field
    .replace(/^budget\./, '')
    .replace(/_usd$/, '')
    .replace(/_/g, '-');
}

/**
 * What an override in force says, built from `file_value`, `value` and
 * `set_by` — never from the operator's free-text `reason`, which is a note to
 * the next human and not a fact this row can stand behind.
 *
 * The direction is read off the two numbers rather than assumed: "raised to"
 * over a *lowered* cap would be the sentence saying the opposite of the money.
 */
export function overrideCopy(field: string, prior: number, value: number, setBy: string): string {
  const who = setBy ? ` (${setBy})` : '';
  const window = capWindow(field);
  if (value > prior) {
    return `${window} cap reached at ${gaugeMoney(prior)}; raised to ${gaugeMoney(value)}${who}`;
  }
  if (value < prior) {
    return `${window} cap lowered from ${gaugeMoney(prior)} to ${gaugeMoney(value)}${who}`;
  }
  return `${window} cap overridden at ${gaugeMoney(value)}${who}`;
}

/**
 * One row per cap an override is in force on, in the order the server serves
 * the map. A field whose `override` is null is the config file's own cap doing
 * its job, which is not a hold.
 */
function overrideRows(budget: BudgetMap): HoldRow[] {
  const rows: HoldRow[] = [];
  for (const [field, entry] of Object.entries(budget)) {
    const over = entry?.override;
    if (!over) continue;
    // A cap RAISED above the file value is the operator's answer to a budget
    // hold, not a hold of its own: the spend it was blocking is running again,
    // and counting it kept a cleared hold in the "N active" head for as long as
    // the override stood (#2280 D14). A cap lowered below the file value is
    // still a live restriction, so it stays.
    if (over.value >= entry.file_value) continue;
    rows.push({
      key: `budget:${field}`,
      kind: 'budget',
      mark: HOLD_MARK.budget,
      counted: false,
      detail: [
        { text: overrideCopy(field, entry.file_value, over.value, over.set_by), href: null }
      ],
      itemId: null,
      actions: [],
      action: '',
      command: ''
    });
  }
  return rows;
}

// --- the dashboard's own holds ----------------------------------------------

/** Approve and deny, worded exactly as the console's queue words them. */
const ROW_ACTIONS: Record<string, HoldAction[]> = {
  approval_hold: [
    { label: 'Deny', action: 'deny', primary: false },
    { label: 'Approve', action: 'approve', primary: true }
  ],
  // `budget_hold` — every `epic_holds` row — is deliberately absent (#2208).
  // `decideHold` POSTs `/api/approvals/<item>`, which releases the GitHub
  // needs-approval LABEL (`kew.approval.remote_approve`); it has never read
  // the `epic_holds` table, so "Approve this hold" on a round-cap or CI hold
  // was a button whose only possible answer is `not_held`. What does clear one
  // is named on the row instead: `HoldRow.action`.
  //
  // The old console's "Raise cap…" is absent for its own reason: it is a
  // policy change for ALL future issues, and it belongs on Budget, the
  // surface that can explain itself.
};

/**
 * The row type every `epic_holds` row is served under — `_budget_rows`.
 *
 * Exported because the Work pane's entry block (`entryDetail` in
 * `$lib/work/entry-detail`) has to ask the same question this column asks
 * before it lets the hold vocabulary speak for a row: did this entry come out
 * of that table? The type is historical and says nothing about WHAT held the
 * node — that is the reason's job (#2208) — but it is the one field that says
 * which table the row came from, which is exactly what a reader of
 * `HOLD_SPECS` needs to know.
 */
export const EPIC_HOLD = 'budget_hold';

/**
 * Which column a set entry sits in.
 *
 * An epic hold's word comes from its REASON, through the vocabulary
 * `epic_holds.py` owns: the `budget_hold` type says only which table the row
 * came out of, and reading it as the category is how a round-cap hold came to
 * be drawn as BUDGET (#2208). An approval hold keeps its own word.
 *
 * Every other set entry is read the same way round: its reason FIRST, wherever
 * that vocabulary claims it (`reasonCategory`), and the kind of wait the server
 * stamped on it behind that (`attentionCategory`). Reason-first because the
 * sentence beside the word is composed reason-first — a round-cap hold served
 * as an `epic_wait` read "the review round cap was reached" under the word
 * `OTHER` while the same reason out of `epic_holds` read `REVIEW` (#2280).
 *
 * Open, not exhaustive. A row type this file has never heard of is still a
 * thing waiting on a person — the server said so by putting it in the set — so
 * it lands in `needs` and is drawn, rather than being filtered into silence by
 * a table nobody remembered to extend.
 */
export function holdKindOf(row: DashboardRow): HoldKind {
  if (row.type === 'approval_hold') return 'approval';
  if (row.type === EPIC_HOLD) return describeHold(row.reason ?? '', holdPlace(row)).category;
  return reasonCategory(row) ?? attentionCategory(row);
}

/**
 * The hold table's category for a set row whose REASON that table claims, or
 * `null` where it claims none.
 *
 * The sentence right of the kind column is composed by that table wherever it
 * has words for the row's reason — that is `rowWords`' first rule (#2245, "for
 * a reason the table claims, the table's words win") — so the category beside
 * it has to come from the same reading, or one row says two things in two
 * adjacent cells. The committed round-cap hold is served as an `epic_wait`
 * carrying `review_rounds_exhausted`: its sentence read "the review round cap
 * was reached" under the word `OTHER`, while the identical reason served out of
 * `epic_holds` read `REVIEW` two rows up (#2280).
 *
 * The reason is matched through `reasonBody`, because the set writes the item's
 * own reference onto the front of an `epic_wait` reason and `HOLD_SPECS`
 * matches on a prefix — the same string `entryBlock`'s action is read from.
 *
 * It decides the WORD and not the mark: see `setRow`, which keeps the mark on
 * the kind the server stamped, so a node the merge queue is holding cannot
 * take the diamond this UI rations to a person's move (#2280 D14).
 */
function reasonCategory(row: DashboardRow): HoldCategory | null {
  const claimed = describeHold(reasonBody(rowWords(row)), holdPlace(row));
  return claimed.label === '' ? null : claimed.category;
}

/** The category the kind of wait the SERVER named on the row earns. */
function attentionCategory(row: DashboardRow): HoldKind {
  return own(ATTENTION_CATEGORY, row.attention_kind ?? '') ?? 'needs';
}

/**
 * The category word a set entry that is NOT an epic hold earns, from the kind
 * of wait the server named on it (`AttentionKind` in kew/src/kew/waits.py) —
 * read where the hold vocabulary claims nothing about the row's reason, and
 * read for the MARK of every such row whatever its category word ends up
 * being (`setRow`).
 *
 * `needs` was the answer for all of them, so the one row on the Monitor of the
 * design review said `NEEDS` over a merge decision — the category column's
 * whole job is to say WHICH kind of wait this is, and "needs" is the word for
 * "I did not look" (#2280 D14). Open, not exhaustive: a kind this table has
 * never heard of is still a thing waiting on a person, so it falls back to
 * `needs` and is drawn.
 *
 * Every entry here is also a claim about the MARK, through `HOLD_MARK`, so a
 * kind only earns a `HoldCategory` where the machinery really is holding the
 * node. `work_exception` is the one that is deliberately ABSENT: the projection
 * raised an exception on an item and a person has to go and look, which the
 * server itself words `manual` ("a person is next, and nothing more is known"
 * — `ATTENTION_STATE_WORD`) and stamps `waiting`, not `held`. Filing it under
 * `review` — the category the review MACHINERY's holds earn, a crashed
 * reviewer or a spent round cap — drew the amber hold ring over a row whose own
 * stamp said `waiting` and spent the needs-you diamond on nothing. It falls to
 * `needs`, which is what it is.
 */
const ATTENTION_CATEGORY: Record<string, HoldKind> = {
  merge_ready: 'merge',
  merge_decision: 'merge',
  budget_exhausted: 'budget',
  sandbox_refused: 'sandbox',
  publication_held: 'other',
  held: 'other',
  approval: 'approval',
  // Named, not left to the fallback (#2971): a node the host restarted under
  // twice FAILED and nothing holds it, so its mark is the needs-you diamond —
  // read off this kind, never off the `host_restart_repeated` in its reason.
  host_restart_repeated: 'needs'
};

/**
 * Own-property lookup: the kind comes off the wire, and `['constructor']`
 * would otherwise answer out of `Object.prototype` (the same guard
 * `factory/status.ts` and `work/list.ts` take).
 */
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/**
 * The epic and item a hold stands on, as the row carries them.
 *
 * The `0` sentinel a non-GitHub payload puts in `issue_num` names nothing, so
 * it is read as no item at all rather than becoming `#0` in a sentence and an
 * item-zero link in a command.
 */
function holdPlace(row: DashboardRow): HoldPlace {
  const id = itemIdOf(row) ?? '';
  return { epic: row.epic_ref ?? '', item: id === '0' ? '' : id };
}

/**
 * How long the hold has stood: `held 2h13m`, from the row's own `age` (the
 * `held_at` the server stamps it with). '' for a stamp that cannot be read —
 * never a fabricated duration.
 */
export function heldFor(age: string | null | undefined, nowMs: number): string {
  const since = parseInstant(age);
  return since === null ? '' : `held ${elapsed(nowMs - since)}`;
}

/**
 * One epic hold's sentence: the epic, the item, the reason in words and how
 * long it has stood — the four things the first-pass row left out, which is
 * why `BUDGET · review_rounds_exhausted · Approve this hold` named neither the
 * epic nor the issue it was about (#2208).
 *
 * Both references are links, and both lead INTO the app: the epic to its own
 * page, the item to the Work row that can act on it. A hold row carries no
 * `url` (see `_budget_rows`), so there is no outward link to prefer.
 */
function holdDetail(
  row: DashboardRow,
  place: HoldPlace,
  sentence: string,
  stamp: string
): CaptionSegment[] {
  const head: CaptionSegment[] = [];
  if (place.epic !== '') head.push({ text: place.epic, href: epicHref(place.epic) });
  if (place.item !== '') {
    if (head.length > 0) head.push({ text: ' / ', href: null });
    head.push({ text: itemRef(place.item, null), href: safeHref(row.url) ?? workHref(place.item) });
  }
  const tail = [sentence, stamp].filter((part) => part !== '').join(' · ');
  return [...head, { text: head.length > 0 ? ` — ${tail}` : tail, href: null }];
}

/**
 * How long a SET row that is not an epic hold has stood, in the word its own
 * kind earns.
 *
 * `heldFor` says `held` of everything, which is true of an `epic_holds` row
 * and false of most of the rest of the set: a merge decision, an orphan PR and
 * a driver nobody is ticking are waits, not holds. The word follows
 * `attentionState` — the same table the Work pane's Since row and the list's
 * State column read, so the three cannot disagree about whether this wait is a
 * hold (#2280 D14).
 */
function stoodFor(row: DashboardRow, nowMs: number): string {
  if (attentionState(row.attention_kind) === 'held') return heldFor(row.age, nowMs);
  const since = parseInstant(row.age);
  return since === null ? '' : `waiting ${elapsed(nowMs - since)}`;
}

/**
 * The needs-you set as rows — every entry of it, in the server's own
 * consequence order.
 *
 * It used to read only `budget_hold` and `approval_hold`, which is why the head
 * said "4 active" while the header counted 16 of the same thing (#2207). The
 * filter is gone: the set IS the column's row list, so the two numbers on the
 * screen are one number read twice.
 */
function queueRows(dashboard: Dashboard, nowMs: number): HoldRow[] {
  return orderRows(dashboard.rows).map((r: DashboardRow) =>
    r.type === EPIC_HOLD ? epicHoldRow(r, nowMs) : setRow(r, nowMs)
  );
}

/**
 * The sentence that follows the place `holdDetail` has just named.
 *
 * The needs-you set writes the item's own reference onto every `epic_wait`
 * reason (`item_ref(node_id)` in service/needs_you.py), and `rowSummary` keeps
 * it — so prefixing the place onto it said the item twice: `launch-fixes /
 * #2055 — #2055 — the review round cap was reached`. Where the head links the
 * item, the sentence beside it is the ref-less reading; where the head names
 * only the epic, the reference is the sentence's own to carry, because nothing
 * else on the row would say it.
 *
 * A row that says nothing at all falls back to its type, as it always did: the
 * list is of places something is stuck, and a blank line is not one.
 */
/**
 * A set row's move as a link, where it has one and nothing else on it acts.
 *
 * The server's own link first (a merge's `Open PR #N to merge`). Then, for a
 * row the mark draws magenta with no command and no button, the item's Work
 * page — that is where a person's move on it is made, and a magenta row with no
 * line under it says "you" and then nothing about what (#2539, D20).
 */
function setLink(row: DashboardRow, mark: string, command: string): HoldLink | null {
  // A served URL is a link only through the href gate (C10); a refused one
  // falls through as though the server had named none.
  const served = row.link ?? null;
  const href = safeAppHref(served?.href);
  if (served != null && href != null)
    return { label: served.label, href, external: /^https?:/i.test(href) };
  if (mark !== HOLD_MARK.needs || command !== '' || (ROW_ACTIONS[row.type] ?? []).length > 0)
    return null;
  const item = itemIdOf(row);
  return item == null ? null : { label: 'Decide in Work', href: workHref(item), external: false };
}

/** The server's link to a failed merge verify (#3460), or `null`. */
function verifyLinkOf(row: DashboardRow): HoldLink | null {
  const served = row.verify_link ?? null;
  const href = safeAppHref(served?.href);
  if (served == null || href == null) return null;
  return { label: served.label, href, external: /^https?:/i.test(href) };
}

/**
 * True for an approval hold whose sentence never names its item (design review
 * c9 D12). The server says an approval as the action it waits on, which names
 * no item; every other row with no epic already says its reference, or links
 * it where it lives, and keeps `rowCaption` as before.
 *
 * The reference counts as named only as a whole token: `#11` is not named by a
 * sentence about `#114`, nor `SFD-22` by one about `SFD-229`.
 */
function unplacedApproval(row: DashboardRow, place: HoldPlace): boolean {
  if (row.type !== 'approval_hold' || place.item === '') return false;
  const ref = itemRef(place.item, null).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return !new RegExp(`(?:^|[^\\w#-])${ref}(?![\\w-])`).test(rowSummary(row));
}

function placedSentence(row: DashboardRow, place: HoldPlace): string {
  const said = place.item === '' ? rowSummary(row) : rowSentence(row);
  return said || typeLabel(row.type, 1);
}

/**
 * One set entry that is not an epic hold, read the way the epic holds beside it
 * are: the epic and the item first, then the sentence, then how long it has
 * stood — and the command the server computed, on its own line with a Copy
 * button (#2280 D14).
 *
 * The prefix is the change. `rowCaption` threads the row's reference INTO its
 * sentence wherever the sentence happens to mention it, so a merge decision
 * read `merge decision on PR #2059` — no epic, no item, nothing to tell it
 * apart from the hold two rows down. The column is a list of places something
 * is stuck, and every row now says which place.
 */
function setRow(row: DashboardRow, nowMs: number): HoldRow {
  const place = holdPlace(row);
  const kind = holdKindOf(row);
  const mark = HOLD_MARK[attentionCategory(row)];
  const command = (row.command ?? '').trim();
  return {
    key: rowKey(row),
    kind,
    // The mark is read off the kind of wait the SERVER stamped, not off the
    // category word beside it: `kind` follows the reason wherever the hold
    // table claims it (`reasonCategory`), and three of that table's categories
    // are `merge` — reasons the merge queue itself is holding a node on. The
    // diamond this UI rations to "a person is the next move" may not be spent
    // on one of those, and the amber hold ring may not be drawn over a row
    // whose own stamp says `waiting` (#2280 D14). `attentionCategory` is the
    // same field `stoodFor` words that stamp from, so the two cannot disagree.
    mark,
    // The prefix is for the rows that name an EPIC: those are the ones
    // `rowCaption` cannot place, because it threads the item reference into
    // whatever the sentence happens to say and names no epic at all — so a
    // merge decision read `merge decision on PR #2059`, with nothing to tell
    // it apart from the hold two rows down (#2280 D14). A row with no epic is
    // left exactly as it was: its own reference is already in its sentence,
    // and a `#77 — approval needed on #77` prefix would say it twice. The one
    // exception is an approval hold whose sentence does NOT name its item
    // (`unplacedApproval`, design review c9 D12): it gets the item head and
    // reads
    // "#114 — approve a run estimated at $25.00, the per-issue cap; …", never
    // an action with no item to take it on.
    detail:
      place.epic === '' && !unplacedApproval(row, place)
        ? rowCaption(row)
        : holdDetail(row, place, placedSentence(row, place), stoodFor(row, nowMs)),
    itemId: itemIdOf(row),
    actions: ROW_ACTIONS[row.type] ?? [],
    action: '',
    command,
    counted: true,
    link: setLink(row, mark, command),
    verifyLink: verifyLinkOf(row)
  };
}

/**
 * One `epic_holds` row, read once: the reason decides the word, the sentence
 * and the action together, so the three cannot disagree about what held the
 * node.
 */
function epicHoldRow(row: DashboardRow, nowMs: number): HoldRow {
  const place = holdPlace(row);
  const words = describeHold(row.reason ?? '', place);
  const command = (row.command ?? '').trim();
  return {
    key: rowKey(row),
    kind: words.category,
    // `HELD_MARK`, not `HOLD_MARK[category]`: every row of this table IS a
    // hold, and three of its reasons are `merge` — the category a needs-you
    // merge decision also earns, where the diamond is right and here it is not.
    mark: HELD_MARK,
    // `heldFor`, not `stoodFor`: every row of this table IS a hold, whatever
    // attention kind the server put on it.
    // The sentence is the server's (#2539), the one Work and the epic popover
    // print for this row too; the hold table still decides the word and the
    // action, which are not sentences about the wait.
    detail: holdDetail(row, place, placedSentence(row, place), heldFor(row.age, nowMs)),
    itemId: itemIdOf(row),
    // No button, whatever the hold: see `ROW_ACTIONS`. `words.action` is what
    // this row offers instead, and it is a sentence rather than a write.
    actions: [],
    // One statement per hold (#2896 R7): a row with a command block carries
    // no action line under it — the block is what clears the hold. Without a
    // command the action is the row's only statement of what ends the hold,
    // and `commandNote` reads it as the Work pane's `Do this` note does.
    action: command === '' ? commandNote(words.action, '') : '',
    command,
    counted: true
  };
}

/**
 * The judgment-queue sources this column reads: the `unavailable` name, what a
 * notice calls it, and the row `type` — which is also the key family, since
 * `rowKey` is `type:id` — whose rows it serves. A dashboard that fail-softed one
 * of them serves the rest, so the column must say which reading it is missing
 * rather than drawing the survivors as the whole world.
 */
const QUEUE_SOURCES = [
  { name: 'budget_holds', said: 'budget holds', family: 'budget_hold' },
  { name: 'approval_holds', said: 'approval holds', family: 'approval_hold' },
  { name: 'pr_reviews', said: 'the work projection', family: 'pr_review' },
  { name: 'work_rows', said: 'work exceptions', family: 'work_exception' },
  { name: 'stalled_epics', said: 'stalled epics', family: 'stalled_epic' },
  { name: 'epic_waits', said: 'epic waits', family: 'epic_wait' },
  { name: 'orphan_prs', said: 'orphan PRs', family: 'orphan_pr' },
  { name: 'triage_proposals', said: 'triage proposals', family: 'triage_proposal' },
  { name: 'merge_queue', said: 'the merge queue', family: 'merge_queue_suspect' }
] as const;

/**
 * The key families whose source did not answer this read.
 *
 * Every key in this column is `<family>:<rest>`, one family per source that can
 * fail on its own: `blocked:…` from the epics walk, `budget:<field>` from the
 * caps map, and `rowKey`'s `budget_hold:…` / `approval_hold:…` from the queue —
 * which fail-softs per row source inside one 200 answer. `pruneRowState` holds
 * a key of an unread family rather than reading its absence as a landed write.
 */
function keyFamily(key: string): string {
  const cut = key.indexOf(':');
  return cut === -1 ? key : key.slice(0, cut);
}

function unreadFamilies(sources: HoldsSources): string[] {
  const unread: string[] = [];
  if (isUnavailable(sources.epics)) unread.push('blocked');
  if (isUnavailable(sources.budget)) unread.push('budget');
  if (isUnavailable(sources.dashboard)) {
    for (const source of QUEUE_SOURCES) unread.push(source.family);
  } else {
    const out = new Set(sources.dashboard.unavailable ?? []);
    for (const source of QUEUE_SOURCES) if (out.has(source.name)) unread.push(source.family);
  }
  return unread;
}

// --- composition ------------------------------------------------------------

/** The head's right-hand reading. An incomplete list never claims a total. */
export function holdsMeta(count: number, complete: boolean): string {
  if (!complete) return count === 0 ? 'count unknown' : `at least ${count} active`;
  return count === 0 ? 'none active' : `${count} active`;
}

/**
 * The rows still on screen: everything `composeHolds` built, less the ones a
 * successful approve/deny has already removed.
 *
 * Dismissal is kept OUTSIDE the composition on purpose — the sources go on
 * serving a decided hold until the next read catches up, and re-deriving the
 * list from them would put the row back under the operator's cursor.
 */
export function visibleHolds(
  rows: readonly HoldRow[],
  dismissed: Readonly<Record<string, boolean>>
): HoldRow[] {
  return rows.filter((row) => !dismissed[row.key]);
}

/**
 * Per-row state (a dismissal, a write error) narrowed to the rows the sources
 * are still serving — `dashboard.ts`'s own `pruneKeyedState`, over this
 * column's row keys. The console prunes its queue the same way against the
 * same keys (`pruneDashboardState`): a dismissal that outlives the read it
 * bridges is how either surface comes to swallow the next hold raised on the
 * same item.
 *
 * It is pruned against the view, not the row list, because a row missing from
 * an UNREAD source is not a row that left. A dashboard that fail-softed
 * `approval_holds` answers 200 with no approval rows in it, and a decided row
 * whose dismissal was dropped on that reading is drawn again the moment the
 * source recovers while the write's own read-back still lags — the CS-6
 * resurrection the dismissal exists to prevent. Same for a failed epics walk or
 * caps read: those rows are missing, not gone.
 */
export function pruneRowState<T>(view: HoldsView, state: Record<string, T>): Record<string, T> {
  const unread = new Set(view.unread);
  const held = Object.keys(state).filter((key) => unread.has(keyFamily(key)));
  return pruneKeyedState([...view.rows.map((row) => row.key), ...held], state);
}

/**
 * The column as it is actually drawn: the composed view less the rows a decide
 * has already removed, counted over what is left.
 *
 * The head counts the DRAWN list, never the composed one. Counting the rows
 * dismissal hid is how a section head comes to read "1 active" directly above
 * the words "nothing held or blocked" — the surface contradicting itself about
 * the single fact it exists to state.
 */
export function drawnHolds(
  view: HoldsView,
  dismissed: Readonly<Record<string, boolean>>
): HoldsView {
  const rows = visibleHolds(view.rows, dismissed);
  return { ...view, rows, meta: holdsMeta(rows.length, view.complete) };
}

/**
 * The rows the demo's "What needs me?" tour points at, by row key: the first
 * magenta row that carries the command the server computed, and the first
 * merge decision, whose move is a link to the forge. An amber hold carries a
 * command too, but a person is not its next move, so it is never the one.
 * One row each at most, because a tour step highlights one element.
 */
export function tourTargets(rows: readonly HoldRow[]): Record<string, string> {
  const yours = rows.filter((row) => row.mark === HOLD_MARK.needs);
  const command = yours.find((row) => row.command !== '');
  const merge = yours.find((row) => row.command === '' && row.link?.external === true);
  const out: Record<string, string> = {};
  if (command) out[command.key] = 'hold-command';
  if (merge) out[merge.key] = 'merge-decision';
  return out;
}

/**
 * The drawn rows that are the needs-you set's own — `HoldRow.counted`.
 *
 * Nothing in the column's own head reads this: the head counts what it drew.
 * It is the column's answer to "which of these rows is the thing the header
 * counted", which is what a reader crossing from the header is looking for, and
 * what `holds.test.ts` holds the set-shaped half of this list to.
 */
export function countedHolds(rows: readonly HoldRow[]): HoldRow[] {
  return rows.filter((row) => row.counted);
}

/**
 * The whole column, from three sources that each fail on their own.
 *
 * Pure: the caller owns the fetching and the clock, so a vitest can hold every
 * row, word, duration and count here to the artboard. `nowMs` is what the
 * since-when on each hold row is measured against — the component's own second
 * hand, exactly as `liveRunsView` takes it.
 */
export function composeHolds(sources: HoldsSources, nowMs: number = Date.now()): HoldsView {
  const notices: string[] = [];
  // Spelled out rather than derived from HOLD_ORDER, so a kind added to the
  // union is a type error here and not a row silently pushed onto undefined.
  const byKind: Record<HoldKind, HoldRow[]> = {
    blocked: [],
    budget: [],
    review: [],
    sandbox: [],
    dispatch: [],
    merge: [],
    ci: [],
    other: [],
    approval: [],
    needs: []
  };

  if (isUnavailable(sources.epics)) notices.push(sources.epics.reason);
  else byKind.blocked = blockedRows(sources.epics);

  if (isUnavailable(sources.budget)) notices.push(sources.budget.reason);
  else byKind.budget = overrideRows(sources.budget);

  if (isUnavailable(sources.dashboard)) notices.push(sources.dashboard.reason);
  else {
    const out = new Set(sources.dashboard.unavailable ?? []);
    for (const source of QUEUE_SOURCES) {
      if (out.has(source.name)) notices.push(`${source.said} unavailable`);
    }
    for (const row of queueRows(sources.dashboard, nowMs)) byKind[row.kind].push(row);
  }

  const rows = HOLD_ORDER.flatMap((kind) => byKind[kind]);
  const complete = notices.length === 0;
  return {
    rows,
    meta: holdsMeta(rows.length, complete),
    notices,
    complete,
    unread: unreadFamilies(sources)
  };
}

// --- the two readings this column takes for itself --------------------------

/** The reads behind the polled sources, injectable so a vitest can drive them. */
export type HoldsFetchers = {
  epics: typeof fetchEpics;
  epic: typeof fetchEpic;
  budget: typeof fetchBudget;
};

/** The pair `composeHolds` needs that the page's shared poll does not supply. */
export type PolledSources = Pick<HoldsSources, 'epics' | 'budget'>;

/** What the column has in hand when it assembles its three sources. */
export type HoldsInputs = {
  /** The component's own reading, or null until its first poll answers. */
  polled: PolledSources | null;
  /** `/api/config/budget` as the page loaded it — the seed, nothing more. */
  seedBudget: Source<BudgetMap>;
  /** A payload from the caller; when present the column does not poll at all. */
  epics?: Source<readonly EpicSnapshot[]>;
  /** The judgment queue off the shell's shared poll; null before its first tick. */
  dashboard: Source<Dashboard> | null;
};

/**
 * The three sources, chosen from what the column has — the rule the component
 * used to carry inline, kept here where a vitest can hold it.
 *
 * The caps come from the POLL as soon as it has answered, and the page's
 * `load()` map stands in only until then: a map read once at navigation goes
 * on drawing an override another operator has already lifted. A poll that
 * FAILED is an answer too — it replaces the seed rather than falling back to
 * it, so the column says "budget unavailable" instead of quietly re-serving a
 * reading it took minutes ago as though it were current.
 *
 * A reading nobody has taken yet is named, never defaulted to empty: before
 * the first poll the epics are `EPICS_UNREAD`, and a shell whose pulse has not
 * ticked leaves the queue `QUEUE_UNREAD`. Both hedge the head count, which is
 * the point — see `holdsMeta`.
 */
export function holdSources(inputs: HoldsInputs): HoldsSources {
  return {
    epics: inputs.epics ?? inputs.polled?.epics ?? { available: false, reason: EPICS_UNREAD },
    budget: inputs.polled?.budget ?? inputs.seedBudget,
    dashboard: inputs.dashboard ?? { available: false, reason: QUEUE_UNREAD }
  };
}

/**
 * The blocked nodes, with one snapshot per epic that has one.
 *
 * One failed snapshot fails the whole reading rather than quietly serving the
 * epics that did answer: a short list of blocks that looks complete is the
 * failure this column exists to avoid, and "epics unavailable" is the honest
 * reading of a partial walk.
 *
 * Only the epics whose `counts.blocked` is non-zero are opened — a monitor left
 * open all day must not walk every epic every poll. Those counts include
 * external boundary nodes at their graph status, which is what makes them the
 * right filter: an epic held up by a blocked dependency it merely depends on
 * is opened too, and `blockedRows` decides whether that node is already listed
 * by the epic that owns it.
 *
 * An ARCHIVED epic is not opened at all (#2243). `kew epic archive` promises
 * the epic "is excluded from the console's needs-you count", and the server
 * keeps that promise for every entry the epic owns — its stalled row, its
 * nodes' waits and its driver's open holds. This column walks the epics itself
 * rather than reading `rows`, so a missing check here would draw blocks the
 * header has already stopped counting: the four-surface disagreement #2207
 * removed, one epic over. `archived` is optional on the wire, so a missing
 * field reads as active — an older `kew serve` saying nothing is not a
 * retirement decision.
 */
async function readBlockedEpics(
  f: HoldsFetchers,
  signal?: AbortSignal
): Promise<readonly EpicSnapshot[]> {
  const summaries = await f.epics(fetch, signal);
  const blocked = summaries.filter((e) => !e.archived && (e.counts?.blocked ?? 0) > 0);
  const details = await Promise.all(blocked.map((e) => f.epic(e.epic_ref, fetch, signal)));
  return details.map((d) => d.snapshot).filter((s): s is EpicSnapshot => s !== null);
}

/**
 * Both readings, re-taken every poll.
 *
 * The overrides are re-read here rather than loaded once with the page: an
 * override raised, reverted or re-pointed by another operator moves a row in
 * and out of this column, and a map read at navigation would go on drawing a
 * hold that no longer exists — with `complete: true` behind the count, which is
 * precisely the confident-reading-over-a-stale-world failure the column's
 * second rule forbids.
 *
 * `allSettled`, not `all`: the two fail apart, and each becomes the reason its
 * own rows are missing rather than taking the other's rows with it.
 *
 * `signal` reaches every fetch in the round, so a round the poll aborts stops
 * its requests rather than leaving them running behind the poll's guard.
 */
export async function readHoldSources(
  fetchers: Partial<HoldsFetchers> = {},
  signal?: AbortSignal
): Promise<PolledSources> {
  const f = { epics: fetchEpics, epic: fetchEpic, budget: fetchBudget, ...fetchers };
  const [epics, budget] = await Promise.allSettled([
    readBlockedEpics(f, signal),
    f.budget(fetch, signal)
  ]);
  return {
    epics: epics.status === 'fulfilled' ? epics.value : unreadable('epics', epics.reason),
    budget: budget.status === 'fulfilled' ? budget.value : unreadable('budget', budget.reason)
  };
}

/**
 * The column's poll: one round out at a time, and the newest answer wins
 * (#3468 C9).
 *
 * A round is `/api/epics`, one snapshot per blocked epic and the caps, every
 * `HOLDS_POLL_MS`. Started bare off the interval, a server slower than the
 * beat stacked one more round per tick, and a slow round landing after a newer
 * one drew the older world over it. This is the Epics list's `RosterPoll`, not
 * a guard of its own: a beat while a round is out starts nothing, a round
 * older than the newest drawn is never drawn, and the tab coming back aborts
 * whatever is out and reads once. `readHoldSources` settles every source into
 * a reading of its own, so a round never fails; a round that cannot matter any
 * more is simply not drawn.
 *
 * The round carries the poll's `AbortSignal` into every fetch, and may stay
 * open for `HOLDS_READ_HOLD_MS` — two beats, not the Epics list's 20s, which
 * is shorter than this beat: under it a round still out at the next beat had
 * already left the guard, and a server slower than 20s was never drawn at all.
 * A round past the hold is aborted, its fetches with it, so a server that
 * never answers costs one round open at a time.
 */
export function holdsPoll(
  land: (sources: PolledSources) => void,
  hidden: () => boolean,
  fetchers: Partial<HoldsFetchers> = {}
): RosterPoll<PolledSources> {
  return new RosterPoll<PolledSources>({
    read: (signal) => readHoldSources(fetchers, signal),
    land,
    fail: () => {},
    hidden,
    holdMs: HOLDS_READ_HOLD_MS
  });
}
