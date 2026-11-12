// One motion vocabulary: only what kew is EXECUTING moves (#2305).
//
// The console's animation used to be decided per surface, and the surfaces
// disagreed. `lib/epic/metro.ts` called an edge live when its target was
// `in_progress` or `in_review` — but `in_review` is a published pull request
// waiting for a PERSON, where nothing is running — and it had never heard of
// `adversarial_review` or `remediating`, the two statuses where kew's own
// reviewer is the thing executing. So the cyan went to work that had stopped
// and skipped the work that had not.
//
// This module is the browser half of `kew/src/kew/motion.py`: the same table,
// keyed by the same status words, answering the same question. The backend
// serves `motion` and `alive` on the run, loop and node DTOs; this table is
// what a surface uses when it holds a status word and no DTO (an edge's target,
// a fixture, a replayed tick from a server that predates the field), and it is
// what `kew/tests/test_motion_parity.py` holds the two sides to.
//
// Four words. The whole animation budget rides on the first:
//
//  * `executing`     — kew is running code against this now. ONLY these move.
//  * `waiting_kew`   — stopped, and kew's own turn (it will act, unasked).
//  * `waiting_human` — stopped, and a person is the next move.
//  * `settled`       — terminal. Nothing more happens here.
//
// The motion RULES this vocabulary feeds, from Dagster's live timelines kept
// quieter (an idea taken from its behaviour, re-implemented here — no code was
// ported, so there is nothing to attribute):
//
//  * only `executing` items move: a cyan halo on the status dot and a running
//    bar whose width grows;
//  * queued items never pulse — Dagster pulses them and kew does not;
//  * the clock ticks once a second and bars `transition: width 1s linear`, so
//    a bar slides between ticks instead of stepping;
//  * the clock stops when nothing executes or the stream drops (`clock.svelte.ts`);
//  * past windows never redraw: a card that is not executing is drawn against
//    the frame's own arrival, never against the ticking clock;
//  * history never moves: a REPLAYED frame animates nothing at all, however
//    `executing` its nodes were at the tick, and is read against the tick's own
//    instant rather than the wall clock (DagView's `frameNow`);
//  * hovering pauses the clock, so a mark never slides out from under a pointer;
//  * a node that finishes settles to done over `SETTLE_MS`;
//  * `prefers-reduced-motion` switches all of it off — the halo, the bar's
//    transition and the clock itself.

/** What a thing is doing, in the only four words a surface may animate from. */
export type Motion = 'executing' | 'waiting_kew' | 'waiting_human' | 'settled';

/**
 * The motion of a status word this table has never heard of.
 *
 * `waiting_kew` and not `executing` (a status kew ships later must not earn a
 * halo by default), and not `settled` either — that would claim the work is
 * over, and report it as not alive while it is still in flight. "Stopped,
 * kew's turn" is the honest degradation: nothing moves, nothing is declared
 * finished. `motion.py` falls back to the same word.
 */
export const FALLBACK_MOTION: Motion = 'waiting_kew';

/**
 * Status word -> motion, for every word the node, run and loop vocabularies can
 * serve. One table, three vocabularies; `awaiting_publication` belongs to two
 * of them and means the same thing in both.
 *
 * `pending` (a run) and `pursuing` (a loop) are the two entries the server
 * REFINES before it serves them: a pending run whose owner is provably dead,
 * and a pursuing loop nothing has advanced for longer than an iteration can
 * take, are not executing. The refinement needs facts only the server has, so a
 * surface that has the DTO must prefer its `motion` field over this table.
 *
 * `queued` is the one word two vocabularies spell the same and read oppositely;
 * this table holds the NODE reading and `RUN_MOTION_OF` the run one.
 *
 * Kept as a literal object the Python parity test can read line by line — a
 * computed table would leave it nothing to compare against.
 */
