/**
 * #2246 — the Work page's half of "every row selects in place".
 *
 * `list.ts` decides that every row's href is a `?item=`; `keys.ts` decides what
 * each key press means. What is left is the wiring, and the wiring is where the
 * defect actually lived: the model already gave an ordinary row a self-selecting
 * href, and the page handed the entry rows `row.href` with a `target` instead —
 * so five of the live screen's seven rows opened GitHub, Linear or the epic page
 * and the operator lost the list they were working through.
 *
 * There is no component renderer for a route in this suite (see
 * `list-geometry.test.ts`), so these are assertions against the page source.
 * Narrow on purpose: one anchor shape, one keyboard entry point, one legend.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { KEY_LEGEND } from '$lib/work/keys';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');

describe('every row anchor selects in place', () => {
  it('builds every row href from the URL the reader is on', () => {
    expect(markup).toContain('{#each shown as row, i (row.itemId)}');
    expect(markup).toContain('href={itemHref(row.itemId)}');
    expect(markup).toContain('onclick={(event) => select(event, row.itemId)}');
  });

  it('gives no row anchor a target, and reads no row href at all', () => {
    // The acceptance test for this issue: `row.href` with a `target` is the
    // exact shape that took the reader off the screen.
    const rows = markup.slice(
      markup.indexOf('{#each shown as row'),
      markup.indexOf('{#if shown.length === 0}')
    );
    expect(rows).not.toContain('row.href');
    expect(rows).not.toContain('row.external');
    expect(rows).not.toContain('target=');
    expect(rows).not.toContain('rel=');
    // And nowhere else on the page either — the needs-you pane's own entry
    // list used to carry the same pair.
    expect(markup).not.toContain('row.href');
    expect(markup).not.toContain('row.external');
  });

  it('keeps the modified click, so a new tab still gets a real load', () => {
    // `select` returns early for ⌘/^/⇧/⌥ and for a non-primary button, which is
    // what makes a `?item=` row openable in a new tab at all.
    expect(page).toContain('if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey');
  });
});

describe('the selection survives shallow routing', () => {
  it('reads ?item= and ?filter= through page.state, never page.url alone', () => {
    // `replaceState` moves `page.state`, not `page.url`: reading the URL left
    // every click writing `?item=` while the pane stayed on "Select an issue".
    expect(page).toContain(
      'const search = $derived(new URLSearchParams(page.state.workSearch ?? page.url.search));'
    );
    expect(page).not.toContain('page.url.searchParams');
  });

  it('writes every change through `write`, which carries the search in page.state', () => {
    expect(page).toContain('replaceState(url, { workSearch: url.search });');
    expect(page.match(/replaceState\(/g)?.length).toBe(1);
    expect(page).not.toContain('new URL(page.url);\n    url.searchParams');
  });
});

describe('the keyboard', () => {
  it('asks `workIntent` what a press means, and nothing else', () => {
    expect(page).toMatch(/import \{[^}]*workIntent[^}]*\} from '\$lib\/work\/keys'/);
    // `bind:innerWidth` rides along (#2281 D2): the one keyboard decision that
    // has to read the breakpoint reads a MEASURED width, not a guess.
    expect(markup).toContain('<svelte:window bind:innerWidth={viewportW} />');
    // The presses arrive through the console's key layer (#2512), and the
    // one press a field keeps — Escape — is read by the field itself.
    expect(page).toContain('registerShortcuts(workShortcuts(onIntent))');
    expect(markup).toContain('onkeydown={onSearchKey}');
    // No second table of key strings in the page: one decision, one place.
    for (const key of ['ArrowDown', 'ArrowUp', "'j'", "'k'"]) {
      expect(page, key).not.toContain(key);
    }
  });

  it('moves the selection over the FILTERED list, with replaceState', () => {
    // `shown` is the searched, filtered list — the rows on screen. Moving over
    // `rows` would land the selection on a row the reader cannot see.
    expect(page).toContain('nextRow(shown, selected?.itemId, intent)');
    // Arrow movement must not flood history: `selectId` goes through `write`,
    // which is `replaceState`, as a click does, so Back leaves the page rather
    // than unwinding every row.
    expect(page).toMatch(/function selectId\([\s\S]*?write\(url\);/);
    expect(page).not.toContain('pushState');
  });

  it('copies the selected row’s command from the model, never from the DOM', () => {
    // The first-run card's next move (#2515) or the on-screen block's command.
    expect(page).toContain(
      "firstRunModel !== null ? firstRunModel.primary : ((selected ? block : soleBlock)?.command ?? '')"
    );
    expect(page).toContain('await copyCommandText(copyCommand)');
    expect(page).toContain('navigator.clipboard.writeText(command)');
  });

  it('draws the legend from KEY_LEGEND, so it cannot promise a dead key', () => {
    expect(markup).toContain('{#each KEY_LEGEND as hint (hint.label)}');
    expect(markup).toContain('{keyGlyph(key)}');
    expect(markup).toContain('aria-label="Keyboard"');
    expect(KEY_LEGEND.map((h) => h.label)).toContain('copy command');
  });
});

describe('the detail pane', () => {
  it('draws ONE needs-you block, whichever payload the row came from', () => {
    // #2280 D9: there were two of these, and the one a projection row got —
    // the band — had no Why, no Since, no command and no magenta border. The
    // `{#if entry} … {:else} … {/if}` that chose between them is gone with the
    // second component.
    const pane = markup.slice(markup.indexOf('aria-label="Item detail"'));
    expect(pane).toContain('<NeedsYouBlock {block} {copied} oncopy={copySelected} itemId={selected.itemId} />');
    expect(pane).not.toContain('<EntryDetail');
    expect(pane).not.toContain('<NeedsYou view=');
    // The block comes first, where the artboard puts it: the pane reads
    // summary -> Why -> Since -> Do this, then the pipeline.
    expect(pane.indexOf('<NeedsYouBlock')).toBeLessThan(pane.indexOf('<Pipeline'));
  });

  it('reads the block through the shared model, not its own composition', () => {
    expect(page).toMatch(/import \{ needsYouBlock \} from '\$lib\/needs-you\/block'/);
    expect(page).toContain('needsYouBlock(selected, clock)');
  });
});
