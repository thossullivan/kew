/**
 * #2249 item 7, re-cut for #2250 — the dagre layout's invariants, proved
 * against the real `@dagrejs/dagre` on the epics the owner reviews.
 *
 * The geometry is the UXEpic artboard's: 196x92 cards, nodesep 16, ranksep 40,
 * every edge from its source card's right middle to its target card's left
 * middle. Checked here: no overlapping cards, every edge joining its own two
 * ports, NO EDGE CROSSING A CARD (the bug the owner saw in the browser — edges
 * drawn as long lines through node titles), every segment horizontal or
 * vertical with the verticals only in the channels between layers, one
 * geometry per topology whatever the statuses, and model order inside a
 * layer.
 */
import { describe, expect, it } from 'vitest';
import goLiveJson from '../../../fixtures/factory/epics-go-live-2026-09.json';
import launchFixesJson from '../../../fixtures/factory/epics-launch-fixes.json';
import {
  dagreLayout,
  inPort,
  laneRoute,
  MARGIN,
  NODE_H,
  NODE_SEP,
  NODE_W,
  outPort,
  PITCH,
  RANK_SEP,
  resetDagreCache,
  roundedPath,
  topologyHash,
  type DagLayout,
  type DagPos
} from './dagre-layout';

type FixtureNode = { id: string; status: string };

const nodesOf = (json: {
  snapshot: { nodes: readonly { id: string; status: string }[] };
}): FixtureNode[] => json.snapshot.nodes.map((n) => ({ id: n.id, status: n.status }));

const edgesOf = (json: {
  snapshot: { edges: readonly (readonly string[])[] };
}): [string, string][] => json.snapshot.edges.map((e) => [e[0], e[1]] as [string, string]);

/** A shape whose longest edge (`v1 > v6`, five layers) dagre routes through a
 * lane BELOW the bottom card of the layers it skips. */
const LOW_BEND_NODES: FixtureNode[] = ['v1', 'v2', 'v5', 'v6', 'v7', 'v8'].map((id) => ({
  id,
  status: 'todo'
}));
const LOW_BEND_EDGES: [string, string][] = [
  ['v1', 'v6'],
  ['v1', 'v7'],
  ['v2', 'v6'],
  ['v7', 'v5'],
  ['v7', 'v8'],
  ['v8', 'v2']
];

/** A cyclic shape: one of its edges is drawn against the flow. */
const CYCLE_NODES: FixtureNode[] = ['a', 'b', 'c'].map((id) => ({ id, status: 'todo' }));
const CYCLE_EDGES: [string, string][] = [
  ['a', 'b'],
  ['a', 'c'],
  ['b', 'a'],
  ['c', 'b']
];

const GO_LIVE_NODES = nodesOf(goLiveJson);
const GO_LIVE_EDGES = edgesOf(goLiveJson);
const LAUNCH_NODES = nodesOf(launchFixesJson);
const LAUNCH_EDGES = edgesOf(launchFixesJson);

/** The card a node is drawn as, in canvas coordinates. */
function cardOf(at: DagPos): { x0: number; x1: number; y0: number; y1: number } {
  return { x0: at.x, x1: at.x + NODE_W, y0: at.y, y1: at.y + NODE_H };
}

/** Every `M`/`L`/`Q` coordinate pair in a path, in order. */
function pathPoints(d: string): DagPos[] {
  return [...d.matchAll(/[MLQ]\s*(-?[\d.]+)\s+(-?[\d.]+)/g)].map((m) => ({
    x: Number(m[1]),
    y: Number(m[2])
  }));
}

function fresh(
  nodes: readonly { id: string }[],
  edges: readonly [string, string][]
): DagLayout {
  resetDagreCache();
  return dagreLayout(nodes, edges);
}

