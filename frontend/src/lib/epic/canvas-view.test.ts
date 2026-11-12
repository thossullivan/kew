/**
 * #2250 — where the DAG canvas looks and where its popover goes.
 *
 * Every one of these was wrong in the browser before it was written down:
 * the needs-you node opened off screen, a small epic did not read at a glance,
 * a card was cut in half by the left edge, and the popover landed on top of
 * the very card it was about.
 */
import { describe, expect, it } from 'vitest';
import type { EpicNodeAttention } from '$lib/api';
import goLiveJson from '../../../fixtures/factory/epics-go-live-2026-09.json';
import { needsYou } from './attention';
import {
  boxOf,
  clearLeftEdge,
  FIT_PAD,
  fitAllView,
  firstFrame,
  FIRST_FRAME_MIN_ZOOM,
  fitZoom,
  FOCUS_MIN_ZOOM,
  FRAME_PAD,
  hasMinimap,
  MINIMAP_INSET,
  MINIMAP_MIN_W,
  MINIMAP_H,
  MINIMAP_W,
  MAX_ZOOM,
  MIN_ZOOM,
  onScreen,
  padClearOfMinimap,
  panForPopover,
  placePopover,
  POPOVER_GAP,
  READABLE_ZOOM,
  reveal,
  RIM_GAP,
  upstreamGenerations,
  viewFor,
  wholeBox,
  zoomBy,
  type Box
} from './canvas-view';
import { NODE_H, NODE_W, resetDagreCache } from './dagre-layout';
import { buildFlowModel, type FlowModel } from './flow-model';

type Node = { id: string; status: string; attention?: EpicNodeAttention | null; stuck?: string | null };

const HOLD = { kind: 'held', reason: 'preserved worktree', url: null } as EpicNodeAttention;

function model(nodes: Node[], edges: [string, string][]): FlowModel<Node> {
  resetDagreCache();
  return buildFlowModel(nodes, edges, { showDone: true });
}

/** A long chain feeding a fan: wide AND tall, so it cannot read whole in a
 * 1440x480 canvas, with one held node at its end. */
function bigEpic(): FlowModel<Node> {
  const nodes: Node[] = [];
  const edges: [string, string][] = [];
  for (let i = 0; i < 8; i++) nodes.push({ id: `c${i}`, status: 'done' });
  for (let i = 1; i < 8; i++) edges.push([`c${i - 1}`, `c${i}`]);
  for (let i = 0; i < 10; i++) {
    nodes.push({ id: `f${i}`, status: 'done' });
    edges.push(['c0', `f${i}`]);
  }
  nodes.push({ id: 'held', status: 'held', attention: HOLD });
  edges.push(['c7', 'held']);
  return model(nodes, edges);
}

/** A single fan 24 cards deep: taller than any canvas can fit at `MIN_ZOOM`,
 * so a whole-graph fit CLAMPS there instead of shrinking to fit. */
function tallEpic(): FlowModel<Node> {
  const nodes: Node[] = [{ id: 'root', status: 'done' }];
  const edges: [string, string][] = [];
  for (let i = 0; i < 24; i++) {
    nodes.push({ id: `f${i}`, status: 'done' });
    edges.push(['root', `f${i}`]);
  }
  return model(nodes, edges);
}

const GO_LIVE = model(
  goLiveJson.snapshot.nodes.map((n) => ({
    id: n.id,
    status: n.status,
    attention: n.attention as EpicNodeAttention | null
  })),
  goLiveJson.snapshot.edges.map((e) => [e[0], e[1]] as [string, string])
);

const cardOf = (m: FlowModel<Node>, id: string): Box => {
  const n = m.nodes.find((x) => x.id === id)!;
  return { x: n.position.x, y: n.position.y, width: NODE_W, height: NODE_H };
};

/** Fully inside the canvas, with the frame's padding kept clear. */
function inFrame(s: Box, w: number, h: number): boolean {
  return (
    s.x >= FRAME_PAD.left - 1 &&
    s.y >= FRAME_PAD.top - 1 &&
    s.x + s.width <= w - FRAME_PAD.right + 1 &&
    s.y + s.height <= h - FRAME_PAD.bottom + 1
  );
}

