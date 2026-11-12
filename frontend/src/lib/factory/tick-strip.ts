/**
 * #2304 — the tick strip: what an epic driver (or a loop) has actually been
 * doing, one slot per attempt.
 *
 * `epic_ticks` is a change journal, so the console's only reading of a driver
 * was the ticks that CHANGED something — about one in nine. A driver that ran
 * 476 ticks showed 18, and the attempts that never reached the journal at all
 * (the raise, the lock-out, the empty graph) showed nothing but a line on
 * somebody's stderr. `epic_tick_log` records every attempt, and this module is
 * how a strip of them is read.
 *
 * Four decisions live here, and all four are the kind that no screenshot
 * catches:
 *
 * 1. **Fixed slots, padded on the OLD side.** A strip is `slots` wide whether
 *    the epic has ticked twice or two hundred times, and the newest tick is
 *    always the rightmost slot. Padding the young side instead would put each
 *    row's "now" at a different x, and a column of strips that do not share an
 *    edge cannot be compared down the page — which is the entire reason to put
 *    one on every row.
 * 2. **Cyan is rationed to the live tick.** DESIGN.md: cyan is the only "live"
 *    colour. An in-progress tick is the one slot that has it, and the only one
 *    that grows; a finished tick that merely *did something* takes its shape
 *    and colour from `STATUS_MARKS` like every other state in this console, and
 *    a quiet tick is `--text-5` and no shape at all.
 * 3. **An unknown outcome is quiet, never loud.** A row written by a build this
 *    one has never heard of must not be able to colour a strip. Tolerating a
 *    bad historical row is the operator rule — the row is read as it was
 *    stored, never rewritten.
 * 4. **"Running" is a bounded claim, and the row is not the only evidence.**
 *    An open row means a tick in progress, but only for as long as the driver
 *    that opened it could still be alive. A driver killed mid-tick closes
 *    nothing, and an unbounded reading of its row is a strip that claims a
 *    tick is running for the rest of the epic's life — so the row alone is
 *    read as live for `TICK_LIVE_MAX_MS`. Past that the question belongs to
 *    the server's own liveness reading (`DRIVER_LIVE`), which thresholds the
 *    heartbeat the driver keeps beating MID-tick: a long tick is a supported
 *    case, and a floor on the tick's start cannot be allowed to call one
 *    abandoned on its own.
 *
 * The severity mapping feeds `$lib/time/batch`, so the full strip on the epic
 * page batches overlapping ticks exactly as the Gantt and the event strip do.
 * Nothing here draws: `TickStrip.svelte` turns these slots into marks.
 */
import type { EpicHeartbeat, EpicTickStrip, Iteration } from '$lib/api';
import { STATUS_MARKS, statusShape, type StatusMeaning } from '$lib/factory/status';
import type { AxisWindow } from '$lib/time/axis';
import {
  MIN_MARK_W,
  batchSeverity,
  severityRank,
  type BatchMark,
  type BatchSeverity
} from '$lib/time/batch';

/**
 * How many slots a compact row strip reserves.
 *
 * Sixteen, because the epics roster's strip column is 112px and a slot is 7px
 * — the two numbers have to multiply out, or the strip overflows the cell it
 * was padded to fit. The loops sidebar passes its own narrower count.
 */
export const COMPACT_SLOTS = 16;

/**
 * The full strip's total width, in px: `TickTimeline.svelte`'s own default, and
 * the number the pad, the drawable lane and the attempt limit below are all
 * read off. None of those may drift from it, so they live together.
 */
export const FULL_STRIP_WIDTH = 760;

/**
 * The pixels a full strip keeps clear to the RIGHT of its newest attempt.
 *
 * The lane covers the attempts it was given and nothing wider, so with no pad
 * the newest instant lands exactly on x = `width` — and a mark with no
 * DURATION is then drawn from that edge outwards, into the clip of a
 * `width`-px SVG. A tick still running is precisely that mark: it has a start
 * stamp and nothing else (see `tickMarks`), so the live cyan bar — the one bar
 * a reader is looking for — drew zero visible pixels for exactly as long as a
 * tick was in flight.
 *
 * `MIN_MARK_W` and not a rounder number: it is the narrowest `$lib/time/batch`
 * will draw, so it is exactly the room such a mark needs and the least the
 * drawable lane has to give up for it. Reserved by solving for the window
 * rather than by widening the canvas past the width the caller sized, which is
 * how `epic/EpicTimeline.svelte` holds its own pads open (`FIT_PAD_PX`).
 */
