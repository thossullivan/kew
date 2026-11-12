/**
 * #2208 — one epic-hold reason, read as a category, a sentence and an action.
 *
 * The owner's screenshots of 2026-09-29 caught a round-cap hold drawn as
 * `BUDGET · review_rounds_exhausted · Approve this hold`: the wrong category,
 * the raw reason string in place of words, and a button that cannot clear it
 * (`/api/approvals` decides the GitHub needs-approval LABEL — it has never
 * touched an `epic_holds` row). All three are decided here.
 */
import { describe, expect, it } from 'vitest';
import {
  capKeyOf,
  describeHold,
  holdSentence,
  HOLD_SPECS,
  unholdCommand,
  UNKNOWN_HOLD_ACTION,
  type HoldPlace
} from './hold-reasons';

const WHERE: HoldPlace = { epic: 'go-live', item: '2005' };

describe('the category comes from epic_holds.py`s prefixes', () => {
  it('reads the round cap as review, never as budget', () => {
    const words = describeHold('review_rounds_exhausted', WHERE);
    expect(words.category).toBe('review');
    expect(words.label).toBe('the review round cap was reached');
  });

  it.each([
    ['ci_pending', 'ci'],
    ['budget_preflight: spent $12.00 > per_issue cap $10.00', 'budget'],
    ['review_budget: review pool $5.00 exhausted (spent $6.00)', 'budget'],
    ['review_crash', 'review'],
    ['review_unparseable', 'review'],
    ['review_pr_closed', 'review'],
    ['review_residuals_unfiled', 'review'],
    ['review_launch_failed: no agent image', 'review'],
    ['review_sandbox_refused: no mapped image', 'sandbox'],
    ['sandbox_containment: no mapped image for claude', 'sandbox'],
    ['admission_refused: dispatch child refused', 'dispatch'],
    ['credential_issuer_refused: issuer unreachable', 'dispatch'],
    ['preexisting_worktree_refused: /tmp/wt is dirty', 'dispatch'],
    ['base_integration_conflict: src/kew/cli.py', 'merge'],
    ['base_integration_failed: git returned 128', 'merge'],
    ['merge_gate_touched@abc123: scripts/verify.sh', 'merge']
  ])('reads %s as %s', (reason, category) => {
    expect(describeHold(reason, WHERE).category).toBe(category);
  });

  it('reads a reason it has never heard of as other, with the raw reason', () => {
    // The one failure this table must not have: a default that files an
    // unknown hold under a category it invented. `other` plus the reason the
    // driver actually wrote is the honest reading of a vocabulary that grew.
    const words = describeHold('moon_phase_wrong: waxing gibbous', WHERE);
    expect(words.category).toBe('other');
    expect(words.label).toBe('');
    expect(words.detail).toBe('moon_phase_wrong: waxing gibbous');
    expect(holdSentence(words)).toBe('moon_phase_wrong: waxing gibbous');
    expect(words.action).toBe(`fix the cause, then ${unholdCommand(WHERE)}`);
  });

  it('reads an empty reason as other and says it was held with none recorded', () => {
    const words = describeHold('', WHERE);
    expect(words.category).toBe('other');
    expect(holdSentence(words)).toBe('held, with no reason recorded');
  });

  it('never lets one prefix shadow another — first match wins, so order is a fact', () => {
    for (const [i, spec] of HOLD_SPECS.entries()) {
      const shadowed = HOLD_SPECS.slice(i + 1).filter((other) =>
        other.prefix.startsWith(spec.prefix)
      );
      expect(shadowed, `${spec.prefix} shadows ${shadowed.map((s) => s.prefix)}`).toEqual([]);
    }
  });
});

