// The epic run timeline (#2271): "where did the time on this epic go?", as
// plain arithmetic over the `/api/epics/{ref}/timeline` projection (#2270).
// `EpicTimeline.svelte` draws what this module computes and decides nothing
// itself — the same split as `canvas-view.ts` under the DAG canvas, and for
// the same reason: the scale, the zoom and the geometry are the part that can
// be wrong in a way no screenshot shows.
//
// Three readings it is built on:
//
// * ONE ROW PER NODE, in the API's order, which is first-activity order. The
//   model does not re-sort: the server owns the ordering (it is the only end
//   that can see a node with no activity at all), and two sorts that agree
//   today are two sorts that disagree after the next change.
// * THE CLOCK IS STILL RUNNING. A band or a run with no end is drawn to NOW,
//   never to the epoch and never to nothing — an epic's open work is most of
//   what the picture is for.
// * STATUS IS SIGNAL. Cyan while kew is working, magenta when a person is the
//   next move, and nothing at all for the quiet states: a band is a claim
//   that the clock is running on somebody, so `queued`, `ready` and `done`
//   make none. Every colour here is a `var(--token)`, never a literal.
import type { EpicTimeline, EpicTimelineNode, EpicTimelineRound, EpicTimelineRun } from '$lib/api';
import { statusShape } from '$lib/factory/status';
import type { KeyDef } from '$lib/keys/registry';
import { runEventsHref } from '$lib/runs/events';
import { fmtDateTime, fmtTime, parseInstant } from '$lib/time';
import {
  LABEL_GAP_PX,
  MIN_LABEL_SPACING_PX,
  NICE_INTERVALS,
  axisIntervals,
  axisMsAt,
  axisSpanMs,
  axisTicks,
  axisX,
  nowLabelBox,
  type AxisWindow
} from '$lib/time/axis';
import { elapsed, fmtUSD } from './format';

// --- Layout, in CSS pixels -------------------------------------------------

/** One row per node. The brief's 28px. */
export const ROW_H = 28;
/** The sticky label column: `#id` and the clipped title. */
export const LABEL_W = 220;
/** The time ruler above the rows. */
export const RULER_H = 26;
/** A run bar, centred in its row. */
export const BAR_H = 10;
/** The offset that centres a bar (or a tick) in its row. */
export const BAR_TOP = (ROW_H - BAR_H) / 2;
/** A verdict dot's diameter; the merge dot takes the same. */
export const DOT_D = 6;
/** What a refused or zero-length run is drawn as instead of a bar. */
export const TICK_W = 2;
/** The left edge a needs-you band carries. */
export const BAND_EDGE_W = 2;
/** The narrowest a band may draw: a status that lasted seconds is still a
 * fact, and a 0px rect is an invisible one. */
export const MIN_BAND_W = 1;
/** The closest two ruler labels may sit before one of them is dropped, and
 * the spacing the tick step is chosen for — the shared axis's own default
 * (`$lib/time/axis`), named here because this module's geometry is written in
 * terms of it. */
export const MIN_TICK_PX = MIN_LABEL_SPACING_PX;

/** The whole chart's height for `count` rows (the ruler is not part of it). */
export const chartHeight = (count: number): number => count * ROW_H;

// --- The time axis ---------------------------------------------------------

/** An interval. */
export type Span = { startMs: number; endMs: number };

/**
 * The time axis: `startMs` sits at x = 0 and `endMs` at x = `width`.
 *
 * Kept in floating point on purpose. Rounding the window to whole
 * milliseconds would move the instant under the pointer by up to half a
 * millisecond on every wheel notch, and a zoom that drifts is the one bug
 * this geometry exists to prevent.
 */
export type Scale = AxisWindow;

/** The visible duration. */
export const windowMs = (scale: Scale): number => axisSpanMs(scale);

/** Where an instant is drawn. */
export const xOf = (scale: Scale, ms: number): number => axisX(scale, ms);

/** Which instant is under a pixel. */
export const msAt = (scale: Scale, x: number): number => axisMsAt(scale, x);

/** The narrowest and widest windows the wheel may reach. Five minutes is as
 * close as an epic's clock is worth reading — the shared axis rules that
 * window with half-minute ticks (#2299); a year is far past any epic, and is
 * here so a pinch cannot send the axis somewhere `Date` cannot format. */