describe('the first frame', () => {
  it('fits a small epic whole, at no more than the cards` own size', () => {
    const m = model(
      [
        { id: 'a', status: 'done' },
        { id: 'b', status: 'in_review', attention: HOLD },
        { id: 'c', status: 'done' }
      ],
      [
        ['a', 'b'],
        ['c', 'b']
      ]
    );
    const f = firstFrame(m, 1440, 480)!;
    expect(f.focus).toBeNull();
    expect(f.view.zoom).toBe(1);
    for (const n of m.nodes) expect(inFrame(onScreen(cardOf(m, n.id), f.view), 1440, 480)).toBe(true);
  });

  it('centres a big epic on the node that needs you, readable, on screen', () => {
    const m = bigEpic();
    for (const [w, h] of [
      [1440, 480],
      [1280, 400]
    ]) {
      expect(fitZoom(wholeBox(m), w, h)).toBeLessThan(READABLE_ZOOM);
      const f = firstFrame(m, w, h)!;
      expect(f.focus).toBe('held');
      expect(f.view.zoom).toBeGreaterThanOrEqual(FOCUS_MIN_ZOOM);
      expect(inFrame(onScreen(cardOf(m, 'held'), f.view), w, h)).toBe(true);
      // And its parent, the first step of the upstream path, with it.
      expect(inFrame(onScreen(cardOf(m, 'c7'), f.view), w, h)).toBe(true);
    }
  });

  it('opens a `?node=` link on that node instead', () => {
    const m = bigEpic();
    const f = firstFrame(m, 1440, 480, 'f9')!;
    expect(f.focus).toBe('f9');
    expect(inFrame(onScreen(cardOf(m, 'f9'), f.view), 1440, 480)).toBe(true);
  });

  it('ignores a `?node=` the canvas does not draw', () => {
    expect(firstFrame(bigEpic(), 1440, 480, 'ghost')!.focus).toBe('held');
  });

  it('fits a big epic nobody is waiting on whole only while it reads at the floor', () => {
    // #2543, D28: go-live opened fitted whole at 20%, where no card reads.
    // D37: and the floor is the cards' own size, 100%.
    const chain = (n: number) =>
      model(
        Array.from({ length: n }, (_, i) => ({ id: `n${i}`, status: 'done' })),
        Array.from({ length: n - 1 }, (_, i) => [`n${i}`, `n${i + 1}`] as [string, string])
      );
    const short = chain(3);
    expect(fitAllView(short, 1440, 480).zoom).toBeGreaterThanOrEqual(FIRST_FRAME_MIN_ZOOM);
    expect(firstFrame(short, 1440, 480)).toEqual({
      view: fitAllView(short, 1440, 480),
      focus: null,
      why: null
    });

    // Whole at 74%: that used to open fitted whole, below the cards' size.
    const middling = chain(8);
    expect(fitZoom(wholeBox(middling), 1440, 480, FIT_PAD)).toBeLessThan(READABLE_ZOOM);
    const m = firstFrame(middling, 1440, 480)!;
    expect(m.view.zoom).toBe(1);
    expect([m.focus, m.why]).toEqual(['n0', 'start']);

    const long = chain(30);
    expect(fitAllView(long, 1440, 480).zoom).toBeLessThan(FIRST_FRAME_MIN_ZOOM);
    const f = firstFrame(long, 1440, 480)!;
    expect(f.view.zoom).toBeGreaterThanOrEqual(FIRST_FRAME_MIN_ZOOM);
    // Nothing to focus on, so it opens on the graph's start, and says so.
    expect(f.focus).toBe('n0');
    expect(f.why).toBe('start');
    expect(inFrame(onScreen(cardOf(long, 'n0'), f.view), 1440, 480)).toBe(true);
    // "Fit all" (F) is still the whole graph, at whatever zoom that takes.
    expect(fitAllView(long, 1440, 480).zoom).toBeGreaterThanOrEqual(MIN_ZOOM);
  });

  it('opens on the held node, then the stalled frontier, then the ready one', () => {
    const fan = (nodes: Node[]) =>
      model(
        [{ id: 'root', status: 'done' }, ...nodes],
        nodes.map((n) => ['root', n.id] as [string, string])
      );
    const wide = (extra: Node[]) => [
      ...Array.from({ length: 40 }, (_, i) => ({ id: `t${i}`, status: 'todo' })),
      ...extra
    ];
    const held = fan(wide([{ id: 'h', status: 'held' }]));
    let f = firstFrame(held, 1440, 480, null, new Set(['t3']))!;
    expect([f.focus, f.why]).toEqual(['h', 'held']);
    expect(f.view.zoom).toBeGreaterThanOrEqual(FIRST_FRAME_MIN_ZOOM);

    const stuck = fan(wide([{ id: 's', status: 'todo', stuck: 'waiting on a slot' } as Node]));
    f = firstFrame(stuck, 1440, 480, null, new Set(['t3', 's']))!;
    expect([f.focus, f.why]).toEqual(['s', 'stalled']);

    const ready = fan(wide([]));
    f = firstFrame(ready, 1440, 480, null, new Set(['t3']))!;
    expect([f.focus, f.why]).toEqual(['t3', 'frontier']);
    expect(f.view.zoom).toBeGreaterThanOrEqual(FIRST_FRAME_MIN_ZOOM);
    expect(inFrame(onScreen(cardOf(ready, 't3'), f.view), 1440, 480)).toBe(true);
  });

  it('opens the go-live epic readable, at zoom >= 0.6, at every canvas size', () => {
    for (const [w, h] of [
      [1440, 480],
      [1440, 300],
      [1280, 400],
      [390, 300]
    ]) {
      expect(firstFrame(GO_LIVE, w, h)!.view.zoom, `${w}x${h}`).toBeGreaterThanOrEqual(0.6);
    }
  });

  it('never opens below 100%, whatever the graph and the canvas (D37)', () => {
    const chain = (n: number) =>
      model(
        Array.from({ length: n }, (_, i) => ({ id: `n${i}`, status: 'done' })),
        Array.from({ length: n - 1 }, (_, i) => [`n${i}`, `n${i + 1}`] as [string, string])
      );
    const sizes = [
      [1440, 900],
      [1440, 480],
      [1280, 400],
      [768, 400],
      [390, 400],
      [390, 300]
    ];
    for (const m of [GO_LIVE, bigEpic(), chain(4), chain(8), chain(30)]) {
      for (const [w, h] of sizes) {
        const f = firstFrame(m, w, h)!;
        expect(f.view.zoom, `${w}x${h}`).toBeGreaterThanOrEqual(1);
        // The node it is about is on screen, at that size.
        if (f.focus !== null) {
          const s = onScreen(cardOf(m, f.focus), f.view);
          expect(s.x >= 0 && s.x + s.width <= w, `${f.focus} at ${w}x${h}`).toBe(true);
        }
      }
    }
    // A needs-you path that does not fit at 100% is centred on its node.
    const f = firstFrame(GO_LIVE, 1440, 480)!;
    expect(f.why).toBe('needs_you');
    expect(f.view.zoom).toBe(1);
  });

  it('keeps the framed path clear of the minimap corner when there is one', () => {
    const m = bigEpic();
    const [w, h] = [1280, 400];
    expect(hasMinimap(m, w, h)).toBe(true);
    const f = firstFrame(m, w, h)!;
    const s = onScreen(cardOf(m, 'held'), f.view);
    expect(s.x + s.width).toBeLessThanOrEqual(w - 16 - MINIMAP_W);
  });

  it('draws no minimap for a small graph that is merely a little zoomed out', () => {
    const four = model(
      ['a', 'b', 'c', 'd'].map((id) => ({ id, status: 'held' })),
      []
    );
    expect(hasMinimap(four, 1440, 447)).toBe(false);
  });

  it('cuts no card in half at the left edge (the go-live epic)', () => {
    for (const [w, h] of [
      [1440, 480],
      [1280, 400]
    ]) {
      const f = firstFrame(GO_LIVE, w, h)!;
      expect(f.focus).not.toBeNull();
      expect(needsYou(GO_LIVE.nodes.find((n) => n.id === f.focus)!.node!)).toBe(true);
      for (const n of GO_LIVE.nodes) {
        const s = onScreen(cardOf(GO_LIVE, n.id), f.view);
        expect(s.x < 0 && s.x + s.width > 0, n.id).toBe(false);
      }
    }
  });

  it('has nothing to frame on an empty graph or a canvas with no size', () => {
    expect(firstFrame(model([], []), 1440, 480)).toBeNull();
    expect(firstFrame(bigEpic(), 0, 480)).toBeNull();
  });
});

