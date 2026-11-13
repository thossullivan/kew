/**
 * The two loops-page readings, and the invariant that ties them (#2157 review).
 *
 * Both findings the review raised are regressions of the same kind: a reading
 * the page used to carry disappeared into a default. The gate line defaulted to
 * "pending" for any status it did not name — including two the page itself calls
 * terminal, so a finished loop reported its verification as still in progress
 * forever and stopped polling for the update that would never come. The budget
 * block defaulted to the Gauge, whose only numeric caption is over-cap, so an
 * under-cap loop printed its spend figure nowhere.
 *
 * The last block renders `Gauge` server-side to pin that second fact about the
 * component rather than trusting the reading of it: the page has to print the
 * figure because the gauge genuinely does not.
 */
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Gauge from '../ui/Gauge.svelte';
import { statusShape } from './status';
import { LOOP_TERMINAL, loopBudget, loopGate } from './loops';

/** The `LoopStatus` literal in kew/run_store.py, all six words. */
const LOOP_STATUSES = [
  'pursuing',
  'achieved',
  'unmet',
  'budget_limited',
  'needs_human',
  'paused'
];

describe('loopGate', () => {
  it('attests only the loop whose gate passed', () => {
    expect(loopGate('achieved')).toEqual({ kind: 'attested', word: 'gate passed' });
  });

  it('reads a failed gate as a guarantee that never arrived', () => {
    expect(loopGate('needs_human')).toEqual({ kind: 'missing', word: 'gate failed' });
    expect(loopGate('unmet')).toEqual({ kind: 'missing', word: 'gate unmet' });
  });

  it('reads a cap stop as unverified, the warn dashed ring its status already gets', () => {
    // budget_limited short-circuits BEFORE the gate (`_ERROR_STATUS` in
    // kew/loops/delegate.py): work recorded, no verdict on it. Not the danger X
    // — a cap is a stop, not a defect, exactly as status.ts reads the status.
    expect(loopGate('budget_limited')).toEqual({ kind: 'unverified', word: 'gate not reached' });
  });

  it('leaves pending only the two states where the gate is still ahead', () => {
    const pending = LOOP_STATUSES.filter((s) => loopGate(s).kind === 'pending');
    expect(pending).toEqual(['pursuing', 'paused']);
  });

  it('never reads a terminal loop as pending', () => {
    // The finding: `unmet` is in LOOP_TERMINAL, so the poll stops on it — a
    // pending gate reading there can never be corrected.
    for (const status of LOOP_TERMINAL) {
      expect(loopGate(status).kind, `${status} stops the poll while reading pending`).not.toBe(
        'pending'
      );
    }
  });

  it('gives every served loop status a reading of its own', () => {
    for (const status of LOOP_STATUSES) {
      expect(loopGate(status).word, `${status} has no gate word`).toMatch(/^gate /);
    }
  });

  it('names a kind StatusMark can actually draw', () => {
    // `kind` is a status WORD, not a StatusMeaning: it goes straight to
    // StatusMark, which paints an unrecognised word as the muted todo ring. A
    // gate reading that misspelled its own vocabulary would render as "nothing
    // has started" with no type error to catch it.
    for (const status of LOOP_STATUSES) {
      const shape = statusShape(loopGate(status).kind);
      expect(shape.known, `${status} has a gate kind status.ts does not know`).toBe(true);
    }
  });

  it('degrades an unshipped status to pending rather than inventing a verdict', () => {
    expect(loopGate('some_future_state')).toEqual({ kind: 'pending', word: 'gate pending' });
    expect(loopGate(null)).toEqual({ kind: 'pending', word: 'gate pending' });
    expect(loopGate(undefined)).toEqual({ kind: 'pending', word: 'gate pending' });
  });

  it('answers for a prototype member instead of walking up the chain', () => {
    // Same reachable input as `own` in status.ts guards: the word is off the wire.
    expect(loopGate('constructor')).toEqual({ kind: 'pending', word: 'gate pending' });
    expect(loopGate('__proto__')).toEqual({ kind: 'pending', word: 'gate pending' });
  });

  it('reads a status the server spelled loudly', () => {
    expect(loopGate(' Achieved ')).toEqual({ kind: 'attested', word: 'gate passed' });
  });
});

describe('LOOP_TERMINAL', () => {
  /** `_RESUMABLE` in kew/loops/delegate.py, transcribed. */
  const RESUMABLE = ['needs_human', 'budget_limited', 'paused'];

  it('holds the two finished states and excludes every resumable one', () => {
    expect([...LOOP_TERMINAL].sort()).toEqual(['achieved', 'unmet']);
    expect(LOOP_TERMINAL.has('pursuing')).toBe(false);
  });

  it('excludes every status an operator can restart', () => {
    // The regression: `budget_limited` was listed as terminal, so the page
    // permanently disarmed its 4s poll for a loop `kew loop resume --budget`
    // puts straight back into `pursuing` — the row then read "held" forever
    // while the loop was running. The Python side of this guard is
    // `test_loop_terminal_excludes_every_resumable_status` in
    // kew/tests/test_factory_status_parity.py, which reads `_RESUMABLE` itself
    // rather than the transcription above.
    for (const status of RESUMABLE) {
      expect(LOOP_TERMINAL.has(status), `${status} is resumable but stops the poll`).toBe(false);
    }
  });
});

describe('loopBudget', () => {
  it('prints the spend figure under cap, where the Gauge prints nothing', () => {
    expect(loopBudget(0.42, 5)).toEqual({ figure: '$0.42', capLabel: '$5', over: false });
  });

  it('spells the cap the way the gauge axis tick under it does', () => {
    // gaugeTicks labels the 100% tick `cap $5`, so the reading must not say
    // `$5.00` two lines above it.
    expect(loopBudget(0, 5).capLabel).toBe('$5');
    expect(loopBudget(0, 1500).capLabel).toBe('$1,500');
  });

  it('says "no cap" for an absent or unusable cap, as CapBar did', () => {
    expect(loopBudget(1.5, null)).toEqual({ figure: '$1.50', capLabel: 'no cap', over: false });
    expect(loopBudget(1.5, 0)).toEqual({ figure: '$1.50', capLabel: 'no cap', over: false });
  });

  it('flags the overrun so the figure can be painted in the violation tone', () => {
    // Loop #307: $14.42 against a $2.00 cap.
    expect(loopBudget(14.42, 2)).toEqual({ figure: '$14.42', capLabel: '$2', over: true });
    expect(loopBudget(2, 2).over).toBe(false);
  });

  it('reads a spend with no detail loaded as zero, never NaN', () => {
    expect(loopBudget(0, undefined).figure).toBe('$0');
  });
});

describe('the Gauge alone', () => {
  it('prints no spend figure under cap — which is why the page must', () => {
    const under = render(Gauge, { props: { spent: 0.42, cap: 5 } }).body;
    expect(under, 'the gauge names the cap on its axis').toContain('cap $5');
    expect(under, 'the under-cap gauge printed the spend after all').not.toContain('$0.42');
  });

  it('captions only the overrun, and still without the figure', () => {
    const over = render(Gauge, { props: { spent: 14.42, cap: 2 } }).body;
    expect(over).toContain('over cap');
    expect(over).not.toContain('$14.42');
  });
});