export const MIN_WINDOW_MS = 5 * 60_000;
export const MAX_WINDOW_MS = 365 * 86_400_000;

/** The window an epic with no recorded activity is drawn in — there is no
 * span to fit, and a chart one instant wide draws nothing. */
export const EMPTY_SPAN_MS = 60 * 60_000;

/** Room a fit keeps at each end: a little at the left, and enough at the
 * right for the now cap and the word "merged" to sit beside their marks. */
export const FIT_PAD_PX = { left: 8, right: 64 } as const;

/** How often the now line and every open band and bar are refreshed. */
export const NOW_POLL_MS = 15_000;

/** The instant the server answered with, or the viewer's clock when it sent
 * one that cannot be read. */
export const nowOf = (timeline: EpicTimeline): number =>
  parseInstant(timeline.now) ?? Date.now();

/** The statuses a node can finish in: nothing more will happen to it. */
const SETTLED: ReadonlySet<string> = new Set(['done', 'cancelled']);

/**
 * When a finished epic's last thing happened, or null while anything on it is
 * still open (#2783, N21).
 *
 * The served `span.end` is the serve host's clock, so a complete epic was
 * fitted out to "now": every bar sat in the left tenth and seventeen empty days
 * ran on to the right. An epic whose every node rests in a settled status, with
 * no run still going, has an end of its own — the latest instant it recorded —
 * and that is the end the fit is drawn to. One open node or run, or an epic with
 * no nodes, keeps the clock as the end, which is still the honest picture of
 * something unfinished.
 */
export function settledEnd(timeline: EpicTimeline): number | null {
  if (timeline.nodes.length === 0) return null;
  const stamps: (string | null)[] = [];
  for (const node of timeline.nodes) {
    const final = node.bands.at(-1);
    if (!final || !SETTLED.has(statusShape(final.status).meaning)) return null;
    if (node.runs.some((run) => run.finished_at === null)) return null;
    stamps.push(...node.bands.flatMap((band) => [band.from, band.to]));
    stamps.push(...node.runs.flatMap((run) => [run.started_at, run.finished_at]));
    stamps.push(...node.rounds.map((round) => round.recorded_at), node.merged_at);
  }
  const instants = stamps.map(parseInstant).filter((ms): ms is number => ms !== null);
  return instants.length === 0 ? null : Math.max(...instants);
}

/**
 * The span the API reported, with the clock standing in for an end it could
 * not state and `EMPTY_SPAN_MS` for an epic that has not started.
 *
 * The two ends come from two hosts: `span.end` is the serve host's own clock,
 * while the band and run stamps inside it were written by the driver's. So the
 * earliest activity CAN land after the end, and when it does the window has to
 * carry the end out past it — collapsing to the hour before `now` instead
 * would draw every band and bar off the right edge of a canvas that clips its
 * own overflow, and `F` could not recover it because the fit is handed the
 * same degenerate span. Skewed or not, `now` stays in the picture, which is
 * the one reading that explains what happened.
 */
export function spanOf(timeline: EpicTimeline): Span {
  const endMs = settledEnd(timeline) ?? parseInstant(timeline.span.end) ?? nowOf(timeline);
  const startMs = parseInstant(timeline.span.start);
  if (startMs === null) return { startMs: endMs - EMPTY_SPAN_MS, endMs };
  if (startMs >= endMs) return { startMs: endMs, endMs: startMs + EMPTY_SPAN_MS };
  return { startMs, endMs };
}

/** The scale that shows the whole span — the first view, and what `F` goes
 * back to. Deliberately unclamped: a fit must COVER the span, whatever its
 * length, so it is the one move the zoom limits do not apply to. */
export function fitScale(span: Span, width: number, rightPad: number = FIT_PAD_PX.right): Scale {
  const w = Math.max(1, width);
  const raw = Math.max(1, span.endMs - span.startMs);
  // Solve for the visible window whose pixel pads come to FIT_PAD_PX.
  const frac = Math.min(0.5, (FIT_PAD_PX.left + rightPad) / w);
  const visible = raw / (1 - frac);
  const startMs = span.startMs - (visible * FIT_PAD_PX.left) / w;
  return { startMs, endMs: startMs + visible, width: w };
}

/** The most of a narrow lane a finished epic's fit leaves empty on the right. */
export const SETTLED_PAD_FRAC = 0.1;

