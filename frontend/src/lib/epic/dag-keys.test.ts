/**
 * #2249 item 7 — the keyboard moves of the brief's table, as a pure function
 * of the Flow model. No DOM, no focus, no Svelte Flow: an id in, an id or
 * null out.
 *
 * Plus `artboardKey` and `strayKey`, which are the other half of that table:
 * WHICH presses the graph may answer at all — one for a press on the artboard,
 * one for a press that landed on the body, as a `?node=` link's does. They
 * live here as predicates over a few fields rather than as conditions inside
 * the canvas, because the canvas is the one surface in this app with no DOM to
 * mount.
 */
import { describe, expect, it } from 'vitest';
import goLiveJson from '../../../fixtures/factory/epics-go-live-2026-09.json';
import {
  artboardKey,
  DAG_KEYS,
  lineageStops,
  neighbor,
  readingOrder,
  strayKey,
  strayWithoutSelection,
  tabStep,
  type KeyOrigin,
  type StrayOrigin
} from './dag-keys';
import { resetDagreCache } from './dagre-layout';
import { buildFlowModel, groupId, type FlowModel } from './flow-model';

type Node = { id: string; status: string };

const GO_LIVE: Node[] = goLiveJson.snapshot.nodes.map((n) => ({
  id: n.id,
  status: n.status
}));
const GO_LIVE_EDGES: [string, string][] = goLiveJson.snapshot.edges.map(
  (e) => [e[0], e[1]] as [string, string]
);

function model(
  nodes: readonly Node[],
  edges: readonly [string, string][],
  options?: Parameters<typeof buildFlowModel>[2]
): FlowModel<Node> {
  resetDagreCache();
  return buildFlowModel(nodes, edges, options);
}

/** root → mid → sink, with a second root into mid, a skipping root → sink
 * edge, and one isolated node. */
const SHAPE: Node[] = [
  { id: 'root', status: 'todo' },
  { id: 'root2', status: 'todo' },
  { id: 'mid', status: 'todo' },
  { id: 'mid2', status: 'todo' },
  { id: 'sink', status: 'todo' },
  { id: 'far', status: 'todo' }
];
const SHAPE_EDGES: [string, string][] = [
  ['root', 'mid'],
  ['root2', 'mid'],
  ['root', 'mid2'],
  ['mid', 'sink'],
  ['root', 'sink']
];

/** A cyclic epic: `a → b → c → d → e`, with `e` back onto `b` and on to `f`.
 * `e`'s back edge reaches three layers LEFT while `f` is one layer right, and
 * `b`'s parents are one layer left (`a`) and three right (`e`). */
const CYCLE: Node[] = ['a', 'b', 'c', 'd', 'e', 'f'].map((id) => ({ id, status: 'todo' }));
const CYCLE_EDGES: [string, string][] = [
  ['a', 'b'],
  ['b', 'c'],
  ['c', 'd'],
  ['d', 'e'],
  ['e', 'b'],
  ['e', 'f']
];

/** A cycle whose back edge TIES with a forward one: `c`'s children are `b` one
 * layer left and `d` one layer right, so an unsigned layer distance alone
 * cannot tell them apart and the id breaks the tie — the wrong way. */
const TIE: Node[] = ['a', 'b', 'c', 'd', 'e'].map((id) => ({ id, status: 'todo' }));
const TIE_EDGES: [string, string][] = [
  ['a', 'b'],
  ['b', 'c'],
  ['c', 'b'],
  ['c', 'd'],
  ['d', 'e'],
  ['c', 'e']
];

describe('readingOrder', () => {
  it('reads layer by layer, top to bottom', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    const order = readingOrder(m);
    expect(order).toEqual(m.nodes.map((n) => n.id));
    const at = new Map(m.nodes.map((n) => [n.id, n]));
    for (let i = 1; i < order.length; i++) {
      const prev = at.get(order[i - 1])!;
      const next = at.get(order[i])!;
      expect(prev.layer < next.layer || (prev.layer === next.layer && prev.row < next.row))
        .toBe(true);
    }
  });

  it('has one stop per collapsed chain, not one per done node', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    const order = readingOrder(m);
    expect(order).toContain(groupId('SFD-204'));
    expect(order).not.toContain('SFD-201');
    expect(order).toHaveLength(GO_LIVE.length - 1);
  });
});

