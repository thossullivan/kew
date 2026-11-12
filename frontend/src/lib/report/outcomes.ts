/**
 * C3/#2301 — the outcome scorecard's geometry and wording, as pure readings.
 *
 * Four small multiples on ONE weekly x-axis: merged share, time to READY,
 * rounds per PR, cost per merged PR. Every number the chart draws, every
 * sentence it prints when it can draw nothing, and the band a pointer is over
 * are decided here so they can be checked without a browser. The component is
 * left with nothing but SVG elements and a hovered index.
 *
 * Geometry choices worth knowing before editing:
 *
 * - The plot spans the full width of its box with NO x padding, and week *i*
 *   sits at the centre of band *i*. That is what lets the shared axis labels
 *   be one equal-width grid cell each and still line up with the SVG marks,
 *   and what makes "hovering a week lights that week on all four" a single
 *   index rather than four hit tests.
 * - Each chart is drawn in a fixed `viewBox` stretched to the box
 *   (`preserveAspectRatio="none"`), so the row height is CSS and never a
 *   percentage of an indefinite parent (DESIGN: the DAG canvas once rendered
 *   0px tall). Strokes carry `vector-effect="non-scaling-stroke"` and markers
 *   are rects, because a stretched viewBox would otherwise scale a 1px
 *   hairline and squash a circle.
 * - A missing week is a GAP, never a bridged line: `polylines` returns one
 *   subpath per run of consecutive readable weeks.
 *
 * No chart library and no animation: an instrument that slides into place is
 * unreadable for the half-second a glance actually lasts.
 */
import type { OutcomeReport, OutcomeWeek } from '$lib/api';
import type { KeyDef } from '$lib/keys/registry';
import { gaugeMoney } from '$lib/factory/gauge';
import { NO_TIME, fmtDateTime, fmtUtcMonthDay } from '$lib/time';

/** Internal width of every plot. Bands are fractions of it, so the number
 *  only sets the arithmetic's resolution. */
export const VIEW_W = 320;
/** Internal height of every plot. */
export const VIEW_H = 64;
/** Top/bottom breathing room inside the plot, in view units. */
export const PLOT_TOP = 7;
export const PLOT_BOTTOM = 7;

/** Which of the four a chart is. */
export type OutcomeChartKey = 'merged' | 'ready' | 'rounds' | 'cost';

/** One week of one chart. `value === null` means this week has no reading at
 *  all (not a zero), and `thin` means it has one but too small a sample to
 *  read — the console greys those rather than hiding them. */
export type OutcomePoint = {
  week: string;
  label: string;
  value: number | null;
  /** Printed value, e.g. `2.3 h`, `$24.77`, `53%`. `—` when there is none. */
  text: string;
  /** The served reading beside the plotted one — the tail of a distribution,
   *  the mean behind a median, the counts behind a share. Every field the API
   *  serves is read somewhere, or it should not be served. */
  detail: string;
  /** Sample size behind this reading. */
  n: number;
  thin: boolean;
  partial: boolean;
};

export type OutcomeChart = {
  key: OutcomeChartKey;
  label: string;
  /** What `n` counts, for the hover line: "merged PRs", "items". */
  sample: string;
  /** Vertical scale top. Always > 0 so a flat zero series still has a floor. */
  max: number;
  points: OutcomePoint[];
  /** Why this chart is empty, as a sentence — or null when it has data. */
  empty: string | null;
  /** Money is amber (DESIGN: amber is money and held); nothing else is. */
  money: boolean;
};

/**
 * `Sep 21` — the Monday, as the axis prints it, through the console's one
 * formatter. UTC, because the server cut the week in UTC: see
 * `fmtUtcMonthDay`. A week start the server could not have produced is printed
 * as it arrived rather than as a dash, so a bad row stays visible.
 */
export function weekLabel(weekStart: string): string {
  const label = fmtUtcMonthDay(weekStart);
  return label === NO_TIME ? weekStart : label;
}

/** `1 h 24 m` under a day, `2.3 h` above — a median in hours, read at a glance. */
export function hoursText(hours: number | null): string {
  if (hours === null) return '—';
  if (hours < 1) return `${Math.round(hours * 60)} m`;
  return `${hours.toFixed(1)} h`;
}

/** kew's share of the week's merges, as a percentage of what landed on main. */
export function mergedShare(week: OutcomeWeek): number | null {
  const total = week.merged_main;
  if (total === null || total <= 0) return null;
  return (week.merged_kew_authored / total) * 100;
}

/**
 * The two segments of a week's bar: kew-authored, and every other PR that
 * merged on main that week.
 *
 * `merged_main` is the denominator when the clone could be read. When it
 * could not, the bar is kew's own count alone and `total` says so — drawing
 * a full bar off a missing denominator would read as "kew merged all of it".
 */