describe('the upstream path, one generation at a time', () => {
  it('walks parents, then theirs, over the edges the model draws', () => {
    const m = model(
      [
        { id: 'a', status: 'done' },
        { id: 'b', status: 'done' },
        { id: 'x', status: 'done' },
        { id: 'c', status: 'todo' }
      ],
      [
        ['a', 'b'],
        ['b', 'c'],
        ['x', 'c']
      ]
    );
    expect(upstreamGenerations(m, 'c').map((g) => [...g].sort())).toEqual([['c'], ['b', 'x'], ['a']]);
  });
});

describe('clearLeftEdge', () => {
  it('brings a cut column in, or pushes it out, whichever is less', () => {
    const m = bigEpic();
    const box = boxOf(m.nodes.filter((n) => n.id === 'held'));
    // A view whose left edge cuts the first column 20px in.
    const v = { x: -20 - m.layers[0].x, y: 0, zoom: 1 };
    const out = clearLeftEdge(m, v, box, 4000);
    const left = m.layers[0].x * out.zoom + out.x;
    expect(left === 0 || left + NODE_W <= 0).toBe(true);
  });
});

describe('zoom and reveal', () => {
  it('zooms about the canvas centre and clamps', () => {
    const v = zoomBy({ x: 100, y: 50, zoom: 1 }, 1.5, 1000, 600);
    expect(v.zoom).toBe(1.5);
    // The canvas centre maps to the same flow point before and after.
    expect((500 - 100) / 1).toBeCloseTo((500 - v.x) / v.zoom);
    expect(zoomBy(v, 10, 1000, 600).zoom).toBe(MAX_ZOOM);
    expect(zoomBy(v, 0.001, 1000, 600).zoom).toBe(MIN_ZOOM);
  });

  it('leaves a visible card alone and pans an off-screen one in, least movement', () => {
    const box = { x: 100, y: 100, width: NODE_W, height: NODE_H };
    expect(reveal(box, { x: 0, y: 0, zoom: 1 }, 1440, 480)).toBeNull();
    const v = reveal(box, { x: 1400, y: 0, zoom: 1 }, 1440, 480)!;
    const s = onScreen(box, v);
    expect(s.x + s.width).toBe(1440 - FRAME_PAD.right);
  });
});

