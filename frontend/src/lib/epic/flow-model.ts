// The Svelte Flow model for the epic DAG (#2249 item 7).
//
// Everything the canvas draws, as plain data: one node per visible node (or
// one per collapsed done chain), one edge per visible dependency with its
// bend points and its stroke, and the hover lineage sets. No component, no
// `@xyflow/svelte` import — the wrapper in item 8 maps these straight onto
// Svelte Flow's own `Node`/`Edge` shapes, which is what keeps this module
// testable in Node under vitest.
//
// Geometry comes from `dagre-layout.ts` and is laid out over the WHOLE graph,
// done nodes included, so neither the "show done" toggle nor expanding a
// collapsed chain moves a node that was already on screen — and neither does
// a status change, which the layout cannot see at all.
//
// Standalone nodes are not this module's business: the caller partitions them
// out with `splitStandalone` + `keepAttentionInGraph` first (metro.ts and
// attention.ts, both unchanged) and passes the graph half here.
import { needsYou, type NodeFacts } from './attention';
import {
  backRoute,
  dagreLayout,
  laneRoute,
  MARGIN,
  NODE_H,
  NODE_SEP,
  NODE_W,
  roundedPath,
  type DagLayer,
  type DagLayout,
  type DagPos
} from './dagre-layout';
import { edgeStyle, type EdgeStyle } from './metro';

/**
 * Smallest done chain worth collapsing.
 *
 * A one-node "1 done" summary is strictly worse than the node it hides — same
 * box, same slot, less information — so a done chain of one stays in the
 * graph. The brief's example is "6 done".
 */
export const MIN_COLLAPSE = 2;

/** The id of the group node a collapsed chain becomes, keyed on the chain's
 * TAIL — its first member in reading order. Prefixed so it can never collide
 * with a node id (GitHub numbers, Linear refs).
 *
 * The tail, not the head the card stands on: a chain grows at its head, when
 * the next node downstream finishes and joins it, so an id taken from the
 * head changed under a group the operator had opened and closed it again
 * (#2782). The tail is the history furthest back, which a finishing node
 * does not move. */
export const groupId = (tail: string): string => `done:${tail}`;

/** A collapsed, fully done upstream chain. */
export type DoneGroup = {
  readonly id: string;
  /** Members in reading order. */
  readonly ids: readonly string[];
  readonly count: number;
  /** What the group node says: "6 done". */
  readonly summary: string;
};

export type FlowNodeKind = 'node' | 'group';

export type FlowNode<T> = {
  readonly id: string;
  readonly kind: FlowNodeKind;
  /** The LAYOUT's layer, not a renumbering of the visible ones: a collapse
   * must not relabel the layer a node is in. */
  readonly layer: number;
  /** Slot within the layer among the nodes that are VISIBLE, top to bottom. */
  readonly row: number;
  /** Top-left of the card, for Svelte Flow's `position`. */
  readonly position: DagPos;
  readonly width: number;
  readonly height: number;
  /** The epic node, or null on a group node. */
  readonly node: T | null;
  /** The collapsed chain, or null on an ordinary node. */
  readonly group: DoneGroup | null;
};

export type FlowEdge = {
  /** `${source}>${target}`, which is the layout's edge key for an edge no
   * collapse rewrote. */
  readonly id: string;
  readonly source: string;
  readonly target: string;
  /** Bend points, the source card's out port first and the target card's in
   * port last. */
  readonly points: readonly DagPos[];
  readonly d: string;
  readonly style: EdgeStyle;
};

