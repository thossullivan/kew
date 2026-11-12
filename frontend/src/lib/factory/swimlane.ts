/**
 * FUI-07 — the pipeline swimlane's pure decisions, with no SVG and no DOM.
 *
 * A swimlane is a row of stage cells, and each cell is two things: a marker for
 * what happened at that stage, and the segment that leads to the next cell. The
 * segment is the part that carries the claim. A solid green line between two
 * cells says "the path from here to there is attested"; a dashed one says the
 * path has not been walked yet. So the segment cannot be read off one cell — it
 * is a property of the PAIR, which is why `segmentStyle` takes both.
 *
 * Two rules keep it honest, and swimlane.test.ts pins both:
 *
 * 1. **An unknown state is pending, never attested.** The stage vocabulary is
 *    FUI-11's projection, which is not built; a word this file has never seen
 *    is a word whose meaning it cannot vouch for, and the failure that matters
 *    is a green line under an unproven step. Unknown draws the dashed pending
 *    ring, and keeps the server's own word as the mark's accessible name.
 * 2. **Green needs green on both ends.** `attested` is the only pair that earns
 *    the solid green segment. A skipped stage leading into an attested one gets
 *    the quiet hairline-strong line the artboard draws under "not required":
 *    the path was walked, but nothing along it was proven.
 *
 * Shapes and colours are not decided here — `statusShape` (FUI-04) owns those,
 * so the swimlane cannot invent an eighteenth meaning or leak the magenta that is
 * rationed to "a human is needed". This file only maps the pipeline's own state
 * words onto that vocabulary and measures the lines between them.
 *
 * Every number comes off `frontend/design/factory-ui/`: the DWork pipeline
 * lanes (x 468–1392, y ≈336–522) at 14px and the DEpic selected-rail pipeline
 * (x ≈368–832, y ≈780–840) at 12px.
 */
import {
  QUIET_MEANINGS,
  statusShape,
  type StatusMark,
  type StatusMeaning,
  type StatusVocabulary
} from './status';

/** How far a cell's state lets the path advance. */
export type CellFlow =
  /** Proven: the stage happened and left something attestable. */
  | 'settled'
  /** Happening right now. */
  | 'live'
  /** The path stops here — pending, awaiting, needs you, or gone wrong. */
  | 'stop'
  /** Nothing to do at this stage, so the path passes straight through. */
  | 'skipped'
  /** Not part of this lane at all: the 6px stub on a dashed hairline. */
  | 'off';

/** Which flow class each status meaning puts a cell in. */
export const FLOW_OF: Record<StatusMeaning, CellFlow> = {
  done: 'settled',
  working: 'live',
  // `live` here is about the PATH — the connector reaches this cell and goes on
  // — not about motion. A published pull request waiting for a human is a stage
  // that happened, so the path advances through it; whether it ANIMATES is
  // $lib/factory/motion's call, and there the answer is no (#2305).
  published_waiting: 'live',
  not_required: 'skipped',
  // Everything else stops the path. `held`, `blocked`, `failed`, `cancelled`,
  // `missing` and `unverified` stop it for a reason worth reading; `todo`,
  // `idle`, `queued`, `pending` and `needs_you` stop it because it has not
  // got here yet.
  todo: 'stop',
  idle: 'stop',
  queued: 'stop',
  pending: 'stop',
  needs_you: 'stop',
  held: 'stop',
  blocked: 'stop',
  failed: 'stop',
  cancelled: 'stop',
  missing: 'stop',
  unverified: 'stop',
  multi_task: 'stop',
  // A review round that went against the change (#3466) is amber, not the
  // cyan of work in flight, but the path did reach it and goes on: the change
  // is being reworked. `live` is about the PATH, as for `published_waiting`.
  not_ready: 'live'
};

/**
 * Pipeline words that are not in the status vocabulary, and what they mean
 * there. `awaiting` is the artboard's release lane ("awaiting backport"): a
 * step that is expected and has not arrived, which is exactly `pending`.
 */
const STATE_ALIAS: Record<string, string> = {
  awaiting: 'pending',
  skipped: 'not_required'
};

/** The state word for a cell that is not part of its lane. */
export const INACTIVE = 'inactive';

/**
 * The stub, which is the one mark the swimlane draws itself.
 *
 * It is not a status: it says the lane has no stage here at all, which is a
 * statement about the GRID rather than about any work. `statusShape` has no
 * word for that and should not grow one, so the dot is spelled out — the
 * artboard's 6px `--hair-strong` circle, the same weight as the hairline it
 * sits on (DWork release lane, x 468–1392, y ≈450–522).
 */