/** Everything drawn is inside the canvas, on BOTH sides of it. */
function expectContained(l: DagLayout): void {
  for (const n of l.nodes) {
    const c = cardOf(n);
    expect(c.x0).toBeGreaterThanOrEqual(0);
    expect(c.y0).toBeGreaterThanOrEqual(0);
    expect(c.x1).toBeLessThanOrEqual(l.width);
    expect(c.y1).toBeLessThanOrEqual(l.height);
  }
  for (const e of l.edges) {
    for (const p of e.points) {
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.y).toBeGreaterThanOrEqual(0);
      expect(p.x).toBeLessThanOrEqual(l.width);
      expect(p.y).toBeLessThanOrEqual(l.height);
    }
  }
}

/** Every segment of every edge that runs through the INSIDE of a card —
 * touching a card's edge (where an edge enters or leaves it) is not a
 * crossing. */
function crossings(l: DagLayout): string[] {
  const out: string[] = [];
  for (const e of l.edges) {
    for (let i = 1; i < e.points.length; i++) {
      const a = e.points[i - 1];
      const b = e.points[i];
      const sx0 = Math.min(a.x, b.x);
      const sx1 = Math.max(a.x, b.x);
      const sy0 = Math.min(a.y, b.y);
      const sy1 = Math.max(a.y, b.y);
      for (const n of l.nodes) {
        const c = cardOf(n);
        const xIn = sx1 > c.x0 && sx0 < c.x1;
        const yIn = sy1 > c.y0 && sy0 < c.y1;
        // A horizontal segment is inside when its y is strictly inside the
        // card; a vertical one when its x is.
        const inside =
          a.y === b.y
            ? a.y > c.y0 && a.y < c.y1 && xIn
            : a.x > c.x0 && a.x < c.x1 && yIn;
        if (inside) out.push(`${e.key} through ${n.id}`);
      }
    }
  }
  return out;
}