export const FULL_STRIP_RIGHT_PAD = MIN_MARK_W;

/** The pixels of a full strip that marks are actually placed across, at the
 *  design width — a strip fitted to a narrower box has that much less
 *  (`fitStripWidth`), and `stripLimit` is the same subtraction at any width. */
export const FULL_STRIP_LANE_W = FULL_STRIP_WIDTH - FULL_STRIP_RIGHT_PAD;

/**
 * The width a full strip may draw at inside a box of `available` px (#2473).
 *
 * `FULL_STRIP_WIDTH` is the width the design draws at, and on a phone it is
 * more than twice the box it sits in: the epic page's header strip is the
 * window less its own gutters — ~310px at 390 — and the column it sits in clips
 * the x axis rather than scrolling it. A 760px strip in a 310px clip loses
 * 450px of itself, and because this strip puts its NEWEST attempt against the
 * right edge (the pad `stripLane` solves for), what the clip takes is exactly
 * the recent ticks somebody opened the page to read, while the oldest ones keep
 * their pixels.
 *
 * So the strip is fitted to the box rather than anchored to its newest end.
 * Anchoring the other way would keep the resolution and push the oldest
 * attempts out of the clip instead — but a bar half outside a clip is a bar
 * whose hover target and popover are half outside it too. What the fitted strip
 * then shows is `stripLimit` attempts of history rather than all of them: the
 * width is only half the geometry, and the other half is below.
 *
 * `available <= 0` is "not measured yet" — a `clientWidth` binding reads 0
 * until its box has been laid out — and reads as the design width: right on
 * every viewport where the strip already fitted, and one frame of the old clip
 * on the ones where it did not.
 */
export function fitStripWidth(available: number): number {
  if (!Number.isFinite(available) || available <= 0) return FULL_STRIP_WIDTH;
  return Math.min(FULL_STRIP_WIDTH, Math.floor(available));
}

/**
 * How many attempts a full strip `width` px wide can draw APART — so how many
 * the epic page asks the API for, and how many of those it hands the strip.
 *
 * Read OFF the lane rather than chosen: `$lib/time/batch` draws no mark
 * narrower than `MIN_MARK_W`, so a lane of `width - FULL_STRIP_RIGHT_PAD`
 * drawable px has room for that many marks side by side and no more. Ask for
 * more than that and the strip cannot draw them apart however the driver was
 * paced — every mark touches its neighbour, one sweep merges the lot, and the
 * full strip degenerates to a single bar that says "200 ticks" and nothing
 * else. The first 200 asked against a 760px lane was already past that floor
 * (PR #2438).
 *
 * A FUNCTION of the width, not a constant, because the strip no longer always
 * draws at 760px (`fitStripWidth`): the same floor bites the same way in the
 * fitted lane, and it bites as a cliff rather than as a squeeze. The merge
 * cascades — a mark pulled into a batch widens it, which pulls in the next —
 * so at the 310px box 77 evenly paced attempts keep 77 bars and 78 collapse
 * into ONE. Handing a fitted strip a count read off the 760px lane was
 * therefore not a tighter picture but no picture at all (review round 1 of
 * #2473).
 *
 * At least one attempt however narrow the box measures: a strip with nothing
 * in it is not a reading, and the page only draws this strip when it has rows.
 */
export function stripLimit(width: number): number {
  const lane = fitStripWidth(width) - FULL_STRIP_RIGHT_PAD;
  return Math.max(1, Math.floor(lane / MIN_MARK_W));
}

/**
 * How many attempts the strip asks the API for: `stripLimit` at the design
 * width, which is the most any viewport can draw apart.
 *
 * The read is sized once, off this, rather than re-fetched per measured width:
 * a narrower box draws the newest `stripLimit(width)` of what was read, so
 * widening the window (a rotation, a drag) re-adds the older attempts from
 * rows already in hand instead of going back to the server for them.
 */
export const FULL_STRIP_LIMIT = stripLimit(FULL_STRIP_WIDTH);

/**
 * How wide a lane with nothing to measure is opened to, so the axis has
 * something to divide: no attempts at all, one attempt, or a clock that did
 * not move between the first and the last of them all read as a minute.
 */
export const EMPTY_LANE_MS = 60_000;

/**
 * The window a full strip draws in: the attempts it was given, nothing wider,
 * with `FULL_STRIP_RIGHT_PAD` px held clear past the newest one.
 *
 * Here rather than in a `$derived` in `TickTimeline.svelte` because it is
 * geometry, and geometry is the part that can be wrong in a way no screenshot
 * shows — the pad above is the whole reason this is a tested function.
 */
