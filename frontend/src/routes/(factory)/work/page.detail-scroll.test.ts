/**
 * #2278 (C4 review) — the detail pane scrolls what the shell clips.
 *
 * The shell is exactly one viewport tall and its `main` clips what does not fit
 * (`(factory)/+layout.svelte`), so the scrolling has to live in the panes. The
 * list side always had its own (`list-geometry.test.ts` pins its 420px frame);
 * the detail side had none, and `main`'s old `overflow-x-hidden` — which
 * computes to `overflow-y: auto`, so `main` scrolled — was quietly carrying it.
 * Once that became `overflow-hidden`, a short laptop lost the bottom of the
 * pane: the item header, the band and the pipeline fill the box on their own,
 * the lower grid's `flex-1` resolves to zero, and the description and the whole
 * evidence ledger sit inside two 0px-tall scrollers that no scroll can reach.
 *
 * Source assertions, like the other geometry tests on this page: there is no
 * component renderer for a route in this suite (see list-geometry.test.ts) and
 * no CI run can resize a window.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
const layout = readFileSync(new URL('../+layout.svelte', import.meta.url), 'utf8');
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');

/** The opening tag of one element, located by a string it carries. */
function tag(source: string, needle: string): string {
  const at = source.lastIndexOf('<', source.indexOf(needle));
  return source.slice(at, source.indexOf('>', at) + 1);
}

describe('the Work panes under a clipping shell', () => {
  it('renders inside a shell that clips, which is what the rest is about', () => {
    expect(layout).toContain('overflow-hidden');
    // Not the `overflow-x-hidden` that used to scroll on the y axis for it.
    expect(layout).not.toContain('overflow-x-hidden');
  });

  it('gives both panes a scroller of their own, not just the list', () => {
    for (const label of ['aria-label="Issues"', 'aria-label="Item detail"']) {
      expect(tag(markup, label), `${label}: nothing scrolls what the shell clips`).toContain(
        'overflow-y-auto'
      );
    }
  });

  it('floors the lower grid so a short screen cannot squeeze it to nothing', () => {
    const grid = tag(markup, 'grid-cols-[5fr_7fr]');
    expect(grid).toContain('flex-1');
    // `min-h-0` was the bug: it let `flex-1` resolve to 0 and hid the
    // description and the ledger inside their own scrollers.
    expect(grid).not.toContain('min-h-0');
    expect(grid).toMatch(/min-h-\d/);
  });

  it('keeps the two lower columns scrolling inside that floor', () => {
    for (const file of ['Description.svelte', 'EvidenceLedger.svelte']) {
      const source = readFileSync(new URL(`../../../lib/work/${file}`, import.meta.url), 'utf8');
      expect(source, `${file}: the column must scroll its own content`).toContain('overflow-y-auto');
    }
  });
});
