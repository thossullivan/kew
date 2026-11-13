// Layered layout for the epic DAG, by `@dagrejs/dagre` (#2249, Tom's
// decision 2 of 2026-10-02: dagre, not ELK).
//
// The geometry is the UXEpic artboard's (frontend/design/factory-ui/UXEpic.html):
// every node is a 196x92 CARD, dagre lays the cards out left to right with
// nodesep 16 and ranksep 40, and every edge runs from the middle of its source
// card's right edge to the middle of its target card's left edge.
//
// The contract, proved in `dagre-layout.test.ts`:
//  1. INPUTS: a topology and nothing else. `dagreLayout` takes `{ id }` nodes,
//     so status is not an input — it is a type error, not a promise. The cache
//     is keyed on `topologyHash`, so status churn between ticks can never move
//     a node.
//  2. ORDER: the caller's MODEL ORDER of nodes is a deliberate input (Tom's
//     decision 2: "model order inside a layer where dagre allows it"). It is
//     therefore hashed — see `topologyHash`.
//  3. DIMENSIONS: every card is the same fixed size and its title is clamped
//     to two lines, so text is never a layout input either.
//  4. NO EDGE CROSSES A CARD. dagre's own edge points are diagonals whose
//     ends land on the card boundary wherever the diagonal meets it; drawn
//     as-is they ran straight through titles. Only dagre's LANES are kept: the
//     y at which a long edge crosses each layer it skips (a dummy node, which
//     dagre keeps clear of every card in that layer). The route itself is
//     orthogonal — horizontal along a lane inside a layer, vertical only in the
//     empty channel between two layers — and neither a lane nor a channel
//     holds a card.
import { Graph, layout, type EdgeLabel, type GraphLabel, type NodeLabel } from '@dagrejs/dagre';

/** The artboard's card. */
export const NODE_W = 196;
export const NODE_H = 92;
/** Clear air dagre keeps between two cards in one layer, and between a card
 * and an edge lane running past it. */
export const NODE_SEP = 16;
/** Clear air between two layers: the channel every vertical edge run is in. */
export const RANK_SEP = 40;
/** Space kept round the whole picture, on every side. More than half a
 * channel, so an edge drawn against the flow (`backRoute`) can turn in the
 * channel before the first layer and still be on the canvas. */
export const MARGIN = 24;
/** Left edge to left edge, one layer to the next. */
export const PITCH = NODE_W + RANK_SEP;
/** Quadratic corner radius on a bend (the artboard's 9). */
export const CORNER_R = 9;

// Every field is `readonly`: `dagreLayout` hands the same cached object to
// every caller, so the compiler refuses the in-place edit `freezeLayout`
// would otherwise only catch at runtime.
export type DagPos = { readonly x: number; readonly y: number };
export type DagNode = {
  readonly id: string;
  /** 0-based layer, left to right. */
  readonly layer: number;
  /** The node's slot WITHIN its layer, top to bottom. */
  readonly row: number;
  /** The card's top-left corner. */
  readonly x: number;
  readonly y: number;
};
export type DagEdge = {
  readonly key: string;
  readonly src: string;
  readonly dst: string;
  /** For a forward edge, the y it runs at in each layer from its source's to
   * its target's, both ends included (the two card middles). `null` for an
   * edge drawn against the flow, which only a cycle produces. */
  readonly lanes: readonly number[] | null;
  /** Bend points: the source card's out port first, the target card's in
   * port last. */
  readonly points: readonly DagPos[];
  /** `points` as an SVG path, with `CORNER_R` rounding at every bend. */
  readonly d: string;
};
/** A layer, and the x of its cards' left edges. */
export type DagLayer = { readonly index: number; readonly x: number };
export type DagLayout = {
  /** `topologyHash` of the graph this was laid out from. */
  readonly hash: string;
  /** Canvas the content needs, cards, edges and margins included. 0 for an
   * empty graph. */
  readonly width: number;
  readonly height: number;
  readonly layers: readonly DagLayer[];
  /** Reading order: by layer, then by row — the brief's Tab order. */
  readonly nodes: readonly DagNode[];
  /** Each card's top-left corner. */
  readonly positions: Readonly<Record<string, DagPos>>;
  /** Canonical (key-sorted) order. */
  readonly edges: readonly DagEdge[];
};