export function barSegments(week: OutcomeWeek): {
  authored: number;
  other: number;
  total: number;
} {
  const authored = week.merged_kew_authored;
  const main = week.merged_main;
  if (main === null) return { authored, other: 0, total: authored };
  const total = Math.max(main, authored);
  return { authored, other: Math.max(0, total - authored), total };
}

/** Width of one week's band, in view units. */
export function bandWidth(count: number): number {
  return count > 0 ? VIEW_W / count : VIEW_W;
}

/** Centre of week *i*'s band, in view units. */
export function bandX(index: number, count: number): number {
  return (index + 0.5) * bandWidth(count);
}

/** y of *value* against a scale topping out at *max*, in view units. */
export function plotY(value: number, max: number): number {
  const span = VIEW_H - PLOT_TOP - PLOT_BOTTOM;
  const fraction = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  return VIEW_H - PLOT_BOTTOM - fraction * span;
}

/**
 * One subpath per run of consecutive readable weeks — never one path across a
 * gap. A lone readable week between two blanks yields a zero-length subpath,
 * which draws nothing; its marker is what shows it.
 */
export function polylines(points: OutcomePoint[], max: number): string[] {
  const paths: string[] = [];
  let run: string[] = [];
  const flush = () => {
    if (run.length > 1) paths.push(`M ${run.join(' L ')}`);
    run = [];
  };
  points.forEach((point, index) => {
    if (point.value === null) {
      flush();
      return;
    }
    run.push(`${bandX(index, points.length).toFixed(2)},${plotY(point.value, max).toFixed(2)}`);
  });
  flush();
  return paths;
}

/**
 * The week a pointer at *ratio* (0–1 across the plot) is over, or null when
 * there are no weeks. Bands, not nearest point: the whole column belongs to
 * its week, so there is no dead space between marks.
 */
export function weekIndexAt(ratio: number, count: number): number | null {
  if (count <= 0) return null;
  const index = Math.floor(ratio * count);
  return Math.min(count - 1, Math.max(0, index));
}

/** What a key on the outcome charts does to the crosshair. */
export type CrosshairKey = 'older' | 'newer' | 'first' | 'last' | 'clear';

/**
 * The outcome charts' shortcuts (#2781), on the console's key layer so the `?`
 * overlay lists them: the arrows step the crosshair, Home and End jump to the
 * ends, Esc drops it.
 */
export const OUTCOME_KEYS: readonly (KeyDef & { readonly action: CrosshairKey })[] = [
  { key: 'ArrowLeft', description: 'previous week', action: 'older' },
  { key: 'ArrowRight', description: 'next week', action: 'newer' },
  { key: 'Home', description: 'first week', action: 'first' },
  { key: 'End', description: 'last week', action: 'last' },
  { key: 'Escape', description: 'clear the crosshair', action: 'clear' }
];

/**
 * The charts' reading of a press, or `null` when it is not theirs. A press
 * carrying ⌘, ^ or ⌥ is the browser's (⌘← is Back), never the crosshair's.
 */
export function crosshairKey(press: {
  readonly key: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly altKey: boolean;
}): CrosshairKey | null {
  if (press.metaKey || press.ctrlKey || press.altKey) return null;
  return OUTCOME_KEYS.find((def) => def.key === press.key)?.action ?? null;
}

/** Clamp a crosshair index to the weeks that exist (keyboard stepping). */
export function stepIndex(current: number | null, delta: number, count: number): number | null {
  if (count <= 0) return null;
  const from = current === null ? count - 1 : current;
  return Math.min(count - 1, Math.max(0, from + delta));
}

function pointOf(
  week: OutcomeWeek,
  value: number | null,
  n: number,
  minSample: number,
  text: string,
  detail = ''
): OutcomePoint {
  return {
    week: week.week_start,
    label: weekLabel(week.week_start),
    value,
    text: value === null ? '—' : text,
    detail,
    n,
    thin: n < minSample,
    partial: week.partial
  };
}

/** Scale top for a series: its largest reading, or 1 when it has none. */
function scaleMax(points: OutcomePoint[]): number {
  const values = points.map((p) => p.value).filter((v): v is number => v !== null);
  return values.length ? Math.max(...values) : 1;
}

/**
 * Why a chart has nothing to draw, in one sentence — never a blank box
 * (DESIGN: say what an empty read means).
 *
 * The merged-share chart has its own answer: an unreadable clone is a
 * different fact from a quiet fortnight, and only the server knows which.
 */
function emptyReason(
  key: OutcomeChartKey,
  points: OutcomePoint[],
  weeks: number,
  mainUnavailable: string | null
): string | null {
  if (points.some((p) => p.value !== null)) return null;
  const window = `in the last ${weeks} ${weeks === 1 ? 'week' : 'weeks'}`;
  if (key === 'merged' && mainUnavailable) {
    return `kew's share of main cannot be drawn: ${mainUnavailable}.`;
  }
  if (key === 'merged') return `no PRs merged on main ${window}.`;
  if (key === 'cost') return `no merged PR carried a recorded cost ${window}.`;
  return `no review round reached READY ${window}, so there is nothing to time.`;
}

