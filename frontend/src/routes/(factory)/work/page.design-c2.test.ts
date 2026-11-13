/**
 * #2281 — design review cycle 1's four findings about the Work screen.
 *
 *   - D2  — at 390px selecting a row highlighted it and left the detail
 *           off-canvas to the right. DESIGN.md already says where it goes:
 *           "Below 1024 the Work list and detail stack", and on a phone "a
 *           selected Work item shows full width with a back control".
 *   - D12 — list titles were cut at ~22 characters on one line, against the
 *           typography principle "titles clamp to two lines".
 *   - D13 — the STATE column spoke its own vocabulary (success / idle /
 *           manual) while the DAG said done / todo about the same meaning.
 *   - C5  — j/k selected rows off-screen: nothing scrolled the selection into
 *           view, so holding j walked the selection out of the list.
 *
 * The words themselves are held in `$lib/work/list.test.ts` beside the model
 * that decides them. What this file holds is the markup: asserted as source,
 * for the reason monitor/phone-layout.test.ts gives — no CI run can resize a
 * window or scroll a real scroller, so a 390px promise is held against the
 * utilities the page actually carries, and the operator's browser check at
 * 390×844 is the other half.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  ATTENTION_STATE_WORD,
  EXCEPTION_STATE_WORD,
  EXECUTION_STATE_WORD,
  stateWord
} from '$lib/work/list';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
/** Comments strip out: a rule written in prose governs nothing. */
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');

/** The opening tag of one element, located by a string it carries. */
function tag(source: string, needle: string): string {
  const found = source.indexOf(needle);
  expect(found, needle).toBeGreaterThan(-1);
  const at = source.lastIndexOf('<', found);
  return source.slice(at, source.indexOf('>', at) + 1);
}

/** One row of the list: the anchor the `{#each}` draws per item. */
const row = markup.slice(markup.indexOf('{#each shown as row'), markup.indexOf('{#if shown.length'));

describe('one column below 1024 (D2)', () => {
  it('gives the list the whole width below lg and the artboard’s 420px above', () => {
    const aside = tag(markup, 'aria-label="Issues"');
    expect(aside).toContain('lg:w-[420px]');
    expect(aside).toContain('lg:shrink-0');
    // The 420px column's right-hand hairline is the seam BETWEEN two columns;
    // at 390px there is nothing on the other side of it.
    expect(aside).toContain('lg:border-r');
    expect(aside).not.toMatch(/\sw-\[420px\]/);
  });

  it('shows exactly one of the two panes below lg, and both from lg up', () => {
    const aside = tag(markup, 'aria-label="Issues"');
    const detail = tag(markup, 'aria-label="Item detail"');
    // Selected: the detail is the screen, and the list stands down.
    // So does a first run (#2515): an empty list has nothing to pick, and the
    // first-run card in the detail pane takes the column.
    expect(aside).toContain("selected || firstRunModel ? 'hidden' : 'flex w-full'");
    expect(aside).toContain('lg:flex');
    // Nothing selected: the list is the screen. The detail's empty state is
    // the needs-you queue, which is a second answer to the question the
    // header's own counter already answers on a phone.
    expect(detail).toContain("selected || firstRunModel ? 'flex' : 'hidden'");
    expect(detail).toContain('lg:flex');
  });

  it('carries a back control, below lg only, that is a real link to the list', () => {
    const back = tag(markup, '← Work');
    expect(back.startsWith('<a')).toBe(true);
    expect(back).toContain('href={backHref()}');
    expect(back).toContain('onclick={back}');
    // Two columns need no way back to the one beside them.
    expect(back).toContain('lg:hidden');
    // D25 — every control is at least 32px tall at 390px.
    expect(back).toContain('min-h-8');
  });

  it('keeps `?item=` as the state, read from `page.state` first', () => {
    // The back control drops one parameter from the URL this page last wrote
    // (`here()` reads `page.state.workSearch`), and writes it through the one
    // writer. Reading `page.url` back after `replaceState` is the trap
    // DESIGN.md names and this page's `here()` exists to avoid.
    const fn = page.slice(page.indexOf('function backHref()'));
    expect(fn.slice(0, fn.indexOf('}'))).toContain("url.searchParams.delete('item')");
    expect(page).toContain('const search = $derived(new URLSearchParams(page.state.workSearch');
    // And the press itself clears the selection rather than navigating.
    const press = page.slice(page.indexOf('function back(event: MouseEvent)'));
    expect(press.slice(0, press.indexOf('\n  }'))).toContain('clearSelection()');
  });

  it('halves the detail gutter, which was a quarter of a 390px screen', () => {
    const detail = tag(markup, 'aria-label="Item detail"');
    expect(detail).toContain('px-4');
    expect(detail).toContain('sm:px-12');
  });

  it('stacks the description over the ledger, and turns the hairline between', () => {
    const grid = tag(markup, 'grid-cols-[5fr_7fr]');
    expect(grid).toContain('grid-cols-1');
    expect(grid).toContain('lg:grid-cols-[5fr_7fr]');
    const description = readFileSync(
      new URL('../../../lib/work/Description.svelte', import.meta.url),
      'utf8'
    );
    const ledger = readFileSync(
      new URL('../../../lib/work/EvidenceLedger.svelte', import.meta.url),
      'utf8'
    );
    // The column gutter belongs to the two-column layout only.
    expect(description).toContain('lg:pr-9');
    expect(description).not.toMatch(/\spr-9/);
    // A vertical rule down the middle of one stacked column is a rule across
    // the text; it becomes the hairline between the two blocks.
    expect(ledger).toContain('border-t border-hair pt-[22px] lg:border-t-0 lg:border-l lg:pt-0');
    expect(ledger).toContain('lg:pl-9');
    expect(ledger).not.toMatch(/\spl-9/);
  });
});