export const MOTION_OF: Record<string, Motion> = {
  // --- node: DriverStatus (kew/graph/driver/overlay.py), all 14 ------------
  // Undispatched work kew takes off its own frontier: nobody has to act.
  todo: 'waiting_kew',
  // Not armed — a PERSON adds the marker the adapter looks for (#2209). A
  // RUN's `queued` is this console's own display word and means the opposite;
  // see `RUN_MOTION_OF`.
  queued: 'waiting_human',
  in_progress: 'executing',
  // The run is over and kew owes it a branch and a pull request.
  awaiting_publication: 'waiting_kew',
  awaiting_continuation: 'waiting_human',
  // A pull request open for review: the status this whole module exists to
  // stop the console drawing as live.
  in_review: 'waiting_human',
  // kew's fresh-context reviewer, running against the recorded head.
  adversarial_review: 'executing',
  // kew acting on that reviewer's findings.
  remediating: 'executing',
  awaiting_no_change: 'waiting_human',
  done: 'settled',
  // A human's label, and only a human clears it. A dependency wait is `todo`.
  blocked: 'waiting_human',
  // Stopped work; `kew epic retry` is the only way on.
  failed: 'waiting_human',
  cancelled: 'settled',
  held: 'waiting_human',
  // --- run: RunStatus (kew/run_store.py), all 9 ---------------------------
  pending: 'executing',
  success: 'settled',
  failure: 'settled',
  refused: 'settled',
  budget_exceeded: 'settled',
  budget_stopped: 'settled',
  budget_stop: 'settled',
  killed: 'settled',
  // --- the console's own display words for a run (#2050, kpi.ts) ----------
  // Not RunStatus members: `displayStatus` refines a stored row into these
  // before a surface sees it, so the Monitor's live-run table holds `running`
  // where the store holds `pending`. Without them the console would read its
  // own vocabulary as unknown and leave its live rows still.
  running: 'executing',
  working: 'executing',
  idle: 'waiting_kew',
  flawed: 'settled',
  manual: 'waiting_human',
  // --- loop: LoopStatus (kew/run_store.py), all 6 -------------------------
  pursuing: 'executing',
  achieved: 'settled',
  unmet: 'settled',
  // The three resumable loop states: a person restarts each one.
  budget_limited: 'waiting_human',
  needs_human: 'waiting_human',
  paused: 'waiting_human'
};

/**
 * Which vocabulary a status word was handed over in.
 *
 * Only needed for a word two vocabularies spell the same and read differently,
 * which today is exactly one — see `RUN_MOTION_OF`. `'epic'` is the default,
 * and deliberately: it is the reading that names a PERSON as the next move, so
 * a caller that forgets to say never silently turns somebody's wait into kew's
 * own. `status.ts`'s `StatusVocabulary` splits the same word the same way.
 */
export type MotionVocabulary = 'epic' | 'run';

/**
 * The run vocabulary's reading of a word `MOTION_OF` maps for a node.
 *
 * Refinements only: every key here is also a key of `MOTION_OF`, and
 * `kew/tests/test_motion_parity.py` holds this table to `motion.py`'s copy and
 * to that rule, so a second collision cannot land unnoticed.
 *
 * `queued` is the collision. A queued NODE is work nobody has armed — the
 * Linear adapter reports it precisely when the marker a person adds is absent
 * (#2209) — so a person is the next move. A queued RUN is this console's own
 * display word (`displayStatus` in kpi.ts) for an untouched `pending` row with
 * no steps and no start time: the dispatcher picks it up on its own turn and
 * nobody has to touch it. Same word, opposite answer to "who moves next",
 * which is the only question this module asks.
 */
export const RUN_MOTION_OF: Record<string, Motion> = {
  queued: 'waiting_kew'
};

