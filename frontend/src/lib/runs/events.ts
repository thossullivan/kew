/**
 * #2300 (C3) — the run page's event list, as readings rather than as markup.
 *
 * The page is a split: the run's phase timeline on top, drawn on the shared
 * axis (`$lib/time/axis`), and the event list below. Everything that decides
 * WHAT the list shows lives here, because all of it is a function of the
 * ledger page the server returned — the phases, the filter, the kind counts,
 * the presets, which row a permalink points at — and a reading that lives in a
 * component can only be checked by photographing it.
 *
 * Three rules this module exists to hold:
 *
 *   * **A phase is derived, never invented.** The run's phases are read off the
 *     recorded event names; a run whose events this vocabulary has no word for
 *     gets its own names shown back, not a stage-shaped guess.
 *   * **The filter is URL state.** `phase:`, `type:` and free text round-trip
 *     through one query param, so a filter survives a reload and a link
 *     carries it. Clearing the filter drops what was TYPED and keeps what was
 *     CLICKED — the selection is the operator's place in the run, not a
 *     filter term.
 *   * **A preset is built from the data.** "Errors", "Slowest phases" and
 *     "Cost spikes" are unavailable when this run has none, rather than
 *     offered and then answering nothing.
 *
 * Re-implemented from the description in #2300; no Dagster source is adapted
 * here (the axis arithmetic it draws on carries its own attribution).
 */
import { RUN_AUDIT_PAGE, type RunAuditEvent } from '$lib/api';
import { runHref } from '$lib/flight-recorder';
import { parseInstant } from '$lib/time';
import { eventLabel } from '$lib/factory/names';
import { isErrorEvent, isHoldEvent, stageOf } from './event-words';

// --- phases ----------------------------------------------------------------

/** One named segment of the run's clock. */
export type RunPhase = {
  /** The recorded event name the segment is attributed to. */
  name: string;
  startMs: number;
  /** The next segment's start, or the run's end for the last one. */
  endMs: number;
  /** Events in the segment — a phase of one event is still a phase. */
  count: number;
  /** Summed cost recorded inside it (0 when nothing recorded a figure). */
  costUsd: number;
  /** Whether anything in it reads as a failure (see `isErrorEvent`). */
  failed: boolean;
  /** Whether anything in it is a deferral or a hold (see `isHoldEvent`). */
  held: boolean;
  /** Ledger seq of its first event: the segment's identity, stable per run. */
  seq: number;
};

/** How long a phase lasted. Never negative; zero for an instant. */
export const phaseDurationMs = (phase: Pick<RunPhase, 'startMs' | 'endMs'>): number =>
  Math.max(0, phase.endMs - phase.startMs);

/**
 * The run's phases: consecutive events sharing a name, collapsed.
 *
 * A segment runs from its first event to the NEXT segment's first event, which
 * is the only honest reading available — the ledger records when something
 * happened, never how long it then took. `endMs` closes the last segment (the
 * run's `finished_at`, or now for a live run); an `endMs` before the last
 * event is ignored rather than drawn backwards.
 */
export function runPhases(events: RunAuditEvent[], endMs: number): RunPhase[] {
  const phases: RunPhase[] = [];
  for (const event of events) {
    const ms = parseInstant(event.ts);
    if (ms === null) continue; // a timestamp that cannot be read has no place
    const open = phases[phases.length - 1];
    if (open && open.name === event.event) {
      open.count += 1;
      open.endMs = Math.max(open.endMs, ms);
      open.costUsd += eventCostUsd(event);
      open.failed = open.failed || isErrorEvent(event);
      open.held = open.held || isHoldEvent(event);
      continue;
    }
    if (open) open.endMs = ms;
    phases.push({
      name: event.event,
      startMs: ms,
      endMs: ms,
      count: 1,
      costUsd: eventCostUsd(event),
      failed: isErrorEvent(event),
      held: isHoldEvent(event),
      seq: event.seq
    });
  }
  const last = phases[phases.length - 1];
  if (last) last.endMs = Math.max(last.endMs, endMs);
  return phases;
}

