/**
 * #2249 item 7 — the Flow model: what the canvas draws, with no canvas.
 *
 * The collapse rules and the lineage sets are the two places this picture can
 * lie about the epic (hide a node someone is waiting on; brighten work that
 * is not downstream of the node under the cursor), so both are proved on the
 * go-live epic the owner reviewed as well as on hand-built graphs.
 */
import { describe, expect, it } from 'vitest';
import type { EpicNodeAttention } from '$lib/api';
import goLiveJson from '../../../fixtures/factory/epics-go-live-2026-09.json';
import {
  dagreLayout,
  inPort,
  MARGIN,
  NODE_H,
  NODE_W,
  outPort,
  resetDagreCache,
  type DagPos
} from './dagre-layout';
import {
  buildFlowModel,
  groupId,
  lineage,
  MIN_COLLAPSE,
  type FlowModel
} from './flow-model';

type Node = {
  id: string;
  status: string;
  attention?: EpicNodeAttention | null;
};

const GO_LIVE: Node[] = goLiveJson.snapshot.nodes.map((n) => ({
  id: n.id,
  status: n.status,
  attention: n.attention as EpicNodeAttention | null
}));
const GO_LIVE_EDGES: [string, string][] = goLiveJson.snapshot.edges.map(
  (e) => [e[0], e[1]] as [string, string]
);

const HOLD: EpicNodeAttention = {
  kind: 'approval',
  reason: 'a person has to say yes',
  url: null
} as unknown as EpicNodeAttention;

function model(
  nodes: readonly Node[],
  edges: readonly [string, string][],
  options?: Parameters<typeof buildFlowModel>[2]
): FlowModel<Node> {
  resetDagreCache();
  return buildFlowModel(nodes, edges, options);
}

/** A chain of `n` done nodes feeding `top`, the done node the in-progress
 * `live` depends on directly — which a collapse always keeps drawn. */
function chain(n: number): { nodes: Node[]; edges: [string, string][] } {
  const nodes: Node[] = Array.from({ length: n }, (_, i) => ({
    id: `d${i}`,
    status: 'done'
  }));
  const edges: [string, string][] = nodes
    .slice(1)
    .map((node, i) => [nodes[i].id, node.id] as [string, string]);
  nodes.push({ id: 'top', status: 'done' }, { id: 'live', status: 'in_progress' });
  if (n) edges.push([`d${n - 1}`, 'top']);
  edges.push(['top', 'live']);
  return { nodes, edges };
}

/**
 * A shape dagre routes a long edge BELOW the bottom row of: `v1 > v6` spans
 * five layers and dagre sends it down a channel under `v5`'s box, because the
 * dummy nodes it routes through are zero-height and so do not push the rows
 * apart.
 */
