/**
 * FUI-25 — the pure reading behind the Monitor's event stream (DMonitor
 * x 607–1400, y ≈476–924).
 *
 * `/api/audit` is a hash-chained log of what the factory did. This module
 * turns one page of it into the two things the artboard draws — a 30-minute
 * strip with a mark per event, and the list under it — and nothing in here
 * touches the clock, the network or the DOM, so a vitest can hold every
 * position, word and colour to the artboard.
 *
 * Three rules run through it.
 *
 * **The colour ration.** The factory spends yellow on money, green on an
 * attestation that came back good, and magenta on "a human has to decide".
 * Everything else is neutral, and an event this file does not recognise is
 * neutral too — a new event name must never arrive already painted as a
 * refusal or an attestation. That is why `eventCategory` is a closed set of
 * rules over the name and the actor rather than a guess at the summary.
 *
 * **Green is a verdict, not a topic.** `publication_output_scan` is green when
 * the scan says `clean` and neutral when it says anything else; the artboard
 * paints `sandbox_resolved · sandboxed` and `publication_posture · canonical`
 * neutral for the same reason. Those record which posture was taken, not that
 * a check passed, and a green tick over an unexamined fact is the one lie this
 * surface must not tell.
 *
 * **One clock.** Every time on this surface — the axis labels and the list's
 * own times — is formatted from the same parsed instants through `$lib/time`,
 * in the viewer's zone, with the full UTC instant in a `title`. The strip and
 * the list used to parse and format independently, and shipped the same
 * moment in two zones: rows reading 13:33–14:26 under an axis reading
 * 16:35–17:05 (#2205). The window the axis spans is widened to the newest
 * event the page holds for the same reason — an event stamped a little ahead
 * of the viewer's clock must not fall off the right edge while still being
 * listed below it — but only as far as `MAX_SKEW_MS`, so a stamp from a clock
 * that is simply wrong cannot carry the window away from the rows either.
 *
 * **A row is a line, not a payload.** The log's `summary` is a JSON excerpt of
 * the event's payload, and printing it verbatim made every row something to
 * parse (#2210). `event-line.ts` reads it into kind · subject · fact, and the
 * excerpt itself stays on the row as `detail`, for the expand.
 *
 * **Silence is a reading.** A stream that could not be fetched says so
 * ("stream unavailable"); it never renders as an empty list, which on this
 * screen would read as a calm fleet. Quiet between events is drawn as a quiet
 * gap with its own duration rather than closed up, so 22 minutes of nothing
 * looks like 22 minutes of nothing. And a page that ran out before the window
 * did says *that* — a busy factory whose 50 events do not reach back 30
 * minutes must not draw the minutes it never read as calm ones.
 */
import type { AuditEvent, AuditList } from '$lib/api';
import { eventLine, eventWords } from '$lib/monitor/event-line';
import { queueAttention } from '$lib/monitor/merge-queue-attention';
import { fmtDateTime, fmtTime, NO_TIME, parseInstant, utcTitle } from '$lib/time';
import { itemRef } from '$lib/utils';
import type { LabelAlign } from '$lib/factory/gauge';
import { isUnavailable, type Source } from '$lib/monitor/readouts';

/** How a strip label sits on its mark, borrowed from the gauge's axis. */
export type { LabelAlign };

/**
 * The window the strip is drawn for, when the caller has one — the brush's
 * (`stream-query.ts`). Absent, the strip is the last 30 minutes it always was.
 */
export type StreamSpan = { startMs: number; endMs: number };

/** The poll the plan specifies: one page of the log every 5s, while live. */
export const STREAM_POLL_MS = 5000;

/**
 * How long a burst of windows is given to settle before the stream is read for
 * the newest of them (#2248).
 *
 * A query change is a read of `/api/audit` and a read of
 * `/api/audit/histogram`, and the brush can change the query far faster than a
 * person can mean to: a held arrow key repeats about thirty times a second, and
 * every repeat is two requests against the same SQLite store the fleet is
 * appending audit events to. The read that matters is the one for where the
 * operator's hand stopped, so the ones in front of it are coalesced away. A
 * tenth of a second is below the threshold where a navigation reads as lag, and
 * the FIRST read of a mount is never delayed — there is nothing on the screen
 * yet for it to be an update to.
 */
export const STREAM_SETTLE_MS = 120;

/**
 * `?limit=` — one page of the list. 200 is both what the plan asks for and
 * `MAX_LIMIT` in `service/audit.py`, so the UI's page size is the largest the
 * API will serve and "Load older" is the only way past it (#2248).
 */
export const STREAM_LIMIT = 200;

/** The strip's default span, its major tick and its minor tick, in minutes. */
export const WINDOW_MINUTES = 30;
export const MAJOR_TICK_MINUTES = 5;
export const MINOR_TICK_MINUTES = 1;