/**
 * The fit the chart opens on, and what `F` goes back to: `fitScale` over
 * `spanOf`. A finished epic draws no now cap, so its right pad only has to
 * hold the word "merged" — and on a phone's 170px lane the full 64px was over
 * a third of the chart, empty, beside its last bar (#2783, N21). There it is
 * held to a tenth of the lane.
 */
export function fitTimeline(timeline: EpicTimeline, width: number): Scale {
  const settled = settledEnd(timeline) !== null;
  const right = settled
    ? Math.min(FIT_PAD_PX.right, Math.max(1, width) * SETTLED_PAD_FRAC)
    : FIT_PAD_PX.right;
  return fitScale(spanOf(timeline), width, right);
}

const clampWindow = (ms: number): number =>
  Math.min(MAX_WINDOW_MS, Math.max(MIN_WINDOW_MS, ms));

/**
 * The window a zoom of `factor` lands on, inside the limits — except that a
 * limit may never REVERSE the gesture.
 *
 * `fitScale` is unclamped on purpose (a fit must cover the span), so a fitted
 * window can start outside the zoom range: an epic that took 40 seconds fits
 * narrower than the five-minute floor, and one spanning more than a year fits
 * wider than the ceiling. A plain clamp there answers the first zoom-IN with a
 * jump out to five minutes, and the first zoom-OUT on the long one with a jump
 * in to a year — the picture moving the opposite way to the wheel. On a window
 * already past a limit that limit holds it still instead, so the fit is as far
 * as the gesture goes in that direction.
 */
function zoomWindow(current: number, factor: number): number {
  const next = clampWindow(current / factor);
  if (factor > 1) return Math.min(current, next);
  if (factor < 1) return Math.max(current, next);
  return current;
}

/**
 * Zoom by `factor` about the pixel `x` — the wheel's and the pinch's one
 * move. The instant under `x` stays under `x`, which is what makes a wheel
 * zoom feel like a magnifier rather than a scroll.
 */
export function zoomAt(scale: Scale, factor: number, x: number): Scale {
  const at = msAt(scale, x);
  const next = zoomWindow(windowMs(scale), factor);
  const startMs = at - (next * x) / Math.max(1, scale.width);
  return { startMs, endMs: startMs + next, width: scale.width };
}

/** Drag: `dx` pixels of pointer move takes the picture with it. */
export function panBy(scale: Scale, dx: number): Scale {
  const shift = (dx / Math.max(1, scale.width)) * windowMs(scale);
  return { startMs: scale.startMs - shift, endMs: scale.endMs - shift, width: scale.width };
}

// --- The ruler -------------------------------------------------------------

/** The steps the ruler is allowed to take: the shared axis's ladder
 * (`$lib/time/axis`), which is where this one went (#2299) so that no two
 * timelines can put two readings of the same minute on one screen. */
export const TICK_STEPS_MS: readonly number[] = NICE_INTERVALS;

/** What this ruler asks the shared axis for: labelled lines only — it draws
 * no minor gridlines — and labels drawn beside their tick, which is where
 * `EpicTimeline.svelte` puts them and therefore which edge one can run off. */
const RULER_AXIS = {
  minLabelSpacingPx: MIN_TICK_PX,
  minors: false,
  dayLines: true,
  labelAnchor: 'start'
} as const;

/** The finest step whose ticks are still `MIN_TICK_PX` apart, or a day — the
 * widest there is — when none of them are. */
export const tickStep = (scale: Scale): number => axisIntervals(scale, RULER_AXIS).majorMs;

export type TimelineTick = {
  ms: number;
  x: number;
  /** "09:20", or the date on a day boundary. Empty when the label was
   * dropped to keep the ruler readable; the tick's line is still drawn. */
  label: string;
  /** A local midnight: a stronger line, and the date rather than the time. */
  day: boolean;
};

