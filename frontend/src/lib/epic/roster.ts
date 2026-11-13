/**
 * #2206 — the Epics list's two shelves, and the order within each.
 *
 * An epic has no end. `complete` is a reading the driver can take back the
 * moment a merged PR reopens something, and an abandoned epic never reaches a
 * terminal signal at all — so the roster only ever grows. Sixty-six of them,
 * most last ticked two months ago, read as one undifferentiated list sorted by
 * ref: an epic that finished in July sat above one that ticked a minute ago.
 *
 * So the list has two shelves. **Active** is what a person might still act on.
 * **Finished / Archived** is what nobody will: a `complete` drain, or one an
 * operator retired with `kew epic archive` — the same decision the backend
 * reads off the audit chain to drop the epic from the needs-you count and from
 * `kew epic run --all` (kew/src/kew/epic_archive.py). This module is the UI's
 * one owner of that reading, so the page cannot draw a different line than the
 * one the console counts by.
 *
 * #2283 widens it from the shelves to the Epics LIST's own readings — the
 * three the design review's D16 and D17 moved out of the page: whether a
 * signal's live claim is earned, where a needs-you mark links, and which
 * counts are worth a mark. They live beside the shelf split because they are
 * the same kind of thing — a reading of the roster, printed by one page and
 * therefore testable nowhere else.
 */
import type { EpicTickStrip } from '$lib/api';
import { heartbeatFromTickLog } from '$lib/factory/tick-strip';
import { signalTone } from './progress';
import { freshBeat } from './staleness';
import { epicHref } from '$lib/work/refs';

/** The fields the split and the sort read. A superset of this is fine. */
export type RosterEpic = {
  epic_ref: string;
  signal: string | null;
  last_tick_ts: string | null;
  /** Absent from a `kew serve` that predates #2206 — read as "not archived". */
  archived?: boolean;
};

/**
 * The driver signals that mean an epic is finished of its own accord. Mirrors
 * `TERMINAL_SIGNALS` in kew/src/kew/epic_archive.py, which is the set that
 * already keeps a completed epic out of the judgment queue (and, through
 * `epic_is_retired`, out of its own needs-you legend) — the shelf and the count
 * have to agree about which epics are done.
 */
export const TERMINAL_SIGNALS: readonly string[] = ['complete'];

/**
 * Whether an epic belongs on the Finished / Archived shelf.
 *
 * `stalled` and `awaiting_merges` deliberately stay ACTIVE: both are states a
 * person is expected to act on, and shelving them is exactly the quiet
 * disappearance `kew epic archive` exists to make deliberate instead.
 */
export function isRetired(epic: RosterEpic): boolean {
  return epic.archived === true || (epic.signal != null && TERMINAL_SIGNALS.includes(epic.signal));
}

/**
 * When an epic last ticked, as a sortable number — or `null` for an epic that
 * has never ticked, or whose timestamp will not parse.
 *
 * Both of those are "no reading", not "ticked at the epoch": coercing them to 0
 * would rank a never-driven epic below every real one as if that were a
 * measurement, and letting `NaN` into the comparator would make it
 * non-transitive and the sort order arbitrary.
 */
function tickedAt(epic: RosterEpic): number | null {
  if (!epic.last_tick_ts) return null;
  const ms = Date.parse(epic.last_tick_ts);
  return Number.isNaN(ms) ? null : ms;
}

/** Newest tick first; epics with no tick after them; ties broken by ref. */
function byLastTick(a: RosterEpic, b: RosterEpic): number {
  const left = tickedAt(a);
  const right = tickedAt(b);
  if (left !== right) {
    if (left === null) return 1;
    if (right === null) return -1;
    return right - left;
  }
  return a.epic_ref.localeCompare(b.epic_ref);
}

/**
 * The roster, split and sorted. Returns new arrays — the caller's list is the
 * poll's own payload and is re-read on the next tick, so sorting it in place
 * would reorder state another reader still holds.
 */
