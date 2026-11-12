/**
 * FUI-21 — the Work screen's lower half, as pure models: the needs-you band
 * (DWork y ≈522–598), the description panel and the evidence ledger
 * (DWork y ≈624–928).
 *
 * Three claims live here, and each of them is a claim a surface can get wrong
 * in a way nobody notices:
 *
 * 1. **Magenta is rationed.** The band is the one place on this screen that
 *    paints solid magenta, which the whole UI reserves for "a person is the
 *    next move" (Factory UI plan finding 3). So the band is not shown because an item
 *    looks stuck, or because the exception is blocking: it is shown when the
 *    projection's own judgment says a human decision is outstanding, or when
 *    the ONE safest action it derived is an action kew cannot perform. Which
 *    actions those are is `SAFEST_ACTION_ACTOR`, a transcription of
 *    `derive_verdict`'s vocabulary held to it by
 *    `kew/tests/test_factory_exception_parity.py` — a new action nobody
 *    classifies would otherwise silently stop raising a band.
 * 2. **A green check means the host vouched for the fact.** `state ===
 *    'attested'` is the only row that gets one, and an unverified chain
 *    downgrades every row before the question is even asked (`evidenceState`).
 *    That guard is enforced here as well as on the server, and it is shared
 *    with the DEpic rail (`epic/rail.ts` calls into it) so there is exactly one
 *    copy of it in the frontend.
 * 3. **A description is a REMOTE read, so the panel says what it is.** The
 *    body is drawn only for `availability` "available" or "stale" (labelled),
 *    and every other answer — including one this file does not recognise — is
 *    the server's reason, with the "Open on GitHub" link beside it wherever the
 *    payload named a URL. It names one from the READING, so an answer that
 *    landed no reading (`availability: "unavailable"`) carries `url: null` and
 *    gets the reason alone: this panel never constructs a tracker URL of its
 *    own, because a guessed link is a claim about where the item lives that
 *    nothing here observed. An empty body under any other availability is a
 *    failed read, never a short issue. And a read still in flight is none of
 *    those: it claims nothing at all, and is taken again on the shell's tick
 *    (`rereadsDescription`) so a refusal that clears does not outlive itself —
 *    but only the refusals the tracker never heard, because a retry of a
 *    reading that DID reach it spends a ration the whole process shares.
 *
 * Nothing here words a fact about an item. Titles, the count and the fallback
 * note are UI furniture; every sentence a reader sees about their own work is
 * the projection's own (`safest_action.reason`, `exception.detail`,
 * `ItemEvidence.reason`, `ItemDescription.reason`), printed verbatim.
 */
import type { ItemDescription, ItemEvidence, EvidenceRow, WorkView } from '$lib/api';
import { QUIET_MEANINGS, statusShape } from '$lib/factory/status';
import { fmtTime, utcTitle } from '$lib/time';
import { parseMarkdown, type Block } from './markdown';
import { prRef } from './refs';
import { safeHref } from '$lib/safe-href';

/** What a cell with no reading shows. The artboard's own em dash. */
const DASH = '—';

// --- The needs-you band ------------------------------------------------------

/**
 * Who acts next, per safest action `kew.service.work.derive_verdict` derives.
 *
 * `person` means kew cannot do it: a decision only a human makes (a merge, a
 * zero-diff outcome, a risk hold), an edit kew does not perform (a conflict, a
 * red build, a reviewer's changes), or a look kew is explicitly asking for.
 * `kew` means the console or the driver performs it — those get the header's
 * offered button, or the wait the projection asked for, and no magenta.
 *
 * This table is a transcription, so it is not the authority:
 * `kew/tests/test_factory_exception_parity.py` reads every `action=` out of
 * `_verdict()` and fails on one this table does not classify. An action missing
 * from BOTH is the failure that test exists to catch — an unclassified action
 * reads as `kew` here, so a band that should be magenta would simply never
 * appear, which is the silent direction.
 */
