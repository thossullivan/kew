/**
 * What survives `metro.ts` after #2250: what an edge's stroke MEANS, and which
 * nodes belong in the graph at all.
 *
 * The 650-line hand-rolled layout this module used to hold — its pitch clamps,
 * its channels, its packing, its path strings — is gone, and so are the ~450
 * lines of this suite that proved it. The replacement geometry is dagre's, and
 * `dagre-layout.test.ts` and `flow-model.test.ts` hold it to the same
 * invariants (no overlapping boxes, deterministic positions per topology,
 * model order inside a layer, every edge joining its own two endpoints).
 *
 * The fixture below stays as it was: the DEpic artboard's own graph, which is
 * still what both surviving functions are read against.
 */
import { describe, expect, it } from 'vitest';
import type { EpicNode } from '$lib/api';
import { edgeStroke, edgeStyle, splitStandalone } from './metro';

const node = (id: string, status = 'todo', title = 't'): EpicNode => ({
  id, title, status, status_since: null, url: null, pr_url: null,
  external: false, stuck: null, attention: null, blocked_reason: null
});

// ── The DEpic fixture ──────────────────────────────────────────────────────
// frontend/design/factory-ui/DEpic.html: the twelve graph nodes, the eight
// done orphans in the strip below it, and the edge list from the graph's
// aria-label. Statuses are the ones the artboard captions.
const GRAPH_NODES: EpicNode[] = [
  node('2000', 'done', 'W4.3 renames'),
  node('2001', 'done', 'W4.4 grammar'),
  node('2040', 'todo', 'docs waiting on snapshot'),
  node('2021', 'done', 'review verdict from the stream'),
  node('2047', 'done', 'review gate trailing notices'),
  node('2055', 'in_review', 'review gate lows'),
  node('2053', 'blocked', 'stderr noise'),
  node('1969', 'done', 'pre-push credential scan'),
  node('2054', 'todo', 'key-shape cap'),
  node('2058', 'todo', 'dispatch dry run'),
  node('2060', 'todo', 'changed-files .venv'),
  node('2033', 'in_progress', 'agent-PR CI gate')
];
const STRIP_NODES: EpicNode[] = [
  node('1117', 'done', 'Linear launch gate'),
  node('1994', 'done', 'public-ready workflows'),
  node('1870', 'done', 'npm advisories'),
  node('2002', 'done', 'first-run blockers'),
  node('2031', 'done', 'CodeRabbit opt-in'),
  node('2030', 'done', 'docs truth'),
  node('1803', 'done', 'governor swap + ci-fix budget'),
  node('2045', 'done', 'hotfix classify')
];
const EDGES: [string, string][] = [
  ['2001', '2040'], ['2021', '2040'], ['1969', '2054'], ['1969', '2058'],
  ['1969', '2060'], ['2047', '2053'], ['2000', '2001'], ['2021', '2047'],
  ['2047', '2055']
];
const STATUSES: Record<string, string> = Object.fromEntries(
  [...GRAPH_NODES, ...STRIP_NODES].map(n => [n.id, n.status])
);

const ALL_STATUSES = [
  'todo', 'in_progress', 'in_review', 'done', 'failed', 'held', 'blocked', 'cancelled'
];

// The artboard's literal hexes, mapped back to the tokens they were exported
// from (frontend/src/app.css dark block).
const TEXT_5 = 'var(--text-5)'; // #5a5070
const LAVENDER = 'var(--status-blocked)'; // #b48ade
const EDGE_DONE = 'var(--edge-done)'; // #3a3052
const CYAN = 'var(--accent-2)'; // #05d9e8

describe('standalone extraction', () => {
  it('moves done orphans to the strip and keeps a live orphan in the graph', () => {
    const { graph, strip } = splitStandalone([...GRAPH_NODES, ...STRIP_NODES], EDGES);
    expect(strip.map(n => n.id)).toEqual(STRIP_NODES.map(n => n.id));
    expect(graph.map(n => n.id)).toEqual(GRAPH_NODES.map(n => n.id));
    // #2033 is isolated but in progress, so the artboard draws it in the graph
    // with its "no dependency" caption rather than hiding it in the strip.
    expect(graph.map(n => n.id)).toContain('2033');
  });

  it('keeps every non-done isolated status in the graph', () => {
    for (const status of ALL_STATUSES.filter(s => s !== 'done')) {
      const { graph, strip } = splitStandalone([node('x', status)], []);
      expect({ status, graph: graph.length, strip: strip.length })
        .toEqual({ status, graph: 1, strip: 0 });
    }
  });

  it('counts only the edges the layout keeps, so a malformed one is no link', () => {
    // A self-loop and an edge to an id outside the node set are both dropped
    // by canonicalEdges, so the layout draws nothing for them. Treating them
    // as links here left a done node in the graph as an isolated mark with no
    // edge explaining why it was there.
    const done = node('d', 'done');
    for (const edge of [['d', 'd'], ['d', 'ghost'], ['ghost', 'd']] as [string, string][]) {
      const { graph, strip } = splitStandalone([done], [edge]);
      expect({ edge, graph: graph.map(n => n.id), strip: strip.map(n => n.id) })
        .toEqual({ edge, graph: [], strip: ['d'] });
    }
    // A real edge still pins both ends into the graph.
    const other = node('e', 'done');
    const { graph, strip } = splitStandalone([done, other], [['d', 'e']]);
    expect([graph.map(n => n.id), strip]).toEqual([['d', 'e'], []]);
  });

});