export function stripLane<T>(
  marks: readonly BatchMark<T>[],
  width: number = FULL_STRIP_WIDTH,
  now: number = Date.now()
): AxisWindow {
  const w = Math.max(1, width);
  const startMs = marks.length ? Math.min(...marks.map((m) => m.startMs)) : now - EMPTY_LANE_MS;
  const lastMs = marks.length ? Math.max(...marks.map((m) => m.endMs ?? m.startMs)) : now;
  const measured = lastMs > startMs ? lastMs - startMs : EMPTY_LANE_MS;
  // Solve for the window whose own right pad comes out at the pad above: the
  // measured span has to land inside `w - pad`, so the window is that span
  // scaled up by the fraction of the lane the pad takes.
  const frac = Math.min(0.5, FULL_STRIP_RIGHT_PAD / w);
  return { startMs, endMs: startMs + measured / (1 - frac), width: w };
}

/**
 * Outcome -> the meaning whose `STATUS_MARKS` shape and colour the slot wears.
 *
 * `dispatched` and `merged` are both the green dot: this tick moved the epic
 * forward, and WHICH way it moved is the popover's job, not a colour's — the
 * console has four meanings to spend and spending two of them on the
 * difference would leave none for the failure. `held` is the amber thick ring
 * (DESIGN.md: amber is money and held). `failed` is the red ×. `empty_graph`
 * is the OTHER red mark, the missing ring-×: the tracker query matched no work
 * at all, which is a real alarm but not an exception — see `EmptyEpicGraphError`
 * in kew/graph/driver/tick.py.
 *
 * Absent from the table — `nothing`, `locked_out`, `abandoned`, and any word a
 * later build invents — reads quiet. A locked-out tick is quiet because THIS
 * driver did nothing: another one holds the epic, and its own strip says what
 * it did. An abandoned one is quiet because nobody knows what it did: its
 * driver died mid-tick, and a colour would be a claim the row cannot support.
 */
const MEANING_OF_OUTCOME: Readonly<Record<string, StatusMeaning>> = {
  dispatched: 'done',
  merged: 'done',
  held: 'held',
  failed: 'failed',
  empty_graph: 'missing'
};

/** The meaning an in-progress tick wears: the one cyan slot in a strip. */
export const LIVE_MEANING: StatusMeaning = 'working';

/** A quiet slot's colour. Not a mark — a rule in `--text-5`. */
export const QUIET_COLOR = 'var(--text-5)';

/**
 * A batch's colour by severity, read off the one status table rather than a
 * palette of the strip's own: `failed` is the danger ×'s colour, `attention`
 * the held ring's amber, `did` the done dot's green.
 */
const SEVERITY_COLOR: Readonly<Record<BatchSeverity, string>> = {
  failed: STATUS_MARKS.failed.color,
  attention: STATUS_MARKS.held.color,
  did: STATUS_MARKS.done.color,
  quiet: QUIET_COLOR
};

/**
 * The fill of one bar on the batched full strip: its batch's WORST member's
 * colour, with cyan reserved for a live batch that has nothing in it to act on.
 *
 * Live used to win outright, which let the running tick's cyan repaint whatever
 * it was batched with — and at the full strip's own width a day of attempts is
 * a handful of batches, so "batched with the running tick" is most of the lane.
 * A failure beside a live tick came out cyan, which is precisely the severity
 * smear batching exists to prevent (#2299). The bar's HEIGHT still says live;
 * only the colour defers to the worse fact, and the popover lists both.
 */
export const batchFill = (live: boolean, severity: BatchSeverity): string =>
  live && severityRank(severity) >= severityRank('did')
    ? STATUS_MARKS[LIVE_MEANING].color
    : SEVERITY_COLOR[severity];

/** An instant, in ms, from whichever of a tick's stamps is readable. A row
 * whose `ts_started` cannot be parsed is dropped from the batched strip rather
 * than placed at the epoch — a bad historical row is tolerated, not drawn at
 * 1970. */
const instant = (value: string | null | undefined): number | null => {
  if (value == null || value === '') return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
};