/** The window the timeline draws: the run's first event to its end. */
export function phaseSpan(phases: RunPhase[], endMs: number): { startMs: number; endMs: number } {
  if (phases.length === 0) return { startMs: endMs, endMs };
  return { startMs: phases[0].startMs, endMs: Math.max(endMs, phases[phases.length - 1].endMs) };
}

// --- named stages ------------------------------------------------------------

/**
 * One named segment of the timeline lane (#3458): consecutive phases that open
 * the same stage — dispatch, sandbox, work, seal, publish, review, done, or a
 * kind's own label — drawn as one bar with its name under it.
 *
 * A stage is a way of DRAWING phases, not a new filter term: clicking it
 * selects the phases it holds (`toggleStage`), so the `phase:` tokens in the
 * URL, the presets and every link already written keep meaning what they did.
 */
export type RunStage = {
  /** The name drawn under the bar. Never empty. */
  label: string;
  /** The recorded phase names it holds, in order and without repeats. */
  phases: string[];
  startMs: number;
  endMs: number;
  count: number;
  /** Summed cost recorded inside it. */
  costUsd: number;
  failed: boolean;
  held: boolean;
  /** Ledger seq of its first event: its identity, stable per run. */
  seq: number;
};

export function runStages(phases: RunPhase[]): RunStage[] {
  const stages: RunStage[] = [];
  for (const phase of phases) {
    const label = stageOf(phase.name);
    const open = stages[stages.length - 1];
    if (open && open.label === label) {
      if (!open.phases.includes(phase.name)) open.phases.push(phase.name);
      open.endMs = phase.endMs;
      open.count += phase.count;
      open.costUsd += phase.costUsd;
      open.failed = open.failed || phase.failed;
      open.held = open.held || phase.held;
      continue;
    }
    const { startMs, endMs, count, costUsd, failed, held, seq } = phase;
    stages.push({ label, phases: [phase.name], startMs, endMs, count, costUsd, failed, held, seq });
  }
  return stages;
}

/** Whether every phase a stage holds is selected — the bar draws pressed. */
export const stageSelected = (filter: RunFilter, stage: RunStage): boolean =>
  stage.phases.every((name) => filter.phases.includes(name));

/**
 * A click on a stage: `togglePhase` over every phase it holds. A plain click
 * selects the stage alone, or clears it when it was already the whole
 * selection; shift-click adds it, or removes it when it was all selected.
 */
export function toggleStage(filter: RunFilter, stage: RunStage, additive = false): RunFilter {
  const names = stage.phases;
  const all = stageSelected(filter, stage);
  if (additive) {
    const next = all
      ? filter.phases.filter((p) => !names.includes(p))
      : dedupe([...filter.phases, ...names]);
    return { ...filter, phases: next };
  }
  const sole = all && filter.phases.length === names.length;
  return { ...filter, phases: sole ? [] : [...names] };
}

/** Where a stage's name lands under the lane: its x, and which line it is on. */
export type LanePlace = { x: number; row: number };

/**
 * Place every stage's name under the lane so none overlaps another and none
 * runs past the plot (#3458: "every phase segment is named").
 *
 * Each name goes on the first line where it fits at its bar's start, nudged
 * right past the name before it and pulled left only as far as it must be to
 * end inside the plot; a name that fits on no line opens a new one. A
 * run of one-second stages therefore reads as a row of names, not a pile.
 */
export function placeLaneLabels(
  items: readonly { x: number; width: number }[],
  plotW: number,
  gap = 8
): LanePlace[] {
  const ends: number[] = [];
  return items.map(({ x, width }) => {
    for (let row = 0; row < ends.length; row += 1) {
      const after = ends[row] + gap;
      const at = Math.min(Math.max(x, after), plotW - width);
      if (at >= after) {
        ends[row] = at + width;
        return { x: at, row };
      }
    }
    const at = Math.max(0, Math.min(x, plotW - width));
    ends.push(at + width);
    return { x: at, row: ends.length - 1 };
  });
}

