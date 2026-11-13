/**
 * #2515 — the first-run card on `/work`: what a new user fixes, and the one
 * command they run next.
 *
 * It replaced the legacy console's welcome card, whose checklist was typed by hand
 * and drifted from the CLI (it taught `kew setup` as the first step). Every
 * fix here is the readiness check's OWN `fix` field, served by
 * `/api/readiness` out of `kew doctor`'s checks, so this file computes no
 * command for a check and cannot teach one the doctor would not.
 *
 * A model, like `$lib/needs-you/block`: every word, which checks are listed and
 * which ONE command carries the magenta Copy (and the `c` key) is decided here;
 * `FirstRunCard.svelte` only draws it.
 */
import type { Dashboard, Readiness, ReadinessCheck, Run, WorkProjection } from '$lib/api';
import type { WorkRow } from '$lib/work/list';

/**
 * The step after the checks pass: set the repository up for kew (its labels,
 * security hook and log directories). A registered command — `first-run.test.ts`
 * holds every `kew <command>` this card teaches to a `docs/cli/` page.
 */
export const SETUP_COMMAND = 'kew setup';

/**
 * The step before everything else when no `kew.toml` is in force (#2652):
 * `kew serve` runs on defaults without one, so the console can be open first.
 * `kew init` writes the file and runs setup itself, so it replaces `kew setup`.
 */
export const INIT_COMMAND = 'kew init';

/** The doctor check that says whether a config file is in force. */
export const PROJECT_CONFIG_CHECK = 'project-config';


/** One readiness check that needs attention, as the card lists it. */
export type FirstRunCheck = {
  /** The check's stable id (`CHECK_IDS`), printed in mono. */
  id: string;
  status: 'fail' | 'warn';
  summary: string;
  /** The check's one runnable fix, or '' when it has only prose. */
  command: string;
  /** The check's prose hint; the card prints it only where there is no command. */
  hint: string;
};

export type FirstRunCardModel = {
  /** Failing checks first, then warnings, each in the server's order. */
  checks: FirstRunCheck[];
  /**
   * With no config in force (#2776), every other failing or warning check: it
   * read built-in defaults, so its fix is premature until `kew init` writes the
   * config. The card keeps these behind one quiet disclosure, after the next
   * command, and none of them is `primary`. Empty whenever a config is in force.
   */
  deferred: FirstRunCheck[];
  /** The command after the checks: `kew setup`, or `kew init` with no config. */
  next: string;
  /**
   * The first dispatch, the step after `next`: `kew dispatch <n>` for the
   * first issue the dashboard serves as ready or as a queue candidate, the form
   * `dispatchCommand` prints. '' when it serves none, and the card says the
   * step in prose — never a template a terminal would refuse (#3467 D28).
   */
  then: string;
  /**
   * The ONE command that is the next move: `kew init` when no config is in
   * force (never a `deferred` check's fix), else the first listed check's
   * fix, or `next` when no check needs one. It alone gets the magenta Copy and `c` (DESIGN.md, `button-primary`).
   */
  primary: string;
  /**
   * Where the readiness reading stands. Only `read` may say the checks pass:
   * `pending` (not landed yet) and `failed` list no check because there is no
   * reading, not because nothing is wrong.
   */
  reading: 'pending' | 'failed' | 'read';
};

export type FirstRunInput = {
  rows: WorkRow[];
  projection: WorkProjection | null;
  dashboard: Dashboard | null;
  runs: Run[] | null;
};

/**
 * True when there is no work AND no run history. Every read must have answered:
 * a failed or pending read is not evidence that nothing exists, and showing a
 * new user's welcome over a factory whose reads are down would be a lie.
 */
export function isFirstRun(input: FirstRunInput): boolean {
  const { rows, projection, dashboard, runs } = input;
  if (projection === null || dashboard === null || runs === null) return false;
  return rows.length === 0 && runs.length === 0;
}

function asCheck(check: ReadinessCheck): FirstRunCheck {
  return {
    id: check.id,
    status: check.status === 'fail' ? 'fail' : 'warn',
    summary: check.summary,
    command: (check.fix ?? '').trim(),
    hint: (check.hint ?? '').trim()
  };
}

/**
 * The first dispatch for the issue the dashboard serves first: the ready queue,
 * then the open issues kew has not touched. A tracker key or the `0` sentinel
 * has no `kew dispatch` form, so it is skipped rather than printed.
 */
function firstDispatch(dashboard: Dashboard | null | undefined): string {
  const glance = dashboard?.glance;
  const issues = [...(glance?.ready_queue ?? []), ...(glance?.queue_candidates ?? [])];
  const first = issues.find((issue) => Number.isInteger(issue.issue_num) && issue.issue_num > 0);
  return first === undefined ? '' : `kew dispatch ${first.issue_num}`;
}

/**
 * The card for one readiness reading. `undefined` is a read that has not
 * landed and `null` one that failed: either way the card says so and still
 * gives the next command, rather than claiming every check passed.
 */
export function firstRunCard(
  readiness: Readiness | null | undefined,
  dashboard: Dashboard | null = null
): FirstRunCardModel {
  const then = firstDispatch(dashboard);
  const issues = (readiness?.checks ?? []).filter(
    (c) => c.status === 'fail' || c.status === 'warn'
  );
  const checks = [
    ...issues.filter((c) => c.status === 'fail'),
    ...issues.filter((c) => c.status === 'warn')
  ].map(asCheck);
  const reading = readiness === undefined ? 'pending' : readiness === null ? 'failed' : 'read';
  // No config in force: every other check read built-in defaults, so writing
  // the config is the move. Its command lives in `next` alone, so the card
  // never shows it twice (and never as two magenta Copies); the other checks
  // wait behind the disclosure, since `kew init` may change their reading.
  const config = checks.find((c) => c.id === PROJECT_CONFIG_CHECK && c.status === 'fail');
  if (config) {
    return {
      checks: [{ ...config, command: '' }],
      deferred: checks.filter((c) => c !== config),
      next: INIT_COMMAND,
      then,
      primary: INIT_COMMAND,
      reading
    };
  }
  const fix = checks.find((c) => c.command !== '');
  return {
    checks,
    deferred: [],
    next: SETUP_COMMAND,
    then,
    primary: fix ? fix.command : SETUP_COMMAND,
    reading
  };
}
