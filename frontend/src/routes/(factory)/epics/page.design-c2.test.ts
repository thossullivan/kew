/**
 * #2283 — design review cycle 1's three findings about the Epics list.
 *
 *   - D4  — at 390px the six-column row lost the epic names, merged the heads
 *           into the cells and stacked nine count marks over the rows below.
 *   - D16 — "progressing" wore a live cyan halo while the Last tick cell two
 *           columns over said "no ticks yet".
 *   - D17 — no needs-you column, nine unlabelled marks of which five were
 *           zero, and a second name for the screen ("dependency graph").
 *
 * The needs-you column itself is held in page.shelves.test.ts, beside the
 * reading it moved; the readings behind D16 and D17 are held in
 * `$lib/epic/roster.test.ts`. What this file holds is that the PAGE asks for
 * them, and the widths it draws them at.
 *
 * Asserted as markup, for the reason monitor/phone-layout.test.ts gives: no
 * CI run can resize a window, so a 390px promise is held against the
 * utilities the page actually carries. The operator's browser check at
 * 390×844 is the other half.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { chipClasses, dataRowClasses, TAP_INLINE, TAP_PILL } from '$lib/factory/classes';
import { COMPACT_SLOTS } from '$lib/factory/tick-strip';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
/** Comments strip out: a rule written in prose governs nothing. */
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');
const row = markup.slice(markup.indexOf('snippet shelf'));

/**
 * The row's track list, read out of the scoped stylesheet.
 *
 * It moved there from an inline `style:` when #2304 added a column that only
 * exists at 1280 — an inline grid-template cannot carry a media query. `base`
 * is the seven-column list this review settled, `wide` the eight-column one.
 */
const trackLists = (): string[] =>
  [...page.matchAll(/grid-template-columns: ([^;]+);/g)].map((m) => m[1]);

describe('each epic at 390px is a two-line block (D4)', () => {
  it('draws the seven-column table only where it fits', () => {
    const [base] = trackLists();
    expect(base).toBe('104px minmax(120px, 1fr) 200px minmax(0, 2fr) 88px 72px 96px');
    expect(markup).toContain("dataRowClasses({ head: true, class: 'epic-row hidden lg:grid' })");
  });

  // The budget is COMPUTED from the template the page actually carries, not
  // restated in prose beside it: the first draft of this test wrote down a
  // fixed total that had dropped the 72px Change column, so the "336px at
  // 1024" it justified the template with was 72px of elastic room the row
  // never had. A sum a reader can only check by hand is a sum that drifts the
  // next time a column is added.
  it('leaves room for the two elastic cells at 1024, and none at 768', () => {
    const template = trackLists()[0];
    expect(template).toBeTruthy();
    const cells = template!.split(/ (?![^(]*\))/);
    expect(cells).toHaveLength(7);
    const fixed = cells.filter((c) => c.endsWith('px'));
    const elastic = cells.filter((c) => c.startsWith('minmax('));
    expect(fixed).toHaveLength(5);
    expect(elastic).toHaveLength(2);

    const GAP = 20; // `gap-5` on the row, six gaps between seven cells
    const GUTTER = 80; // the page's own `sm:px-10`, both sides
    const budget = fixed.reduce((sum, c) => sum + parseInt(c, 10), 0) + (cells.length - 1) * GAP;
    expect(budget).toBe(680);
    // 264px for an epic name and its node marks is tight but readable; at 768
    // the pair gets 8px, which is why the table starts at `lg` and not before.
    expect(1024 - GUTTER - budget).toBe(264);
    expect(768 - GUTTER - budget).toBe(8);
  });

  // #2304's tick strip is an EIGHTH column, and this is why it is not at `lg`:
  // spending 112px of that 264px would leave the epic name and its node marks
  // 132px between them, which is the defect D4 fixed. The same arithmetic,
  // over the wide list, at the width that list applies from.
  it('spends the tick strip at 1280 and not out of the 1024 budget', () => {
    const lists = trackLists();
    expect(lists).toHaveLength(2);
    const cells = lists[1]!.split(/ (?![^(]*\))/);
    expect(cells).toHaveLength(8);
    expect(cells.filter((c) => c.startsWith('minmax('))).toHaveLength(2);
    // The strip's own column, and the slot arithmetic that has to multiply
    // out: COMPACT_SLOTS slots at STRIP_SLOT_PX each. A strip padded wider
    // than its cell overflows the row it was padded to align with.
    expect(cells).toContain(`${COMPACT_SLOTS * 7}px`);
    expect(page).toContain('const STRIP_SLOT_PX = 7;');

    const GAP = 20; // seven gaps between eight cells
    const GUTTER = 80;
    const budget = cells.filter((c) => c.endsWith('px')).reduce((s, c) => s + parseInt(c, 10), 0);
    expect(budget + (cells.length - 1) * GAP).toBe(868);
    // D22 (c4): the Signal track is 256px here, so a word and its note fit.
    expect(cells[2]).toBe('256px');
    expect(1280 - GUTTER - 868).toBe(332);
    // The cell is `hidden xl:block`, and a display:none cell is not a grid
    // item — which is what keeps the seven-track list above exact at 1024.
    // The head carries the strip's legend as its title (#2844 D23).
    expect(markup).toContain('<span class="hidden xl:block" title={CHANGE_STRIP_LEGEND}>Ticks</span>');
  });

  it('wraps the row into lines below 1024 instead of gridding it', () => {
    // `flex-wrap` with `h-auto` is what makes a long name or a long signal
    // wrap rather than be cut off by a 56px row; the grid and the 56px come
    // back from `lg` up. `cn` resolves the conflict in that direction.
    expect(markup).toContain(
      "class: 'epic-row flex h-auto flex-wrap gap-3 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0'"
    );
    // And that the merge really resolves it that way: the base is `grid
    // gap-5 h-14`, so a row that only ADDED `flex` would be a grid still.
    const merged = dataRowClasses({
      class: 'flex h-auto flex-wrap gap-3 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0'
    }).split(' ');
    expect(merged).toContain('flex');
    expect(merged).not.toContain('grid');
    expect(merged).not.toContain('gap-5');
    expect(merged).not.toContain('h-14');
    expect(merged).toEqual(expect.arrayContaining(['lg:grid', 'lg:gap-5', 'lg:h-14']));
  });

  it('breaks the block after the signal, which is a decision and not an overflow', () => {
    // A full-width flex item ends the first line wherever it is placed, so
    // the name and its signal are line one and the counts and the drain are
    // line two — however wide the phone is. Removed from the grid above 1024.
    expect(markup).toContain('<span class="order-5 w-full lg:hidden" aria-hidden="true"></span>');
    const orders = [...row.matchAll(/order-(\d)\b/g)].map((m) => Number(m[1]));
    // needs-you, epic, signal on line one; counts and drain on line two.
    expect(orders).toEqual([1, 1, 2, 3, 6, 7, 5]);
  });

  it('keeps the DOM in the order the heads name, so the reading order is one order', () => {
    const cells = ['needsYouHref(', 'epicHref(', 'signal.color', 'progress.done', 'fmtUSD('];
    const at = cells.map((c) => row.indexOf(c));
    expect(at).toEqual([...at].sort((a, b) => a - b));
    expect(at[0]).toBeGreaterThan(-1);
  });

  it('leaves the table’s own two columns to the table', () => {
    // Change and Last tick would cost the block a third line, and a bare "0"
    // under no head row is not a reading. The heartbeat still speaks at every
    // width — the signal cell says "no heartbeat" in words (D16).
    expect(markup).toContain('<span class="hidden text-right font-mono text-13 text-text-3 lg:block"');
    expect(markup).toMatch(/class="hidden truncate text-right font-mono text-13 text-text-3 lg:block"/);
  });

  it('halves the page gutter, which was a fifth of a 390px screen', () => {
    // The same treatment the Monitor took at this width (#2282 D3).
    expect(markup).toContain('px-4 pt-5 pb-[18px] sm:px-10');
    expect(markup).toContain('overflow-y-auto px-4 pt-7 pb-9 sm:px-10');
  });

  it('gives every control in the row a 32px tap target (D25)', () => {
    expect(TAP_INLINE).toBe('py-2.5 sm:py-0');
    // Three inline links: the needs-you link, the epic link, and the "no
    // epic" row's needs-you link (#2784). The shelf toggle is a chip now
    // (#2787 D34), and a chip takes the press height as a pill (`TAP_PILL`).
    expect([...markup.matchAll(/\$\{TAP_INLINE\}/g)].length).toBe(3);
    expect(markup).toContain('class={chipClasses(showFinished)}');
    expect(chipClasses(false)).toContain(TAP_PILL);
  });
});