/**
 * How long an open row may be read as a tick that is still running on the
 * row's own evidence ALONE — the floor, lifted by `DRIVER_LIVE` below.
 *
 * Without a bound there is none anywhere: a driver killed mid-tick (TaskStop,
 * OOM, a host reboot) closes no row, so every strip paints that one attempt as
 * a tick still running, forever. The epic's next tick closes it for real —
 * `_TickLog.sweep_abandoned` in `graph/driver/tick.py` — but an epic whose
 * driver never comes back has no next tick, which is exactly the case this
 * floor is for.
 *
 * `HEARTBEAT_STALE_SECONDS` in `kew/graph/driver/lock.py`, because that is the
 * age at which a driver that has gone silent loses its lock — but note what
 * this measures: the age of the tick's START, not of the driver's last
 * heartbeat. The two are the same number only for a driver that beats nowhere
 * else, and `_lock_keepalive` (`graph/driver/tick.py`) exists precisely so a
 * step that blocks longer than this — the merge queue's combined-tree
 * verification, #2148 — keeps beating mid-tick instead of going silent. So on
 * the row alone this floor cannot tell a 6-minute tick from a dead driver, and
 * it is not allowed to decide that question by itself.
 */
export const TICK_LIVE_MAX_MS = 300_000;

/**
 * The server's own liveness reading for the driver that opened the row:
 * `EpicHeartbeat.driver_status` / `EpicSummary.driver_status`, "live".
 *
 * This is the half of the question the row cannot answer. `driver_liveness`
 * (kew/service/epics.py) thresholds the NEWEST heartbeat the driver writes —
 * the tick stamps AND the lock file's own, the one `_lock_keepalive` keeps
 * beating mid-tick — so a driver still holding a freshly-beaten lock reads
 * `live` however long its current tick has been running.
 */
export const DRIVER_LIVE = 'live';

/**
 * A driver whose heartbeat lapsed while its lock holder is provably alive — it
 * is inside one long foreground stage, so its open tick is still running
 * (#2534).
 */
export const DRIVER_BUSY = 'busy';

/** True while a tick is still running. No finish stamp and no outcome IS the
 * live state server-side, so a killed driver leaves an honestly open row
 * rather than a stale "running" flag; either half being absent is enough.
 *
 * `driver` is the server's reading for this epic's driver, where the caller
 * has it. It only ever EXTENDS the claim: an open row inside
 * `TICK_LIVE_MAX_MS` is live on its own evidence, and past that it is live
 * exactly while the server can still see the driver beating. A long tick is a
 * supported case, not a bug — `_lock_keepalive` is there to support it — so
 * reading one as quiet and labelling it "abandoned" while its driver holds a
 * fresh lock and nothing has swept its row is the strip contradicting the
 * server (review round 5). Where no reading is passed (an older `kew serve`
 * serves none) the floor is all there is, which is the pre-existing bound.
 *
 * A row whose start cannot be read is not live either — there is no instant to
 * bound it against, and an unreadable stamp must not buy a permanent cyan.
 *
 * #2882 — nor is a row the server does not say reached its tick
 * (`reached_tick`, set once `begin_epic_tick` succeeds). The row opens before
 * the lock, the preflight and the graph build, so an attempt blocked in
 * preflight is open and running nothing: drawn cyan and "running" it read as
 * a live tick beside a heartbeat reading none. It is drawn quiet instead. */
export const isTickLive = (
  tick: EpicTickStrip,
  now: number = Date.now(),
  driver?: string | null
): boolean => tick.reached_tick === true && isTickOpen(tick, now, driver);

/** The liveness rule without the reached-tick half: an open row inside the
 * bound, or one whose driver the server can still see beating. */
function isTickOpen(tick: EpicTickStrip, now: number, driver?: string | null): boolean {
  if (tick.ts_finished != null && tick.outcome != null) return false;
  const startMs = instant(tick.ts_started);
  if (startMs === null) return false;
  return now - startMs <= TICK_LIVE_MAX_MS || driver === DRIVER_LIVE || driver === DRIVER_BUSY;
}

/** True while a loop iteration is still running — the same rule, on the only
 * stamp an iteration has. */
export const isIterationLive = (it: Iteration): boolean => it.finished_at == null;

/** Own-property lookup: `MEANING_OF_OUTCOME['constructor']` is not undefined,
 * and an outcome word comes off the wire. Same guard as `status.ts`'s. */
const own = <T>(table: Readonly<Record<string, T>>, key: string): T | undefined =>
  Object.hasOwn(table, key) ? table[key] : undefined;

/**
 * One slot of a strip.
 *
 * A `pad` slot holds the strip's width open and stands for no tick at all —
 * which is NOT the same as a tick that did nothing, and must not be drawn the
 * same way: an epic that has ticked twice has two slots of history, not
 * twenty-two quiet ticks it never ran.
 */