export const STUB_MARK: StatusMark = {
  shape: 'dot',
  color: 'var(--hair-strong)',
  word: INACTIVE,
  meaning: 'idle',
  known: true
};

export type SegmentKind =
  /** Solid green: both ends attested. */
  | 'attested'
  /** Solid cyan: the path is being walked into the next cell right now. */
  | 'working'
  /** Solid hairline-strong: walked, but nothing along it was proven. */
  | 'quiet'
  /** Dashed `--text-5`: the path has not reached the next cell. */
  | 'pending'
  /** 1px dashed: a lane that is not running. */
  | 'hairline'
  /** The last column, which leads nowhere. */
  | 'none';

export type SegmentStyle = {
  kind: SegmentKind;
  /** A `var(--token)` reference, never a literal hex — themes must switch. */
  color: string;
  /** Line thickness in px; 0 draws nothing. */
  width: number;
  dashed: boolean;
};

/** The five lines a swimlane draws, plus the absence of one. */
export const SEGMENTS: Record<SegmentKind, SegmentStyle> = {
  attested: { kind: 'attested', color: 'var(--success)', width: 2, dashed: false },
  working: { kind: 'working', color: 'var(--accent-2)', width: 2, dashed: false },
  quiet: { kind: 'quiet', color: 'var(--hair-strong)', width: 2, dashed: false },
  pending: { kind: 'pending', color: 'var(--text-5)', width: 2, dashed: true },
  hairline: { kind: 'hairline', color: 'var(--hair-strong)', width: 1, dashed: true },
  none: { kind: 'none', color: 'transparent', width: 0, dashed: false }
};

export type SwimlaneMarker = {
  /** True for the inactive stub: a small dot, not a status mark. */
  stub: boolean;
  /** The status word to draw — alias resolved, unknown collapsed to `pending`. */
  status: string;
  /** The caller's own word, kept for the accessible name even when unknown. */
  word: string;
  /** Shape and colour, so a caller can paint without `StatusMark.svelte`. */
  mark: StatusMark;
  flow: CellFlow;
  /** False when the state was not recognised and the pending ring was drawn. */
  known: boolean;
};

/** Which caption line, if either, carries the status and takes its colour. */
export type StatusLine = 'primary' | 'secondary' | null;

export type SwimlaneCell = {
  /** The stage's state. `null` or `'inactive'` draws the stub. */
  state?: string | null;
  /** First caption line — the artboard's "run f5dcb6a5", "round 1". */
  primary?: string | null;
  /** Second caption line — "success", "reviewing", "merge decision". */
  secondary?: string | null;
  /** Defaults to `'secondary'`, which is where the artboard puts the word. */
  statusLine?: StatusLine;
  /**
   * The stage is over though its state draws something still owed: a
   * published PR nobody scanned keeps the pending ring (#3466). The mark is
   * unchanged; the path runs through the cell as through a settled one.
   */
  passed?: boolean;
};

export type SwimlaneLane = {
  label: string;
  cells: readonly SwimlaneCell[];
  /** A lane nobody is running: its name is drawn quiet. */
  muted?: boolean;
};

/** One cell, resolved: everything `Swimlane.svelte` needs to draw it. */
export type SwimlaneCellView = {
  /** 0-based column. */
  column: number;
  /** The column's stage name, or `null` for a column no stage named. */
  stage: string | null;
  cell: SwimlaneCell;
  marker: SwimlaneMarker;
  /** The line leading to the next cell; `none` in the last column. */
  segment: SegmentStyle;
};

export type SwimlaneSize = 12 | 14;

export type SwimlaneGeometry = {
  /** The marker box, px. */
  size: SwimlaneSize;
  /** Gap between a cell's parts: stage label, marker row, caption. */
  gap: number;
  /** Row padding above and below the cells. */
  padTop: number;
  padBottom: number;
  /** Width of the lane-label column. */
  laneWidth: number;
  /** Right gutter on a caption, so two columns' text never touches. */
  captionGutter: number;
  /**
   * The narrowest a stage column may be drawn, px.
   *
   * A floor, not a width: above it the columns are still `1fr` each and the
   * artboards' own tracks come out unchanged. Below it the grid stops
   * squeezing and the lane scrolls, because a column narrower than its own
   * header prints the next header on top of it — at 390px the DWork pipeline
   * read "IMPLEMENTPUBLISHREVIEW" with "f5dcb6a5" over "scan" (#2372).
   *
   * The number is the widest thing one column has to hold: a stage name in
   * 11px mono tracked 0.14em (~8.2px a character, so "IMPLEMENT" is ~74px),
   * and a caption's longest unbreakable token — a short SHA, eight characters
   * of 13px mono, ~63px — plus the `captionGutter` it owes the next column.
   */
  minColumn: number;
  /** Whether a lane row closes with a hairline. */
  rowRule: boolean;
};

