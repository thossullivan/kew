import { describe, expect, it } from 'vitest';
import {
  avgReviews,
  deriveHeadline,
  isLocalRow,
  lifecycleNote,
  mergeRate,
  perMerged,
  planningRatio,
  windowHasPRs
} from './scorecard-headline';
import type { ScorecardRow } from './api';

const row = (over: Partial<ScorecardRow>): ScorecardRow => ({
  model: 'm',
  channel: 'hosted',
  lifecycle_status: '',
  runs: 0,
  opened: 0,
  merged: 0,
  review_total: 0,
  review_n: 0,
  spend: 0,
  planned_spend: 0,
  paired_spend: 0,
  planned_n: 0,
  by_complexity: [],
  ...over
});

describe('deriveHeadline', () => {
  it('sums spend + merged and isolates local spend', () => {
    const h = deriveHeadline([
      row({ model: 'claude-opus-4-6', merged: 20, spend: 180 }),
      row({ model: 'claude-sonnet-4-6', merged: 3, spend: 40 }),
      row({ model: 'lemonade/Qwen3-Coder-30B', channel: 'local', merged: 2, spend: 0 })
    ]);
    expect(h.totalSpend).toBe(220);
    expect(h.totalMerged).toBe(25);
    expect(h.localSpend).toBe(0);
  });

  it('sums local spend when local models incur cost', () => {
    const h = deriveHeadline([
      row({ model: 'claude-opus-4-6', merged: 5, spend: 100 }),
      row({ model: 'lemonade/local-30b', channel: 'local', merged: 4, spend: 7 })
    ]);
    expect(h.totalSpend).toBe(107);
    expect(h.totalMerged).toBe(9);
    expect(h.localSpend).toBe(7);
  });

  it('keys local spend on backend channel, not a model-name heuristic (N11)', () => {
    const h = deriveHeadline([
      // ollama/ — the old startsWith('lemonade/') heuristic missed it.
      row({ model: 'ollama/qwen3', channel: 'local', spend: 4 }),
      // hosted model whose name contains "local" — the old includes('local')
      // heuristic false-positived it as free.
      row({ model: 'acme/local-fastpath-v2', channel: 'hosted', spend: 30 })
    ]);
    expect(h.totalSpend).toBe(34);
    expect(h.localSpend).toBe(4);
  });

  it('returns zeros for an empty scorecard', () => {
    const h = deriveHeadline([]);
    expect(h.totalSpend).toBe(0);
    expect(h.totalMerged).toBe(0);
    expect(h.localSpend).toBe(0);
  });
});

describe('windowHasPRs', () => {
  it('is true when any row opened a PR in the window', () => {
    expect(
      windowHasPRs([
        row({ model: 'claude-opus-4-6', opened: 0, merged: 0 }),
        row({ model: 'lemonade/local-30b', opened: 3, merged: 2 })
      ])
    ).toBe(true);
  });

  it('is false when no row opened a PR in the window', () => {
    expect(
      windowHasPRs([
        row({ model: 'claude-opus-4-6', runs: 4, opened: 0, spend: 12 }),
        row({ model: 'lemonade/local-30b', runs: 2, opened: 0, spend: 0 })
      ])
    ).toBe(false);
  });

  it('is false for an empty scorecard', () => {
    expect(windowHasPRs([])).toBe(false);
  });
});

describe('planningRatio', () => {
  it('reports actual over the planned bound (the #351 baseline)', () => {
    expect(
      planningRatio(row({ planned_spend: 1.0787, paired_spend: 0.1064, planned_n: 1 }))
    ).toBe('0.10x');
  });

  it('is 1.00x when the bound predicted spend exactly', () => {
    expect(planningRatio(row({ planned_spend: 2, paired_spend: 2, planned_n: 2 }))).toBe('1.00x');
  });

  it('renders no ratio when no run recorded a bound', () => {
    // Historical rows carry NULL, never a $0 planned stand-in.
    expect(planningRatio(row({ spend: 5, planned_spend: 0, paired_spend: 0, planned_n: 0 }))).toBe(
      '—'
    );
  });
});

describe('row cells (FUI-28)', () => {
  const money = (n: number) => '$' + n.toFixed(2);

  it('reads "local" off the served channel, never off the model id (N11)', () => {
    expect(isLocalRow(row({ model: 'lemonade/Qwen3-Coder-30B', channel: 'local' }))).toBe(true);
    // A hosted model whose NAME looks local is hosted: the fixture's own
    // `lemonade/` prefix is not the definition.
    expect(isLocalRow(row({ model: 'lemonade/hosted-preview', channel: 'hosted' }))).toBe(false);
    expect(isLocalRow(row({ model: 'claude-opus-4-6', channel: 'hosted' }))).toBe(false);
    // A row from a serve that ships no channel at all is not claimed as local.
    expect(isLocalRow({})).toBe(false);
  });

  it('states a merge rate with its own counts, and no rate at all without one', () => {
    expect(mergeRate({ opened: 21, merged: 18 })).toBe('86% (18/21)');
    expect(mergeRate({ opened: 3, merged: 0 })).toBe('0% (0/3)');
    // Nothing opened is not a 0% merge rate — there was nothing to merge.
    expect(mergeRate({ opened: 0, merged: 0 })).toBe('—');
  });

  it('never divides spend by a window that merged nothing', () => {
    expect(perMerged({ spend: 124.5, merged: 10 }, money)).toBe('$12.45');
    expect(perMerged({ spend: 124.5, merged: 0 }, money)).toBe('—');
    expect(perMerged({ spend: 0, merged: 0 }, money)).toBe('—');
  });

  it('averages review rounds only over the runs that recorded one', () => {
    expect(avgReviews({ review_total: 7, review_n: 4 })).toBe('1.8');
    expect(avgReviews({ review_total: 0, review_n: 2 })).toBe('0.0');
    expect(avgReviews({ review_total: 0, review_n: 0 })).toBe('—');
  });

  it('carries a lifecycle warning only when the snapshot has one to give', () => {
    expect(lifecycleNote('retired')).toEqual({ label: 'retired', class: 'text-danger' });
    expect(lifecycleNote('retiring')?.class).toBe('text-warn');
    expect(lifecycleNote('scheduled')?.class).toBe('text-accent-2');
    expect(lifecycleNote('watch')?.label).toBe('watching');
    // "ok", "untracked", an empty status and an unknown word all say nothing.
    for (const quiet of ['ok', 'untracked', '', 'something-new']) {
      expect(lifecycleNote(quiet), quiet).toBeNull();
    }
  });
});
