/**
 * #2245, #2539 — one sentence per needs-you row, and it is the server's.
 *
 * #2245 composed the sentence here, once, so the surfaces of one screen read
 * one row one way. #2539 moved that composition to the server
 * (`kew.service.attention.say_wait`), because the epic popover and the rail
 * composed their own and one merge decision read four ways across the
 * console. What is pinned here is that nothing on this side composes it any
 * more: the served `sentence` is printed verbatim, the reference the server
 * wrote on the reason is the only thing added, and a serve that predates the
 * field has its reason — the server's words too — stand in.
 */
import { describe, expect, it } from 'vitest';
import type { DashboardRow } from '$lib/api';
import { rowSentence, rowSummary } from './summary';

const row = (over: Partial<DashboardRow> = {}): DashboardRow => ({
  type: 'budget_hold',
  consequence: 'money',
  age: '2026-09-28T09:00:00+00:00',
  reason: '',
  primary_action: 'approve',
  issue_num: 2101,
  item_id: '2101',
  estimate_usd: null,
  epic_ref: 'go-live',
  url: null,
  pr_number: null,
  ...over
});

describe('a needs-you row as one sentence', () => {
  it('prints the sentence the server wrote, verbatim', () => {
    const r = row({
      reason: 'budget_preflight: epic cap reached at $40.00',
      label: 'held on budget',
      sentence: 'held on budget: epic cap reached at $40.00'
    });
    expect(rowSummary(r)).toBe('held on budget: epic cap reached at $40.00');
    expect(rowSentence(r)).toBe('held on budget: epic cap reached at $40.00');
  });

  it('never re-words the sentence out of the label or the hold table', () => {
    // A label and a hold prefix the console's own table has words for, and a
    // sentence that uses neither: the sentence wins, untouched.
    const r = row({
      reason: 'review_rounds_exhausted',
      label: 'the review round cap was reached',
      sentence: 'something the server chose to say'
    });
    expect(rowSummary(r)).toBe('something the server chose to say');
  });

  it('keeps the leading item reference the server wrote on the reason', () => {
    const r = row({
      type: 'epic_wait',
      reason: '#2055 — review_rounds_exhausted',
      label: 'the review round cap was reached',
      sentence: 'the review round cap was reached'
    });
    expect(rowSummary(r)).toBe('#2055 — the review round cap was reached');
    expect(rowSentence(r)).toBe('the review round cap was reached');
  });

  it('lets the reason stand in for a serve that predates the field', () => {
    const r = row({ type: 'pr_review', reason: 'merge decision on PR #2059' });
    expect(rowSummary(r)).toBe('merge decision on PR #2059');
    expect(rowSentence(r)).toBe('merge decision on PR #2059');
    // As written, prefix and all — composing words for it is the server's job.
    const held = row({ type: 'epic_wait', reason: '#2055 — flux_capacitor_cold: at 1.21GW' });
    expect(rowSummary(held)).toBe('#2055 — flux_capacitor_cold: at 1.21GW');
    expect(rowSentence(held)).toBe('flux_capacitor_cold: at 1.21GW');
  });

  it('leaves a tracker reference off too, not only a GitHub number', () => {
    const r = row({ type: 'epic_wait', reason: 'SFD-12 — in_review awaiting merge' });
    expect(rowSentence(r)).toBe('in_review awaiting merge');
  });

  it('says nothing, rather than inventing a sentence, when the row says nothing', () => {
    expect(rowSummary(row({ reason: '' }))).toBe('');
    expect(rowSentence(row({ reason: '   ', label: '  ', sentence: ' ' }))).toBe('');
  });
});