/**
 * Share one band of the lane among the marks drawn in it — the bars, or one
 * line of names — so every mark's press is its own (#3458).
 *
 * Each press is widened to `min` around its mark and kept inside the plot, but
 * never past the midpoint between its mark and the next one along: two presses
 * never overlap, so the bar or name a reader presses is always the stage it
 * filters to, whichever was drawn last. Returned in the order given.
 */
export function bandPresses(
  items: readonly { x: number; width: number }[],
  plotW: number,
  min: number
): { x: number; width: number }[] {
  const order = items.map((_, i) => i).sort((a, b) => items[a].x - items[b].x || a - b);
  const bounds: number[] = [];
  for (let k = 0; k + 1 < order.length; k += 1) {
    const left = items[order[k]];
    const mid = (left.x + left.width + items[order[k + 1]].x) / 2;
    bounds.push(Math.min(plotW, Math.max(0, mid, bounds[k - 1] ?? 0)));
  }
  const presses: { x: number; width: number }[] = new Array(items.length);
  order.forEach((i, k) => {
    const lo = k === 0 ? 0 : bounds[k - 1];
    const hi = k === order.length - 1 ? plotW : bounds[k];
    const { x, width: own } = items[i];
    const width = Math.max(0, Math.min(hi - lo, Math.max(min, own)));
    presses[i] = { x: Math.max(lo, Math.min(x - (width - own) / 2, hi - width)), width };
  });
  return presses;
}

// --- what an event says about itself ---------------------------------------

// Whether an event is a failure or a hold is the formatter's reading
// (`./event-words`): a deferral records its error and is still a hold.
export { isErrorEvent, isHoldEvent };

