/**
 * #3140 — the Work screen's merge verify reading, as a pure model.
 *
 * The pipeline payload carries the latest `verify_completed` linked to one of
 * the item's pull-request heads (`service/merge_verify_reading.py`). This
 * module words it as one stage reading: "Merge verify failed at 3670e43f: 2 of
 * main's tests fail on this PR." with the failing test ids beneath, or "Merge
 * verify passed at b71c04e2." A verify of a head the pull request has moved
 * past says so, and names that head.
 *
 * The colour rule is DESIGN.md's: magenta only when a person is the next move.
 * A failed verify is the machine's refusal, so it reads `failed` (red) unless a
 * stage of the same payload already says `needs_you` — then it is the reason
 * that person is looking, and it carries their colour.
 */
import type { ItemPipeline, MergeVerify } from '$lib/api';

/** One failing test, and whether the PR body's Behavior changes excused it. */
export type MergeVerifyTest = { id: string; excused: boolean };

export type MergeVerifyReading = {
  /** A status word for `StatusMark`: `done`, `failed`, `needs_you`, `unknown`, `pending`. */
  status: string;
  sentence: string;
  /** The head it joined on, short form; `''` when the sentence names none. */
  head: string;
  /** Main's tests that failed against the PR, undeclared first. */
  tests: MergeVerifyTest[];
  /** How many failing tests the server left off `tests` (it sends at most 50). */
  more: number;
  /** The verify's error category exactly as recorded, when it recorded one. */
  category: string;
};

/** The `unavailable` entry the server names when the verify read failed. */
export const UNREADABLE_MERGE_VERIFY = 'pipeline:merge_verify';

/**
 * A published PR no verify is linked to. Not "none is recorded": a verify
 * recorded before kew wrote the verified commit's parents links only when it
 * verified the bare head, so an unlinked one may still exist.
 */
export const NO_MERGE_VERIFY = "No merge verify is linked to this pull request's heads.";

/** The verify record itself did not answer. */
export const MERGE_VERIFY_UNREADABLE = 'The merge verify record could not be read.';

const PUBLISHED_STAGE_STATES = new Set(['done', 'needs_you', 'failed', 'unknown']);

function personIsNext(pipeline: ItemPipeline): boolean {
  return pipeline.lanes.some((lane) => lane.stages.some((stage) => stage.state === 'needs_you'));
}

function published(pipeline: ItemPipeline): boolean {
  return pipeline.lanes.some((lane) =>
    lane.stages.some(
      (stage) =>
        stage.stage === 'publish' &&
        // A PR nobody scanned is `pending` (#3466: not the success green), and
        // `passed` is what says it is up — never the worded label.
        (PUBLISHED_STAGE_STATES.has(stage.state) || stage.passed === true)
    )
  );
}

function failingTests(verify: MergeVerify): MergeVerifyTest[] {
  const heldOut = verify.held_out;
  if (heldOut === null) return [];
  const declared = new Set(heldOut.declared);
  const failed = heldOut.failed.map((id) => ({
    id,
    excused: declared.has(id)
  }));
  return [...failed.filter((t) => !t.excused), ...failed.filter((t) => t.excused)];
}

function tests(count: number): string {
  return count === 1 ? "1 of main's tests fails" : `${count} of main's tests fail`;
}

/** Where the verify ran: "at <head>", or "at an older head, <head>". */
function where(head: string, current: boolean): string {
  if (head === '') return '';
  return current ? ` at ${head}` : ` at an older head, ${head}`;
}

/**
 * Why a failed verify failed, read off `held_out.outcome` (the held-out phase
 * runs main's tests on the PR): `passed` is the PR's own checks failing while
 * main's tests pass (any failure among them excused by a declared behavior
 * change); `skipped` ran none of main's tests; `failed` with nothing
 * undeclared is a phase that broke before it reached a verdict. `not_run` is
 * worded on its own (`NOT_RUN`): it only says the phase reported nothing.
 */
