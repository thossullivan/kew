/**
 * FUI-14 — the epic header strip's numbers, segmented bar and legend.
 *
 * DEpic (y ≈56–171) states an epic's shape three times over, each louder than
 * the last: "13 / 20 done · 65%", then one 12×10 cell per node, then a legend
 * naming every status with its count. This module decides all three from the
 * snapshot's `counts` map and nothing else, so the page only has to choose
 * which span carries which of them.
 *
 * Three rules it exists to keep:
 *
 * 1. **A cell means a node.** The bar orders the statuses the way the artboard
 *    draws them — settled first, waiting last — but the snapshot serves the
 *    whole `DriverStatus` enum (graph/driver/overlay.py), not the seven words
 *    `BAR_ORDER` names. A status the order forgot still gets its cells, after
 *    the ones it named, so the cell count is the node count rather than
 *    whatever subset of the vocabulary this file happened to anticipate.
 * 2. **The bar stays one row.** Past `MAX_CELLS` nodes a cell per node would
 *    either overflow the 1360px strip or shrink below a pixel, so the bar
 *    switches to one proportional segment per status. Same order, same
 *    colours, a share instead of a tally.
 * 3. **Colour is rationed.** Nothing here names a colour: every fill comes from
 *    `STATUS_MARKS` via `statusShape`, which is the one table that decides what
 *    a status looks like (FUI-04). The quiet states are the deliberate
 *    exception — see `cellStyle`.
 */
import {
  QUIET_MEANINGS,
  statusShape,
  type MarkShape,
} from "$lib/factory/status";

/**
 * The bar's reading order: what is finished, then what is moving, then what is
 * stopped, then what has not started. It is not the legend's order — the legend
 * follows the payload, so a new status appears there without being ranked here.
 */
export const BAR_ORDER = [
  "done",
  "in_review",
  "in_progress",
  "blocked",
  "held",
  "failed",
  "todo",
] as const;

/**
 * The most nodes the bar draws a cell each for. 40 × (12px + 2px gap) is 560px,
 * which is what DEpic's row leaves between "done · 65%" and the legend.
 */
export const MAX_CELLS = 40;

export type ProgressSegment = {
  status: string;
  count: number;
  /** Share of the bar, in percent. The segments of a bar sum to 100. */
  pct: number;
};

/**
 * One cell per node while an epic is small enough to show each of them, one
 * proportional segment per status once it is not.
 */
export type ProgressBar =
  | { kind: "cells"; cells: string[] }
  | { kind: "segments"; segments: ProgressSegment[] };

export type LegendEntry = { status: string; count: number };

export type EpicProgress = {
  /** Nodes in the epic, as the counts add up. */
  total: number;
  done: number;
  /**
   * Whole percent done. `0` for an empty epic — never a division by zero — and
   * only ever `100` for an epic that actually finished (see `wholePct`).
   */
  pct: number;
  bar: ProgressBar;
  /** Every status the payload counted, in its order, zeros included. */
  legend: LegendEntry[];
  /** The bar's accessible name: "13 done, 1 in review, 4 todo". */
  barLabel: string;
};

/**
 * A count that can be drawn: a whole, positive number of nodes.
 *
 * Counts arrive over the wire, and a negative, fractional or non-finite one
 * would otherwise fabricate cells (`Array(1.5)` throws, `Array(-1)` throws) or
 * push the percentage past 100. Such a count is dropped rather than coerced —
 * a status nobody can count is not a status with zero nodes.
 */
function drawable(n: number): boolean {
  return Number.isSafeInteger(n) && n > 0;
}

/**
 * Whole percent done, with the two ends reserved for the readings that mean
 * something absolute.
 *
 * Plain rounding lets the strip contradict the tally printed 4px to its left:
 * 199 of 200 nodes is 99.5%, which `Math.round` turns into "199 / 200 done ·
 * 100%", and one node of 500 is 0.2%, which it turns into "1 / 500 done · 0%".
 * Both read as a claim about whether the epic is finished or untouched, which
 * is the one thing a percentage here must not get wrong. So 100 is spent only
 * on `done === total`, 0 only on nothing done, and everything between is
 * rounded into 1..99.
 */
function wholePct(done: number, total: number): number {
  if (total === 0 || done === 0) return 0;
  if (done >= total) return 100;
  return Math.min(99, Math.max(1, Math.round((100 * done) / total)));
}