describe('two-line titles (D12)', () => {
  it('clamps the title to two lines instead of cutting it at one', () => {
    expect(row).toContain('line-clamp-2 text-15');
    expect(row).not.toContain('truncate');
  });

  it('keeps the two lines inside the artboard’s 56px row, and grows a row only for its reason', () => {
    // prose-15 is 15px at a 1.5 line height (DESIGN.md), so two lines are 45px
    // and fit the row builder's `h-14` with 11px to spare: unlike the Monitor
    // and Epics rows, this one does not have to grow to wrap, and the 56px
    // rhythm of the artboard's list is kept at every width.
    expect(2 * 15 * 1.5).toBeLessThanOrEqual(56);
    expect(markup).not.toContain('h-auto');
    // #2848 P14 — a row with a reason line keeps BOTH title lines over it: 45px,
    // 2px of gap and a 13px line at 1.5 do not fit 56, so that row, and only
    // that row (`TWO_LINE`), grows from a 56px floor.
    expect(2 * 15 * 1.5 + 2 + 13 * 1.5).toBeGreaterThan(56);
    expect(page).toMatch(/const TWO_LINE =\s*'h-auto min-h-14 /);
    expect(page).not.toContain('-webkit-line-clamp');
  });

  it('narrows the STATE column to its mark and its longest word', () => {
    const template = /const COLUMNS = '([^']+)'/.exec(page)?.[1];
    expect(template).toBeTruthy();
    const cells = template!.split(/ (?![^(]*\))/);
    expect(cells).toHaveLength(3);
    const state = parseInt(cells[2], 10);

    // IBM Plex Mono advances 600/1000 em, and label-11 tracks +0.14em. The
    // tracking lands after every glyph INCLUDING the last, and that trailing
    // step is part of the inline BOX whatever it looks like — so it is budgeted
    // for. Discounting it is what left the 88px track a quarter of a pixel
    // short of `NEEDS YOU`: the word is a shrinkable flex item with a space in
    // it, and a flex item one pixel short of its content breaks at the space
    // rather than overflowing. There is no credit to take for a trailing
    // letter-space being invisible.
    // #2898 — and Chromium on Linux snaps each 6.6px advance to a whole 7px:
    // the 390 sweep (`ui-checks/status-words.mjs`) read `IN REVIEW` at 76.875px
    // in a cell budgeted for 73.26, cut. The budget takes the rounded advance.
    const ink = (word: string) => word.length * (Math.ceil(11 * 0.6) + 11 * 0.14);
    const words = [
      ...Object.values(EXECUTION_STATE_WORD),
      ...Object.values(EXCEPTION_STATE_WORD),
      ...Object.values(ATTENTION_STATE_WORD)
    ]
      .filter((w) => w !== '')
      .map(stateWord);
    const widest = words.reduce((a, b) => (ink(b) > ink(a) ? b : a));
    // `needs you` and `in review` are the two longest, and tie at nine.
    expect(ink(widest)).toBe(ink('needs you'));
    expect(ink(widest)).toBe(ink('in review'));

    const MARK = 7; // the 7px mark of the DWork row
    const GAP = 8; // `class="gap-2"` on the mark, as the artboard spaces it
    // #2848 P19 — and a merge decision's `in review` wears the needs-you
    // diamond beside it (7px, `mr-2`): at 92 that word broke over two lines.
    const DIAMOND = 7 + 8;
    const need = DIAMOND + MARK + GAP + ink(widest);
    // The number this pins: 7 + 8 + 7 + 8 + 76.86 = 106.86px of content.
    expect(need).toBeCloseTo(106.86, 2);
    expect(state).toBeGreaterThanOrEqual(need);
    // …and it is the narrowest 4px step that holds it: the point of the
    // narrowing is the pixels it hands the title.
    expect(state - 4).toBeLessThan(need);
    expect(state).toBeLessThan(112);
    // 88 is what the discounted budget allowed, and it does not fit.
    expect(state).toBeGreaterThan(88);
  });
});