describe('tabStep', () => {
  it('starts at the first node and wraps at the last', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    const order = readingOrder(m);
    expect(tabStep(m, null, 1)).toBe(order[0]);
    expect(tabStep(m, null, -1)).toBe(order[order.length - 1]);
    expect(tabStep(m, order[0], 1)).toBe(order[1]);
    expect(tabStep(m, order[order.length - 1], 1)).toBe(order[0]);
    expect(tabStep(m, order[0], -1)).toBe(order[order.length - 1]);
    // The canvas's own handler does not wrap: past either end, focus leaves.
    expect(tabStep(m, order[order.length - 1], 1, { wrap: false })).toBeNull();
    expect(tabStep(m, order[0], -1, { wrap: false })).toBeNull();
    expect(tabStep(m, order[0], 1, { wrap: false })).toBe(order[1]);
  });

  it('walks the whole model in order and comes back', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    const order = readingOrder(m);
    let at: string | null = null;
    const walked = order.map(() => (at = tabStep(m, at, 1)));
    expect(walked).toEqual(order);
    expect(tabStep(m, at, 1)).toBe(order[0]);
  });

  it('falls back to an end when the selection is off the canvas', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    // Collapsed into the group node, so it is no longer a Tab stop.
    expect(tabStep(m, 'SFD-201', 1)).toBe(readingOrder(m)[0]);
    expect(tabStep(m, null, 1)).toBe(readingOrder(m)[0]);
  });

  it('has nothing to land on in an empty graph', () => {
    const m = model([], []);
    expect(tabStep(m, null, 1)).toBeNull();
    expect(tabStep(m, 'anything', -1)).toBeNull();
  });
});

describe('neighbor along an edge', () => {
  it('goes right to a direct downstream node', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    expect(neighbor(m, 'mid', 'right')).toBe('sink');
    expect(neighbor(m, 'root2', 'right')).toBe('mid');
  });

  it('returns null at a sink', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    expect(neighbor(m, 'sink', 'right')).toBeNull();
    expect(neighbor(m, 'far', 'right')).toBeNull();
  });

  it('goes left to a direct upstream node, and null at a root', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    expect(neighbor(m, 'sink', 'left')).toBe('mid');
    expect(neighbor(m, 'root', 'left')).toBeNull();
  });

  it('prefers the nearest layer when a dependency skips one', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    const at = new Map(m.nodes.map((n) => [n.id, n]));
    // `root` feeds `mid` in the next layer and `sink` two layers along.
    expect(at.get('sink')!.layer).toBeGreaterThan(at.get('mid')!.layer);
    expect(at.get(neighbor(m, 'root', 'right')!)!.layer).toBe(at.get('mid')!.layer);
  });

  it('only follows edges the model actually draws', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES);
    // SFD-212's upstream is the collapsed pair, not SFD-201 or SFD-204.
    expect(neighbor(m, 'SFD-212', 'left')).toBe(groupId('SFD-204'));
    expect(neighbor(m, groupId('SFD-204'), 'right')).toBe('SFD-212');
    // Three targets; the nearest layer holds two of them, and the rule takes
    // the row closest to SFD-212's own.
    expect(neighbor(m, 'SFD-212', 'right')).toBe('SFD-224');
  });

  it('measures the nearest layer unsigned, so a back edge cannot drag → left', () => {
    const m = model(CYCLE, CYCLE_EDGES);
    const at = new Map(m.nodes.map((n) => [n.id, n]));
    // The premise: `e`'s two children are `f` one layer right and `b` three
    // layers LEFT, and `b`'s two parents are `a` one left and `e` three right.
    expect(at.get('f')!.layer - at.get('e')!.layer).toBe(1);
    expect(at.get('b')!.layer - at.get('e')!.layer).toBeLessThan(0);
    expect(neighbor(m, 'e', 'right')).toBe('f');
    expect(neighbor(m, 'b', 'left')).toBe('a');
  });

  it('breaks an equal-distance tie by side, so → and ← cannot agree', () => {
    const m = model(TIE, TIE_EDGES);
    const at = new Map(m.nodes.map((n) => [n.id, n]));
    // The premise: `c`'s children are `b` one layer LEFT and `d` one layer
    // right, equally adjacent, with `b` first on the id tie-break.
    expect(at.get('b')!.layer).toBe(at.get('c')!.layer - 1);
    expect(at.get('d')!.layer).toBe(at.get('c')!.layer + 1);
    expect(neighbor(m, 'c', 'right')).toBe('d');
    // ← still has its own answer, and it is not the one → gives.
    expect(neighbor(m, 'c', 'left')).toBe('b');
  });

  it('still follows a back edge when it is the only one there is', () => {
    const m = model(CYCLE, CYCLE_EDGES);
    // `f` has nothing downstream, and `a`'s only child is `b`: the cursor is
    // not stuck, it just prefers the adjacent layer when there is a choice.
    expect(neighbor(m, 'f', 'right')).toBeNull();
    expect(neighbor(m, 'a', 'right')).toBe('b');
    // `b`'s own children: `c` next layer along.
    expect(neighbor(m, 'c', 'left')).toBe('b');
  });

  it('is null for a node that is not on the canvas', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    for (const dir of ['left', 'right', 'up', 'down'] as const) {
      expect(neighbor(m, 'nope', dir)).toBeNull();
    }
  });
});