describe('dagreLayout geometry', () => {
  it('lays cards out at the artboard`s own sizes', () => {
    expect([NODE_W, NODE_H, NODE_SEP, RANK_SEP]).toEqual([196, 92, 16, 40]);
    expect(PITCH).toBe(NODE_W + RANK_SEP);
  });

  it('lays the go-live epic out with no overlapping cards', () => {
    const l = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    expect(l.nodes).toHaveLength(GO_LIVE_NODES.length);
    const cards = l.nodes.map((n) => ({ id: n.id, ...cardOf(n) }));
    const clashes: string[] = [];
    for (let i = 0; i < cards.length; i++) {
      for (let j = i + 1; j < cards.length; j++) {
        const a = cards[i];
        const b = cards[j];
        if (a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1) {
          clashes.push(`${a.id}/${b.id}`);
        }
      }
    }
    expect(clashes).toEqual([]);
  });

  it('never runs an edge through a card', () => {
    // The browser bug: dagre's diagonal points, snapped onto marks inside the
    // cards, drew long lines through titles and status rows.
    for (const [nodes, edges] of [
      [GO_LIVE_NODES, GO_LIVE_EDGES],
      [LAUNCH_NODES, LAUNCH_EDGES],
      [LOW_BEND_NODES, LOW_BEND_EDGES],
      [CYCLE_NODES, CYCLE_EDGES]
    ] as const) {
      expect(crossings(fresh(nodes, edges))).toEqual([]);
    }
  });

  it('draws every segment horizontal or vertical, and turns only between layers', () => {
    const l = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    for (const e of l.edges) {
      for (let i = 1; i < e.points.length; i++) {
        const a = e.points[i - 1];
        const b = e.points[i];
        expect(a.x === b.x || a.y === b.y).toBe(true);
        if (a.x === b.x) {
          // A vertical run is in a channel: never inside a layer's column.
          for (const layer of l.layers) {
            expect(a.x > layer.x && a.x < layer.x + NODE_W).toBe(false);
          }
        }
      }
    }
  });

  it('keeps every card and every bend inside the canvas it reports', () => {
    expectContained(fresh(GO_LIVE_NODES, GO_LIVE_EDGES));
    expectContained(fresh(LAUNCH_NODES, LAUNCH_EDGES));
    expectContained(fresh(LOW_BEND_NODES, LOW_BEND_EDGES));
    expectContained(fresh(CYCLE_NODES, CYCLE_EDGES));
  });

  it('puts the nearest card or lane on the margin, and no further in', () => {
    for (const l of [fresh(GO_LIVE_NODES, GO_LIVE_EDGES), fresh(LOW_BEND_NODES, LOW_BEND_EDGES)]) {
      expect(Math.min(...l.nodes.map((n) => n.x))).toBe(MARGIN);
      const tops = [
        ...l.nodes.map((n) => n.y),
        ...l.edges.flatMap((e) => (e.lanes ? [...e.lanes] : []))
      ];
      expect(Math.min(...tops)).toBe(MARGIN);
    }
  });

  it('keeps two cards of one layer a clear gap apart', () => {
    const l = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    for (const layer of l.layers) {
      const col = l.nodes.filter((n) => n.layer === layer.index);
      for (let i = 1; i < col.length; i++) {
        expect(col[i].y - col[i - 1].y).toBeGreaterThanOrEqual(NODE_H + NODE_SEP);
      }
    }
  });

  it('starts every edge at its source card`s out port and ends it at its target`s in port', () => {
    const l = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    expect(l.edges).toHaveLength(GO_LIVE_EDGES.length);
    for (const e of l.edges) {
      const drawn = pathPoints(e.d);
      expect(drawn[0]).toEqual(outPort(l.positions[e.src]));
      expect(drawn[drawn.length - 1]).toEqual(inPort(l.positions[e.dst]));
      expect(e.points[0]).toEqual(outPort(l.positions[e.src]));
      expect(e.points[e.points.length - 1]).toEqual(inPort(l.positions[e.dst]));
      // A dependency always reads left to right: the target is downstream.
      expect(l.positions[e.dst].x).toBeGreaterThan(l.positions[e.src].x);
    }
  });

  it('records a lane per layer an edge spans, both card middles included', () => {
    const l = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    const layerOf = new Map(l.nodes.map((n) => [n.id, n.layer]));
    for (const e of l.edges) {
      expect(e.lanes).not.toBeNull();
      expect(e.lanes).toHaveLength(layerOf.get(e.dst)! - layerOf.get(e.src)! + 1);
      expect(e.lanes![0]).toBe(l.positions[e.src].y + NODE_H / 2);
      expect(e.lanes!.at(-1)).toBe(l.positions[e.dst].y + NODE_H / 2);
    }
  });

  it('numbers layers left to right, one pitch apart, and rows top to bottom', () => {
    const l = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    expect(l.layers.map((c) => c.index)).toEqual(l.layers.map((_, i) => i));
    for (let i = 1; i < l.layers.length; i++) {
      expect(l.layers[i].x - l.layers[i - 1].x).toBe(PITCH);
    }
    // Reading order is layer, then row — the brief's Tab order.
    const seen = l.nodes.map((n) => [n.layer, n.row] as const);
    expect(seen).toEqual([...seen].sort((a, b) => a[0] - b[0] || a[1] - b[1]));
    for (const layer of l.layers) {
      const col = l.nodes.filter((n) => n.layer === layer.index);
      expect(col.map((n) => n.row)).toEqual(col.map((_, i) => i));
      for (const n of col) expect(n.x).toBe(layer.x);
    }
  });
});

describe('laneRoute', () => {
  it('runs along each lane and turns in the middle of each channel', () => {
    const layerX = [0, PITCH, 2 * PITCH];
    const pts = laneRoute(layerX, 0, [46, 200, 120]);
    const ch1 = PITCH - RANK_SEP / 2;
    const ch2 = 2 * PITCH - RANK_SEP / 2;
    expect(pts).toEqual([
      { x: NODE_W, y: 46 },
      { x: ch1, y: 46 },
      { x: ch1, y: 200 },
      { x: ch2, y: 200 },
      { x: ch2, y: 120 },
      { x: 2 * PITCH, y: 120 }
    ]);
  });

  it('draws a straight edge between two cards on one lane as one segment', () => {
    expect(laneRoute([0, PITCH], 0, [46, 46])).toEqual([
      { x: NODE_W, y: 46 },
      { x: PITCH, y: 46 }
    ]);
  });
});