export type TickSlot<T> = {
  /** `change` is the Epics list's neutral change dot (#2844 D23, `changeSlots`). */
  readonly kind: 'pad' | 'quiet' | 'mark' | 'change';
  /** Null only for a pad slot. */
  readonly item: T | null;
  /** The `STATUS_MARKS` meaning to draw, for a `mark` slot. */
  readonly meaning: StatusMeaning | null;
  /** True for the one slot a strip may paint cyan, and the only one that grows. */
  readonly live: boolean;
  readonly severity: BatchSeverity;
};

const PAD_SLOT: TickSlot<never> = {
  kind: 'pad',
  item: null,
  meaning: null,
  live: false,
  severity: 'quiet'
};

/** The meaning a finished tick's outcome draws, or null when it is quiet.
 *
 * `driver` is the server's liveness reading, passed through to `isTickLive`. */
export function tickMeaning(tick: EpicTickStrip, driver?: string | null): StatusMeaning | null {
  if (isTickLive(tick, Date.now(), driver)) return LIVE_MEANING;
  return own(MEANING_OF_OUTCOME, tick.outcome ?? '') ?? null;
}

/** One tick as a slot. */
export function tickSlot(tick: EpicTickStrip, driver?: string | null): TickSlot<EpicTickStrip> {
  const meaning = tickMeaning(tick, driver);
  const live = isTickLive(tick, Date.now(), driver);
  return {
    kind: meaning === null ? 'quiet' : 'mark',
    item: tick,
    meaning,
    live,
    severity: meaning === null ? 'quiet' : batchSeverity(meaning)
  };
}

/** One loop iteration as a slot, read through the run vocabulary. */
export function iterationSlot(it: Iteration): TickSlot<Iteration> {
  const live = isIterationLive(it);
  const shape = statusShape(it.status, 'run');
  // An unknown iteration status degrades to `todo`, which is not a thing that
  // happened — draw it quiet rather than ringing a slot for a word nobody here
  // can read.
  const meaning = live ? LIVE_MEANING : shape.known ? shape.meaning : null;
  const quiet = meaning === null || batchSeverity(meaning) === 'quiet';
  return {
    kind: quiet && !live ? 'quiet' : 'mark',
    item: it,
    meaning: quiet && !live ? null : meaning,
    live,
    severity: meaning === null ? 'quiet' : batchSeverity(meaning)
  };
}

/**
 * Exactly `slots` slots, newest LAST, padded on the old side.
 *
 * `items` arrives oldest-first (both APIs page the tail that way), so the tail
 * is the newest `slots` of them and the padding goes in front. A strip asked
 * for fewer slots than it has items keeps the newest, because the question a
 * strip answers is "what has it been doing lately".
 */
export function padSlots<T>(items: readonly T[], slots: number, toSlot: (item: T) => TickSlot<T>) {
  const width = Math.max(0, Math.trunc(slots));
  const shown = width === 0 ? [] : items.slice(-width);
  const pads: TickSlot<T>[] = Array.from({ length: width - shown.length }, () => PAD_SLOT);
  return [...pads, ...shown.map(toSlot)];
}

/** A driver's compact strip: fixed slots, newest at the right edge.
 *
 * `driver` is this epic's served `driver_status`, so a row whose driver the
 * server can still see beating keeps its cyan through a long tick. */
export const tickSlots = (
  ticks: readonly EpicTickStrip[],
  slots: number = COMPACT_SLOTS,
  driver?: string | null
): TickSlot<EpicTickStrip>[] => padSlots(ticks, slots, (t) => tickSlot(t, driver));

/**
 * How many marks the Epics list asks for per row (#2844 D23): one strip's
 * worth. The read is `fetchEpicChangeStrip`, which the server filters to the
 * marks themselves, so a change is found however many no-op ticks follow it —
 * reading 200 attempts and filtering them here lost a change behind 201.
 */
export const CHANGE_WINDOW = COMPACT_SLOTS;

/** Whether an attempt points at a journal row. Not, on its own, a change: an
 * attempt over an unchanged snapshot carries the previous change's id, because
 * the journal's dedup hands back the row it already has. `changeSlots` counts
 * only the FIRST attempt carrying each id, as the server's change read does. */
export const isChange = (tick: EpicTickStrip): boolean => tick.journal_tick_id != null;

/** The attempts that moved the snapshot: the first one carrying each journal
 * id. Every later attempt with the same id changed nothing. */
function firstChanges(ticks: readonly EpicTickStrip[]): Set<EpicTickStrip> {
  const seen = new Set<number>();
  const first = new Set<EpicTickStrip>();
  for (const tick of ticks) {
    if (tick.journal_tick_id == null || seen.has(tick.journal_tick_id)) continue;
    seen.add(tick.journal_tick_id);
    first.add(tick);
  }
  return first;
}

