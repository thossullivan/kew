import { describe, expect, it } from 'vitest';
import type { ImplementerOutcome, ReviewerOutcome } from '$lib/api';
import {
  implementerCells,
  OUTCOMES_READ_ATTEMPTS,
  tableAgreement,
  tooFew,
  verdictCells,
  verdictColumns,
  verdictCount,
  verdictLabel
} from './outcomes';

const row = (over: Partial<ImplementerOutcome>): ImplementerOutcome => ({
  version: 2,
  label: 'v2',
  state: 'active',
  n: 12,
  comparable: true,
  round_one_items: 12,
  first_pass_rate: 0.75,
  ready_items: 11,
  mean_rounds_to_ready: 1.2727,
  merged_items: 8,
  median_cost_usd: 2.27,
  ...over
});

describe('the outcomes table cells', () => {
  it('prints a rate, a mean and a cost, mono-ready', () => {
    expect(implementerCells(row({}))).toEqual(['75%', '1.3', '$2.27']);
  });

  it('prints a dash for a rate there was nothing to measure for', () => {
    expect(implementerCells(row({ first_pass_rate: null, mean_rounds_to_ready: null, median_cost_usd: null }))).toEqual(
      ['—', '—', '—']
    );
  });

  it('says a version is too small to compare, with its n', () => {
    expect(tooFew(4)).toBe('Too few items to compare yet (n = 4).');
    expect(tooFew(6, 'rounds')).toBe('Too few rounds to compare yet (n = 6).');
  });

  const reviewer = (over: Partial<ReviewerOutcome>): ReviewerOutcome => ({
    version: null,
    label: 'built-in',
    state: null,
    n: 82,
    comparable: true,
    verdicts: { DEGRADED: 1, NEEDS_CHANGES: 30, READY: 51 },
    ...over
  });

  it('orders the verdict columns by count across comparable versions, then name', () => {
    const rows = [
      reviewer({ label: 'v2', version: 2, n: 40, verdicts: { NEEDS_CHANGES: 30, READY: 5, ERROR: 5 } }),
      reviewer({ label: 'v1', version: 1, n: 3, comparable: false, verdicts: { TIMEOUT: 3 } }),
      reviewer({})
    ];
    // NEEDS_CHANGES 60 outranks READY 56 only summed across both versions;
    // v1's TIMEOUT prints as a sentence, so it gets no column.
    expect(verdictColumns(rows)).toEqual(['NEEDS_CHANGES', 'READY', 'ERROR', 'DEGRADED']);
  });

  it('prints a count per verdict cell, zero where a version returned none', () => {
    const row = reviewer({ verdicts: { READY: 51 } });
    expect(verdictCount(row, 'READY')).toBe('51');
    expect(verdictCount(row, 'NEEDS_CHANGES')).toBe('0');
    expect(verdictLabel('NEEDS_CHANGES')).toBe('NEEDS CHANGES');
  });

  it('lays out one cell per verdict column, in column order', () => {
    const row = reviewer({ verdicts: { READY: 51, ERROR: 2 } });
    expect(verdictCells(row, ['NEEDS_CHANGES', 'READY', 'ERROR'])).toEqual([
      { verdict: 'NEEDS_CHANGES', count: '0' },
      { verdict: 'READY', count: '51' },
      { verdict: 'ERROR', count: '2' }
    ]);
  });
});

describe('an outcomes table beside the versions read', () => {
  const settled = { pending: false, attempts: 0 };

  it('is drawn when both name one generation', () => {
    expect(tableAgreement({ ...settled, outcomes: 2, history: 2 })).toBe('draw');
  });

  it('is read again, not drawn, when the versions landed at another generation', () => {
    // The versions read lands at 1 (v2 proposed), a CLI approval commits, and
    // the outcomes read lands at 2 (v2 active).
    expect(tableAgreement({ ...settled, outcomes: 2, history: 1 })).toBe('reread');
    expect(tableAgreement({ ...settled, outcomes: 1, history: 2 })).toBe('reread');
  });

  it('waits while either read is in flight', () => {
    expect(tableAgreement({ ...settled, outcomes: 2, history: 1, pending: true })).toBe('wait');
    expect(tableAgreement({ ...settled, outcomes: 2, history: undefined })).toBe('wait');
  });

  it('gives up after the bounded re-reads', () => {
    const attempts = OUTCOMES_READ_ATTEMPTS;
    expect(tableAgreement({ ...settled, outcomes: 2, history: 1, attempts })).toBe('diverged');
  });

  it('is drawn when there is no history to compare with', () => {
    expect(tableAgreement({ ...settled, outcomes: 2, history: null })).toBe('draw');
  });
});