/**
 * The ruler: one tick per step across the window, plus a dated line at every
 * local midnight — all of it `$lib/time/axis`'s arithmetic now (#2299), which
 * is #2271's own ticks lifted out of here so the other timelines step on the
 * same grid. What this module still owns is what the Gantt asks for: majors
 * only, `MIN_TICK_PX` of room, and labels drawn beside their tick — which is
 * the edge a label here can run off, and so be dropped at rather than
 * clipped.
 *
 * The grid steps on the viewer's local clock (the shared axis's one
 * divergence from Dagster's) so a zone offset by half an hour still puts a
 * tick on local midnight and the ruler still carries its date (review r7).
 *
 * `nowMs` is the instant the now cap is drawn at, where there is one: the
 * axis keeps that word's pixels clear, so an hour tick under it loses its
 * reading rather than printing "10:00now" (#2385). A dated tick does not —
 * the date outranks the cap, and the cap is what goes. Which is why the word
 * is `nowLabelX`'s answer and not the caller's arithmetic.
 */
export const ticks = (scale: Scale, nowMs?: number): TimelineTick[] =>
  axisTicks(scale, { ...RULER_AXIS, nowMs }).map(({ ms, x, label, day }) => ({
    ms,
    x,
    label,
    day
  }));

/** Where the word "now" is drawn on the ruler, or `null` when it is not: the
 * marker is off the canvas, or the word would have landed on a dated reading,
 * which keeps its pixels. The axis measures the word and keeps it inside the
 * chart, which is the same box `ticks` thins against — one reading of where
 * it sits, so the ruler cannot clear space the cap is not in (#2385). The
 * marker's own line is drawn either way; only the word is at stake. */
export const nowLabelX = (scale: Scale, nowMs: number): number | null => {
  const box = nowLabelBox(scale, { ...RULER_AXIS, nowMs });
  return box === null ? null : box.from + LABEL_GAP_PX;
};

// --- Status bands ----------------------------------------------------------

/** What a band means: kew is working on it, or a person is the next move. */
export type BandKind = 'flight' | 'attention';

export const BAND_FILL: Record<BandKind, string> = {
  flight: 'var(--accent-2-wash)',
  attention: 'var(--accent-wash)'
};

/** The 2px left edge that says "this one is waiting on you". */
export const BAND_EDGE: Record<BandKind, string | null> = {
  flight: null,
  attention: 'var(--accent)'
};

/**
 * Which band a status draws, or null for none.
 *
 * Read through `statusShape`, which is the console's single status
 * vocabulary (`$lib/factory/status`): the driver has fourteen status words and
 * a hand-written list of four here would miss the next one it grows. An
 * unrecognised word degrades to the todo reading, i.e. to no band — honest,
 * rather than a confident magenta on a status nobody has mapped.
 */
export function bandKind(status: string): BandKind | null {
  const meaning = statusShape(status).meaning;
  // `published_waiting` keeps the flight wash on purpose. A band is a claim
  // that THE CLOCK IS RUNNING on an item, which it is while a pull request sits
  // open; whether kew is EXECUTING is a separate reading, and the one thing
  // that animates ($lib/factory/motion).
  if (meaning === 'working' || meaning === 'published_waiting') return 'flight';
  if (meaning === 'held' || meaning === 'needs_you') return 'attention';
  return null;
}

export type TimelineBand = {
  status: string;
  kind: BandKind;
  fromMs: number;
  /** The band's end, with `now` standing in for one that has not come. */
  toMs: number;
  x: number;
  width: number;
};

/** `[fromMs, toMs]` as a rect, never narrower than `min`. */
function box(scale: Scale, fromMs: number, toMs: number, min: number): { x: number; width: number } {
  const x = xOf(scale, fromMs);
  return { x, width: Math.max(min, xOf(scale, toMs) - x) };
}

/** The bands behind one row. An open band ends at `nowMs`; a band the server
 * could not date is dropped rather than drawn from the epoch. */
export function bandBoxes(node: EpicTimelineNode, scale: Scale, nowMs: number): TimelineBand[] {
  return node.bands.flatMap((band) => {
    const kind = bandKind(band.status);
    const fromMs = parseInstant(band.from);
    if (kind === null || fromMs === null) return [];
    const toMs = Math.max(fromMs, parseInstant(band.to) ?? nowMs);
    return [{ status: band.status, kind, fromMs, toMs, ...box(scale, fromMs, toMs, MIN_BAND_W) }];
  });
}

// --- Run bars --------------------------------------------------------------

/** What a run was, as far as the picture is concerned. */
export type BarRole = 'dispatch' | 'review' | 'remediation' | 'other';