describe('the live claim (D16)', () => {
  it('takes the signal through the honest reading, never the raw tone', () => {
    expect(page).toMatch(/import \{[^}]*\bsignalReading\b[^}]*\} from '\$lib\/epic\/roster'/);
    // #2539 D24: with whether a person is next, so a stall is magenta or amber.
    expect(markup).toContain('{@const signal = signalReading(epic, Date.now(), needs.count > 0)}');
    // `signalTone` straight from `progress` is exactly the old behaviour: a
    // halo decided by the driver's last verdict rather than by its pulse.
    expect(page).not.toMatch(/import \{[^}]*\bsignalTone\b/);
    expect(markup).not.toContain('signalTone(');
  });

  it('paints the dot and the halo from that reading alone', () => {
    expect(markup).toContain('style:color={signal.color}');
    expect(markup).toContain('style:background={signal.color}');
    expect(markup).toMatch(/style:box-shadow=\{signal\.halo/);
  });

  it('says "no heartbeat" on the row, in the quiet ink', () => {
    expect(markup).toContain('{#if signal.note}');
    expect(markup).toContain('<span class="truncate text-[11px] text-text-3">· {signal.note}</span>');
    // The whole sentence survives a cell too narrow for it.
    expect(markup).toContain(
      "title={signal.note ? `${epic.signal ?? 'unknown'} · ${signal.note}` : undefined}"
    );
  });
});

describe('only real counts, and one name for the screen (D17, D18)', () => {
  it('draws a mark only for a status that happened', () => {
    expect(page).toMatch(/import \{[^}]*\bnonZeroCounts\b[^}]*\} from '\$lib\/epic\/roster'/);
    expect(markup).toContain('{#each nonZeroCounts(progress.legend) as entry (entry.status)}');
    // The muting of a zero is gone with the zero itself.
    expect(markup).not.toContain('class:text-text-3={entry.count === 0}');
  });

  it('still counts the done/total over every status the payload serves', () => {
    // `epicProgress` is unfiltered: dropping the zero MARKS must not change
    // the figure beside them, which is the epic's actual shape.
    expect(markup).toContain('{@const progress = epicProgress(epic.counts)}');
    expect(markup).toContain('{progress.done}<span class="text-text-3"> / {progress.total}</span>');
  });

  it('calls the screen Epics, in mono, and nothing else', () => {
    expect(markup).toContain('<h1 class="font-mono text-26 text-text-1">Epics</h1>');
    expect(markup).not.toContain('dependency graph');
  });
});