const LOW_BEND: Node[] = ['v1', 'v2', 'v5', 'v6', 'v7', 'v8'].map((id) => ({
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

/** The near side of the same gap: dagre routes this cycle's bend above the top
 * row, which `dagreLayout` slides onto its top margin. The model inherits
 * that, which is why `bounds` only measures the far side. */
const HIGH_BEND: Node[] = ['a', 'b', 'c'].map((id) => ({ id, status: 'todo' }));
const HIGH_BEND_EDGES: [string, string][] = [
  ['a', 'b'],
  ['a', 'c'],
  ['b', 'a'],
  ['c', 'b']
];

describe('buildFlowModel nodes', () => {
  it('gives every node the artboard`s card, where the layout put it', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    const l = dagreLayout(GO_LIVE, GO_LIVE_EDGES);
    expect(m.nodes).toHaveLength(GO_LIVE.length);
    for (const n of m.nodes) {
      expect(n.kind).toBe('node');
      expect(n.node?.id).toBe(n.id);
      expect(n.group).toBeNull();
      expect(n.width).toBe(NODE_W);
      expect(n.height).toBe(NODE_H);
      expect(n.position).toEqual(l.positions[n.id]);
    }
  });

  it('returns nodes in reading order and renumbers rows per layer', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    const seen = m.nodes.map((n) => [n.layer, n.row] as const);
    expect(seen).toEqual([...seen].sort((a, b) => a[0] - b[0] || a[1] - b[1]));
    for (const layer of m.layers) {
      const col = m.nodes.filter((n) => n.layer === layer.index);
      expect(col.map((n) => n.row)).toEqual(col.map((_, i) => i));
    }
  });

  it('reports a canvas that covers every visible node', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    for (const n of m.nodes) {
      expect(n.position.x + n.width).toBeLessThanOrEqual(m.width);
      expect(n.position.y + n.height).toBeLessThanOrEqual(m.height);
    }
    expect(m.width).toBeGreaterThan(0);
    expect(m.height).toBeGreaterThan(0);
  });

  it('reports a canvas that covers every bend, not just every box', () => {
    const m = model(LOW_BEND, LOW_BEND_EDGES);
    for (const e of m.edges) {
      for (const p of e.points) {
        expect(p.x).toBeLessThanOrEqual(m.width);
        expect(p.y).toBeLessThanOrEqual(m.height);
      }
    }
    const bottom = Math.max(
      ...m.nodes.map((n) => n.position.y + n.height),
      ...m.edges.flatMap((e) => e.points.map((p) => p.y))
    );
    expect(m.height).toBe(bottom + MARGIN);
    // And never past the layout's own canvas, which covers the whole graph.
    const l = dagreLayout(LOW_BEND, LOW_BEND_EDGES);
    expect(m.width).toBeLessThanOrEqual(l.width);
    expect(m.height).toBeLessThanOrEqual(l.height);
  });

  it('keeps a bend routed above the top row on the canvas too', () => {
    // `bounds` measures the far side only, so the near side has to come in
    // non-negative from the layout. Checked on the fixture that overhangs it,
    // and on a collapsed epic whose stand-in edges are re-pointed routes.
    for (const m of [model(HIGH_BEND, HIGH_BEND_EDGES), model(GO_LIVE, GO_LIVE_EDGES)]) {
      for (const n of m.nodes) {
        expect(n.position.x).toBeGreaterThanOrEqual(0);
        expect(n.position.y).toBeGreaterThanOrEqual(0);
      }
      for (const e of m.edges) {
        for (const p of e.points) {
          expect(p.x).toBeGreaterThanOrEqual(0);
          expect(p.y).toBeGreaterThanOrEqual(0);
        }
      }
    }
  });

  it('keeps one geometry per topology whatever the statuses', () => {
    const shown = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    const churned = model(
      GO_LIVE.map((n) => ({ ...n, status: 'in_review', attention: null })),
      GO_LIVE_EDGES,
      { showDone: true }
    );
    expect(churned.hash).toBe(shown.hash);
    const cards = (m: FlowModel<Node>): Record<string, DagPos> =>
      Object.fromEntries(m.nodes.map((n) => [n.id, n.position]));
    expect(cards(churned)).toEqual(cards(shown));
  });

  it('is empty for an empty graph', () => {
    const m = model([], []);
    expect(m).toEqual({
      hash: m.hash,
      width: 0,
      height: 0,
      layers: [],
      nodes: [],
      edges: [],
      groups: []
    });
  });
});