export type FlowModel<T> = {
  /** The layout's topology hash: the same graph, laid out once. */
  readonly hash: string;
  /** The canvas the VISIBLE content needs, boxes and bends alike — what
   * decides whether the minimap is worth showing. Never bigger than the
   * layout's own canvas, which covers the whole graph. */
  readonly width: number;
  readonly height: number;
  /** The layout's layers, less any that collapsed away entirely. */
  readonly layers: readonly DagLayer[];
  /** Reading order: layer, then row. */
  readonly nodes: readonly FlowNode<T>[];
  /** Paint order: by stroke (dashed, then quiet, then live), then down the
   * graph — `paintOrder`'s rule, over the visible edges. */
  readonly edges: readonly FlowEdge[];
  /** Every collapsed chain, in reading order of its group node. */
  readonly groups: readonly DoneGroup[];
};

export type FlowOptions = {
  /** `false` (the default) collapses fully done upstream chains. */
  readonly showDone?: boolean;
  /** Group ids the operator has clicked open. */
  readonly expanded?: readonly string[];
};

/** The lineage of one node: itself, everything transitively upstream and
 * downstream, and every edge on the way. */
export type Lineage = {
  readonly nodes: ReadonlySet<string>;
  readonly edges: ReadonlySet<string>;
};

const byCodeUnit = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/**
 * The nodes a "show done" collapse may swallow.
 *
 * A node qualifies when it is done, nobody is waiting on it, and every parent
 * qualifies too — that is what makes the chain "fully done upstream" rather
 * than a done node with unfinished history behind it. The parent condition is
 * reached by fixpoint rather than a topological walk so a cyclic graph (which
 * the layout tolerates) terminates here as well.
 *
 * A done node with an attention reason is never collapsed, for
 * `keepAttentionInGraph`'s reason: the header counts it, so the surface that
 * is supposed to show it cannot hide it inside a summary box. Neither is the
 * direct parent of open or needs-you work (below).
 */
function collapsible<T extends NodeFacts>(
  nodes: readonly T[],
  parents: ReadonlyMap<string, readonly string[]>
): Set<string> {
  const open = fullyDone(
    new Set(nodes.filter((n) => n.status === 'done' && !needsYou(n)).map((n) => n.id)),
    parents
  );
  // The DIRECT finished parents of open and needs-you work stay drawn (#2560):
  // they are the step the work in front of the operator hangs off, so a
  // collapse that took them would hide the very edge being waited on. What
  // folds is the history further up, behind them. Done again after the cut so
  // a group's parents are still all in it — no edge ever runs INTO a group.
  const waiting = nodes.filter((n) => n.status !== 'done' || needsYou(n)).map((n) => n.id);
  for (const id of waiting) for (const p of parents.get(id) ?? []) open.delete(p);
  return fullyDone(open, parents);
}

/** `open` less every node with a parent outside it, to a fixpoint. */
function fullyDone(
  open: Set<string>,
  parents: ReadonlyMap<string, readonly string[]>
): Set<string> {
  for (;;) {
    const drop = [...open].filter((id) =>
      (parents.get(id) ?? []).some((p) => !open.has(p))
    );
    if (!drop.length) return open;
    for (const id of drop) open.delete(id);
  }
}

/** Weakly connected components of the collapsible subgraph, each in the
 * layout's reading order. Two members that feed the same node are joined
 * even when that node stays drawn: the history behind one parent is one
 * "N done" card (#2560), not a card per root. */
function chainsOf(
  open: ReadonlySet<string>,
  order: readonly string[],
  kids: ReadonlyMap<string, readonly string[]>,
  parents: ReadonlyMap<string, readonly string[]>
): string[][] {
  const seen = new Set<string>();
  const out: string[][] = [];
  for (const start of order) {
    if (!open.has(start) || seen.has(start)) continue;
    const chain: string[] = [];
    const queue = [start];
    seen.add(start);
    while (queue.length) {
      const id = queue.shift()!;
      chain.push(id);
      const out = kids.get(id) ?? [];
      const coParents = out.flatMap((k) => parents.get(k) ?? []);
      for (const next of [...out, ...(parents.get(id) ?? []), ...coParents]) {
        if (open.has(next) && !seen.has(next)) {
          seen.add(next);
          queue.push(next);
        }
      }
    }
    out.push(chain.sort((a, b) => order.indexOf(a) - order.indexOf(b)));
  }
  return out;
}

