// Where the DAG canvas looks, and where the popover goes (#2250), as plain
// arithmetic over the Flow model — the canvas itself has no DOM in this test
// suite, so every decision it makes about the viewport is made here.
//
// Three decisions from the UXEpic artboard and Tom's notes on it:
//
// * FIRST FRAME. A small epic is fitted whole, so it reads at a glance. A big
//   one opens on the node that needs a person, framed with as much of its
//   upstream path as still reads ("Fitting all 39 nodes would not read"), with
//   "Fit all" (F) one key away. A `?node=` link opens on that node instead.
// * ZOOM is clamped, and zooming from the controls keeps the canvas centre
//   where it was.
// * THE POPOVER sits clear of every card: it is placed on whichever side of
//   its node overlaps nothing, and only when no side is free does it move
//   further out — with a leader back to the node, as the artboard draws it.
import type { FlowModel, FlowNode } from './flow-model';
import { needsYou, type NodeFacts } from './attention';
import { NODE_W, RANK_SEP } from './dagre-layout';

export type Box = { x: number; y: number; width: number; height: number };
export type View = { x: number; y: number; zoom: number };
export type Pad = { top: number; right: number; bottom: number; left: number };

// The canvas's node area is the geometry every fit is measured in, and the
// canvas's chrome is NOT in it (design review C2, D19).
//
// Reserving the controls bar as padding was the first answer, and it only held
// where the frame was a whole-graph fit. The first frame of a big epic is a
// CROP at 100% (#2250), so cards the frame is not about carry on past every
// edge, and at 1024x700 — a short canvas, where the rail takes the height the
// graph does not — the operator's browser pass found the bar and the key
// legend sitting on two of them. No arithmetic can empty a floating band on a
// crop: the band is 34px tall and the layout's gaps are 16px, so there is no
// slide that puts a 34px bar in one.
//
// So the chrome is no longer floated over the cards at all. `DagView` draws it
// as a rail BELOW the flow — the toolbar, the key legend and the first frame's
// caption in one row the graph does not paint into — and the flow clips at its
// own foot. What is left for a pad to keep clear is the rim itself.
/** How far the minimap sits off the node area's corner (`.svelte-flow__minimap`
 * carries the same 16px margin). The one overlay still drawn over the graph:
 * DESIGN.md's `dag-canvas` puts it bottom-right, and `firstFrame` keeps the
 * node it is about clear of its column. */
export const MINIMAP_INSET = 16;
/** The gap a frame leaves between a card and the rim of the node area, where
 * the canvas's mask fades an edge card out. */
export const RIM_GAP = 16;

/** Room kept clear round a CROPPED frame: wider at the sides, because the
 * path it frames is read left to right and the column either side of it should
 * read as continuing rather than as cut off. */
export const FRAME_PAD: Pad = { top: RIM_GAP, right: 48, bottom: RIM_GAP, left: 48 };
/** Room kept round a WHOLE-graph frame, whose own box already carries the
 * layout's margin: the rim, and nothing else to clear. */
export const FIT_PAD: Pad = { top: 8, right: RIM_GAP, bottom: RIM_GAP, left: RIM_GAP };
export const MIN_ZOOM = 0.2;
export const MAX_ZOOM = 1.6;
/** The largest zoom a fit goes to: the artboard's cards at their own size. */
export const FIT_MAX_ZOOM = 1;
/** Below this zoom a card is drawn zoomed out (its status row as marks). */
export const READABLE_ZOOM = 0.75;
/** The smallest zoom a first frame focused on one node may use: the cards'
 * own size (D37). At 80% a card's 11px status row rendered at 9px, below the
 * type scale's floor, so the path that does not fit at 100% is cropped to the
 * part that does and the rest is left to pan and Fit all. */
export const FOCUS_MIN_ZOOM = FIT_MAX_ZOOM;
/** The minimap is drawn when the whole graph would fit only below this zoom:
 * a picture of the graph is only worth a corner when the graph itself cannot
 * be taken in at once. */
export const MINIMAP_BELOW_ZOOM = 0.5;