describe('the sentence is the reason in words, keeping the detail it carries', () => {
  it('names the numbers a budget hold was written with, under the words', () => {
    const words = describeHold(
      'budget_preflight: spent $12.00 > per_issue cap $10.00 — raise budget.per_issue_usd in kew.toml',
      WHERE
    );
    expect(holdSentence(words)).toBe(
      'held on the dispatch budget: spent $12.00 > per_issue cap $10.00'
    );
  });

  it('drops the raise clause from the sentence — it is the action, said once', () => {
    const words = describeHold(
      'review_budget: review pool $5.00 exhausted (spent $6.00) — raise [review].max_review_usd',
      WHERE
    );
    expect(holdSentence(words)).toBe(
      'held on the review budget: review pool $5.00 exhausted (spent $6.00)'
    );
    expect(words.action).toBe('raise [review].max_review_usd');
  });

  it('says the words alone when the reason carries no detail', () => {
    expect(holdSentence(describeHold('review_rounds_exhausted', WHERE))).toBe(
      'the review round cap was reached'
    );
  });

  it('keeps the head a merge-gate hold carries out of the sentence', () => {
    const words = describeHold('merge_gate_touched@abc123: scripts/verify.sh', WHERE);
    expect(holdSentence(words)).toBe(
      "the PR edits the merge gate's own definition: scripts/verify.sh"
    );
  });
});

describe('the action is the one that actually clears the hold', () => {
  it('names the key to raise on a budget hold, read off the reason the server wrote', () => {
    expect(
      describeHold(
        'budget_preflight: spent $12.00 > per_issue cap $10.00 — raise budget.per_issue_usd in kew.toml',
        WHERE
      ).action
    ).toBe('raise budget.per_issue_usd');
  });

  it('falls back to naming the reason when a budget hold names no key', () => {
    expect(describeHold('budget_preflight: over the cap', WHERE).action).toBe(
      'raise the cap its reason names'
    );
  });

  it('offers the unhold that restarts the review cycle on the round cap', () => {
    // `kew epic unhold` is the ONE command that recovers a round-cap hold:
    // it restarts the review cycle in the same transaction as the release
    // (REVIEW_CYCLE_RESET_HOLD_REASONS), because a bare release hands the
    // next pass the same verdict against the same count.
    expect(describeHold('review_rounds_exhausted', WHERE).action).toBe(
      'kew epic unhold go-live 2005 — it restarts the review cycle'
    );
  });

  it('offers nothing at all on a CI-pending hold — nobody can make a check finish', () => {
    expect(describeHold('ci_pending', WHERE).action).toBe('');
  });

  it('names the fix before the unhold where there is one to make', () => {
    expect(describeHold('sandbox_containment: no image for claude', WHERE).action).toBe(
      'map [sandbox] images.<runner>, then kew epic unhold go-live 2005'
    );
  });

  it('prints a bare command when the row names no epic or item', () => {
    expect(unholdCommand({ epic: '', item: '' })).toBe('kew epic unhold');
    expect(unholdCommand({ epic: 'go-live', item: '' })).toBe('kew epic unhold go-live');
  });

  it('keeps the unbucketed fallback the CLI and TUI already print', () => {
    expect(UNKNOWN_HOLD_ACTION).toBe('fix the cause, then kew epic unhold');
  });
});

describe('the cap key a reason names', () => {
  it.each([
    ['budget_preflight: — raise budget.per_issue_usd in kew.toml', 'budget.per_issue_usd'],
    ['review_budget: — raise [review].max_review_usd', '[review].max_review_usd'],
    ['review_budget: — raise [review].max_remediation_usd', '[review].max_remediation_usd'],
    ['budget_preflight: spent over the weekly cap', ''],
    ['', '']
  ])('reads %s as %s', (reason, key) => {
    expect(capKeyOf(reason)).toBe(key);
  });
});

describe('a refused PR head clears itself when the head moves', () => {
  const REASON =
    'admission_refused@93e6183f8ede: credential_publication_refused: publication ancestor evidence is missing';

  it('reads as a review hold, with the head cut from the sentence', () => {
    const words = describeHold(REASON, WHERE);
    expect(words.category).toBe('review');
    expect(holdSentence(words)).toBe(
      'the PR head was refused: credential_publication_refused: publication ancestor evidence is missing'
    );
  });

  it('says a pushed fix clears it, and keeps the unhold as the other way out', () => {
    expect(describeHold(REASON, WHERE).action).toBe(
      'push a fix (the hold clears when the PR head moves), or kew epic unhold go-live 2005'
    );
  });

  it('leaves a dispatch child refusal, which carries no head, to the unhold', () => {
    const words = describeHold('admission_refused: unpriced model', WHERE);
    expect(words.category).toBe('dispatch');
    expect(words.action).toBe('fix what its reason names, then kew epic unhold go-live 2005');
  });
});
