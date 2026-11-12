/**
 * #2305 — one motion vocabulary, and the two readings built on it.
 *
 * The TABLE itself is held to the backend's copy by
 * `kew/tests/test_motion_parity.py`; duplicating that mapping here would just
 * be a third copy to drift. What this file pins is the behaviour around it: the
 * precedence between a served `motion` and a status word, the degradation of a
 * word neither side has shipped, and the running bar's shape — which has to
 * grow forever without ever claiming to have arrived.
 */
import { describe, expect, it } from 'vitest';
import {
  barPct,
  FALLBACK_MOTION,
  isAlive,
  isExecuting,
  MOTION_OF,
  motionOf,
  RUN_MOTION_OF,
  runBarPct,
  SETTLE_MS
} from './motion';

describe('motionOf', () => {
  it('names every status that moves, and only those', () => {
    const executing = Object.entries(MOTION_OF)
      .filter(([, m]) => m === 'executing')
      .map(([status]) => status)
      .sort();
    // Three NODE statuses (the DAG's), one run status and one loop status —
    // `pending` and `pursuing`, the two the server refines, because an
    // orphaned run and a loop nothing is advancing are not executing and only
    // the server can tell — and the console's own two display words for a run
    // in flight.
    expect(executing).toEqual([
      'adversarial_review',
      'in_progress',
      'pending',
      'pursuing',
      'remediating',
      'running',
      'working'
    ]);
  });

  it('reads a published pull request as waiting on a person', () => {
    // The whole point. `in_review` used to be "live" in metro.ts, so the one
    // cyan edge on the canvas pointed at work that had stopped.
    expect(motionOf('in_review')).toBe('waiting_human');
    expect(motionOf('awaiting_publication')).toBe('waiting_kew');
  });

  it('reads a status word however it is spelled', () => {
    for (const spelling of ['In Progress', 'in-progress', '  IN_PROGRESS  '])
      expect(motionOf(spelling)).toBe('executing');
  });

  it('degrades an unknown status without moving it or burying it', () => {
    // A status a later kew ships must not earn a halo, and must not be
    // reported as history either.
    for (const unknown of ['teleporting', '', null, undefined])
      expect(motionOf(unknown)).toBe(FALLBACK_MOTION);
    expect(FALLBACK_MOTION).not.toBe('executing');
    expect(isAlive({ motion: FALLBACK_MOTION })).toBe(true);
  });

  it('cannot be walked off the table into Object.prototype', () => {
    // The status word comes off the wire, and `__proto__` and `constructor`
    // survive normalising unchanged.
    for (const key of ['constructor', '__proto__', 'hasOwnProperty', 'toString'])
      expect(motionOf(key)).toBe(FALLBACK_MOTION);
  });
});

describe('the one word the two vocabularies read oppositely', () => {
  it('reads a queued run as the dispatcher’s turn and a queued node as a person’s', () => {
    // A queued NODE is work nobody has armed: the adapter reports it precisely
    // when the marker a person adds is absent (#2209). A queued RUN is this
    // console's own display word (`displayStatus` in kpi.ts) for an untouched
    // `pending` row, which the dispatcher picks up unasked.
    expect(motionOf('queued')).toBe('waiting_human');
    expect(motionOf('queued', 'epic')).toBe('waiting_human');
    expect(motionOf('queued', 'run')).toBe('waiting_kew');
    expect(isAlive({ status: 'queued' }, 'run')).toBe(true);
    expect(isExecuting({ status: 'queued' }, 'run')).toBe(false);
  });

  it('refines that word and no other', () => {
    // A second collision is a decision: it belongs in `RUN_MOTION_OF`, which
    // `kew/tests/test_motion_parity.py` holds to the backend's copy.
    expect(Object.keys(RUN_MOTION_OF)).toEqual(['queued']);
    for (const word of Object.keys(MOTION_OF))
      if (word !== 'queued') expect(motionOf(word, 'run')).toBe(motionOf(word, 'epic'));
  });

  it('leaves a served motion alone whichever vocabulary is asked for', () => {
    // The server's own reading wins in both: it knows the facts no word carries.
    expect(motionOf('teleporting', 'run')).toBe(FALLBACK_MOTION);
    expect(isExecuting({ status: 'queued', motion: 'executing' }, 'run')).toBe(true);
  });
});

describe('isExecuting / isAlive', () => {
  it("prefers the server's reading to the status word", () => {
    // A pending run whose owner is provably dead: the word says executing and
    // the server says otherwise, because only the server probed the owner.
    const orphan = { status: 'pending', motion: 'waiting_kew' };
    expect(isExecuting(orphan)).toBe(false);
    expect(isAlive(orphan)).toBe(true);
  });

  it('falls back to the status word when no motion was served', () => {
    // A replayed tick from a server older than #2305 carries no `motion`.
    expect(isExecuting({ status: 'remediating' })).toBe(true);
    expect(isExecuting({ status: 'in_review' })).toBe(false);
    expect(isExecuting({ status: 'in_progress', motion: '' })).toBe(true);
    expect(isAlive({ status: 'done' })).toBe(false);
  });

  it('is alive for everything but settled', () => {
    expect(isAlive({ motion: 'executing' })).toBe(true);
    expect(isAlive({ motion: 'waiting_human' })).toBe(true);
    expect(isAlive({ motion: 'settled' })).toBe(false);
  });
});

describe('the running bar', () => {
  it('always grows and never arrives', () => {
    const widths = [0, 1_000, 60_000, 600_000, 3_600_000, 86_400_000].map(runBarPct);
    for (let i = 1; i < widths.length; i++) expect(widths[i]).toBeGreaterThan(widths[i - 1]);
    // Not a progress bar: kew cannot know how far through its work an agent is,
    // so the bar must never read as complete however long it runs.
    expect(widths[widths.length - 1]).toBeLessThan(100);
  });

  it('starts visible, so an executing card without a start time still says so', () => {
    expect(runBarPct(0)).toBeGreaterThan(0);
    expect(barPct(null, Date.now())).toBe(runBarPct(0));
    expect(barPct('not a date', Date.now())).toBe(runBarPct(0));
    expect(barPct('', Date.now())).toBe(runBarPct(0));
  });

  it('never draws backwards off a clock skew', () => {
    const now = Date.parse('2026-10-06T12:00:00Z');
    const future = '2026-10-06T12:05:00+00:00';
    expect(barPct(future, now)).toBe(runBarPct(0));
    expect(runBarPct(Number.NaN)).toBe(runBarPct(0));
  });

  it('is visibly wider a minute in than a second in', () => {
    const now = Date.parse('2026-10-06T12:00:00Z');
    const second = barPct('2026-10-06T11:59:59+00:00', now);
    const minute = barPct('2026-10-06T11:59:00+00:00', now);
    expect(minute).toBeGreaterThan(second + 1);
  });

  it('settles over about four hundred milliseconds', () => {
    expect(SETTLE_MS).toBe(400);
  });
});