/**
 * The four charts, oldest week first, or an empty list when there is no
 * report at all (offline — the page says that once, not four times).
 */
export function buildCharts(report: OutcomeReport | null): OutcomeChart[] {
  if (!report) return [];
  const weeks = report.weeks;
  const min = report.min_sample;
  const unavailable = report.merged_main_unavailable;

  const merged = weeks.map((w) => {
    const share = mergedShare(w);
    return pointOf(
      w,
      share,
      w.merged_main ?? w.merged_kew_touched,
      min,
      share === null ? '—' : `${Math.round(share)}%`,
      w.merged_main === null
        ? `${w.merged_kew_authored} authored · main not readable`
        : `${w.merged_kew_authored} of ${w.merged_main} · ${w.merged_kew_touched} touched`
    );
  });
  const ready = weeks.map((w) =>
    pointOf(
      w,
      w.ready_hours.p50,
      w.ready_items,
      min,
      hoursText(w.ready_hours.p50),
      w.ready_hours.p50 === null
        ? ''
        : `p90 ${hoursText(w.ready_hours.p90)} · worst ${hoursText(w.ready_hours.max)}`
    )
  );
  const rounds = weeks.map((w) =>
    pointOf(
      w,
      w.rounds_to_ready.p50,
      w.ready_items,
      min,
      w.rounds_to_ready.p50 === null ? '—' : w.rounds_to_ready.p50.toFixed(1),
      w.rounds_to_ready.max === null ? '' : `worst ${w.rounds_to_ready.max}`
    )
  );
  const cost = weeks.map((w) =>
    pointOf(
      w,
      w.cost_per_merged.p50,
      w.merged_kew_touched,
      min,
      w.cost_per_merged.p50 === null ? '—' : gaugeMoney(w.cost_per_merged.p50),
      w.cost_per_merged.mean === null ? '' : `mean ${gaugeMoney(w.cost_per_merged.mean)}`
    )
  );

  const built: OutcomeChart[] = [
    {
      key: 'merged',
      label: 'kew share of merged',
      sample: 'merged on main',
      max: 100,
      points: merged,
      empty: null,
      money: false
    },
    {
      key: 'ready',
      label: 'median time to READY',
      sample: 'items',
      max: scaleMax(ready),
      points: ready,
      empty: null,
      money: false
    },
    {
      key: 'rounds',
      label: 'median rounds per PR',
      sample: 'items',
      max: scaleMax(rounds),
      points: rounds,
      empty: null,
      money: false
    },
    {
      key: 'cost',
      label: 'median $ per merged PR',
      sample: 'merged PRs',
      max: scaleMax(cost),
      points: cost,
      empty: null,
      money: true
    }
  ];
  return built.map((chart) => ({
    ...chart,
    empty: emptyReason(chart.key, chart.points, weeks.length, unavailable)
  }));
}

/**
 * One chart's reading of the hovered week — or of the newest week when
 * nothing is hovered, so a row is never blank.
 *
 * Printed beside the chart's own label rather than in a floating tip: a
 * tooltip is a toast with a pointer, and the reading has to stay legible while
 * the eye is on the plot (DESIGN: show state in place).
 */
export function crosshairNote(chart: OutcomeChart, index: number | null): string {
  const point = index === null ? chart.points[chart.points.length - 1] : chart.points[index];
  if (!point) return '';
  const parts = [point.label, point.text];
  if (point.detail) parts.push(point.detail);
  parts.push(`n=${point.n} ${chart.sample}`);
  if (point.thin) parts.push('sample too small to read');
  if (point.partial) parts.push('week in progress');
  return parts.join(' · ');
}

/**
 * The stack's one footer line: the week's whole bill, the part of it no merge
 * can be divided by, the nodes it closed, and how fresh its PR states are.
 *
 * A cost-per-merged figure read alone understates by omission — the runs that
 * never opened a PR are spend too — so the unattributed share is printed
 * where the median is read rather than on another page. The observation time
 * is there for the same reason: the merge counts are a ledger snapshot, and
 * they are no fresher than the last row that wrote one.
 */
export function weekFooter(report: OutcomeReport | null, index: number | null): string {
  const weeks = report?.weeks ?? [];
  const week = index === null ? weeks[weeks.length - 1] : weeks[index];
  if (!week) return '';
  const observed = week.pr_sync_observed_at
    ? `PR states as of ${fmtDateTime(week.pr_sync_observed_at)}`
    : 'no PR state observed this week';
  return [
    `${weekLabel(week.week_start)} · ${gaugeMoney(week.spend_total)} dispatched`,
    `${gaugeMoney(week.spend_unattributed)} of it on runs with no PR`,
    `${week.nodes_done} ${week.nodes_done === 1 ? 'node' : 'nodes'} done`,
    observed
  ].join(' · ');
}