describe('dagreLayout determinism', () => {
  it('gives identical positions for the same topology with different statuses', () => {
    const a = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    // Every node done, every node blocked: the layout cannot see either.
    const done = GO_LIVE_NODES.map((n) => ({ ...n, status: 'done' }));
    const blocked = GO_LIVE_NODES.map((n) => ({ ...n, status: 'blocked' }));
    resetDagreCache();
    const b = dagreLayout(done, GO_LIVE_EDGES);
    resetDagreCache();
    const c = dagreLayout(blocked, GO_LIVE_EDGES);
    expect(b.hash).toBe(a.hash);
    expect(c.hash).toBe(a.hash);
    expect(b.positions).toEqual(a.positions);
    expect(c.positions).toEqual(a.positions);
    expect(b.edges.map((e) => e.d)).toEqual(a.edges.map((e) => e.d));
  });

  it('returns the cached layout for a topology it has already laid out', () => {
    resetDagreCache();
    const a = dagreLayout(GO_LIVE_NODES, GO_LIVE_EDGES);
    const b = dagreLayout(
      GO_LIVE_NODES.map((n) => ({ ...n, status: 'in_review' })),
      GO_LIVE_EDGES.map((e) => [...e] as [string, string])
    );
    expect(b).toBe(a);
    resetDagreCache();
    expect(dagreLayout(GO_LIVE_NODES, GO_LIVE_EDGES)).not.toBe(a);
  });

  it('is frozen, so one caller cannot move the graph for the next', () => {
    const l = fresh(GO_LIVE_NODES, GO_LIVE_EDGES);
    expect(Object.isFrozen(l)).toBe(true);
    expect(Object.isFrozen(l.nodes)).toBe(true);
    expect(Object.isFrozen(l.nodes[0])).toBe(true);
    expect(Object.isFrozen(l.edges[0].points)).toBe(true);
    expect(Object.isFrozen(l.edges[0].lanes)).toBe(true);
    expect(Object.isFrozen(l.positions[l.nodes[0].id])).toBe(true);
  });

  it('hashes the topology, including the model order that is a layout input', () => {
    const h = topologyHash(GO_LIVE_NODES, GO_LIVE_EDGES);
    expect(topologyHash(GO_LIVE_NODES.map((n) => ({ ...n, status: 'done' })), GO_LIVE_EDGES)).toBe(h);
    expect(topologyHash(GO_LIVE_NODES, [...GO_LIVE_EDGES].reverse())).toBe(h);
    expect(topologyHash([...GO_LIVE_NODES].reverse(), GO_LIVE_EDGES)).not.toBe(h);
    // A dropped edge — self-loop, unknown endpoint, duplicate — is not topology.
    expect(
      topologyHash(GO_LIVE_NODES, [
        ...GO_LIVE_EDGES,
        ['SFD-201', 'SFD-201'],
        ['SFD-201', 'nope'],
        GO_LIVE_EDGES[0]
      ])
    ).toBe(h);
  });

  it('reorders the geometry when the model order changes', () => {
    const a = fresh(LAUNCH_NODES, LAUNCH_EDGES);
    const b = fresh([...LAUNCH_NODES].reverse(), LAUNCH_EDGES);
    expect(b.hash).not.toBe(a.hash);
    expect(b.positions).not.toEqual(a.positions);
    // Same graph, so the same shape: every layer, and every layer's size.
    expect(b.layers).toEqual(a.layers);
    const sizes = (l: DagLayout): number[] =>
      l.layers.map((c) => l.nodes.filter((n) => n.layer === c.index).length);
    expect(sizes(b)).toEqual(sizes(a));
  });
});

describe('dagreLayout model order', () => {
  it('orders each layer of the launch-fixes epic by model order', () => {
    const l = fresh(LAUNCH_NODES, LAUNCH_EDGES);
    const rank = new Map(LAUNCH_NODES.map((n, i) => [n.id, i]));
    expect(l.layers.length).toBeGreaterThan(1);
    for (const layer of l.layers) {
      const col = l.nodes.filter((n) => n.layer === layer.index).map((n) => n.id);
      expect(col.length).toBeGreaterThan(0);
      expect(col).toEqual([...col].sort((a, b) => rank.get(a)! - rank.get(b)!));
    }
  });
});