/**
 * The two sizes, measured off the artboards: 14px for the DWork pipeline (rows
 * padded 14/16 and closed with a rule) and 12px for the DEpic rail, which is a
 * single unruled band inside a column that already has its own border.
 */
export const SWIMLANE_SIZES: Record<SwimlaneSize, SwimlaneGeometry> = {
  14: {
    size: 14,
    gap: 10,
    padTop: 14,
    padBottom: 16,
    laneWidth: 148,
    captionGutter: 16,
    minColumn: 112,
    rowRule: true
  },
  12: {
    size: 12,
    gap: 8,
    padTop: 0,
    padBottom: 0,
    laneWidth: 120,
    captionGutter: 12,
    minColumn: 84,
    rowRule: false
  }
};

/**
 * The narrowest the whole lane may be drawn: the lane column, when there is
 * one, plus `minColumn` per stage column.
 *
 * `Swimlane` puts this on every band of the grid — the header rule, the 6px
 * ticks and each lane row — so they keep one set of columns while the box
 * scrolls under them, and the rules span the lane rather than stopping at the
 * viewport's edge.
 */
export function laneMinWidth(
  size: SwimlaneSize,
  columns: number,
  laneLabels: boolean
): number {
  const g = SWIMLANE_SIZES[size];
  return (laneLabels ? g.laneWidth : 0) + Math.max(columns, 0) * g.minColumn;
}

/** The stub's diameter as a fraction of the box: 6px in DWork's 14px cell. */
export const STUB = 6 / 14;

