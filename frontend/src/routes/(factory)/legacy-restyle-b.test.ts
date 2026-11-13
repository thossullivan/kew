/**
 * FUI-28 — Budget, Config and Scorecard on the factory's hairline primitives.
 *
 * The epic's visual check is a headless-Chrome screenshot compared by eye
 * (`npm run shoot -- /budget DMonitor`), which no CI run performs. The numbers
 * that check would catch are in the DMonitor markup, so they are asserted the
 * way `work/list-geometry.test.ts` asserts DWork's: the artboard is read as the
 * source of truth, each measurement is matched against the utility the page
 * actually carries, and each colour against the app.css token it comes from — a
 * screenshot cannot tell `#221c33` from `var(--hair)`, and only one of the two
 * switches theme.
 *
 * Narrow on purpose: the page frame, the readout and gauge grammar, the
 * hairline table, and the promise that nothing on these three pages encloses
 * itself in a Card or reaches past the five-step type scale any more. It is not
 * a rendering test (there is no component-render infra here — see
 * StatusMark.svelte), and it asserts no behaviour: the writes these pages
 * perform are held by `config-page.test.ts`, the money rule by
 * `monitor/readouts.test.ts` and `cap-bar.test.ts`, and the scorecard's cells by
 * `scorecard-headline.test.ts`.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { dataRowClasses, readoutClasses } from '$lib/factory/classes';

const artboard = readFileSync(
  new URL('../../../design/factory-ui/DMonitor.html', import.meta.url),
  'utf8'
);
const css = readFileSync(new URL('../../app.css', import.meta.url), 'utf8');

/** The dark-theme value of one `--token`, as app.css declares it. */
function token(name: string): string {
  return css.match(new RegExp(`--${name}:\\s*([^;]+);`))![1].trim();
}

const PAGES = ['budget', 'config', 'scorecard'] as const;

/** The pages whose frame narrows its gutter below 640 (#3457). */
const PHONE_GUTTER = new Set<string>(['budget', 'scorecard']);
const source = Object.fromEntries(
  PAGES.map((name) => [
    name,
    readFileSync(new URL(`./${name}/+page.svelte`, import.meta.url), 'utf8')
  ])
) as Record<(typeof PAGES)[number], string>;

/** Markup only: a page's comments name the legacy parts they stopped using. */
const markup = (src: string) => src.replace(/<!--[\s\S]*?-->/g, '');