/**
 * `pad` with the minimap's corner added, when the canvas is drawing one.
 *
 * The map is the one piece of chrome still floated over the node area
 * (DESIGN.md's `dag-canvas` draws it bottom-right), so a fit that reserves
 * only the rim hangs the bottom-right cards under it — the same finding as
 * D19, one corner over. Whether there IS a map is decided on the plain pad, so
 * this cannot feed back into itself.
 */
export function padClearOfMinimap<T>(pad: Pad, model: FlowModel<T>, w: number, h: number): Pad {
  if (!hasMinimap(model, w, h)) return pad;
  return {
    ...pad,
    right: Math.max(pad.right, MINIMAP_INSET + MINIMAP_W + RIM_GAP),
    bottom: Math.max(pad.bottom, MINIMAP_INSET + MINIMAP_H + RIM_GAP)
  };
}

/** The view that shows the whole graph ("Fit all", F). */
export function fitAllView<T>(model: FlowModel<T>, w: number, h: number): View {
  return viewFor(
    wholeBox(model),
    w,
    h,
    MIN_ZOOM,
    FIT_MAX_ZOOM,
    padClearOfMinimap(FIT_PAD, model, w, h)
  );
}

/** The zoom that fits `box` inside a `w`x`h` canvas, with `pad` kept clear. */
export function fitZoom(box: Box, w: number, h: number, pad: Pad = FRAME_PAD): number {
  const zx = (w - pad.left - pad.right) / Math.max(box.width, 1);
  const zy = (h - pad.top - pad.bottom) / Math.max(box.height, 1);
  return Math.min(zx, zy);
}

/** The view that centres `box` in the padded canvas at its fitted zoom,
 * clamped to `[min, max]`.
 *
 * `pad.bottom` is kept clear even when the zoom clamps and the box therefore
 * CANNOT fit: a graph too big for `min` zoom overflows the padded canvas, and
 * centring the overflow hung its lowest cards in the one band of the node area
 * something is still drawn over — the minimap's corner. The leftover goes off
 * the top instead, which is clear now that the first frame's caption is in the
 * rail below the graph rather than floating over it. */
export function viewFor(
  box: Box,
  w: number,
  h: number,
  min = MIN_ZOOM,
  max = FIT_MAX_ZOOM,
  pad: Pad = FRAME_PAD
): View {
  const zoom = Math.min(max, Math.max(min, fitZoom(box, w, h, pad)));
  const cx = pad.left + (w - pad.left - pad.right) / 2;
  const cy = pad.top + (h - pad.top - pad.bottom) / 2;
  const centred = cy - (box.y + box.height / 2) * zoom;
  const floor = h - pad.bottom - (box.y + box.height) * zoom;
  return {
    x: Math.round(cx - (box.x + box.width / 2) * zoom),
    y: Math.round(Math.min(centred, floor)),
    zoom
  };
}