type Adjacency = {
  kids: Map<string, string[]>;
  parents: Map<string, string[]>;
};

function adjacencyOf(layout: DagLayout): Adjacency {
  const kids = new Map<string, string[]>();
  const parents = new Map<string, string[]>();
  for (const n of layout.nodes) {
    kids.set(n.id, []);
    parents.set(n.id, []);
  }
  for (const e of layout.edges) {
    kids.get(e.src)!.push(e.dst);
    parents.get(e.dst)!.push(e.src);
  }
  return { kids, parents };
}

/**
 * The group node a chain becomes, placed on its HEAD member — the furthest
 * right, topmost member, which is the one the downstream work actually hangs
 * off.
 *
 * Reusing a member's own slot is what keeps a group from landing on top of a
 * node that stayed: every other member of the chain leaves the canvas, so the
 * slot the group takes is one that has just been vacated.
 */
function groupOf(
  chain: readonly string[],
  layout: DagLayout
): { group: DoneGroup; head: string } {
  const head = [...chain].sort((a, b) => {
    const x = layout.positions[b].x - layout.positions[a].x;
    return x || layout.positions[a].y - layout.positions[b].y || byCodeUnit(a, b);
  })[0];
  // `chain` is in reading order, so its first member is the tail.
  const group = {
    id: groupId(chain[0]),
    ids: [...chain],
    count: chain.length,
    summary: `${chain.length} done`
  };
  return { group, head };
}

/**
 * The Flow model for one snapshot.
 *
 * `nodes` is the graph half of the caller's split, in model order — which the
 * layout honours inside a layer, so it is the order the operator's own list
 * is in. `edges` is the snapshot's edge list; the layout drops self-loops and
 * edges with an unknown endpoint, exactly as `splitStandalone` does.
 */
export function buildFlowModel<T extends NodeFacts>(
  nodes: readonly T[],
  edges: readonly [string, string][],
  options: FlowOptions = {}
): FlowModel<T> {
  const layout = dagreLayout(nodes, edges);
  const { kids, parents } = adjacencyOf(layout);
  const order = layout.nodes.map((n) => n.id);
  const expanded = new Set(options.expanded ?? []);

  const chains = options.showDone
    ? []
    : chainsOf(collapsible(nodes, parents), order, kids, parents)
        .filter((c) => c.length >= MIN_COLLAPSE)
        .map((c) => groupOf(c, layout))
        .filter(({ group }) => !expanded.has(group.id));

  // Member -> the group node that stands in for it. Everything else is drawn
  // as itself, including the members of an expanded group.
  const standsFor = new Map<string, string>();
  for (const { group: g } of chains) for (const id of g.ids) standsFor.set(id, g.id);

  // What each DRAWN id stands on in the layout: itself, or — for a group —
  // the member whose slot it took. That is what tells `flowEdges` whether
  // dagre's route for an edge still joins the two points it was routed
  // between, which `standsFor` alone cannot say.
  const standsOn = new Map<string, string>();
  const facts = new Map(nodes.map((n) => [n.id, n] as const));
  const placed: FlowNode<T>[] = [];
  for (const n of layout.nodes) {
    if (standsFor.has(n.id)) continue;
    standsOn.set(n.id, n.id);
    placed.push(nodeOf(n.id, facts.get(n.id) ?? null, null, n.layer, layout));
  }
  for (const { group: g, head } of chains) {
    const at = layout.nodes.find((n) => n.id === head)!;
    standsOn.set(g.id, head);
    placed.push(nodeOf<T>(g.id, null, g, at.layer, layout, layout.positions[head]));
  }

  const flowNodes = withRows(placed);
  const statuses: Record<string, string> = {};
  for (const n of nodes) statuses[n.id] = n.status;
  for (const { group: g } of chains) statuses[g.id] = 'done';
  const flowEdgeList = flowEdges(layout, standsFor, standsOn, statuses);

  return {
    hash: layout.hash,
    ...bounds(flowNodes, flowEdgeList),
    layers: layout.layers.filter((c) => flowNodes.some((n) => n.layer === c.index)),
    nodes: flowNodes,
    edges: flowEdgeList,
    groups: flowNodes.filter((n) => n.group).map((n) => n.group!)
  };
}