describe.each(PAGES)('%s — the restyled frame', (name) => {
  const page = source[name];

  it('paints on DMonitor’s content frame', () => {
    // padding: 32px 40px 36px, gap: 36px — pt-8 / px-10 / pb-9, gap-9.
    expect(artboard).toContain(
      'padding: 32px 40px 36px; display: flex; flex-direction: column; gap: 36px'
    );
    // `min-h-0 flex-1 overflow-y-auto` joined the frame with #2278 C4: each of
    // these three is one long column, and the shell is now exactly one viewport
    // tall, so the column scrolls in its own pane rather than the document.
    // Budget and Scorecard keep that 40px gutter from 640 up and narrow it to
    // 16px below, as Monitor does, so a 390px screen has room for the column
    // (#3457). Config has not folded yet.
    const frame = PHONE_GUTTER.has(name)
      ? 'flex min-h-0 flex-1 flex-col gap-9 overflow-y-auto px-4 pt-8 pb-9 sm:px-10'
      : 'flex min-h-0 flex-1 flex-col gap-9 overflow-y-auto px-10 pt-8 pb-9';
    expect(page).toContain(frame);
  });

  it('separates with rules instead of enclosing in Cards', () => {
    expect(markup(page)).not.toMatch(/<Card[\s>]|<\/Card>/);
    expect(page).not.toContain("Card } from '$lib/ui'");
    expect(page).not.toContain('rounded-[var(--radius-lg)]');
    expect(page).not.toContain('bg-surface');
    // The artboard draws every separation as one hairline.
    expect(artboard).toContain('border-bottom: 1px solid #221c33');
    expect(token('hair')).toBe('#221c33');
  });

  it('says every size in the five-step scale', () => {
    // `text-[11px]` is the one exception the factory already makes: `.text-11`
    // uppercases and tracks out its text, which the gauge captions must not.
    const sizes = [...markup(page).matchAll(/(?<![\w-])text-(?!\[11px\])(?:\[[^\]]*\]|[a-z0-9-]+)/g)].map(
      (m) => m[0]
    );
    const allowed = new Set([
      'text-11',
      'text-13',
      'text-15',
      'text-26',
      'text-44',
      'text-text-1',
      'text-text-2',
      'text-text-3',
      'text-text-5',
      'text-accent',
      'text-accent-2',
      'text-warn',
      'text-danger',
      'text-left',
      'text-right',
      'text-ink'
    ]);
    for (const size of sizes) expect(allowed, `${name}: ${size}`).toContain(size);
  });

  it('reads the factory ink ramp, never the legacy card ramp', () => {
    for (const legacy of ['text-fg', 'text-muted', 'text-faint', 'border-border', 'eyebrow']) {
      expect(markup(page), legacy).not.toContain(legacy);
    }
  });

  it('names its own regions, and nests no second main landmark', () => {
    expect(markup(page)).not.toMatch(/<main[\s>]/);
    expect(page).toMatch(/aria-label=[{"]/);
  });
});

describe('budget — the spend readouts and their gauges (DMonitor y ≈88–258)', () => {
  const page = source.budget;

  it('draws the readout row on hairlines above and below, like the artboard', () => {
    expect(artboard).toContain(
      'aria-label="Readouts" style="display: grid; grid-template-columns: 1fr 1fr 1.35fr 1.35fr; border-top: 1px solid #221c33; border-bottom: 1px solid #221c33;'
    );
    expect(page).toContain('grid grid-cols-2 border-y border-hair');
    expect(page).toContain('<Readout');
    expect(page).toContain("edge={i === 0 ? 'start' : 'end'}");
  });

  it('borrows the money figure and the "/ $250" beside it from the readout', () => {
    expect(artboard).toContain('font-size: 44px; font-weight: 500; line-height: 1; color: #ffc94a');
    expect(artboard).toContain(
      'font-size: 15px; color: #8d84a3; font-weight: 400; margin-left: 10px;">/ $250'
    );
    expect(readoutClasses({ tone: 'money' }).figure).toContain('text-warn');
    expect(page).toContain("readoutClasses({ tone: 'money' }).figure");
    // From 640 up; below it the cap is the meta's and the gauge's to say (#3457).
    expect(page).toContain('ml-2.5 hidden text-15 font-normal text-text-3 sm:inline');
    expect(token('warn')).toBe('#ffc94a');
  });

  it('replaces CapBar with the ticked Gauge (FUI-06)', () => {
    // The page's own header comment still names what it replaced; the import
    // and the element are what must be gone.
    expect(page).not.toMatch(/<CapBar|CapBar[,}]/);
    expect(page).toContain('<Gauge spent={cell.gauge.spent} cap={cell.gauge.cap}');
    // 26px of headroom for a marked gauge, 8px for a bare one — as Monitor.
    expect(page).toContain("cell.gauge.marker ? '26px' : '8px'");
  });

  it('reads the enforced figure only, and never against the cap the governor keeps (N5)', () => {
    // Every word of the two cells comes from the shared builder, which reads
    // `agentic` and refuses `spent`; the page computes no money of its own.
    expect(page).toContain('spendReadout(');
    expect(page).toContain('priorCap(');
    // The only `spent` the page passes on is the builder's enforced figure.
    expect(page).not.toMatch(/(?:cell|observed|daily|weekly)\.spent\b/);
    expect(page).toContain('<Gauge spent={cell.gauge.spent}');
    // Interactive is stated beneath the gauge, never inside it.
    expect(page).toContain('view.observed.interactive');
    expect(page).toContain('observed · not enforced');
  });

  it('refreshes the whole gauge after a write, never half of it', () => {
    // A gauge's figure, cap tick and "% of cap" come from /api/report/spend
    // while its prior-cap pin comes from /api/config/budget, so a write that
    // re-read only the map pinned "prior cap $250" on a track whose own tick
    // still read $250. Both readings are taken by one function now, and the
    // load payload is a seed the page may not keep reading from.
    expect(page).not.toMatch(/\bfetchBudget\(|\bfetchSpend\(/);
    expect(page).toContain('await readBudgetPage()');
    expect([...page.matchAll(/await reread\(\)/g)]).toHaveLength(2); // save, revert
    expect([...page.matchAll(/\bdata\.spend\b/g)]).toHaveLength(1); // the $state seed
    expect(page).toContain('let windows = $state<Source<SpendWindows>>(data.spend)');
  });

  it('lists the three caps as one hairline table with an 11px head', () => {
    expect(page).toContain('<SectionHead label="Budget caps"');
    expect(page).toContain("const CAP_COLUMNS = ");
    // The head reads from 1024 up; below it each row folds to two lines and
    // labels its own kew.toml figure (#3457).
    expect(page).toContain(
      '<DataRow columns={CAP_COLUMNS} head class="hidden lg:grid" data-cap-head>'
    );
    expect(page).toContain('<span class="mr-2 lg:hidden">kew.toml</span>');
    expect(dataRowClasses({ head: true })).toContain('text-11');
    expect(page).toContain("rule={i === CAPS.length - 1 && editing !== cap.key ? 'none' : 'quiet'}");
  });

  it('offers each write as a quiet instrument button, and labels its fields', () => {
    expect(page).toContain('variant="factory"');
    expect(page).not.toContain('variant="primary"');
    expect(page).not.toContain('variant="outline"');
    expect(page).toContain('class="sr-only"');
    expect(page).toContain('aria-expanded');
  });
});

describe('config — the override rows', () => {
  const page = source.config;

  it('heads every section with the 11px section head', () => {
    expect(page).toContain('<SectionHead');
    expect(page).toContain('label={section.title}');
    expect(page).toContain('label={REVIEW_SECTION}');
  });

  it('says the layer in force as a coloured word, not a pill', () => {
    expect(page).not.toContain('<Badge');
    expect(page).toContain('SOURCE_TONE_CLASS');
  });

  it('keeps every write path the page had, dialogs included', () => {
    for (const fn of [
      'confirmOverride',
      'revertOverride',
      'confirmReviewOverride',
      'revertReviewOverride',
      'closeDialog',
      'closeReviewDialog'
    ]) {
      expect(page, fn).toContain(fn);
    }
    // The compare-and-swap the review write rides on is untouched.
    expect(page).toContain('ifGeneration: policy.generation');
  });

  // D45 (#3470): a dialog is a float, so its hairline is `hair-strong` and its
  // corners the popover's 11px.
  it('draws each dialog as a hairline box on the raised ink, not a Card', () => {
    expect(page).toContain('rounded-[11px] border border-hair-strong bg-ink-raised');
    expect(page).toContain('aria-modal="true"');
    expect(token('hair-strong')).toBe('#342a4a');
    expect(token('ink-raised')).toBe('#0d0a18');
  });
});

describe('scorecard — the same data, in the table grammar', () => {
  const page = source.scorecard;

  it('states the leverage headline as readouts, not a boxed banner', () => {
    expect(page).toContain('<Readout');
    expect(page).toContain("tone=\"money\"");
    expect(page).toContain('deriveHeadline');
  });

  it('draws the model table on the shared row builder', () => {
    expect(page).toContain('const SCORECARD_COLUMNS =');
    expect(page).toContain('<DataRow columns={SCORECARD_COLUMNS} head>');
    // The by-complexity rows are the artboard's tighter 50px row.
    expect(page).toContain('height={50}');
    expect(dataRowClasses({ height: 50 })).toContain('h-[50px]');
  });

  it('reads each cell from the shared formatters — one formula, not two', () => {
    for (const fn of ['mergeRate', 'perMerged', 'avgReviews', 'planningRatio', 'isLocalRow']) {
      expect(page, fn).toContain(fn);
    }
    // #2092: the model row keyed "local" off the model id while the headline
    // keyed it off the served channel (N11). One reading now.
    expect(page).not.toContain("startsWith('lemonade/')");
    expect(page).not.toContain('Math.round((100 * ');
  });

  it('keeps the window selector reachable, and says which one is showing', () => {
    expect(page).toContain('aria-label="Time window"');
    expect(page).toContain('aria-pressed={w === days}');
    expect(page).toContain('bg-ink-selected');
    expect(token('ink-selected')).toBe('#151025');
  });

  it('expands a model with a real button, not a clickable row', () => {
    expect(page).toContain('aria-expanded={expanded.has(r.model)}');
    expect(markup(page)).not.toMatch(/<tr[\s>]/);
  });
});
