// What the epic DAG's edges MEAN, and which nodes belong in the graph at all
// (plan §FUI-08, frontend/design/factory-ui).
//
// All that is left of the metro map. #2250 replaced the 650-line hand-rolled
// layout in this module with dagre (`dagre-layout.ts`) behind a Svelte Flow
// canvas, and deleted it: a layered layout with real crossing reduction and a
// minimap is strictly better than a bespoke one, and two layout engines in one
// view is two pictures nobody can keep in agreement. What survives is the half
// that was never geometry —
//
//  * `edgeStyle`: what an edge's stroke says, which is about STATUS and so was
//    always outside the layout (`flow-model.ts` and `DagEdge.svelte` read it);
//  * `splitStandalone`: which nodes go to the strip under the graph rather than
//    into it, the one status-dependent partition the caller makes BEFORE any
//    layout runs.
//
// Golden reference: the DEpic artboard's graph SVG (line 61 of
// frontend/design/factory-ui/DEpic.html). The one documented departure from the
// plan's prose is recorded at `edgeStyle` below.

import { motionOf } from '$lib/factory/motion';

/** Source statuses that make an edge settled history rather than a pending
 * dependency — see `edgeStyle`. */
const SETTLED_STATUS = new Set(['done', 'cancelled']);

export type EdgeKind = 'pending' | 'blocked' | 'quiet' | 'live';
export type EdgeStyle = {
  kind: EdgeKind;
  stroke: string;
  width: number;
  dash: string | null;
  opacity: number | null;
  /** Paint order: dashed first (pending, then blocked), then quiet, then
   * live — so the one edge that is moving is drawn over everything. */
  order: number;
};

// Frozen: these are shared constants handed to every caller, not per-edge
// objects, so a renderer tweaking one in place would restyle the whole graph.
const PENDING: EdgeStyle = Object.freeze({
  kind: 'pending', stroke: 'var(--text-5)', width: 1.5, dash: '4 4', opacity: null, order: 0
} as const);
const BLOCKED: EdgeStyle = Object.freeze({
  kind: 'blocked', stroke: 'var(--status-blocked)', width: 1.5, dash: '2 4', opacity: 0.85, order: 1
} as const);
const QUIET: EdgeStyle = Object.freeze({
  kind: 'quiet', stroke: 'var(--edge-done)', width: 1.5, dash: null, opacity: null, order: 2
} as const);
const LIVE: EdgeStyle = Object.freeze({
  kind: 'live', stroke: 'var(--accent-2)', width: 2, dash: null, opacity: null, order: 3
} as const);

/** Stroke for one edge. An edge means what it feeds, so the target's status
 * decides — except for `quiet`, which also needs a settled source: an edge
 * out of a node that is being redone is still a pending dependency, not
 * history, however far its target got.
 *
 * `live` is the target's MOTION and not a list of status words (#2305). The
 * list it replaces was `['in_progress', 'in_review']`, which was wrong twice
 * over: `in_review` is a published pull request waiting for a PERSON, where
 * nothing is running, and the two statuses where kew's own reviewer IS running
 * — `adversarial_review` and `remediating` — were missing, so a governed
 * review drew as a pending dependency. `motionOf` answers off the one table the
 * backend serves `motion` from, so this edge and the server agree about what
 * is moving (`kew/tests/test_motion_parity.py`).
 *
 * DEPARTURE from the plan's prose and its `edgeStyle(src, dst, statuses,
 * frontier)` signature: there is no `frontier` parameter, and `live` is the
 * target's status alone. The plan reads "live (into an in-progress or
 * in-review frontier node)", but kew's ready frontier is TODO-only by
 * construction — `compute_frontier` (kew/src/kew/graph/driver/frontier.py)
 * admits a node only while its raw provider status is TODO — so a node can
 * never be both moving and in the frontier a snapshot publishes. Requiring
 * both made `live` unreachable: every edge into a moving node silently drew
 * as `pending`, and only a hand-written frontier could produce the artboard's
 * one cyan edge (#2047 → #2055, which the artboard captions "in review").
 * Dropping the conjunct is what draws the artboard from a real snapshot.
 * Frontier membership is left to the caller's node marks, where it can be
 * shown without overloading an edge that is already saying something else.
 *
 * An unknown status falls back to `pending`, the same forward-compatible
 * degradation as render.ts's status colours — never `quiet` and never `live`,
 * so a status kew adds later can't claim to be attested or moving. */
export function edgeStyle(
  src: string,
  dst: string,
  statuses: Readonly<Record<string, string>>
): EdgeStyle {
  const from = statuses[src] ?? 'todo';
  const to = statuses[dst] ?? 'todo';
  if (motionOf(to) === 'executing') return LIVE;
  if (to === 'blocked') return BLOCKED;
  if (to === 'done' && SETTLED_STATUS.has(from)) return QUIET;
  return PENDING;
}

/** What one edge draws, beyond the stroke its status earned. */
export type EdgeLighting = {
  /** On the SELECTED node's lineage. */
  sel: boolean;
  /** On the HOVERED node's lineage. */
  lit: boolean;
  /** Whether anything on the canvas is selected at all. */
  anySelected: boolean;
};

/**
 * The stroke one edge actually draws.
 *
 * Cyan is the selected lineage and nothing else (DESIGN.md dag-edge). A
 * `live` edge borrowed it too, so a canvas with NOTHING selected drew cyan
 * lines — including one running under the zoom controls — and the operator
 * had no selection to read them against (design review C2, D21). With no
 * selection every edge is `edge-done` or `hair-strong`; the moment one is
 * made, cyan means its lineage.
 */
export function edgeStroke(style: EdgeStyle, light: EdgeLighting): string {
  if (light.sel) return 'var(--accent-2)';
  if (style.kind === 'live' && !light.anySelected) return 'var(--hair-strong)';
  if (light.lit && style.kind !== 'live') return 'var(--text-2)';
  return style.stroke;
}

/** Standalone extraction: a done node with no edges at all belongs in the
 * strip under the graph, not in it. A non-done isolated node stays in the
 * graph — an unstarted or blocked orphan is exactly what someone scanning the
 * DAG is looking for.
 *
 * This is the one status-dependent step, which is why it sits OUTSIDE
 * `metroLayout`: the caller partitions first, then lays out the graph half,
 * so status still never reaches the geometry. Input order is preserved in
 * both halves — the strip is a caller-rendered list, not geometry.
 *
 * "No edges" means no edge the LAYOUT would keep: this canonicalises exactly
 * as `canonicalEdges` does, so a self-loop or an edge with an unknown endpoint
 * leaves its node standalone here too. Counting those as links would strand a
 * done node in the graph as an isolated mark with no edge drawn to explain
 * it — the split and the geometry must agree on what an edge is. */
export function splitStandalone<T extends { id: string; status: string }>(
  nodes: readonly T[],
  edges: readonly [string, string][]
): { graph: T[]; strip: T[] } {
  const ids = new Set(nodes.map(n => n.id));
  const linked = new Set<string>();
  for (const [src, dst] of edges) {
    if (src === dst || !ids.has(src) || !ids.has(dst)) continue;
    linked.add(src);
    linked.add(dst);
  }
  const graph: T[] = [];
  const strip: T[] = [];
  for (const n of nodes) {
    if (!linked.has(n.id) && n.status === 'done') strip.push(n);
    else graph.push(n);
  }
  return { graph, strip };
}