/** `in-progress`, `In Progress` and `in_progress` are one state. */
function normalize(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

/** Own-property lookup; see `status.ts` for why a plain index is not enough. */
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/** 2dp is finer than a pixel on a 1440px board. */
function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/**
 * The marker for one cell's state.
 *
 * Never throws and never returns nothing. A missing state, or the word
 * `inactive`, is the stub; anything the status vocabulary does not recognise
 * becomes the dashed pending ring while keeping its own word, so a projection
 * that grows a stage state renders honestly rather than claiming attestation.
 */
export function markerFor(
  state: string | null | undefined,
  vocabulary: StatusVocabulary = 'epic',
  passed = false
): SwimlaneMarker {
  const marker = stateMarker(state, vocabulary);
  // A stage that is over lets the path through whatever its mark says is
  // still owed; only the server's `passed` says so, never a word.
  return passed && marker.flow === 'stop' ? { ...marker, flow: 'settled' } : marker;
}

function stateMarker(
  state: string | null | undefined,
  vocabulary: StatusVocabulary
): SwimlaneMarker {
  const key = state == null ? INACTIVE : normalize(state);
  if (key === '' || key === INACTIVE) {
    return {
      stub: true,
      status: INACTIVE,
      word: INACTIVE,
      mark: STUB_MARK,
      flow: 'off',
      known: true
    };
  }
  const status = own(STATE_ALIAS, key) ?? key;
  const mark = statusShape(status, vocabulary);
  if (!mark.known) {
    // The word is unknown, so its meaning is unknown: draw the step still
    // ahead, and let the caption and the accessible name say what came in.
    const pending = statusShape('pending', vocabulary);
    return {
      stub: false,
      status: 'pending',
      word: mark.word,
      mark: { ...pending, word: mark.word, known: false },
      flow: FLOW_OF.pending,
      known: false
    };
  }
  return { stub: false, status, word: mark.word, mark, flow: FLOW_OF[mark.meaning], known: true };
}

/** Whether the path has arrived at a cell in this flow class. */
function reached(flow: CellFlow): boolean {
  return flow === 'settled' || flow === 'live' || flow === 'skipped';
}

/**
 * The line leading from `cur` to the cell after it.
 *
 * Pass `next` as `undefined` — which is what indexing past the end of a lane
 * gives you — for the last column, which leads nowhere. `null` is different: it
 * is a cell that exists and is not part of this lane, and it takes the dashed
 * hairline.
 */
export function segmentStyle(
  cur: string | null | undefined,
  next?: string | null,
  vocabulary: StatusVocabulary = 'epic'
): SegmentStyle {
  if (next === undefined) return SEGMENTS.none;
  return flowSegment(markerFor(cur, vocabulary).flow, markerFor(next, vocabulary).flow);
}

/** The line from a cell in flow class `from` to one in `to`. */
function flowSegment(from: CellFlow, to: CellFlow): SegmentStyle {
  if (from === 'off' || to === 'off') return SEGMENTS.hairline;
  if (to === 'live') return SEGMENTS.working;
  if (!reached(from) || !reached(to)) return SEGMENTS.pending;
  // Both ends were walked. Green is reserved for the pair that can prove it.
  return from === 'settled' && to === 'settled' ? SEGMENTS.attested : SEGMENTS.quiet;
}

/**
 * How many columns the grid needs: the named stages, widened by any lane that
 * carries more cells than there are names. A lane is never truncated — a cell
 * the caller sent is a cell the reader gets to see, even unlabelled.
 */
export function columnCount(
  stages: readonly string[],
  lanes: readonly SwimlaneLane[] = []
): number {
  return lanes.reduce((n, lane) => Math.max(n, lane.cells.length), stages.length);
}

/**
 * One lane, padded out to `columns`.
 *
 * A lane with fewer stages than the grid has columns is the normal case, not an
 * error: the DWork release lane names one stage and sits out the other four.
 * The padding is inactive, so those columns draw the stub on a hairline instead
 * of inheriting the last real cell's state.
 */
export function laneCells(
  cells: readonly SwimlaneCell[],
  columns: number
): readonly SwimlaneCell[] {
  if (cells.length >= columns) return cells;
  return [...cells, ...Array.from({ length: columns - cells.length }, () => ({ state: null }))];
}

/** A whole lane resolved to markers and segments, ready to draw. */
export function laneView(
  lane: SwimlaneLane,
  columns: number,
  stages: readonly string[] = [],
  vocabulary: StatusVocabulary = 'epic'
): SwimlaneCellView[] {
  const cells = laneCells(lane.cells, columns);
  const markers = cells.map((cell) => markerFor(cell.state, vocabulary, cell.passed === true));
  return cells.map((cell, column) => ({
    column,
    stage: stages[column] ?? null,
    cell,
    marker: markers[column],
    segment:
      column + 1 < cells.length
        ? flowSegment(markers[column].flow, markers[column + 1].flow)
        : SEGMENTS.none
  }));
}

/**
 * The colour of one caption line.
 *
 * The artboard colours whichever line carries the status word in that status's
 * own colour and leaves the other quiet — "run f5dcb6a5" in `--text-1` over a
 * green "success", and a magenta "needs you" over a `--text-3` "merge
 * decision". A quiet meaning keeps a quiet word, exactly as `StatusMark` does,
 * so "not required" does not shout in `--text-4`.
 */
export function captionTone(
  marker: SwimlaneMarker,
  line: 'primary' | 'secondary',
  statusLine: StatusLine = 'secondary'
): string {
  if (marker.stub) return 'var(--text-3)';
  if (line === statusLine) {
    return QUIET_MEANINGS.has(marker.mark.meaning) ? 'var(--text-3)' : marker.mark.color;
  }
  return line === 'primary' ? 'var(--text-1)' : 'var(--text-3)';
}

/** Where a segment starts: the marker's centre, so it leaves from the mark. */
export function segmentLeft(size: number): number {
  return size / 2;
}

/**
 * A segment's top edge, so its centreline crosses the marker's.
 *
 * Rounded, because a 1px line on a half pixel renders as two grey ones: the
 * artboards draw the 2px lines at 6 (of 14) and 5 (of 12) and the 1px hairline
 * at 7, which is exactly what rounding the true centre gives.
 */
export function segmentTop(size: number, width: number): number {
  return Math.round((size - width) / 2);
}

/** The inactive stub's diameter — 6px in a 14px cell. */
export function stubSize(size: number): number {
  return round2(size * STUB);
}

/** The stub's offset from the cell's corner, which centres it. */
export function stubOffset(size: number): number {
  return round2((size - stubSize(size)) / 2);
}
