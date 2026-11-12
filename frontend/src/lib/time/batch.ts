/**
 * #2299 — overlap batching: what a timeline draws where two marks land on the
 * same pixels.
 *
 * A timeline's marks are placed by their instants, so at any zoom wider than a
 * few minutes a day's runs, verdicts and audit events sit on top of each
 * other. Drawn as they are, the picture is a smear whose topmost mark is
 * whichever one the `#each` happened to reach last — i.e. the screen reports
 * the array's order as if it were a severity. Batching makes that explicit:
 * overlapping marks become ONE mark that says how many it stands for and wears
 * the state of its worst member, and the detail moves into a popover.
 *
 * The sweep (sort by left edge, merge while the visual extents overlap, take
 * the union of the times and the widened extent) follows the idea of Dagster's
 * `js_modules/ui-core/src/runs/batchRunsForTimeline.tsx` (Apache-2.0,
 * https://github.com/dagster-io/dagster @ 54577dad, Copyright 2025 Dagster
 * Labs, Inc.). Modified by kew: re-implemented in TypeScript over this
 * console's own status vocabulary and severity order, and with none of
 * Dagster's presentation.
 *
 * This module decides and draws nothing — same split as every other model
 * here: the geometry is the part that can be wrong in a way no screenshot
 * shows.
 */
import { statusShape, type StatusMeaning, type StatusVocabulary } from '$lib/factory/status';
import { axisX, type AxisWindow } from './axis';

/**
 * kew's severity order, worst first.
 *
 * Not a palette and not a log level: it is the order the console already reads
 * a screen in. Something broke; a person is the next move; kew did something;
 * everything else is quiet. `attention` is the magenta/amber pair — held work
 * and work waiting on a human — and `quiet` covers the states that make no
 * claim on anybody's attention (todo, queued, pending, cancelled).
 */
export const BATCH_SEVERITIES = ['failed', 'attention', 'did', 'quiet'] as const;
export type BatchSeverity = (typeof BATCH_SEVERITIES)[number];

/** Lower is worse. */
export const severityRank = (severity: BatchSeverity): number =>
  BATCH_SEVERITIES.indexOf(severity);

/** The worse of two severities — what a batch shows. */
export const worseOf = (a: BatchSeverity, b: BatchSeverity): BatchSeverity =>
  severityRank(a) <= severityRank(b) ? a : b;

/**
 * Status meaning -> severity. Everything absent is `quiet`, which is the
 * honest reading for a word no vocabulary here has mapped: a mark nobody can
 * place must not be able to colour a whole batch.
 *
 * `blocked` is quiet on purpose: a node waiting on a dependency is not waiting
 * on a person, and `attention` is rationed to the states where somebody has to
 * act (DESIGN.md — magenta only where a person is the next move).
 */
const SEVERITY_OF: Partial<Record<StatusMeaning, BatchSeverity>> = {
  failed: 'failed',
  missing: 'failed',
  held: 'attention',
  needs_you: 'attention',
  working: 'did',
  published_waiting: 'did',
  done: 'did'
};

/** The severity of a kew status word, read through the console's one status
 * vocabulary (`$lib/factory/status`) rather than a hand-written list that
 * would miss the driver's next status. */
export const batchSeverity = (
  status: string | null | undefined,
  vocabulary?: StatusVocabulary
): BatchSeverity => SEVERITY_OF[statusShape(status, vocabulary).meaning] ?? 'quiet';

/** The narrowest a mark may draw. A 0px rect is an invisible fact, and two
 * marks a pixel apart are one mark to a reader. */
export const MIN_MARK_W = 4;

/** A mark offered to the batcher: when it happened, how bad it is, and
 * whatever the caller needs back out of the batch. */
export type BatchMark<T> = {
  readonly startMs: number;
  /** The mark's end, for a mark with a duration. Defaults to `startMs`. */
  readonly endMs?: number;
  readonly severity: BatchSeverity;
  readonly item: T;
};

export type Batch<T> = {
  /** The union of the batched times. */
  startMs: number;
  endMs: number;
  /** The widened extent, in pixels, never narrower than `MIN_MARK_W`. */
  x: number;
  width: number;
  /** The most severe member's severity. */
  severity: BatchSeverity;
  /** Every mark in it, in left-edge order. */
  items: BatchMark<T>[];
};

/**
 * Marks merged while they overlap, left to right.
 *
 * One sweep: a mark joins the batch under construction when its left edge is
 * at or before that batch's right edge, which widens the batch and so can pull
 * in the mark after it too. A dense run of marks therefore collapses into one
 * batch — that is the point, not a bug: they occupy one patch of screen and
 * are one thing to click.
 */
export function batchMarks<T>(
  marks: readonly BatchMark<T>[],
  window: AxisWindow,
  options: { readonly minWidthPx?: number } = {}
): Batch<T>[] {
  const minWidth = options.minWidthPx ?? MIN_MARK_W;
  const sorted = [...marks].sort((a, b) => a.startMs - b.startMs);
  const out: Batch<T>[] = [];
  for (const mark of sorted) {
    const endMs = Math.max(mark.startMs, mark.endMs ?? mark.startMs);
    const x = axisX(window, mark.startMs);
    const right = Math.max(x + minWidth, axisX(window, endMs));
    const open = out[out.length - 1];
    if (open !== undefined && x <= open.x + open.width) {
      open.endMs = Math.max(open.endMs, endMs);
      open.width = Math.max(open.x + open.width, right) - open.x;
      open.severity = worseOf(open.severity, mark.severity);
      open.items.push(mark);
      continue;
    }
    out.push({
      startMs: mark.startMs,
      endMs,
      x,
      width: right - x,
      severity: mark.severity,
      items: [mark]
    });
  }
  return out;
}

/** How many of a batch's marks its popover lists before it stops counting
 * them out one by one. */
export const BATCH_LIST_CAP = 50;

/** What a batch's popover shows, in order: its marks worst first, a rule
 * before the quiet remainder, and a last line for what did not fit. */
export type BatchEntry<T> =
  | { kind: 'item'; severity: BatchSeverity; mark: BatchMark<T> }
  | { kind: 'rule' }
  | { kind: 'more'; hidden: number; label: string };

/**
 * A batch's popover, as rows.
 *
 * Failures first, then what kew did, then a rule, then the quiet marks — the
 * severity order above, with the rule standing where the list stops being
 * something to act on. Held and needs-you marks sit with the failures rather
 * than after them: they are the other half of "somebody has to look at this",
 * and burying them under thirty successful runs is how a console loses a hold.
 *
 * Capped at `cap` rows, with "N more not shown" rather than a scroll bar in a
 * popover. Within a severity the marks keep their time order, because `sort`
 * is stable and they arrive in left-edge order.
 */
export function batchEntries<T>(
  marks: readonly BatchMark<T>[],
  cap: number = BATCH_LIST_CAP
): BatchEntry<T>[] {
  const ranked = [...marks].sort((a, b) => severityRank(a.severity) - severityRank(b.severity));
  const shown = ranked.slice(0, Math.max(0, cap));
  const out: BatchEntry<T>[] = shown.map((mark) => ({
    kind: 'item' as const,
    severity: mark.severity,
    mark
  }));
  const firstQuiet = shown.findIndex((mark) => mark.severity === 'quiet');
  if (firstQuiet > 0) out.splice(firstQuiet, 0, { kind: 'rule' });
  const hidden = ranked.length - shown.length;
  if (hidden > 0) out.push({ kind: 'more', hidden, label: `${hidden} more not shown` });
  return out;
}
