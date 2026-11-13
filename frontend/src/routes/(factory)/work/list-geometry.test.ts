/**
 * FUI-18 — the issue list against its pixel reference (DWork x 0–420, y 56–720).
 *
 * The epic's visual check is a headless-Chrome screenshot compared by eye
 * (`npm run shoot -- /work DWork`), which no CI run performs and no container
 * without a browser can. The numbers that check would catch are all in the
 * DWork markup, so they are asserted here the way `factory-header.test.ts`
 * asserts the header's: the artboard is read as the source of truth, each
 * measurement is matched against the utility `+page.svelte` actually carries,
 * and each colour is matched against the app.css token it must come from — a
 * screenshot cannot tell `#151025` from `var(--ink-selected)`, and only one of
 * the two switches theme.
 *
 * Narrow on purpose: the frame, the search row, the column grid, the row height
 * and the four colours that say which row is selected. It is not a rendering
 * test (there is no component-render infra here — see StatusMark.svelte).
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { dataRowClasses } from '$lib/factory/classes';

const artboard = readFileSync(
  new URL('../../../../design/factory-ui/DWork.html', import.meta.url),
  'utf8'
);
/**
 * The list is the artboard's one `<aside>`. The detail area beside it is the
 * artboard's `<main>`, and the page's `<section aria-label="Item detail">` — the
 * artboard is a standalone page that owns its landmark, while the page renders
 * inside the factory layout's `<main>` and must not nest a second one.
 */
const aside = artboard.match(/<aside[\s\S]*?<\/aside>/)![0];
const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../../app.css', import.meta.url), 'utf8');

/** The dark-theme value of one `--token`, as app.css declares it. */
function token(name: string): string {
  return css.match(new RegExp(`--${name}:\\s*([^;]+);`))![1].trim();
}

