// Honest cockpit KPIs (CS-4). While a stranger's only run is in flight, the
// naive `ok / runs.length` reads "success rate 0%" and the badge says
// `pending` even though the agent is 16 steps deep — first impression:
// "failing and stuck". The fix: count only *terminal* runs in the success-rate
// denominator (render `—` when there are none), and derive a display-only
// `running` state for pending runs that show activity.
//
// The status vocabulary itself lives in `run-status.ts` — one shared set, so
// this module, the runs filter and the runs page can no longer disagree about
// which statuses are finished (C3/#2296). There is no server `running` state;
// `running`/`queued` below are display-only refinements of `pending`.

import { TERMINAL_STATUSES, isLive } from './run-status';

export { TERMINAL_STATUSES };

/** Minimal run shape for KPI math — only the status matters. */
export interface KpiRun {
  status: string;
}

export interface RunKpis {
  /** All runs in the window. */
  total: number;
  /** Runs that reached a terminal outcome (the success-rate denominator). */
  terminal: number;
  /** Terminal runs that succeeded (the success-rate numerator). */
  ok: number;
  /** Percent success over terminal runs, or `null` when nothing is terminal. */
  successRate: number | null;
  /**
   * Runs executing right now.
   *
   * Counted from the live statuses, NOT as `total - terminal`: a run that has
   * stopped without a judgment (`awaiting_publication`), or that carries a
   * status this bundle has never heard of, is settled — and the subtraction
   * reported both as live work (C3/#2296).
   */
  live: number;
}

/**
 * Compute cockpit KPIs over a run window. The success-rate denominator is
 * *terminal* runs only, so in-flight runs never drag the rate down; when no run
 * has finished yet, `successRate` is `null` (render as `—`, never `0%`).
 */
export function runKpis(runs: readonly KpiRun[]): RunKpis {
  const total = runs.length;
  let terminal = 0;
  let ok = 0;
  let live = 0;
  for (const r of runs) {
    if (isLive(r.status)) live += 1;
    if (TERMINAL_STATUSES.has(r.status)) {
      terminal += 1;
      if (r.status === 'success') ok += 1;
    }
  }
  const successRate = terminal ? Math.round((100 * ok) / terminal) : null;
  return { total, terminal, ok, successRate, live };
}

/** Minimal run shape for display-state derivation. */
export interface DisplayRun {
  status: string;
  /** ISO start timestamp, if the run has begun. */
  startedAt?: string | null;
  /** Flight-recorder step count, if known. */
  steps?: number | null;
}

/**
 * Display-only status. A `pending` run with evidence of activity (steps taken
 * or a start timestamp) renders as `running`; an untouched `pending` run
 * renders as `queued`. Every other status passes through unchanged. This does
 * NOT change the backend status enum.
 */
export function displayStatus(run: DisplayRun): string {
  if (run.status !== 'pending') return run.status;
  const active = (run.steps ?? 0) > 0 || Boolean(run.startedAt);
  return active ? 'running' : 'queued';
}
