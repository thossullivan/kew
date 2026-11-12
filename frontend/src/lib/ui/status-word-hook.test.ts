/**
 * Every status word a component draws carries `data-status-word` (#2951).
 *
 * The 390 sweep (`scripts/ui-checks/status-words.mjs`) reads only elements
 * that carry the hook, so a renderer without it is never measured and the
 * sweep still passes: the legacy console's FleetStrip (since removed, #3454)
 * drew `Completion.status` bare, and
 * a served long status ran out of its row and was clipped by its card, with
 * the sweep green. This reads every `.svelte` file's template and fails on a
 * status value (`.status`, `.verdict`, `.state`, or a bare `status` /
 * `verdict`) printed as text in an element that does not carry the hook.
 * StatusMark draws its word with the hook, so a status passed to it as a prop
 * is not text here and needs nothing more.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

const SRC = fileURLToPath(new URL('../../', import.meta.url));

/** The names a status value goes by. */
const STATUS_NAMES = new Set(['status', 'verdict', 'state']);

type Node = { type: string; [key: string]: unknown };

const isNode = (value: unknown): value is Node =>
  typeof value === 'object' && value !== null && typeof (value as Node).type === 'string';

/** Whether `expr` prints a status value: the value itself, either side of a
 *  fallback (`c.status || 'done'`), either branch of a ternary (not its test),
 *  a part of a template string, a string method called on it, or a helper
 *  called with it (`statusWord(c.status)`, `exceptionLabel(v.exception.state)`),
 *  the codebase's usual way of turning a status into its word. */
function printsStatus(expr: unknown): boolean {
  if (!isNode(expr)) return false;
  switch (expr.type) {
    case 'Identifier':
      return STATUS_NAMES.has(expr.name as string) && expr.name !== 'state';
    case 'MemberExpression': {
      const property = expr.property as Node;
      return !expr.computed && property.type === 'Identifier' && STATUS_NAMES.has(property.name as string);
    }
    case 'LogicalExpression':
      return printsStatus(expr.left) || printsStatus(expr.right);
    case 'ConditionalExpression':
      return printsStatus(expr.consequent) || printsStatus(expr.alternate);
    case 'TemplateLiteral':
      return (expr.expressions as unknown[]).some(printsStatus);
    case 'CallExpression': {
      const callee = expr.callee as Node;
      if (callee.type === 'MemberExpression' && printsStatus(callee.object)) return true;
      return (expr.arguments as unknown[]).some(printsStatus);
    }
    case 'ChainExpression':
      return printsStatus(expr.expression);
    default:
      return false;
  }
}

const ELEMENTS = new Set(['RegularElement', 'SvelteElement', 'Component']);

const hooked = (element: Node | null): boolean =>
  element !== null &&
  element.type !== 'Component' &&
  (element.attributes as Node[]).some((a) => a.type === 'Attribute' && a.name === 'data-status-word');

/** Each status value printed as text in an element without the hook, as
 *  `<tag> prints {expression}` at its line. */
export function unhookedStatusWords(source: string): string[] {
  const ast = parse(source, { modern: true });
  const found: string[] = [];
  const lineOf = (offset: number) => source.slice(0, offset).split('\n').length;

  const walk = (node: unknown, element: Node | null): void => {
    if (Array.isArray(node)) {
      for (const child of node) walk(child, element);
      return;
    }
    if (!isNode(node)) return;
    if (node.type === 'ExpressionTag') {
      if (printsStatus(node.expression) && !hooked(element)) {
        const text = source.slice(node.start as number, node.end as number);
        found.push(`line ${lineOf(node.start as number)}: <${element?.name ?? 'root'}> prints ${text}`);
      }
      return;
    }
    const inner = ELEMENTS.has(node.type) ? node : element;
    for (const [key, value] of Object.entries(node)) {
      // An attribute's value (`title={c.status}`) is not drawn text, and the
      // script is not the template.
      if (key === 'attributes' || key === 'instance' || key === 'module' || key === 'css') continue;
      if (typeof value === 'object' && value !== null) walk(value, inner);
    }
  };
  walk(ast.fragment, null);
  return found;
}

function svelteFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = `${dir}${entry.name}`;
    if (entry.isDirectory()) return svelteFiles(`${path}/`);
    return entry.name.endsWith('.svelte') ? [path] : [];
  });
}

describe('status-word hook coverage', () => {
  it('finds no status value drawn without `data-status-word`', () => {
    const files = svelteFiles(SRC);
    // Not vacuous: the walk read the component tree.
    expect(files.length).toBeGreaterThan(50);
    const unhooked = files.flatMap((file) =>
      unhookedStatusWords(readFileSync(file, 'utf8')).map((hit) => `${file.slice(SRC.length)} ${hit}`)
    );
    expect(unhooked).toEqual([]);
  });

  it('flags a new unhooked status renderer (negative control)', () => {
    expect(unhookedStatusWords('<span class="w">{c.status}</span>')).toHaveLength(1);
    expect(unhookedStatusWords("<li><b>{c.status || 'done'}</b></li>")).toHaveLength(1);
    expect(unhookedStatusWords('<span>{run.verdict}</span>')).toHaveLength(1);
    expect(unhookedStatusWords('{#each runs as r}<i>{r.status.replace(/_/g, " ")}</i>{/each}')).toHaveLength(1);
    // A status drawn as a component's children: the component's own markup
    // is not the hook.
    expect(unhookedStatusWords('<Badge tone="warn">{c.status}</Badge>')).toHaveLength(1);
    // A status turned into its word by a helper, the usual way one is drawn.
    expect(unhookedStatusWords('<span>{statusWord(c.status)}</span>')).toHaveLength(1);
    expect(unhookedStatusWords('<Badge tone="warn">{exceptionLabel(v.exception.state)}</Badge>')).toHaveLength(1);
  });

  it('passes a hooked renderer, and a status that is not drawn as text', () => {
    expect(unhookedStatusWords('<span data-status-word>{c.status}</span>')).toEqual([]);
    expect(unhookedStatusWords('<span data-status-word>{statusWord(c.status)}</span>')).toEqual([]);
    expect(unhookedStatusWords('<Badge><span data-status-word>{c.status}</span></Badge>')).toEqual([]);
    expect(unhookedStatusWords('<span title={c.status}>{c.title}</span>')).toEqual([]);
    expect(unhookedStatusWords("<span>{p.status === 'loading' ? 'reading…' : 'show'}</span>")).toEqual([]);
    expect(unhookedStatusWords('<StatusMark kind={c.status} word />')).toEqual([]);
  });
});