describe('edgeStyle', () => {
  it('draws the artboard table', () => {
    const st: Record<string, string> = {
      d1: 'done', d2: 'done', t: 'todo', b: 'blocked', p: 'in_progress', r: 'in_review'
    };
    expect(edgeStyle('d1', 'd2', st))
      .toEqual({ kind: 'quiet', stroke: EDGE_DONE, width: 1.5, dash: null, opacity: null, order: 2 });
    expect(edgeStyle('d1', 't', st))
      .toEqual({ kind: 'pending', stroke: TEXT_5, width: 1.5, dash: '4 4', opacity: null, order: 0 });
    expect(edgeStyle('d1', 'b', st))
      .toEqual({ kind: 'blocked', stroke: LAVENDER, width: 1.5, dash: '2 4', opacity: 0.85, order: 1 });
    expect(edgeStyle('d1', 'p', st))
      .toEqual({ kind: 'live', stroke: CYAN, width: 2, dash: null, opacity: null, order: 3 });
    // #2305: the artboard draws the edge into its `in_review` node cyan, and
    // that is the one reading the motion vocabulary overrules. An open pull
    // request is waiting for a PERSON; nothing is executing behind it, so the
    // edge feeding it is a pending dependency like any other.
    expect(edgeStyle('d1', 'r', st).kind).toBe('pending');
  });

  it('is live into the two governed-review statuses the word list missed', () => {
    // `adversarial_review` (kew's own fresh-context reviewer, running) and
    // `remediating` (kew acting on its findings) are where code is executing
    // during a governed review. The old `['in_progress', 'in_review']` set had
    // never heard of either, so a review round drew as a pending dependency.
    const st = { d: 'done', a: 'adversarial_review', m: 'remediating' };
    expect(edgeStyle('d', 'a', st).kind).toBe('live');
    expect(edgeStyle('d', 'm', st).kind).toBe('live');
  });

  it('paints dashed, then quiet, then live', () => {
    const st = {
      done: 'done', settled: 'done', todo: 'todo', blocked: 'blocked',
      live: 'adversarial_review'
    };
    const orders = [
      edgeStyle('done', 'todo', st),
      edgeStyle('done', 'blocked', st),
      edgeStyle('done', 'settled', st),
      edgeStyle('done', 'live', st)
    ].map(s => s.order);
    expect(orders).toEqual([...orders].sort((a, b) => a - b));
    expect(new Set(orders).size).toBe(4);
    // Over the artboard's own graph: six pending, one blocked, two quiet and
    // — since #2305 — NO live edge. The artboard's single cyan edge fed its
    // `in_review` node, which is work waiting on a person, so the picture it
    // draws from a real snapshot now has nothing moving in it. The fixture's
    // one executing node (#2033, `in_progress`) has no edges of its own.
    // `flow-model.test.ts` holds the SORT these orders drive; this holds what
    // each of the fixture's edges earns.
    const kinds = EDGES.map(([src, dst]) => edgeStyle(src, dst, STATUSES).kind);
    expect(kinds.filter(k => k === 'pending')).toHaveLength(6);
    expect(kinds.filter(k => k === 'blocked')).toHaveLength(1);
    expect(kinds.filter(k => k === 'quiet')).toHaveLength(2);
    expect(kinds.filter(k => k === 'live')).toHaveLength(0);
  });

  it('makes live reachable from a real snapshot: a moving target is enough', () => {
    // Regression for the frontier conjunct this module used to carry. kew's
    // ready frontier is TODO-only (compute_frontier in
    // kew/src/kew/graph/driver/frontier.py), so no snapshot ever publishes a
    // moving node in it; requiring both left `live` unreachable and drew
    // every edge into moving work as `pending`.
    const st = { a: 'done', b: 'in_progress', c: 'adversarial_review' };
    expect(edgeStyle('a', 'b', st).kind).toBe('live');
    expect(edgeStyle('a', 'c', st).kind).toBe('live');
    // Neither moving status can be in a frontier, so a `live` gated on
    // frontier membership would still be unreachable.
    const governed = { ...STATUSES, '2055': 'adversarial_review' };
    expect(edgeStyle('2047', '2055', governed).kind).toBe('live');
  });

  it('is disjoint from the frontier a real driver would publish', () => {
    // The fixture's honest ready frontier, by compute_frontier's rule: TODO
    // nodes whose every dependency is done. Every live edge in the artboard
    // lands outside it, which is why frontier membership cannot be a
    // precondition for live without silencing the style entirely.
    const ready = GRAPH_NODES
      .filter(n => n.status === 'todo')
      .map(n => n.id)
      .filter(id => EDGES.filter(([, d]) => d === id)
        .every(([s]) => STATUSES[s] === 'done'));
    expect(ready).toEqual(['2040', '2054', '2058', '2060']);
    // Read against the governed-review reading of the fixture, because since
    // #2305 the artboard's own statuses draw no live edge at all (its one
    // cyan edge fed an `in_review` node) and the check would pass vacuously.
    const governed = { ...STATUSES, '2055': 'adversarial_review' };
    const liveTargets = EDGES
      .filter(([src, dst]) => edgeStyle(src, dst, governed).kind === 'live')
      .map(([, dst]) => dst);
    expect(liveTargets).toEqual(['2055']);
    expect(liveTargets.filter(id => ready.includes(id))).toEqual([]);
  });

  it('a done target off an unsettled source is pending, not history', () => {
    // A retried dependency is still a pending edge however far its target got.
    expect(edgeStyle('a', 'b', { a: 'in_progress', b: 'done' }).kind).toBe('pending');
    expect(edgeStyle('a', 'b', { a: 'failed', b: 'done' }).kind).toBe('pending');
    expect(edgeStyle('a', 'b', { a: 'cancelled', b: 'done' }).kind).toBe('quiet');
  });

  it('degrades an unknown status to pending — never quiet, never live', () => {
    expect(edgeStyle('a', 'b', { a: 'shipped', b: 'vibing' }).kind).toBe('pending');
    expect(edgeStyle('a', 'b', {}).kind).toBe('pending');
  });

  it('hands back frozen shared styles, so a renderer cannot restyle the graph', () => {
    const style = edgeStyle('a', 'b', { a: 'done', b: 'done' });
    expect(Object.isFrozen(style)).toBe(true);
  });
});


