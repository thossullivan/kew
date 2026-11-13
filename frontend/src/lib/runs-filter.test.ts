import { describe, expect, it } from 'vitest';
import { filterRuns, STATUS_LABELS, toneFor } from './runs-filter';
import type { Run } from './api';

const mk = (over: Partial<Run>): Run => ({
  run_id: 'r', issue_num: 1, title: 't', runner: 'x', model: 'm',
  status: 'success', cost_usd: 0, channel: 'hosted', started_at: null, finished_at: null,
  // Flight-recorder columns (F3a) — backend DTO always sends them; defaulted here.
  duration_seconds: 0, input_tokens: 0, output_tokens: 0,
  cache_read_tokens: 0, cache_creation_tokens: 0,
  diff_additions: null, diff_deletions: null, diff_files: null, review_cycles: null,
  pr_url: null, pr_state: null, attempt_number: 0, error_category: '', complexity: '', branch: '',
  // FUI-13 stage + the posture the DTO always sent; defaulted as serve does.
  stage: 'unknown', stage_detail: '',
  posture: 'not recorded', filesystem_boundary: 'not recorded',
  network_boundary: 'not recorded', egress_verification_basis: null,
  credential_names: [], command_guard: 'not recorded', admission: 'not recorded',
  reason: null,
  // C3 (#2300) — the recorded route and accounting columns; the detail-only
  // verdict is null on a list row.
  run_kind: 'dispatch', item_id: null, reported_model: null, provider: null,
  transport_mode: 'direct', planned_cost_usd: null, accounting_status: null,
  verdict: null,
  ...over
});

describe('STATUS_LABELS', () => {
  it('is all/active/merged/failed in order', () => {
    expect(STATUS_LABELS).toEqual(['all', 'active', 'merged', 'failed']);
  });
});

describe('filterRuns', () => {
  // Statuses reconciled to the real backend RunStatus enum: pending is the only
  // non-terminal (active) state; success is the merged/ok outcome; failure et al.
  const runs = [
    mk({ issue_num: 12, title: 'fix login', status: 'success' }),
    mk({ issue_num: 34, title: 'add cache', status: 'pending' }),
    mk({ issue_num: 56, title: 'flaky test', status: 'failure' })
  ];
  it('matches query on issue number', () => {
    expect(filterRuns(runs, '34', 'all').map((r) => r.issue_num)).toEqual([34]);
  });
  it('matches query on title (case-insensitive)', () => {
    expect(filterRuns(runs, 'LOGIN', 'all').map((r) => r.issue_num)).toEqual([12]);
  });
  it('filters active (pending)', () => {
    expect(filterRuns(runs, '', 'active').map((r) => r.status)).toEqual(['pending']);
  });
  it('filters merged (success)', () => {
    expect(filterRuns(runs, '', 'merged').map((r) => r.status)).toEqual(['success']);
  });
  it('filters failed', () => {
    expect(filterRuns(runs, '', 'failed').map((r) => r.status)).toEqual(['failure']);
  });
  it('includes budget_stopped and killed under failed', () => {
    const rs = [mk({ status: 'budget_stopped' }), mk({ status: 'killed' })];
    expect(filterRuns(rs, '', 'failed')).toHaveLength(2);
  });
});

describe('toneFor', () => {
  it('maps real statuses and display states to badge tones', () => {
    expect(toneFor('success')).toBe('success');
    expect(toneFor('failure')).toBe('danger');
    expect(toneFor('budget_exceeded')).toBe('warn');
    expect(toneFor('running')).toBe('accent');
    expect(toneFor('queued')).toBe('neutral');
  });
  it('falls back to neutral for unknown statuses', () => {
    expect(toneFor('something-new')).toBe('neutral');
  });
});