/**
 * FUI-14 — the epic header strip's numbers, bar and legend.
 *
 * The bar is the one place the strip claims a shape for every node at once, so
 * the two claims worth pinning are that it orders the statuses the way DEpic
 * draws them (settled first, waiting last) and that it never draws more cells
 * than the artboard's row can hold.
 */
import { describe, expect, it } from "vitest";
import {
  BAR_ORDER,
  cellStyle,
  epicProgress,
  MAX_CELLS,
  heartbeatTone,
  signalTone,
  type ProgressSegment,
} from "./progress";

/** The fixture epic (`fixtures/factory/epics-launch-fixes.json`), which is the
 * counts DEpic itself was drawn from. */
const FIXTURE = {
  todo: 4,
  in_progress: 1,
  in_review: 1,
  done: 13,
  failed: 0,
  held: 0,
  blocked: 1,
  cancelled: 0,
};

function segments(counts: Record<string, number>): ProgressSegment[] {
  const bar = epicProgress(counts).bar;
  if (bar.kind !== "segments")
    throw new Error(`expected segments, got ${bar.kind}`);
  return bar.segments;
}

function cells(counts: Record<string, number>): string[] {
  const bar = epicProgress(counts).bar;
  if (bar.kind !== "cells") throw new Error(`expected cells, got ${bar.kind}`);
  return bar.cells;
}

describe("epicProgress", () => {
  it("counts the nodes DEpic counts: 13 / 20 done · 65%", () => {
    const p = epicProgress(FIXTURE);
    expect(p.total).toBe(20);
    expect(p.done).toBe(13);
    expect(p.pct).toBe(65);
  });

  it("draws one cell per node, in the order done → in review → in progress → blocked → held → failed → todo", () => {
    expect(cells(FIXTURE)).toEqual([
      ...Array(13).fill("done"),
      "in_review",
      "in_progress",
      "blocked",
      ...Array(4).fill("todo"),
    ]);
  });

  it("keeps the bar order it publishes", () => {
    expect([...BAR_ORDER]).toEqual([
      "done",
      "in_review",
      "in_progress",
      "blocked",
      "held",
      "failed",
      "todo",
    ]);
  });

  it("draws a cell for a status the bar order never named, after the ones it did", () => {
    // The snapshot serves the whole `DriverStatus` enum, not the seven words
    // the bar orders — a cell per node has to mean a cell per node.
    expect(cells({ done: 1, awaiting_publication: 2, todo: 1 })).toEqual([
      "done",
      "todo",
      "awaiting_publication",
      "awaiting_publication",
    ]);
  });

  it("stays on cells at the 40-node limit and switches to segments past it", () => {
    expect(epicProgress({ todo: MAX_CELLS }).bar.kind).toBe("cells");
    expect(epicProgress({ todo: MAX_CELLS + 1 }).bar.kind).toBe("segments");
  });

  it("gives each segment its share of the bar, in the same order, zeros omitted", () => {
    const s = segments({ done: 45, todo: 15, held: 0 });
    expect(s).toEqual([
      { status: "done", count: 45, pct: 75 },
      { status: "todo", count: 15, pct: 25 },
    ]);
  });

  it("spends the whole bar: the segment shares sum to 100", () => {
    const s = segments({ done: 20, in_review: 7, blocked: 13, todo: 41 });
    expect(s.reduce((acc, seg) => acc + seg.pct, 0)).toBeCloseTo(100, 6);
  });

  it("gives every status count a legend entry, zeros included, in the payload order", () => {
    const p = epicProgress(FIXTURE);
    expect(p.legend.map((e) => e.status)).toEqual(Object.keys(FIXTURE));
    expect(p.legend.map((e) => e.count)).toEqual(Object.values(FIXTURE));
  });

  it("names the bar by what it drew, so a screen reader gets the counts too", () => {
    expect(epicProgress(FIXTURE).barLabel).toBe(
      "13 done, 1 in review, 1 in progress, 1 blocked, 4 todo",
    );
  });

  it("keeps 100% for a finished epic, never for one with work left", () => {
    // 199/200 is 99.5, and rounding it up put "199 / 200 done · 100%" on the
    // strip — the percentage contradicting the tally printed beside it.
    expect(epicProgress({ done: 199, todo: 1 }).pct).toBe(99);
    expect(epicProgress({ done: 200 }).pct).toBe(100);
    // Nothing but done statuses left: still complete, still 100.
    expect(epicProgress({ done: 3, failed: 0, todo: 0 }).pct).toBe(100);
  });

  it("keeps 0% for an epic nobody has moved, never for one with a node done", () => {
    // The mirror of the above: 1/500 is 0.2, and rounding it down would read
    // "1 / 500 done · 0%" over a bar with a filled cell in it.
    expect(epicProgress({ done: 1, todo: 499 }).pct).toBe(1);
    expect(epicProgress({ todo: 500 }).pct).toBe(0);
  });

  it("rounds everything between to the nearest whole percent", () => {
    expect(epicProgress({ done: 1, todo: 2 }).pct).toBe(33);
    expect(epicProgress({ done: 2, todo: 1 }).pct).toBe(67);
    expect(epicProgress({ done: 1, todo: 1 }).pct).toBe(50);
  });

  it("reads an empty epic as 0 / 0 rather than dividing by it", () => {
    const p = epicProgress({});
    expect([p.total, p.done, p.pct]).toEqual([0, 0, 0]);
    expect(p.bar).toEqual({ kind: "cells", cells: [] });
    expect(p.legend).toEqual([]);
    expect(p.barLabel).toBe("nothing yet");
  });

  it("ignores a count that is not a whole number of nodes", () => {
    // Counts come off the wire; a negative or fractional one would otherwise
    // fabricate cells, or a percentage over 100.
    const p = epicProgress({
      done: 2,
      todo: -1,
      held: 1.5,
      blocked: Number.NaN,
    });
    expect(p.total).toBe(2);
    expect(
      cells({ done: 2, todo: -1, held: 1.5, blocked: Number.NaN }),
    ).toEqual(["done", "done"]);
    expect(p.legend.map((e) => e.status)).toEqual(["done"]);
  });
});