export const SAFEST_ACTION_ACTOR: Record<string, string> = {
  address_review_feedback: 'person',
  await_human_decision: 'person',
  confirm_no_change: 'person',
  confirm_publication_risk: 'person',
  fix_checks: 'person',
  inspect: 'person',
  resolve_conflict: 'person',
  resume_fleet: 'person',
  dispatch: 'kew',
  finalize_publication: 'kew',
  none: 'kew',
  observe_remote: 'kew',
  synchronize_source: 'kew',
  synchronize_tracker: 'kew',
  wait: 'kew'
};

/** The band's heading per action, in the words DEpic's rail heads with. */
export const NEEDS_YOU_TITLE: Record<string, string> = {
  address_review_feedback: 'Changes requested',
  await_human_decision: 'Merge decision',
  confirm_no_change: 'No-change decision',
  confirm_publication_risk: 'Publication hold',
  fix_checks: 'Checks failing',
  inspect: 'Needs a look',
  resolve_conflict: 'Merge conflict',
  resume_fleet: 'Fleet stopped'
};

/** The judgment state that is a human decision, whatever the action says. */
export const AWAITING_DECISION = 'awaiting_human_decision';

/** True when the projection's safest action is one only a person can take. */
export function actionNeedsPerson(action: string): boolean {
  return (
    Object.hasOwn(SAFEST_ACTION_ACTOR, action) && SAFEST_ACTION_ACTOR[action] === 'person'
  );
}

/** The magenta call to action, or null when there is nowhere to send anyone. */
export type BandCta = { label: string; href: string; external: boolean };

export type NeedsYouBand = {
  /** The action the band is about — the key its title came from. */
  key: string;
  /** "Merge decision". An unrecognised action is worded, never dropped. */
  title: string;
  /** The projection's own sentence. `''` when it wrote none. */
  prose: string;
  cta: BandCta | null;
};

/**
 * Where the band sends the reader.
 *
 * kew performs no merge (the surface-convergence rule), so the one call to
 * action is a link to the forge, and there is none at all without a pull
 * request — a band that linked somewhere else would be inventing a step. The
 * label is the epic rail's (`railCta`), so the two surfaces name the same
 * pull request the same way.
 */
export function bandCta(key: string, prUrl: string | null | undefined): BandCta | null {
  const href = safeHref(prUrl);
  if (href == null) return null;
  const ref = prRef(href);
  const named = ref.startsWith('#') ? `PR ${ref}` : 'the pull request';
  return {
    label: key === 'await_human_decision' ? `Open ${named} to merge` : `Open ${named}`,
    href,
    external: true
  };
}

