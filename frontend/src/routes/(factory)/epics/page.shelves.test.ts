/**
 * #2206 — the Epics list shows Active, and keeps Finished / Archived shelved.
 *
 * `$lib/epic/roster` decides the split and the order, and `roster.test.ts`
 * pins that decision. What this file pins is that the page still *asks* it:
 * the regression to guard against is not a wrong sort, it is a page that
 * quietly goes back to rendering one `{#each epics}` and puts sixty-six epics
 * — twelve of them retired — back in front of the operator.
 *
 * There is no component renderer in this suite (see epic/render.ts), so the
 * assertions are against the page source, the same way page.strip.test.ts and
 * run-links.test.ts hold their surfaces to one table.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
/** Comments strip out: a rule written in prose governs nothing. */
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');

describe('the epics list', () => {
  it('splits the roster through the shared reading, not its own', () => {
    expect(page).toMatch(/import \{[^}]*\bepicRoster\b[^}]*\} from '\$lib\/epic\/roster'/);
    expect(page).toMatch(/\$derived\(epicRoster\(epics\)\)/);
    // No second line between finished and active drawn in the page itself.
    expect(page).not.toMatch(/signal === 'complete'/);
    expect(page).not.toMatch(/\.archived\b[^;]*\?\s*finished/);
  });

  it('renders both shelves from the roster, never the raw list', () => {
    expect(markup).toContain('{@render shelf(roster.active)}');
    expect(markup).toContain('{@render shelf(roster.finished)}');
    // `{#each epics}` over the unsplit payload is exactly the old behaviour.
    expect(markup).not.toMatch(/\{#each epics\b/);
  });

  it('names both shelves, so neither is an unlabelled remainder', () => {
    expect(markup).toContain('label="Active"');
    expect(markup).toContain('label="Finished / Archived"');
  });

  it('keeps the finished shelf collapsed until asked', () => {
    // The default IS the requirement: archived epics are out of the default
    // view. A `$state(true)` would ship the thing this issue removes.
    expect(page).toMatch(/let showFinished = \$state\(false\)/);
    expect(markup).toMatch(/\{#if showFinished\}[\s\S]*shelf\(roster\.finished\)/);
  });

  it('shows how many are shelved even while the shelf is shut', () => {
    // A count outside the `{#if showFinished}` guard — a shelf whose size you
    // cannot see is a shelf you forget.
    const toggle = markup.slice(markup.indexOf('label="Finished / Archived"'));
    const count = toggle.indexOf('{roster.finished.length} retired');
    expect(count).toBeGreaterThan(-1);
    expect(count).toBeLessThan(toggle.indexOf('{#if showFinished}'));
  });

  it('says why an epic is shelved rather than leaving it unexplained', () => {
    expect(page).toContain('archive_reason');
  });
});

describe('the needs-you reading on each row (#2279/D6, moved by #2283/D17)', () => {
  it('reads the served set through the shared filter, not a count of its own', () => {
    expect(page).toMatch(/import \{ epicNeedsYou, needsYouSetNotice \} from '\$lib\/epic\/attention'/);
    expect(markup).toContain('{@const needs = epicNeedsYou(factoryPulse.dashboard, epic.epic_ref)}');
    // The header counts the same payload (`factoryPulse.dashboard`), so the
    // list cannot derive a different number from a different read. `/api/epics`
    // carries no needs-you field, and a count built out of `epic.counts` here
    // would be the fourth opinion this node exists to remove.
    expect(markup).not.toMatch(/needsYouReading\(/);
    expect(markup).not.toMatch(/needs_you_by_epic/);
  });

  it('spends magenta only where somebody is waiting, and text-3 where nobody could tell', () => {
    // DESIGN.md: magenta means a person is the next move, and it is rationed.
    // A row with nothing waiting draws no mark at all — "0 needs you" on
    // sixty-six rows is sixty-six marks carrying no information.
    expect(markup).toContain('{#if !needs.known || needs.count > 0}');
    // An unread count is text-3 with its sentence in the title: amber is money
    // and held, and a failed read is neither (#2787 N10).
    expect(markup).toContain("style:color={needs.known ? 'var(--accent)' : 'var(--text-3)'}");
    // The mark is the console's one needs-you shape, not a dot of its own.
    expect(markup).toContain('<StatusMark kind="needs_you" size={10} />');
  });

  it('leads the row with it, as its own first column (#2283 D17)', () => {
    // #2279 rode the reading INSIDE the Epic cell, which kept the column
    // template at six. The design review's finding was that a list whose
    // first column is a name buries the one thing it is read for, so the
    // count is a column now — the first one, named by the first head.
    // The track list lives in the scoped stylesheet since #2304 (an inline
    // grid-template cannot carry the media query its eighth column needs);
    // the base list is still this review's seven, with Needs you leading.
    expect(page).toContain(
      'grid-template-columns: 104px minmax(120px, 1fr) 200px minmax(0, 2fr) 88px 72px 96px;'
    );
    const head = markup.slice(markup.indexOf('snippet headRow'), markup.indexOf('snippet shelf'));
    expect(head).toContain('<span>Needs you</span>');
    expect(head.indexOf('<span>Needs you</span>')).toBeLessThan(head.indexOf('<span>Epic</span>'));
    // First in the row too: the grid places children in order, and a screen
    // reader reads that order at every width.
    const row = markup.slice(markup.indexOf('snippet shelf'));
    expect(row.indexOf('needsYouHref(')).toBeLessThan(row.indexOf('epicHref('));
  });

  it('links the mark to the node that is waited on', () => {
    // The destination is decided in `$lib/epic/roster` (roster.test.ts pins
    // which node, and when the epic is the honest answer instead); what this
    // holds is that the cell is a LINK and asks that reading for its href.
    expect(markup).toContain('href={needsYouHref(epic.epic_ref, factoryPulse.dashboard?.rows)}');
    expect(markup).toMatch(/<a\s+href=\{needsYouHref\(/);
    // The row can no longer be one big anchor — an anchor inside an anchor is
    // not a document — so the epic's own link carries that destination.
    expect(markup).not.toMatch(/<a\s+href=\{`\/epics\//);
    expect(markup).toContain('href={epicHref(epic.epic_ref)}');
  });

  it('names itself where no head row does, and takes a 32px tap target', () => {
    // Below 1024 the heads are gone (D4), so the link carries the words
    // itself — and it is a word in a sentence, which is what TAP_INLINE is
    // for (D25: every control is 32px tall at 390px).
    expect(markup).toContain('aria-label={`${needs.label} needs you`}');
    expect(markup).toMatch(/needsYouHref[\s\S]{0,400}\$\{TAP_INLINE\}/);
    expect(page).toMatch(/import \{[^}]*\bTAP_INLINE\b[^}]*\} from '\$lib\/factory\/classes'/);
  });

  it('still fills the cell on a row with nothing waiting, so nothing slides left', () => {
    // A grid places its children in order: a row that simply skipped the
    // needs-you cell would print its epic under NEEDS YOU and every reading
    // one column off. The placeholder is drawn only where the cells are.
    expect(markup).toContain('<span class="order-1 hidden lg:order-none lg:block"></span>');
  });

  it('keeps every cell shrinkable, so 390px still has no sideways scroll', () => {
    // `min-w-0` + `truncate`, never `shrink-0`: a cell that cannot shrink is
    // a cell that pushes the row wider than the viewport (the design-c2 rule).
    expect(markup).toMatch(/order-1 flex min-w-0 items-center gap-1\.5 truncate font-mono/);
    expect(markup).not.toContain('shrink-0 items-center gap-1.5 font-mono');
  });
});
