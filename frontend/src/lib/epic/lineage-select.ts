// Lineage selection (#2544), Dagster's asset-selection grammar over an epic:
//
//   +#2469     #2469 and its parents           (one `+` is one layer)
//   #2469+     #2469 and its children
//   +#2469+    both
//   ++#2469    two layers up; `#2469++` two down
//
// Pure, and the grammar's only home: the epic graph's selection box parses
// with it today, and the CLI is meant to accept the same text later, so
// nothing here knows about Svelte, the URL or the canvas.
import type { FlowModel, Lineage } from './flow-model';

/** A parsed selection: the node it is rooted at and how far it reaches. */
export type LineageSelection = {
  /** The node id, as the snapshot spells it (`2469`, no `#`). */
  readonly id: string;
  /** Layers upstream — the number of leading `+`. */
  readonly up: number;
  /** Layers downstream — the number of trailing `+`. */
  readonly down: number;
};

/** A selection, nothing (an empty box), or the sentence that says why not. */
export type SelectionParse =
  | { readonly ok: true; readonly selection: LineageSelection | null }
  | { readonly ok: false; readonly error: string };

const FORM = /^(\+*)#?(\d+)(\+*)$/;

/**
 * Read the text of a selection box or a `?select=` param.
 *
 * Blank is no selection, not an error: clearing the box is how a reader drops
 * one. Anything else that is not the grammar answers a sentence naming what
 * was typed and the forms that work, because a box that silently ignores what
 * was typed reads as broken.
 */
export function parseSelection(text: string): SelectionParse {
  const raw = text.trim();
  if (raw === '') return { ok: true, selection: null };
  const m = FORM.exec(raw);
  if (m === null) {
    return {
      ok: false,
      error: `"${raw}" is not a selection: write +#N for upstream, #N+ for downstream or +#N+ for both.`
    };
  }
  const [, before, id, after] = m;
  if (before === '' && after === '') {
    return {
      ok: false,
      error: `#${id} selects no lineage: put + before it for upstream or after it for downstream.`
    };
  }
  return { ok: true, selection: { id, up: before.length, down: after.length } };
}

/** The canonical text of a selection: what the box shows and the URL carries. */
export function formatSelection(s: LineageSelection): string {
  return `${'+'.repeat(s.up)}#${s.id}${'+'.repeat(s.down)}`;
}

/**
 * The nodes and edges a selection keeps at full weight on `model`: the root,
 * then `up` layers of parents and `down` layers of children, with the edges
 * walked to reach them. Null when the model does not draw the root, which is
 * the caller's to say rather than a canvas dimmed around nothing.
 *
 * A layer is one edge, not one column of the layout: a dependency that skips
 * columns is still a direct parent, which is how Dagster counts `+` too.
 */
export function selectLineage<T>(model: FlowModel<T>, s: LineageSelection): Lineage | null {
  if (!model.nodes.some((n) => n.id === s.id)) return null;
  const nodes = new Set<string>([s.id]);
  const edges = new Set<string>();
  for (const [dir, depth] of [['up', s.up], ['down', s.down]] as const) {
    let ring = [s.id];
    const seen = new Set([s.id]);
    for (let step = 0; step < depth && ring.length; step++) {
      const next: string[] = [];
      for (const e of model.edges) {
        const [near, far] = dir === 'down' ? [e.source, e.target] : [e.target, e.source];
        if (!ring.includes(near)) continue;
        edges.add(e.id);
        nodes.add(far);
        if (!seen.has(far)) {
          seen.add(far);
          next.push(far);
        }
      }
      ring = next;
    }
  }
  return { nodes, edges };
}

/**
 * `model` cut down to a selection, for the keyboard to walk (#2544: the arrow
 * keys walk within the selection). Rows are renumbered within each layer, so
 * ↑ / ↓ step to the next SELECTED node in the column rather than stopping at
 * a gap where a dimmed one sits.
 */
export function withinSelection<T>(model: FlowModel<T>, keep: Lineage): FlowModel<T> {
  const rows = new Map<number, number>();
  const nodes = model.nodes
    .filter((n) => keep.nodes.has(n.id))
    .sort((a, b) => a.layer - b.layer || a.row - b.row)
    .map((n) => {
      const row = rows.get(n.layer) ?? 0;
      rows.set(n.layer, row + 1);
      return { ...n, row };
    });
  const edges = model.edges.filter((e) => keep.edges.has(e.id));
  return { ...model, nodes, edges };
}