describe("cellStyle", () => {
  it("fills a cell for a state that is settled or running", () => {
    expect(cellStyle("done")).toBe("background: var(--success)");
    expect(cellStyle("in_progress")).toBe("background: var(--accent-2)");
    expect(cellStyle("failed")).toBe("background: var(--danger)");
  });

  it("outlines a cell in its own colour for a state that is waiting on something", () => {
    expect(cellStyle("in_review")).toBe(
      "box-shadow: inset 0 0 0 1.5px var(--accent-2)",
    );
    expect(cellStyle("held")).toBe("box-shadow: inset 0 0 0 1.5px var(--warn)");
    expect(cellStyle("blocked")).toBe(
      "box-shadow: inset 0 0 0 1.5px var(--status-blocked)",
    );
  });

  it("draws the quiet states as a hairline box, never in a status colour", () => {
    // DEpic's four todo cells are a 1px --hair-strong box: work nobody has
    // started is the ground the bar is drawn on, not a state with a colour.
    const hairline = "box-shadow: inset 0 0 0 1px var(--hair-strong)";
    expect(cellStyle("todo")).toBe(hairline);
    expect(cellStyle("cancelled")).toBe(hairline);
    expect(cellStyle("a status nobody has shipped")).toBe(hairline);
  });
});

describe("signalTone", () => {
  it("halos the cyan dot while the driver is progressing", () => {
    expect(signalTone("progressing")).toEqual({
      color: "var(--accent-2)",
      halo: true,
    });
  });

  it("turns the dot green when the epic is complete, with no halo to pulse", () => {
    expect(signalTone("complete")).toEqual({
      color: "var(--success)",
      halo: false,
    });
  });

  it("draws a stall amber, and magenta when a person is next — never red (#2539)", () => {
    // DESIGN.md reserves --danger for failed; a stalled driver is a wait.
    expect(signalTone("stalled")).toEqual({
      color: "var(--warn)",
      halo: false,
    });
    expect(signalTone("stalled", true)).toEqual({
      color: "var(--accent)",
      halo: false,
    });
    // Only a stall turns on who is next.
    expect(signalTone("progressing", true).color).toBe("var(--accent-2)");
  });

  it("stays quiet for a signal it does not know, rather than picking an alarm", () => {
    expect(signalTone("something new")).toEqual({
      color: "var(--text-3)",
      halo: false,
    });
    expect(signalTone(null)).toEqual({ color: "var(--text-3)", halo: false });
  });
});

describe("heartbeatTone (design review c4, D32)", () => {
  it("keeps the cyan halo over a fresh live heartbeat", () => {
    expect(heartbeatTone("progressing", true)).toEqual(signalTone("progressing"));
  });

  it("greys progressing and drops the halo without one", () => {
    expect(heartbeatTone("progressing", false)).toEqual({
      color: "var(--text-3)",
      halo: false,
    });
  });

  it("leaves the signals that never halo as they are", () => {
    for (const signal of ["complete", "stalled", "something new", null]) {
      expect(heartbeatTone(signal, false)).toEqual(signalTone(signal));
    }
  });

  it("carries the person-next stall colour through (#2539 D24 with #2540 D32)", () => {
    // Magenta with a person next, amber without, heartbeat or not: a stall
    // never haloes, so the heartbeat rule leaves its colour alone.
    expect(heartbeatTone("stalled", false, true)).toEqual({ color: "var(--accent)", halo: false });
    expect(heartbeatTone("stalled", true, false)).toEqual({ color: "var(--warn)", halo: false });
    // And a person next never puts a halo on a progressing epic without a heartbeat.
    expect(heartbeatTone("progressing", false, true)).toEqual({ color: "var(--text-3)", halo: false });
  });
});
