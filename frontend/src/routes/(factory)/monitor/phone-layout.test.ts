/**
 * #2282 (D3) — the Monitor at 390px.
 *
 * The design review found three things wrong with this screen on a phone: the
 * four readouts were clipped (four 44px figures and a 40px gutter do not fit
 * across 390px), the live-runs headers collided with the cells under them, and
 * the holds column — five twelfths of a 390px screen — wrapped one word per
 * line beside the stream. DESIGN.md's Responsive Behavior already says where
 * the last one goes: "Below 1024 … Monitor's holds move above the stream".
 *
 * Asserted as markup, for the reason `factory-header.test.ts` gives: no CI run
 * can resize a window, so the numbers a 390px pass would catch are held
 * against the utilities the components actually carry. The operator's browser
 * check at 390×844 is the other half.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { dataRowClasses, readoutClasses, TAP_INLINE, TAP_PILL } from '$lib/factory/classes';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
const liveRuns = readFileSync(
  new URL('../../../lib/monitor/LiveRuns.svelte', import.meta.url),
  'utf8'
);

describe('the readouts below 1024', () => {
  it('folds the four cells to 2×2 and keeps the artboard ratio above it', () => {
    expect(page).toContain(
      'grid grid-cols-2 border-y border-hair lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.35fr)_minmax(0,1.35fr)]'
    );
  });

  it('takes the figure one step down the same five-size scale', () => {
    // 26 and 44 are both in the scale (app.css). A sixth size would not be.
    expect(readoutClasses().figure.split(' ')).toContain('text-26');
    expect(readoutClasses().figure.split(' ')).toContain('lg:text-44');
  });

  it('halves the page gutter, which was a fifth of a 390px screen', () => {
    expect(page).toContain('overflow-y-auto px-4 pt-8 pb-9 sm:px-10');
  });

  it('moves each cell’s flush edge and divider to where the 2×2 grid puts it', () => {
    // Row 1: Working is flush left and loses its right padding to the divider;
    // Needs you carries the divider and is flush right.
    expect(page).toContain('class="min-w-0 pr-4 lg:pr-7"');
    expect(page).toContain('class="min-w-0 pr-0 pl-4 lg:pr-7 lg:pl-7"');
    // Row 2: Today starts the row (no divider, flush left) under a rule, and
    // This week keeps the divider it has at both widths.
    expect(page).toContain(
      "'min-w-0 border-t border-l-0 border-hair pr-4 pl-0 lg:border-t-0 lg:border-l lg:pr-7 lg:pl-7'"
    );
    expect(page).toContain("'min-w-0 border-t border-hair pl-4 lg:border-t-0 lg:pl-7'");
  });
});

describe('the lower band below 1024', () => {
  it('stacks the two columns, holds first', () => {
    expect(page).toContain('grid min-h-0 grow grid-cols-1 border-t border-hair lg:grid-cols-[5fr_7fr]');
    // Holds is the first child of that grid, so stacking puts it above.
    expect(page.indexOf('<Holds')).toBeLessThan(page.indexOf('<EventStream'));
  });

  it('turns the hairline between them, rather than ruling one screen edge', () => {
    expect(page).toContain(
      'min-w-0 border-t border-hair pt-[22px] lg:border-t-0 lg:border-l lg:pl-10'
    );
    // …and the holds column's gutter goes with the column it separated.
    expect(page).toContain('min-w-0 pt-[22px] pb-6 lg:pr-10 lg:pb-0');
  });
});

/**
 * Both components reach their phone layout by OVERRIDING a shared builder, and
 * `cn` is tailwind-merge: an override that lands in a group the builder has
 * already filled is dropped rather than applied. These pin the resolved class
 * list, because a dropped `flex` is a six-column grid on a 390px screen and no
 * markup assertion above would notice.
 */