describe('one status vocabulary (D13)', () => {
  it('prints the state as a label-11 word from `stateWord`', () => {
    const cell = tag(row, 'flex justify-end');
    expect(cell).toContain('text-11');
    // Not the 13px data size the cell used to borrow from the No. column:
    // DESIGN.md gives status words to label-11.
    expect(cell).not.toContain('text-13');
    expect(row).toContain('label={stateWord(row.state)}');
    // The mark is still decided by the status table, in the run vocabulary.
    const mark = tag(row, 'kind={row.state}');
    expect(mark).toContain('vocabulary="run"');
    expect(mark).toContain('size={7}');
    expect(mark).toContain('word');
  });

  it('takes the word from the model, never from the markup', () => {
    expect(page).toContain('stateWord');
    // text-11 is what uppercases it (app.css), so no second casing anywhere.
    expect(row).not.toContain('toUpperCase');
    expect(row).not.toContain('uppercase');
  });
});

describe('j/k keeps the selected row in view (C5)', () => {
  it('scrolls the row the keyboard just selected, by the least that works', () => {
    const fn = page.slice(page.indexOf('function revealRow'));
    const body = fn.slice(0, fn.indexOf('\n  }'));
    expect(body).toContain("scrollIntoView({ block: 'nearest' })");
    // Found inside the list's own scroller, by a selector the rows carry.
    expect(body).toContain('listPane');
    expect(body).toContain('data-item=');
    expect(markup).toContain('data-item={row.itemId}');
    expect(tag(markup, 'aria-label="Issues"')).toContain('bind:this={listPane}');
  });

  it('is called for a keyboard move, and only after the move is taken', () => {
    const branch = page.slice(page.indexOf("if (intent === 'up' || intent === 'down')"));
    const body = branch.slice(0, branch.indexOf("} else if (intent === 'open')"));
    expect(body.indexOf('selectId(to)')).toBeLessThan(body.indexOf('revealRow(to)'));
    // A press that moved nothing must not scroll: the early return is first.
    expect(body.indexOf('return;')).toBeLessThan(body.indexOf('selectId(to)'));
  });

  it('does not scroll on a mouse press, which is a row already on screen', () => {
    const click = page.slice(page.indexOf('function select(event: MouseEvent'));
    expect(click.slice(0, click.indexOf('\n  }'))).not.toContain('revealRow');
  });
});

/**
 * #2281 D2 — the keyboard honours the one column.
 *
 * `needsListColumn` decides WHETHER a press has to bring the list back and is
 * unit-tested in `keys.test.ts`; what is held here is that the page asks it,
 * with the measured width, and does the two things the answer implies.
 */
describe('the list controls, below 1024 (D2)', () => {
  const handler = page.slice(page.indexOf('function onIntent'));
  const body = handler.slice(0, handler.indexOf('\n  }\n'));

  it('measures the viewport, rather than guessing at the breakpoint', () => {
    expect(markup).toContain('bind:innerWidth={viewportW}');
    expect(page).toContain('let viewportW = $state(0)');
    // The breakpoint itself is `keys.ts`'s constant, so the handler and the
    // `lg:` prefixes in the markup below cannot drift to two numbers.
    expect(page).not.toMatch(/viewportW\s*[<>]=?\s*1024/);
  });

  it('asks `needsListColumn` with the measured width and the selection', () => {
    expect(body).toContain('needsListColumn(intent, viewportW, selected !== null)');
    expect(page).toContain("needsListColumn");
  });

  it('brings the list back in the SAME write as the filter it sets', () => {
    // Two `replaceState` calls in one tick and the second builds its URL from
    // state the first has not settled into: whichever landed second would drop
    // the other's change, so `f` below 1024 would clear the item and lose the
    // filter, or narrow the list and leave it hidden.
    expect(body).toContain("setFilter(filter === 'needs-you' ? 'all' : 'needs-you', reveal)");
    const fn = page.slice(page.indexOf('function setFilter'));
    const set = fn.slice(0, fn.indexOf('\n  }'));
    expect(set).toContain("if (alsoClearItem) url.searchParams.delete('item')");
    expect(set.match(/write\(url\)/g)).toHaveLength(1);
    // The menu's own buttons still take the default, which touches no item.
    expect(markup).toContain('onclick={() => setFilter(option)}');
  });

  it('focuses the search field only once the list is back in the column', () => {
    // `focus()` on an input inside a `display:none` subtree is a no-op, so the
    // press that un-hid the list cannot focus in the same breath. The wait is
    // on the selection being gone — the thing that actually puts the field in
    // flow — and not on a tick count.
    expect(body).toContain('searchWanted = true');
    const effect = page.slice(page.indexOf('if (!searchWanted'));
    expect(effect.slice(0, effect.indexOf('\n  }'))).toContain('selected !== null) return');
    expect(page).toContain('searchWanted = false');
  });

  it('leaves every other key out of it', () => {
    // `up`/`down` write the selection, and below 1024 the column they fill is
    // the detail of the item they moved to — one column, so moving the
    // selection and showing it are the same act, exactly as a tap is. `open`
    // and `copy` act on that same pane, and `clear` is the way back.
    const move = body.slice(0, body.indexOf("} else if (intent === 'copy')"));
    expect(move).not.toContain('needsListColumn');
    expect(move).not.toContain('viewportW');
  });
});