function nodeOf<T>(
  id: string,
  node: T | null,
  group: DoneGroup | null,
  layer: number,
  layout: DagLayout,
  at: DagPos = layout.positions[id]
): FlowNode<T> {
  return {
    id,
    kind: group ? 'group' : 'node',
    layer,
    row: 0,
    position: at,
    width: NODE_W,
    height: NODE_H,
    node,
    group
  };
}

/** Reading order (layer, then row), with the rows renumbered over the nodes
 * that are actually visible. */
function withRows<T>(placed: readonly FlowNode<T>[]): FlowNode<T>[] {
  const sorted = [...placed].sort(
    (a, b) => a.layer - b.layer || a.position.y - b.position.y || byCodeUnit(a.id, b.id)
  );
  let layer = -1;
  let row = 0;
  return sorted.map((n) => {
    row = n.layer === layer ? row + 1 : 0;
    layer = n.layer;
    return { ...n, row };
  });
}

/**
 * The canvas the visible content needs, with the layout's own margins kept on
 * the far side so nothing sits against an edge.
 *
 * Over the bends as well as the node boxes: an edge can reach further than
 * either of the boxes it joins — down a channel past the bottom row, say —
 * and a canvas that cropped it would clip the picture on `fitView`. This is
 * `dagre-layout`'s own containment guarantee, carried over the visible half
 * of the graph.
 *
 * Only the far side is measured because the near side is already settled
 * upstream: `dagreLayout` slides a picture whose routing overhangs the top or
 * the left onto its own margins, so every card and every bend it hands over is
 * non-negative — and the visible half is a SUBSET of those, re-pointed between
 * cards that are themselves in it. There is nothing left here to push back.
 */
function bounds<T>(
  nodes: readonly FlowNode<T>[],
  edges: readonly FlowEdge[]
): { width: number; height: number } {
  if (!nodes.length) return { width: 0, height: 0 };
  const xs = nodes.map((n) => n.position.x + n.width);
  const ys = nodes.map((n) => n.position.y + n.height);
  for (const e of edges) {
    for (const p of e.points) {
      xs.push(p.x);
      ys.push(p.y);
    }
  }
  return {
    width: Math.max(...xs) + MARGIN,
    height: Math.max(...ys) + MARGIN
  };
}

/**
 * The visible edges, in paint order.
 *
 * An edge out of a collapsed member is rewritten to leave the group node;
 * an edge INTO one cannot exist, because a collapsible node's parents are all
 * collapsible and so all collapsed with it. Two members feeding the same
 * target collapse to one edge, and the route is dagre's own whenever the
 * group is standing on the member that edge actually left.
 *
 * "Standing on" is the test, not "was not rewritten": a group node takes its
 * HEAD member's slot, so an edge off the head joins the very two cards dagre
 * laid it out between even though the id at the near end changed. An edge
 * from some other member is re-routed: out of the group card and onto the
 * member's own lanes from the group's layer on, which keeps the "no edge
 * crosses a card" guarantee. The dedup below still prefers the head's own
 * edge when both reach the same target.
 */
