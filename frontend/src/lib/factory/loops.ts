/**
 * FUI-27 — the two readings the loops page derives from a `LoopStatus`.
 *
 * Both used to be inline in `loops/+page.svelte`, and both were wrong there
 * (#2157 review). The gate line fell through to "gate pending" for every status
 * it did not name, so `unmet` — finished, in the page's own terminal set,
 * polling stopped — reported its verification as still in progress forever,
 * beside a StatusMark on the same row already reading cancelled; `budget_limited`
 * read the same way beside a held mark. And the budget block printed no spend
 * figure at all: the Gauge captions itself only when over cap, and its axis is
 * derived from the cap alone, so an under-cap loop said what it had spent
 * nowhere on the page.
 *
 * They live together because they are one invariant: a status this file calls
 * terminal must not read as pending, and `loops.test.ts` asserts exactly that
 * across `LOOP_TERMINAL` and `loopGate`. Kept pure for the same reason as
 * gauge.ts — the page's own block only renders after a fetch, so a server
 * render of the route never reaches it.
 */
import { capBarState } from '$lib/cap-bar';
import { gaugeMoney } from './gauge';

/**
 * TRUE terminal loop states: the poll stops once one is observed.
 *
 * The exclusion rule is resumability, and `_RESUMABLE` in kew/loops/delegate.py
 * is its authority: every word in that frozenset is deliberately absent here,
 * because an operator can restart the loop and the page has to be still polling
 * to catch the transition back to `pursuing`. `needs_human` and `paused` are the
 * obvious two; `budget_limited` is the third and was wrongly listed as terminal
 * until the #2157 review — `kew loop resume --budget` bumps the cap and puts the
 * loop back in flight, and the page had permanently disarmed its 4s poll for it.
 * `kew/tests/test_factory_status_parity.py` holds this set to `_RESUMABLE` so
 * the two cannot drift again.
 */
export const LOOP_TERMINAL: ReadonlySet<string> = new Set(['achieved', 'unmet']);

/**
 * The evidence chain's four words (FUI-12), which is all a gate reading can be.
 *
 * These are status WORDS, not `StatusMeaning`s — `MEANING_OF` in status.ts maps
 * each to the mark it draws, and `loops.test.ts` asserts all four are words that
 * table knows, so a gate reading can never reach the unknown-status fallback.
 */
export type LoopGateKind = 'attested' | 'pending' | 'missing' | 'unverified';

export type LoopGate = {
  kind: LoopGateKind;
  /** The word beside the mark, in the gate's own vocabulary. */
  word: string;
};

/**
 * The verification gate per loop status, in the evidence vocabulary (FUI-04).
 *
 * Read off what `DelegateDriver._decide` (kew/loops/delegate.py) actually did
 * before it stamped each status:
 *
 * - `achieved` — the gate evaluated and passed. The one attested reading.
 * - `needs_human` — the gate evaluated and did not pass, or `_pre_gate_status`
 *   short-circuited on a failed run. A promised guarantee that never arrived.
 * - `unmet` — the loop is over and the goal was never met: same missing
 *   guarantee, with nobody left to ask about it.
 * - `budget_limited` — `_ERROR_STATUS` stopped the run at its cap BEFORE the
 *   gate, so there is work recorded and no verdict on it: `unverified`, the
 *   warn dashed ring, matching the hold its StatusMark already paints. Not the
 *   danger X — a cap is a stop, not a defect (status.ts says the same).
 * - `pursuing` / `paused` — the gate is still ahead. The only pending readings.
 */
const LOOP_GATE: Record<string, LoopGate> = {
  achieved: { kind: 'attested', word: 'gate passed' },
  needs_human: { kind: 'missing', word: 'gate failed' },
  unmet: { kind: 'missing', word: 'gate unmet' },
  budget_limited: { kind: 'unverified', word: 'gate not reached' },
  pursuing: { kind: 'pending', word: 'gate pending' },
  paused: { kind: 'pending', word: 'gate pending' }
};

/**
 * The gate reading for a loop status.
 *
 * An unrecognised status degrades to pending, the same forward-compatible
 * honesty as `statusShape`'s todo ring: a state kew has not shipped reads as
 * "not here yet" rather than inventing a verdict.
 * `kew/tests/test_factory_status_parity.py` fails if `LoopStatus` grows a word
 * this table does not have, so no SHIPPED state can reach that fallback.
 */
export function loopGate(status: string | null | undefined): LoopGate {
  const key = status == null ? '' : status.trim().toLowerCase();
  return (
    (Object.hasOwn(LOOP_GATE, key) ? LOOP_GATE[key] : undefined) ?? {
      kind: 'pending',
      word: 'gate pending'
    }
  );
}

export type LoopBudget = {
  /** What the loop has spent, always printed — the Gauge does not print it. */
  figure: string;
  /** The denominator, or "no cap" when there is no usable one, as CapBar said it. */
  capLabel: string;
  over: boolean;
};

/**
 * The numeric spend reading that sits above the gauge.
 *
 * Money is `gaugeMoney`, not the page's 2dp `fmtUSD`, so the figure and the
 * axis tick under it spell the same cap the same way ("$5" / "cap $5"), the way
 * the DMonitor spend readouts do.
 */
export function loopBudget(spent: number, cap: number | null | undefined): LoopBudget {
  const state = capBarState(spent, cap);
  return {
    figure: gaugeMoney(spent),
    capLabel: state.pct == null ? 'no cap' : gaugeMoney(cap as number),
    over: state.over
  };
}