/** `in-progress`, `In Progress` and `in_progress` are one status. */
function normalize(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

/**
 * Own-property lookup, because the table above is an object literal:
 * `MOTION_OF['constructor']` otherwise answers with something that is not
 * `undefined` and walks straight past the fallback. The status word comes off
 * the wire, and `__proto__` survives `normalize` unchanged.
 */
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/**
 * The motion of one status word, by the tables alone. Never throws: an
 * unrecognised or missing word degrades to {@link FALLBACK_MOTION}.
 *
 * Pass `vocabulary` when the word was handed over as a RUN's status; it changes
 * only the words the two vocabularies spell the same and read differently
 * (`RUN_MOTION_OF`).
 *
 * This is the UNREFINED reading — see `MOTION_OF` on the two words a DTO's own
 * `motion` field answers better.
 */
export function motionOf(
  status: string | null | undefined,
  vocabulary: MotionVocabulary = 'epic'
): Motion {
  if (status == null) return FALLBACK_MOTION;
  const word = normalize(status);
  const refined = vocabulary === 'run' ? own(RUN_MOTION_OF, word) : undefined;
  return refined ?? own(MOTION_OF, word) ?? FALLBACK_MOTION;
}

/** Anything the server serves with a status: a run, a loop, an epic node. */
export type Moving = { motion?: string | null; status?: string | null };

/**
 * One thing's motion: the SERVER's reading when it served one, else the table's.
 *
 * The server's wins because it knows the two facts no status word carries — a
 * pending run's owner liveness and a pursuing loop's staleness — and a surface
 * that second-guessed it would animate an orphan forever. The fallback is for a
 * payload with no `motion` at all: a replayed tick, a hand-made fixture, or a
 * server older than #2305.
 *
 * A word the server serves that this build has never heard of is passed
 * through: it came from the one table both sides are held to
 * (`kew/tests/test_motion_parity.py`), and nothing animates on a word that is
 * not `executing` anyway.
 *
 * `vocabulary` reaches the fallback only: say `'run'` for a run's status —
 * including the display-only words this console invents for one — so a queued
 * run reads as the dispatcher's turn rather than a person's.
 */
export function servedMotion(thing: Moving, vocabulary: MotionVocabulary = 'epic'): Motion {
  const served = thing.motion;
  if (served != null && served !== '') return served as Motion;
  return motionOf(thing.status, vocabulary);
}

/** Whether this thing is moving, and so may animate. */
export function isExecuting(thing: Moving, vocabulary: MotionVocabulary = 'epic'): boolean {
  return servedMotion(thing, vocabulary) === 'executing';
}

/** Whether this is an open concern rather than history. */
export function isAlive(thing: Moving, vocabulary: MotionVocabulary = 'epic'): boolean {
  return servedMotion(thing, vocabulary) !== 'settled';
}

/** How long a finished item takes to settle to done. */
export const SETTLE_MS = 400;

/** How wide the running bar can ever get, in percent of its track. */
const BAR_CEILING = 96;

/**
 * Where the running bar starts, in percent of its track.
 *
 * Not zero: a card that IS executing must look like it, and a dispatch kew has
 * no start time for ("status_since" absent, a hand-made snapshot) would
 * otherwise draw a zero-width bar — indistinguishable from the card having no
 * bar at all. The floor says "running, length unknown"; the halo beside it is
 * what moves.
 */
const BAR_FLOOR = 3;

/**
 * Time it takes the running bar to cover about 63% of its track.
 *
 * Forty-five minutes, which is the shape of a dispatch rather than a round
 * number: a bar that saturates early is a bar that stops saying anything, and
 * an agent run is capped at `AgentConfig.timeout_minutes` (120 by default). At
 * this rate a minute in reads ~2%, ten minutes ~19%, an hour ~73%, and the
 * two-hour cap is still short of the ceiling — so the bar is visibly growing
 * for the whole of any run kew allows.
 */
const BAR_TAU_MS = 45 * 60 * 1000;

/**
 * How far along the running bar is after `elapsedMs` of execution.
 *
 * NOT a progress bar: kew cannot know how far through its work an agent is, and
 * a bar that implied it would be inventing the one number the operator most
 * wants. It is an asymptote — `1 - e^(-t/tau)` between `BAR_FLOOR` and
 * `BAR_CEILING` — so it always grows, never arrives, and never claims a
 * completion it has not got.
 *
 * A negative or unreadable elapsed time is the floor: a clock skew between
 * server and browser must not draw a bar backwards.
 */
export function runBarPct(elapsedMs: number): number {
  if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return BAR_FLOOR;
  return BAR_FLOOR + (BAR_CEILING - BAR_FLOOR) * (1 - Math.exp(-elapsedMs / BAR_TAU_MS));
}

/**
 * The running bar's width for something that started at `since`, read at `now`.
 *
 * `since` is a DTO's `status_since` — an ISO-8601 instant carrying its offset
 * (`UtcIso` in kew/service/models.py). One it cannot parse, or none at all, is
 * the floor rather than a guess.
 */
export function barPct(since: string | null | undefined, now: number): number {
  if (since == null || since === '') return BAR_FLOOR;
  const started = Date.parse(since);
  if (Number.isNaN(started)) return BAR_FLOOR;
  return runBarPct(now - started);
}