function flowEdges(
  layout: DagLayout,
  standsFor: ReadonlyMap<string, string>,
  standsOn: ReadonlyMap<string, string>,
  statuses: Readonly<Record<string, string>>
): FlowEdge[] {
  // Where each drawn id's card is: the position of the layout node it stands
  // on, which for a group is its head member's.
  const cards = new Map([...standsOn].map(([id, on]) => [id, layout.positions[on]] as const));
  const layerOf = new Map(layout.nodes.map((n) => [n.id, n.layer] as const));
  const layerX = layout.layers.map((l) => l.x);
  const floor = Math.max(...layout.nodes.map((n) => n.y + NODE_H)) + NODE_SEP;
  const out = new Map<string, FlowEdge>();
  for (const e of layout.edges) {
    const source = standsFor.get(e.src) ?? e.src;
    const target = standsFor.get(e.dst) ?? e.dst;
    if (source === target) continue;
    const id = `${source}>${target}`;
    const from = cards.get(source);
    const to = cards.get(target);
    if (!from || !to) continue;
    const kept = standsOn.get(source) === e.src && standsOn.get(target) === e.dst;
    const points = kept
      ? e.points
      : standIn(e.lanes, layerOf.get(e.src)!, layerOf.get(standsOn.get(source)!)!, from, to, layerX, floor);
    const edge: FlowEdge = {
      id,
      source,
      target,
      points,
      d: kept ? e.d : roundedPath(points),
      style: edgeStyle(source, target, statuses)
    };
    // A rewritten edge from the group's own head keeps the layout's route, so
    // it wins over a re-routed stand-in for the same pair.
    if (!out.has(id) || kept) out.set(id, edge);
  }
  return [...out.values()].sort(
    (a, b) =>
      a.style.order - b.style.order ||
      cards.get(a.target)!.y - cards.get(b.target)!.y ||
      cards.get(a.source)!.y - cards.get(b.source)!.y ||
      byCodeUnit(a.id, b.id)
  );
}

/**
 * The route of an edge whose source collapsed into a group card standing on
 * ANOTHER member's slot: out of the group card, then along the member's own
 * lanes from the group's layer on. Only a target at or left of the group's
 * layer (a member and its target sharing the head's layer) has no lane to
 * follow, and that one goes round the cards as a back edge does.
 */
function standIn(
  lanes: readonly number[] | null,
  memberLayer: number,
  groupLayer: number,
  group: DagPos,
  target: DagPos,
  layerX: readonly number[],
  floor: number
): DagPos[] {
  const skip = groupLayer - memberLayer;
  if (lanes === null || skip < 0 || skip >= lanes.length - 1) {
    return backRoute(group, target, floor);
  }
  return laneRoute(layerX, groupLayer, [group.y + NODE_H / 2, ...lanes.slice(skip + 1)]);
}

/**
 * The hover lineage: this node, everything transitively upstream and
 * downstream of it, and every edge on the way.
 *
 * Dagster's reading — the cone, not the neighbourhood: a sibling edge into a
 * downstream node is NOT lineage, because the work it carries is not this
 * node's. The caller brightens what is in the sets and dims the rest.
 *
 * `null` — not an empty Lineage — for an id this model does not draw: a node
 * the pointer was over and the next frame moved to the standalone strip or
 * collapsed into a done group leaves a `hovered` id with nothing on the canvas
 * to light, and the caller's rule is "dim everything outside the sets". Empty
 * sets under that rule dim the whole graph; `null` is the honest "there is no
 * lineage to light", which the caller reads as no hover at all.
 */
export function lineage<T>(model: FlowModel<T>, id: string): Lineage | null {
  if (!model.nodes.some((n) => n.id === id)) return null;
  const nodes = new Set<string>();
  const edges = new Set<string>();
  nodes.add(id);
  for (const dir of ['down', 'up'] as const) {
    const queue = [id];
    const seen = new Set([id]);
    while (queue.length) {
      const at = queue.shift()!;
      for (const e of model.edges) {
        const [near, far] = dir === 'down' ? [e.source, e.target] : [e.target, e.source];
        if (near !== at) continue;
        edges.add(e.id);
        nodes.add(far);
        if (!seen.has(far)) {
          seen.add(far);
          queue.push(far);
        }
      }
    }
  }
  return { nodes, edges };
}
