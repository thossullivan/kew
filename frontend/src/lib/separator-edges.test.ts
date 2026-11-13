/**
 * No separator loses its space at an element's edge (design review c9, D14).
 *
 * Svelte trims the whitespace at the start and end of an element's text. So
 * `<span class="lead">Centred on #107 · </span>needs you` rendered as
 * "Centred on #107 ·needs you" on the Epic page, and the run page's
 * `<span> · attempt 2</span>` as "label· attempt 2". The spaces survive as an
 * expression (`{SEP}` from `$lib/utils`).
 *
 * The first test pins the compiler's behaviour the rule rests on, by rendering
 * the faulty shape; the second reads every component's parsed markup — the
 * text nodes the compiler will trim — rather than grepping its source.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile, parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

const SRC = fileURLToPath(new URL('../', import.meta.url));

/** A separator a sentence is joined with: middle dot, em dash, en dash. */
const LEADING = /^\s+[·—–]/;
const TRAILING = /[·—–]\s+$/;

function svelteFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return svelteFiles(path);
    return name.endsWith('.svelte') ? [path] : [];
  });
}

type Node = { type: string; data?: string; start: number; nodes?: Node[] };

type Edge = { data: string; start: number; fault: RegExp };

/** Where a node sits: its fragment's nodes, its index, and where THAT fragment's owner sits. */
type Place = { nodes: Node[]; index: number; up: Place | null };

/**
 * The node just outside `place` on one side, climbing out of the fragments it
 * is first (or last) in: what the reader sees beside the trimmed edge.
 *
 * A neighbour that is only whitespace and is itself the far edge of its own
 * fragment brings nothing: the compiler trims it too. In
 * `<p>{#if c}<span>{a} · </span> {/if}{b}</p>` the `' '` after the span is
 * the `{#if}` branch's last node, so the page reads "A ·B"; the climb goes on
 * past it to what follows the block.
 */
function outside(place: Place | null, step: -1 | 1): Node | null {
  if (place === null) return null;
  const at = place.index + step;
  if (at < 0 || at >= place.nodes.length) return outside(place.up, step);
  const node = place.nodes[at];
  const farEdge = step === 1 ? at === place.nodes.length - 1 : at === 0;
  if (farEdge && node.type === 'Text' && /^\s*$/.test(node.data ?? '')) return outside(place.up, step);
  return node;
}

/** Whether that neighbour supplies the space the edge lost. */
function spaced(node: Node | null, step: -1 | 1): boolean {
  if (node?.type !== 'Text' || node.data === undefined) return false;
  return step === -1 ? /\s$/.test(node.data) : /^\s/.test(node.data);
}

/** Every `Fragment` under `value`, each with the place of the node that owns it. */
function eachFragment(value: unknown, owner: Place | null, visit: (f: Node, owner: Place | null) => void): void {
  if (value === null || typeof value !== 'object') return;
  if (Array.isArray(value)) return value.forEach((v) => eachFragment(v, owner, visit));
  const n = value as Node;
  if (n.type === 'Fragment') return visit(n, owner);
  for (const child of Object.values(n)) eachFragment(child, owner, visit);
}

/**
 * Every separator a fragment's edge trims and nothing beside it puts back.
 *
 * The compiler trims the whitespace at both edges of every fragment: an
 * element's children, a component's children, and each branch of an `{#if}`,
 * `{#each}`, `{#await}`, `{#key}` or snippet (the first test pins the element,
 * `{#if}` and component cases). The parser gives each of them a `Fragment`,
 * so every one is read here. A trimmed edge is a fault only where the node
 * outside it does not bring a space of its own: `{a}\n{#if b} · {b}{/if}`
 * still reads "a · b", `{a}{#if b} · {b}{/if}` reads "a· b". A separator
 * alone in its fragment (`<span aria-hidden="true">·</span>`) is a mark the
 * layout spaces, not a joint in a sentence, and is left alone.
 */