export function epicRoster<T extends RosterEpic>(
  epics: readonly T[]
): { active: T[]; finished: T[] } {
  const active: T[] = [];
  const finished: T[] = [];
  for (const epic of epics) (isRetired(epic) ? finished : active).push(epic);
  return { active: active.sort(byLastTick), finished: finished.sort(byLastTick) };
}

// --- the row's own readings (#2283) ----------------------------------------

/**
 * The fields a liveness reading is taken from. `EpicSummary` satisfies it.
 *
 * `interval_s` is the cadence `/api/epics` thresholded its own reading against
 * — a poller's `--interval`, else the operator's configured expectation for an
 * externally-driven epic — and `null` is a real answer: a drive with neither
 * has no cadence at all.
 *
 * `last_tick_started_at` is optional because a `kew serve` predating #2283
 * does not serve it on the list; see `heartbeatAt`. The last three are the
 * rest of the heartbeat the epic page reads (#2844 N18) — the last tick's
 * duration, which widens the window, and the newest tick ATTEMPT's instants —
 * optional because a `kew serve` predating #2844 serves none of them.
 */
export type HeartbeatEpic = {
  signal: string | null;
  last_tick_ts: string | null;
  last_tick_started_at?: string | null;
  last_tick_duration_s?: number | null;
  last_attempt_started_at?: string | null;
  last_attempt_finished_at?: string | null;
  last_attempt_outcome?: string | null;
  last_attempt_reached_tick?: boolean;
  interval_s: number | null;
};

/** The wording for a signal whose live claim could not be earned. DESIGN.md
 * writes it out: "show the signal in `{colors.text-3}` and say 'no
 * heartbeat'". One constant, so the page and its test cannot drift. */
export const NO_HEARTBEAT = 'no heartbeat';

/**
 * Which stamp the liveness reading is taken from: the tick's START, falling
 * back to its finish.
 *
 * `last_tick_started_at` is written before a tick does any work;
 * `last_tick_ts` only lands when the tick COMPLETES. Thresholding the finish
 * stamp calls a driver quiet for exactly as long as it is busy — a tick that
 * runs longer than ONE interval (a drive that dispatched three nodes, a slow
 * review) already ages the finish stamp out of the window, so the row drew
 * `no heartbeat` while `driver_liveness` (kew/src/kew/service/epics.py)
 * called the same driver live off the start stamp.
 *
 * The fallback is for a `kew serve` predating #2283, which serves no start
 * stamp on the list at all: the finish stamp is a worse reading of the same
 * thing, not a different question, and is strictly better than withdrawing
 * every live claim on an older server.
 */
function heartbeatAt(epic: HeartbeatEpic): string | null {
  return epic.last_tick_started_at || epic.last_tick_ts;
}

/**
 * The newest tick attempt the summary serves, as the one-row tick log
 * `heartbeatFromTickLog` reads — or none, from a `kew serve` predating #2844.
 * Only its two instants, its outcome (#2882: a refused attempt is no beat) and
 * whether it reached its tick (#2882: an open attempt still before the lock or
 * the preflight is no beat either) are read; the rest is not a reading of
 * anything.
 */
export function summaryAttempt(epic: HeartbeatEpic): EpicTickStrip[] {
  if (!epic.last_attempt_started_at) return [];
  return [
    {
      id: 0,
      ts_started: epic.last_attempt_started_at,
      ts_finished: epic.last_attempt_finished_at ?? null,
      outcome: epic.last_attempt_outcome ?? null,
      signal: null,
      n_dispatched: 0,
      n_merged: 0,
      n_held: 0,
      n_stuck: 0,
      journal_tick_id: null,
      error_class: null,
      reached_tick: epic.last_attempt_reached_tick === true
    }
  ];
}

/**
 * #2844 N18 — a row's heartbeat, read the way the epic page reads its own.
 *
 * The page takes its driver's heartbeat through `heartbeatFromTickLog`: where
 * the tick log holds a newer attempt than the driver state states, that
 * attempt's start is the heartbeat, and its run time the duration that widens
 * the window. The row goes through the same function over the newest attempt
 * the SUMMARY serves — not over the row's strip, which the list fetches for
 * at most `STRIP_ROW_CAP` rows a shelf, so row 21 read "no heartbeat" beside a
 * page that read live. Everything else on the row is the summary's own.
 */