/** Remediation is a neutral, the brightest of the three greys: not amber,
 * which DESIGN.md keeps for money and held (#2783, D33), and not violet, which
 * it gives to "blocked on a dependency" — the DAG beside this chart draws a
 * blocked node in it, so one colour would carry two meanings on one page
 * (#2843 P16). A fix round is not a signal. */
export const BAR_COLOR: Record<BarRole, string> = {
  dispatch: 'var(--text-2)',
  review: 'var(--accent-2)',
  remediation: 'var(--text-1)',
  other: 'var(--text-3)'
};

/** The chart's legend, one entry per bar colour, in the order a node's clock
 * is usually spent. Read off `BAR_COLOR`, so a colour cannot change without
 * its key changing with it. */
export const BAR_LEGEND: readonly { role: BarRole; label: string; color: string }[] = (
  [
    ['dispatch', 'dispatch'],
    ['review', 'review'],
    ['remediation', 'remediation'],
    ['other', 'other run']
  ] as const
).map(([role, label]) => ({ role, label, color: BAR_COLOR[role] }));

/** The stored `run_kind` -> its colour's role. The three kinds an epic node's
 * own clock is spent on are named; `ci_fix`, `resolve` and anything a newer
 * host sends take the muted "other", which is what "we have no colour for
 * this" looks like. */
const ROLE_OF: Record<string, BarRole> = {
  dispatch: 'dispatch',
  adversarial_review: 'review',
  review_remediation: 'remediation'
};

export const barRole = (kind: string): BarRole =>
  (Object.hasOwn(ROLE_OF, kind) ? ROLE_OF[kind] : undefined) ?? 'other';

export type TimelineBar = {
  run: EpicTimelineRun;
  role: BarRole;
  startMs: number;
  /** The run's end, with `now` standing in for a run still going. */
  endMs: number;
  /** Whether it is still going. */
  open: boolean;
  /** Refused, or over before it began: a tick, not a bar. */
  tick: boolean;
  /** Failed or killed: a `--danger` outline. */
  broken: boolean;
  x: number;
  width: number;
};

/** The runs on one row. A run with no start has no geometry, so it is left
 * out rather than drawn at the window's left edge. */
export function barBoxes(node: EpicTimelineNode, scale: Scale, nowMs: number): TimelineBar[] {
  return node.runs.flatMap((run) => {
    const startMs = parseInstant(run.started_at);
    if (startMs === null) return [];
    const finished = parseInstant(run.finished_at);
    const endMs = Math.max(startMs, finished ?? nowMs);
    const tick = run.status.trim().toLowerCase() === 'refused' || endMs <= startMs;
    const geometry = tick
      ? { x: xOf(scale, startMs), width: TICK_W }
      : box(scale, startMs, endMs, TICK_W);
    return [
      {
        run,
        role: barRole(run.kind),
        startMs,
        endMs,
        open: finished === null,
        tick,
        broken: statusShape(run.status, 'run').meaning === 'failed',
        ...geometry
      }
    ];
  });
}

/**
 * Where a bar links: the run page, at the attempt this bar IS, focused on the
 * instant it started (#2300).
 *
 * The attempt is named because a run page given none selects the latest, which
 * is the wrong evidence for an earlier bar on the same lane; the focused time
 * is the bar's start, so the page opens with that moment marked and the
 * matching event row highlighted.
 */
export const barHref = (bar: TimelineBar): string =>
  runEventsHref(bar.run.run_id, {
    attempt: bar.run.attempt_number,
    focusedTime: bar.run.started_at
  });

/** What the bar's link says it opens, for a reader who cannot see the
 * picture. The visible reading is the tooltip; this is the same three facts
 * in one sentence. */
export const barLabel = (bar: TimelineBar): string =>
  `${bar.run.kind} run ${bar.run.run_id}, attempt ${bar.run.attempt_number}, ${bar.run.status}`;

/** A bar's hover reading, a line at a time: what it was, when it ran, and
 * what it cost. */
export function barTooltip(bar: TimelineBar): string[] {
  return [
    `${bar.run.kind} · ${bar.run.status}`,
    `${fmtDateTime(bar.startMs)} → ${bar.open ? 'now' : fmtTime(bar.endMs, false)}`,
    `${elapsed(bar.endMs - bar.startMs)} · ${fmtUSD(bar.run.cost_usd)}`
  ];
}