/** Cost recorded on this event, or 0. Only a real number counts as a figure. */
export function eventCostUsd(event: RunAuditEvent): number {
  const payload = event.payload ?? {};
  for (const key of ['cost_usd', 'spend_usd', 'estimated_cost_usd']) {
    const value = payload[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return 0;
}

// --- the filter ------------------------------------------------------------

/**
 * What the list admits. `phases` is the operator's SELECTION (clicked on the
 * timeline), `types` the event kinds chosen from the chips, `text` free words
 * matched against the name and the payload.
 */
export type RunFilter = {
  readonly phases: readonly string[];
  readonly types: readonly string[];
  readonly text: string;
  /**
   * Only the events that read as failures (`isErrorEvent`), as `is:error` in
   * the URL. The "Errors" preset sets it beside its kinds, because a kind is
   * not a failure: a `dispatch_completed` that succeeded is the same kind as
   * one that failed, and the preset showed both under a count of one.
   */
  readonly errors?: true;
};

export const EMPTY_RUN_FILTER: RunFilter = { phases: [], types: [], text: '' };

/** The query param the filter travels in. */
export const RUN_FILTER_PARAM = 'q';

/** The token that narrows the list to failures (`RunFilter.errors`). */
const ERRORS_TOKEN = 'is:error';

const dedupe = (values: Iterable<string>): string[] => [...new Set(values)].filter((v) => v !== '');

/**
 * Parse the `q=` token string: `phase:<name>`, `type:<kind>`, anything else is
 * free text. Unknown prefixes stay free text rather than being dropped — a
 * token an operator typed is shown back to them, not swallowed.
 */
export function parseRunFilter(raw: string | null | undefined): RunFilter {
  const phases: string[] = [];
  const types: string[] = [];
  const words: string[] = [];
  let errors = false;
  for (const token of (raw ?? '').trim().split(/\s+/)) {
    if (token === '') continue;
    if (token.startsWith('phase:')) phases.push(token.slice('phase:'.length));
    else if (token.startsWith('type:')) types.push(token.slice('type:'.length));
    else if (token === ERRORS_TOKEN) errors = true;
    else words.push(token);
  }
  const filter: RunFilter = { phases: dedupe(phases), types: dedupe(types), text: words.join(' ') };
  return errors ? { ...filter, errors: true } : filter;
}

/** The same filter as a `q=` value — the exact inverse of `parseRunFilter`. */
export function runFilterQuery(filter: RunFilter): string {
  return [
    ...filter.phases.map((p) => `phase:${p}`),
    ...filter.types.map((t) => `type:${t}`),
    filter.errors ? ERRORS_TOKEN : '',
    filter.text.trim()
  ]
    .filter((part) => part !== '')
    .join(' ');
}

/**
 * Clearing the filter drops what was TYPED and keeps what was CLICKED.
 *
 * The selected phases are where the operator is in the run, not a filter term
 * they entered; a clear that threw them away would make "clear" mean "go back
 * to the top of a 200-event list".
 */
export const clearRunFilter = (filter: RunFilter): RunFilter => ({
  phases: filter.phases,
  types: [],
  text: ''
});

/**
 * Whether pressing "clear" would change anything.
 *
 * `clearRunFilter` keeps the clicked selection, so on a phase-only filter it
 * returns an identical filter: a non-empty filter is NOT the same question as
 * a clearable one, and offering the control there is a button that cannot be
 * pressed to any effect (the console's rule, `poll-hygiene.test.ts`). A phase
 * selection is dropped the way it was made — by clicking the bar again.
 */
export const canClearRunFilter = (filter: RunFilter): boolean =>
  filter.types.length > 0 || filter.text.trim() !== '' || filter.errors === true;

/**
 * A click on a phase. Plain click selects that one alone — or clears it, when
 * it was already the only one. Shift-click adds and removes.
 */
export function togglePhase(filter: RunFilter, name: string, additive = false): RunFilter {
  if (additive) {
    const next = filter.phases.includes(name)
      ? filter.phases.filter((p) => p !== name)
      : [...filter.phases, name];
    return { ...filter, phases: next };
  }
  const sole = filter.phases.length === 1 && filter.phases[0] === name;
  return { ...filter, phases: sole ? [] : [name] };
}

/** A click on a kind chip: chips always add and remove, never replace. */
export function toggleKind(filter: RunFilter, kind: string): RunFilter {
  const next = filter.types.includes(kind)
    ? filter.types.filter((t) => t !== kind)
    : [...filter.types, kind];
  return { ...filter, types: next };
}

const haystack = (event: RunAuditEvent): string =>
  `${event.event} ${eventLabel(event.event)} ${event.actor_type ?? ''} ${event.item_id ?? ''} ${
    event.payload ? JSON.stringify(event.payload) : ''
  }`.toLowerCase();

/**
 * The events the filter admits, in the order they were recorded.
 *
 * The three terms AND together and each is an OR within itself: a phase
 * selection of two phases plus a kind selection of one shows that kind inside
 * either phase. Free words must all appear somewhere in the row.
 */
export function filterRunEvents(
  events: RunAuditEvent[],
  filter: RunFilter,
  phases: RunPhase[] = runPhases(events, 0)
): RunAuditEvent[] {
  // The phase lookup is built only when a phase is actually selected: the
  // unfiltered list is the common case and walks the phases for nothing.
  const inPhase = filter.phases.length > 0 ? phaseOfSeq(phases) : null;
  const words = filter.text
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter((w) => w !== '');
  return events.filter((event) => {
    if (inPhase !== null && !filter.phases.includes(inPhase(event.seq) ?? '')) return false;
    if (filter.types.length > 0 && !filter.types.includes(event.event)) return false;
    if (filter.errors && !isErrorEvent(event)) return false;
    if (words.length === 0) return true;
    const text = haystack(event);
    return words.every((word) => text.includes(word));
  });
}

/**
 * Which phase a seq fell in. Phases are contiguous by construction, so the
 * last one that starts at or before the seq is the answer.
 */
export function phaseOfSeq(phases: RunPhase[]): (seq: number) => string | null {
  return (seq: number) => {
    let found: string | null = null;
    for (const phase of phases) {
      if (phase.seq > seq) break;
      found = phase.name;
    }
    return found;
  };
}

// --- kind chips ------------------------------------------------------------

export type KindChip = { kind: string; count: number; selected: boolean };

/**
 * One chip per recorded event kind with its count, commonest first and ties
 * broken by name so the row does not reshuffle between polls.
 *
 * The count is over the whole page, not over the filtered view: a chip says
 * how much it would show, which is the question a reader asks of it.
 */
export function kindChips(events: RunAuditEvent[], filter: RunFilter): KindChip[] {
  const counts = new Map<string, number>();
  for (const event of events) counts.set(event.event, (counts.get(event.event) ?? 0) + 1);
  return [...counts]
    .map(([kind, count]) => ({ kind, count, selected: filter.types.includes(kind) }))
    .sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind));
}