export function epicWithTickLog<T extends HeartbeatEpic>(epic: T): T {
  const log = summaryAttempt(epic);
  if (log.length === 0) return epic;
  const started = heartbeatAt(epic);
  const read = heartbeatFromTickLog(
    {
      last_tick_started_at: started,
      last_tick_ts: epic.last_tick_ts,
      last_tick_duration_s: epic.last_tick_duration_s ?? null,
      tick_count: 0,
      interval_s: epic.interval_s,
      last_signal: epic.signal,
      change_count: 0
    },
    log
  );
  if (read === null || read.last_tick_started_at === started) return epic;
  return {
    ...epic,
    last_tick_started_at: read.last_tick_started_at,
    last_tick_duration_s: read.last_tick_duration_s
  };
}

/**
 * The heartbeat word a surface states beside a signal (#2844 N18): `live` where
 * the signal claims live and a fresh tick backs it, `no heartbeat` where it
 * claims live without one, and null for a signal that never claims live. The
 * Epics row and the epic page both carry it, so the two can be compared.
 */
export function heartbeatWord(
  signal: string | null | undefined,
  fresh: boolean
): 'live' | typeof NO_HEARTBEAT | null {
  if (!signalTone(signal).halo) return null;
  return fresh ? 'live' : NO_HEARTBEAT;
}

/**
 * Whether this epic ticked recently enough to be called alive.
 *
 * Four ways to be not-fresh, and all of them mean the same thing here: never
 * ticked, ticked outside the window, has no window to be fresh in (a one-shot
 * drive, or a cadence that is not a duration), or carries a timestamp that
 * will not parse. "We cannot tell" is not "alive" — which is the whole of
 * D16 — and the row says which one it is in its own Last tick column.
 *
 * The window is the epic page's own (`freshBeat`, #2844 N18): max(3x interval,
 * interval + 2x the last tick's duration). The row held DESIGN.md's flat 2x
 * before, and a tick 2.5 intervals old read "no heartbeat" here and live on
 * the page.
 *
 * A tick from the FUTURE is fresh. A clock skewed between the driver's host
 * and this browser is not a dead driver, and `no heartbeat` printed over a
 * tick that has only just landed is the same lie pointing the other way.
 */
export function heartbeatFresh(epic: HeartbeatEpic, nowMs: number): boolean {
  const beat = heartbeatAt(epic);
  if (!beat) return false;
  if (epic.interval_s == null || epic.interval_s <= 0) return false;
  const ticked = Date.parse(beat);
  if (Number.isNaN(ticked)) return false;
  return freshBeat((nowMs - ticked) / 1000, epic.interval_s, epic.last_tick_duration_s);
}

/**
 * How a row draws its signal: the shared tone, with the LIVE claim withdrawn
 * from an epic whose heartbeat cannot back it up (#2283 D16).
 *
 * The cyan halo is the console's one "this is happening now" (DESIGN.md:
 * "Cyan = live"), and `signalTone` spends it on exactly one word —
 * `progressing`. This list drew that halo on three epics whose Last tick
 * column, two cells to the right, said "no ticks yet": the halo was a reading
 * of the driver's last VERDICT, and a verdict is not a pulse.
 *
 * Only a signal that claims live can lose the claim. `complete`, `stalled`
 * and a word this console does not know draw no halo in the first place, so
 * there is nothing to withdraw and nothing to confess — "no heartbeat" beside
 * a July epic on the Finished shelf would be an alarm about a drain that
 * ended on purpose.
 */
export function signalReading(
  epic: HeartbeatEpic,
  nowMs: number,
  personNext: boolean = false
): { color: string; halo: boolean; note: string | null } {
  const tone = signalTone(epic.signal, personNext);
  if (!tone.halo || heartbeatFresh(epic, nowMs)) return { ...tone, note: null };
  return { color: 'var(--text-3)', halo: false, note: NO_HEARTBEAT };
}

