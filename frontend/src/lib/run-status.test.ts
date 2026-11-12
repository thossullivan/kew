import { describe, it, expect } from 'vitest';
import {
  BACKEND_RUN_STATUSES,
  FAILURE_STATUSES,
  LIVE_STATUSES,
  TERMINAL_STATUSES,
  isLive,
  isSettled
} from './run-status';
import { runKpis } from './kpi';
import { filterRuns } from './runs-filter';
import type { Run } from './api';

// Every status the backend RunStatus enum can carry
// (kew/src/kew/run_store.py), transcribed here a second time on purpose: the
// assertion below pins `BACKEND_RUN_STATUSES` to this list, so adding a status
// to the module without adding it here fails rather than passing vacuously.
const BACKEND_STATUSES = [
  'pending',
  'awaiting_publication',
  'success',
  'failure',
  'refused',
  'budget_exceeded',
  'budget_stopped',
  'budget_stop',
  'killed'
];

describe('run status vocabulary', () => {
  it('exports the backend enum as the set a payload status must be in', () => {
    expect([...BACKEND_RUN_STATUSES].sort()).toEqual([...BACKEND_STATUSES].sort());
    // The display-only refinements are NOT backend statuses: a payload that
    // carries one is a payload the backend could not have produced (#2377).
    expect(BACKEND_RUN_STATUSES.has('running')).toBe(false);
    expect(BACKEND_RUN_STATUSES.has('queued')).toBe(false);
    expect(BACKEND_RUN_STATUSES.has('failed')).toBe(false);
  });

  it('treats only pending (and its display refinements) as live', () => {
    expect([...LIVE_STATUSES].sort()).toEqual(['pending', 'queued', 'running']);
    for (const status of BACKEND_STATUSES) {
      expect(isLive(status)).toBe(status === 'pending');
    }
  });

  it('treats an unknown status as settled, not live', () => {
    expect(isLive('a_status_from_a_newer_host')).toBe(false);
    expect(isSettled('a_status_from_a_newer_host')).toBe(true);
    expect(TERMINAL_STATUSES.has('a_status_from_a_newer_host')).toBe(false);
  });

  it('keeps refusals and budget stops out of the failure set no longer', () => {
    expect(FAILURE_STATUSES.has('refused')).toBe(true);
    expect(FAILURE_STATUSES.has('budget_stop')).toBe(true);
    expect(FAILURE_STATUSES.has('success')).toBe(false);
  });

  it('leaves awaiting_publication unjudged: neither live nor terminal', () => {
    expect(isLive('awaiting_publication')).toBe(false);
    expect(TERMINAL_STATUSES.has('awaiting_publication')).toBe(false);
  });
});

describe('the runs page live KPI', () => {
  it('equals the count of pending runs', () => {
    const statuses = [...BACKEND_STATUSES, 'pending', 'pending'];
    const runs = statuses.map((status) => ({ status }));

    const pending = statuses.filter((status) => status === 'pending').length;

    expect(runKpis(runs).live).toBe(pending);
    expect(runKpis(runs).live).toBe(3);
  });
});

describe('the runs filter shares that vocabulary', () => {
  const run = (run_id: string, status: string) =>
    ({ run_id, status, title: run_id, issue_num: 1 }) as unknown as Run;

  it('files every backend status under active, merged or failed', () => {
    const runs = BACKEND_STATUSES.map((status) => run(status, status));
    const filed = new Set([
      ...filterRuns(runs, '', 'active').map((r) => r.status),
      ...filterRuns(runs, '', 'merged').map((r) => r.status),
      ...filterRuns(runs, '', 'failed').map((r) => r.status)
    ]);

    // awaiting_publication is the one status with no bucket: it is neither
    // running, merged nor failed, and inventing a bucket for it would be a
    // claim about work nothing has judged.
    expect([...filed].sort()).toEqual(
      BACKEND_STATUSES.filter((status) => status !== 'awaiting_publication').sort()
    );
  });

  it('shows a refusal and a budget stop under failed', () => {
    const runs = [run('a', 'refused'), run('b', 'budget_stop'), run('c', 'pending')];

    expect(filterRuns(runs, '', 'failed').map((r) => r.run_id)).toEqual(['a', 'b']);
    expect(filterRuns(runs, '', 'active').map((r) => r.run_id)).toEqual(['c']);
  });
});