// --- presets ---------------------------------------------------------------

export type RunPresetId = 'errors' | 'slowest' | 'cost';

export type RunPreset = {
  id: RunPresetId;
  label: string;
  /** False when this run holds nothing the preset would show. */
  available: boolean;
  /** How many rows its filter shows: `filterRunEvents` over this run, so the
   * number on the chip is the number of rows pressing it draws. */
  count: number;
  filter: RunFilter;
};

/** How many timeline stages "Slowest phases" and "Cost spikes" select. */
export const PRESET_PHASES = 3;

/**
 * The phases of the heaviest stages, whole (#3458): the timeline draws a
 * stage as one bar and presses it as one, so a preset that picked single
 * phases selected rows of a stage the lane could not draw as selected — and
 * pressing that stage then replaced the selection instead of reading it.
 */
const topStages = (stages: RunStage[], weight: (stage: RunStage) => number): string[] =>
  dedupe(
    [...stages]
      .filter((stage) => weight(stage) > 0)
      .sort((a, b) => weight(b) - weight(a))
      .slice(0, PRESET_PHASES)
      .flatMap((stage) => stage.phases)
  );

/**
 * The three presets, built from this run's own events.
 *
 * Each is `available: false` when the run holds nothing it would select —
 * a run that failed nowhere offers no "Errors", and a free local run offers no
 * "Cost spikes" — so the control is honest about what pressing it would do
 * instead of answering an empty list. "Errors" narrows its kinds to the
 * events that failed (`is:error`), so a kind that also succeeded is not shown
 * as an error.
 */
export function runPresets(events: RunAuditEvent[], phases: RunPhase[]): RunPreset[] {
  const errorKinds = dedupe(events.filter(isErrorEvent).map((event) => event.event));
  const stages = runStages(phases);
  const slowest = topStages(stages, phaseDurationMs);
  const costly = topStages(stages, (stage) => stage.costUsd);
  const preset = (id: RunPresetId, label: string, filter: RunFilter, applies: boolean): RunPreset => ({
    id,
    label,
    available: applies,
    count: applies ? filterRunEvents(events, filter, phases).length : 0,
    filter
  });
  return [
    preset('errors', 'Errors', { phases: [], types: errorKinds, text: '', errors: true }, errorKinds.length > 0),
    preset('slowest', 'Slowest phases', { phases: slowest, types: [], text: '' }, slowest.length > 0),
    preset('cost', 'Cost spikes', { phases: costly, types: [], text: '' }, costly.length > 0)
  ];
}

// --- the focused time permalink -------------------------------------------

/** The query param a timestamp permalink writes. */
export const FOCUSED_TIME_PARAM = 'focusedTime';

/**
 * The row a `?focusedTime=` points at: the event recorded at that instant, the
 * last one before it, or — when nothing precedes it — the first one after.
 *
 * Nearest rather than exact-only, because the parameter also arrives from a
 * Gantt bar, where the instant is the run's recorded `started_at` and not a
 * timestamp the ledger ever holds. The fall-forward arm is what makes that
 * link work at all: a run's own events are all recorded AFTER the row's
 * `started_at` (the row exists before anything is appended under its id), so
 * nearest-before alone resolved every bar link to nothing — no marker, no row
 * highlight, no caption, silently.
 */