describe('buildFlowModel edges', () => {
  it('draws one edge per dependency, from card edge to card edge', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    expect(m.edges).toHaveLength(GO_LIVE_EDGES.length);
    const card = new Map(m.nodes.map((n) => [n.id, n.position]));
    for (const e of m.edges) {
      expect(e.id).toBe(`${e.source}>${e.target}`);
      expect(e.points[0]).toEqual(outPort(card.get(e.source)!));
      expect(e.points[e.points.length - 1]).toEqual(inPort(card.get(e.target)!));
      expect(e.d.startsWith(`M${e.points[0].x} ${e.points[0].y}`)).toBe(true);
    }
  });

  it('paints dashed first and live last', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    const orders = m.edges.map((e) => e.style.order);
    expect(orders).toEqual([...orders].sort((a, b) => a - b));
    // The go-live epic has all four strokes on it.
    expect(new Set(m.edges.map((e) => e.style.kind))).toEqual(
      new Set(['pending', 'blocked', 'quiet', 'live'])
    );
  });

  it('styles an edge out of a collapsed chain as settled history', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    const group = groupId('SFD-204');
    const out = m.edges.filter((e) => e.source === group);
    expect(out.map((e) => e.target)).toEqual(['SFD-212']);
    // The group reads as done, so its edge into a done node is the same quiet
    // stroke the members' own edges were — never "dependency not met".
    const shown = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    expect(out[0].style).toEqual(shown.edges.find((e) => e.id === 'SFD-204>SFD-212')!.style);
  });

  it('keeps dagre’s route on an edge out of the group’s own head', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    // Same topology, so this is the very layout the model was built from and
    // the routes compare point for point.
    const routed = new Map(
      dagreLayout(GO_LIVE, GO_LIVE_EDGES).edges.map((e) => [e.key, e] as const)
    );
    const out = m.edges.filter((e) => e.source === groupId('SFD-204'));
    expect(out).toHaveLength(1);
    for (const e of out) {
      const dagre = routed.get(`SFD-204>${e.target}`)!;
      expect(e.points).toEqual(dagre.points);
      expect(e.d).toBe(dagre.d);
    }
  });

  it('drops the straight stand-in when the head’s own edge covers the pair', () => {
    const nodes: Node[] = [
      { id: 'a', status: 'done' },
      { id: 'b', status: 'done' },
      { id: 'c', status: 'done' },
      { id: 'live', status: 'todo' }
    ];
    const edges: [string, string][] = [
      ['a', 'b'],
      ['a', 'c'],
      ['b', 'c'],
      ['c', 'live']
    ];
    const m = model(nodes, edges);
    const routed = dagreLayout(nodes, edges).edges.find((e) => e.key === 'b>c')!;
    // `a>c` sorts FIRST, so the stand-in for the chain's other member is seen
    // before the head's own edge — and loses to it.
    const into = m.edges.filter((e) => e.target === 'c');
    expect(into.map((e) => e.id)).toEqual([`${groupId('a')}>c`]);
    expect(into[0].points).toEqual(routed.points);
    expect(into[0].d).toBe(routed.d);
    expect(into[0].points).not.toEqual([routed.points[0], routed.points.at(-1)]);
  });
});

