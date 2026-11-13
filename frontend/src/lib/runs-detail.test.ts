import { describe, expect, it } from 'vitest';
import { groupRunsByDay, cacheHitPct, diffStat } from './runs-detail';
import type { Run } from './api';

const mk = (over: Partial<Run>): Run => ({
  run_id: 'r', issue_num: 1, title: 't', runner: 'x', model: 'm',
  status: 'success', cost_usd: 0, channel: 'hosted', started_at: null, finished_at: null,
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

describe('groupRunsByDay', () => {
  it('groups by UTC calendar day, not local time', () => {
    // 22:00 EDT on the 15th is 02:00 UTC on the 16th → must land in the 16th.
    const runs = [
      mk({ run_id: 'a', started_at: '2026-07-15T23:30:00Z', cost_usd: 1 }),
      mk({ run_id: 'b', started_at: '2026-07-15T22:00:00-04:00', cost_usd: 2 })
    ];
    const groups = groupRunsByDay(runs);
    expect(groups.map((g) => g.day)).toEqual(['2026-07-15', '2026-07-16']);
    expect(groups[0].runs.map((r) => r.run_id)).toEqual(['a']);
    expect(groups[1].runs.map((r) => r.run_id)).toEqual(['b']);
  });

  it('sums cost_usd into a per-day subtotal', () => {
    const runs = [
      mk({ started_at: '2026-07-15T01:00:00Z', cost_usd: 0.5 }),
      mk({ started_at: '2026-07-15T02:00:00Z', cost_usd: 1.25 }),
      mk({ started_at: '2026-07-14T02:00:00Z', cost_usd: 3 })
    ];
    const groups = groupRunsByDay(runs);
    expect(groups.map((g) => g.subtotal)).toEqual([1.75, 3]);
  });

  it('preserves input order of days and runs within a day', () => {
    const runs = [
      mk({ run_id: 'x', started_at: '2026-07-16T05:00:00Z' }),
      mk({ run_id: 'y', started_at: '2026-07-15T05:00:00Z' }),
      mk({ run_id: 'z', started_at: '2026-07-16T01:00:00Z' })
    ];
    const groups = groupRunsByDay(runs);
    expect(groups.map((g) => g.day)).toEqual(['2026-07-16', '2026-07-15']);
    expect(groups[0].runs.map((r) => r.run_id)).toEqual(['x', 'z']);
  });

  it('carries a human UTC label per day', () => {
    const groups = groupRunsByDay([mk({ started_at: '2026-07-15T12:00:00Z' })]);
    expect(groups[0].label).toBe('Jul 15, 2026');
  });

  it('falls back to finished_at when started_at is null', () => {
    const groups = groupRunsByDay([mk({ started_at: null, finished_at: '2026-07-15T12:00:00Z' })]);
    expect(groups[0].day).toBe('2026-07-15');
  });

  it('buckets runs with no usable timestamp under "unknown"', () => {
    const groups = groupRunsByDay([mk({ started_at: null, finished_at: null })]);
    expect(groups[0].day).toBe('unknown');
    expect(groups[0].label).toBe('Unknown date');
  });
});

describe('cacheHitPct', () => {
  it('is cache_read over all prompt tokens, rounded', () => {
    const run = mk({ input_tokens: 200, cache_read_tokens: 700, cache_creation_tokens: 100 });
    expect(cacheHitPct(run)).toBe(70);
  });

  it('returns null when there are zero prompt tokens (guard)', () => {
    expect(cacheHitPct(mk({ input_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 }))).toBeNull();
  });

  it('is 0 when nothing was served from cache', () => {
    expect(cacheHitPct(mk({ input_tokens: 500, cache_read_tokens: 0, cache_creation_tokens: 0 }))).toBe(0);
  });
});

describe('diffStat', () => {
  it('formats additions, deletions and file count', () => {
    expect(diffStat(mk({ diff_additions: 412, diff_deletions: 87, diff_files: 9 }))).toBe('+412 −87 · 9 files');
  });

  it('returns null when all diff fields are null', () => {
    expect(diffStat(mk({ diff_additions: null, diff_deletions: null, diff_files: null }))).toBeNull();
  });

  it('treats a present-but-partial diff as zeros for the missing parts', () => {
    expect(diffStat(mk({ diff_additions: 5, diff_deletions: null, diff_files: 1 }))).toBe('+5 −0 · 1 file');
  });
});