describe('the overrides survive the merge', () => {
  it('leaves the live-runs row a flex below lg and a grid above it', () => {
    const row = dataRowClasses({
      rule: 'quiet',
      class: 'flex h-auto flex-wrap gap-2 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0'
    }).split(' ');
    expect(row).toContain('flex');
    expect(row).not.toContain('grid');
    expect(row).toContain('lg:grid');
    expect(row).toContain('h-auto');
    expect(row).not.toContain('h-14');
    expect(row).toContain('lg:h-14');
    expect(row).not.toContain('gap-5');
    expect(row).toContain('gap-2');
    // …and the row still rules itself off from the next one.
    expect(row).toContain('border-b');
  });

  it('takes the column heads off the phone entirely', () => {
    const head = dataRowClasses({ head: true, class: 'hidden lg:grid' }).split(' ');
    expect(head).toContain('hidden');
    expect(head).not.toContain('grid');
    expect(head).toContain('lg:grid');
  });

  it('moves a readout’s divider and flush edge without losing either', () => {
    const today = readoutClasses({
      divided: true,
      class: 'border-t border-l-0 border-hair pr-4 pl-0 lg:border-t-0 lg:border-l lg:pr-7 lg:pl-7'
    }).root.split(' ');
    expect(today).not.toContain('border-l');
    expect(today).toContain('border-l-0');
    expect(today).toContain('lg:border-l');
    expect(today).toContain('border-t');
    expect(today).toContain('lg:border-t-0');
    // `px-7` survives the merge (tailwind-merge does not drop an axis for one
    // side), and Tailwind orders `pl`/`pr` after `px`, so the sides win.
    expect(today).toContain('pl-0');
    expect(today).toContain('pr-4');
  });
});

describe('the live runs below 1024', () => {
  it('drops the column heads, which name columns that are not there', () => {
    expect(liveRuns).toContain("dataRowClasses({ head: true, class: 'hidden lg:grid' })");
  });

  it('wraps a row onto two lines and lets it grow to hold them', () => {
    expect(liveRuns).toContain(
      "class: 'flex h-auto flex-wrap gap-2 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0'"
    );
    // The break is a decision, not an overflow: a zero-height full-width item
    // after the State cell, gone from the grid above `lg`.
    expect(liveRuns).toContain('<span class="order-4 w-full lg:hidden" aria-hidden="true"></span>');
  });

  it('puts the run and its clock on the first line, what it is doing on the second', () => {
    // 1·2·3 before the break, 5·6·7 after it — and every one of them back to
    // the DOM order the heads name from 1024 up.
    for (const order of ['order-1', 'order-2', 'order-3', 'order-5', 'order-6', 'order-7']) {
      expect(liveRuns, order).toContain(order);
    }
    // Seven: the six cells, plus the Item cell's second branch (a run that
    // names no item is a span rather than a link).
    expect(liveRuns.split('lg:order-none')).toHaveLength(8);
    // Elapsed is what pushes the clock to the right edge of the first line.
    expect(liveRuns).toContain('order-2 ml-auto text-right font-mono text-13 text-text-3');
  });

  it('lets a long stage, runner or posture shrink instead of pushing the row wide', () => {
    for (const cell of ['stageLine(row)', 'row.runner', 'row.posture']) {
      const at = liveRuns.indexOf(cell);
      expect(liveRuns.slice(at - 220, at), cell).toContain('min-w-0 truncate');
    }
  });
});

/**
 * #2282 (D25) — "every control is at least 32px tall" at 390px (DESIGN.md).
 *
 * The operator browser check at 390×844 measured the six range chips at 22px
 * and the issue refs in the live runs and holds rows at 19–20px. Both are now
 * taken from the two shared rules in `classes.ts`, so this walks every control
 * in the Monitor's own markup and holds it to one of them: a chip, pill or
 * select takes the height; a word in a sentence takes the padding, which is
 * what an inline box is tapped by without moving the line it sits in.
 */