describe('the popover`s place', () => {
  const size = { width: 300, height: 260 };
  const node = { x: 400, y: 100, width: NODE_W, height: NODE_H };

  it('goes beside its node when that side is free, with no leader', () => {
    const p = placePopover(node, size, [], [], 1440, 480);
    expect(p).toMatchObject({ x: 400 + NODE_W + POPOVER_GAP, y: 100, clear: true, leader: null });
  });

  it('takes the free side when a neighbour blocks the right', () => {
    const right = { x: 400 + NODE_W + 40, y: 100, width: NODE_W, height: NODE_H };
    const p = placePopover(node, size, [right], [], 1440, 480);
    expect(p.clear).toBe(true);
    const box = { x: p.x, y: p.y, ...size };
    expect(box.x + box.width <= right.x || box.y >= right.y + right.height || box.y + box.height <= right.y).toBe(true);
  });

  it('never sits on the card it is about, even with no clear side', () => {
    // A canvas too small for the popover anywhere but over something.
    const crowd = [
      { x: 0, y: 0, width: 380, height: 480 },
      { x: 620, y: 0, width: 400, height: 480 }
    ];
    const p = placePopover(node, size, crowd, [], 1000, 480);
    const box = { x: p.x, y: p.y, ...size };
    const overlapsNode =
      box.x < node.x + node.width && node.x < box.x + box.width &&
      box.y < node.y + node.height && node.y < box.y + box.height;
    expect(overlapsNode).toBe(false);
  });

  it('stays on the canvas', () => {
    const edge = { x: 1300, y: 400, width: NODE_W, height: NODE_H };
    const p = placePopover(edge, size, [], [], 1440, 480);
    expect(p.x).toBeGreaterThanOrEqual(0);
    expect(p.y).toBeGreaterThanOrEqual(0);
    expect(p.x + size.width).toBeLessThanOrEqual(1440);
    expect(p.y + size.height).toBeLessThanOrEqual(480);
  });

  it('pans to make room beside a node at the rim, and only as far as needed', () => {
    // The only free side is the right, and the canvas rim cuts it off.
    const atRim = { x: 1180, y: 100, width: NODE_W, height: NODE_H };
    const left = [{ x: 700, y: 0, width: 460, height: 400 }];
    expect(placePopover(atRim, size, left, [], 1440, 400).clear).toBe(false);
    const pan = panForPopover(atRim, size, left, [], 1440, 400)!;
    // No further than the right-hand spot would need; a nearer free spot
    // (below, pulled in from the rim) wins when there is one.
    const rightNeeds = 1440 - 8 - (atRim.x + NODE_W + POPOVER_GAP + size.width);
    expect(Math.hypot(pan.dx, pan.dy)).toBeLessThanOrEqual(Math.abs(rightNeeds));
    expect(pan.dx).toBeLessThan(0);
    // A pan moves the cards with the node.
    const shift = (b: Box): Box => ({ ...b, x: b.x + pan.dx, y: b.y + pan.dy });
    expect(placePopover(shift(atRim), size, left.map(shift), [], 1440, 400).clear).toBe(true);
  });

  it('needs no pan when a side is already free', () => {
    expect(panForPopover(node, size, [], [], 1440, 480)).toEqual({ dx: 0, dy: 0 });
  });

  it('does not pan when every side of the node is taken', () => {
    const around = [
      { x: node.x + NODE_W + 20, y: 0, width: 400, height: 480 },
      { x: 0, y: 0, width: node.x - 20, height: 480 },
      { x: node.x - 40, y: node.y + NODE_H + 20, width: 400, height: 200 },
      { x: node.x - 40, y: 0, width: 400, height: node.y - 20 }
    ];
    expect(panForPopover(node, size, around, [], 1440, 480)).toBeNull();
  });
});