/**
 * A change dot's colour: neutral, not a status (#2844 D23, operator gate).
 * Green means ready, merged or done (DESIGN.md), and a strip of green dots
 * read the same on three epics whose CHANGE was 2735, 560 and 209. A change
 * says only that the snapshot moved; how many did is the density's to carry.
 */
export const CHANGE_COLOR = 'var(--text-2)';

/** What a change strip's marks are, for the head and the strip's own label. */
export const CHANGE_STRIP_LEGEND =
  'one grey dot per change the driver recorded · red × a failed tick · cyan the tick running now';

/**
 * #2844 D23 — the Epics list's sparkline: one mark per CHANGE, not per tick.
 *
 * A strip of the last sixteen attempts was accurate and flat: on live data
 * 196-199 of an epic's last 200 ticks did nothing, while reviews and merges
 * land outside ticks (detached review stages, operator merges) and reach the
 * driver as a snapshot change on a tick whose own outcome is `nothing`. So the
 * marks are the attempts that appended a change (the first carrying each
 * journal id — a no-op after it carries the same id), each one a
 * neutral `change` dot whatever its outcome, and the strip moves when CHANGE
 * moves. A failed tick keeps its red ×, and the live tick its cyan: both are
 * what a reader of a driver's strip looks for first. Everything else — the
 * no-op, the held repeat, the empty graph — is left out rather than drawn as a
 * quiet rule, which is what made the line flat.
 *
 * #3054 N18 — the marks start at the strip's LEFT edge, oldest first, with the
 * empty slots after them: the Ticks head above is left-aligned, and a strip of
 * three marks pushed to the right edge sat under nothing.
 */
export function changeSlots(
  ticks: readonly EpicTickStrip[],
  slots: number = COMPACT_SLOTS,
  driver?: string | null
): TickSlot<EpicTickStrip>[] {
  const { marks } = changeMarks(ticks, driver);
  const padded = padSlots(marks, slots, (tick) => {
    const slot = tickSlot(tick, driver);
    if (slot.live || slot.meaning === 'failed') return slot;
    return { ...slot, kind: 'change', meaning: null, severity: 'quiet' };
  });
  return [...padded.filter((s) => s.kind !== 'pad'), ...padded.filter((s) => s.kind === 'pad')];
}

/**
 * #3054 N1 — the Epics list's CHANGE: the changes inside the change strip's
 * window, counted off the same read the strip draws.
 *
 * The served `change_count` is the journal's row count, and before #2879 the
 * journal wrote a row on every tick, so two live epics read 4554 and 2725
 * while their strips showed a handful of changes. Counting the strip's own
 * first-attempt-per-journal-id marks makes CHANGE and the strip one reading;
 * no migration rewrites the old rows. The window is the strip's: its newest
 * `CHANGE_WINDOW` marks, so the count is the grey dots the row draws. `null`
 * while the strip is unread.
 */
export function windowChangeCount(
  ticks: readonly EpicTickStrip[] | undefined,
  driver?: string | null
): number | null {
  if (ticks === undefined) return null;
  const { marks, changes } = changeMarks(ticks, driver);
  return marks.slice(-CHANGE_WINDOW).filter((tick) => changes.has(tick)).length;
}

/** What a change strip draws: the changes, the failed ticks and the live one,
 * in order, with the changes among them. */
function changeMarks(ticks: readonly EpicTickStrip[], driver?: string | null) {
  const changes = firstChanges(ticks);
  const marks = ticks.filter(
    (tick) =>
      changes.has(tick) || isTickLive(tick, Date.now(), driver) || tickMeaning(tick, driver) === 'failed'
  );
  return { marks, changes };
}

/** A change mark's word: `changed` where its outcome says `nothing`, so the
 * popover does not contradict the mark it is about. */
export function changeWord(tick: EpicTickStrip | null, driver?: string | null): string {
  const word = tickWord(tick, driver);
  return tick !== null && isChange(tick) && word === 'nothing' ? 'changed' : word;
}

/**
 * #2545 D23 — an epic's last tick, read off the same tick log its strip is.
 *
 * The roster's `last_tick_ts` is the change journal's (`epic_ticks`), which a
 * driver whose ticks changed nothing never writes to, so a row could print
 * "no ticks yet" beside a strip full of attempts. The newest of the two is the
 * reading: the log holds every attempt the journal does, and the journal's
 * stamp stands in only while the strip has not been read.
 *
 * #2882 — only an attempt that beat the heartbeat (`beatsHeartbeat`) is a
 * last tick: a refused or not-yet-reached attempt stamped "last tick 10s ago"
 * beside a heartbeat reading none.
 */