// --- Verdicts and the merge ------------------------------------------------

export type VerdictTone = 'success' | 'warn';

export const VERDICT_COLOR: Record<VerdictTone, string> = {
  success: 'var(--success)',
  warn: 'var(--warn)'
};

/** The outline a failed or killed run's bar carries. */
export const BROKEN_STROKE = 'var(--danger)';

/** How a key entry draws its swatch: a bar's outline, a wash with an
 * optional left edge, or a dot. */
export type KeyShape = 'outline' | 'band' | 'dot';

/**
 * The chart's key for everything that is not a bar's fill (#2843 P16): the
 * failed run's outline, the two band washes, and the dots. Read off the same
 * maps the chart draws with, so with `BAR_LEGEND` every colour on the chart
 * has a key entry and a colour cannot change without its entry changing too.
 * The merge dot is `--success`, the ready verdict's green, so one entry names
 * both; `bandKind` draws held and needs-you in one wash, so one entry names
 * both of those too.
 */
export const MARK_LEGEND: readonly {
  key: string;
  label: string;
  shape: KeyShape;
  color: string;
  edge: string | null;
}[] = [
  { key: 'failed', label: 'failed run', shape: 'outline', color: BROKEN_STROKE, edge: null },
  {
    key: 'attention',
    label: 'needs you · held',
    shape: 'band',
    color: BAND_FILL.attention,
    edge: BAND_EDGE.attention
  },
  { key: 'flight', label: 'in flight', shape: 'band', color: BAND_FILL.flight, edge: BAND_EDGE.flight },
  { key: 'ready', label: 'ready · merged', shape: 'dot', color: VERDICT_COLOR.success, edge: null },
  { key: 'not-ready', label: 'not ready', shape: 'dot', color: VERDICT_COLOR.warn, edge: null }
];

/** The two verdicts that ended a round in kew's favour. `READY_WITH_RESIDUALS`
 * is one of them: the driver accepted the round (#2151), so the picture must
 * not paint it as a setback. */
const READY_VERDICTS: readonly string[] = ['READY', 'READY_WITH_RESIDUALS'];

export const verdictTone = (verdict: string): VerdictTone =>
  READY_VERDICTS.includes(verdict.trim().toUpperCase()) ? 'success' : 'warn';

export type TimelineMark = {
  round: EpicTimelineRound;
  tone: VerdictTone;
  ms: number;
  x: number;
  /**
   * The mark's identity for a keyed `#each`, and the reason this field
   * exists: `round` is NOT unique on a lane. `record_review_round` restarts
   * the count at 1 for each review cycle (a `kew epic retry` opens a new one)
   * and the API serves every cycle, so a retried node arrives as rounds
   * [1, 2, 1, 2] — and keying the dots on the number alone drops half of the
   * rounds the chart exists to show. The ordinal makes it unique whatever the
   * server sends, including two rounds recorded in the same microsecond.
   */
  key: string;
};

/** The round marks on one row, at the instant each verdict was recorded. */
export function roundMarks(node: EpicTimelineNode, scale: Scale): TimelineMark[] {
  return node.rounds.flatMap((round, i) => {
    const ms = parseInstant(round.recorded_at);
    if (ms === null) return [];
    return [
      {
        round,
        tone: verdictTone(round.verdict),
        ms,
        x: xOf(scale, ms),
        key: `${i}:${round.round}:${round.recorded_at}`
      }
    ];
  });
}

/** A round mark's hover reading — the brief's "round 6 · NEEDS_CHANGES". */
export const roundTooltip = (mark: TimelineMark): string[] => [
  `round ${mark.round.round} · ${mark.round.verdict}`,
  fmtDateTime(mark.ms)
];

export type TimelineMerge = { ms: number; x: number };

/** Where this node's PR merged, or null when it has not. */
export function mergeMark(node: EpicTimelineNode, scale: Scale): TimelineMerge | null {
  const ms = parseInstant(node.merged_at);
  return ms === null ? null : { ms, x: xOf(scale, ms) };
}

// --- Rows ------------------------------------------------------------------

export type TimelineRow = {
  id: string;
  title: string;
  /** The row's top, relative to the first row (the ruler is above all of
   * them and is not counted here). */
  y: number;
  bands: TimelineBand[];
  bars: TimelineBar[];
  marks: TimelineMark[];
  merge: TimelineMerge | null;
};