/**
 * D21 (design review C2) — "with nothing selected, some edges are cyan, and a
 * stray cyan segment runs into the zoom controls". Cyan is the selected
 * lineage (DESIGN.md dag-edge); a `live` edge borrowed it, so a canvas nobody
 * had clicked drew cyan lines standing for nothing.
 */
describe('edgeStroke — cyan is the selected lineage and nothing else', () => {
  const QUIET = edgeStyle('a', 'b', { a: 'done', b: 'done' });
  // `adversarial_review` since #2305: `in_review` is no longer a live edge,
  // and this block is about what a live one draws.
  const LIVE = edgeStyle('a', 'b', { a: 'done', b: 'adversarial_review' });
  const PENDING = edgeStyle('a', 'b', { a: 'done', b: 'todo' });
  const BLOCKED = edgeStyle('a', 'b', { a: 'done', b: 'blocked' });

  it('draws no cyan at all when nothing is selected', () => {
    for (const style of [QUIET, LIVE, PENDING, BLOCKED]) {
      for (const lit of [false, true]) {
        const stroke = edgeStroke(style, { sel: false, lit, anySelected: false });
        expect(stroke, style.kind).not.toContain('accent-2');
      }
    }
  });

  it('quiets the live edge to a hairline rather than dropping it', () => {
    // It is still an edge, and it still has to be followed: `edge-done` or
    // `hair-strong`, which is what D21 asked for.
    expect(edgeStroke(LIVE, { sel: false, lit: false, anySelected: false })).toBe(
      'var(--hair-strong)'
    );
    expect(edgeStroke(QUIET, { sel: false, lit: false, anySelected: false })).toBe(
      'var(--edge-done)'
    );
  });

  it('gives cyan to the selected lineage, and to the live edge beside it', () => {
    expect(edgeStroke(QUIET, { sel: true, lit: false, anySelected: true })).toBe(
      'var(--accent-2)'
    );
    expect(edgeStroke(LIVE, { sel: false, lit: false, anySelected: true })).toBe(LIVE.stroke);
  });

  it('lights a hovered lineage in text-2, which is not an accent', () => {
    expect(edgeStroke(QUIET, { sel: false, lit: true, anySelected: true })).toBe('var(--text-2)');
  });
});
