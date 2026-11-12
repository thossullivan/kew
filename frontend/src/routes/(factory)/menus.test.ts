/**
 * #2278 (C15) — no menu ships unclosable.
 *
 * A `<details>` is the app's menu primitive, and the browser's own answer to
 * "close it" is a second press of the summary and nothing else: the More menu
 * and the Work filter both sat open through a click anywhere else on the page,
 * and through Escape. `dismissable` is the one fix; this reads the tree so the
 * next menu cannot be written without it rather than being found in the next
 * design review.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const SRC = fileURLToPath(new URL('../../', import.meta.url));

function svelteFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return svelteFiles(path);
    return path.endsWith('.svelte') ? [path] : [];
  });
}

/** Every `<details …>` opening tag in the tree, with the file it came from. */
const MENUS = svelteFiles(SRC).flatMap((path) => {
  const source = readFileSync(path, 'utf8');
  // Markup only: a comment that names `<details>` (Description.svelte says a
  // `<details>` wrapper in an issue body "stays the text") is not a menu.
  const markup = source
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  return [...markup.matchAll(/<details\b[^>]*>/g)].map((m) => ({
    where: path.slice(SRC.length),
    tag: m[0],
    source
  }));
});

describe('every <details> menu', () => {
  it('is one of the two the app has — a third must be declared here', () => {
    expect(MENUS.map((m) => m.where).sort()).toEqual([
      'lib/shell/FactoryHeader.svelte',
      'routes/(factory)/work/+page.svelte'
    ]);
  });

  it('closes on an outside press and on Escape, via the one action', () => {
    for (const { where, tag, source } of MENUS) {
      expect(tag, `${where}: <details> without use:dismissable`).toContain('use:dismissable');
      expect(source, `${where}: imports dismissable`).toContain(
        "import { dismissable } from '$lib/ui/dismissable'"
      );
    }
  });
});