/** One row per node, in the API's order — see the header: the server owns the
 * ordering and this does not second-guess it. */
export function rows(timeline: EpicTimeline, scale: Scale, nowMs: number): TimelineRow[] {
  return timeline.nodes.map((node, i) => ({
    id: node.id,
    title: node.title,
    y: i * ROW_H,
    bands: bandBoxes(node, scale, nowMs),
    bars: barBoxes(node, scale, nowMs),
    marks: roundMarks(node, scale),
    merge: mergeMark(node, scale)
  }));
}

/**
 * The first moment anything happened on a node, or null when nothing has.
 *
 * This is the key the API sorts its nodes by. It is not used to sort them
 * again; it is here so the contract can be asserted (`timeline.test.ts`) and
 * so a reader of this module can see what the order they are given means.
 */
export function firstActivityMs(node: EpicTimelineNode): number | null {
  const stamps = [
    ...node.bands.map((b) => parseInstant(b.from)),
    ...node.runs.map((r) => parseInstant(r.started_at))
  ].filter((ms): ms is number => ms !== null);
  return stamps.length ? Math.min(...stamps) : null;
}

/**
 * ↑ / ↓ over the rows. Does not wrap: the ends of the chart are a real edge
 * of the picture, and a selection that jumps from the last row to the first
 * reads as a loss of place (the DAG canvas's `inLayer` rule). With nothing
 * selected, ↓ lands on the first row and ↑ on the last.
 */
export function rowStep(
  ids: readonly string[],
  selected: string | null,
  delta: number
): string | null {
  if (!ids.length) return null;
  const at = selected === null ? -1 : ids.indexOf(selected);
  if (at < 0) return delta < 0 ? ids[ids.length - 1] : ids[0];
  const next = at + delta;
  return next < 0 || next >= ids.length ? null : ids[next];
}

// --- The Graph / Timeline switch -------------------------------------------

/** The two canvas views the epic page offers. `graph` is the default: an
 * unrecognised `?view=` is not a third view. */
export const CANVAS_VIEWS = ['graph', 'timeline'] as const;
export type CanvasView = (typeof CANVAS_VIEWS)[number];

export const CANVAS_VIEW_LABEL: Record<CanvasView, string> = {
  graph: 'Graph',
  timeline: 'Timeline'
};

/** Which view a `?view=` reading means. Anything else is the graph — a stale
 * or hand-typed link must land on a view this page has. */
export const canvasView = (raw: string | null | undefined): CanvasView =>
  raw === 'timeline' ? 'timeline' : 'graph';

/** The other one. */
export const otherView = (view: CanvasView): CanvasView =>
  view === 'timeline' ? 'graph' : 'timeline';

/**
 * The epic page's own shortcuts (#2512), as it registers them with the
 * console's key layer: Esc, whose two steps (popover, then selection) are the
 * page's state, and the Graph / Timeline switch's `T`. The DAG's legend draws
 * Esc from here, so the legend and the handler are one entry. Esc is offered
 * from a field too ("Esc anywhere on the page", the replay scrubber included),
 * while `T` still leaves a field to the field (`togglesView`).
 */
export const EPIC_KEYS: readonly KeyDef[] = [
  { key: 'Escape', description: 'close', legend: true, inFields: true },
  { key: 't', description: 'graph / timeline' },
  { key: 'T', description: 'graph / timeline' }
];

/**
 * The timeline canvas's shortcuts: ↑ and ↓ walk its rows, F fits the run.
 */
export const TIMELINE_KEYS: readonly KeyDef[] = [
  { key: 'ArrowUp', description: 'previous row' },
  { key: 'ArrowDown', description: 'next row' },
  { key: 'f', description: 'fit' },
  { key: 'F', description: 'fit' }
];

/**
 * Whether a key press is the switch's `T`.
 *
 * `typing` is passed in rather than sniffed here, so this stays a pure
 * reading (the `work` page's `workIntent` rule): a modifier combination
 * belongs to the browser, and a `t` typed into a field belongs to the field.
 */
export function togglesView(press: {
  readonly key: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly altKey: boolean;
  readonly typing: boolean;
}): boolean {
  if (press.metaKey || press.ctrlKey || press.altKey || press.typing) return false;
  return press.key === 't' || press.key === 'T';
}
