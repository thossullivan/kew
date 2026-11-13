/**
 * A governed-review verdict as the run page prints it (design review c9, D11).
 *
 * The page printed the raw code — `NEEDS_CHANGES`, `READY` — as a coloured
 * headline with no shape, under a header whose status word said "success" for
 * a review run that refused the change. DESIGN.md: status is shape plus colour
 * from `STATUS_MARKS`, and a raw code is never the sentence, only secondary
 * detail. So a verdict is a status word the mark table knows (`kind`) and
 * plain words (`words`); the page prints the code after them, quiet and mono.
 *
 * The verdict vocabulary is the governed review's: READY and
 * READY_WITH_RESIDUALS end a round in the change's favour (`verdictTone` in
 * `$lib/epic/timeline`), NEEDS_CHANGES and NEEDS_FULL against it, and the four
 * the driver attests when a person or a policy short-circuits the stage
 * (`PASS_VERDICT` in `$lib/monitor/stream` names them). A code this table does
 * not know keeps its tone — not-ready unless `verdictTone` reads it ready —
 * and is worded from its own letters, never dropped.
 */
import type { Run } from '$lib/api';
import { verdictTone } from '$lib/epic/timeline';
import { VERDICT_SCOPE_RUN } from '$lib/runs/events';

export type VerdictReading = {
  /** A status word `StatusMark` draws: its shape and colour carry the state. */
  kind: string;
  /** What the verdict says, in plain words: "Changes requested". */
  words: string;
};

/**
 * The words per code, and the mark of a verdict that went against the change
 * where it is not the plain not-ready one. Whether a verdict is ready at all is
 * never decided here: that is `verdictTone`, the console's one verdict mapping,
 * so READY_WITH_RESIDUALS cannot read as a setback on this page while the
 * epic timeline calls it ready.
 */
const READINGS: Readonly<Record<string, { words: string; against?: string }>> = {
  READY: { words: 'Ready' },
  READY_WITH_RESIDUALS: { words: 'Ready, with residuals' },
  NEEDS_CHANGES: { words: 'Changes requested' },
  NEEDS_FULL: { words: 'Full review requested' },
  DEGRADED: { words: 'Review degraded', against: 'unverified' },
  UNPARSEABLE: { words: 'Verdict unreadable', against: 'unverified' },
  MERGED_UNREVIEWED: { words: 'Merged without review', against: 'missing' },
  PRE_POLICY: { words: 'Before the review policy', against: 'not_required' }
};

/** The mark and the words for one verdict code. */
export function verdictReading(code: string): VerdictReading {
  const key = code.trim().toUpperCase();
  const known = Object.hasOwn(READINGS, key) ? READINGS[key] : null;
  const spaced = key.replace(/_+/g, ' ').toLowerCase();
  return {
    kind: verdictTone(key) === 'success' ? 'done' : (known?.against ?? 'not_ready'),
    words: known?.words ?? (spaced ? spaced[0].toUpperCase() + spaced.slice(1) : 'Verdict recorded')
  };
}

/**
 * What a review run was, for the page header: "Review · round 1" when the
 * verdict on the page is the run's OWN (scope `run`), "Review" when the page
 * can only show the work item's standing verdict, and '' for any other kind of
 * run — whose header keeps its attempt and status as before.
 */
export function reviewHeading(run: Run | null | undefined): string {
  if (run?.run_kind !== 'adversarial_review') return '';
  const own = run.verdict?.scope === VERDICT_SCOPE_RUN ? run.verdict : null;
  return own ? `Review · round ${own.round}` : 'Review';
}