describe('buildFlowModel collapse', () => {
  it('collapses a fully done upstream chain into one group with the right count', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    // SFD-212 is done, but three open nodes depend on it directly, so it
    // stays; the two roots behind it are the history that folds — as ONE
    // card, though nothing joins them but the node they both feed.
    expect(m.groups).toEqual([
      {
        id: groupId('SFD-204'),
        ids: ['SFD-204', 'SFD-201'],
        count: 2,
        summary: '2 done'
      }
    ]);
    const group = m.nodes.find((n) => n.kind === 'group')!;
    expect(group.id).toBe(groupId('SFD-204'));
    expect(group.node).toBeNull();
    // It stands exactly where the chain's head stood, so it cannot land on a
    // node that stayed.
    const shown = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    expect(group.position).toEqual(shown.nodes.find((n) => n.id === 'SFD-204')!.position);
    expect(m.nodes.map((n) => n.id)).not.toContain('SFD-201');
    expect(m.nodes.map((n) => n.id)).toContain('SFD-212');
    expect(m.nodes).toHaveLength(GO_LIVE.length - 1);
    expect(m.edges).toHaveLength(GO_LIVE_EDGES.length - 1);
  });

  it('never collapses a done node somebody is waiting on', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    // SFD-258 is done with an approval hold on it.
    const held = m.nodes.find((n) => n.id === 'SFD-258');
    expect(held?.kind).toBe('node');
    expect(held?.node?.attention).toBeTruthy();
  });

  it('keeps a done node whose own history is not done', () => {
    const nodes: Node[] = [
      { id: 'a', status: 'in_review' },
      { id: 'b', status: 'done' },
      { id: 'c', status: 'todo' }
    ];
    const m = model(nodes, [
      ['a', 'b'],
      ['b', 'c']
    ]);
    expect(m.groups).toEqual([]);
    expect(m.nodes.map((n) => n.id)).toEqual(['a', 'b', 'c']);
  });

  it('stops a chain at the done node that needs a person', () => {
    const nodes: Node[] = [
      { id: 'a', status: 'done' },
      { id: 'b', status: 'done' },
      { id: 'c', status: 'done' },
      { id: 'held', status: 'done', attention: HOLD },
      { id: 'd', status: 'done' },
      { id: 'live', status: 'in_progress' }
    ];
    const m = model(nodes, [
      ['a', 'b'],
      ['b', 'c'],
      ['c', 'held'],
      ['held', 'd'],
      ['d', 'live']
    ]);
    // `c` is the needs-you node's direct parent, so it stays beside it.
    expect(m.groups.map((g) => g.ids)).toEqual([['a', 'b']]);
    expect(m.nodes.map((n) => n.id)).toEqual([groupId('a'), 'c', 'held', 'd', 'live']);
    expect(m.edges.map((e) => e.id).sort()).toEqual([
      'c>held',
      'd>live',
      `${groupId('a')}>c`,
      'held>d'
    ]);
  });

  it('leaves a done chain of one in the graph', () => {
    expect(MIN_COLLAPSE).toBe(2);
    const one = chain(1);
    expect(model(one.nodes, one.edges).groups).toEqual([]);
    const two = chain(2);
    expect(model(two.nodes, two.edges).groups.map((g) => g.summary)).toEqual(['2 done']);
  });

  it('collapses the whole prefix of a six-node chain into "6 done"', () => {
    const six = chain(6);
    const m = model(six.nodes, six.edges);
    expect(m.groups.map((g) => g.summary)).toEqual(['6 done']);
    expect(m.nodes.map((n) => n.id)).toEqual([groupId('d0'), 'top', 'live']);
    expect(m.edges.map((e) => e.id)).toEqual([`${groupId('d0')}>top`, 'top>live']);
    // The group's layer is the head's own, not a renumbering.
    const shown = model(six.nodes, six.edges, { showDone: true });
    expect(m.nodes[0].layer).toBe(shown.nodes.find((n) => n.id === 'd5')!.layer);
  });

  it('merges two members feeding the same target into one edge', () => {
    const nodes: Node[] = [
      { id: 'a', status: 'done' },
      { id: 'b', status: 'done' },
      { id: 'c', status: 'done' },
      { id: 'live', status: 'todo' }
    ];
    const m = model(nodes, [
      ['a', 'b'],
      ['a', 'c'],
      ['b', 'c'],
      ['c', 'live']
    ]);
    expect(m.groups.map((g) => g.count)).toEqual([2]);
    expect(m.edges.map((e) => e.id).sort()).toEqual(['c>live', `${groupId('a')}>c`]);
  });

  it('shows a group’s members again once it is expanded', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { expanded: [groupId('SFD-204')] });
    expect(m.groups).toEqual([]);
    expect(m.nodes).toHaveLength(GO_LIVE.length);
    const shown = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    expect(m.nodes.map((n) => n.position)).toEqual(shown.nodes.map((n) => n.position));
  });

  it('folds host-contract’s history and keeps the needs-you node’s direct parent (#2560)', () => {
    // The live epic the toggle did nothing on: eight finished nodes in a chain
    // into #2399, which is finished too and is the one thing #2526 (in
    // review, needs you) depends on. Every finished node is upstream of
    // #2526, which is what kept the old path rule from folding any of them.
    const chainIds = ['2378', '2379', '2380', '2381', '2397', '2398', '2411', '2433'];
    const nodes: Node[] = [
      ...chainIds.map((id) => ({ id, status: 'done' })),
      { id: '2399', status: 'done' },
      { id: '2526', status: 'in_review', attention: HOLD }
    ];
    const edges: [string, string][] = [
      ...chainIds.slice(1).map((id, i) => [chainIds[i], id] as [string, string]),
      ['2433', '2399'],
      ['2399', '2526']
    ];
    const m = model(nodes, edges);
    expect(m.groups).toHaveLength(1);
    const [group] = m.groups;
    expect(group.count).toBe(8);
    expect(group.summary).toBe('8 done');
    expect([...group.ids].sort()).toEqual([...chainIds].sort());
    expect(m.nodes.map((n) => n.id)).toEqual([group.id, '2399', '2526']);
    expect(m.edges.map((e) => e.id).sort()).toEqual(['2399>2526', `${group.id}>2399`]);
    // And the toggle has something to change: ten cards shown, three folded.
    expect(model(nodes, edges, { showDone: true }).nodes).toHaveLength(10);
  });

  it('keeps the direct finished parent of open work even when it is a chain head', () => {
    // `b` feeds `live` directly: it stays, and `a` alone is too short to fold.
    const nodes: Node[] = [
      { id: 'a', status: 'done' },
      { id: 'b', status: 'done' },
      { id: 'live', status: 'todo' }
    ];
    const m = model(nodes, [
      ['a', 'b'],
      ['b', 'live']
    ]);
    expect(m.groups).toEqual([]);
    expect(m.nodes.map((n) => n.id)).toEqual(['a', 'b', 'live']);
  });

  it('keeps a group`s id, and an opened group open, when its chain grows a new head', () => {
    // `live` finishing frees `top`: it is no longer the direct parent of open
    // work, so it joins the chain and becomes the chain's new head.
    const before = chain(3);
    const grown = {
      nodes: [
        ...before.nodes.map((n) => (n.id === 'live' ? { ...n, status: 'done' } : n)),
        { id: 'next', status: 'todo' }
      ],
      edges: [...before.edges, ['live', 'next'] as [string, string]]
    };
    const was = model(before.nodes, before.edges).groups;
    const now = model(grown.nodes, grown.edges).groups;
    expect(was.map((g) => g.ids)).toEqual([['d0', 'd1', 'd2']]);
    expect(now.map((g) => g.ids)).toEqual([['d0', 'd1', 'd2', 'top']]);
    expect(now.map((g) => g.id)).toEqual(was.map((g) => g.id));
    // Opened before the change, still open after it.
    const open = model(grown.nodes, grown.edges, { expanded: [was[0].id] });
    expect(open.groups).toEqual([]);
    expect(open.nodes.map((n) => n.id)).toEqual(['d0', 'd1', 'd2', 'top', 'live', 'next']);
  });

  it('collapses nothing when "show done" is on', () => {
    const six = chain(6);
    const m = model(six.nodes, six.edges, { showDone: true });
    expect(m.groups).toEqual([]);
    expect(m.nodes).toHaveLength(8);
  });
});