describe('the controls below sm (D25)', () => {
  const eventStream = readFileSync(
    new URL('../../../lib/monitor/EventStream.svelte', import.meta.url),
    'utf8'
  );
  const holds = readFileSync(new URL('../../../lib/monitor/Holds.svelte', import.meta.url), 'utf8');
  // No control of its own today — the brush is a drag surface — but it is one
  // of the four files that draw this screen, so a button added here is caught.
  const overview = readFileSync(
    new URL('../../../lib/monitor/OverviewTrack.svelte', import.meta.url),
    'utf8'
  );

  /**
   * The opening tag starting at `from`, brace- and quote-aware: `onclick={() =>
   * …}` puts a `>` inside the attribute list, so the tag cannot end at the
   * first one found.
   */
  function openingTag(src: string, from: number): string {
    let depth = 0;
    let quote = '';
    for (let i = from; i < src.length; i++) {
      const ch = src[i];
      if (quote !== '') {
        if (ch === quote) quote = '';
      } else if (ch === '"' || ch === "'" || ch === '`') {
        quote = ch;
      } else if (ch === '{') {
        depth += 1;
      } else if (ch === '}') {
        depth -= 1;
      } else if (ch === '>' && depth === 0) {
        return src.slice(from, i + 1);
      }
    }
    throw new Error(`unterminated tag at ${from}`);
  }

  /** Every `<button>`, `<a>` and `<select>` the file opens. */
  function controls(src: string): string[] {
    const tags: string[] = [];
    for (const m of src.matchAll(/<(?:button|a|select)[\s>]/g)) {
      tags.push(openingTag(src, m.index));
    }
    return tags;
  }

  /**
   * How a control is allowed to say it has a press target: either of the two
   * shared rules by name or spelled out, or one of the local constants built
   * out of them (asserted below, so a name here is not taken on trust).
   */
  const RULES = ['TAP_PILL', 'TAP_INLINE', TAP_PILL, TAP_INLINE, 'CHIP', 'OLDER', 'SELECT'];

  it('states both rules in terms a browser gives 32px for', () => {
    // A box takes the height; `sm:` hands the artboard's 22px pill back.
    expect(TAP_PILL).toBe('min-h-8 sm:min-h-0');
    // 10px either side of an 11–15px line clears 32px, and vertical padding
    // on an inline box does not enter the line height, so nothing reflows.
    expect(TAP_INLINE).toBe('py-2.5 sm:py-0');
  });

  it('gives every chip, pill and select on the stream its height', () => {
    // The six range chips the check measured, the kind toggles and the item
    // pill all come from one constant, so one rule reaches all of them.
    expect(eventStream).toContain(
      "'inline-flex h-[22px] cursor-pointer items-center gap-1.5 rounded-full border px-2.5 font-mono text-[11px] ' +\n    TAP_PILL"
    );
    // Two constants on this strip — the chips, which the epic filter now draws
    // too (#3468 D33), and the Load older pill — and the rule is the last
    // thing each of them says.
    expect(eventStream.match(/\n\s+TAP_PILL;/g)).toHaveLength(2);
  });

  it('gives the issue refs in both row tables a press target', () => {
    // Live runs: a cell of its own, so it takes the height.
    expect(liveRuns).toContain('order-1 inline-flex items-center font-mono text-13 text-text-1');
    expect(liveRuns).toContain('hover:underline lg:order-none ${TAP_PILL}');
    // Holds: a ref inside a sentence, so it takes the padding.
    expect(holds).toContain('text-text-1 underline-offset-2 hover:underline ${TAP_INLINE}');
  });

  it('leaves no control on the Monitor without one of the two rules', () => {
    // The walk has to find them before it can hold them to anything: the
    // stream alone opens eight controls, and each of the other three files one.
    expect(controls(eventStream).length).toBeGreaterThanOrEqual(8);
    for (const src of [page, liveRuns, holds]) {
      expect(controls(src).length).toBeGreaterThanOrEqual(1);
    }
    for (const src of [page, eventStream, liveRuns, holds, overview]) {
      for (const tag of controls(src)) {
        expect(
          RULES.some((rule) => tag.includes(rule)),
          tag
        ).toBe(true);
      }
    }
  });
});