describe('dagreLayout edge cases', () => {
  it('lays out an empty graph without asking dagre for a canvas', () => {
    const l = fresh([], []);
    expect(l).toEqual({
      hash: l.hash,
      width: 0,
      height: 0,
      layers: [],
      nodes: [],
      positions: {},
      edges: []
    });
  });

  it('places a lone node on the margin and draws no edge', () => {
    const l = fresh([{ id: '7' }], [['7', '7']]);
    expect(l.nodes).toEqual([{ id: '7', layer: 0, row: 0, x: MARGIN, y: MARGIN }]);
    expect(l.edges).toEqual([]);
    expect(l.layers).toEqual([{ index: 0, x: MARGIN }]);
    expect([l.width, l.height]).toEqual([MARGIN + NODE_W + MARGIN, MARGIN + NODE_H + MARGIN]);
  });

  it('ignores an edge whose endpoint is not in the graph', () => {
    const l = fresh([{ id: 'a' }, { id: 'b' }], [
      ['a', 'b'],
      ['a', 'ghost'],
      ['ghost', 'b']
    ]);
    expect(l.edges.map((e) => e.key)).toEqual(['a>b']);
    expect(l.nodes.map((n) => n.layer)).toEqual([0, 1]);
  });

  it('survives a cycle, and routes the edge against the flow round the cards', () => {
    const l = fresh([{ id: 'a' }, { id: 'b' }, { id: 'c' }], [
      ['a', 'b'],
      ['b', 'c'],
      ['c', 'a']
    ]);
    expect(l.nodes.map((n) => n.id).sort()).toEqual(['a', 'b', 'c']);
    expect(l.edges.map((e) => e.key)).toEqual(['a>b', 'b>c', 'c>a']);
    for (const e of l.edges) {
      const drawn = pathPoints(e.d);
      expect(drawn[0]).toEqual(outPort(l.positions[e.src]));
      expect(drawn[drawn.length - 1]).toEqual(inPort(l.positions[e.dst]));
    }
    const back = l.edges.find((e) => e.lanes === null)!;
    expect(back).toBeDefined();
    // Under every card, then back up into its target.
    const floor = Math.max(...l.nodes.map((n) => n.y + NODE_H));
    expect(Math.max(...back.points.map((p) => p.y))).toBeGreaterThan(floor);
    expect(crossings(l)).toEqual([]);
    expectContained(l);
  });
});

describe('roundedPath', () => {
  it('draws a straight line between two points', () => {
    expect(roundedPath([{ x: 0, y: 0 }, { x: 100, y: 0 }])).toBe('M0 0 L100 0');
  });

  it('rounds a bend at the artboard`s 9px without moving either end', () => {
    const d = roundedPath([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }]);
    expect(d).toBe('M0 0 L91 0 Q100 0 100 9 L100 100');
  });

  it('caps the radius at the room a cramped leg leaves', () => {
    // A 4px middle leg: each corner may take half of it, never the full 9.
    const d = roundedPath([
      { x: 0, y: 0 },
      { x: 50, y: 0 },
      { x: 50, y: 4 },
      { x: 100, y: 4 }
    ]);
    expect(d).toBe('M0 0 L48 0 Q50 0 50 2 L50 2 Q50 4 52 4 L100 4');
    expect(pathPoints(d)[0]).toEqual({ x: 0, y: 0 });
  });
});

describe('the cache key is the published hash', () => {
  it('reports the hash `topologyHash` computes', () => {
    expect(fresh(GO_LIVE_NODES, GO_LIVE_EDGES).hash).toBe(
      topologyHash(GO_LIVE_NODES, GO_LIVE_EDGES)
    );
    expect(fresh(LAUNCH_NODES, LAUNCH_EDGES).hash).toBe(
      topologyHash(LAUNCH_NODES, LAUNCH_EDGES)
    );
    expect(fresh([], []).hash).toBe(topologyHash([], []));
  });
});