/** The note a stalled epic with nobody waiting and a fresh heartbeat carries. */
export const NOTHING_READY = 'nothing ready';

/**
 * #3054 N14 — why a stalled epic is stalled, and what would move it.
 *
 * "stalled" read amber on one epic and magenta on another with nothing to say
 * why, or what anyone could do. The colour already follows whether a person is
 * next (`signalTone`); this is the words that go with it. Three reasons:
 *
 *   - people are waiting: "10 need you" — the magenta one, a person is next;
 *   - the driver is not beating: "no heartbeat" — restarting it moves it;
 *   - neither: "nothing ready" — no node is ready and none is in flight, so a
 *     dependency landing or a node being released moves it.
 *
 * `null` for any other signal: only "stalled" was bare.
 */
export function stalledReason(
  epic: HeartbeatEpic & { epic_ref: string },
  nowMs: number,
  needsYou: number
): { note: string; hint: string; personNext: boolean } | null {
  if (epic.signal !== 'stalled') return null;
  if (needsYou > 0) {
    return {
      note: `${needsYou} need you`,
      hint: 'stalled until a person answers what is waiting — the Needs you link opens it',
      personNext: true
    };
  }
  if (!heartbeatFresh(epic, nowMs)) {
    return {
      note: NO_HEARTBEAT,
      hint: `stalled with no driver beating — kew epic run ${epic.epic_ref} starts it again`,
      personNext: false
    };
  }
  return {
    note: NOTHING_READY,
    hint: 'stalled with no node ready and none in flight — a dependency landing or a held node released moves it',
    personNext: false
  };
}

/** One entry of the served needs-you set, as a row reads it. `DashboardRow`
 * satisfies it; the set's own fields are `item_id` and `epic_ref`
 * (`needs_you_set` in kew/src/kew/service/needs_you.py). */
export type NeedsYouPlace = { item_id?: string | null; epic_ref?: string | null };

/**
 * Where a row's needs-you mark goes (#2283 D17).
 *
 * The finding was that the Epics list leads with nothing a person can act on,
 * and a count that names no destination is half of that defect: the operator
 * reads "1 needs you" and then has to find which node. So the mark is a link,
 * and `/epics/<ref>?node=<id>` is the in-app "show me the node" the Work
 * detail pane already uses (`$lib/work/entry-detail`).
 *
 * A bucket naming ONE node links to it. Anything else links to the epic:
 * `?node=` selects a single node, so picking the first of three would be an
 * arbitrary answer to "which one needs me", and an epic-wide wait — a stalled
 * driver, an orphan PR, a CI hold — is served with no item at all and has no
 * node to go to. The graph answers both honestly.
 */
export function needsYouHref(epicRef: string, rows: readonly NeedsYouPlace[] | undefined): string {
  const items = new Set(
    (rows ?? [])
      .filter((row) => (row.epic_ref ?? '') === epicRef)
      .map((row) => (row.item_id ?? '').trim())
      .filter((item) => item !== '')
  );
  const base = epicHref(epicRef);
  if (items.size !== 1) return base;
  const [item] = [...items];
  return `${base}?node=${encodeURIComponent(item)}`;
}

/**
 * The legend entries worth a mark (#2283 D17, and DESIGN.md: "Show only
 * non-zero counts; a zero is noise in a legend or a list").
 *
 * Nine marks per row, five of them zero, is what the review photographed:
 * forty-five marks on a five-epic list carrying the information of four.
 * `epicProgress` still counts every status the payload serves — the done/total
 * figure beside these marks is over all of them — so nothing is lost but the
 * noise. Order is the legend's own (the payload's), never re-ranked here.
 */
export function nonZeroCounts<T extends { count: number }>(legend: readonly T[]): T[] {
  return legend.filter((entry) => entry.count > 0);
}