/**
 * D19 (design review C2) — the zoom controls and the keyboard legend sat on
 * top of cards and cut their status rows off.
 *
 * Reserving their band as padding was the first answer, and it held only for
 * the frames that are FITS: the operator's browser pass at 1024x700 found the
 * bar and the legend over two cards of a CROPPED frame, where the graph is
 * drawn at 100% and runs past every edge of a short canvas. Nothing a pad can
 * say empties a floating band there — it is 34px tall and the layout's gaps
 * are 16px, so no slide puts it in one.
 *
 * So the bar is not in the node area at all now: `DagView` draws it as a row
 * below the flow (pinned in dag-view.test.ts, which is where that claim can be
 * made), and the flow clips at its own foot. What is left for these claims is
 * the geometry that remains — the rim, and the minimap, which is the one
 * overlay DESIGN.md still draws over the graph.
 */
describe('a frame keeps its cards off the rim and out of the map`s corner (D19)', () => {
  /** The minimap, in node-area pixels, as `DagView` and the flow draw it. */
  const minimap = (w: number, h: number): Box => ({
    x: w - MINIMAP_INSET - MINIMAP_W,
    y: h - MINIMAP_INSET - MINIMAP_H,
    width: MINIMAP_W,
    height: MINIMAP_H
  });

  const intersects = (a: Box, b: Box): boolean =>
    a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

  /** Canvas sizes the browser pass reads at, and the SHORT ones a pinned rail
   * leaves of them: 1024x700 is where AC1 failed, and the canvas there is a
   * few hundred pixels, not 700. */
  const SIZES: [number, number][] = [
    [1440, 424],
    [1280, 360],
    [1024, 300],
    [1024, 240],
    [960, 360],
    [390, 300]
  ];

  it('reserves the rim in both pads, and no band of chrome in either', () => {
    // The chrome is outside the node area, so a pad that still reserved 66px
    // for it would be shrinking the graph for a bar that is not there.
    expect(FIT_PAD.bottom).toBe(RIM_GAP);
    expect(FRAME_PAD.bottom).toBe(RIM_GAP);
    expect(FRAME_PAD.top).toBe(RIM_GAP);
  });

  it('leaves no card under the minimap in a whole-graph fit', () => {
    for (const [w, h] of SIZES) {
      for (const m of [GO_LIVE, bigEpic(), tallEpic()]) {
        if (!hasMinimap(m, w, h)) continue;
        const view = fitAllView(m, w, h);
        for (const n of m.nodes) {
          expect(intersects(onScreen(cardOf(m, n.id), view), minimap(w, h)), n.id).toBe(false);
        }
      }
    }
  });

  it('keeps it clear when the fit clamps at MIN_ZOOM and cannot fit at all', () => {
    // The clearance the pad encodes is a GUARANTEE, not a by-product of the
    // box happening to fit: a graph too tall to fit even at `MIN_ZOOM` clamps
    // there, and centring what is left over hung its lowest cards in the
    // corner the map is drawn in. The overflow goes off the top instead,
    // which is clear now the frame's caption is in the rail.
    const m = tallEpic();
    for (const [w, h] of SIZES) {
      const view = fitAllView(m, w, h);
      expect(view.zoom).toBe(MIN_ZOOM);
      const pad = padClearOfMinimap(FIT_PAD, m, w, h);
      // Really overflowing: the premise of the case, not just a tall graph.
      expect(onScreen(wholeBox(m), view).height).toBeGreaterThan(h - pad.top - pad.bottom);
      if (!hasMinimap(m, w, h)) continue;
      for (const n of m.nodes) {
        expect(intersects(onScreen(cardOf(m, n.id), view), minimap(w, h)), n.id).toBe(false);
      }
    }
  });

  it('keeps the node a cropped first frame is ABOUT inside the frame, on a short canvas', () => {
    // A crop draws the graph at its own size, so cards the frame is not about
    // carry on past every edge — and on a short canvas there are plenty of
    // them. What the frame owes the reader is the node it opened for: inside
    // the padded frame, and clear of the map.
    for (const [w, h] of SIZES) {
      for (const m of [GO_LIVE, bigEpic()]) {
        const f = firstFrame(m, w, h)!;
        if (f.focus === null) continue;
        const pad = padClearOfMinimap(FRAME_PAD, m, w, h);
        const card = onScreen(cardOf(m, f.focus), f.view);
        expect(card.x, `${w}x${h}`).toBeGreaterThanOrEqual(pad.left / 2 - 1);
        expect(card.y + card.height, `${w}x${h}`).toBeLessThanOrEqual(h - pad.bottom + 1);
        // Below 1024 no map is drawn at all (D5), so there is no corner to
        // keep off; above it, the frame is about a node and it is not the map's.
        if (hasMinimap(m, w, h)) expect(intersects(card, minimap(w, h)), `${w}x${h}`).toBe(false);
      }
    }
  });

  it('keeps the selection clear of the map when the keyboard walks there', () => {
    const [w, h] = [1280, 360];
    const m = GO_LIVE;
    // A view panned so the lowest card is in the map's corner: `reveal` is
    // what brings it back, and it must bring it back clear of the map.
    const low = m.nodes.reduce((a, b) => (a.position.y > b.position.y ? a : b));
    const view = { x: w - MINIMAP_W, y: h - MINIMAP_INSET - low.position.y, zoom: 1 };
    const pad = padClearOfMinimap(FIT_PAD, m, w, h);
    const next = reveal(cardOf(m, low.id), view, w, h, pad) ?? view;
    expect(intersects(onScreen(cardOf(m, low.id), next), minimap(w, h))).toBe(false);
  });
});

describe('the minimap below 1024 (D5)', () => {
  it('is not drawn on a canvas too narrow to spare the corner', () => {
    const m = bigEpic();
    // Wide enough, and the graph is too big to read whole: a map.
    expect(hasMinimap(m, MINIMAP_MIN_W, 400)).toBe(true);
    // A phone canvas: the map would cover a third of it.
    for (const w of [390, 768, MINIMAP_MIN_W - 1]) expect(hasMinimap(m, w, 400)).toBe(false);
  });
});
