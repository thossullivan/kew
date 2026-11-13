// The ONE run-status vocabulary (C3/#2296).
//
// Three surfaces each kept their own copy of "which statuses are finished" —
// `kpi.ts` TERMINAL_STATUSES, `runs-filter.ts` ACTIVE/FAILED, and the runs
// page's own TERMINAL_FAILURES — and all three were written against a shorter
// version of the backend enum. `refused`, `budget_stop` and
// `awaiting_publication` were in none of them, so every one of those runs was
// counted as still in flight and the "live" readout was inflated by runs that
// had stopped days ago.
//
// The backend enum is `RunStatus` (kew/src/kew/run_store.py):
//   pending | awaiting_publication | success | failure | refused |
//   budget_exceeded | budget_stopped | budget_stop | killed
//
// `pending` is the only status the host writes while a run is executing.

/**
 * Every status the backend can store on a run — the run_store `RunStatus`
 * enum, and nothing else.
 *
 * This is the *vocabulary*, not a reading of it: `LIVE_STATUSES` below adds
 * `kpi.ts`'s display-only refinements of `pending` (`queued`, `running`), which
 * no payload ever carries. The two are kept apart because a fixture or a test
 * double written against the display words serves a run the backend cannot
 * produce — `frontend/fixtures/` did exactly that (#2377): a `failed` run that
 * was terminal under no vocabulary, so the Runs page read 100% success with a
 * failure in the list. `fixtures.test.ts` holds every committed fixture run
 * status to this set.
 */
export const BACKEND_RUN_STATUSES: ReadonlySet<string> = new Set([
  'pending',
  'awaiting_publication',
  'success',
  'failure',
  'refused',
  'budget_exceeded',
  'budget_stopped',
  'budget_stop',
  'killed'
]);

/** Statuses that mean a run is executing right now. */
export const LIVE_STATUSES: ReadonlySet<string> = new Set([
  'pending',
  // Display-only refinements of `pending` (see kpi.ts `displayStatus`); the
  // API never sends them, but a caller that has already refined its rows must
  // get the same answer as one that has not.
  'queued',
  'running'
]);

/**
 * Statuses that carry a judged outcome — the success-rate denominator.
 *
 * `awaiting_publication` is deliberately absent, as is `pending`: the work has
 * stopped but nothing has judged it, and counting it as a non-success would
 * report a success rate below the one actually earned.
 */
export const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  'success',
  'failure',
  'refused',
  'budget_exceeded',
  'budget_stopped',
  'budget_stop',
  'killed'
]);

/** Terminal statuses that are not a success — what surfaces an error line. */
export const FAILURE_STATUSES: ReadonlySet<string> = new Set(
  [...TERMINAL_STATUSES].filter((status) => status !== 'success')
);

/**
 * True only for a status that means "executing now".
 *
 * An unrecognised status is settled, not live: a host newer than this bundle
 * can add a status, and the safe reading of one we have never heard of is that
 * the run is no longer running — a stale row must not light the console up as
 * live work forever.
 */
export function isLive(status: string): boolean {
  return LIVE_STATUSES.has(status);
}

/** True when a run has stopped, whether or not anything has judged it. */
export function isSettled(status: string): boolean {
  return !isLive(status);
}
