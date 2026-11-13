/**
 * FUI-09 — where the factory shell can actually be seen.
 *
 * The `(factory)` group shipped its layout before any page moved into it: the
 * plan gave `/work` to FUI-18, `/monitor` to FUI-22 and the move of the epic
 * page to FUI-14, each of which depends on this issue (all three have since
 * landed). That sequencing
 * is deliberate, but it has a cost worth pinning rather than leaving implicit —
 * a tab whose page has not landed goes nowhere, and no CI run can photograph
 * the header anyway, so the geometry a screenshot would compare is asserted in
 * `factory-header.test.ts` against the DWork markup instead.
 *
 * `DEFERRED_TABS` keeps that honest in both directions: a tab that gains its
 * route must be struck from the list (the third case fails otherwise), and a
 * fourth tab pointing at nothing cannot be added without being declared here.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { FACTORY_TABS } from '$lib/shell/nav';

const ROUTES = fileURLToPath(new URL('../', import.meta.url));
const layout = readFileSync(join(ROUTES, '(factory)', '+layout.svelte'), 'utf8');

/**
 * Factory tabs whose page has not been written yet — none, now that Work,
 * Monitor and the epic page have all landed. A route group is invisible in
 * URLs, so a tab is satisfied by a page on either side of it.
 */
const DEFERRED_TABS: Record<string, string> = {};

function hasRoute(href: string): boolean {
  const rel = href.replace(/^\//, '');
  return [join(ROUTES, '(factory)', rel), join(ROUTES, rel)].some((dir) =>
    existsSync(join(dir, '+page.svelte'))
  );
}

describe('the (factory) shell', () => {
  it("mounts the factory header from its layout, the group's one shell", () => {
    expect(layout).toContain("import FactoryHeader from '$lib/shell/FactoryHeader.svelte'");
    expect(layout).toContain('<FactoryHeader />');
  });

  it('takes the one shared pulse for the whole group, not per page', () => {
    expect(layout).toContain('factoryPulse.subscribe()');
  });

  it('points every tab at a route, or at one this issue defers by name', () => {
    for (const tab of FACTORY_TABS) {
      const deferred = DEFERRED_TABS[tab.href];
      expect(hasRoute(tab.href) || Boolean(deferred), `${tab.href} goes nowhere`).toBe(true);
    }
  });

  it('still defers exactly the tabs it claims to — strike one when its page lands', () => {
    for (const [href, issue] of Object.entries(DEFERRED_TABS)) {
      expect(hasRoute(href), `${href} now has a page: drop it from DEFERRED_TABS (${issue})`).toBe(
        false
      );
    }
  });
});

/**
 * #2278 (C4) — the shell fills the viewport, and each page scrolls its own pane.
 *
 * The group's root was `min-h-screen`, which is a floor and not a ceiling: a
 * list longer than the window grew the document instead, and the whole page
 * scrolled — the 56px header and the Work detail pane slid away with the rows
 * the reader was walking. `h-dvh` on the root plus `min-h-0 flex-1
 * overflow-hidden` on `main` makes the shell exactly one viewport tall, which
 * puts the scrolling on whichever pane inside a page owns it.
 *
 * That only holds if every page in the group lives inside that box, so the
 * second case reads them all: a page must claim the shell's remaining height
 * (`min-h-0` and `flex-1`) and something inside it must scroll. A page that is
 * one long column — Monitor, Config, Budget, Scorecard, the console — satisfies
 * both on its root, with `overflow-y-auto` on the root itself.
 *
 * The scroller it must have is an UNCAPPED one, and that word is the whole
 * check: a `max-h-…` scroller is a sub-list that scrolls within the band it
 * sits in — the epic page's event ticker is one — and it says nothing about
 * whether the bottom of the page can be reached. The epic page passed this
 * case on the ticker alone while the foot of its selected-node rail was
 * clipped away and unreachable on a short window (#2278 C4 review); the rail
 * is a fixed band below the canvas, so that page's scroll path has to be the
 * column itself.
 */
function markupOf(source: string): string {
  return source.slice(source.lastIndexOf('</script>') + 1);
}

function pageRoot(markup: string): string {
  // Snippets and comments are indented; a page root is at column 0.
  const root = /^<(?:div|section|main|article|nav|aside|form)\b[^>]*>/m.exec(markup);
  return root![0];
}

/**
 * The class list of every element in the markup that declares a vertical
 * scroller — `class="…"` and Svelte's `class={`…`}` both, newlines folded out
 * because Prettier wraps a long one over several lines.
 */
function scrollers(markup: string): string[] {
  return [...markup.matchAll(/class=(?:"([^"]*)"|\{`([^`]*)`\})/g)]
    .map((match) => (match[1] ?? match[2]).replace(/\s+/g, ' '))
    .filter((klass) => klass.includes('overflow-y-auto'));
}

const PAGES = (function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return walk(path);
    return entry === '+page.svelte' ? [path] : [];
  });
})(join(ROUTES, '(factory)'));

/** The one page whose last band is fixed-height and outside every scroller. */
const EPIC_PAGE = join(ROUTES, '(factory)', 'epics', '[ref]', '+page.svelte');

describe('the shell against the viewport', () => {
  it('is exactly one viewport tall, with the scrolling inside it', () => {
    expect(layout).toContain('flex h-dvh flex-col');
    expect(layout).not.toContain('min-h-screen');
    expect(layout).toContain('flex min-h-0 flex-1 flex-col overflow-hidden');
  });

  it('gives every page in the group a box to scroll in, and one that scrolls', () => {
    expect(PAGES.length).toBeGreaterThan(9);
    for (const path of PAGES) {
      const markup = markupOf(readFileSync(path, 'utf8'));
      const where = path.slice(ROUTES.length);
      const root = pageRoot(markup);
      expect(root, `${where}: page root must claim the shell's height`).toContain('min-h-0');
      expect(root, `${where}: page root must claim the shell's height`).toContain('flex-1');
      const uncapped = scrollers(markup).filter((klass) => !klass.includes('max-h-'));
      expect(
        uncapped,
        `${where}: nothing in this page scrolls but a capped sub-list, so its ` +
          `bottom is unreachable once the shell is shorter than the page`
      ).not.toHaveLength(0);
    }
  });

  it("puts the epic page's scroll path on the column that carries its rail", () => {
    const markup = markupOf(readFileSync(EPIC_PAGE, 'utf8'));
    expect(pageRoot(markup)).toContain('overflow-y-auto');
  });
});