describe('neighbor within a layer', () => {
  it('moves down and up the rows of one layer', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    const roots = m.nodes.filter((n) => n.layer === 0).map((n) => n.id);
    expect(roots.length).toBeGreaterThan(1);
    expect(neighbor(m, roots[0], 'down')).toBe(roots[1]);
    expect(neighbor(m, roots[1], 'up')).toBe(roots[0]);
  });

  it('does not wrap at the top or the bottom of a layer', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    const roots = m.nodes.filter((n) => n.layer === 0).map((n) => n.id);
    expect(neighbor(m, roots[0], 'up')).toBeNull();
    expect(neighbor(m, roots[roots.length - 1], 'down')).toBeNull();
  });

  it('never leaves the layer it started in', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    const at = new Map(m.nodes.map((n) => [n.id, n]));
    for (const n of m.nodes) {
      for (const dir of ['up', 'down'] as const) {
        const to = neighbor(m, n.id, dir);
        if (to) expect(at.get(to)!.layer).toBe(n.layer);
      }
    }
  });

  it('visits every row of a layer by going down it', () => {
    const m = model(GO_LIVE, GO_LIVE_EDGES, { showDone: true });
    const col = m.nodes.filter((n) => n.layer === 3);
    expect(col.length).toBeGreaterThan(2);
    const walked = [col[0].id];
    for (let at = neighbor(m, walked[0], 'down'); at; at = neighbor(m, at, 'down')) {
      walked.push(at);
    }
    expect(walked).toEqual(col.map((n) => n.id));
  });
});

describe('artboardKey — whose key press this is', () => {
  const artboard = { name: 'the .dag element' };
  const press = (over: Partial<KeyOrigin> = {}): KeyOrigin => ({
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    target: artboard,
    currentTarget: artboard,
    ...over
  });

  it('answers a press on the artboard itself', () => {
    expect(artboardKey(press())).toBe(true);
  });

  it('leaves a press from INSIDE the canvas alone', () => {
    // The node popover (`NodeToolbar` -> `NodePopover`) renders inside
    // `<SvelteFlow>`, which is inside the `.dag` element the handler is bound
    // to, and stops no key event of its own. Answered here, Tab out of its
    // Copy button was preventDefaulted and moved the graph SELECTION — so the
    // popover's Copy and its links were unreachable from the keyboard, which
    // makes the box a trap around the controls the brief put in it.
    for (const inside of [{ name: 'the Copy button' }, { name: 'the done toggle' }]) {
      expect(artboardKey(press({ target: inside }))).toBe(false);
    }
  });

  it('leaves every modifier combination to the browser', () => {
    for (const mod of ['metaKey', 'ctrlKey', 'altKey'] as const) {
      expect(artboardKey(press({ [mod]: true }))).toBe(false);
    }
  });
});

