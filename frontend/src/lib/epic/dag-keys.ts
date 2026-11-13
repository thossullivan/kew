// Keyboard navigation over the Flow model (#2249 item 7, the brief's keyboard
// table). Pure: ids in, an id or null out — no event handling, no focus, no
// Svelte Flow. The canvas in item 8 binds keys to these.
//
//   Tab      next node in reading order (layer, then row)  → `tabStep`
//   ← / →    the upstream / downstream neighbour along an edge → `neighbor`
//            (with nothing selected, the first node in reading order)
//   ↑ / ↓    the next node in the same layer                   → `neighbor`
//   L        fill the lineage selection with `+#N+` (#2544)
//
// With a lineage selection (`lineage-select.ts`) the arrows walk the
// selection alone: the canvas hands `neighbor` the model `withinSelection`
// cuts down, so the rules below are the same inside it.
//
// Every move is over the VISIBLE model: a collapsed done chain is one stop,
// not six, and arrowing into one lands on the group node.
import type { KeyDef } from '$lib/keys/registry';
import type { FlowModel, FlowNode } from './flow-model';

/**
 * The graph's shortcuts (#2512), as the canvas registers them with the
 * console's key layer and as its legend draws them. Esc is not here: it is
 * the epic page's (`EPIC_KEYS` in `timeline.ts`), because its two steps read
 * and write both halves of the focus.
 */
export const DAG_KEYS: readonly KeyDef[] = [
  { key: 'Tab', description: 'nodes', legend: true },
  { key: 'ArrowLeft', description: 'step along an edge', legend: true },
  { key: 'ArrowRight', description: 'step along an edge', legend: true },
  { key: 'ArrowUp', description: 'layer up' },
  { key: 'ArrowDown', description: 'layer down' },
  { key: 'Enter', description: 'popover', legend: true },
  { key: '0', description: 'zoom to node', legend: 'wide' },
  { key: 'l', description: 'lineage' },
  { key: 'L', description: 'lineage', legend: true },
  { key: 'f', description: 'fit all' },
  { key: 'F', description: 'fit all' }
];

export type Direction = 'left' | 'right' | 'up' | 'down';

/** The modifiers both predicates below leave to the browser. */
type Modifiers = {
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly altKey: boolean;
};

/** The two fields of a key event that decide whether the graph answers it. */
export type KeyOrigin = Modifiers & {
  readonly target: unknown;
  readonly currentTarget: unknown;
};

/** A press that landed on nothing: which key it was, and where. */
export type StrayOrigin = Modifiers & {
  readonly key: string;
  readonly target: unknown;
};

/**
 * Whether this key press is the GRAPH's to answer.
 *
 * Two reasons it is not:
 *
 * * a modifier is down — ⌘/ctrl/alt combinations belong to the browser, and
 *   `preventDefault` on them takes away ⌘-Tab and ctrl-arrow;
 * * it did not happen on the artboard itself. The canvas binds this handler to
 *   its outer `.dag` element, and the node popover renders INSIDE the flow, so
 *   its Copy button's Tab bubbles up here. Answered, it was preventDefaulted
 *   and moved the graph selection — so the popover's own links were
 *   unreachable from the keyboard, which makes the box a trap around the one
 *   control the brief put there to be pressed. The artboard is the graph's
 *   only tab stop (its nodes and edges are deliberately not focusable), so
 *   "the event landed on the element the handler is bound to" is exactly "the
 *   operator is driving the graph".
 */
export function artboardKey(e: KeyOrigin): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey) return false;
  return e.target === e.currentTarget;
}

/**
 * Whether this key press is the graph's when it landed on NOTHING — on the
 * body, which is where a `?node=` link leaves the focus. The canvas answers
 * those so the legend's keys work on a linked node before it is clicked.
 *
 * TAB IS NOT ONE OF THEM, even though it is the graph's on the artboard. From
 * the body its job is to move the DOM focus INTO the page: the artboard is a
 * tab stop, so a Tab off a `?node=` landing walks into the graph (`artboardKey`
 * owns Tab from there, where Esc clears the selection and the Tab after it
 * leaves) or on to the popover's own Copy and links. Answered here it was
 * preventDefaulted with the focus still on the body, which has nowhere to go
 * next: every control on the page was unreachable from the keyboard for as
 * long as a node stayed selected — `artboardKey`'s trap, one element further
 * out, and page-wide instead of inside the canvas.
 */
export function strayKey(e: StrayOrigin, body: unknown): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey) return false;
  if (e.key === 'Tab') return false;
  return e.target === body;
}

/**
 * Which stray presses the graph answers with NOTHING selected (#2780): ← and
 * →, which land on the first node in reading order. Before, a fresh load of
 * an epic with nobody waited on (so no opening pick) ignored them. The rest
 * stay the browser's until there is a selection: Enter, 0 and L need one to
 * act on, and ↑ / ↓ scroll the page.
 */
export function strayWithoutSelection(key: string): boolean {
  return key === 'ArrowLeft' || key === 'ArrowRight';
}

const byCodeUnit = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/** Reading order — the Tab order: layer, then row. This is already the order
 * `buildFlowModel` returns its nodes in; named here so the canvas reads from
 * the keyboard module rather than relying on that. */