describe('lineage', () => {
  it('returns exactly the transitive upstream and downstream edges', () => {
    const nodes: Node[] = ['root', 'a', 'b', 'mid', 'c', 'sib', 'leaf'].map((id) => ({
      id,
      status: 'todo'
    }));
    const m = model(nodes, [
      ['root', 'a'],
      ['a', 'mid'],
      ['b', 'mid'],
      ['mid', 'c'],
      ['c', 'leaf'],
      ['sib', 'leaf']
    ]);
    const l = lineage(m, 'mid')!;
    expect([...l.edges].sort()).toEqual([
      'a>mid',
      'b>mid',
      'c>leaf',
      'mid>c',
      'root>a'
    ]);
    expect([...l.nodes].sort()).toEqual(['a', 'b', 'c', 'leaf', 'mid', 'root']);
    // `sib>leaf` is a sibling's edge, not this node's lineage.
    expect(l.edges.has('sib>leaf')).toBe(false);
    expect(l.nodes.has('sib')).toBe(false);
  });

  it('gives an isolated node itself and nothing else', () => {
    const m = model([{ id: 'alone', status: 'todo' }], []);
    expect(lineage(m, 'alone')).toEqual({ nodes: new Set(['alone']), edges: new Set() });
  });

  it('is NULL for a node that is not on the canvas, never empty sets', () => {
    // The caller's rule is "dim everything outside the sets" (DagView's
    // `lit`), so empty sets would dim every node and every edge on the canvas
    // — and a `hovered` id outliving its node is routine: a frame that moves
    // the node to the standalone strip or collapses it into a done group
    // removes it from under the pointer, and `onnodepointerleave` never fires.
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    // Collapsed away into the group node.
    expect(lineage(m, 'SFD-201')).toBeNull();
    expect(lineage(m, 'nope')).toBeNull();
    // The reading that matters: nothing is dimmed, not everything.
    const lit = lineage(m, 'SFD-201');
    expect(m.nodes.some((n) => lit !== null && !lit.nodes.has(n.id))).toBe(false);
    expect(m.edges.some((e) => lit !== null && !lit.edges.has(e.id))).toBe(false);
  });

  it('walks the collapsed graph, so a group is one stop', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    const l = lineage(m, 'SFD-247')!;
    expect(l.nodes.has(groupId('SFD-204'))).toBe(true);
    expect(l.nodes.has('SFD-201')).toBe(false);
    expect([...l.edges].sort()).toEqual([
      'SFD-212>SFD-247',
      'SFD-247>SFD-264',
      `${groupId('SFD-204')}>SFD-212`
    ]);
  });

  it('does not run away on a cycle', () => {
    const nodes: Node[] = [
      { id: 'a', status: 'todo' },
      { id: 'b', status: 'todo' },
      { id: 'c', status: 'todo' }
    ];
    const m = model(nodes, [
      ['a', 'b'],
      ['b', 'c'],
      ['c', 'a']
    ]);
    const l = lineage(m, 'a')!;
    expect([...l.nodes].sort()).toEqual(['a', 'b', 'c']);
    expect([...l.edges].sort()).toEqual(['a>b', 'b>c', 'c>a']);
  });
});