/**
 * How far past the viewer's own clock an event stamp may still carry the
 * window's end. A host and a browser disagreeing by seconds is ordinary skew
 * and the axis follows it; one major tick is already a generous reading of
 * "ordinary". Past that the stamp is not skew but a clock that is wrong
 * somewhere, and following it would drag the 30 minutes off the hour the rows
 * were drawn in: the far stamp would be the only mark on an axis whose window
 * holds nothing else, every other mark would fall off the left edge, and the
 * unread run at the old end would read as covered. Such an event is still
 * listed below — the strip simply does not chase it.
 */
export const MAX_SKEW_MS = MAJOR_TICK_MINUTES * 60_000;

/** Five minutes of nothing is a quiet gap, drawn dashed and named. */
export const QUIET_GAP_MS = 5 * 60_000;
/** Events closer together than this are one burst … */
export const BURST_SPACING_MS = 60_000;
/** … and a burst is only worth labelling from three events up. */
export const BURST_MIN_EVENTS = 3;

/**
 * What the screen says when the window it is drawn for holds no events.
 *
 * Sentences, and capitalised (#2282 D15: "empty states are sentences"). The
 * charts said nothing at all when the window was empty — an axis with no marks
 * on it, over a list still showing yesterday's events, reads as a feed that has
 * broken rather than as a quiet half hour.
 */
export const STREAM_EMPTY = 'No events yet';

/** The same, for a paused window — where "yet" promises an update that never comes. */
export const STREAM_EMPTY_WINDOW = 'No events in this window';

/**
 * What the strip and the list say where the page ran out, and the sentence a
 * screen reader gets for the same row.
 */
export const STREAM_UNSEEN = 'older events not shown';
export const STREAM_UNSEEN_ARIA = 'older events not shown — the page reached its limit';

/**
 * Before the first poll lands. "not read" rather than "unavailable": nothing
 * has failed yet, and the two must not print the same sentence (the same
 * distinction `readouts.ts` draws for the pulse-backed cells).
 */
export const STREAM_UNREAD = 'event stream not read';

/**
 * What an event means, which is the only thing that colours it.
 *
 * `needs-you` is the magenta the whole UI reserves for a pending human
 * decision; it is spent here on a hold that is waiting for one, never on a
 * decision already taken (`approval_granted` is history, not a request).
 */
export type EventCategory = 'money' | 'evidence' | 'needs-you' | 'danger' | 'neutral';

/** The mark and the rail dot. Neutral is --text-2: present, not shouting. */
export const MARK_COLOR: Record<EventCategory, string> = {
  money: 'var(--warn)',
  evidence: 'var(--success)',
  'needs-you': 'var(--accent)',
  danger: 'var(--danger)',
  neutral: 'var(--text-2)'
};

/*
 * There is no TEXT_COLOR beside it any more (#2282 D15: "colour only the
 * mark"). The list painted every word of a row in its category's colour,
 * which made a page of green and yellow sentences out of what is meant to be
 * one legible column with a coloured dot down its left edge. The category is
 * said by the mark; the words are read.
 */

/** Any event whose name is about money, whoever recorded it. */
const MONEY_NAME = /budget|spend/;

/** The budget governor, which records its refusals as an approval hold. */
const MONEY_ACTOR_TYPE = 'policy';
const MONEY_ACTOR_ID = 'budget';

/** A money event that stopped something, as against one that merely spent. */
const REFUSAL_NAME = /refus|exceed|hold|declin|block|paus/;

/**
 * The verdict each attestation prints when its check passed, keyed by event
 * because the two do not share a vocabulary. The output scan records `clean`
 * against `found` (`publication_scan.py`); the governed review records
 * `READY` against `NEEDS_CHANGES`, `NEEDS_FULL` and the four degenerate
 * verdicts the driver attests when a human or a policy short-circuits the
 * stage — DEGRADED, UNPARSEABLE, MERGED_UNREVIEWED, PRE_POLICY
 * (`review_audit.py`, `review_delta.py`).
 *
 * The map is the closed set, and a verdict word the backend adds after this
 * was written stays neutral until it is named here. That is the safe
 * direction: an unread verdict drawn grey is a reading not yet taken, and an
 * unread verdict drawn green is a check this surface claims passed without
 * knowing that it did.
 */
const PASS_VERDICT: ReadonlyMap<string, ReadonlySet<string>> = new Map([
  ['publication_output_scan', new Set(['clean'])],
  ['adversarial_review_verdict', new Set(['ready'])]
]);

/**
 * Events that are, in themselves, a request for a person. The merge queue's
 * attention (#2814) is read by its reason code instead (#2982,
 * `queueAttention`): a red main is a person's move, a contract the queue
 * requeued itself over is not.
 */
const NEEDS_HUMAN: ReadonlySet<string> = new Set(['approval_hold', 'needs_human_raised']);

/** Events that state a missing guarantee, which DESIGN.md reserves red for. */
const DANGER: ReadonlySet<string> = new Set(['host_lease_takeover_unconfirmed']);