export function lastTickTs(
  journalTs: string | null,
  ticks: readonly EpicTickStrip[] | undefined
): string | null {
  let newest = journalTs;
  let newestMs = journalTs === null ? Number.NaN : Date.parse(journalTs);
  for (const tick of ticks ?? []) {
    if (!beatsHeartbeat(tick)) continue;
    const ms = Date.parse(tick.ts_started);
    if (Number.isNaN(ms)) continue;
    if (newest === null || Number.isNaN(newestMs) || ms > newestMs) {
      newest = tick.ts_started;
      newestMs = ms;
    }
  }
  return newest;
}

/**
 * The outcomes of an attempt that reached a tick (`begin_epic_tick`, past the
 * lock, the preflight and the empty-graph guard): what the tick DID
 * (`_tick_outcome`, kew/graph/driver/tick.py). `failed`, `locked_out`,
 * `empty_graph` and `abandoned` are not here — a failed tick that did reach
 * the tick has already moved the served heartbeat itself.
 */
const REACHED_TICK: ReadonlySet<string> = new Set(['dispatched', 'merged', 'held', 'nothing']);

/** Whether an attempt may advance the heartbeat (#2882): a finished attempt
 * only on an outcome that reached a tick; an open one only once the server
 * says it got past `begin_epic_tick` (`reached_tick`) — the row opens before
 * the lock, the preflight and the graph build, so an attempt blocked in
 * preflight is open and no beat. A finished attempt with no outcome is one a
 * `kew serve` predating the served outcome could not name, and is read as it
 * was before. An outcome word this build has no name for is not a beat. */
export function beatsHeartbeat(tick: EpicTickStrip): boolean {
  if (tick.ts_finished === null) return tick.reached_tick === true;
  return tick.outcome == null || REACHED_TICK.has(tick.outcome);
}

/**
 * #2783 N18 — the epic page's heartbeat, read against the same tick log its
 * strip and the Epics list read.
 *
 * The served heartbeat's stamps are the change journal's, so a driver whose
 * ticks changed nothing left the page saying "last tick —" and "no heartbeat
 * yet" above a strip full of attempts, while the list read "30s ago" off that
 * strip. When the log holds a newer attempt than the heartbeat states, that
 * attempt is the heartbeat's last tick: its start, and its duration where it
 * has finished. `last_tick_ts` is the journal's stamp, which a complete epic
 * reads as when it finished, so the log only fills it where it is absent.
 * Everything else (the counts, the cadence, the signal) stays the heartbeat's
 * own. No
 * heartbeat at all stays null: the log says when the driver ran, not what
 * cadence to hold it to.
 *
 * #2882 — only an attempt that reached a tick is a beat (`beatsHeartbeat`). A
 * failed, locked-out or empty-graph attempt is the driver being refused, so a
 * fresh one over a ten-minute-old tick left the page and the list reading
 * live while the server read the driver `gone`.
 */
export function heartbeatFromTickLog(
  hb: EpicHeartbeat | null,
  ticks: readonly EpicTickStrip[]
): EpicHeartbeat | null {
  if (hb === null) return hb;
  let newest: EpicTickStrip | null = null;
  let newestMs = Number.NEGATIVE_INFINITY;
  for (const tick of ticks) {
    if (!beatsHeartbeat(tick)) continue;
    const ms = Date.parse(tick.ts_started);
    if (!Number.isNaN(ms) && ms > newestMs) {
      newest = tick;
      newestMs = ms;
    }
  }
  if (newest === null) return hb;
  const startedMs =
    hb.last_tick_started_at === null ? Number.NaN : Date.parse(hb.last_tick_started_at);
  if (!Number.isNaN(startedMs) && startedMs >= newestMs) return hb;
  const finishedMs = newest.ts_finished === null ? Number.NaN : Date.parse(newest.ts_finished);
  return {
    ...hb,
    last_tick_started_at: newest.ts_started,
    last_tick_ts: hb.last_tick_ts ?? newest.ts_started,
    last_tick_duration_s: Number.isNaN(finishedMs)
      ? hb.last_tick_duration_s
      : Math.max(0, (finishedMs - newestMs) / 1000)
  };
}