/** The bounding box of some cards. */
export function boxOf<T>(nodes: readonly FlowNode<T>[]): Box {
  const x0 = Math.min(...nodes.map((n) => n.position.x));
  const y0 = Math.min(...nodes.map((n) => n.position.y));
  const x1 = Math.max(...nodes.map((n) => n.position.x + n.width));
  const y1 = Math.max(...nodes.map((n) => n.position.y + n.height));
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

/** The whole model's canvas. */
export function wholeBox<T>(model: FlowModel<T>): Box {
  return { x: 0, y: 0, width: model.width, height: model.height };
}

/** The node itself, then its parents, then theirs: the upstream path one
 * generation at a time, over the edges the model draws. */
export function upstreamGenerations<T>(model: FlowModel<T>, id: string): string[][] {
  const seen = new Set([id]);
  const out: string[][] = [[id]];
  let front = [id];
  for (;;) {
    const next: string[] = [];
    for (const at of front) {
      for (const e of model.edges) {
        if (e.target === at && !seen.has(e.source)) {
          seen.add(e.source);
          next.push(e.source);
        }
      }
    }
    if (!next.length) return out;
    out.push(next);
    front = next;
  }
}

/**
 * The first frame never opens below this zoom (#2543 D28, D37): go-live-2026-09
 * opened fitted whole at 20%, where no card reads, and then at 80%, where a
 * card's smallest text fell below the type scale's 11px. Below it the frame
 * is a crop, and "Fit all" (F) is still the whole graph, at whatever zoom
 * that takes.
 */
export const FIRST_FRAME_MIN_ZOOM = FIT_MAX_ZOOM;

/** Why a first frame is centred where it is — what the caption says. */
export type FrameReason = 'selected' | 'needs_you' | 'held' | 'stalled' | 'frontier' | 'start';

/** Which node a first frame is about, and why: the `?node=` selection when it
 * is on the canvas, else the first node somebody is waiting on, else the first
 * held one, else the first frontier node the driver recorded as stuck — or
 * null when the graph has none of those. */
export function focusOf<T extends NodeFacts>(
  model: FlowModel<T>,
  selected: string | null,
  frontier: ReadonlySet<string> = new Set()
): { id: string; why: FrameReason } | null {
  const picked = selected === null ? undefined : model.nodes.find((n) => n.id === selected);
  // The page's opening selection IS the node that needs you (D20), so a
  // selection says which of the two it is.
  if (picked)
    return { id: picked.id, why: picked.node !== null && needsYou(picked.node) ? 'needs_you' : 'selected' };
  const facts = model.nodes.filter((n) => n.node !== null);
  const first = (test: (node: T, id: string) => boolean): string | null =>
    facts.find((n) => test(n.node!, n.id))?.id ?? null;
  const asked = first((node) => needsYou(node));
  if (asked !== null) return { id: asked, why: 'needs_you' };
  const held = first((node) => node.status === 'held');
  if (held !== null) return { id: held, why: 'held' };
  const stalled = first((node, id) => frontier.has(id) && (node.stuck ?? '').trim() !== '');
  if (stalled !== null) return { id: stalled, why: 'stalled' };
  return null;
}

/** Where a frame with nothing to focus on starts when the whole graph does not
 * read: the first ready node, else the first node in the graph's reading
 * order (the top of its first column). */
function startOf<T>(
  model: FlowModel<T>,
  frontier: ReadonlySet<string>
): { id: string; why: FrameReason } {
  const ready = model.nodes.find((n) => frontier.has(n.id));
  if (ready) return { id: ready.id, why: 'frontier' };
  const head = [...model.nodes].sort(
    (a, b) => a.position.x - b.position.x || a.position.y - b.position.y
  )[0];
  return { id: head.id, why: 'start' };
}

export type FirstFrame = {
  view: View;
  /** The node the frame is centred on, or null when the whole graph is
   * shown. */
  focus: string | null;
  /** Why it is centred there; null with `focus`. */
  why: FrameReason | null;
};

/**
 * The first frame for a canvas `w`x`h`.
 *
 * Whole graph when it fits whole at `FIRST_FRAME_MIN_ZOOM`. Otherwise the
 * focus node (or, with none, the graph's start) and as many generations of its
 * upstream path as still fit at `FOCUS_MIN_ZOOM`, centred — never below
 * `FIRST_FRAME_MIN_ZOOM`, which is the cards' own size (D37).
 */
export function firstFrame<T extends NodeFacts>(
  model: FlowModel<T>,
  w: number,
  h: number,
  selected: string | null = null,
  frontier: ReadonlySet<string> = new Set()
): FirstFrame | null {
  if (!model.nodes.length || w <= 0 || h <= 0) return null;
  const whole = wholeBox(model);
  if (fitZoom(whole, w, h, FIT_PAD) >= FIRST_FRAME_MIN_ZOOM) {
    return { view: fitAllView(model, w, h), focus: null, why: null };
  }
  const focus = focusOf(model, selected, frontier) ?? startOf(model, frontier);
  // The minimap will be drawn in the bottom-right corner (the graph is too big
  // to read whole), so the framed path keeps clear of its corner: a card the
  // first frame is ABOUT must not open under the map.
  const pad = padClearOfMinimap(FRAME_PAD, model, w, h);
  const byId = new Map(model.nodes.map((n) => [n.id, n] as const));
  let path: FlowNode<T>[] = [byId.get(focus.id)!];
  for (const generation of upstreamGenerations(model, focus.id).slice(1)) {
    const wider = [...path, ...generation.map((id) => byId.get(id)!)];
    if (fitZoom(boxOf(wider), w, h, pad) < FOCUS_MIN_ZOOM) break;
    path = wider;
  }
  const box = boxOf(path);
  const view = viewFor(box, w, h, Math.max(FOCUS_MIN_ZOOM, FIRST_FRAME_MIN_ZOOM), FIT_MAX_ZOOM, pad);
  return { view: clearLeftEdge(model, view, box, w, pad), focus: focus.id, why: focus.why };
}

/** The minimap's size in the canvas corner. */
export const MINIMAP_W = 168;
export const MINIMAP_H = 100;

/** The width below which the popover stops being a box beside its card and
 * becomes a bottom sheet: a 320px panel placed "clear of every card" on a
 * 390px canvas can only ever cover the card it is about (D5, and DESIGN.md's
 * Responsive Behavior: "on the Epic page the popover becomes a bottom
 * sheet"). The same 1024 the key hints and the minimap go at. */
export const SHEET_BELOW_W = 1024;

/** The narrowest canvas that gets a minimap. Below 1024 the map took a third
 * of a phone's canvas to picture a graph already too small to read
 * (DESIGN.md Responsive Behavior, D5), so there it is not drawn at all. */
export const MINIMAP_MIN_W = SHEET_BELOW_W;

/** Whether the canvas draws a minimap: only when the graph is too big to be
 * taken in at once (`MINIMAP_BELOW_ZOOM`), and only on a canvas wide enough
 * to spare the corner (`MINIMAP_MIN_W`). */
export function hasMinimap<T>(model: FlowModel<T>, w: number, h: number): boolean {
  return (
    w >= MINIMAP_MIN_W &&
    h > 0 &&
    model.width > 0 &&
    fitZoom(wholeBox(model), w, h, FIT_PAD) < MINIMAP_BELOW_ZOOM
  );
}

/**
 * Slide a cropped frame sideways so no card is cut in half by the canvas's
 * left edge: the artboard's frame "starts just right of SFD-250, so no
 * partial card shows at the left edge". The column the edge cuts through is
 * either brought fully in or pushed fully out — whichever is the smaller move
 * that still keeps the framed path inside the padded canvas.
 */
export function clearLeftEdge<T>(
  model: FlowModel<T>,
  view: View,
  box: Box,
  w: number,
  pad: Pad = FRAME_PAD
): View {
  const z = view.zoom;
  const cut = model.layers.find((l) => {
    const left = l.x * z + view.x;
    return left < 0 && left + NODE_W * z > 0;
  });
  if (!cut) return view;
  const bringIn = -(cut.x * z + view.x);
  const pushOut = -((cut.x + NODE_W + RANK_SEP / 2) * z + view.x);
  const fits = (dx: number): boolean => {
    const s = onScreen(box, { ...view, x: view.x + dx });
    // Half the left pad may go (the column being cleared is what sits
    // there), none of the right: that is where the minimap is kept clear of.
    return s.x >= pad.left / 2 && s.x + s.width <= w - pad.right;
  };
  const moves = [bringIn, pushOut].filter(fits).sort((a, b) => Math.abs(a) - Math.abs(b));
  return moves.length ? { ...view, x: Math.round(view.x + moves[0]) } : view;
}

/** Zoom by `factor` about the canvas centre, clamped. */
export function zoomBy(view: View, factor: number, w: number, h: number): View {
  const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, view.zoom * factor));
  const k = zoom / view.zoom;
  return { x: w / 2 - (w / 2 - view.x) * k, y: h / 2 - (h / 2 - view.y) * k, zoom };
}