/** `{"verdict":"READY",…}` — the verdict read by its key, never by its neighbours. */
const VERDICT_KEY = /"verdict"\s*:\s*"([^"]*)"/;

/**
 * The verdict the summary states, lowercased, or `null` when it states none.
 *
 * The summary is a JSON excerpt of the payload in production
 * (`service/audit.py`, capped at 200 characters) and prose in the artboard
 * fixtures ("clean · 3670e43f"). The JSON form is read by its key rather than
 * scanned for a good-sounding word, because the review verdict's payload also
 * carries `acceptance_reason` — free text lifted straight out of the
 * reviewer's sidecar (`review_contract.py`), which can say "acceptance checks
 * passed" under a NEEDS_CHANGES. A green tick bought off a neighbouring field
 * is exactly the lie this surface must not tell, so an excerpt that is JSON
 * and carries no verdict key states nothing at all. Prose falls back to its
 * first word, which is where the verdict sits in the fixtures.
 */
function verdictOf(summary: string | null | undefined): string | null {
  const text = (summary ?? '').trim();
  if (!text) return null;
  const keyed = VERDICT_KEY.exec(text);
  if (keyed) return keyed[1].toLowerCase();
  if (text.startsWith('{')) return null;
  return text.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)[0] ?? null;
}

function isMoney(event: AuditEvent): boolean {
  return (
    MONEY_NAME.test(event.event) ||
    (event.actor_type === MONEY_ACTOR_TYPE && event.actor_id === MONEY_ACTOR_ID)
  );
}

/**
 * The one place an event name becomes a colour. Order matters: the budget
 * governor's refusal is a hold, and it is yellow, because the reader's next
 * move is about money and not about the approval queue.
 */
export function eventCategory(event: AuditEvent): EventCategory {
  if (isMoney(event)) return 'money';
  const queue = queueAttention(event);
  if (queue) return queue.personNext ? 'needs-you' : 'neutral';
  if (NEEDS_HUMAN.has(event.event) || /needs_human/.test(event.event)) return 'needs-you';
  if (DANGER.has(event.event)) return 'danger';
  const passing = PASS_VERDICT.get(event.event);
  if (passing) {
    const verdict = verdictOf(event.summary);
    return verdict !== null && passing.has(verdict) ? 'evidence' : 'neutral';
  }
  return 'neutral';
}

/**
 * What a money mark says above the strip. The artboard annotates the one the
 * governor refused; a money event that spent rather than stopped gets its own
 * name, opened up, rather than a word nobody recorded.
 */
function annotation(event: AuditEvent): string | null {
  if (!isMoney(event)) return null;
  return REFUSAL_NAME.test(event.event) ? 'budget refusal' : eventWords(event.event);
}

/**
 * "8s", "22m 35s", "1h 04m", "2d 03h" — the compact form the strip and the
 * list use. It carries two units, never three: a quiet row saying how long the
 * factory was idle is read at a glance, and "2d 03h 14m 09s" is not.
 */
