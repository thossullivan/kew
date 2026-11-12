import { describe, expect, it } from 'vitest';
import type { Run, RunVerdict } from '$lib/api';
import { verdictTone } from '$lib/epic/timeline';
import { statusShape } from '$lib/factory/status';
import { reviewHeading, verdictReading } from './verdict';

describe('a verdict as the run page prints it (design review c9 D11)', () => {
  it('words the two verdicts a review round ends on, each with a mark of its own', () => {
    expect(verdictReading('NEEDS_CHANGES')).toEqual({ kind: 'not_ready', words: 'Changes requested' });
    expect(verdictReading('READY')).toEqual({ kind: 'done', words: 'Ready' });
    // Shape plus colour, from the one table: the not-ready half ring and the
    // done dot, never the same mark.
    expect(statusShape('not_ready').shape).toBe('ring-half');
    expect(statusShape('done').shape).toBe('dot');
  });

  it('reads ready exactly where the console`s one verdict mapping does', () => {
    for (const code of ['READY', 'READY_WITH_RESIDUALS', 'NEEDS_CHANGES', 'NEEDS_FULL', 'DEGRADED', 'X']) {
      expect(verdictReading(code).kind === 'done', code).toBe(verdictTone(code) === 'success');
    }
  });

  it('gives every short-circuit verdict a mark the status table knows', () => {
    for (const code of ['NEEDS_FULL', 'DEGRADED', 'UNPARSEABLE', 'MERGED_UNREVIEWED', 'PRE_POLICY']) {
      const reading = verdictReading(code);
      expect(statusShape(reading.kind).known, code).toBe(true);
      expect(reading.words, code).not.toMatch(/_/);
    }
  });

  it('words a code it has never seen from its own letters, against the change', () => {
    expect(verdictReading('NEEDS_RETHINK')).toEqual({ kind: 'not_ready', words: 'Needs rethink' });
  });
});

describe('what a review run was, for its header', () => {
  const verdict = (scope: string): RunVerdict => ({
    round: 2,
    verdict: 'READY',
    head_sha: null,
    reviewer_model: null,
    ts: null,
    scope
  });
  const run = (run_kind: string, v: RunVerdict | null) => ({ run_kind, verdict: v }) as Run;

  it('names the round a review run attested itself', () => {
    expect(reviewHeading(run('adversarial_review', verdict('run')))).toBe('Review · round 2');
  });

  it('names no round it cannot vouch for', () => {
    expect(reviewHeading(run('adversarial_review', verdict('item')))).toBe('Review');
    expect(reviewHeading(run('adversarial_review', null))).toBe('Review');
  });

  it('says nothing for any other kind of run', () => {
    expect(reviewHeading(run('dispatch', verdict('item')))).toBe('');
    expect(reviewHeading(null)).toBe('');
  });
});