/** Where an edge leaves a card: the middle of its right edge. */
export const outPort = (at: DagPos): DagPos => ({ x: at.x + NODE_W, y: at.y + NODE_H / 2 });
/** Where an edge enters a card: the middle of its left edge. */
export const inPort = (at: DagPos): DagPos => ({ x: at.x, y: at.y + NODE_H / 2 });

/** Ordinal (code-unit) comparison, not localeCompare — locale-sensitive
 * collation orders ids differently across environments, which would make the
 * layout environment-dependent. */
const byCodeUnit = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/** FNV-1a. */
function fnv1a(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

type CanonEdge = { key: string; src: string; dst: string };

/** Model order, deduped. NOT sorted: see `topologyHash`. */
function modelIds(nodes: readonly { id: string }[]): string[] {
  return [...new Set(nodes.map((n) => n.id))];
}

/** Edges the layout will keep, key-sorted: self-loops and edges with an
 * endpoint that is not in the graph are dropped, exactly as
 * `splitStandalone` drops them when it decides a node has no links. */
function canonicalEdges(
  ids: ReadonlySet<string>,
  edges: readonly [string, string][]
): CanonEdge[] {
  const seen = new Set<string>();
  const out: CanonEdge[] = [];
  for (const [src, dst] of edges) {
    if (src === dst || !ids.has(src) || !ids.has(dst)) continue;
    const key = `${src}>${dst}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ key, src, dst });
  }
  return out.sort((a, b) => byCodeUnit(a.key, b.key));
}

/**
 * Hash of the topology — never status, never title.
 *
 * Node order IS geometry here: dagre breaks ordering ties inside a layer by
 * the order nodes were added (decision 2's "model order inside a layer"), so
 * two snapshots with the same nodes in a different order are two different
 * layouts and must hash differently — or the cache would hand back the first
 * one's coordinates for the second.
 *
 * Edges stay canonically sorted: they are fed to dagre in that order, so
 * their array order genuinely cannot reach the geometry.
 */
export function topologyHash(
  nodes: readonly { id: string }[],
  edges: readonly [string, string][]
): string {
  const ids = modelIds(nodes);
  return hashOf(ids, canonicalEdges(new Set(ids), edges));
}

/** The hash, over a graph that is already canonical. One function so the
 * cache key and the hash `topologyHash` publishes cannot drift apart. */
function hashOf(ids: readonly string[], canon: readonly CanonEdge[]): string {
  return fnv1a(`${ids.join('|')}#${canon.map((e) => e.key).join('|')}`);
}

const legLen = (a: DagPos, b: DagPos): number => Math.hypot(b.x - a.x, b.y - a.y);

/** The point `r` along the segment from `at` towards `to`. */
function toward(at: DagPos, to: DagPos, r: number): string {
  const len = legLen(at, to);
  if (len === 0 || r === 0) return `${at.x} ${at.y}`;
  const t = r / len;
  const round = (v: number): number => Math.round(v * 100) / 100;
  return `${round(at.x + (to.x - at.x) * t)} ${round(at.y + (to.y - at.y) * t)}`;
}

/**
 * A polyline as an SVG path, with the artboard's rounded bends.
 *
 * Each interior point becomes a quadratic whose control point is the bend
 * itself, so the curve stays inside the corner. The radius is capped at half
 * of any shared leg — and at the WHOLE of a first or last leg, which only one
 * corner touches — so two corners can never eat past each other however
 * cramped the route, and `points[0]` and the last point come out EXACTLY.
 * That is what makes "every drawn edge joins its two ports" checkable from
 * the path string alone.
 */
export function roundedPath(points: readonly DagPos[]): string {
  const last = points.length - 1;
  const room = (i: number, j: number): number => {
    const len = legLen(points[i], points[j]);
    return i === 0 || j === last ? len : len / 2;
  };
  let d = `M${points[0].x} ${points[0].y}`;
  for (let i = 1; i < last; i++) {
    const at = points[i];
    const r = Math.min(CORNER_R, room(i - 1, i), room(i, i + 1));
    d += ` L${toward(at, points[i - 1], r)} Q${at.x} ${at.y} ${toward(at, points[i + 1], r)}`;
  }
  return `${d} L${points[last].x} ${points[last].y}`;
}

/** Drop a point that repeats the one before it, and a point in the middle of
 * a straight run, so no bend has a zero-length leg to round against and a
 * straight run is never drawn as a bend. */
function simplify(points: readonly DagPos[]): DagPos[] {
  const out: DagPos[] = [];
  for (const p of points) {
    const prev = out[out.length - 1];
    if (prev && prev.x === p.x && prev.y === p.y) continue;
    const before = out[out.length - 2];
    const straight =
      prev !== undefined &&
      before !== undefined &&
      ((before.x === prev.x && prev.x === p.x) || (before.y === prev.y && prev.y === p.y));
    if (straight) out[out.length - 1] = p;
    else out.push(p);
  }
  return out;
}

/**
 * The orthogonal route of a forward edge, from its lanes.
 *
 * `layerX` is every layer's left edge; `from` is the source's layer, and
 * `lanes[i]` the y the edge runs at in layer `from + i` — the first is the
 * source card's middle, the last the target card's. The edge leaves the source
 * card's right edge, runs to the middle of the next channel, turns there onto
 * the next layer's lane, and so on until it enters the target's left edge:
 * horizontal inside a layer, vertical only between two.
 *
 * Exported because a collapsed done chain re-uses it: an edge out of a chain
 * member that is not standing in the group's own slot is re-routed from the
 * group card along the member's own lanes (`flow-model.ts`).
 */
export function laneRoute(
  layerX: readonly number[],
  from: number,
  lanes: readonly number[]
): DagPos[] {
  const points: DagPos[] = [{ x: layerX[from] + NODE_W, y: lanes[0] }];
  for (let i = 1; i < lanes.length; i++) {
    const left = layerX[from + i];
    const channel = left - RANK_SEP / 2;
    points.push(
      { x: channel, y: lanes[i - 1] },
      { x: channel, y: lanes[i] },
      { x: left, y: lanes[i] }
    );
    if (i < lanes.length - 1) points.push({ x: left + NODE_W, y: lanes[i] });
  }
  return simplify(points);
}

/**
 * The route of an edge drawn against the flow (a cycle: dagre reverses one
 * edge of it to rank the graph). It cannot run left through the layers
 * without crossing cards, so it goes round them: out of the source's right
 * edge, down its channel to below the lowest card, back along the bottom, and
 * up the channel before its target.
 */
export function backRoute(src: DagPos, dst: DagPos, floor: number): DagPos[] {
  const out = outPort(src);
  const into = inPort(dst);
  const right = out.x + RANK_SEP / 2;
  const left = into.x - RANK_SEP / 2;
  return simplify([
    out,
    { x: right, y: out.y },
    { x: right, y: floor },
    { x: left, y: floor },
    { x: left, y: into.y },
    into
  ]);
}

// One layout per topology. Topology changes are rare relative to the status
// and telemetry churn this cache exists to absorb; nothing else is a layout
// input, so there is no second cache key.
const cache = new Map<string, DagLayout>();

/** test-only: drops the cache so a test observes a genuine re-layout instead
 * of a cache hit. */
export function resetDagreCache(): void {
  cache.clear();
}

const EMPTY: DagLayout = Object.freeze({
  hash: hashOf([], []),
  width: 0,
  height: 0,
  layers: Object.freeze([]),
  nodes: Object.freeze([]),
  positions: Object.freeze({}),
  edges: Object.freeze([])
});

/**
 * Deterministic layered layout. `nodes` is `{ id }` only — status is not an
 * input, by construction — and its ORDER is honoured inside a layer as far as
 * dagre's crossing reduction allows.
 */
export function dagreLayout(
  nodes: readonly { id: string }[],
  edges: readonly [string, string][]
): DagLayout {
  const ids = modelIds(nodes);
  const canon = canonicalEdges(new Set(ids), edges);
  const hash = hashOf(ids, canon);
  const hit = cache.get(hash);
  if (hit) return hit;
  const out = ids.length ? freezeLayout(buildLayout(hash, ids, canon)) : EMPTY;
  cache.set(hash, out);
  return out;
}

/** Freeze the layout and everything reachable from it: every caller with this
 * topology is handed the ONE cached object, so a view sorting `nodes` in place
 * or nudging a `positions` entry would silently move the graph for every later
 * consumer. Run once per topology, off the cache-hit path. */
function freezeLayout(l: DagLayout): DagLayout {
  for (const n of l.nodes) Object.freeze(n);
  for (const p of Object.values(l.positions)) Object.freeze(p);
  for (const e of l.edges) {
    for (const p of e.points) Object.freeze(p);
    Object.freeze(e.points);
    if (e.lanes) Object.freeze(e.lanes);
    Object.freeze(e);
  }
  for (const layer of l.layers) Object.freeze(layer);
  Object.freeze(l.nodes);
  Object.freeze(l.positions);
  Object.freeze(l.edges);
  Object.freeze(l.layers);
  return Object.freeze(l);
}

/**
 * dagre's node key for one of our ids.
 *
 * The prefix is not cosmetic. graphlib keeps its nodes in a plain object, and
 * a plain object iterates INTEGER-LIKE keys in numeric order before anything
 * else — so a graph of GitHub issue numbers ("2000", "1969", ...) reached
 * dagre's `initOrder` renumbered, and model order inside a layer was lost
 * before the layout even started. A non-numeric prefix makes every key a
 * string key, which iterates in insertion order, which is model order.
 */
const key = (id: string): string => `n:${id}`;

/** dagre types every coordinate as optional, because a node label can be
 * handed IN without one. After `layout` every node has both; the fallback is
 * there for the compiler, not for a case that happens. */
const coord = (v: number | undefined): number => v ?? 0;

/** A card centre, where dagre left it. */
type Centre = { readonly cx: number; readonly cy: number };

/**
 * One forward edge's lanes, in dagre's own coordinates: the source card's
 * middle, then for every layer the edge skips the y of the dummy node dagre
 * routed it through there (the interior point nearest that layer's centre),
 * then the target card's middle.
 */
function lanesOf(
  points: readonly DagPos[],
  from: number,
  to: number,
  centres: readonly number[],
  src: Centre,
  dst: Centre
): number[] {
  const lanes = [src.cy];
  const interior = points.slice(1, -1);
  for (let layer = from + 1; layer < to; layer++) {
    const cx = centres[layer];
    let best: DagPos | null = null;
    for (const p of interior) {
      if (best === null || Math.abs(p.x - cx) < Math.abs(best.x - cx)) best = p;
    }
    lanes.push(Math.round(best ? best.y : dst.cy));
  }
  lanes.push(dst.cy);
  return lanes;
}

function buildLayout(hash: string, ids: string[], canon: CanonEdge[]): DagLayout {
  const g = runDagre(ids, canon);
  const centre = new Map<string, Centre>(
    ids.map((id) => {
      const box = g.node(key(id));
      return [id, { cx: Math.round(coord(box.x)), cy: Math.round(coord(box.y)) }];
    })
  );
  // Every card is the same width, so one x is exactly one layer — and this
  // holds whether or not a future dagre keeps `rank` on the label it hands
  // back.
  const centres = [...new Set([...centre.values()].map((c) => c.cx))].sort((a, b) => a - b);
  const layerOf = new Map(centres.map((x, i) => [x, i]));
  const rawLanes = canon.map((e) => {
    const s = centre.get(e.src)!;
    const t = centre.get(e.dst)!;
    const a = layerOf.get(s.cx)!;
    const b = layerOf.get(t.cx)!;
    if (b <= a) return null;
    const points = (g.edge(key(e.src), key(e.dst)).points ?? []) as readonly DagPos[];
    return lanesOf(points, a, b, centres, s, t);
  });

  // Slide the picture so the nearest card or lane sits on the margin. dagre
  // measures its own box over cards only, and a lane can run above the top
  // card of every layer it crosses.
  const tops = [...centre.values()].map((c) => c.cy - NODE_H / 2);
  for (const lanes of rawLanes) if (lanes) tops.push(...lanes);
  const shiftX = MARGIN - (centres[0] - NODE_W / 2);
  const shiftY = MARGIN - Math.min(...tops);

  const layerX = centres.map((cx) => cx - NODE_W / 2 + shiftX);
  const placed: DagNode[] = ids
    .map((id) => {
      const c = centre.get(id)!;
      return {
        id,
        layer: layerOf.get(c.cx)!,
        row: 0,
        x: c.cx - NODE_W / 2 + shiftX,
        y: c.cy - NODE_H / 2 + shiftY
      };
    })
    .sort((a, b) => a.layer - b.layer || a.y - b.y || byCodeUnit(a.id, b.id));
  let layer = -1;
  let row = 0;
  const nodes = placed.map((n) => {
    row = n.layer === layer ? row + 1 : 0;
    layer = n.layer;
    return { ...n, row };
  });

  const positions: Record<string, DagPos> = {};
  const layerOfId = new Map<string, number>();
  for (const n of nodes) {
    positions[n.id] = { x: n.x, y: n.y };
    layerOfId.set(n.id, n.layer);
  }
  const floor = Math.max(...nodes.map((n) => n.y + NODE_H)) + NODE_SEP;

  const laidEdges: DagEdge[] = canon.map((e, i) => {
    const raw = rawLanes[i];
    const lanes = raw ? raw.map((y) => y + shiftY) : null;
    const points = lanes
      ? laneRoute(layerX, layerOfId.get(e.src)!, lanes)
      : backRoute(positions[e.src], positions[e.dst], floor);
    return { ...e, lanes, points, d: roundedPath(points) };
  });

  return {
    hash,
    ...canvasOf(nodes, laidEdges),
    layers: layerX.map((x, index) => ({ index, x })),
    nodes,
    positions,
    edges: laidEdges
  };
}

/** The canvas: every card and every bend, with the margin on the far side as
 * well as the near one. */
function canvasOf(
  nodes: readonly DagNode[],
  edges: readonly DagEdge[]
): { width: number; height: number } {
  const xs = nodes.map((n) => n.x + NODE_W);
  const ys = nodes.map((n) => n.y + NODE_H);
  for (const e of edges) {
    for (const p of e.points) {
      xs.push(p.x);
      ys.push(p.y);
    }
  }
  return { width: Math.max(...xs) + MARGIN, height: Math.max(...ys) + MARGIN };
}

/**
 * The dagre call itself, and the only place the library is touched.
 *
 * `rankdir: 'LR'` is the brief's left-to-right flow. The ranker is pinned to
 * `network-simplex`, dagre's default, so a future default cannot silently
 * re-rank every epic. Nodes go in in MODEL order and edges in canonical order,
 * which together are exactly what `topologyHash` covers.
 */
function runDagre(
  ids: readonly string[],
  canon: readonly CanonEdge[]
): Graph<GraphLabel, NodeLabel, EdgeLabel> {
  const g = new Graph<GraphLabel, NodeLabel, EdgeLabel>({
    directed: true,
    multigraph: false,
    compound: false
  });
  g.setGraph({
    rankdir: 'LR',
    ranker: 'network-simplex',
    nodesep: NODE_SEP,
    edgesep: NODE_SEP,
    ranksep: RANK_SEP
  });
  g.setDefaultEdgeLabel(() => ({}));
  for (const id of ids) g.setNode(key(id), { width: NODE_W, height: NODE_H });
  for (const e of canon) g.setEdge(key(e.src), key(e.dst), {});
  layout(g);
  return g;
}