export function duration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600) % 24;
  const d = Math.floor(total / 86_400);
  if (d > 0) return `${d}d ${String(h).padStart(2, '0')}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

/** The same duration spelled out, for the screen reader on a quiet row. */
export function spellDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const parts: string[] = [];
  const d = Math.floor(total / 86_400);
  const h = Math.floor(total / 3600) % 24;
  const m = Math.floor(total / 60) % 60;
  const s = total % 60;
  if (d > 0) parts.push(`${d} day${d === 1 ? '' : 's'}`);
  if (h > 0) parts.push(`${h} hour${h === 1 ? '' : 's'}`);
  if (m > 0) parts.push(`${m} minute${m === 1 ? '' : 's'}`);
  if (s > 0 || parts.length === 0) parts.push(`${s} second${s === 1 ? '' : 's'}`);
  return parts.slice(0, 2).join(' ');
}

/** One tick on the strip's axis: a minute, with a label every five. */
export type StreamTick = {
  /** Position along the strip, 0–100. */
  pct: number;
  major: boolean;
  /** "15:45" on a major tick (viewer zone), `null` on a minor one. */
  label: string | null;
  align: LabelAlign;
};

/** One event on the strip: a 2×24 mark, coloured by what it meant. */
export type StreamMark = {
  id: number;
  pct: number;
  category: EventCategory;
  /** The word printed above the mark, e.g. "budget refusal". */
  annotation: string | null;
};

/** A stretch of nothing between two marks, drawn dashed and named. */
export type StreamGap = {
  fromPct: number;
  toPct: number;
  midPct: number;
  /** "22m 35s quiet". */
  label: string;
};

/**
 * The stretch of the window the page could not reach — same geometry as a
 * quiet gap, the opposite reading.
 *
 * `/api/audit` answers with one page and `next_after`, its truncation signal.
 * A factory busy enough to fill a 50-event page inside 30 minutes leaves the
 * strip's earlier minutes with no marks and — because a quiet gap is only
 * ever drawn between two events the page holds — no label either, which on
 * this strip reads as silence. So the unread stretch is drawn and named:
 * the strip says what it could not see rather than showing calm.
 */
export type StreamUnseen = StreamGap;

/** Three or more events crowded together, labelled at their newest mark. */
export type StreamBurst = {
  pct: number;
  /** How many events the run holds — what decides which bursts keep words. */
  count: number;
  /** "3 events in 8s". */
  label: string;
};

export type StreamStrip = {
  /** ISO-free bounds, kept so a test can state the window it expected. */
  startMs: number;
  endMs: number;
  ticks: StreamTick[];
  marks: StreamMark[];
  gaps: StreamGap[];
  bursts: StreamBurst[];
  /** The unread run at the old end, or `null` when the page covers the window. */
  unseen: StreamUnseen | null;
};

/** A list row: an event, or the quiet between two of them. */
export type StreamRow =
  | {
      kind: 'event';
      key: string;
      /** The audit log's own id for the event, which a row's time cannot stand
       * in for: two events often share one second (#2921). */
      id: number;
      /** "15:46:58" in the viewer's zone, or '—' for a stamp that would not
       * parse. Same instant, same formatter as the axis above it. */
      time: string;
      /** "2026-09-29 13:33:18 UTC" — the `title` under that reading. Empty
       * when there was no instant to state. */
      title: string;
      /**
       * "Publication posture" — the row's lead, which is a label and never a
       * code (#2282 D15). `event-line.ts` composes it; the row prints it in
       * `--text-1` ahead of everything else it says.
       */
      lead: string;
      /**
       * "#2055 · canonical" — the subject and the one fact, its recorded
       * values worded (`EventLine.plain`, #2896 R5). Secondary to the lead,
       * and '' where the event names neither.
       */
      rest: string;
      /**
       * The fact as the log recorded it ("in_review → done"), for the row's
       * `title`: the code is kept, never dropped. '' when the worded fact is
       * the recorded one, and the row then carries no title.
       */
      code: string;
      /**
       * What the log recorded, for the row's expand: the payload formatted,
       * or the excerpt as it arrived when the 200-character cap cut it.
       * Empty when the line already prints everything recorded, and the row
       * then offers no expand at all.
       */
      detail: string;
      category: EventCategory;
      /**
       * What the row can link to, for the expand's in-app links (#2248):
       * `/work?item=<itemId>`, `/runs/<runId>`, and the item filter the chip
       * row takes. `ref` is how the link reads ("#2219"); all three are `null`
       * where the event names nothing, and the link is then not offered.
       */
      ref: string | null;
      itemId: string | null;
      runId: string | null;
    }
  | {
      kind: 'gap';
      key: string;
      /**
       * "22m 35s quiet" — the strip's own words for the same quiet (#3052 N15),
       * so one stretch of nothing is not "22M 35S" in one place and "22m 35s
       * quiet" an inch above it.
       */
      label: string;
      /** "22 minutes 35 seconds without events". */
      aria: string;
    }
  | {
      /**
       * The tail of a truncated page. It is not a quiet row: the list stops
       * here because the page did, and saying nothing would let the oldest
       * event fetched read as the oldest event there is.
       */
      kind: 'unseen';
      key: string;
      label: string;
      aria: string;
    };

/** The whole reading, or the reason there isn't one. */
export type StreamReading =
  | {
      available: true;
      strip: StreamStrip;
      rows: StreamRow[];
      meta: string;
      /**
       * "No events in this window · last event 10:46" — what the strip says of
       * itself when nothing in the page falls inside the window it draws, and
       * '' when something does (#2282 D15).
       *
       * The list below is deliberately NOT clipped to the window, so an empty
       * strip over a list of events is a normal reading and not a fault; this
       * is the sentence that says which of the two the reader is looking at,
       * and names the last event there WAS so "Load older" has something to
       * reach for.
       */
      emptyWindow: string;
    }
  | { available: false; notice: string };

/** The strip's geometry, in px off DMonitor (y ≈502–572). */
export const STRIP = {
  height: 70,
  axisTop: 40,
  majorTick: 7,
  minorTick: 3,
  tickLabelTop: 52,
  markTop: 16,
  markHeight: 24,
  markWidth: 2,
  /** The dashed run stops short of the marks it joins, on both sides. */
  gapInset: 10,
  gapTop: 27,
  gapLabelTop: 20,
  annotationTop: -4
} as const;

/** The list row's geometry (DMonitor y ≈592–924). */
export const ROW = {
  height: 36,
  gapHeight: 30,
  timeWidth: 84,
  railWidth: 24,
  dotSize: 9,
  dotLeft: 1,
  dotTop: 14,
  railLeft: 5
} as const;

/**
 * One event with its timestamp parsed exactly once. `ms` is `null` for a
 * stamp the backend cannot have meant; the row still renders (as a dash) so
 * an unreadable timestamp never deletes an event from the log.
 */
type Stamped = { event: AuditEvent; ms: number | null };

/** The page, each stamp parsed once, in the order the API returned it. */
function stamp(events: AuditEvent[]): Stamped[] {
  return events.map((event) => ({ event, ms: parseInstant(event.timestamp) }));
}

/** The instants the page is evidence for, oldest first. */
function instants(page: Stamped[]): number[] {
  return page
    .map((e) => e.ms)
    .filter((ms): ms is number => ms !== null)
    .sort((a, b) => a - b);
}

/**
 * The oldest instant a page in hand is evidence for, or `null` where nothing
 * in it carries a stamp that would parse.
 *
 * This is what "Load older" reaches back past, and so the only honest reading
 * for its label (#2248). The next page is fetched with `after=next_after` — the
 * cursor the page in hand ended on — so every event it can return is older than
 * this instant. The window's own start is not: it goes out as `since`, which is
 * INCLUSIVE, so every event the button can load is at or after it and a label
 * that printed it said the opposite of what the request asks for.
 */
export function oldestRead(events: AuditEvent[]): number | null {
  const stamps = instants(stamp(events));
  return stamps.length === 0 ? null : stamps[0];
}

/**
 * The window the strip draws: the last 30 minutes, ending at the next
 * 5-minute mark so the major ticks land on round clock times (DMonitor reads
 * 15:20 … 15:50 for a fleet last heard from at 15:46:58).
 *
 * `stamps` are the page's own instants — the same numbers the list's rows are
 * formatted from. They only ever push the end LATER, and only from inside
 * `MAX_SKEW_MS` of `nowMs`: the backend stamps an audit event from its clock
 * and the strip is drawn against the viewer's, so an event a few seconds "in
 * the future" is ordinary skew, not a bad timestamp. Without the push it would
 * be listed in the rows and have no place on the axis above them, which is the
 * asymmetry #2205 is about. A stamp further ahead than that says nothing about
 * when "now" is, so it is left out of the reckoning entirely — otherwise one
 * event from a clock set to next year takes the window with it and the strip
 * shows 30 minutes in which nothing happened.
 */
export function streamWindow(
  nowMs: number,
  stamps: number[] = []
): { startMs: number; endMs: number } {
  const step = MAJOR_TICK_MINUTES * 60_000;
  // A stamp past the tolerance is not evidence about when "now" is, so the
  // window is the one it would have been without it — which is the newest
  // stamp still INSIDE the tolerance, not `nowMs`. Falling back to the clock
  // would let one event from a wrong clock strip a legitimately skewed event
  // of its mark on the axis while the row below still lists it.
  const inside = stamps.filter((ms) => ms <= nowMs + MAX_SKEW_MS);
  const reach = inside.length > 0 ? Math.max(...inside) : nowMs;
  const endMs = Math.ceil(Math.max(nowMs, reach) / step) * step;
  return { startMs: endMs - WINDOW_MINUTES * 60_000, endMs };
}

/**
 * The tick spacings the axis may use, finest first. Each is a round amount of
 * clock time, because the ticks are laid out on the clock and not on the
 * window's own left edge: a label is only worth reading if it falls on a time a
 * person would say out loud.
 */
const TICK_LADDER_MS = [
  10_000,
  15_000,
  30_000,
  60_000,
  2 * 60_000,
  5 * 60_000,
  10 * 60_000,
  15 * 60_000,
  30 * 60_000,
  3_600_000,
  2 * 3_600_000,
  3 * 3_600_000,
  6 * 3_600_000,
  12 * 3_600_000,
  86_400_000,
  2 * 86_400_000
] as const;

/** At most this many labelled ticks, whatever the window's span. */
export const MAX_MAJOR_TICKS = 8;

/** The unit the spacing is counted in past the ladder's coarsest rung. */
const DAY_MS = 86_400_000;

/**
 * The major and minor spacing for a window of any span (#2248). The 30-minute
 * default resolves to the 5-minute major and 1-minute minor the artboard draws,
 * which is why the strip's existing readings are unchanged by the brush.
 *
 * `MAX_MAJOR_TICKS` holds for EVERY span, including the ones no rung of the
 * ladder is coarse enough for. The window arrives from the URL, so its span is
 * whatever an operator or a link says it is; falling through to the coarsest
 * rung gave a 90-day window 45 two-day majors, and a year-long one tens of
 * thousands of ticks to lay out. Past the ladder the spacing is counted in whole
 * days instead, which is still a round amount of clock time.
 */
export function tickStepsFor(spanMs: number): { minorMs: number; majorMs: number } {
  const fitted = TICK_LADDER_MS.find((step) => spanMs / step <= MAX_MAJOR_TICKS);
  if (fitted === undefined) {
    const majorMs = Math.ceil(spanMs / MAX_MAJOR_TICKS / DAY_MS) * DAY_MS;
    // Nothing on the ladder divides a major this wide, and a minor that did not
    // divide it would be a row of unlabelled ticks its majors never land on —
    // `ticksFor` steps by the minor and labels the multiples of the major. So at
    // these spans every tick is a labelled one.
    return { minorMs: majorMs, majorMs };
  }
  // A minor tick subdivides its major, so it is the coarsest step that fits at
  // least three times into it; where none does, every tick is a major one.
  const minors = TICK_LADDER_MS.filter((step) => step <= fitted / 3);
  return { minorMs: minors[minors.length - 1] ?? fitted, majorMs: fitted };
}

/** A year of days — the span a "MM-DD HH:MM" label wraps around in. */
const YEAR_MS = 365 * DAY_MS;

/**
 * A major tick's label: enough fields of the clock to tell every major on the
 * axis apart, and no more.
 *
 * An axis carries at most `MAX_MAJOR_TICKS` steps between its first major and
 * its last, so `majorMs` times that is the span a label has to stay distinct
 * across — and the label carries whatever field that span can wrap around.
 * Seconds appear only on a window fine enough to need them. The date appears
 * once the axis can cross midnight, because "00:00" twice over is two majors a
 * reader cannot tell apart. The year appears — in place of the time of day,
 * which at these spacings is always the same — once the axis can cross a new
 * year: eight majors 73 days apart span 584 days, and "10-02 00:00" was printed
 * twice on that axis. All from `$lib/time`, in the viewer's zone, like every
 * other time here.
 */
function tickLabel(at: number, majorMs: number): string {
  const axisMs = majorMs * MAX_MAJOR_TICKS;
  if (axisMs >= YEAR_MS) return fmtDateTime(at).slice(0, 10);
  if (axisMs >= DAY_MS) return fmtDateTime(at).slice(5, 16);
  return fmtTime(at, majorMs < 60_000);
}

function ticksFor(startMs: number, endMs: number): StreamTick[] {
  const span = endMs - startMs;
  if (span <= 0) return [];
  const { minorMs, majorMs } = tickStepsFor(span);
  const ticks: StreamTick[] = [];
  // Laid out on the clock: the first tick is the first round time inside the
  // window, not its left edge, so a brushed window's labels still read 03:00.
  for (let at = Math.ceil(startMs / minorMs) * minorMs; at <= endMs; at += minorMs) {
    const major = at % majorMs === 0;
    const pct = ((at - startMs) / span) * 100;
    ticks.push({
      pct,
      major,
      label: major ? tickLabel(at, majorMs) : null,
      align: pct <= 0 ? 'start' : pct >= 100 ? 'end' : 'center'
    });
  }
  return ticks;
}

/** Consecutive events no more than `BURST_SPACING_MS` apart, oldest first. */
function clusters(timed: { ms: number }[]): { ms: number }[][] {
  const out: { ms: number }[][] = [];
  for (const point of timed) {
    const last = out[out.length - 1];
    if (last && point.ms - last[last.length - 1].ms <= BURST_SPACING_MS) last.push(point);
    else out.push([point]);
  }
  return out;
}

/**
 * The unread run at the old end of the strip, or `null` when the page reaches
 * back past `startMs` and the window is therefore fully accounted for — a
 * truncated page is not by itself a hole, only one that stops inside the
 * window is. A page with no readable timestamp at all covers none of it.
 *
 * `stamps` is the whole page's instants, not the strip's slice: an event
 * outside the window still bounds how far back the page is evidence for.
 */
function unseenFor(
  stamps: number[],
  truncated: boolean,
  startMs: number,
  endMs: number
): StreamUnseen | null {
  if (!truncated) return null;
  const floorMs = stamps.length > 0 ? stamps[0] : endMs;
  if (floorMs <= startMs) return null;
  const toPct = ((Math.min(floorMs, endMs) - startMs) / (endMs - startMs)) * 100;
  return { fromPct: 0, toPct, midPct: toPct / 2, label: STREAM_UNSEEN };
}

function stripFor(
  page: Stamped[],
  nowMs: number,
  truncated: boolean,
  window?: StreamSpan
): StreamStrip {
  const stamps = instants(page);
  const { startMs, endMs } = window ?? streamWindow(nowMs, stamps);
  const span = endMs - startMs;
  const inWindow = page
    .filter((e): e is { event: AuditEvent; ms: number } => e.ms !== null)
    .filter((e) => e.ms >= startMs && e.ms <= endMs)
    .sort((a, b) => a.ms - b.ms);

  const pctAt = (ms: number) => ((ms - startMs) / span) * 100;

  const marks = inWindow.map(({ event, ms }) => ({
    id: event.id,
    pct: pctAt(ms),
    category: eventCategory(event),
    annotation: annotation(event)
  }));

  const gaps: StreamGap[] = [];
  for (let i = 1; i < inWindow.length; i += 1) {
    const quiet = inWindow[i].ms - inWindow[i - 1].ms;
    if (quiet < QUIET_GAP_MS) continue;
    const fromPct = pctAt(inWindow[i - 1].ms);
    const toPct = pctAt(inWindow[i].ms);
    gaps.push({ fromPct, toPct, midPct: (fromPct + toPct) / 2, label: `${duration(quiet)} quiet` });
  }

  const bursts = clusters(inWindow)
    .filter((run) => run.length >= BURST_MIN_EVENTS)
    .map((run) => {
      const spanned = run[run.length - 1].ms - run[0].ms;
      return {
        pct: pctAt(run[run.length - 1].ms),
        count: run.length,
        label: `${run.length} events in ${duration(spanned)}`
      };
    });

  return {
    startMs,
    endMs,
    ticks: ticksFor(startMs, endMs),
    marks,
    gaps,
    bursts,
    unseen: unseenFor(stamps, truncated, startMs, endMs)
  };
}

/**
 * The advance of one character of the strip's 11px mono type, in px: IBM Plex
 * Mono is 600 units to the em, as is every fallback in `--font-mono`.
 */
export const STRIP_CHAR_PX = 6.6;

/** The px the strip keeps clear between two labels on one line. */
const LABEL_GUTTER_PX = 8;

/** The px a gap label's ink ground adds on each side (`px-2.5`). */
const GAP_LABEL_PAD_PX = 10;

const labelPx = (label: string) => label.length * STRIP_CHAR_PX;

/**
 * The strip with only the words that fit it at `widthPx` (#2248).
 *
 * The thresholds the strip names things by — five minutes is a quiet gap,
 * events a minute apart are a burst — are clock time, which is what the
 * 30-minute strip was drawn for. A brushed window is any span, and over six
 * hours the same rules name dozens of gaps and bursts a few pixels apart: the
 * labels printed over each other, over the marks, and past the strip's left
 * edge into the column beside it. Every mark, gap and burst is still drawn;
 * what this decides is which of them also gets words.
 *
 * - A quiet gap (and the unread run) keeps its label only where the run is wide
 *   enough to hold it. The label sits on an ink ground across its own run, so a
 *   wider label covered the marks on either side of it.
 * - The line above the marks is placed, never overlapped and never past either
 *   edge: a money mark's annotation first, oldest first, then the bursts,
 *   biggest first. One that has no room is not drawn; the list below names
 *   every event and every gap, so nothing is lost but the crowding.
 *
 * `widthPx` of zero is the first frame, before the strip has been measured:
 * there is nothing to fit against yet, so the strip is returned as composed.
 */
export function fitStrip(strip: StreamStrip, widthPx: number): StreamStrip {
  if (widthPx <= 0) return strip;
  const px = (pct: number) => (pct / 100) * widthPx;
  const roomy = (run: StreamGap, inset: number) =>
    labelPx(run.label) + 2 * GAP_LABEL_PAD_PX <= px(run.toPct - run.fromPct) - inset;

  const taken: [number, number][] = [];
  /** Claims [left, right] on the line above the marks, if it is free and on the strip. */
  const claim = (left: number, right: number): boolean => {
    if (left < 0 || right > widthPx) return false;
    const clash = taken.some(
      ([a, b]) => left < b + LABEL_GUTTER_PX && right > a - LABEL_GUTTER_PX
    );
    if (!clash) taken.push([left, right]);
    return !clash;
  };

  // An annotation starts at its mark and reads rightwards (`margin-left` is
  // half the mark, so it starts at the mark's own left edge).
  const marks = strip.marks.map((mark) => {
    if (mark.annotation === null) return mark;
    const left = px(mark.pct) - STRIP.markWidth / 2;
    return claim(left, left + labelPx(mark.annotation)) ? mark : { ...mark, annotation: null };
  });

  // A burst is labelled at its newest mark and reads leftwards from it.
  const bursts = strip.bursts
    .map((burst, order) => ({ burst, order }))
    .sort((a, b) => b.burst.count - a.burst.count || a.order - b.order)
    .filter(({ burst }) => claim(px(burst.pct) - labelPx(burst.label), px(burst.pct)))
    .sort((a, b) => a.order - b.order)
    .map(({ burst }) => burst);

  return {
    ...strip,
    marks,
    bursts,
    gaps: strip.gaps.map((gap) =>
      roomy(gap, 2 * STRIP.gapInset) ? gap : { ...gap, label: '' }
    ),
    unseen:
      strip.unseen === null || roomy(strip.unseen, STRIP.gapInset)
        ? strip.unseen
        : { ...strip.unseen, label: '' }
  };
}

/**
 * The list, newest first, with a quiet row inserted wherever two neighbours
 * are five minutes or more apart. The list is not clipped to the strip's
 * window: an idle fleet must still show its last events, under one long quiet
 * row that says how long ago they were.
 *
 * A truncated page closes with a row saying so. Without it the oldest event
 * fetched reads as the oldest event there is, which is the same mistake the
 * strip's unread run avoids at the other end of the same page.
 */
function rowsFor(page: Stamped[], truncated: boolean): StreamRow[] {
  const rows: StreamRow[] = [];
  page.forEach(({ event, ms }, i) => {
    // The line and the payload behind it are `event-line.ts`'s reading, so the
    // list and the expand under a row cannot state different facts (#2210).
    const read = eventLine(event);
    // The item the event's own columns name — `item_id` for a tracker item, the
    // issue number otherwise. Read off the columns rather than out of the line,
    // so a link cannot point somewhere the row does not say (#2248).
    const itemId = event.item_id ?? (event.issue_num === null ? null : String(event.issue_num));
    // A merge-queue row's subject is half its move (#2982): "fix main" means
    // nothing without the repo, nor "re-review, then re-add" without the PR.
    // So it rides in the lead, which wraps and is never cut, not in the rest
    // the row truncates — on a phone that rest had no room left at all.
    const targeted = read.subject !== '' && queueAttention(event) !== null;
    rows.push({
      kind: 'event',
      key: `e${event.id}`,
      id: event.id,
      time: ms === null ? NO_TIME : fmtTime(ms),
      title: ms === null ? '' : utcTitle(ms),
      lead: targeted ? `${read.label} · ${read.subject}` : read.label,
      rest: [targeted ? '' : read.subject, read.plain].filter((part) => part !== '').join(' · '),
      code: read.plain === read.fact ? '' : read.fact,
      detail: read.detail,
      category: eventCategory(event),
      ref: itemId === null ? null : itemRef(event.item_id, event.issue_num),
      itemId,
      runId: event.run_id
    });
    const older = page[i + 1];
    if (!older) return;
    const before = older.ms;
    if (ms === null || before === null) return;
    const quiet = ms - before;
    if (quiet < QUIET_GAP_MS) return;
    rows.push({
      kind: 'gap',
      key: `q${event.id}`,
      label: `${duration(quiet)} quiet`,
      aria: `${spellDuration(quiet)} without events`
    });
  });
  if (truncated && rows.length > 0) {
    rows.push({ kind: 'unseen', key: 'unseen', label: STREAM_UNSEEN, aria: STREAM_UNSEEN_ARIA });
  }
  return rows;
}

/**
 * "last event 15:46:58 · 5 events" — the head's right-hand reading. A
 * truncated page counts what it holds, not what the log holds, so it says
 * "newest 50 events" rather than passing a page size off as a total.
 */
function metaFor(page: Stamped[], truncated: boolean): string {
  const plural = page.length === 1 ? '' : 's';
  const count = truncated ? `newest ${page.length} event${plural}` : `${page.length} event${plural}`;
  if (page.length === 0) return count;
  return `last event ${fmtTime(page[0].ms)} · ${count}`;
}

/**
 * The sentence an empty window says (#2282 D15), or '' where the window holds
 * events. "Live" is the window that follows the clock, where "yet" is a
 * promise that can still be kept; every other window is a window that has
 * been chosen, and the only honest thing to add to it is the last event there
 * was — without seconds, as the "Load older" label beside it reads.
 */
function emptyWindowFor(page: Stamped[], marks: number, live: boolean): string {
  if (marks > 0) return '';
  const last = page.map((e) => e.ms).find((ms): ms is number => ms !== null);
  if (last === undefined) return live ? STREAM_EMPTY : STREAM_EMPTY_WINDOW;
  return `${STREAM_EMPTY_WINDOW} · last event ${fmtTime(last, false)}`;
}

/**
 * The whole reading, from one page of the log and one clock reading. Pure:
 * the same page composed twice at the same `nowMs` is the same reading, which
 * is what lets the poll re-render every 5s without the strip moving under the
 * reader.
 *
 * `window` is the brushed window (#2248). Where it is given the strip is drawn
 * for exactly it — the axis does not snap to a 5-minute mark and does not chase
 * a skewed stamp, because the operator chose those edges and the overview above
 * is drawing the same two. Where it is absent the strip is the last 30 minutes,
 * ending at the next 5-minute mark, as it was before the brush existed.
 */
export function composeStream(
  source: Source<AuditList>,
  nowMs: number,
  window?: StreamSpan
): StreamReading {
  if (isUnavailable(source)) return { available: false, notice: source.reason };
  // Parsed once, here: the strip's axis, its marks and the list's times are
  // then three readings of the same instants rather than three parses of the
  // same strings (#2205).
  const page = stamp(source.events);
  // `next_after` is the API's truncation signal: set only when older events
  // exist beyond this page. Discarding it is what lets a page that stops
  // inside the window draw the minutes before it as silence.
  const truncated = source.next_after !== null;
  const strip = stripFor(page, nowMs, truncated, window);
  return {
    available: true,
    strip,
    rows: rowsFor(page, truncated),
    meta: metaFor(page, truncated),
    // A mark is one event inside the window, so an unmarked strip is an empty
    // window — the same count the axis above is drawn from, never a second
    // derivation of "is there anything here?".
    emptyWindow: emptyWindowFor(page, strip.marks.length, window === undefined)
  };
}