function edgeTexts(fragment: { nodes?: unknown[] }, owner: Place | null = null, out: Edge[] = []): Edge[] {
  const kids = (fragment.nodes ?? []) as Node[];
  if (kids.length > 0) {
    for (const [edge, fault, step] of [
      [kids[0], LEADING, -1],
      [kids[kids.length - 1], TRAILING, 1]
    ] as const) {
      if (edge.type !== 'Text' || edge.data === undefined) continue;
      if (kids.length === 1 && /^\s*[·—–]\s*$/.test(edge.data)) continue;
      if (spaced(outside(owner, step), step)) continue;
      out.push({ data: edge.data, start: edge.start, fault });
    }
  }
  kids.forEach((kid, index) => {
    const here: Place = { nodes: kids, index, up: owner };
    for (const value of Object.values(kid)) eachFragment(value, here, (f, o) => edgeTexts(f, o, out));
  });
  return out;
}

describe('separators at an element edge', () => {
  it('lose their space when written as text, and keep it as an expression', () => {
    const render = (markup: string) =>
      compile(`<script>let id = '#107';</script>${markup}`, { generate: 'server' }).js.code;
    expect(render('<p><span>Centred on {id} · </span>needs you</p>')).toContain('Centred on #107 ·</span>');
    expect(render(`<p><span>Centred on {id}{' · '}</span>needs you</p>`)).not.toContain('·</span>');
    // The same trim at the edge of an `{#if}` branch and of a component's children.
    expect(render('<p>a{#if id} · {id}{/if}</p>')).not.toContain(' · #107');
    expect(render(`<p>a{#if id}{' · '}{id}{/if}</p>`)).toContain(' · ');
    // A space that is itself a block's last node is trimmed as well: the span
    // ends "·" and nothing puts the space back before what follows the block.
    for (const block of ['{#if id}<span>{id} · </span> {/if}', '{#each [1] as i}<span>{i} · </span> {/each}']) {
      const code = render(`<p>${block}b</p>`);
      expect(code).toContain('·</span>');
      expect(code).not.toMatch(/·<\/span> /);
    }
  });

  it('finds an edge separator in a block and in a component child', () => {
    const faults = (markup: string) =>
      edgeTexts(parse(`<script>import C from './C.svelte'; let id = 1;</script>${markup}`, { modern: true }).fragment)
        .filter((t) => t.fault.test(t.data))
        .map((t) => t.data.trim());
    expect(faults('<p>a{#if id} · {id}{/if}</p>')).toEqual(['·']);
    expect(faults('<C>{id} · </C>')).toEqual(['·']);
    expect(faults('<p>{#each [1] as i} — {i}{/each}</p>')).toEqual(['—']);
    expect(faults(`<p>a{#if id}{' · '}{id}{/if}</p>`)).toEqual([]);
    // A space outside the trimmed edge puts it back: no fault.
    expect(faults('<p>{id}\n  {#if id} · {id}{/if}</p>')).toEqual([]);
    expect(faults('<p><span>{id} · </span> next</p>')).toEqual([]);
    // ...unless that space is itself the far edge of its own fragment, which
    // the compiler trims too (the second test pins the rendering).
    expect(faults('<p>{#if id}<span>{id} · </span> {/if}{id}</p>')).toEqual(['·']);
    expect(faults('<p>{#each [1] as i}<span>{i} · </span> {/each}{id}</p>')).toEqual(['·']);
    expect(faults('<p>{id} {#if id} · {id}{/if}</p>')).toEqual([]);
  });

  it('are never written as edge text in a component', () => {
    const faults: string[] = [];
    for (const file of svelteFiles(SRC)) {
      const source = readFileSync(file, 'utf8');
      const ast = parse(source, { modern: true });
      for (const text of edgeTexts(ast.fragment)) {
        if (text.fault.test(text.data)) {
          const line = source.slice(0, text.start).split('\n').length;
          faults.push(`${relative(SRC, file)}:${line} ${JSON.stringify(text.data.trim())}`);
        }
      }
    }
    expect(faults).toEqual([]);
  });
});