/** The statuses with nodes, in bar order, with the unranked ones after them. */
function orderedStatuses(counts: Record<string, number>): string[] {
  const entries = Object.entries(counts).filter(([, n]) => drawable(n));
  const ranked = BAR_ORDER.filter((s) =>
    entries.some(([status]) => status === s),
  );
  const rest = entries
    .map(([status]) => status)
    .filter((s) => !ranked.includes(s as never));
  return [...ranked, ...rest];
}

export function epicProgress(counts: Record<string, number>): EpicProgress {
  const order = orderedStatuses(counts);
  const of = (status: string) =>
    drawable(counts[status]) ? counts[status] : 0;
  const total = order.reduce((acc, status) => acc + of(status), 0);
  const done = of("done");

  const bar: ProgressBar =
    total > MAX_CELLS
      ? {
          kind: "segments",
          segments: order.map((status) => ({
            status,
            count: of(status),
            pct: (100 * of(status)) / total,
          })),
        }
      : {
          kind: "cells",
          cells: order.flatMap((status) => Array(of(status)).fill(status)),
        };

  return {
    total,
    done,
    pct: wholePct(done, total),
    bar,
    legend: Object.entries(counts)
      .filter(([, n]) => n === 0 || drawable(n))
      .map(([status, count]) => ({ status, count })),
    // An epic with no countable node has no shape to describe, and "0 done"
    // would read as a claim about work that exists.
    barLabel:
      order.length === 0
        ? "nothing yet"
        : order
            .map((status) => `${of(status)} ${statusShape(status).word}`)
            .join(", "),
  };
}

/** The mark shapes that are drawn as an outline rather than filled in. */
const HOLLOW: ReadonlySet<MarkShape> = new Set<MarkShape>([
  "ring",
  "ring-thick",
  "ring-dashed",
  "ring-x",
  "hatch-square",
]);

/**
 * How one bar cell (or segment) is painted, as an inline `style` string.
 *
 * The shape of the status's own mark decides it: a filled mark fills the cell,
 * a hollow one outlines it. The quiet meanings — todo, cancelled, and anything
 * unrecognised — are the exception DEpic draws explicitly: a 1px --hair-strong
 * box, not a coloured one. Work nobody has started is the ground the bar sits
 * on, and giving it a colour of its own would make a 40-cell todo bar the
 * loudest thing on the screen.
 */
export function cellStyle(status: string): string {
  const mark = statusShape(status);
  if (QUIET_MEANINGS.has(mark.meaning))
    return "box-shadow: inset 0 0 0 1px var(--hair-strong)";
  if (HOLLOW.has(mark.shape))
    return `box-shadow: inset 0 0 0 1.5px ${mark.color}`;
  return `background: ${mark.color}`;
}

/**
 * The strip's signal dot and word: cyan with a halo while the driver is
 * progressing, green when the epic is complete. A stalled epic is magenta when
 * a PERSON is its next move (`personNext`: the needs-you set names something
 * on it) and amber otherwise — never --danger, which DESIGN.md reserves for
 * failed (#2539, D24; it was red from the owner's default of 2026-09-24). A
 * signal this does not know stays
 * quiet — inventing an alarm for an unrecognised word is the one reading that
 * cannot be walked back by looking at the graph.
 */
export function signalTone(
  signal: string | null | undefined,
  personNext: boolean = false,
): {
  color: string;
  halo: boolean;
} {
  if (signal === "progressing") return { color: "var(--accent-2)", halo: true };
  if (signal === "complete") return { color: "var(--success)", halo: false };
  if (signal === "stalled")
    return { color: personNext ? "var(--accent)" : "var(--warn)", halo: false };
  return { color: "var(--text-3)", halo: false };
}

/**
 * `signalTone`, with the live claim withdrawn unless a fresh heartbeat backs
 * it (design review c4, D32). The halo says the drain is moving NOW, so it is
 * only drawn over a fresh tick in live mode — DESIGN.md: "Show 'live' only
 * with a fresh heartbeat", and "A replayed frame draws no halo". Anything else
 * reads in text-3, the way the Epics list's `signalReading` already does;
 * `progressing` with a cyan halo beside "no heartbeat yet" was the defect.
 */
export function heartbeatTone(
  signal: string | null | undefined,
  liveAndFresh: boolean,
  personNext: boolean = false,
): { color: string; halo: boolean } {
  const tone = signalTone(signal, personNext);
  if (!tone.halo || liveAndFresh) return tone;
  return { color: "var(--text-3)", halo: false };
}
