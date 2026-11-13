import type { Run } from './api';
import { FAILURE_STATUSES, LIVE_STATUSES } from './run-status';

export type RunStatus = 'all' | 'active' | 'merged' | 'failed';
export const STATUS_LABELS: RunStatus[] = ['all', 'active', 'merged', 'failed'];

// The status vocabulary comes from `run-status.ts` — the one shared set
// (C3/#2296). This file used to keep its own, written against a shorter
// version of the backend enum, so a `refused` or `budget_stop` run matched
// neither ACTIVE nor FAILED and vanished from every filter but "all".
const ACTIVE = LIVE_STATUSES;
const MERGED = new Set(['success']);
const FAILED = FAILURE_STATUSES;

type Tone = 'accent' | 'success' | 'info' | 'danger' | 'warn' | 'neutral';

// Badge tone per status. Covers every real RunStatus plus the display-only
// `running`/`queued` states. Dead vocabulary (`merged`, `review`, `failed`)
// that never appears in live data has been dropped.
//
// Amber is money and held, so a budget stop and a run waiting on publication
// both take it; red is a failed outcome or a missing guarantee, which is what
// a refusal is. An unknown status falls through `toneFor` to neutral.
export const STATE_TONE: Record<string, Tone> = {
  pending: 'neutral',
  queued: 'neutral',
  running: 'accent',
  awaiting_publication: 'warn',
  success: 'success',
  failure: 'danger',
  refused: 'danger',
  killed: 'danger',
  budget_exceeded: 'warn',
  budget_stopped: 'warn',
  budget_stop: 'warn'
};

export function toneFor(status: string): Tone {
  return STATE_TONE[status] ?? 'neutral';
}

export function filterRuns(runs: Run[], query: string, status: RunStatus): Run[] {
  const q = query.trim().toLowerCase();
  return runs.filter((r) => {
    const matchesQ = !q || r.title.toLowerCase().includes(q) || String(r.issue_num).includes(q);
    const matchesS =
      status === 'all' ||
      (status === 'active' && ACTIVE.has(r.status)) ||
      (status === 'merged' && MERGED.has(r.status)) ||
      (status === 'failed' && FAILED.has(r.status));
    return matchesQ && matchesS;
  });
}