/** Segments of the model's edges that run through the inside of a VISIBLE
 * card. */
function crossings(m: FlowModel<Node>): string[] {
  const out: string[] = [];
  for (const e of m.edges) {
    for (let i = 1; i < e.points.length; i++) {
      const a = e.points[i - 1];
      const b = e.points[i];
      for (const n of m.nodes) {
        const x0 = n.position.x;
        const x1 = x0 + n.width;
        const y0 = n.position.y;
        const y1 = y0 + n.height;
        const inside =
          a.y === b.y
            ? a.y > y0 && a.y < y1 && Math.max(a.x, b.x) > x0 && Math.min(a.x, b.x) < x1
            : a.x > x0 && a.x < x1 && Math.max(a.y, b.y) > y0 && Math.min(a.y, b.y) < y1;
        if (inside) out.push(`${e.id} through ${n.id}`);
      }
    }
  }
  return out;
}

describe('an edge out of a collapsed chain', () => {
  it('leaves the group card and follows its member`s lanes, never a straight cut', () => {
    // `a` is a member but not the head (`c` is): its edge to `d` is drawn
    // from the group card, along `a`'s own lanes from `c`'s layer on.
    const nodes: Node[] = [
      { id: 'a', status: 'done' },
      { id: 'b', status: 'done' },
      { id: 'c', status: 'done' },
      { id: 'd', status: 'done' },
      { id: 'live', status: 'todo' }
    ];
    const m = model(nodes, [
      ['a', 'b'],
      ['b', 'c'],
      ['c', 'd'],
      ['a', 'd'],
      ['d', 'live']
    ]);
    expect(m.groups.map((g) => g.id)).toEqual([groupId('a')]);
    const group = m.nodes.find((n) => n.group)!;
    const target = m.nodes.find((n) => n.id === 'd')!;
    const e = m.edges.find((x) => x.id === `${group.id}>d`)!;
    expect(e.points[0]).toEqual(outPort(group.position));
    expect(e.points.at(-1)).toEqual(inPort(target.position));
    for (let i = 1; i < e.points.length; i++) {
      const p = e.points[i - 1];
      const q = e.points[i];
      expect(p.x === q.x || p.y === q.y).toBe(true);
    }
    expect(crossings(m)).toEqual([]);
  });

  it('goes round the cards when its target shares the group`s layer', () => {
    const nodes: Node[] = [
      { id: 'a', status: 'done' },
      { id: 'b', status: 'done' },
      { id: 'c', status: 'done' },
      { id: 'live', status: 'todo' }
    ];
    const m = model(nodes, [
      ['a', 'b'],
      ['a', 'c'],
      ['c', 'live']
    ]);
    const group = m.nodes.find((n) => n.group)!;
    const target = m.nodes.find((n) => n.id === 'c')!;
    expect(group.layer).toBe(target.layer);
    const e = m.edges.find((x) => x.target === 'c')!;
    expect(e.points[0]).toEqual(outPort(group.position));
    expect(e.points.at(-1)).toEqual(inPort(target.position));
    expect(crossings(m)).toEqual([]);
  });

  it('crosses no card on the go-live epic, collapsed or not', () => {
    expect(crossings(model(GO_LIVE, GO_LIVE_EDGES))).toEqual([]);
    expect(crossings(model(GO_LIVE, GO_LIVE_EDGES, { showDone: true }))).toEqual([]);
  });
});