export function focusedRunEvent(
  events: RunAuditEvent[],
  focusedTime: string | null | undefined
): RunAuditEvent | null {
  const target = parseInstant(focusedTime ?? null);
  if (target === null) return null;
  let before: RunAuditEvent | null = null;
  let after: RunAuditEvent | null = null;
  for (const event of events) {
    const ms = parseInstant(event.ts);
    if (ms === null) continue;
    if (ms === target) return event;
    if (ms < target) before = event;
    else if (after === null) after = event;
  }
  return before ?? after;
}

// --- the live tail --------------------------------------------------------

/**
 * The batching window a live run's new events are applied in.
 *
 * Distinct from how often the tail is READ (`TAIL_POLL_MS`): events arrive in
 * pages and are folded into the list on this beat, so a page carrying thirty
 * rows is one update rather than thirty. A read cadence of 100ms would be ten
 * requests a second for a run that may last an hour — the poll nobody counted
 * (#2285 C7), which is the opposite of what batching is for.
 */
export const TAIL_BATCH_MS = 100;

/** How often a live run's forward cursor is read. */
export const TAIL_POLL_MS = 1000;

/**
 * How often a tailing page re-reads the run's OWN row.
 *
 * The `run` prop is loader data and nothing on this page invalidates it — no
 * interval on the page or the layout, and the shared pulse does not invalidate
 * a loader — so a run that finishes while the page is open would stay "live"
 * for the rest of the session and the tail would poll a dead run for ever. The
 * page therefore asks the row for itself, on a tenth of the tail's cadence
 * because a status changes once per run where a cursor moves every second.
 */
export const STATUS_POLL_MS = 10_000;

/**
 * Whether a run is still executing, given the loader's reading and the page's
 * own fresher one.
 *
 * Both must say live. A run never goes back to executing, so the conjunction
 * is monotone: a stale prop can never re-arm a timer the row has already
 * stopped, and a read that has not happened yet (`null`) defers to the prop.
 */
export const stillLive = (
  live: ReadonlySet<string>,
  propStatus: string | null | undefined,
  readStatus: string | null
): boolean =>
  propStatus != null && live.has(propStatus) && (readStatus === null || live.has(readStatus));

/** The hover-to-marker debounce. */
export const HOVER_DEBOUNCE_MS = 100;

/**
 * Fold a newly read page into what is already shown.
 *
 * Keyed by ledger seq, so a page re-read after a dropped connection (the
 * resume cursor is inclusive of nothing, but a server restart can repeat) adds
 * nothing twice, and the result stays in recorded order.
 */
export function mergeRunEvents(
  existing: RunAuditEvent[],
  incoming: RunAuditEvent[]
): RunAuditEvent[] {
  if (incoming.length === 0) return existing;
  const bySeq = new Map(existing.map((event) => [event.seq, event]));
  for (const event of incoming) bySeq.set(event.seq, event);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/** The cursor to resume a live read from: the newest seq held, or 0. */
export const tailCursor = (events: RunAuditEvent[]): number =>
  events.length === 0 ? 0 : events[events.length - 1].seq;

// --- further pages --------------------------------------------------------

/**
 * Whether the page just read may be followed by another.
 *
 * `next_seq` is a resume cursor, not a has-more flag (see `$lib/api`), so the
 * only signal the server gives is a FULL page: a read that came back at the
 * cap has more behind it, and a short one is the end of the ledger as it
 * stands. A finished run with more events than the cap used to be truncated in
 * silence — the loader read page 0, forward paging existed only inside the
 * live tail, and the header counted the 200 rows it held as the whole run.
 */
export const mayHoldMoreRunEvents = (
  page: { events: RunAuditEvent[] } | null,
  limit = RUN_AUDIT_PAGE
): boolean => page !== null && page.events.length >= limit;

/**
 * The list's count, which must never pass a truncated page off as the run.
 *
 * `more` adds the `+`: "30 of 200+" says there is ledger this page has not
 * read, where "30 of 200" claimed 200 was all there was.
 */
export const runEventsCount = (shown: number, held: number, more: boolean): string =>
  `${shown} of ${held}${more ? '+' : ''}`;

/** How close to the bottom still counts as reading the tail. */
export const TAIL_SLACK_PX = 24;

/**
 * Whether the reader is at the bottom of the list.
 *
 * The list follows the tail only while this is true: a live run that scrolled
 * the reader away from the line they were reading would make the page unusable
 * exactly when it matters most.
 */
export function followsTail(
  box: { scrollTop: number; clientHeight: number; scrollHeight: number },
  slackPx = TAIL_SLACK_PX
): boolean {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= slackPx;
}

// --- debounce -------------------------------------------------------------

export type Debounced<T> = {
  (value: T): void;
  cancel(): void;
};

/**
 * Trailing debounce, used for the hover marker.
 *
 * Both setting and clearing the mark go through the one timer, so a pointer
 * crossing six rows draws one marker at the row it stopped on rather than six
 * in a row — and leaving the list clears it on the same beat instead of
 * flickering against the next row's enter.
 */
export function debounce<T>(apply: (value: T) => void, delayMs = HOVER_DEBOUNCE_MS): Debounced<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const run = (value: T): void => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      apply(value);
    }, delayMs);
  };
  run.cancel = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  return run;
}