const FAILED_BECAUSE: Record<string, string> = {
  passed: "the PR's own checks failed; main's tests pass on it",
  skipped: "the PR's own checks failed, and main's tests were skipped",
  failed: "main's tests did not run to a verdict on it"
};

const EXCUSED_ONLY =
  "the PR's own checks failed; main's tests fail only where a behavior change is declared";

/**
 * `not_run`: the held-out phase reported nothing, which follows a failed
 * preflight, image build or setup as readily as the PR's own gate — so the
 * sentence says when it failed, and blames nothing.
 */
const NOT_RUN = " before main's tests ran";

/** Main's tests nobody declared, counting the ones the server left off. */
function undeclaredCount(verify: MergeVerify, failing: MergeVerifyTest[]): number {
  const heldOut = verify.held_out;
  if (heldOut === null) return 0;
  const listed = Math.max(heldOut.undeclared.length, failing.filter((t) => !t.excused).length);
  return listed + heldOut.undeclared_more;
}

function failedSentence(verify: MergeVerify, at: string, failing: MergeVerifyTest[]): string {
  const undeclared = undeclaredCount(verify, failing);
  const heldOut = verify.held_out?.outcome ?? '';
  const on = verify.is_current_head ? 'on this PR' : 'on that head';
  if (undeclared > 0) return `Merge verify failed${at}: ${tests(undeclared)} ${on}.`;
  if (heldOut === 'not_run') return `Merge verify failed${at}${NOT_RUN}.`;
  const because =
    heldOut === 'passed' && failing.length > 0 ? EXCUSED_ONLY : FAILED_BECAUSE[heldOut];
  return because ? `Merge verify failed${at}: ${because}.` : `Merge verify failed${at}.`;
}

function passedSentence(verify: MergeVerify, at: string): string {
  const skipped = verify.held_out?.outcome === 'skipped' ? ", with main's tests skipped" : '';
  const moved = verify.is_current_head ? '' : '; the pull request has moved since';
  return `Merge verify passed${at}${skipped}${moved}.`;
}

function sentence(verify: MergeVerify, head: string, failing: MergeVerifyTest[]): string {
  const at = where(head, verify.is_current_head);
  if (verify.outcome === 'passed') return passedSentence(verify, at);
  if (verify.outcome === 'failed') return failedSentence(verify, at, failing);
  return `Merge verify did not finish${at}.`;
}

function statusOf(verify: MergeVerify, pipeline: ItemPipeline): string {
  if (verify.outcome === 'passed') return 'done';
  if (verify.outcome !== 'failed') return 'unknown';
  return personIsNext(pipeline) ? 'needs_you' : 'failed';
}

function plain(status: string, text: string): MergeVerifyReading {
  return { status, sentence: text, head: '', tests: [], more: 0, category: '' };
}

/**
 * A test id cut where a line may break: after every `::` and `/`, so a long
 * node id wraps at its own seams rather than mid-word. Joined, the pieces are
 * the id unchanged.
 */
export function idBreaks(id: string): string[] {
  return id.split(/(?<=::|\/)/).filter((piece) => piece !== '');
}

/**
 * The reading for one payload, or `null` when there is nothing to say: no
 * payload, a server that predates the field, or an unpublished item with no
 * verify behind it (an idle row has no pull request to verify).
 */
export function mergeVerifyReading(
  pipeline: ItemPipeline | null | undefined
): MergeVerifyReading | null {
  if (!pipeline || pipeline.merge_verify === undefined) return null;
  const verify = pipeline.merge_verify;
  if (verify === null) {
    if (!published(pipeline)) return null;
    if (pipeline.unavailable.includes(UNREADABLE_MERGE_VERIFY)) {
      return plain('unknown', MERGE_VERIFY_UNREADABLE);
    }
    return plain('pending', NO_MERGE_VERIFY);
  }
  const head = verify.head.slice(0, 8);
  const failing = failingTests(verify);
  return {
    status: statusOf(verify, pipeline),
    sentence: sentence(verify, head, failing),
    head,
    tests: failing,
    more: verify.held_out?.failed_more ?? 0,
    category: verify.error_category ?? ''
  };
}