/** An action with no title of its own, printed as words rather than dropped. */
function bandTitle(key: string): string {
  if (Object.hasOwn(NEEDS_YOU_TITLE, key)) return NEEDS_YOU_TITLE[key];
  const words = key.replace(/[_-]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : 'Needs you';
}

/**
 * The band, or `null` — which is the ordinary case and renders NOTHING.
 *
 * Two doors, in this order: the safest action when it is a person's, else the
 * judgment. An item awaiting a merge decision whose build has since gone red
 * reads as "Checks failing", because that is the move the projection ranked
 * first; the decision is still named by the header's state word.
 *
 * The prose is the safest action's reason, falling back to the exception's
 * detail. The two are different sentences and neither is written here; which
 * surface prints the exception's is `exceptionLine`'s answer in
 * `$lib/work/header`, which asks the block below it whether that sentence is
 * already there (#2280 D10) rather than assuming either way.
 */
export function needsYouBand(view: WorkView | null | undefined): NeedsYouBand | null {
  if (view == null) return null;
  const action = view.safest_action?.action ?? '';
  const awaiting = view.judgment?.state === AWAITING_DECISION;
  if (!actionNeedsPerson(action) && !awaiting) return null;
  const key = actionNeedsPerson(action) ? action : 'await_human_decision';
  return {
    key,
    title: bandTitle(key),
    prose: view.safest_action?.reason || view.exception?.detail || '',
    cta: bandCta(key, view.artifact?.pr_url)
  };
}

// --- What a failed read says -------------------------------------------------

/**
 * The sentence for a read that failed, the way `pipelineReadFailure` words one:
 * `get()` throws `Error("<path> -> <status>")`, and the status is worth keeping
 * because a 401 and a 500 are different things to go and look at.
 *
 * Neither of this node's two reads has a 404 that means "absent" — the evidence
 * chain answers for an item with no runs at all, and the description answers
 * `unavailable` with a reason rather than rejecting — so unlike the pipeline
 * every failure here is a gap to say out loud.
 */
export function readFailure(err: unknown, what: string): string {
  const message = err instanceof Error ? err.message : String(err);
  const status = /-> (\d{3})$/.exec(message)?.[1] ?? '';
  return status ? `${what} could not be read (${status})` : `${what} could not be read`;
}

// --- The evidence ledger -----------------------------------------------------

/** What an evidence payload with rows the reader can see nothing in means. */
export const LEDGER_ABSENT = 'no guarantees are recorded for this item yet';

/** The rail drawn between two rows: a hairline, or dashed into what is next. */
export type LedgerSegment = 'solid' | 'dashed';

export type LedgerRow = {
  key: string;
  /** The payload's audit event, which names the row for a reader (#2916). */
  event: string;
  /** The 1-based position, zero-padded the way both artboards draw it. */
  n: string;
  label: string;
  /** `event · summary`, the payload's own words — this file writes none. */
  summary: string;
  /** The recorded time in the viewer's zone, or the em dash. NEVER another
   * row's time. */
  time: string;
  /** The full UTC instant behind `time`, for its `title`; empty when there is
   * no recorded row to state one for. */
  timeTitle: string;
  /** False when `time` is the em dash: there is no recorded row to time. */
  timed: boolean;
  /** The attestation state's own word. */
  word: string;
  /** A `var(--token)`, from `statusShape` — no colour is named here. */
  wordColor: string;
  /** The state for `StatusMark`, after the `chain_verified` downgrade. */
  state: string;
  /** The ONE thing that earns a check: the host's fact on a verified chain. */
  check: boolean;
  /** The rail above this row; null on the first row, which has none. */
  above: LedgerSegment | null;
  /** The rail below this row; null on the last row. */
  below: LedgerSegment | null;
};

export type EvidenceLedgerModel = {
  rows: LedgerRow[];
  /** The numerator alone, so the head can colour it green on its own. */
  attested: number;
  total: number;
  /** "3 / 4 attested", or the em dash when there is nothing to count. */
  count: string;
  /** The server's own sentence about why the rows read as they do. `''` if none. */
  notice: string;
  /** Why there are no rows: a read that failed, or the absence itself. `''` while pending. */
  reason: string;
  /** True while the read is still out: no payload, and no failure either. */
  pending: boolean;
  /** True when there is nothing to draw but the reason. */
  empty: boolean;
};

/** The 1-based index, zero-padded. Shared with the DEpic evidence column. */
export function ledgerIndex(n: number): string {
  return String(n).padStart(2, '0');
}

/**
 * One row's attestation state, with the chain's verdict applied.
 *
 * The rule is that a check requires the host's own fact on a chain that
 * verifies, so an unverified chain downgrades every `attested` row to
 * `unverified` rather than trusting the row's own word. That is a guard, not a
 * disagreement — the server composes it the same way — and it means no surface
 * can draw a green check on a chain kew has not vouched for, however the
 * payload was produced.
 */
export function evidenceState(evidence: ItemEvidence, row: EvidenceRow): string {
  return !evidence.chain_verified && row.state === 'attested' ? 'unverified' : row.state;
}

/** The rail into a row: dashed while what comes next has not happened yet. */
export function segmentInto(state: string): LedgerSegment {
  return state === 'pending' ? 'dashed' : 'solid';
}

/**
 * The rows, each carrying the rail above and below it.
 *
 * The segment between two rows is decided by the LOWER row's state, so the
 * solid chain of what happened runs into a dashed line at exactly the point
 * the record stops — which is the artboard's row 03 dashing down into a
 * pending verdict, drawn once and read by both rows it touches.
 */
export function ledgerRows(evidence: ItemEvidence): LedgerRow[] {
  const rows = evidence.rows ?? [];
  const states = rows.map((row) => evidenceState(evidence, row));
  return rows.map((row, i) => {
    const state = states[i];
    const mark = statusShape(state);
    const summary = [row.event, row.summary].filter((part) => part !== '').join(' · ');
    return {
      key: `${row.n} ${row.event}`,
      event: row.event,
      n: ledgerIndex(row.n),
      label: row.label,
      summary,
      time: row.ts ? fmtTime(row.ts) : DASH,
      timeTitle: row.ts ? utcTitle(row.ts) : '',
      timed: row.ts !== '',
      word: mark.word,
      wordColor: QUIET_MEANINGS.has(mark.meaning) ? 'var(--text-3)' : mark.color,
      state,
      check: state === 'attested',
      above: i === 0 ? null : segmentInto(state),
      below: i === rows.length - 1 ? null : segmentInto(states[i + 1])
    };
  });
}

/**
 * The ledger. `notice` is a read that FAILED, which is a different thing from a
 * payload that answered with nothing: the endpoint never 404s, because the
 * guarantees exist before the work does and are then all pending.
 *
 * And both of those are a different thing again from a read still in flight —
 * no payload and no failure — which claims NOTHING. `LEDGER_ABSENT` is a
 * positive statement about the operator's own item, and the moment after a row
 * is selected is the moment this page knows least about it; the sibling
 * Pipeline section draws nothing at all in the same state.
 */
export function evidenceLedger(
  evidence: ItemEvidence | null | undefined,
  notice = ''
): EvidenceLedgerModel {
  const rows = evidence ? ledgerRows(evidence) : [];
  // The numerator is the server's count, zeroed by its own chain verdict — the
  // head must not read higher than the rows under it.
  const attested = evidence && evidence.chain_verified ? evidence.attested_count : 0;
  const total = evidence?.total ?? rows.length;
  const empty = rows.length === 0;
  const pending = evidence == null && notice === '';
  return {
    rows,
    attested,
    total,
    count: empty ? DASH : `${attested} / ${total} attested`,
    notice: empty ? '' : (evidence?.reason ?? ''),
    reason: empty && !pending ? notice || LEDGER_ABSENT : '',
    pending,
    empty
  };
}

// --- The description panel ---------------------------------------------------

/**
 * The fallback link's words. The one link, whatever the tracker is called —
 * and it is offered only where the payload named a URL to put behind it.
 */
export const DESCRIPTION_LINK = 'Open on GitHub';

/**
 * The plain note for a read that gave us nothing to show and no reason for it:
 * a read this page has not taken, or a payload whose `reason` is empty. It
 * stands alone where there is no URL to put a link beside it.
 */
export const DESCRIPTION_UNSYNCED = 'the description is not synced into kew';

/** An `available` read of an item whose body really is empty. */
export const DESCRIPTION_EMPTY = 'this item has no description on its tracker';

/** A section the body named for itself, drawn with its label inline. */
export type DescriptionSection = { key: string; label: string; text: string };

export type DescriptionPanel = {
  /**
   * The body as the blocks `./markdown` reads out of it, in the order they were
   * written. Empty draws nothing. A line outside the subset is a paragraph of
   * literal text, so this is never shorter than what the person wrote.
   */
  blocks: Block[];
  sections: DescriptionSection[];
  /** "older reading; …" when the body is a stale one. `''` otherwise. */
  stale: string;
  /** Why there is no body, in the server's words where it wrote any. `''` while pending. */
  notice: string;
  /**
   * The fallback link, or null when the payload named no URL to open — which
   * is every `unavailable` answer, whose URL would have come from the reading
   * it never took. Nothing here derives one from the item's id or repo.
   */
  link: { label: string; href: string } | null;
  /** True while the read is still out: no payload, and no failure either. */
  pending: boolean;
  /** True when the panel has nothing but its notice and maybe a link. */
  empty: boolean;
  /**
   * What the closed panel's summary names (#2786 D16): the body's size in
   * words, with the stale note beside it; the notice where there is no body;
   * `''` while the read is out.
   */
  contents: string;
};

/** Words in what a person wrote: runs of non-space, markup counted as written. */
function wordCount(...texts: string[]): number {
  return texts.reduce((n, text) => n + (text.match(/\S+/g)?.length ?? 0), 0);
}

/** The availabilities a body may be drawn under. Anything else is a failure. */
const READABLE: ReadonlySet<string> = new Set(['available', 'stale']);

/**
 * Whether the panel has no reading it can stand behind: no payload at all, or
 * one whose availability is not a body it will draw. The panel asks this of
 * itself, and `rereadsDescription` asks it first — though not last, because an
 * answer with no body is not by itself an answer worth taking again.
 */
export function descriptionUnread(description: ItemDescription | null | undefined): boolean {
  return description == null || !READABLE.has(description.availability);
}

/**
 * The words FUI-31 ends a refusal it never ATTEMPTED with: the tail of
 * `BUSY_REASON`, `SAME_ITEM_REASON` and `BUDGET_REASON`, and of no other reason
 * the read can give (`kew/service/description.py`, held to those three by
 * `kew/tests/test_description_retry_parity.py`).
 *
 * This mark is the whole of what separates a refusal worth taking again from a
 * reading that failed, and the difference is what the retry SPENDS. Those three
 * gates turn a read away before the request leaves the box: the tracker never
 * hears it, the read budget is never charged for it, and each gate clears on its
 * own within a minute — so taking it again is free, and is shortly answered.
 *
 * A read that REACHED the tracker and failed — `gh` could not read the issue,
 * the Linear read failed, the response would not parse — already spent one of
 * the `MAX_READS_PER_WINDOW` reads a minute the whole process shares with the
 * dispatch driver, and it is a fact about the tracker rather than about this
 * page's timing. Re-reading it on every tick would multiply ONE item's spend of
 * that shared ration by `DESCRIPTION_RETRIES + 1`, to be told the same thing
 * each time, and ration away the cold reads of every other item — and every
 * other operator on this process — until the window rolled over. So a failed
 * reading is drawn once, with its reason and whatever link it named, and left
 * alone.
 *
 * The refusals this mark also excludes are the settled ones (no `project.repo`,
 * an id that is not an issue number, no `LINEAR_API_KEY`, a tracker with no read
 * at all): free like the gates, but naming nothing a tick can change.
 */
export const DESCRIPTION_UNATTEMPTED = 'this one was not attempted';

/**
 * How many times the shell's tick re-reads a description that landed no body.
 *
 * FUI-31 refuses a read outright — not attempted, so it is not even a fact
 * about the tracker — while another read of the same item is out, while
 * `MAX_CONCURRENT_READS` reads are in flight process-wide, or while the
 * minute's read budget is spent (`kew/service/description.py`). Each of those
 * clears on its own. But the description is deliberately NOT on the poll (an
 * issue body somebody edits is not what that clock watches), so without a retry
 * a refusal that lasted a second would sit under the item's header for as long
 * as the row stays selected — a reason with no body, no link and nothing to
 * press, because an answer that took no reading names no URL either.
 *
 * Ten ticks of the 7s pulse outlasts the longest of those windows (the 60s read
 * budget), and then it stops: a gate that is somehow still shut after a whole
 * budget window is not a thing this page keeps asking about.
 */
export const DESCRIPTION_RETRIES = 10;

/**
 * Whether the tick after `tries` re-reads should take the read again.
 *
 * Only what costs the tracker nothing: an answer that arrived is re-read solely
 * for the three unattempted refusals `DESCRIPTION_UNATTEMPTED` marks, never for
 * a reading that failed at the tracker or a refusal that will not change.
 *
 * No payload at all is the one case decided without a reason, because there is
 * none to read: either the read is still out — which `$lib/work/reads` answers
 * separately, so a tick never duplicates a read of the item being read — or the
 * console's own read of the endpoint threw. The latter is a fault on THIS side of the guard (a refused
 * origin, a lost session, a dropped connection), and a retry of it costs the
 * tracker at most the one read the winning request already cached.
 */
export function rereadsDescription(
  description: ItemDescription | null | undefined,
  tries: number
): boolean {
  if (tries >= DESCRIPTION_RETRIES) return false;
  if (description == null) return true;
  return descriptionUnread(description) && description.reason.includes(DESCRIPTION_UNATTEMPTED);
}

/**
 * The panel.
 *
 * `null` with a `notice` is a read that failed, worded by the page, and with no
 * link at all — a payload is the only thing that names a URL. `null` with no
 * notice is a read still IN FLIGHT, and it says nothing: `DESCRIPTION_UNSYNCED`
 * is a positive claim about the operator's own item, and the moment after a row
 * is selected is the moment this page knows least about it. It is kept for the
 * payload that lands with no reason of its own, which is the one answer that
 * really did arrive saying nothing.
 *
 * A reading that arrived decides for itself — an unknown `availability` is
 * treated as unavailable, because the one thing this panel must never do is
 * print a body it cannot stand behind. The link follows the URL rather than the
 * availability: a failed answer that named one is offered it, and the many that
 * name none are the reason by itself.
 */
export function descriptionPanel(
  description: ItemDescription | null | undefined,
  notice = ''
): DescriptionPanel {
  // A served URL is a link only through safeHref (C10); a refused one is no link.
  const url = safeHref(description?.url);
  const link = url == null ? null : { label: DESCRIPTION_LINK, href: url };
  // The `== null` half is `descriptionUnread`'s own first clause, written out
  // again so the reading below is typed as one that arrived.
  if (description == null || descriptionUnread(description)) {
    const pending = description == null && notice === '';
    return {
      blocks: [],
      sections: [],
      stale: '',
      notice: pending ? '' : description?.reason || notice || DESCRIPTION_UNSYNCED,
      link,
      pending,
      empty: true,
      contents: pending ? '' : description?.reason || notice || DESCRIPTION_UNSYNCED
    };
  }
  const blocks = parseMarkdown(description.description);
  const sections: DescriptionSection[] = [];
  if (description.expected !== '')
    sections.push({ key: 'expected', label: 'Expected', text: description.expected });
  if (description.actual !== '')
    sections.push({ key: 'actual', label: 'Actual', text: description.actual });
  const empty = blocks.length === 0 && sections.length === 0;
  const words = wordCount(description.description, description.expected, description.actual);
  const stale = description.availability === 'stale';
  return {
    blocks,
    sections,
    stale: stale ? `older reading${description.reason ? `; ${description.reason}` : ''}` : '',
    // An empty body IS the answer under a read that worked, so it is said in
    // plain words — and the link stays out of the way of a real description.
    notice: empty ? DESCRIPTION_EMPTY : '',
    link: empty ? link : null,
    pending: false,
    empty,
    contents: empty
      ? DESCRIPTION_EMPTY
      : `${words} word${words === 1 ? '' : 's'}${stale ? ' · older reading' : ''}`
  };
}