// --- empty states --------------------------------------------------------

export const LOADING_EVENTS = 'Loading run events…';
export const NO_KINDS_SELECTED = 'No kinds selected';
export const RUN_QUEUED = 'Run queued';
export const NO_MATCH = 'No events match this filter';

/**
 * What the list says when it has no rows to draw, or null when it has rows.
 *
 * Four different statements, deliberately not one: a read still in flight, a
 * run that has recorded nothing yet, a kind selection that admits nothing in
 * this run (reachable from a permalink taken on another run), and a filter
 * that simply matched nothing.
 */
export function runEventsEmptyState(state: {
  loading: boolean;
  events: RunAuditEvent[];
  shown: RunAuditEvent[];
  filter: RunFilter;
}): string | null {
  if (state.loading && state.events.length === 0) return LOADING_EVENTS;
  if (state.events.length === 0) return RUN_QUEUED;
  if (state.shown.length > 0) return null;
  const kinds = new Set(state.events.map((event) => event.event));
  const selectable = state.filter.types.some((kind) => kinds.has(kind));
  if (state.filter.types.length > 0 && !selectable) return NO_KINDS_SELECTED;
  return NO_MATCH;
}

// --- the link into this page ----------------------------------------------

/**
 * The run page's own deep link: the canonical `/runs/[id]` href from
 * `run-links`/`flight-recorder`, carrying this page's three parameters.
 *
 * `attempt` matters because a run page given none selects the LATEST attempt,
 * which is the wrong evidence for an earlier bar on an epic's Gantt. Every
 * parameter is left out when it has nothing to say, so the plain link stays the
 * plain link.
 */
export function runEventsHref(
  runId: string,
  options: {
    attempt?: number | null;
    focusedTime?: string | null;
    filter?: RunFilter | null;
  } = {}
): string {
  const params = new URLSearchParams();
  if (options.attempt != null) params.set('attempt', String(options.attempt));
  if (options.focusedTime) params.set(FOCUSED_TIME_PARAM, options.focusedTime);
  const query = options.filter ? runFilterQuery(options.filter) : '';
  if (query !== '') params.set(RUN_FILTER_PARAM, query);
  const search = params.toString();
  return `${runHref(runId)}${search === '' ? '' : `?${search}`}`;
}

// --- whose verdict the run page is showing --------------------------------

/** The verdict scope the service projects for a run's own attested round. */
export const VERDICT_SCOPE_RUN = 'run';

/**
 * What the Governed review section says about whose decision it is showing.
 *
 * An implementation run attests no verdict of its own, so the page falls back
 * to the latest one standing over the work item — which may have been recorded
 * for a LATER attempt. Printing its round and reviewed head unqualified made
 * another attempt's decision read as this run's evidence; the scope the
 * service sends is said out loud instead.
 */
export const verdictScopeNote = (scope: string): string =>
  scope === VERDICT_SCOPE_RUN
    ? 'attested by this run'
    : 'the decision standing over this work item — not this run’s own attempt';