describe('strayKey — a press that landed on the body', () => {
  const body = { name: 'document.body' };
  const press = (over: Partial<StrayOrigin> = {}): StrayOrigin => ({
    key: 'ArrowRight',
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    target: body,
    ...over
  });

  it('answers the legend`s keys, so a `?node=` link needs no click first', () => {
    // The router leaves the focus on the body, and until the canvas answered
    // these the legend's keys did nothing on a linked node.
    for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Enter', 'F', '0']) {
      expect(strayKey(press({ key }), body)).toBe(true);
    }
  });

  it('withholds Tab, which is the only way off the body', () => {
    // The regression: Tab answered here is preventDefaulted with the focus
    // still on the body, so it never moves — every control on the page,
    // including the popover's own Copy / Open in Work / Issue / PR, was
    // unreachable from the keyboard for as long as a node stayed selected.
    // From the body Tab belongs to the browser: the artboard is a tab stop,
    // so it walks INTO the graph, where `artboardKey` owns Tab and Esc
    // clears the selection.
    // Shift-Tab is the same `key`, so one check covers both directions.
    expect(strayKey(press({ key: 'Tab' }), body)).toBe(false);
  });

  it('leaves a press on anything BUT the body alone', () => {
    // Somebody is typing in the search box, or tabbing through the rail: the
    // graph does not reach over the element that has the focus.
    for (const elsewhere of [{ name: 'the rail`s link' }, { name: 'the Copy button' }]) {
      expect(strayKey(press({ target: elsewhere }), body)).toBe(false);
    }
  });

  it('leaves every modifier combination to the browser', () => {
    for (const mod of ['metaKey', 'ctrlKey', 'altKey'] as const) {
      expect(strayKey(press({ [mod]: true }), body)).toBe(false);
    }
  });
});

describe('strayWithoutSelection — the body`s keys with nothing selected', () => {
  it('answers ← and →, which land on the first node', () => {
    // A fresh load has no selection and the focus on the body: ← and → did
    // nothing there while ↓ selected a node (#2780 C5).
    expect(strayWithoutSelection('ArrowLeft')).toBe(true);
    expect(strayWithoutSelection('ArrowRight')).toBe(true);
  });

  it('leaves the keys that need a selection, and the page scroll, alone', () => {
    for (const key of ['ArrowUp', 'ArrowDown', 'Enter', '0', 'l', 'L', 'f', 'F', 'Tab']) {
      expect(strayWithoutSelection(key)).toBe(false);
    }
  });
});

describe('the legend`s words (#2780 D26)', () => {
  const label = (key: string): string | undefined =>
    DAG_KEYS.find((d) => d.key === key)?.description;

  it('calls ← → the one-edge step, not lineage', () => {
    expect(label('ArrowLeft')).toBe('step along an edge');
    expect(label('ArrowRight')).toBe('step along an edge');
  });

  it('calls L lineage, and draws it', () => {
    expect(label('L')).toBe('lineage');
    expect(label('l')).toBe('lineage');
    expect(DAG_KEYS.find((d) => d.key === 'L')?.legend).toBe(true);
  });
});

describe('lineageStops — the ← / → lineage buttons (#2533)', () => {
  it('walks one edge each way, the same move as the arrow keys', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    expect(lineageStops(m, 'mid')).toEqual({ up: neighbor(m, 'mid', 'left'), down: 'sink' });
    expect(lineageStops(m, 'mid').up).not.toBeNull();
  });

  it('closes ← at a root and → at a sink', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    expect(lineageStops(m, 'root')).toEqual({ up: null, down: neighbor(m, 'root', 'right') });
    expect(lineageStops(m, 'root').down).not.toBeNull();
    expect(lineageStops(m, 'sink')).toEqual({ up: 'mid', down: null });
    expect(lineageStops(m, 'far')).toEqual({ up: null, down: null });
  });

  it('walks a whole chain end to end and back', () => {
    const chain: Node[] = ['a', 'b', 'c', 'd'].map((id) => ({ id, status: 'todo' }));
    const m = model(chain, [
      ['a', 'b'],
      ['b', 'c'],
      ['c', 'd']
    ]);
    const forward: string[] = ['a'];
    for (let at = lineageStops(m, 'a').down; at !== null; at = lineageStops(m, at).down) {
      forward.push(at);
    }
    expect(forward).toEqual(['a', 'b', 'c', 'd']);
    const back: string[] = ['d'];
    for (let at = lineageStops(m, 'd').up; at !== null; at = lineageStops(m, at).up) back.push(at);
    expect(back).toEqual(['d', 'c', 'b', 'a']);
  });

  it('has nowhere to go with nothing selected, or off the canvas', () => {
    const m = model(SHAPE, SHAPE_EDGES);
    expect(lineageStops(m, null)).toEqual({ up: null, down: null });
    expect(lineageStops(m, 'not-drawn')).toEqual({ up: null, down: null });
  });
});