/** A box in flow coordinates, on screen. */
export function onScreen(box: Box, view: View): Box {
  return {
    x: box.x * view.zoom + view.x,
    y: box.y * view.zoom + view.y,
    width: box.width * view.zoom,
    height: box.height * view.zoom
  };
}

/**
 * The view that brings `box` fully on screen with the least panning, or null
 * when it already is — so walking the graph with the keyboard follows the
 * selection without re-centring on every step.
 */
export function reveal(box: Box, view: View, w: number, h: number, pad: Pad = FRAME_PAD): View | null {
  const s = onScreen(box, view);
  let dx = 0;
  let dy = 0;
  if (s.x < pad.left) dx = pad.left - s.x;
  else if (s.x + s.width > w - pad.right) dx = w - pad.right - (s.x + s.width);
  if (s.y < pad.top) dy = pad.top - s.y;
  else if (s.y + s.height > h - pad.bottom) dy = h - pad.bottom - (s.y + s.height);
  if (dx === 0 && dy === 0) return null;
  return { x: view.x + dx, y: view.y + dy, zoom: view.zoom };
}

function overlap(a: Box, b: Box): number {
  const w = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

/** The gap between two boxes, 0 when they touch or overlap. */
function gap(a: Box, b: Box): number {
  const dx = Math.max(0, a.x - (b.x + b.width), b.x - (a.x + a.width));
  const dy = Math.max(0, a.y - (b.y + b.height), b.y - (a.y + a.height));
  return Math.hypot(dx, dy);
}


export type Placement = {
  /** The popover's top-left, in canvas pixels. */
  x: number;
  y: number;
  /** Whether it covers no card and none of the canvas's furniture. */
  clear: boolean;
  /** A dashed line from the node to the popover, when they do not touch. */
  leader: { x1: number; y1: number; x2: number; y2: number } | null;
};

/** How far the popover sits off its node when a side is free. */
export const POPOVER_GAP = 14;
/** How far the popover keeps off the canvas rim — on every edge, which is
 * also how much shorter than the node area the box itself may be drawn
 * (`.pop-at` in DagView caps it, because a taller box is clamped to this rim
 * and the overflow is clipped away by `.floor`). */
export const POPOVER_RIM = 8;

/** The node area under which the box beside a card draws its rows tighter
 * (`short` on NodePopover, #3460). Summed from what the box pins at its
 * clamps, each rounded up: its padding and border 26, the header 24, a
 * two-line title 41, a three-line needs-you sentence under its label 89,
 * the action row wrapped to two rows 75, and the four 8px gaps between them;
 * then the 60px the detail needs before its fade can show (`FADE_ROOM`), and
 * the rim on each side. The action row is counted wrapped: its three links'
 * arrows are drawn in Plex Mono on every platform (`lib/fonts/`, #3478), and
 * the box is sized for the row on two rows. Under this, on a canvas near its
 * 360px floor, the box is 291px and the loose rows leave the detail no fade. */
export const POPOVER_SHORT_BELOW_H = 26 + 24 + 41 + 89 + 75 + 4 * 8 + 60 + 2 * POPOVER_RIM;

type Spot = { x: number; y: number };

/** The twelve spots beside a node: each side, at three alignments. */
function besideSpots(node: Box, pw: number, ph: number): Spot[] {
  const g = POPOVER_GAP;
  const ys = [node.y, node.y + node.height / 2 - ph / 2, node.y + node.height - ph];
  const xs = [node.x, node.x + node.width - pw, node.x + node.width / 2 - pw / 2];
  return [
    ...ys.map((y) => ({ x: node.x + node.width + g, y })),
    ...xs.map((x) => ({ x, y: node.y + node.height + g })),
    ...ys.map((y) => ({ x: node.x - g - pw, y })),
    ...xs.map((x) => ({ x, y: node.y - g - ph }))
  ];
}

/**
 * Where the popover goes, in canvas pixels.
 *
 * `node` is the selected card on screen, `cards` every other card on screen,
 * `furniture` the canvas's own overlays (controls, minimap, hint). Spots are
 * the four sides of the node at three alignments each, then a grid further
 * out; every spot is first pulled inside the canvas, and the one covering
 * least wins — covering the selected card itself costs far more than covering
 * a neighbour, because the popover is ABOUT that card. A popover that does
 * not touch its node gets a leader back to it.
 */
export function placePopover(
  node: Box,
  size: { width: number; height: number },
  cards: readonly Box[],
  furniture: readonly Box[],
  w: number,
  h: number
): Placement {
  const { width: pw, height: ph } = size;
  const inside = (s: Spot): Spot => ({
    x: Math.round(Math.min(Math.max(s.x, POPOVER_RIM), Math.max(POPOVER_RIM, w - pw - POPOVER_RIM))),
    y: Math.round(Math.min(Math.max(s.y, POPOVER_RIM), Math.max(POPOVER_RIM, h - ph - POPOVER_RIM)))
  });
  const covered = (s: Spot): number => {
    const box = { x: s.x, y: s.y, width: pw, height: ph };
    const others = [...cards, ...furniture].reduce((sum, b) => sum + overlap(box, b), 0);
    return others + overlap(box, node) * 50;
  };
  const near = besideSpots(node, pw, ph).map(inside);
  const far: Spot[] = [];
  for (let dy = -480; dy <= 480; dy += 24) {
    for (let dx = -480; dx <= 480; dx += 24) far.push(inside({ x: node.x + dx, y: node.y + dy }));
  }
  let best = near[0];
  let bestCost = Infinity;
  const consider = (s: Spot, rank: number): void => {
    const box = { x: s.x, y: s.y, width: pw, height: ph };
    const cost = covered(s) * 1000 + gap(box, node) + rank;
    if (cost < bestCost) {
      best = s;
      bestCost = cost;
    }
  };
  near.forEach((s, i) => consider(s, i));
  if (bestCost >= 1000) for (const s of far) consider(s, near.length);
  const box = { x: best.x, y: best.y, width: pw, height: ph };
  return {
    x: best.x,
    y: best.y,
    clear: bestCost < 1000,
    leader: gap(box, node) > POPOVER_GAP + 2 ? leaderOf(node, box) : null
  };
}

/**
 * The smallest pan that makes room for the popover beside its node, or null
 * when no pan does.
 *
 * Panning moves the node and every card together, so whether the popover
 * covers a card beside the node is the same before and after; what a pan
 * changes is the canvas rim and the fixed furniture. So for each spot beside
 * the node that covers no card, this is the shift that brings that spot (and
 * the node) inside the canvas clear of the furniture — the smallest one wins.
 * It is how a node near the rim gets its popover on the free side instead of
 * over its neighbours.
 */
export function panForPopover(
  node: Box,
  size: { width: number; height: number },
  cards: readonly Box[],
  furniture: readonly Box[],
  w: number,
  h: number
): { dx: number; dy: number } | null {
  const { width: pw, height: ph } = size;
  let best: { dx: number; dy: number } | null = null;
  for (const s of besideSpots(node, pw, ph)) {
    const box = { x: s.x, y: s.y, width: pw, height: ph };
    if (cards.some((c) => overlap(box, c) > 0)) continue;
    // The union of the popover and its node has to fit in the canvas.
    const x0 = Math.min(box.x, node.x);
    const y0 = Math.min(box.y, node.y);
    const x1 = Math.max(box.x + pw, node.x + node.width);
    const y1 = Math.max(box.y + ph, node.y + node.height);
    if (x1 - x0 > w - 2 * POPOVER_RIM || y1 - y0 > h - 2 * POPOVER_RIM) continue;
    const dx = x0 < POPOVER_RIM ? POPOVER_RIM - x0 : x1 > w - POPOVER_RIM ? w - POPOVER_RIM - x1 : 0;
    const dy = y0 < POPOVER_RIM ? POPOVER_RIM - y0 : y1 > h - POPOVER_RIM ? h - POPOVER_RIM - y1 : 0;
    // The node itself must stay clear of the furniture too, or the pan
    // tucks the card under the controls to make room for its popover.
    const nodeMoved = { ...node, x: node.x + dx, y: node.y + dy };
    if (furniture.some((f) => overlap(nodeMoved, f) > 0)) continue;
    const moved = { x: box.x + dx, y: box.y + dy, width: pw, height: ph };
    if (furniture.some((f) => overlap(moved, f) > 0)) continue;
    if (best === null || Math.hypot(dx, dy) < Math.hypot(best.dx, best.dy)) best = { dx, dy };
  }
  return best;
}

/** The shortest line from one box's edge to the other's. */
function leaderOf(a: Box, b: Box): { x1: number; y1: number; x2: number; y2: number } {
  const clampTo = (v: number, lo: number, hi: number): number => Math.min(Math.max(v, lo), hi);
  const bx = clampTo(a.x + a.width / 2, b.x, b.x + b.width);
  const by = clampTo(a.y + a.height / 2, b.y, b.y + b.height);
  const ax = clampTo(bx, a.x, a.x + a.width);
  const ay = clampTo(by, a.y, a.y + a.height);
  return { x1: Math.round(ax), y1: Math.round(ay), x2: Math.round(bx), y2: Math.round(by) };
}
