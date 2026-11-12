/**
 * #2544 — the lineage selection grammar (`+#N`, `#N+`, `+#N+`, `++#N`) and
 * what a selection keeps lit on a Flow model. Pure: text in, a selection or a
 * sentence out; a model in, the kept node and edge ids out.
 */
import { describe, expect, it } from 'vitest';
import { neighbor } from './dag-keys';
import { resetDagreCache } from './dagre-layout';
import { buildFlowModel, type FlowModel } from './flow-model';
import {
  formatSelection,
  parseSelection,
  selectLineage,
  withinSelection
} from './lineage-select';

describe('parseSelection', () => {
  it.each([
    ['+#2469', { id: '2469', up: 1, down: 0 }],
    ['#2469+', { id: '2469', up: 0, down: 1 }],
    ['+#2469+', { id: '2469', up: 1, down: 1 }],
    ['++#2469', { id: '2469', up: 2, down: 0 }]
  ])('reads %s', (text, selection) => {
    expect(parseSelection(text)).toEqual({ ok: true, selection });
  });

  it('round-trips each form through formatSelection', () => {
    for (const text of ['+#2469', '#2469+', '+#2469+', '++#2469', '#2469++']) {
      const parsed = parseSelection(text);
      expect(parsed.ok && parsed.selection && formatSelection(parsed.selection)).toBe(text);
    }
  });

  it('takes the id without its # and trims the box', () => {
    expect(parseSelection('  +2469 ')).toEqual({
      ok: true,
      selection: { id: '2469', up: 1, down: 0 }
    });
  });

  it('reads a blank box as no selection, not an error', () => {
    expect(parseSelection('')).toEqual({ ok: true, selection: null });
    expect(parseSelection('   ')).toEqual({ ok: true, selection: null });
  });

  it.each(['#2469', '2469'])('rejects %s, which names no lineage, with a sentence', (text) => {
    const r = parseSelection(text);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error).toBe(
        '#2469 selects no lineage: put + before it for upstream or after it for downstream.'
      );
    }
  });

  it.each(['+#', 'abc', '+#24a9', '#2469-', '+ #2469', '*#2469', '+#2469+#2470', '##2469'])(
    'rejects %s with a sentence naming what was typed',
    (text) => {
      const r = parseSelection(text);
      expect(r.ok).toBe(false);
      if (!r.ok) {
        expect(r.error).toBe(
          `"${text.trim()}" is not a selection: write +#N for upstream, #N+ for downstream or +#N+ for both.`
        );
        expect(r.error.endsWith('.')).toBe(true);
      }
    }
  );
});

type Node = { id: string; status: string };

/** a → b → c → d, plus x → c, plus b → e. */
const NODES: Node[] = ['a', 'b', 'c', 'd', 'e', 'x'].map((id) => ({ id, status: 'todo' }));
const EDGES: [string, string][] = [
  ['a', 'b'],
  ['b', 'c'],
  ['c', 'd'],
  ['x', 'c'],
  ['b', 'e']
];

function model(): FlowModel<Node> {
  resetDagreCache();
  return buildFlowModel(NODES, EDGES);
}

describe('selectLineage', () => {
  it('keeps one layer of parents for +', () => {
    const m = model();
    expect([...selectLineage(m, { id: 'c', up: 1, down: 0 })!.nodes].sort()).toEqual([
      'b',
      'c',
      'x'
    ]);
  });

  it('keeps two layers for ++, and children for a trailing +', () => {
    const m = model();
    expect([...selectLineage(m, { id: 'c', up: 2, down: 0 })!.nodes].sort()).toEqual([
      'a',
      'b',
      'c',
      'x'
    ]);
    expect([...selectLineage(m, { id: 'b', up: 0, down: 1 })!.nodes].sort()).toEqual([
      'b',
      'c',
      'e'
    ]);
    const both = selectLineage(m, { id: 'c', up: 1, down: 1 })!;
    expect([...both.nodes].sort()).toEqual(['b', 'c', 'd', 'x']);
    expect([...both.edges].sort()).toEqual(['b>c', 'c>d', 'x>c']);
  });

  it('answers null for a root the model does not draw', () => {
    expect(selectLineage(model(), { id: 'nope', up: 1, down: 1 })).toBeNull();
  });
});

describe('withinSelection', () => {
  it('walks the arrows inside the selection only', () => {
    const m = model();
    const lit = selectLineage(m, { id: 'c', up: 1, down: 0 })!;
    const sub = withinSelection(m, lit);
    expect(sub.nodes.map((n) => n.id).sort()).toEqual(['b', 'c', 'x']);
    // From b, → reaches c (in the selection) and never e (out of it).
    expect(neighbor(sub, 'b', 'right')).toBe('c');
    // From c, ← stops at a parent; there is no grandparent to walk on to.
    const up = neighbor(sub, 'c', 'left');
    expect(['b', 'x']).toContain(up);
    expect(neighbor(sub, up!, 'left')).toBeNull();
  });

  it('renumbers rows so ↑ / ↓ skip a dimmed node in the same layer', () => {
    // b's children c and e share a layer; keep c and d only and the walk
    // must still find a row 0 in every layer it lands in.
    const m = model();
    const lit = selectLineage(m, { id: 'b', up: 0, down: 2 })!;
    const sub = withinSelection(m, { nodes: new Set(['e', 'd']), edges: lit.edges });
    for (const n of sub.nodes) {
      const layer = sub.nodes.filter((o) => o.layer === n.layer).map((o) => o.row);
      expect([...layer].sort()).toEqual(layer.map((_, i) => i));
    }
    const e = m.nodes.find((n) => n.id === 'e')!;
    const c = m.nodes.find((n) => n.id === 'c')!;
    expect(e.layer).toBe(c.layer);
    expect(sub.nodes.find((n) => n.id === 'e')!.row).toBe(0);
  });
});