describe('DWork issue list (x 0–420, y 56–720)', () => {
  it('is a fixed 420px column with a hairline down its right edge', () => {
    expect(aside).toContain('width: 420px; flex-shrink: 0');
    expect(aside).toContain('border-right: 1px solid #221c33');
    expect(aside).toContain('aria-label="Issues"');
    // Taken at `lg` since #2281 D2: below 1024 the page is one column, so the
    // artboard's fixed 420px column and the hairline down its right edge — the
    // seam between two columns — are what the page carries from `lg` up.
    expect(page).toContain('lg:w-[420px]');
    expect(page).toContain('lg:shrink-0');
    expect(page).toContain('lg:border-r');
    expect(page).toContain('lg:border-hair');
    expect(page).toContain('aria-label="Issues"');
    expect(token('hair')).toBe('#221c33');
  });

  it('gives the search row a 44px input and a 44px filter button, 8px apart', () => {
    expect(aside).toContain('padding: 20px 28px 16px; display: flex; gap: 8px');
    expect(aside).toContain('height: 44px');
    expect(aside).toContain('padding: 0 14px; border-radius: 4px; border: 1px solid #342a4a');
    expect(aside).toContain('width: 44px; height: 44px; border-radius: 4px');
    // pt-5 = 20px, px-7 = 28px, pb-4 = 16px, gap-2 = 8px, h-11/w-11 = 44px,
    // px-3.5 = 14px.
    expect(page).toContain('items-center gap-2 px-7 pt-5 pb-4');
    expect(page).toContain('h-11 min-w-0 flex-1 rounded-[4px] border border-hair-strong');
    expect(page).toContain('px-3.5');
    expect(page).toContain('h-11 w-11 cursor-pointer list-none items-center justify-center');
    expect(token('hair-strong')).toBe('#342a4a');
  });

  it('labels the search field for a reader that cannot see the placeholder', () => {
    expect(aside).toContain('>Search issues</label>');
    expect(page).toContain('<label class="sr-only" for="work-search">Search issues</label>');
    expect(page).toContain('placeholder="Search issues"');
    expect(aside).toContain('aria-label="Filter issues"');
    expect(page).toContain('aria-label="Filter issues"');
  });

  it('heads the table "No. / Issue / State" over one 56/1fr/108 grid, 12px apart', () => {
    expect(aside).toContain('grid-template-columns: 56px minmax(0, 1fr) 96px; gap: 12px');
    expect(aside).toContain('<span>No.</span><span>Issue</span>');
    expect(aside).toContain('font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase');
    // 92 where the artboard draws 96 (#2281 D12): the state cell is its mark
    // and its longest word at label-11 and no wider, and the four pixels go to
    // the title. The budget is computed in page.design-c2.test.ts, trailing
    // letter-space included — 88 was a quarter-pixel short of `NEEDS YOU`.
    // 104 since #2848: the cell also holds the needs-you diamond beside an
    // `in review`, which broke over two lines at 92.
    // 108 since #2898: Chromium snaps Plex Mono's advances to whole pixels, and
    // `in review` beside the diamond ran 3px past 104.
    expect(page).toContain("const COLUMNS = '56px minmax(0, 1fr) 108px';");
    expect(page).toContain('<span>No.</span><span>Issue</span>');
    // The head's 11px/tracked/uppercase and its 10px of air come from FUI-05's
    // row builder, not from this page: gap-3 = 12px, pb-2.5 = 10px.
    const head = dataRowClasses({ head: true, class: 'gap-3 px-7' });
    expect(head).toContain('text-11');
    expect(head).toContain('pb-2.5');
    expect(head).toContain('gap-3');
    expect(head).not.toContain('gap-5');
    expect(page).toContain("dataRowClasses({ head: true, class: 'gap-3 px-7' })");
  });

  it('draws 56px rows over a quiet rule, with 28px gutters', () => {
    expect(aside).toContain('height: 56px; padding: 0 28px; border-bottom: 1px solid #1a1528');
    // h-14 = 56px; the rule is the row builder's `quiet` hairline.
    const row = dataRowClasses({ rule: 'quiet', class: 'gap-3 px-7' });
    expect(row).toContain('h-14');
    expect(row).toContain('border-b');
    expect(row).toContain('border-hair-quiet');
    expect(row).toContain('px-7');
    expect(token('hair-quiet')).toBe('#1a1528');
    // The artboard's last row carries no rule; `rule: 'none'` is that row.
    expect(page).toContain("rule: i === shown.length - 1 ? 'none' : 'quiet'");
    expect(dataRowClasses({ rule: 'none' })).not.toContain('border-b');
  });

  it('marks the selected row with --ink-selected and says so to a reader', () => {
    expect(aside).toContain('aria-current="true"');
    expect(aside).toContain('background: #151025');
    expect(page).toContain("aria-current={selected?.itemId === row.itemId ? 'true' : undefined}");
    expect(page).toContain('class:bg-ink-selected={selected?.itemId === row.itemId}');
    expect(token('ink-selected')).toBe('#151025');
  });

  it('lifts the selected row’s number and title, and quiets every other', () => {
    // Selected: --text-1 number, --text-1 title at weight 500. Otherwise the
    // number is --text-3 and the title --text-2, both at the artboard's sizes.
    expect(aside).toContain('font-size: 13px; color: #ebe6f2');
    expect(aside).toContain('font-size: 15px; color: #ebe6f2; font-weight: 500');
    expect(aside).toContain('font-size: 13px; color: #8d84a3');
    expect(aside).toContain('font-size: 15px; color: #b5acc8');
    expect(page).toContain('class:text-text-1={selected?.itemId === row.itemId}');
    expect(page).toContain('class:text-text-3={selected?.itemId !== row.itemId}');
    expect(page).toContain('class:font-medium={selected?.itemId === row.itemId}');
    expect(page).toContain('class:text-text-2={selected?.itemId !== row.itemId}');
    expect(page).toContain('class="line-clamp-2 text-15"');
    expect(token('text-1')).toBe('#ebe6f2');
    expect(token('text-2')).toBe('#b5acc8');
    expect(token('text-3')).toBe('#8d84a3');
  });

  it('right-aligns the state as a 7px mark 8px from its word', () => {
    expect(aside).toContain('justify-content: flex-end; align-items: center; gap: 8px');
    expect(aside).toContain('width: 7px; height: 7px');
    // The artboard writes the word at the 13px data size; #2281 D13 moves it to
    // label-11, the size DESIGN.md gives status words, and takes the word
    // itself from `stateWord` so the list and the DAG name one meaning once.
    // Held in page.design-c2.test.ts, beside the rest of that finding.
    expect(page).toContain('<span class="flex justify-end text-11">');
    expect(page).toContain('label={stateWord(row.state)}');
  });

  it('leaves the one `main` landmark to the layout the page renders inside', () => {
    // #2131 review: the detail area was a `<main>` nested inside the factory
    // layout's `<main>` — two `main` landmarks, invalid HTML, and two
    // destinations for one "skip to main content". The artboard's `<main>` is
    // its own standalone page's and does not travel into the app.
    const layout = readFileSync(new URL('../+layout.svelte', import.meta.url), 'utf8');
    // Markup only: the page's own comment explains the deviation and names the
    // element it does not use, which is prose, not a landmark.
    const markup = (src: string) => src.replace(/<!--[\s\S]*?-->/g, '');
    expect(artboard).toContain('<main');
    expect(markup(layout).match(/<main[\s>]/g)).toHaveLength(1);
    expect(markup(page)).not.toMatch(/<main[\s>]/);
    // Still a labelled region: the artboard's other panes name themselves too.
    expect(page).toContain('aria-label="Item detail"');
  });

  it('clamps a long title to two lines rather than cutting it at one', () => {
    // The artboard ellipsises on one line, which cut a row title at about 22
    // characters — and DESIGN.md's typography principle is the other reading:
    // "titles clamp to two lines with an ellipsis; never clip mid-glyph"
    // (#2281 D12). Two 15px lines are ~44px and still fit the 56px row.
    expect(aside).toContain('white-space: nowrap; overflow: hidden; text-overflow: ellipsis');
    expect(page).toContain('line-clamp-2');
  });

  it('lets each notice name its own mark, and decides them against the merged list', () => {
    // #2131 review: every notice carried a hardcoded `label="unreadable"`, so
    // the needs-you notice — which is not about a failed read — was announced
    // to assistive tech as an unreadable payload. The name comes from the
    // notice now (list.ts decides it; list.test.ts asserts the values).
    expect(page).toContain('label={notice.label}');
    expect(page).not.toContain('label="unreadable"');
    // And against `rows`, not `shown`: a notice decided against the searched or
    // filtered list would claim a row is missing the moment one is typed away.
    expect(page).toContain('listNotices(input, rows)');
  });
});