/** A loop's compact strip, on the same slot geometry. */
export const iterationSlots = (
  iterations: readonly Iteration[],
  slots: number = COMPACT_SLOTS
): TickSlot<Iteration>[] => padSlots(iterations, slots, iterationSlot);

/**
 * Ticks as marks for `$lib/time/batch`, for the full strip on the epic page.
 *
 * A tick has a duration, so its mark spans start to finish and a long tick
 * draws wide. A tick still running has no end: it is given its start, and the
 * strip grows it on its own (the one slot that may).
 */
export function tickMarks(
  ticks: readonly EpicTickStrip[],
  driver?: string | null
): BatchMark<EpicTickStrip>[] {
  const out: BatchMark<EpicTickStrip>[] = [];
  for (const tick of ticks) {
    const startMs = instant(tick.ts_started);
    if (startMs === null) continue;
    const endMs = instant(tick.ts_finished);
    out.push({
      startMs,
      endMs: endMs === null ? startMs : Math.max(startMs, endMs),
      severity: tickSlot(tick, driver).severity,
      item: tick
    });
  }
  return out;
}

/** One line per non-zero count, plus the failure class. What the popover says.
 *
 * Only the counts that happened (DESIGN.md: show only non-zero counts) — a
 * popover printing four zeroes for a quiet tick is four readings for no
 * information. The error class is printed verbatim and is the ONLY thing the
 * server records about a failure: an exception message carries checkout paths
 * and occasionally a token-bearing URL, so it never leaves the driver. */
export function tickCounts(tick: EpicTickStrip): string[] {
  const pairs: [string, number][] = [
    ['dispatched', tick.n_dispatched],
    ['merged', tick.n_merged],
    ['held', tick.n_held],
    ['stuck', tick.n_stuck]
  ];
  const lines = pairs.filter(([, n]) => n > 0).map(([word, n]) => `${n} ${word}`);
  if (tick.error_class) lines.push(tick.error_class);
  return lines;
}

/** The outcome word a slot shows: the server's own, or "running" for the live
 * tick, or "no tick" for a pad. Never invented — an outcome this build has no
 * mark for still prints as itself.
 *
 * An open row the liveness rule has given up on reads "abandoned", which is the
 * word the epic's next tick writes into that row (`_TickLog.sweep_abandoned`):
 * the strip says the same thing before the sweep as after it, rather than
 * "running" forever on one side of it and "abandoned" on the other. It takes
 * the same `driver` reading as `isTickLive` for the same reason — the word has
 * to agree with the colour, and a driver the server can still see beating is
 * running, not abandoned. */
export function tickWord(tick: EpicTickStrip | null, driver?: string | null): string {
  if (tick === null) return 'no tick';
  if (isTickLive(tick, Date.now(), driver)) return 'running';
  // #2882 — open but not past `begin_epic_tick`: an attempt still in the
  // lock, the preflight or the graph build. Not running a tick, and not yet
  // given up on either.
  if (isTickOpen(tick, Date.now(), driver)) return 'starting';
  if (tick.outcome == null) return 'abandoned';
  return tick.outcome;
}

/** One line per non-zero reading of a loop iteration. */
export function iterationCounts(it: Iteration): string[] {
  const lines: string[] = [];
  if (it.kind) lines.push(it.kind);
  if (it.cost_usd > 0) lines.push(`$${it.cost_usd.toFixed(2)}`);
  if (it.tokens > 0) lines.push(`${it.tokens} tokens`);
  return lines;
}

/** The word an iteration slot shows: its index and its status, or "running"
 * while it has no finish stamp. */
export function iterationWord(it: Iteration | null): string {
  if (it === null) return 'no iteration';
  return `#${it.idx} ${isIterationLive(it) ? 'running' : it.status || 'unknown'}`;
}

/**
 * A strip's whole reading, in one sentence.
 *
 * The compact strip's slots are deliberately NOT focusable — it sits inside the
 * loops roster's row button, and interactive content inside a button is not a
 * document. So the strip is one `role="img"` whose label has to carry
 * everything a reader cannot hover: how many attempts, and how many of each
 * kind, worst first. Pads are counted as nothing at all, because a tick that
 * never happened is not a reading.
 */
export function stripSummary<T>(slots: readonly TickSlot<T>[], word: (item: T) => string): string {
  const tally = new Map<string, number>();
  for (const slot of slots) {
    if (slot.item === null) continue;
    const key = word(slot.item);
    tally.set(key, (tally.get(key) ?? 0) + 1);
  }
  if (tally.size === 0) return 'no ticks yet';
  return [...tally].map(([key, n]) => `${n} ${key}`).join(', ');
}