export function readingOrder<T>(model: FlowModel<T>): string[] {
  return [...model.nodes]
    .sort((a, b) => a.layer - b.layer || a.row - b.row || byCodeUnit(a.id, b.id))
    .map((n) => n.id);
}

/**
 * Tab (`+1`) and shift-Tab (`-1`) through the reading order.
 *
 * With nothing selected, Tab lands on the first node and shift-Tab on the
 * last. By default the order wraps. The canvas's own Tab handler passes
 * `wrap: false`, so a step past either end returns null and the browser moves
 * focus out of the graph: a Tab that always wraps is a keyboard trap (review
 * round 5 of #2250), and DESIGN.md's canvas must let focus leave.
 */
export function tabStep<T>(
  model: FlowModel<T>,
  id: string | null,
  delta: number,
  { wrap = true }: { wrap?: boolean } = {}
): string | null {
  const order = readingOrder(model);
  if (!order.length) return null;
  const at = id == null ? -1 : order.indexOf(id);
  if (at < 0) return delta < 0 ? order[order.length - 1] : order[0];
  const next = at + delta;
  if (!wrap && (next < 0 || next >= order.length)) return null;
  return order[(next % order.length + order.length) % order.length];
}

/** The node next to `id` in `dir`, or null when there is none — a sink to the
 * right, a root to the left, the top or bottom of a layer. */
export function neighbor<T>(
  model: FlowModel<T>,
  id: string,
  dir: Direction
): string | null {
  const from = model.nodes.find((n) => n.id === id);
  if (!from) return null;
  return dir === 'up' || dir === 'down'
    ? inLayer(model, from, dir === 'down' ? 1 : -1)
    : alongEdge(model, from, dir);
}

/** ↑ / ↓: the next slot in the same layer. Does not wrap — the ends of a
 * column are a real edge of the picture, and wrapping there reads as a jump. */
function inLayer<T>(model: FlowModel<T>, from: FlowNode<T>, step: number): string | null {
  const want = from.row + step;
  return model.nodes.find((n) => n.layer === from.layer && n.row === want)?.id ?? null;
}

/**
 * ← / →: along an edge, to the nearest neighbour THE WAY THE KEY POINTS —
 * everything on that side of the cursor's layer first, then the closest layer,
 * then the row closest to the one the cursor is on, then the top row, then
 * the id.
 *
 * "Closest layer" matters because a dependency can skip layers: from a node
 * with one child in the next layer and one four layers along, → goes to the
 * near one, which is the next thing to read.
 *
 * Both halves of that are needed, because a cyclic epic (which the layout
 * tolerates and the model draws) has edges that run backwards. A SIGNED
 * distance alone would rank a child four layers LEFT ahead of the adjacent
 * child on its right, moving the cursor the wrong way down the graph. An
 * unsigned distance alone would call those two children one layer away each
 * and break the tie on the id: on `c → b` plus `c → d`, → walked left onto
 * `b`, the very node ← returns. Side first, then unsigned distance within
 * it, reads as the picture does — → takes the nearest child to the right, and
 * follows a back edge only when there is no child to the right at all.
 */
function alongEdge<T>(
  model: FlowModel<T>,
  from: FlowNode<T>,
  dir: 'left' | 'right'
): string | null {
  const ids = new Set(
    model.edges
      .filter((e) => (dir === 'right' ? e.source : e.target) === from.id)
      .map((e) => (dir === 'right' ? e.target : e.source))
  );
  const options = model.nodes.filter((n) => ids.has(n.id));
  if (!options.length) return null;
  const span = (n: FlowNode<T>): number => Math.abs(n.layer - from.layer);
  // 0 for a node the key actually points at, 1 for one behind the cursor or
  // in its own layer — so a back edge can never outrank a forward one.
  const behind = (n: FlowNode<T>): number =>
    (dir === 'right' ? n.layer > from.layer : n.layer < from.layer) ? 0 : 1;
  return [...options].sort(
    (a, b) =>
      behind(a) - behind(b) ||
      span(a) - span(b) ||
      Math.abs(a.row - from.row) - Math.abs(b.row - from.row) ||
      a.row - b.row ||
      byCodeUnit(a.id, b.id)
  )[0].id;
}

/** Where the canvas's two lineage buttons go from the selection. */
export type LineageStops = {
  /** ← — the upstream neighbour, or null at a root. */
  readonly up: string | null;
  /** → — the downstream neighbour, or null at a sink. */
  readonly down: string | null;
};

/**
 * The ← and → lineage buttons' stops from `id` (#2533): the same move the
 * arrow keys make, so a button and its key can never disagree. Null on a side
 * is "the lineage ends here" and is what disables that button — a root has no
 * ←, a sink no →. Both are null with nothing selected, and for a selection the
 * canvas does not draw (a node in the standalone strip): there is no lineage
 * on the canvas to walk.
 */
export function lineageStops<T>(model: FlowModel<T>, id: string | null): LineageStops {
  if (id === null) return { up: null, down: null };
  return { up: neighbor(model, id, 'left'), down: neighbor(model, id, 'right') };
}
